from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QSignalBlocker, Qt, QThreadPool, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSizePolicy,
    QSpinBox,
    QPushButton,
    QPlainTextEdit,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.ai.models import AiPatchResponse, AiPlan, AiResponse
from src.ai.openai_client import OpenAIClient
from src.core.logging import get_logger
from src.gui.models.app_state import AppState
from src.gui.models.pair_state import BotState, PairState
from src.gui.widgets.pair_logs_panel import PairLogsPanel
from src.gui.widgets.pair_topbar import PairTopBar
from src.services.price_hub import PriceHub


@dataclass
class MarketContext:
    market_type: str
    volatility: str
    liquidity: str
    spread: float
    spread_status: str


class _AiWorkerSignals(QObject):
    success = Signal(object)
    error = Signal(str)


class _AiWorker(QRunnable):
    def __init__(self, fn: Callable[[], object]) -> None:
        super().__init__()
        self.signals = _AiWorkerSignals()
        self._fn = fn

    def run(self) -> None:
        try:
            result = self._fn()
        except Exception as exc:  # noqa: BLE001
            self.signals.error.emit(str(exc))
            return
        self.signals.success.emit(result)


class PairWorkspaceTab(QWidget):
    def __init__(
        self,
        symbol: str,
        app_state: AppState,
        price_hub: PriceHub | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.symbol = symbol
        self._app_state = app_state
        self._price_hub = price_hub
        self._logger = get_logger(f"gui.pair_workspace.{symbol.lower()}")

        self._pair_state = PairState()
        self._data_ready = False
        self._plan_applied = False
        self._market_context: MarketContext | None = None
        self._prepared_mid_price: float | None = None
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._log_tick)
        self._tick_count = 0
        self._ai_values: dict[str, Any] = {}
        self._user_touched: set[str] = set()
        self._thread_pool = QThreadPool.globalInstance()
        self._latest_price: float | None = None
        self._latest_price_source: str | None = None
        self._latest_price_age_ms: int | None = None
        self._last_ai_response: AiResponse | None = None
        self._last_datapack: dict[str, Any] | None = None

        layout = QVBoxLayout()
        self._topbar = PairTopBar(
            symbol=symbol,
            default_period=app_state.default_period,
            default_quality=app_state.default_quality,
        )
        layout.addWidget(self._topbar)
        layout.addWidget(self._build_body())
        layout.setStretch(0, 0)
        layout.setStretch(1, 1)
        self.setLayout(layout)

        self._connect_signals()
        self.update_controls_by_state()
        if self._price_hub is not None:
            self._price_hub.price_updated.connect(self._handle_price_update)

    def shutdown(self) -> None:
        self._tick_timer.stop()
        if self._price_hub is not None:
            self._price_hub.price_updated.disconnect(self._handle_price_update)

    def _connect_signals(self) -> None:
        self._topbar.prepare_button.clicked.connect(self._prepare_data)
        self._topbar.analyze_button.clicked.connect(self._analyze)
        self._topbar.apply_button.clicked.connect(self._apply_plan)
        self._topbar.confirm_button.clicked.connect(self._confirm_start)
        self._topbar.pause_button.clicked.connect(self._toggle_pause)
        self._topbar.stop_button.clicked.connect(self._stop_run)
        self._chat_send_button.clicked.connect(self._send_chat)

    def _build_body(self) -> QSplitter:
        splitter = QSplitter(Qt.Horizontal)

        left_splitter = QSplitter(Qt.Vertical)
        left_splitter.addWidget(self._build_analysis_panel())
        left_splitter.addWidget(self._build_strategy_panel())
        left_splitter.addWidget(self._build_plan_panel())
        left_splitter.addWidget(self._build_logs_panel())
        left_splitter.setStretchFactor(0, 1)
        left_splitter.setStretchFactor(1, 1)
        left_splitter.setStretchFactor(2, 2)
        left_splitter.setStretchFactor(3, 2)

        main_panel = QWidget()
        main_layout = QVBoxLayout()
        main_layout.addWidget(left_splitter)
        main_layout.addStretch()
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_panel.setLayout(main_layout)
        main_panel.setMinimumWidth(560)

        chat_panel = self._build_chat_panel()
        chat_panel.setMinimumWidth(340)

        splitter.addWidget(main_panel)
        splitter.addWidget(chat_panel)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        return splitter

    def _build_analysis_panel(self) -> QWidget:
        group = QGroupBox("Analysis summary")
        layout = QVBoxLayout()
        self._analysis_summary = QTextEdit()
        self._analysis_summary.setReadOnly(True)
        self._analysis_summary.setText(self._idle_summary())
        self._analysis_summary.setMinimumHeight(100)
        self._analysis_summary.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self._analysis_summary)

        self._ai_request_card = QGroupBox("AI requested more data")
        request_layout = QVBoxLayout()
        self._ai_request_label = QLabel("Adjust period to 24h and quality to Deep to proceed.")
        request_layout.addWidget(self._ai_request_label)
        self._ai_request_card.setLayout(request_layout)
        self._ai_request_card.hide()
        layout.addWidget(self._ai_request_card)

        group.setLayout(layout)
        group.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        return group

    def _build_strategy_panel(self) -> QWidget:
        group = QGroupBox("Strategy form")
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        self._budget_input = QDoubleSpinBox()
        self._budget_input.setRange(10.0, 1_000_000.0)
        self._budget_input.setDecimals(2)
        self._budget_input.setMinimumWidth(160)

        self._mode_input = QComboBox()
        self._mode_input.addItems(["Grid", "Adaptive Grid", "Manual"])
        self._mode_input.setMinimumWidth(160)

        self._grid_count_input = QSpinBox()
        self._grid_count_input.setRange(3, 60)
        self._grid_count_input.setMinimumWidth(160)

        self._grid_step_input = QDoubleSpinBox()
        self._grid_step_input.setRange(0.1, 10.0)
        self._grid_step_input.setDecimals(2)
        self._grid_step_input.setMinimumWidth(160)

        self._range_low_input = QDoubleSpinBox()
        self._range_low_input.setRange(0.5, 20.0)
        self._range_low_input.setDecimals(2)
        self._range_low_input.setMinimumWidth(160)

        self._range_high_input = QDoubleSpinBox()
        self._range_high_input.setRange(0.5, 20.0)
        self._range_high_input.setDecimals(2)
        self._range_high_input.setMinimumWidth(160)

        for widget in (
            self._budget_input,
            self._mode_input,
            self._grid_count_input,
            self._grid_step_input,
            self._range_low_input,
            self._range_high_input,
        ):
            widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

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
        group.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)

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
        self._plan_preview_status = QLabel("Plan preview will appear here after analysis.")
        self._plan_preview_status.setWordWrap(True)
        layout.addWidget(self._plan_preview_status)

        self._plan_preview_table = QTableWidget(0, 4)
        self._plan_preview_table.setHorizontalHeaderLabels(["Side", "Price", "Qty", "% from mid"])
        self._plan_preview_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._plan_preview_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._plan_preview_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._plan_preview_table.verticalHeader().setVisible(False)
        header = self._plan_preview_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self._plan_preview_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self._plan_preview_table)

        metrics_layout = QHBoxLayout()
        self._preview_capital_label = QLabel("Estimated capital usage: —")
        self._preview_exposure_label = QLabel("Max exposure: —")
        self._preview_levels_label = QLabel("Levels: 0")
        metrics_layout.addWidget(self._preview_capital_label)
        metrics_layout.addWidget(self._preview_exposure_label)
        metrics_layout.addStretch()
        metrics_layout.addWidget(self._preview_levels_label)
        layout.addLayout(metrics_layout)
        group.setLayout(layout)
        group.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        return group

    def _build_logs_panel(self) -> QWidget:
        group = QGroupBox("Local logs")
        layout = QVBoxLayout()
        self._logs_panel = PairLogsPanel()
        layout.addWidget(self._logs_panel)
        group.setLayout(layout)
        group.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        return group

    def _build_chat_panel(self) -> QWidget:
        panel = QGroupBox("AI Chat")
        layout = QVBoxLayout()
        self._chat_history = QTextEdit()
        self._chat_history.setReadOnly(True)
        self._chat_history.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self._chat_history)

        input_row = QHBoxLayout()
        self._chat_input = QLineEdit()
        self._chat_input.setPlaceholderText("Ask AI to adjust strategy...")
        self._chat_send_button = QPushButton("Send")
        self._chat_input.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._chat_send_button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        input_row.addWidget(self._chat_input)
        input_row.addWidget(self._chat_send_button)

        layout.addLayout(input_row)
        panel.setLayout(layout)
        panel.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
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

    def _format_ai_summary(self, response: AiResponse) -> str:
        lines = [
            "AI analysis summary:",
            f"Market type: {response.summary.market_type}",
            f"Volatility: {response.summary.volatility}",
            f"Liquidity: {response.summary.liquidity}",
            f"Spread: {response.summary.spread}",
        ]
        if response.summary.advice:
            lines.append(f"Advice: {response.summary.advice}")
        return "\n".join(lines)

    def _normalize_mode(self, mode: str) -> str:
        normalized = mode.strip().lower()
        if "adaptive" in normalized:
            return "Adaptive Grid"
        if "manual" in normalized:
            return "Manual"
        return "Grid"

    def _split_symbol(self, symbol: str) -> tuple[str, str]:
        if "/" in symbol:
            base, quote = symbol.split("/", maxsplit=1)
            return base.strip(), quote.strip()
        if symbol.upper().endswith("USDT"):
            return symbol[:-4], "USDT"
        return symbol, self._app_state.default_quote

    def _plan_to_dict(self, plan: AiPlan) -> dict[str, Any]:
        return {
            "mode": plan.mode,
            "grid_count": plan.grid_count,
            "grid_step_pct": plan.grid_step_pct,
            "range_low_pct": plan.range_low_pct,
            "range_high_pct": plan.range_high_pct,
            "budget_usdt": plan.budget_usdt,
        }

    def _render_ai_questions(self, questions: list[str], tool_requests: list[str]) -> None:
        if questions:
            self._append_chat("AI", f"Questions: {', '.join(questions)}")
            self._log_event(f"[AI] Questions: {', '.join(questions)}")
        if tool_requests:
            self._append_chat("AI", f"Tool requests: {', '.join(tool_requests)}")
            self._log_event(f"[AI] Tool requests: {', '.join(tool_requests)}")

    def _render_preview_from_ai(self, response: AiResponse) -> None:
        if response.preview_levels:
            self._render_plan_preview_levels(response.preview_levels)
        else:
            self._rebuild_plan_preview(reason="built")

    def _render_plan_preview_levels(self, levels: list[Any]) -> None:
        if not levels:
            self._plan_preview_table.setRowCount(0)
            self._plan_preview_status.setText("No preview levels returned by AI.")
            return
        self._plan_preview_status.setText("AI preview levels loaded.")
        self._plan_preview_table.setRowCount(len(levels))
        for row, level in enumerate(levels):
            side = getattr(level, "side", "")
            price = getattr(level, "price", 0.0)
            qty = getattr(level, "qty", 0.0)
            pct = getattr(level, "pct_from_mid", 0.0)
            for col, value in enumerate(
                [side, f"{price:.6f}", f"{qty:.6f}", f"{pct:.2f}%"],
            ):
                item = QTableWidgetItem(value)
                if col == 0:
                    item.setTextAlignment(Qt.AlignVCenter | Qt.AlignLeft)
                else:
                    item.setTextAlignment(Qt.AlignVCenter | Qt.AlignRight)
                self._plan_preview_table.setItem(row, col, item)
        buy_levels = [level for level in levels if getattr(level, "side", "").upper() == "BUY"]
        estimated_capital = sum(getattr(level, "price", 0) * getattr(level, "qty", 0) for level in buy_levels)
        budget = self._budget_input.value()
        self._preview_capital_label.setText(f"Estimated capital usage: {estimated_capital:.2f} USDT")
        self._preview_exposure_label.setText(f"Max exposure: {budget:.2f} USDT")
        self._preview_levels_label.setText(f"Levels: {len(levels)}")
        self._log_event(f"[PLAN] AI preview loaded | levels={len(levels)}")

    def _apply_ai_patch(self, patch: AiPatchResponse) -> None:
        if self._last_ai_response is None:
            return
        plan = self._last_ai_response.plan
        updated = {
            "mode": plan.mode,
            "grid_count": plan.grid_count,
            "grid_step_pct": plan.grid_step_pct,
            "range_low_pct": plan.range_low_pct,
            "range_high_pct": plan.range_high_pct,
            "budget_usdt": plan.budget_usdt,
        }
        for key, value in patch.patch.items():
            if key in updated:
                updated[key] = value
        self._last_ai_response.plan = AiPlan(
            mode=str(updated["mode"]),
            grid_count=int(float(updated["grid_count"])),
            grid_step_pct=float(updated["grid_step_pct"]),
            range_low_pct=float(updated["range_low_pct"]),
            range_high_pct=float(updated["range_high_pct"]),
            budget_usdt=float(updated["budget_usdt"]),
        )
        if patch.preview_levels:
            self._last_ai_response.preview_levels = patch.preview_levels
        else:
            self._last_ai_response.preview_levels = []
        self._apply_plan()

    def _prepare_data(self) -> None:
        self._data_ready = False
        self._plan_applied = False
        self._set_state(BotState.PREPARING_DATA, reason="prepare_data")
        self._log_event("Prepare data requested.")
        self._analysis_summary.setText(self._preparing_summary())
        delay_ms = random.randint(300, 800)
        QTimer.singleShot(delay_ms, self._finish_prepare)

    def _finish_prepare(self) -> None:
        self._data_ready = True
        self._prepared_mid_price = round(random.uniform(95, 105), 2)
        self._market_context = self._build_market_context()
        period = self._topbar.period_combo.currentText()
        quality = self._topbar.quality_combo.currentText()
        self._analysis_summary.setText(
            f"Prepared demo datapack ({self.symbol}, {period}, {quality})."
        )
        self._ai_request_card.hide()
        self._set_state(BotState.DATA_READY, reason="prepare_complete")
        self._log_event("Prepared data pack (demo).")

    def _analyze(self) -> None:
        if not self._data_ready:
            QMessageBox.warning(self, "No data", "Prepare data before AI analysis.")
            self._log_event("AI analyze requested without data.")
            return
        if not self._app_state.openai_key_present:
            QMessageBox.warning(self, "Missing OpenAI key", "Set the OpenAI API key in Settings.")
            self._log_event("AI analyze skipped: missing OpenAI key.")
            return
        datapack = self._build_datapack()
        self._last_datapack = datapack
        self._set_state(BotState.ANALYZING, reason="ai_analyze")
        self._log_event("AI analysis started.")
        self._analysis_summary.setText(self._analyzing_summary())
        worker = _AiWorker(lambda: self._run_ai_analyze(datapack))
        worker.signals.success.connect(self._handle_ai_analyze_success)
        worker.signals.error.connect(self._handle_ai_analyze_error)
        self._thread_pool.start(worker)

    def _run_ai_analyze(self, datapack: dict[str, Any]) -> AiResponse:
        client = OpenAIClient(
            api_key=self._app_state.openai_api_key,
            model=self._app_state.openai_model,
            timeout_s=25.0,
            retries=2,
        )
        return asyncio.run(client.analyze_pair(datapack))

    def _handle_ai_analyze_success(self, response: object) -> None:
        if not isinstance(response, AiResponse):
            self._handle_ai_analyze_error("AI response invalid or empty.")
            return
        self._ai_request_card.hide()
        self._last_ai_response = response
        self._analysis_summary.setText(self._format_ai_summary(response))
        self._plan_preview_status.setText("AI analysis complete. Apply plan to populate strategy.")
        self._set_state(BotState.PLAN_READY, reason="analysis_complete")
        self._log_event("AI analyze ok")
        self._append_chat("AI", "Analysis complete. Click Apply Plan to populate strategy.")
        if response.risk_notes:
            self._append_chat("AI", f"Risks: {', '.join(response.risk_notes)}")
        self._render_ai_questions(response.questions, response.tool_requests)
        self._render_preview_from_ai(response)

    def _handle_ai_analyze_error(self, message: str) -> None:
        self._log_event(f"AI analyze failed: {message}")
        self._append_chat("AI", f"AI error: {message}")
        self._analysis_summary.setText("AI analysis failed. Check logs for details.")
        self._set_state(BotState.DATA_READY, reason="analysis_failed")

    def _is_plan_applied(self) -> bool:
        return self._plan_applied

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
        self._rebuild_plan_preview(reason="strategy_edit")

    def _reset_strategy_to_ai(self) -> None:
        if not self._ai_values:
            return
        for field_key, value in self._ai_values.items():
            self._set_field_value(field_key, value, origin="ai")
        self._user_touched.clear()
        self._reset_ai_button.setEnabled(True)
        self._log_event("[UI] Reset strategy to AI values")

    def _apply_plan(self) -> None:
        if self._last_ai_response is None:
            QMessageBox.warning(self, "No AI plan", "Run AI Analyze before applying the plan.")
            self._log_event("Apply plan skipped: missing AI response.")
            return
        demo_plan = self._last_ai_response.plan
        self._ai_values = {}
        self._user_touched.clear()
        self._set_field_value("budget", demo_plan.budget_usdt, origin="ai")
        self._set_field_value("mode", self._normalize_mode(demo_plan.mode), origin="ai")
        self._set_field_value("grid_count", demo_plan.grid_count, origin="ai")
        self._set_field_value("grid_step_pct", demo_plan.grid_step_pct, origin="ai")
        self._set_field_value("range_low_pct", demo_plan.range_low_pct, origin="ai")
        self._set_field_value("range_high_pct", demo_plan.range_high_pct, origin="ai")
        self._reset_ai_button.setEnabled(True)
        self._plan_applied = True
        self._set_state(BotState.PLAN_READY, reason="apply_plan")
        self._log_event("[AI] Plan applied -> strategy populated")
        self._render_preview_from_ai(self._last_ai_response)

    def _confirm_start(self) -> None:
        if self._pair_state.state == BotState.PAUSED:
            self._tick_timer.start()
            self._set_state(BotState.RUNNING, reason="resume")
            self._log_event("RUNNING (resume)")
            return
        self._tick_count = 0
        self._tick_timer.start()
        self._set_state(BotState.RUNNING, reason="confirm_start")
        if self._topbar.dry_run_toggle.isChecked():
            self._log_event("RUNNING (dry-run)")
        else:
            self._log_event("RUNNING")

    def _toggle_pause(self) -> None:
        if self._pair_state.state == BotState.RUNNING:
            self._tick_timer.stop()
            self._set_state(BotState.PAUSED, reason="pause")
            self._log_event("Execution paused.")
        elif self._pair_state.state == BotState.PAUSED:
            self._tick_timer.start()
            self._set_state(BotState.RUNNING, reason="resume")
            self._log_event("Execution resumed.")

    def _stop_run(self) -> None:
        self._tick_timer.stop()
        self._tick_count = 0
        self._data_ready = False
        self._plan_applied = False
        self._analysis_summary.setText(self._idle_summary())
        self._set_state(BotState.IDLE, reason="stop")
        self._log_event("Execution stopped.")

    def _log_tick(self) -> None:
        self._tick_count += 1
        self._log_event(f"tick {self._tick_count}")

    def _send_chat(self) -> None:
        message = self._chat_input.text().strip()
        if not message:
            return
        if not self._app_state.openai_key_present:
            self._append_chat("AI", "Missing OpenAI key. Set it in Settings.")
            self._log_event("AI chat skipped: missing OpenAI key.")
            return
        if self._last_ai_response is None or self._last_datapack is None:
            self._append_chat("AI", "Run AI Analyze before requesting adjustments.")
            self._log_event("AI chat skipped: missing AI response.")
            return
        self._append_chat("User", message)
        self._chat_input.clear()
        worker = _AiWorker(lambda: self._run_ai_chat_adjust(message))
        worker.signals.success.connect(self._handle_ai_chat_adjust_success)
        worker.signals.error.connect(self._handle_ai_chat_adjust_error)
        self._thread_pool.start(worker)

    def _run_ai_chat_adjust(self, message: str) -> AiPatchResponse:
        if self._last_datapack is None or self._last_ai_response is None:
            raise RuntimeError("No AI plan to adjust")
        client = OpenAIClient(
            api_key=self._app_state.openai_api_key,
            model=self._app_state.openai_model,
            timeout_s=25.0,
            retries=2,
        )
        return asyncio.run(
            client.chat_adjust(
                datapack=self._last_datapack,
                last_plan=self._plan_to_dict(self._last_ai_response.plan),
                user_message=message,
            )
        )

    def _handle_ai_chat_adjust_success(self, response: object) -> None:
        if not isinstance(response, AiPatchResponse):
            self._handle_ai_chat_adjust_error("AI patch response invalid or empty.")
            return
        if self._last_ai_response is None:
            self._handle_ai_chat_adjust_error("No AI plan available.")
            return
        self._apply_ai_patch(response)
        message = response.message or "Plan updated."
        self._append_chat("AI", f"AI updated plan: {message}")
        self._render_ai_questions(response.questions, response.tool_requests)

    def _handle_ai_chat_adjust_error(self, message: str) -> None:
        self._append_chat("AI", f"AI adjust error: {message}")
        self._log_event(f"AI chat adjust failed: {message}")

    def _append_chat(self, sender: str, message: str) -> None:
        self._chat_history.append(f"{sender}: {message}")

    def _handle_price_update(self, symbol: str, price: float, source: str, age_ms: int) -> None:
        if symbol != self.symbol:
            return
        self._latest_price = price
        self._latest_price_source = source
        self._latest_price_age_ms = age_ms
        self._topbar.price_label.setText(f"Price: {price:.6f} ({source}, {age_ms} ms)")

    def _set_state(self, state: BotState, reason: str = "") -> None:
        previous = self._pair_state.state
        if previous == state:
            return
        self._pair_state.set_state(state, reason=reason)
        self._topbar.state_label.setText(f"State: {self._pair_state.state.value}")
        transition = f"state: {previous.value} -> {self._pair_state.state.value}"
        if reason:
            transition += f" (reason={reason})"
        self._log_event(transition)
        self.update_controls_by_state()

    def update_controls_by_state(self) -> None:
        state = self._pair_state.state
        self._topbar.prepare_button.setEnabled(state in {BotState.IDLE, BotState.ERROR})
        ai_ready = self._app_state.ai_connected or self._app_state.openai_key_present
        self._topbar.analyze_button.setEnabled(state == BotState.DATA_READY and ai_ready)
        self._topbar.apply_button.setEnabled(state == BotState.PLAN_READY)
        self._topbar.confirm_button.setEnabled(
            (state == BotState.PLAN_READY and self._plan_applied) or state == BotState.PAUSED
        )
        self._topbar.pause_button.setEnabled(state == BotState.RUNNING)
        self._topbar.stop_button.setEnabled(state in {BotState.RUNNING, BotState.PAUSED})

    def _log_event(self, message: str) -> None:
        self._logs_panel.append(message)
        self._logger.info("%s | %s", self.symbol, message)

    def _rebuild_plan_preview(self, reason: str) -> None:
        mid_price = self._prepared_mid_price or 100.0
        budget = self._budget_input.value()
        grid_count = self._grid_count_input.value()
        range_low_pct = self._range_low_input.value()
        range_high_pct = self._range_high_input.value()
        if budget <= 0:
            budget = 1000.0
        if grid_count <= 0:
            grid_count = 10
        if range_low_pct <= 0:
            range_low_pct = 1.0
        if range_high_pct <= 0:
            range_high_pct = 1.0
        levels = self._build_demo_levels(
            mid_price=mid_price,
            grid_count=grid_count,
            range_low_pct=range_low_pct,
            range_high_pct=range_high_pct,
            budget=budget,
        )
        self._plan_preview_table.setRowCount(len(levels))
        for row, level in enumerate(levels):
            for col, value in enumerate(
                [level["side"], level["price"], level["qty"], level["pct_from_mid"]],
            ):
                item = QTableWidgetItem(value)
                if col == 0:
                    item.setTextAlignment(Qt.AlignVCenter | Qt.AlignLeft)
                else:
                    item.setTextAlignment(Qt.AlignVCenter | Qt.AlignRight)
                self._plan_preview_table.setItem(row, col, item)

        buy_levels = [level for level in levels if level["side"] == "BUY"]
        estimated_capital = sum(level["quote"] for level in buy_levels)
        self._preview_capital_label.setText(f"Estimated capital usage: {estimated_capital:.2f} USDT")
        self._preview_exposure_label.setText(f"Max exposure: {budget:.2f} USDT")
        self._preview_levels_label.setText(f"Levels: {len(levels)}")

        if reason == "built":
            self._log_event(f"[PLAN] Preview built | levels={len(levels)} mid={mid_price:.2f}")
        elif reason == "strategy_edit":
            self._log_event("[PLAN] Preview updated from strategy edits")

    def _build_demo_levels(
        self,
        mid_price: float,
        grid_count: int,
        range_low_pct: float,
        range_high_pct: float,
        budget: float,
    ) -> list[dict[str, float | str]]:
        low_price = mid_price * (1 - range_low_pct / 100)
        high_price = mid_price * (1 + range_high_pct / 100)
        buy_count = grid_count // 2
        sell_count = grid_count - buy_count
        per_level_quote = budget / grid_count
        levels: list[dict[str, float | str]] = []

        if buy_count:
            buy_step = (mid_price - low_price) / (buy_count + 1)
            for idx in range(buy_count):
                price = low_price + buy_step * (idx + 1)
                qty = per_level_quote / price
                pct = (price - mid_price) / mid_price * 100
                levels.append(
                    {
                        "side": "BUY",
                        "price": f"{price:.4f}",
                        "qty": f"{qty:.6f}",
                        "pct_from_mid": f"{pct:.2f}%",
                        "quote": per_level_quote,
                    },
                )

        if sell_count:
            sell_step = (high_price - mid_price) / (sell_count + 1)
            for idx in range(sell_count):
                price = mid_price + sell_step * (idx + 1)
                qty = per_level_quote / price
                pct = (price - mid_price) / mid_price * 100
                levels.append(
                    {
                        "side": "SELL",
                        "price": f"{price:.4f}",
                        "qty": f"{qty:.6f}",
                        "pct_from_mid": f"{pct:.2f}%",
                        "quote": per_level_quote,
                    },
                )

        return levels

    def _build_datapack(self) -> dict[str, Any]:
        period = self._topbar.period_combo.currentText()
        quality = self._topbar.quality_combo.currentText()
        now_price = self._latest_price if self._latest_price is not None else self._prepared_mid_price
        source = self._latest_price_source or "None"
        age_ms = self._latest_price_age_ms
        base_asset, quote_asset = self._split_symbol(self.symbol)
        spread_estimate = self._market_context.spread if self._market_context else None
        volatility_proxy = None
        if self._prepared_mid_price and now_price:
            try:
                volatility_proxy = (now_price - self._prepared_mid_price) / self._prepared_mid_price * 100
            except ZeroDivisionError:
                volatility_proxy = None
        strategy_inputs = {
            "budget": self._budget_input.value(),
            "mode": self._mode_input.currentText(),
            "grid_count": self._grid_count_input.value(),
            "grid_step_pct": self._grid_step_input.value(),
            "range_low_pct": self._range_low_input.value(),
            "range_high_pct": self._range_high_input.value(),
        }
        return {
            "version": "v2",
            "symbol": self.symbol,
            "base_asset": base_asset,
            "quote_asset": quote_asset,
            "period": period,
            "quality": quality,
            "current_price": {
                "value": now_price,
                "source": source,
                "age_ms": age_ms,
            },
            "spread_estimate_pct": spread_estimate,
            "volatility_proxy_pct": volatility_proxy,
            "exchange_limits": {
                "tickSize": None,
                "stepSize": None,
                "minNotional": None,
            },
            "user_inputs": strategy_inputs,
            "app_settings": {
                "dry_run": self._topbar.dry_run_toggle.isChecked(),
                "allow_request_more_data": self._app_state.allow_ai_more_data,
            },
        }
