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

WORKERS_SHOWN = 4


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
    #: "baseline", "membership" or "order".
    kind: str = "membership"

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
            return [
                f"baseline: {_workers(self.worker_count)} collected "
                f"{self.test_count} tests, and everything below is measured "
                "against that list"
            ]
        if self.kind == "order":
            return [f"{_workers(self.worker_count)} collected the same "
                    f"{self.test_count} tests in a different order {self._who()}"
                    ] + self._order_detail() + self._replacement_note()
        return (
            [f"{_workers(self.worker_count)} {self._difference()}{self._inline_where()} {self._who()}"]
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
            f"    {', '.join(self.replacements)} joined after the run started - "
            "xdist drops a replacement whose collection differs, without saying so"
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
    baseline_digest: str = ""
    variants: list[CollectionVariant] = Field(default_factory=list)
    #: digest -> the file holding that variant's full id list. Sixty workers
    #: times fifty thousand ids does not belong in an alert.
    variant_files: dict[str, str] = Field(default_factory=dict)

    def ends_this_run(self) -> bool:
        # xdist aborts when the initial collections disagree, but a worker that
        # registers *after* scheduling began is dropped silently and the run
        # carries on a worker short.
        differing = [variant for variant in self.variants if variant.role != "baseline"]
        if not differing:
            return False
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

    def suspect_basis_for(self, path: str) -> str:
        return f"owner of a module the workers disagreed about ({path})"

    def fingerprint_parts(self) -> list[str]:
        modules: list[str] = []
        for variant in self.variants:
            modules.extend(variant.modules)
        return [self.kind, self.verdict, ",".join(sorted(set(modules)))]

    def details(self) -> list[str]:
        lines = [
            f"{_workers(self.worker_count)} produced "
            f"{self.variant_count} different collections"
        ]
        for variant in self.variants:
            lines.extend(variant.describe())
        if self.variant_files:
            # The whole collections, for whoever still has the machine. The
            # difference - the part anyone actually needs - travels in the
            # incident, because these files are on a runner that may already
            # be gone by the time the alert is read.
            directory = Path(next(iter(self.variant_files.values()))).parent
            lines.append(
                f"whole collections written to {directory}; the difference "
                "above travels in the incident"
            )
        return lines


def build(tracker: CollectionTracker, directory: Path) -> CollectionMismatchIncident:
    summary = tracker.summarise()
    variants = [CollectionVariant(**variant) for variant in summary["variants"]]
    order_only = all(
        variant.kind == "order" for variant in variants if variant.role != "baseline"
    )
    incident = CollectionMismatchIncident(
        worker="controller",
        verdict="COLLECTION_ORDER_DIFFERS" if order_only else "COLLECTION_MEMBERSHIP_DIFFERS",
        confidence="high",
        variant_count=summary["variant_count"],
        worker_count=summary["worker_count"],
        baseline_digest=summary["baseline_digest"],
        variants=variants,
        variant_files=write_variant_files(tracker, directory),
        evidence=[
            "xdist addresses tests by position rather than by id, so any "
            "difference between the lists is fatal - a reordering as much as a "
            "missing test",
        ],
    )
    # Which of xdist's two behaviours happened is the difference between a run
    # that stopped and a run that quietly lost a worker.
    incident.evidence.append(
        "the initial collections disagreed, so the run was aborted"
        if incident.ends_this_run()
        else "a replacement worker's collection differed, so xdist dropped that "
        "worker and the run continued one short"
    )
    return incident


def write_variant_files(tracker: CollectionTracker, directory: Path) -> dict[str, str]:
    """Full id lists belong on disk, not in an alert."""
    written: dict[str, str] = {}
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return written
    for variant in tracker.variants():
        digest = variant["digest"]
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
