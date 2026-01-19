from __future__ import annotations

import asyncio
import random
import re
from dataclasses import dataclass
from datetime import datetime, timezone
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
    QScrollArea,
    QPushButton,
    QPlainTextEdit,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.ai.models import (
    AiAnalysisResult,
    AiLevel,
    AiResponseEnvelope,
    AiTradeOption,
    AiStrategyPatch,
)
from src.ai.openai_client import OpenAIClient
from src.core.logging import get_logger
from src.gui.models.app_state import AppState
from src.gui.models.pair_state import BotState, PairState
from src.gui.models.pair_workspace_state import PairWorkspaceState
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


class _LiquidityWorkerSignals(QObject):
    success = Signal(object)
    error = Signal(str)


class _LiquidityWorker(QRunnable):
    def __init__(self, fn: Callable[[], object]) -> None:
        super().__init__()
        self.signals = _LiquidityWorkerSignals()
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
        self._workspace_state = PairWorkspaceState(symbol=symbol)
        self._data_ready = False
        self._plan_applied = False
        self._market_context: MarketContext | None = None
        self._prepared_mid_price: float | None = None
        self._prepared_period: str | None = None
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
        self._liquidity_stats: dict[str, float | int | None] | None = None
        self._liquidity_in_flight = False
        self._last_ai_response: AiResponseEnvelope | None = None
        self._last_ai_analysis: AiAnalysisResult | None = None
        self._last_datapack: dict[str, Any] | None = None
        self._pending_patch: AiStrategyPatch | None = None
        self._last_strategy_patch: AiStrategyPatch | None = None
        self._last_trade_options: list[AiTradeOption] = []
        self._latest_ai_levels: list[AiLevel] = []
        self._ai_block_confirm = False
        self._trading_windows: list[QWidget] = []
        self._runtime_started_at: datetime | None = None

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

    @property
    def workspace_state(self) -> PairWorkspaceState:
        return self._workspace_state

    def _connect_signals(self) -> None:
        self._topbar.prepare_button.clicked.connect(self._prepare_data)
        self._topbar.analyze_button.clicked.connect(self._analyze)
        self._topbar.apply_button.clicked.connect(self._apply_plan)
        self._topbar.confirm_button.clicked.connect(self._confirm_start)
        self._topbar.trading_button.clicked.connect(self._open_trading_workspace)
        self._topbar.pause_button.clicked.connect(self._toggle_pause)
        self._topbar.stop_button.clicked.connect(self._stop_run)
        self._chat_send_button.clicked.connect(self._send_chat)
        self._chat_apply_button.clicked.connect(self._apply_pending_patch)

    def _build_body(self) -> QSplitter:
        splitter = QSplitter(Qt.Horizontal)
        left_panel = QWidget()
        left_layout = QVBoxLayout()
        left_layout.addWidget(self._build_analysis_panel())
        left_layout.addWidget(self._build_strategy_panel())
        left_layout.addWidget(self._build_plan_panel())
        left_layout.addWidget(self._build_logs_panel())
        left_layout.addStretch()
        left_layout.setContentsMargins(4, 4, 4, 4)
        left_panel.setLayout(left_layout)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setMinimumWidth(560)
        scroll_area.setWidget(left_panel)

        chat_panel = self._build_chat_panel()
        chat_panel.setMinimumWidth(360)

        splitter.addWidget(scroll_area)
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
        self._analysis_summary.setMinimumHeight(140)
        self._analysis_summary.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self._analysis_summary)

        self._ai_request_card = QGroupBox("AI запросил больше данных")
        request_layout = QVBoxLayout()
        self._ai_request_label = QLabel("Для продолжения выберите период 24h и качество Deep.")
        request_layout.addWidget(self._ai_request_label)
        self._ai_request_card.setLayout(request_layout)
        self._ai_request_card.hide()
        layout.addWidget(self._ai_request_card)

        self._trade_options_card = QGroupBox("ZERO_FEE варианты сделок")
        trade_layout = QVBoxLayout()
        self._trade_options_table = QTableWidget(0, 10)
        self._trade_options_table.setHorizontalHeaderLabels(
            [
                "Название",
                "Вход",
                "Выход",
                "TP %",
                "Стоп %",
                "Профит %",
                "Профит USDT",
                "ETA (мин)",
                "Уверенность",
                "Риск",
            ]
        )
        self._trade_options_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._trade_options_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._trade_options_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._trade_options_table.verticalHeader().setVisible(False)
        trade_layout.addWidget(self._trade_options_table)
        self._apply_best_option_button = QPushButton("Применить лучший вариант")
        self._apply_best_option_button.setEnabled(False)
        self._apply_best_option_button.clicked.connect(self._apply_best_trade_option)
        trade_layout.addWidget(self._apply_best_option_button)
        self._trade_options_card.setLayout(trade_layout)
        self._trade_options_card.hide()
        layout.addWidget(self._trade_options_card)

        group.setLayout(layout)
        group.setMinimumHeight(180)
        group.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        return group

    def _build_strategy_panel(self) -> QWidget:
        group = QGroupBox("Strategy form")
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        self._strategy_id_input = QComboBox()
        self._strategy_id_input.addItems(
            [
                "GRID_CLASSIC",
                "GRID_BIASED_LONG",
                "GRID_BIASED_SHORT",
                "RANGE_MEAN_REVERSION",
                "TREND_FOLLOW_GRID",
                "DO_NOT_TRADE",
            ]
        )
        self._strategy_id_input.setMinimumWidth(180)

        self._budget_input = QDoubleSpinBox()
        self._budget_input.setRange(10.0, 1_000_000.0)
        self._budget_input.setDecimals(2)
        self._budget_input.setMinimumWidth(180)

        self._mode_input = QComboBox()
        self._mode_input.addItems(["Grid", "Adaptive Grid", "Manual"])
        self._mode_input.setMinimumWidth(180)

        self._grid_count_input = QSpinBox()
        self._grid_count_input.setRange(3, 60)
        self._grid_count_input.setMinimumWidth(180)

        self._grid_step_input = QDoubleSpinBox()
        self._grid_step_input.setRange(0.1, 10.0)
        self._grid_step_input.setDecimals(2)
        self._grid_step_input.setMinimumWidth(180)

        self._range_low_input = QDoubleSpinBox()
        self._range_low_input.setRange(0.5, 20.0)
        self._range_low_input.setDecimals(2)
        self._range_low_input.setMinimumWidth(180)

        self._range_high_input = QDoubleSpinBox()
        self._range_high_input.setRange(0.5, 20.0)
        self._range_high_input.setDecimals(2)
        self._range_high_input.setMinimumWidth(180)

        self._bias_buy_input = QDoubleSpinBox()
        self._bias_buy_input.setRange(0, 100)
        self._bias_buy_input.setDecimals(1)
        self._bias_buy_input.setMinimumWidth(180)

        self._bias_sell_input = QDoubleSpinBox()
        self._bias_sell_input.setRange(0, 100)
        self._bias_sell_input.setDecimals(1)
        self._bias_sell_input.setMinimumWidth(180)

        self._order_size_mode_input = QComboBox()
        self._order_size_mode_input.addItems(["equal", "martingale_light", "liquidity_weighted"])
        self._order_size_mode_input.setMinimumWidth(180)

        self._max_orders_input = QSpinBox()
        self._max_orders_input.setRange(1, 200)
        self._max_orders_input.setMinimumWidth(180)

        self._max_exposure_input = QDoubleSpinBox()
        self._max_exposure_input.setRange(1, 100)
        self._max_exposure_input.setDecimals(1)
        self._max_exposure_input.setMinimumWidth(180)

        self._hard_stop_input = QDoubleSpinBox()
        self._hard_stop_input.setRange(0.1, 50.0)
        self._hard_stop_input.setDecimals(2)
        self._hard_stop_input.setMinimumWidth(180)

        self._cooldown_input = QSpinBox()
        self._cooldown_input.setRange(1, 240)
        self._cooldown_input.setMinimumWidth(180)

        self._soft_stop_input = QLineEdit()
        self._soft_stop_input.setMinimumWidth(180)
        self._soft_stop_input.setPlaceholderText("comma separated")

        self._kill_switch_input = QLineEdit()
        self._kill_switch_input.setMinimumWidth(180)
        self._kill_switch_input.setPlaceholderText("comma separated")

        self._recheck_interval_input = QSpinBox()
        self._recheck_interval_input.setRange(10, 3600)
        self._recheck_interval_input.setMinimumWidth(180)

        self._ai_reanalyze_interval_input = QSpinBox()
        self._ai_reanalyze_interval_input.setRange(30, 7200)
        self._ai_reanalyze_interval_input.setMinimumWidth(180)

        self._min_change_rebuild_input = QDoubleSpinBox()
        self._min_change_rebuild_input.setRange(0.05, 10.0)
        self._min_change_rebuild_input.setDecimals(2)
        self._min_change_rebuild_input.setMinimumWidth(180)

        self._volatility_mode_input = QComboBox()
        self._volatility_mode_input.addItems(["low", "medium", "high"])
        self._volatility_mode_input.setMinimumWidth(180)

        for widget in (
            self._strategy_id_input,
            self._budget_input,
            self._mode_input,
            self._grid_count_input,
            self._grid_step_input,
            self._range_low_input,
            self._range_high_input,
            self._bias_buy_input,
            self._bias_sell_input,
            self._order_size_mode_input,
            self._max_orders_input,
            self._max_exposure_input,
            self._hard_stop_input,
            self._cooldown_input,
            self._soft_stop_input,
            self._kill_switch_input,
            self._recheck_interval_input,
            self._ai_reanalyze_interval_input,
            self._min_change_rebuild_input,
            self._volatility_mode_input,
        ):
            widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        form.addRow("Strategy ID", self._strategy_id_input)
        form.addRow("Budget (USDT)", self._budget_input)
        form.addRow("Mode", self._mode_input)
        form.addRow("Grid count", self._grid_count_input)
        form.addRow("Grid step %", self._grid_step_input)
        form.addRow("Range low %", self._range_low_input)
        form.addRow("Range high %", self._range_high_input)
        form.addRow("Bias buy %", self._bias_buy_input)
        form.addRow("Bias sell %", self._bias_sell_input)
        form.addRow("Order size mode", self._order_size_mode_input)
        form.addRow("Max orders", self._max_orders_input)
        form.addRow("Max exposure %", self._max_exposure_input)
        form.addRow("Hard stop %", self._hard_stop_input)
        form.addRow("Cooldown (min)", self._cooldown_input)
        form.addRow("Soft stop rules", self._soft_stop_input)
        form.addRow("Kill switch rules", self._kill_switch_input)
        form.addRow("Recheck interval (s)", self._recheck_interval_input)
        form.addRow("AI reanalyze (s)", self._ai_reanalyze_interval_input)
        form.addRow("Min change rebuild %", self._min_change_rebuild_input)
        form.addRow("Volatility mode", self._volatility_mode_input)

        self._reset_ai_button = QPushButton("Сбросить к AI")
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
            "strategy_id": self._strategy_id_input,
            "budget": self._budget_input,
            "mode": self._mode_input,
            "grid_count": self._grid_count_input,
            "grid_step_pct": self._grid_step_input,
            "range_low_pct": self._range_low_input,
            "range_high_pct": self._range_high_input,
            "bias_buy_pct": self._bias_buy_input,
            "bias_sell_pct": self._bias_sell_input,
            "order_size_mode": self._order_size_mode_input,
            "max_orders": self._max_orders_input,
            "max_exposure_pct": self._max_exposure_input,
            "hard_stop_pct": self._hard_stop_input,
            "cooldown_minutes": self._cooldown_input,
            "soft_stop_rules": self._soft_stop_input,
            "kill_switch_rules": self._kill_switch_input,
            "recheck_interval_sec": self._recheck_interval_input,
            "ai_reanalyze_interval_sec": self._ai_reanalyze_interval_input,
            "min_change_to_rebuild_pct": self._min_change_rebuild_input,
            "volatility_mode": self._volatility_mode_input,
        }

        self._strategy_id_input.currentTextChanged.connect(lambda _: self._on_field_changed("strategy_id"))
        self._budget_input.valueChanged.connect(lambda _: self._on_field_changed("budget"))
        self._mode_input.currentTextChanged.connect(lambda _: self._on_field_changed("mode"))
        self._grid_count_input.valueChanged.connect(lambda _: self._on_field_changed("grid_count"))
        self._grid_step_input.valueChanged.connect(lambda _: self._on_field_changed("grid_step_pct"))
        self._range_low_input.valueChanged.connect(lambda _: self._on_field_changed("range_low_pct"))
        self._range_high_input.valueChanged.connect(lambda _: self._on_field_changed("range_high_pct"))
        self._bias_buy_input.valueChanged.connect(lambda _: self._on_field_changed("bias_buy_pct"))
        self._bias_sell_input.valueChanged.connect(lambda _: self._on_field_changed("bias_sell_pct"))
        self._order_size_mode_input.currentTextChanged.connect(lambda _: self._on_field_changed("order_size_mode"))
        self._max_orders_input.valueChanged.connect(lambda _: self._on_field_changed("max_orders"))
        self._max_exposure_input.valueChanged.connect(lambda _: self._on_field_changed("max_exposure_pct"))
        self._hard_stop_input.valueChanged.connect(lambda _: self._on_field_changed("hard_stop_pct"))
        self._cooldown_input.valueChanged.connect(lambda _: self._on_field_changed("cooldown_minutes"))
        self._soft_stop_input.textChanged.connect(lambda _: self._on_field_changed("soft_stop_rules"))
        self._kill_switch_input.textChanged.connect(lambda _: self._on_field_changed("kill_switch_rules"))
        self._recheck_interval_input.valueChanged.connect(lambda _: self._on_field_changed("recheck_interval_sec"))
        self._ai_reanalyze_interval_input.valueChanged.connect(
            lambda _: self._on_field_changed("ai_reanalyze_interval_sec")
        )
        self._min_change_rebuild_input.valueChanged.connect(
            lambda _: self._on_field_changed("min_change_to_rebuild_pct")
        )
        self._volatility_mode_input.currentTextChanged.connect(lambda _: self._on_field_changed("volatility_mode"))

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
        group.setMinimumHeight(240)
        group.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        return group

    def _build_logs_panel(self) -> QWidget:
        group = QGroupBox("Local logs")
        layout = QVBoxLayout()
        self._logs_panel = PairLogsPanel()
        layout.addWidget(self._logs_panel)
        group.setLayout(layout)
        group.setMinimumHeight(220)
        group.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        return group

    def _build_chat_panel(self) -> QWidget:
        panel = QGroupBox("AI чат")
        layout = QVBoxLayout()
        self._chat_history = QTextEdit()
        self._chat_history.setReadOnly(True)
        self._chat_history.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self._chat_history)

        input_row = QHBoxLayout()
        self._chat_input = QLineEdit()
        self._chat_input.setPlaceholderText("Попросите AI скорректировать стратегию...")
        self._chat_send_button = QPushButton("Send")
        self._chat_apply_button = QPushButton("Apply Patch")
        self._chat_apply_button.setEnabled(False)
        self._chat_input.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._chat_send_button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        input_row.addWidget(self._chat_input)
        input_row.addWidget(self._chat_send_button)
        input_row.addWidget(self._chat_apply_button)

        layout.addLayout(input_row)
        panel.setLayout(layout)
        panel.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        return panel

    def _idle_summary(self) -> str:
        return "Данные не подготовлены.\nНажмите «Prepare Data», чтобы собрать рынок."

    def _preparing_summary(self) -> str:
        return "Подготовка рыночных данных…"

    def _analyzing_summary(self) -> str:
        return "Данные готовы.\nИдет анализ AI…"

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
            lines.append(f"AI заметка: {ai_note}")
        return "\n".join(lines)

    def _format_ai_summary(self, envelope: AiResponseEnvelope, analysis: AiAnalysisResult) -> str:
        lines = [
            "Сводка AI анализа:",
            f"Статус: {analysis.status}",
        ]
        if analysis.confidence is not None:
            lines.append(f"Уверенность: {analysis.confidence:.2f}")
        if analysis.reason_codes:
            lines.append(f"Причины: {', '.join(analysis.reason_codes)}")
        if analysis.summary_ru:
            lines.append(analysis.summary_ru)
        return "\n".join(lines)

    def _normalize_mode(self, mode: str) -> str:
        normalized = mode.strip().lower()
        if "adaptive" in normalized:
            return "Adaptive Grid"
        if "manual" in normalized:
            return "Manual"
        return "Grid"

    def _set_summary_status(self, status: str) -> None:
        if status in {"WARN"}:
            self._analysis_summary.setStyleSheet("background-color: rgba(217, 164, 65, 0.15);")
            return
        if status in {"DANGER", "ERROR"}:
            self._analysis_summary.setStyleSheet("background-color: rgba(217, 65, 65, 0.18);")
            return
        self._analysis_summary.setStyleSheet("")

    def _split_symbol(self, symbol: str) -> tuple[str, str]:
        if "/" in symbol:
            base, quote = symbol.split("/", maxsplit=1)
            return base.strip(), quote.strip()
        if symbol.upper().endswith("USDT"):
            return symbol[:-4], "USDT"
        return symbol, self._app_state.default_quote

    def _is_zero_fee_symbol(self) -> bool:
        return self.symbol.upper() in {symbol.upper() for symbol in self._app_state.zero_fee_symbols}

    def _extract_budget_value(self, message: str) -> float | None:
        pattern = r"(?:\bbudget\b|\bбюджет\b)\s*([0-9]+(?:[\.,][0-9]+)?)"
        match = re.search(pattern, message.lower())
        if not match:
            return None
        raw = match.group(1).replace(",", ".")
        try:
            return float(raw)
        except ValueError:
            return None

    def _collect_current_strategy(self) -> dict[str, Any]:
        levels: list[dict[str, Any]] = []
        source_levels = self._latest_ai_levels
        if not source_levels and self._workspace_state.plan_levels:
            source_levels = self._coerce_levels(self._workspace_state.plan_levels)
        if source_levels:
            levels = [
                {
                    "side": level.side,
                    "price": level.price,
                    "qty": level.qty,
                    "pct_from_mid": level.pct_from_mid,
                }
                for level in source_levels
            ]
        return {
            "strategy_id": self._strategy_id_input.currentText(),
            "budget_usdt": self._budget_input.value(),
            "levels": levels,
            "grid_step_pct": self._grid_step_input.value(),
            "range_low_pct": self._range_low_input.value(),
            "range_high_pct": self._range_high_input.value(),
            "bias": {"buy_pct": self._bias_buy_input.value(), "sell_pct": self._bias_sell_input.value()},
            "order_size_mode": self._order_size_mode_input.currentText(),
            "max_orders": self._max_orders_input.value(),
            "max_exposure_pct": self._max_exposure_input.value(),
            "hard_stop_pct": self._hard_stop_input.value(),
            "cooldown_minutes": self._cooldown_input.value(),
            "soft_stop_rules": self._split_rules(self._soft_stop_input.text()),
            "kill_switch_rules": self._split_rules(self._kill_switch_input.text()),
            "recheck_interval_sec": self._recheck_interval_input.value(),
            "ai_reanalyze_interval_sec": self._ai_reanalyze_interval_input.value(),
            "min_change_to_rebuild_pct": self._min_change_rebuild_input.value(),
            "volatility_mode": self._volatility_mode_input.currentText(),
        }

    def _render_ai_actions(self, actions: list[str]) -> None:
        if not actions:
            return
        self._append_chat("AI", f"Рекомендации: {', '.join(actions)}")
        self._log_event(f"[AI] Рекомендации: {', '.join(actions)}")

    def _split_rules(self, text: str) -> list[str]:
        if not text:
            return []
        return [item.strip() for item in text.split(",") if item.strip()]

    def _coerce_levels(self, raw_levels: list[Any]) -> list[AiLevel]:
        levels: list[AiLevel] = []
        for item in raw_levels:
            if isinstance(item, AiLevel):
                levels.append(item)
                continue
            if not isinstance(item, dict):
                continue
            try:
                levels.append(
                    AiLevel(
                        side=str(item.get("side", "")),
                        price=float(item.get("price", 0)),
                        qty=float(item.get("qty", 0)),
                        pct_from_mid=float(item.get("pct_from_mid", 0)),
                    )
                )
            except (TypeError, ValueError):
                continue
        return levels

    def _render_preview_from_ai(self, patch: AiStrategyPatch | None) -> None:
        levels = []
        if patch is not None:
            raw_levels = patch.parameters_patch.get("levels")
            if isinstance(raw_levels, list):
                levels = self._coerce_levels(raw_levels)
        if levels:
            self._render_plan_preview_levels(levels)
        else:
            self._rebuild_plan_preview(reason="built")

    def _render_plan_preview_levels(self, levels: list[AiLevel]) -> None:
        if not levels:
            self._plan_preview_table.setRowCount(0)
            self._plan_preview_status.setText("AI не вернул уровни для предпросмотра.")
            self._update_workspace_plan([], source="none")
            return
        self._latest_ai_levels = list(levels)
        self._plan_preview_status.setText("Предпросмотр уровней от AI загружен.")
        self._plan_preview_table.setRowCount(len(levels))
        plan_levels: list[dict[str, str]] = []
        for row, level in enumerate(levels):
            side = getattr(level, "side", "")
            price = getattr(level, "price", 0.0)
            qty = getattr(level, "qty", 0.0)
            pct = getattr(level, "pct_from_mid", 0.0)
            row_values = [side, f"{price:.6f}", f"{qty:.6f}", f"{pct:.2f}%"]
            plan_levels.append(
                {
                    "side": str(side),
                    "price": row_values[1],
                    "qty": row_values[2],
                    "pct_from_mid": row_values[3],
                }
            )
            for col, value in enumerate(row_values):
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
        self._update_workspace_plan(plan_levels, source="ai")
        self._log_event(f"[PLAN] AI preview loaded | levels={len(levels)}")

    def _render_trade_options(self, options: list[AiTradeOption]) -> None:
        if not self._is_zero_fee_symbol():
            self._trade_options_card.hide()
            self._apply_best_option_button.setEnabled(False)
            self._trade_options_table.setRowCount(0)
            return
        if not options:
            self._trade_options_card.show()
            self._apply_best_option_button.setEnabled(False)
            self._trade_options_table.setRowCount(0)
            return
        self._trade_options_card.show()
        self._trade_options_table.setRowCount(len(options))
        for row, option in enumerate(options):
            row_values = [
                option.name_ru,
                f"{option.entry:.6f}",
                f"{option.exit:.6f}",
                f"{option.tp_pct:.2f}",
                f"{option.stop_pct:.2f}",
                f"{option.expected_profit_pct:.2f}",
                f"{option.expected_profit_usdt:.2f}",
                str(option.eta_min),
                f"{option.confidence:.2f}" if option.confidence is not None else "-",
                option.risk_note_ru or "-",
            ]
            for col, value in enumerate(row_values):
                item = QTableWidgetItem(value)
                item.setTextAlignment(Qt.AlignCenter)
                self._trade_options_table.setItem(row, col, item)
        self._apply_best_option_button.setEnabled(True)

    def _build_patch_from_trade_option(self, option: AiTradeOption) -> AiStrategyPatch:
        budget = self._budget_input.value()
        mid_price = self._prepared_mid_price or option.entry
        qty = 0.0
        if option.entry > 0:
            qty = budget / option.entry
        levels = [
            {
                "side": "BUY",
                "price": option.entry,
                "qty": qty,
                "pct_from_mid": (option.entry - mid_price) / mid_price * 100 if mid_price else 0.0,
            },
            {
                "side": "SELL",
                "price": option.exit,
                "qty": qty,
                "pct_from_mid": (option.exit - mid_price) / mid_price * 100 if mid_price else 0.0,
            },
        ]
        parameters_patch = {
            "strategy_id": "RANGE_MEAN_REVERSION",
            "budget_usdt": budget,
            "levels": levels,
            "grid_step_pct": max(0.1, abs(option.tp_pct) / 2),
            "range_low_pct": max(0.1, abs(option.stop_pct)),
            "range_high_pct": max(0.1, abs(option.tp_pct)),
            "bias": {"buy_pct": 50, "sell_pct": 50},
            "order_size_mode": "equal",
            "max_orders": max(2, self._max_orders_input.value()),
            "max_exposure_pct": self._max_exposure_input.value(),
            "hard_stop_pct": max(0.1, abs(option.stop_pct)),
            "cooldown_minutes": self._cooldown_input.value(),
            "soft_stop_rules": [],
            "kill_switch_rules": [],
            "recheck_interval_sec": self._recheck_interval_input.value(),
            "ai_reanalyze_interval_sec": self._ai_reanalyze_interval_input.value(),
            "min_change_to_rebuild_pct": self._min_change_rebuild_input.value(),
            "volatility_mode": self._volatility_mode_input.currentText(),
        }
        return AiStrategyPatch(parameters_patch=parameters_patch)

    def _apply_best_trade_option(self) -> None:
        if not self._last_trade_options:
            return
        best = max(
            self._last_trade_options,
            key=lambda option: (option.expected_profit_pct, option.expected_profit_usdt),
        )
        patch = self._last_strategy_patch or self._build_patch_from_trade_option(best)
        reply = QMessageBox.question(
            self,
            "Подтверждение",
            f"Применить лучший вариант сделки: {best.name_ru}?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        self._apply_strategy_patch(patch, origin="ai")
        self._last_strategy_patch = patch
        self._log_event(f"[AI] Applied best trade option: {best.name_ru}")

    def _update_workspace_plan(
        self,
        plan_levels: list[dict[str, str]],
        source: str,
        demo_source_symbol: str | None = None,
    ) -> None:
        self._workspace_state.plan_levels = list(plan_levels)
        self._workspace_state.plan_source = source
        self._workspace_state.demo_source_symbol = demo_source_symbol
        self._sync_workspace_orders()

    def _sync_workspace_orders(self) -> None:
        if self._plan_applied and self._workspace_state.plan_levels and self._workspace_state.plan_source != "demo":
            self._workspace_state.open_orders = list(self._workspace_state.plan_levels)
        else:
            self._workspace_state.open_orders = []

    def _request_liquidity_stats(self) -> None:
        if self._price_hub is None or self._liquidity_in_flight:
            return
        self._liquidity_in_flight = True

        def _run() -> object:
            return self._price_hub.fetch_ticker_24h(self.symbol)

        worker = _LiquidityWorker(_run)
        worker.signals.success.connect(self._handle_liquidity_success)
        worker.signals.error.connect(self._handle_liquidity_error)
        self._thread_pool.start(worker)

    def _handle_liquidity_success(self, payload: object) -> None:
        self._liquidity_in_flight = False
        if not isinstance(payload, dict):
            self._handle_liquidity_error("Liquidity response invalid.")
            return
        volume = payload.get("volume")
        quote_volume = payload.get("quoteVolume")
        trade_count = payload.get("count")
        try:
            volume_value = float(volume) if volume is not None else None
        except (TypeError, ValueError):
            volume_value = None
        try:
            quote_value = float(quote_volume) if quote_volume is not None else None
        except (TypeError, ValueError):
            quote_value = None
        try:
            trade_value = int(trade_count) if trade_count is not None else None
        except (TypeError, ValueError):
            trade_value = None
        if volume_value is None and quote_value is None:
            self._liquidity_stats = None
            return
        self._liquidity_stats = {
            "volume": volume_value,
            "quote_volume": quote_value,
            "trade_count": trade_value,
        }

    def _handle_liquidity_error(self, message: str) -> None:
        self._liquidity_in_flight = False
        self._liquidity_stats = None
        self._logger.warning("Liquidity 24h unavailable: %s", message)

    def _apply_strategy_patch(self, patch: AiStrategyPatch, origin: str = "ai") -> None:
        updated = self._collect_current_strategy()
        for key, value in patch.parameters_patch.items():
            updated[key] = value
        self._last_strategy_patch = patch
        self._set_field_value("strategy_id", updated.get("strategy_id", self._strategy_id_input.currentText()), origin)
        self._set_field_value("budget", updated.get("budget_usdt", self._budget_input.value()), origin)
        self._set_field_value("grid_step_pct", updated.get("grid_step_pct", self._grid_step_input.value()), origin)
        self._set_field_value("range_low_pct", updated.get("range_low_pct", self._range_low_input.value()), origin)
        self._set_field_value("range_high_pct", updated.get("range_high_pct", self._range_high_input.value()), origin)
        bias = updated.get("bias", {})
        if isinstance(bias, dict):
            if "buy_pct" in bias:
                self._set_field_value("bias_buy_pct", bias.get("buy_pct", self._bias_buy_input.value()), origin)
            if "sell_pct" in bias:
                self._set_field_value("bias_sell_pct", bias.get("sell_pct", self._bias_sell_input.value()), origin)
        self._set_field_value(
            "order_size_mode",
            updated.get("order_size_mode", self._order_size_mode_input.currentText()),
            origin,
        )
        self._set_field_value("max_orders", updated.get("max_orders", self._max_orders_input.value()), origin)
        self._set_field_value(
            "max_exposure_pct",
            updated.get("max_exposure_pct", self._max_exposure_input.value()),
            origin,
        )
        self._set_field_value("hard_stop_pct", updated.get("hard_stop_pct", self._hard_stop_input.value()), origin)
        self._set_field_value("cooldown_minutes", updated.get("cooldown_minutes", self._cooldown_input.value()), origin)
        if "soft_stop_rules" in updated:
            self._set_field_value("soft_stop_rules", ", ".join(updated.get("soft_stop_rules", [])), origin)
        if "kill_switch_rules" in updated:
            self._set_field_value("kill_switch_rules", ", ".join(updated.get("kill_switch_rules", [])), origin)
        self._set_field_value(
            "recheck_interval_sec",
            updated.get("recheck_interval_sec", self._recheck_interval_input.value()),
            origin,
        )
        self._set_field_value(
            "ai_reanalyze_interval_sec",
            updated.get("ai_reanalyze_interval_sec", self._ai_reanalyze_interval_input.value()),
            origin,
        )
        self._set_field_value(
            "min_change_to_rebuild_pct",
            updated.get("min_change_to_rebuild_pct", self._min_change_rebuild_input.value()),
            origin,
        )
        self._set_field_value(
            "volatility_mode",
            updated.get("volatility_mode", self._volatility_mode_input.currentText()),
            origin,
        )
        if "levels" in updated and isinstance(updated["levels"], list):
            self._latest_ai_levels = self._coerce_levels(updated["levels"])
        self._render_preview_from_ai(patch)
        self._plan_applied = True
        self._sync_workspace_orders()
        self._log_event("[AI] Patch applied -> strategy updated")

    def _prepare_data(self) -> None:
        self._data_ready = False
        self._plan_applied = False
        self._ai_block_confirm = False
        self._update_workspace_plan([], source="none")
        self._render_trade_options([])
        self._last_ai_analysis = None
        self._last_strategy_patch = None
        self._last_trade_options = []
        self._prepared_period = None
        self._set_state(BotState.PREPARING_DATA, reason="prepare_data")
        self._log_event("Prepare data requested.")
        self._analysis_summary.setText(self._preparing_summary())
        self._set_summary_status("OK")
        delay_ms = random.randint(300, 800)
        QTimer.singleShot(delay_ms, self._finish_prepare)

    def _finish_prepare(self) -> None:
        self._data_ready = True
        self._prepared_mid_price = round(random.uniform(95, 105), 2)
        self._market_context = self._build_market_context()
        period, fallback_used = self._resolve_period()
        self._prepared_period = period
        quality = self._topbar.quality_combo.currentText()
        if fallback_used:
            QMessageBox.warning(
                self,
                "Minute data unavailable",
                f"Minute candles are unavailable right now. Falling back to {period}.",
            )
            self._log_event(f"Minute data unavailable; fallback to {period}.")
        self._analysis_summary.setText(
            f"Prepared demo datapack ({self.symbol}, {period}, {quality})."
        )
        self._set_summary_status("OK")
        self._ai_request_card.hide()
        self._set_state(BotState.DATA_READY, reason="prepare_complete")
        self._log_event("Prepared data pack (demo).")
        self._request_liquidity_stats()

    def _resolve_period(self) -> tuple[str, bool]:
        period = self._topbar.period_combo.currentText()
        minute_periods = {"1m", "3m", "5m", "15m", "30m"}
        if period in minute_periods and self._latest_price is None:
            return "1h", True
        return period, False

    def _analyze(self) -> None:
        if not self._data_ready:
            QMessageBox.warning(self, "Нет данных", "Сначала подготовьте данные для AI анализа.")
            self._log_event("AI analyze requested without data.")
            return
        if not self._app_state.openai_key_present:
            QMessageBox.warning(self, "Нет ключа OpenAI", "Укажите ключ OpenAI в Settings.")
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

    def _run_ai_analyze(self, datapack: dict[str, Any]) -> AiResponseEnvelope:
        client = OpenAIClient(
            api_key=self._app_state.openai_api_key,
            model=self._app_state.openai_model,
            timeout_s=25.0,
            retries=2,
        )
        return asyncio.run(client.analyze_pair(datapack))

    def _handle_ai_analyze_success(self, response: object) -> None:
        if not isinstance(response, AiResponseEnvelope):
            self._handle_ai_analyze_error("AI response invalid or empty.")
            return
        if response.analysis_result.status == "ERROR":
            self._handle_ai_analyze_error(response.analysis_result.summary_ru or "AI response invalid or empty.")
            return
        self._ai_request_card.hide()
        self._last_ai_response = response
        self._last_ai_analysis = response.analysis_result
        self._last_strategy_patch = response.strategy_patch
        self._last_trade_options = list(response.trade_options)
        self._analysis_summary.setText(self._format_ai_summary(response, response.analysis_result))
        self._set_summary_status(response.analysis_result.status)
        if self._is_zero_fee_symbol():
            self._plan_preview_status.setText("AI анализ завершен. Проверьте варианты сделок ниже.")
        else:
            self._plan_preview_status.setText("AI анализ завершен. Нажмите Apply Plan для заполнения формы.")
        self._plan_applied = False
        self._pending_patch = None
        self._chat_apply_button.setEnabled(False)
        suggested_strategy = None
        if response.strategy_patch is not None:
            suggested_strategy = response.strategy_patch.parameters_patch.get("strategy_id")
        self._ai_block_confirm = (
            response.analysis_result.status in {"WARN", "DANGER", "ERROR"}
            or suggested_strategy == "DO_NOT_TRADE"
        )
        self._render_trade_options(response.trade_options)
        if self._ai_block_confirm:
            self._append_chat("AI", "AI отметил риски; подтверждение торговли заблокировано до повторного анализа.")
            self._log_event("[AI] Confirm Start blocked due to AI status.")
        self._set_state(BotState.PLAN_READY, reason="analysis_complete")
        self._log_event("AI analyze ok")
        self._log_event(
            "[AI] status=%s confidence=%s",
            response.analysis_result.status,
            response.analysis_result.confidence,
        )
        status_bits = [f"Статус: {response.analysis_result.status}"]
        if response.analysis_result.confidence is not None:
            status_bits.append(f"уверенность={response.analysis_result.confidence:.2f}")
        if response.analysis_result.reason_codes:
            status_bits.append(f"причины={', '.join(response.analysis_result.reason_codes)}")
        self._append_chat("AI", "Анализ завершен. Нажмите Apply Plan для заполнения формы.")
        self._append_chat("AI", " | ".join(status_bits))
        if response.analysis_result.summary_ru:
            self._append_chat("AI", response.analysis_result.summary_ru)
        if response.action_suggestions:
            action_details = [
                suggestion.note_ru or suggestion.type
                for suggestion in response.action_suggestions
                if suggestion.type
            ]
            self._render_ai_actions(action_details)
        self._render_preview_from_ai(response.strategy_patch)

    def _handle_ai_analyze_error(self, message: str) -> None:
        self._log_event(f"AI analyze failed: {message}")
        self._append_chat("AI", f"Ошибка AI: {message}")
        self._analysis_summary.setText("AI анализ не удался. Проверьте логи.")
        self._set_summary_status("ERROR")
        self._ai_block_confirm = True
        self._pending_patch = None
        self._last_ai_analysis = None
        self._last_strategy_patch = None
        self._last_trade_options = []
        self._render_trade_options([])
        self._chat_apply_button.setEnabled(False)
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
            field.setProperty("autoFilled", True)
            self._mark_field_state(field_key, "ai")
        else:
            field.setProperty("autoFilled", False)

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
        self._strategy_fields[field_key].setProperty("autoFilled", False)
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
        if self._last_strategy_patch is None:
            QMessageBox.warning(self, "Нет плана AI", "Сначала запустите AI Analyze.")
            self._log_event("Apply plan skipped: missing AI response.")
            return
        self._apply_strategy_patch(self._last_strategy_patch, origin="ai")
        self._reset_ai_button.setEnabled(True)
        self._plan_applied = True
        self._set_state(BotState.PLAN_READY, reason="apply_plan")
        self._sync_workspace_orders()
        self._log_event("[AI] Plan applied -> strategy populated")

    def _confirm_start(self) -> None:
        if self._ai_block_confirm:
            QMessageBox.warning(
                self,
                "AI блокировка",
                "AI отметил этот план как рискованный. Перезапустите анализ, чтобы продолжить.",
            )
            self._log_event("Confirm start blocked by AI status.")
            return
        if self._pair_state.state == BotState.PAUSED:
            self._tick_timer.start()
            self._set_state(BotState.RUNNING, reason="resume")
            self._log_event("RUNNING (resume)")
            return
        self._tick_count = 0
        self._tick_timer.start()
        self._runtime_started_at = datetime.now(timezone.utc)
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
        self._ai_block_confirm = False
        self._runtime_started_at = None
        self._update_workspace_plan([], source="none")
        self._render_trade_options([])
        self._analysis_summary.setText(self._idle_summary())
        self._set_summary_status("OK")
        self._set_state(BotState.IDLE, reason="stop")
        self._log_event("Execution stopped.")

    def _log_tick(self) -> None:
        self._tick_count += 1
        self._log_event(f"tick {self._tick_count}")

    def _send_chat(self) -> None:
        message = self._chat_input.text().strip()
        if not message:
            return
        budget_value = self._extract_budget_value(message)
        if budget_value is not None:
            self._set_field_value("budget", budget_value, origin="user")
            self._mark_field_state("budget", "user")
            self._append_chat(
                "AI",
                f"Локальное изменение: бюджет установлен на {budget_value:.2f} USDT.",
            )
            self._log_event(f"[INTENT] budget -> {budget_value:.2f} USDT")
            self._chat_input.clear()
            return
        if not self._app_state.openai_key_present:
            self._append_chat("AI", "Не задан ключ OpenAI. Укажите его в Settings.")
            self._log_event("AI chat skipped: missing OpenAI key.")
            return
        if self._last_ai_analysis is None or self._last_datapack is None:
            self._append_chat("AI", "Сначала запустите AI Analyze перед запросом изменений.")
            self._log_event("AI chat skipped: missing AI response.")
            return
        self._append_chat("User", message)
        self._chat_input.clear()
        self._pending_patch = None
        self._chat_apply_button.setEnabled(False)
        worker = _AiWorker(lambda: self._run_ai_chat_adjust(message))
        worker.signals.success.connect(self._handle_ai_chat_adjust_success)
        worker.signals.error.connect(self._handle_ai_chat_adjust_error)
        self._thread_pool.start(worker)

    def _run_ai_chat_adjust(self, message: str) -> AiResponseEnvelope:
        if self._last_datapack is None or self._last_ai_analysis is None:
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
                last_plan=self._collect_current_strategy(),
                user_message=message,
            )
        )

    def _handle_ai_chat_adjust_success(self, response: object) -> None:
        if not isinstance(response, AiResponseEnvelope):
            self._handle_ai_chat_adjust_error("AI patch response invalid or empty.")
            return
        if response.analysis_result.status == "ERROR" or response.strategy_patch is None:
            self._handle_ai_chat_adjust_error(
                response.analysis_result.summary_ru or "AI patch response invalid or empty."
            )
            return
        self._pending_patch = response.strategy_patch
        self._last_strategy_patch = response.strategy_patch
        self._chat_apply_button.setEnabled(True)
        message = response.analysis_result.summary_ru or "Патч готов к применению."
        self._append_chat("AI", f"Патч AI готов: {message}")
        if response.action_suggestions:
            action_details = [
                suggestion.note_ru or suggestion.type
                for suggestion in response.action_suggestions
                if suggestion.type
            ]
            self._render_ai_actions(action_details)

    def _handle_ai_chat_adjust_error(self, message: str) -> None:
        self._append_chat("AI", f"Ошибка AI при изменении: {message}")
        self._log_event(f"AI chat adjust failed: {message}")
        self._pending_patch = None
        self._chat_apply_button.setEnabled(False)

    def _apply_pending_patch(self) -> None:
        if self._pending_patch is None:
            self._append_chat("AI", "Нет патча для применения.")
            return
        reply = QMessageBox.question(
            self,
            "Подтверждение",
            "Применить предложенный AI патч?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        self._apply_strategy_patch(self._pending_patch)
        self._pending_patch = None
        self._chat_apply_button.setEnabled(False)
        self._append_chat("AI", "Патч применен к форме стратегии.")
        self.update_controls_by_state()

    def _open_trading_workspace(self) -> None:
        from src.gui.trading_workspace_window import TradingWorkspaceWindow

        window = TradingWorkspaceWindow(
            pair_workspace=self,
            pair_state=self._workspace_state,
            app_state=self._app_state,
            parent=self,
        )
        window.show()
        self._trading_windows.append(window)

    def apply_strategy_patch_from_observer(self, patch: AiStrategyPatch) -> None:
        self._apply_strategy_patch(patch, origin="ai")
        self._pending_patch = None
        self._chat_apply_button.setEnabled(False)
        self._log_event("[AI Observer] Patch applied from Trading Workspace")

    def get_trading_snapshot(self) -> dict[str, Any]:
        datapack_summary = None
        if self._last_datapack:
            datapack_summary = {
                "period": self._last_datapack.get("user_context", {}).get("selected_period"),
                "quality": self._last_datapack.get("user_context", {}).get("quality"),
                "last_price": self._last_datapack.get("market", {}).get("last_price"),
            }
        uptime_sec = None
        if self._runtime_started_at:
            uptime_sec = int((datetime.now(timezone.utc) - self._runtime_started_at).total_seconds())
        position = {
            "status": self._workspace_state.position_status,
            "qty": self._workspace_state.position_qty,
            "entry_price": self._workspace_state.entry_price,
            "realized_pnl": self._workspace_state.realized_pnl,
            "unrealized_pnl": self._workspace_state.unrealized_pnl,
        }
        pnl_total = self._workspace_state.realized_pnl + self._workspace_state.unrealized_pnl
        return {
            "symbol": self.symbol,
            "dry_run": self._topbar.dry_run_toggle.isChecked(),
            "state": self._pair_state.state.value,
            "last_reason": self._pair_state.last_reason,
            "datapack_summary": datapack_summary,
            "open_orders": list(self._workspace_state.open_orders),
            "plan_levels": list(self._workspace_state.plan_levels),
            "plan_source": self._workspace_state.plan_source,
            "position": position,
            "pnl": {
                "total": pnl_total,
                "realized": self._workspace_state.realized_pnl,
                "unrealized": self._workspace_state.unrealized_pnl,
            },
            "recent_fills": list(self._workspace_state.recent_fills),
            "risk": {
                "hard_stop_pct": self._hard_stop_input.value(),
                "cooldown_minutes": self._cooldown_input.value(),
                "volatility_mode": self._volatility_mode_input.currentText(),
            },
            "recheck_interval_sec": self._recheck_interval_input.value(),
            "runtime": {
                "uptime_sec": uptime_sec,
                "last_price_age_ms": self._latest_price_age_ms,
            },
        }

    def build_monitor_datapack(self) -> dict[str, Any]:
        return self._build_monitor_datapack()

    def _extract_preview_orders(self) -> list[dict[str, str]]:
        return list(self._workspace_state.open_orders)

    def _append_chat(self, sender: str, message: str) -> None:
        self._chat_history.append(f"{sender}: {message}")

    def _handle_price_update(self, symbol: str, price: float, source: str, age_ms: int) -> None:
        if symbol != self.symbol:
            return
        self._latest_price = price
        self._latest_price_source = source
        self._latest_price_age_ms = age_ms
        self._topbar.price_label.setText(f"Price: {price:.6f} ({source}, {age_ms} ms)")
        if self._workspace_state.position_qty:
            self._workspace_state.unrealized_pnl = (
                (price - self._workspace_state.entry_price) * self._workspace_state.position_qty
            )

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
            ((state == BotState.PLAN_READY and self._plan_applied and not self._ai_block_confirm))
            or state == BotState.PAUSED
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
        demo_levels = [
            {
                "side": str(level["side"]),
                "price": str(level["price"]),
                "qty": str(level["qty"]),
                "pct_from_mid": str(level["pct_from_mid"]),
            }
            for level in levels
        ]
        self._update_workspace_plan(demo_levels, source="demo", demo_source_symbol=self.symbol)

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
        period = self._prepared_period or self._topbar.period_combo.currentText()
        quality = self._topbar.quality_combo.currentText()
        now_price = self._latest_price if self._latest_price is not None else self._prepared_mid_price
        source = self._latest_price_source or "None"
        age_ms = self._latest_price_age_ms
        base_asset, quote_asset = self._split_symbol(self.symbol)
        spread_estimate = self._market_context.spread if self._market_context else 0.2
        volatility_proxy = 0.0
        if self._prepared_mid_price and now_price:
            try:
                volatility_proxy = (now_price - self._prepared_mid_price) / self._prepared_mid_price * 100
            except ZeroDivisionError:
                volatility_proxy = 0.0
        bid = None
        ask = None
        if now_price is not None:
            bid = now_price * (1 - spread_estimate / 200)
            ask = now_price * (1 + spread_estimate / 200)
        ranges: dict[str, dict[str, float]] = {}
        base_vol = abs(volatility_proxy) if volatility_proxy is not None else 0.2
        if now_price:
            for range_period, scale in (("1m", 0.25), ("5m", 0.45), ("1h", 0.75), ("24h", 1.4)):
                pct = round(base_vol * scale + 0.15, 3)
                low = round(now_price * (1 - pct / 100), 6)
                high = round(now_price * (1 + pct / 100), 6)
                ranges[range_period] = {"low": low, "high": high, "pct": pct}
        quote_volume_24h = None
        base_volume_24h = None
        trade_count_24h = None
        if self._liquidity_stats:
            quote_volume_24h = self._liquidity_stats.get("quote_volume")
            base_volume_24h = self._liquidity_stats.get("volume")
            trade_count_24h = self._liquidity_stats.get("trade_count")
        liquidity_value: float | str = "unknown"
        if quote_volume_24h is not None:
            liquidity_value = float(quote_volume_24h)
        analysis_mode = "ZERO_FEE_CONVERT" if self._is_zero_fee_symbol() else "GRID_RANGE"
        return {
            "version": "v2.1.2",
            "symbol": self.symbol,
            "analysis_mode": analysis_mode,
            "market": {
                "last_price": now_price,
                "bid": bid,
                "ask": ask,
                "spread_pct": spread_estimate,
                "ranges": ranges,
                "volatility_est": round(base_vol, 4),
                "liquidity": liquidity_value,
                "source_latency_ms": age_ms,
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            },
            "constraints": {
                "tick_size": None,
                "step_size": None,
                "min_qty": None,
                "min_notional": None,
            },
            "budget": {
                "budget_usdt": self._budget_input.value(),
                "mode": self._normalize_mode(self._mode_input.currentText()),
            },
            "user_context": {
                "budget_usdt": self._budget_input.value(),
                "mode": self._normalize_mode(self._mode_input.currentText()),
                "dry_run": self._topbar.dry_run_toggle.isChecked(),
                "selected_period": period,
                "quality": quality,
                "liquidity_summary": {
                    "base_volume_24h": base_volume_24h,
                    "quote_volume_24h": quote_volume_24h,
                    "trade_count_24h": trade_count_24h,
                },
            },
            "pair_context": {
                "base_asset": base_asset,
                "quote_asset": quote_asset,
                "source": source,
            },
        }

    def _build_monitor_datapack(self) -> dict[str, Any]:
        snapshot = self.get_trading_snapshot()
        runtime = snapshot.get("runtime", {})
        health = {
            "ws_latency_ms": runtime.get("last_price_age_ms"),
            "http_latency_ms": None,
            "errors": self._workspace_state.error_count,
        }
        return {
            "version": "v2.1.2",
            "symbol": self.symbol,
            "analysis_mode": "ZERO_FEE_CONVERT" if self._is_zero_fee_symbol() else "GRID_RANGE",
            "runtime": {
                "state": snapshot["state"],
                "dry_run": snapshot["dry_run"],
                "uptime_sec": runtime.get("uptime_sec"),
                "last_reason": snapshot["last_reason"],
            },
            "open_orders": snapshot["open_orders"],
            "position": snapshot["position"],
            "pnl": snapshot["pnl"],
            "recent_fills": snapshot["recent_fills"],
            "strategy_state": {
                "strategy_id": self._strategy_id_input.currentText(),
                "budget_usdt": self._budget_input.value(),
                "mode": self._normalize_mode(self._mode_input.currentText()),
                "grid_step_pct": self._grid_step_input.value(),
                "range_low_pct": self._range_low_input.value(),
                "range_high_pct": self._range_high_input.value(),
                "bias": {"buy_pct": self._bias_buy_input.value(), "sell_pct": self._bias_sell_input.value()},
                "order_size_mode": self._order_size_mode_input.currentText(),
                "max_orders": self._max_orders_input.value(),
                "max_exposure_pct": self._max_exposure_input.value(),
                "hard_stop_pct": self._hard_stop_input.value(),
                "cooldown_minutes": self._cooldown_input.value(),
                "recheck_interval_sec": self._recheck_interval_input.value(),
                "ai_reanalyze_interval_sec": self._ai_reanalyze_interval_input.value(),
                "min_change_to_rebuild_pct": self._min_change_rebuild_input.value(),
                "volatility_mode": self._volatility_mode_input.currentText(),
                "plan_applied": self._plan_applied,
                "plan_levels": snapshot["plan_levels"],
            },
            "health": health,
        }
