from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from src.core.logging import get_logger


_LOGGER = get_logger("ai.models")

AI_STATUS_VALUES = {"OK", "WARN", "DANGER", "ERROR"}


@dataclass
class AiLevel:
    side: str
    price: float
    qty: float
    pct_from_mid: float


@dataclass
class AiAnalysisResult:
    status: str
    confidence: float | None
    reason_codes: list[str]
    summary_ru: str


@dataclass
class AiTradeOption:
    name_ru: str
    entry: float
    exit: float
    tp_pct: float
    stop_pct: float
    expected_profit_pct: float
    expected_profit_usdt: float
    eta_min: int
    confidence: float | None
    risk_note_ru: str | None


@dataclass
class AiActionSuggestion:
    type: str
    note_ru: str | None


@dataclass
class AiStrategyPatch:
    parameters_patch: dict[str, Any]


@dataclass
class AiResponseEnvelope:
    analysis_result: AiAnalysisResult
    trade_options: list[AiTradeOption]
    action_suggestions: list[AiActionSuggestion]
    strategy_patch: AiStrategyPatch | None
    language: str | None


def safe_json_loads(text: str) -> Any | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        _LOGGER.warning("Failed to parse AI JSON: %s", exc)
        return None


def parse_ai_response(text: str) -> AiResponseEnvelope:
    if not text:
        return _error_envelope("Invalid AI JSON")
    payload = safe_json_loads(text.strip())
    if not isinstance(payload, dict):
        return _error_envelope("Invalid AI JSON")
    return _build_envelope(payload)


def parse_ai_response_with_fallback(text: str) -> AiResponseEnvelope:
    envelope = parse_ai_response(text)
    if envelope.analysis_result.status != "ERROR":
        return envelope
    return fallback_do_not_trade(envelope.analysis_result.summary_ru)


def _build_envelope(payload: dict[str, Any]) -> AiResponseEnvelope:
    analysis_payload = payload.get("analysis_result")
    if not isinstance(analysis_payload, dict):
        return _error_envelope("Invalid AI JSON")
    status = str(analysis_payload.get("status", "ERROR")).upper()
    if status not in AI_STATUS_VALUES:
        status = "ERROR"
    analysis = AiAnalysisResult(
        status=status,
        confidence=_to_float(analysis_payload.get("confidence")),
        reason_codes=_parse_string_list(analysis_payload.get("reason_codes")),
        summary_ru=str(analysis_payload.get("summary_ru") or ""),
    )
    trade_options = _parse_trade_options(payload.get("trade_options"))
    action_suggestions = _parse_action_suggestions(payload.get("action_suggestions"))
    strategy_patch = _parse_strategy_patch(payload.get("strategy_patch"))
    language = payload.get("language")
    return AiResponseEnvelope(
        analysis_result=analysis,
        trade_options=trade_options,
        action_suggestions=action_suggestions,
        strategy_patch=strategy_patch,
        language=str(language) if language is not None else None,
    )


def _parse_trade_options(payload: Any) -> list[AiTradeOption]:
    if not isinstance(payload, list):
        return []
    options: list[AiTradeOption] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            options.append(
                AiTradeOption(
                    name_ru=str(item.get("name_ru", "")),
                    entry=float(item.get("entry", 0)),
                    exit=float(item.get("exit", 0)),
                    tp_pct=float(item.get("tp_pct", 0)),
                    stop_pct=float(item.get("stop_pct", 0)),
                    expected_profit_pct=float(item.get("expected_profit_pct", 0)),
                    expected_profit_usdt=float(item.get("expected_profit_usdt", 0)),
                    eta_min=int(float(item.get("eta_min", 0))),
                    confidence=_to_float(item.get("confidence")),
                    risk_note_ru=str(item.get("risk_note_ru")) if item.get("risk_note_ru") is not None else None,
                )
            )
        except (TypeError, ValueError):
            continue
    return options


def _parse_action_suggestions(payload: Any) -> list[AiActionSuggestion]:
    if not isinstance(payload, list):
        return []
    suggestions: list[AiActionSuggestion] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        action_type = item.get("type") or item.get("action")
        note = item.get("note_ru") or item.get("detail_ru") or item.get("detail")
        if not action_type:
            continue
        suggestions.append(
            AiActionSuggestion(
                type=str(action_type),
                note_ru=str(note) if note is not None else None,
            )
        )
    return suggestions


def _parse_strategy_patch(payload: Any) -> AiStrategyPatch | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        return None
    if "parameters_patch" in payload and isinstance(payload.get("parameters_patch"), dict):
        parameters_patch = payload.get("parameters_patch", {})
    else:
        parameters_patch = payload
    return AiStrategyPatch(parameters_patch=parameters_patch)


def _parse_string_list(payload: Any) -> list[str]:
    if not isinstance(payload, list):
        return []
    return [str(item) for item in payload if item is not None]


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _error_envelope(message: str) -> AiResponseEnvelope:
    analysis = AiAnalysisResult(
        status="ERROR",
        confidence=None,
        reason_codes=["INVALID_JSON"],
        summary_ru=message,
    )
    return AiResponseEnvelope(
        analysis_result=analysis,
        trade_options=[],
        action_suggestions=[],
        strategy_patch=None,
        language="ru",
    )


def fallback_do_not_trade(message: str) -> AiResponseEnvelope:
    analysis = AiAnalysisResult(
        status="ERROR",
        confidence=None,
        reason_codes=["INVALID_JSON"],
        summary_ru=message,
    )
    return AiResponseEnvelope(
        analysis_result=analysis,
        trade_options=[],
        action_suggestions=[],
        strategy_patch=None,
        language="ru",
    )
