from __future__ import annotations

from PySide6.QtWidgets import QWidget

from src.core.config import Config
from src.core.logging import get_logger
from src.gui.modes.ai_full_strateg_v2.window import AiFullStrategV2Window
from src.gui.models.app_state import AppState
from src.services.price_feed_manager import PriceFeedManager


class AiFullStrategV2Controller:
    def __init__(
        self,
        config: Config | None,
        app_state: AppState | None,
        price_feed_manager: PriceFeedManager | None,
        parent: QWidget | None = None,
    ) -> None:
        self._config = config
        self._app_state = app_state
        self._price_feed_manager = price_feed_manager
        self._parent = parent
        self._logger = get_logger("gui.ai_full_strateg_v2")

    def open(self, symbol: str) -> AiFullStrategV2Window | None:
        if self._config is None or self._app_state is None or self._price_feed_manager is None:
            self._logger.error("AI Full Strateg v2.0 unavailable: missing runtime dependencies.")
            return None
        window = AiFullStrategV2Window(
            symbol=symbol,
            config=self._config,
            app_state=self._app_state,
            price_feed_manager=self._price_feed_manager,
            parent=self._parent,
        )
        window.show()
        return window
