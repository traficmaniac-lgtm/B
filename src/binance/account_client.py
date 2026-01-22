from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from src.core.logging import get_logger


@dataclass
class AccountStatus:
    can_trade: bool
    permissions: list[str]
    msg: str | None


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
        self._time_offset_ms = 0

    def get_account_info(self) -> dict[str, Any]:
        payload = self._request_signed("GET", "/api/v3/account")
        if not isinstance(payload, dict):
            raise ValueError("Unexpected account response format")
        return payload

    def get_account_status(self, account_info: dict[str, Any] | None = None) -> "AccountStatus":
        try:
            info = account_info or self.get_account_info()
        except Exception as exc:  # noqa: BLE001
            return AccountStatus(can_trade=False, permissions=[], msg=str(exc))
        can_trade = bool(info.get("canTrade")) if "canTrade" in info else False
        permissions_raw = info.get("permissions")
        permissions = [str(item) for item in permissions_raw if isinstance(item, str)] if isinstance(permissions_raw, list) else []
        return AccountStatus(can_trade=can_trade, permissions=permissions, msg=None)

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

    def get_my_trades(self, symbol: str, limit: int = 50) -> list[dict[str, Any]]:
        params = {"symbol": symbol, "limit": limit}
        payload = self._request_signed("GET", "/api/v3/myTrades", params=params)
        if not isinstance(payload, list):
            raise ValueError("Unexpected trades response format")
        return [item for item in payload if isinstance(item, dict)]

    def get_order(
        self,
        symbol: str,
        order_id: str | None = None,
        orig_client_order_id: str | None = None,
    ) -> dict[str, Any]:
        if not order_id and not orig_client_order_id:
            raise ValueError("order_id or orig_client_order_id is required")
        params: dict[str, str] = {"symbol": symbol}
        if orig_client_order_id:
            params["origClientOrderId"] = orig_client_order_id
        else:
            params["orderId"] = str(order_id)
        payload = self._request_signed("GET", "/api/v3/order", params=params)
        if not isinstance(payload, dict):
            raise ValueError("Unexpected order response format")
        return payload

    def place_limit_order(
        self,
        symbol: str,
        side: str,
        price: str,
        quantity: str,
        time_in_force: str = "GTC",
        new_client_order_id: str | None = None,
    ) -> dict[str, Any]:
        params = {
            "symbol": symbol,
            "side": side,
            "type": "LIMIT",
            "timeInForce": time_in_force,
            "quantity": quantity,
            "price": price,
        }
        if new_client_order_id:
            params["newClientOrderId"] = new_client_order_id
        try:
            payload = self._request_signed("POST", "/api/v3/order", params=params, retry_on_timestamp=True)
        except Exception as exc:  # noqa: BLE001
            self._log_order_error(
                "place",
                exc,
                symbol=symbol,
                side=side,
                price=price,
                quantity=quantity,
                client_order_id=new_client_order_id,
            )
            raise
        if not isinstance(payload, dict):
            raise ValueError("Unexpected order response format")
        return payload

    def cancel_order(
        self,
        symbol: str,
        order_id: str | None = None,
        orig_client_order_id: str | None = None,
    ) -> dict[str, Any]:
        if not order_id and not orig_client_order_id:
            raise ValueError("order_id or orig_client_order_id is required")
        params = {"symbol": symbol}
        if orig_client_order_id:
            params["origClientOrderId"] = orig_client_order_id
        else:
            params["orderId"] = str(order_id)
        try:
            payload = self._request_signed("DELETE", "/api/v3/order", params=params, retry_on_timestamp=True)
        except Exception as exc:  # noqa: BLE001
            self._log_order_error(
                "cancel",
                exc,
                symbol=symbol,
                side=None,
                price=None,
                quantity=None,
                client_order_id=orig_client_order_id,
                order_id=order_id,
            )
            raise
        if not isinstance(payload, dict):
            raise ValueError("Unexpected cancel order response format")
        return payload

    def cancel_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        params = {"symbol": symbol}
        payload = self._request_signed("DELETE", "/api/v3/openOrders", params=params)
        if not isinstance(payload, list):
            raise ValueError("Unexpected cancel open orders response format")
        return [item for item in payload if isinstance(item, dict)]

    def get_server_time(self) -> dict[str, Any]:
        with httpx.Client(base_url=self._base_url, timeout=self._timeout_s) as client:
            response = client.get("/api/v3/time")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Unexpected server time response format")
        return payload

    def sync_time_offset(self) -> dict[str, int]:
        payload = self.get_server_time()
        server_time = int(payload.get("serverTime", 0) or 0)
        local_time = int(time.time() * 1000)
        self._time_offset_ms = server_time - local_time
        self._logger.info("Time sync offset=%sms", self._time_offset_ms)
        return {"server_time": server_time, "local_time": local_time, "offset_ms": self._time_offset_ms}

    def _log_order_error(
        self,
        action: str,
        exc: Exception,
        *,
        symbol: str,
        side: str | None,
        price: str | None,
        quantity: str | None,
        client_order_id: str | None,
        order_id: str | None = None,
    ) -> None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        response_text = None
        if response is not None:
            try:
                response_text = response.json()
            except Exception:  # noqa: BLE001
                response_text = response.text
        timestamp = int(time.time() * 1000) + self._time_offset_ms
        self._logger.error(
            "Binance %s error status=%s response=%s symbol=%s side=%s price=%s qty=%s orderId=%s clientOrderId=%s recvWindow=%s timestamp=%s",
            action,
            status,
            response_text,
            symbol,
            side,
            price,
            quantity,
            order_id,
            client_order_id,
            self._recv_window,
            timestamp,
        )

    def _request_signed(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        retry_on_timestamp: bool = False,
    ) -> Any:
        if not self._api_key or not self._api_secret:
            raise RuntimeError("Binance API key/secret not configured")
        last_exc: Exception | None = None
        attempts = self._retries + 1
        retried_time_sync = False
        for attempt in range(attempts):
            try:
                data = dict(params or {})
                data["timestamp"] = int(time.time() * 1000) + self._time_offset_ms
                data["recvWindow"] = self._recv_window
                query = urlencode(data, doseq=True)
                signature = hmac.new(self._api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
                signed_query = f"{query}&signature={signature}"
                url = f"{path}?{signed_query}"
                headers = {"X-MBX-APIKEY": self._api_key}
                with httpx.Client(base_url=self._base_url, timeout=self._timeout_s) as client:
                    response = client.request(method, url, headers=headers)
                if response.status_code == 429 or response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"Retryable status {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                if response.status_code >= 400:
                    payload = None
                    try:
                        payload = response.json()
                    except Exception:  # noqa: BLE001
                        payload = None
                    if isinstance(payload, dict) and payload.get("code") == -1021:
                        self.sync_time_offset()
                        if retry_on_timestamp and not retried_time_sync:
                            retried_time_sync = True
                            continue
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
