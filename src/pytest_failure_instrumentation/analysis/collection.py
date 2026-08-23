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
from collections import Counter
from collections.abc import Iterable
from typing import Any, Optional

#: How many distinct collections keep their full id list. The design assumes a
#: handful of variants; a suite whose ids are not stable produces one per
#: worker, which is the hundreds-of-megabytes case the digest exists to avoid.
VARIANTS_KEPT = 5

#: How many differing ids the payload carries per side. The *collection* is
#: unbounded and stays on disk; the *difference* is what a reader needs and is
#: almost always small - one test, or one module's worth. A conftest that fails
#: to import a whole tree is the case this cap exists for, and it records what
#: it dropped rather than trimming quietly.
IDS_KEPT = 500

#: How many of those the alert text shows. A payload is read by a database; a
#: line of text is read by a person.
SAMPLE_SIZE = 3
MODULES_SHOWN = 5


#: A parametrized node id ends in its parameters. Stripping them asks a
#: different question: are these the same *tests*, or genuinely different ones?
PARAMETERS = re.compile(r"\[.*\]$")


def without_parameters(identifier: str) -> str:
    return PARAMETERS.sub("", identifier)


def parameter_of(identifier: str, limit: int = 48) -> str:
    """The bracketed part, which is pytest's rendering of the value itself."""
    stripped = without_parameters(identifier)
    value = identifier[len(stripped) :].strip("[]")
    return value if len(value) <= limit else value[: limit - 1] + "\u2026"


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
        #: The same collections with parameters stripped. Cheap to keep, and
        #: it is the only way to tell "different tests" from "the same tests
        #: with different parameter values".
        self.stable_digest_by_worker: dict[str, str] = {}
        self.identifiers_by_digest: dict[str, list[str]] = {}
        #: Kept for every digest, including the ones whose list was dropped.
        self.count_by_digest: dict[str, int] = {}
        #: Workers that registered after scheduling began. xdist drops a
        #: replacement whose collection differs instead of aborting the run,
        #: and says so only in its own log.
        self.replacements: set[str] = set()

    def record(
        self, worker: str, identifiers: list[str], replacement: bool = False
    ) -> None:
        digest = digest_of(identifiers)
        self.digest_by_worker[worker] = digest
        self.stable_digest_by_worker[worker] = digest_of(
            without_parameters(identifier) for identifier in identifiers
        )
        self.count_by_digest[digest] = len(identifiers)
        # One full list per distinct collection, not one per worker - and only
        # for the first few, since "distinct collection" stops being a small
        # number the moment ids are unstable.
        if (
            digest not in self.identifiers_by_digest
            and len(self.identifiers_by_digest) < VARIANTS_KEPT
        ):
            self.identifiers_by_digest[digest] = identifiers
        if replacement:
            self.replacements.add(worker)

    @property
    def has_mismatch(self) -> bool:
        return len(set(self.digest_by_worker.values())) > 1

    @property
    def parameters_unstable(self) -> bool:
        """The workers collected the same tests with different parameters.

        A parametrize that draws its values at collection time - a random
        number, a timestamp, a set iteration order - gives every worker a
        different id for the same test. Reported as a membership difference it
        reads as thousands of missing tests, which is both wrong and the wrong
        thing to go and fix.
        """
        return (
            self.has_mismatch
            and len(set(self.stable_digest_by_worker.values())) == 1
        )

    def parameter_samples(
        self, workers_shown: int = 3, values_shown: int = 4
    ) -> list[dict[str, Any]]:
        """What each of a few workers actually produced, side by side.

        Naming the test says where to look; this says what you are looking at.
        Floating-point noise reads as a random number, and a row of ids from a
        live system reads as a fetch running at collection time - neither of
        which is apparent from one worker's list alone.
        """
        representative: dict[str, str] = {}
        for worker in sorted(self.digest_by_worker, key=worker_key):
            representative.setdefault(self.digest_by_worker[worker], worker)

        samples = []
        for test in self.unstable_tests():
            rows = []
            for digest, identifiers in self.identifiers_by_digest.items():
                shown_by = representative.get(digest)
                values = [
                    parameter_of(identifier)
                    for identifier in identifiers
                    if without_parameters(identifier) == test
                    and identifier != test
                ]
                if shown_by and values:
                    rows.append({"worker": shown_by, "values": values[:values_shown]})
                if len(rows) >= workers_shown:
                    break
            if rows:
                samples.append({"test": test, "workers": rows})
        return samples

    def unstable_tests(self, limit: int = MODULES_SHOWN) -> list[str]:
        """The test functions whose parametrized ids differ, without the ids."""
        kept = list(self.identifiers_by_digest.values())
        if len(kept) < 2:
            return []
        common = set(kept[0]).intersection(*(set(other) for other in kept[1:]))
        everything = set(kept[0]).union(*(set(other) for other in kept[1:]))
        differing = {without_parameters(one) for one in everything - common}
        return sorted(differing)[:limit]

    def variants(self) -> list[dict[str, Any]]:
        """Distinct collections, largest group first - the majority leads."""
        grouped: dict[str, list[str]] = {}
        for worker, digest in self.digest_by_worker.items():
            grouped.setdefault(digest, []).append(worker)
        variants: list[dict[str, Any]] = [
            {
                "digest": digest,
                "workers": sorted(workers, key=worker_key),
                "worker_count": len(workers),
                "test_count": self.count_by_digest.get(digest, 0),
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

    def summarise(self, ids_kept: int = IDS_KEPT) -> dict[str, Any]:
        """Every minority variant, described against the majority."""
        variants = self.variants()
        baseline = variants[0]
        baseline_identifiers = self.identifiers_by_digest.get(baseline["digest"], [])

        # A variant whose list was dropped cannot be diffed, and neither can
        # anything else if the baseline's was. Saying so is the only honest
        # option: comparing two empty lists reports "the same tests in a
        # different order", which is a finding invented out of missing data.
        have_baseline = baseline["digest"] in self.identifiers_by_digest

        described = [dict(baseline, role="baseline", kind="baseline", compared=True)]
        for variant in variants[1:]:
            if not have_baseline or variant["digest"] not in self.identifiers_by_digest:
                described.append(
                    dict(variant, role="differs", kind="uncompared", compared=False)
                )
                continue
            described.append(
                dict(
                    variant,
                    role="differs",
                    compared=True,
                    **difference(
                        baseline_identifiers,
                        self.identifiers_by_digest[variant["digest"]],
                        ids_kept,
                    ),
                )
            )
        return {
            "variant_count": len(variants),
            "worker_count": len(self.digest_by_worker),
            "baseline_digest": baseline["digest"],
            "variants": described,
        }


def difference(
    baseline: list[str], variant: list[str], ids_kept: int = IDS_KEPT
) -> dict[str, Any]:
    """What one collection has that the other does not.

    Counted, not set-compared. A collection can hold the same node id twice -
    a plugin appending to ``items``, a parametrize generating a colliding id -
    and set arithmetic cannot see that at all: the two lists differ in length
    while the difference comes out empty, which was then reported as "the same
    tests in a different order" with a test count that contradicted the line
    above it. A multiset says what actually differs, once per occurrence.
    """
    baseline_counts, variant_counts = Counter(baseline), Counter(variant)
    missing = sorted((baseline_counts - variant_counts).elements())
    extra = sorted((variant_counts - baseline_counts).elements())

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
            "missing": [],
            "extra": [],
        }

    changed = missing + extra
    modules = sorted({identifier.split("::")[0] for identifier in changed})
    return {
        "kind": "membership",
        "missing_count": len(missing),
        "extra_count": len(extra),
        "first_divergence_index": None,
        "first_divergence": [],
        # Sorted, so the same mismatch reports the same ids in the same order
        # on every run and the fingerprint does not drift with worker timing.
        "missing": missing[:ids_kept],
        "extra": extra[:ids_kept],
        "modules": modules[:MODULES_SHOWN],
        "module_count": len(modules),
    }


def first_divergence(baseline: list[str], variant: list[str]) -> Optional[int]:
    for index, (left, right) in enumerate(zip(baseline, variant)):
        if left != right:
            return index
    return None
