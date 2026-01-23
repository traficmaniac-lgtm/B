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


def test_config_loads_defaults() -> None:
    from src.core.config import load_config

    config = load_config()
    assert config.prices.ttl_ms > 0
    assert config.prices.refresh_interval_ms > 0


def test_openai_client_import() -> None:
    pytest.importorskip("openai")
    from src.ai.openai_client import OpenAIClient
    from src.ai.models import AiAnalysisResult, AiResponseEnvelope, AiStrategyPatch

    assert OpenAIClient.__name__ == "OpenAIClient"
    assert AiAnalysisResult.__name__ == "AiAnalysisResult"
    assert AiResponseEnvelope.__name__ == "AiResponseEnvelope"
    assert AiStrategyPatch.__name__ == "AiStrategyPatch"


@pytest.mark.skipif(not PYSIDE_AVAILABLE, reason="PySide6 dependencies unavailable")
def test_imports() -> None:
    from src.app.main import main
    from src.gui.lite_all_strategy_nc_micro_window import LiteAllStrategyNcMicroWindow
    from src.gui.lite_all_strategy_terminal_window import LiteAllStrategyTerminalWindow
    from src.gui.main_window import MainWindow
    from src.gui.models.pair_state import BotState, PairState
    from src.gui.overview_tab import OverviewTab
    from src.gui.pair_workspace_tab import PairWorkspaceTab

    assert callable(main)
    assert LiteAllStrategyNcMicroWindow.__name__ == "LiteAllStrategyNcMicroWindow"
    assert LiteAllStrategyTerminalWindow.__name__ == "LiteAllStrategyTerminalWindow"
    assert MainWindow.__name__ == "MainWindow"
    assert OverviewTab.__name__ == "OverviewTab"
    assert PairWorkspaceTab.__name__ == "PairWorkspaceTab"
    assert BotState.__name__ == "BotState"
    assert PairState.__name__ == "PairState"


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


@pytest.mark.skipif(not PYSIDE_AVAILABLE, reason="PySide6 dependencies unavailable")
def test_plan_preview_table_populates() -> None:
    from src.gui.models.app_state import AppState
    from src.gui.pair_workspace_tab import PairWorkspaceTab

    app = QApplication.instance() or QApplication([])
    app_state = AppState(env="TEST", log_level="INFO", config_path="config.yml")
    tab = PairWorkspaceTab(symbol="ETHUSDT", app_state=app_state)
    tab._rebuild_plan_preview(reason="built")
    assert tab._plan_preview_table.rowCount() > 0
