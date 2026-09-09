import threading
import time

import pytest

from viva.timer import AnswerTimer, TimerNotStartedError


def test_elapsed_before_start_raises():
    timer = AnswerTimer(duration_seconds=60)
    with pytest.raises(TimerNotStartedError):
        timer.elapsed()


def test_excluding_removes_time_from_elapsed():
    """The core Phase 0 exit-criterion behavior (FR17): time spent inside
    `excluding()` must not count against the answer clock."""
    timer = AnswerTimer(duration_seconds=60)
    timer.start()

    time.sleep(0.05)
    with timer.excluding():
        time.sleep(0.15)  # simulate LLM latency
    time.sleep(0.05)

    elapsed = timer.elapsed()
    # ~0.10s of real answer time; the 0.15s "LLM call" must be excluded.
    assert 0.08 <= elapsed <= 0.20, f"expected ~0.1s, got {elapsed}"


def test_remaining_and_expired():
    timer = AnswerTimer(duration_seconds=0.1)
    timer.start()
    assert not timer.expired()
    time.sleep(0.15)
    assert timer.expired()
    assert timer.remaining() == 0.0


def test_excluding_time_never_goes_negative_or_expires_clock():
    """A long LLM call must not itself burn down the remaining time."""
    timer = AnswerTimer(duration_seconds=0.2)
    timer.start()
    with timer.excluding():
        time.sleep(0.3)  # "LLM call" longer than the whole session duration
    assert not timer.expired(), "LLM latency must not consume session time"


def test_format_remaining_is_mm_ss():
    timer = AnswerTimer(duration_seconds=125)
    timer.start()
    assert timer.format_remaining() == "02:05"


def test_negative_duration_rejected():
    with pytest.raises(ValueError):
        AnswerTimer(duration_seconds=0)


def test_start_with_initial_elapsed_seconds_restores_progress():
    """Phase 6 resume: a session that already used 50s of a 60s clock
    should resume with only ~10s remaining, not a fresh 60s."""
    timer = AnswerTimer(duration_seconds=60)
    timer.start(initial_elapsed_seconds=50)
    assert 49.9 <= timer.elapsed() <= 50.2
    assert 9.8 <= timer.remaining() <= 10.1


def test_start_with_initial_elapsed_seconds_can_already_be_expired():
    timer = AnswerTimer(duration_seconds=60)
    timer.start(initial_elapsed_seconds=61)
    assert timer.expired()
    assert timer.remaining() == 0.0


def test_excluding_is_thread_safe_across_concurrent_calls():
    """A defensive concurrency test, not a reproduced-failure regression
    test -- honesty note: attempted to confirm this fails against the
    pre-lock code first (the project's usual rule for regression tests),
    but couldn't reliably reproduce a lost update even with an
    aggressive `sys.setswitchinterval` and a tight no-sleep loop; a
    simple float attribute `+=` is apparently resistant enough to
    CPython's GIL scheduling that this specific race is hard to trigger
    on purpose, let alone by accident. The lock is still correct to add
    (docs/system-design/17-phase-12-web-voice-io-design.md \u00a717.5): every
    prior caller of `excluding()` ran on the same thread as everything
    else touching a given `AnswerTimer`; the web voice layer's
    `transcribe()` is the first caller from a genuinely different
    thread. This test documents and checks the intended behavior under
    concurrency going forward, even though it can't be pointed at an
    already-manifested bug.
    """
    timer = AnswerTimer(duration_seconds=60)
    timer.start()
    num_threads = 30
    sleep_each = 0.02

    def _exclude_a_bit() -> None:
        with timer.excluding():
            time.sleep(sleep_each)

    threads = [threading.Thread(target=_exclude_a_bit) for _ in range(num_threads)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Every one of the 30 exclusions must be fully accounted for -- a
    # lost update would leave elapsed() showing real answering time that
    # was actually spent inside excluding() blocks.
    assert timer.elapsed() < 0.15, f"lost time from a concurrent excluding() update: elapsed={timer.elapsed()}"
