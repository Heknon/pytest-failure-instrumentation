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
    assert "No heartbeat was written" in verdict.reason


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
    assert "holds the GIL" in verdict.reason


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


def test_an_unmeasurable_cpu_rate_is_not_a_measured_zero():
    """One beat gives no rate at all. Treating that as "burned nothing" turns
    the absence of a measurement into the evidence the verdict rests on - and
    a worker at full tilt produces exactly this input when its beats collide.
    """
    verdict = stall.assess(
        beats((99.0, 1.0)), now=100.0, silent_for=60.0, interval=5.0
    )
    assert verdict.state == "BLOCKED"
    assert verdict.cpu_rate is None
    # Same verdict, half the evidence, and it has to say so.
    assert verdict.confidence == "low"
    assert "no CPU figure could be measured" in verdict.reason


def test_a_zero_length_window_is_not_a_measured_zero_either():
    # 98 CPU-seconds between two beats stamped at the same instant.
    verdict = stall.assess(
        beats((99.0, 1.0), (99.0, 99.0)), now=100.0, silent_for=60.0, interval=5.0
    )
    assert verdict.state == "BLOCKED"
    assert verdict.confidence == "low"


def test_a_measured_zero_keeps_the_confidence_it_earns():
    verdict = stall.assess(
        beats((90.0, 5.0), (100.0, 5.0)), now=100.0, silent_for=60.0, interval=5.0
    )
    assert verdict.cpu_rate == 0.0
    assert verdict.confidence is None  # the state's own confidence stands


def test_only_this_run_s_events_are_this_run_s_evidence():
    """An earlier run's beats are old by definition, and old beats are exactly
    what FROZEN is read off. A record with no run id at all cannot be placed
    either way and is kept: dropping it reports a worker whose controller never
    reached it with an id as one that never beat at all."""
    from pytest_failure_instrumentation.capture import events

    log = [
        {"event": "heartbeat", "run_id": "run-earlier"},
        {"event": "heartbeat", "run_id": "run-now"},
        {"event": "heartbeat", "run_id": None},
    ]

    kept = events.this_run(log, "run-now")
    assert [event.get("run_id") for event in kept] == ["run-now", None]
    assert events.this_run(log, None) == log
