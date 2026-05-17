"""Pass E — deductive speaker-identity resolver (v0.2.13).

The v0.2.12 name-detector surfaces *candidate* names with weak speaker
bindings ("Becky might be Speaker 3"). On a real business meeting it
caught 4 of 9 named participants. The rest require multi-step
reasoning across the transcript.

This pass does that reasoning using a small constraint-satisfaction
solver. It's deterministic (no LLM) and runs in milliseconds on a
hundred-segment meeting.

## The rules

1. **Self-reference exclusion** (HARD).
   If Speaker X says name Y inside their own utterance, Speaker X is
   NOT Y. People rarely talk about themselves in the third person.
2. **Vocative → next speaker** (SOFT, +3 weight).
   "Y, I think..." said by X, then Y speaks → NextSpeaker ≈ Y.
3. **Vocative-thank → previous speaker** (SOFT, +3).
   "Y, thanks for joining" → Y is the speaker who just finished.
4. **Welcome → in-meeting** (SOFT, +4).
   "Welcome Y" / "Y, glad you joined" → Y is in this meeting AND
   typically the next speaker.
5. **3rd-person reference → out-of-meeting** (HARD).
   "Y is charged with..." / "she'll lead the analysis" / "Y leads
   the team" → Y is referenced, not present.
6. **Future-tense → out-of-meeting** (HARD).
   "I'll talk to Y tomorrow" / "We're meeting Y later" → Y is not
   in this meeting now.
7. **Past in-meeting → in-meeting** (SOFT, +1 to all candidate
   speakers, doesn't bind to one).
   "Y said earlier..." → Y is in the meeting somewhere.

After scoring all (speaker, name) pairs, a greedy assignment locks in
the highest-confidence pairings first, removing assigned speakers and
names from further consideration. Ties below the auto-apply silent
threshold surface as manual review.

## Auto-apply integration

Identity assignments use the same `tier_for_confidence` classifier as
Passes C and D. Silent-tier assignments call `approve_speaker_label`
to update `speaker_assignments` + create `people` rows. The user sees
the speakers named-correctly on their first dashboard load with no
clicks required.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field

from app.config import AppConfig
from app.db.database import connect
from app.services.repair import tier_for_confidence
from app.services.speaker_identity import _STOP_NAMES, _valid_name

_LOG = logging.getLogger(__name__)

# Trigger words that classify a mention's relationship to the speaker.
# Order matters: more specific patterns first.

# "She/He/They will/is/leads ..." within 50 chars of a name → 3rd-person.
_THIRD_PERSON_PATTERN = re.compile(
    r"\b(?:she|he|they)(?:'ll|'s|\s+(?:will|is|was|leads|runs|owns|"
    r"manages|directs|chairs|heads|founded|joined))\b",
    re.IGNORECASE,
)
# "X is charged with..." / "X leads..." / "X will run..." — name in the
# subject position of a role-verb construction.
_NAME_AS_REFERENCED_ROLE = re.compile(
    r"\b(?:is\s+(?:charged|the\s+(?:lead|head|director|VP|SVP|EVP|CEO|"
    r"CTO|CFO|founder|owner))|leads\s+the|runs\s+the|owns\s+the|"
    r"manages\s+the|chairs\s+the|will\s+(?:lead|head|run|join|present))\b",
    re.IGNORECASE,
)
# "I'll talk to X tomorrow" / "We're meeting X later" / "X next week".
_FUTURE_REFERENCE_PATTERN = re.compile(
    r"\b(?:tomorrow|later|next\s+(?:week|month|meeting|time)|"
    r"after\s+(?:this|the\s+call|tomorrow)|in\s+the\s+(?:next|coming))\b",
    re.IGNORECASE,
)
# "Welcome X" / "X just joined" / "glad you could join, X".
_WELCOME_PATTERN = re.compile(
    r"\b(?:welcome|just\s+joined|glad\s+(?:you'?re\s+here|to\s+have\s+you)|"
    r"thanks\s+for\s+joining)\b",
    re.IGNORECASE,
)
# "X said earlier" / "X mentioned" / "X brought up" — past in-meeting.
_PAST_IN_MEETING_PATTERN = re.compile(
    r"\b(?:said\s+earlier|mentioned\s+(?:earlier|before)|"
    r"brought\s+up\s+(?:earlier|before)|just\s+said|noted\s+that)\b",
    re.IGNORECASE,
)
# v0.2.14: phrases that specifically mean "X JUST JOINED the meeting",
# distinguishing from a normal "thank you for that" vocative. When this
# fires, the addressee is a NEW speaker whose first appearance comes
# AFTER this welcome event — not the previous speaker.
_JOIN_EVENT_PATTERN = re.compile(
    r"\b(?:thanks?\s+for\s+joining|glad\s+(?:you're\s+here|you\s+made\s+it|"
    r"you\s+could\s+join|you\s+joined)|welcome\s+(?:to\s+the|aboard)|"
    r"you\s+just\s+joined|nice\s+to\s+have\s+you|good\s+to\s+have\s+you)\b",
    re.IGNORECASE,
)
# Vocative + question/imperative shape. Same family as the v0.2.12
# direct-address pattern.
_VOCATIVE_TRIGGER_WORDS = {
    "i",
    "you",
    "are",
    "do",
    "did",
    "can",
    "could",
    "would",
    "will",
    "how",
    "when",
    "what",
    "tell",
    "let",
    "please",
    "that",
    "where",
    "why",
    "which",
    "who",
    "here",
    "there",
}


@dataclass(frozen=True)
class NameMention:
    """A single name occurrence in the transcript with its context."""

    name: str  # lowercased
    display_name: str  # title-cased for user-facing surfaces
    mentioner_speaker_id: str
    segment_id: int
    segment_index: int
    start_ms: int
    kind: str
    context: str  # ~40 char window around the name


@dataclass
class IdentityAssignment:
    """A proposed (speaker_id → name) binding with evidence + score."""

    speaker_id: str
    name: str  # display-cased
    confidence: float
    score: float
    evidence: list[str] = field(default_factory=list)
    ruled_out_names: list[str] = field(default_factory=list)


def resolve_identities(
    config: AppConfig, meeting_id: int
) -> list[IdentityAssignment]:
    """Run the full deductive resolver on a meeting. Read-only —
    `persist_identity_assignments` is the wrapper that writes results.

    v0.2.13: uses the v0.2.12 speaker_name_candidates as a positive
    name filter. The token-level regex in `_gather_mentions` is too
    permissive on its own (catches geos like 'Oklahoma' and
    contractions like 'isn't'); the v0.2.12 detector applies the
    proper-noun-aware patterns and produces a clean shortlist.
    """
    rows = _load_segments(config, meeting_id)
    if not rows:
        return []
    # Two-source mention pool:
    #   1. v0.2.12 candidates — each NameEvidence already carries a
    #      speaker binding and evidence type. Replay them as
    #      pre-classified mentions; the embedded-vocative patterns
    #      catch shapes the resolver's own classifier doesn't ("you
    #      know those becky i" can't be parsed by our after-name
    #      trigger lookup).
    #   2. Common-first-name scan — picks up names that the v0.2.12
    #      patterns miss because the ASR drops vocative commas ("alex
    #      i don't know" with no comma).
    from app.services.speaker_identity import _speaker_name_candidates

    candidate_groups = _speaker_name_candidates(config, meeting_id)
    # v0.2.12 evidence carries the correct speaker binding in
    # `ev.speaker_id`. Score them directly into the constraint graph
    # so we preserve the v0.2.12 logic (which handles next-speaker
    # gap windows, panelist boosts, etc.) rather than re-deriving the
    # binding from the segment alone. Replay mentions still flow into
    # `mentions` for exclusion detection and self-reference checks.
    replay_mentions: list[NameMention] = []
    bound_scores: dict[tuple[str, str], float] = defaultdict(float)
    bound_evidence: dict[tuple[str, str], list[str]] = defaultdict(list)
    seg_index_by_id = {int(r["id"]): i for i, r in enumerate(rows)}
    # Weight scale by anchor strength:
    #   self_introduction (strongest — explicit "I am X") = 5
    #   vocative_thank (explicit "X, thanks" — clear addressee) = 4
    #   response_after_direct_address_panelist (named in host-intro
    #     pool + direct address) = 4
    #   response_after_direct_address (basic vocative) = 3
    kind_map = {
        "self_introduction": ("self_introduction", 5.0),
        "response_after_direct_address": ("vocative_address", 3.0),
        "response_after_direct_address_panelist": ("vocative_address", 4.0),
        "vocative_thank": ("vocative_thank", 4.0),
    }
    for group in candidate_groups:
        for ev in group:
            seg_idx = seg_index_by_id.get(ev.segment_id)
            if seg_idx is None:
                continue
            seg_row = rows[seg_idx]
            mention_kind, weight = kind_map.get(
                ev.evidence_type, ("bare_mention", 0.0)
            )
            replay_mentions.append(
                NameMention(
                    name=ev.name,
                    display_name=ev.name.title(),
                    mentioner_speaker_id=str(seg_row["diarization_speaker_id"]),
                    segment_id=ev.segment_id,
                    segment_index=seg_idx,
                    start_ms=int(seg_row["start_ms"]),
                    kind=mention_kind,
                    context=ev.phrase,
                )
            )
            # Use ev.speaker_id (already speaker-bound by v0.2.12) for
            # score accumulation. The scan path below adds additional
            # signal from 3rd-person/future-tense markers.
            if weight > 0 and ev.speaker_id:
                bound_scores[(ev.speaker_id, ev.name)] += weight
                bound_evidence[(ev.speaker_id, ev.name)].append(
                    f"{ev.evidence_type} at seg {ev.segment_id}"
                )
    v0212_names = {ev.name for group in candidate_groups for ev in group}
    # Scan the full transcript for ALL known names — both v0.2.12's
    # output and common first names. The scan catches 3rd-person /
    # future-tense markers that the v0.2.12 replay path doesn't
    # classify (Arthi mentioned as "she'll lead" → should exclude).
    # Dedupe at the scoring stage to avoid double-counting the same
    # (segment, name) pair.
    known_names = set(v0212_names)
    transcript_text = " ".join(str(r["text"]) for r in rows).casefold()
    for name in _COMMON_FIRST_NAMES:
        if re.search(rf"\b{re.escape(name)}\b", transcript_text):
            known_names.add(name)
    if not known_names:
        return []
    scan_mentions = _gather_mentions(rows, known_names=known_names)
    # Dedupe replay vs scan by (segment_id, name, mentioner, kind) —
    # a replayed v0.2.12 mention with vocative_address at seg N
    # should not also count as a separate vocative_address scan hit
    # at the same seg.
    seen: set[tuple] = set()
    mentions: list[NameMention] = []
    for m in replay_mentions + scan_mentions:
        key = (m.segment_id, m.name, m.mentioner_speaker_id, m.kind)
        if key in seen:
            continue
        seen.add(key)
        mentions.append(m)
    if not mentions:
        return []
    not_in_meeting = _detect_out_of_meeting(mentions)
    # Seed scores with v0.2.12 bound evidence (preserves their
    # carefully-derived speaker bindings).
    scores: dict[tuple[str, str], float] = defaultdict(float)
    evidence_log: dict[tuple[str, str], list[str]] = defaultdict(list)
    for key, weight in bound_scores.items():
        if key[1] in not_in_meeting:
            continue
        scores[key] += weight
        evidence_log[key].extend(bound_evidence[key])

    # Track which (segment, name, kind) tuples already scored via the
    # replay path so we don't double-count them in the scan loop below.
    replay_keys = {
        (m.segment_id, m.name, m.kind) for m in replay_mentions
    }
    for m in mentions:
        if m.name in not_in_meeting:
            continue
        # Hard exclusion: self-reference. Always applies regardless of
        # whether the mention came from replay or scan.
        scores[(m.mentioner_speaker_id, m.name)] -= 100.0
        # Skip soft scoring for replay mentions — they were already
        # weighted via `bound_scores` using v0.2.12's bindings.
        if (m.segment_id, m.name, m.kind) in replay_keys:
            continue

        if m.kind == "vocative_address":
            next_speaker = _next_different_speaker(rows, m.segment_index)
            if next_speaker and next_speaker != m.mentioner_speaker_id:
                scores[(next_speaker, m.name)] += 3.0
                evidence_log[(next_speaker, m.name)].append(
                    f"addressed by {m.mentioner_speaker_id} at seg {m.segment_id}"
                )
        elif m.kind == "vocative_thank":
            prev_speaker = _previous_different_speaker(rows, m.segment_index)
            if prev_speaker and prev_speaker != m.mentioner_speaker_id:
                scores[(prev_speaker, m.name)] += 3.0
                evidence_log[(prev_speaker, m.name)].append(
                    f"thanked by {m.mentioner_speaker_id} at seg {m.segment_id}"
                )
        elif m.kind == "join_event":
            # v0.2.14: "thanks for joining" binds to a NEW speaker —
            # one whose first segment appears at or after this welcome
            # event. The previous-speaker rule misfires here because
            # the joiner hasn't said anything yet at this point.
            new_speaker = _first_new_speaker_after(rows, m.segment_index)
            if new_speaker and new_speaker != m.mentioner_speaker_id:
                # Stronger weight than plain vocative_thank because
                # join events are very specific anchors.
                scores[(new_speaker, m.name)] += 5.0
                evidence_log[(new_speaker, m.name)].append(
                    f"joined-and-welcomed by {m.mentioner_speaker_id} at seg {m.segment_id}"
                )
        elif m.kind == "welcome":
            next_speaker = _next_different_speaker(rows, m.segment_index)
            if next_speaker and next_speaker != m.mentioner_speaker_id:
                scores[(next_speaker, m.name)] += 4.0
                evidence_log[(next_speaker, m.name)].append(
                    f"welcomed by {m.mentioner_speaker_id} at seg {m.segment_id}"
                )
        elif m.kind == "past_in_meeting":
            # Doesn't bind to a specific speaker — just confirms the
            # name belongs to someone in the meeting. +1 weight goes
            # to ALL speakers we haven't excluded for this name.
            speaker_ids = {str(r["diarization_speaker_id"]) for r in rows}
            for sid in speaker_ids:
                if sid != m.mentioner_speaker_id:
                    scores[(sid, m.name)] += 1.0

    # Stage B (v0.2.16): voice-profile matches injected as resolver
    # evidence. `persist_voice_profile_match_candidates` ran earlier
    # in the pipeline and wrote `speaker_profile_match` review_items
    # for any diarized speaker whose voice embedding clears the
    # similarity threshold against a known profile. Until now the
    # resolver ignored those entirely. Wire them as evidence here:
    # high-confidence voice match = strong binding signal, with an
    # extra weight bump when the matched name is the dashboard owner
    # (the user uploading the meeting is almost always one of the
    # speakers).
    # Stage C (v0.2.16): LLM identity resolver as an additional
    # evidence source. The synthesizer ran earlier in the pipeline
    # and cached its output as `llm_speaker_identities`; we just read
    # the cache here. Each LLM assignment with confidence >= 0.5
    # contributes score 4.0 * llm_confidence — so a strong LLM match
    # (confidence 0.9) adds weight 3.6, putting it between
    # vocative_thank (3.0) and welcome (4.0). Combined with a regex
    # signal it can push to silent tier; alone it's toast (Stage A's
    # evidence_count gate enforces this).
    from app.services.repair.llm_identity import load_llm_speaker_identities

    for lid in load_llm_speaker_identities(config, meeting_id):
        name = lid["name"].casefold()
        if name in not_in_meeting:
            continue
        confidence = float(lid["confidence"])
        if confidence < 0.5:
            continue
        speaker_id = lid["speaker_id"]
        weight = 4.0 * confidence
        scores[(speaker_id, name)] += weight
        evidence_log[(speaker_id, name)].append(
            f"llm_resolver conf={confidence:.2f}"
        )

    for vm in _load_voice_profile_matches(config, meeting_id):
        # Casefold the candidate so this signal stacks with the regex
        # and LLM evidence on the same (speaker_id, name) key. Voice
        # match payloads are written title-cased (display form), so
        # without casefolding they'd accumulate under a separate score
        # key and never combine with other signals. Pre-Stage-C bug
        # exposed by Stage C casefolding the LLM block.
        name = vm["candidate_name"].casefold()
        if name in not_in_meeting:
            continue
        speaker_id = vm["speaker_id"]
        cosine = float(vm["confidence"])
        # Owner match: first-token equality against owner display_name
        # OR any configured alias. "Wolf" vs "Wolfgang" requires the
        # user to add "Wolfgang" to `OwnerConfig.aliases`; prefix
        # containment can't bridge them since they only share "nat"
        # (3 chars, too loose to be safe as an automatic rule).
        is_owner = _owner_matches(
            vm["candidate_name"],
            config.owner.display_name,
            owner_aliases=config.owner.aliases,
        )
        # Bucket the weight by cosine strength. Owner gets +1 because
        # they're a high prior (their voice profile exists from an
        # earlier confirmed meeting, and they're uploading this one).
        if cosine >= 0.85:
            weight = 6.0 if is_owner else 5.0
        elif cosine >= 0.70:
            weight = 4.0 if is_owner else 3.0
        elif cosine >= 0.55:
            weight = 2.0 if is_owner else 1.0
        else:
            continue
        scores[(speaker_id, name)] += weight
        suffix = " (owner)" if is_owner else ""
        evidence_log[(speaker_id, name)].append(
            f"voice_match cosine={cosine:.2f}{suffix}"
        )

    assignments = _greedy_assign(scores, evidence_log)
    return assignments


def _owner_matches(
    candidate_name: str,
    owner_display_name: str | None,
    owner_aliases: list[str] | None = None,
) -> bool:
    """Case-insensitive first-token match against the owner's display
    name and configured aliases.

    The voice-profile `display_name` was set at first speaker
    confirmation and may be a fuller form (e.g. "Wolfgang") than the
    owner's configured display_name (e.g. "Wolf"). "Wolf" and
    "Wolfgang" share no exact prefix substring beyond "nat", so the
    only reliable bridge is the explicit `OwnerConfig.aliases` list.
    Users with nickname-style mismatches set their aliases once and
    every future meeting picks up the owner bump.

    Exact first-token comparison case-folded — "Wolf Mozart" in the
    profile matches "Wolf" in the config, but "Alex" doesn't match
    "Alan" (Stage B regression risk identified by audit).
    """
    candidate_first = _first_token(candidate_name)
    if not candidate_first:
        return False
    for candidate_pool in (owner_display_name, *(owner_aliases or [])):
        pool_first = _first_token(candidate_pool or "")
        if pool_first and pool_first == candidate_first:
            return True
    return False


def _first_token(name: str | None) -> str:
    """Lowercase first whitespace-separated token, or empty string."""
    if not name:
        return ""
    stripped = name.strip()
    if not stripped:
        return ""
    return stripped.split()[0].casefold()


def _load_voice_profile_matches(config: AppConfig, meeting_id: int) -> list[dict]:
    """Pull the `speaker_profile_match` review_items written by
    `persist_voice_profile_match_candidates` earlier in the pipeline.
    Returns a list of dicts with `speaker_id`, `candidate_name`,
    `confidence` (cosine in [0, 1]).

    Deduplicates by `(speaker_id, candidate_name)`, keeping the
    highest-cosine entry. Without this, a duplicate-write from a
    retry path would stack two `voice_match` evidence entries onto
    the same (speaker, name) and accidentally satisfy the Stage A
    `evidence_count >= 2` silent-tier gate without real corroboration.
    """
    out_by_key: dict[tuple[str, str], dict] = {}
    with connect(config.paths.database_path) as conn:
        rows = conn.execute(
            """
            SELECT id, payload_json, confidence FROM review_items
            WHERE meeting_id = ? AND kind = 'speaker_profile_match'
            """,
            (meeting_id,),
        ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            # `id` is in the SELECT, so we don't need defensive
            # membership check (sqlite3.Row's `in` checks values, not
            # keys — so `.keys()` would be required anyway).
            _LOG.warning(
                "voice_match: skipping malformed review_items row id=%s",
                row["id"],
            )
            continue
        speaker = payload.get("speaker_id")
        name = (payload.get("candidate_name") or "").strip()
        if not speaker or not name:
            continue
        cosine = float(payload.get("confidence", row["confidence"] or 0.0))
        key = (str(speaker), name)
        existing = out_by_key.get(key)
        if existing is None or cosine > existing["confidence"]:
            out_by_key[key] = {
                "speaker_id": str(speaker),
                "candidate_name": name,
                "confidence": cosine,
            }
    return list(out_by_key.values())


def persist_identity_assignments(
    config: AppConfig, meeting_id: int
) -> dict:
    """Run the resolver, classify by confidence tier, and either apply
    inline (silent / toast) or surface as `review_items` for manual
    review.

    Returns dict with `total`, `auto_applied`, `manual` counts.
    """
    if not getattr(config.repair, "identity_resolver_enabled", True):
        return {"total": 0, "auto_applied": 0, "manual": 0}
    auto_enabled = bool(getattr(config.repair, "auto_apply_enabled", True))
    # Fallbacks match the current RepairConfig defaults (v0.2.16) — if
    # `config.repair` lacks these attributes via a partial deserialization
    # or test-only stub, the safe default is the post-Stage-A value, not
    # the old pre-Stage-A 0.90/0.70 floors that silent-auto-applied the
    # "Speaker 1 → Two" bug.
    silent_thr = float(getattr(config.repair, "auto_apply_silent_threshold", 0.95))
    toast_thr = float(getattr(config.repair, "auto_apply_toast_threshold", 0.85))

    assignments = resolve_identities(config, meeting_id)
    auto_applied = 0
    manual = 0
    # Insert review_items in one transaction, collect (id, tier,
    # speaker, name) for later apply. Closing the outer connect()
    # releases the SQLite write lock so approve_speaker_label can open
    # its own connection without deadlocking.
    inserted: list[tuple[int, str, str, str]] = []
    with connect(config.paths.database_path) as conn:
        conn.execute(
            """
            DELETE FROM review_items
            WHERE meeting_id = ? AND kind = 'identity_assignment'
              AND status = 'open'
            """,
            (meeting_id,),
        )
        for a in assignments:
            # An identity assignment whose ONLY evidence is the
            # `past_in_meeting` sentinel ("name was mentioned in this
            # meeting") is not a real binding — that sentinel adds
            # +1.0 to every speaker simultaneously, so it can't
            # discriminate between them. Demote to manual review so
            # the user sees the suggestion but it doesn't silently
            # mis-attach to whichever speaker happened to score
            # highest by tie-breaker order.
            evidence_count = _meaningful_evidence_count(a.evidence)
            if evidence_count == 0:
                tier = "manual"
            else:
                tier = tier_for_confidence(
                    a.confidence,
                    auto_enabled,
                    silent_thr,
                    toast_thr,
                    evidence_count=evidence_count,
                )
            payload = {
                "speaker_id": a.speaker_id,
                "candidate_name": a.name,
                "score": a.score,
                "evidence": a.evidence,
                "tier": tier,
                "source": "identity_resolver",
            }
            cursor = conn.execute(
                """
                INSERT INTO review_items
                  (meeting_id, kind, title, payload_json, status,
                   confidence, source_segment_ids)
                VALUES (?, 'identity_assignment', ?, ?, 'open', ?, ?)
                """,
                (
                    meeting_id,
                    f"Speaker identity: {a.speaker_id} → {a.name}",
                    json.dumps(payload),
                    a.confidence,
                    "[]",
                ),
            )
            inserted.append(
                (int(cursor.lastrowid), tier, a.speaker_id, a.name)
            )

    # Now apply outside the original connect() so we don't deadlock.
    for review_item_id, tier, speaker_id, name in inserted:
        if tier not in ("silent", "toast"):
            manual += 1
            continue
        try:
            from app.services.review import approve_speaker_label

            approve_speaker_label(config, meeting_id, speaker_id, name)
            with connect(config.paths.database_path) as conn:
                conn.execute(
                    "UPDATE review_items SET status = 'auto_applied', "
                    "resolved_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (review_item_id,),
                )
            auto_applied += 1
        except Exception as exc:  # noqa: BLE001 — best-effort
            _LOG.warning(
                "auto-apply of identity assignment %s failed: %s",
                review_item_id,
                exc,
            )
            manual += 1
    return {
        "total": len(assignments),
        "auto_applied": auto_applied,
        "manual": manual,
    }


# ── Internals ───────────────────────────────────────────────────────────


def _load_segments(config: AppConfig, meeting_id: int) -> list[dict]:
    with connect(config.paths.database_path) as conn:
        rows = conn.execute(
            """
            SELECT id, start_ms, end_ms, text, diarization_speaker_id
            FROM transcript_segments
            WHERE meeting_id = ?
            ORDER BY start_ms
            """,
            (meeting_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# Stop-name set extended with non-name discourse markers a vocative
# scan would otherwise treat as candidates. Reuses the global set
# from speaker_identity so adjustments compound across modules.
_NAME_TOKEN_RE = re.compile(r"\b([A-Za-z][A-Za-z'-]{2,24})\b")


def _gather_mentions(
    rows: list[dict], known_names: set[str] | None = None
) -> list[NameMention]:
    """Walk segments and collect every plausible name occurrence with
    its classification.

    If `known_names` is supplied, ONLY tokens in that set are kept
    (case-insensitive match on the lowercased name). This is the
    v0.2.13 positive-filter mode — the v0.2.12 detector's output
    bootstraps the candidate pool so the resolver doesn't fan out
    to every capitalized word in the transcript.
    """
    mentions: list[NameMention] = []
    for idx, row in enumerate(rows):
        text = str(row["text"])
        speaker_id = str(row["diarization_speaker_id"])
        for m in _NAME_TOKEN_RE.finditer(text):
            token = m.group(1)
            cased = token.casefold()
            if known_names is not None and cased not in known_names:
                continue
            if not _valid_name(cased):
                continue
            # Skip if the token is the speaker label itself, e.g. don't
            # treat "Speaker" or "Speakers" as a name.
            if cased in {"speaker", "speakers"} or cased in _STOP_NAMES:
                continue
            window_start = max(0, m.start() - 40)
            window_end = min(len(text), m.end() + 40)
            window = text[window_start:window_end]
            kind = _classify_mention(text, m.start(), m.end(), window)
            if kind is None:
                continue
            mentions.append(
                NameMention(
                    name=cased,
                    display_name=token.title(),
                    mentioner_speaker_id=speaker_id,
                    segment_id=int(row["id"]),
                    segment_index=idx,
                    start_ms=int(row["start_ms"]),
                    kind=kind,
                    context=window,
                )
            )
    return mentions


def _classify_mention(
    text: str, start: int, end: int, window: str
) -> str | None:
    """Return one of the mention-kind strings or None to skip.

    Order:
      1. HARD exclusions first (3rd-person + future-tense) — these
         markers strongly suggest the name is referenced rather than
         present in the meeting, and `_detect_out_of_meeting` later
         uses them to drop the name from the assignment graph.
      2. Position-sensitive binding signals (vocative_thank /
         vocative_address / welcome) — these contribute to in-meeting
         evidence only if no hard exclusion fires.
      3. Past-in-meeting and bare mention as fallbacks.
    """
    # ── HARD exclusions ──
    # "I'll see X tomorrow", "X next week" — future-tense reference.
    if _FUTURE_REFERENCE_PATTERN.search(window):
        return "future_reference"
    # "She/he/they..." within 40 chars of the name.
    if _THIRD_PERSON_PATTERN.search(window):
        return "third_person_reference"
    # "X is charged with..." / "X leads the team" — name in subject
    # position of a role-verb construction.
    if _NAME_AS_REFERENCED_ROLE.search(text[start : end + 60]):
        return "third_person_reference"
    # ── Binding classification ──
    # Welcome FIRST: trigger comes BEFORE the name ("Welcome, X" / "X
    # just joined"). Welcome has its own positional cue (a preceding
    # welcome verb), so it doesn't need the vocative-comma anchor that
    # the address/thank patterns require.
    welcome_match = _WELCOME_PATTERN.search(window)
    if welcome_match:
        window_start = max(0, start - 40)
        welcome_abs = window_start + welcome_match.start()
        if welcome_abs < start:
            return "welcome"
    # Vocative-thank: "X, thanks" — name first, trigger after.
    after_name = text[end : end + 40].lstrip(",. ").lower()
    # v0.2.14: distinguish "thanks for joining" / "glad you joined"
    # from a generic thank — the former is a JOIN EVENT and the
    # addressee is a NEW speaker, not the previous one.
    if _JOIN_EVENT_PATTERN.search(text[max(0, start - 20) : end + 50]):
        return "join_event"
    if re.match(
        r"(?:thanks|thank you|that was|appreciate|good (?:point|question|answer))",
        after_name,
    ):
        return "vocative_thank"
    # Pre-check: vocative_address requires the name to be at a
    # vocative-style anchor (start, after punctuation, or after a
    # conversational interjection). Without this guard, ANY word
    # followed by "you/I/are/..." would classify as vocative_address
    # (e.g. "this area you know it's arizona").
    before_name = text[:start]
    has_vocative_anchor = bool(
        re.search(
            r"(?:^|[.!?]\s+|[.!?]$|,\s+|"
            r"\b(?:hey|oh|listen|well|and|but|so)[,]?\s+)$",
            before_name,
        )
    )
    if has_vocative_anchor:
        # Vocative-address: name followed by a question/imperative
        # trigger word. Binds to the NEXT speaker.
        after_token = text[end : end + 20]
        after_match = re.match(r"[,.\s]+(\w+)", after_token)
        if (
            after_match
            and after_match.group(1).casefold() in _VOCATIVE_TRIGGER_WORDS
        ):
            return "vocative_address"
    # Past-tense in-meeting reference.
    if _PAST_IN_MEETING_PATTERN.search(window):
        return "past_in_meeting"
    # Bare mention — keep for accounting; not used as binding signal.
    return "bare_mention"


def _detect_out_of_meeting(mentions: list[NameMention]) -> set[str]:
    """Names that have STRONG out-of-meeting evidence are excluded.

    A name is excluded when the number of 3rd-person / future-tense
    references is >= the number of in-meeting binding signals. Tied
    counts also exclude — when there's ambiguity, prefer "Y is
    referenced" over "Y is present" because the cost of a wrong
    binding (mis-naming a speaker) is higher than the cost of leaving
    a speaker un-named.

    Welcome / vocative_thank with a clear next-or-prev-speaker
    response are strong enough on their own to keep the name in-
    meeting even if a 3rd-person reference exists elsewhere.
    """
    out_count: dict[str, int] = defaultdict(int)
    in_count: dict[str, int] = defaultdict(int)
    strong_in: set[str] = set()
    for m in mentions:
        if m.kind in ("third_person_reference", "future_reference"):
            out_count[m.name] += 1
        elif m.kind in ("welcome", "join_event"):
            in_count[m.name] += 1
            strong_in.add(m.name)  # welcome / join is the strongest in-meeting signal
        elif m.kind == "vocative_thank":
            in_count[m.name] += 1
            strong_in.add(m.name)
        elif m.kind in ("vocative_address", "past_in_meeting"):
            in_count[m.name] += 1
    excluded: set[str] = set()
    for name, oc in out_count.items():
        if name in strong_in:
            continue
        ic = in_count.get(name, 0)
        if oc >= ic:
            excluded.add(name)
    return excluded


def _next_different_speaker(
    rows: list[dict], index: int, max_gap_ms: int = 10_000
) -> str | None:
    current = rows[index]
    for candidate in rows[index + 1 : index + 4]:
        if candidate["diarization_speaker_id"] == current["diarization_speaker_id"]:
            continue
        if int(candidate["start_ms"]) - int(current["end_ms"]) <= max_gap_ms:
            return str(candidate["diarization_speaker_id"])
        return None
    return None


def _first_new_speaker_after(
    rows: list[dict], welcome_index: int, look_ahead: int = 6
) -> str | None:
    """Return the speaker_id of the first speaker whose FIRST APPEARANCE
    in the meeting is at or after `welcome_index`. That's the speaker
    being welcomed in a "thanks for joining" event — they hadn't
    spoken before.

    Scans up to `look_ahead` segments past the welcome to find the
    new voice. Returns None if every speaker active in the look-ahead
    window also spoke earlier (i.e., no new speaker actually joined).
    """
    speakers_before: set[str] = set()
    for r in rows[: welcome_index + 1]:
        speakers_before.add(str(r["diarization_speaker_id"]))
    for candidate in rows[welcome_index + 1 : welcome_index + 1 + look_ahead]:
        sid = str(candidate["diarization_speaker_id"])
        if sid not in speakers_before:
            return sid
    return None


def _previous_different_speaker(
    rows: list[dict], index: int, max_gap_ms: int = 10_000
) -> str | None:
    current = rows[index]
    start_idx = max(0, index - 3)
    for candidate in reversed(rows[start_idx:index]):
        if candidate["diarization_speaker_id"] == current["diarization_speaker_id"]:
            continue
        if int(current["start_ms"]) - int(candidate["end_ms"]) <= max_gap_ms:
            return str(candidate["diarization_speaker_id"])
        return None
    return None


# Curated common first names (English + frequent international).
# Used as a positive filter so the resolver doesn't fan out to every
# capitalized word in the transcript. Casefolded for matching.
# Source: top US Social Security baby name lists, top UK ONS lists,
# plus common diminutives. NOT an exhaustive list — names not here
# can still be caught via the v0.2.12 speaker_name_candidate path.
_COMMON_FIRST_NAMES: frozenset[str] = frozenset(
    {
        # A
        "aaron", "abby", "abigail", "adam", "adrian", "aidan", "alan", "albert",
        "alex", "alexa", "alexander", "alexandra", "alexis", "alice", "alicia",
        "alison", "alyssa", "amanda", "amber", "amelia", "amy", "andrea",
        "andrew", "andy", "angela", "anita", "ann", "anna", "anne", "annie",
        "anthony", "antonio", "april", "aranza", "ariana", "ariel", "arthur",
        "arti", "arthi", "ashley", "audrey", "ava",
        # B
        "barbara", "becca", "becky", "ben", "benjamin", "bernard", "beth",
        "bethany", "betsy", "betty", "beverly", "bill", "billy", "bob",
        "bobby", "bonnie", "brad", "bradley", "brandon", "brenda", "brendan",
        "brent", "brett", "brian", "brianna", "brittany", "bruce", "bryan",
        # C
        "caitlin", "caleb", "cameron", "carl", "carla", "carlos", "carmen",
        "carol", "carolina", "caroline", "carolyn", "carrie", "carter",
        "casey", "cassandra", "catherine", "cathy", "cecilia", "chad",
        "chandra", "charles", "charlie", "charlotte", "chelsea", "cheryl",
        "chris", "christian", "christina", "christine", "christopher",
        "chuck", "cindy", "claire", "clara", "clarence", "claudia", "clayton",
        "clifford", "clinton", "cody", "colby", "colin", "connor", "conrad",
        "courtney", "craig", "crystal", "curtis", "cynthia",
        # D
        "dale", "dan", "dana", "daniel", "danielle", "danny", "daphne",
        "darren", "darryl", "dave", "david", "dawn", "dean", "deanna",
        "debbie", "deborah", "debra", "denise", "dennis", "derek", "derrick",
        "diana", "diane", "diego", "dolores", "dominic", "don", "donald",
        "donna", "doris", "dorothy", "doug", "douglas", "drew", "dustin",
        "dwayne", "dylan",
        # E
        "earl", "eddie", "edgar", "edith", "edward", "elaine", "eleanor",
        "elena", "elijah", "eliza", "elizabeth", "ella", "ellen", "ellie",
        "elliot", "emily", "emma", "eric", "erica", "erika", "erin", "ernest",
        "ethan", "eugene", "eva", "evan", "evelyn",
        # F
        "faith", "fernando", "florence", "frances", "francis", "francisco",
        "frank", "fred", "frederick",
        # G
        "gabriel", "gabriella", "gail", "gary", "gavin", "geoffrey", "george",
        "georgia", "gerald", "gilbert", "glen", "glenn", "gloria", "gordon",
        "grace", "grant", "greg", "gregory", "gwen",
        # H
        "hailey", "hank", "hannah", "harold", "harriet", "harry", "harvey",
        "hazel", "heather", "heidi", "helen", "henry", "herbert", "holly",
        "howard", "hugh",
        # I
        "ian", "ida", "iris", "isaac", "isabel", "isabella", "ivan",
        # J
        "jack", "jackson", "jacob", "jade", "jake", "james", "jamie", "jan",
        "jane", "janet", "janice", "jared", "jason", "jasmine", "jay", "jean",
        "jeanette", "jeff", "jeffrey", "jen", "jenna", "jennifer", "jenny",
        "jeremy", "jerome", "jerry", "jesse", "jessica", "jill", "jim",
        "jimmy", "joan", "joann", "joanna", "joe", "joel", "john", "johnny",
        "jon", "jonathan", "jordan", "jose", "joseph", "josh", "joshua",
        "joy", "joyce", "juan", "judith", "judy", "julia", "julian", "julie",
        "justin",
        # K
        "kara", "karen", "karl", "kate", "katherine", "kathleen", "kathy",
        "katie", "katrina", "kayla", "keith", "kelly", "ken", "kendra",
        "kenneth", "kerry", "kevin", "kim", "kimberly", "kirk", "kris",
        "krista", "kristen", "kristin", "kristina", "kristine", "kurt",
        "kyle",
        # L
        "lance", "larry", "laura", "lauren", "laurence", "laurie", "leah",
        "lee", "leo", "leon", "leonard", "leroy", "leslie", "lewis", "liam",
        "linda", "lindsay", "lindsey", "lisa", "logan", "lois", "louis",
        "louise", "lucas", "lucia", "lucy", "luis", "luke", "lydia", "lynn",
        # M
        "mackenzie", "madeline", "madison", "manuel", "marc", "marcia",
        "marcus", "margaret", "maria", "marie", "marilyn", "mario", "mark",
        "marlene", "marshall", "martha", "martin", "marvin", "mary", "mason",
        "matt", "matthew", "maureen", "maxwell", "megan", "melanie", "melissa",
        "melvin", "meredith", "michael", "michele", "michelle", "miguel",
        "mike", "miranda", "miriam", "mitchell", "molly", "monica", "morgan",
        "morris",
        # N
        "nadine", "nancy", "naomi", "natalia", "natalie", "natasha", "nate",  # pii-ok
        "nathan", "neal", "neil", "nelson", "nicholas", "nicole", "noah",
        "nora", "norma", "norman",
        # O
        "olive", "oliver", "olivia", "oscar",
        # P
        "pam", "pamela", "pat", "patricia", "patrick", "paul", "paula",
        "pauline", "pedro", "peggy", "penelope", "peter", "philip", "phyllis",
        "preston",
        # Q
        "quincy", "quinn",
        # R
        "rachel", "ralph", "ramon", "randall", "randy", "raul", "ray",
        "raymond", "rebecca", "reginald", "rene", "renee", "rich", "richard",
        "rick", "ricky", "riley", "rita", "rob", "robert", "roberta",
        "roberto", "robin", "rod", "roger", "ron", "ronald", "ronnie",
        "rosa", "rose", "rosemary", "roy", "ruben", "russell", "ruth", "ryan",
        # S
        "sally", "sam", "samantha", "samuel", "sandra", "sara", "sarah",
        "sasha", "savannah", "scott", "sean", "sebastian", "sergio", "seth",
        "shane", "shanda", "shannon", "shari", "sharon", "shaun", "shawn",
        "sheila", "shelley", "sherri", "sherry", "sheryl", "shirley",
        "sidney", "simon", "sofia", "sonia", "sophia", "stacey", "stacy",
        "stanley", "stella", "stephanie", "stephen", "steve", "steven",
        "stuart", "sue", "susan", "suzanne", "sylvia",
        # T
        "tamara", "tammy", "tanya", "tara", "ted", "teresa", "terri",
        "terrence", "terry", "thelma", "theodore", "theresa", "thomas",
        "tiffany", "tim", "timothy", "tina", "todd", "tom", "tony", "tracey",
        "tracy", "travis", "trevor", "tristan", "troy", "tyler",
        # U
        # V
        "valerie", "vanessa", "veronica", "vicki", "vickie", "victor",
        "victoria", "vincent", "violet", "virginia", "vivian",
        # W
        "walter", "wanda", "warren", "wayne", "wendy", "wesley", "willie",
        "william", "wilson", "wolf", "wolfgang",
        # X
        "xander", "xavier",
        # Y
        "yolanda", "yvette", "yvonne",
        # Z
        "zachary", "zack", "zane", "zoe",
    }
)


def _meaningful_evidence_count(evidence: list) -> int:
    """How many evidence entries are real bindings.

    Right now the `past_in_meeting` mention.kind contributes +1.0 to
    every speaker's score but appends NOTHING to evidence_log — it's
    invisible at the consumer level. So a speaker whose score only
    came from past_in_meeting boosts has an empty evidence list,
    which signals "no real binding evidence" and should demote to
    manual review.

    Future kinds that we wire as score-only (no log entry) should
    follow the same pattern: any kind that can't discriminate between
    speakers must not surface in evidence_log.
    """
    return len(evidence)


def _greedy_assign(
    scores: dict[tuple[str, str], float],
    evidence_log: dict[tuple[str, str], list[str]],
) -> list[IdentityAssignment]:
    """Pick the highest-score (speaker, name) pair, lock both, repeat.
    O(N log N) for small N. Sufficient for ≤10 speakers × ≤20 names.

    Each assignment's user-facing confidence is `0.5 + score * 0.08`,
    capped at 0.95. A self-reference penalty drives the score negative
    so the speaker is never assigned to a name they mentioned.
    """
    sorted_pairs = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    used_speakers: set[str] = set()
    used_names: set[str] = set()
    assignments: list[IdentityAssignment] = []
    for (speaker, name), score in sorted_pairs:
        if score < 1.0:
            break
        if speaker in used_speakers or name in used_names:
            continue
        confidence = round(min(0.95, 0.5 + score * 0.08), 3)
        assignments.append(
            IdentityAssignment(
                speaker_id=speaker,
                name=name.title(),
                confidence=confidence,
                score=round(score, 2),
                evidence=evidence_log.get((speaker, name), []),
            )
        )
        used_speakers.add(speaker)
        used_names.add(name)
    return assignments
