from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx
from PySide6.QtCore import QObject, Signal, Slot

from src.binance.account_client import BinanceAccountClient
from src.binance.http_client import BinanceHttpClient
from src.core.logging import get_logger


@dataclass(frozen=True)
class NetRequest:
    request_id: str
    action: str
    params: dict[str, Any]


class BinanceNetWorker(QObject):
    request = Signal(object)
    request_finished = Signal(str, str, object, int)
    request_failed = Signal(str, str, object)

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        api_secret: str | None,
        recv_window: int,
        timeout_s: float,
        retries: int,
        backoff_base_s: float,
        backoff_max_s: float,
    ) -> None:
        super().__init__()
        self._base_url = base_url
        self._api_key = api_key or ""
        self._api_secret = api_secret or ""
        self._recv_window = recv_window
        self._timeout_s = timeout_s
        self._retries = retries
        self._backoff_base_s = backoff_base_s
        self._backoff_max_s = backoff_max_s
        self._client: httpx.Client | None = None
        self._http_client: BinanceHttpClient | None = None
        self._account_client: BinanceAccountClient | None = None
        self._logger = get_logger("net.worker")
        self.request.connect(self._handle_request)

    @Slot()
    def initialize(self) -> None:
        if self._client is not None:
            return
        limits = httpx.Limits(max_connections=10, max_keepalive_connections=5)
        self._client = httpx.Client(
            timeout=self._timeout_s,
            limits=limits,
            http2=False,
        )
        self._http_client = BinanceHttpClient(
            base_url=self._base_url,
            timeout_s=self._timeout_s,
            retries=self._retries,
            backoff_base_s=self._backoff_base_s,
            backoff_max_s=self._backoff_max_s,
            client=self._client,
        )
        if self._api_key and self._api_secret:
            self._account_client = BinanceAccountClient(
                base_url=self._base_url,
                api_key=self._api_key,
                api_secret=self._api_secret,
                recv_window=self._recv_window,
                timeout_s=self._timeout_s,
                retries=self._retries,
                backoff_base_s=self._backoff_base_s,
                backoff_max_s=self._backoff_max_s,
                client=self._client,
            )

    @Slot()
    def shutdown(self) -> None:
        if self._client is None:
            return
        try:
            self._client.close()
        except Exception:
            self._logger.exception("net worker client close failed")
        self._client = None
        self._http_client = None
        self._account_client = None

    @Slot(object)
    def _handle_request(self, request: NetRequest) -> None:
        try:
            if self._client is None:
                self.initialize()
            result, latency_ms = self._dispatch_request(request)
            self.request_finished.emit(request.request_id, request.action, result, latency_ms)
        except Exception as exc:  # noqa: BLE001
            self.request_failed.emit(request.request_id, request.action, exc)

    def _dispatch_request(self, request: NetRequest) -> tuple[object, int]:
        action = request.action
        params = request.params
        start = time.perf_counter()
        if action == "account_info":
            return self._require_account().get_account_info(), self._latency_ms(start)
        if action == "open_orders":
            symbol = str(params.get("symbol", ""))
            return self._require_account().get_open_orders(symbol), self._latency_ms(start)
        if action == "trade_fees":
            symbol = str(params.get("symbol", ""))
            return self._require_account().get_trade_fees(symbol), self._latency_ms(start)
        if action == "my_trades":
            symbol = str(params.get("symbol", ""))
            limit = int(params.get("limit", 50))
            return self._require_account().get_my_trades(symbol, limit=limit), self._latency_ms(start)
        if action == "order":
            symbol = str(params.get("symbol", ""))
            order_id = params.get("order_id")
            orig_client_order_id = params.get("orig_client_order_id")
            return (
                self._require_account().get_order(
                    symbol,
                    order_id=str(order_id) if order_id is not None else None,
                    orig_client_order_id=str(orig_client_order_id) if orig_client_order_id else None,
                ),
                self._latency_ms(start),
            )
        if action == "place_limit_order":
            return (
                self._require_account().place_limit_order(
                    symbol=str(params.get("symbol", "")),
                    side=str(params.get("side", "")),
                    price=str(params.get("price", "")),
                    quantity=str(params.get("quantity", "")),
                    time_in_force=str(params.get("time_in_force", "GTC")),
                    new_client_order_id=params.get("new_client_order_id"),
                ),
                self._latency_ms(start),
            )
        if action == "cancel_order":
            return (
                self._require_account().cancel_order(
                    symbol=str(params.get("symbol", "")),
                    order_id=str(params.get("order_id")) if params.get("order_id") else None,
                    orig_client_order_id=params.get("orig_client_order_id"),
                ),
                self._latency_ms(start),
            )
        if action == "cancel_open_orders":
            symbol = str(params.get("symbol", ""))
            return self._require_account().cancel_open_orders(symbol), self._latency_ms(start)
        if action == "sync_time_offset":
            return self._require_account().sync_time_offset(), self._latency_ms(start)
        if action == "exchange_info_symbol":
            symbol = str(params.get("symbol", ""))
            return self._require_http().get_exchange_info_symbol(symbol), self._latency_ms(start)
        if action == "book_ticker":
            symbol = str(params.get("symbol", ""))
            return self._require_http().get_book_ticker(symbol), self._latency_ms(start)
        if action == "klines":
            symbol = str(params.get("symbol", ""))
            interval = str(params.get("interval", "1h"))
            limit = int(params.get("limit", 120))
            return self._require_http().get_klines(symbol, interval=interval, limit=limit), self._latency_ms(start)
        raise ValueError(f"Unknown net action: {action}")

    def _require_http(self) -> BinanceHttpClient:
        if self._http_client is None:
            raise RuntimeError("HTTP client not initialized")
        return self._http_client

    def _require_account(self) -> BinanceAccountClient:
        if self._account_client is None:
            raise RuntimeError("Account client not initialized (missing API keys)")
        return self._account_client

    @staticmethod
    def _latency_ms(start: float) -> int:
        return int((time.perf_counter() - start) * 1000)
