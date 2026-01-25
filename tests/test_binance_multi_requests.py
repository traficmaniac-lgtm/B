import httpx

from src.binance.account_client import BinanceAccountClient
from src.binance.http_client import BinanceHttpClient


def test_open_orders_multi_omits_symbol() -> None:
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    account = BinanceAccountClient(
        base_url="https://api.binance.com",
        api_key="key",
        api_secret="secret",
        client=client,
    )
    account.get_open_orders("MULTI")
    assert seen_urls, "expected request to be sent"
    assert "symbol=MULTI" not in seen_urls[0]


def test_exchange_info_multi_uses_all_symbols() -> None:
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, json={"symbols": []})

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    http_client = BinanceHttpClient(base_url="https://api.binance.com", client=client)
    http_client.get_exchange_info_symbol("MULTI")
    assert seen_urls, "expected request to be sent"
    assert seen_urls[0].endswith("/api/v3/exchangeInfo")
