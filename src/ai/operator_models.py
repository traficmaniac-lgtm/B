from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from src.ai.operator_profiles import get_profile_preset
from src.core.logging import get_logger

_LOGGER = get_logger("ai.operator_models")

_ALLOWED_STATES = {"OK", "WARNING", "DANGER"}
_ALLOWED_TRENDS = {"UP", "DOWN", "MIXED"}
_ALLOWED_VOLATILITY = {"LOW", "MED", "HIGH"}
_ALLOWED_DATA_QUALITY = {"OK", "WARMUP", "LOST", "NO"}
_ALLOWED_HTTP_QUALITY = {"OK", "NO"}
_ALLOWED_PROFILES = {"AGGRESSIVE", "BALANCED", "CONSERVATIVE"}
_ALLOWED_BIAS = {"UP", "DOWN", "FLAT"}
_ALLOWED_RANGE_MODE = {"MANUAL", "AUTO_ATR"}
_ALLOWED_LOOKBACK = {1, 7, 30, 365}


@dataclass
class AnalysisDataQuality:
    ws: str
    http: str
    klines: str
    trades: str
    orderbook: str
    notes: str


@dataclass
class AnalysisResult:
    state: str
    summary: str
    trend: str
    volatility: str
    confidence: float
    risks: list[str]
    data_quality: AnalysisDataQuality


@dataclass
class StrategyPatch:
    profile: str
    bias: str
    range_mode: str
    step_pct: float
    range_down_pct: float
    range_up_pct: float
    levels: int
    tp_pct: float
    max_active_orders: int
    notes: str


@dataclass
class OperatorAIRequest:
    lookback_days: int
    why: str


@dataclass
class OperatorAIDiagnostics:
    lookback_days_used: int
    requested_more_data: bool
    request: OperatorAIRequest | None


@dataclass
class OperatorAIResult:
    analysis_result: AnalysisResult
    strategy_patch: StrategyPatch
    diagnostics: OperatorAIDiagnostics


AiOperatorStrategyPatch = StrategyPatch
AiOperatorResponse = OperatorAIResult


def parse_ai_operator_response(text: str) -> OperatorAIResult:
    if not text:
        _LOGGER.warning("[AI] parse failed: empty response -> fallback SAFE patch")
        return _fallback_result("AI_PARSE_FAIL")
    payload = _parse_json_payload(text)
    if not isinstance(payload, dict):
        _LOGGER.warning("[AI] parse failed: payload not dict -> fallback SAFE patch")
        return _fallback_result("AI_PARSE_FAIL")

    return _parse_payload(payload)


def _parse_payload(payload: dict[str, Any]) -> OperatorAIResult:
    analysis_payload = payload.get("analysis_result") if isinstance(payload.get("analysis_result"), dict) else {}
    patch_payload = payload.get("strategy_patch") if isinstance(payload.get("strategy_patch"), dict) else {}
    diagnostics_payload = payload.get("diagnostics") if isinstance(payload.get("diagnostics"), dict) else {}

    analysis_result = _parse_analysis_result(analysis_payload)
    strategy_patch = _parse_strategy_patch(patch_payload)
    diagnostics = _parse_diagnostics(diagnostics_payload)

    return OperatorAIResult(
        analysis_result=analysis_result,
        strategy_patch=strategy_patch,
        diagnostics=diagnostics,
    )


def _parse_json_payload(text: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    return payload if isinstance(payload, dict) else None


def operator_ai_result_to_dict(result: OperatorAIResult) -> dict[str, Any]:
    return {
        "analysis_result": {
            "state": result.analysis_result.state,
            "summary": result.analysis_result.summary,
            "trend": result.analysis_result.trend,
            "volatility": result.analysis_result.volatility,
            "confidence": result.analysis_result.confidence,
            "risks": list(result.analysis_result.risks),
            "data_quality": {
                "ws": result.analysis_result.data_quality.ws,
                "http": result.analysis_result.data_quality.http,
                "klines": result.analysis_result.data_quality.klines,
                "trades": result.analysis_result.data_quality.trades,
                "orderbook": result.analysis_result.data_quality.orderbook,
                "notes": result.analysis_result.data_quality.notes,
            },
        },
        "strategy_patch": _strategy_patch_to_dict(result.strategy_patch),
        "diagnostics": {
            "lookback_days_used": result.diagnostics.lookback_days_used,
            "requested_more_data": result.diagnostics.requested_more_data,
            "request": (
                {
                    "lookback_days": result.diagnostics.request.lookback_days,
                    "why": result.diagnostics.request.why,
                }
                if result.diagnostics.request
                else None
            ),
        },
    }


def operator_ai_result_to_json(result: OperatorAIResult) -> str:
    return json.dumps(operator_ai_result_to_dict(result), ensure_ascii=False)


def _fallback_result(reason: str) -> OperatorAIResult:
    profile = "CONSERVATIVE"
    preset = get_profile_preset(profile)
    levels = preset.levels_min
    step_pct = 0.3
    min_range = max(preset.min_range_pct, step_pct * (levels / 2))
    tp_pct = max(preset.target_profit_pct + preset.safety_margin_pct, 0.2)
    return OperatorAIResult(
        analysis_result=AnalysisResult(
            state="WARNING",
            summary=f"Fallback: {reason}",
            trend="MIXED",
            volatility="MED",
            confidence=0.2,
            risks=[reason],
            data_quality=AnalysisDataQuality(
                ws="NO",
                http="NO",
                klines="NO",
                trades="NO",
                orderbook="NO",
                notes="Fallback response due to parse failure.",
            ),
        ),
        strategy_patch=StrategyPatch(
            profile=profile,
            bias="FLAT",
            range_mode="MANUAL",
            step_pct=step_pct,
            range_down_pct=min_range,
            range_up_pct=min_range,
            levels=levels,
            tp_pct=tp_pct,
            max_active_orders=preset.max_active_orders,
            notes="Fallback conservative patch.",
        ),
        diagnostics=OperatorAIDiagnostics(
            lookback_days_used=1,
            requested_more_data=False,
            request=None,
        ),
    )

def _normalize_state(state: str) -> str:
    if state in _ALLOWED_STATES:
        return state
    legacy = {
        "SAFE": "OK",
        "WARN": "WARNING",
        "WAIT": "WARNING",
        "DO_NOT_TRADE": "DANGER",
    }
    return legacy.get(state, "WARNING")


def _normalize_trend(trend: Any) -> str:
    if not trend:
        return "MIXED"
    normalized = str(trend).strip().upper()
    return normalized if normalized in _ALLOWED_TRENDS else "MIXED"


def _normalize_volatility(value: Any) -> str:
    if not value:
        return "MED"
    normalized = str(value).strip().upper()
    mapping = {"MEDIUM": "MED"}
    normalized = mapping.get(normalized, normalized)
    return normalized if normalized in _ALLOWED_VOLATILITY else "MED"


def _normalize_quality(value: Any, allowed: set[str], default: str) -> str:
    if not value:
        return default
    normalized = str(value).strip().upper()
    return normalized if normalized in allowed else default


def _parse_analysis_result(payload: dict[str, Any]) -> AnalysisResult:
    state = _normalize_state(str(payload.get("state", "WARNING")).strip().upper())
    summary = str(payload.get("summary", "")).strip()[:320]
    trend = _normalize_trend(payload.get("trend"))
    volatility = _normalize_volatility(payload.get("volatility"))
    confidence = _to_float_or_none(payload.get("confidence"))
    if confidence is None:
        confidence = 0.4
    confidence = max(0.0, min(confidence, 1.0))
    risks = payload.get("risks")
    if not isinstance(risks, list):
        risks = []
    risks = [str(item) for item in risks if item]
    dq_payload = payload.get("data_quality") if isinstance(payload.get("data_quality"), dict) else {}
    data_quality = AnalysisDataQuality(
        ws=_normalize_quality(dq_payload.get("ws"), _ALLOWED_DATA_QUALITY, "NO"),
        http=_normalize_quality(dq_payload.get("http"), _ALLOWED_HTTP_QUALITY, "NO"),
        klines=_normalize_quality(dq_payload.get("klines"), _ALLOWED_HTTP_QUALITY, "NO"),
        trades=_normalize_quality(dq_payload.get("trades"), _ALLOWED_HTTP_QUALITY, "NO"),
        orderbook=_normalize_quality(dq_payload.get("orderbook"), _ALLOWED_HTTP_QUALITY, "NO"),
        notes=str(dq_payload.get("notes", "")).strip()[:320],
    )
    return AnalysisResult(
        state=state,
        summary=summary or "No summary provided.",
        trend=trend,
        volatility=volatility,
        confidence=confidence,
        risks=risks,
        data_quality=data_quality,
    )


def _parse_strategy_patch(payload: Any) -> StrategyPatch:
    profile = str(payload.get("profile", "BALANCED")).strip().upper()
    if profile not in _ALLOWED_PROFILES:
        profile = "BALANCED"
    preset = get_profile_preset(profile)
    bias = str(payload.get("bias", "FLAT")).strip().upper()
    if bias not in _ALLOWED_BIAS:
        bias = "FLAT"
    range_mode = str(payload.get("range_mode", "MANUAL")).strip().upper()
    if range_mode not in _ALLOWED_RANGE_MODE:
        range_mode = "MANUAL"
    step_pct = _to_float_or_none(payload.get("step_pct"))
    range_down = _to_float_or_none(payload.get("range_down_pct"))
    range_up = _to_float_or_none(payload.get("range_up_pct"))
    levels = _to_int_or_none(payload.get("levels"))
    tp_pct = _to_float_or_none(payload.get("tp_pct"))
    max_orders = _to_int_or_none(payload.get("max_active_orders"))
    notes = str(payload.get("notes", "")).strip()[:240]

    if step_pct is None or step_pct <= 0:
        step_pct = 0.3
    if levels is None or levels <= 0:
        levels = preset.levels_min
    min_range = max(preset.min_range_pct, step_pct * (levels / 2))
    if range_down is None or range_down <= 0:
        range_down = min_range
    if range_up is None or range_up <= 0:
        range_up = min_range
    if tp_pct is None or tp_pct <= 0:
        tp_pct = max(preset.target_profit_pct + preset.safety_margin_pct, 0.2)
    if max_orders is None or max_orders <= 0:
        max_orders = preset.max_active_orders

    return StrategyPatch(
        profile=profile,
        bias=bias,
        range_mode=range_mode,
        step_pct=step_pct,
        range_down_pct=range_down,
        range_up_pct=range_up,
        levels=levels,
        tp_pct=tp_pct,
        max_active_orders=max_orders,
        notes=notes,
    )


def _parse_diagnostics(payload: dict[str, Any]) -> OperatorAIDiagnostics:
    lookback_days = _to_int_or_none(payload.get("lookback_days_used")) or 1
    if lookback_days not in _ALLOWED_LOOKBACK:
        lookback_days = 1
    requested = bool(payload.get("requested_more_data"))
    request_payload = payload.get("request") if isinstance(payload.get("request"), dict) else None
    request = None
    if isinstance(request_payload, dict):
        requested_days = _to_int_or_none(request_payload.get("lookback_days"))
        why = str(request_payload.get("why", "")).strip()[:240]
        if requested_days in _ALLOWED_LOOKBACK:
            request = OperatorAIRequest(lookback_days=requested_days, why=why)
    return OperatorAIDiagnostics(
        lookback_days_used=lookback_days,
        requested_more_data=requested,
        request=request,
    )


def _strategy_patch_to_dict(patch: StrategyPatch) -> dict[str, Any]:
    return {
        "profile": patch.profile,
        "bias": patch.bias,
        "range_mode": patch.range_mode,
        "step_pct": patch.step_pct,
        "range_down_pct": patch.range_down_pct,
        "range_up_pct": patch.range_up_pct,
        "levels": patch.levels,
        "tp_pct": patch.tp_pct,
        "max_active_orders": patch.max_active_orders,
        "notes": patch.notes,
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
