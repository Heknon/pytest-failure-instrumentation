"""Attribute pytest-xdist worker failures that would otherwise leave no trace.

A worker that is killed, crashes natively, stalls, or disagrees about which
tests exist produces almost nothing today: xdist reports ``node down: Not
properly terminated``, and a stall or a collection mismatch reports nothing at
all. This records what a dying process cannot say afterwards, and raises one
hook per incident with an owner attached.

Installing it is a ``pip install``; it registers itself as a ``pytest11`` entry
point and reads its settings from ini. A framework that wraps pytest and
computes its own settings installs it by hand instead::

    from pytest_failure_instrumentation import Settings, install

    def pytest_configure(config):
        install(config, packages=("yourcore",), directory=evidence_dir)

Nothing here imports pydantic. The incident models do, and they are loaded on
the controller only, once something has already gone wrong - so a worker's
per-test path is unaffected by any of it.
"""

from .config import Settings
from .registration import install, installed_settings

__version__ = "0.2.0"

__all__ = ["Settings", "__version__", "install", "installed_settings"]
