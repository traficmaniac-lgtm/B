import pytest

pytest.importorskip("PySide6")

from src.app.main import main
from src.gui.main_window import MainWindow
from src.gui.models.pair_state import PairRunState
from src.gui.overview_tab import OverviewTab
from src.gui.pair_workspace_tab import PairWorkspaceTab


def test_imports() -> None:
    assert callable(main)
    assert MainWindow.__name__ == "MainWindow"
    assert OverviewTab.__name__ == "OverviewTab"
    assert PairWorkspaceTab.__name__ == "PairWorkspaceTab"
    assert PairRunState.__name__ == "PairRunState"
