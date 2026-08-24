from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from enum import Enum, auto

logger = logging.getLogger(__name__)


class TransferOutcome(Enum):
    OK = auto()
    HTTP_THROTTLE = auto()  # 503/429, or a 403 arriving mid-stream after a 200 already started
    RATE_COLLAPSE = auto()  # aggregate throughput collapsed vs. the run's early baseline
    CONN_RESET = auto()  # connection reset/aborted mid-transfer
    TIMEOUT = auto()
    OTHER_ERROR = auto()


def initial_parallelism(
    max_connections: int, active_cons: int, safety_margin: int = 1, ceiling: int = 4
) -> int:
    """How many parallel transfers to start with, from the account's own numbers.

    Forces serial (1) whenever the account can't reliably support more than
    one stream, or when the numbers say there's no headroom left.
    """
    if max_connections <= 1:
        return 1
    available = max_connections - active_cons - safety_margin
    return max(1, min(available, ceiling))


class ConcurrencyController:
    """Sizes a pool of parallel transfers and adapts it to server-side pushback.

    Knows nothing about HTTP or ffprobe — it only deals in "slots" (acquire/
    release) and "outcomes" (report_outcome/record_bytes) reported by callers.

    - A single isolated CONN_RESET/TIMEOUT/OTHER_ERROR is treated as a genuine
      network hiccup: it breaks the recovery streak but does not shrink the
      pool — that's the caller's per-job retry's job.
    - HTTP_THROTTLE and RATE_COLLAPSE are treated as real overload signals and
      immediately step the limit down.
    - CONN_RESET events from >=2 distinct jobs within `reset_correlation_window_s`
      are also promoted to an overload signal — a single stream being reset is
      noise, several at once usually isn't.
    - Recovery is deliberately conservative: additive step-up (+1) only after
      `recovery_streak` consecutive clean transfers *and* the cooldown from the
      last step-down has elapsed; step-down is multiplicative (halved).

    The throughput-collapse check is a heuristic sampled over `sample_interval_s`
    windows fed by record_bytes() — it needs real tuning against your actual
    server's behavior (see config.toml's [concurrency] section); the values
    here are reasonable defaults, not measured constants.
    """

    def __init__(
        self,
        initial: int,
        *,
        minimum: int = 1,
        maximum: int = 4,
        cooldown_s: float = 30.0,
        recovery_streak: int = 3,
        collapse_floor_pct: float = 0.2,
        sample_interval_s: float = 5.0,
        collapse_confirm_windows: int = 2,
        reset_correlation_window_s: float = 5.0,
        now_fn: Callable[[], float] = time.monotonic,
    ):
        self.minimum = minimum
        self.maximum = maximum
        self.cooldown_s = cooldown_s
        self.recovery_streak = recovery_streak
        self.collapse_floor_pct = collapse_floor_pct
        self.sample_interval_s = sample_interval_s
        self.collapse_confirm_windows = collapse_confirm_windows
        self.reset_correlation_window_s = reset_correlation_window_s
        self.now_fn = now_fn

        self._cond = threading.Condition()
        self._current_limit = max(minimum, min(initial, maximum))
        self._active = 0
        self._clean_streak = 0
        self._cooldown_until = 0.0

        self._recent_resets: list[tuple[str, float]] = []

        self._stats_lock = threading.Lock()
        self._window_start = now_fn()
        self._window_bytes = 0
        self._baseline_bps: float | None = None
        self._collapse_streak = 0

    @property
    def current_limit(self) -> int:
        with self._cond:
            return self._current_limit

    def acquire(self) -> None:
        with self._cond:
            while self._active >= self._current_limit:
                self._cond.wait()
            self._active += 1

    def release(self) -> None:
        with self._cond:
            self._active = max(0, self._active - 1)
            self._cond.notify_all()

    def record_bytes(self, job_id: str, n: int) -> None:
        with self._stats_lock:
            self._window_bytes += n
            now = self.now_fn()
            elapsed = now - self._window_start
            if elapsed < self.sample_interval_s:
                return
            bps = self._window_bytes / elapsed
            self._window_start = now
            self._window_bytes = 0

        if self._baseline_bps is None:
            if bps > 0:
                self._baseline_bps = bps
                logger.info("concurrency: throughput baseline set to %.0f B/s", bps)
            return

        if bps < self._baseline_bps * self.collapse_floor_pct:
            self._collapse_streak += 1
            if self._collapse_streak >= self.collapse_confirm_windows:
                self._collapse_streak = 0
                self._baseline_bps = None  # re-baseline after adapting
                self.report_outcome(job_id, TransferOutcome.RATE_COLLAPSE)
        else:
            self._collapse_streak = 0

    def report_outcome(self, job_id: str, outcome: TransferOutcome) -> None:
        if outcome is TransferOutcome.OK:
            with self._cond:
                self._clean_streak += 1
            self._maybe_step_up()
            return

        if outcome is TransferOutcome.HTTP_THROTTLE:
            self._step_down("http_throttle")
            return

        if outcome is TransferOutcome.RATE_COLLAPSE:
            self._step_down("rate_collapse")
            return

        if outcome is TransferOutcome.CONN_RESET:
            now = self.now_fn()
            with self._cond:
                self._recent_resets = [
                    (j, t)
                    for j, t in self._recent_resets
                    if now - t <= self.reset_correlation_window_s
                ]
                self._recent_resets.append((job_id, now))
                distinct_jobs = {j for j, _ in self._recent_resets}
                self._clean_streak = 0
            if len(distinct_jobs) >= 2:
                self._step_down("correlated_conn_reset")
            return

        # TIMEOUT / OTHER_ERROR: an isolated hiccup — retried at the job level,
        # doesn't shrink the shared pool, but does interrupt the recovery streak.
        with self._cond:
            self._clean_streak = 0

    def _step_down(self, reason: str) -> None:
        with self._cond:
            new_limit = max(self.minimum, self._current_limit // 2)
            if new_limit != self._current_limit:
                logger.info(
                    "concurrency: step down %d -> %d (%s)", self._current_limit, new_limit, reason
                )
                self._current_limit = new_limit
                self._cond.notify_all()
            self._clean_streak = 0
            self._cooldown_until = self.now_fn() + self.cooldown_s

    def _maybe_step_up(self) -> None:
        with self._cond:
            if self._current_limit >= self.maximum:
                return
            if self.now_fn() < self._cooldown_until:
                return
            if self._clean_streak < self.recovery_streak:
                return
            self._current_limit += 1
            self._clean_streak = 0
            logger.info("concurrency: step up -> %d", self._current_limit)
            self._cond.notify_all()
