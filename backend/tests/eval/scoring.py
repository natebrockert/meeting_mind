"""Scoring helpers for eval-fixture assertions.

These are intentionally simple: precision/recall on driver kinds, set
overlap on segment_ids, exact bucket match on categorical fields. Fancy
fuzzy-matching (e.g. text similarity on descriptions) is out of scope —
we score *what the pipeline surfaced*, not *how it phrased it*.

When an assertion fails, the helper raises an AssertionError with a
diagnostic message that names the fixture so the report tells you which
case broke.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass


@dataclass(frozen=True)
class DriverScore:
    """How well a pipeline run matched a fixture's expected drivers.

    precision_kind: of the drivers the pipeline emitted, the fraction
      whose `kind` was expected at all (the pipeline isn't surfacing
      kinds the fixture doesn't expect).
    recall_kind: of the kinds the fixture expected, the fraction the
      pipeline emitted at least once.
    segment_matches: of the expected drivers that pinned a specific
      segment_index, the count whose (kind, segment_id) tuple was found
      in the pipeline's output.
    """

    precision_kind: float
    recall_kind: float
    segment_matches: int
    segment_total: int


def score_drivers(
    actual_kinds: list[str],
    actual_segment_ids: list[int],
    expected_kinds: list[str],
    expected_segment_ids: list[int | None],
    resolved_expected_seg_ids: list[int],
) -> DriverScore:
    """Score pipeline output against fixture expectations.

    `actual_kinds[i]` corresponds to `actual_segment_ids[i]`. Same for
    expected. `resolved_expected_seg_ids` is the subset of expected
    segment_ids where the fixture pinned a specific segment (not None).
    """
    actual_counter = Counter(actual_kinds)
    expected_counter = Counter(expected_kinds)

    if not actual_kinds:
        precision = 1.0  # vacuously precise — nothing wrong with nothing
    else:
        matched = sum(
            min(actual_counter[k], expected_counter.get(k, 0))
            for k in actual_counter
        )
        precision = matched / len(actual_kinds)

    if not expected_kinds:
        recall = 1.0
    else:
        kinds_seen = set(actual_kinds)
        recall = sum(1 for k in expected_kinds if k in kinds_seen) / len(expected_kinds)

    # Segment-level: pair (kind, segment_id) tuples. We only count
    # expected drivers that named a specific segment_index.
    actual_pairs = set(zip(actual_kinds, actual_segment_ids, strict=False))
    expected_pairs = []
    for kind, seg_id in zip(expected_kinds, expected_segment_ids, strict=False):
        if seg_id is None:
            continue
        expected_pairs.append((kind, seg_id))
    seg_matches = sum(1 for p in expected_pairs if p in actual_pairs)
    return DriverScore(
        precision_kind=precision,
        recall_kind=recall,
        segment_matches=seg_matches,
        segment_total=len(expected_pairs),
    )
