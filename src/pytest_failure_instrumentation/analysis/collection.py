"""Compare what every worker collected, without keeping every collection.

xdist compares each worker against whichever one registered first and emits a
full unified diff per differing worker. With sixty workers and one odd node
that is fifty-nine reports, each a complete diff, all of them naming the
majority as the deviation.

Sixty workers never produce sixty collections - they produce two or three
*variants* - so what is worth reporting is one row per variant, measured
against the largest. Memory matters as much as readability: sixty workers times
fifty thousand node ids is hundreds of megabytes on the controller, so only the
digest is kept per worker and the full id list once per distinct variant.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable, Optional

SAMPLE_SIZE = 3
MODULES_SHOWN = 5


def worker_key(worker: str) -> list:
    """Sort gw3 before gw11.

    Worker ids are numbered, and lexical order puts gw11 first - which reads
    as a mistake at any scale worth reporting on.
    """
    return [
        int(part) if part.isdigit() else part for part in re.split(r"(\d+)", worker)
    ]


def digest_of(identifiers: Iterable[str]) -> str:
    hasher = hashlib.sha1()
    for identifier in identifiers:
        hasher.update(identifier.encode("utf-8"))
        hasher.update(b"\0")
    return hasher.hexdigest()[:12]


class CollectionTracker:
    """Accumulates collections and can say, at any point, whether they agree."""

    def __init__(self) -> None:
        self.digest_by_worker: dict[str, str] = {}
        self.identifiers_by_digest: dict[str, list[str]] = {}
        #: Workers that registered after scheduling began. xdist drops a
        #: replacement whose collection differs instead of aborting the run,
        #: and says so only in its own log.
        self.replacements: set[str] = set()

    def record(
        self, worker: str, identifiers: list[str], replacement: bool = False
    ) -> None:
        digest = digest_of(identifiers)
        self.digest_by_worker[worker] = digest
        # One full list per distinct collection, not one per worker.
        self.identifiers_by_digest.setdefault(digest, identifiers)
        if replacement:
            self.replacements.add(worker)

    @property
    def has_mismatch(self) -> bool:
        return len(set(self.digest_by_worker.values())) > 1

    def variants(self) -> list[dict[str, Any]]:
        """Distinct collections, largest group first - the majority leads."""
        grouped: dict[str, list[str]] = {}
        for worker, digest in self.digest_by_worker.items():
            grouped.setdefault(digest, []).append(worker)
        variants = [
            {
                "digest": digest,
                "workers": sorted(workers, key=worker_key),
                "worker_count": len(workers),
                "test_count": len(self.identifiers_by_digest.get(digest, [])),
                "replacements": sorted(set(workers) & self.replacements, key=worker_key),
            }
            for digest, workers in grouped.items()
        ]
        # Most workers first - the majority leads. On a tie the larger
        # collection wins, so the same difference is reported as something
        # missing rather than as something extra; the digest only ever breaks
        # a tie between two equals, so the choice stays stable across runs.
        variants.sort(
            key=lambda variant: (
                -variant["worker_count"],
                -variant["test_count"],
                variant["digest"],
            )
        )
        return variants

    def summarise(self, sample_size: int = SAMPLE_SIZE) -> dict[str, Any]:
        """Every minority variant, described against the majority."""
        variants = self.variants()
        baseline = variants[0]
        baseline_identifiers = self.identifiers_by_digest.get(baseline["digest"], [])

        described = [dict(baseline, role="baseline", kind="baseline")]
        for variant in variants[1:]:
            identifiers = self.identifiers_by_digest.get(variant["digest"], [])
            described.append(
                dict(
                    variant,
                    role="differs",
                    **difference(baseline_identifiers, identifiers, sample_size),
                )
            )
        return {
            "variant_count": len(variants),
            "worker_count": len(self.digest_by_worker),
            "baseline_digest": baseline["digest"],
            "variants": described,
        }


def difference(
    baseline: list[str], variant: list[str], sample_size: int = SAMPLE_SIZE
) -> dict[str, Any]:
    baseline_set, variant_set = set(baseline), set(variant)
    missing = sorted(baseline_set - variant_set)
    extra = sorted(variant_set - baseline_set)

    if not missing and not extra:
        # Same tests, different sequence. No sample of ids explains that - the
        # useful fact is where the two lists first disagree. xdist indexes
        # tests by position, so this is just as fatal as a real difference,
        # and a unified diff renders it as a near-total rewrite.
        index = first_divergence(baseline, variant)
        return {
            "kind": "order",
            "missing_count": 0,
            "extra_count": 0,
            "first_divergence_index": index,
            "first_divergence": (
                [baseline[index], variant[index]] if index is not None else []
            ),
            "modules": [],
            "module_count": 0,
            "missing_sample": [],
            "extra_sample": [],
        }

    changed = missing + extra
    modules = sorted({identifier.split("::")[0] for identifier in changed})
    return {
        "kind": "membership",
        "missing_count": len(missing),
        "extra_count": len(extra),
        "first_divergence_index": None,
        "first_divergence": [],
        # Sorted, so the same mismatch samples the same ids on every run and
        # the fingerprint does not drift with worker timing.
        "missing_sample": missing[:sample_size],
        "extra_sample": extra[:sample_size],
        "modules": modules[:MODULES_SHOWN],
        "module_count": len(modules),
    }


def first_divergence(baseline: list[str], variant: list[str]) -> Optional[int]:
    for index, (left, right) in enumerate(zip(baseline, variant)):
        if left != right:
            return index
    return None
