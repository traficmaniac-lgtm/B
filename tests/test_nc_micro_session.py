from src.services.nc_micro.session import NcMicroSession


def test_sessions_isolated_configs() -> None:
    session_one = NcMicroSession(symbol="EURIUSDT")
    session_two = NcMicroSession(symbol="EURIUSDC")

    session_one.config.budget = 500.0
    session_one.config.grid_step_pct = 0.25

    assert session_two.config.budget != session_one.config.budget
    assert session_two.config.grid_step_pct != session_one.config.grid_step_pct


def test_minute_summary_timer() -> None:
    session = NcMicroSession(symbol="EURIUSDT")
    now = 0.0

    assert session.should_emit_minute_summary(now) is False
    assert session.should_emit_minute_summary(30.0) is False
    assert session.should_emit_minute_summary(60.0) is True
    assert session.should_emit_minute_summary(61.0) is False
    assert session.should_emit_minute_summary(120.0) is True
