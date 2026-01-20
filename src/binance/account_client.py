from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from src.core.logging import get_logger


class BinanceAccountClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        api_secret: str,
        recv_window: int = 5000,
        timeout_s: float = 10.0,
        retries: int = 2,
        backoff_base_s: float = 0.5,
        backoff_max_s: float = 5.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._api_secret = api_secret
        self._recv_window = recv_window
        self._timeout_s = timeout_s
        self._retries = max(0, retries)
        self._backoff_base_s = backoff_base_s
        self._backoff_max_s = backoff_max_s
        self._logger = get_logger("binance.account")

    def get_account_info(self) -> dict[str, Any]:
        payload = self._request_signed("GET", "/api/v3/account")
        if not isinstance(payload, dict):
            raise ValueError("Unexpected account response format")
        return payload

    def get_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        params = {"symbol": symbol}
        payload = self._request_signed("GET", "/api/v3/openOrders", params=params)
        if not isinstance(payload, list):
            raise ValueError("Unexpected open orders response format")
        return [item for item in payload if isinstance(item, dict)]

    def get_trade_fees(self, symbol: str) -> list[dict[str, Any]]:
        params = {"symbol": symbol}
        payload = self._request_signed("GET", "/sapi/v1/asset/tradeFee", params=params)
        if not isinstance(payload, list):
            raise ValueError("Unexpected trade fee response format")
        return [item for item in payload if isinstance(item, dict)]

    def place_limit_order(
        self,
        symbol: str,
        side: str,
        price: str,
        quantity: str,
        time_in_force: str = "GTC",
    ) -> dict[str, Any]:
        params = {
            "symbol": symbol,
            "side": side,
            "type": "LIMIT",
            "timeInForce": time_in_force,
            "quantity": quantity,
            "price": price,
        }
        payload = self._request_signed("POST", "/api/v3/order", params=params)
        if not isinstance(payload, dict):
            raise ValueError("Unexpected order response format")
        return payload

    def cancel_order(self, symbol: str, order_id: str) -> dict[str, Any]:
        params = {"symbol": symbol, "orderId": order_id}
        payload = self._request_signed("DELETE", "/api/v3/order", params=params)
        if not isinstance(payload, dict):
            raise ValueError("Unexpected cancel order response format")
        return payload

    def cancel_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        params = {"symbol": symbol}
        payload = self._request_signed("DELETE", "/api/v3/openOrders", params=params)
        if not isinstance(payload, list):
            raise ValueError("Unexpected cancel open orders response format")
        return [item for item in payload if isinstance(item, dict)]

    def _request_signed(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        if not self._api_key or not self._api_secret:
            raise RuntimeError("Binance API key/secret not configured")
        data = dict(params or {})
        data["timestamp"] = int(time.time() * 1000)
        data["recvWindow"] = self._recv_window
        query = urlencode(data, doseq=True)
        signature = hmac.new(self._api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        signed_query = f"{query}&signature={signature}"
        url = f"{path}?{signed_query}"
        headers = {"X-MBX-APIKEY": self._api_key}
        last_exc: Exception | None = None
        attempts = self._retries + 1
        for attempt in range(attempts):
            try:
                with httpx.Client(base_url=self._base_url, timeout=self._timeout_s) as client:
                    response = client.request(method, url, headers=headers)
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
                    "Binance signed request error on %s (attempt %s/%s): %s",
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
        message = f"Binance signed request failed after {attempts} attempts"
        self._logger.error("%s: %s", message, last_exc)
        if last_exc is None:
            raise RuntimeError(message)
        raise last_exc
