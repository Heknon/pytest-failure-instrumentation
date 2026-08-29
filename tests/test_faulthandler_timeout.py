"""pytest's own ``faulthandler_timeout``, which this plugin must not take over.

There is exactly one ``faulthandler.dump_traceback_later`` timer per process,
and arming it cancels whatever was armed before. pytest's faulthandler plugin
arms it at the start of every test when that ini is set; this plugin's
frozen-interpreter fallback re-arms it every second. Whoever armed last owns
it, and the fallback always armed last - so a user who configured a timeout got
a plugin that silently threw it away, along with the
``faulthandler_exit_on_timeout`` that was meant to end a hung run.

The fallback stands down where the ini is set. That costs a stall its
frozen-fallback stack, which is a worse report; the alternative is a run that
hangs past a timeout somebody configured, which is a worse run.
"""

from __future__ import annotations

from .conftest import INNER_CONFTEST, needs_xdist

pytestmark = needs_xdist

HANGING_SUITE = """
import time


def test_filler():
    assert True


def test_takes_longer_than_the_timeout():
    time.sleep(12)
"""


def test_a_configured_timeout_still_fires_in_a_worker(distributed):
    distributed.pytester.makeini(
        """
        [pytest]
        failure_packages = victim
        faulthandler_timeout = 4
        """
    )
    distributed.pytester.makeconftest(INNER_CONFTEST)
    distributed.pytester.makepyfile(test_hang=HANGING_SUITE)

    distributed.run(
        "-n", "1", "-o", "failure_heartbeat_interval=1", "test_hang.py", timeout=180
    )

    output = distributed.result.stderr.str() + distributed.result.stdout.str()
    assert "Timeout (0:00:04)" in output, (
        "the worker's frozen-fallback re-arming cancelled pytest's own timer"
    )


def test_standing_down_is_recorded_rather_than_silent(distributed):
    """A reader who finds no .frozen stack on a stalled worker needs to be able
    to find out why, and "somebody configured faulthandler_timeout" is not
    something they could deduce from its absence."""
    distributed.pytester.makeini(
        """
        [pytest]
        failure_packages = victim
        faulthandler_timeout = 60
        """
    )
    distributed.pytester.makeconftest(INNER_CONFTEST)
    distributed.pytester.makepyfile(
        test_quick="""
        def test_one():
            assert True
        """
    )

    distributed.run("-n", "1", "test_quick.py", timeout=180)

    # One directory per run, named for the session - see "Two runs at once".
    evidence = distributed.pytester.path / ".pytest-failures"
    events = "".join(
        path.read_text(encoding="utf-8") for path in evidence.glob("*/*.events")
    )
    assert "frozen_fallback_stood_down" in events
    assert not list(evidence.glob("*/*.frozen"))
