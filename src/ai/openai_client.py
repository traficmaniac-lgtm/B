from __future__ import annotations

import asyncio
import importlib.util
import json
import time
from typing import Any, Awaitable, Callable

if importlib.util.find_spec("openai") is None:
    AsyncOpenAI = None
else:
    from openai import AsyncOpenAI

from src.ai.models import AiPatchResponse, AiResponse, parse_ai_json, parse_ai_patch_json
from src.core.logging import get_logger


class OpenAIClient:
    def __init__(self, api_key: str, model: str, timeout_s: float = 25.0, retries: int = 2) -> None:
        if AsyncOpenAI is None:
            raise RuntimeError("OpenAI SDK not installed")
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
            response = await self._chat_completion(
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

    async def analyze_pair(self, datapack: dict[str, Any]) -> AiResponse:
        payload = json.dumps(datapack, ensure_ascii=False, indent=2)
        system_prompt = (
            "You are a market analysis assistant. Return ONLY a JSON object that follows this schema:\n"
            "{\n"
            '  "summary": {\n'
            '    "market_type": "range|trend|mixed",\n'
            '    "volatility": "low|medium|high",\n'
            '    "liquidity": "ok|thin",\n'
            '    "spread": "<string or percent>",\n'
            '    "advice": "<short advice>"\n'
            "  },\n"
            '  "plan": {\n'
            '    "mode": "grid|adaptive|manual",\n'
            '    "grid_count": 12,\n'
            '    "grid_step_pct": 0.3,\n'
            '    "range_low_pct": 1.5,\n'
            '    "range_high_pct": 1.8,\n'
            '    "budget_usdt": 500\n'
            "  },\n"
            '  "preview_levels": {\n'
            '    "levels": [\n'
            '      {"side": "BUY", "price": 100.0, "qty": 0.5, "pct_from_mid": -1.0}\n'
            "    ]\n"
            "  },\n"
            '  "risk_notes": ["..."],\n'
            '  "questions": ["..."],\n'
            '  "tool_requests": ["..."]\n'
            "}\n"
            "No trading, leverage, or order execution instructions."
        )

        async def _analyze() -> AiResponse:
            response = await self._chat_completion(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Analyze this datapack:\n{payload}"},
                ],
                max_tokens=520,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content if response.choices else None
            parsed = parse_ai_json(content or "")
            if parsed is None:
                raise RuntimeError("Failed to parse AI JSON response")
            return parsed

        try:
            return await self._run_with_retries(_analyze)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("OpenAI analyze failed: %s", exc)
            raise

    async def chat_adjust(
        self,
        datapack: dict[str, Any],
        last_plan: dict[str, Any],
        user_message: str,
    ) -> AiPatchResponse:
        payload = json.dumps(datapack, ensure_ascii=False, indent=2)
        plan_payload = json.dumps(last_plan, ensure_ascii=False, indent=2)
        system_prompt = (
            "You adjust a grid trading plan. Return ONLY a JSON object with this schema:\n"
            "{\n"
            '  "patch": {\n'
            '    "mode": "grid|adaptive|manual",\n'
            '    "grid_count": 12,\n'
            '    "grid_step_pct": 0.3,\n'
            '    "range_low_pct": 1.5,\n'
            '    "range_high_pct": 1.8,\n'
            '    "budget_usdt": 500\n'
            "  },\n"
            '  "preview_levels": {\n'
            '    "levels": [\n'
            '      {"side": "BUY", "price": 100.0, "qty": 0.5, "pct_from_mid": -1.0}\n'
            "    ]\n"
            "  },\n"
            '  "message": "Short explanation",\n'
            '  "questions": ["..."],\n'
            '  "tool_requests": ["..."]\n'
            "}\n"
            "Only include the fields you want to change inside patch. "
            "Do not include any extra text outside JSON."
        )

        async def _adjust() -> AiPatchResponse:
            response = await self._chat_completion(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": (
                            "Last datapack:\n"
                            f"{payload}\n\n"
                            "Last plan:\n"
                            f"{plan_payload}\n\n"
                            f"User message: {user_message}"
                        ),
                    },
                ],
                max_tokens=420,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content if response.choices else None
            parsed = parse_ai_patch_json(content or "")
            if parsed is None:
                raise RuntimeError("Failed to parse AI patch response")
            return parsed

        try:
            return await self._run_with_retries(_adjust)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("OpenAI chat adjust failed: %s", exc)
            raise

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

    async def _chat_completion(self, **kwargs: Any) -> Any:
        start = time.perf_counter()
        try:
            response = await self._client.chat.completions.create(model=self._model, **kwargs)
        except TypeError as exc:
            response_format = kwargs.pop("response_format", None)
            if response_format is None:
                raise
            self._logger.warning("response_format not supported, retrying without it: %s", exc)
            response = await self._client.chat.completions.create(model=self._model, **kwargs)
        latency_ms = (time.perf_counter() - start) * 1000
        request_id = getattr(response, "id", None)
        self._logger.info("OpenAI response id=%s latency=%.1fms", request_id, latency_ms)
        return response
