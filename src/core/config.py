from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class AppConfig:
    env: str = "DEV"
    log_level: str = "INFO"


@dataclass(frozen=True)
class HttpConfig:
    timeout_s: int = 10
    retries: int = 3
    backoff_base_s: float = 0.5
    backoff_max_s: float = 10.0


@dataclass(frozen=True)
class RateLimitConfig:
    per_minute: int = 1200


@dataclass(frozen=True)
class PricesConfig:
    ttl_ms: int = 2000
    fallback_enabled: bool = True
    refresh_interval_ms: int = 500


@dataclass(frozen=True)
class BinanceConfig:
    base_url: str = "https://api.binance.com"
    ws_url: str = "wss://stream.binance.com:9443/ws"
    recv_window: int = 5000
    api_key: str | None = None
    api_secret: str | None = None


@dataclass(frozen=True)
class Config:
    app: AppConfig = field(default_factory=AppConfig)
    http: HttpConfig = field(default_factory=HttpConfig)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    prices: PricesConfig = field(default_factory=PricesConfig)
    binance: BinanceConfig = field(default_factory=BinanceConfig)

    def validate(self) -> None:
        if self.http.timeout_s <= 0:
            raise ValueError("http.timeout_s must be positive")
        if self.http.retries < 0:
            raise ValueError("http.retries must be zero or positive")
        if self.http.backoff_base_s <= 0:
            raise ValueError("http.backoff_base_s must be positive")
        if self.http.backoff_max_s < self.http.backoff_base_s:
            raise ValueError("http.backoff_max_s must be >= backoff_base_s")
        if self.rate_limit.per_minute <= 0:
            raise ValueError("rate_limit.per_minute must be positive")
        if self.prices.ttl_ms <= 0:
            raise ValueError("prices.ttl_ms must be positive")
        if self.prices.refresh_interval_ms <= 0:
            raise ValueError("prices.refresh_interval_ms must be positive")
        if self.binance.recv_window <= 0:
            raise ValueError("binance.recv_window must be positive")


def load_config(path: str | Path | None = None) -> Config:
    file_data: dict[str, object] = {}
    if path:
        config_path = Path(path)
        if config_path.exists():
            file_data = json.loads(config_path.read_text(encoding="utf-8"))

    app = AppConfig(
        env=_get_env("APP_ENV", _get_nested(file_data, ["app", "env"], "DEV")),
        log_level=_get_env("LOG_LEVEL", _get_nested(file_data, ["app", "log_level"], "INFO")),
    )

    http = HttpConfig(
        timeout_s=_get_env_int("HTTP_TIMEOUT", _get_nested(file_data, ["http", "timeout_s"], 10)),
        retries=_get_env_int("HTTP_RETRIES", _get_nested(file_data, ["http", "retries"], 3)),
        backoff_base_s=_get_env_float(
            "HTTP_BACKOFF_BASE",
            _get_nested(file_data, ["http", "backoff_base_s"], 0.5),
        ),
        backoff_max_s=_get_env_float(
            "HTTP_BACKOFF_MAX",
            _get_nested(file_data, ["http", "backoff_max_s"], 10.0),
        ),
    )

    rate_limit = RateLimitConfig(
        per_minute=_get_env_int(
            "RATE_LIMIT_PER_MIN",
            _get_nested(file_data, ["rate_limit", "per_minute"], 1200),
        )
    )

    prices = PricesConfig(
        ttl_ms=_get_env_int("PRICE_TTL_MS", _get_nested(file_data, ["prices", "ttl_ms"], 2000)),
        fallback_enabled=_get_env_bool(
            "PRICE_FALLBACK_ENABLED",
            _get_nested(file_data, ["prices", "fallback_enabled"], True),
        ),
        refresh_interval_ms=_get_env_int(
            "PRICE_REFRESH_MS",
            _get_nested(file_data, ["prices", "refresh_interval_ms"], 500),
        ),
    )

    binance = BinanceConfig(
        base_url=_get_env("BINANCE_BASE_URL", _get_nested(file_data, ["binance", "base_url"], BinanceConfig.base_url)),
        ws_url=_get_env("BINANCE_WS_URL", _get_nested(file_data, ["binance", "ws_url"], BinanceConfig.ws_url)),
        recv_window=_get_env_int(
            "BINANCE_RECV_WINDOW",
            _get_nested(file_data, ["binance", "recv_window"], BinanceConfig.recv_window),
        ),
        api_key=_get_env("BINANCE_API_KEY", _get_nested(file_data, ["binance", "api_key"], None)),
        api_secret=_get_env("BINANCE_API_SECRET", _get_nested(file_data, ["binance", "api_secret"], None)),
    )

    config = Config(app=app, http=http, rate_limit=rate_limit, prices=prices, binance=binance)
    config.validate()
    return config


def _get_nested(data: dict[str, object], keys: list[str], default: object) -> object:
    current: object = data
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def _get_env(name: str, default: object) -> object:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value


def _get_env_int(name: str, default: object) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return int(default)
    return int(value)


def _get_env_float(name: str, default: object) -> float:
    value = os.getenv(name)
    if value is None or value == "":
        return float(default)
    return float(value)


def _get_env_bool(name: str, default: object) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return bool(default)
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}
