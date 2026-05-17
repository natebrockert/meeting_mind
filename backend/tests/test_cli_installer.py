from __future__ import annotations

from app.cli import _merge_env_file_text, _selected_services


def test_merge_env_file_text_preserves_comments_and_updates_values() -> None:
    existing = (
        "# Local secrets\n"
        "HUGGING_FACE_HUB_TOKEN=old-token\n"
        "OTHER_VALUE=keep-me\n"
    )

    merged = _merge_env_file_text(
        existing,
        {
            "HUGGING_FACE_HUB_TOKEN": "new-token",
            "ADDED_VALUE": "created",
        },
    )

    assert "# Local secrets" in merged
    assert "HUGGING_FACE_HUB_TOKEN=new-token" in merged
    assert "OTHER_VALUE=keep-me" in merged
    assert "ADDED_VALUE=created" in merged
    assert "old-token" not in merged


def test_merge_env_file_text_appends_missing_trailing_newline() -> None:
    merged = _merge_env_file_text("EXISTING=1", {"NEW_VALUE": "2"})

    assert merged.endswith("\n")
    assert merged.splitlines() == ["EXISTING=1", "NEW_VALUE=2"]


def test_selected_services_defaults_to_backend_and_frontend() -> None:
    assert _selected_services(backend=False, frontend=False) == ("backend", "frontend")


def test_selected_services_can_target_one_service() -> None:
    assert _selected_services(backend=True, frontend=False) == ("backend",)
    assert _selected_services(backend=False, frontend=True) == ("frontend",)
