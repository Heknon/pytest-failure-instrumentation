"""Entry point. Registration only - no logic lives here.

Installed as a ``pytest11`` entry point, so ``pip install`` is the whole setup
and nothing has to be added to a conftest. Implement
``pytest_failure_incident`` to receive what it finds; without that the evidence
is still written to disk.

Disable entirely with ``-p no:failure_instrumentation``.
"""

from __future__ import annotations

import pytest

from . import hookspec
# Imported by function, not as a module: the hookimpl below must keep the
# parameter name `config`, because pytest matches hook arguments by name.
from .config import add_options, resolve


def pytest_addhooks(pluginmanager: pytest.PytestPluginManager) -> None:
    pluginmanager.add_hookspecs(hookspec)


def pytest_addoption(parser: pytest.Parser) -> None:
    add_options(parser)


def pytest_configure(config: pytest.Config) -> None:
    settings = resolve(config)

    if hasattr(config, "workerinput"):
        from .capture.recorder import WorkerRecorder

        worker_id = config.workerinput["workerid"]
        config.pluginmanager.register(
            WorkerRecorder(settings.directory, worker_id, settings),
            "failure-instrumentation-worker",
        )
        return

    # Registered whether or not xdist is in the picture. Two of the three
    # sources are plain pytest - an internal error ends any run, distributed or
    # not, and the run summary is what says a run reached its end at all. Only
    # worker deaths need xdist, and that hookimpl declares itself optional so
    # its spec being absent is not an error.
    from .incidents.engine import IncidentEngine

    config.pluginmanager.register(
        IncidentEngine(config, settings), "failure-instrumentation-controller"
    )
