from __future__ import annotations

import time
from typing import Any

import httpx

from src.core.logging import get_logger


class BinanceHttpClient:
    def __init__(
        self,
        base_url: str = "https://api.binance.com",
        timeout_s: float = 10.0,
        retries: int = 3,
        backoff_base_s: float = 0.5,
        backoff_max_s: float = 10.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._retries = max(0, retries)
        self._backoff_base_s = backoff_base_s
        self._backoff_max_s = backoff_max_s
        self._logger = get_logger("binance.http")

    def get_exchange_info(self) -> dict[str, Any]:
        return self._request_json("/api/v3/exchangeInfo")

    def get_ticker_prices(self) -> dict[str, str]:
        data = self._request_json("/api/v3/ticker/price")
        if not isinstance(data, list):
            raise ValueError("Unexpected ticker response format")
        prices: dict[str, str] = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            symbol = item.get("symbol")
            price = item.get("price")
            if isinstance(symbol, str) and isinstance(price, str):
                prices[symbol] = price
        return prices

    def get_time(self) -> dict[str, Any]:
        return self._request_json("/api/v3/time")

    def _request_json(self, path: str) -> dict[str, Any] | list[Any]:
        last_exc: Exception | None = None
        attempts = self._retries + 1
        for attempt in range(attempts):
            try:
                with httpx.Client(base_url=self._base_url, timeout=self._timeout_s) as client:
                    response = client.get(path)
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
