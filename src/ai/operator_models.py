from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any


 codex/implement-fullpack-data-fetching-strategy
_ALLOWED_STATES = {"OK", "WAIT", "DO_NOT_TRADE"}
_ALLOWED_PROFILES = {"AGGRESSIVE", "BALANCED", "CONSERVATIVE"}
_ALLOWED_FORECAST_BIAS = {"UP", "DOWN", "FLAT"}


@dataclass
class AiOperatorForecast:
    bias: str
    confidence: float
    horizon_min: int
    comment: str

_ALLOWED_STATES = {"TRADE", "WAIT", "DO_NOT_TRADE"}
_ALLOWED_BIAS = {"NEUTRAL", "LONG", "SHORT"}
_ALLOWED_PROFILE = {"AGGRESSIVE", "BALANCED", "CONSERVATIVE"}
_ALLOWED_ACTIONS = {"START", "REBUILD_GRID", "WAIT", "PAUSE"}
_ALLOWED_FORECAST_BIAS = {"UP", "DOWN", "FLAT"}
 main


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
 codex/implement-fullpack-data-fetching-strategy

class AiOperatorProfile:
    strategy_patch: AiOperatorStrategyPatch | None


@dataclass
class AiOperatorForecast:
    bias: str
    confidence: float
    horizon_min: int
    comment: str


@dataclass
 main
class AiOperatorResponse:
    state: str
    reason_short: str
    recommended_profile: str
    profiles: dict[str, AiOperatorProfile]
 codex/implement-fullpack-data-fetching-strategy
    forecast: AiOperatorForecast
    risks: list[str]

    actions: list[str]
    forecast: AiOperatorForecast
    risks: list[str]

    @property
    def strategy_patch(self) -> AiOperatorStrategyPatch | None:
        profile = self.profiles.get(self.recommended_profile)
        return profile.strategy_patch if profile else None
 main


def parse_ai_operator_response(text: str) -> AiOperatorResponse:
    if not text:
        raise ValueError("Empty AI response")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid AI JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Invalid AI JSON payload")

 codex/implement-fullpack-data-fetching-strategy
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

    state = str(payload.get("state", "")).upper()
    if state not in _ALLOWED_STATES:
        raise ValueError("Invalid state")
    reason_short = str(payload.get("reason_short", "")).strip()
    recommended_profile = str(payload.get("recommended_profile", "")).upper()
    if recommended_profile not in _ALLOWED_PROFILE:
        raise ValueError("Invalid recommended_profile")

    profiles_payload = payload.get("profiles")
    if not isinstance(profiles_payload, dict):
        raise ValueError("Invalid profiles")
    normalized_profiles = {str(key).upper(): value for key, value in profiles_payload.items()}
    profiles: dict[str, AiOperatorProfile] = {}
    for profile_name in _ALLOWED_PROFILE:
        entry = normalized_profiles.get(profile_name)
        if entry is None:
            profiles[profile_name] = AiOperatorProfile(strategy_patch=None)
            continue
        if not isinstance(entry, dict):
            raise ValueError("Invalid profiles entry")
        strategy_patch_payload = entry.get("strategy_patch")
        strategy_patch = _parse_strategy_patch(strategy_patch_payload)
        profiles[profile_name] = AiOperatorProfile(strategy_patch=strategy_patch)

    actions_payload = payload.get("actions")
    if not isinstance(actions_payload, list):
        raise ValueError("Invalid actions")
    actions = [str(item).strip().upper() for item in actions_payload if item is not None]
    if any(action not in _ALLOWED_ACTIONS for action in actions):
        raise ValueError("Invalid actions")
 main

    forecast_payload = payload.get("forecast")
    if not isinstance(forecast_payload, dict):
        raise ValueError("Invalid forecast")
 codex/implement-fullpack-data-fetching-strategy
    forecast_bias = str(forecast_payload.get("bias", "")).strip().upper()
    if forecast_bias not in _ALLOWED_FORECAST_BIAS:
        raise ValueError("Invalid forecast.bias")
    confidence = _to_float_or_none(forecast_payload.get("confidence"))
    if confidence is None or not (0.0 <= confidence <= 1.0):
        raise ValueError("Invalid forecast.confidence")
    horizon_min = _to_int_or_none(forecast_payload.get("horizon_min"))
    if horizon_min is None or horizon_min < 0:

    bias = str(forecast_payload.get("bias", "")).upper()
    if bias not in _ALLOWED_FORECAST_BIAS:
        raise ValueError("Invalid forecast.bias")
    confidence = _to_float_or_none(forecast_payload.get("confidence"))
    if confidence is None:
        raise ValueError("Invalid forecast.confidence")
    horizon_min = _to_int_or_none(forecast_payload.get("horizon_min"))
    if horizon_min is None:
 main
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
 codex/implement-fullpack-data-fetching-strategy
        forecast=AiOperatorForecast(
            bias=forecast_bias, confidence=float(confidence), horizon_min=int(horizon_min), comment=comment

        actions=actions,
        forecast=AiOperatorForecast(
            bias=bias,
            confidence=float(confidence),
            horizon_min=int(horizon_min),
            comment=comment,
 main
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
 codex/implement-fullpack-data-fetching-strategy
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

    return str(value).strip().upper() or None


def _parse_strategy_patch(payload: Any) -> AiOperatorStrategyPatch | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ValueError("Invalid strategy_patch")
    bias = _to_str_or_none(payload.get("bias"))
    if bias is not None and bias not in _ALLOWED_BIAS:
        raise ValueError("Invalid strategy_patch.bias")
    return AiOperatorStrategyPatch(
        budget=_to_float_or_none(payload.get("budget")),
        bias=bias,
        levels=_to_int_or_none(payload.get("levels")),
        step_pct=_to_float_or_none(payload.get("step_pct")),
        range_down_pct=_to_float_or_none(payload.get("range_down_pct")),
        range_up_pct=_to_float_or_none(payload.get("range_up_pct")),
        take_profit_pct=_to_float_or_none(payload.get("take_profit_pct")),
        max_exposure=_to_float_or_none(payload.get("max_exposure")),
 main
    )
