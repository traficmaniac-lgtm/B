from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any

from src.core.logging import get_logger

_LOGGER = get_logger("ai.operator_models")

_ALLOWED_STATES = {"OK", "WAIT", "DO_NOT_TRADE"}
_ALLOWED_ACTIONS = {
    "START",
    "STOP",
    "PAUSE",
    "RESUME",
    "WAIT",
    "APPLY_PATCH",
    "CANCEL_ORDERS",
    "ADJUST_PARAMS",
    "REBUILD_GRID",
}
_ALLOWED_PROFILES = {"AGGRESSIVE", "BALANCED", "CONSERVATIVE"}
_ALLOWED_FORECAST_BIAS = {"UP", "DOWN", "FLAT"}
_ALLOWED_BIAS = {"NEUTRAL", "LONG", "SHORT", "UP", "DOWN", "FLAT"}


@dataclass
class StrategyPatch:
    budget: float | None = None
    step_pct: float | None = None
    range_down_pct: float | None = None
    range_up_pct: float | None = None
    levels: int | None = None
    tp_pct: float | None = None
    bias: str | None = None
    max_active_orders: int | None = None


@dataclass
class OperatorAIProfile:
    strategy_patch: StrategyPatch | None


@dataclass
class OperatorAIForecast:
    bias: str
    confidence: float
    horizon_min: int
    comment: str


@dataclass
class OperatorAIResult:
    state: str
    reason_short: str
    recommended_profile: str
    profiles: dict[str, OperatorAIProfile]
    actions: list[str]
    forecast: OperatorAIForecast
    risks: list[str]

    @property
    def strategy_patch(self) -> StrategyPatch | None:
        profile = self.profiles.get(self.recommended_profile)
        return profile.strategy_patch if profile else None


AiOperatorStrategyPatch = StrategyPatch
AiOperatorProfile = OperatorAIProfile
AiOperatorForecast = OperatorAIForecast
AiOperatorResponse = OperatorAIResult


def parse_ai_operator_response(text: str) -> OperatorAIResult:
    if not text:
        _LOGGER.warning("[AI] parse failed: empty response -> fallback WAIT")
        return _fallback_result("AI_PARSE_FAIL")
    payload = _parse_json_payload(text)
    if payload is None:
        parsed = _parse_text_protocol(text)
        if parsed is None:
            _LOGGER.warning("[AI] parse_failed: no action -> fallback WAIT")
            return _fallback_result("AI_PARSE_FAIL")
        return parsed
    if not isinstance(payload, dict):
        _LOGGER.warning("[AI] parse failed: payload not dict -> fallback WAIT")
        return _fallback_result("AI_PARSE_FAIL")

    return _parse_payload(payload)


def _parse_payload(payload: dict[str, Any]) -> OperatorAIResult:
    state = str(payload.get("state", "WAIT")).strip().upper()
    if state not in _ALLOWED_STATES:
        state = "WAIT"

    recommended_profile = str(payload.get("recommended_profile", "BALANCED")).strip().upper()
    if recommended_profile not in _ALLOWED_PROFILES:
        recommended_profile = "BALANCED"

    profiles_payload = payload.get("profiles")
    profiles: dict[str, OperatorAIProfile] = {}
    normalized_profiles: dict[str, Any] = {}
    if isinstance(profiles_payload, dict):
        normalized_profiles = {str(key).upper(): value for key, value in profiles_payload.items()}
    for profile_name in _ALLOWED_PROFILES:
        entry = normalized_profiles.get(profile_name)
        patch = _parse_strategy_patch(entry.get("strategy_patch") if isinstance(entry, dict) else None)
        profiles[profile_name] = OperatorAIProfile(strategy_patch=patch)

    actions_payload = payload.get("actions")
    actions: list[str] = []
    if isinstance(actions_payload, list):
        actions = [str(item).strip().upper() for item in actions_payload if item]
    actions = _normalize_actions(actions)
    if not actions:
        actions = ["WAIT"]

    reason_short = str(payload.get("reason_short", ""))[:120]

    forecast_payload = payload.get("forecast")
    forecast = _parse_forecast(forecast_payload)

    risks_payload = payload.get("risks")
    risks = [str(item) for item in risks_payload if item] if isinstance(risks_payload, list) else []

    return OperatorAIResult(
        state=state,
        reason_short=reason_short,
        recommended_profile=recommended_profile,
        profiles=profiles,
        actions=actions,
        forecast=forecast,
        risks=risks,
    )


def _parse_json_payload(text: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        return payload
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        _LOGGER.warning("[AI] parse failed: %s -> fallback WAIT", exc)
        return None
    return payload if isinstance(payload, dict) else None


def _parse_text_protocol(text: str) -> OperatorAIResult | None:
    state = _extract_line_value(text, "State") or "WAIT"
    state = state.strip().upper()
    if state not in _ALLOWED_STATES:
        state = "WAIT"
    actions_line = _extract_line_value(text, "Actions") or ""
    actions = _split_actions(actions_line)
    actions = _normalize_actions(actions)
    if not actions:
        return None
    reason_short = _extract_line_value(text, "Reason") or ""
    patch_payload = _extract_patch_payload(text)
    patch = _parse_strategy_patch(patch_payload)
    profiles = {key: OperatorAIProfile(strategy_patch=None) for key in _ALLOWED_PROFILES}
    recommended_profile = "BALANCED"
    profiles[recommended_profile] = OperatorAIProfile(strategy_patch=patch)
    return OperatorAIResult(
        state=state,
        reason_short=reason_short[:120],
        recommended_profile=recommended_profile,
        profiles=profiles,
        actions=actions,
        forecast=OperatorAIForecast(bias="FLAT", confidence=0.0, horizon_min=0, comment=""),
        risks=[],
    )


def _extract_line_value(text: str, key: str) -> str | None:
    match = re.search(rf"^\\s*{re.escape(key)}\\s*:\\s*(.+)$", text, flags=re.IGNORECASE | re.MULTILINE)
    if not match:
        return None
    return match.group(1).strip()


def _split_actions(actions_line: str) -> list[str]:
    if not actions_line:
        return []
    tokens = re.split(r"[,\n;|]+", actions_line)
    return [token.strip().upper() for token in tokens if token.strip()]


def _extract_patch_payload(text: str) -> dict[str, Any] | None:
    match = re.search(r"^\\s*Patch\\s*:\\s*(.*)$", text, flags=re.IGNORECASE | re.MULTILINE)
    if not match:
        return None
    remainder = match.group(1).strip()
    if remainder:
        payload = _parse_json_payload(remainder)
        if payload is not None:
            return payload
    after = text[match.end() :]
    payload = _parse_json_payload(after)
    if payload is not None:
        return payload
    return None


def operator_ai_result_to_dict(result: OperatorAIResult) -> dict[str, Any]:
    return {
        "state": result.state,
        "reason_short": result.reason_short,
        "recommended_profile": result.recommended_profile,
        "profiles": {
            key: {"strategy_patch": _strategy_patch_to_dict(profile.strategy_patch)}
            for key, profile in result.profiles.items()
        },
        "actions": list(result.actions),
        "forecast": {
            "bias": result.forecast.bias,
            "confidence": result.forecast.confidence,
            "horizon_min": result.forecast.horizon_min,
            "comment": result.forecast.comment,
        },
        "risks": list(result.risks),
    }


def operator_ai_result_to_json(result: OperatorAIResult) -> str:
    return json.dumps(operator_ai_result_to_dict(result), ensure_ascii=False)


def _fallback_result(reason: str) -> OperatorAIResult:
    profiles = {key: OperatorAIProfile(strategy_patch=None) for key in _ALLOWED_PROFILES}
    return OperatorAIResult(
        state="WAIT",
        reason_short=reason,
        recommended_profile="BALANCED",
        profiles=profiles,
        actions=["WAIT"],
        forecast=OperatorAIForecast(bias="FLAT", confidence=0.0, horizon_min=0, comment=""),
        risks=[],
    )


def _normalize_actions(actions: list[str]) -> list[str]:
    normalized: list[str] = []
    for action in actions:
        if action == "REQUEST_MORE_DATA":
            continue
        if action in _ALLOWED_ACTIONS and action not in normalized:
            normalized.append(action)
    return normalized


def _parse_strategy_patch(payload: Any) -> StrategyPatch | None:
    if not isinstance(payload, dict):
        return None
    bias = str(payload.get("bias", "")).strip().upper() if payload.get("bias") else None
    if bias and bias not in _ALLOWED_BIAS:
        bias = None
    tp_value = payload.get("tp_pct")
    if tp_value is None:
        tp_value = payload.get("take_profit_pct")
    range_down = payload.get("range_down_pct")
    if range_down is None:
        range_down = payload.get("range_low_pct")
    range_up = payload.get("range_up_pct")
    if range_up is None:
        range_up = payload.get("range_high_pct")
    return StrategyPatch(
        budget=_to_float_or_none(payload.get("budget")),
        step_pct=_to_float_or_none(payload.get("step_pct")),
        range_down_pct=_to_float_or_none(range_down),
        range_up_pct=_to_float_or_none(range_up),
        levels=_to_int_or_none(payload.get("levels")),
        tp_pct=_to_float_or_none(tp_value),
        bias=bias,
        max_active_orders=_to_int_or_none(payload.get("max_active_orders")),
    )


def _parse_forecast(payload: Any) -> OperatorAIForecast:
    if not isinstance(payload, dict):
        return OperatorAIForecast(bias="FLAT", confidence=0.0, horizon_min=0, comment="")
    bias = str(payload.get("bias", "FLAT")).strip().upper()
    if bias not in _ALLOWED_FORECAST_BIAS:
        bias = "FLAT"
    confidence = _to_float_or_none(payload.get("confidence"))
    if confidence is None:
        confidence = 0.0
    horizon_min = _to_int_or_none(payload.get("horizon_min"))
    if horizon_min is None:
        horizon_min = 0
    comment = str(payload.get("comment", ""))[:120]
    return OperatorAIForecast(
        bias=bias,
        confidence=float(confidence),
        horizon_min=int(horizon_min),
        comment=comment,
    )


def _strategy_patch_to_dict(patch: StrategyPatch | None) -> dict[str, Any] | None:
    if patch is None:
        return None
    return {
        "budget": patch.budget,
        "step_pct": patch.step_pct,
        "range_down_pct": patch.range_down_pct,
        "range_up_pct": patch.range_up_pct,
        "levels": patch.levels,
        "tp_pct": patch.tp_pct,
        "bias": patch.bias,
        "max_active_orders": patch.max_active_orders,
    }


def _to_float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
