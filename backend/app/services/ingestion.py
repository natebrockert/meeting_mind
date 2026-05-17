from __future__ import annotations

import shutil
import threading
from dataclasses import dataclass
from pathlib import Path

from app.config import AppConfig, ensure_local_layout
from app.db.database import connect
from app.services.audio import (
    is_supported_media,
    probe_duration_seconds,
    sha256_file,
    slugify_filename,
)


@dataclass(frozen=True)
class IngestResult:
    source_path: Path
    meeting_id: int | None
    status: str
    detail: str = ""


def _unique_destination(processed_dir: Path, source: Path) -> Path:
    base = processed_dir / source.name
    if not base.exists():
        return base
    for index in range(1, 10_000):
        candidate = processed_dir / f"{source.stem}-{index}{source.suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not create unique processed destination for {source}")


def _unique_slug(conn, slug: str) -> str:
    existing = conn.execute("SELECT 1 FROM meetings WHERE slug = ?", (slug,)).fetchone()
    if not existing:
        return slug
    for index in range(1, 10_000):
        candidate = f"{slug}-{index}"
        existing = conn.execute("SELECT 1 FROM meetings WHERE slug = ?", (candidate,)).fetchone()
        if not existing:
            return candidate
    raise RuntimeError(f"Could not create unique meeting slug for {slug}")


def ingest_file(
    config: AppConfig,
    source_path: Path,
    *,
    template: str | None = None,
) -> IngestResult:
    """Ingest a single file. When `template` is supplied, the new meeting row
    is stamped with that extraction template at creation so the first
    extraction uses it without a separate template-switch round trip.
    """
    ensure_local_layout(config)
    if not source_path.exists():
        return IngestResult(source_path=source_path, meeting_id=None, status="missing")
    if not is_supported_media(source_path):
        return IngestResult(source_path=source_path, meeting_id=None, status="unsupported")

    duration = probe_duration_seconds(source_path)
    source_hash = sha256_file(source_path)
    title = source_path.stem
    base_slug = slugify_filename(source_path)

    with connect(config.paths.database_path) as conn:
        existing = conn.execute(
            "SELECT meeting_id FROM source_files WHERE source_hash = ?",
            (source_hash,),
        ).fetchone()
        if existing:
            duplicate_path = _unique_destination(config.paths.archive_dir, source_path)
            shutil.move(str(source_path), duplicate_path)
            # `detail` used to carry the raw SHA-256 — leaked the file
            # fingerprint over the HTTP response. The hash already lives in
            # the source_files table for internal dedup; surface a short
            # human label instead.
            return IngestResult(
                duplicate_path,
                int(existing["meeting_id"]),
                "duplicate",
                "duplicate_of_existing_meeting",
            )
        slug = _unique_slug(conn, base_slug)
        destination = _unique_destination(config.paths.processed_dir, source_path)
        shutil.move(str(source_path), destination)

        cursor = conn.execute(
            """
            INSERT INTO meetings (title, slug, source_path, imported_path, duration_seconds, status, template)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title,
                slug,
                str(source_path),
                str(destination),
                duration,
                "ingested",
                template,
            ),
        )
        meeting_id = int(cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO source_files (meeting_id, storage_path, source_hash, retention_status)
            VALUES (?, ?, ?, ?)
            """,
            (meeting_id, str(destination), source_hash, "processed"),
        )
        conn.execute(
            """
            INSERT INTO processing_jobs (meeting_id, stage, status, progress)
            VALUES (?, ?, ?, ?)
            """,
            (meeting_id, "ingestion", "complete", 1.0),
        )
    return IngestResult(destination, meeting_id, "ingested", "")


# Module-level lock so concurrent `ingest_pending_files` (or
# `ingest_pending_files` + `/api/upload`-driven scans) don't race over
# the inbox directory. Audit finding M-B: previously two concurrent
# uploads each kicked a full inbox scan, so the second upload's call
# could ingest the first's file and return that result to the wrong
# caller. The lock is process-local, which is enough — only one
# uvicorn worker handles ingestion in this app.
_INGEST_LOCK = threading.Lock()


def ingest_pending_files(
    config: AppConfig,
    *,
    template: str | None = None,
    template_map: dict[str, str | None] | None = None,
    only_filename: str | None = None,
) -> list[IngestResult]:
    """Ingest files in the watch folder.

    `template_map` maps filename -> chosen template for per-file overrides.
    Files not in the map fall back to the `template` argument (a bulk
    default). Files matched in the map but with None value use None too —
    the user explicitly asked for "no template," not the default.

    `only_filename` restricts ingestion to a single file in the inbox —
    used by `/api/upload` so two concurrent uploads can't ingest each
    other's files. Falsy => scan and ingest everything in the inbox.
    """
    ensure_local_layout(config)
    results: list[IngestResult] = []
    with _INGEST_LOCK:
        for path in sorted(config.paths.inbox_dir.iterdir()):
            if not path.is_file():
                continue
            if only_filename and path.name != only_filename:
                continue
            chosen = (
                template_map[path.name]
                if template_map and path.name in template_map
                else template
            )
            results.append(ingest_file(config, path, template=chosen))
    return results
