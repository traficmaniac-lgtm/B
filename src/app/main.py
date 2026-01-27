from __future__ import annotations

import atexit
import logging
import os
import sys
import time
from pathlib import Path

import faulthandler

from PySide6.QtWidgets import QApplication

from src.core.config import Config, load_config
from src.core.crash_guard import install_crash_guard
from src.core.httpx_singleton import close_shared_client
from src.services.net import shutdown_net_worker
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


def _install_crash_catcher(logger: logging.LoggerAdapter, root_path: Path) -> Path:
    crash_dir = root_path / "logs" / "crash"
    crash_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    crash_path = crash_dir / f"APP_{timestamp}.log"
    crash_file = crash_path.open("a", buffering=1, encoding="utf-8")
    faulthandler.enable(crash_file, all_threads=True)
    logger.info("Crash log enabled: %s", os.fspath(crash_path))

    def _write_line(message: str) -> None:
        try:
            crash_file.write(f"{message}\n")
            crash_file.flush()
        except Exception:
            return

    pid = os.getpid()
    _write_line(f"=== START pid={pid} ts={time.strftime('%Y-%m-%d %H:%M:%S')} ===")

    def _on_clean_exit() -> None:
        _write_line(f"=== CLEAN EXIT pid={pid} ts={time.strftime('%Y-%m-%d %H:%M:%S')} ===")

    atexit.register(_on_clean_exit)

    return crash_path


def main() -> int:
    root_path = Path(__file__).resolve().parents[2]
    config = load_config(root_path / "config.json")
    configure_logging(config.app.log_level)
    logger = get_logger("app")

    app_state = _load_app_state(config, root_path)
    logging.getLogger().setLevel(app_state.log_level)

    crash_path = _install_crash_catcher(logger, root_path)
    install_crash_guard(logger, crash_log_path=crash_path)

    app = QApplication(sys.argv)
    window = MainWindow(config, app_state)
    logging.getLogger().addHandler(window.log_handler)
    logger.info("application starting")
    window.show()
    exit_code = app.exec()
    logger.info("application closed")
    shutdown_net_worker()
    close_shared_client()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
