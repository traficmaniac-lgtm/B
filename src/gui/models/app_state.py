from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class AppState:
    env: str
    log_level: str
    config_path: str
    show_logs: bool = True
    user_config_path: Path | None = None

    @classmethod
    def load(cls, path: Path, defaults: "AppState") -> "AppState":
        if not path.exists():
            defaults.user_config_path = path
            return defaults

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        env = str(data.get("env", defaults.env)).upper()
        log_level = str(data.get("log_level", defaults.log_level)).upper()
        config_path = str(data.get("config_path", defaults.config_path))
        show_logs = bool(data.get("show_logs", defaults.show_logs))
        return cls(
            env=env,
            log_level=log_level,
            config_path=config_path,
            show_logs=show_logs,
            user_config_path=path,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "env": self.env,
            "log_level": self.log_level,
            "config_path": self.config_path,
            "show_logs": self.show_logs,
        }

    def save(self, path: Path) -> None:
        path.write_text(yaml.safe_dump(self.to_dict(), sort_keys=False), encoding="utf-8")
        self.user_config_path = path
