from __future__ import annotations

from src.core.nc_micro_safe_call import safe_call


def test_safe_call_runs_fn() -> None:
    touched = {"value": 0}

    def _fn() -> None:
        touched["value"] += 1

    safe_call(_fn, label="test:run")

    assert touched["value"] == 1


def test_safe_call_handles_exception_and_calls_on_error() -> None:
    touched = {"on_error": False}

    def _fn() -> None:
        raise RuntimeError("boom")

    def _on_error() -> None:
        touched["on_error"] = True

    safe_call(_fn, label="test:error", on_error=_on_error)

    assert touched["on_error"] is True
