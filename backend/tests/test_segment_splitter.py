"""v0.2.10 Pass D: segment-split proposals.

Diarizer-boundary lag puts the start of the next speaker's words at the
TAIL of the previous segment. The detector scans low-confidence
segments for a discourse-opener pattern in the second half, locates the
word-level split point, and emits a review_item proposal.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from app.config import (
    AppConfig,
    AsrConfig,
    DiarizationConfig,
    PathConfig,
    RepairConfig,
    ReviewConfig,
)
from app.db.database import initialize_database
from app.services.repair.segment_splitter import (
    accept_split_proposal,
    persist_segment_split_proposals,
    propose_segment_splits,
    reject_split_proposal,
)


def _cfg(
    tmp_path: Path,
    min_conf: float = 0.55,
    auto_apply: bool = False,
    auto_apply_silent: float = 0.90,
    auto_apply_toast: float = 0.70,
) -> AppConfig:
    paths = PathConfig(
        repo_root=tmp_path,
        data_dir=tmp_path / "data",
        inbox_dir=tmp_path / "data" / "inbox",
        processed_dir=tmp_path / "data" / "processed",
        archive_dir=tmp_path / "data" / "archive",
        delete_review_dir=tmp_path / "data" / "delete-review",
        runtime_dir=tmp_path / "runtime",
        database_path=tmp_path / "runtime" / "meetingmind.sqlite3",
        vault_dir=tmp_path / "vault" / "meeting_mind",
    )
    paths.runtime_dir.mkdir(parents=True, exist_ok=True)
    cfg = AppConfig(
        config_path=tmp_path / "config" / "local.toml",
        paths=paths,
        asr=AsrConfig(),
        diarization=DiarizationConfig(),
        review=ReviewConfig(),
        repair=RepairConfig(
            segment_split_min_confidence=min_conf,
            auto_apply_enabled=auto_apply,
            auto_apply_silent_threshold=auto_apply_silent,
            auto_apply_toast_threshold=auto_apply_toast,
        ),
    )
    initialize_database(paths.database_path)
    with sqlite3.connect(paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO meetings (id, title, slug, source_path, imported_path,
                                  duration_seconds, status)
            VALUES (1, 'Demo', 'demo', '/dev/null', '/dev/null', 60, 'complete')
            """
        )
    return cfg


def _seed_boundary_leak_pair(cfg: AppConfig) -> None:
    """Mirror the real v0.2.10 panel-test scenario: seg 15 is Jan
    (Speaker 2), ends with low confidence and a tail that opens with
    'Okay, so I am of'. Seg 16 is Paul (Speaker 4) — should be the
    speaker the tail moves to. Words are seeded so the splitter can
    pick a real timestamp.
    """
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.executemany(
            """
            INSERT INTO transcript_segments
              (id, meeting_id, start_ms, end_ms, text, diarization_speaker_id,
               confidence, speaker_confidence)
            VALUES (?, 1, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    15,
                    16_05_000,
                    16_32_000,
                    "are these companies wise or silly to be rushing to "
                    "spend billions on AI and might some of them regret "
                    "it ? Okay, so I am of",
                    "Speaker 2",
                    0.45,
                    0.25,
                ),
                (
                    16,
                    16_32_000,
                    17_07_000,
                    "the opinion that we are witnessing that they are "
                    "way over their skis on this.",
                    "Speaker 4",
                    0.86,
                    0.45,
                ),
            ],
        )
        # Word-level timestamps for seg 15. The split point should land
        # on "Okay" — the first word after the head text (last word of
        # head is "it").
        words = [
            ("are", 16_05_000),
            ("these", 16_05_400),
            ("companies", 16_05_900),
            ("wise", 16_06_500),
            ("or", 16_06_900),
            ("silly", 16_07_200),
            ("to", 16_07_700),
            ("be", 16_08_000),
            ("rushing", 16_08_300),
            ("to", 16_08_800),
            ("spend", 16_09_000),
            ("billions", 16_09_500),
            ("on", 16_10_000),
            ("AI", 16_10_300),
            ("and", 16_10_700),
            ("might", 16_11_000),
            ("some", 16_11_300),
            ("of", 16_11_600),
            ("them", 16_11_900),
            ("regret", 16_12_200),
            ("it", 16_12_800),
            ("?", 16_13_000),
            ("Okay", 16_28_000),  # ← the split point lands here
            (",", 16_28_300),
            ("so", 16_28_500),
            ("I", 16_28_700),
            ("am", 16_28_900),
            ("of", 16_29_100),
        ]
        conn.executemany(
            """
            INSERT INTO transcript_words
              (meeting_id, segment_id, start_ms, end_ms, text)
            VALUES (1, 15, ?, ?, ?)
            """,
            [(start, start + 200, text) for text, start in words],
        )


def test_propose_split_finds_boundary_leak(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed_boundary_leak_pair(cfg)

    proposals = propose_segment_splits(cfg, 1)
    assert len(proposals) == 1, proposals
    p = proposals[0]
    assert p.segment_id == 15
    assert "Okay" in p.tail_text
    # tail_text should NOT be in head_text
    assert "Okay" not in p.head_text
    # The proposed speaker is Paul (Speaker 4).
    assert p.tail_speaker_id == "Speaker 4"
    # Split timestamp lands at or after the start of "Okay" (16:28).
    assert p.split_at_ms >= 16_28_000
    assert p.confidence >= 0.6


def test_propose_split_skips_confident_segments(tmp_path: Path) -> None:
    """Pass D should not propose splits on segments where speaker
    confidence is already high — those are exactly the segments the
    diarizer got right.
    """
    cfg = _cfg(tmp_path)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.executemany(
            """
            INSERT INTO transcript_segments
              (id, meeting_id, start_ms, end_ms, text, diarization_speaker_id,
               confidence, speaker_confidence)
            VALUES (?, 1, ?, ?, ?, ?, ?, ?)
            """,
            [
                (10, 0, 30_000, "This is a long confident statement. Okay, so I am happy with that.", "Speaker 1", 0.9, 0.9),
                (11, 31_000, 50_000, "Yes I agree.", "Speaker 2", 0.9, 0.9),
            ],
        )
    proposals = propose_segment_splits(cfg, 1)
    assert proposals == [], "no splits should fire on high-confidence segments"


def test_propose_split_skips_same_speaker_neighbor(tmp_path: Path) -> None:
    """If the next segment is the SAME speaker, splitting wouldn't
    help — both halves would point to the same person. Skip.
    """
    cfg = _cfg(tmp_path)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.executemany(
            """
            INSERT INTO transcript_segments
              (id, meeting_id, start_ms, end_ms, text, diarization_speaker_id,
               confidence, speaker_confidence)
            VALUES (?, 1, ?, ?, ?, ?, ?, ?)
            """,
            [
                (10, 0, 30_000, "Continuing my long answer here. Okay, so I am also thinking about the next point now.", "Speaker 1", 0.4, 0.3),
                (11, 31_000, 50_000, "and another thought from me.", "Speaker 1", 0.9, 0.9),
            ],
        )
    proposals = propose_segment_splits(cfg, 1)
    assert proposals == []


def test_propose_split_skips_opener_in_first_half(tmp_path: Path) -> None:
    """If the discourse-opener appears in the FIRST half of the
    segment, it's almost certainly just a normal mid-thought
    transition — not a speaker boundary leak. Skip.
    """
    cfg = _cfg(tmp_path)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.executemany(
            """
            INSERT INTO transcript_segments
              (id, meeting_id, start_ms, end_ms, text, diarization_speaker_id,
               confidence, speaker_confidence)
            VALUES (?, 1, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    10,
                    0,
                    30_000,
                    "Okay, so I am of the opinion this is fine and totally normal "
                    "and there is much more to add here right.",
                    "Speaker 1",
                    0.4,
                    0.3,
                ),
                (11, 31_000, 50_000, "Right.", "Speaker 2", 0.9, 0.9),
            ],
        )
    proposals = propose_segment_splits(cfg, 1)
    assert proposals == []


def test_persist_writes_review_item(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed_boundary_leak_pair(cfg)

    summary = persist_segment_split_proposals(cfg, 1)
    assert summary["total"] == 1
    assert summary["manual"] == 1
    assert summary["auto_applied"] == 0

    with sqlite3.connect(cfg.paths.database_path) as conn:
        item = conn.execute(
            "SELECT kind, status, payload_json, confidence "
            "FROM review_items WHERE meeting_id = 1"
        ).fetchone()
    assert item is not None
    assert item[0] == "segment_split_proposal"
    assert item[1] == "open"
    payload = json.loads(item[2])
    assert payload["segment_id"] == 15
    assert payload["tail_speaker_id"] == "Speaker 4"


def test_accept_applies_split(tmp_path: Path) -> None:
    """Accept should: shrink the head, insert a new tail segment with
    the proposed speaker, repoint words, and mark the review item
    resolved.
    """
    cfg = _cfg(tmp_path)
    _seed_boundary_leak_pair(cfg)
    persist_segment_split_proposals(cfg, 1)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        item_id = conn.execute(
            "SELECT id FROM review_items WHERE meeting_id = 1"
        ).fetchone()[0]

    result = accept_split_proposal(cfg, 1, item_id)

    assert result["head_segment_id"] == 15
    new_id = result["tail_segment_id"]
    assert new_id != 15
    with sqlite3.connect(cfg.paths.database_path) as conn:
        head = conn.execute(
            "SELECT end_ms, text, diarization_speaker_id FROM transcript_segments WHERE id = 15"
        ).fetchone()
        tail = conn.execute(
            "SELECT start_ms, text, diarization_speaker_id FROM transcript_segments WHERE id = ?",
            (new_id,),
        ).fetchone()
        # Head shrinks: end_ms now matches the split timestamp.
        assert head[0] == result["split_at_ms"]
        assert "Okay" not in head[1]
        # Tail picks up at the split, has the right speaker.
        assert tail[0] == result["split_at_ms"]
        assert "Okay" in tail[1]
        assert tail[2] == "Speaker 4"
        # Words after the split were repointed.
        tail_word_count = conn.execute(
            "SELECT COUNT(*) FROM transcript_words WHERE segment_id = ?",
            (new_id,),
        ).fetchone()[0]
        assert tail_word_count > 0
        # Review item marked resolved.
        status = conn.execute(
            "SELECT status FROM review_items WHERE id = ?", (item_id,)
        ).fetchone()[0]
        assert status == "resolved"


def test_reject_marks_proposal_rejected(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed_boundary_leak_pair(cfg)
    persist_segment_split_proposals(cfg, 1)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        item_id = conn.execute(
            "SELECT id FROM review_items WHERE meeting_id = 1"
        ).fetchone()[0]

    result = reject_split_proposal(cfg, 1, item_id)
    assert result["result"] == "rejected"

    # Idempotent on second call.
    result2 = reject_split_proposal(cfg, 1, item_id)
    assert result2["result"] == "already_rejected"


def test_accept_then_reject_raises_already_resolved(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed_boundary_leak_pair(cfg)
    persist_segment_split_proposals(cfg, 1)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        item_id = conn.execute(
            "SELECT id FROM review_items WHERE meeting_id = 1"
        ).fetchone()[0]
    accept_split_proposal(cfg, 1, item_id)
    with pytest.raises(ValueError, match="already_resolved"):
        reject_split_proposal(cfg, 1, item_id)


def test_accept_split_refuses_after_segment_edit(tmp_path: Path) -> None:
    """v0.2.10 audit H2: if the user edited the segment text after
    persisting the proposal, accepting should refuse — otherwise we'd
    silently overwrite the user's edit with the stale snapshot.
    """
    cfg = _cfg(tmp_path)
    _seed_boundary_leak_pair(cfg)
    persist_segment_split_proposals(cfg, 1)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        item_id = conn.execute(
            "SELECT id FROM review_items WHERE meeting_id = 1"
        ).fetchone()[0]
        # Simulate a user manually editing the segment text after the
        # proposal was generated.
        conn.execute(
            "UPDATE transcript_segments SET text = ? WHERE id = 15",
            ("user edited this completely",),
        )

    with pytest.raises(ValueError, match="segment_changed"):
        accept_split_proposal(cfg, 1, item_id)


def test_accept_split_no_words_falls_back_proportionally(tmp_path: Path) -> None:
    """v0.2.10 audit M6: some ASR paths don't persist per-word
    timestamps. Accept-split must still produce sane head/tail
    boundaries — the proportional fallback in `_locate_split_ms` does
    that.
    """
    cfg = _cfg(tmp_path)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        conn.executemany(
            """
            INSERT INTO transcript_segments
              (id, meeting_id, start_ms, end_ms, text, diarization_speaker_id,
               confidence, speaker_confidence)
            VALUES (?, 1, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    20,
                    0,
                    30_000,
                    "first half of the segment with some content here "
                    "to ensure we are past the midpoint. Okay, so I am "
                    "of the opinion this is the tail.",
                    "Speaker 2",
                    0.45,
                    0.30,
                ),
                (21, 31_000, 50_000, "next segment", "Speaker 4", 0.9, 0.8),
            ],
        )
    persist_segment_split_proposals(cfg, 1)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        item_id = conn.execute(
            "SELECT id FROM review_items WHERE meeting_id = 1"
        ).fetchone()[0]

    result = accept_split_proposal(cfg, 1, item_id)
    assert result["head_segment_id"] == 20
    new_id = result["tail_segment_id"]
    with sqlite3.connect(cfg.paths.database_path) as conn:
        head_end = conn.execute(
            "SELECT end_ms FROM transcript_segments WHERE id = 20"
        ).fetchone()[0]
        tail_start = conn.execute(
            "SELECT start_ms FROM transcript_segments WHERE id = ?", (new_id,)
        ).fetchone()[0]
    # Split lands somewhere in the segment, not at the edges.
    assert 5000 < head_end < 25_000
    assert head_end == tail_start


def test_concurrent_accept_split_only_one_wins(tmp_path: Path) -> None:
    """v0.2.10 audit M3: hitting accept twice should produce exactly one
    tail segment, not two. The conditional UPDATE on the review item
    is the lock — the second caller sees rowcount=0 and bails.
    """
    cfg = _cfg(tmp_path)
    _seed_boundary_leak_pair(cfg)
    persist_segment_split_proposals(cfg, 1)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        item_id = conn.execute(
            "SELECT id FROM review_items WHERE meeting_id = 1"
        ).fetchone()[0]

    accept_split_proposal(cfg, 1, item_id)
    with pytest.raises(ValueError, match="already_resolved"):
        accept_split_proposal(cfg, 1, item_id)

    with sqlite3.connect(cfg.paths.database_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM transcript_segments WHERE meeting_id = 1"
        ).fetchone()[0]
    # Original 2 segments + one tail from the accepted split = 3.
    # A double-apply would produce 4.
    assert count == 3


def test_pipeline_smoke_persist_segment_splits(tmp_path: Path) -> None:
    """v0.2.10 audit M5: pipeline integration is wired behind a broad
    try/except — a failure to import or call would be silent. Smoke
    test that `persist_segment_split_proposals` runs cleanly on an
    empty meeting (the most likely call from the pipeline).
    """
    cfg = _cfg(tmp_path)
    # No segments → no proposals, no exceptions.
    summary = persist_segment_split_proposals(cfg, 1)
    assert summary["total"] == 0


def test_auto_apply_silent_tier_writes_resolved_review_item(tmp_path: Path) -> None:
    """v0.2.11: a proposal at/above the silent threshold (0.90) applies
    immediately and stores `status='auto_applied'` with `tier='silent'`
    in payload. The transcript split is reflected in transcript_segments
    even though the user did nothing.
    """
    cfg = _cfg(tmp_path, auto_apply=True, auto_apply_silent=0.5, auto_apply_toast=0.4)
    _seed_boundary_leak_pair(cfg)

    summary = persist_segment_split_proposals(cfg, 1)
    assert summary["total"] == 1
    assert summary["auto_applied"] == 1
    assert summary["manual"] == 0

    with sqlite3.connect(cfg.paths.database_path) as conn:
        item = conn.execute(
            "SELECT status, payload_json FROM review_items WHERE meeting_id=1"
        ).fetchone()
        assert item[0] == "auto_applied"
        payload = json.loads(item[1])
        assert payload["tier"] == "silent"
        # Transcript was actually split — original seg 15 shrank,
        # a new tail segment exists.
        segments = conn.execute(
            "SELECT id, diarization_speaker_id FROM transcript_segments "
            "WHERE meeting_id=1 ORDER BY id"
        ).fetchall()
        # Original 2 seeds + 1 inserted tail = 3
        assert len(segments) == 3
        tail_speakers = [s[1] for s in segments if s[0] not in (15, 16)]
        assert tail_speakers == ["Speaker 4"]


def test_auto_apply_toast_tier_tags_payload(tmp_path: Path) -> None:
    """Confidence in [toast, silent) → tier='toast' payload tag. Same
    apply behavior, frontend uses the tag to decide whether to surface
    a notice.
    """
    # Silent thr above proposal conf (0.75), toast thr below it.
    cfg = _cfg(tmp_path, auto_apply=True, auto_apply_silent=0.95, auto_apply_toast=0.5)
    _seed_boundary_leak_pair(cfg)

    summary = persist_segment_split_proposals(cfg, 1)
    assert summary["auto_applied"] == 1

    with sqlite3.connect(cfg.paths.database_path) as conn:
        payload = json.loads(
            conn.execute(
                "SELECT payload_json FROM review_items WHERE meeting_id=1"
            ).fetchone()[0]
        )
        assert payload["tier"] == "toast"


def test_auto_apply_below_thresholds_stays_manual(tmp_path: Path) -> None:
    """Confidence below the toast threshold → status='open' (manual
    review). Same as v0.2.10 behavior.
    """
    cfg = _cfg(tmp_path, auto_apply=True, auto_apply_silent=0.99, auto_apply_toast=0.99)
    _seed_boundary_leak_pair(cfg)

    summary = persist_segment_split_proposals(cfg, 1)
    assert summary["auto_applied"] == 0
    assert summary["manual"] == 1

    with sqlite3.connect(cfg.paths.database_path) as conn:
        status = conn.execute(
            "SELECT status FROM review_items WHERE meeting_id=1"
        ).fetchone()[0]
        assert status == "open"


def test_auto_apply_failure_demotes_to_manual(tmp_path: Path, monkeypatch) -> None:
    """Audit-fix M2: if `_apply_split_inline` raises, the row is
    demoted to status='open' and counted as manual. The audit list
    never claims an apply that didn't actually mutate the transcript.
    """
    cfg = _cfg(tmp_path, auto_apply=True, auto_apply_silent=0.5, auto_apply_toast=0.4)
    _seed_boundary_leak_pair(cfg)

    from app.services.repair import segment_splitter as splitter_module

    def explode(*_args, **_kwargs):
        raise RuntimeError("simulated apply failure")

    monkeypatch.setattr(splitter_module, "_apply_split_inline", explode)

    summary = persist_segment_split_proposals(cfg, 1)
    assert summary["total"] == 1
    assert summary["auto_applied"] == 0
    assert summary["manual"] == 1

    with sqlite3.connect(cfg.paths.database_path) as conn:
        status = conn.execute(
            "SELECT status FROM review_items WHERE meeting_id=1"
        ).fetchone()[0]
        # Demoted row should be 'open' so the user can apply by hand.
        assert status == "open"
        # No new segment was inserted.
        seg_count = conn.execute(
            "SELECT COUNT(*) FROM transcript_segments WHERE meeting_id=1"
        ).fetchone()[0]
        assert seg_count == 2  # original seeds, no split


def test_auto_apply_disabled_keeps_v0210_behavior(tmp_path: Path) -> None:
    """`auto_apply_enabled=False` reverts to v0.2.10 behavior: every
    proposal is manual regardless of confidence.
    """
    cfg = _cfg(tmp_path, auto_apply=False, auto_apply_silent=0.1, auto_apply_toast=0.1)
    _seed_boundary_leak_pair(cfg)
    summary = persist_segment_split_proposals(cfg, 1)
    assert summary["auto_applied"] == 0
    assert summary["manual"] == 1


def test_accept_wrong_kind_raises(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed_boundary_leak_pair(cfg)
    with sqlite3.connect(cfg.paths.database_path) as conn:
        cursor = conn.execute(
            "INSERT INTO review_items (meeting_id, kind, title, payload_json, status) "
            "VALUES (1, 'speaker_confidence', 'noise', '{}', 'open')"
        )
        wrong_id = cursor.lastrowid
    with pytest.raises(ValueError, match="not_a_split_proposal"):
        accept_split_proposal(cfg, 1, wrong_id)
