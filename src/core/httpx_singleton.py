from __future__ import annotations

import ssl
import threading

import httpx

from src.core.logging import get_logger
from src.core.version import VERSION

CLIENT_LOCK = threading.Lock()
SSL_LOCK = threading.Lock()
_client: httpx.Client | None = None
_ssl_context: ssl.SSLContext | None = None


def build_ssl_context_once() -> ssl.SSLContext:
    global _ssl_context
    if _ssl_context is not None:
        return _ssl_context
    with SSL_LOCK:
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
    ssl_context = build_ssl_context_once()
    with CLIENT_LOCK:
        if _client is None:
            logger = get_logger("core.httpx")
            _client = httpx.Client(
                timeout=_build_timeout(),
                limits=_build_limits(),
                verify=ssl_context,
                headers=_build_headers(),
            )
            logger.info("httpx shared client initialized")
    return _client


def close_shared_client() -> None:
    global _client
    logger = get_logger("core.httpx")
    with CLIENT_LOCK:
        if _client is None:
            return
        try:
            _client.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("httpx shared client close failed: %s", exc)
        _client = None
