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

from ..analysis.collection import CollectionTracker
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
    missing_sample: list[str] = Field(default_factory=list)
    extra_sample: list[str] = Field(default_factory=list)
    modules: list[str] = Field(default_factory=list)
    module_count: int = 0
    first_divergence_index: Optional[int] = None
    first_divergence: list[str] = Field(default_factory=list)

    def describe(self) -> list[str]:
        shown = ", ".join(self.workers[:WORKERS_SHOWN])
        if self.worker_count > WORKERS_SHOWN:
            shown += f" and {self.worker_count - WORKERS_SHOWN} more"
        head = (
            f"{self.digest}: {self.worker_count} worker(s), "
            f"{self.test_count} tests ({shown})"
        )
        if self.role == "baseline":
            return [head + " - majority, used as the baseline"]

        if self.kind == "order":
            lines = [
                head
                + " - same tests, different order"
                + (
                    f", first differing at index {self.first_divergence_index}"
                    if self.first_divergence_index is not None
                    else ""
                )
            ]
            if self.first_divergence:
                lines.append(
                    f"  baseline has {self.first_divergence[0]}, "
                    f"this has {self.first_divergence[1]}"
                )
            return lines + self._replacement_note()

        lines = [head + f" - {self.missing_count} missing, {self.extra_count} extra"]
        if self.modules:
            spread = f"{self.module_count} modules" if self.module_count > 1 else "one module"
            lines.append(f"  across {spread}: {', '.join(self.modules)}")
        lines += [f"  missing: {identifier}" for identifier in self.missing_sample]
        lines += [f"  extra:   {identifier}" for identifier in self.extra_sample]
        return lines + self._replacement_note()

    def _replacement_note(self) -> list[str]:
        if not self.replacements:
            return []
        return [
            f"  {', '.join(self.replacements)} joined after the run started - "
            "xdist drops a replacement whose collection differs, without saying so"
        ]


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
            for identifier in variant.missing_sample + variant.extra_sample:
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
            f"{self.variant_count} distinct collections across {self.worker_count} workers"
        ]
        for variant in self.variants:
            lines.extend(variant.describe())
        return lines


def build(tracker: CollectionTracker, directory: Path) -> CollectionMismatchIncident:
    summary = tracker.summarise()
    variants = [CollectionVariant(**variant) for variant in summary["variants"]]
    order_only = all(
        variant.kind == "order" for variant in variants if variant.role != "baseline"
    )
    return CollectionMismatchIncident(
        worker="controller",
        verdict="COLLECTION_ORDER_DIFFERS" if order_only else "COLLECTION_MEMBERSHIP_DIFFERS",
        confidence="high",
        variant_count=summary["variant_count"],
        worker_count=summary["worker_count"],
        baseline_digest=summary["baseline_digest"],
        variants=variants,
        variant_files=write_variant_files(tracker, directory),
        evidence=[
            f"{summary['variant_count']} distinct collections across "
            f"{summary['worker_count']} workers",
            "xdist compares every worker against whichever registered first and "
            "emits a full diff per differing worker; this is one row per variant, "
            "measured against the majority",
        ],
    )


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
