from src.core.symbols import sanitize_symbol


def test_sanitize_symbol_rejects_unicode_and_spaces() -> None:
    assert sanitize_symbol("币安人生USDT") is None
    assert sanitize_symbol(" BTCUSDT ") == "BTCUSDT"
    assert sanitize_symbol("btc usdt") is None
    assert sanitize_symbol("BTCUSDT!") is None
    assert sanitize_symbol("A") is None
