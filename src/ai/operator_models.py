from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any


_ALLOWED_STATES = {"OK", "WAIT", "DO_NOT_TRADE"}
_ALLOWED_PROFILES = {"AGGRESSIVE", "BALANCED", "CONSERVATIVE"}
_ALLOWED_FORECAST_BIAS = {"UP", "DOWN", "FLAT"}


@dataclass
class AiOperatorForecast:
    bias: str
    confidence: float
    horizon_min: int
    comment: str


@dataclass
class AiOperatorStrategyPatch:
    step_pct: float | None = None
    range_down_pct: float | None = None
    range_up_pct: float | None = None
    levels: int | None = None
    tp_pct: float | None = None
    bias: str | None = None
    max_active_orders: int | None = None


@dataclass
class AiOperatorProfile:
    strategy_patch: AiOperatorStrategyPatch


@dataclass
class AiOperatorResponse:
    state: str
    reason_short: str
    recommended_profile: str
    profiles: dict[str, AiOperatorProfile]
    forecast: AiOperatorForecast
    risks: list[str]


def parse_ai_operator_response(text: str) -> AiOperatorResponse:
    if not text:
        raise ValueError("Empty AI response")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid AI JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Invalid AI JSON payload")

    state = str(payload.get("state", "")).strip().upper()
    if state not in _ALLOWED_STATES:
        raise ValueError("Invalid state")
    reason_short = str(payload.get("reason_short", "")).strip()
    recommended_profile = str(payload.get("recommended_profile", "")).strip().upper()
    if recommended_profile not in _ALLOWED_PROFILES:
        raise ValueError("Invalid recommended_profile")
    profiles_payload = payload.get("profiles")
    if not isinstance(profiles_payload, dict):
        raise ValueError("Invalid profiles")
    profiles: dict[str, AiOperatorProfile] = {}
    for key in _ALLOWED_PROFILES:
        raw_profile = profiles_payload.get(key)
        if not isinstance(raw_profile, dict):
            raise ValueError(f"Invalid profiles.{key}")
        patch_payload = raw_profile.get("strategy_patch")
        if not isinstance(patch_payload, dict):
            raise ValueError(f"Invalid profiles.{key}.strategy_patch")
        profiles[key] = AiOperatorProfile(strategy_patch=_parse_strategy_patch(patch_payload))

    forecast_payload = payload.get("forecast")
    if not isinstance(forecast_payload, dict):
        raise ValueError("Invalid forecast")
    forecast_bias = str(forecast_payload.get("bias", "")).strip().upper()
    if forecast_bias not in _ALLOWED_FORECAST_BIAS:
        raise ValueError("Invalid forecast.bias")
    confidence = _to_float_or_none(forecast_payload.get("confidence"))
    if confidence is None or not (0.0 <= confidence <= 1.0):
        raise ValueError("Invalid forecast.confidence")
    horizon_min = _to_int_or_none(forecast_payload.get("horizon_min"))
    if horizon_min is None or horizon_min < 0:
        raise ValueError("Invalid forecast.horizon_min")
    comment = str(forecast_payload.get("comment", "")).strip()

    risks_payload = payload.get("risks")
    if not isinstance(risks_payload, list):
        raise ValueError("Invalid risks")
    risks = [str(item) for item in risks_payload if item is not None]

    return AiOperatorResponse(
        state=state,
        reason_short=reason_short,
        recommended_profile=recommended_profile,
        profiles=profiles,
        forecast=AiOperatorForecast(
            bias=forecast_bias, confidence=float(confidence), horizon_min=int(horizon_min), comment=comment
        ),
        risks=risks,
    )


def _to_float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("Invalid numeric value")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid numeric value") from exc


def _to_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("Invalid integer value")
    try:
        return int(float(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid integer value") from exc


def _to_str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value).strip() or None


def _parse_strategy_patch(payload: dict[str, Any]) -> AiOperatorStrategyPatch:
    bias = _to_str_or_none(payload.get("bias"))
    if bias:
        bias = bias.upper()
    return AiOperatorStrategyPatch(
        step_pct=_to_float_or_none(payload.get("step_pct")),
        range_down_pct=_to_float_or_none(payload.get("range_down_pct")),
        range_up_pct=_to_float_or_none(payload.get("range_up_pct")),
        levels=_to_int_or_none(payload.get("levels")),
        tp_pct=_to_float_or_none(payload.get("tp_pct")),
        bias=bias,
        max_active_orders=_to_int_or_none(payload.get("max_active_orders")),
    )
