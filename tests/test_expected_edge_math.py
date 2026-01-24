from __future__ import annotations

from src.core.micro_edge import compute_expected_edge_bps


def test_expected_edge_math_alignment() -> None:
    kpi_result = compute_expected_edge_bps(
        spread_bps=1.7,
        fees_roundtrip_bps=0.2,
        slippage_bps=0.3,
        pad_bps=0.5,
        risk_buffer_bps=0.1,
    )
    guard_result = compute_expected_edge_bps(
        spread_bps=1.7,
        fees_roundtrip_bps=0.2,
        slippage_bps=0.3,
        pad_bps=0.5,
        risk_buffer_bps=0.1,
    )
    assert kpi_result.expected_profit_bps == guard_result.expected_profit_bps
    assert kpi_result.edge_raw_bps == 1.2
    assert kpi_result.expected_profit_bps == 0.6
