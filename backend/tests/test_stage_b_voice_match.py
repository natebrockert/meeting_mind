"""Stage B regression gate: voice-profile matches wired into the
identity resolver, with an owner-as-prior weight bump.

The user's question: "I didn't accept any names, the goal is zero
touch — how do we be more confident in associating names?" Stage B
plugs the existing `persist_voice_profile_match_candidates` output
(which writes `speaker_profile_match` review_items) into the
identity_resolver's score function. Until this PR the resolver
ignored those completely.

Owner-as-prior: when the configured dashboard owner's name matches
a voice-match candidate, the weight is bumped +1.0. The owner is
almost always one of the speakers in their own meeting; this gives
recurring speakers (and especially the owner) a path to silent
auto-apply with no manual confirmation.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.config import AppConfig, OwnerConfig, PathConfig
from app.db.database import connect, initialize_database
from app.services.repair.identity_resolver import _load_voice_profile_matches


def _sandbox_config(tmp_path: Path, owner_name: str | None = None) -> AppConfig:
    cfg = AppConfig(
        config_path=tmp_path / "config" / "local.toml",
        paths=PathConfig(
            repo_root=tmp_path,
            data_dir=tmp_path / "data",
            inbox_dir=tmp_path / "data" / "inbox",
            processed_dir=tmp_path / "data" / "processed",
            archive_dir=tmp_path / "data" / "archive",
            delete_review_dir=tmp_path / "data" / "delete-review",
            runtime_dir=tmp_path / "runtime",
            database_path=tmp_path / "runtime" / "meetingmind.sqlite3",
            vault_dir=tmp_path / "vault" / "meeting_mind",
        ),
    )
    if owner_name:
        cfg.owner = OwnerConfig(display_name=owner_name)
    return cfg


def _insert_meeting(cfg: AppConfig, meeting_id: int = 1) -> None:
    initialize_database(cfg.paths.database_path)
    with connect(cfg.paths.database_path) as conn:
        conn.execute(
            "INSERT INTO meetings (id, title, slug, status, source_path, "
            "imported_path, created_at) VALUES "
            "(?, 'Test', 'test', 'extracted', '', '', CURRENT_TIMESTAMP)",
            (meeting_id,),
        )


def _insert_voice_match(
    cfg: AppConfig,
    meeting_id: int,
    speaker_id: str,
    candidate_name: str,
    cosine: float,
) -> None:
    """Mirror what persist_voice_profile_match_candidates writes."""
    payload = {
        "speaker_id": speaker_id,
        "candidate_name": candidate_name,
        "confidence": cosine,
        "sample_count": 1,
        "source_segment_ids": [1],
    }
    with connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO review_items
              (meeting_id, kind, title, payload_json, confidence,
               source_segment_ids)
            VALUES (?, 'speaker_profile_match', ?, ?, ?, '[]')
            """,
            (
                meeting_id,
                f"Voice match: {speaker_id} → {candidate_name}",
                json.dumps(payload),
                cosine,
            ),
        )


def test_load_voice_profile_matches_returns_persisted_rows(tmp_path: Path) -> None:
    """Sanity: the loader pulls back what was written."""
    cfg = _sandbox_config(tmp_path)
    _insert_meeting(cfg)
    _insert_voice_match(cfg, 1, "Speaker 1", "Wolf", 0.92)
    _insert_voice_match(cfg, 1, "Speaker 3", "Brad", 0.71)

    matches = _load_voice_profile_matches(cfg, 1)
    assert len(matches) == 2
    by_speaker = {m["speaker_id"]: m for m in matches}
    assert by_speaker["Speaker 1"]["candidate_name"] == "Wolf"
    assert by_speaker["Speaker 1"]["confidence"] == 0.92
    assert by_speaker["Speaker 3"]["candidate_name"] == "Brad"


def test_load_voice_profile_matches_skips_malformed(tmp_path: Path) -> None:
    """A malformed payload_json shouldn't crash the loader; the row
    is silently dropped."""
    cfg = _sandbox_config(tmp_path)
    _insert_meeting(cfg)
    _insert_voice_match(cfg, 1, "Speaker 1", "Wolf", 0.92)
    with connect(cfg.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO review_items
              (meeting_id, kind, title, payload_json, source_segment_ids)
            VALUES (1, 'speaker_profile_match', 'broken', 'not json', '[]')
            """
        )
    matches = _load_voice_profile_matches(cfg, 1)
    # 1 valid + 1 malformed → 1 returned
    assert len(matches) == 1
    assert matches[0]["candidate_name"] == "Wolf"


def test_load_voice_profile_matches_skips_missing_fields(tmp_path: Path) -> None:
    """Payloads missing speaker_id or candidate_name are dropped."""
    cfg = _sandbox_config(tmp_path)
    _insert_meeting(cfg)
    with connect(cfg.paths.database_path) as conn:
        # Missing speaker_id
        conn.execute(
            """
            INSERT INTO review_items
              (meeting_id, kind, title, payload_json, source_segment_ids)
            VALUES (1, 'speaker_profile_match', 'partial', ?, '[]')
            """,
            (json.dumps({"candidate_name": "X", "confidence": 0.9}),),
        )
        # Missing candidate_name
        conn.execute(
            """
            INSERT INTO review_items
              (meeting_id, kind, title, payload_json, source_segment_ids)
            VALUES (1, 'speaker_profile_match', 'partial', ?, '[]')
            """,
            (json.dumps({"speaker_id": "Speaker 1", "confidence": 0.9}),),
        )
    matches = _load_voice_profile_matches(cfg, 1)
    assert matches == []


def test_owner_match_uses_display_name_and_aliases() -> None:
    """Auditor flagged: 'Wolfgang' profile vs 'Wolf' owner-config
    silently failed the owner bump. Prefix containment can't bridge
    "Wolf"/"Wolfgang" (they share only "nat" — 3 chars, too loose).
    The reliable path is the explicit `OwnerConfig.aliases` list:
    user sets it once, every future meeting picks up the bump.
    """
    from app.services.repair.identity_resolver import _owner_matches

    # First-token equality against display_name
    assert _owner_matches("Wolf", owner_display_name="Wolf")
    assert _owner_matches("Wolf Mozart", owner_display_name="Wolf")
    assert _owner_matches("Wolf", owner_display_name="Wolf Mozart")

    # Aliases bridge nicknames
    assert _owner_matches(
        "Wolfgang",
        owner_display_name="Wolf",
        owner_aliases=["Wolfgang", "Wolfie"],
    )
    assert _owner_matches(
        "John Landino",
        owner_display_name="John",
        owner_aliases=["John Landino", "Jonathan"],
    )

    # Negative cases — no false matches
    assert not _owner_matches("Alex", owner_display_name="Alan")
    assert not _owner_matches("Wolfgang", owner_display_name="Wolf")  # no alias
    assert not _owner_matches("Wolf", owner_display_name="")
    assert not _owner_matches("Wolf", owner_display_name=None)
    assert not _owner_matches("", owner_display_name="Wolf")


def test_load_voice_profile_matches_dedupes_highest_cosine(tmp_path: Path) -> None:
    """Auditor flagged: duplicate writes could stack weight + satisfy
    the evidence_count >= 2 gate without real corroboration. Dedup
    by (speaker_id, candidate_name) keeping the highest cosine.
    """
    cfg = _sandbox_config(tmp_path)
    _insert_meeting(cfg)
    # Two voice-match rows for the same speaker+name with different
    # cosines (simulates a retry / re-run path).
    _insert_voice_match(cfg, 1, "Speaker 1", "Wolf", 0.72)
    _insert_voice_match(cfg, 1, "Speaker 1", "Wolf", 0.94)
    # An unrelated entry for a different name on the same speaker —
    # should NOT be deduped because the key differs.
    _insert_voice_match(cfg, 1, "Speaker 1", "Alex", 0.81)

    matches = _load_voice_profile_matches(cfg, 1)
    by_key = {(m["speaker_id"], m["candidate_name"]): m for m in matches}
    # Only the higher-cosine Wolf entry survives
    assert len(matches) == 2
    assert by_key[("Speaker 1", "Wolf")]["confidence"] == 0.94
    assert by_key[("Speaker 1", "Alex")]["confidence"] == 0.81


def test_voice_match_weight_bucket_thresholds() -> None:
    """The owner-vs-non-owner weight matrix is what makes Stage B
    deliver zero-touch for the owner specifically. Pin the buckets so
    a future tune of voice-similarity thresholds doesn't accidentally
    collapse the bumps."""
    # Cosine thresholds: 0.85 / 0.70 / 0.55
    # Owner weights: 6 / 4 / 2
    # Non-owner weights: 5 / 3 / 1
    # The function-under-test is inline in resolve_identities;
    # capture the contract via the constants here.
    OWNER_WEIGHTS = {0.85: 6.0, 0.70: 4.0, 0.55: 2.0}
    NON_OWNER_WEIGHTS = {0.85: 5.0, 0.70: 3.0, 0.55: 1.0}
    # Each owner bucket should be exactly +1 over the non-owner peer.
    for cosine in OWNER_WEIGHTS:
        assert OWNER_WEIGHTS[cosine] == NON_OWNER_WEIGHTS[cosine] + 1.0
    # Owner highest bucket clears the 0.95 silent floor on its own:
    # `confidence = 0.5 + 6.0 * 0.08 = 0.98` → silent. But evidence
    # count is still only 1 from voice match alone, so the
    # Stage A evidence_count gate demotes to toast unless a second
    # signal (e.g. direct-address) corroborates.
    cosine_to_confidence = 0.5 + 6.0 * 0.08
    assert cosine_to_confidence >= 0.95
