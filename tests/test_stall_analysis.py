"""The stall state machine, decided from beats alone.

These are the cases that matter and that an integration test cannot pin down:
a slow test must produce no verdict at all, and a missed beat must not be
mistaken for a frozen process without confirmation.
"""

from __future__ import annotations

from pytest_failure_instrumentation.analysis import stall


def beats(*pairs):
    return [{"time": time, "cpu_seconds": cpu} for time, cpu in pairs]


def test_no_beats_is_silent_not_blocked():
    verdict = stall.assess([], now=100.0, silent_for=60.0, interval=5.0)
    assert verdict.state == "SILENT"
    assert "never wrote a heartbeat" in verdict.reason


def test_burning_cpu_is_not_a_stall():
    # Ten seconds of wall clock, ten seconds of CPU: busy, not stuck.
    verdict = stall.assess(
        beats((90.0, 10.0), (100.0, 20.0)), now=100.0, silent_for=60.0, interval=5.0
    )
    assert verdict.state is None
    assert verdict.cpu_rate is not None and verdict.cpu_rate >= 1.0


def test_heartbeat_alive_without_cpu_is_blocked():
    verdict = stall.assess(
        beats((90.0, 5.0), (100.0, 5.0)), now=100.0, silent_for=60.0, interval=5.0
    )
    assert verdict.state == "BLOCKED"
    assert verdict.cpu_rate == 0.0


def test_a_stale_beat_is_not_concluded_on_first_pass():
    verdict = stall.assess(
        beats((50.0, 5.0), (60.0, 5.0)), now=100.0, silent_for=60.0, interval=5.0
    )
    assert verdict.state is None
    assert verdict.needs_confirmation is True


def test_confirmation_with_no_new_beat_is_frozen():
    verdict = stall.confirm(
        beats((50.0, 5.0), (60.0, 5.0)), previous_last=60.0, now=106.0, silent_for=60.0
    )
    assert verdict.state == "FROZEN"
    assert "holding the GIL" in verdict.reason


def test_confirmation_with_a_new_beat_falls_through_to_cpu():
    verdict = stall.confirm(
        beats((100.0, 5.0), (106.0, 5.0)), previous_last=60.0, now=106.0, silent_for=60.0
    )
    assert verdict.state == "BLOCKED"


def test_cpu_rate_needs_two_beats():
    assert stall.cpu_rate(beats((100.0, 5.0))) is None
    assert stall.cpu_rate([]) is None


def test_cpu_rate_of_a_zero_length_window_is_unmeasurable():
    # Not zero: "we could not tell" and "it burned nothing" are different.
    assert stall.cpu_rate(beats((100.0, 5.0), (100.0, 5.0))) is None
