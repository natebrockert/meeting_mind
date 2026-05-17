from __future__ import annotations

import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from app.config import AppConfig
from app.db.database import connect
from app.services.asr_vocabulary import build_asr_initial_prompt
from app.services.audio import extract_audio_clip
from app.services.speaker_identity import persist_speaker_name_candidates
from app.services.transcript_quality import detect_transcript_quality_issue
from app.services.transcription.factory import create_transcription_provider


@dataclass(frozen=True)
class AsrCandidateProfile:
    """A single ASR retry strategy used for transcript repair comparisons."""

    name: str
    condition_on_previous_text: bool
    compression_ratio_threshold: float
    hallucination_silence_threshold: float


@dataclass(frozen=True)
class AsrCandidateResult:
    """User-visible transcript alternative with its aggregate confidence score."""

    segment_id: int
    profile_name: str
    text: str
    score: float


@dataclass(frozen=True)
class _CandidateOutput:
    segment_id: int
    profile_name: str
    start_ms: int
    end_ms: int
    text: str
    provider_metrics: dict


def run_asr_candidate_passes(
    config: AppConfig,
    meeting_id: int,
    audio_path: Path,
    limit: int | None = None,
) -> list[AsrCandidateResult]:
    """Generate competing ASR outputs for low-confidence or suspicious segments."""
    targets = _candidate_target_segments(config, meeting_id, limit or config.asr.candidate_limit)
    results: list[AsrCandidateResult] = []
    errors: list[str] = []
    with connect(config.paths.database_path) as conn:
        _mark_stale_candidates(conn, meeting_id)
        conn.execute(
            """
            INSERT INTO processing_jobs
              (meeting_id, stage, status, progress)
            VALUES (?, ?, ?, ?)
            """,
            (meeting_id, "asr_candidates", "running", 0.0),
        )
        job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    try:
        for target in targets:
            outputs: list[_CandidateOutput] = []
            for profile in _asr_candidate_profiles(config):
                try:
                    clip = extract_audio_clip(
                        audio_path,
                        config.paths.runtime_dir / "asr-candidates",
                        int(target["start_ms"]),
                        int(target["end_ms"]),
                        f"s{target['id']}-{profile.name}",
                        padding_ms=config.asr.candidate_clip_padding_ms,
                    )
                    text, provider_metrics = _transcribe_candidate(config, clip, profile)
                except Exception as exc:
                    errors.append(
                        "segment "
                        f"{target['id']} {profile.name}: {type(exc).__name__}: {exc}"
                    )
                    continue
                if not text:
                    continue
                outputs.append(
                    _CandidateOutput(
                        segment_id=int(target["id"]),
                        profile_name=profile.name,
                        start_ms=int(target["start_ms"]),
                        end_ms=int(target["end_ms"]),
                        text=text,
                        provider_metrics=provider_metrics,
                    )
                )
            for output in outputs:
                peer_texts = [
                    item.text
                    for item in outputs
                    if item.profile_name != output.profile_name
                ]
                score, metrics = score_asr_candidate(
                    str(target["text"]),
                    output.text,
                    provider_metrics=output.provider_metrics,
                    peer_texts=peer_texts,
                )
                _persist_candidate(
                    config,
                    meeting_id,
                    output.segment_id,
                    output.profile_name,
                    output.start_ms,
                    output.end_ms,
                    output.text,
                    score,
                    metrics,
                )
                results.append(
                    AsrCandidateResult(
                        segment_id=output.segment_id,
                        profile_name=output.profile_name,
                        text=output.text,
                        score=score,
                    )
                )
        status = "complete"
        if errors:
            status = "partial" if results else "failed"
        with connect(config.paths.database_path) as conn:
            conn.execute(
                """
                UPDATE processing_jobs
                SET status = ?, progress = ?, error = ?, completed_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, 1.0, "\n".join(errors[:8]) if errors else None, job_id),
            )
        persist_candidate_audit_items(config, meeting_id)
    except Exception as exc:
        with connect(config.paths.database_path) as conn:
            conn.execute(
                """
                UPDATE processing_jobs
                SET status = ?, error = ?, completed_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                ("failed", str(exc), job_id),
            )
        raise
    return results


def run_automatic_audio_repair(
    config: AppConfig,
    meeting_id: int,
    audio_path: Path,
) -> dict:
    """Run the ASR repair pipeline when enabled and auto-accept conservative winners."""
    if not config.asr.auto_repair_after_process:
        return {"generated": 0, "accepted": 0}
    results = run_asr_candidate_passes(config, meeting_id, audio_path)
    accepted = rerank_and_accept_transcript_candidates(config, meeting_id)
    return {"generated": len(results), "accepted": accepted}


def rerank_and_accept_transcript_candidates(config: AppConfig, meeting_id: int) -> int:
    """Accept only the top candidate per segment when it clears repair thresholds."""
    with connect(config.paths.database_path) as conn:
        rows = conn.execute(
            """
            SELECT
              tc.id,
              tc.segment_id,
              tc.text AS candidate_text,
              tc.score,
              tc.metrics_json,
              ts.text AS accepted_text
            FROM transcript_candidates tc
            JOIN transcript_segments ts
              ON ts.id = tc.segment_id
             AND ts.meeting_id = tc.meeting_id
            WHERE tc.meeting_id = ?
              AND tc.status = 'suggested'
            ORDER BY tc.segment_id, tc.score DESC, tc.id
            """,
            (meeting_id,),
        ).fetchall()
    accepted = 0
    seen_segments: set[int] = set()
    for row in rows:
        segment_id = int(row["segment_id"])
        if segment_id in seen_segments:
            continue
        seen_segments.add(segment_id)
        try:
            metrics = json.loads(row["metrics_json"] or "{}")
        except json.JSONDecodeError:
            metrics = {}
        if not _should_auto_accept_candidate(
            config,
            float(row["score"]),
            metrics,
            str(row["accepted_text"]),
            str(row["candidate_text"]),
        ):
            continue
        accept_transcript_candidate(config, meeting_id, int(row["id"]))
        accepted += 1
    return accepted


def _mark_stale_candidates(conn, meeting_id: int) -> None:
    conn.execute(
        """
        UPDATE transcript_candidates
        SET status = 'stale'
        WHERE meeting_id = ?
          AND status = 'suggested'
        """,
        (meeting_id,),
    )


def accept_transcript_candidate(config: AppConfig, meeting_id: int, candidate_id: int) -> None:
    """Apply a transcript alternative and preserve the original text as a correction."""
    with connect(config.paths.database_path) as conn:
        candidate = conn.execute(
            """
            SELECT *
            FROM transcript_candidates
            WHERE id = ? AND meeting_id = ?
            """,
            (candidate_id, meeting_id),
        ).fetchone()
        if not candidate:
            raise ValueError("candidate_not_found")
        if candidate["status"] != "suggested":
            raise ValueError("candidate_not_suggested")
        segment = conn.execute(
            """
            SELECT text
            FROM transcript_segments
            WHERE id = ? AND meeting_id = ?
            """,
            (candidate["segment_id"], meeting_id),
        ).fetchone()
        if not segment:
            raise ValueError("segment_not_found")
        conn.execute(
            """
            INSERT INTO transcript_corrections
              (meeting_id, segment_id, original_text, corrected_text, reason, applied_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                meeting_id,
                candidate["segment_id"],
                segment["text"],
                candidate["text"],
                f"accepted_asr_candidate:{candidate['profile_name']}",
            ),
        )
        conn.execute(
            """
            UPDATE transcript_segments
            SET text = ?,
                text_confidence = NULL,
                confidence = NULL
            WHERE id = ? AND meeting_id = ?
            """,
            (candidate["text"], candidate["segment_id"], meeting_id),
        )
        conn.execute(
            "DELETE FROM transcript_words WHERE meeting_id = ? AND segment_id = ?",
            (meeting_id, candidate["segment_id"]),
        )
        _mark_speaker_evidence_after_candidate_accept(
            conn,
            meeting_id,
            int(candidate["segment_id"]),
            int(candidate_id),
        )
        conn.execute(
            """
            UPDATE transcript_candidates
            SET status = CASE WHEN id = ? THEN 'accepted' ELSE 'superseded' END
            WHERE meeting_id = ? AND segment_id = ?
            """,
            (candidate_id, meeting_id, candidate["segment_id"]),
        )
    _refresh_review_state(config, meeting_id)


def reject_transcript_candidate(config: AppConfig, meeting_id: int, candidate_id: int) -> None:
    with connect(config.paths.database_path) as conn:
        candidate = conn.execute(
            """
            SELECT segment_id, status
            FROM transcript_candidates
            WHERE id = ? AND meeting_id = ?
            """,
            (candidate_id, meeting_id),
        ).fetchone()
        if not candidate:
            raise ValueError("candidate_not_found")
        if candidate["status"] != "suggested":
            raise ValueError("candidate_not_suggested")
        conn.execute(
            """
            UPDATE transcript_candidates
            SET status = 'rejected'
            WHERE id = ? AND meeting_id = ?
            """,
            (candidate_id, meeting_id),
        )
    persist_candidate_audit_items(config, meeting_id)


def score_asr_candidate(
    current_text: str,
    candidate_text: str,
    provider_metrics: dict | None = None,
    peer_texts: list[str] | None = None,
) -> tuple[float, dict]:
    """Score an ASR alternative using text agreement, repetition risk, and provider signals."""
    current_tokens = _tokens(current_text)
    candidate_tokens = _tokens(candidate_text)
    agreement = _jaccard(current_tokens, candidate_tokens)
    issue = detect_transcript_quality_issue(candidate_text)
    repetition_penalty = issue.confidence if issue else 0.0
    length_ratio = len(candidate_tokens) / max(1, len(current_tokens))
    length_score = 1.0 - min(1.0, abs(1.0 - length_ratio))
    provider_quality = _provider_quality_score(provider_metrics or {})
    interpass_agreement = _interpass_agreement(candidate_text, peer_texts or [])
    score = (
        (agreement * 0.30)
        + ((1.0 - repetition_penalty) * 0.25)
        + (length_score * 0.15)
        + (provider_quality * 0.15)
        + (interpass_agreement * 0.15)
    )
    metrics = {
        "agreement": round(agreement, 3),
        "repetition_penalty": round(repetition_penalty, 3),
        "length_ratio": round(length_ratio, 3),
        "provider_quality": round(provider_quality, 3),
        "interpass_agreement": round(interpass_agreement, 3),
        "quality_issue": issue.kind if issue else None,
    }
    metrics.update(_scoring_provider_metrics(provider_metrics or {}))
    return round(max(0.0, min(0.99, score)), 3), metrics


def persist_candidate_audit_items(
    config: AppConfig,
    meeting_id: int,
    minimum_score: float = 0.85,
) -> int:
    """Surface strong transcript alternatives as review items for the dashboard."""
    with connect(config.paths.database_path) as conn:
        conn.execute(
            "DELETE FROM review_items WHERE meeting_id = ? AND kind = ?",
            (meeting_id, "transcript_audit"),
        )
        rows = conn.execute(
            """
            SELECT
              tc.segment_id,
              tc.profile_name,
              tc.text AS candidate_text,
              tc.score,
              ts.text AS accepted_text
            FROM transcript_candidates tc
            JOIN transcript_segments ts
              ON ts.id = tc.segment_id
             AND ts.meeting_id = tc.meeting_id
            WHERE tc.meeting_id = ?
              AND tc.status = 'suggested'
              AND tc.score >= ?
            ORDER BY tc.segment_id, tc.score DESC
            """,
            (meeting_id, minimum_score),
        ).fetchall()
        seen_segment_ids: set[int] = set()
        count = 0
        for row in rows:
            segment_id = int(row["segment_id"])
            if segment_id in seen_segment_ids:
                continue
            if not _has_material_candidate_difference(
                row["accepted_text"],
                row["candidate_text"],
            ):
                continue
            seen_segment_ids.add(segment_id)
            conn.execute(
                """
                INSERT INTO review_items
                  (meeting_id, kind, title, payload_json, confidence, source_segment_ids)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    meeting_id,
                    "transcript_audit",
                    f"ASR candidate differs materially: segment {segment_id}",
                    json.dumps(
                        {
                            "segment_id": segment_id,
                            "profile_name": row["profile_name"],
                            "accepted_text": row["accepted_text"],
                            "candidate_text": row["candidate_text"],
                            "candidate_score": row["score"],
                        }
                    ),
                    row["score"],
                    json.dumps([segment_id]),
                ),
            )
            count += 1
    return count


def _candidate_target_segments(config: AppConfig, meeting_id: int, limit: int) -> list:
    with connect(config.paths.database_path) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT ts.*
            FROM transcript_segments ts
            LEFT JOIN review_items ri
              ON ri.meeting_id = ts.meeting_id
             AND ri.source_segment_ids = '[' || ts.id || ']'
             AND ri.kind IN ('transcript_quality', 'transcript_audit')
            WHERE ts.meeting_id = ?
              AND (
                ri.id IS NOT NULL
                OR (ts.text_confidence IS NOT NULL AND ts.text_confidence < ?)
                OR (
                  ts.text_confidence IS NOT NULL
                  AND (ts.end_ms - ts.start_ms) >= ?
                  AND ts.text_confidence < ?
                )
              )
            ORDER BY COALESCE(ts.text_confidence, ts.confidence, 1.0), ts.start_ms
            LIMIT ?
            """,
            (
                meeting_id,
                config.review.transcript_uncertainty_threshold,
                config.asr.candidate_long_segment_ms,
                config.asr.candidate_long_segment_content_threshold,
                limit,
            ),
        ).fetchall()
    return rows


def _asr_candidate_profiles(config: AppConfig) -> list[AsrCandidateProfile]:
    return [
        AsrCandidateProfile(
            name="conservative",
            condition_on_previous_text=False,
            compression_ratio_threshold=(
                config.asr.candidate_conservative_compression_ratio_threshold
            ),
            hallucination_silence_threshold=(
                config.asr.candidate_conservative_hallucination_silence_threshold
            ),
        ),
        AsrCandidateProfile(
            name="balanced",
            condition_on_previous_text=False,
            compression_ratio_threshold=config.asr.compression_ratio_threshold,
            hallucination_silence_threshold=config.asr.hallucination_silence_threshold,
        ),
        AsrCandidateProfile(
            name="contextual",
            condition_on_previous_text=True,
            compression_ratio_threshold=(
                config.asr.candidate_contextual_compression_ratio_threshold
            ),
            hallucination_silence_threshold=(
                config.asr.candidate_contextual_hallucination_silence_threshold
            ),
        ),
    ]


def _transcribe_candidate(
    config: AppConfig,
    clip_path: Path,
    profile: AsrCandidateProfile,
) -> tuple[str, dict]:
    provider = create_transcription_provider(
        config,
        condition_on_previous_text=profile.condition_on_previous_text,
        compression_ratio_threshold=profile.compression_ratio_threshold,
        hallucination_silence_threshold=profile.hallucination_silence_threshold,
        initial_prompt=build_asr_initial_prompt(config),
    )
    segments = provider.transcribe(clip_path)
    return " ".join(segment.text for segment in segments).strip(), _provider_metrics(segments)


def _persist_candidate(
    config: AppConfig,
    meeting_id: int,
    segment_id: int,
    profile_name: str,
    start_ms: int,
    end_ms: int,
    text: str,
    score: float,
    metrics: dict,
) -> None:
    with connect(config.paths.database_path) as conn:
        conn.execute(
            """
            INSERT INTO transcript_candidates
              (meeting_id, segment_id, profile_name, provider, start_ms, end_ms,
               text, score, metrics_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(meeting_id, segment_id, profile_name)
            DO UPDATE SET
              text = excluded.text,
              score = excluded.score,
              metrics_json = excluded.metrics_json,
              status = 'suggested',
              created_at = CURRENT_TIMESTAMP
            """,
            (
                meeting_id,
                segment_id,
                profile_name,
                "mlx_whisper",
                start_ms,
                end_ms,
                text,
                score,
                json.dumps(metrics),
            ),
        )


def _mark_speaker_evidence_after_candidate_accept(
    conn,
    meeting_id: int,
    segment_id: int,
    candidate_id: int,
) -> None:
    row = conn.execute(
        """
        SELECT id, metrics_json
        FROM speaker_assignment_evidence
        WHERE meeting_id = ? AND segment_id = ?
        """,
        (meeting_id, segment_id),
    ).fetchone()
    if not row:
        return
    try:
        metrics = json.loads(row["metrics_json"])
    except json.JSONDecodeError:
        metrics = {}
    metrics.update(
        {
            "accepted_asr_candidate_id": candidate_id,
            "transcript_words_invalidated": True,
            "has_word_timestamps": False,
        }
    )
    conn.execute(
        "UPDATE speaker_assignment_evidence SET metrics_json = ? WHERE id = ?",
        (json.dumps(metrics), row["id"]),
    )


def _refresh_review_state(config: AppConfig, meeting_id: int) -> None:
    from app.services.pipeline import persist_speaker_confidence_issues
    from app.services.speaker_learning import refresh_confirmed_speaker_profile_observations
    from app.services.transcript_quality import persist_transcript_quality_issues

    persist_transcript_quality_issues(config, meeting_id)
    persist_candidate_audit_items(config, meeting_id)
    persist_speaker_confidence_issues(config, meeting_id)
    persist_speaker_name_candidates(config, meeting_id)
    refresh_confirmed_speaker_profile_observations(config, meeting_id)


def _normalized_text(text: str) -> str:
    return " ".join(_ordered_tokens(text))


def _has_material_candidate_difference(accepted_text: str, candidate_text: str) -> bool:
    if _normalized_text(accepted_text) == _normalized_text(candidate_text):
        return False
    accepted_tokens = _ordered_tokens(accepted_text)
    candidate_tokens = _ordered_tokens(candidate_text)
    if not accepted_tokens or not candidate_tokens:
        return accepted_tokens != candidate_tokens
    if abs(len(candidate_tokens) - len(accepted_tokens)) >= 4:
        return True
    similarity = SequenceMatcher(None, accepted_tokens, candidate_tokens).ratio()
    return similarity < 0.94


def _should_auto_accept_candidate(
    config: AppConfig,
    score: float,
    metrics: dict,
    accepted_text: str,
    candidate_text: str,
) -> bool:
    if score < config.asr.candidate_auto_accept_score_threshold:
        return False
    interpass_agreement = float(metrics.get("interpass_agreement") or 0.0)
    if interpass_agreement < config.asr.candidate_auto_accept_interpass_threshold:
        return False
    if metrics.get("quality_issue"):
        return False
    return _has_material_candidate_difference(accepted_text, candidate_text)


def _provider_metrics(segments) -> dict:
    metric_names = ["avg_logprob", "compression_ratio", "no_speech_prob"]
    metrics: dict[str, float] = {}
    for name in metric_names:
        values = [
            float(segment.metadata[name])
            for segment in segments
            if isinstance(segment.metadata.get(name), (int, float))
        ]
        if values:
            metrics[name] = round(sum(values) / len(values), 4)
    if segments:
        confidence_values = [
            float(segment.text_confidence)
            for segment in segments
            if segment.text_confidence is not None
        ]
        if confidence_values:
            metrics["mean_text_confidence"] = round(
                sum(confidence_values) / len(confidence_values),
                4,
            )
    return metrics


def _provider_quality_score(metrics: dict) -> float:
    scores: list[float] = []
    avg_logprob = metrics.get("avg_logprob")
    if isinstance(avg_logprob, (int, float)):
        scores.append(max(0.0, min(1.0, 2.718281828 ** float(avg_logprob))))
    compression_ratio = metrics.get("compression_ratio")
    if isinstance(compression_ratio, (int, float)):
        scores.append(max(0.0, min(1.0, 1.0 - max(0.0, float(compression_ratio) - 1.8) / 2.0)))
    no_speech_prob = metrics.get("no_speech_prob")
    if isinstance(no_speech_prob, (int, float)):
        scores.append(max(0.0, min(1.0, 1.0 - float(no_speech_prob))))
    mean_text_confidence = metrics.get("mean_text_confidence")
    if isinstance(mean_text_confidence, (int, float)):
        scores.append(max(0.0, min(1.0, float(mean_text_confidence))))
    if not scores:
        return 0.75
    return sum(scores) / len(scores)


def _interpass_agreement(candidate_text: str, peer_texts: list[str]) -> float:
    if not peer_texts:
        return 0.75
    candidate_tokens = _ordered_tokens(candidate_text)
    if not candidate_tokens:
        return 0.0
    ratios = []
    for peer_text in peer_texts:
        peer_tokens = _ordered_tokens(peer_text)
        if not peer_tokens:
            ratios.append(0.0)
            continue
        ratios.append(SequenceMatcher(None, candidate_tokens, peer_tokens).ratio())
    return sum(ratios) / len(ratios)


def _scoring_provider_metrics(metrics: dict) -> dict:
    return {
        key: round(float(value), 4)
        for key, value in metrics.items()
        if key in {"avg_logprob", "compression_ratio", "no_speech_prob", "mean_text_confidence"}
        and isinstance(value, (int, float))
    }


def _tokens(text: str) -> set[str]:
    return set(_ordered_tokens(text))


def _ordered_tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:'[a-z0-9]+)?", text.casefold())


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)
