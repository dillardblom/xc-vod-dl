from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from xc_vod_dl.exceptions import VerificationError

VerifyMode = Literal["quick", "full"]


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    duration_s: float | None = None
    reason: str | None = None


def resolve_tool_path(configured: str | None, tool_name: str) -> str:
    """Autodetect via PATH when no explicit path is configured."""
    if configured:
        return configured
    found = shutil.which(tool_name)
    if not found:
        raise VerificationError(f"'{tool_name}' not found on PATH and no explicit path configured")
    return found


def verify_media(
    path: Path,
    mode: VerifyMode = "quick",
    ffprobe_path: str | None = None,
    ffmpeg_path: str | None = None,
    timeout_s: float = 120.0,
) -> VerifyResult:
    """Check that a downloaded media file is actually playable, not just present.

    "quick" inspects container metadata (fast, catches truncation/unreadable
    headers). "full" fully decodes the stream (slow, also catches corruption
    buried mid-file that a container-level check can miss).
    """
    if mode == "quick":
        tool = resolve_tool_path(ffprobe_path, "ffprobe")
        return _verify_quick(path, tool, timeout_s)
    if mode == "full":
        tool = resolve_tool_path(ffmpeg_path, "ffmpeg")
        return _verify_full(path, tool, timeout_s)
    raise ValueError(f"unknown verify mode: {mode!r}")


def _verify_quick(path: Path, ffprobe_path: str, timeout_s: float) -> VerifyResult:
    try:
        proc = subprocess.run(
            [
                ffprobe_path,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return VerifyResult(ok=False, reason="ffprobe timed out")

    stderr = proc.stderr.strip()
    if proc.returncode != 0:
        return VerifyResult(ok=False, reason=stderr or f"ffprobe exited {proc.returncode}")
    if stderr:
        return VerifyResult(ok=False, reason=stderr)

    stdout = proc.stdout.strip()
    try:
        duration = float(stdout)
    except ValueError:
        return VerifyResult(ok=False, reason=f"could not parse duration from ffprobe output: {stdout!r}")

    if duration <= 0:
        return VerifyResult(ok=False, duration_s=duration, reason="duration is zero or negative")
    return VerifyResult(ok=True, duration_s=duration)


def _verify_full(path: Path, ffmpeg_path: str, timeout_s: float) -> VerifyResult:
    try:
        proc = subprocess.run(
            [ffmpeg_path, "-v", "error", "-i", str(path), "-f", "null", "-"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return VerifyResult(ok=False, reason="ffmpeg timed out")

    stderr = proc.stderr.strip()
    if proc.returncode != 0 or stderr:
        return VerifyResult(ok=False, reason=stderr or f"ffmpeg exited {proc.returncode}")
    return VerifyResult(ok=True)
