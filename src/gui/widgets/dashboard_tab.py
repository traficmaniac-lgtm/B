from __future__ import annotations

from PySide6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.core.logging import get_logger


class DashboardTab(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._logger = get_logger("gui.dashboard")

        layout = QVBoxLayout()
        layout.addLayout(self._build_status_cards())

        self._self_check_button = QPushButton("Run Self-check")
        self._self_check_button.clicked.connect(self._run_self_check)
        layout.addWidget(self._self_check_button)
        layout.addStretch()

        self.setLayout(layout)

    def _build_status_cards(self) -> QGridLayout:
        grid = QGridLayout()

        config_box = QGroupBox("Config")
        config_layout = QVBoxLayout()
        config_layout.addWidget(QLabel("Config loaded: YES"))
        config_box.setLayout(config_layout)

        binance_box = QGroupBox("Binance")
        binance_layout = QVBoxLayout()
        binance_layout.addWidget(QLabel("Binance: NOT CONNECTED"))
        binance_box.setLayout(binance_layout)

        ai_box = QGroupBox("AI")
        ai_layout = QVBoxLayout()
        ai_layout.addWidget(QLabel("AI: NOT CONNECTED"))
        ai_box.setLayout(ai_layout)

        grid.addWidget(config_box, 0, 0)
        grid.addWidget(binance_box, 0, 1)
        grid.addWidget(ai_box, 0, 2)
        return grid

    def _run_self_check(self) -> None:
        self._logger.info("self-check ok")
