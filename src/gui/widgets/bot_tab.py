from __future__ import annotations

from PySide6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.core.logging import get_logger


class BotTab(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._logger = get_logger("gui.bot")

        layout = QVBoxLayout()

        title = QLabel("Bot workspace (placeholder)")
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        layout.addWidget(title)

        selected_pair = QGroupBox("Selected pair")
        selected_layout = QVBoxLayout()
        self._selected_pair_label = QLabel("BTCUSDT")
        selected_layout.addWidget(self._selected_pair_label)
        selected_pair.setLayout(selected_layout)
        layout.addWidget(selected_pair)

        layout.addLayout(self._build_action_buttons())
        layout.addWidget(self._build_strategy_fields())
        layout.addStretch()

        self.setLayout(layout)

    def _build_action_buttons(self) -> QHBoxLayout:
        layout = QHBoxLayout()
        self._prepare_button = QPushButton("Prepare")
        self._analyze_button = QPushButton("AI Analyze")
        self._start_button = QPushButton("Confirm & Start")
        self._stop_button = QPushButton("Stop")

        self._start_button.setEnabled(False)
        self._stop_button.setEnabled(False)

        self._prepare_button.clicked.connect(self._prepare)
        self._analyze_button.clicked.connect(self._analyze)
        self._start_button.clicked.connect(self._start)
        self._stop_button.clicked.connect(self._stop)

        layout.addWidget(self._prepare_button)
        layout.addWidget(self._analyze_button)
        layout.addWidget(self._start_button)
        layout.addWidget(self._stop_button)
        layout.addStretch()
        return layout

    def _build_strategy_fields(self) -> QWidget:
        group = QGroupBox("Strategy Settings")
        form = QFormLayout()

        self._budget_input = QLineEdit()
        self._mode_input = QLineEdit()
        self._grid_count_input = QLineEdit()
        self._grid_step_input = QLineEdit()
        self._range_input = QLineEdit()

        form.addRow("Budget (USDT)", self._budget_input)
        form.addRow("Mode", self._mode_input)
        form.addRow("Grid count", self._grid_count_input)
        form.addRow("Grid step %", self._grid_step_input)
        form.addRow("Range low/high %", self._range_input)

        group.setLayout(form)
        return group

    def _prepare(self) -> None:
        self._logger.info("prepare called")

    def _analyze(self) -> None:
        self._logger.info("ai analyze called")
        self._budget_input.setText("500")
        self._mode_input.setText("Market Making")
        self._grid_count_input.setText("12")
        self._grid_step_input.setText("0.35")
        self._range_input.setText("-1.5 / 1.8")
        self._logger.info("AI suggested settings applied")
        self._start_button.setEnabled(True)

    def _start(self) -> None:
        dialog = QMessageBox.question(self, "Confirm", "Confirm start?")
        if dialog != QMessageBox.StandardButton.Yes:
            self._logger.info("start canceled")
            return
        self._logger.info("start confirmed")
        self._start_button.setEnabled(False)
        self._stop_button.setEnabled(True)

    def _stop(self) -> None:
        self._logger.info("stop called")
        self._stop_button.setEnabled(False)
        self._start_button.setEnabled(True)
