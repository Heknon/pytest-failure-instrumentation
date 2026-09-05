"""Workers disagreed about which tests exist.

No hook fires for this either. xdist notices, writes a unified diff per
differing worker into its own log, and aborts - and with sixty workers and one
odd node that is fifty-nine complete diffs, every one of them naming the
majority as the deviation. Nothing reaches a database.

Two shapes, and they are different bugs. *Membership* means a conftest,
a marker or an environment variable made a test exist on one machine and not
another. *Order* means the same tests came back in a different sequence, which
matters because xdist addresses tests by position - and which a unified diff
renders as a near-total rewrite, hiding the one fact worth having: where the
two lists first disagree.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from ..analysis.collection import SAMPLE_SIZE, CollectionTracker
from .base import Incident

#: Variant rows in the alert text. Beyond this the report says how many more
#: there were rather than printing a page of near-identical blocks.
VARIANTS_SHOWN = 4

WORKERS_SHOWN = 4


class UnstableParameters(BaseModel):
    """One parametrized test, and what a few workers produced for it."""

    model_config = ConfigDict(extra="forbid")

    test: str
    #: worker id -> the parameter values that worker collected.
    workers: list[dict[str, Any]] = Field(default_factory=list)


class CollectionVariant(BaseModel):
    """One distinct collection, and how it differs from the majority."""

    model_config = ConfigDict(extra="forbid")

    digest: str
    workers: list[str] = Field(default_factory=list)
    worker_count: int = 0
    test_count: int = 0
    #: Workers in this group that joined after the run started. xdist drops a
    #: replacement whose collection differs rather than aborting, and says so
    #: only in its own log.
    replacements: list[str] = Field(default_factory=list)
    role: str = "differs"
    #: "baseline", "membership", "order", or "uncompared" when the id lists
    #: needed to diff this variant were not held.
    kind: str = "membership"
    #: False when this variant was never diffed against the baseline.
    compared: bool = True

    missing_count: int = 0
    extra_count: int = 0
    #: The differing ids themselves, capped at analysis.collection.IDS_KEPT.
    #: Compare against missing_count and extra_count to see whether the cap
    #: was reached - those are the true totals.
    missing: list[str] = Field(default_factory=list)
    extra: list[str] = Field(default_factory=list)
    modules: list[str] = Field(default_factory=list)
    module_count: int = 0
    first_divergence_index: Optional[int] = None
    first_divergence: list[str] = Field(default_factory=list)

    def describe(self) -> list[str]:
        """One variant, as a sentence about what it did differently.

        Magnitude leads and identity follows: how many workers and what they
        got wrong is what a reader is scanning for, while the digest is a
        pointer to a file and belongs at the end of the line it labels.
        """
        if self.role == "baseline":
            # The clause about what it is measured against is added by
            # details(), which is the only place that knows whether anything
            # below it was actually compared.
            return [
                f"Baseline: {_workers(self.worker_count)} collected "
                f"{self.test_count} tests"
            ]
        if self.kind == "uncompared" or not self.compared:
            return [
                f"{_workers(self.worker_count).capitalize()} collected {self.test_count} "
                f"tests, not compared {self._who()}."
            ] + self._replacement_note()
        if self.kind == "order":
            return [f"{_workers(self.worker_count).capitalize()} collected the same "
                    f"{self.test_count} tests in a different order {self._who()}."
                    ] + self._order_detail() + self._replacement_note()
        return (
            [f"{_workers(self.worker_count).capitalize()} {self._difference()}{self._inline_where()} {self._who()}."]
            + self._spread()
            + self._samples()
            + self._replacement_note()
        )

    # -- the parts of that sentence --------------------------------------

    def _who(self) -> str:
        shown = ", ".join(self.workers[:WORKERS_SHOWN])
        if self.worker_count > WORKERS_SHOWN:
            shown += f" and {self.worker_count - WORKERS_SHOWN} more"
        return f"({shown})"

    def _difference(self) -> str:
        plural = self.worker_count != 1
        if self.missing_count and self.extra_count:
            return f"{'differ' if plural else 'differs'}: {self.missing_count} missing, {self.extra_count} extra"
        if self.missing_count:
            return (
                f"{'are' if plural else 'is'} missing "
                f"{_tests(self.missing_count)}"
            )
        noun = "test" if self.extra_count == 1 else "tests"
        return f"{'have' if plural else 'has'} {self.extra_count} extra {noun}"

    def _inline_where(self) -> str:
        """One module fits in the sentence. A list of five does not - it pushes
        the worker ids past where anyone is still reading."""
        if self.module_count == 1 and self.modules:
            return f", in {self.modules[0]}"
        return ""

    def _spread(self) -> list[str]:
        if self.module_count <= 1 or not self.modules:
            return []
        listed = ", ".join(self.modules)
        if self.module_count > len(self.modules):
            listed += f" and {self.module_count - len(self.modules)} more"
        return [f"    across {self.module_count} modules: {listed}"]

    def _samples(self) -> list[str]:
        """Diff notation, because everyone already reads it: - is what the
        baseline had, + is what this worker had instead."""
        lines = [f"    - {identifier}" for identifier in self.missing[:SAMPLE_SIZE]]
        lines += [f"    + {identifier}" for identifier in self.extra[:SAMPLE_SIZE]]
        withheld = (self.missing_count - min(len(self.missing), SAMPLE_SIZE)) + (
            self.extra_count - min(len(self.extra), SAMPLE_SIZE)
        )
        if withheld > 0:
            # Never silently truncate: a sample that looks like the whole
            # story is worse than no sample.
            carried = len(self.missing) + len(self.extra)
            total = self.missing_count + self.extra_count
            lines.append(
                f"    and {withheld} more"
                + ("" if carried >= total else f" ({carried} of {total} in the payload)")
            )
        return lines

    def _order_detail(self) -> list[str]:
        if self.first_divergence_index is None or not self.first_divergence:
            return []
        return [
            f"    first difference at index {self.first_divergence_index}",
            f"    - {self.first_divergence[0]}",
            f"    + {self.first_divergence[1]}",
        ]

    def _replacement_note(self) -> list[str]:
        if not self.replacements:
            return []
        return [
            f"    {', '.join(self.replacements)} joined after the run started; "
            "xdist drops a replacement whose collection differs, and says so only in its own log"
        ]


def _workers(count: int) -> str:
    return "1 worker" if count == 1 else f"{count} workers"


def _tests(count: int) -> str:
    return "1 test" if count == 1 else f"{count} tests"


class CollectionMismatchIncident(Incident):
    model_config = ConfigDict(extra="forbid")

    #: Overridden per instance: see ends_this_run.
    ends_run: ClassVar[bool] = True

    kind: Literal["collection_mismatch"] = "collection_mismatch"

    variant_count: int = 0
    worker_count: int = 0
    #: False when a worker died still owing a collection, so the counts below
    #: describe the workers that answered rather than the whole run. Which
    #: variant is the majority is only meaningful once everyone has voted.
    complete: bool = True
    baseline_digest: str = ""
    variants: list[CollectionVariant] = Field(default_factory=list)
    #: digest -> the file holding that variant's full id list. Sixty workers
    #: times fifty thousand ids does not belong in an alert.
    variant_files: dict[str, str] = Field(default_factory=dict)
    #: The workers collected the same tests, and only the parameter values
    #: differ. A different problem with a different fix, so it is not reported
    #: as tests appearing and disappearing.
    parameters_unstable: bool = False
    #: The parametrized tests responsible, named without their parameters.
    unstable_tests: list[str] = Field(default_factory=list)
    #: What a few workers actually produced for each of them. This is the part
    #: that makes the cause visible rather than merely located.
    parameter_samples: list[UnstableParameters] = Field(default_factory=list)

    def ends_this_run(self) -> bool:
        # xdist aborts when the initial collections disagree, but a worker that
        # registers *after* scheduling began is dropped silently and the run
        # carries on a worker short.
        differing = [variant for variant in self.variants if variant.role != "baseline"]
        if not differing:
            # No variants at all means this is a degraded incident - gathering
            # the detail failed. The mismatch itself was real, so fall back to
            # what the kind normally does rather than reporting a run that
            # carried on when it did not.
            return type(self).ends_run
        return not all(
            variant.replacements and len(variant.replacements) == variant.worker_count
            for variant in differing
        )

    def suspect_nodeid(self) -> Optional[str]:
        # No stack to attribute, but the ids that differ have owners: whoever
        # owns the module that appeared on one machine and not another.
        for variant in self.variants:
            for identifier in variant.missing + variant.extra:
                return identifier
        return None

    def _unstable_detail(self) -> list[str]:
        lines = [
            "The same tests exist on every worker; only the parameter values in their "
            "ids differ."
        ]
        if not self.parameter_samples:
            lines.extend(f"    {identifier}" for identifier in self.unstable_tests)
        for sample in self.parameter_samples:
            lines.append(f"    {sample.test}")
            for row in sample.workers:
                values = ", ".join(row.get("values", []))
                lines.append(f"        {row.get('worker')} collected {values}")
        lines.append(
            "Ids that differ per worker were computed at collection time from "
            "something that differs per process, and xdist requires the ids to match."
        )
        lines.append("Look at: the parametrize arguments of those tests.")
        return lines

    def suspect_basis_for(self, path: str) -> str:
        return f"a module the workers disagreed about, {path}"

    def summary(self) -> str:
        if self.verdict == "INSTRUMENTATION_FAILED":
            return super().summary()
        if self.parameters_unstable:
            what = "Workers collected the same tests with different parameter values"
        elif self.verdict == "COLLECTION_ORDER_DIFFERS":
            what = "Workers collected the same tests in different orders"
        else:
            what = "Workers collected different tests"
        answered = "" if self.complete else ", of the workers that answered"
        return (
            f"{what}: {_workers(self.worker_count)} produced {self.variant_count} "
            f"different collections{answered}"
        )

    def fingerprint_parts(self) -> list[str]:
        if self.parameters_unstable:
            # The ids themselves change every run; the test names do not.
            return [self.kind, self.verdict, ",".join(self.unstable_tests)]
        modules: list[str] = []
        for variant in self.variants:
            modules.extend(variant.modules)
        return [self.kind, self.verdict, ",".join(sorted(set(modules)))]

    def details(self) -> list[str]:
        """The variant rows: one per distinct collection, measured against the
        largest. Rendered from the structured fields, because the same rows
        are what a consumer reads out of ``variants``."""
        if self.verdict == "INSTRUMENTATION_FAILED":
            return []
        if self.parameters_unstable:
            # The variant rows would be one near-identical block per worker,
            # every one of them the same finding said differently.
            return self._unstable_detail()

        lines: list[str] = []
        for variant in self.variants[:VARIANTS_SHOWN]:
            lines.extend(variant.describe())
        if lines and any(
            variant.compared for variant in self.variants if variant.role != "baseline"
        ):
            lines[0] += "; the rows below are measured against that list."
        elif lines:
            lines[0] += "."
        withheld = len(self.variants) - VARIANTS_SHOWN
        if withheld > 0:
            lines.append(f"And {withheld} more collections, not shown.")
        if any(not variant.compared for variant in self.variants):
            lines.append(
                "Collections marked \"not compared\" were not diffed: id lists are "
                "kept for the first few variants only."
            )
        return lines


def build(
    tracker: CollectionTracker, directory: Path, complete: bool = True
) -> CollectionMismatchIncident:
    summary = tracker.summarise()
    variants = [CollectionVariant(**variant) for variant in summary["variants"]]
    unstable = tracker.parameters_unstable
    order_only = all(
        variant.kind == "order" for variant in variants if variant.role != "baseline"
    )
    if unstable:
        verdict = "COLLECTION_PARAMETERS_UNSTABLE"
    elif order_only:
        verdict = "COLLECTION_ORDER_DIFFERS"
    else:
        verdict = "COLLECTION_MEMBERSHIP_DIFFERS"

    incident = CollectionMismatchIncident(
        worker="controller",
        verdict=verdict,
        confidence="high",
        variant_count=summary["variant_count"],
        worker_count=summary["worker_count"],
        complete=complete,
        baseline_digest=summary["baseline_digest"],
        variants=variants,
        variant_files=write_variant_files(tracker, directory),
        parameters_unstable=unstable,
        unstable_tests=tracker.unstable_tests() if unstable else [],
        parameter_samples=(
            [UnstableParameters(**sample) for sample in tracker.parameter_samples()]
            if unstable
            else []
        ),
        evidence=[
            "xdist addresses tests by position rather than by id, so any difference "
            "between the lists stops it: a reordering as much as a missing test.",
        ],
    )
    if unstable:
        incident.evidence.append(
            "Stripping the parameters from the ids makes every worker's collection "
            "identical."
        )
    if not complete:
        incident.evidence.append(
            "A worker ended without registering a collection, so only the workers "
            "that answered are compared; which variant is the majority is settled "
            "only once every worker has reported."
        )
    # Which of xdist's two behaviours happened is the difference between a run
    # that stopped and a run that quietly lost a worker.
    incident.evidence.append(
        "The initial collections disagreed, so xdist aborted the run."
        if incident.ends_this_run()
        else "A replacement worker's collection differed, so xdist dropped that "
        "worker and the run continued one short."
    )
    if incident.variant_files and not unstable:
        # The whole collections, for whoever still has the machine. The
        # difference - the part anyone actually needs - travels in the
        # incident, because these files are on a runner that may already
        # be gone by the time the alert is read.
        directory = Path(next(iter(incident.variant_files.values()))).parent
        incident.evidence.append(f"Look at: the full id lists in {directory}.")
    return incident


def write_variant_files(tracker: CollectionTracker, directory: Path) -> dict[str, str]:
    """Full id lists belong on disk, not in an alert."""
    written: dict[str, str] = {}
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return written
    for digest in tracker.identifiers_by_digest:
        path = directory / f"collection-{digest}.txt"
        try:
            path.write_text(
                "\n".join(tracker.identifiers_by_digest.get(digest, [])),
                encoding="utf-8",
            )
            written[digest] = str(path)
        except OSError:
            continue
    return written
