from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass

import httpx
from app.config import AppConfig, load_config

# Curated short list of locally-runnable models with notes about why each is
# called out. Surfaced in the install wizard and the dashboard Settings page so
# users do not have to guess from the raw `lms ls` output. Each tier is a
# generic family pattern; the actual key on the user's machine may include a
# quantization suffix (e.g. "@q8_0") that the provider listing will reveal.
# `speed_class` is a coarse latency hint surfaced in the dashboard's
# Settings → Model field. Values: "fast" (<60s for a typical 15-min
# meeting), "mid" (60-180s), "slow" (>180s, usually a cloud frontier
# model). Helps users pick before they discover the wait empirically.
RECOMMENDED_MODELS: list[dict[str, str]] = [
    {
        "id": "nvidia/nemotron-3-nano-omni",
        "tier": "recommended",
        "role": "default",
        "speed_class": "fast",
        "note": "Best balance of quality, speed, and action attribution. Fast.",
    },
    {
        "id": "qwen3.6-35b-a3b-ud-mlx",
        "tier": "recommended",
        "role": "quality",
        "speed_class": "mid",
        "note": "Stronger judgment for workstream consolidation. MLX-optimised.",
    },
    {
        "id": "gemma-4-e4b-it@q8_0",
        "tier": "baseline",
        "role": "default",
        "speed_class": "fast",
        "note": "Lightweight ~7.5B q8 fallback when only a small model is available.",
    },
    {
        "id": "qwen3.6-27b@q8_0",
        "tier": "alternate",
        "role": "quality",
        "speed_class": "slow",
        "note": "Big-dense alternative if MLX MoE is not available; meaningfully slower.",
    },
    # OpenRouter routes worth surfacing as presets — they auto-cache, have
    # generous context windows, and pencil out at low per-meeting cost.
    # Listed here so the install wizard / Settings can suggest them when
    # the provider is OpenRouter; the dashboard's Model field still
    # accepts any string for power users.
    {
        "id": "x-ai/grok-4.1-fast",
        "tier": "cloud_recommended",
        "role": "default",
        "speed_class": "fast",
        "note": "Cloud sweet spot — 2M context, auto-cached prefix, ~$0.20/M in. Cheap default extraction.",
    },
    {
        "id": "x-ai/grok-4.3",
        "tier": "cloud_alternate",
        "role": "quality",
        "speed_class": "mid",
        "note": "Stronger judgment for narrative + consolidation; 1M context, supports prompt caching.",
    },
    {
        "id": "openai/gpt-oss-120b",
        "tier": "cloud_baseline",
        "role": "default",
        "speed_class": "fast",
        "note": "Open-weight 120B MoE on OpenRouter with a free tier; native structured output. Worth evaluating before paying for proprietary.",
    },
]


@dataclass(frozen=True)
class ChatMessage:
    """Provider-neutral chat message passed to a model bus.

    When `cache_control` is True, the OpenRouter serializer marks this
    block as a prompt-cache breakpoint for providers that honour explicit
    markers (Anthropic, Qwen, Alibaba). Auto-cache providers (OpenAI,
    xAI Grok, DeepSeek, Groq, Gemini 2.5+) don't need the marker — their
    caches fire on any matching prefix. Providers without caching support
    (Tencent, etc.) ignore the flag entirely.

    The pattern: put the long stable content (system preamble + transcript)
    first with `cache_control=True`, then the per-task instructions after.
    Subsequent calls in the same meeting hit the warm cache.
    """

    role: str
    content: str
    cache_control: bool = False


# Models on OpenRouter that honour Anthropic-style explicit cache_control
# markers. For these we emit content as a structured array with the
# `{"cache_control": {"type": "ephemeral"}}` annotation on the first block.
# Other supported providers (OpenAI, Grok, DeepSeek, Groq, Gemini 2.5+)
# auto-cache matching prefixes without needing markers — for those we
# leave content as a plain string and rely on the prefix-byte-match.
# Tencent, Mistral non-2503, etc. don't support caching at all yet.
#
# Conservative allowlist: only `anthropic/` is end-to-end verified on
# OpenRouter. Qwen and Alibaba routes are served through non-Anthropic
# backends (Together, Fireworks, etc.) where the marker shape can be
# silently ignored or returned as a 4xx by strict providers, so we keep
# them out and let prefix-match auto-caching handle them (which costs us
# nothing if it doesn't fire). Add a new prefix only after the roundtrip
# is verified end-to-end against a real route.
_EXPLICIT_CACHE_PREFIXES = ("anthropic/",)


# Short stable preamble that always wraps a cacheable transcript prefix.
# Kept generic on purpose — every call site that supplies a cache_prefix
# gets the same byte sequence here, so the prefix [preamble + transcript]
# matches across the meeting's extraction pipeline and the provider's
# prompt cache fires on subsequent calls.
#
# DO NOT change this string casually. Any byte-level edit invalidates
# every warm prompt cache for every meeting on every cache-supporting
# provider (Anthropic explicit, OpenAI / Grok / DeepSeek / Groq / Gemini
# auto). Treat as a versioned constant — bump in a dedicated PR with a
# CHANGELOG note when the change is justified.
_CACHE_PREAMBLE = (
    "MeetingMind extraction pipeline. The following meeting transcript "
    "will be analyzed across several JSON-output tasks. Each task message "
    "below specifies its own role, schema, and instructions."
)


def _supports_explicit_cache_markers(model_id: str) -> bool:
    """True iff the configured OpenRouter route honours `cache_control`
    breakpoint markers. False for auto-cache providers and unsupported
    routes — both render content as a plain string.
    """
    if not model_id:
        return False
    lowered = model_id.lower()
    return any(lowered.startswith(prefix) for prefix in _EXPLICIT_CACHE_PREFIXES)


def _fold_cache_prefix(
    messages: list[ChatMessage], cache_prefix: str | None
) -> list[ChatMessage]:
    """For non-OpenRouter providers, fold the cache_prefix into the first
    user message so the prompt content is preserved even though no
    caching happens. Returns the messages list unchanged when no prefix
    was supplied.
    """
    if not cache_prefix:
        return messages
    folded: list[ChatMessage] = []
    injected = False
    for msg in messages:
        if not injected and msg.role == "user":
            folded.append(ChatMessage("user", f"{cache_prefix}\n\n{msg.content}"))
            injected = True
        else:
            folded.append(msg)
    if not injected:
        # No user message in the list — append the prefix as one.
        folded.append(ChatMessage("user", cache_prefix))
    return folded


def _serialize_message(message: ChatMessage, *, explicit_cache: bool) -> dict:
    """Render a ChatMessage as an OpenAI-API-compatible dict.

    Plain path (`cache_control=False` OR provider auto-caches): emit
    `{"role": ..., "content": "..."}` with content as a string. This is
    the default for every existing call site.

    Explicit-cache path (`cache_control=True` AND provider supports
    markers): emit `{"role": ..., "content": [{"type": "text", "text":
    "...", "cache_control": {"type": "ephemeral"}}]}` so Anthropic
    treats the block as a prefix breakpoint. See `_EXPLICIT_CACHE_PREFIXES`
    for the current allowlist.
    """
    if message.cache_control and explicit_cache:
        return {
            "role": message.role,
            "content": [
                {
                    "type": "text",
                    "text": message.content,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        }
    return {"role": message.role, "content": message.content}


def _parse_llm_json(content: str) -> dict:
    """Parse LLM-emitted JSON with graceful repair on common malformations.

    v0.2.10 fix: `_chat_json_openrouter` previously called `json.loads`
    directly; a single bad token from the LLM (trailing comma, unquoted
    key, truncated mid-output, explanation text wrapping the object)
    crashed the entire extract stage with a 500. This helper attempts:

      1. Plain `json.loads` (fast path; matches the prior behavior).
      2. Strip markdown fences if the model wrapped despite our system
         prompt asking it not to.
      3. Trim explanation text before the first `{` and after the last
         `}` so a model that monologues around its answer still parses.
      4. Strip trailing commas (`,]` / `,}` are valid JS but not JSON).

    If all four fail, re-raises the final `JSONDecodeError` so the
    caller can choose to retry the LLM call or fail loudly.
    """
    # Audit-round-2 fix: coerce None / non-string early so callers don't
    # see a TypeError from `json.loads(None)` — the contract is "give us
    # what the model emitted, we'll figure out if it parses." Anything
    # non-string becomes "" and falls through to JSONDecodeError.
    if not isinstance(content, str):
        content = ""
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    cleaned = content.strip()
    # Step 2: strip ``` / ```json fences.
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    # Step 3: extract substring from the first `{` to the last matching `}`.
    # Useful when the model emits "Sure, here's the JSON: {...} hope this
    # helps!" — we want just the object.
    if "{" in cleaned and "}" in cleaned:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end > start:
            candidate = cleaned[start : end + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                cleaned = candidate  # try further repairs below

    # Step 4: strip trailing commas before } and ].
    repaired = re.sub(r",\s*([}\]])", r"\1", cleaned)
    return json.loads(repaired)  # raises JSONDecodeError on failure


# In-process TTL cache for model-list subprocess calls.
# Audit finding (perf HIGH/MED): GET /api/settings and /api/setup-status
# each shelled out `lms ls` + `ollama list` on every hit — 2× up to 10s
# blocking subprocesses per request. The dashboard polls settings/setup
# every few seconds, so this could chew significant CPU under normal use.
# A short TTL (15s) is enough to amortize burst polling but still picks
# up freshly-installed models within a reasonable window.
_MODEL_LIST_TTL_SECONDS = 15.0
_model_list_cache: dict[str, tuple[float, list[str]]] = {}


def _cached_model_list(key: str, fetcher) -> list[str]:
    """Return cached fetcher() result, refreshing if older than TTL.

    The cache lives at module scope so it survives across requests within
    a single backend process. Restarts clear it — that's fine, the first
    post-restart call repopulates.
    """
    now = time.monotonic()
    cached = _model_list_cache.get(key)
    if cached and (now - cached[0]) < _MODEL_LIST_TTL_SECONDS:
        return cached[1]
    fresh = fetcher()
    _model_list_cache[key] = (now, fresh)
    return fresh


def _list_lm_studio_models_uncached() -> list[str]:
    try:
        result = subprocess.run(
            ["lms", "ls"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    models: list[str] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("You have") or stripped.startswith("LLM"):
            continue
        if " Local" in stripped:
            models.append(stripped.split()[0])
    return models


def list_lm_studio_models() -> list[str]:
    """Return locally installed LM Studio model identifiers, or an empty list if unavailable.

    Cached for ~15s in-process to avoid hammering `lms ls` on every dashboard poll.
    """
    return _cached_model_list("lm_studio", _list_lm_studio_models_uncached)


def _list_ollama_models_uncached() -> list[str]:
    try:
        result = subprocess.run(
            ["ollama", "list"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    models: list[str] = []
    for line in result.stdout.splitlines()[1:]:
        stripped = line.strip()
        if stripped:
            models.append(stripped.split()[0])
    return models


def list_ollama_models() -> list[str]:
    """Return locally installed Ollama model identifiers, or an empty list if unavailable.

    Cached for ~15s in-process — see `list_lm_studio_models`.
    """
    return _cached_model_list("ollama", _list_ollama_models_uncached)


def invalidate_model_list_cache() -> None:
    """Force the next list_* call to re-probe.

    Call this if you know a model was just installed/removed and you want
    the dashboard to reflect it immediately (e.g. after `lms install`).
    """
    _model_list_cache.clear()


def resolve_lm_studio_base_url(config: AppConfig) -> str:
    """Find a responding LM Studio OpenAI-compatible endpoint, starting the server if needed."""
    configured = config.models.lm_studio_base_url.rstrip("/")
    if _server_responds(configured):
        return configured

    status = _run_lms(["server", "status"])
    port = _parse_lm_studio_port(status.stdout + status.stderr)
    if not port:
        started = _run_lms(["server", "start"])
        port = _parse_lm_studio_port(started.stdout + started.stderr)
    if port:
        discovered = f"http://127.0.0.1:{port}/v1"
        if _server_responds(discovered):
            return discovered
    return configured


_VALID_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./@:-]{0,200}$")


def _validate_model_name(model: str) -> str:
    """Reject model names that could be smuggled as CLI flags.

    Audit finding H-3: `_run_lms(["load", model, ...])` previously accepted
    any string. A model name like `--verbose` or `--unload-all` would be
    passed to the `lms` binary as a flag rather than a model identifier
    because `_run_lms` uses list-form (no shell), but argparse on the lms
    side parses everything before positional terminators as options.

    Real model names are slugs (`qwen3-4b-instruct`, `meta-llama/Llama-3-8B`,
    `openrouter:anthropic/claude-3.5-sonnet`) — never start with `-`.
    """
    if not _VALID_MODEL_NAME_RE.fullmatch(model):
        raise ValueError(f"invalid_model_name: {model!r}")
    return model


# Track which LM Studio models we've already verified loaded in this
# process, with the timestamp. Audit finding (perf HIGH): chat_json()
# called ensure_lm_studio_model_loaded() on every LLM request, which
# shelled out `lms ps` every single time. For batch synthesis (key terms,
# workstreams, summaries) that's 5-10 `lms ps` calls per meeting. The
# loaded state only changes when (a) we just loaded a new model, or
# (b) the TTL elapses on the lms side and lms unloads it. We mirror
# lms's TTL so our cache invalidates around the same time.
_loaded_model_cache: dict[str, float] = {}


def _model_marked_loaded(model: str, ttl_seconds: int) -> bool:
    now = time.monotonic()
    last = _loaded_model_cache.get(model)
    # Re-verify slightly before the TTL so we don't race lms unloading it.
    safe_window = max(ttl_seconds - 30, ttl_seconds * 0.8)
    return last is not None and (now - last) < safe_window


def _mark_model_loaded(model: str) -> None:
    _loaded_model_cache[model] = time.monotonic()


def ensure_lm_studio_model_loaded(model: str, ttl_seconds: int) -> None:
    """Load the requested LM Studio model with a TTL so idle models can detach automatically.

    Caches the "verified loaded" state in-process so we don't shell out to
    `lms ps` on every LLM call. A safe window slightly shorter than the
    TTL is used so we re-verify before lms unloads on its side.
    """
    _validate_model_name(model)
    if _model_marked_loaded(model, ttl_seconds):
        return
    loaded = _run_lms(["ps"])
    if model in loaded.stdout:
        _mark_model_loaded(model)
        return
    # `--` terminator: belt-and-braces in case lms ever changes its arg parser
    # to accept flags after positionals. Combined with the regex validator,
    # this closes audit finding H-3 entirely.
    loaded = _run_lms(["load", "--ttl", str(ttl_seconds), "-y", "--", model], timeout=180)
    if loaded.returncode != 0:
        detail = (loaded.stderr or loaded.stdout or "unknown error").strip()
        raise RuntimeError(f"LM Studio failed to load {model}: {detail}")
    _mark_model_loaded(model)


class ModelBus:
    """Small provider facade for JSON-only local LLM calls."""

    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or load_config()

    def chat_json(
        self,
        messages: list[ChatMessage],
        schema: dict,
        model: str | None = None,
        *,
        timeout: float | None = None,
        cache_prefix: str | None = None,
    ) -> dict:
        """Run a chat completion and parse the provider response as JSON.

        `timeout` (seconds) caps the inference HTTP call. Primary extraction
        defaults to 120 s; secondary enrichment calls (key terms, workstream
        consolidation) should pass a lower value so a stuck model can't hang
        the entire synthesis for the full timeout window.

        `cache_prefix` (optional, OpenRouter only) is the long stable
        content — typically the rendered transcript — that should be
        treated as a prompt-cache breakpoint. When supplied, it's
        prepended as the first user message with `cache_control=True`,
        so subsequent calls within the same meeting hit the warm cache
        (Anthropic / Qwen explicit, OpenAI / Grok / DeepSeek / Groq /
        Gemini 2.5+ automatic). Local providers ignore it; the prefix
        is then folded into the caller's existing user message so the
        prompt content is preserved.
        """
        selected_model = model or self.config.models.default_model
        request_timeout = timeout if timeout is not None else 120
        provider = self.config.models.provider
        if provider == "lm_studio":
            ensure_lm_studio_model_loaded(selected_model, self.config.models.idle_ttl_seconds)
            base_url = resolve_lm_studio_base_url(self.config)
            return self._chat_json_openai(
                _fold_cache_prefix(messages, cache_prefix),
                schema, selected_model, base_url, request_timeout,
            )
        if provider == "ollama":
            return self._chat_json_ollama(
                _fold_cache_prefix(messages, cache_prefix),
                schema, selected_model, request_timeout,
            )
        if provider == "openrouter":
            return self._chat_json_openrouter(
                messages, schema, selected_model, request_timeout,
                cache_prefix=cache_prefix,
            )
        raise ValueError(f"Unsupported model provider: {provider}")

    def _chat_json_openai(
        self,
        messages: list[ChatMessage],
        schema: dict,
        selected_model: str,
        base_url: str,
        request_timeout: float,
    ) -> dict:
        # Strip the dataclass's `cache_control` field — LM Studio expects
        # only {role, content} and some strict implementations 4xx on the
        # unknown key. Use _serialize_message with explicit_cache=False
        # so we always emit plain-string content for local providers.
        payload = {
            "model": selected_model,
            "messages": [
                _serialize_message(m, explicit_cache=False) for m in messages
            ],
            "temperature": self.config.models.temperature,
            "response_format": {"type": "json_schema", "json_schema": schema},
            "ttl": self.config.models.idle_ttl_seconds,
        }
        url = base_url.rstrip("/") + "/chat/completions"
        with httpx.Client(timeout=request_timeout) as client:
            response = client.post(url, json=payload)
            response.raise_for_status()
            message = response.json()["choices"][0]["message"]
        # Thinking-mode models (Qwen3.5/3.6, nemotron, crow distill) emit the
        # constrained JSON into `reasoning_content` instead of `content`.
        # LM Studio passes both through. Prefer `content`; fall back to
        # `reasoning_content` so the production pipeline doesn't fail with
        # `Expecting value: line 1 column 1 (char 0)` on these models.
        content = message.get("content") or message.get("reasoning_content") or ""
        # v0.2.10: same fence/comma resilience as the OpenRouter path.
        return _parse_llm_json(content)

    def _chat_json_openrouter(
        self,
        messages: list[ChatMessage],
        schema: dict,
        selected_model: str,
        request_timeout: float,
        *,
        cache_prefix: str | None = None,
    ) -> dict:
        """Cloud provider: same OpenAI-compatible shape as LM Studio, but
        the user supplies an API key (env var) and the transcript leaves
        the machine. OpenRouter is a multi-model gateway — the model id
        is what selects between Claude, GPT, Gemini, etc.

        We use `response_format: json_object` rather than `json_schema`
        because OpenRouter's underlying model menu has mixed schema
        support; the prompts already include explicit JSON-shape hints.
        """
        # Accept either the configured env var name (default OPENROUTER) or
        # the legacy OPENROUTER_API_KEY name so older .env.local files still
        # work without a forced rename.
        configured_var = self.config.models.openrouter_api_key_env
        api_key = (
            os.environ.get(configured_var, "").strip()
            or os.environ.get("OPENROUTER_API_KEY", "").strip()
        )
        if not api_key:
            raise RuntimeError(
                f"OpenRouter API key not set. Add `{configured_var}=sk-or-v1-...` "
                "to .env.local at the repo root (the file is gitignored), or "
                "switch Settings → Provider back to LM Studio / Ollama for "
                "fully-local inference."
            )
        # Inject a strict JSON-only system hint so the response is parseable
        # without depending on provider-side json_schema enforcement.
        schema_hint = json.dumps(schema.get("schema", schema), separators=(",", ":"))
        schema_hint_message = ChatMessage(
            "system",
            "Respond with valid JSON that matches this schema. Do not "
            "wrap in markdown fences. Schema: " + schema_hint,
        )
        # Message ordering matters for prompt-cache hits. When a cache
        # prefix is supplied we structure as:
        #   1. Stable preamble + transcript (cache breakpoint)
        #   2. Schema hint + caller's existing messages (varies per call)
        # Anthropic / Qwen explicit-cache providers see the breakpoint
        # via the `cache_control` marker on the prefix block; auto-cache
        # providers fire on the byte-identical prefix without markers.
        # Without a cache_prefix we preserve the prior structure exactly
        # so existing callers keep their schema-first ordering.
        if cache_prefix:
            prefix_message = ChatMessage(
                "user",
                _CACHE_PREAMBLE + "\n\n" + cache_prefix,
                cache_control=True,
            )
            augmented = [prefix_message, schema_hint_message, *messages]
        else:
            augmented = [schema_hint_message, *messages]
        # Cache-aware serialization. For Anthropic-style explicit-cache
        # providers, the cache_control flag on a ChatMessage becomes a
        # structured-content block with `{"cache_control": "ephemeral"}`.
        # For auto-cache providers we keep plain strings — their caches
        # fire on prefix matches without markers, but the markers cost
        # nothing if we accidentally send them (OpenRouter normalizes).
        explicit_cache = _supports_explicit_cache_markers(selected_model)
        serialized_messages = [
            _serialize_message(m, explicit_cache=explicit_cache) for m in augmented
        ]
        base_payload: dict = {
            "model": selected_model,
            "messages": serialized_messages,
            "temperature": self.config.models.temperature,
        }
        url = self.config.models.openrouter_base_url.rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/natebrockert/meeting_mind",
            "X-Title": "MeetingMind",
        }
        # Try strict json_object mode first. OpenRouter's catalogue is mixed —
        # some models (Anthropic, OpenAI) honour response_format, others
        # (free-tier, Tencent etc.) return 400 "Json mode is not supported".
        # On a 400 we retry without the constraint and rely on the system
        # prompt's "respond with valid JSON" hint plus the fence-stripping
        # post-process to keep parsing reliable.
        # Track whether json-mode worked on the first call so the retry
        # below doesn't pay another 400 for the same model.
        json_mode_supported = True
        with httpx.Client(timeout=request_timeout) as client:
            strict_payload = {**base_payload, "response_format": {"type": "json_object"}}
            response = client.post(url, json=strict_payload, headers=headers)
            if response.status_code == 400 and "json mode" in response.text.lower():
                json_mode_supported = False
                response = client.post(url, json=base_payload, headers=headers)
            response.raise_for_status()
            message = response.json()["choices"][0]["message"]
        content = message.get("content") or message.get("reasoning_content") or ""
        # v0.2.10: route through `_parse_llm_json` for fence-strip +
        # substring-extract + trailing-comma repair. If those all fail,
        # do ONE retry with a more forceful system prompt before giving up.
        # Audit fix (round 2): retry skips strict json-mode if the
        # first call already fell back, avoiding a duplicate 400.
        try:
            return _parse_llm_json(content)
        except json.JSONDecodeError as first_error:
            corrective_message = ChatMessage(
                "system",
                "Your previous response was not valid JSON: "
                f"{first_error.msg}. Respond again with ONLY a single "
                "valid JSON object. No markdown fences, no explanation, "
                "no leading or trailing text. Schema: " + schema_hint,
            )
            # Preserve the cacheable prefix message so the retry still
            # hits the warm prompt cache (and so the retry sees the same
            # transcript as the first attempt — without it the model
            # would be reasoning over a shorter, schema-only prompt).
            if cache_prefix:
                retry_messages = [
                    augmented[0],  # prefix_message with cache_control=True
                    corrective_message,
                    *messages,
                ]
            else:
                retry_messages = [corrective_message, *messages]
            retry_payload: dict = {
                **base_payload,
                "messages": [
                    _serialize_message(m, explicit_cache=explicit_cache)
                    for m in retry_messages
                ],
            }
            if json_mode_supported:
                retry_payload["response_format"] = {"type": "json_object"}
            with httpx.Client(timeout=request_timeout) as client:
                retry_response = client.post(url, json=retry_payload, headers=headers)
                retry_response.raise_for_status()
                retry_message = retry_response.json()["choices"][0]["message"]
            retry_content = (
                retry_message.get("content") or retry_message.get("reasoning_content") or ""
            )
            try:
                return _parse_llm_json(retry_content)
            except json.JSONDecodeError as retry_error:
                raise RuntimeError(
                    f"OpenRouter returned invalid JSON after one retry. "
                    f"Original error: {first_error.msg}. Retry error: "
                    f"{retry_error.msg}. First 200 chars of last reply: "
                    f"{retry_content[:200]!r}"
                ) from retry_error

    def _chat_json_ollama(
        self,
        messages: list[ChatMessage],
        schema: dict,
        selected_model: str,
        request_timeout: float,
    ) -> dict:
        # Strip the dataclass's `cache_control` field — Ollama's chat
        # endpoint expects only {role, content}. Use _serialize_message
        # with explicit_cache=False so we always emit plain-string content
        # for local providers.
        payload = {
            "model": selected_model,
            "messages": [
                _serialize_message(m, explicit_cache=False) for m in messages
            ],
            "format": schema.get("schema", schema),
            "stream": False,
            "options": {"temperature": self.config.models.temperature},
            "keep_alive": f"{self.config.models.idle_ttl_seconds}s",
        }
        url = self.config.models.ollama_base_url.rstrip("/") + "/api/chat"
        with httpx.Client(timeout=request_timeout) as client:
            response = client.post(url, json=payload)
            response.raise_for_status()
            content = response.json()["message"]["content"]
        # v0.2.10: same resilience as the OpenRouter path. Ollama's local
        # models are usually well-behaved but a quantized fallthrough can
        # still emit fences or trailing commas.
        return _parse_llm_json(content)

    def unload(self, model: str | None = None) -> None:
        """Best-effort unload for LM Studio models after a run or manual maintenance.

        Audit M-A: pass model through `_validate_model_name` and use the
        `--` flag terminator so a malicious model name configured in
        settings can't smuggle CLI flags into `lms unload` (e.g. an
        unintended `--all`).
        """
        selected_model = model or self.config.models.default_model
        try:
            _validate_model_name(selected_model)
        except ValueError:
            return
        # Drop the cached "loaded" state so the next chat_json() re-verifies.
        _loaded_model_cache.pop(selected_model, None)
        subprocess.run(
            ["lms", "unload", "--", selected_model],
            check=False,
            capture_output=True,
            text=True,
        )


def _server_responds(base_url: str) -> bool:
    try:
        response = httpx.get(base_url.rstrip("/") + "/models", timeout=2)
        return response.status_code == 200
    except httpx.HTTPError:
        return False


def _run_lms(args: list[str], timeout: int = 20) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["lms", *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(["lms", *args], returncode=1, stdout="", stderr=str(exc))


def _parse_lm_studio_port(text: str) -> str | None:
    match = re.search(r"port\s+(\d+)", text)
    return match.group(1) if match else None
