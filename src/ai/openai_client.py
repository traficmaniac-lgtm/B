from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

from openai import AsyncOpenAI

from src.core.logging import get_logger


class OpenAIClient:
    def __init__(self, api_key: str, model: str, timeout_s: float = 25.0, retries: int = 1) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout_s = timeout_s
        self._retries = max(0, retries)
        self._logger = get_logger("services.openai")
        self._client = AsyncOpenAI(api_key=api_key, timeout=timeout_s)

    async def self_check(self) -> tuple[bool, str]:
        if not self._api_key.strip():
            return False, "OpenAI API key missing"

        async def _check() -> str:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": "Reply with OK."},
                    {"role": "user", "content": "OK"},
                ],
                max_tokens=10,
            )
            content = response.choices[0].message.content if response.choices else None
            return content or "OK"

        try:
            reply = await self._run_with_retries(_check)
        except Exception as exc:  # noqa: BLE001
            return False, f"OpenAI self-check failed: {exc}"

        return True, f"OpenAI self-check ok: {reply.strip()}"

    async def analyze_pair(self, datapack: dict[str, Any]) -> str:
        payload = json.dumps(datapack, ensure_ascii=False, indent=2)
        system_prompt = (
            "You are a market analysis assistant. Provide a short, structured response (5-8 lines) "
            "with:\n"
            "- Market type\n"
            "- Volatility (low/med/high)\n"
            "- Liquidity (ok/thin)\n"
            "- Spread note\n"
            "- Recommendation (grid yes/no)\n"
            "- Suggested grid_count, grid_step%, range_low/high% (draft)\n"
            "- One risk\n"
            "- One advice\n"
            "No trading, leverage, or order execution instructions."
        )

        async def _analyze() -> str:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Analyze this datapack:\n{payload}"},
                ],
                max_tokens=220,
            )
            content = response.choices[0].message.content if response.choices else None
            return content or "No response from AI."

        try:
            return await self._run_with_retries(_analyze)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("OpenAI analyze failed: %s", exc)
            return f"AI error: {exc}"

    async def _run_with_retries(self, coro_factory: Callable[[], Awaitable[Any]]) -> Any:
        last_exc: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                return await coro_factory()
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt >= self._retries:
                    raise
                await asyncio.sleep(0.5 * (attempt + 1))
        raise last_exc or RuntimeError("OpenAI request failed without exception")
