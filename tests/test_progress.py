from xc_vod_dl.ui.progress import ProgressReporter


def _task(reporter: ProgressReporter, job_id: str):
    task_id = reporter._task_ids[job_id]
    return next(t for t in reporter._progress.tasks if t.id == task_id)


def test_start_is_required_before_a_job_gets_a_visible_row():
    with ProgressReporter() as reporter:
        assert reporter._progress.tasks == []
        reporter.start("movie:1", "Example")
        assert len(reporter._progress.tasks) == 1
        assert _task(reporter, "movie:1").fields["title"] == "Example"


def test_report_and_set_total_before_start_are_a_harmless_noop():
    with ProgressReporter() as reporter:
        # No start() call yet — must not raise (e.g. a stray callback firing
        # for a job that hasn't reached the front of a serial/parallel queue).
        reporter.report("movie:1", 100)
        reporter.set_total("movie:1", 500)
        assert reporter._progress.tasks == []


def test_report_advances_the_started_task():
    with ProgressReporter() as reporter:
        reporter.start("movie:1", "Example")
        reporter.report("movie:1", 50)
        reporter.report("movie:1", 25)
        assert _task(reporter, "movie:1").completed == 75


def test_new_task_starts_with_unknown_total_and_pulses():
    with ProgressReporter() as reporter:
        reporter.start("movie:1", "Example")
        task = _task(reporter, "movie:1")
        assert task.total is None
        assert task.started is True  # started immediately -> elapsed clock is real


def test_complete_forces_a_definite_full_bar_even_without_a_known_total():
    with ProgressReporter() as reporter:
        reporter.start("movie:1", "Example")
        reporter.report("movie:1", 42)  # total never becomes known
        reporter.complete("movie:1", True)
        task = _task(reporter, "movie:1")
        assert task.total == 42
        assert task.completed == 42
        assert task.finished is True  # bar stops pulsing, spinner freezes
        assert "done" in task.fields["status"]


def test_complete_on_failure_marks_failed_status():
    with ProgressReporter() as reporter:
        reporter.start("movie:1", "Example")
        reporter.complete("movie:1", False)
        assert "failed" in _task(reporter, "movie:1").fields["status"]


def test_complete_before_start_is_a_harmless_noop():
    with ProgressReporter() as reporter:
        reporter.complete("movie:1", True)  # no start() call — must not raise
        assert reporter._progress.tasks == []
