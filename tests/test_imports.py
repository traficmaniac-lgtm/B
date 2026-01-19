from src.app.main import main
from src.gui.main_window import MainWindow
from src.gui.pair_workspace_window import PairWorkspaceWindow
from src.services.ai_provider import AIProvider


def test_imports() -> None:
    assert callable(main)
    assert MainWindow.__name__ == "MainWindow"
    assert PairWorkspaceWindow.__name__ == "PairWorkspaceWindow"
    assert AIProvider.__name__ == "AIProvider"
