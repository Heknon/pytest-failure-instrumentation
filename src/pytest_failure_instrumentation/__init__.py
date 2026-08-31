"""Attribute the pytest failures that would otherwise leave no trace.

A run that is killed, crashes natively, stalls, or - under xdist - disagrees
about which tests exist produces almost nothing today: xdist reports ``node
down: Not properly terminated``, and a stall or a collection mismatch reports
nothing at all. This records what a dying process cannot say afterwards, and
raises one hook per incident with an owner attached.

Whichever process runs the tests is the one that records, so a run with no
workers is covered as deeply as a distributed one: under xdist that process is
a worker and the controller reads what it left, and without xdist it is the
session itself. A run killed outright leaves nobody to report it, so the next
run over the same evidence directory does.

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

__version__ = "0.4.0"

__all__ = ["Settings", "__version__", "install", "installed_settings"]
