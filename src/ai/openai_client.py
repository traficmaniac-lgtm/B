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

from src.ai.models import AiResponseEnvelope, parse_ai_response
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

    async def analyze_pair(self, datapack: dict[str, Any]) -> AiResponseEnvelope:
        payload = json.dumps(datapack, ensure_ascii=False, indent=2)
        system_prompt = (
            "You are a market analysis assistant.\n"
            "Return ONLY valid JSON, no markdown, no extra prose.\n"
            "You MUST return an object that matches this schema:\n"
            "{\n"
            '  "type": "analysis_result",\n'
            '  "status": "OK|WARN|DANGER|ERROR",\n'
            '  "confidence": 0.0,\n'
            '  "reason_codes": ["..."],\n'
            '  "message": "short message",\n'
            '  "analysis_result": {\n'
            '    "summary_lines": ["..."],\n'
            '    "strategy": {\n'
            '      "strategy_id": "GRID_CLASSIC|GRID_BIASED_LONG|GRID_BIASED_SHORT|RANGE_MEAN_REVERSION|TREND_FOLLOW_GRID|DO_NOT_TRADE",\n'
            '      "budget_usdt": 500,\n'
            '      "levels": [\n'
            '        {"side": "BUY", "price": 100.0, "qty": 0.5, "pct_from_mid": -1.0}\n'
            "      ],\n"
            '      "grid_step_pct": 0.3,\n'
            '      "range_low_pct": 1.5,\n'
            '      "range_high_pct": 1.8,\n'
            '      "bias": {"buy_pct": 50, "sell_pct": 50},\n'
            '      "order_size_mode": "equal|martingale_light|liquidity_weighted",\n'
            '      "max_orders": 12,\n'
            '      "max_exposure_pct": 35\n'
            "    },\n"
            '    "risk": {\n'
            '      "hard_stop_pct": 8,\n'
            '      "cooldown_minutes": 15,\n'
            '      "soft_stop_rules": ["..."],\n'
            '      "kill_switch_rules": ["..."],\n'
            '      "volatility_mode": "low|medium|high"\n'
            "    },\n"
            '    "control": {\n'
            '      "recheck_interval_sec": 60,\n'
            '      "ai_reanalyze_interval_sec": 300,\n'
            '      "min_change_to_rebuild_pct": 0.3\n'
            "    },\n"
            '    "actions": [\n'
            '      {"action": "note", "detail": "example", "severity": "info"}\n'
            "    ]\n"
            "  }\n"
            "}\n"
            "No trading, leverage, or order execution instructions."
        )

        async def _analyze() -> AiResponseEnvelope:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Analyze this datapack:\n{payload}"},
            ]
            response = await self._chat_completion(
                messages=messages,
                max_tokens=520,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content if response.choices else None
            parsed = parse_ai_response(content or "", expected_type="analysis_result")
            if parsed.status != "ERROR":
                return parsed
            retry_messages = messages + [
                {"role": "user", "content": "Your previous response was invalid JSON; output ONLY JSON."},
            ]
            response = await self._chat_completion(
                messages=retry_messages,
                max_tokens=520,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content if response.choices else None
            parsed = parse_ai_response(content or "", expected_type="analysis_result")
            if parsed.status == "ERROR":
                raise RuntimeError(parsed.message or "Invalid AI JSON")
            return parsed

        try:
            return await self._run_with_retries(_analyze)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("OpenAI analyze failed: %s", exc)
            raise

    async def monitor_datapack(self, datapack: dict[str, Any]) -> tuple[AiResponseEnvelope, str]:
        payload = json.dumps(datapack, ensure_ascii=False, indent=2)
        system_prompt = (
            "You are an AI observer monitoring a running strategy.\n"
            "Return ONLY valid JSON, no markdown, no extra prose.\n"
            "You MUST return an object that matches this schema:\n"
            "{\n"
            '  "type": "analysis_result",\n'
            '  "status": "OK|WARN|DANGER|ERROR",\n'
            '  "confidence": 0.0,\n'
            '  "reason_codes": ["..."],\n'
            '  "message": "short message",\n'
            '  "analysis_result": {\n'
            '    "summary_lines": ["..."],\n'
            '    "strategy": {\n'
            '      "strategy_id": "GRID_CLASSIC|GRID_BIASED_LONG|GRID_BIASED_SHORT|RANGE_MEAN_REVERSION|TREND_FOLLOW_GRID|DO_NOT_TRADE",\n'
            '      "budget_usdt": 500,\n'
            '      "levels": [],\n'
            '      "grid_step_pct": 0.3,\n'
            '      "range_low_pct": 1.5,\n'
            '      "range_high_pct": 1.8,\n'
            '      "bias": {"buy_pct": 50, "sell_pct": 50},\n'
            '      "order_size_mode": "equal|martingale_light|liquidity_weighted",\n'
            '      "max_orders": 12,\n'
            '      "max_exposure_pct": 35\n'
            "    },\n"
            '    "risk": {\n'
            '      "hard_stop_pct": 8,\n'
            '      "cooldown_minutes": 15,\n'
            '      "soft_stop_rules": ["..."],\n'
            '      "kill_switch_rules": ["..."],\n'
            '      "volatility_mode": "low|medium|high"\n'
            "    },\n"
            '    "control": {\n'
            '      "recheck_interval_sec": 10,\n'
            '      "ai_reanalyze_interval_sec": 60,\n'
            '      "min_change_to_rebuild_pct": 0.3\n'
            "    },\n"
            '    "actions": [\n'
            '      {"action": "note", "detail": "example", "severity": "info"}\n'
            "    ]\n"
            "  }\n"
            "}\n"
            "Do not execute trades; only suggest actions."
        )

        async def _monitor() -> tuple[AiResponseEnvelope, str]:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Monitor this datapack:\n{payload}"},
            ]
            response = await self._chat_completion(
                messages=messages,
                max_tokens=420,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content if response.choices else None
            parsed = parse_ai_response(content or "", expected_type="analysis_result")
            if parsed.status != "ERROR":
                return parsed, content or ""
            retry_messages = messages + [
                {"role": "user", "content": "Your previous response was invalid JSON; output ONLY JSON."},
            ]
            response = await self._chat_completion(
                messages=retry_messages,
                max_tokens=420,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content if response.choices else None
            parsed = parse_ai_response(content or "", expected_type="analysis_result")
            if parsed.status == "ERROR":
                raise RuntimeError(parsed.message or "Invalid AI JSON")
            return parsed, content or ""

        try:
            return await self._run_with_retries(_monitor)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("OpenAI monitor failed: %s", exc)
            raise

    async def chat_adjust(
        self,
        datapack: dict[str, Any],
        last_plan: dict[str, Any],
        user_message: str,
    ) -> AiResponseEnvelope:
        payload = json.dumps(datapack, ensure_ascii=False, indent=2)
        plan_payload = json.dumps(last_plan, ensure_ascii=False, indent=2)
        system_prompt = (
            "You adjust a grid trading plan.\n"
            "Return ONLY valid JSON, no markdown, no extra prose.\n"
            "You MUST return an object that matches this schema:\n"
            "{\n"
            '  "type": "strategy_patch",\n'
            '  "status": "OK|WARN|DANGER|ERROR",\n'
            '  "confidence": 0.0,\n'
            '  "reason_codes": ["..."],\n'
            '  "message": "short message",\n'
            '  "strategy_patch": {\n'
            '    "parameters_patch": {\n'
            '      "budget_usdt": 500,\n'
            '      "levels": [],\n'
            '      "grid_step_pct": 0.3,\n'
            '      "range_low_pct": 1.5,\n'
            '      "range_high_pct": 1.8,\n'
            '      "bias": {"buy_pct": 50, "sell_pct": 50},\n'
            '      "order_size_mode": "equal|martingale_light|liquidity_weighted",\n'
            '      "max_orders": 12,\n'
            '      "max_exposure_pct": 35,\n'
            '      "hard_stop_pct": 8,\n'
            '      "cooldown_minutes": 15,\n'
            '      "soft_stop_rules": [],\n'
            '      "kill_switch_rules": [],\n'
            '      "recheck_interval_sec": 60,\n'
            '      "ai_reanalyze_interval_sec": 300,\n'
            '      "min_change_to_rebuild_pct": 0.3,\n'
            '      "volatility_mode": "low|medium|high"\n'
            "    },\n"
            '    "recommended_actions": [\n'
            '      {"action": "note", "detail": "example", "severity": "info"}\n'
            "    ],\n"
            '    "message": "Short explanation"\n'
            "  }\n"
            "}\n"
            "Only include fields you want to change inside parameters_patch. "
            "Do not include any extra text outside JSON."
        )

        async def _adjust() -> AiResponseEnvelope:
            messages = [
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
            ]
            response = await self._chat_completion(
                messages=messages,
                max_tokens=420,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content if response.choices else None
            parsed = parse_ai_response(content or "", expected_type="strategy_patch")
            if parsed.status != "ERROR":
                return parsed
            retry_messages = messages + [
                {"role": "user", "content": "Your previous response was invalid JSON; output ONLY JSON."},
            ]
            response = await self._chat_completion(
                messages=retry_messages,
                max_tokens=420,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content if response.choices else None
            parsed = parse_ai_response(content or "", expected_type="strategy_patch")
            if parsed.status == "ERROR":
                raise RuntimeError(parsed.message or "Invalid AI JSON")
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
