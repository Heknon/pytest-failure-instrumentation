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
``TRANSIENT_PEAK``      a test climbed and came back down
``STEADY_GROWTH``       a run of tests each left a little behind
``WORKER_IMBALANCE``    one worker holds far more than its siblings
``PEAK_OVER_CEILING``   a test reached the absolute size nothing may reach
======================= =====================================================

A memory finding about one test also names the code that was running while
the memory climbed. The sampler charges every rise in resident memory to the
test thread's stack at that moment, and those stacks are blamed the same way
CPU is - so "peaked 900 MB" comes with "under ``load_everything`` in
``reports.py``", which is the difference between a number and a fix.
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
    growth_tests: int = 0
    growth_per_test_mb: float = 0.0
    worker_rss: dict[str, int] = field(default_factory=dict)
    median_mb: Optional[int] = None
    #: For a memory finding: how much of the climb was charged to the blamed
    #: stack, out of how much was charged at all.
    climb_mb: int = 0
    climb_total_mb: int = 0


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
    workers: dict[str, dict[str, Any]]
    tests: int


# -- reading records ------------------------------------------------------------


def _frame(entry: str, attributor: Attributor) -> FrameRef:
    file, _, rest = entry.partition("|")
    line, _, function = rest.partition("|")
    try:
        number = int(line)
    except ValueError:
        number = 0
    return FrameRef(file, number, _enclosing(function), attributor.owner_of(file))


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


def _blame_index(frames: list[FrameRef]) -> int:
    for owners in (OWNED, ("third-party",)):
        for index, frame in enumerate(frames):
            if frame.owner in owners:
                return index
    return 0


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
    workers: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"tests": 0, "cpu_s": 0.0, "wall_s": 0.0, "peak_mb": None, "end_mb": None, "gc_s": 0.0}
    )
    gc_by_test: dict[str, float] = defaultdict(float)
    native_by_name: Counter = Counter()
    tests = 0

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
        seconds = float((record.get("gc") or {}).get("seconds") or 0.0)
        gc_seconds += seconds
        if record.get("nodeid") and seconds:
            gc_by_test[record["nodeid"]] += seconds
        for thread in record.get("native_threads") or []:
            native_cpu += int(thread.get("cpu_ns") or 0)
            native_by_name[str(thread.get("name"))] += int(thread.get("cpu_ns") or 0)

        table = [_frame(entry, attributor) for entry in record.get("frames") or []]
        nodeid = record.get("nodeid") or f"({BACKGROUND_RECORD} on {worker or 'this process'})"
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
            cost.tests[nodeid] += cpu_ns
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
    findings.extend(_cpu_findings(ranked, total_cpu_ns, limits))
    findings.extend(_gc_finding(gc_seconds, total_cpu_ns, gc_by_test, limits))
    findings.extend(_native_finding(native_cpu, native_by_name, total_cpu_ns, limits))
    findings.extend(_memory_findings(records, limits, attributor))

    return Report(
        findings=findings,
        functions=ranked,
        sampled_cpu_s=sampled_cpu / 1e9,
        process_cpu_s=process_cpu,
        wall_s=wall,
        gc_s=gc_seconds,
        native_cpu_s=native_cpu / 1e9,
        cpu_weighted=cpu_weighted,
        workers=dict(workers),
        tests=tests,
    )


def _percent(part: float, whole: float) -> float:
    return round(100.0 * part / whole, 1) if whole > 0 else 0.0


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
        evidence = [
            f"{share:g}% of the run's CPU ({seconds:.1f} s) across "
            f"{len(cost.tests)} test(s), on thread {thread!r}",
        ]
        if background >= 50.0:
            verdict = "BACKGROUND_THREAD"
            evidence.append(
                f"{background:g}% of it is on a thread other than the one running the "
                "test: this cost is paid whatever test is in flight"
            )
        elif below_share >= 50.0 and cost.below:
            verdict = "LIBRARY_CALL"
            (file, owner), below_ns = cost.below.most_common(1)[0]
            function = cost.below_functions.most_common(1)[0][0]
            below_frame = FrameRef(file, 0, function, owner)
            evidence.append(
                f"{_percent(below_ns, cost.cpu_ns):g}% of it is below "
                f"{Path(file).name} ({owner}), mostly in {function}: the cost is in "
                "what this function calls, not in its own lines"
            )
        else:
            verdict = "PYTHON_CODE"
            evidence.append(
                f"{self_share:g}% of it is on this function's own lines: Python-level "
                "work, or C calls made from them that leave no frame of their own"
            )
        hottest = [
            (line, _percent(line_ns, cost.cpu_ns)) for line, line_ns in cost.lines.most_common(3)
        ]
        if hottest and verdict == "PYTHON_CODE":
            evidence.append(
                "hottest lines: "
                + ", ".join(f"line {line} ({percent:g}%)" for line, percent in hottest)
            )
        examples = [nodeid for nodeid, _ in cost.tests.most_common(3)]
        frame = cost.representative[0] if cost.representative else None
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
        f"the garbage collector took {gc_seconds:.1f} s, {share:g}% of the run's CPU",
        "collections are triggered by allocation volume: a test building millions of "
        "small objects pays for full passes over everything the process holds",
    ]
    if worst:
        evidence.append(
            "most of it in: "
            + ", ".join(f"{nodeid} ({seconds:.1f} s)" for nodeid, seconds in worst)
        )
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
                f"{share:g}% of the run's CPU ({seconds:.1f} s) was burnt by threads "
                "Python has no stack for - a thread pool started by a C extension, "
                "or a runtime of its own",
                f"by kernel thread name: {names}",
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
    wins. Returns the frame, its stack as faulthandler lines, the megabytes
    charged to it, the megabytes charged in total, and the evidence sentence.
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
        try:
            frames = _without_the_instrumentation([table[index] for index in entry["frames"]])
        except (IndexError, KeyError, TypeError):
            continue
        total += megabytes
        if not frames:
            unplaced += megabytes
            continue
        blamed_at = _blame_index(frames)
        blamed = frames[blamed_at]
        key = (blamed.file, blamed.function)
        charged[key] += megabytes
        if key not in heaviest or megabytes > heaviest[key][0]:
            heaviest[key] = (megabytes, frames[blamed_at:])
    too_quick = (
        f"{unplaced} MB of the {total} MB climb happened between two readings and "
        "could not be placed under a stack"
    )
    if not charged:
        return None, [], 0, total, [too_quick] if unplaced else []
    key, megabytes = max(charged.items(), key=lambda item: item[1])
    stack = heaviest[key][1]
    frame = stack[0]
    evidence = [
        f"{megabytes} MB of the {total} MB climb happened under "
        f"{Path(frame.file).name}:{frame.line} in {frame.function} ({frame.owner})"
    ]
    if len(stack) > 1:
        evidence[0] += f", called from {Path(stack[1].file).name}:{stack[1].line} in {stack[1].function}"
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

    for worker, tests in by_worker.items():
        for record in tests:
            before, after = int(record["rss_before_mb"]), int(record["rss_after_mb"])
            peak = int(record.get("rss_peak_mb") or max(before, after))
            retained = _kept(record)
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
                frame, stack, climb, climb_total, climb_evidence = _climb(record, attributor)
                findings.append(
                    Finding(
                        kind="memory_profile",
                        verdict="PEAK_OVER_CEILING",
                        evidence=[
                            f"resident memory reached {peak} MB during the test, over the "
                            f"{limits.peak_mb} MB ceiling; it started at {before} MB and "
                            f"ended at {after} MB",
                            "the size is the finding whatever happened to it afterwards: "
                            "a worker that reaches this once needs the machine to have "
                            "it, and a run with several of them is an OOM kill waiting "
                            "for the right schedule",
                        ]
                        + climb_evidence
                        + (
                            [f"and {retained} MB of it was still there once the test was over"]
                            if retained >= limits.retained_mb
                            else []
                        ),
                        nodeid=record["nodeid"],
                        worker=worker,
                        before_mb=before,
                        after_mb=after,
                        peak_mb=peak,
                        delta_mb=peak - before,
                        frame=frame,
                        stack=stack,
                        climb_mb=climb,
                        climb_total_mb=climb_total,
                    )
                )
            elif retained >= limits.retained_mb:
                phase = _phase_of_step(record.get("rss_at") or {}, limits.retained_mb)
                evidence = [
                    f"resident memory {before} MB before, {after} MB after: "
                    f"{after - before} MB kept once the test was over",
                ]
                if retained > after - before:
                    evidence[0] = (
                        f"resident memory {before} MB before, {after} MB after, but the "
                        f"live heap grew {retained} MB: the test filled pages an earlier "
                        "test had freed, so the resident figure understates what it kept"
                    )
                live, live_evidence = _still_in_use(record, retained)
                evidence.extend(live_evidence)
                frame, stack, climb, climb_total, climb_evidence = _climb(record, attributor)
                evidence.extend(climb_evidence)
                if live is False:
                    findings.append(
                        Finding(
                            kind="memory_profile",
                            verdict="HEAP_NOT_RETURNED",
                            evidence=evidence
                            + [
                                "freed, but the allocator kept the pages mapped: fragmentation "
                                "rather than a leak. Later allocations reuse it; what it costs "
                                "is the worker's footprint, and the fix is isolating the test "
                                "that needs it rather than hunting a leak"
                            ],
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
                        "the step happened during setup, so a fixture built it - a "
                        "session or module fixture keeps it for the rest of the run"
                    )
                elif phase == "call":
                    evidence.append(
                        "the step happened in the test's own body and survived teardown: "
                        "a cache, a module-level list, or a leak"
                    )
                elif phase:
                    evidence.append(f"the step happened during {phase}")
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
                frame, stack, climb, climb_total, climb_evidence = _climb(record, attributor)
                risen = peak - before
                findings.append(
                    Finding(
                        kind="memory_profile",
                        verdict="TRANSIENT_PEAK",
                        evidence=[
                            f"resident memory climbed {risen} MB to {peak} MB during the "
                            f"test and came back to {after} MB",
                            "freed on return, so it costs peak memory rather than a leak: "
                            "it is what decides how many workers fit on the machine",
                        ]
                        + climb_evidence,
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
        findings.extend(_growth(worker, tests, limits))

    findings.extend(_imbalance(by_worker, limits))
    return findings


#: A rough size for one small-object block, to turn a block count into
#: megabytes a reader can set against the resident figure. pymalloc blocks
#: are at most 512 bytes and most are far smaller.
BYTES_PER_BLOCK = 64


def _still_in_use(record: dict[str, Any], retained_mb: int) -> tuple[Optional[bool], list[str]]:
    """Whether the memory a test left behind is alive, from the live-heap
    readings, and the sentence that says so. None where nothing was read."""
    heap_before, heap_after = record.get("heap_before_mb"), record.get("heap_after_mb")
    blocks_before, blocks_after = record.get("blocks_before"), record.get("blocks_after")
    evidence: list[str] = []
    live_mb = 0
    measured = False
    if heap_before is not None and heap_after is not None:
        measured = True
        grew = int(heap_after) - int(heap_before)
        live_mb += max(0, grew)
        evidence.append(
            f"the C allocator has {grew:+d} MB more in use than before the test"
        )
    if blocks_before is not None and blocks_after is not None:
        measured = True
        grew_blocks = int(blocks_after) - int(blocks_before)
        live_mb += max(0, grew_blocks * BYTES_PER_BLOCK // 1048576)
        evidence.append(
            f"Python holds {grew_blocks:+,d} small-object blocks more than before"
        )
    if not measured:
        return None, evidence
    return live_mb >= retained_mb // 2, evidence


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


def _growth(worker: str, tests: list[dict[str, Any]], limits: Thresholds) -> list[Finding]:
    """A run of tests that each left a little: the shape of a leak.

    A step has to be worth noticing on its own - a twentieth of the threshold
    - so the megabyte a poller's buffers add to every test does not stitch
    unrelated tests into one run. And the run is trimmed at both ends to the
    steps that look like each other: a test that happened to leave five
    megabytes just before the leaking ones started is not the first of them.
    """
    floor = max(2, limits.retained_mb // 20)
    run: list[dict[str, Any]] = []
    best: list[dict[str, Any]] = []
    for record in tests:
        step = _step(record)
        if floor <= step < limits.retained_mb:
            run.append(record)
        else:
            best = _better(best, _trimmed(run))
            run = []
    best = _better(best, _trimmed(run))
    total = _growth_total(best)
    if len(best) < limits.growth_tests or total < limits.retained_mb:
        return []
    first, last = best[0], best[-1]
    names = {str(record["nodeid"]).split("[")[0] for record in best}
    evidence = [
        f"{len(best)} consecutive tests on {worker} each left memory behind: "
        f"{int(first['rss_before_mb'])} MB before {first['nodeid']} to "
        f"{int(last['rss_after_mb'])} MB after {last['nodeid']}, "
        f"{total / len(best):.0f} MB per test",
        "no single test crossed the threshold, so a per-test check would never flag this; "
        "the pattern is what a leak looks like",
    ]
    if len(names) == 1:
        evidence.append(f"every one of them is a parametrisation of {names.pop()}")
    return [
        Finding(
            kind="memory_profile",
            verdict="STEADY_GROWTH",
            evidence=evidence,
            nodeid=str(first["nodeid"]),
            worker=worker,
            before_mb=int(first["rss_before_mb"]),
            after_mb=int(last["rss_after_mb"]),
            delta_mb=total,
            growth_tests=len(best),
            growth_per_test_mb=round(total / len(best), 1),
            tests=[str(record["nodeid"]) for record in best[:3]],
            test_count=len(best),
        )
    ]


def _step(record: dict[str, Any]) -> int:
    return _kept(record)


def _kept(record: dict[str, Any]) -> int:
    """What a test left behind: the resident step, or the live-heap step
    when that is larger.

    Resident memory understates what a test kept whenever the test reuses
    pages an earlier test freed and the allocator held on to. The cache test
    in the examples kept 146 MB and grew the process by 93, because the
    loader before it had just given 500 MB back to the allocator. The heap
    in-use figure is not fooled by that, so the larger of the two is what a
    test is held to.
    """
    resident = int(record["rss_after_mb"]) - int(record["rss_before_mb"])
    heap_before, heap_after = record.get("heap_before_mb"), record.get("heap_after_mb")
    if heap_before is None or heap_after is None:
        return resident
    return max(resident, int(heap_after) - int(heap_before))


def _trimmed(run: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The run without the small steps at either end of it."""
    if len(run) < 3:
        return run
    typical = median(_step(record) for record in run)
    start, end = 0, len(run)
    while start < end and _step(run[start]) < typical / 2:
        start += 1
    while end > start and _step(run[end - 1]) < typical / 2:
        end -= 1
    return run[start:end]


def _better(best: list[dict[str, Any]], candidate: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return candidate if _growth_total(candidate) > _growth_total(best) else best


def _growth_total(run: list[dict[str, Any]]) -> int:
    return sum(_kept(record) for record in run)


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
    evidence = [
        f"{worst_worker} peaked at {worst} MB while the median worker peaked at {typical} MB",
        "xdist hands tests out one at a time, so the worker that happened to receive the "
        "test or fixture that builds this is the one that holds it",
    ]
    if diverged is not None:
        evidence.append(f"it first stood clear of its siblings during {diverged['nodeid']}")
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


# -- flame graph files -----------------------------------------------------------


def speedscope(record: dict[str, Any], name: str) -> dict[str, Any]:
    """One test's record as a speedscope document: a profile per thread,
    weighted in nanoseconds of CPU, openable in speedscope or any reader of
    that format."""
    frames = []
    for entry in record.get("frames") or []:
        file, _, rest = entry.partition("|")
        line, _, function = rest.partition("|")
        frames.append({"name": function, "file": file, "line": int(line or 0)})
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
    return {
        "$schema": "https://www.speedscope.app/file-format-schema.json",
        "name": name,
        "exporter": "pytest-failure-instrumentation",
        "shared": {"frames": frames},
        "profiles": profiles,
    }
