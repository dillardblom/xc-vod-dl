import shutil
import subprocess
from unittest.mock import patch

import pytest

from xc_vod_dl.download.verify import resolve_tool_path, verify_media
from xc_vod_dl.exceptions import VerificationError

pytestmark = pytest.mark.skipif(
    shutil.which("ffprobe") is None or shutil.which("ffmpeg") is None,
    reason="ffprobe/ffmpeg not available on PATH",
)

FIXTURES = None  # set in fixtures below


@pytest.fixture(autouse=True)
def _fixtures_dir():
    global FIXTURES
    from pathlib import Path

    FIXTURES = Path(__file__).parent / "fixtures"


def test_verify_quick_passes_for_valid_file():
    result = verify_media(FIXTURES / "tiny.mp4", mode="quick")
    assert result.ok is True
    assert result.duration_s is not None and result.duration_s > 0


def test_verify_quick_fails_for_truncated_file():
    result = verify_media(FIXTURES / "tiny_truncated.mp4", mode="quick")
    assert result.ok is False
    assert result.reason


def test_verify_full_passes_for_valid_file():
    result = verify_media(FIXTURES / "tiny.mp4", mode="full")
    assert result.ok is True


def test_verify_full_fails_for_truncated_file():
    result = verify_media(FIXTURES / "tiny_truncated.mp4", mode="full")
    assert result.ok is False


def test_verify_quick_accepts_a_file_with_a_benign_stderr_warning():
    """Regression test: found on a real Xtream server (Dispatcharr) — a fully,
    correctly downloaded MP4 can make ffprobe print an `error`-level message
    for an unrelated QuickTime chapter-track reference while still exiting 0
    and reporting a valid duration. That must NOT fail verification — it did,
    before this fix, and caused a good download to be discarded and re-tried
    forever."""
    fake_proc = subprocess.CompletedProcess(
        args=["ffprobe"],
        returncode=0,
        stdout="5820.0\n",
        stderr="[mov,mp4,m4a,3gp,3g2,mj2 @ 0x0] Referenced QT chapter track not found\n",
    )
    with patch("xc_vod_dl.download.verify.subprocess.run", return_value=fake_proc):
        result = verify_media(FIXTURES / "tiny.mp4", mode="quick", ffprobe_path="/fake/ffprobe")

    assert result.ok is True
    assert result.duration_s == 5820.0
    assert result.warning and "chapter track" in result.warning


def test_verify_full_accepts_a_file_with_a_benign_stderr_warning():
    fake_proc = subprocess.CompletedProcess(
        args=["ffmpeg"],
        returncode=0,
        stdout="",
        stderr="[mov,mp4,m4a,3gp,3g2,mj2 @ 0x0] Referenced QT chapter track not found\n",
    )
    with patch("xc_vod_dl.download.verify.subprocess.run", return_value=fake_proc):
        result = verify_media(FIXTURES / "tiny.mp4", mode="full", ffmpeg_path="/fake/ffmpeg")

    assert result.ok is True
    assert result.warning and "chapter track" in result.warning


def test_verify_quick_still_fails_on_nonzero_exit_despite_parseable_stdout():
    fake_proc = subprocess.CompletedProcess(
        args=["ffprobe"], returncode=1, stdout="5820.0\n", stderr="some real error\n"
    )
    with patch("xc_vod_dl.download.verify.subprocess.run", return_value=fake_proc):
        result = verify_media(FIXTURES / "tiny.mp4", mode="quick", ffprobe_path="/fake/ffprobe")

    assert result.ok is False
    assert result.reason == "some real error"


def test_verify_media_rejects_unknown_mode():
    with pytest.raises(ValueError):
        verify_media(FIXTURES / "tiny.mp4", mode="bogus")


def test_resolve_tool_path_uses_explicit_path_when_given():
    assert resolve_tool_path("/custom/ffprobe", "ffprobe") == "/custom/ffprobe"


def test_resolve_tool_path_autodetects_via_which():
    found = resolve_tool_path(None, "ffprobe")
    assert found  # exact path depends on the environment


def test_resolve_tool_path_raises_when_not_found():
    with pytest.raises(VerificationError):
        resolve_tool_path(None, "definitely-not-a-real-binary-xyz")
