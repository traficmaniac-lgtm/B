from typing import Any

from src.ai.operator_models import (
    AnalysisDataQuality,
    AnalysisResult,
    OperatorAIDiagnostics,
    OperatorAIRequest,
    OperatorAIResult,
    StrategyPatch,
)
from src.ai.operator_request_loop import run_request_data_loop


def _make_response(need_more: bool) -> OperatorAIResult:
    request = OperatorAIRequest(lookback_days=7, why="test") if need_more else None
    return OperatorAIResult(
        analysis_result=AnalysisResult(
            state="OK",
            summary="test",
            trend="MIXED",
            volatility="MED",
            confidence=0.5,
            risks=[],
            data_quality=AnalysisDataQuality(
                ws="OK",
                http="OK",
                klines="OK",
                trades="OK",
                orderbook="OK",
                notes="",
            ),
        ),
        strategy_patch=StrategyPatch(
            profile="BALANCED",
            bias="FLAT",
            range_mode="MANUAL",
            step_pct=0.3,
            range_down_pct=1.5,
            range_up_pct=1.5,
            levels=4,
            tp_pct=0.3,
            max_active_orders=8,
            notes="",
        ),
        diagnostics=OperatorAIDiagnostics(
            lookback_days_used=1,
            requested_more_data=need_more,
            request=request,
        ),
    )


def test_request_data_loop_fetches_and_requeries() -> None:
    calls: list[dict[str, Any]] = []

    def ai_call(_: dict[str, Any]) -> OperatorAIResult:
        calls.append({"called": True})
        return _make_response(need_more=len(calls) < 2)

    def fetch_extra(lookback_days: int) -> dict[str, Any]:
        return {"lookback_days": lookback_days, "payload": "ok"}

    response, pack, exhausted = run_request_data_loop(
        datapack={"meta": {"symbol": "BTCUSDT"}},
        ai_call=ai_call,
        fetch_extra=fetch_extra,
        max_rounds=3,
    )
    assert not exhausted
    assert response.diagnostics.requested_more_data is False
    assert "extra_data" in pack


def test_request_data_loop_fallback_wait() -> None:
    def ai_call(_: dict[str, Any]) -> OperatorAIResult:
        return _make_response(need_more=True)

    def fetch_extra(lookback_days: int) -> dict[str, Any]:
        return {"lookback_days": lookback_days}

    response, _, exhausted = run_request_data_loop(
        datapack={"meta": {"symbol": "BTCUSDT"}},
        ai_call=ai_call,
        fetch_extra=fetch_extra,
        max_rounds=3,
    )
    assert exhausted
    assert response.diagnostics.requested_more_data is True


def test_request_data_loop_runs_once_by_default() -> None:
    calls: list[int] = []

    def ai_call(_: dict[str, Any]) -> OperatorAIResult:
        return _make_response(need_more=True)

    def fetch_extra(lookback_days: int) -> dict[str, Any]:
        calls.append(lookback_days)
        return {"lookback_days": lookback_days}

    _, _, exhausted = run_request_data_loop(
        datapack={"meta": {"symbol": "BTCUSDT"}},
        ai_call=ai_call,
        fetch_extra=fetch_extra,
    )
    assert exhausted
    assert calls == [7]
