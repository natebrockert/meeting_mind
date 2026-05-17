# Meeting Output Improvements & Reflections

**Status:** Draft for review (not yet accepted). Author: design draft, 2026-05-13.
**Branch:** `claude/meeting-output-improvements`.

## 1. What we're trying to do

Two related upgrades to MeetingMind's synthesis output:

1. **Sharpen the wrap-up.** Take the existing [`MeetingAtoms`](../../backend/app/services/extraction.py) schema and prompts (second-order: tighter actions, clearer decisions, better topic structure; third-order: meeting-level behavior patterns the whole team can see).
2. **Add "Reflections" — an owner-centric self-awareness surface.** A new optional pass that surfaces the *one or two non-obvious things* about how the owner showed up in this specific meeting — talk-time imbalances, questions that went unanswered, decisions made without rationale, etc. — each one source-anchored to a transcript segment. Not every meeting produces a Reflection; many produce zero, and that's correct.

**Constraints the user set:**
- Ships behind an **experimental flag**, with explicit guidance that it's best run on frontier or near-frontier models (Claude/GPT-class, or a top-tier OpenRouter model). Local 9B-class models will not be reliable enough for behavior interpretation, and we should say so out loud.
- **Selective, not exhaustive.** The model picks the most-relevant Reflection(s) for *this* conversation. A balanced 1:1 with even talk-time may produce no Reflections at all — and that's the right behavior. Generic "your talk time was 50%" output is noise.
- Feature name is **"Reflections"** (not "coaching," "performance," "growth").

## 2. Research grounding (what's defensible vs. junk)

A short literature pass to make sure we don't ship pop-psych dressed up as analysis. Sources at the bottom; key takeaways:

### Signals that are well-supported

| Signal | What the research says | How we'd use it |
|---|---|---|
| **Talk-time distribution** | In meetings of 8+, 60–80% of people say nothing. Imbalance correlates with low participation, which Google's Project Aristotle linked to low psychological safety. Extraversion drives talk time but psychological safety drives effectiveness — so balanced participation is the better target. | Per-speaker talk-time %, owner's share vs. group median. Frame as *observation*, not "you talked too much." |
| **Question rate & open-endedness** | Socratic / open-ended questions in coaching and therapy contexts produce measurable engagement and learning gains. Open questions stimulate broader cognitive engagement than closed ones. | Count of questions owner asked; classify open vs. closed; flag questions that got no direct response within N turns. |
| **Psychological-safety markers** | Edmondson: safe teams show people asking for help, admitting uncertainty, debating ideas, and proposing half-formed thoughts. The single highest-leverage leader behavior is admitting "I don't know" or a mistake. | Detect: owner-admits-uncertainty, owner-invites-input ("what do you think?"), owner-acknowledges-other-view. |
| **Unanswered questions** | After-action review research shows decision-quality drops when raised concerns aren't resolved. | Detect owner questions with no on-topic response in the next ~90s of transcript. |
| **Action ownership clarity** | Structured action items with explicit owner + deadline raise completion by ~73% in field studies. | Already supported in schema — tighten prompt to push the model to assign owners and deadlines, not leave them null. |

### Signals that are **NOT** reliable — explicitly excluded from v1

- **Filler words ("um", "uh")** — Stanford and HBR research shows fillers often *aid* listener comprehension. Treating them as a defect is a coaching myth. We won't surface filler counts.
- **Hedging language ("I just think", "maybe", "I feel like")** — Empirical work shows hedges build trust and reduce face-threat; they're prosocial in most business contexts. Won't surface.
- **Sentiment analysis** — Per-speaker positive/negative scores are noisy on transcripts and easy to misinterpret. We already have `tension_points` for genuine disagreement; that's enough.
- **"Confidence" / "assertiveness" scores** — Subjective, easy to bias against introverts and non-native speakers. Not shipping.

### What competitors do (so we know what's table-stakes)

- **Fireflies** — Talk-time ratios, filler-word frequency, sentiment by speaker, question rates. Manager-team view. We pass on filler/sentiment per above.
- **Read.ai** — Coaching from sentiment + engagement; cross-surface (meetings + email + chat). Their sentiment focus is exactly what we want to avoid.
- **Granola** — Note-quality leader; light on analytics. Different bet (note enhancement) than what we're proposing.
- **Otter** — Conversational search over transcripts; minimal coaching.

Our differentiation: **local-first + owner-chosen Reflections grounded in defensible signals**, not a manager-surveillance dashboard.

## 2b. Will Reflections actually help, or will they give people the ick?

Before designing, validating whether this *kind* of feature lands for users — based on feedback research, coaching research, and the workplace-surveillance backlash literature.

### What the research says it takes for AI-driven self-reflection to work

| Finding | Source | Implication for Reflections |
|---|---|---|
| AI-generated feedback produces learning gains **statistically equivalent to human feedback**. | Meta-analysis of 41 studies, N=4,813. | The capability is there; the design has to be right. |
| Working-alliance trust is identical for AI vs. human coaches when clients don't know which they're getting. | Frontiers in Psychology, 2024. | Trust is a UX problem, not a capability ceiling. |
| **"Disclosure effect":** when employees know feedback is AI-generated, productivity dips. | Conversation-intelligence research, 2025. | Be transparent that this is model-generated — but frame as a *mirror the user is looking into*, not a manager's report. |
| Reflective practice (Schön, Kolb) is a strong driver of professional growth — **but** self-appraisal is cognitively biased, so reflection only works when paired with concrete external evidence. | Schön (1983), Kolb (1984); 2022 meta-review. | This is exactly why every observation must cite segment_ids. The transcript is the external evidence that breaks the bias. |
| Negative feedback boosts self-efficacy *only* when paired with emotional support; without it, it backfires. | Nature Scientific Reports RCT, Japan, 2025. | Wise-feedback framing isn't optional. Every observation gets a "I'm showing you this because I think it's useful" header. |
| AI behavior-change apps shift mindsets but not always behavior (Stanford Bloom fitness study). | Stanford HAI. | Sell self-awareness, not behavior change. Don't promise "you'll become a better communicator" — promise "you'll see what you couldn't see in the moment." |

### What makes feedback land (Yeager & Cohen "wise feedback", Dweck, GAIN framework)

- **Specific + concrete** — "Your question at [142] about the rollout timeline got no on-topic response in the next 90s" beats "Your questions sometimes don't land."
- **Forward-looking** — "What could we try next?" beats "What went wrong?" Every Reflection should imply a next-time, not catalogue a past-time.
- **High-standards + capability framing** — Yeager-Cohen "wise feedback": *"I'm giving you these observations because I hold a high standard and I think you can meet it."* Drops defensiveness measurably in field studies.
- **Agency-preserving** — Suggestions, not prescriptions. Never "you should." Use "one option is" or "you might try."
- **Source-anchored** — Receiving feedback you can verify against evidence drops the felt threat (the bias-counter Schön/Kolb identified).

### What gives people the ick (workplace-surveillance and analytics-backlash research)

- **Boilerplate / horoscope quality** — generic observations that could fit any meeting. Worse than nothing.
- **Personality inference vs. behavioral observation** — "you tend to be assertive" → ick. "At [142] you cut Avery off after 2s" → useful.
- **Imposed rather than self-chosen** — top-down "your manager assigned you Reflections" triggers reactance. Our experimental flag + per-meeting opt-out + owner-only delivery is the antidote.
- **Aggregate scores or grades** — "Communication score: 6.2/10" reads as judgment, never lands.
- **Comparison to peers** — "Avery asked more questions than you." Surveillance smell, every time.
- **Repetition / nagging** — surfacing the same observation across 5 meetings is a guilt-trip. We'll need a de-duplication mechanism (open question §9).
- **High-confidence claims from low-quality models** — covered by the model-tier warning.

### Net call

Reflections is **defensibly likely to help**, conditional on these design rules being non-negotiable:

1. Every observation cites segment_ids (UI refuses to render without).
2. Selective by default (zero observations is a valid result).
3. Wise-feedback framing baked into the prompt header.
4. Forward-looking language ("one option is…") not retrospective blame.
5. No scores, no peer comparison, no personality inference.
6. Owner-only, opt-in, deletable per meeting.
7. Model-tier warning when running on small local models.

If we ship without any one of these, ick-risk goes up sharply. These are the lines we don't cross.

## 3. Schema upgrades (second/third-order improvements)

Changes to [`MeetingAtoms`](../../backend/app/services/extraction.py:90) and the `general` prompt template. Each change is independently shippable.

### 3.1 Tighten existing fields

| Field | Issue today | Proposed change |
|---|---|---|
| `actions[].owner` | Often null when speaker is clearly inferable | Prompt: "If the owner is ambiguous, name the speaker who said it; only leave null when the action is genuinely group-owned." |
| `actions[].due_date` | Almost always null | Prompt: "Parse explicit dates and relative phrases ('by Friday', 'next week', 'EOD'). Resolve relative phrases against the meeting date." Add `due_date_source` field referencing segment_id where the date was stated. |
| `decisions[]` | Schema lacks rationale | Add `decisions[].rationale: str` — one sentence on *why* the decision was made. Decision logs without rationale rot fastest. |
| `open_questions[]` | Flat list, no priority or owner | Promote to objects: `{question, raised_by, addressed_to, status}` where status ∈ {`unanswered`, `partially_answered`, `deferred`}. |
| `chapter_markers[]` | Sometimes too broad | Add `summary: str` (1 sentence) per chapter — already useful for skim, mandatory for the reflection pass which needs chapter context. |

### 3.2 New meeting-level fields

| Field | What | Why |
|---|---|---|
| `next_review_date: str \| None` | Suggested follow-up date based on action due-dates | Closes the loop; "when should I revisit this?" |
| `dependencies: list[Dependency]` | Cross-action / cross-workstream blocking relations: "action X blocks workstream Y" | Workstream view is currently flat; dependencies are where most slippage hides. |
| `risks: list[Risk]` | Explicit risks raised but not in `tension_points` (which is for disagreement, not risk) | Today risks get dumped into `uncertainties` or `open_questions`. Worth their own bucket. |
| `meeting_health: MeetingHealth` | See §3.3 below | Team-level behavior pattern, no owner shaming |

### 3.3 `meeting_health` — team-level patterns

A small object surfaced alongside the wrap-up, **not** behind the experimental flag (it's meeting-level, not owner-judgmental):

```python
class MeetingHealth(BaseModel):
    participation_balance: Literal["balanced", "skewed", "dominated"]
      # balanced: top speaker <= 40% of words
      # skewed: top speaker 40-60%
      # dominated: top speaker > 60%
    speaker_count_active: int  # speakers who contributed >= 60s of speech
    speaker_count_silent: int  # speakers diarized but <60s total
    decision_density: Literal["low", "moderate", "high"]  # decisions per 30min
    unresolved_question_count: int
    action_clarity_score: Literal["low", "moderate", "high"]
      # heuristic: % of actions with explicit owner AND due_date
```

These are derived from transcript stats + extracted atoms, **not** from a separate LLM judgment, so they're cheap and safe to ship to all users on all model tiers.

## 4. Reflections pass (experimental, frontier-model-recommended)

### 4.1 Activation

- **Setting:** `config.experimental.reflections_enabled` (default `false`).
- **Dashboard UI:** Toggle in Settings → Experimental, with copy:
  > "**Reflections (experimental)** — After each meeting, surfaces one or two source-anchored observations about how *you* showed up — questions that didn't land, decisions made without rationale, places you invited input. Designed for frontier models (Claude Opus, GPT-class, or top-tier OpenRouter). Local 9B models may produce unreliable or judgmental output. Requires a confirmed owner in Settings → Identity. Some meetings will produce no Reflections — that's intentional."
- **Provider gate:** If the active synthesis model is detected as local + <14B parameters, show a yellow warning in the panel: "These Reflections were generated by a small local model and may be unreliable — treat as a starting point, not an assessment." Don't block — just warn. Users with strong local hardware can override.
- **Per-meeting opt-out:** A "skip Reflections for this meeting" toggle in the dashboard, persisted per meeting (some meetings — therapy, legal, sensitive 1:1s — shouldn't be reflected on at all).
- **Per-kind mute:** In Settings, the user can mute any `kind` (e.g. "don't show me talk_time observations") — see §6.7.

### 4.2 What it produces — `Reflections` schema

```python
class Observation(BaseModel):
    kind: Literal[
        # Speaking patterns
        "talk_time", "interruption_pattern",
        # Question quality
        "question_quality", "unanswered_question", "clarifying_question",
        # Engagement / psych-safety
        "uncertainty_admission", "invited_input", "specific_invitation",
        "paraphrase_check", "build_on_other",
        # Leadership / facilitation
        "framing_quality",      # opened a topic with clear problem statement
        "loop_closure",         # summarized decisions/next steps at end
        "delegation_balance",   # self-assigned vs. delegated actions
        # Communication clarity
        "bluf_response",        # led with the answer, or buried it
        "decision_rationale",   # articulated trade-off, or just pronounced
        # Commitment tracking
        "commitment", "decision_driven",
    ]
    observation: str
        # 1-2 sentences. OBSERVATIONAL, not judgmental.
        # Forward-framed: "Next time you could..." beats "You should have..."
    evidence_segment_ids: list[int]
        # MUST be non-empty. UI refuses to render without.
    confidence: Literal["high", "medium", "low"]
    why_this_matters: str | None = None
        # Optional one-sentence rationale tying observation to a defensible principle.
        # E.g. "Specific invitations get responses 2-3x more often than broadcast ones."
        # Lets the user evaluate whether they agree with the framing.
    suggested_next_time: str | None = None
        # Optional forward-looking option, phrased as "one option is..." not "you should."

class Reflections(BaseModel):
    owner_display_name: str
    # Deterministic stats (computed in Python, injected into prompt as context).
    # Always present, even when no observations are emitted.
    talk_time_seconds: float
    talk_time_pct: float
    questions_asked: int
    questions_open_ended: int
    questions_unanswered: int
    commitments_made: int
    uncertainty_admissions: int
    inputs_invited: int
    # Qualitative output. EMPTY IS A VALID RESULT.
    observations: list[Observation]  # 0-3 typical; cap at 5.
    skipped_reason: str | None = None
        # When observations is empty AND the model judged "nothing notable in this meeting",
        # capture that reasoning so the UI can show "no Reflections — this meeting looked
        # well-balanced" instead of a blank panel.
```

### 4.3 Prompt design principles (sketch — full prompt comes in implementation PR)

The prompt has a non-negotiable header (wise-feedback framing) and a strict selection discipline.

**Header (always sent):**
> You are helping a user reflect on how they showed up in a meeting they're choosing to look back on. You're a mirror, not a judge. The user has explicitly opted into this. Surface only the *one or two non-obvious things* about this specific meeting that an observant peer would mention to a friend — never a checklist. Many meetings will yield zero observations and that is the correct output. You are giving these observations because the user holds a high standard for themselves and you believe they can act on what you show them.

**Selection discipline:**
- **Default to fewer.** 0-3 observations is the right shape for most meetings. 5 is the hard cap.
- **Skip the obvious.** If a stat is already on the Meeting Health strip (e.g. talk-time was 38%, perfectly balanced), do NOT emit a `talk_time` observation. The observation has to *add* signal beyond what the user can see.
- **Skip the generic.** Each observation must be tied to a specific moment with segment_ids. If you can't anchor it, don't surface it.
- **Selectivity over coverage.** Better to emit one excellent observation than five mediocre ones. Empty is fine.

**Per-observation rules:**
- **Observational, not judgmental.** "Your question at [142] received no direct response in the next 90s" beats "You asked unclear questions." Behavior + evidence, never trait inference.
- **Forward-framed.** Pair every observation with an optional `suggested_next_time` phrased as "one option is…" or "you might try…" — never "you should." (Agency-preserving.)
- **Confidence-gated.** Emit `confidence: low` when the signal is weak. UI hides `low` by default behind a "show all" toggle.
- **No personality, no comparison, no scores.** Hard prohibition. Do not infer traits. Do not compare to other speakers. Do not aggregate into a rating.
- **Cite-or-skip.** If `evidence_segment_ids` would be empty, drop the observation entirely.

**Deterministic stats injected as context, not asked of the model.** Talk-time %, question count, unanswered-question count, etc. are computed in Python from segments + atoms and *passed into the prompt as known facts*. The model only does qualitative interpretation. This dramatically reduces hallucination risk on numeric claims.

**Quality refusal.** If the transcript is short (<5 min of speech), low-confidence (avg ASR confidence < 0.6), or the owner spoke <60s total, the prompt instructs the model to return zero observations with a `skipped_reason`. We don't try to find meaning in noise.

### 4.4 What it explicitly does NOT do in v1

- No cross-meeting trend tracking. **Deferred to v2** (see §4.5) — but several v1 decisions are designed to make a good v2 possible.
- No comparison to other speakers ("you talked more than Avery") — that surveils others.
- No personality typing.
- No filler-word / hedge counts (see §2).
- No real-time / in-meeting feedback. Reflections are post-meeting only.
- No aggregate score, grade, or rating. Ever.
- No streaks, badges, or gamification.

### 4.6 Caching pattern (load-bearing for Reflections)

A pattern landed in PR #25 (key-terms cache) that Reflections must follow. The pattern:

- New table `reflection_observations` (meeting_id PK, reflections_json, computed_at, owner_person_id).
- A helper `_reflections_cached_or_llm(config, meeting_id, owner)`:
  1. Look up cached row keyed by `(meeting_id, owner_person_id)`.
  2. If present, deserialize and return.
  3. Otherwise call the model, persist the result, return it.
- An `invalidate_reflections_cache(meeting_id)` hook called at the top of `extract_meeting_atoms` (alongside the existing `invalidate_key_terms_cache`). When the user re-runs synthesis on a meeting, Reflections regenerates.
- Owner-scope: if the configured owner changes, cache rows from the old owner remain in the table but are no longer read (lookup is keyed by current owner_person_id). On owner switch, we don't auto-regenerate — Reflections only generates on a fresh extraction or explicit user request.

Real-world impact in #25 was 15,000× speedup (46s → 3ms) on Review-page load. For Reflections the latency win is comparable, and crucially keeps frontier-model cost predictable — one LLM call per meeting per owner, not per page-open.

## 4b. Conversation Drivers / Center of Gravity (Phase B+)

A complement to Meeting Health and Reflections that surfaces **who and what drove the meeting**, distinct from who talked the most. Most meeting tools over-index on talk-time because it's easy to measure; talk-time alone misses the person who introduces the right question and then stays mostly quiet while others discuss it.

### 4b.1 Two views of the same data

| View | Question it answers | Surface |
|---|---|---|
| **Speaker attribution (CoG)** | "Whose contributions drove this meeting?" → "Avery, despite 12% talk-time." | Meeting Health chip + Reflections observation kinds |
| **Conversation drivers** | "What moments actually drove the meeting?" → "the question at [142], the reframing at [340], the decision pivot at [507]." | Mind Map panel + transcript markers + Review page prioritization |

These aren't redundant — same underlying data, two slices. The "who" answer naturally implies the "what" via speaker attribution; the "what" view is segment-anchored and reviewable.

### 4b.2 Deterministic v1 signals

We can compute these from segments + atoms with no LLM call:

| Signal | What it measures |
|---|---|
| **Chapter introduction** | First substantive speaker after each `chapter_markers[].start_segment_id`. One gravity point per chapter introduced. |
| **Post-utterance discussion** | For each turn by speaker X, sum of *other-speaker* speech seconds in the next 90s. Large sums = high pull. |
| **Question → discussion ratio** | Of this speaker's questions (sentence-ending `?` or interrogative openers), what fraction triggered ≥3 multi-speaker turns? |
| **Decision authorship** | For each decision, the speaker whose source_segment_ids appear earliest in the seed. They get authorship credit even if a different speaker pronounces the decision. |

Composite (subject to tuning):
```
gravity_score(speaker) =
    0.30 * normalize(chapters_introduced)
  + 0.35 * normalize(other_speaker_seconds_after_their_turns)
  + 0.20 * normalize(questions_that_triggered_long_chains)
  + 0.15 * normalize(decisions_seeded)
```
Classified into `high` / `moderate` / `low` per speaker.

### 4b.3 LLM-judged signals (deferred, v2+)

These add precision but cost budget. Skip for v1; revisit after the deterministic version ships.

- **Reference detection** — "as Avery said," "to your point," verbatim phrase echoing.
- **Idea adoption** — a concept this speaker introduced reappears in later turns from other speakers.
- **Building-on vs. polite redirect** — distinguishing substantive "yes, and" from "yes, but actually."

### 4b.4 Schema

```python
class ConversationDriver(BaseModel):
    """A specific moment that meaningfully reshaped what followed."""
    kind: Literal[
        "topic_introduction",    # first substantive utterance of a chapter
        "pivot_question",         # a question that triggered a sustained exchange
        "reframing",              # a new framing of the existing topic
        "challenge",              # counterpoint that shifted direction
        "decision_moment",        # the moment a decision was made
        "unstick",                # moment that broke a circular discussion
    ]
    segment_id: int               # the pivot moment itself
    speaker_label: str | None     # display name when confirmed; null otherwise
    description: str              # one-sentence why this moment was a driver
    impact_seconds: float         # length of follow-on multi-speaker discussion
    confidence: Literal["high", "medium", "low"]


class CenterOfGravity(BaseModel):
    """Per-meeting CoG snapshot. Always computed; cheap; no LLM."""
    rankings: list[dict]              # [{speaker_id, label, score, talk_time_pct, gravity_pct}]
    standout_speaker_id: str | None   # set only when CoG rank diverges meaningfully from talk-time rank
    standout_reason: str | None       # one-sentence explanation when standout is set
```

`ConversationDriver` records are persisted (own table or a `kind='conversation_driver'` in `review_items`) so the Review page can iterate them; `CenterOfGravity` is computed-on-demand and lives on the overview dict.

### 4b.5 Surfaces

**(a) Mind Map (always-on, team-level).** A "What drove this meeting" panel rendering 3-6 driver moments with click-to-jump. Renders only when drivers exist. Plus a conditional Meeting Health chip — `Driven by · Avery (12% talk, 4 of 6 chapters)` — surfaced **only when CoG meaningfully diverges from talk-time** (the non-obvious case). When the top talker is also the top driver, no chip.

**(b) Reflections (owner-only, experimental flag).** Three new observation kinds: `topic_introducer`, `question_sparked_discussion`, `idea_seeded_decision`. Each frames a driver moment from the owner's POV ("your question at [142] triggered 8 minutes of discussion"). Adds a positive-signal vector to Reflections — counterweight to the "what could you do differently" lean.

**(c) Review page (always-on, verification-prioritization).** New: the review queue prioritizes segments overlapping driver moments where the speaker is still unconfirmed. Concretely a banner: "3 driver moments are pending speaker confirmation — verify these first." Click-to-jump. Same logic for low-confidence segments: a 70%-confidence segment in a driver moment matters far more than a 70%-confidence segment in small talk. Practical payoff: scarce reviewer attention goes where attribution mistakes have the highest cost.

### 4b.6 Diarization caveat

Per implementation note: diarization quality is currently weak. Design accommodates this without changing schemas later:
- Driver detection (the "what" view) doesn't depend on diarization quality — segments are segments.
- Speaker attribution (the "who" view) gates on `confirmed_by_user`. When a driver moment's speaker is unconfirmed, the moment renders without a name + a "needs speaker review" pill. This same gate **also drives the review-page prioritization in (c)** — circular benefit.
- Diarization quality improvements down the line lift attribution precision with zero schema change.

### 4.5 v2 — cross-meeting Reflections (design intent, not v1 scope)

Eventually Reflections should aggregate across meetings — the user has confirmed this direction. Specifics deferred to a separate design doc when v1 has shipped and we have real signal from users, but capturing the shape now because **several v1 design choices are load-bearing for v2**:

**v1 decisions that v2 depends on:**
- **`evidence_segment_ids` always non-empty.** Aggregate views are useless without drill-down to evidence; this is why the constraint is non-negotiable in v1.
- **The `kind` enum.** The 17 kinds are the only way to aggregate meaningfully across meetings ("3 of your last 10 meetings showed `unanswered_question` patterns"). If we used freeform observations, aggregation would be impossible without a clustering step we'd inevitably get wrong.
- **The helpful/unhelpful thumb (§6.9).** v2 should default to showing only patterns the user *previously rated helpful*. Raw frequency counts are surveillance; user-validated patterns are a journal. The thumb is the trust signal that makes this work.
- **Per-kind mute (§6.7).** v2 inherits the same mute list — muted kinds stay muted in the aggregate.
- **Per-meeting opt-out (§6.3).** Skipped meetings stay out of the aggregate entirely.

**Likely v2 shape (sketch, not commitment):**

A "Reflections" index page at the dashboard level (peer of the meetings list), separate from the per-meeting Reflections tab. Contents:

- **Stats over time** — deterministic numerics only (talk-time trend, question-rate trend, unanswered-question count). These are facts; trends in facts are non-judgmental.
- **Recurring patterns** — only `kind`s that appeared in at least N meetings *and* received at least one helpful-thumb from the user. Each pattern card links to the specific meetings/segments where it surfaced. Surface as "you've noticed this before — here are the moments" rather than "we've detected a pattern."
- **Helpful-rated observations bookmarked** — the user's own thumbs-up'd observations, browsable like a journal. This is the highest-trust, lowest-ick view.
- **Time decay.** Observations from >90 days ago are excluded by default (configurable). Old patterns aren't actionable; surfacing them is nagging.

**Design rules that get stricter at the aggregate level:**

| v1 (per-meeting) | v2 (cross-meeting) |
|---|---|
| 0-5 observations per meeting | Patterns require ≥3 supporting meetings AND user validation to surface |
| Observation can be `confidence: medium` | Aggregate patterns require ≥1 `confidence: high` instance |
| Model judges relevance | User-thumbed observations are the floor; model can't promote unrated patterns to the aggregate |
| Empty state is fine | Empty state is the default and the goal |
| Per-meeting opt-out | Per-meeting AND per-pattern opt-out ("don't surface `interruption_pattern` for me anymore") |

**Anti-patterns we will not ship in v2:**
- Push notifications about patterns. (No "you've interrupted 3 times this week.")
- Comparison to a baseline or to other users.
- "Areas of concern" or any negative aggregation.
- Auto-generated summary like "your meeting style is X."
- A score that goes up or down over time.

The framing for the v2 index page should be **a private journal you choose to open**, not a dashboard that pushes insights at you.

## 5. UI / surfacing

### 5.1 Wrap-up changes (always-on)

- New "Meeting Health" strip near top of overview: small chips for `participation_balance`, `decision_density`, `unresolved_question_count`, `action_clarity_score`. Each chip is clickable → scrolls to the evidence.
- Decisions render with rationale below the title.
- Open questions get status pills (unanswered / partial / deferred).
- Dependencies render as a small "X blocks Y" list under workstreams.

### 5.2 Reflections panel (experimental)

A new dashboard tab/section "**Reflections**", shown only when the experimental flag is on AND owner is set AND the meeting included the owner. Layout:

```
┌─ Reflections ─────────────────────────────────────┐
│ Stats strip: talk-time 38% · 6 questions (4 open) │
│             · 2 commitments · 1 unanswered Q     │
├───────────────────────────────────────────────────┤
│ ── If observations exist ──                       │
│   • You asked Avery at [142] "what's blocking     │
│     the rollout" — no on-topic response followed. │
│     Why this matters: specific questions that go   │
│     unanswered are usually where slippage hides.   │
│     One option next time: name the response you    │
│     want ("Avery, can you give me a yes/no on…?"). │
│     [confidence: high · jump to clip · helpful?]   │
│                                                    │
│   • You acknowledged uncertainty at [203] ("good   │
│     question, I'm not sure"). This is one of the   │
│     stronger psychological-safety signals leaders  │
│     can model. [confidence: medium · jump to clip] │
│                                                    │
│ ── If no observations ──                          │
│   Nothing notable surfaced this meeting.           │
│   The stats above are still here if you want them. │
└───────────────────────────────────────────────────┘
```

Every observation is clickable and scrolls the transcript to the cited segment. The "helpful?" thumb captures user signal we'll eventually use for prompt tuning (kept local, never shipped anywhere). Empty state is a feature, not a failure — it tells the user "this meeting didn't need a Reflection."

## 6. Privacy / ethics / ick-mitigation guardrails

This is the section we will get wrong if we're not careful. The §2b research shows that the line between "useful mirror" and "creepy surveillance" is thin and design-dependent.

1. **Owner-only.** Reflections are generated about and shown to the owner. We do not surface other speakers' behaviors as "coaching." (No manager-surveillance mode.)
2. **Local-first preserved.** When the user runs against a local model, no data leaves the machine. When the user opts into OpenRouter for synthesis, the Reflections pass uses the same provider — no separate egress.
3. **Per-meeting opt-out persists.** Once a meeting is marked "skip Reflections," it stays skipped across regenerations.
4. **1:1 default.** Reflections default off for `template = one_on_one`; user opts in per meeting. 1:1s are the most sensitive context and the cost of an ick-misfire is highest there. (See open question §9.2.)
5. **Stored locally, not shipped.** Reflections JSON lives in the same SQLite store as `review_items`; we add a new `kind='reflections'`. No external sync.
5a. **Export boundary — Reflections never leave the dashboard.** HTML export, PDF export, Obsidian Markdown export, and any future share/copy/email action MUST NOT include Reflections content (observations, growth notes, stats strip, or even the existence of a Reflections panel). Concretely:
    - `build_meeting_overview` does NOT pull `kind='reflections'` rows.
    - `render_meeting_note` (Obsidian), `html_export`, and `pdf_export` have no code path that reads the Reflections review_items.
    - The dashboard "Share / Copy to clipboard / Export" affordances never include the Reflections tab content.
    - The only surface for Reflections is the in-app Reflections tab, gated by the experimental flag and the owner check.
    - This must be enforced by a unit test: `kind='reflections'` rows are present in the DB, but no exported artifact (html / pdf / md / obsidian vault) contains the observation text. Easy to forget when adding a new export path, so the test is the safeguard.
6. **Deletable.** A "delete Reflections for this meeting" button next to "skip Reflections."
7. **Per-kind mute (ick-mitigation).** Settings → Reflections shows the list of observation `kind`s with toggles. If the user finds `talk_time` observations preachy, they can mute that kind globally. The mute applies post-generation (the model still produces them, the UI filters them) so re-enabling doesn't require regeneration.
8. **De-duplication across meetings (ick-mitigation).** If the same `kind` of observation surfaces in 3+ consecutive meetings, the dashboard auto-suppresses it with a "we've mentioned this before — keep showing?" prompt. Prevents nagging. (Implementation detail: track `kind` history per owner in DB; suppress fourth occurrence by default.)
9. **Helpful/unhelpful signal stays local.** The thumbs-up/down on each observation is stored locally and used only for the user's own filtering preferences. Never aggregated, never exfiltrated. (If we eventually want it for prompt tuning, that's a separate consent flow.)
10. **Honest about what generated it.** Each observation card shows a small "AI-generated · model: claude-opus-4-7" footer. The disclosure-effect research says hiding this backfires worse than disclosing.
11. **No coaching language.** Strings in the UI say "observations" and "options to try" — not "coaching tips" or "areas for improvement." Words carry the surveillance smell or don't.

## 7. Implementation phases

Revised after the v0.1.2–v0.1.5 refactor landed on main (caching pattern, frontend component split). Phase 0 added; B+ added for Conversation Drivers / CoG.

| Phase | Scope | Risk |
|---|---|---|
| **0. Doc alignment** (this section) | Capture CoG/Drivers + caching pattern in this doc before any code. | None. |
| **A. Schema tightening** (no flag) | §3.1 prompt + schema changes: owner inference, due-date parsing, decision rationale, open_question objects, chapter summaries, due_date_source. Backend + small frontend rendering additions. | Low. Tweaks an existing pass; backward-compatible if old payloads coexist (use optional fields). |
| **B. Meeting Health** (no flag) | §3.3 — deterministic computation from segments + atoms, no new LLM call. Backend module + frontend chip strip. | Low. Pure derivation. |
| **B+. Conversation Drivers / CoG** (no flag) | §4b — deterministic v1 (chapter intros, post-utterance discussion, question→chain, decision authorship). Three surfaces: Mind Map driver panel, conditional CoG chip, Review-page prioritization data. | Medium. Three surfaces; doc-update prerequisite. |
| **F. Eval harness** (cross-cutting) | Build a small set of 3-5 hand-labeled meetings with expected observations + drivers to regression-test prompt changes. | Medium. Without this we can't iterate on D safely. |
| **D. Reflections pass — backend** (flag) | §4.2, §4.3 schema + extraction logic + deterministic stat computation + LLM call. Adopts caching pattern from §4.6. New `kind='reflections'` review_items + `reflection_observations` cache table + cache invalidation hook in `extract_meeting_atoms`. Includes per-kind mute storage + cross-meeting de-dup. | Medium. Big new prompt; need eval set. |
| **E. Reflections panel — frontend** (flag) | §5.2 Reflections tab, clickable evidence, model-tier warning, helpful/unhelpful signal, empty-state copy. | Low once D lands. |
| **C. New meeting-level fields** (no flag) | §3.2 — dependencies, risks, next_review_date. New prompt instructions. | Medium. More fields = more chance of model getting confused; pilot with a few meetings before merging. Deferred until D/E land so we know what's actually being read. |

Recommended merge order: **0 → A → B → B+ → F → D → E → C**.

## 8. Cost / latency considerations

- Reflection is a single extra LLM call per meeting (post-extraction, pre-snapshot). With deterministic numerics pre-computed, the prompt is short (~2k tokens) and the output is bounded (~1k tokens).
- On Claude Opus / GPT-class: ~$0.05–0.15 per meeting at current pricing. Acceptable.
- On local Ollama (Gemma2 9B): ~30–60s extra latency, output unreliable — hence the warning.
- We do NOT regenerate reflection on every dashboard load; cache in DB and regen only on explicit user action.

## 9. Open questions for the maintainer

1. **Should `meeting_health` be always-on, or also experimental?** I leaned always-on because it's deterministic and team-level (not judgmental). Easy to flip.
2. **1:1 handling.** Default to off for `template = one_on_one` is my current proposal (§6.4). Alternative: detect whether owner is the manager (running the meeting) vs. the report, and default-off only when owner = report. The detection is fragile though. Leaning toward "default off for all 1:1s, user can opt in per-meeting."
3. **De-duplication threshold.** §6.8 suppresses a `kind` after 3 consecutive meetings showing it. Right number? Too low and we miss persistent patterns; too high and we nag.
4. **Model-tier detection aggressiveness.** Soft yellow banner vs. hard gate that requires explicit override? I leaned soft.
5. **Eval set — how to build it.** Phase F needs ground-truth Reflections on 3-5 hand-labeled meetings. Who hand-labels? You yourself on your own past meetings is the most authentic option but limited sample. Synthetic + a couple of yours might be a starting point.
6. **First-run onboarding.** When a user first enables Reflections, should we run it retroactively on their last 1-3 meetings as a "here's what it looks like" demo, or only forward from enablement? Demo is more compelling but risks a bad first impression if those past meetings happen to produce ick-prone observations.
7. **Action items for the *owner specifically* — duplicate or link?** The Reflections panel will show "you committed to X." Should that be a copy of the existing action-item card, or a link? I lean link.

## 10. References

**Meeting science & behavior signals:**
- Rogelberg, S.G. *The Surprising Science of Meetings* (2019) and *Glad We Met: The Art and Science of 1:1 Meetings* (2024). Oxford University Press.
- *Thirty Years of Meeting Science: Lessons Learned and the Road Ahead.* Annual Review of Organizational Psychology, 2025.
- Edmondson, A.C. (1999). *Psychological Safety and Learning Behavior in Work Teams.* Administrative Science Quarterly.
- CIPD (2023). *Productive Meetings: An Evidence Review.*
- *Testing the babble hypothesis: Speaking time predicts leader emergence in small groups.*
- Stanford GSB / HBR (2019). Filler-words research summary — "Um, Like, So: How Filler Words Can Be Effective." (Cited to *reject* filler-word coaching, not endorse it.)
- US Army AAR doctrine (timing / decay of AAR effectiveness).

**Question quality & coaching:**
- *Therapist Use of Socratic Questioning Predicts Session-to-Session Symptom Change in Cognitive Therapy for Depression.* PMC.
- *A new purpose for Socratic questioning in coaching.* Philosophy of Coaching, vol. 8.
- *The art and science behind socratic questioning and guided discovery: a research review.* Psychotherapy Research, 2023.

**Feedback that lands (§2b foundation):**
- Yeager, D.S. & Cohen, G.L. — "Wise Feedback" framing (high standards + capability assurance).
- Dweck, C. *Mindset* (2006) — growth-mindset framing.
- GAIN framework for feedback (Lenny's newsletter, evidence-based summary).
- UCLA Teaching & Learning Center — Wise Feedback applications.

**Reflective practice for growth:**
- Schön, D.A. *The Reflective Practitioner* (1983) — reflection-in-action vs. reflection-on-action.
- Kolb, D.A. *Experiential Learning* (1984).
- *Conceptualizing the complexity of reflective practice in education.* PMC, 2022 (caveat that self-appraisal is biased without external evidence).

**AI feedback effectiveness & trust:**
- *How does artificial intelligence compare to human feedback? A meta-analysis of performance, feedback perception, and learning dispositions.* (41 studies, N=4,813.)
- *Artificial intelligence vs. human coaches: examining the development of working alliance in a single session.* Frontiers in Psychology, 2024.
- *AI feedback and workplace social support in enhancing occupational self-efficacy: a randomized controlled trial in Japan.* Nature Scientific Reports, 2025.
- Stanford HAI — Bloom AI fitness coach study (mindset shift vs. behavior).
- Conversation-intelligence "disclosure effect" research (2025).

**Workplace surveillance / ick-factor:**
- *The ethics of self-tracking. A comprehensive review of the literature.* Ethics & Behavior, 2022.
- *Private Eyes, They See Your Every Move: Workplace Surveillance and Worker Well-Being.* Glavin, Bierman, Schieman, 2024.
- *Integrating Social Scientific Perspectives on the Quantified Employee Self.* MDPI Social Sciences.
- AIHR — *People Analytics: Ethical Considerations* (81% of projects jeopardized by ethics/privacy).

Competitive scan: Read.ai, Fireflies, Granola, Otter, Fathom feature pages and 2026 comparisons.

Codebase touch-points:
- [`backend/app/services/extraction.py`](../../backend/app/services/extraction.py) — `MeetingAtoms`, prompts, extraction loop.
- [`backend/app/services/synthesis.py`](../../backend/app/services/synthesis.py) — review snapshot.
- [`backend/app/services/owner.py`](../../backend/app/services/owner.py) — owner identity + annotation.
- [`backend/app/db/database.py`](../../backend/app/db/database.py) — review_items + action_items schema.
- [`frontend/src/observatory.tsx`](../../frontend/src/observatory.tsx) — main dashboard view.
