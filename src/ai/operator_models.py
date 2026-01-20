from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any


_ALLOWED_STATES = {"TRADE", "WAIT", "PAUSE"}
_ALLOWED_BIAS = {"NEUTRAL", "LONG", "SHORT"}


@dataclass
class AiOperatorAnalysisResult:
    state: str
    summary: str
    risks: list[str]


@dataclass
class AiOperatorStrategyPatch:
    budget: float | None = None
    bias: str | None = None
    levels: int | None = None
    step_pct: float | None = None
    range_down_pct: float | None = None
    range_up_pct: float | None = None
    take_profit_pct: float | None = None
    max_exposure: float | None = None


@dataclass
class AiOperatorResponse:
    analysis_result: AiOperatorAnalysisResult
    strategy_patch: AiOperatorStrategyPatch | None
    actions_suggested: list[str]
    need_data: list[str]


def parse_ai_operator_response(text: str) -> AiOperatorResponse:
    if not text:
        raise ValueError("Empty AI response")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid AI JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Invalid AI JSON payload")

    analysis_payload = payload.get("analysis_result")
    if not isinstance(analysis_payload, dict):
        raise ValueError("Missing analysis_result")
    state = str(analysis_payload.get("state", "")).upper()
    if state not in _ALLOWED_STATES:
        raise ValueError("Invalid analysis_result.state")
    summary = str(analysis_payload.get("summary", "")).strip()
    risks_payload = analysis_payload.get("risks")
    if not isinstance(risks_payload, list):
        raise ValueError("Invalid analysis_result.risks")
    risks = [str(item) for item in risks_payload if item is not None]

    strategy_patch_payload = payload.get("strategy_patch")
    strategy_patch = None
    if strategy_patch_payload is not None:
        if not isinstance(strategy_patch_payload, dict):
            raise ValueError("Invalid strategy_patch")
        bias = _to_str_or_none(strategy_patch_payload.get("bias"))
        if bias is not None and bias not in _ALLOWED_BIAS:
            raise ValueError("Invalid strategy_patch.bias")
        strategy_patch = AiOperatorStrategyPatch(
            budget=_to_float_or_none(strategy_patch_payload.get("budget")),
            bias=bias,
            levels=_to_int_or_none(strategy_patch_payload.get("levels")),
            step_pct=_to_float_or_none(strategy_patch_payload.get("step_pct")),
            range_down_pct=_to_float_or_none(strategy_patch_payload.get("range_down_pct")),
            range_up_pct=_to_float_or_none(strategy_patch_payload.get("range_up_pct")),
            take_profit_pct=_to_float_or_none(strategy_patch_payload.get("take_profit_pct")),
            max_exposure=_to_float_or_none(strategy_patch_payload.get("max_exposure")),
        )

    actions_payload = payload.get("actions_suggested")
    if not isinstance(actions_payload, list):
        raise ValueError("Invalid actions_suggested")
    actions = [str(item).strip().upper() for item in actions_payload if item is not None]

    need_data_payload = payload.get("need_data", [])
    if need_data_payload is None:
        need_data_payload = []
    if not isinstance(need_data_payload, list):
        raise ValueError("Invalid need_data")
    raw_need_data = [str(item) for item in need_data_payload if item is not None]
    need_data: list[str] = []
    for item in raw_need_data:
        normalized = _normalize_need_data_key(item)
        if normalized:
            need_data.append(normalized)

    return AiOperatorResponse(
        analysis_result=AiOperatorAnalysisResult(state=state, summary=summary, risks=risks),
        strategy_patch=strategy_patch,
        actions_suggested=actions,
        need_data=need_data,
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
    return str(value).strip().upper() or None


def _normalize_need_data_key(value: str) -> str:
    cleaned = str(value).strip()
    if not cleaned:
        return ""
    lowered = cleaned.lower()
    if lowered in {"orderbook", "orderbook_depth", "orderbook_depth_50"}:
        return "orderbook_depth_50"
    if lowered in {"trades", "recent_trades", "recent_trades_1m"}:
        return "recent_trades_1m"
    return cleaned
