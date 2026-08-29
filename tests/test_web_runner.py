import asyncio

import pytest

from xc_vod_dl.api.client import XtreamClient
from xc_vod_dl.config import AccountConfig, Config
from xc_vod_dl.jobs import JobSpec
from xc_vod_dl.state.store import StateStore
from xc_vod_dl.web.runner import JobRunner

pytestmark = pytest.mark.integration


def test_run_batch_publishes_a_log_event_when_nothing_resolves(xtream_server):
    """A batch where every spec fails to resolve (e.g. an unknown vod_id)
    used to return silently — the web UI showed nothing at all happening,
    indistinguishable from the request never having been received."""
    base_url, _state = xtream_server
    client = XtreamClient(base_url, "demo", "demo")
    config = Config(account=AccountConfig(server=base_url, username="demo", password="demo"))
    state = StateStore(":memory:")
    runner = JobRunner(client, config, state)

    async def scenario():
        q = runner.subscribe(asyncio.get_running_loop())
        runner.submit([JobSpec(kind="movie", id=999999)])  # never registered -> 404
        event = await asyncio.wait_for(q.get(), timeout=5)
        assert event.type == "log"
        assert "nothing could be resolved" in event.data["message"]

    asyncio.run(scenario())
