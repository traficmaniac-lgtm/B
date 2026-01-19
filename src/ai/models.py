from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from src.core.logging import get_logger


_LOGGER = get_logger("ai.models")


@dataclass
class AiSummary:
    market_type: str
    volatility: str
    liquidity: str
    spread: str | float
    advice: str


@dataclass
class AiPlan:
    mode: str
    grid_count: int
    grid_step_pct: float
    range_low_pct: float
    range_high_pct: float
    budget_usdt: float


@dataclass
class AiLevel:
    side: str
    price: float
    qty: float
    pct_from_mid: float


@dataclass
class AiResponse:
    summary: AiSummary
    plan: AiPlan
    preview_levels: list[AiLevel]
    risk_notes: list[str]
    questions: list[str]
    tool_requests: list[str]


@dataclass
class AiPatchResponse:
    patch: dict[str, Any]
    preview_levels: list[AiLevel] | None
    message: str | None
    questions: list[str]
    tool_requests: list[str]


def safe_json_loads(text: str) -> Any | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        _LOGGER.warning("Failed to parse AI JSON: %s", exc)
        return None


def parse_ai_json(text: str) -> AiResponse | None:
    if not text:
        return None
    for candidate in _extract_json_candidates(text):
        payload = safe_json_loads(candidate)
        if isinstance(payload, dict):
            response = _build_ai_response(payload)
            if response is not None:
                return response
    return None


def parse_ai_patch_json(text: str) -> AiPatchResponse | None:
    if not text:
        return None
    for candidate in _extract_json_candidates(text):
        payload = safe_json_loads(candidate)
        if isinstance(payload, dict):
            response = _build_ai_patch_response(payload)
            if response is not None:
                return response
    return None


def _extract_json_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    depth = 0
    start_idx: int | None = None
    for idx, char in enumerate(text):
        if char == "{":
            if depth == 0:
                start_idx = idx
            depth += 1
        elif char == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start_idx is not None:
                candidates.append(text[start_idx : idx + 1])
                start_idx = None
    if text.strip().startswith("{") and text.strip().endswith("}"):
        candidates.append(text.strip())
    return candidates


def _build_ai_response(payload: dict[str, Any]) -> AiResponse | None:
    summary_payload = payload.get("summary")
    plan_payload = payload.get("plan")
    if not isinstance(summary_payload, dict) or not isinstance(plan_payload, dict):
        return None
    summary = AiSummary(
        market_type=str(summary_payload.get("market_type", "Unknown")),
        volatility=str(summary_payload.get("volatility", "Unknown")),
        liquidity=str(summary_payload.get("liquidity", "Unknown")),
        spread=summary_payload.get("spread", "Unknown"),
        advice=str(summary_payload.get("advice", "")),
    )
    plan = _parse_plan(plan_payload)
    if plan is None:
        return None
    preview_levels = _parse_levels(payload.get("preview_levels"))
    risk_notes = _parse_string_list(payload.get("risk_notes"))
    questions = _parse_string_list(payload.get("questions"))
    tool_requests = _parse_string_list(payload.get("tool_requests"))
    return AiResponse(
        summary=summary,
        plan=plan,
        preview_levels=preview_levels,
        risk_notes=risk_notes,
        questions=questions,
        tool_requests=tool_requests,
    )


def _build_ai_patch_response(payload: dict[str, Any]) -> AiPatchResponse | None:
    patch_payload = payload.get("patch")
    if not isinstance(patch_payload, dict):
        return None
    preview_levels = _parse_levels(payload.get("preview_levels"))
    if not preview_levels:
        preview_levels = None
    message = payload.get("message")
    questions = _parse_string_list(payload.get("questions"))
    tool_requests = _parse_string_list(payload.get("tool_requests"))
    return AiPatchResponse(
        patch=patch_payload,
        preview_levels=preview_levels,
        message=str(message) if message is not None else None,
        questions=questions,
        tool_requests=tool_requests,
    )


def _parse_plan(plan_payload: dict[str, Any]) -> AiPlan | None:
    required = ("mode", "grid_count", "grid_step_pct", "range_low_pct", "range_high_pct", "budget_usdt")
    if not all(key in plan_payload for key in required):
        return None
    return AiPlan(
        mode=str(plan_payload.get("mode")),
        grid_count=int(float(plan_payload.get("grid_count"))),
        grid_step_pct=float(plan_payload.get("grid_step_pct")),
        range_low_pct=float(plan_payload.get("range_low_pct")),
        range_high_pct=float(plan_payload.get("range_high_pct")),
        budget_usdt=float(plan_payload.get("budget_usdt")),
    )


def _parse_levels(payload: Any) -> list[AiLevel]:
    levels_payload: Any = payload
    if isinstance(levels_payload, dict):
        levels_payload = levels_payload.get("levels")
    if not isinstance(levels_payload, list):
        return []
    levels: list[AiLevel] = []
    for item in levels_payload:
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


def _parse_string_list(payload: Any) -> list[str]:
    if not isinstance(payload, list):
        return []
    return [str(item) for item in payload if item is not None]
