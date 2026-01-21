from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from src.core.logging import get_logger

_LOGGER = get_logger("ai.operator_models")

_ALLOWED_STATES = {"SAFE", "WARNING", "DANGER"}
_ALLOWED_ACTIONS = {
    "WAIT",
    "DO_NOT_TRADE",
    "START",
    "APPLY_PATCH",
    "REQUEST_DATA",
    "ADJUST_PARAMS",
    "STOP",
}
_ALLOWED_PROFILES = {"AGGRESSIVE", "BALANCED", "CONSERVATIVE"}
_ALLOWED_BIAS = {"UP", "DOWN", "FLAT"}


@dataclass
class StrategyPatch:
    strategy_id: str | None = None
    budget: float | None = None
    step_pct: float | None = None
    range_down_pct: float | None = None
    range_up_pct: float | None = None
    levels: int | None = None
    tp_pct: float | None = None
    bias: str | None = None
    max_active_orders: int | None = None


@dataclass
class OperatorAIRequestDataItem:
    id: str
    window: str | None
    limit: int | None
    reason: str | None
    levels: int | None


@dataclass
class OperatorAIRequestData:
    need_more: bool
    items: list[OperatorAIRequestDataItem]


@dataclass
class OperatorAIMathCheck:
    net_edge_pct: float | None
    break_even_tp_pct: float | None
    assumptions: dict[str, Any]


@dataclass
class OperatorAIResult:
    state: str
    recommendation: str
    next_action: str
    reason: str
    profile: str
    actions_allowed: list[str]
    strategy_patch: StrategyPatch | None
    request_data: OperatorAIRequestData
    math_check: OperatorAIMathCheck


AiOperatorStrategyPatch = StrategyPatch
AiOperatorResponse = OperatorAIResult


def parse_ai_operator_response(text: str) -> OperatorAIResult:
    if not text:
        _LOGGER.warning("[AI] parse failed: empty response -> fallback WAIT")
        return _fallback_result("AI_PARSE_FAIL")
    payload = _parse_json_payload(text)
    if not isinstance(payload, dict):
        _LOGGER.warning("[AI] parse failed: payload not dict -> fallback WAIT")
        return _fallback_result("AI_PARSE_FAIL")

    return _parse_payload(payload)


def _parse_payload(payload: dict[str, Any]) -> OperatorAIResult:
    state = _normalize_state(str(payload.get("state", "WARNING")).strip().upper())

    recommendation = _normalize_action(payload.get("recommendation"))
    next_action = _normalize_action(payload.get("next_action"))
    if not recommendation:
        recommendation = "WAIT"
    if not next_action:
        next_action = "WAIT"

    profile = str(payload.get("profile", "BALANCED")).strip().upper()
    if profile not in _ALLOWED_PROFILES:
        profile = "BALANCED"

    actions_allowed = _normalize_actions(payload.get("actions_allowed"))
    if next_action not in actions_allowed:
        actions_allowed.insert(0, next_action)

    reason = str(payload.get("reason", ""))[:160]

    strategy_patch = _parse_strategy_patch(payload.get("patch"))

    request_data = _parse_request_data(payload.get("request_data"))
    math_check = _parse_math_check(payload.get("math_check"))

    return OperatorAIResult(
        state=state,
        recommendation=recommendation,
        next_action=next_action,
        reason=reason,
        profile=profile,
        actions_allowed=actions_allowed,
        strategy_patch=strategy_patch,
        request_data=request_data,
        math_check=math_check,
    )


def _parse_json_payload(text: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    return payload if isinstance(payload, dict) else None


def operator_ai_result_to_dict(result: OperatorAIResult) -> dict[str, Any]:
    return {
        "state": result.state,
        "recommendation": result.recommendation,
        "next_action": result.next_action,
        "reason": result.reason,
        "profile": result.profile,
        "actions_allowed": list(result.actions_allowed),
        "patch": _strategy_patch_to_dict(result.strategy_patch),
        "request_data": {
            "need_more": result.request_data.need_more,
            "items": [
                {
                    "id": item.id,
                    "window": item.window,
                    "limit": item.limit,
                    "reason": item.reason,
                    "levels": item.levels,
                }
                for item in result.request_data.items
            ],
        },
        "math_check": {
            "net_edge_pct": result.math_check.net_edge_pct,
            "break_even_tp_pct": result.math_check.break_even_tp_pct,
            "assumptions": result.math_check.assumptions,
        },
    }


def operator_ai_result_to_json(result: OperatorAIResult) -> str:
    return json.dumps(operator_ai_result_to_dict(result), ensure_ascii=False)


def _fallback_result(reason: str) -> OperatorAIResult:
    return OperatorAIResult(
        state="WARNING",
        recommendation="WAIT",
        next_action="WAIT",
        reason=reason,
        profile="BALANCED",
        actions_allowed=["WAIT"],
        strategy_patch=None,
        request_data=OperatorAIRequestData(need_more=False, items=[]),
        math_check=OperatorAIMathCheck(net_edge_pct=None, break_even_tp_pct=None, assumptions={}),
    )


def _normalize_action(action: Any) -> str:
    if not action:
        return ""
    normalized = str(action).strip().upper()
    return normalized if normalized in _ALLOWED_ACTIONS else ""


def _normalize_actions(actions: Any) -> list[str]:
    if not isinstance(actions, list):
        return ["WAIT"]
    normalized: list[str] = []
    for action in actions:
        action_value = _normalize_action(action)
        if action_value and action_value not in normalized:
            normalized.append(action_value)
    if not normalized:
        normalized.append("WAIT")
    return normalized


def _normalize_state(state: str) -> str:
    if state in _ALLOWED_STATES:
        return state
    legacy = {
        "OK": "SAFE",
        "WAIT": "WARNING",
        "DO_NOT_TRADE": "DANGER",
    }
    return legacy.get(state, "WARNING")


def _strategy_patch_has_values(patch: StrategyPatch | None) -> bool:
    if patch is None:
        return False
    values = [
        patch.strategy_id,
        patch.budget,
        patch.step_pct,
        patch.range_down_pct,
        patch.range_up_pct,
        patch.levels,
        patch.tp_pct,
        patch.bias,
        patch.max_active_orders,
    ]
    return any(value is not None for value in values)


def _parse_strategy_patch(payload: Any) -> StrategyPatch | None:
    if not isinstance(payload, dict):
        return None
    bias = str(payload.get("bias", "")).strip().upper() if payload.get("bias") else None
    if bias and bias not in _ALLOWED_BIAS:
        bias = None
    tp_value = payload.get("tp_pct")
    range_down = payload.get("range_down_pct")
    range_up = payload.get("range_up_pct")
    return StrategyPatch(
        strategy_id=str(payload.get("strategy_id")).strip().upper() if payload.get("strategy_id") else None,
        budget=_to_float_or_none(payload.get("budget")),
        step_pct=_to_float_or_none(payload.get("step_pct")),
        range_down_pct=_to_float_or_none(range_down),
        range_up_pct=_to_float_or_none(range_up),
        levels=_to_int_or_none(payload.get("levels")),
        tp_pct=_to_float_or_none(tp_value),
        bias=bias,
        max_active_orders=_to_int_or_none(payload.get("max_active_orders")),
    )

def _parse_request_data(payload: Any) -> OperatorAIRequestData:
    if not isinstance(payload, dict):
        return OperatorAIRequestData(need_more=False, items=[])
    items_payload = payload.get("items")
    items: list[OperatorAIRequestDataItem] = []
    if isinstance(items_payload, list):
        for item in items_payload:
            if not isinstance(item, dict):
                continue
            items.append(
                OperatorAIRequestDataItem(
                    id=str(item.get("id", "")),
                    window=str(item.get("window", "")) if item.get("window") else None,
                    limit=_to_int_or_none(item.get("limit")),
                    reason=str(item.get("reason", "")) if item.get("reason") else None,
                    levels=_to_int_or_none(item.get("levels")),
                )
            )
    return OperatorAIRequestData(need_more=bool(payload.get("need_more")), items=items)


def _parse_math_check(payload: Any) -> OperatorAIMathCheck:
    if not isinstance(payload, dict):
        return OperatorAIMathCheck(net_edge_pct=None, break_even_tp_pct=None, assumptions={})
    assumptions = payload.get("assumptions")
    if not isinstance(assumptions, dict):
        assumptions = {}
    return OperatorAIMathCheck(
        net_edge_pct=_to_float_or_none(payload.get("net_edge_pct")),
        break_even_tp_pct=_to_float_or_none(payload.get("break_even_tp_pct")),
        assumptions=assumptions,
    )


def _strategy_patch_to_dict(patch: StrategyPatch | None) -> dict[str, Any] | None:
    if patch is None:
        return None
    return {
        "strategy_id": patch.strategy_id,
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
