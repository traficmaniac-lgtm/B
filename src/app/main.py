from __future__ import annotations

import logging
import sys
import traceback
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox

from src.core.config import Config, load_config
from src.core.logging import configure_logging, get_logger
from src.gui.main_window import MainWindow
from src.gui.models.app_state import AppState


def _load_app_state(config: Config, root_path: Path) -> AppState:
    default_config_path = str((root_path / "config.json").resolve())
    price_ttl_ms = getattr(config.prices, "ttl_ms", 2500)
    price_refresh_ms = getattr(config.prices, "refresh_interval_ms", 500)
    defaults = AppState(
        env=config.app.env,
        log_level=config.app.log_level,
        config_path=default_config_path,
        show_logs=True,
        binance_api_key="",
        binance_api_secret="",
        openai_api_key="",
        openai_model="gpt-4o-mini",
        default_period="4h",
        default_quality="Standard",
        price_ttl_ms=price_ttl_ms,
        price_refresh_ms=price_refresh_ms,
        default_quote="USDT",
        pnl_period="24h",
        ai_connected=False,
        ai_checked=False,
    )
    user_config_path = root_path / "config.user.yaml"
    app_state = AppState.load(user_config_path, defaults)
    if not user_config_path.exists():
        app_state.save(user_config_path)
    return app_state


def _configure_exception_hook(logger: logging.LoggerAdapter) -> None:
    def handle_exception(exc_type: type[BaseException], exc: BaseException, tb: object) -> None:
        message = "".join(traceback.format_exception(exc_type, exc, tb))
        logger.error("Unhandled exception:\n%s", message)
        app = QApplication.instance()
        if app:
            QMessageBox.critical(None, "Unhandled exception", str(exc))

    sys.excepthook = handle_exception


def main() -> int:
    root_path = Path(__file__).resolve().parents[2]
    config = load_config(root_path / "config.json")
    configure_logging(config.app.log_level)
    logger = get_logger("app")

    app_state = _load_app_state(config, root_path)
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
