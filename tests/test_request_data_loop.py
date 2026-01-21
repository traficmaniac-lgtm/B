from typing import Any

from src.ai.operator_models import OperatorAIMathCheck, OperatorAIRequestData, OperatorAIRequestDataItem, OperatorAIResult
from src.ai.operator_request_loop import run_request_data_loop


def _make_response(need_more: bool) -> OperatorAIResult:
    request = OperatorAIRequestData(
        need_more=need_more,
        items=[
            OperatorAIRequestDataItem(
                id="klines_raw",
                window="1m",
                limit=10,
                reason="test",
                levels=None,
            )
        ]
        if need_more
        else [],
    )
    return OperatorAIResult(
        state="SAFE",
        recommendation="WAIT",
        next_action="REQUEST_DATA" if need_more else "WAIT",
        reason="test",
        profile="BALANCED",
        actions_allowed=["REQUEST_DATA", "WAIT"] if need_more else ["WAIT"],
        strategy_patch=None,
        request_data=request,
        math_check=OperatorAIMathCheck(net_edge_pct=None, break_even_tp_pct=None, assumptions={}),
    )


def test_request_data_loop_fetches_and_requeries() -> None:
    calls: list[dict[str, Any]] = []

    def ai_call(_: dict[str, Any]) -> OperatorAIResult:
        calls.append({"called": True})
        return _make_response(need_more=len(calls) < 2)

    def fetch_extra(items: list[dict[str, Any]]) -> dict[str, Any]:
        return {"items": items, "payload": "ok"}

    response, pack, exhausted = run_request_data_loop(
        datapack={"meta": {"symbol": "BTCUSDT"}},
        ai_call=ai_call,
        fetch_extra=fetch_extra,
        max_rounds=3,
    )
    assert not exhausted
    assert response.request_data.need_more is False
    assert "extra_data" in pack


def test_request_data_loop_fallback_wait() -> None:
    def ai_call(_: dict[str, Any]) -> OperatorAIResult:
        return _make_response(need_more=True)

    def fetch_extra(items: list[dict[str, Any]]) -> dict[str, Any]:
        return {"items": items}

    response, _, exhausted = run_request_data_loop(
        datapack={"meta": {"symbol": "BTCUSDT"}},
        ai_call=ai_call,
        fetch_extra=fetch_extra,
        max_rounds=3,
    )
    assert exhausted
    assert response.next_action == "WAIT"
