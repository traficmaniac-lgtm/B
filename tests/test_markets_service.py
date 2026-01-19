from __future__ import annotations

from pathlib import Path

from src.core.models import Pair
from src.services.markets_service import MarketsService


class StubClient:
    def __init__(self) -> None:
        self.called = False

    def get_exchange_info(self) -> dict[str, object]:
        self.called = True
        return {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "baseAsset": "BTC",
                    "quoteAsset": "USDT",
                    "status": "TRADING",
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                        {"filterType": "LOT_SIZE", "stepSize": "0.001"},
                    ],
                },
                {
                    "symbol": "ETHBUSD",
                    "baseAsset": "ETH",
                    "quoteAsset": "BUSD",
                    "status": "TRADING",
                    "filters": [],
                },
                {
                    "symbol": "XRPUSDT",
                    "baseAsset": "XRP",
                    "quoteAsset": "USDT",
                    "status": "BREAK",
                    "filters": [],
                },
                {
                    "symbol": "BTCUPUSDT",
                    "baseAsset": "BTC",
                    "quoteAsset": "USDT",
                    "status": "TRADING",
                    "filters": [],
                },
            ]
        }


def test_load_pairs_filters_quote_status_and_blacklist() -> None:
    client = StubClient()
    service = MarketsService(client)

    pairs = service.load_pairs("usdt")

    assert client.called is True
    assert all(isinstance(pair, Pair) for pair in pairs)
    assert [pair.symbol for pair in pairs] == ["BTCUSDT"]
    assert pairs[0].tick_size == "0.01"
    assert pairs[0].step_size == "0.001"


def test_load_pairs_cached_uses_cache(tmp_path: Path) -> None:
    client = StubClient()
    service = MarketsService(client, cache_dir=tmp_path)
    service.refresh_pairs("USDT")

    cached_client = StubClient()
    cached_service = MarketsService(cached_client, cache_dir=tmp_path)
    pairs, from_cache = cached_service.load_pairs_cached("USDT")

    assert from_cache is True
    assert cached_client.called is False
    assert [pair.symbol for pair in pairs] == ["BTCUSDT"]


def test_load_pairs_cached_ttl_zero_forces_refresh(tmp_path: Path) -> None:
    client = StubClient()
    service = MarketsService(client, cache_dir=tmp_path)
    service.refresh_pairs("USDT")

    refresh_client = StubClient()
    refresh_service = MarketsService(refresh_client, cache_dir=tmp_path)
    pairs, from_cache = refresh_service.load_pairs_cached("USDT", ttl_hours=0)

    assert refresh_client.called is True
    assert from_cache is False
    assert [pair.symbol for pair in pairs] == ["BTCUSDT"]
