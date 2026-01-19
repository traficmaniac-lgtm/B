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

    def load_pairs(self, quote_asset: str) -> list[Pair]:
        data = self._client.get_exchange_info()
        symbols = data.get("symbols", []) if isinstance(data, dict) else []
        quote = quote_asset.upper()
        pairs: list[Pair] = []

        for item in symbols:
            if not isinstance(item, dict):
                continue
            if str(item.get("status", "")).upper() != "TRADING":
                continue
            if str(item.get("quoteAsset", "")).upper() != quote:
                continue
            symbol = str(item.get("symbol", ""))
            if not symbol:
                continue
            if any(substring in symbol for substring in self._blacklist_substrings):
                continue
            base_asset = str(item.get("baseAsset", ""))
            filters = self._map_filters(item.get("filters", []))
            pairs.append(
                Pair(
                    symbol=symbol,
                    base_asset=base_asset,
                    quote_asset=quote,
                    status=str(item.get("status", "TRADING")),
                    filters=filters,
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
