from src.services.nc_pilot.kpi_state import has_valid_bidask


def test_has_valid_bidask_transitions_from_missing() -> None:
    assert has_valid_bidask(None, None) is False
    assert has_valid_bidask(1.0, 1.1) is True
