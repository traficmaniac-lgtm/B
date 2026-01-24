from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MicroEdgeResult:
    edge_raw_bps: float
    expected_profit_bps: float


def compute_expected_edge_bps(
    spread_bps: float,
    fees_roundtrip_bps: float,
    slippage_bps: float,
    pad_bps: float,
    risk_buffer_bps: float,
) -> MicroEdgeResult:
    edge_raw_bps = spread_bps - fees_roundtrip_bps - slippage_bps
    expected_profit_bps = edge_raw_bps - pad_bps - risk_buffer_bps
    return MicroEdgeResult(edge_raw_bps=edge_raw_bps, expected_profit_bps=expected_profit_bps)
