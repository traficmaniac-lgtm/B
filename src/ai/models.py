from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from src.core.logging import get_logger


_LOGGER = get_logger("ai.models")


AI_RESPONSE_TYPES = {"analysis_result", "strategy_patch"}
AI_STATUS_VALUES = {"OK", "WARN", "DANGER", "ERROR"}


@dataclass
class AiLevel:
    side: str
    price: float
    qty: float
    pct_from_mid: float


@dataclass
class AiBias:
    buy_pct: float
    sell_pct: float


@dataclass
class AiStrategy:
    strategy_id: str
    budget_usdt: float
    levels: list[AiLevel]
    grid_step_pct: float
    range_low_pct: float
    range_high_pct: float
    bias: AiBias
    order_size_mode: str
    max_orders: int
    max_exposure_pct: float


@dataclass
class AiRisk:
    hard_stop_pct: float
    cooldown_minutes: int
    soft_stop_rules: list[str]
    kill_switch_rules: list[str]
    volatility_mode: str


@dataclass
class AiControl:
    recheck_interval_sec: int
    ai_reanalyze_interval_sec: int
    min_change_to_rebuild_pct: float


@dataclass
class AiAction:
    action: str
    detail: str
    severity: str | None = None


@dataclass
class AiAnalysisResult:
    summary_lines: list[str]
    strategy: AiStrategy
    risk: AiRisk
    control: AiControl
    actions: list[AiAction]


@dataclass
class AiStrategyPatch:
    parameters_patch: dict[str, Any]
    recommended_actions: list[AiAction]
    message: str | None = None


@dataclass
class AiResponseEnvelope:
    type: str
    status: str
    confidence: float | None
    reason_codes: list[str]
    message: str | None
    analysis_result: AiAnalysisResult | None = None
    strategy_patch: AiStrategyPatch | None = None


def safe_json_loads(text: str) -> Any | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        _LOGGER.warning("Failed to parse AI JSON: %s", exc)
        return None


def parse_ai_response(text: str, expected_type: str | None = None) -> AiResponseEnvelope:
    if not text:
        return _error_envelope("Invalid AI JSON", expected_type or "analysis_result")
    payload = safe_json_loads(text.strip())
    if not isinstance(payload, dict):
        return _error_envelope("Invalid AI JSON", expected_type or "analysis_result")
    envelope = _build_envelope(payload)
    if expected_type and envelope.type != expected_type:
        return _error_envelope("Invalid AI JSON", expected_type)
    return envelope


def _build_envelope(payload: dict[str, Any]) -> AiResponseEnvelope:
    response_type = payload.get("type")
    if response_type not in AI_RESPONSE_TYPES:
        return _error_envelope("Invalid AI JSON", response_type)
    status = payload.get("status")
    if status not in AI_STATUS_VALUES:
        status = "ERROR"
    confidence = _to_float(payload.get("confidence"))
    reason_codes = _parse_string_list(payload.get("reason_codes"))
    message = payload.get("message")
    analysis_result: AiAnalysisResult | None = None
    strategy_patch: AiStrategyPatch | None = None
    if response_type == "analysis_result":
        analysis_result = _parse_analysis_result(payload.get("analysis_result"))
        if analysis_result is None:
            return _error_envelope("Invalid AI JSON", response_type)
    if response_type == "strategy_patch":
        strategy_patch = _parse_strategy_patch(payload.get("strategy_patch"))
        if strategy_patch is None:
            return _error_envelope("Invalid AI JSON", response_type)
    return AiResponseEnvelope(
        type=response_type,
        status=status,
        confidence=confidence,
        reason_codes=reason_codes,
        message=str(message) if message is not None else None,
        analysis_result=analysis_result,
        strategy_patch=strategy_patch,
    )


def _parse_analysis_result(payload: Any) -> AiAnalysisResult | None:
    if not isinstance(payload, dict):
        return None
    strategy_payload = payload.get("strategy")
    risk_payload = payload.get("risk")
    control_payload = payload.get("control")
    if not isinstance(strategy_payload, dict) or not isinstance(risk_payload, dict) or not isinstance(control_payload, dict):
        return None
    strategy = _parse_strategy(strategy_payload)
    risk = _parse_risk(risk_payload)
    control = _parse_control(control_payload)
    if strategy is None or risk is None or control is None:
        return None
    summary_lines = _parse_string_list(payload.get("summary_lines"))
    actions = _parse_actions(payload.get("actions"))
    return AiAnalysisResult(
        summary_lines=summary_lines,
        strategy=strategy,
        risk=risk,
        control=control,
        actions=actions,
    )


def _parse_strategy(payload: dict[str, Any]) -> AiStrategy | None:
    required = (
        "strategy_id",
        "budget_usdt",
        "levels",
        "grid_step_pct",
        "range_low_pct",
        "range_high_pct",
        "bias",
        "order_size_mode",
        "max_orders",
        "max_exposure_pct",
    )
    if not all(key in payload for key in required):
        return None
    bias_payload = payload.get("bias")
    if not isinstance(bias_payload, dict):
        return None
    bias = AiBias(
        buy_pct=float(bias_payload.get("buy_pct")),
        sell_pct=float(bias_payload.get("sell_pct")),
    )
    levels = _parse_levels(payload.get("levels"))
    return AiStrategy(
        strategy_id=str(payload.get("strategy_id")),
        budget_usdt=float(payload.get("budget_usdt")),
        levels=levels,
        grid_step_pct=float(payload.get("grid_step_pct")),
        range_low_pct=float(payload.get("range_low_pct")),
        range_high_pct=float(payload.get("range_high_pct")),
        bias=bias,
        order_size_mode=str(payload.get("order_size_mode")),
        max_orders=int(float(payload.get("max_orders"))),
        max_exposure_pct=float(payload.get("max_exposure_pct")),
    )


def _parse_risk(payload: dict[str, Any]) -> AiRisk | None:
    required = (
        "hard_stop_pct",
        "cooldown_minutes",
        "soft_stop_rules",
        "kill_switch_rules",
        "volatility_mode",
    )
    if not all(key in payload for key in required):
        return None
    return AiRisk(
        hard_stop_pct=float(payload.get("hard_stop_pct")),
        cooldown_minutes=int(float(payload.get("cooldown_minutes"))),
        soft_stop_rules=_parse_string_list(payload.get("soft_stop_rules")),
        kill_switch_rules=_parse_string_list(payload.get("kill_switch_rules")),
        volatility_mode=str(payload.get("volatility_mode")),
    )


def _parse_control(payload: dict[str, Any]) -> AiControl | None:
    required = ("recheck_interval_sec", "ai_reanalyze_interval_sec", "min_change_to_rebuild_pct")
    if not all(key in payload for key in required):
        return None
    return AiControl(
        recheck_interval_sec=int(float(payload.get("recheck_interval_sec"))),
        ai_reanalyze_interval_sec=int(float(payload.get("ai_reanalyze_interval_sec"))),
        min_change_to_rebuild_pct=float(payload.get("min_change_to_rebuild_pct")),
    )


def _parse_strategy_patch(payload: Any) -> AiStrategyPatch | None:
    if not isinstance(payload, dict):
        return None
    parameters_patch = payload.get("parameters_patch")
    if not isinstance(parameters_patch, dict):
        return None
    recommended_actions = _parse_actions(payload.get("recommended_actions"))
    message = payload.get("message")
    return AiStrategyPatch(
        parameters_patch=parameters_patch,
        recommended_actions=recommended_actions,
        message=str(message) if message is not None else None,
    )


def _parse_levels(payload: Any) -> list[AiLevel]:
    if not isinstance(payload, list):
        return []
    levels: list[AiLevel] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            levels.append(
                AiLevel(
                    side=str(item.get("side", "")),
                    price=float(item.get("price", 0)),
                    qty=float(item.get("qty", 0)),
                    pct_from_mid=float(item.get("pct_from_mid", 0)),
                )
            )
        except (TypeError, ValueError):
            continue
    return levels


def _parse_actions(payload: Any) -> list[AiAction]:
    if not isinstance(payload, list):
        return []
    actions: list[AiAction] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        action = item.get("action")
        detail = item.get("detail")
        severity = item.get("severity")
        if action is None or detail is None:
            continue
        actions.append(
            AiAction(
                action=str(action),
                detail=str(detail),
                severity=str(severity) if severity is not None else None,
            )
        )
    return actions


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


def _error_envelope(message: str, response_type: str) -> AiResponseEnvelope:
    return AiResponseEnvelope(
        type=response_type,
        status="ERROR",
        confidence=None,
        reason_codes=[],
        message=message,
        analysis_result=None,
        strategy_patch=None,
    )
