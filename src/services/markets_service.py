from __future__ import annotations

from collections.abc import Iterable

from src.binance.http_client import BinanceHttpClient
from src.core.models import Pair

DEFAULT_BLACKLIST_SUBSTRINGS = ("UP", "DOWN", "BULL", "BEAR", "3L", "3S")


class MarketsService:
    def __init__(
        self,
        client: BinanceHttpClient,
        blacklist_substrings: Iterable[str] | None = None,
    ) -> None:
        self._client = client
        self._blacklist_substrings = tuple(
            substring.upper()
            for substring in (blacklist_substrings or DEFAULT_BLACKLIST_SUBSTRINGS)
        )

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
