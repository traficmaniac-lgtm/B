from __future__ import annotations

import logging
import hashlib
import hmac
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from src.core.logging import LogDeduper, get_logger
from src.services.net import get_net_worker


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
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._api_secret = api_secret
        self._recv_window = recv_window
        self._timeout_s = timeout_s
        self._retries = max(0, retries)
        self._backoff_base_s = backoff_base_s
        self._backoff_max_s = backoff_max_s
        self._client = client
        self._logger = get_logger("binance.account")
        self._log_deduper = LogDeduper(cooldown_s=3.0)
        self._time_offset_ms = 0
        self._time_lock = threading.Lock()

    def _now_ms(self) -> int:
        return int(time.time() * 1000)

    def _get_time_offset_ms(self) -> int:
        with self._time_lock:
            return self._time_offset_ms

    def _set_time_offset_ms(self, offset_ms: int) -> None:
        with self._time_lock:
            self._time_offset_ms = offset_ms

    def _get_recv_window(self) -> int:
        with self._time_lock:
            return self._recv_window

    def _set_recv_window(self, value: int) -> None:
        with self._time_lock:
            self._recv_window = value

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

    def get_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        params = None
        symbol_clean = symbol.strip().upper() if symbol else ""
        if symbol_clean and symbol_clean != "MULTI":
            params = {"symbol": symbol_clean}
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
        if self._client is None:
            worker = get_net_worker(timeout_s=self._timeout_s)
            return worker.call(
                "binance_signed:server_time",
                lambda client: self._get_server_time_with_client(client),
                timeout_s=self._timeout_s,
            )
        return self._get_server_time_with_client(self._client)

    def _get_server_time_with_client(self, client: httpx.Client) -> dict[str, Any]:
        response = client.get(f"{self._base_url}/api/v3/time", timeout=self._timeout_s)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Unexpected server time response format")
        return payload

    def sync_time_offset(self) -> dict[str, int]:
        payload = self.get_server_time()
        server_time = int(payload.get("serverTime", 0) or 0)
        local_time = self._now_ms()
        offset_ms = server_time - local_time
        self._set_time_offset_ms(offset_ms)
        self._logger.info("Time sync offset=%sms", offset_ms)
        return {"server_time": server_time, "local_time": local_time, "offset_ms": offset_ms}

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
        timestamp = self._now_ms() + self._get_time_offset_ms()
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
        if self._client is None:
            worker = get_net_worker(timeout_s=self._timeout_s)
            return worker.call(
                f"binance_signed:{path}",
                lambda client: self._request_signed_with_client(
                    client,
                    method=method,
                    path=path,
                    params=params,
                    retry_on_timestamp=retry_on_timestamp,
                ),
                timeout_s=self._timeout_s,
            )
        return self._request_signed_with_client(
            self._client,
            method=method,
            path=path,
            params=params,
            retry_on_timestamp=retry_on_timestamp,
        )

    def _request_signed_with_client(
        self,
        client: httpx.Client,
        *,
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
        increased_recv_window = False
        for attempt in range(attempts):
            try:
                data = dict(params or {})
                timestamp_ms = self._now_ms() + self._get_time_offset_ms()
                data["timestamp"] = timestamp_ms
                data["recvWindow"] = self._get_recv_window()
                query = urlencode(data, doseq=True)
                signature = hmac.new(self._api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
                signed_query = f"{query}&signature={signature}"
                url = f"{path}?{signed_query}"
                headers = {"X-MBX-APIKEY": self._api_key}
                response = client.request(
                    method,
                    f"{self._base_url}{url}",
                    headers=headers,
                    timeout=self._timeout_s,
                )
                status = response.status_code
                if status in {418, 429} or status >= 500:
                    raise httpx.HTTPStatusError(
                        f"Retryable status {status}",
                        request=response.request,
                        response=response,
                    )
                if status >= 400:
                    payload = None
                    try:
                        payload = response.json()
                    except Exception:  # noqa: BLE001
                        payload = None
                    if status == 400:
                        code = payload.get("code") if isinstance(payload, dict) else None
                        msg = payload.get("msg") if isinstance(payload, dict) else response.text
                        hint = " time sync likely wrong" if code == -1021 else ""
                        self._logger.warning(
                            "[BINANCE][400] endpoint=%s code=%s msg=%s ts=%s recvWindow=%s offset_ms=%s%s",
                            path,
                            code,
                            msg,
                            timestamp_ms,
                            self._get_recv_window(),
                            self._get_time_offset_ms(),
                            hint,
                        )
                    if isinstance(payload, dict) and payload.get("code") == -1021:
                        self.sync_time_offset()
                        if retry_on_timestamp and not retried_time_sync:
                            retried_time_sync = True
                            continue
                        if not increased_recv_window:
                            current_window = self._get_recv_window()
                            next_window = min(max(current_window * 2, 10_000), 20_000)
                            if next_window != current_window:
                                self._set_recv_window(next_window)
                                increased_recv_window = True
                                self._logger.warning(
                                    "[BINANCE] recvWindow increased after -1021: %s -> %s (offset_ms=%s)",
                                    current_window,
                                    next_window,
                                    self._get_time_offset_ms(),
                                )
                    raise httpx.HTTPStatusError(
                        f"Non-retryable status {status}",
                        request=response.request,
                        response=response,
                    )
                return response.json()
            except (httpx.TimeoutException, httpx.RequestError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                status = None
                retryable = isinstance(exc, (httpx.TimeoutException, httpx.RequestError))
                if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
                    status = exc.response.status_code
                    retryable = status in {418, 429} or status >= 500
                message = str(exc)
                dedup_key = (path, status, message)
                self._log_deduper.log(
                    self._logger,
                    logging.WARNING,
                    dedup_key,
                    "Binance signed request error on %s (attempt %s/%s): %s",
                    path,
                    attempt + 1,
                    attempts,
                    exc,
                )
                if retryable and attempt < attempts - 1:
                    backoff_s = min(self._backoff_base_s * (2**attempt), self._backoff_max_s)
                    time.sleep(backoff_s)
                    continue
                break
        message = f"Binance signed request failed after {attempts} attempts"
        self._logger.error("%s: %s", message, last_exc)
        if last_exc is None:
            raise RuntimeError(message)
        raise last_exc
