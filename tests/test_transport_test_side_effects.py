from __future__ import annotations

from src.core.config import Config
from src.services.price_feed_manager import PriceFeedManager


def test_transport_test_preserves_active_subscription() -> None:
    manager = PriceFeedManager(Config())
    symbol = "BTCUSDT"
    manager.register_symbol(symbol)
    before_count = manager._registry.count(symbol)
    report = manager.run_transport_test([symbol])
    after_count = manager._registry.count(symbol)

    assert report
    assert before_count == after_count == 1
