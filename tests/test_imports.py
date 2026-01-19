from src.app.main import main
from src.gui.main_window import MainWindow


def test_imports() -> None:
    assert callable(main)
    assert MainWindow.__name__ == "MainWindow"
