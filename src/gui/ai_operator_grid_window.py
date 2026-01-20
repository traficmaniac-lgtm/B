from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from src.core.config import Config
from src.gui.lite_grid_window import LiteGridWindow
from src.gui.models.app_state import AppState
from src.services.price_feed_manager import PriceFeedManager


class AiOperatorGridWindow(LiteGridWindow):
    def __init__(
        self,
        symbol: str,
        config: Config,
        app_state: AppState,
        price_feed_manager: PriceFeedManager,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            symbol=symbol,
            config=config,
            app_state=app_state,
            price_feed_manager=price_feed_manager,
            parent=parent,
        )
        self.setWindowTitle(f"AI Operator Grid — {symbol}")

    def _build_body(self) -> QSplitter:
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)

        left_stack = QSplitter(Qt.Vertical)
        left_stack.setChildrenCollapsible(False)
        left_stack.addWidget(self._build_ai_panel())
        left_stack.addWidget(super()._build_market_panel())
        left_stack.setStretchFactor(0, 1)
        left_stack.setStretchFactor(1, 1)

        splitter.addWidget(left_stack)
        splitter.addWidget(super()._build_grid_panel())
        splitter.addWidget(super()._build_runtime_panel())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        splitter.setStretchFactor(2, 1)
        return splitter

    def _build_ai_panel(self) -> QWidget:
        group = QGroupBox("AI Operator")
        group.setStyleSheet(
            "QGroupBox { border: 1px solid #e5e7eb; border-radius: 6px; margin-top: 6px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 8px; }"
        )
        layout = QVBoxLayout(group)
        layout.setSpacing(4)

        top_actions = QHBoxLayout()
        analyze_button = QPushButton("AI Analyze")
        analyze_button.clicked.connect(lambda: self._append_log("AI analyze requested.", "INFO"))
        top_actions.addWidget(analyze_button)

        apply_plan_button = QPushButton("Apply Plan")
        apply_plan_button.clicked.connect(lambda: self._append_log("AI plan applied.", "INFO"))
        top_actions.addWidget(apply_plan_button)

        top_actions.addStretch()
        layout.addLayout(top_actions)

        self._history = QPlainTextEdit()
        self._history.setReadOnly(True)
        self._history.appendPlainText("[AI] Waiting…")
        layout.addWidget(self._history, stretch=1)

        input_row = QHBoxLayout()
        self._command_input = QLineEdit()
        self._command_input.setPlaceholderText("Type a message for AI Operator…")
        self._command_input.returnPressed.connect(self._handle_send)
        input_row.addWidget(self._command_input)

        send_button = QPushButton("Send")
        send_button.clicked.connect(self._handle_send)
        input_row.addWidget(send_button)
        layout.addLayout(input_row)

        bottom_actions = QHBoxLayout()
        approve_button = QPushButton("Approve")
        approve_button.clicked.connect(self._handle_start)
        bottom_actions.addWidget(approve_button)

        pause_button = QPushButton("Pause (AI)")
        pause_button.clicked.connect(self._handle_pause)
        bottom_actions.addWidget(pause_button)
        bottom_actions.addStretch()
        layout.addLayout(bottom_actions)

        return group

    def _handle_send(self) -> None:
        text = self._command_input.text().strip()
        if not text:
            return
        self._history.appendPlainText(f"[YOU] {text}")
        self._history.appendPlainText("[AI] received")
        self._append_log(f"AI command sent: {text}", "INFO")
        self._command_input.clear()
