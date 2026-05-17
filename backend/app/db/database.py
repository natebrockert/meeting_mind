from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.config import load_config

# Tables that cache per-meeting LLM output and need to be cleared when
# an extraction re-runs (or when the eval harness wants to A/B fresh
# runs across models). Maintenance contract: when you add a new LLM
# cache table, append it here. Anything that misses this list will
# silently serve stale data across re-extracts and across A/B passes,
# which is one of the easier classes of bug to ship by accident. The
# list is consumed by `eval_models._clear_caches_for_meeting`; service-
# specific `invalidate_*_cache` helpers in each module remain in place
# for targeted invalidation, but this list is the canonical "all of
# them" view.
PER_MEETING_LLM_CACHE_TABLES: tuple[str, ...] = (
    "meeting_key_terms",
    "meeting_llm_drivers",
    "meeting_driver_enrichment",
    "reflection_observations",
)


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS meetings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  slug TEXT NOT NULL UNIQUE,
  source_path TEXT NOT NULL,
  imported_path TEXT NOT NULL,
  duration_seconds REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  processed_at TEXT
);

CREATE TABLE IF NOT EXISTS source_files (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  meeting_id INTEGER NOT NULL,
  storage_path TEXT NOT NULL,
  source_hash TEXT NOT NULL,
  retention_status TEXT NOT NULL DEFAULT 'processed',
  deleted_at TEXT,
  FOREIGN KEY(meeting_id) REFERENCES meetings(id)
);

CREATE TABLE IF NOT EXISTS processing_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  meeting_id INTEGER,
  stage TEXT NOT NULL,
  status TEXT NOT NULL,
  progress REAL NOT NULL DEFAULT 0,
  error TEXT,
  started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_at TEXT,
  FOREIGN KEY(meeting_id) REFERENCES meetings(id)
);

CREATE TABLE IF NOT EXISTS transcript_segments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  meeting_id INTEGER NOT NULL,
  start_ms INTEGER NOT NULL,
  end_ms INTEGER NOT NULL,
  text TEXT NOT NULL,
  diarization_speaker_id TEXT NOT NULL,
  assigned_person_id INTEGER,
  confidence REAL,
  text_confidence REAL,
  speaker_confidence REAL,
  FOREIGN KEY(meeting_id) REFERENCES meetings(id)
);

CREATE TABLE IF NOT EXISTS transcript_words (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  meeting_id INTEGER NOT NULL,
  segment_id INTEGER NOT NULL,
  start_ms INTEGER NOT NULL,
  end_ms INTEGER NOT NULL,
  text TEXT NOT NULL,
  probability REAL,
  FOREIGN KEY(meeting_id) REFERENCES meetings(id),
  FOREIGN KEY(segment_id) REFERENCES transcript_segments(id)
);

CREATE TABLE IF NOT EXISTS speaker_assignment_evidence (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  meeting_id INTEGER NOT NULL,
  segment_id INTEGER NOT NULL,
  speaker_id TEXT NOT NULL,
  confidence REAL NOT NULL,
  metrics_json TEXT NOT NULL DEFAULT '{}',
  FOREIGN KEY(meeting_id) REFERENCES meetings(id),
  FOREIGN KEY(segment_id) REFERENCES transcript_segments(id)
);

CREATE TABLE IF NOT EXISTS people (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  display_name TEXT NOT NULL UNIQUE,
  aliases TEXT NOT NULL DEFAULT '[]',
  role TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_seen_at TEXT
);

CREATE TABLE IF NOT EXISTS speaker_assignments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  meeting_id INTEGER NOT NULL,
  diarization_speaker_id TEXT NOT NULL,
  person_id INTEGER,
  approved_label TEXT,
  confirmed_by_user INTEGER NOT NULL DEFAULT 0,
  confidence REAL,
  UNIQUE(meeting_id, diarization_speaker_id),
  FOREIGN KEY(meeting_id) REFERENCES meetings(id),
  FOREIGN KEY(person_id) REFERENCES people(id)
);

CREATE TABLE IF NOT EXISTS speaker_profiles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  display_name TEXT NOT NULL UNIQUE,
  embedding_json TEXT NOT NULL,
  sample_count INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS speaker_match_suggestions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  meeting_id INTEGER NOT NULL,
  diarization_speaker_id TEXT NOT NULL,
  speaker_profile_id INTEGER NOT NULL,
  confidence REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'suggested',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(meeting_id) REFERENCES meetings(id),
  FOREIGN KEY(speaker_profile_id) REFERENCES speaker_profiles(id)
);

CREATE TABLE IF NOT EXISTS speaker_profile_observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id INTEGER NOT NULL,
  display_name TEXT NOT NULL,
  meeting_id INTEGER NOT NULL,
  diarization_speaker_id TEXT NOT NULL,
  sample_segment_count INTEGER NOT NULL,
  sample_duration_ms INTEGER NOT NULL,
  lexical_fingerprint_json TEXT NOT NULL DEFAULT '[]',
  source_segment_ids TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(person_id, meeting_id, diarization_speaker_id),
  FOREIGN KEY(person_id) REFERENCES people(id),
  FOREIGN KEY(meeting_id) REFERENCES meetings(id)
);

CREATE TABLE IF NOT EXISTS workstreams (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  display_name TEXT NOT NULL UNIQUE,
  aliases TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS meeting_workstreams (
  meeting_id INTEGER NOT NULL,
  workstream_id INTEGER NOT NULL,
  confidence REAL,
  confirmed_by_user INTEGER NOT NULL DEFAULT 0,
  source_segment_ids TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY(meeting_id, workstream_id),
  FOREIGN KEY(meeting_id) REFERENCES meetings(id),
  FOREIGN KEY(workstream_id) REFERENCES workstreams(id)
);

CREATE TABLE IF NOT EXISTS action_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  meeting_id INTEGER NOT NULL,
  owner_person_id INTEGER,
  text TEXT NOT NULL,
  due_date TEXT,
  priority TEXT NOT NULL DEFAULT 'normal',
  status TEXT NOT NULL DEFAULT 'open',
  source_segment_ids TEXT NOT NULL DEFAULT '[]',
  -- cluster_id: soft self-reference to the canonical row's id when this
  -- row is a member of a near-duplicate cluster. Not declared as an FK
  -- because the meeting-wide DELETE on re-extract would race the FK
  -- check (canonical vs member deletion order is unspecified); the
  -- cluster_role enum is the source of truth and orphaned members
  -- self-heal at read time.
  cluster_id INTEGER,
  cluster_role TEXT,
  FOREIGN KEY(meeting_id) REFERENCES meetings(id)
);

CREATE TABLE IF NOT EXISTS review_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  meeting_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  confidence REAL,
  source_segment_ids TEXT NOT NULL DEFAULT '[]',
  resolved_at TEXT,
  FOREIGN KEY(meeting_id) REFERENCES meetings(id)
);

CREATE TABLE IF NOT EXISTS transcript_corrections (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  meeting_id INTEGER NOT NULL,
  segment_id INTEGER NOT NULL,
  original_text TEXT NOT NULL,
  corrected_text TEXT NOT NULL,
  reason TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  applied_at TEXT,
  FOREIGN KEY(meeting_id) REFERENCES meetings(id),
  FOREIGN KEY(segment_id) REFERENCES transcript_segments(id)
);

CREATE TABLE IF NOT EXISTS transcript_candidates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  meeting_id INTEGER NOT NULL,
  segment_id INTEGER NOT NULL,
  profile_name TEXT NOT NULL,
  provider TEXT NOT NULL,
  start_ms INTEGER NOT NULL,
  end_ms INTEGER NOT NULL,
  text TEXT NOT NULL,
  score REAL NOT NULL,
  metrics_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'suggested',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(meeting_id, segment_id, profile_name),
  FOREIGN KEY(meeting_id) REFERENCES meetings(id),
  FOREIGN KEY(segment_id) REFERENCES transcript_segments(id)
);

CREATE TABLE IF NOT EXISTS obsidian_exports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  meeting_id INTEGER NOT NULL,
  output_path TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  exported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(meeting_id) REFERENCES meetings(id)
);

-- Cache for the LLM-generated key terms shown as transcript highlights.
-- Without this cache, every dashboard page load called the quality model
-- to re-extract terms, adding 20-30s per Review-page open. Cached entry
-- is invalidated when extraction re-runs for a meeting.
CREATE TABLE IF NOT EXISTS meeting_key_terms (
  meeting_id INTEGER PRIMARY KEY,
  terms_json TEXT NOT NULL DEFAULT '[]',
  computed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(meeting_id) REFERENCES meetings(id)
);

-- LLM-judged conversation driver cache (reframing, challenge, unstick).
-- Mirrors the meeting_key_terms pattern: cached entry is invalidated
-- when extraction re-runs so a fresh transcript regenerates drivers.
-- Empty result is a valid cache hit ('[]') — we don't re-call the
-- model just because no drivers were found last time.
CREATE TABLE IF NOT EXISTS meeting_llm_drivers (
  meeting_id INTEGER PRIMARY KEY,
  drivers_json TEXT NOT NULL DEFAULT '[]',
  computed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(meeting_id) REFERENCES meetings(id)
);

-- Cache of LLM-rewritten Conversation Driver descriptions. The
-- deterministic kinds (topic_introduction, pivot_question, decision_
-- moment) yield mechanical one-liners about impact; this cache holds
-- the narrative rewrite that says who spoke, what they said, why it
-- mattered, and what came of it. Keyed by meeting_id; the JSON blob
-- carries description text keyed by (kind, segment_id) so the
-- enrichment can be spliced back into the freshly-computed driver
-- list on each overview load without re-calling the model.
-- Invalidated by extract_meeting_atoms.
CREATE TABLE IF NOT EXISTS meeting_driver_enrichment (
  meeting_id INTEGER PRIMARY KEY,
  enrichment_json TEXT NOT NULL DEFAULT '{}',
  computed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(meeting_id) REFERENCES meetings(id)
);

-- Reflections cache. Owner-scoped because the same meeting can produce
-- different Reflections under different owners (e.g. a manager and
-- their report both viewing a 1:1). Empty result is a valid cache hit
-- — many meetings produce zero observations by design. Invalidated at
-- the top of extract_meeting_atoms so a re-extracted meeting regenerates.
CREATE TABLE IF NOT EXISTS reflection_observations (
  meeting_id INTEGER NOT NULL,
  -- NOT NULL is load-bearing: SQLite treats NULL as distinct in
  -- composite PRIMARY KEY, so leaving this nullable would let two rows
  -- with (meeting_id=X, owner_person_id=NULL) coexist and the
  -- ON CONFLICT UPSERT would silently insert duplicates instead of
  -- updating. The compute path already short-circuits when there's no
  -- configured owner, so we never have a reason to write a NULL here.
  owner_person_id INTEGER NOT NULL,
  reflections_json TEXT NOT NULL DEFAULT '{}',
  computed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (meeting_id, owner_person_id),
  FOREIGN KEY(meeting_id) REFERENCES meetings(id)
);

-- v0.2.2: linguistic overlap detection. Pure text-based heuristic to find
-- moments where speakers talked over each other ("sorry, go ahead",
-- stuttering self-interrupt, rapid speaker alternation). Stored separately
-- from review_items because these are quality hints for the UI / synthesis,
-- not items the user needs to act on.
CREATE TABLE IF NOT EXISTS segment_overlap_hints (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  meeting_id INTEGER NOT NULL,
  segment_id INTEGER NOT NULL,
  partner_segment_id INTEGER,
  kind TEXT NOT NULL,        -- 'yield_marker' | 'stutter_interrupt' | 'rapid_alternation'
  evidence TEXT NOT NULL DEFAULT '',
  confidence REAL NOT NULL DEFAULT 0.5,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(meeting_id) REFERENCES meetings(id),
  FOREIGN KEY(segment_id) REFERENCES transcript_segments(id)
);
CREATE INDEX IF NOT EXISTS idx_overlap_hints_meeting
  ON segment_overlap_hints(meeting_id);

CREATE TABLE IF NOT EXISTS scheduled_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  job_type TEXT NOT NULL,
  schedule TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 0,
  model_policy_json TEXT NOT NULL DEFAULT '{}',
  last_run_at TEXT,
  next_run_at TEXT
);

CREATE TABLE IF NOT EXISTS scheduled_job_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scheduled_job_id INTEGER NOT NULL,
  status TEXT NOT NULL,
  started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_at TEXT,
  model_used TEXT,
  error TEXT,
  FOREIGN KEY(scheduled_job_id) REFERENCES scheduled_jobs(id)
);

CREATE TABLE IF NOT EXISTS segment_comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  meeting_id INTEGER NOT NULL,
  segment_id INTEGER NOT NULL,
  parent_id INTEGER,
  body TEXT NOT NULL,
  author TEXT NOT NULL DEFAULT 'you',
  status TEXT NOT NULL DEFAULT 'open',
  resolved_at TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(meeting_id) REFERENCES meetings(id) ON DELETE CASCADE,
  FOREIGN KEY(segment_id) REFERENCES transcript_segments(id) ON DELETE CASCADE,
  FOREIGN KEY(parent_id) REFERENCES segment_comments(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_segment_comments_segment
  ON segment_comments(segment_id);

-- Audit finding (perf HIGH): every meeting-scoped query in the app
-- (loading a meeting, refreshing the inbox, regenerating synthesis)
-- scans these tables by meeting_id. Without these indexes SQLite
-- falls back to full table scans, which is fine on tiny demo DBs but
-- compounds quickly once users have dozens of meetings with hundreds
-- of transcript segments each.
CREATE INDEX IF NOT EXISTS idx_transcript_segments_meeting
  ON transcript_segments(meeting_id);
CREATE INDEX IF NOT EXISTS idx_review_items_meeting
  ON review_items(meeting_id);
CREATE INDEX IF NOT EXISTS idx_action_items_meeting
  ON action_items(meeting_id);
CREATE INDEX IF NOT EXISTS idx_speaker_assignments_meeting
  ON speaker_assignments(meeting_id);
"""


def initialize_database(path: Path | None = None) -> None:
    db_path = path or load_config().paths.database_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)
        _ensure_columns(
            conn,
            "transcript_segments",
            {
                "text_confidence": "REAL",
                "speaker_confidence": "REAL",
            },
        )
        # `meetings.template` selects the extraction prompt (general /
        # standup / one_on_one / customer_interview). Existing rows stay
        # NULL → default to "general" at read time.
        _ensure_columns(conn, "meetings", {"template": "TEXT"})
        # `action_items.due_date_source` links a parsed due date back to
        # the segment where the date phrase was spoken so the dashboard
        # can jump-to-evidence. Existing actions stay NULL — readers
        # handle the missing case.
        _ensure_columns(conn, "action_items", {"due_date_source": "INTEGER"})
        # Action clustering: groups of near-duplicate actions are linked
        # via a self-FK to the cluster's canonical row. NULL on standalone
        # actions and on canonicals themselves (canonicals identify
        # themselves by cluster_role='canonical'). Readers filter members
        # out by default so the user sees one row per real commitment;
        # the canonical's payload carries member text + segment IDs for
        # the "N related mentions" disclosure.
        _ensure_columns(
            conn,
            "action_items",
            {"cluster_id": "INTEGER", "cluster_role": "TEXT"},
        )
        # `meetings.skip_reflections` is the per-meeting opt-out from
        # the Reflections feature. Sticky across regenerations so a
        # meeting the user explicitly excluded (sensitive 1:1, therapy
        # session, legal) doesn't regenerate Reflections on re-extract.
        _ensure_columns(
            conn, "meetings", {"skip_reflections": "INTEGER NOT NULL DEFAULT 0"}
        )
        # Backfill the threading + resolution columns onto segment_comments
        # for installs that pre-date the threaded-comments feature.
        _ensure_columns(
            conn,
            "segment_comments",
            {
                "parent_id": "INTEGER",
                "status": "TEXT DEFAULT 'open'",
                "resolved_at": "TEXT",
            },
        )


def _ensure_columns(
    conn: sqlite3.Connection,
    table_name: str,
    columns: dict[str, str],
) -> None:
    existing = {
        row[1]
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }
    for column_name, column_type in columns.items():
        if column_name not in existing:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    db_path = path or load_config().paths.database_path
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # SQLite ships with foreign keys OFF by default. Turn them on per
    # connection so ON DELETE CASCADE (e.g. segment_comments) actually
    # fires and orphaned rows can't accumulate.
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        # Roll back any partial writes so the next connection doesn't see
        # them. sqlite3's default isolation mode autocommits each statement
        # outside an explicit transaction, but DDL + multi-statement helpers
        # inside the yield still need an explicit rollback on failure.
        conn.rollback()
        raise
    finally:
        conn.close()
