from __future__ import annotations

import logging
import sys
import traceback
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox

from src.core.config import load_config
from src.core.logging import configure_logging, get_logger
from src.gui.main_window import MainWindow
from src.gui.models.app_state import AppState


def _load_app_state(config_env: str, config_log_level: str, root_path: Path) -> AppState:
    default_config_path = str(root_path / "config.json")
    defaults = AppState(
        env=config_env,
        log_level=config_log_level,
        config_path=default_config_path,
        show_logs=True,
    )
    user_config_path = root_path / "config.user.yaml"
    return AppState.load(user_config_path, defaults)


def _configure_exception_hook(logger: logging.LoggerAdapter) -> None:
    def handle_exception(exc_type: type[BaseException], exc: BaseException, tb: object) -> None:
        message = "".join(traceback.format_exception(exc_type, exc, tb))
        logger.error("Unhandled exception:\n%s", message)
        app = QApplication.instance()
        if app:
            QMessageBox.critical(None, "Unhandled exception", str(exc))

    sys.excepthook = handle_exception


def main() -> int:
    config = load_config()
    configure_logging(config.app.log_level)
    logger = get_logger("app")

    root_path = Path(__file__).resolve().parents[2]
    app_state = _load_app_state(config.app.env, config.app.log_level, root_path)
    logging.getLogger().setLevel(app_state.log_level)

    app = QApplication(sys.argv)
    window = MainWindow(config, app_state)
    logging.getLogger().addHandler(window.log_handler)
    _configure_exception_hook(logger)

    logger.info("application starting")
    window.show()
    exit_code = app.exec()
    logger.info("application closed")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
