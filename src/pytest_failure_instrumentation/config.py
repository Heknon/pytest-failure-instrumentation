"""Every setting in one place, resolved once.

Defaults are chosen so that installing the plugin and doing nothing costs a
run almost nothing: the watchdog samples on a timer rather than per test, the
expensive probes are off, and no memory ceiling is imposed.

There are two ways settings arrive. Most runs read them from ini, which is
what ``resolve`` does. A framework that wraps pytest usually cannot: its
values come from its own configuration, computed in Python, and it wants them
applied without asking every team to copy an ini block. Those runs build a
``Settings`` and hand it to :func:`.install`. Both paths end in the same
frozen object, and the invariants below are enforced on the object rather than
in ``resolve``, so a hand-built one cannot skip them.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any, Optional

import pytest

DEFAULT_DIRECTORY = ".pytest-failures"

#: "Pick one for me" - the value the kernel itself reads as *any free port*,
#: so the lottery costs no branch at the bind. It is the default because a
#: session that has not been told which port to use has no reason to fight the
#: other sessions on the host for one: it serves its own workers on a port
#: nobody else wants, and writes down where. A port is only worth fixing when
#: something outside has to be told about it once - a firewall rule, a UI with
#: the address compiled in - and that is what naming one means.
PORT_LOTTERY = 0

#: The conventional fixed port, used when one is asked for by name rather than
#: by number. Not a default: 8080 is the most contended port on a developer's
#: machine, and taking it uninvited is how this feature turns itself off.
CONVENTIONAL_STACK_SERVER_PORT = 8080

#: Loopback. Overridable, because a container's UI is not inside the container
#: - see ``failure_stack_server_host``.
DEFAULT_STACK_SERVER_HOST = "127.0.0.1"

#: Addresses that reach this machine and nowhere else. ``localhost`` is in the
#: list because people type it, and it resolves to one of the other two.
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})

#: Below this the heartbeat thread costs more than it measures. Clamped on the
#: object rather than where a setting is read, because the controller decides
#: what counts as a stale beat from the same number - and two different
#: answers to "how often does a worker beat" make a healthy worker look frozen.
MIN_HEARTBEAT_INTERVAL = 1.0

#: The sampler walks a directory, reads every state file and tails every
#: event log on each pass, and may spawn a py-spy per stuck worker. A
#: cadence below this is a busy loop wearing a setting's clothes.
MIN_SAMPLE_SECONDS = 1.0

#: Accepted values for ``failure_tracer``; anything else falls back to the
#: default rather than failing a run over a typo in an ini file.
TRACER_POLICIES = ("parent", "any", "off")


class FailureInstrumentationWarning(UserWarning):
    """Raised for a setting this plugin could not use, and for setup it had to
    give up on. Never an error: nothing here is worth ending a run over."""


def advise(message: str) -> None:
    """Say the above, without being able to end the run - see that promise.

    Every warning in this module is raised from ``pytest_configure``: either
    building :class:`Settings` or reading an ini value on the way there. A
    project running ``filterwarnings = error``, which is a recommended setting
    and a common one, turns each of them into an exception thrown out of a
    configure hook, and pytest has one answer for that - INTERNALERROR, exit
    status 3, not a single test collected. "Never an error" was a promise this
    module made and could not keep.

    Measured: ``--callstack-host 0.0.0.0`` - the documented way to reach the
    live view from outside a container, which warns on purpose because it is a
    real exposure - ended the whole session that way; so did
    ``failure_stall_seconds = 5m``. A plugin installed to report failures does
    not get to be the thing that stops the run, least of all over advice about
    its own configuration.

    The advice is downgraded rather than dropped: re-emitted with the
    project's filters set aside, which leaves it in the warnings summary where
    it was always meant to be read. pytest's own recorder is left in place, so
    it is reported exactly as before on every project that had not turned
    warnings into errors.
    """
    try:
        warnings.warn(message, FailureInstrumentationWarning, stacklevel=3)
    except FailureInstrumentationWarning:
        with warnings.catch_warnings():
            warnings.simplefilter("always")
            warnings.warn(message, FailureInstrumentationWarning, stacklevel=3)


@dataclass(frozen=True)
class Settings:
    """Everything this plugin reads, and the only thing :func:`.install` takes.

    Every field has a default, so a caller states what it cares about and
    leaves the rest alone::

        Settings(packages=("yourcore",), directory=Path("/var/log/evidence"))

    ``__post_init__`` coerces and clamps, because this is a public type now: a
    framework passing ``packages=["a", "b"]`` or ``directory="/tmp/x"`` means
    the obvious thing, and a heartbeat interval below the floor is the bug the
    floor exists to prevent whichever way the object was built.
    """

    directory: Path = Path(DEFAULT_DIRECTORY)
    #: Your own top-level packages, for attribution.
    packages: tuple[str, ...] = ()
    product_version: Optional[str] = None
    watchdog: bool = True
    heartbeat_interval: float = 5.0
    tracemalloc_depth: int = 0
    object_census: bool = False
    high_water_mb: int = 0
    memory_limit_mb: int = 0
    #: How long a test may run before it starts leaving a stack, and how often
    #: it refreshes it after that - ``repeat=True``, so this is also the
    #: staleness bound on the only stack a stalled worker has on Windows. The
    #: file is emptied when the test ends, so the cadence costs one test's
    #: worth of dumps rather than a run's.
    slow_test_seconds: float = 20.0
    stall_seconds: float = 300.0
    stack_probe: bool = True
    #: Who a worker declares may read its stack on Linux, where Yama restricts
    #: it. "parent" nominates the controller and covers a session reading its
    #: own workers, which is the default mode; "any" is what a *shared* server
    #: needs, since another session's reader is no descendant of this
    #: controller; "off" declares nothing. Nothing outside Linux consults it.
    tracer: str = "parent"
    #: How often to push a ``pytest_failure_worker_sample`` while the run is
    #: going. 0 is off, and is the default: this is the only hook here that
    #: fires when nothing is wrong, so it is the only one a run pays for
    #: continuously - see :mod:`.sampling`.
    sample_seconds: float = 0.0
    #: Whether a sample carries frames for the workers that look stuck. The
    #: statuses are read from files the run wrote anyway and are nearly free;
    #: a stack is a subprocess that pauses its target and is ~30x the size, so
    #: it is worth being able to keep the first and decline the second.
    sample_stacks: bool = True
    #: Serve the stack of any local process over HTTP, for a UI watching a run
    #: - see :mod:`.stack_server`. Off by default: opening a listening socket
    #: is not something a plugin installed for crash reporting should start
    #: doing to everybody who upgrades it.
    stack_server: bool = False
    #: 0 draws a free port and writes it down; any other number is claimed,
    #: shared with the other sessions on the host, and waited for if somebody
    #: else has it first. See :mod:`.stack_server` for why those are different
    #: modes rather than one with a different number in it.
    stack_server_port: int = PORT_LOTTERY
    #: What the server binds. Loopback keeps it off the network, which is
    #: right on a laptop and wrong in a container, where the UI is outside and
    #: 127.0.0.1 is unreachable from there. Anything else is a deliberate
    #: exposure and says so.
    stack_server_host: str = DEFAULT_STACK_SERVER_HOST
    #: How many workers share the machine. Per worker, never sent between
    #: processes - the controller's copy would be wrong on every worker.
    worker_count: int = 1
    #: Which run this is. Minted by the controller and pushed down; likewise
    #: never copied from one process's settings to another's.
    run_id: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "directory", Path(self.directory))
        object.__setattr__(self, "packages", tuple(self.packages))
        object.__setattr__(
            self,
            "heartbeat_interval",
            max(MIN_HEARTBEAT_INTERVAL, float(self.heartbeat_interval)),
        )
        object.__setattr__(
            self,
            "sample_seconds",
            0.0 if self.sample_seconds <= 0 else max(MIN_SAMPLE_SECONDS, float(self.sample_seconds)),
        )
        object.__setattr__(
            self,
            "tracer",
            self.tracer if self.tracer in TRACER_POLICIES else "parent",
        )
        object.__setattr__(self, "stack_server_host", str(self.stack_server_host))
        object.__setattr__(self, "stack_server_port", int(self.stack_server_port))
        self._warn_if_a_stall_is_judged_before_it_has_evidence()
        self._warn_if_the_port_is_not_a_port()
        self._warn_if_the_stack_server_is_reachable_from_off_the_machine()

    def _warn_if_a_stall_is_judged_before_it_has_evidence(self) -> None:
        """The watchdog has to have fired before the stall is assessed.

        These two settings look independent and are not: the stack a stalled
        worker is reported with is whatever the watchdog last wrote, so a stall
        judged sooner than the watchdog's first dump is judged with no stack at
        all. On Windows, where nothing can ask a live process for one, that is
        every stall. Nothing is clamped - both numbers are legitimate choices -
        but silently losing the stack is the kind of absence this plugin exists
        to stop people misreading.
        """
        if not self.watchdog or self.stall_seconds <= 0 or self.slow_test_seconds <= 0:
            return  # one of the two is switched off, so there is no ordering
        if self.slow_test_seconds < self.stall_seconds:
            return
        advise(
            f"failure_slow_test_seconds ({self.slow_test_seconds:g}) is not below "
            f"failure_stall_seconds ({self.stall_seconds:g}), so a stalled worker "
            "is assessed before its watchdog has written a stack and will be "
            "reported without one",
        )

    def _warn_if_the_port_is_not_a_port(self) -> None:
        """Said here, where the number was typed, rather than only on the wire.

        A port outside 0-65535 cannot be bound, and the bind says so in an
        ``OverflowError`` rather than the ``OSError`` every other unbindable
        address answers with. The server treats it as the bind refusal it is
        and reports an incident - but an incident goes to a hook, and the
        person who mistyped a number on the command line is owed the answer in
        the terminal they typed it in.

        Not clamped, and not quietly replaced with a drawn port. Somebody who
        names a port has told something outside this run where to look; moving
        it and saying nothing leaves them watching an address the server was
        never on, which is the failure this warning exists to prevent rather
        than a fix for it.
        """
        if not self.stack_server or 0 <= self.stack_server_port <= 65535:
            return
        advise(
            f"failure_stack_server_port is {self.stack_server_port}, which is "
            "not a port - the range is 0-65535, where 0 means draw a free one. "
            "The live stack server cannot bind this and will report itself "
            "unavailable; the run is otherwise unaffected",
        )

    def _warn_if_the_stack_server_is_reachable_from_off_the_machine(self) -> None:
        """Binding anything but loopback is a decision, and it is worth saying
        out loud that it was made.

        The server answers with the stack of any local process it can read. It
        does ask who is asking - every endpoint but ``/identity`` wants the
        token - but a token is a secret in a file, and what changes off
        loopback is the set of people who get to try it. On loopback that set
        is whoever can open a socket on this machine; on 0.0.0.0 it is the
        network, which inside a cluster is every other pod. That is the right
        setting for a container whose UI is outside it and the wrong one
        everywhere else, and only the person who typed it can tell which case
        this is.
        """
        if not self.stack_server or self.stack_server_host in LOOPBACK:
            return
        advise(
            f"the live stack server is bound to {self.stack_server_host}, not "
            "loopback, so anything that can reach this host on port "
            f"{self.stack_server_port or '<drawn at random>'} can read the stack "
            "of any process it serves, and off loopback the boundary is the network "
            "rather than who can open a socket here - the token guards the "
            "endpoints, but anyone who can reach the address can try. This is what "
            "a container whose UI is outside it needs; it is not what a shared "
            "machine wants",
        )

    def with_overrides(self, **overrides: Any) -> Settings:
        """A copy with some fields changed, rejecting names that do not exist.

        A silently ignored ``pacakges=`` is a framework shipping unattributed
        incidents to every one of its customers and nobody finding out.
        """
        unknown = sorted(set(overrides) - _FIELD_NAMES)
        if unknown:
            raise TypeError(
                f"unknown failure-instrumentation setting(s): {', '.join(unknown)}. "
                f"Known settings: {', '.join(sorted(_FIELD_NAMES))}"
            )
        return replace(self, **overrides) if overrides else self

    # -- crossing a process boundary --------------------------------------

    def as_payload(self) -> dict[str, Any]:
        """A form execnet can carry to a worker.

        Only primitives: execnet serialises a fixed set of builtin types and
        nothing else, so ``Path`` and the tuple both have to be flattened.
        Per-process fields are left out rather than sent and ignored - the
        controller's worker count is not any worker's worker count.
        """
        return {
            "directory": str(self.directory),
            "packages": list(self.packages),
            "product_version": self.product_version,
            "watchdog": self.watchdog,
            "heartbeat_interval": self.heartbeat_interval,
            "tracemalloc_depth": self.tracemalloc_depth,
            "object_census": self.object_census,
            "high_water_mb": self.high_water_mb,
            "memory_limit_mb": self.memory_limit_mb,
            "slow_test_seconds": self.slow_test_seconds,
            "stall_seconds": self.stall_seconds,
            "stack_probe": self.stack_probe,
            # Handed down: it is the *worker* that makes the declaration, and
            # only the controller was told which mode this run is in.
            "tracer": self.tracer,
            # Sampling is absent for the same reason as the stack server below:
            # it runs on the controller, over every worker at once, and a
            # worker that read these would sample itself and its siblings.
            # The stack server is deliberately absent. It is the controller's
            # alone - a worker that read these and started one would have every
            # worker on the host racing for the same port, which is the exact
            # collision the server exists to avoid.
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any], **per_process: Any) -> Settings:
        """The inverse, plus the fields that only this process can answer."""
        known = {name: value for name, value in payload.items() if name in _FIELD_NAMES}
        return cls(**known, **per_process)


_FIELD_NAMES = {field.name for field in fields(Settings)}


def add_options(parser: pytest.Parser) -> None:
    _add_command_line_options(parser)
    parser.addini("failure_directory", help="Where evidence is written.", default=DEFAULT_DIRECTORY)
    parser.addini(
        "failure_packages",
        help="Your own top-level packages, so a failing frame can be told from "
        "a dependency's and from the customer's own tests.",
        type="args",
        default=[],
    )
    parser.addini("failure_product_version", help="Version recorded on every incident.", default="")
    parser.addini("failure_watchdog", help="Sample memory and liveness on a timer.", default="true")
    parser.addini("failure_heartbeat_interval", help="Seconds between liveness beats.", default="5.0")
    parser.addini(
        "failure_tracemalloc_depth",
        help="Allocation traceback frames to keep. 0 disables. 1 is cheap and "
        "names the allocating line, which is what attributes an OOM kill.",
        default="0",
    )
    parser.addini(
        "failure_object_census",
        help="Count live objects at a memory high-water mark. Off by default: "
        "walking the heap on a worker near its ceiling makes things worse.",
        default="false",
    )
    parser.addini("failure_high_water_mb", help="Absolute memory mark for a snapshot.", default="0")
    parser.addini(
        "failure_memory_limit_mb",
        help="Soft address-space cap per worker (POSIX). Turns a silent OOM "
        "kill into a MemoryError attributed to the offending test.",
        default="0",
    )
    parser.addini(
        "failure_slow_test_seconds",
        help="How long a test may run before it starts leaving a stack, and "
        "how often it refreshes it after that. This is the only stack a "
        "stalled worker has on Windows, so it doubles as the age of the "
        "freshest evidence available. The heartbeat thread writes it, so it "
        "needs failure_watchdog on and cannot be finer than "
        "failure_heartbeat_interval. 0 disables.",
        default="20",
    )
    parser.addini("failure_stall_seconds", help="Silence before a stall is assessed. 0 disables.", default="300")
    parser.addini(
        "failure_stack_server",
        help="Serve the live stack of any local process over HTTP, for a UI "
        "watching a run. One server per host, shared by every pytest session "
        "on it; reading a process other than the server's own needs py-spy "
        "installed. Loopback only.",
        default="false",
    )
    parser.addini(
        "failure_stack_server_port",
        help="Port for that server. 0 (the default) draws a free one and writes "
        "it to the evidence directory for a UI to read; any other number is "
        "claimed and shared with every other session on the host. Overridden by "
        "--callstack-port.",
        default=str(PORT_LOTTERY),
    )
    parser.addini(
        "failure_stack_server_host",
        help="What that server binds. Loopback by default, which keeps it off "
        "the network; 0.0.0.0 is what a container needs, since its UI is "
        "outside and cannot reach 127.0.0.1 in there. Overridden by "
        "--callstack-host.",
        default=DEFAULT_STACK_SERVER_HOST,
    )
    parser.addini(
        "failure_sample_seconds",
        help="Push a worker sample to pytest_failure_worker_sample this often, "
        "while the run is going. 0 disables, and is the default.",
        default="0",
    )
    parser.addini(
        "failure_sample_stacks",
        help="Whether those samples carry frames for workers that look stuck. "
        "The statuses are nearly free; the frames are not.",
        default="true",
    )
    parser.addini(
        "failure_tracer",
        help="Who a worker lets read its stack on Linux under Yama: parent "
        "(the controller, the default), any (needed by a shared server), off.",
        default="parent",
    )
    parser.addini(
        "failure_stack_probe",
        help="Ask an already-diagnosed stalled worker for a fresh stack "
        "(POSIX only). Can nudge a C extension blocked in a syscall.",
        default="true",
    )


def _add_command_line_options(parser: pytest.Parser) -> None:
    """The two settings worth having on the command line.

    Everything else about this plugin is a property of the project and belongs
    in ini. These two are properties of *where this run happens* - which port
    is free on this machine, which interface a container needs bound - and that
    is not something a repository can know on behalf of everybody running it.

    Naming either one switches the server on. An option that is accepted,
    parsed, and then silently ignored because a separate ini flag was left at
    its default is the worst of the available behaviours.
    """
    group = parser.getgroup("failure-instrumentation")
    group.addoption(
        "--callstack-port",
        type=int,
        default=None,
        metavar="PORT",
        help="Serve live stacks on this port, shared with any other session on "
        "the host that names the same one. Omit to draw a free port instead.",
    )
    group.addoption(
        "--callstack-host",
        default=None,
        metavar="HOST",
        help="Serve live stacks on this interface (default 127.0.0.1). Use "
        "0.0.0.0 to reach the server from outside a container.",
    )


def _option(config: pytest.Config, name: str) -> Any:
    """A command-line value, or None when there is not one to be had.

    Three ways there is not, and all of them are ordinary. The option may not
    be registered, because ``-p no:failure_instrumentation`` skips
    ``pytest_addoption`` - and asking pytest for an unregistered option raises
    rather than answering. The config may not be pytest's at all: a framework
    installing this by hand passes what it has. Or the option is registered and
    simply was not given, which is the common case and answers None by itself.
    """
    ask = getattr(config, "getoption", None)
    if ask is None:
        return None
    try:
        return ask(name)
    except (ValueError, KeyError):
        return None


def _ini(config: pytest.Config, name: str, fallback: Any) -> Any:
    """An ini value, or the fallback when the option is not registered at all.

    ``-p no:failure_instrumentation`` means ``add_options`` never ran, and a
    framework installing this by hand is exactly the case where that happens.
    Asking pytest for an unregistered ini key raises, so the absence is treated
    as "not configured" rather than allowed to end the run.
    """
    try:
        return config.getini(name)
    except (ValueError, KeyError):
        return fallback


def _flag(config: pytest.Config, name: str, fallback: bool) -> bool:
    raw = _ini(config, name, None)
    if raw is None:
        return fallback
    return str(raw).strip().lower() not in ("false", "0", "no", "")


def _number(config: pytest.Config, name: str, fallback: float) -> float:
    """A setting that cannot be read falls back, but says so.

    Silently substituting the default turns ``failure_stall_seconds = 5m`` into
    a stall detector the reader believes they configured and never hear from.
    """
    raw = _ini(config, name, None)
    try:
        return float(raw or fallback)
    except (ValueError, TypeError):
        advise(
            f"{name}={raw!r} is not a number; using {fallback} instead",
        )
        return fallback


def pytest_faulthandler_timeout(config: pytest.Config) -> float:
    """pytest's own ``faulthandler_timeout``, or 0 when nobody set one.

    There is exactly one ``faulthandler.dump_traceback_later`` timer per
    process, and arming it cancels whatever was armed before. pytest's
    faulthandler plugin arms it at the start of every test when this ini is
    set; the frozen-interpreter fallback re-arms it every second. Whichever
    ran last owns it, and the fallback always runs last - so a user who
    configured a timeout got a plugin that silently threw it away, including
    the ``faulthandler_exit_on_timeout`` that was supposed to end a hung run.

    Read here rather than in the worker so there is one answer to "is pytest
    using the timer", and it is read the same way pytest reads it.
    """
    try:
        return float(config.getini("faulthandler_timeout") or 0.0)
    except (ValueError, KeyError, TypeError):
        return 0.0


def resolve(config: pytest.Config) -> Settings:
    """The settings this process should use, before anyone overrides them.

    On a worker the controller's copy wins where it exists: a framework that
    built its settings in Python has no ini for the worker to re-read, and even
    where there is one, two processes resolving separately is two chances to
    disagree.
    """
    workerinput: dict[str, Any] = getattr(config, "workerinput", {}) or {}
    worker_count = int(workerinput.get("workercount", 1) or 1)
    # Pushed into workerinput by the controller (see IncidentEngine's
    # pytest_configure_node), so both sides of a run stamp their evidence with
    # the same id and a stale file from an earlier run is recognisable.
    run_id = str(workerinput.get("failure_run_id") or "") or None

    # The command line outranks ini for these two: they answer "where does this
    # run happen", which the person starting it knows and the repository does
    # not.
    chosen_port = _option(config, "callstack_port")
    chosen_host = _option(config, "callstack_host")
    named_on_cli = chosen_port is not None or chosen_host is not None

    handed_down = workerinput.get("failure_settings")
    if handed_down:
        return Settings.from_payload(
            dict(handed_down), worker_count=worker_count, run_id=run_id
        )

    return Settings(
        directory=Path(_ini(config, "failure_directory", "") or DEFAULT_DIRECTORY),
        packages=tuple(_ini(config, "failure_packages", ()) or ()),
        product_version=_ini(config, "failure_product_version", "") or None,
        watchdog=_flag(config, "failure_watchdog", True),
        heartbeat_interval=_number(config, "failure_heartbeat_interval", 5.0),
        tracemalloc_depth=int(_number(config, "failure_tracemalloc_depth", 0)),
        object_census=_flag(config, "failure_object_census", False),
        high_water_mb=int(_number(config, "failure_high_water_mb", 0)),
        memory_limit_mb=int(_number(config, "failure_memory_limit_mb", 0)),
        slow_test_seconds=_number(config, "failure_slow_test_seconds", 20.0),
        stall_seconds=_number(config, "failure_stall_seconds", 300.0),
        stack_probe=_flag(config, "failure_stack_probe", True),
        tracer=str(_ini(config, "failure_tracer", "parent") or "parent").strip().lower(),
        sample_seconds=_number(config, "failure_sample_seconds", 0.0),
        sample_stacks=_flag(config, "failure_sample_stacks", True),
        stack_server=_flag(config, "failure_stack_server", False) or named_on_cli,
        stack_server_port=(
            chosen_port
            if chosen_port is not None
            else int(_number(config, "failure_stack_server_port", PORT_LOTTERY))
        ),
        stack_server_host=(
            chosen_host
            or _ini(config, "failure_stack_server_host", "")
            or DEFAULT_STACK_SERVER_HOST
        ),
        worker_count=worker_count,
        run_id=run_id,
    )
