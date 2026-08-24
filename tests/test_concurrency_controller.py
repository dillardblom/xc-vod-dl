import threading
import time

from xc_vod_dl.download.concurrency import (
    ConcurrencyController,
    TransferOutcome,
    initial_parallelism,
)


class FakeClock:
    def __init__(self, start: float = 0.0):
        self._now = start

    def now(self) -> float:
        return self._now

    def tick(self, seconds: float) -> None:
        self._now += seconds


# --- initial_parallelism ---------------------------------------------------


def test_initial_parallelism_forces_serial_when_max_connections_is_one():
    assert initial_parallelism(max_connections=1, active_cons=0) == 1


def test_initial_parallelism_forces_serial_when_max_connections_is_zero():
    assert initial_parallelism(max_connections=0, active_cons=0) == 1


def test_initial_parallelism_leaves_safety_margin():
    assert initial_parallelism(max_connections=5, active_cons=1, safety_margin=1, ceiling=4) == 3


def test_initial_parallelism_respects_ceiling():
    assert initial_parallelism(max_connections=10, active_cons=0, safety_margin=1, ceiling=4) == 4


def test_initial_parallelism_floors_at_one_when_no_headroom():
    assert initial_parallelism(max_connections=3, active_cons=3, safety_margin=1, ceiling=4) == 1


# --- acquire/release ---------------------------------------------------


def test_acquire_release_enforces_current_limit():
    controller = ConcurrencyController(initial=2, maximum=4)
    active = 0
    max_active = 0
    lock = threading.Lock()

    def worker():
        nonlocal active, max_active
        controller.acquire()
        try:
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.03)
            with lock:
                active -= 1
        finally:
            controller.release()

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert max_active <= 2


# --- step down on explicit overload signals ---------------------------------


def test_http_throttle_halves_the_limit():
    clock = FakeClock()
    controller = ConcurrencyController(initial=4, maximum=4, now_fn=clock.now)
    controller.report_outcome("job-1", TransferOutcome.HTTP_THROTTLE)
    assert controller.current_limit == 2


def test_step_down_floors_at_minimum():
    clock = FakeClock()
    controller = ConcurrencyController(initial=1, minimum=1, maximum=4, now_fn=clock.now)
    controller.report_outcome("job-1", TransferOutcome.HTTP_THROTTLE)
    assert controller.current_limit == 1


def test_repeated_throttle_steps_down_multiple_times():
    clock = FakeClock()
    controller = ConcurrencyController(initial=4, minimum=1, maximum=4, now_fn=clock.now)
    controller.report_outcome("job-1", TransferOutcome.HTTP_THROTTLE)
    assert controller.current_limit == 2
    controller.report_outcome("job-1", TransferOutcome.HTTP_THROTTLE)
    assert controller.current_limit == 1


# --- isolated hiccups vs. correlated resets ---------------------------------


def test_isolated_conn_reset_does_not_step_down():
    clock = FakeClock()
    controller = ConcurrencyController(initial=4, maximum=4, now_fn=clock.now)
    controller.report_outcome("job-1", TransferOutcome.CONN_RESET)
    assert controller.current_limit == 4


def test_isolated_timeout_does_not_step_down():
    clock = FakeClock()
    controller = ConcurrencyController(initial=4, maximum=4, now_fn=clock.now)
    controller.report_outcome("job-1", TransferOutcome.TIMEOUT)
    assert controller.current_limit == 4


def test_correlated_conn_resets_from_two_jobs_steps_down():
    clock = FakeClock()
    controller = ConcurrencyController(
        initial=4, maximum=4, reset_correlation_window_s=5.0, now_fn=clock.now
    )
    controller.report_outcome("job-1", TransferOutcome.CONN_RESET)
    clock.tick(1.0)
    controller.report_outcome("job-2", TransferOutcome.CONN_RESET)
    assert controller.current_limit == 2


def test_conn_resets_outside_correlation_window_do_not_correlate():
    clock = FakeClock()
    controller = ConcurrencyController(
        initial=4, maximum=4, reset_correlation_window_s=5.0, now_fn=clock.now
    )
    controller.report_outcome("job-1", TransferOutcome.CONN_RESET)
    clock.tick(10.0)  # well outside the correlation window
    controller.report_outcome("job-2", TransferOutcome.CONN_RESET)
    assert controller.current_limit == 4


def test_repeated_resets_from_same_job_do_not_self_correlate():
    clock = FakeClock()
    controller = ConcurrencyController(
        initial=4, maximum=4, reset_correlation_window_s=5.0, now_fn=clock.now
    )
    controller.report_outcome("job-1", TransferOutcome.CONN_RESET)
    clock.tick(1.0)
    controller.report_outcome("job-1", TransferOutcome.CONN_RESET)
    assert controller.current_limit == 4


# --- recovery / step up ---------------------------------


def test_step_up_requires_cooldown_elapsed_even_with_a_full_streak():
    clock = FakeClock()
    controller = ConcurrencyController(
        initial=4, maximum=4, recovery_streak=3, cooldown_s=30.0, now_fn=clock.now
    )
    controller.report_outcome("job-1", TransferOutcome.HTTP_THROTTLE)  # -> 2, cooldown starts
    assert controller.current_limit == 2

    # A full recovery streak arriving before the cooldown elapses must not step up yet.
    for _ in range(3):
        controller.report_outcome("job-1", TransferOutcome.OK)
    assert controller.current_limit == 2

    clock.tick(31.0)  # cooldown elapsed — the still-accumulating streak is now enough
    controller.report_outcome("job-1", TransferOutcome.OK)
    assert controller.current_limit == 3


def test_step_up_requires_full_recovery_streak_after_cooldown():
    clock = FakeClock()
    controller = ConcurrencyController(
        initial=4, maximum=4, recovery_streak=3, cooldown_s=5.0, now_fn=clock.now
    )
    controller.report_outcome("job-1", TransferOutcome.HTTP_THROTTLE)  # -> 2, cooldown starts
    clock.tick(6.0)  # cooldown elapsed, but streak is still 0

    controller.report_outcome("job-1", TransferOutcome.OK)
    controller.report_outcome("job-1", TransferOutcome.OK)
    assert controller.current_limit == 2  # only 2 clean transfers so far

    controller.report_outcome("job-1", TransferOutcome.OK)  # 3rd clean transfer
    assert controller.current_limit == 3


def test_step_up_never_exceeds_maximum():
    clock = FakeClock()
    controller = ConcurrencyController(
        initial=4, maximum=4, recovery_streak=1, cooldown_s=0.0, now_fn=clock.now
    )
    for _ in range(10):
        controller.report_outcome("job-1", TransferOutcome.OK)
    assert controller.current_limit == 4


def test_hiccup_breaks_recovery_streak():
    clock = FakeClock()
    controller = ConcurrencyController(
        initial=2, maximum=4, recovery_streak=2, cooldown_s=0.0, now_fn=clock.now
    )
    controller.report_outcome("job-1", TransferOutcome.OK)
    controller.report_outcome("job-1", TransferOutcome.TIMEOUT)  # breaks the streak
    controller.report_outcome("job-1", TransferOutcome.OK)
    assert controller.current_limit == 2  # only 1 clean transfer since the hiccup


# --- throughput collapse via record_bytes ---------------------------------


def test_rate_collapse_detected_after_confirm_windows():
    clock = FakeClock()
    controller = ConcurrencyController(
        initial=4,
        maximum=4,
        sample_interval_s=5.0,
        collapse_floor_pct=0.2,
        collapse_confirm_windows=2,
        now_fn=clock.now,
    )

    # Establish baseline: 5s window, 5000 bytes -> 1000 B/s baseline.
    clock.tick(5.0)
    controller.record_bytes("job-1", 5000)
    assert controller.current_limit == 4  # baseline window only, no verdict yet

    # Two consecutive collapsed windows (well under 20% of baseline).
    clock.tick(5.0)
    controller.record_bytes("job-1", 50)  # 10 B/s, collapse streak = 1
    assert controller.current_limit == 4

    clock.tick(5.0)
    controller.record_bytes("job-1", 50)  # collapse streak = 2 -> triggers step down
    assert controller.current_limit == 2


def test_healthy_throughput_does_not_trigger_collapse():
    clock = FakeClock()
    controller = ConcurrencyController(
        initial=4, maximum=4, sample_interval_s=5.0, collapse_floor_pct=0.2, now_fn=clock.now
    )
    clock.tick(5.0)
    controller.record_bytes("job-1", 5000)  # baseline: 1000 B/s
    clock.tick(5.0)
    controller.record_bytes("job-1", 4800)  # ~960 B/s, well above floor
    assert controller.current_limit == 4


def test_single_collapsed_window_alone_does_not_step_down():
    clock = FakeClock()
    controller = ConcurrencyController(
        initial=4,
        maximum=4,
        sample_interval_s=5.0,
        collapse_floor_pct=0.2,
        collapse_confirm_windows=2,
        now_fn=clock.now,
    )
    clock.tick(5.0)
    controller.record_bytes("job-1", 5000)  # baseline
    clock.tick(5.0)
    controller.record_bytes("job-1", 50)  # 1 collapsed window only
    assert controller.current_limit == 4
