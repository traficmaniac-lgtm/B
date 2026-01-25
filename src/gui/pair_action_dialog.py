from __future__ import annotations

from typing import Callable

from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.core.logging import get_logger
from src.gui.models.pair_mode import (
    PAIR_MODE_ALGO_PILOT,
    PAIR_MODE_LITE,
    PAIR_MODE_LITE_ALL_STRATEGY,
    PAIR_MODE_NC_MICRO,
    PairMode,
)


class PairActionDialog(QDialog):
    def __init__(self, symbol: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._symbol = symbol
        self.selected_mode: PairMode | None = None
        self._logger = get_logger("gui.pair_action_dialog")

        self.setWindowTitle(f"Работа с парой: {symbol}")
        self.setModal(True)
        self.resize(520, 340)

        layout = QVBoxLayout()
        layout.addWidget(self._build_option_card(
            title="Lite Grid Terminal",
            description="старый родной Lite (LIVE grid)",
            button_text="Open Lite Grid Terminal",
            on_click=lambda: self._choose_mode(PAIR_MODE_LITE),
        ))
        layout.addWidget(self._build_option_card(
            title="Lite All Strategy Terminal (v1.0)",
            description="клон Lite для экспериментов со стратегиями",
            button_text="Open Lite All Strategy",
            on_click=lambda: self._choose_mode(PAIR_MODE_LITE_ALL_STRATEGY),
        ))
        layout.addWidget(self._build_option_card(
            title="Lite All Strategy — ALGO PILOT",
            description="Lite All Strategy Terminal — ALGO PILOT",
            button_text="Open Lite All Strategy — ALGO PILOT",
            on_click=lambda: self._choose_mode(PAIR_MODE_ALGO_PILOT),
        ))
        layout.addWidget(self._build_option_card(
            title="NC MICRO",
            description="No-Commission Micro Grid (auto pilot)",
            button_text="Open NC MICRO",
            on_click=lambda: self._choose_mode(PAIR_MODE_NC_MICRO),
        ))
        layout.addStretch()
        self.setLayout(layout)

    def _build_option_card(
        self,
        title: str,
        description: str,
        button_text: str,
        on_click: Callable[[], None],
    ) -> QFrame:
        card = QFrame()
        card.setFrameShape(QFrame.StyledPanel)
        card_layout = QVBoxLayout()

        title_label = QLabel(title)
        title_label.setStyleSheet("font-weight: 600; font-size: 14px;")
        card_layout.addWidget(title_label)

        description_label = QLabel(description)
        description_label.setWordWrap(True)
        description_label.setStyleSheet("color: #6b7280;")
        card_layout.addWidget(description_label)

        buttons_row = QHBoxLayout()
        buttons_row.addStretch()
        action_button = QPushButton(button_text)
        action_button.clicked.connect(on_click)
        buttons_row.addWidget(action_button)
        card_layout.addLayout(buttons_row)

        card.setLayout(card_layout)
        return card

    def _choose_mode(self, mode: PairMode) -> None:
        self.selected_mode = mode
        self._logger.info("[MODE] selected=%s symbol=%s", mode.name, self._symbol)
        self.accept()
