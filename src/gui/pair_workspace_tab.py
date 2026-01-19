from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
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
    budget: str
    mode: str
    grid_count: str
    grid_step: str
    range_band: str


class PairWorkspaceTab(QWidget):
    def __init__(self, symbol: str, app_state: AppState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.symbol = symbol
        self._app_state = app_state
        self._logger = get_logger(f"gui.pair_workspace.{symbol.lower()}")

        self._state = PairRunState.IDLE
        self._data_ready = False
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._log_tick)
        self._tick_count = 0

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
        self._analysis_summary = QLabel("No data prepared yet.")
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

    def _prepare_data(self) -> None:
        self._set_state(PairRunState.PREPARING)
        self._log_event("Prepare data requested.")
        QTimer.singleShot(700, self._finish_prepare)

    def _finish_prepare(self) -> None:
        self._data_ready = True
        period = self._topbar.period_combo.currentText()
        quality = self._topbar.quality_combo.currentText()
        self._analysis_summary.setText(
            f"Data prepared for {self.symbol}: period={period}, quality={quality}.",
        )
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
        QTimer.singleShot(700, self._finish_analysis)

    def _finish_analysis(self) -> None:
        period = self._topbar.period_combo.currentText()
        quality = self._topbar.quality_combo.currentText()
        if period != "24h" or quality != "Deep":
            self._ai_request_card.show()
            self._analysis_summary.setText(
                "AI requested more data to proceed with a deeper plan.",
            )
            self._plan_preview.setText("Awaiting additional data request resolution.")
            self._set_state(PairRunState.NEED_MORE_DATA)
            self._log_event("AI requested more data (demo).")
            return
        self._ai_request_card.hide()
        self._plan_preview.setText(
            "Plan preview: Grid MM, 12 grids, 0.35% step, budget 500 USDT.",
        )
        self._analysis_summary.setText("AI analysis complete. Plan ready to apply.")
        self._set_state(PairRunState.PLAN_READY)
        self._log_event("AI plan ready.")

    def _apply_plan(self) -> None:
        demo_plan = DemoPlan(
            budget="500",
            mode="Market Making",
            grid_count="12",
            grid_step="0.35",
            range_band="-1.5 / 1.8",
        )
        self._budget_input.setText(demo_plan.budget)
        self._mode_input.setText(demo_plan.mode)
        self._grid_count_input.setText(demo_plan.grid_count)
        self._grid_step_input.setText(demo_plan.grid_step)
        self._range_input.setText(demo_plan.range_band)
        self._set_state(PairRunState.APPLIED)
        self._log_event("Applied plan to strategy form.")

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
