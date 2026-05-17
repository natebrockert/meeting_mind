"""A/B harness for LLM extraction across local models.

Runs the same transcript through each candidate model via the existing
ModelBus + extraction prompts, captures atoms + timing, and writes a
side-by-side comparison.

Usage:
    uv run python scripts/ab/run_ab.py [--meeting-id 3]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from copy import deepcopy
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.config import load_config  # noqa: E402
from app.services.extraction import (  # noqa: E402
    MeetingAtoms,
    TEMPLATE_PROMPTS,
    _consolidate_workstreams,
    _template_for_meeting,
    chunk_transcript,
)
from app.services.model_bus import (  # noqa: E402
    ChatMessage,
    ModelBus,
    ensure_lm_studio_model_loaded,
    resolve_lm_studio_base_url,
)
import httpx  # noqa: E402


def _chat_json_with_reasoning_fallback(
    bus: ModelBus, messages, schema_payload, *, timeout: float = 180
) -> dict:
    """LM Studio call that recovers the JSON from `reasoning_content` for
    qwen/crow-style thinking models that ignore the `content` channel.
    """
    cfg = bus.config
    model = cfg.models.default_model
    ensure_lm_studio_model_loaded(model, cfg.models.idle_ttl_seconds)
    base_url = resolve_lm_studio_base_url(cfg)
    payload = {
        "model": model,
        "messages": [m.__dict__ for m in messages],
        "temperature": cfg.models.temperature,
        "response_format": {"type": "json_schema", "json_schema": schema_payload},
        "ttl": cfg.models.idle_ttl_seconds,
    }
    with httpx.Client(timeout=timeout) as client:
        response = client.post(
            base_url.rstrip("/") + "/chat/completions", json=payload
        )
        response.raise_for_status()
        body = response.json()
    message = body["choices"][0]["message"]
    content = message.get("content") or message.get("reasoning_content") or ""
    return json.loads(content)

CANDIDATES = [
    {
        "label": "baseline-gemma-e4b",
        "default": "gemma-4-e4b-it@q8_0",
        "quality": "gemma-4-e4b-it@q8_0",
        "note": "Higher-quality baseline (q8 of current default e4b)",
    },
    {
        "label": "qwen-27b-dense",
        "default": "qwen3.6-27b@q8_0",
        "quality": "qwen3.6-27b@q8_0",
        "note": "Big dense 27B at q8",
    },
    {
        "label": "qwen-35b-moe-mlx",
        "default": "qwen3.6-35b-a3b-ud-mlx",
        "quality": "qwen3.6-35b-a3b-ud-mlx",
        "note": "35B MoE (~3B active) MLX-optimised",
    },
    {
        "label": "crow-9b-opus-distill",
        "default": "crow-9b-opus-4.6-distill-heretic_qwen3.5",
        "quality": "crow-9b-opus-4.6-distill-heretic_qwen3.5",
        "note": "9B Qwen3.5 distilled from Opus 4.6",
    },
    {
        "label": "nemotron-3-nano-omni",
        "default": "nvidia/nemotron-3-nano-omni",
        "quality": "nvidia/nemotron-3-nano-omni",
        "note": "NVIDIA Nemotron 3 Nano Omni (30B-A3B MoE)",
    },
]


def run_extraction(config, meeting_id: int) -> MeetingAtoms:
    chunks = chunk_transcript(config, meeting_id)
    if not chunks:
        return MeetingAtoms(suggested_title="Untitled", summary="No transcript.")
    bus = ModelBus(config)

    def _patched_openai(messages, schema, selected_model, base_url, request_timeout):
        payload = {
            "model": selected_model,
            "messages": [m.__dict__ for m in messages],
            "temperature": config.models.temperature,
            "response_format": {"type": "json_schema", "json_schema": schema},
            "ttl": config.models.idle_ttl_seconds,
        }
        with httpx.Client(timeout=request_timeout) as client:
            response = client.post(
                base_url.rstrip("/") + "/chat/completions", json=payload
            )
            response.raise_for_status()
            message = response.json()["choices"][0]["message"]
        content = message.get("content") or message.get("reasoning_content") or ""
        return json.loads(content)

    bus._chat_json_openai = _patched_openai  # type: ignore[assignment]
    schema = MeetingAtoms.model_json_schema()
    template = _template_for_meeting(config, meeting_id)
    system_prompt = TEMPLATE_PROMPTS[template]
    partials: list[MeetingAtoms] = []
    for chunk in chunks:
        payload = bus.chat_json(
            [
                ChatMessage("system", system_prompt),
                ChatMessage("user", chunk.text),
            ],
            {"name": "MeetingAtoms", "schema": schema},
        )
        partials.append(MeetingAtoms.model_validate(payload))
    merged = MeetingAtoms(
        suggested_title=partials[0].suggested_title,
        summary="\n".join(part.summary for part in partials if part.summary),
        actions=[a for p in partials for a in p.actions],
        decisions=[d for p in partials for d in p.decisions],
        workstreams=[w for p in partials for w in p.workstreams],
        open_questions=[q for p in partials for q in p.open_questions],
        uncertainties=[u for p in partials for u in p.uncertainties],
    )
    if len(merged.workstreams) > 4:
        merged.workstreams = _consolidate_workstreams(bus, merged.workstreams)
    return merged


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--meeting-id", type=int, default=3)
    parser.add_argument("--out", default="scripts/ab/results")
    args = parser.parse_args()

    base_config = load_config()
    out_dir = REPO_ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for candidate in CANDIDATES:
        print(f"\n=== {candidate['label']} ({candidate['default']}) ===", flush=True)
        cfg = deepcopy(base_config)
        cfg.models.default_model = candidate["default"]
        cfg.models.quality_model = candidate["quality"]
        start = time.monotonic()
        try:
            atoms = run_extraction(cfg, args.meeting_id)
            elapsed = time.monotonic() - start
            payload = atoms.model_dump()
            (out_dir / f"{candidate['label']}.json").write_text(
                json.dumps(payload, indent=2)
            )
            results.append(
                {
                    "label": candidate["label"],
                    "model": candidate["default"],
                    "note": candidate["note"],
                    "elapsed_seconds": round(elapsed, 1),
                    "summary_chars": len(atoms.summary),
                    "actions": len(atoms.actions),
                    "decisions": len(atoms.decisions),
                    "workstreams": len(atoms.workstreams),
                    "open_questions": len(atoms.open_questions),
                    "uncertainties": len(atoms.uncertainties),
                    "atoms": payload,
                }
            )
            print(
                f"  ok in {elapsed:.1f}s — actions={len(atoms.actions)} "
                f"decisions={len(atoms.decisions)} workstreams={len(atoms.workstreams)}",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001
            elapsed = time.monotonic() - start
            print(f"  FAILED in {elapsed:.1f}s: {exc}", flush=True)
            results.append(
                {
                    "label": candidate["label"],
                    "model": candidate["default"],
                    "note": candidate["note"],
                    "elapsed_seconds": round(elapsed, 1),
                    "error": str(exc),
                }
            )
        finally:
            try:
                ModelBus(cfg).unload(candidate["default"])
            except Exception:  # noqa: BLE001
                pass

    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(results, indent=2))
    print(f"\nWrote {summary_path}", flush=True)
    print("\n| label | model | sec | actions | decisions | workstreams |")
    print("|---|---|--:|--:|--:|--:|")
    for r in results:
        if "error" in r:
            print(f"| {r['label']} | {r['model']} | {r['elapsed_seconds']} | — | — | ERROR |")
        else:
            print(
                f"| {r['label']} | {r['model']} | {r['elapsed_seconds']} | "
                f"{r['actions']} | {r['decisions']} | {r['workstreams']} |"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
