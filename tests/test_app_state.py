from __future__ import annotations

from pathlib import Path

from src.gui.models.app_state import AppState


def test_app_state_load_and_save_roundtrip(tmp_path: Path) -> None:
    config_path = tmp_path / "config.user.yaml"
    defaults = AppState(env="TEST", log_level="INFO", config_path="config.json")

    loaded = AppState.load(config_path, defaults)
    assert loaded.user_config_path == config_path
    assert loaded.env == "TEST"

    loaded.default_period = "1h"
    loaded.save(config_path)

    reloaded = AppState.load(config_path, defaults)
    assert reloaded.default_period == "1h"
