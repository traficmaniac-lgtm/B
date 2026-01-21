from src.ai.operator_state_machine import AiOperatorState, AiOperatorStateMachine


def test_state_machine_transitions() -> None:
    machine = AiOperatorStateMachine()

    assert machine.state == AiOperatorState.IDLE
    machine.set_data_ready()
    assert machine.state == AiOperatorState.DATA_READY

    assert machine.start_analyzing()
    assert machine.state == AiOperatorState.ANALYZING

    assert machine.set_plan_ready()
    assert machine.state == AiOperatorState.PLAN_READY

    assert machine.apply_plan()
    assert machine.state == AiOperatorState.PLAN_APPLIED
    assert machine.ai_confidence_locked

    assert machine.start()
    assert machine.state == AiOperatorState.RUNNING

    assert machine.pause()
    assert machine.state == AiOperatorState.WATCHING

    assert machine.stop()
    assert machine.state == AiOperatorState.IDLE
    assert not machine.ai_confidence_locked


def test_start_blocked_without_plan_applied() -> None:
    machine = AiOperatorStateMachine()

    assert not machine.start()
    assert machine.state == AiOperatorState.IDLE

    machine.set_data_ready()
    assert not machine.start()
    assert machine.state == AiOperatorState.DATA_READY


def test_apply_plan_requires_plan_ready() -> None:
    machine = AiOperatorStateMachine()

    assert not machine.apply_plan()
    assert machine.state == AiOperatorState.IDLE

    machine.set_data_ready()
    machine.start_analyzing()
    assert not machine.apply_plan()
    assert machine.state == AiOperatorState.ANALYZING


def test_manual_invalidate_clears_confidence() -> None:
    machine = AiOperatorStateMachine()
    machine.set_data_ready()
    machine.start_analyzing()
    machine.set_plan_ready()
    machine.apply_plan()

    assert machine.ai_confidence_locked
    machine.invalidate_manual()
    assert machine.state == AiOperatorState.INVALID
    assert not machine.ai_confidence_locked
