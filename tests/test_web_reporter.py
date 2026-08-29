import asyncio
import threading

from xc_vod_dl.web.reporter import EventBus, ProgressEvent, WebProgressReporter


def test_event_bus_delivers_events_published_from_another_thread():
    """The core fix: EventBus.publish() is called from JobRunner's plain
    background thread, not the event loop thread — a subscriber's queue
    must still receive it via call_soon_threadsafe. A blocking queue.Queue
    bridged in via run_in_executor "worked" for this too, but couldn't be
    cancelled cleanly on shutdown (the actual bug this replaced)."""

    async def scenario():
        bus = EventBus()
        q = bus.subscribe(asyncio.get_running_loop())

        def publish_from_thread():
            bus.publish(ProgressEvent("start", "movie:1", {"title": "Example"}))

        t = threading.Thread(target=publish_from_thread)
        t.start()
        t.join()

        event = await asyncio.wait_for(q.get(), timeout=5)
        assert event.type == "start"
        assert event.job_id == "movie:1"
        assert event.data == {"title": "Example"}

    asyncio.run(scenario())


def test_event_bus_unsubscribe_stops_delivery():
    async def scenario():
        bus = EventBus()
        q = bus.subscribe(asyncio.get_running_loop())
        bus.unsubscribe(q)

        bus.publish(ProgressEvent("start", "movie:1", {}))

        assert q.empty()

    asyncio.run(scenario())


def test_event_bus_publish_survives_a_closed_event_loop():
    """A download can finish reporting progress after the server has
    started shutting down — publish() must not raise into the caller
    (WebProgressReporter, and beyond that DownloadEngine's own callback
    chain) just because there's no one left to receive it."""
    loop = asyncio.new_event_loop()
    bus = EventBus()
    bus.subscribe(loop)
    loop.close()

    bus.publish(ProgressEvent("complete", "movie:1", {"ok": True}))  # must not raise


def test_web_progress_reporter_start_updates_jobs_and_publishes():
    jobs: dict = {}
    published = []
    reporter = WebProgressReporter(jobs, published.append)

    reporter.start("movie:1", "Example Movie")

    assert jobs["movie:1"] == {
        "title": "Example Movie",
        "completed": 0,
        "total": None,
        "status": "downloading",
    }
    assert published[-1].type == "start"
    assert published[-1].job_id == "movie:1"


def test_web_progress_reporter_report_accumulates_completed():
    jobs: dict = {}
    published = []
    reporter = WebProgressReporter(jobs, published.append)

    reporter.start("movie:1", "Example Movie")
    reporter.report("movie:1", 100)
    reporter.report("movie:1", 50)

    assert jobs["movie:1"]["completed"] == 150
    assert published[-1].data == {"completed": 150}


def test_web_progress_reporter_set_total():
    jobs: dict = {}
    reporter = WebProgressReporter(jobs, lambda e: None)

    reporter.start("movie:1", "Example Movie")
    reporter.set_total("movie:1", 1000)

    assert jobs["movie:1"]["total"] == 1000


def test_web_progress_reporter_complete_success_and_failure():
    jobs: dict = {}
    reporter = WebProgressReporter(jobs, lambda e: None)

    reporter.start("movie:1", "A")
    reporter.complete("movie:1", True)
    assert jobs["movie:1"]["status"] == "done"

    reporter.start("movie:2", "B")
    reporter.complete("movie:2", False)
    assert jobs["movie:2"]["status"] == "failed"
