from src.gui.models.pair_state import BotState, PairState


def test_bot_state_transitions() -> None:
    pair_state = PairState()
    assert pair_state.state == BotState.IDLE

    previous = pair_state.set_state(BotState.PREPARING_DATA, reason="prepare")
    assert previous == BotState.IDLE
    assert pair_state.state == BotState.PREPARING_DATA
    assert pair_state.last_reason == "prepare"

    previous = pair_state.set_state(BotState.DATA_READY, reason="prepared")
    assert previous == BotState.PREPARING_DATA
    assert pair_state.state == BotState.DATA_READY
    assert pair_state.last_reason == "prepared"
