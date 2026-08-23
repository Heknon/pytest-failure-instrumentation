"""Entry point. Registration only - no logic lives here.

Installed as a ``pytest11`` entry point, so ``pip install`` is the whole setup
and nothing has to be added to a conftest. Implement
``pytest_failure_incident`` to receive what it finds; without that the evidence
is still written to disk.

Disable entirely with ``-p no:failure_instrumentation``.

Registration is the one place this plugin touches the filesystem before
anything has gone wrong, and it is allowed to fail: a read-only image, a
vanished mount, a name already taken by a file. Every path below degrades to
*not registering* and a warning. An exception here becomes an INTERNALERROR
before a single test runs, which would mean the instrumentation was a worse
outcome than the failures it exists to explain.
"""

from __future__ import annotations

import warnings
from typing import Any, Callable

import pytest

from . import hookspec

# Imported by function, not as a module: the hookimpl below must keep the
# parameter name `config`, because pytest matches hook arguments by name.
from .config import FailureInstrumentationWarning, add_options, resolve


def pytest_addhooks(pluginmanager: pytest.PytestPluginManager) -> None:
    pluginmanager.add_hookspecs(hookspec)


def pytest_addoption(parser: pytest.Parser) -> None:
    add_options(parser)


def _built(build: Callable[[], Any], what: str) -> Any | None:
    """Whatever ``build`` returns, or None and a warning."""
    try:
        return build()
    except Exception as failure:  # noqa: BLE001 - see the module docstring
        warnings.warn(
            f"failure instrumentation is off for this {what}: {failure!r}",
            FailureInstrumentationWarning,
            stacklevel=2,
        )
        return None


def pytest_configure(config: pytest.Config) -> None:
    settings = _built(lambda: resolve(config), "run")
    if settings is None:
        return

    if hasattr(config, "workerinput"):
        from .capture.recorder import WorkerRecorder

        worker_id = config.workerinput["workerid"]
        recorder = _built(
            lambda: WorkerRecorder(settings.directory, worker_id, settings), "worker"
        )
        if recorder is None:
            return
        config.pluginmanager.register(recorder, "failure-instrumentation-worker")
        return

    # Registered whether or not xdist is in the picture. Two of the three
    # sources are plain pytest - an internal error ends any run, distributed or
    # not, and the run summary is what says a run reached its end at all. Only
    # worker deaths need xdist, and that hookimpl declares itself optional so
    # its spec being absent is not an error.
    from .incidents.engine import IncidentEngine

    engine = _built(lambda: IncidentEngine(config, settings), "run")
    if engine is None:
        return
    config.pluginmanager.register(engine, "failure-instrumentation-controller")
