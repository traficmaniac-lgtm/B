from __future__ import annotations

from dataclasses import dataclass, field
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
    openai_model: str = "gpt-4o-mini"
    default_period: str = "4h"
    default_quality: str = "Standard"
    price_ttl_ms: int = 2500
    price_refresh_ms: int = 500
    default_quote: str = "USDT"
    pnl_period: str = "24h"
    zero_fee_symbols: list[str] = field(default_factory=lambda: ["USDTUSDC", "EURIUSDT"])
    nc_micro_legacy_policy: str = "CANCEL"
    nc_micro_profit_guard_mode: str = "BLOCK"
    ai_connected: bool = False
    ai_checked: bool = False
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
        openai_model = str(data.get("openai_model", defaults.openai_model) or defaults.openai_model)
        default_period = str(data.get("default_period", defaults.default_period))
        default_quality = str(data.get("default_quality", defaults.default_quality))
        price_ttl_ms = int(data.get("price_ttl_ms", defaults.price_ttl_ms))
        price_refresh_ms = int(data.get("price_refresh_ms", defaults.price_refresh_ms))
        default_quote = str(data.get("default_quote", defaults.default_quote)).upper()
        pnl_period = str(data.get("pnl_period", defaults.pnl_period))
        zero_fee_symbols = _parse_symbol_list(data.get("zero_fee_symbols", defaults.zero_fee_symbols))
        nc_micro_legacy_policy = str(
            data.get("nc_micro_legacy_policy", defaults.nc_micro_legacy_policy)
        ).upper()
        nc_micro_profit_guard_mode = str(
            data.get("nc_micro_profit_guard_mode", defaults.nc_micro_profit_guard_mode)
        ).upper()
        return cls(
            env=env,
            log_level=log_level,
            config_path=config_path,
            show_logs=show_logs,
            binance_api_key=binance_api_key,
            binance_api_secret=binance_api_secret,
            openai_api_key=openai_api_key,
            openai_model=openai_model,
            default_period=default_period,
            default_quality=default_quality,
            price_ttl_ms=price_ttl_ms,
            price_refresh_ms=price_refresh_ms,
            default_quote=default_quote,
            pnl_period=pnl_period,
            zero_fee_symbols=zero_fee_symbols,
            nc_micro_legacy_policy=nc_micro_legacy_policy,
            nc_micro_profit_guard_mode=nc_micro_profit_guard_mode,
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
            "openai_model": self.openai_model,
            "default_period": self.default_period,
            "default_quality": self.default_quality,
            "price_ttl_ms": self.price_ttl_ms,
            "price_refresh_ms": self.price_refresh_ms,
            "default_quote": self.default_quote,
            "pnl_period": self.pnl_period,
            "zero_fee_symbols": list(self.zero_fee_symbols),
            "nc_micro_legacy_policy": self.nc_micro_legacy_policy,
            "nc_micro_profit_guard_mode": self.nc_micro_profit_guard_mode,
        }

    def save(self, path: Path) -> None:
        path.write_text(yaml.safe_dump(self.to_dict(), sort_keys=False), encoding="utf-8")
        self.user_config_path = path

    @property
    def openai_key_present(self) -> bool:
        return bool(self.openai_api_key.strip())

    def get_binance_keys(self) -> tuple[str, str]:
        return self.binance_api_key.strip(), self.binance_api_secret.strip()


def _parse_symbol_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip().upper() for item in value if str(item).strip()]
    if isinstance(value, str):
        parts = [item.strip().upper() for item in value.replace("\n", ",").split(",")]
        return [item for item in parts if item]
    return []
