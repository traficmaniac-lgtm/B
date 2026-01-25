from __future__ import annotations

import functools
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx
from PySide6.QtCore import QObject, QTimer

from src.core.logging import get_logger


@dataclass(frozen=True)
class NetTask:
    name: str
    fn: Callable[[httpx.Client], Any]
    on_ok: Callable[[Any, int], None] | None
    on_err: Callable[[Exception], None] | None


class NetWorker:
    def __init__(
        self,
        *,
        timeout_s: float = 10.0,
        max_connections: int = 10,
        max_keepalive_connections: int = 5,
    ) -> None:
        self._timeout_s = timeout_s
        self._limits = httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
        )
        self._queue: queue.Queue[NetTask | None] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="net-worker", daemon=True)
        self._logger = get_logger("services.net.worker")
        self._client: httpx.Client | None = None
        self._thread_id: int | None = None
        self._closing = False
        self._stopped = False
        self._started = False
        self._lock = threading.Lock()

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
            self._queue.put(None)
            self._thread.join(timeout=2.0)
        with self._lock:
            self._stopped = True

    def submit(
        self,
        name: str,
        fn: Callable[[httpx.Client], Any],
        on_ok: Callable[[Any, int], None] | None = None,
        on_err: Callable[[Exception], None] | None = None,
    ) -> None:
        if self._closing or self._stopped:
            self._logger.info("[NET] drop submit (closing) name=%s", name)
            return
        self.start()
        self._logger.info("[NET] submit name=%s", name)
        self._queue.put(NetTask(name=name, fn=fn, on_ok=on_ok, on_err=on_err))

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
            task = self._queue.get()
            if task is None:
                break
            if self._closing:
                self._logger.info("[NET] drop submit (closing) name=%s", task.name)
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
