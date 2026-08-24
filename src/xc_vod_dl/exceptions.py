class XcVodDlError(Exception):
    """Base class for all xc-vod-dl errors."""


class XtreamAPIError(XcVodDlError):
    """The Xtream API returned an unexpected or malformed response."""


class AuthError(XtreamAPIError):
    """The server rejected the configured credentials."""


class VerificationError(XcVodDlError):
    """A downloaded file failed ffprobe/ffmpeg integrity verification."""


class DownloadError(XcVodDlError):
    """A media transfer failed (network error or unexpected HTTP status).

    `kind` classifies the failure for the concurrency controller:
    "timeout" | "conn_reset" | "throttle" | "other".
    """

    def __init__(self, message: str, *, kind: str = "other"):
        super().__init__(message)
        self.kind = kind


class ThrottledError(XcVodDlError):
    """A transfer was rejected or collapsed in a way that indicates server-side throttling."""


class ConfigError(XcVodDlError):
    """Configuration or credentials could not be resolved."""
