from __future__ import annotations

from typing import Any, Callable

from src.ai.operator_models import OperatorAIMathCheck, OperatorAIRequestData, OperatorAIResult


def run_request_data_loop(
    *,
    datapack: dict[str, Any],
    ai_call: Callable[[dict[str, Any]], OperatorAIResult],
    fetch_extra: Callable[[list[dict[str, Any]]], dict[str, Any]],
    max_rounds: int = 3,
) -> tuple[OperatorAIResult, dict[str, Any], bool]:
    current_pack = dict(datapack)
    last_response = ai_call(current_pack)
    for _ in range(max_rounds):
        request = last_response.request_data
        if not request.need_more:
            return last_response, current_pack, False
        items = [
            {
                "id": item.id,
                "window": item.window,
                "limit": item.limit,
                "reason": item.reason,
                "levels": item.levels,
            }
            for item in request.items
        ]
        extra_data = fetch_extra(items)
        current_pack = dict(current_pack)
        current_pack["extra_data"] = extra_data
        last_response = ai_call(current_pack)
    fallback = _fallback_wait("insufficient data")
    return fallback, current_pack, True


def _fallback_wait(reason: str) -> OperatorAIResult:
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
