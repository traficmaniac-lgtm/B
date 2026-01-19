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
    binance_api_key: str = ""
    binance_api_secret: str = ""
    openai_api_key: str = ""
    default_period: str = "4h"
    default_quality: str = "Standard"
    allow_ai_more_data: bool = True
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
        binance_api_key = str(data.get("binance_api_key", defaults.binance_api_key) or "")
        binance_api_secret = str(data.get("binance_api_secret", defaults.binance_api_secret) or "")
        openai_api_key = str(data.get("openai_api_key", defaults.openai_api_key) or "")
        default_period = str(data.get("default_period", defaults.default_period))
        default_quality = str(data.get("default_quality", defaults.default_quality))
        allow_ai_more_data = bool(data.get("allow_ai_more_data", defaults.allow_ai_more_data))
        return cls(
            env=env,
            log_level=log_level,
            config_path=config_path,
            show_logs=show_logs,
            binance_api_key=binance_api_key,
            binance_api_secret=binance_api_secret,
            openai_api_key=openai_api_key,
            default_period=default_period,
            default_quality=default_quality,
            allow_ai_more_data=allow_ai_more_data,
            user_config_path=path,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "env": self.env,
            "log_level": self.log_level,
            "config_path": self.config_path,
            "show_logs": self.show_logs,
            "binance_api_key": self.binance_api_key,
            "binance_api_secret": self.binance_api_secret,
            "openai_api_key": self.openai_api_key,
            "default_period": self.default_period,
            "default_quality": self.default_quality,
            "allow_ai_more_data": self.allow_ai_more_data,
        }

    def save(self, path: Path) -> None:
        path.write_text(yaml.safe_dump(self.to_dict(), sort_keys=False), encoding="utf-8")
        self.user_config_path = path
