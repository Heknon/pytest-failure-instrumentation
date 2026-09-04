"""Installing the plugin yourself, with settings you computed.

The ``pytest11`` entry point covers the ordinary case: ``pip install``,
``--failure-instrumentation`` on the command line, and the plugin configures
itself from ini. A framework that wraps pytest and ships it to its own teams
usually cannot use that. Its settings come from its own
configuration - a service catalogue, a deploy manifest, an environment it
already parsed - and asking every consuming team to copy an ini block that
must stay in step with it is a migration that never finishes.

    from pytest_failure_instrumentation import Settings, install

    def pytest_configure(config):
        install(config, packages=deployment.owned_packages,
                        directory=deployment.artifact_dir)

``install`` is the whole interface. It is idempotent, it works whether or not
the entry point loaded, and it may be called from a conftest or from another
plugin's ``pytest_configure``.

Three things make that work, and each of them was a way to get this wrong:

**Ordering.** A conftest's ``pytest_configure`` runs before an entry point's,
and one entry point's runs in an order nobody controls. Measured against this
plugin, a framework shipped as a conftest ran first and a framework shipped as
a package ran *second* - so the obvious "register in pytest_configure" would
have honoured a framework distributed one way and silently ignored the other.
The entry point's own hookimpl is therefore ``trylast``: it registers only
what nobody has already installed, after everyone else has had their turn.

**Workers.** A worker is a different process. Settings resolved from ini are
resolved again there and come out the same; settings computed in Python on the
controller do not exist there at all, and the framework's code may not even be
loaded. So whatever settings end up in force are pushed down through
``workerinput``, and a worker prefers them over anything it could read itself.

**Overriding versus replacing.** Passing keyword overrides layers them on top
of whatever ini said, which is what a framework setting one or two fields
wants. Passing a whole ``Settings`` replaces ini entirely, which is what a
framework that owns the configuration wants. Both are useful and they are not
the same, so they are spelled differently.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from . import hookspec
from .config import (
    SECRET_SETTINGS,
    SOLE_WORKER,
    Settings,
    advise,
    pytest_faulthandler_timeout,
    resolve,
)
from .errors import TracemallocConflict, refuse_a_shared_tracer

#: Where the settings in force are kept, so a later caller can see them and
#: the entry point can tell "already installed" from "nobody has yet".
STASH_KEY = "_failure_instrumentation_settings"

CONTROLLER_NAME = "failure-instrumentation-controller"
WORKER_NAME = "failure-instrumentation-worker"
#: The same recorder as ``WORKER_NAME``, in a run that has no workers. Named
#: apart because it is not one: ``--trace-config`` listing a worker in a
#: process that is plainly a session is the kind of small lie that costs
#: somebody an afternoon.
RECORDER_NAME = "failure-instrumentation-recorder"


def installed_settings(config: pytest.Config) -> Optional[Settings]:
    """The settings in force for this run, or None if nothing is installed."""
    return getattr(config, STASH_KEY, None)


def install(
    config: pytest.Config,
    settings: Optional[Settings] = None,
    **overrides: Any,
) -> Optional[Settings]:
    """Register the plugin on ``config`` with the settings you want.

    ``settings`` replaces what ini would have said; ``overrides`` are applied
    on top of it, or on top of ini when no ``settings`` is given. Returns the
    settings actually in force, or None if the plugin could not be installed
    at all - which is reported as a warning, never an exception, because a
    reporting tool that ends the run has cost more than any failure it might
    have explained. The deliberate exception is allocation profiling: it
    raises a usage error when another process-wide tracemalloc profiler is
    already active, because sharing it would make both profilers inaccurate.

    Calling this twice is safe. The second call does not rebuild a recorder
    that has already opened its files; it warns if it was asked for something
    different, so a second framework quietly losing to the first is visible.
    """
    try:
        wanted = _resolve(config, settings, overrides)
    except TypeError:
        raise  # a misspelled setting is the caller's bug, and worth raising
    except Exception as failure:  # noqa: BLE001 - see the docstring
        _warn(f"settings could not be resolved: {failure!r}")
        return None

    existing = installed_settings(config)
    if existing is not None:
        # A caller that did not name a run id is not asking to change it, so
        # the comparison must not turn a freshly minted one into a difference.
        asked_for = wanted.with_overrides(run_id=wanted.run_id or existing.run_id)
        if asked_for != existing:
            _warn(
                "failure instrumentation is already installed on this run and "
                "keeps the settings it was built with; the later call asked "
                f"for {_difference(existing, asked_for)}"
            )
        return existing

    _ensure_hookspecs(config)
    plugins = _build(config, wanted)
    if not plugins:
        return None

    setattr(config, STASH_KEY, wanted)
    for name, plugin in plugins:
        config.pluginmanager.register(plugin, name)
    return wanted


# -- the parts of that ----------------------------------------------------


def _resolve(
    config: pytest.Config, settings: Optional[Settings], overrides: dict[str, Any]
) -> Settings:
    from_run = resolve(config)
    base = from_run if settings is None else settings.with_overrides(
        # A caller handing over a whole Settings cannot know the worker count:
        # that is xdist's answer about this process, not a preference.
        worker_count=from_run.worker_count,
        # Their run id stands if they set one - correlating incidents with a
        # CI build id is a reason to install this by hand in the first place.
        # Left as None otherwise, which is what lets the engine prefer xdist's
        # own id for the run over anything this plugin could invent.
        run_id=settings.run_id or from_run.run_id,
    )
    return base.with_overrides(**overrides)


def _build(config: pytest.Config, settings: Settings) -> list[tuple[str, Any]]:
    """The plugin objects for this process, in the order they register.

    There are three processes this can be, and two jobs to do in them.
    *Recording* is done by whichever process runs the tests, because
    everything worth knowing about a death or a stall is in that process and
    leaves it only if it was written down beforehand. *Reporting* is done by
    whichever process survives to do it.

    Under xdist those are different processes and the split is the obvious
    one: a worker records, the controller reports. A run with no workers is
    the case that was missing. There the two jobs land in one process, so it
    does both - and a plain ``pytest`` gets the state slot, the heartbeat, the
    watchdog stack and the live view it had none of, rather than only the two
    incident kinds a controller can raise without help.

    Every branch imports inside the callable ``_built`` guards rather than at
    the top of the branch, and that placement is the whole point rather than
    style. Each import reaches ``probes`` - the recorder through the memory
    and stack probes, the engine directly - and ``probes`` imports psutil at
    module scope. psutil is a declared dependency and is normally there, but
    "normally" is doing real work in that sentence: no wheel for the platform
    so the sdist build failed, a C extension broken by a libc the wheel was
    not built against, ``pip install --no-deps``, a mirror that has the pure
    Python dependencies and not the compiled one. An import left outside the
    guard turns any of those into an ImportError thrown out of
    ``pytest_configure``, which pytest answers with INTERNALERROR, exit status
    3 and nothing collected - on the controller and, worse, on every worker.
    Inside the guard the same failure is the warning-and-disable path the
    module docstring promises: no instrumentation, and a run that still runs.
    """
    # Asked in every process that reaches here, and therefore first in the
    # one that spawns the others: under xdist the controller configures
    # before a worker exists, so a tracer that is already running is one
    # usage error at the top of the run rather than the same error thrown
    # out of pytest_configure on every worker at once. Not inside a _built
    # guard, because it is the one failure here that is meant to end the run
    # - see refuse_a_shared_tracer and _built.
    if settings.profile_allocations:
        try:
            refuse_a_shared_tracer()
        except TracemallocConflict as failure:
            raise pytest.UsageError(str(failure)) from failure

    # pytest's faulthandler_timeout is read here rather than in the recorder:
    # it is not a setting of this plugin's and does not travel in Settings,
    # but it decides whether the frozen-interpreter fallback may arm the one
    # process-wide timer the two plugins share.
    timeout = pytest_faulthandler_timeout(config)

    if hasattr(config, "workerinput"):
        worker_id = config.workerinput["workerid"]

        def worker() -> Any:
            from .capture.recorder import WorkerRecorder

            return WorkerRecorder(
                settings.directory, worker_id, settings, faulthandler_timeout=timeout
            )

        built = _built(worker, "worker")
        return [(WORKER_NAME, built)] if built is not None else []

    # Registered whether or not xdist is in the picture. Two of the five kinds
    # are not distributed problems - an internal error ends a single-process
    # run just as finally, and the run summary is what says a run reached its
    # end at all.
    def engine() -> Any:
        from .incidents.engine import IncidentEngine

        return IncidentEngine(config, settings)

    running = _built(engine, "run")
    if running is None:
        return []
    plugins: list[tuple[str, Any]] = [(CONTROLLER_NAME, running)]
    if running.distributed:
        return plugins  # the workers record; this process only reports

    def alone() -> Any:
        from .capture.recorder import WorkerRecorder

        return WorkerRecorder(
            # The *run's* directory, not the configured one. A worker is handed
            # this through workerinput because it cannot work out which run it
            # belongs to; here the two objects are in one process and the
            # engine has already named it, so it is asked rather than guessed.
            running.directory,
            SOLE_WORKER,
            # Stamped with the id the engine reports, so this process's own
            # evidence carries the same name its incidents do. A worker is
            # handed one through workerinput for the same reason; here the
            # engine is in the room and can simply be asked.
            settings.with_overrides(run_id=running.run_id),
            faulthandler_timeout=timeout,
            # The one thing a lone run does differently from a worker, and the
            # one thing it is asked rather than told: there is a single
            # destination for a fatal dump and pytest has already pointed it
            # at a terminal somebody is watching. See failure_crash_stack.
            claims_fatal_dumps=settings.crash_stack,
        )

    # A recorder that cannot be built leaves the engine registered rather than
    # taking the run's reporting down with it: an internal error and a run
    # summary need nothing from the filesystem.
    recording = _built(alone, "session")
    if recording is not None:
        plugins.append((RECORDER_NAME, recording))
    return plugins


def _ensure_hookspecs(config: pytest.Config) -> None:
    """Add the hookspec if the entry point did not.

    ``-p no:failure_instrumentation`` skips ``pytest_addhooks`` along with
    everything else, and adding a spec twice is an error - so this asks
    whether the spec is already there rather than trying and catching.

    Asking must be about the *specification*, not the attribute. Registering a
    conftest that implements ``pytest_failure_incident`` creates a hook caller
    with no spec behind it, so the attribute exists while the hook is still
    unspecced - and pluggy fails the whole session at ``check_pending`` with
    "unknown hook", before a test has run.
    """
    hook = getattr(config.hook, "pytest_failure_incident", None)
    if hook is None or not hook.has_spec():
        config.pluginmanager.add_hookspecs(hookspec)


def _built(build: Any, what: str) -> Any:
    """Whatever ``build`` returns, or None and a warning.

    An allocation-profile request that conflicts with an already active
    tracemalloc profiler becomes a pytest usage error instead.

    Registration is the one place this plugin touches the filesystem before
    anything has gone wrong, and it is allowed to fail: a read-only image, a
    vanished mount, a name already taken by a file.

    ``build`` also does the importing, so a dependency that is declared but
    absent lands here too rather than out of ``pytest_configure``. See
    :func:`_build` for why that is arranged rather than incidental.
    """
    try:
        return build()
    except TracemallocConflict as failure:
        raise pytest.UsageError(str(failure)) from failure
    except Exception as failure:  # noqa: BLE001 - see the module docstring
        _warn(f"failure instrumentation is off for this {what}: {failure!r}")
        return None


def _difference(existing: Settings, wanted: Settings) -> str:
    """What the second ``install`` asked for that the first did not, in words.

    Every field is printed with both values, because "packages differ" tells
    the reader nothing they can act on - except the fields in
    :data:`~.config.SECRET_SETTINGS`, which are reported by name alone.

    The exception is the whole reason this has a docstring.
    ``stack_server_token`` is a field like any other, so two frameworks that
    disagreed about the token put both the offered one and the one in force
    into a ``FailureInstrumentationWarning`` - which pytest reproduces in the
    warnings summary, and CI keeps in the job log for anyone with read access
    to the build. Nothing else in this package ever writes that value down:
    it is supplied rather than minted precisely so that it need not be, and a
    warning about a misconfiguration is not a reason to make an exception.

    What the reader needs is that the two calls disagree about the token, and
    they get that. The values would only tell them which of two secrets they
    already hold is in force, which is not worth publishing either one for.
    """
    changed = []
    for name in vars(type(existing)).get("__dataclass_fields__", {}):
        in_force, asked_for = getattr(existing, name), getattr(wanted, name)
        if in_force == asked_for:
            continue
        if name in SECRET_SETTINGS:
            changed.append(f"a different {name} (not shown - it is a credential)")
        else:
            changed.append(f"{name}={asked_for!r} (in force: {in_force!r})")
    return ", ".join(changed) or "the same settings"


def _warn(message: str) -> None:
    """Routed through :func:`config.advise` so a project that turns warnings
    into errors gets the advice rather than an INTERNALERROR - see there."""
    advise(message)
