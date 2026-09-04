"""What the profiler found, as incidents.

Three kinds. A CPU hotspot has a frame and is attributed like a crash - the
engine reads its stack and names the owner - so "your ``is_images_different``
is 38% of the run" arrives through ``pytest_failure_incident`` with the same
``owner``, ``severity`` and ``fingerprint`` a segfault would carry. A CPU
burst is a hotspot in time rather than in share: a test, a fixture or a
background thread that held a core for a stretch of a run that otherwise
waited, with the stack that was there for most of it. A memory finding names
the test the memory arrived in, and carries the stack that was running while
it climbed - or, with allocation tracing on, the lines holding it - when one
was seen, and leaves attribution to ``suspect_owner`` otherwise.

All are informational whatever the owner, because nothing failed. They are
flags for a reader to look at, and severity says so - see
:mod:`..analysis.severity`.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from ..profile.analysis import Finding
from .base import Frame, Incident


class HotLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line: int
    percent: float


class CpuHotspotIncident(Incident):
    model_config = ConfigDict(extra="forbid")

    ends_run: ClassVar[bool] = False

    kind: Literal["cpu_hotspot"] = "cpu_hotspot"

    #: Seconds of CPU charged to the blamed function over the whole run.
    cpu_seconds: float = 0.0
    #: As a share of every second the samplers attributed, including native
    #: threads. This is the number a threshold is applied to.
    share_percent: float = 0.0
    #: How much of the function's cost is its own lines, as against calls it
    #: makes. Near 100 is a Python loop; near 0 is a library call.
    self_share_percent: float = 0.0
    #: How much of it is on a thread other than the one running the test.
    background_share_percent: float = 0.0
    thread: Optional[str] = None
    #: How many tests it was seen in, and the three it cost most in.
    test_count: int = 0
    tests: list[str] = Field(default_factory=list)
    hottest_lines: list[HotLine] = Field(default_factory=list)
    #: The deepest frame the cost is under, for a LIBRARY_CALL: which library.
    below: Optional[Frame] = None
    #: The most expensive stack this was blamed for, deepest first, as
    #: faulthandler would print it. The blamed frame is its first line.
    stack: list[str] = Field(default_factory=list)

    def raw_stack(self) -> list[str]:
        return list(self.stack)

    def blame_stack(self) -> tuple[list[str], bool]:
        return list(self.stack), False

    def suspect_nodeid(self) -> Optional[str]:
        return self.tests[0] if self.tests else None

    def suspect_basis_for(self, path: str) -> str:
        return f"owner of the test it cost most in ({path})"

    def owner_when_unattributable(self) -> Optional[str]:
        # The collector and a native thread pool belong to no frame; what
        # drives them is the test's allocation pattern, which the suspect
        # line names. Nobody's frame, so nobody's owner.
        return "runtime" if self.verdict in ("GC_PRESSURE", "NATIVE_THREADS") else None

    def fingerprint_parts(self) -> list[str]:
        return [self.kind, self.verdict]

    def details(self) -> list[str]:
        lines = []
        if self.blamed_frame is not None:
            where = f"{self.share_percent:g}% of the run's CPU, {self.cpu_seconds:.1f} s"
            if self.thread and self.verdict == "BACKGROUND_THREAD":
                where += f", on thread {self.thread!r}"
            lines.append(where)
        if self.below is not None:
            # No line: what is under a call is a module and a function, and
            # the line moves with every sample.
            where = Path(self.below.file).name
            lines.append(f"below {where} in {self.below.function} ({self.below.owner})")
        return lines


class CpuBurstIncident(Incident):
    model_config = ConfigDict(extra="forbid")

    ends_run: ClassVar[bool] = False

    kind: Literal["cpu_burst"] = "cpu_burst"

    #: The test the burst was in, or None for one between tests or for the
    #: whole-run CONTENDED verdict.
    nodeid: Optional[str] = None
    phase: Optional[str] = None
    thread: Optional[str] = None
    #: How long the burst held the cores; for RECURRING_BURST the typical
    #: length of one; for CONTENDED the seconds the machine was pinned.
    burst_seconds: float = 0.0
    #: Cores' worth of CPU over that time.
    cores: float = 0.0
    #: Seconds of CPU in the burst (all of them, for a recurring one).
    cpu_seconds: float = 0.0
    #: Seconds into the test, or the gap, that it started.
    started_s: float = 0.0
    #: How busy the whole machine was meanwhile, in percent, when it could
    #: be read; for CONTENDED the share of the run it was pinned for.
    machine_busy_percent: Optional[float] = None
    cpus: Optional[int] = None
    workers: int = 0
    #: How many tests burst here, and the three that burnt most.
    test_count: int = 0
    tests: list[str] = Field(default_factory=list)
    #: The stack that was there for most of the burst, deepest first.
    stack: list[str] = Field(default_factory=list)

    def raw_stack(self) -> list[str]:
        return list(self.stack)

    def blame_stack(self) -> tuple[list[str], bool]:
        return list(self.stack), False

    def suspect_nodeid(self) -> Optional[str]:
        return self.nodeid or (self.tests[0] if self.tests else None)

    def suspect_basis_for(self, path: str) -> str:
        return f"owner of the test the burst was in ({path})"

    def owner_when_unattributable(self) -> Optional[str]:
        # The machine being full is nobody's frame and nobody's test.
        return "runtime" if self.verdict == "CONTENDED" else None

    def fingerprint_parts(self) -> list[str]:
        # A recurring burst is one row however many tests it recurs in, and
        # is told apart by the frame the engine attributes; a long burst is
        # its test's, whichever parametrisation and whichever worker.
        if self.verdict == "RECURRING_BURST":
            return [self.kind, self.verdict]
        return [self.kind, self.verdict, (self.nodeid or "").split("[")[0]]

    def details(self) -> list[str]:
        lines = []
        if self.nodeid:
            lines.append(f"test {self.nodeid}" + (f"  worker={self.worker}" if self.worker else ""))
        if self.verdict == "RECURRING_BURST":
            lines.append(
                f"{self.cpu_seconds:.1f} s of CPU in bursts across {self.test_count} tests, "
                f"typically {self.burst_seconds:g} s at {self.cores:g} cores"
                + (f" in {self.phase}" if self.phase else "")
            )
        elif self.verdict == "CONTENDED":
            lines.append(
                f"machine pinned for {self.burst_seconds:g} s, {self.cores:g} cores per worker"
                + (f", {self.workers} workers on {self.cpus} cores" if self.cpus else "")
            )
        else:
            lines.append(
                f"{self.burst_seconds:g} s at {self.cores:g} cores, {self.started_s:g} s in"
                + (f", in {self.phase}" if self.phase else "")
                + (f", on thread {self.thread!r}" if self.thread and self.verdict == "BACKGROUND_BURST" else "")
            )
        return lines


class MemoryGrowth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tests: int
    per_test_mb: float
    #: Live objects added per test, when the count was read.
    objects_per_test: Optional[int] = None


class MemoryProfileIncident(Incident):
    model_config = ConfigDict(extra="forbid")

    ends_run: ClassVar[bool] = False

    kind: Literal["memory_profile"] = "memory_profile"

    #: The test the finding is about: the one that kept or climbed, the first
    #: of a growing run, or the one where an imbalanced worker diverged.
    nodeid: Optional[str] = None
    #: Which phase the step landed in, when the boundary readings say.
    phase: Optional[str] = None
    before_mb: Optional[int] = None
    after_mb: Optional[int] = None
    peak_mb: Optional[int] = None
    #: What the verdict measures: kept, climbed, grown, or the gap to the
    #: median worker.
    delta_mb: Optional[int] = None
    growth: Optional[MemoryGrowth] = None
    #: Every worker's peak, for an imbalance.
    worker_rss: dict[str, int] = Field(default_factory=dict)
    median_mb: Optional[int] = None
    tests: list[str] = Field(default_factory=list)
    #: The stack that was running while most of the memory climbed, deepest
    #: first, as faulthandler prints it. Empty for a finding that is not about
    #: one test's climb, or when nothing was seen climbing.
    stack: list[str] = Field(default_factory=list)
    #: Megabytes of the climb charged to that stack, out of all charged.
    climb_mb: int = 0
    climb_total_mb: int = 0

    def raw_stack(self) -> list[str]:
        return list(self.stack)

    def blame_stack(self) -> tuple[list[str], bool]:
        return list(self.stack), False

    def suspect_nodeid(self) -> Optional[str]:
        return self.nodeid

    def suspect_basis_for(self, path: str) -> str:
        return f"owner of the test the memory arrived in ({path})"

    def fingerprint_parts(self) -> list[str]:
        # A parametrised test's growth is one finding however many cases it
        # has, and the same test leaking on gw3 today and gw7 tomorrow is one
        # row - the worker is left out on purpose.
        name = (self.nodeid or "").split("[")[0]
        return [self.kind, self.verdict, name]

    def details(self) -> list[str]:
        lines = []
        if self.nodeid and self.verdict != "STEADY_GROWTH":
            lines.append(f"test {self.nodeid}" + (f"  worker={self.worker}" if self.worker else ""))
        if self.verdict == "RETAINED_AFTER_TEST" and self.delta_mb is not None:
            lines.append(f"kept {self.delta_mb} MB" + (f" in {self.phase}" if self.phase else ""))
        elif self.verdict == "HEAP_NOT_RETURNED" and self.delta_mb is not None:
            lines.append(f"{self.delta_mb} MB still mapped after it, none of it in use")
        elif self.verdict == "TRANSIENT_PEAK" and self.delta_mb is not None:
            lines.append(f"peaked {self.delta_mb} MB above where it started")
        elif self.verdict == "PEAK_OVER_CEILING" and self.peak_mb is not None:
            lines.append(f"reached {self.peak_mb} MB")
        elif self.verdict == "STEADY_GROWTH" and self.growth is not None:
            lines.append(
                f"{self.delta_mb} MB over {self.growth.tests} tests on {self.worker}, "
                f"{self.growth.per_test_mb:g} MB each"
                + (f" and {self.growth.objects_per_test:,d} objects" if self.growth.objects_per_test else "")
            )
        elif self.verdict == "WORKER_IMBALANCE":
            lines.append(
                ", ".join(f"{worker} {rss} MB" for worker, rss in self.worker_rss.items())
            )
        return lines


def build(finding: Finding, worker: str) -> Incident:
    """One incident from one finding. The engine enriches it like any other."""
    if finding.kind == "cpu_hotspot":
        below = (
            Frame(
                file=finding.below.file,
                line=finding.below.line,
                function=finding.below.function,
                module=Path(finding.below.file).stem,
                owner=finding.below.owner,
            )
            if finding.below is not None
            else None
        )
        return CpuHotspotIncident(
            worker=worker,
            verdict=finding.verdict,
            confidence="high" if finding.stack else "medium",
            evidence=list(finding.evidence),
            cpu_seconds=finding.cpu_seconds,
            share_percent=finding.share_percent,
            self_share_percent=finding.self_share_percent,
            background_share_percent=finding.background_share_percent,
            thread=finding.thread,
            test_count=finding.test_count,
            tests=list(finding.tests),
            hottest_lines=[HotLine(line=line, percent=percent) for line, percent in finding.hottest_lines],
            below=below,
            stack=list(finding.stack),
        )
    if finding.kind == "cpu_burst":
        return CpuBurstIncident(
            worker=finding.worker or worker,
            verdict=finding.verdict,
            confidence="high" if finding.stack or finding.verdict == "CONTENDED" else "medium",
            evidence=list(finding.evidence),
            nodeid=finding.nodeid,
            phase=finding.phase,
            thread=finding.thread,
            burst_seconds=finding.burst_seconds,
            cores=finding.cores,
            cpu_seconds=finding.cpu_seconds,
            started_s=finding.started_s,
            machine_busy_percent=finding.machine_busy_percent,
            cpus=finding.cpus,
            workers=finding.worker_count,
            test_count=finding.test_count,
            tests=list(finding.tests),
            stack=list(finding.stack),
        )
    return MemoryProfileIncident(
        worker=finding.worker or worker,
        verdict=finding.verdict,
        confidence="high",
        evidence=list(finding.evidence),
        nodeid=finding.nodeid,
        phase=finding.phase,
        before_mb=finding.before_mb,
        after_mb=finding.after_mb,
        peak_mb=finding.peak_mb,
        delta_mb=finding.delta_mb,
        growth=(
            MemoryGrowth(
                tests=finding.growth_tests,
                per_test_mb=finding.growth_per_test_mb,
                objects_per_test=finding.growth_objects_per_test,
            )
            if finding.growth_tests
            else None
        ),
        worker_rss=dict(finding.worker_rss),
        median_mb=finding.median_mb,
        tests=list(finding.tests),
        stack=list(finding.stack),
        climb_mb=finding.climb_mb,
        climb_total_mb=finding.climb_total_mb,
    )
