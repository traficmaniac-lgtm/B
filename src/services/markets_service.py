from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any

from src.binance.http_client import BinanceHttpClient
from src.core.logging import get_logger
from src.core.models import Pair

DEFAULT_BLACKLIST_SUBSTRINGS = ("UP", "DOWN", "BULL", "BEAR", "3L", "3S")


class MarketsService:
    def __init__(
        self,
        client: BinanceHttpClient,
        blacklist_substrings: Iterable[str] | None = None,
        cache_dir: Path | None = None,
    ) -> None:
        self._client = client
        self._logger = get_logger("services.markets")
        self._blacklist_substrings = tuple(
            substring.upper()
            for substring in (blacklist_substrings or DEFAULT_BLACKLIST_SUBSTRINGS)
        )
        self._cache_dir = cache_dir or self._default_cache_dir()
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def load_pairs(
        self,
        quote_asset: str,
        status: str = "TRADING",
        blacklist_substrings: Iterable[str] | None = None,
    ) -> list[Pair]:
        data = self._client.get_exchange_info()
        symbols = data.get("symbols", []) if isinstance(data, dict) else []
        quote = quote_asset.upper()
        status_filter = status.upper()
        blacklist = (
            tuple(substring.upper() for substring in blacklist_substrings)
            if blacklist_substrings is not None
            else self._blacklist_substrings
        )
        pairs: list[Pair] = []

        for item in symbols:
            if not isinstance(item, dict):
                continue
            if str(item.get("status", "")).upper() != status_filter:
                continue
            if str(item.get("quoteAsset", "")).upper() != quote:
                continue
            symbol = str(item.get("symbol", ""))
            if not symbol:
                continue
            if any(substring in symbol for substring in blacklist):
                continue
            base_asset = str(item.get("baseAsset", ""))
            filters = self._map_filters(item.get("filters", []))
            tick_size = self._extract_filter_value(filters, "PRICE_FILTER", "tickSize")
            step_size = self._extract_filter_value(filters, "LOT_SIZE", "stepSize")
            pairs.append(
                Pair(
                    symbol=symbol,
                    base_asset=base_asset,
                    quote_asset=quote,
                    status=str(item.get("status", "TRADING")),
                    filters=filters,
                    tick_size=tick_size,
                    step_size=step_size,
                )
            )

        return pairs

    def load_pairs_all(
        self,
        status: str = "TRADING",
        blacklist_substrings: Iterable[str] | None = None,
    ) -> list[Pair]:
        data = self._client.get_exchange_info()
        symbols = data.get("symbols", []) if isinstance(data, dict) else []
        status_filter = status.upper()
        blacklist = (
            tuple(substring.upper() for substring in blacklist_substrings)
            if blacklist_substrings is not None
            else self._blacklist_substrings
        )
        pairs: list[Pair] = []

        for item in symbols:
            if not isinstance(item, dict):
                continue
            if str(item.get("status", "")).upper() != status_filter:
                continue
            symbol = str(item.get("symbol", ""))
            if not symbol:
                continue
            if any(substring in symbol for substring in blacklist):
                continue
            quote_asset = str(item.get("quoteAsset", "")).upper()
            if not quote_asset:
                continue
            base_asset = str(item.get("baseAsset", ""))
            filters = self._map_filters(item.get("filters", []))
            tick_size = self._extract_filter_value(filters, "PRICE_FILTER", "tickSize")
            step_size = self._extract_filter_value(filters, "LOT_SIZE", "stepSize")
            pairs.append(
                Pair(
                    symbol=symbol,
                    base_asset=base_asset,
                    quote_asset=quote_asset,
                    status=str(item.get("status", "TRADING")),
                    filters=filters,
                    tick_size=tick_size,
                    step_size=step_size,
                )
            )

        return pairs

    def load_pairs_cached(self, quote_asset: str, ttl_hours: int = 24) -> tuple[list[Pair], bool]:
        cached = self._read_cache(quote_asset)
        if cached is not None:
            saved_at, cached_pairs, _ = cached
            if self._cache_is_fresh(saved_at, ttl_hours):
                return cached_pairs, True
        try:
            pairs = self.refresh_pairs(quote_asset)
            return pairs, False
        except Exception as exc:
            if cached is not None:
                self._logger.warning("Failed to refresh pairs (%s); using cache.", exc)
                return cached[1], True
            raise

    def refresh_pairs(self, quote_asset: str) -> list[Pair]:
        pairs = self.load_pairs(quote_asset)
        self._write_cache(quote_asset, pairs, scope=f"QUOTE:{quote_asset.strip().upper()}")
        return pairs

    def load_pairs_cached_all(self, ttl_hours: int = 24) -> tuple[list[Pair], bool, bool]:
        cached = self._read_cache("ALL")
        if cached is not None:
            saved_at, cached_pairs, meta = cached
            scope = meta.get("scope") if isinstance(meta, dict) else None
            partial = scope != "ALL"
            if self._cache_is_fresh(saved_at, ttl_hours):
                return cached_pairs, True, partial
        try:
            pairs = self.refresh_pairs_all()
            return pairs, False, False
        except Exception as exc:
            if cached is not None:
                self._logger.warning("Failed to refresh all pairs (%s); using cache.", exc)
                return cached[1], True, True
            raise

    def refresh_pairs_all(self) -> list[Pair]:
        pairs = self.load_pairs_all()
        self._write_cache("ALL", pairs, scope="ALL")
        return pairs

    @staticmethod
    def _default_cache_dir() -> Path:
        return Path(__file__).resolve().parents[2] / "data"

    def _cache_path(self, quote_asset: str) -> Path:
        sanitized = quote_asset.strip().upper()
        return self._cache_dir / f"exchange_info_{sanitized}.json"

    @staticmethod
    def _cache_is_fresh(saved_at: datetime | None, ttl_hours: int) -> bool:
        if saved_at is None:
            return False
        if ttl_hours <= 0:
            return False
        return datetime.now(timezone.utc) - saved_at <= timedelta(hours=ttl_hours)

    def _read_cache(self, quote_asset: str) -> tuple[datetime | None, list[Pair], dict[str, Any]] | None:
        path = self._cache_path(quote_asset)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            self._logger.warning("Failed to parse cache %s: %s", path, exc)
            return None
        if not isinstance(payload, dict):
            return None
        meta = payload.get("meta")
        saved_at = None
        if isinstance(meta, dict):
            saved_at_raw = meta.get("saved_at")
            if isinstance(saved_at_raw, str):
                try:
                    saved_at = datetime.fromisoformat(saved_at_raw)
                except ValueError:
                    saved_at = None
        pairs_payload = payload.get("pairs")
        if not isinstance(pairs_payload, list):
            return None
        pairs = [pair for pair in (self._payload_to_pair(item) for item in pairs_payload) if pair]
        return saved_at, pairs, meta if isinstance(meta, dict) else {}

    def _write_cache(self, quote_asset: str, pairs: list[Pair], scope: str) -> None:
        path = self._cache_path(quote_asset)
        meta = {
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "quote": quote_asset.strip().upper(),
            "count": len(pairs),
            "scope": scope,
        }
        payload = {
            "meta": meta,
            "pairs": [self._pair_to_payload(pair) for pair in pairs],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _pair_to_payload(pair: Pair) -> dict[str, Any]:
        return {
            "symbol": pair.symbol,
            "base_asset": pair.base_asset,
            "quote_asset": pair.quote_asset,
            "status": pair.status,
            "filters": dict(pair.filters),
            "tick_size": pair.tick_size,
            "step_size": pair.step_size,
        }

    @staticmethod
    def _payload_to_pair(payload: object) -> Pair | None:
        if not isinstance(payload, dict):
            return None
        symbol = payload.get("symbol")
        base_asset = payload.get("base_asset")
        quote_asset = payload.get("quote_asset")
        if not all(isinstance(value, str) and value for value in (symbol, base_asset, quote_asset)):
            return None
        status = payload.get("status")
        filters = payload.get("filters")
        tick_size = payload.get("tick_size")
        step_size = payload.get("step_size")
        return Pair(
            symbol=symbol,
            base_asset=base_asset,
            quote_asset=quote_asset,
            status=str(status) if status else "TRADING",
            filters=filters if isinstance(filters, dict) else {},
            tick_size=str(tick_size) if isinstance(tick_size, str) else None,
            step_size=str(step_size) if isinstance(step_size, str) else None,
        )

    @staticmethod
    def _map_filters(filters: object) -> dict[str, object]:
        if not isinstance(filters, list):
            return {}
        mapped: dict[str, object] = {}
        for entry in filters:
            if not isinstance(entry, dict):
                continue
            filter_type = entry.get("filterType")
            if isinstance(filter_type, str):
                mapped[filter_type] = entry
        return mapped

    @staticmethod
    def _extract_filter_value(
        filters: dict[str, object],
        filter_type: str,
        key: str,
    ) -> str | None:
        payload = filters.get(filter_type)
        if not isinstance(payload, dict):
            return None
        value = payload.get(key)
        if isinstance(value, str):
            return value
        return None
