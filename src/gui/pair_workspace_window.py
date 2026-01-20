from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDockWidget,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.gui.models.app_state import AppState
from src.gui.models.pair_workspace import DataPackSummary, StrategyPlan, StrategyPatch
from src.services.ai_provider import AIProvider, apply_patch


class PairWorkspaceWindow(QMainWindow):
    def __init__(self, symbol: str, app_state: AppState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._symbol = symbol
        self._app_state = app_state
        self._ai_provider = AIProvider()
        self._datapack: DataPackSummary | None = None
        self._latest_plan: StrategyPlan | None = None
        self._pending_patch: StrategyPatch | None = None

        self.setWindowTitle(f"Pair Workspace — {symbol}")
        self.resize(1200, 800)

        central = QWidget()
        layout = QVBoxLayout()
        layout.addWidget(self._build_top_bar())
        layout.addWidget(self._build_tabs())
        central.setLayout(layout)
        self.setCentralWidget(central)

        self._build_chat_dock()
        self._update_status("Idle")

    def _build_top_bar(self) -> QWidget:
        bar = QWidget()
        layout = QHBoxLayout()

        self._symbol_label = QLabel(self._symbol)
        self._symbol_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(self._symbol_label)

        self._period_combo = QComboBox()
        self._period_combo.addItems(["1m", "3m", "5m", "15m", "30m", "1h", "4h", "12h", "24h", "1d", "7d"])
        self._period_combo.setCurrentText(self._app_state.default_period)
        layout.addWidget(QLabel("Period"))
        layout.addWidget(self._period_combo)

        self._quality_combo = QComboBox()
        self._quality_combo.addItems(["Standard", "Deep"])
        self._quality_combo.setCurrentText(self._app_state.default_quality)
        layout.addWidget(QLabel("Quality"))
        layout.addWidget(self._quality_combo)

        self._prepare_button = QPushButton("Prepare Data")
        self._prepare_button.clicked.connect(self._prepare_data)
        layout.addWidget(self._prepare_button)

        self._analyze_button = QPushButton("AI Analyze")
        self._analyze_button.clicked.connect(self._run_ai_analysis)
        self._analyze_button.setEnabled(False)
        layout.addWidget(self._analyze_button)

        self._apply_button = QPushButton("Apply")
        self._apply_button.clicked.connect(self._apply_latest_plan)
        self._apply_button.setEnabled(False)
        layout.addWidget(self._apply_button)

        self._confirm_button = QPushButton("Confirm&Start")
        self._confirm_button.clicked.connect(self._confirm_start)
        self._confirm_button.setEnabled(False)
        layout.addWidget(self._confirm_button)

        self._stop_button = QPushButton("Stop")
        self._stop_button.clicked.connect(self._stop_simulation)
        self._stop_button.setEnabled(False)
        layout.addWidget(self._stop_button)

        layout.addStretch()
        bar.setLayout(layout)
        return bar

    def _build_tabs(self) -> QTabWidget:
        self._tabs = QTabWidget()
        self._analysis_text = QTextEdit()
        self._analysis_text.setReadOnly(True)
        self._tabs.addTab(self._analysis_text, "Analysis")

        self._strategy_form = self._build_strategy_form()
        self._tabs.addTab(self._strategy_form, "Strategy")

        self._execution_text = QTextEdit()
        self._execution_text.setReadOnly(True)
        self._execution_text.setText("Execution controls will appear here.")
        self._tabs.addTab(self._execution_text, "Execution")

        self._logs_text = QTextEdit()
        self._logs_text.setReadOnly(True)
        self._tabs.addTab(self._logs_text, "Logs")
        return self._tabs

    def _build_strategy_form(self) -> QWidget:
        form_widget = QWidget()
        form = QFormLayout()

        self._budget_input = QDoubleSpinBox()
        self._budget_input.setRange(10.0, 1_000_000.0)
        self._budget_input.setValue(250.0)
        self._budget_input.setDecimals(2)

        self._mode_combo = QComboBox()
        self._mode_combo.addItems(["MM", "Scalp"])

        self._grid_count_input = QSpinBox()
        self._grid_count_input.setRange(3, 60)
        self._grid_count_input.setValue(10)

        self._grid_step_input = QDoubleSpinBox()
        self._grid_step_input.setRange(0.1, 10.0)
        self._grid_step_input.setDecimals(2)
        self._grid_step_input.setValue(0.5)
        self._grid_step_input.setSuffix(" %")

        self._range_low_input = QDoubleSpinBox()
        self._range_low_input.setRange(0.5, 20.0)
        self._range_low_input.setDecimals(2)
        self._range_low_input.setValue(1.5)
        self._range_low_input.setSuffix(" %")

        self._range_high_input = QDoubleSpinBox()
        self._range_high_input.setRange(0.5, 20.0)
        self._range_high_input.setDecimals(2)
        self._range_high_input.setValue(2.5)
        self._range_high_input.setSuffix(" %")

        self._refresh_interval_input = QSpinBox()
        self._refresh_interval_input.setRange(5, 300)
        self._refresh_interval_input.setValue(30)
        self._refresh_interval_input.setSuffix(" s")

        form.addRow(QLabel("Budget"), self._budget_input)
        form.addRow(QLabel("Mode"), self._mode_combo)
        form.addRow(QLabel("Grid count"), self._grid_count_input)
        form.addRow(QLabel("Grid step %"), self._grid_step_input)
        form.addRow(QLabel("Range low %"), self._range_low_input)
        form.addRow(QLabel("Range high %"), self._range_high_input)
        form.addRow(QLabel("Refresh interval"), self._refresh_interval_input)

        form_widget.setLayout(form)
        return form_widget

    def _build_chat_dock(self) -> None:
        self._chat_history = QTextEdit()
        self._chat_history.setReadOnly(True)

        self._chat_input = QLineEdit()
        self._chat_input.setPlaceholderText("Ask AI to adjust the strategy...")

        self._chat_send_button = QPushButton("Send")
        self._chat_send_button.clicked.connect(self._send_chat_message)

        self._apply_patch_button = QPushButton("Apply suggested changes")
        self._apply_patch_button.setEnabled(False)
        self._apply_patch_button.clicked.connect(self._apply_patch)

        layout = QVBoxLayout()
        layout.addWidget(QLabel("AI Chat"))
        layout.addWidget(self._chat_history)
        layout.addWidget(self._chat_input)
        layout.addWidget(self._chat_send_button)
        layout.addWidget(self._apply_patch_button)

        panel = QWidget()
        panel.setLayout(layout)

        dock = QDockWidget("AI Chat", self)
        dock.setWidget(panel)
        dock.setFeatures(QDockWidget.DockWidgetClosable | QDockWidget.DockWidgetMovable)
        self.addDockWidget(Qt.RightDockWidgetArea, dock)

    def _prepare_data(self) -> None:
        period = self._period_combo.currentText()
        quality = self._quality_combo.currentText()
        self._datapack = DataPackSummary(
            symbol=self._symbol,
            period=period,
            quality=quality,
            candles_count=450,
            last_price=68250.12,
            volatility_est=0.042,
        )
        self._analysis_text.setText(self._format_datapack(self._datapack))
        self._analyze_button.setEnabled(True)
        self._log_event("Prepared demo datapack.")

    def _run_ai_analysis(self) -> None:
        if not self._datapack:
            return
        result = self._ai_provider.analyze(
            symbol=self._symbol,
            datapack=self._datapack,
            user_budget=self._budget_input.value(),
            mode=self._mode_combo.currentText(),
            chat_context=self._chat_history.toPlainText().splitlines(),
        )
        self._latest_plan = result
        self._apply_button.setEnabled(True)
        self._confirm_button.setEnabled(True)
        self._fill_strategy_fields(result)
        self._append_chat("AI", "AI proposed plan. Apply?")
        self._log_event("AI strategy plan ready.")

    def _apply_latest_plan(self) -> None:
        if not self._latest_plan:
            return
        self._fill_strategy_fields(self._latest_plan)
        self._log_event("Strategy plan applied.")

    def _send_chat_message(self) -> None:
        message = self._chat_input.text().strip()
        if not message:
            return
        self._append_chat("User", message)
        self._chat_input.clear()
        if not self._latest_plan:
            self._append_chat("AI", "Prepare data and run analysis first.")
            return
        response = self._ai_provider.chat_adjustment(self._latest_plan, message)
        if isinstance(response, StrategyPatch):
            self._pending_patch = response
            self._apply_patch_button.setEnabled(True)
            self._append_chat("AI", "Suggested changes ready. Apply suggested changes?")
        else:
            self._append_chat("AI", response)

    def _apply_patch(self) -> None:
        if not self._latest_plan or not self._pending_patch:
            return
        self._latest_plan = apply_patch(self._latest_plan, self._pending_patch)
        self._fill_strategy_fields(self._latest_plan)
        self._pending_patch = None
        self._apply_patch_button.setEnabled(False)
        self._append_chat("System", "Applied AI suggested changes.")
        self._log_event("AI patch applied to strategy.")

    def _confirm_start(self) -> None:
        confirm = QMessageBox.question(
            self,
            "Confirm start",
            f"Start simulated strategy for {self._symbol}?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        self._update_status("Running (simulated)")
        self._stop_button.setEnabled(True)
        self._confirm_button.setEnabled(False)
        self._log_event("Simulation started.")
        self._execution_text.setText("Running simulated execution loop.")

    def _stop_simulation(self) -> None:
        self._update_status("Stopped")
        self._stop_button.setEnabled(False)
        self._confirm_button.setEnabled(True)
        self._log_event("Simulation stopped.")
        self._execution_text.setText("Execution stopped.")

    def _update_status(self, status: str) -> None:
        self.statusBar().showMessage(status)

    def _format_datapack(self, datapack: DataPackSummary) -> str:
        return (
            f"Symbol: {datapack.symbol}\n"
            f"Period: {datapack.period}\n"
            f"Quality: {datapack.quality}\n"
            f"Candles: {datapack.candles_count}\n"
            f"Last price: {datapack.last_price:.2f}\n"
            f"Volatility est: {datapack.volatility_est:.4f}\n"
        )

    def _fill_strategy_fields(self, plan: StrategyPlan) -> None:
        self._budget_input.setValue(plan.budget)
        self._mode_combo.setCurrentText(plan.mode)
        self._grid_count_input.setValue(plan.grid_count)
        self._grid_step_input.setValue(plan.grid_step_pct)
        self._range_low_input.setValue(plan.range_low_pct)
        self._range_high_input.setValue(plan.range_high_pct)
        self._refresh_interval_input.setValue(plan.refresh_interval_s)

    def _append_chat(self, sender: str, message: str) -> None:
        self._chat_history.append(f"{sender}: {message}")

    def _log_event(self, message: str) -> None:
        self._logs_text.append(message)
