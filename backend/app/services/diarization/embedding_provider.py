from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from app.config import AppConfig


@lru_cache(maxsize=4)
def _load_inference(model_name: str, token: str | None, device: str | None) -> object:
    """Load the pyannote embedding `Inference` once per (model, token, device).

    Without this cache every speaker approve triggered a fresh
    `Model.from_pretrained(...)` — ~3-5 seconds of disk read + Lightning
    checkpoint validation + a transient memory spike. Caching cuts that to
    a one-time cost per process and keeps subsequent approves ~50ms.

    Keyed on (model_name, token, device) so a config swap or device move
    still gets a clean reload. lru_cache size 4 is enough headroom for
    realistic tier swaps.
    """
    import torch
    from pyannote.audio import Inference, Model

    model = Model.from_pretrained(model_name, token=token)
    if device:
        model.to(torch.device(device))
    return Inference(model, window="whole")


def infer_pyannote_embedding(config: AppConfig, clip_path: Path, token: str) -> object:
    import torch

    resolved_device = _resolve_torch_device(config.diarization.device, torch)
    inference = _load_inference(
        config.diarization.embedding_model_name,
        token or None,
        resolved_device,
    )
    return inference(str(clip_path))


def _resolve_torch_device(device: str, torch_module) -> str | None:
    if device == "auto":
        if torch_module.backends.mps.is_available():
            return "mps"
        if torch_module.cuda.is_available():
            return "cuda"
        return "cpu"
    if device == "none":
        return None
    return device
