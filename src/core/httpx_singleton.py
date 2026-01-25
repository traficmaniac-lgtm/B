from __future__ import annotations

import ssl
import threading

import httpx

from src.core.logging import get_logger
from src.core.version import VERSION

_client_lock = threading.Lock()
_client: httpx.Client | None = None
_ssl_context: ssl.SSLContext | None = None


def build_ssl_context_once() -> ssl.SSLContext:
    global _ssl_context
    if _ssl_context is not None:
        return _ssl_context
    with _client_lock:
        if _ssl_context is None:
            _ssl_context = ssl.create_default_context()
    return _ssl_context


def _build_headers() -> dict[str, str]:
    return {"User-Agent": f"{VERSION} (httpx shared client)"}


def _build_limits() -> httpx.Limits:
    return httpx.Limits(max_connections=50, max_keepalive_connections=20)


def _build_timeout() -> httpx.Timeout:
    return httpx.Timeout(10.0)


def get_shared_client() -> httpx.Client:
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is None:
            logger = get_logger("core.httpx")
            _client = httpx.Client(
                timeout=_build_timeout(),
                limits=_build_limits(),
                verify=build_ssl_context_once(),
                headers=_build_headers(),
            )
            logger.info("httpx shared client initialized")
    return _client


def close_shared_client() -> None:
    global _client
    with _client_lock:
        if _client is None:
            return
        _client.close()
        _client = None
