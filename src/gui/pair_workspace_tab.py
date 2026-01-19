from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QSignalBlocker, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from src.core.logging import get_logger
from src.gui.models.app_state import AppState
from src.gui.models.pair_state import PairRunState
from src.gui.widgets.pair_logs_panel import PairLogsPanel
from src.gui.widgets.pair_topbar import PairTopBar


@dataclass
class DemoPlan:
    budget: float
    mode: str
    grid_count: int
    grid_step_pct: float
    range_low_pct: float
    range_high_pct: float


@dataclass
class MarketContext:
    market_type: str
    volatility: str
    liquidity: str
    spread: float
    spread_status: str


class PairWorkspaceTab(QWidget):
    def __init__(self, symbol: str, app_state: AppState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.symbol = symbol
        self._app_state = app_state
        self._logger = get_logger(f"gui.pair_workspace.{symbol.lower()}")

        self._state = PairRunState.IDLE
        self._data_ready = False
        self._market_context: MarketContext | None = None
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._log_tick)
        self._tick_count = 0
        self._ai_values: dict[str, Any] = {}
        self._user_touched: set[str] = set()

        layout = QVBoxLayout()
        self._topbar = PairTopBar(
            symbol=symbol,
            default_period=app_state.default_period,
            default_quality=app_state.default_quality,
        )
        layout.addWidget(self._topbar)
        layout.addWidget(self._build_body())
        self.setLayout(layout)

        self._connect_signals()
        self.update_controls_by_state()

    def shutdown(self) -> None:
        self._tick_timer.stop()

    def _connect_signals(self) -> None:
        self._topbar.prepare_button.clicked.connect(self._prepare_data)
        self._topbar.analyze_button.clicked.connect(self._analyze)
        self._topbar.apply_button.clicked.connect(self._apply_plan)
        self._topbar.confirm_button.clicked.connect(self._confirm_start)
        self._topbar.pause_button.clicked.connect(self._toggle_pause)
        self._topbar.stop_button.clicked.connect(self._stop_run)
        self._chat_send_button.clicked.connect(self._send_chat)

    def _build_body(self) -> QSplitter:
        splitter = QSplitter()

        main_panel = QWidget()
        main_layout = QVBoxLayout()
        main_layout.addWidget(self._build_analysis_panel())
        main_layout.addWidget(self._build_strategy_panel())
        main_layout.addWidget(self._build_plan_panel())
        main_layout.addWidget(self._build_logs_panel())
        main_panel.setLayout(main_layout)

        chat_panel = self._build_chat_panel()

        splitter.addWidget(main_panel)
        splitter.addWidget(chat_panel)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        return splitter

    def _build_analysis_panel(self) -> QWidget:
        group = QGroupBox("Analysis summary")
        layout = QVBoxLayout()
        self._analysis_summary = QLabel(self._idle_summary())
        self._analysis_summary.setWordWrap(True)
        layout.addWidget(self._analysis_summary)

        self._ai_request_card = QGroupBox("AI requested more data")
        request_layout = QVBoxLayout()
        self._ai_request_label = QLabel("Adjust period to 24h and quality to Deep to proceed.")
        request_layout.addWidget(self._ai_request_label)
        self._ai_request_card.setLayout(request_layout)
        self._ai_request_card.hide()
        layout.addWidget(self._ai_request_card)

        group.setLayout(layout)
        return group

    def _build_strategy_panel(self) -> QWidget:
        group = QGroupBox("Strategy form")
        form = QFormLayout()

        self._budget_input = QDoubleSpinBox()
        self._budget_input.setRange(10.0, 1_000_000.0)
        self._budget_input.setDecimals(2)

        self._mode_input = QComboBox()
        self._mode_input.addItems(["Grid", "Adaptive Grid", "Manual"])

        self._grid_count_input = QSpinBox()
        self._grid_count_input.setRange(3, 60)

        self._grid_step_input = QDoubleSpinBox()
        self._grid_step_input.setRange(0.1, 10.0)
        self._grid_step_input.setDecimals(2)

        self._range_low_input = QDoubleSpinBox()
        self._range_low_input.setRange(0.5, 20.0)
        self._range_low_input.setDecimals(2)

        self._range_high_input = QDoubleSpinBox()
        self._range_high_input.setRange(0.5, 20.0)
        self._range_high_input.setDecimals(2)

        form.addRow("Budget (USDT)", self._budget_input)
        form.addRow("Mode", self._mode_input)
        form.addRow("Grid count", self._grid_count_input)
        form.addRow("Grid step %", self._grid_step_input)
        form.addRow("Range low %", self._range_low_input)
        form.addRow("Range high %", self._range_high_input)

        self._reset_ai_button = QPushButton("Reset to AI")
        self._reset_ai_button.setEnabled(False)
        self._reset_ai_button.clicked.connect(self._reset_strategy_to_ai)

        header_layout = QHBoxLayout()
        header_layout.addStretch()
        header_layout.addWidget(self._reset_ai_button)

        layout = QVBoxLayout()
        layout.addLayout(header_layout)
        layout.addLayout(form)
        group.setLayout(layout)

        self._strategy_fields = {
            "budget": self._budget_input,
            "mode": self._mode_input,
            "grid_count": self._grid_count_input,
            "grid_step_pct": self._grid_step_input,
            "range_low_pct": self._range_low_input,
            "range_high_pct": self._range_high_input,
        }

        self._budget_input.valueChanged.connect(lambda _: self._on_field_changed("budget"))
        self._mode_input.currentTextChanged.connect(lambda _: self._on_field_changed("mode"))
        self._grid_count_input.valueChanged.connect(lambda _: self._on_field_changed("grid_count"))
        self._grid_step_input.valueChanged.connect(lambda _: self._on_field_changed("grid_step_pct"))
        self._range_low_input.valueChanged.connect(lambda _: self._on_field_changed("range_low_pct"))
        self._range_high_input.valueChanged.connect(lambda _: self._on_field_changed("range_high_pct"))

        return group

    def _build_plan_panel(self) -> QWidget:
        group = QGroupBox("Plan preview")
        layout = QVBoxLayout()
        self._plan_preview = QLabel("Plan preview will appear here after analysis.")
        self._plan_preview.setWordWrap(True)
        layout.addWidget(self._plan_preview)
        group.setLayout(layout)
        return group

    def _build_logs_panel(self) -> QWidget:
        group = QGroupBox("Local logs")
        layout = QVBoxLayout()
        self._logs_panel = PairLogsPanel()
        layout.addWidget(self._logs_panel)
        group.setLayout(layout)
        return group

    def _build_chat_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout()
        layout.addWidget(QLabel("AI Chat"))
        self._chat_history = QPlainTextEdit()
        self._chat_history.setReadOnly(True)
        layout.addWidget(self._chat_history)

        input_row = QHBoxLayout()
        self._chat_input = QLineEdit()
        self._chat_input.setPlaceholderText("Ask AI to adjust strategy...")
        self._chat_send_button = QPushButton("Send")
        input_row.addWidget(self._chat_input)
        input_row.addWidget(self._chat_send_button)

        layout.addLayout(input_row)
        panel.setLayout(layout)
        return panel

    def _idle_summary(self) -> str:
        return "No data prepared.\nClick “Prepare Data” to collect market data."

    def _preparing_summary(self) -> str:
        return "Preparing market data…"

    def _analyzing_summary(self) -> str:
        return "Market data ready.\nAI analysis in progress…"

    def _build_market_context(self) -> MarketContext:
        market_type = random.choice(["Range", "Trend", "Mixed"])
        volatility = random.choice(["Low", "Medium", "High"])
        liquidity = random.choice(["OK", "Thin"])
        spread = round(random.uniform(0.05, 0.45), 2)
        spread_status = "OK" if spread <= 0.2 else "Warning"
        return MarketContext(
            market_type=market_type,
            volatility=volatility,
            liquidity=liquidity,
            spread=spread,
            spread_status=spread_status,
        )

    def _format_market_summary(self, include_ai_note: bool) -> str:
        context = self._market_context or self._build_market_context()
        self._market_context = context
        lines = [
            f"Market type: {context.market_type}",
            f"Volatility: {context.volatility}",
            f"Liquidity: {context.liquidity}",
            f"Spread: {context.spread:.2f}% ({context.spread_status})",
        ]
        if include_ai_note:
            ai_note = random.choice(["Grid suitable", "Grid risky", "Not recommended"])
            lines.append(f"AI note: {ai_note}")
        return "\n".join(lines)

    def _prepare_data(self) -> None:
        self._set_state(PairRunState.PREPARING)
        self._log_event("Prepare data requested.")
        self._analysis_summary.setText(self._preparing_summary())
        QTimer.singleShot(700, self._finish_prepare)

    def _finish_prepare(self) -> None:
        self._data_ready = True
        self._market_context = self._build_market_context()
        self._analysis_summary.setText(self._format_market_summary(include_ai_note=False))
        self._ai_request_card.hide()
        self._set_state(PairRunState.DATA_READY)
        self._log_event("Prepared data pack (demo).")

    def _analyze(self) -> None:
        if not self._data_ready:
            QMessageBox.warning(self, "No data", "Prepare data before AI analysis.")
            self._log_event("AI analyze requested without data.")
            return
        self._set_state(PairRunState.ANALYZING)
        self._log_event("AI analysis started.")
        self._analysis_summary.setText(self._analyzing_summary())
        QTimer.singleShot(700, self._finish_analysis)

    def _finish_analysis(self) -> None:
        period = self._topbar.period_combo.currentText()
        quality = self._topbar.quality_combo.currentText()
        if period != "24h" or quality != "Deep":
            self._ai_request_card.show()
            self._analysis_summary.setText(self._format_market_summary(include_ai_note=False))
            self._plan_preview.setText("Awaiting additional data request resolution.")
            self._set_state(PairRunState.NEED_MORE_DATA)
            self._log_event("AI requested more data (demo).")
            return
        self._ai_request_card.hide()
        self._plan_preview.setText(
            "Plan preview: Grid MM, 12 grids, 0.35% step, budget 500 USDT.",
        )
        self._analysis_summary.setText(self._format_market_summary(include_ai_note=True))
        self._set_state(PairRunState.PLAN_READY)
        self._log_event("AI plan ready.")

    def _is_plan_applied(self) -> bool:
        canonical_state = self._canonical_state(self._state)
        return canonical_state in {
            PairRunState.APPLIED,
            PairRunState.RUNNING,
            PairRunState.PAUSED,
            PairRunState.STOPPED,
        }

    def _set_field_value(self, field_key: str, value: object, origin: str = "ai") -> None:
        field = self._strategy_fields[field_key]
        with QSignalBlocker(field):
            if isinstance(field, QDoubleSpinBox):
                field.setValue(float(value))
            elif isinstance(field, QSpinBox):
                field.setValue(int(value))
            elif isinstance(field, QComboBox):
                field.setCurrentText(str(value))
            elif isinstance(field, QLineEdit):
                field.setText(str(value))
        if origin == "ai":
            self._ai_values[field_key] = value
            self._user_touched.discard(field_key)
            self._mark_field_state(field_key, "ai")

    def _mark_field_state(self, field_key: str, state: str = "neutral") -> None:
        field = self._strategy_fields[field_key]
        if state == "ai":
            field.setStyleSheet(
                "border: 1px solid #7f5ad9;"
                "border-radius: 4px;"
                "background-color: rgba(127, 90, 217, 0.12);"
            )
            return
        if state == "user":
            field.setStyleSheet(
                "border: 1px solid #d9a441;"
                "border-radius: 4px;"
                "background-color: rgba(217, 164, 65, 0.12);"
            )
            return
        field.setStyleSheet("")

    def _on_field_changed(self, field_key: str) -> None:
        if not self._is_plan_applied() or not self._ai_values:
            return
        self._user_touched.add(field_key)
        self._mark_field_state(field_key, "user")

    def _reset_strategy_to_ai(self) -> None:
        if not self._ai_values:
            return
        for field_key, value in self._ai_values.items():
            self._set_field_value(field_key, value, origin="ai")
        self._user_touched.clear()
        self._reset_ai_button.setEnabled(True)
        self._log_event("[UI] Reset strategy to AI values")

    def _apply_plan(self) -> None:
        demo_plan = DemoPlan(
            budget=500.0,
            mode="Grid",
            grid_count=12,
            grid_step_pct=0.35,
            range_low_pct=1.5,
            range_high_pct=1.8,
        )
        self._ai_values = {}
        self._user_touched.clear()
        self._set_field_value("budget", demo_plan.budget, origin="ai")
        self._set_field_value("mode", demo_plan.mode, origin="ai")
        self._set_field_value("grid_count", demo_plan.grid_count, origin="ai")
        self._set_field_value("grid_step_pct", demo_plan.grid_step_pct, origin="ai")
        self._set_field_value("range_low_pct", demo_plan.range_low_pct, origin="ai")
        self._set_field_value("range_high_pct", demo_plan.range_high_pct, origin="ai")
        self._reset_ai_button.setEnabled(True)
        self._set_state(PairRunState.APPLIED)
        self._log_event("[AI] Plan applied -> strategy populated")

    def _confirm_start(self) -> None:
        self._set_state(PairRunState.WAIT_CONFIRM)
        confirm = QMessageBox.question(
            self,
            "Confirm start",
            f"Start {'dry-run ' if self._topbar.dry_run_toggle.isChecked() else ''}execution for {self.symbol}?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            self._set_state(PairRunState.APPLIED)
            self._log_event("Run start canceled.")
            return
        self._tick_count = 0
        self._tick_timer.start()
        self._set_state(PairRunState.RUNNING)
        self._log_event("Execution started.")

    def _toggle_pause(self) -> None:
        if self._state == PairRunState.RUNNING:
            self._tick_timer.stop()
            self._set_state(PairRunState.PAUSED)
            self._log_event("Execution paused.")
        elif self._state == PairRunState.PAUSED:
            self._tick_timer.start()
            self._set_state(PairRunState.RUNNING)
            self._log_event("Execution resumed.")

    def _stop_run(self) -> None:
        self._tick_timer.stop()
        self._set_state(PairRunState.STOPPED)
        self._log_event("Execution stopped.")

    def _log_tick(self) -> None:
        self._tick_count += 1
        self._log_event(f"tick {self._tick_count}")

    def _send_chat(self) -> None:
        message = self._chat_input.text().strip()
        if not message:
            return
        self._append_chat("User", message)
        self._chat_input.clear()
        self._append_chat("AI", "Not implemented yet.")

    def _append_chat(self, sender: str, message: str) -> None:
        self._chat_history.appendPlainText(f"{sender}: {message}")

    def _set_state(self, state: PairRunState) -> None:
        self._state = state
        canonical_state = self._canonical_state(state)
        self._topbar.state_label.setText(f"State: {canonical_state.value}")
        self.update_controls_by_state()

    def _canonical_state(self, state: PairRunState) -> PairRunState:
        if state in {PairRunState.READY, PairRunState.NEED_MORE_DATA}:
            return PairRunState.DATA_READY
        if state == PairRunState.WAIT_CONFIRM:
            return PairRunState.APPLIED
        return state

    def update_controls_by_state(self) -> None:
        canonical_state = self._canonical_state(self._state)
        self._topbar.prepare_button.setEnabled(
            canonical_state
            in {
                PairRunState.IDLE,
                PairRunState.DATA_READY,
                PairRunState.PLAN_READY,
                PairRunState.APPLIED,
                PairRunState.STOPPED,
                PairRunState.ERROR,
            }
        )
        self._topbar.analyze_button.setEnabled(canonical_state == PairRunState.DATA_READY)
        self._topbar.apply_button.setEnabled(canonical_state == PairRunState.PLAN_READY)
        self._topbar.confirm_button.setEnabled(canonical_state == PairRunState.APPLIED)
        self._topbar.pause_button.setEnabled(canonical_state == PairRunState.RUNNING)
        self._topbar.stop_button.setEnabled(canonical_state in {PairRunState.RUNNING, PairRunState.PAUSED})

    def _log_event(self, message: str) -> None:
        self._logs_panel.append(message)
        self._logger.info("%s | %s", self.symbol, message)
