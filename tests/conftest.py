import http.server
import json
import threading
import time
import urllib.parse
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated_xdg_config(monkeypatch, tmp_path_factory):
    """Redirects the XDG config fallback to an empty per-test directory so
    the suite never silently reads (or writes state.db paths pointed at by)
    whatever real ~/.config/xc-vod-dl/config.toml happens to exist on the
    machine running the tests — a config.toml a developer sets up for their
    own real server should never change what the test suite does."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("xdg-config")))


@pytest.fixture
def load_fixture():
    def _load(name: str):
        with (FIXTURES_DIR / name).open() as f:
            return json.load(f)

    return _load


class ConfigurableHandler(http.server.BaseHTTPRequestHandler):
    """A minimal HTTP server whose behavior a test can steer live via
    `server.test_state`, used to exercise resume/retry/throttle scenarios
    against xc_vod_dl.download.engine without any real Xtream server."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        state = self.server.test_state
        data = state["data"]
        total = len(data)
        range_header = self.headers.get("Range")
        state["requests"].append(range_header)
        mode = state["mode"]

        if mode == "fail_503":
            self.send_response(503)
            self.end_headers()
            return

        if mode == "fail_500":
            self.send_response(500)
            self.end_headers()
            return

        if mode == "flaky_once" and not state["flaky_triggered"]:
            state["flaky_triggered"] = True
            self.send_response(200)
            self.send_header("Content-Length", str(total))
            self.end_headers()
            self.wfile.write(data[: total // 2])
            self.close_connection = True
            return

        # Optional: track how many requests are simultaneously "in flight"
        # (held open via delay_s) so tests can assert on real overlap rather
        # than inferring it indirectly.
        concurrency_lock = state.get("concurrency_lock")
        if concurrency_lock is not None:
            with concurrency_lock:
                state["active"] = state.get("active", 0) + 1
                state["max_active"] = max(state.get("max_active", 0), state["active"])
        if state.get("delay_s"):
            time.sleep(state["delay_s"])

        start = 0
        status = 200
        if mode != "ignore_range" and range_header:
            start = int(range_header.split("=")[1].split("-")[0])
            status = 206

        if start >= total:
            if mode == "flaky_500_at_eof":
                # Real-world behavior seen against a live Xtream server: some
                # backend workers answer an at-EOF resume with a bogus 500
                # instead of 416, for the identical already-complete file.
                self.send_response(500)
                self.end_headers()
                return
            # Standard semantics: a Range request starting exactly at (or
            # past) EOF — i.e. resuming a file that's already fully on disk
            # but never got committed — comes back 416, not a 206 with zero
            # bytes.
            self.send_response(416)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        chunk = data[start:]
        self.send_response(status)
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{total - 1}/{total}")
        self.send_header("Content-Length", str(len(chunk)))
        self.end_headers()
        self.wfile.write(chunk)

        if concurrency_lock is not None:
            with concurrency_lock:
                state["active"] -= 1


@pytest.fixture
def media_server():
    data = (FIXTURES_DIR / "tiny.mp4").read_bytes()
    state = {
        "data": data,
        "mode": "normal",
        "flaky_triggered": False,
        "requests": [],
        "concurrency_lock": threading.Lock(),
        "active": 0,
        "max_active": 0,
        "delay_s": 0,
    }
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ConfigurableHandler)
    server.test_state = state
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}/tiny.mp4", state
    finally:
        server.shutdown()
        server.server_close()


class XtreamServerHandler(http.server.BaseHTTPRequestHandler):
    """Fakes just enough of a real Xtream Codes server (player_api.php JSON
    + range-capable /movie//series file serving) to drive the CLI end to end
    without touching any real service."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        state = self.server.test_state
        state["requests"].append(self.path)

        if parsed.path == "/player_api.php":
            action = query.get("action", [None])[0]
            if action is None:
                payload = state["account"]
            elif action == "get_vod_info":
                payload = state["vod_info"].get(query["vod_id"][0])
            elif action == "get_series_info":
                payload = state["series_info"].get(query["series_id"][0])
            elif action == "get_vod_categories":
                payload = state.get("vod_categories", [])
            elif action == "get_series_categories":
                payload = state.get("series_categories", [])
            elif action == "get_vod_streams":
                payload = state.get("vod_streams", [])
            elif action == "get_series":
                payload = state.get("series_streams", [])
            else:
                payload = {}
            if payload is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path.startswith("/movie/") or parsed.path.startswith("/series/"):
            data = state["media"]
            total = len(data)
            range_header = self.headers.get("Range")
            start, status = 0, 200
            if range_header:
                start = int(range_header.split("=")[1].split("-")[0])
                status = 206
            chunk = data[start:]
            self.send_response(status)
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{total - 1}/{total}")
            self.send_header("Content-Length", str(len(chunk)))
            self.end_headers()
            self.wfile.write(chunk)
            return

        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture
def xtream_server():
    media = (FIXTURES_DIR / "tiny.mp4").read_bytes()
    state = {
        "account": {
            "user_info": {
                "username": "demo",
                "status": "Active",
                "max_connections": "2",
                "active_cons": "0",
                "is_trial": "0",
            }
        },
        "vod_info": {},
        "series_info": {},
        "media": media,
        "requests": [],
    }
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), XtreamServerHandler)
    server.test_state = state
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}", state
    finally:
        server.shutdown()
        server.server_close()
