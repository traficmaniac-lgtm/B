from __future__ import annotations

import functools
import itertools
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx
from PySide6.QtCore import QObject, QTimer

from src.core.logging import get_logger


@dataclass
class _DedupState:
    inflight: bool = False
    latest_requested: bool = False
    last_submit_ms: int = 0
    last_task: "NetTask | None" = None


@dataclass(frozen=True)
class NetTask:
    name: str
    key: str | None
    priority: int
    rate_limit_ms: int | None
    fn: Callable[[httpx.Client], Any]
    on_ok: Callable[[Any, int], None] | None
    on_err: Callable[[Exception], None] | None


class NetWorker:
    PRIORITY_HIGH = 0
    PRIORITY_MED = 1
    PRIORITY_LOW = 2
    PRIORITY_LOWEST = 3

    def __init__(
        self,
        *,
        timeout_s: float = 10.0,
        max_connections: int = 10,
        max_keepalive_connections: int = 5,
        book_ticker_min_interval_ms: int = 800,
    ) -> None:
        self._timeout_s = timeout_s
        self._limits = httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
        )
        self._queue: queue.PriorityQueue[tuple[int, int, NetTask | None]] = queue.PriorityQueue()
        self._seq = itertools.count()
        self._thread = threading.Thread(target=self._run, name="net-worker", daemon=True)
        self._logger = get_logger("services.net.worker")
        self._client: httpx.Client | None = None
        self._thread_id: int | None = None
        self._closing = False
        self._stopped = False
        self._started = False
        self._lock = threading.Lock()
        self._dedup_lock = threading.Lock()
        self._dedup_state: dict[str, _DedupState] = {}
        self._book_ticker_min_interval_ms = book_ticker_min_interval_ms

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self._closing = True
            started = self._started
        if started:
            self._queue.put((self.PRIORITY_LOWEST + 1, next(self._seq), None))
            self._thread.join(timeout=2.0)
        with self._lock:
            self._stopped = True

    def submit(
        self,
        name: str,
        fn: Callable[[httpx.Client], Any],
        on_ok: Callable[[Any, int], None] | None = None,
        on_err: Callable[[Exception], None] | None = None,
        *,
        dedup_key: str | None = None,
        priority: int | None = None,
        rate_limit_ms: int | None = None,
    ) -> None:
        if self._closing or self._stopped:
            self._logger.info("[NET] drop submit (closing) name=%s", name)
            return
        self.start()
        resolved_priority = priority if priority is not None else self._resolve_priority(name)
        task = NetTask(
            name=name,
            key=dedup_key,
            priority=resolved_priority,
            rate_limit_ms=rate_limit_ms,
            fn=fn,
            on_ok=on_ok,
            on_err=on_err,
        )
        if dedup_key:
            if self._handle_dedup_submit(task):
                return
        self._logger.info("[NET] submit name=%s", name)
        self._enqueue(task)

    def call(self, name: str, fn: Callable[[httpx.Client], Any], *, timeout_s: float | None = None) -> Any:
        if self.is_net_thread():
            if self._client is None:
                raise RuntimeError("NetWorker client not initialized")
            return fn(self._client)
        result: dict[str, Any] = {"done": False, "value": None, "error": None}
        done = threading.Event()

        def _on_ok(value: Any, _latency_ms: int) -> None:
            result["done"] = True
            result["value"] = value
            done.set()

        def _on_err(error: Exception) -> None:
            result["done"] = True
            result["error"] = error
            done.set()

        self.submit(name, fn, on_ok=_on_ok, on_err=_on_err)
        if not done.wait(timeout_s or self._timeout_s):
            raise TimeoutError(f"net worker call timeout name={name}")
        if result["error"] is not None:
            raise result["error"]
        return result["value"]

    def is_net_thread(self) -> bool:
        return self._thread_id is not None and threading.get_ident() == self._thread_id

    def _run(self) -> None:
        self._thread_id = threading.get_ident()
        self._client = httpx.Client(timeout=self._timeout_s, limits=self._limits, http2=False)
        self._logger.info("[NET] started")
        while True:
            _priority, _seq, task = self._queue.get()
            if task is None:
                break
            if self._closing:
                self._logger.info("[NET] drop submit (closing) name=%s", task.name)
                self._mark_task_done(task)
                continue
            self._execute_task(task)
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                self._logger.exception("[NET] client close failed")
        self._client = None

    def _execute_task(self, task: NetTask) -> None:
        if self._client is None:
            self._logger.error("[NET] client missing for name=%s", task.name)
            return
        if threading.get_ident() != self._thread_id:
            self._logger.error("[NET] thread mismatch for name=%s", task.name)
            return
        start = time.perf_counter()
        try:
            result = task.fn(self._client)
            latency_ms = int((time.perf_counter() - start) * 1000)
            self._logger.info("[NET] ok name=%s ms=%s", task.name, latency_ms)
            self._dispatch_ok(task.on_ok, result, latency_ms)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("[NET] err name=%s err=%s", task.name, exc)
            self._dispatch_err(task.on_err, exc)
        finally:
            self._mark_task_done(task)

    def _dispatch_ok(self, callback: Callable[[Any, int], None] | None, result: Any, latency_ms: int) -> None:
        if callback is None:
            return
        self._dispatch_callback(callback, result, latency_ms)

    def _dispatch_err(self, callback: Callable[[Exception], None] | None, error: Exception) -> None:
        if callback is None:
            return
        self._dispatch_callback(callback, error)

    @staticmethod
    def _dispatch_callback(callback: Callable[..., None], *args: Any) -> None:
        target = getattr(callback, "__self__", None)
        if target is None and isinstance(callback, functools.partial):
            target = getattr(callback.func, "__self__", None)
        if isinstance(target, QObject):
            QTimer.singleShot(0, target, lambda: callback(*args))
            return
        callback(*args)

    def _enqueue(self, task: NetTask) -> None:
        self._queue.put((task.priority, next(self._seq), task))

    def _handle_dedup_submit(self, task: NetTask) -> bool:
        if task.key is None:
            return False
        now_ms = self._now_ms()
        with self._dedup_lock:
            state = self._dedup_state.setdefault(task.key, _DedupState())
            if self._should_rate_limit(task, state, now_ms):
                self._log_rate_limit(task)
                if state.inflight:
                    state.latest_requested = True
                    state.last_task = task
                return True
            if state.inflight:
                state.latest_requested = True
                state.last_task = task
                return True
            state.inflight = True
            state.latest_requested = False
            state.last_task = task
            state.last_submit_ms = now_ms
        return False

    def _mark_task_done(self, task: NetTask) -> None:
        if task.key is None:
            return
        next_task: NetTask | None = None
        now_ms = self._now_ms()
        with self._dedup_lock:
            state = self._dedup_state.get(task.key)
            if state is None:
                return
            if state.latest_requested and state.last_task is not None:
                state.latest_requested = False
                next_task = state.last_task
                if self._should_rate_limit(next_task, state, now_ms):
                    self._log_rate_limit(next_task)
                    state.inflight = False
                    return
                state.inflight = True
                state.last_submit_ms = now_ms
            else:
                state.inflight = False
                state.last_task = None
        if next_task is not None:
            self._enqueue(next_task)

    def _resolve_priority(self, name: str) -> int:
        if name in {"place_limit_order", "cancel_order", "cancel_open_orders"}:
            return self.PRIORITY_HIGH
        if name in {"open_orders"}:
            return self.PRIORITY_MED
        if name in {"account_info", "my_trades"}:
            return self.PRIORITY_LOW
        if name in {"book_ticker", "ticker", "price", "ticker_price"}:
            return self.PRIORITY_LOWEST
        return self.PRIORITY_LOW

    def _should_rate_limit(self, task: NetTask, state: _DedupState, now_ms: int) -> bool:
        if task.name != "book_ticker":
            return False
        interval_ms = task.rate_limit_ms or self._book_ticker_min_interval_ms
        if interval_ms <= 0:
            return False
        if now_ms - state.last_submit_ms < interval_ms:
            return True
        return False

    def _log_rate_limit(self, task: NetTask) -> None:
        if task.name != "book_ticker":
            return
        symbol = self._extract_symbol(task)
        if symbol:
            self._logger.info("[NET][DROP] book_ticker symbol=%s reason=rate_limit", symbol)
        else:
            self._logger.info("[NET][DROP] book_ticker reason=rate_limit")

    @staticmethod
    def _extract_symbol(task: NetTask) -> str | None:
        if not task.key:
            return None
        if ":" not in task.key:
            return None
        _, symbol = task.key.split(":", 1)
        return symbol or None

    @staticmethod
    def _now_ms() -> int:
        return int(time.monotonic() * 1000)
