// ── API DTOs ──────────────────────────────────────────────────────────────
// Mirrors the backend contract (FastAPI / pydantic schemas in
// `backend/app/api/routes.py` + the dataclasses in `backend/app/services/`).
// Extracted from main.tsx in v0.1.4 to make the App component file
// manageable. Keep this file dependency-free (pure types) so it can be
// imported anywhere without circular-dep risk.

export type VersionInfo = {
  current: string;
  latest: string | null;
  upgrade_available: boolean;
  release_notes: string;
  release_published_at: string | null;
  release_url: string | null;
  error: string | null;
  checked_at: number;
};

export type Meeting = {
  id: number;
  title: string;
  slug: string;
  status: string;
  duration_seconds: number;
  created_at: string;
  template?: string | null;
};

export type Segment = {
  id: number;
  start_ms: number;
  end_ms: number;
  text: string;
  diarization_speaker_id: string;
  confidence?: number | null;
  text_confidence?: number | null;
  speaker_confidence?: number | null;
};

export type SchedulerJob = {
  id: number;
  name: string;
  job_type: string;
  schedule: string;
  enabled: number;
  last_status?: string;
  model_policy_json: string;
};

export type ReviewItem = {
  id: number;
  kind: string;
  title: string;
  status: string;
  confidence?: number;
  source_segment_ids?: string;
  payload_json?: string;
};

export type SpeakerSuggestion = {
  item: ReviewItem;
  speakerId: string;
  candidateName: string;
  confidence: number;
  basis: string;
  evidence: string[];
  sourceSegmentIds: number[];
};

export type TranscriptCandidate = {
  id: number;
  meeting_id: number;
  segment_id: number;
  start_ms: number;
  end_ms: number;
  profile_name: string;
  provider: string;
  text: string;
  score: number;
  metrics_json: string;
  status: string;
};

export type AsrCandidateResult = {
  segment_id: number;
  profile_name: string;
  text: string;
  score: number;
};

export type SpeakerEvidence = {
  segment_id: number;
  speaker_id: string;
  confidence: number;
  metrics_json: string;
};

export type SynthesisSnapshot = {
  summary: string;
  key_terms: string[];
  workstreams: string[];
  decisions: string[];
  action_count: number;
  quality_count: number;
  speaker_confidence_count: number;
  words_available: number;
  next_steps: string[];
};

export type MeetingOverview = {
  id: number;
  title: string;
  slug: string;
  status: string;
  created_at: string;
  duration_seconds: number;
  speaker_status: string;
  source_file: string;
  summary: string;
  key_takeaways: string[];
  participants: string[];
  workstreams: string[];
  decisions: string[];
  actions: string[];
  open_questions: string[];
  obsidian_sections: Record<string, string>;
  tldr?: string;
  // Exactly 3 sentences for the Mind Map briefing: PURPOSE / SUBSTANCE
  // / LANDING. Empty array when the model couldn't produce a confident
  // briefing — UI falls back to tldr + first-summary-sentence.
  briefing?: string[];
  themes?: string[];
  stat_callouts?: Array<{
    value: string;
    label: string;
    source_segment_ids: number[];
  }>;
  tension_points?: Array<{
    title: string;
    positive_side: string;
    negative_side: string;
    source_segment_ids: number[];
  }>;
  chapter_markers?: Array<{
    label: string;
    start_segment_id: number;
    // Model-generated one-sentence skim. Optional because legacy chapter
    // markers predate this field and the model is allowed to leave it
    // empty when the chapter is too short to summarise meaningfully.
    summary?: string;
  }>;
  participant_contributions?: Array<{
    speaker: string;
    contribution: string;
    source_segment_ids: number[];
  }>;
  // Structured detail variants of decisions / actions / open_questions
  // that carry rationale, status, and due-date provenance. The flat
  // `decisions`/`actions`/`open_questions` string arrays above are kept
  // for legacy renderers and exports. Optional because old meetings
  // persisted before these fields existed will be missing them.
  decision_details?: Array<{
    decision: string;
    rationale: string | null;
    source_segment_ids: number[];
  }>;
  open_question_details?: Array<{
    question: string;
    status: 'unanswered' | 'partially_answered' | 'deferred';
    raised_by: string | null;
    addressed_to: string | null;
    source_segment_ids: number[];
  }>;
  // Plaud-style narrative recap synthesized from atoms after extraction.
  // Three optional sections — reframe + strategy + risk. Each section may
  // be absent on degenerate meetings (status updates, single-topic chats);
  // the renderer skips any section with null/empty header+body.
  executive_recap?: {
    reframe: {
      header: string | null;
      body: string | null;
    } | null;
    strategy: {
      header: string | null;
      body: string | null;
      bullets: Array<{
        owner: string;
        commitment: string;
        purpose: string | null;
      }>;
      trailer: string | null;
    } | null;
    risk: {
      header: string | null;
      body: string | null;
    } | null;
  } | null;
  action_details?: Array<{
    text: string;
    due_date: string | null;
    due_date_source: number | null;
    priority: string;
    start_ms: number | null;
    source_segment_ids: number[];
    owner_person_id?: number | null;
    owner_display_name?: string | null;
    // Near-duplicate paraphrases of this action collapsed into one row
    // at extraction time. Empty for standalones; populated on canonicals.
    cluster_members?: Array<{
      text: string;
      owner_person_id: number | null;
      due_date: string | null;
      source_segment_ids: number[];
    }>;
    // Prior due-date commitments superseded by the canonical's date.
    // Only present when cluster members carried conflicting non-null
    // dates — the latest mention wins, earlier dates land here so the
    // UI can show "Tue (was Fri earlier)".
    due_date_history?: Array<{ date: string; segment_id: number | null }>;
  }>;
  workstream_confidences?: Record<string, number>;
  workstream_descriptions?: Record<string, string>;
  // Deterministic team-level chips from meeting_health.py — no LLM call,
  // always present, every signal optional so the UI can skip chips that
  // have no value (e.g. action_clarity when there are no actions).
  meeting_health?: {
    participation_balance: 'balanced' | 'skewed' | 'dominated' | null;
    top_speaker_share: number | null;
    top_speaker_label: string | null;
    speaker_count_active: number;
    speaker_count_silent: number;
    decision_density: 'low' | 'moderate' | 'high' | null;
    decision_count: number;
    unresolved_question_count: number;
    action_clarity: 'low' | 'moderate' | 'high' | null;
    action_count: number;
  };
  // Specific moments that drove the meeting forward — chapter intros,
  // pivot questions, decision seedings, plus LLM-judged reframings,
  // challenges, and unsticks. Computed by conversation_drivers.py
  // (deterministic kinds) + llm_drivers.py (LLM-judged kinds). Capped
  // at 6 per meeting with per-kind quotas so the panel stays diverse,
  // then returned in transcript order.
  conversation_drivers?: Array<{
    kind:
      | 'topic_introduction'
      | 'pivot_question'
      | 'decision_moment'
      | 'reframing'
      | 'challenge'
      | 'unstick';
    segment_id: number;
    speaker_label: string;
    // When false, the speaker behind this segment hasn't been confirmed
    // — UI renders the moment without a name + a "needs speaker review"
    // treatment instead of confidently mis-attributing.
    speaker_confirmed: boolean;
    description: string;
    impact_seconds: number;
    confidence: 'high' | 'medium' | 'low';
    // "deterministic" or "llm". LLM-judged kinds get a small badge so
    // the user can tell which moments were heuristic vs. interpreted.
    source: 'deterministic' | 'llm';
  }>;
  // Center of Gravity: per-speaker ranking complementing talk-time.
  // standout_speaker_id is set only when one speaker's gravity rank is
  // meaningfully better than their talk-time rank — the "low-talk,
  // high-impact" case worth surfacing as a chip. When unset, no chip.
  center_of_gravity?: {
    rankings: Array<{
      speaker_id: string;
      speaker_label: string;
      speaker_confirmed: boolean;
      talk_time_pct: number;
      gravity_score: number;
      chapters_introduced: number;
      pivot_questions: number;
      decisions_seeded: number;
      other_seconds_after_turns: number;
    }>;
    standout_speaker_id: string | null;
    standout_label: string | null;
    standout_reason: string | null;
  };
  // Owner-aware annotations from build_meeting_overview
  your_actions?: string[];
  other_actions?: string[];
  your_action_count?: number;
  your_decisions?: string[];
  your_workstreams?: string[];
  you_in_attendance?: boolean;
  owner?: { configured: boolean; person_id: number | null; display_name: string | null };
};

// v0.2.9: surfaces v0.2.2 linguistic overlap detection results to the UI.
// One row per flagged segment; the TranscriptRow renders a badge when its
// id is in the set, with the evidence string on hover.
//
// v0.2.10 (audit NIT): closed union on `kind` so exhaustive ternaries
// surface a TS error when a new kind is added on the backend. An
// `OverlapKind | (string & {})` shape gives forward-compat at the
// runtime level while still preserving autocomplete in the editor.
export type OverlapKind = "yield_marker" | "stutter_interrupt" | "rapid_alternation";

export type OverlapHint = {
  segment_id: number;
  partner_segment_id: number | null;
  kind: OverlapKind | (string & {});
  evidence: string;
  confidence: number;
};

export type MeetingDetail = {
  meeting: Meeting;
  segments: Segment[];
  review_items: ReviewItem[];
  assignments: Array<{
    diarization_speaker_id: string;
    approved_label?: string;
    confirmed_by_user: number;
  }>;
  source_file: { retention_status: string } | null;
  candidates: TranscriptCandidate[];
  speaker_evidence: SpeakerEvidence[];
  // Optional for backward-compat with older API responses; current builds always include it
  overlap_hints?: OverlapHint[];
  synthesis: SynthesisSnapshot;
  overview: MeetingOverview;
  transcript_markdown: string;
};

export type HealthStatus = {
  status: string;
  inbox: string;
  vault: string;
  obsidian_available?: boolean;
  provider: string;
  dashboard_port: number;
  asr_vocabulary_path: string;
  asr_vocabulary_terms: number;
};

export type DashboardSettings = {
  config_path: string;
  dashboard: { port: number; url: string; restart_required_for_port: boolean };
  backend: { port: number; url: string; restart_required_for_port: boolean };
  models: {
    provider: "lm_studio" | "ollama";
    default_model: string;
    quality_model: string;
    lm_studio_base_url: string;
    ollama_base_url: string;
    idle_ttl_seconds: number;
    temperature: number;
    lm_studio_models: string[];
    ollama_models: string[];
    recommendations?: Array<{ id: string; tier: string; role: string; note: string }>;
    openrouter?: {
      base_url: string;
      api_key_env: string;
      api_key_set: boolean;
      default_model: string;
      quality_model: string;
    };
  };
  huggingface?: {
    token_set: boolean;
    model_access_urls: string[];
  };
  transcription: {
    auto_audio_repair: boolean;
    vocal_presentation_cue_scoring: boolean;
    vocal_presentation_cue_max_boost: number;
    asr_vocabulary_path: string;
    asr_vocabulary_terms: string[];
    asr_vocabulary_file_terms: number;
  };
  dashboard_prefs?: {
    show_key_term_highlights: boolean;
    show_transcript_confidence_chips: boolean;
    default_template?: string;
    auto_send_to_obsidian?: boolean;
  };
};

export type SettingsDraft = {
  dashboardPort: string;
  backendPort: string;
  modelProvider: "lm_studio" | "ollama" | "openrouter";
  defaultModel: string;
  qualityModel: string;
  lmStudioBaseUrl: string;
  ollamaBaseUrl: string;
  modelIdleTtlSeconds: string;
  modelTemperature: string;
  autoAudioRepair: boolean;
  vocalPresentationCueScoring: boolean;
  showKeyTermHighlights: boolean;
  showTranscriptConfidenceChips: boolean;
  defaultTemplate: string;
  autoSendToObsidian: boolean;
  vocabularyTerms: string;
};

export type InboxFile = {
  name: string;
  path: string;
  size_bytes: number;
  modified_at: number;
  supported: boolean;
};

export type SearchResult = {
  meeting_id: number;
  meeting_title: string;
  slug: string;
  segment_id?: number | null;
  review_item_id?: number | null;
  start_ms?: number | null;
  speaker: string;
  text: string;
  context_text: string;
  result_type: string;
  source_segment_ids: number[];
};

export type WorkstreamIntelligence = {
  display_name: string;
  meeting_count: number;
  mention_count: number;
  avg_confidence: number;
  meetings: Array<{
    meeting_id: number;
    meeting_title: string;
    slug: string;
    confidence?: number | null;
    source_segment_ids: string;
  }>;
};

export type ReviewTab = "transcript" | "summary" | "minutes" | "reflections";

// ── Reflections (experimental, owner-only) ────────────────────────────────
// Mirrors the Pydantic models in backend/app/services/reflections.py.
// The tab is hidden entirely when the backend returns 404 from
// /api/meetings/{id}/reflections (feature flag off). When the flag is
// on but the meeting can't produce Reflections (no owner, opted out,
// short transcript), the API returns a Reflections with skipped_reason
// set so the UI renders an honest empty state.
//
// 17 observation kinds; the literal type stays in lockstep with the
// backend enum because Pydantic validates the response shape coming
// out of the API.
export type ObservationKind =
  | 'talk_time'
  | 'interruption_pattern'
  | 'question_quality'
  | 'unanswered_question'
  | 'clarifying_question'
  | 'uncertainty_admission'
  | 'invited_input'
  | 'specific_invitation'
  | 'paraphrase_check'
  | 'build_on_other'
  | 'framing_quality'
  | 'loop_closure'
  | 'delegation_balance'
  | 'bluf_response'
  | 'decision_rationale'
  | 'commitment'
  | 'decision_driven';

export type Observation = {
  kind: ObservationKind;
  observation: string;
  // MUST be non-empty (backend drops observations with empty evidence
  // before persisting). UI uses this to render evidence pills and
  // click-to-jump-to-transcript affordances.
  evidence_segment_ids: number[];
  confidence: 'high' | 'medium' | 'low';
  why_this_matters: string | null;
  suggested_next_time: string | null;
};

export type OwnerStats = {
  talk_time_seconds: number;
  talk_time_pct: number;
  questions_asked: number;
  questions_open_ended: number;
  questions_unanswered: number;
  commitments_made: number;
  uncertainty_admissions: number;
  inputs_invited: number;
};

export type Reflections = {
  owner_display_name: string;
  stats: OwnerStats;
  observations: Observation[];
  // Set when the backend declined to attempt the LLM call (no owner,
  // short transcript, opted out, etc.). Drives the empty-state copy
  // so the UI tells the user *why* there's nothing to show rather
  // than fabricating a "well-balanced meeting" framing.
  skipped_reason:
    | 'no_owner_configured'
    | 'skipped_per_meeting'
    | 'transcript_too_short'
    | 'asr_confidence_too_low'
    | 'owner_spoke_too_little'
    | 'compute_error'
    | null;
};
export type IngestResponse = {
  results: Array<{ meeting_id: number | null; status: string; source_path: string; detail?: string }>;
};

export type PersonSummary = {
  id: number;
  display_name: string;
  role: string | null;
  last_seen_at: string | null;
  meeting_count: number;
  action_count: number;
  is_you?: boolean;
};

export type PersonDetail = PersonSummary & {
  aliases: string[];
  meetings: Array<{
    id: number;
    title: string;
    slug: string;
    status: string;
    created_at: string;
    duration_seconds: number;
  }>;
  actions: Array<{
    id: number;
    text: string;
    due_date: string | null;
    priority: string;
    status: string;
    meeting_id: number;
    meeting_title: string;
    slug: string;
  }>;
};

export type ArchiveTimeline = {
  weeks: number;
  cells: number[][];
  total_meetings: number;
  total_minutes: number;
  top_speaker: string | null;
  top_workstream: string | null;
  top_workstream_confidence: number;
  recent: Array<{
    id: number;
    title: string;
    slug: string;
    status: string;
    created_at: string;
    duration_minutes: number;
  }>;
};

export type TemplateOption = { id: string; name: string };

export type WaveformData = {
  sample_rate_hz: number;
  samples_per_bucket: number;
  bucket_ms: number;
  peaks: number[];
  speaker_segments: Array<{
    start_ms: number;
    end_ms: number;
    speaker_id: string;
    label: string | null;
  }>;
};

export type SegmentComment = {
  id: number;
  segment_id: number;
  parent_id: number | null;
  body: string;
  author: string;
  status: "open" | "resolved";
  resolved_at: string | null;
  created_at: string;
};

export type SegmentEdit = {
  id: number;
  original_text: string;
  corrected_text: string;
  reason: string | null;
  created_at: string;
  applied_at: string | null;
};

export type ExtractProgress = {
  stage?: string;
  status: string;
  progress?: number;
  error?: string | null;
};

export type OwnerIdentity = {
  configured: boolean;
  person_id: number | null;
  display_name: string | null;
  aliases: string[];
};

export type OwnerSuggestion = {
  person_id: number;
  display_name: string;
  meeting_count: number;
};

// ── UI-derived types (not DTOs but used cross-component) ─────────────────

export type SetupItem = {
  id: string;
  label: string;
  ok: boolean;
  detail: string;
  action: string | null;
};

export type SetupStatus = {
  items: SetupItem[];
  ready: boolean;
  blocker_count: number;
};

export type ChapterBullet = {
  text: string;
  speakerId: string;
  startMs: number;
};

export type Chapter = {
  id: string;
  title: string;
  // Model-generated one-sentence skim. Optional because legacy chapter
  // markers predate this field; the model also leaves it empty when the
  // chapter is genuinely too short to summarise.
  summary?: string;
  startMs: number;
  segmentIds: number[];
  avgConfidence: number;
  bullets: ChapterBullet[];
  topSpeakerId: string;
};
