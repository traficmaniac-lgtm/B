from __future__ import annotations

from typing import Any, Callable

from src.ai.operator_models import OperatorAIResult


def run_request_data_loop(
    *,
    datapack: dict[str, Any],
    ai_call: Callable[[dict[str, Any]], OperatorAIResult],
    fetch_extra: Callable[[int], dict[str, Any]],
    max_rounds: int = 1,
) -> tuple[OperatorAIResult, dict[str, Any], bool]:
    current_pack = dict(datapack)
    last_response = ai_call(current_pack)
    for _ in range(max_rounds):
        diagnostics = last_response.diagnostics
        if not diagnostics.requested_more_data or not diagnostics.request:
            return last_response, current_pack, False
        requested_days = diagnostics.request.lookback_days
        extra_data = fetch_extra(requested_days)
        current_pack = dict(current_pack)
        current_pack["extra_data"] = extra_data
        last_response = ai_call(current_pack)
    return last_response, current_pack, True
