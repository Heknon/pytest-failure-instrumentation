"""From a run's profile records to the handful of things worth a look.

Pure: takes the records the samplers wrote, an attributor for "whose code is
this", and the thresholds, and returns findings. Nothing here reads a clock,
a file or a process, which is what lets every rule below be tested against a
record built by hand.

**Blame.** A sample's stack is charged to the first frame, walking outward
from the innermost, that belongs to somebody: product or customer code first,
a third-party package failing that, the runtime failing everything. This is
what turns "two million calls to a C pixel accessor" into "your
``is_images_different``", and "a lot of time in json/encoder.py" into "your
``render_report``, below the json encoder". The deepest frame stays in the
finding as the *below* line, because which library the time is under is the
second thing a reader asks.

**Verdicts** say what kind of cost a hotspot is, not whether it is wrong:

=================== ========================================================
``PYTHON_CODE``     the blamed function's own lines are what is hot
``LIBRARY_CALL``    the time is under a library or runtime call it makes
``BACKGROUND_THREAD`` the CPU is on a thread that is not running the test
``GC_PRESSURE``     the collector, which belongs to nobody's frame
``NATIVE_THREADS``  CPU in threads Python has no stack for
=================== ========================================================

and for memory:

======================= =====================================================
``RETAINED_AFTER_TEST`` the worker was left holding more than it started with
``HEAP_NOT_RETURNED``   left holding more, none of it in use: pages the allocator kept
``TRANSIENT_PEAK``      a test climbed and came back down
``STEADY_GROWTH``       a run of tests each left a little behind
``WORKER_IMBALANCE``    one worker holds far more than its siblings
``PEAK_OVER_CEILING``   a test reached the absolute size nothing may reach
``ALLOCATOR_RETENTION`` the worker grew and nothing is using it: memory the
                        allocator was handed back and kept, in thread
                        arenas (``MALLOC_ARENA_MAX``) or one fragmented
                        main heap (``malloc_trim``)
======================= =====================================================

A memory finding about one test also names the code that was running while
the memory climbed. The sampler charges every rise in resident memory to the
test thread's stack at that moment, and those stacks are blamed the same way
CPU is - so "peaked 900 MB" comes with "under ``load_everything`` in
``reports.py``", which is the difference between a number and a fix. With
allocation tracing on, the record also carries what tracemalloc saw holding
the memory, and those lines join the evidence.

And for bursts, read from the timeline - the process's CPU window by window
against the machine's - which is what a suite that waits on I/O for
ninety-nine seconds in a hundred needs, since a share of the run's CPU says
nothing about a hundredth of it that pins a core:

======================= =====================================================
``LONG_BURST``          one test held a core (or more) for longer than allowed
``RECURRING_BURST``     the same function burst in test after test - a fixture
``BACKGROUND_BURST``    a thread that is not running the test held a core
``CONTENDED``          the machine was pinned and the workers got slices of it
======================= =====================================================
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Any, Optional

from ..analysis.attribution import Attributor
from .sampler import BACKGROUND_RECORD, TEST_RECORD

#: Owners a stack is charged to first. The runtime and a dependency are never
#: to blame while somebody's own frame is on the stack under them.
OWNED = ("product", "customer-code")


@dataclass(frozen=True)
class Thresholds:
    """What is worth flagging. Every one is a setting; these are the defaults."""

    #: Percent of the run's sampled CPU one function must hold.
    cpu_share_percent: float = 5.0
    #: And at least this much of it, so a two-second run flags nothing.
    cpu_floor_seconds: float = 0.5
    #: Percent of the run's CPU the collector must take.
    gc_share_percent: float = 10.0
    #: MB a test may leave behind, or climb by, before it is named.
    retained_mb: int = 100
    #: Consecutive tests that must each leave something before growth is
    #: called steady rather than a step.
    growth_tests: int = 4
    #: How many times the median a worker must hold to be called imbalanced.
    imbalance_ratio: float = 2.0
    #: Resident MB no test may reach whatever it started from. 0 is off.
    peak_mb: int = 0
    #: Cores' worth of CPU a window must hold to be part of a burst.
    burst_cores: float = 0.7
    #: Seconds a burst must last to be raised on its own.
    burst_seconds: float = 2.0
    #: Tests the same function must burst in to be raised as recurring,
    #: whatever the length of each burst.
    burst_tests: int = 5


@dataclass
class FrameRef:
    file: str
    line: int
    function: str
    owner: str

    def __str__(self) -> str:
        return f"{Path(self.file).name}:{self.line} in {self.function}"


@dataclass
class FunctionCost:
    """Everything charged to one function across the run."""

    file: str
    function: str
    owner: str
    cpu_ns: int = 0
    self_cpu_ns: int = 0
    background_cpu_ns: int = 0
    wall_ns: int = 0
    samples: int = 0
    tests: Counter = field(default_factory=Counter)
    #: CPU charged with no test in flight - collection, the gaps between
    #: tests - which is nobody's test and is not counted as one.
    gap_cpu_ns: int = 0
    lines: Counter = field(default_factory=Counter)
    #: (file, owner) -> cpu, for what this function's cost is *under*.
    below: Counter = field(default_factory=Counter)
    #: function -> cpu, for the top frames under it, to name the usual one.
    below_functions: Counter = field(default_factory=Counter)
    threads: Counter = field(default_factory=Counter)
    #: The single most expensive stack this was blamed for, innermost first,
    #: from the blamed frame outward. Rendered as the incident's stack.
    representative: list[FrameRef] = field(default_factory=list)
    representative_cpu_ns: int = 0

    @property
    def key(self) -> tuple[str, str]:
        return (self.file, self.function)


@dataclass
class Finding:
    """One thing crossing a threshold. Turned into an incident by the engine."""

    kind: str
    verdict: str
    evidence: list[str]
    #: Where the blamed function is, or None for a finding with no frame.
    frame: Optional[FrameRef] = None
    #: The stack that stands for it, as faulthandler-style lines, deepest
    #: first, so the engine's attributor can read it like any other.
    stack: list[str] = field(default_factory=list)
    below: Optional[FrameRef] = None
    cpu_seconds: float = 0.0
    share_percent: float = 0.0
    self_share_percent: float = 0.0
    background_share_percent: float = 0.0
    thread: Optional[str] = None
    tests: list[str] = field(default_factory=list)
    test_count: int = 0
    hottest_lines: list[tuple[int, float]] = field(default_factory=list)
    # memory
    nodeid: Optional[str] = None
    worker: Optional[str] = None
    phase: Optional[str] = None
    before_mb: Optional[int] = None
    after_mb: Optional[int] = None
    peak_mb: Optional[int] = None
    delta_mb: Optional[int] = None
    #: For a ceiling finding: the configured ceiling it crossed.
    ceiling_mb: Optional[int] = None
    growth_tests: int = 0
    growth_per_test_mb: float = 0.0
    worker_rss: dict[str, int] = field(default_factory=dict)
    median_mb: Optional[int] = None
    #: For a memory finding: how much of the climb was charged to the blamed
    #: stack, out of how much was charged at all.
    climb_mb: int = 0
    climb_total_mb: int = 0
    #: For steady growth: live objects added per test, when they were counted.
    growth_objects_per_test: Optional[int] = None
    #: For allocator retention: the arenas at the end, the threads they serve,
    #: the free memory the allocator keeps mapped, and what a trim would return.
    arenas: Optional[int] = None
    threads: Optional[int] = None
    allocator_free_mb: Optional[int] = None
    trim_mb: Optional[int] = None
    # bursts
    #: How long the burst held the cores, and how many of them.
    burst_seconds: float = 0.0
    cores: float = 0.0
    #: Seconds into the test (or the gap between tests) that it started.
    started_s: float = 0.0
    #: How busy the whole machine was over the burst, in percent.
    machine_busy_percent: Optional[float] = None
    cpus: Optional[int] = None
    worker_count: int = 0


@dataclass
class Report:
    """The whole picture, findings included, for the terminal and the files."""

    findings: list[Finding]
    functions: list[FunctionCost]
    #: Seconds of CPU the samplers attributed to Python stacks.
    sampled_cpu_s: float
    #: What the processes themselves reported burning over the same windows.
    process_cpu_s: float
    wall_s: float
    gc_s: float
    native_cpu_s: float
    cpu_weighted: bool
    #: Whether the CPU was read per thread. Where it could not be, the whole
    #: process's CPU is charged to the test's thread, and a background
    #: thread's cost lands on the test that happened to be running.
    per_thread: bool
    workers: dict[str, dict[str, Any]]
    tests: int
    #: Whether allocation tracing was on. A traced run's CPU is the tracer's
    #: as much as the tests', so no CPU finding is raised from one; memory
    #: findings are what it is for.
    allocations: bool = False


# -- reading records ------------------------------------------------------------


def _parse_frame(entry: str) -> tuple[str, int, str]:
    """``file|line|function`` as the sampler writes it, back into its parts."""
    file, _, rest = entry.partition("|")
    line, _, function = rest.partition("|")
    try:
        number = int(line)
    except ValueError:
        number = 0
    return file, number, function


def _frame(entry: str, attributor: Attributor) -> FrameRef:
    file, number, function = _parse_frame(entry)
    return FrameRef(file, number, _enclosing(function) or "?", attributor.owner_of(file))


def _enclosing(function: str) -> str:
    """A comprehension or lambda folded into the function it is written in.

    Before 3.12 a list comprehension is a code object of its own, so the
    hot line of ``build`` shows up as ``build.<locals>.<listcomp>`` - a
    function nobody wrote and nobody can find. The enclosing name is what a
    reader looks for, and what the same code is called on 3.12 and later.
    """
    while function.endswith(">") and ".<locals>." in function:
        function = function.rsplit(".<locals>.", 1)[0]
    return function


#: Where the sampler's own code lives. Its collector callback runs on the
#: test's thread inside whatever allocated, and a sample taken just as a
#: collection ends lands in it; the frame under it is the one that paid.
INSTRUMENTATION = "/pytest_failure_instrumentation/profile/"


def _without_the_instrumentation(frames: list[FrameRef]) -> list[FrameRef]:
    index = 0
    while index < len(frames) and INSTRUMENTATION in frames[index].file.replace("\\", "/"):
        index += 1
    return frames[index:]


def _owned_index(frames: list[FrameRef]) -> Optional[int]:
    """The first frame that belongs to somebody - product or customer code
    first, a dependency failing that - or None when the whole stack is the
    runtime's."""
    for owners in (OWNED, ("third-party",)):
        for index, frame in enumerate(frames):
            if frame.owner in owners:
                return index
    return None


def _blame_index(frames: list[FrameRef]) -> int:
    index = _owned_index(frames)
    return 0 if index is None else index


def _faulthandler_lines(frames: list[FrameRef]) -> list[str]:
    return [f'  File "{frame.file}", line {frame.line} in {frame.function}' for frame in frames]


# -- the analysis ---------------------------------------------------------------


def analyse(
    records: list[dict[str, Any]],
    attributor: Attributor,
    thresholds: Optional[Thresholds] = None,
) -> Report:
    limits = thresholds or Thresholds()
    functions: dict[tuple[str, str], FunctionCost] = {}
    sampled_cpu = 0
    process_cpu = 0.0
    wall = 0.0
    gc_seconds = 0.0
    native_cpu = 0
    cpu_weighted = True
    per_thread = True
    workers: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"tests": 0, "cpu_s": 0.0, "wall_s": 0.0, "peak_mb": None, "end_mb": None, "gc_s": 0.0}
    )
    gc_by_test: dict[str, float] = defaultdict(float)
    native_by_name: Counter = Counter()
    tests = 0
    allocations = False

    for record in records:
        worker = str(record.get("worker") or "")
        summary = workers[worker]
        summary["cpu_s"] += float(record.get("cpu_s") or 0.0)
        summary["wall_s"] += float(record.get("wall_s") or 0.0)
        summary["gc_s"] += float((record.get("gc") or {}).get("seconds") or 0.0)
        peak = record.get("rss_peak_mb")
        if peak is not None and (summary["peak_mb"] is None or peak > summary["peak_mb"]):
            summary["peak_mb"] = peak
        if record.get("rss_after_mb") is not None:
            summary["end_mb"] = record["rss_after_mb"]
        if record.get("record") == TEST_RECORD:
            summary["tests"] += 1
            tests += 1
        process_cpu += float(record.get("cpu_s") or 0.0)
        wall += float(record.get("wall_s") or 0.0)
        cpu_weighted = cpu_weighted and bool(record.get("cpu_weighted", True))
        per_thread = per_thread and bool(record.get("per_thread", True))
        allocations = allocations or bool(record.get("allocations"))
        seconds = float((record.get("gc") or {}).get("seconds") or 0.0)
        gc_seconds += seconds
        if record.get("nodeid") and seconds:
            gc_by_test[record["nodeid"]] += seconds
        for thread in record.get("native_threads") or []:
            native_cpu += int(thread.get("cpu_ns") or 0)
            native_by_name[str(thread.get("name"))] += int(thread.get("cpu_ns") or 0)

        table = [_frame(entry, attributor) for entry in record.get("frames") or []]
        nodeid = record.get("nodeid")
        for stack in record.get("stacks") or []:
            cpu_ns = int(stack.get("cpu_ns") or 0)
            if cpu_ns <= 0:
                continue
            try:
                frames = [table[index] for index in stack["frames"]]
            except (IndexError, KeyError, TypeError):
                continue
            frames = _without_the_instrumentation(frames)
            if not frames:
                continue
            sampled_cpu += cpu_ns
            blamed_at = _blame_index(frames)
            blamed = frames[blamed_at]
            cost = functions.get((blamed.file, blamed.function))
            if cost is None:
                cost = functions[(blamed.file, blamed.function)] = FunctionCost(
                    blamed.file, blamed.function, blamed.owner
                )
            cost.cpu_ns += cpu_ns
            cost.wall_ns += int(stack.get("wall_ns") or 0)
            cost.samples += int(stack.get("samples") or 0)
            if nodeid:
                cost.tests[nodeid] += cpu_ns
            else:
                cost.gap_cpu_ns += cpu_ns
            cost.threads[str(stack.get("thread"))] += cpu_ns
            if stack.get("background"):
                cost.background_cpu_ns += cpu_ns
            if blamed_at == 0:
                cost.self_cpu_ns += cpu_ns
                cost.lines[blamed.line] += cpu_ns
            else:
                top = frames[0]
                cost.below[(top.file, top.owner)] += cpu_ns
                cost.below_functions[top.function] += cpu_ns
            if cpu_ns > cost.representative_cpu_ns:
                cost.representative_cpu_ns = cpu_ns
                cost.representative = frames[blamed_at:]

    ranked = sorted(functions.values(), key=lambda cost: cost.cpu_ns, reverse=True)
    findings: list[Finding] = []
    total_cpu_ns = sampled_cpu + native_cpu
    if not allocations:
        findings.extend(_cpu_findings(ranked, total_cpu_ns, limits))
        findings.extend(_gc_finding(gc_seconds, total_cpu_ns, gc_by_test, limits))
        findings.extend(_native_finding(native_cpu, native_by_name, total_cpu_ns, limits))
    findings.extend(_memory_findings(records, limits, attributor))
    if not allocations:
        findings.extend(_burst_findings(records, limits, attributor))
        findings.extend(_contention_finding(records, limits, len(workers)))

    return Report(
        findings=findings,
        functions=ranked,
        sampled_cpu_s=sampled_cpu / 1e9,
        process_cpu_s=process_cpu,
        wall_s=wall,
        gc_s=gc_seconds,
        native_cpu_s=native_cpu / 1e9,
        cpu_weighted=cpu_weighted,
        per_thread=per_thread,
        workers=dict(workers),
        tests=tests,
        allocations=allocations,
    )


def _percent(part: float, whole: float) -> float:
    return round(100.0 * part / whole, 1) if whole > 0 else 0.0


def _pct(value: float) -> str:
    """A percentage a reader takes in at a glance: whole from ten up, one
    decimal below."""
    if value >= 10:
        return f"{value:.0f}%"
    return f"{value:.1f}%".replace(".0%", "%")


def _place(frame: FrameRef) -> str:
    return f"{Path(frame.file).name}:{frame.line}"


def _named(frame: FrameRef) -> str:
    return f"{frame.function} ({_place(frame)})"


def _listed(names: list[str], total: int, limit: int = 3) -> str:
    rest = total - min(len(names), limit)
    return ", ".join(names[:limit]) + (f" and {rest} more" if rest > 0 else "")


def _measured(
    record: dict[str, Any],
    before: int,
    after: int,
    traced: bool,
    *,
    peak: Optional[int] = None,
    parts: Optional[list[str]] = None,
    understated: bool = False,
) -> str:
    """The numbers, labelled, in one line at the end of a memory finding."""
    figure = "traced memory" if traced else "process"
    line = f"Measured: {figure} {before} MB before"
    if peak is not None:
        line += f", {peak} MB peak"
    line += f", {after} MB after."
    for part in parts or []:
        line += f" {part}."
    if understated:
        line += (
            " The process figure is lower than the live-heap figure because the test reused "
            "pages an earlier test had freed."
        )
    return line


def _cpu_findings(ranked: list[FunctionCost], total_ns: int, limits: Thresholds) -> list[Finding]:
    findings = []
    for cost in ranked:
        share = _percent(cost.cpu_ns, total_ns)
        seconds = cost.cpu_ns / 1e9
        if share < limits.cpu_share_percent or seconds < limits.cpu_floor_seconds:
            continue
        background = _percent(cost.background_cpu_ns, cost.cpu_ns)
        self_share = _percent(cost.self_cpu_ns, cost.cpu_ns)
        below_share = 100.0 - self_share
        thread = cost.threads.most_common(1)[0][0] if cost.threads else None
        below_frame: Optional[FrameRef] = None
        frame = cost.representative[0] if cost.representative else None
        hottest = [
            (line, _percent(line_ns, cost.cpu_ns)) for line, line_ns in cost.lines.most_common(3)
        ]
        evidence: list[str] = []
        if background >= 50.0:
            verdict = "BACKGROUND_THREAD"
            evidence.append(
                "This thread is not the one running tests, so it uses this CPU whichever test "
                "is executing."
                + ("" if background >= 99.5 else f" {_pct(background)} of the CPU was on it.")
            )
        elif below_share >= 50.0 and cost.below:
            verdict = "LIBRARY_CALL"
            (file, owner), below_ns = cost.below.most_common(1)[0]
            function = cost.below_functions.most_common(1)[0][0]
            below_frame = FrameRef(file, 0, function, owner)
            evidence.append(
                "The time is inside calls this function makes, not in its own lines: "
                f"{_pct(_percent(below_ns, cost.cpu_ns))} of it under {Path(file).name} ({owner}), "
                f"mostly in {function}."
            )
        else:
            verdict = "PYTHON_CODE"
            line = "The time is in this function's own lines, not in calls it makes."
            if hottest:
                line += " Mostly " + ", ".join(
                    f"line {number} ({_pct(percent)})" for number, percent in hottest
                ) + "."
            evidence.append(line)
        examples = [nodeid for nodeid, _ in cost.tests.most_common(3)]
        if cost.tests:
            seen = (
                f"Seen in {len(cost.tests)} test{'s' if len(cost.tests) != 1 else ''}: "
                + _listed(examples, len(cost.tests))
            )
            if cost.gap_cpu_ns:
                seen += ", and between tests"
        else:
            seen = "Seen only between tests, with no test running"
        evidence.append(seen + ".")
        if frame is not None:
            look = f"Look at: {_place(frame)}"
            if verdict == "LIBRARY_CALL" and below_frame is not None:
                look += f" and its calls into {Path(below_frame.file).name}"
            evidence.append(look)
        findings.append(
            Finding(
                kind="cpu_hotspot",
                verdict=verdict,
                evidence=evidence,
                frame=frame,
                stack=_faulthandler_lines(cost.representative),
                below=below_frame,
                cpu_seconds=round(seconds, 3),
                share_percent=share,
                self_share_percent=self_share,
                background_share_percent=background,
                thread=thread,
                tests=examples,
                test_count=len(cost.tests),
                hottest_lines=hottest,
            )
        )
    return findings


def _gc_finding(
    gc_seconds: float, total_ns: int, by_test: dict[str, float], limits: Thresholds
) -> list[Finding]:
    share = _percent(gc_seconds * 1e9, total_ns)
    if share < limits.gc_share_percent or gc_seconds < limits.cpu_floor_seconds:
        return []
    worst = sorted(by_test.items(), key=lambda item: -item[1])[:3]
    evidence = [
        "Collections run in proportion to how many objects are allocated. The time is "
        "counted on whichever test was running.",
    ]
    if worst:
        evidence.append(
            "Most of it during: "
            + ", ".join(f"{nodeid} ({seconds:.1f} s)" for nodeid, seconds in worst)
            + "."
        )
        evidence.append(f"Look at: what {worst[0][0]} allocates.")
    return [
        Finding(
            kind="cpu_hotspot",
            verdict="GC_PRESSURE",
            evidence=evidence,
            cpu_seconds=round(gc_seconds, 3),
            share_percent=share,
            tests=[nodeid for nodeid, _ in worst],
            test_count=len(by_test),
        )
    ]


def _native_finding(
    native_ns: int, by_name: Counter, total_ns: int, limits: Thresholds
) -> list[Finding]:
    share = _percent(native_ns, total_ns)
    seconds = native_ns / 1e9
    if share < limits.cpu_share_percent or seconds < limits.cpu_floor_seconds:
        return []
    names = ", ".join(
        f"{name} ({cpu / 1e9:.1f} s)" for name, cpu in by_name.most_common(3)
    )
    return [
        Finding(
            kind="cpu_hotspot",
            verdict="NATIVE_THREADS",
            evidence=[
                "These threads were started by native code, so there is no Python line to "
                "attribute them to.",
                f"By kernel thread name: {names}.",
                "Look at: which extension modules start threads with these names.",
            ],
            cpu_seconds=round(seconds, 3),
            share_percent=share,
        )
    ]


def _climb(
    record: dict[str, Any], attributor: Attributor
) -> tuple[Optional[FrameRef], list[str], int, int, list[str]]:
    """Who was running while this test's memory climbed.

    The growth entries are charged to a frame like CPU stacks are - the first
    that belongs to somebody, walking out from the innermost - and the heaviest
    wins. Unlike CPU, a climb is never charged to the runtime alone: a
    reading taken just after a test body returned finds pytest's own frames
    running, and naming them would send the reader to the wrong place. The
    stack the thread was in a tick earlier is tried instead, and a climb
    with nobody's code on either is counted as unplaced. Returns the frame,
    its stack as faulthandler lines, the megabytes charged to it, the
    megabytes charged in total, and the evidence sentence.
    """
    table = [_frame(entry, attributor) for entry in record.get("frames") or []]
    charged: dict[tuple[str, str], int] = defaultdict(int)
    heaviest: dict[tuple[str, str], tuple[int, list[FrameRef]]] = {}
    total = 0
    unplaced = 0
    for entry in record.get("growth") or []:
        megabytes = int(entry.get("mb") or 0)
        if megabytes <= 0:
            continue
        placed: Optional[tuple[list[FrameRef], int]] = None
        for indexes in (entry.get("frames"), entry.get("fallback")):
            if not indexes:
                continue
            try:
                frames = _without_the_instrumentation([table[index] for index in indexes])
            except (IndexError, TypeError):
                continue
            owned_at = _owned_index(frames)
            if owned_at is not None:
                placed = (frames, owned_at)
                break
        total += megabytes
        if placed is None:
            unplaced += megabytes
            continue
        frames, blamed_at = placed
        blamed = frames[blamed_at]
        key = (blamed.file, blamed.function)
        charged[key] += megabytes
        if key not in heaviest or megabytes > heaviest[key][0]:
            heaviest[key] = (megabytes, frames[blamed_at:])
    too_quick = f"{unplaced} MB of the increase could not be attributed to any code."
    if not charged:
        return None, [], 0, total, [too_quick] if unplaced else []
    key, megabytes = max(charged.items(), key=lambda item: item[1])
    stack = heaviest[key][1]
    frame = stack[0]
    amount = (
        f"All of the {total} MB increase"
        if megabytes == total
        else f"{megabytes} MB of the {total} MB increase"
    )
    line = f"{amount} happened while {_named(frame)} was running"
    caller = _caller(stack)
    if caller is not None:
        line += f", called from {_named(caller)}"
    evidence = [line + "."]
    if unplaced:
        evidence.append(too_quick)
    return frame, _faulthandler_lines(stack), megabytes, total, evidence


def _memory_findings(
    records: list[dict[str, Any]], limits: Thresholds, attributor: Attributor
) -> list[Finding]:
    findings: list[Finding] = []
    by_worker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if record.get("record") != TEST_RECORD or record.get("nodeid") is None:
            continue
        if record.get("rss_before_mb") is None or record.get("rss_after_mb") is None:
            continue
        by_worker[str(record.get("worker") or "")].append(record)

    # With allocation tracing on, the worker's last background record carries
    # what tracemalloc saw holding the memory the whole session accumulated.
    session_holders = {
        str(record.get("worker") or ""): record
        for record in records
        if record.get("record") == BACKGROUND_RECORD and record.get("holders_session")
    }

    for worker, tests in by_worker.items():
        for record in tests:
            before, after, peak, traced = _figures(record)
            retained = _kept(record)
            traced_note = _traced_note(record) if traced else []
            # A test is raised for the ceiling when it climbed to get there. A
            # worker already over it from what earlier tests kept is every
            # later test's problem, and the growth finding names the cause;
            # raising each of them again would be the same finding N times.
            over_ceiling = (
                bool(limits.peak_mb)
                and peak >= limits.peak_mb
                and peak - before >= limits.retained_mb // 2
            )
            if over_ceiling:
                frame, stack, climb, climb_total, evidence = _climb(record, attributor)
                frame, stack, held = _holders(record, "holders_peak", "Held at the peak", attributor, frame, stack)
                evidence.extend(held)
                if retained >= limits.retained_mb:
                    evidence.append(f"{retained} MB of it was still in use after the test.")
                else:
                    evidence.append("The memory was released before the test ended.")
                if frame is not None:
                    evidence.append(f"Look at: {_place(frame)}")
                evidence.append(
                    _measured(record, before, after, traced, peak=peak)
                    + " Ceiling from failure_profile_peak_mb."
                )
                evidence.extend(traced_note)
                findings.append(
                    Finding(
                        kind="memory_profile",
                        verdict="PEAK_OVER_CEILING",
                        evidence=evidence,
                        nodeid=record["nodeid"],
                        worker=worker,
                        before_mb=before,
                        after_mb=after,
                        peak_mb=peak,
                        delta_mb=peak - before,
                        ceiling_mb=limits.peak_mb,
                        frame=frame,
                        stack=stack,
                        climb_mb=climb,
                        climb_total_mb=climb_total,
                    )
                )
            elif retained >= limits.retained_mb:
                phase = _phase_of_step(record.get("rss_at") or {}, limits.retained_mb)
                live, live_parts = _still_in_use(record, retained)
                frame, stack, climb, climb_total, evidence = _climb(record, attributor)
                frame, stack, held = _holders(record, "holders_kept", "Still held after the test", attributor, frame, stack)
                evidence.extend(held)
                measured = _measured(
                    record, before, after, traced, parts=live_parts, understated=retained > after - before
                )
                if live is False:
                    findings.append(
                        Finding(
                            kind="memory_profile",
                            verdict="HEAP_NOT_RETURNED",
                            evidence=[
                                "The objects were freed. The C allocator kept the pages mapped, "
                                "which is normal allocator behaviour."
                            ]
                            + evidence
                            + [
                                "Look at: nothing in Python holds this. See ALLOCATOR_RETENTION if "
                                "the whole worker grows this way.",
                                measured,
                            ]
                            + traced_note,
                            nodeid=record["nodeid"],
                            worker=worker,
                            phase=phase,
                            before_mb=before,
                            after_mb=after,
                            peak_mb=peak,
                            delta_mb=retained,
                            frame=frame,
                            stack=stack,
                            climb_mb=climb,
                            climb_total_mb=climb_total,
                        )
                    )
                    continue
                if phase == "setup":
                    evidence.append(
                        "The increase happened during setup, so a fixture allocated it, and it "
                        "was still in use after teardown."
                    )
                elif phase == "call":
                    evidence.append(
                        "The increase happened during the test body and was still in use after "
                        "teardown."
                    )
                elif phase:
                    evidence.append(
                        f"The increase happened during {phase} and was still in use afterwards."
                    )
                else:
                    evidence.append("It was still in use after teardown.")
                if frame is not None:
                    evidence.append(f"Look at: {_place(frame)} and what holds its result after the test.")
                elif not traced:
                    evidence.append(
                        "Look at: rerun with --failure-profile-allocations to see which lines "
                        "hold the memory."
                    )
                evidence.append(measured)
                evidence.extend(traced_note)
                findings.append(
                    Finding(
                        kind="memory_profile",
                        verdict="RETAINED_AFTER_TEST",
                        evidence=evidence,
                        nodeid=record["nodeid"],
                        worker=worker,
                        phase=phase,
                        before_mb=before,
                        after_mb=after,
                        peak_mb=peak,
                        delta_mb=retained,
                        frame=frame,
                        stack=stack,
                        climb_mb=climb,
                        climb_total_mb=climb_total,
                    )
                )
            elif peak - max(before, after) >= limits.retained_mb:
                frame, stack, climb, climb_total, evidence = _climb(record, attributor)
                frame, stack, held = _holders(record, "holders_peak", "Held at the peak", attributor, frame, stack)
                evidence.extend(held)
                evidence.append(
                    "The memory was released before the test ended. This is the amount a worker "
                    "needs to have free to run this test."
                )
                if frame is not None:
                    evidence.append(f"Look at: {_place(frame)}")
                evidence.append(_measured(record, before, after, traced, peak=peak))
                evidence.extend(traced_note)
                risen = peak - before
                findings.append(
                    Finding(
                        kind="memory_profile",
                        verdict="TRANSIENT_PEAK",
                        evidence=evidence,
                        nodeid=record["nodeid"],
                        worker=worker,
                        before_mb=before,
                        after_mb=after,
                        peak_mb=peak,
                        delta_mb=risen,
                        frame=frame,
                        stack=stack,
                        climb_mb=climb,
                        climb_total_mb=climb_total,
                    )
                )
        findings.extend(_drift(worker, tests, limits, session_holders.get(worker), attributor))

    findings.extend(_imbalance(by_worker, limits))
    retention = _allocator_retention(by_worker, limits)
    if retention:
        # The same memory, seen per test: a test that left a threshold's
        # worth of it at once was HEAP_NOT_RETURNED on its own. Under the
        # worker's finding those are its biggest steps, not findings of
        # their own - one thing to fix should be one row.
        (whole,) = retention
        steps = [
            finding
            for finding in findings
            if finding.verdict == "HEAP_NOT_RETURNED" and finding.worker in whole.worker_rss
        ]
        if steps:
            steps.sort(key=lambda finding: -(finding.delta_mb or 0))
            whole.evidence.insert(
                2,
                "Biggest single steps: "
                + ", ".join(f"{step.nodeid} ({step.delta_mb} MB on {step.worker})" for step in steps[:3])
                + ".",
            )
            findings = [finding for finding in findings if finding not in steps]
        findings.extend(retention)
    return findings


def _allocator_retention(
    by_worker: dict[str, list[dict[str, Any]]], limits: Thresholds
) -> list[Finding]:
    """The worker grew over its run and nothing is using the growth: the
    allocator was handed the memory back and kept it mapped.

    The per-test rules leave this alone on purpose - drift counts what is in
    use, and a single test's fragmentation is HEAP_NOT_RETURNED only when it
    is a threshold's worth at once. A few megabytes of freed-but-mapped
    memory per test over a long worker is neither, and it is the worker at
    four gigabytes that nobody can find a leak in. The rule is over the
    whole worker: resident memory grew by the threshold more than the live
    heap and the object count did, and the allocator's own free figure
    accounts for most of that gap.

    Two causes, told apart by where the free memory sits, because they are
    fixed by different things. glibc gives every thread that allocates an
    arena of its own, up to eight per core, and each keeps what it frees:
    that is ``MALLOC_ARENA_MAX``. Free space mostly in the main arena is one
    heap fragmented by small survivors between the big allocations: that is
    ``malloc_trim``, and the arena variable does nothing for it. One finding
    for the run, on the worst worker, naming the others.
    """
    def rows_of(worker: str, by_worker: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
        return [
            record
            for record in by_worker[worker]
            if not _figures(record)[3]
            and record.get("allocator_before")
            and record.get("allocator_after")
            and record.get("heap_before_mb") is not None
            and record.get("heap_after_mb") is not None
        ]

    found: list[tuple[int, str, dict[str, Any], dict[str, Any], int, int, int]] = []
    for worker in by_worker:
        rows = rows_of(worker, by_worker)
        if len(rows) < 2:
            continue
        first, last = rows[0], rows[-1]
        resident = int(last["rss_after_mb"]) - int(first["rss_before_mb"])
        live = max(0, int(last["heap_after_mb"]) - int(first["heap_before_mb"]))
        blocks_before, blocks_after = first.get("blocks_before"), last.get("blocks_after")
        if blocks_before is not None and blocks_after is not None:
            live += max(0, (int(blocks_after) - int(blocks_before)) * BYTES_PER_BLOCK // 1048576)
        mapped = resident - live
        free_growth = int(last["allocator_after"].get("free_mb") or 0) - int(
            first["allocator_before"].get("free_mb") or 0
        )
        if mapped < limits.retained_mb or free_growth < mapped / 2:
            continue
        found.append((free_growth, worker, first, last, resident, live, len(rows)))
    if not found:
        return []
    found.sort(key=lambda item: -item[0])
    free_growth, worker, first, last, resident, live, count = found[0]
    after = last["allocator_after"]
    free_mb = int(after.get("free_mb") or 0)
    main_free = int(after.get("main_free_mb") or 0)
    other_free = max(0, free_mb - main_free)
    arenas = int(after.get("arenas") or 0)
    # The most threads the worker had at once over its run: the arenas
    # outlive the pool that earned them, so the last test's count is not it.
    threads = max(int(record.get("threads") or 0) for record in rows_of(worker, by_worker))
    cpus = int(last.get("cpus") or 0)
    trim = int(after.get("trim_mb") or 0)
    evidence = [
        "No Python object holds this memory. It is inside glibc's heaps, mapped and unused.",
        f"{main_free} MB of the {free_mb} MB is in the main heap, {other_free} MB in thread arenas. "
        f"{arenas} arena{'s' if arenas != 1 else ''} existed for up to {threads} threads on {cpus} cores.",
    ]
    if arenas > 1 and other_free >= free_mb / 2:
        evidence.append(
            "glibc keeps freed memory mapped inside each arena, and gives every thread that "
            "allocates an arena of its own, up to eight per core. MALLOC_ARENA_MAX limits how "
            "many thread arenas exist."
        )
    else:
        evidence.append(
            "glibc keeps freed memory mapped inside each arena. Most of this is in the main "
            f"heap, which MALLOC_ARENA_MAX does not affect; malloc_trim(0) releases the main "
            f"heap's free tail, currently {trim} MB, and MALLOC_TRIM_THRESHOLD_ sets when glibc "
            "does that on its own."
        )
    others = found[1:]
    if others:
        evidence.append(
            "The same on " + ", ".join(f"{entry[1]} ({entry[0]} MB)" for entry in others) + "."
        )
    evidence.append(
        f"Measured: process {first['rss_before_mb']} MB at the start, {last['rss_after_mb']} MB "
        f"at the end, up {resident} MB over {count} tests with {live} MB of that in use."
    )
    return [
        Finding(
            kind="memory_profile",
            verdict="ALLOCATOR_RETENTION",
            evidence=evidence,
            worker=worker,
            before_mb=int(first["rss_before_mb"]),
            after_mb=int(last["rss_after_mb"]),
            delta_mb=free_growth,
            test_count=count,
            worker_rss={entry[1]: entry[0] for entry in found},
            arenas=arenas,
            threads=threads,
            cpus=cpus or None,
            allocator_free_mb=free_mb,
            trim_mb=trim,
        )
    ]


def _figures(record: dict[str, Any]) -> tuple[int, int, int, bool]:
    """Before, after and peak, and whether they are tracemalloc's.

    With allocation tracing on, resident memory is not the figure: the
    tracer's tables grow with every allocation it records and churn the
    allocator enough to leave pages mapped after the test freed everything.
    tracemalloc's own count of traced bytes is exact, and its peak resets
    per test, so a traced record is judged on those.
    """
    traced = record.get("traced") or {}
    if all(traced.get(key) is not None for key in ("before_mb", "after_mb", "peak_mb")):
        before, after, peak = int(traced["before_mb"]), int(traced["after_mb"]), int(traced["peak_mb"])
        return before, after, max(peak, before, after), True
    before, after = int(record["rss_before_mb"]), int(record["rss_after_mb"])
    peak = int(record.get("rss_peak_mb") or max(before, after))
    return before, after, max(peak, before, after), False


def _traced_note(record: dict[str, Any]) -> list[str]:
    traced = record.get("traced") or {}
    return [
        "Figures are from tracemalloc (Python allocations only, the tracer's own "
        f"{int(traced.get('tracer_mb') or 0)} MB excluded). Process size: "
        f"{record.get('rss_before_mb')} MB before, {record.get('rss_after_mb')} MB after."
    ]


def _holders(
    record: dict[str, Any],
    key: str,
    label: str,
    attributor: Attributor,
    frame: Optional[FrameRef],
    stack: list[str],
) -> tuple[Optional[FrameRef], list[str], list[str]]:
    """The lines tracemalloc saw holding the memory, as evidence - and as the
    finding's frame when the sampler saw nothing climbing.

    A tracemalloc traceback is oldest frame first; a reader wants the
    allocation site first, so it is turned round. The frames carry no
    function name, and are blamed by file like any other.
    """
    table = [_frame(entry, attributor) for entry in record.get("frames") or []]
    evidence: list[str] = []
    for holder in record.get(key) or []:
        try:
            frames = _without_the_instrumentation([table[index] for index in reversed(holder["frames"])])
        except (IndexError, KeyError, TypeError):
            continue
        if not frames:
            continue
        # A comprehension and the function it is in are one line to a reader.
        places: list[str] = []
        for entry in frames:
            place = _place(entry)
            if not places or places[-1] != place:
                places.append(place)
        line = f"{label}: {holder.get('mb')} MB allocated at {places[0]}"
        if len(places) > 1:
            line += ", called from " + ", ".join(places[1:4])
        evidence.append(line + ".")
        if frame is None:
            blamed_at = _blame_index(frames)
            frame = frames[blamed_at]
            stack = _faulthandler_lines(frames[blamed_at:])
    return frame, stack, evidence


#: A rough size for one small-object block, to turn a block count into
#: megabytes a reader can set against the resident figure. pymalloc blocks
#: are at most 512 bytes and most are far smaller.
BYTES_PER_BLOCK = 64


def _still_in_use(record: dict[str, Any], retained_mb: int) -> tuple[Optional[bool], list[str]]:
    """Whether the memory a test left behind is alive, from the live-heap
    readings, and the labelled figures that say so, for the measured line.
    None where nothing was read."""
    if _figures(record)[3]:
        return True, [f"Live Python allocations +{retained_mb} MB (tracemalloc)"]
    heap_before, heap_after = record.get("heap_before_mb"), record.get("heap_after_mb")
    blocks_before, blocks_after = record.get("blocks_before"), record.get("blocks_after")
    parts: list[str] = []
    live_mb = 0
    measured = False
    if heap_before is not None and heap_after is not None:
        measured = True
        grew = int(heap_after) - int(heap_before)
        live_mb += max(0, grew)
        parts.append(f"Live heap {grew:+d} MB")
    if blocks_before is not None and blocks_after is not None:
        measured = True
        grew_blocks = int(blocks_after) - int(blocks_before)
        live_mb += max(0, grew_blocks * BYTES_PER_BLOCK // 1048576)
        parts.append(f"{grew_blocks:+,d} Python objects")
    if not measured:
        return None, parts
    return live_mb >= retained_mb // 2, parts


def _phase_of_step(rss_at: dict[str, Any], threshold_mb: int) -> Optional[str]:
    """Which phase the memory arrived in, from the boundary readings."""
    best: Optional[tuple[int, str]] = None
    for phase in ("setup", "call", "teardown"):
        start, end = rss_at.get(f"{phase}_start"), rss_at.get(f"{phase}_end")
        if start is None or end is None:
            continue
        step = int(end) - int(start)
        if best is None or step > best[0]:
            best = (step, phase)
    if best is None or best[0] < threshold_mb // 2:
        return None
    return best[1]


def _drift(
    worker: str,
    tests: list[dict[str, Any]],
    limits: Thresholds,
    session: Optional[dict[str, Any]],
    attributor: Attributor,
) -> list[Finding]:
    """The worker drifted upward over its tests: the leak no single test shows.

    Two megabytes a test is nothing; over fifty tests it is a hundred, and
    over a five-hour worker it is the OOM kill. The rule is over the whole
    worker: what its tests kept *in use* between them, net, reaches the
    threshold, no single step is half of it, and at least half of them grew.
    In use, because resident memory drifts up on its own as the allocator
    keeps pages a fixture's worth of freed objects fragmented - twenty
    megabytes a test that no line holds. Where the live heap and the object
    count were read, the step is what they say; elsewhere it is the resident
    step, and the object count is the tiebreak. A test raised on its own is
    left out, and so is the test that gave a fixture's worth back, or the
    release of a module fixture would cancel the leak beside it.
    """
    rows = [
        record
        for record in tests
        if abs(_kept(record)) < limits.retained_mb and abs(_live_step(record)) < limits.retained_mb
    ]
    if len(rows) < limits.growth_tests:
        return []
    steps = [_live_step(record) for record in rows]
    total = sum(steps)
    biggest = max(steps)
    blocks = [_blocks_kept(record) for record in rows]
    growing = sum(
        1 for step, kept in zip(steps, blocks) if step > 0 or (kept is not None and kept > 0)
    )
    if total < limits.retained_mb or biggest >= total / 2 or growing < len(rows) / 2:
        return []
    counted = [kept for kept in blocks if kept is not None]
    objects = sum(counted) // len(rows) if counted else None
    first, last = rows[0], rows[-1]
    per_test = total / len(rows)
    by_name: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for record, step in zip(rows, steps):
        entry = by_name[str(record["nodeid"]).split("[")[0]]
        entry[0] += step
        entry[1] += 1
    resident = sum(_figures(record)[1] - _figures(record)[0] for record in rows)
    traced = _figures(first)[3]
    evidence = [
        f"No single test kept enough to be reported on its own. {growing} of the {len(rows)} "
        "tests each ended with more in use than they started with.",
    ]
    if len(by_name) == 1:
        evidence.append(f"All of them are cases of {next(iter(by_name))}.")
    else:
        heaviest = sorted(by_name.items(), key=lambda item: -item[1][0])[:3]
        evidence.append(
            "Most of it during: "
            + ", ".join(
                f"{name} ({megabytes} MB over {count} test{'s' if count != 1 else ''})"
                for name, (megabytes, count) in heaviest
                if megabytes > 0
            )
            + "."
        )
    frame: Optional[FrameRef] = None
    stack: list[str] = []
    if session is not None:
        frame, stack, held = _holders(
            session, "holders_session", "Held at the end of the worker", attributor, None, []
        )
        evidence.extend(held)
    else:
        evidence.append(
            "Look at: rerun those tests with --failure-profile-allocations to see which lines "
            "hold the memory."
        )
    evidence.append(
        f"Measured: {'traced memory' if traced else 'process'} {_figures(first)[0]} MB before the "
        f"first of these tests, {_figures(last)[1]} MB after the last. Biggest single step {biggest} MB."
        + (
            f" {total} MB of the {resident} MB increase is in use; the rest was freed and kept by "
            "the allocator."
            if resident > total + 1
            else ""
        )
        + (f" +{objects:,d} Python objects per test." if objects else "")
    )
    return [
        Finding(
            kind="memory_profile",
            verdict="STEADY_GROWTH",
            evidence=evidence,
            nodeid=str(first["nodeid"]),
            worker=worker,
            before_mb=_figures(first)[0],
            after_mb=_figures(last)[1],
            delta_mb=total,
            growth_tests=len(rows),
            growth_per_test_mb=round(per_test, 1),
            growth_objects_per_test=objects,
            tests=[str(record["nodeid"]) for record in rows[:3]],
            test_count=len(rows),
            frame=frame,
            stack=stack,
        )
    ]


def _kept(record: dict[str, Any]) -> int:
    """What a test left behind: the resident step, or the live-heap step
    when that is larger - or the traced step, when tracing was on.

    Resident memory understates what a test kept whenever the test reuses
    pages an earlier test freed and the allocator held on to. The cache test
    in the examples kept 146 MB and grew the process by 93, because the
    loader before it had just given 500 MB back to the allocator. The heap
    in-use figure is not fooled by that, so the larger of the two is what a
    test is held to.
    """
    before, after, _, traced = _figures(record)
    resident = after - before
    if traced:
        return resident
    heap_before, heap_after = record.get("heap_before_mb"), record.get("heap_after_mb")
    if heap_before is None or heap_after is None:
        return resident
    return max(resident, int(heap_after) - int(heap_before))


def _live_step(record: dict[str, Any]) -> int:
    """What a test added to memory that is in use, for the drift rule: the
    C heap's step plus the small-object blocks' at their rough size, where
    both were read; the resident step where neither was."""
    if _figures(record)[3]:
        return _kept(record)
    heap_before, heap_after = record.get("heap_before_mb"), record.get("heap_after_mb")
    blocks = _blocks_kept(record)
    if heap_before is None or heap_after is None or blocks is None:
        return _kept(record)
    return int(heap_after) - int(heap_before) + int(blocks * BYTES_PER_BLOCK / 1048576)


def _blocks_kept(record: dict[str, Any]) -> Optional[int]:
    before, after = record.get("blocks_before"), record.get("blocks_after")
    if before is None or after is None:
        return None
    return int(after) - int(before)


def _imbalance(by_worker: dict[str, list[dict[str, Any]]], limits: Thresholds) -> list[Finding]:
    if len(by_worker) < 2:
        return []
    peaks = {
        worker: max(int(record.get("rss_peak_mb") or record["rss_after_mb"]) for record in tests)
        for worker, tests in by_worker.items()
        if tests
    }
    if len(peaks) < 2:
        return []
    worst_worker, worst = max(peaks.items(), key=lambda item: item[1])
    # Against its siblings, not against a median it is part of: with two
    # workers the median of both is the midpoint, and a worker at four times
    # its sibling would read as under twice the median.
    typical = int(median(rss for worker, rss in peaks.items() if worker != worst_worker))
    if worst < typical * limits.imbalance_ratio or worst - typical < limits.retained_mb:
        return []
    # The test after which this worker first stood clear of its siblings.
    line = typical + limits.retained_mb // 2
    diverged = next(
        (
            record
            for record in by_worker[worst_worker]
            if int(record.get("rss_peak_mb") or record["rss_after_mb"]) >= line
        ),
        None,
    )
    evidence = []
    if diverged is not None:
        evidence.append(f"{worst_worker} first exceeded the others during {diverged['nodeid']}.")
    evidence.append(
        "xdist assigns tests to workers as they free up, so the worker that ran this test is "
        "the one holding the memory."
    )
    if diverged is not None:
        evidence.append(f"Look at: {diverged['nodeid']}, and the findings about it.")
    evidence.append("Measured: " + ", ".join(f"{name} {rss} MB" for name, rss in sorted(peaks.items())) + ".")
    return [
        Finding(
            kind="memory_profile",
            verdict="WORKER_IMBALANCE",
            evidence=evidence,
            nodeid=str(diverged["nodeid"]) if diverged is not None else None,
            worker=worst_worker,
            peak_mb=worst,
            median_mb=typical,
            delta_mb=worst - typical,
            worker_rss=dict(sorted(peaks.items())),
        )
    ]


# -- bursts ----------------------------------------------------------------------

#: A burst is at least this many consecutive busy windows: one window is a
#: tick's worth of noise, two is a fifth of a second of a core.
MIN_BURST_WINDOWS = 2
#: Per mille of the machine busy for a window to count as pinned.
PINNED_PERMILLE = 900


@dataclass
class _Burst:
    """Consecutive timeline windows at or over the core threshold."""

    nodeid: Optional[str]
    worker: str
    started_s: float
    seconds: float = 0.0
    cpu_ns: int = 0
    windows: int = 0
    phases: Counter = field(default_factory=Counter)
    threads: Counter = field(default_factory=Counter)
    machine: list[int] = field(default_factory=list)
    #: (thread, frame indexes) -> cpu, for the stack that stands for it.
    stacks: Counter = field(default_factory=Counter)

    @property
    def cores(self) -> float:
        return self.cpu_ns / (self.seconds * 1e9) if self.seconds else 0.0

    @property
    def phase(self) -> Optional[str]:
        return self.phases.most_common(1)[0][0] if self.phases else None

    @property
    def thread(self) -> Optional[str]:
        return self.threads.most_common(1)[0][0] if self.threads else None

    @property
    def machine_busy_percent(self) -> Optional[float]:
        if not self.machine:
            return None
        return round(sum(self.machine) / len(self.machine) / 10, 1)


def _bursts(record: dict[str, Any], limits: Thresholds) -> list[_Burst]:
    """The bursts in one record's timeline.

    A window is busy when the process burnt ``burst_cores`` cores' worth of
    CPU over it; a burst is a run of busy windows. The window's stack is
    the one that burnt the most in it, and the burst keeps every window's,
    so the blame is the stack that was there most of the time rather than
    the one that happened to be there at the end.
    """
    nodeid = record.get("nodeid")
    worker = str(record.get("worker") or "")
    bursts: list[_Burst] = []
    current: Optional[_Burst] = None
    #: A quiet window inside a burst - a collection, a page fault storm, a
    #: read - does not end it; two in a row do. Held back until the next
    #: window says which it was.
    pending: Optional[tuple[int, int, Any]] = None
    previous = 0

    def close() -> None:
        nonlocal current, pending
        if current is not None and current.windows >= MIN_BURST_WINDOWS:
            bursts.append(current)
        current = None
        pending = None

    for entry in record.get("timeline") or []:
        try:
            offset_ms, cpu_ns, machine, phase, thread, indexes = entry
            offset_ms, cpu_ns = int(offset_ms), int(cpu_ns or 0)
        except (TypeError, ValueError):
            continue
        wall_ms = offset_ms - previous
        previous = offset_ms
        if wall_ms <= 0:
            continue
        if cpu_ns / (wall_ms * 1e6) < limits.burst_cores:
            if current is None or pending is not None:
                close()
            else:
                pending = (wall_ms, cpu_ns, phase)
            continue
        if current is None:
            current = _Burst(nodeid, worker, (offset_ms - wall_ms) / 1000)
        elif pending is not None:
            gap_ms, gap_cpu_ns, gap_phase = pending
            current.seconds += gap_ms / 1000
            current.cpu_ns += gap_cpu_ns
            current.phases[gap_phase] += gap_ms
            pending = None
        current.seconds += wall_ms / 1000
        current.cpu_ns += cpu_ns
        current.windows += 1
        current.phases[phase] += wall_ms
        if thread is not None:
            current.threads[str(thread)] += cpu_ns
        if machine is not None:
            current.machine.append(int(machine))
        if indexes:
            current.stacks[(str(thread), tuple(indexes))] += cpu_ns
    close()
    return bursts


def _burst_blame(burst: _Burst, table: list[FrameRef]) -> tuple[Optional[FrameRef], list[FrameRef]]:
    for (_, indexes), _ in burst.stacks.most_common():
        try:
            frames = _without_the_instrumentation([table[index] for index in indexes])
        except (IndexError, TypeError):
            continue
        if frames:
            blamed_at = _blame_index(frames)
            return frames[blamed_at], frames[blamed_at:]
    return None, []


def _where(stack: list[FrameRef]) -> str:
    if not stack:
        return ""
    frame = stack[0]
    where = f"Running {_named(frame)}"
    caller = _caller(stack)
    if caller is not None:
        where += f", called from {_named(caller)}"
    return where + "."


def _caller(stack: list[FrameRef]) -> Optional[FrameRef]:
    """The first frame above the blamed one that is a different function:
    a comprehension folded into its function is not called from itself."""
    if not stack:
        return None
    blamed = stack[0]
    return next(
        (
            frame
            for frame in stack[1:]
            if (frame.file, frame.function) != (blamed.file, blamed.function)
        ),
        None,
    )


def _machine_line(burst: _Burst) -> list[str]:
    busy = burst.machine_busy_percent
    if busy is None:
        return []
    line = f"Machine load during the burst: {_pct(busy)}."
    if busy * 10 >= PINNED_PERMILLE:
        line += " The machine was saturated, so this took longer than its CPU time."
    return [line]


def _burst_findings(
    records: list[dict[str, Any]], limits: Thresholds, attributor: Attributor
) -> list[Finding]:
    """Long bursts per test, recurring bursts per function, and bursts on
    threads that are not running the test - one finding each, however many
    bursts stand behind it."""
    findings: list[Finding] = []
    by_function: dict[tuple[str, str], list[tuple[_Burst, list[FrameRef], dict[str, Any]]]] = defaultdict(list)
    per_test: dict[str, list[tuple[_Burst, list[FrameRef], dict[str, Any]]]] = defaultdict(list)
    background: dict[tuple[str, str], list[tuple[_Burst, list[FrameRef], dict[str, Any]]]] = defaultdict(list)
    for record in records:
        bursts = _bursts(record, limits)
        if not bursts:
            continue
        table = [_frame(entry, attributor) for entry in record.get("frames") or []]
        test_thread = next(
            (str(stack.get("thread")) for stack in record.get("stacks") or [] if not stack.get("background")),
            None,
        )
        for burst in bursts:
            frame, stack = _burst_blame(burst, table)
            on_another_thread = (
                test_thread is not None and burst.thread is not None and burst.thread != test_thread
            )
            if record.get("record") != TEST_RECORD or on_another_thread:
                background[(burst.worker, burst.thread or "")].append((burst, stack, record))
            elif frame is not None:
                by_function[(frame.file, frame.function)].append((burst, stack, record))
                per_test[str(burst.nodeid)].append((burst, stack, record))
            else:
                per_test[str(burst.nodeid)].append((burst, stack, record))

    recurring: set[str] = set()
    for group in by_function.values():
        tests = {str(burst.nodeid) for burst, _, _ in group}
        if len(tests) < limits.burst_tests:
            continue
        findings.append(_recurring_burst(group, limits))
        recurring.update(tests)

    for nodeid, group in per_test.items():
        if nodeid in recurring:
            continue
        burst, stack, record = max(group, key=lambda item: item[0].seconds)
        if burst.seconds >= limits.burst_seconds:
            findings.append(_long_burst(burst, stack, record, limits))

    for group in background.values():
        burst, stack, record = max(group, key=lambda item: item[0].seconds)
        if burst.seconds >= limits.burst_seconds:
            findings.append(_background_burst(burst, stack, record, len(group)))
    return findings


def _long_burst(burst: _Burst, stack: list[FrameRef], record: dict[str, Any], limits: Thresholds) -> Finding:
    wall = float(record.get("wall_s") or 0.0)
    cpu = float(record.get("cpu_s") or 0.0)
    evidence = [_where(stack)] if stack else []
    if cpu > 0 and wall > 0:
        share = min(100.0, _percent(burst.cpu_ns / 1e9, cpu))
        waiting = max(0.0, wall - cpu)
        if waiting >= max(1.0, burst.seconds / 3):
            evidence.append(
                f"This burst is {_pct(share)} of the test's {cpu:.1f} s of CPU. The other "
                f"{waiting:.1f} s of the test's {wall:.1f} s was waiting."
            )
        else:
            evidence.append(
                f"This burst is {_pct(share)} of the test's {cpu:.1f} s of CPU, and the test was "
                f"busy for nearly all of its {wall:.1f} s."
            )
    evidence.extend(_machine_line(burst))
    if stack:
        evidence.append(f"Look at: {_place(stack[0])}")
    return Finding(
        kind="cpu_burst",
        verdict="LONG_BURST",
        evidence=evidence,
        frame=stack[0] if stack else None,
        stack=_faulthandler_lines(stack),
        nodeid=burst.nodeid,
        worker=burst.worker,
        phase=burst.phase,
        thread=burst.thread,
        cpu_seconds=round(burst.cpu_ns / 1e9, 3),
        burst_seconds=round(burst.seconds, 2),
        cores=round(burst.cores, 2),
        started_s=round(burst.started_s, 2),
        machine_busy_percent=burst.machine_busy_percent,
        tests=[str(burst.nodeid)],
        test_count=1,
    )


def _recurring_burst(
    group: list[tuple[_Burst, list[FrameRef], dict[str, Any]]], limits: Thresholds
) -> Finding:
    bursts = [burst for burst, _, _ in group]
    tests: Counter = Counter()
    for burst in bursts:
        tests[str(burst.nodeid)] += burst.cpu_ns
    total_cpu = sum(burst.cpu_ns for burst in bursts) / 1e9
    total_seconds = sum(burst.seconds for burst in bursts)
    typical = median(burst.seconds for burst in bursts)
    phases: Counter = Counter()
    for burst in bursts:
        phases.update(burst.phases)
    phase = phases.most_common(1)[0][0] if phases else None
    _, stack, _ = max(group, key=lambda item: item[0].cpu_ns)
    machine = [value for burst in bursts for value in burst.machine]
    busy = round(sum(machine) / len(machine) / 10, 1) if machine else None
    caller = _caller(stack)
    evidence = [
        f"{total_cpu:.1f} s of CPU in total across the {len(bursts)} bursts."
        + (f" Called from {_named(caller)}." if caller is not None else ""),
        "Tests: " + _listed([nodeid for nodeid, _ in tests.most_common(3)], len(tests)) + ".",
    ]
    if busy is not None:
        evidence.append(f"Machine load during these bursts: {_pct(busy)}.")
    if stack:
        look = f"Look at: {_place(stack[0])}."
        if phase:
            look += f" It ran during {phase} of each of those tests."
        evidence.append(look)
    longest = max(bursts, key=lambda burst: burst.seconds)
    return Finding(
        kind="cpu_burst",
        verdict="RECURRING_BURST",
        evidence=evidence,
        frame=stack[0] if stack else None,
        stack=_faulthandler_lines(stack),
        worker=longest.worker,
        phase=phase,
        thread=longest.thread,
        cpu_seconds=round(total_cpu, 3),
        burst_seconds=round(typical, 2),
        cores=round(total_cpu / total_seconds, 2) if total_seconds else 0.0,
        started_s=round(longest.started_s, 2),
        machine_busy_percent=busy,
        tests=[nodeid for nodeid, _ in tests.most_common(3)],
        test_count=len(tests),
    )


def _background_burst(burst: _Burst, stack: list[FrameRef], record: dict[str, Any], count: int) -> Finding:
    evidence = [
        (_where(stack) + " " if stack else "") + "This thread is not the one running tests."
    ]
    if count > 1:
        evidence.append(f"{count} bursts like it on this thread.")
    evidence.extend(_machine_line(burst))
    if stack:
        evidence.append(f"Look at: {_place(stack[0])}")
    return Finding(
        kind="cpu_burst",
        verdict="BACKGROUND_BURST",
        evidence=evidence,
        frame=stack[0] if stack else None,
        stack=_faulthandler_lines(stack),
        nodeid=burst.nodeid,
        worker=burst.worker,
        phase=burst.phase,
        thread=burst.thread,
        cpu_seconds=round(burst.cpu_ns / 1e9, 3),
        burst_seconds=round(burst.seconds, 2),
        cores=round(burst.cores, 2),
        started_s=round(burst.started_s, 2),
        machine_busy_percent=burst.machine_busy_percent,
        tests=[str(burst.nodeid)] if burst.nodeid else [],
        test_count=1 if burst.nodeid else 0,
    )


def _contention_finding(
    records: list[dict[str, Any]], limits: Thresholds, worker_count: int
) -> list[Finding]:
    """The machine was pinned for most of the run and the workers got slices.

    Twenty workers on four cores read as twenty processes at a few percent
    each, and every test takes longer than its CPU says it should. Nothing
    on any stack explains it; the machine's own figure does.
    """
    busy_ms = total_ms = 0
    busy_cpu_ns = 0
    cpus: Optional[int] = None
    for record in records:
        cpus = int(record.get("cpus") or 0) or cpus
        previous = 0
        for entry in record.get("timeline") or []:
            try:
                offset_ms, cpu_ns, machine = int(entry[0]), int(entry[1] or 0), entry[2]
            except (TypeError, ValueError, IndexError):
                continue
            wall_ms = offset_ms - previous
            previous = offset_ms
            if wall_ms <= 0:
                continue
            total_ms += wall_ms
            if machine is not None and int(machine) >= PINNED_PERMILLE:
                busy_ms += wall_ms
                busy_cpu_ns += cpu_ns
    if total_ms == 0 or busy_ms < limits.burst_seconds * 1000:
        return []
    share = busy_ms / total_ms
    cores = busy_cpu_ns / (busy_ms * 1e6)
    if share < 0.5 or cores >= limits.burst_cores:
        return []
    waiting = "Test durations include waiting for a core, so they are longer than the tests' CPU time."
    workers = f"{worker_count} worker{'s' if worker_count != 1 else ''} on {cpus} core{'s' if cpus != 1 else ''}"
    if cpus and worker_count > cpus:
        evidence = [f"{workers}. {waiting}"]
    elif cpus:
        evidence = [f"{workers}, so the load was not only this run's workers. {waiting}"]
    else:
        evidence = [waiting]
    evidence.append("Look at: the worker count, and what else was running on the machine.")
    return [
        Finding(
            kind="cpu_burst",
            verdict="CONTENDED",
            evidence=evidence,
            cores=round(cores, 2),
            burst_seconds=round(busy_ms / 1000, 1),
            machine_busy_percent=round(100 * share, 1),
            cpus=cpus,
            worker_count=worker_count,
        )
    ]


# -- flame graph files -----------------------------------------------------------


def speedscope(record: dict[str, Any], name: str) -> dict[str, Any]:
    """One test's record as a speedscope document: a profile per thread,
    weighted in nanoseconds of CPU, openable in speedscope or any reader of
    that format."""
    frames = []
    for entry in record.get("frames") or []:
        file, line, function = _parse_frame(entry)
        frames.append({"name": function, "file": file, "line": line})
    by_thread: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for stack in record.get("stacks") or []:
        by_thread[str(stack.get("thread"))].append(stack)
    profiles = []
    for thread, stacks in by_thread.items():
        samples = [list(reversed(stack["frames"])) for stack in stacks]
        weights = [int(stack.get("cpu_ns") or 0) for stack in stacks]
        profiles.append(
            {
                "type": "sampled",
                "name": f"{thread} (cpu)",
                "unit": "nanoseconds",
                "startValue": 0,
                "endValue": sum(weights),
                "samples": samples,
                "weights": weights,
            }
        )
    return _speedscope_document(name, frames, profiles)


def memory_speedscope(record: dict[str, Any], name: str) -> Optional[dict[str, Any]]:
    """One test's live allocations at its peak as a speedscope document,
    weighted in bytes: a memory flame graph. None when the record has no
    allocation stacks - tracing was off, or the test never climbed."""
    stacks = record.get("memory_stacks") or []
    if not stacks:
        return None
    frames = []
    for entry in record.get("frames") or []:
        file, line, function = _parse_frame(entry)
        frames.append({"name": function or f"{Path(file).name}:{line}", "file": file, "line": line})
    # A tracemalloc traceback is oldest frame first, which is the order
    # speedscope wants: the root at the front.
    samples = [list(stack["frames"]) for stack in stacks]
    weights = [int(stack.get("bytes") or 0) for stack in stacks]
    profile = {
        "type": "sampled",
        "name": "live allocations at the peak (bytes)",
        "unit": "bytes",
        "startValue": 0,
        "endValue": sum(weights),
        "samples": samples,
        "weights": weights,
    }
    return _speedscope_document(name, frames, [profile])


def _speedscope_document(
    name: str, frames: list[dict[str, Any]], profiles: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "$schema": "https://www.speedscope.app/file-format-schema.json",
        "name": name,
        "exporter": "pytest-failure-instrumentation",
        "shared": {"frames": frames},
        "profiles": profiles,
    }
