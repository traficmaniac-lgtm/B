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

from src.gui.models.pair_mode import (
    PAIR_MODE_LITE,
    PAIR_MODE_AI_OPERATOR_GRID,
    PAIR_MODE_TRADE_READY,
    PAIR_MODE_TRADING,
    PairMode,
)


class PairActionDialog(QDialog):
    def __init__(self, symbol: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._symbol = symbol
        self.selected_mode: PairMode | None = None

        self.setWindowTitle(f"Работа с парой: {symbol}")
        self.setModal(True)
        self.resize(520, 300)

        layout = QVBoxLayout()
        layout.addWidget(self._build_option_card(
            title="Lite Mode (Grid Terminal)",
            description="Быстрый торговый терминал (LIVE grid) — старый родной Lite",
            button_text="Open Lite Mode",
            on_click=lambda: self._choose_mode(PAIR_MODE_LITE),
        ))
        layout.addWidget(self._build_option_card(
            title="Торговля",
            description="Открыть окно торговли с ордерами, позицией и AI-наблюдателем",
            button_text="Открыть Trading Workspace",
            on_click=lambda: self._choose_mode(PAIR_MODE_TRADING),
        ))
        layout.addWidget(self._build_option_card(
            title="Trade Ready Mode",
            description="Продвинутый режим подготовки торговли с AI-анализом и диалогом",
            button_text="Открыть Trade Ready Mode",
            on_click=lambda: self._choose_mode(PAIR_MODE_TRADE_READY),
        ))
        layout.addWidget(self._build_option_card(
            title="AI Operator Grid",
            description="Adaptive Micro-Grid Market Making (AI Operator)",
            button_text="Open AI Operator Grid",
            on_click=lambda: self._choose_mode(PAIR_MODE_AI_OPERATOR_GRID),
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
        self.accept()
