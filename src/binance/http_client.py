from __future__ import annotations

import time
from typing import Any

import httpx

from src.core.logging import get_logger
from src.services.net import get_net_worker


class BinanceHttpClient:
    def __init__(
        self,
        base_url: str = "https://api.binance.com",
        timeout_s: float = 10.0,
        retries: int = 3,
        backoff_base_s: float = 0.5,
        backoff_max_s: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._retries = max(0, retries)
        self._backoff_base_s = backoff_base_s
        self._backoff_max_s = backoff_max_s
        self._client = client
        self._logger = get_logger("binance.http")

    def get_exchange_info(self) -> dict[str, Any]:
        return self._request_json("/api/v3/exchangeInfo")

    def get_exchange_info_symbol(self, symbol: str) -> dict[str, Any]:
        symbol_clean = symbol.strip().upper()
        return self._request_json(f"/api/v3/exchangeInfo?symbol={symbol_clean}")

    def get_ticker_price(self, symbol: str | None = None) -> dict[str, str] | str:
        if symbol:
            data = self._request_json(f"/api/v3/ticker/price?symbol={symbol}")
            if not isinstance(data, dict) or "price" not in data:
                raise ValueError("Unexpected ticker response format")
            price = data.get("price")
            if not isinstance(price, str):
                raise ValueError("Unexpected ticker response format")
            return price

        data = self._request_json("/api/v3/ticker/price")
        if not isinstance(data, list):
            raise ValueError("Unexpected ticker response format")
        prices: dict[str, str] = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            item_symbol = item.get("symbol")
            item_price = item.get("price")
            if isinstance(item_symbol, str) and isinstance(item_price, str):
                prices[item_symbol] = item_price
        return prices

    def get_book_ticker(self, symbol: str) -> dict[str, str]:
        data = self._request_json(f"/api/v3/ticker/bookTicker?symbol={symbol}")
        if not isinstance(data, dict):
            raise ValueError("Unexpected bookTicker response format")
        return {str(key): str(value) for key, value in data.items() if isinstance(key, str)}

    def get_ticker_prices(self) -> dict[str, str]:
        prices = self.get_ticker_price()
        if not isinstance(prices, dict):
            raise ValueError("Unexpected ticker response format")
        return prices

    def get_ticker_24h(self, symbol: str) -> dict[str, Any]:
        data = self._request_json(f"/api/v3/ticker/24hr?symbol={symbol}")
        if not isinstance(data, dict):
            raise ValueError("Unexpected 24h ticker response format")
        return data

    def get_time(self) -> dict[str, Any]:
        return self._request_json("/api/v3/time")

    def get_klines(self, symbol: str, interval: str = "1h", limit: int = 120) -> list[Any]:
        data = self._request_json(f"/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}")
        if not isinstance(data, list):
            raise ValueError("Unexpected klines response format")
        return data

    def get_orderbook_depth(self, symbol: str, limit: int = 50) -> dict[str, Any]:
        symbol_clean = symbol.strip().upper()
        data = self._request_json(f"/api/v3/depth?symbol={symbol_clean}&limit={limit}")
        if not isinstance(data, dict):
            raise ValueError("Unexpected depth response format")
        return data

    def get_recent_trades(self, symbol: str, limit: int = 500) -> list[dict[str, Any]]:
        symbol_clean = symbol.strip().upper()
        data = self._request_json(f"/api/v3/trades?symbol={symbol_clean}&limit={limit}")
        if not isinstance(data, list):
            raise ValueError("Unexpected trades response format")
        return [item for item in data if isinstance(item, dict)]

    def _request_json(self, path: str) -> dict[str, Any] | list[Any]:
        if self._client is None:
            worker = get_net_worker(timeout_s=self._timeout_s)
            return worker.call(
                f"binance_http:{path}",
                lambda client: self._request_json_with_client(client, path),
                timeout_s=self._timeout_s,
            )
        return self._request_json_with_client(self._client, path)

    def _request_json_with_client(self, client: httpx.Client, path: str) -> dict[str, Any] | list[Any]:
        last_exc: Exception | None = None
        attempts = self._retries + 1
        for attempt in range(attempts):
            try:
                response = client.get(f"{self._base_url}{path}", timeout=self._timeout_s)
                if response.status_code == 429 or response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"Retryable status {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                return response.json()
            except (httpx.TimeoutException, httpx.RequestError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                self._logger.warning(
                    "Binance HTTP error on %s (attempt %s/%s): %s",
                    path,
                    attempt + 1,
                    attempts,
                    exc,
                )
                if attempt < attempts - 1:
                    backoff_s = min(self._backoff_base_s * (2**attempt), self._backoff_max_s)
                    time.sleep(backoff_s)
                    continue
                break

        message = f"Binance request failed after {attempts} attempts"
        self._logger.error("%s: %s", message, last_exc)
        if last_exc is None:
            raise RuntimeError(message)
        raise last_exc
