import pytest

try:
    from PySide6.QtWidgets import QApplication, QComboBox, QDoubleSpinBox, QSpinBox
except ImportError:
    QApplication = None
    QComboBox = None
    QDoubleSpinBox = None
    QSpinBox = None
    PYSIDE_AVAILABLE = False
else:
    PYSIDE_AVAILABLE = True


@pytest.mark.skipif(not PYSIDE_AVAILABLE, reason="PySide6 dependencies unavailable")
def test_imports() -> None:
    from src.app.main import main
    from src.gui.main_window import MainWindow
    from src.gui.models.pair_state import PairRunState
    from src.gui.overview_tab import OverviewTab
    from src.gui.pair_workspace_tab import PairWorkspaceTab

    assert callable(main)
    assert MainWindow.__name__ == "MainWindow"
    assert OverviewTab.__name__ == "OverviewTab"
    assert PairWorkspaceTab.__name__ == "PairWorkspaceTab"
    assert PairRunState.__name__ == "PairRunState"


@pytest.mark.skipif(not PYSIDE_AVAILABLE, reason="PySide6 dependencies unavailable")
def test_strategy_form_widgets() -> None:
    from src.gui.models.app_state import AppState
    from src.gui.pair_workspace_tab import PairWorkspaceTab

    app = QApplication.instance() or QApplication([])
    app_state = AppState(env="TEST", log_level="INFO", config_path="config.yml")
    tab = PairWorkspaceTab(symbol="BTCUSDT", app_state=app_state)
    assert isinstance(tab._budget_input, QDoubleSpinBox)
    assert isinstance(tab._mode_input, QComboBox)
    assert isinstance(tab._grid_count_input, QSpinBox)
    assert isinstance(tab._grid_step_input, QDoubleSpinBox)
    assert isinstance(tab._range_low_input, QDoubleSpinBox)
    assert isinstance(tab._range_high_input, QDoubleSpinBox)
    assert tab._reset_ai_button.text() == "Reset to AI"
    assert tab._strategy_fields["budget"] is tab._budget_input
