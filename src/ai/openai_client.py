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

from src.ai.models import AiResponseEnvelope, parse_ai_response_with_fallback
from src.ai.operator_models import operator_ai_result_to_json, parse_ai_operator_response
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
        finally:
            await self._close_client()

        return True, f"OpenAI self-check ok: {reply.strip()}"

    async def analyze_pair(self, datapack: dict[str, Any]) -> AiResponseEnvelope:
        payload = json.dumps(datapack, ensure_ascii=False, indent=2)
        system_prompt = (
            "Ты аналитик рынка.\n"
            "Всегда отвечай ТОЛЬКО валидным JSON (без markdown и текста вне JSON).\n"
            "Ответ должен быть на русском языке.\n"
            "Строго следуй схеме:\n"
            "{\n"
            '  "analysis_result": {\n'
            '    "status": "OK|WARN|DANGER|ERROR",\n'
            '    "confidence": 0.0,\n'
            '    "reason_codes": ["..."],\n'
            '    "summary_ru": "Короткое резюме"\n'
            "  },\n"
            '  "trade_options": [\n'
            "    {\n"
            '      "name_ru": "Вариант 1",\n'
            '      "entry": 1.0,\n'
            '      "exit": 1.01,\n'
            '      "tp_pct": 0.2,\n'
            '      "stop_pct": 0.1,\n'
            '      "expected_profit_pct": 0.15,\n'
            '      "expected_profit_usdt": 0.5,\n'
            '      "eta_min": 15,\n'
            '      "confidence": 0.55,\n'
            '      "risk_note_ru": "Риск: ..." \n'
            "    }\n"
            "  ],\n"
            '  "action_suggestions": [\n'
            '    {"type": "SUGGEST_REBUILD", "note_ru": "Причина"}\n'
            "  ],\n"
            '  "strategy_patch": {\n'
            '    "parameters_patch": {\n'
            '      "strategy_id": "GRID_CLASSIC|RANGE_MEAN_REVERSION|TREND_FOLLOW_GRID|DO_NOT_TRADE",\n'
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
            "    }\n"
            "  },\n"
            '  "language": "ru"\n'
            "}\n"
            "Если режим ZERO_FEE_CONVERT, верни 3-7 trade_options с ожидаемым профитом и ETA.\n"
            "Если режим не ZERO_FEE_CONVERT, trade_options должен быть пустым.\n"
            "Не давай инструкций по исполнению сделок."
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
            parsed = parse_ai_response_with_fallback(content or "")
            if parsed.analysis_result.status != "ERROR":
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
            parsed = parse_ai_response_with_fallback(content or "")
            return parsed

        try:
            return await self._run_with_retries(_analyze)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("OpenAI analyze failed: %s", exc)
            raise
        finally:
            await self._close_client()

    async def analyze_operator(self, datapack: dict[str, Any], follow_up_note: str | None = None) -> str:
        payload = json.dumps(datapack, ensure_ascii=False, indent=2)
        system_prompt = """Ты торговый оператор. Дай оценку рынка и предложи параметры сетки.
Никакой торговли. Только анализ и предложения.
Всегда отвечай ТОЛЬКО валидным JSON (без markdown и текста вне JSON).
Строго следуй схеме:
{
  "analysis_result": {
    "state": "OK|WARNING|DANGER",
    "summary": "string",
    "trend": "UP|DOWN|MIXED",
    "volatility": "LOW|MED|HIGH",
    "confidence": 0.0,
    "risks": ["..."],
    "data_quality": {
      "ws": "OK|WARMUP|LOST|NO",
      "http": "OK|NO",
      "klines": "OK|NO",
      "trades": "OK|NO",
      "orderbook": "OK|NO",
      "notes": "string"
    }
  },
  "strategy_patch": {
    "profile": "CONSERVATIVE|BALANCED|AGGRESSIVE",
    "bias": "UP|DOWN|FLAT",
    "range_mode": "MANUAL|AUTO_ATR",
    "step_pct": 0.1,
    "range_down_pct": 1.0,
    "range_up_pct": 1.0,
    "levels": 4,
    "tp_pct": 0.2,
    "max_active_orders": 8,
    "notes": "string"
  },
  "diagnostics": {
    "lookback_days_used": 1|7|30|365,
    "requested_more_data": false,
    "request": { "lookback_days": 7|30|365, "why": "string" } | null
  }
}
ВАЖНО: НЕЛЬЗЯ возвращать WAIT или DO_NOT_TRADE. Всегда дай ТОРГУЕМЫЙ strategy_patch с положительными значениями.
Если данные плохие — дай консервативный безопасный план и добавь риски/низкую уверенность.
Если нужна доп. история — выставляй diagnostics.requested_more_data=true и заполни request (1/7/30/365)."""

        async def _analyze() -> str:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Analyze this datapack:\n{payload}"},
            ]
            if follow_up_note:
                messages.append({"role": "user", "content": follow_up_note})
            response = await self._chat_completion(
                messages=messages,
                max_tokens=520,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content if response.choices else ""
            parsed = parse_ai_operator_response(content or "")
            return operator_ai_result_to_json(parsed)

        try:
            return await self._run_with_retries(_analyze)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("OpenAI operator analyze failed: %s", exc)
            raise
        finally:
            await self._close_client()

    async def chat_operator(
        self,
        datapack: dict[str, Any],
        user_message: str,
        last_ai_json: str | None,
        current_ui_params: dict[str, Any],
    ) -> str:
        payload = json.dumps(datapack, ensure_ascii=False, indent=2)
        ui_payload = json.dumps(current_ui_params, ensure_ascii=False, indent=2)
        last_json = last_ai_json or ""
        system_prompt = """Ты торговый оператор и отвечаешь на уточнения пользователя.
Никакой торговли. Только анализ и предложения.
Всегда отвечай ТОЛЬКО валидным JSON (без markdown и текста вне JSON).
Строго следуй схеме:
{
  "analysis_result": {
    "state": "OK|WARNING|DANGER",
    "summary": "string",
    "trend": "UP|DOWN|MIXED",
    "volatility": "LOW|MED|HIGH",
    "confidence": 0.0,
    "risks": ["..."],
    "data_quality": {
      "ws": "OK|WARMUP|LOST|NO",
      "http": "OK|NO",
      "klines": "OK|NO",
      "trades": "OK|NO",
      "orderbook": "OK|NO",
      "notes": "string"
    }
  },
  "strategy_patch": {
    "profile": "CONSERVATIVE|BALANCED|AGGRESSIVE",
    "bias": "UP|DOWN|FLAT",
    "range_mode": "MANUAL|AUTO_ATR",
    "step_pct": 0.1,
    "range_down_pct": 1.0,
    "range_up_pct": 1.0,
    "levels": 4,
    "tp_pct": 0.2,
    "max_active_orders": 8,
    "notes": "string"
  },
  "diagnostics": {
    "lookback_days_used": 1|7|30|365,
    "requested_more_data": false,
    "request": { "lookback_days": 7|30|365, "why": "string" } | null
  }
}
Ответ учитывает datapack, сообщение пользователя, последний JSON AI и текущие параметры UI.
ВАЖНО: НЕЛЬЗЯ возвращать WAIT или DO_NOT_TRADE. Всегда дай ТОРГУЕМЫЙ strategy_patch.
Если нужна доп. история — diagnostics.requested_more_data=true с request."""

        async def _chat() -> str:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Datapack:\n{payload}"},
                {"role": "user", "content": f"Current UI params:\n{ui_payload}"},
                {"role": "user", "content": f"Last AI JSON:\n{last_json}"},
                {"role": "user", "content": f"User message:\n{user_message}"},
            ]
            response = await self._chat_completion(
                messages=messages,
                max_tokens=520,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content if response.choices else ""
            parsed = parse_ai_operator_response(content or "")
            return operator_ai_result_to_json(parsed)

        try:
            return await self._run_with_retries(_chat)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("OpenAI operator chat failed: %s", exc)
            raise
        finally:
            await self._close_client()

    async def monitor_datapack(self, datapack: dict[str, Any]) -> tuple[AiResponseEnvelope, str]:
        payload = json.dumps(datapack, ensure_ascii=False, indent=2)
        system_prompt = (
            "Ты AI-наблюдатель за торговой стратегией.\n"
            "Всегда отвечай ТОЛЬКО валидным JSON (без markdown и текста вне JSON).\n"
            "Ответ должен быть на русском языке.\n"
            "Строго следуй схеме:\n"
            "{\n"
            '  "analysis_result": {\n'
            '    "status": "OK|WARN|DANGER|ERROR",\n'
            '    "confidence": 0.0,\n'
            '    "reason_codes": ["..."],\n'
            '    "summary_ru": "Короткое резюме"\n'
            "  },\n"
            '  "trade_options": [],\n'
            '  "action_suggestions": [\n'
            '    {"type": "SUGGEST_PAUSE", "note_ru": "Причина"}\n'
            "  ],\n"
            '  "strategy_patch": {\n'
            '    "parameters_patch": {}\n'
            "  },\n"
            '  "language": "ru"\n'
            "}\n"
            "Не исполняй сделки; только предлагай действия."
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
            parsed = parse_ai_response_with_fallback(content or "")
            if parsed.analysis_result.status != "ERROR":
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
            parsed = parse_ai_response_with_fallback(content or "")
            return parsed, content or ""

        try:
            return await self._run_with_retries(_monitor)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("OpenAI monitor failed: %s", exc)
            raise
        finally:
            await self._close_client()

    async def chat_adjust(
        self,
        datapack: dict[str, Any],
        last_plan: dict[str, Any],
        user_message: str,
    ) -> AiResponseEnvelope:
        payload = json.dumps(datapack, ensure_ascii=False, indent=2)
        plan_payload = json.dumps(last_plan, ensure_ascii=False, indent=2)
        system_prompt = (
            "Ты корректируешь параметры торговой стратегии.\n"
            "Всегда отвечай ТОЛЬКО валидным JSON (без markdown и текста вне JSON).\n"
            "Ответ должен быть на русском языке.\n"
            "Строго следуй схеме:\n"
            "{\n"
            '  "analysis_result": {\n'
            '    "status": "OK|WARN|DANGER|ERROR",\n'
            '    "confidence": 0.0,\n'
            '    "reason_codes": ["..."],\n'
            '    "summary_ru": "Короткое резюме"\n'
            "  },\n"
            '  "trade_options": [],\n'
            '  "action_suggestions": [],\n'
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
            "    }\n"
            "  },\n"
            '  "language": "ru"\n'
            "}\n"
            "Заполняй только те поля, которые нужно изменить. Не добавляй лишний текст."
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
            parsed = parse_ai_response_with_fallback(content or "")
            if parsed.analysis_result.status != "ERROR":
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
            parsed = parse_ai_response_with_fallback(content or "")
            return parsed

        try:
            return await self._run_with_retries(_adjust)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("OpenAI chat adjust failed: %s", exc)
            raise
        finally:
            await self._close_client()

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

    async def _close_client(self) -> None:
        try:
            await self._client.close()
        except Exception as exc:  # noqa: BLE001
            self._logger.debug("OpenAI client close ignored: %s", exc)
