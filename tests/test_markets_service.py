from __future__ import annotations

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
                    "filters": [],
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
