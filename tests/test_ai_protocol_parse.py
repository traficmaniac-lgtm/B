from src.ai.models import parse_ai_response


def test_parse_ai_analysis_result_valid() -> None:
    payload = {
        "type": "analysis_result",
        "status": "OK",
        "confidence": 0.71,
        "reason_codes": ["RANGE"],
        "message": "Stable range",
        "analysis_result": {
            "summary_lines": ["Range-bound market", "Spread stable"],
            "strategy": {
                "strategy_id": "GRID_CLASSIC",
                "budget_usdt": 500,
                "levels": [
                    {"side": "BUY", "price": 100.0, "qty": 0.5, "pct_from_mid": -1.0}
                ],
                "grid_step_pct": 0.3,
                "range_low_pct": 1.5,
                "range_high_pct": 1.8,
                "bias": {"buy_pct": 50, "sell_pct": 50},
                "order_size_mode": "equal",
                "max_orders": 12,
                "max_exposure_pct": 35,
            },
            "risk": {
                "hard_stop_pct": 8,
                "cooldown_minutes": 15,
                "soft_stop_rules": ["cooldown if spread widens"],
                "kill_switch_rules": ["halt on drawdown"],
                "volatility_mode": "medium",
            },
            "control": {
                "recheck_interval_sec": 60,
                "ai_reanalyze_interval_sec": 300,
                "min_change_to_rebuild_pct": 0.3,
            },
            "actions": [
                {"action": "note", "detail": "Watch breakout", "severity": "info"}
            ],
        },
    }
    envelope = parse_ai_response(json_dumps(payload), expected_type="analysis_result")
    assert envelope.status == "OK"
    assert envelope.analysis_result is not None
    assert envelope.analysis_result.strategy.strategy_id == "GRID_CLASSIC"


def test_parse_ai_invalid_json_returns_error() -> None:
    envelope = parse_ai_response("{not_json}", expected_type="analysis_result")
    assert envelope.status == "ERROR"


def json_dumps(payload: dict) -> str:
    import json

    return json.dumps(payload)
