from __future__ import annotations

import threading

from .net_worker import NetWorker

_worker: NetWorker | None = None
_lock = threading.Lock()


def get_net_worker(
    *,
    timeout_s: float = 10.0,
    max_connections: int = 10,
    max_keepalive_connections: int = 5,
) -> NetWorker:
    global _worker
    with _lock:
        if _worker is None:
            _worker = NetWorker(
                timeout_s=timeout_s,
                max_connections=max_connections,
                max_keepalive_connections=max_keepalive_connections,
            )
            _worker.start()
        return _worker


def shutdown_net_worker() -> None:
    global _worker
    with _lock:
        worker = _worker
        _worker = None
    if worker is not None:
        worker.stop()
