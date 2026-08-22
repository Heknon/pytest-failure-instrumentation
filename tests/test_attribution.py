"""Whose code is on the stack. Paths are built by hand so the answers do not
depend on where the runner installed anything."""

from __future__ import annotations

import sysconfig

from pytest_failure_instrumentation.analysis.attribution import Attributor

# Taken from the interpreter rather than written down: a runner installs Python
# wherever it likes, and a hardcoded /usr/lib path only looks like the stdlib
# on the machine it was written on.
STDLIB = sysconfig.get_paths()["stdlib"].replace("\\", "/")


def frames_for(*paths):
    return [f'  File "{path}", line 4 in do_something' for path in paths]


def test_the_stdlib_is_runtime_wherever_it_is_installed():
    assert Attributor(("yourcore",)).owner_of(f"{STDLIB}/ctypes/__init__.py") == "runtime"


def test_a_frame_in_your_package_is_yours():
    attributor = Attributor(("yourcore",))
    assert attributor.owner_of("/srv/app/yourcore/engine.py") == "product"


def test_a_dash_in_a_distribution_name_still_matches_the_module():
    attributor = Attributor(("your-core",))
    assert attributor.owner_of("/srv/app/your_core/engine.py") == "product"


def test_an_installed_dependency_is_third_party():
    attributor = Attributor(("yourcore",))
    assert (
        attributor.owner_of("/usr/lib/python3.11/site-packages/numpy/core.py")
        == "third-party"
    )


def test_the_frameworks_are_runtime():
    attributor = Attributor(("yourcore",))
    for path in (
        "/usr/lib/python3.11/site-packages/_pytest/runner.py",
        "/usr/lib/python3.11/site-packages/xdist/scheduler/loadscope.py",
        "/usr/lib/python3.11/site-packages/execnet/gateway_base.py",
        "/usr/lib/python3.11/site-packages/pluggy/_callers.py",
    ):
        assert attributor.owner_of(path) == "runtime", path


def test_anything_else_belongs_to_whoever_wrote_the_tests():
    attributor = Attributor(("yourcore",))
    assert attributor.owner_of("/home/someone/suite/test_api.py") == "customer-code"


def test_blame_walks_outward_past_the_runtime():
    # The deepest frame is ctypes, which tells nobody anything.
    attributor = Attributor(("yourcore",))
    blame = attributor.blame(
        frames_for(
            f"{STDLIB}/ctypes/__init__.py",
            "/srv/app/yourcore/engine.py",
            "/home/someone/suite/test_api.py",
        )
    )
    assert blame["owner"] == "product"
    assert blame["blamed_frame"]["module"] == "engine"
    assert blame["top_frame"]["module"] == "__init__"


def test_a_stack_with_no_owned_frame_is_a_finding_not_a_shrug():
    attributor = Attributor(("yourcore",))
    blame = attributor.blame(
        frames_for(
            "/usr/lib/python3.11/site-packages/execnet/gateway_base.py",
            "/usr/lib/python3.11/site-packages/xdist/scheduler/loadscope.py",
        )
    )
    assert blame["owner"] == "runtime"
    # Points at the framework, not the stdlib call it happened to make.
    assert blame["blamed_frame"]["module"] in {"gateway_base", "loadscope"}


def test_no_frames_at_all_is_unknown():
    blame = Attributor(("yourcore",)).blame([])
    assert blame == {"top_frame": None, "blamed_frame": None, "owner": "unknown"}


def test_a_traceback_is_read_outermost_first():
    attributor = Attributor(("yourcore",))
    reversed_blame = attributor.blame(
        frames_for(
            "/home/someone/suite/test_api.py",
            "/srv/app/yourcore/engine.py",
            f"{STDLIB}/ctypes/__init__.py",
        ),
        reverse=True,
    )
    assert reversed_blame["top_frame"]["module"] == "__init__"
