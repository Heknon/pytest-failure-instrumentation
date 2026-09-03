"""Entry point. Registration only - no logic lives here.

Installed as a ``pytest11`` entry point, which puts the hookspecs and the
``failure_*`` options on every run and nothing else: the plugin is off until a
run asks for it with ``--failure-instrumentation`` (or in ``addopts``), names
the live-stack server with ``--callstack-port`` or ``--callstack-host``, or
has :func:`~pytest_failure_instrumentation.install` called on it - see
:func:`~pytest_failure_instrumentation.config.switched_on` for the whole list.
Implement ``pytest_failure_incident`` to receive what it finds; without that
the evidence is still written to disk.

``-p no:failure_instrumentation`` removes the entry point altogether, options
and hookspecs included.

A framework that wants to supply its own settings calls
:func:`~pytest_failure_instrumentation.install` instead of relying on the ini
file - see :mod:`.registration`. The hookimpl below is ``trylast`` precisely so
that it can: it registers only what nobody has already installed, once every
other plugin's ``pytest_configure`` has run.
"""

from __future__ import annotations

import pytest

from . import hookspec
from .config import add_options, switched_on
from .registration import install, installed_settings


def pytest_addhooks(pluginmanager: pytest.PytestPluginManager) -> None:
    pluginmanager.add_hookspecs(hookspec)


def pytest_addoption(parser: pytest.Parser) -> None:
    add_options(parser)


@pytest.hookimpl(trylast=True)
def pytest_configure(config: pytest.Config) -> None:
    """Install from ini, if this run asked for the plugin and nobody already
    installed something better.

    Last, because a framework calling ``install`` from its own
    ``pytest_configure`` has to win, and an entry-point plugin's
    ``pytest_configure`` runs *after* this one would otherwise have.

    Asked, because installed is not switched on: a run that did not name the
    switch gets the hookspecs and the options from this module and nothing
    else, and a framework that installs by hand has asked by calling.
    """
    if installed_settings(config) is None and switched_on(config):
        install(config)
