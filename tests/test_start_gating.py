from src.core.strategies.manual_runtime import check_manual_start


def test_manual_start_allows_dry_run() -> None:
    decision = check_manual_start(dry_run=True, trade_gate="read-only")
    assert decision.ok
    assert decision.reason == "dry_run"


def test_manual_start_blocks_live_when_gate_disabled() -> None:
    decision = check_manual_start(dry_run=False, trade_gate="read-only")
    assert not decision.ok
    assert decision.reason == "trade_gate=read-only"
