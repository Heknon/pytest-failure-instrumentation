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

#: Where the live-stack server listens when it is switched on. Named here
#: rather than in the server module so that the default and the ini help
#: cannot drift apart.
DEFAULT_STACK_SERVER_PORT = 8080

#: Below this the heartbeat thread costs more than it measures. Clamped on the
#: object rather than where a setting is read, because the controller decides
#: what counts as a stale beat from the same number - and two different
#: answers to "how often does a worker beat" make a healthy worker look frozen.
MIN_HEARTBEAT_INTERVAL = 1.0


class FailureInstrumentationWarning(UserWarning):
    """Raised for a setting this plugin could not use, and for setup it had to
    give up on. Never an error: nothing here is worth ending a run over."""


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
    #: Serve the stack of any local process over HTTP, for a UI watching a run
    #: - see :mod:`.stack_server`. Off by default: opening a listening socket
    #: is not something a plugin installed for crash reporting should start
    #: doing to everybody who upgrades it.
    stack_server: bool = False
    #: Fixed on purpose. A port chosen at random is one no firewall has been
    #: told about and no UI can guess; the cost of fixing it is that the
    #: sessions sharing a host have to share the server, which they do.
    stack_server_port: int = DEFAULT_STACK_SERVER_PORT
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
        self._warn_if_a_stall_is_judged_before_it_has_evidence()

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
        warnings.warn(
            f"failure_slow_test_seconds ({self.slow_test_seconds:g}) is not below "
            f"failure_stall_seconds ({self.stall_seconds:g}), so a stalled worker "
            "is assessed before its watchdog has written a stack and will be "
            "reported without one",
            FailureInstrumentationWarning,
            stacklevel=4,
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
        help=f"Port for that server. Fixed rather than chosen at random, so a "
        f"UI and a firewall can both be told about it once. Default "
        f"{DEFAULT_STACK_SERVER_PORT}.",
        default=str(DEFAULT_STACK_SERVER_PORT),
    )
    parser.addini(
        "failure_stack_probe",
        help="Ask an already-diagnosed stalled worker for a fresh stack "
        "(POSIX only). Can nudge a C extension blocked in a syscall.",
        default="true",
    )


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
        warnings.warn(
            f"{name}={raw!r} is not a number; using {fallback} instead",
            FailureInstrumentationWarning,
            stacklevel=2,
        )
        return fallback


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
        stack_server=_flag(config, "failure_stack_server", False),
        stack_server_port=int(
            _number(config, "failure_stack_server_port", DEFAULT_STACK_SERVER_PORT)
        ),
        worker_count=worker_count,
        run_id=run_id,
    )
