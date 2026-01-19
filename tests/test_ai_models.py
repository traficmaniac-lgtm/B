from __future__ import annotations

from src.ai.models import parse_ai_json


def test_parse_ai_json_clean() -> None:
    payload = """
    {
      "summary": {
        "market_type": "range",
        "volatility": "low",
        "liquidity": "ok",
        "spread": "0.1%",
        "advice": "Keep grid tight."
      },
      "plan": {
        "mode": "grid",
        "grid_count": 10,
        "grid_step_pct": 0.3,
        "range_low_pct": 1.0,
        "range_high_pct": 1.2,
        "budget_usdt": 250
      },
      "preview_levels": {
        "levels": [
          {"side": "BUY", "price": 99.5, "qty": 0.1, "pct_from_mid": -0.5}
        ]
      },
      "risk_notes": ["Range break risk"],
      "questions": [],
      "tool_requests": []
    }
    """
    response = parse_ai_json(payload)
    assert response is not None
    assert response.plan.grid_count == 10
    assert response.summary.market_type == "range"
    assert response.preview_levels[0].side == "BUY"


def test_parse_ai_json_with_text_around() -> None:
    payload = """
    Here is your analysis:
    {
      "summary": {
        "market_type": "trend",
        "volatility": "high",
        "liquidity": "thin",
        "spread": "0.4%",
        "advice": "Reduce size."
      },
      "plan": {
        "mode": "adaptive",
        "grid_count": 12,
        "grid_step_pct": 0.4,
        "range_low_pct": 2.0,
        "range_high_pct": 2.2,
        "budget_usdt": 300
      },
      "preview_levels": {
        "levels": []
      },
      "risk_notes": [],
      "questions": [],
      "tool_requests": []
    }
    Thanks!
    """
    response = parse_ai_json(payload)
    assert response is not None
    assert response.plan.budget_usdt == 300
    assert response.summary.volatility == "high"


def test_parse_ai_json_broken() -> None:
    payload = '{"summary": {"market_type": "range", "volatility": "low"'
    response = parse_ai_json(payload)
    assert response is None
