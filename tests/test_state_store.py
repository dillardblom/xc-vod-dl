import pytest

from xc_vod_dl.state.store import StateStore


@pytest.fixture
def store():
    with StateStore(":memory:") as s:
        yield s


def test_upsert_pending_creates_record(store):
    store.upsert_pending(id="movie:101", kind="movie", title="Example Movie", target_path="/x/y.mkv")
    record = store.get("movie:101")
    assert record is not None
    assert record.status == "pending"
    assert record.attempts == 0
    assert record.bytes_downloaded == 0


def test_upsert_pending_is_idempotent(store):
    store.upsert_pending(id="movie:101", kind="movie", title="Example Movie", target_path="/x/y.mkv")
    store.mark_status("movie:101", "downloading", bytes_downloaded=500)
    # Re-registering the same id (e.g. re-running the same manifest) must not
    # clobber in-progress state.
    store.upsert_pending(id="movie:101", kind="movie", title="Example Movie", target_path="/x/y.mkv")
    record = store.get("movie:101")
    assert record.status == "downloading"
    assert record.bytes_downloaded == 500


def test_mark_status_updates_fields(store):
    store.upsert_pending(id="ep:9001", kind="episode", title="Pilot", target_path="/x/e1.mkv")
    store.mark_status("ep:9001", "failed", last_error="timeout", increment_attempts=True)
    record = store.get("ep:9001")
    assert record.status == "failed"
    assert record.last_error == "timeout"
    assert record.attempts == 1


def test_mark_status_increment_attempts_accumulates(store):
    store.upsert_pending(id="ep:9001", kind="episode", title="Pilot", target_path="/x/e1.mkv")
    store.mark_status("ep:9001", "failed", increment_attempts=True)
    store.mark_status("ep:9001", "failed", increment_attempts=True)
    assert store.get("ep:9001").attempts == 2


def test_list_incomplete_excludes_done_and_skipped(store):
    store.upsert_pending(id="a", kind="movie", title="A", target_path="/a.mkv")
    store.upsert_pending(id="b", kind="movie", title="B", target_path="/b.mkv")
    store.upsert_pending(id="c", kind="movie", title="C", target_path="/c.mkv")
    store.mark_status("a", "done")
    store.mark_status("b", "skipped")
    store.mark_status("c", "failed")
    incomplete_ids = {r.id for r in store.list_incomplete()}
    assert incomplete_ids == {"c"}


def test_list_by_status_filters_correctly(store):
    store.upsert_pending(id="a", kind="movie", title="A", target_path="/a.mkv")
    store.upsert_pending(id="b", kind="movie", title="B", target_path="/b.mkv")
    store.mark_status("a", "done")
    done = store.list_by_status("done")
    assert [r.id for r in done] == ["a"]


def test_get_missing_id_returns_none(store):
    assert store.get("nope") is None
