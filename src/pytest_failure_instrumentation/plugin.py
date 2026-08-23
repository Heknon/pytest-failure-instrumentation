"""Entry point. Registration only - no logic lives here.

Installed as a ``pytest11`` entry point, so ``pip install`` is the whole setup
and nothing has to be added to a conftest. Implement
``pytest_failure_incident`` to receive what it finds; without that the evidence
is still written to disk.

Disable entirely with ``-p no:failure_instrumentation``.

A framework that wants to supply its own settings calls
:func:`~pytest_failure_instrumentation.install` instead of relying on the ini
file - see :mod:`.registration`. The hookimpl below is ``trylast`` precisely so
that it can: it registers only what nobody has already installed, once every
other plugin's ``pytest_configure`` has run.
"""

from __future__ import annotations

import pytest

from . import hookspec
from .config import add_options
from .registration import install, installed_settings


def pytest_addhooks(pluginmanager: pytest.PytestPluginManager) -> None:
    pluginmanager.add_hookspecs(hookspec)


def pytest_addoption(parser: pytest.Parser) -> None:
    add_options(parser)


@pytest.hookimpl(trylast=True)
def pytest_configure(config: pytest.Config) -> None:
    """Install from ini, unless somebody already installed something better.

    Last, because a framework calling ``install`` from its own
    ``pytest_configure`` has to win, and an entry-point plugin's
    ``pytest_configure`` runs *after* this one would otherwise have.
    """
    if installed_settings(config) is None:
        install(config)
