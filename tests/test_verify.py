import shutil

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
