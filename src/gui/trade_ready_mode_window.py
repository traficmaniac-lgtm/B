from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.core.logging import get_logger
from src.gui.trading_runtime_window import TradingRuntimeWindow
from src.services.price_feed_manager import PriceFeedManager, PriceUpdate, WS_CONNECTED, WS_DEGRADED, WS_LOST


class TradeReadyState(str, Enum):
    IDLE = "IDLE"
    AI_ANALYZING = "AI_ANALYZING"
    REPORT_READY = "REPORT_READY"
    PLAN_READY = "PLAN_READY"


@dataclass(frozen=True)
class TradeVariant:
    title: str
    expected_profit_pct: float
    expected_profit_usdt: float
    risk_level: str
    strategy: dict[str, Any]


class _TradeReadySignals(QObject):
    price_tick = Signal(object)
    ws_status = Signal(str)


class TradeReadyModeWindow(QMainWindow):
    def __init__(
        self,
        symbol: str,
        exchange: str,
        last_price: str | None = None,
        market_state: object | None = None,
        price_feed_manager: PriceFeedManager | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._symbol = symbol
        self._exchange = exchange
        self._last_price = last_price
        self._market_state = market_state
        self._price_feed_manager = price_feed_manager
        self._state = TradeReadyState.IDLE
        self._analysis_timer: QTimer | None = None
        self._latest_ai_report: dict[str, Any] | None = None
        self._variants: list[TradeVariant] = []
        self._selected_variant: TradeVariant | None = None
        self._runtime_windows: list[TradingRuntimeWindow] = []
        self._logger = get_logger(f"gui.trade_ready.{symbol.lower()}")
        self._price_signals = _TradeReadySignals()
        self._price_signals.price_tick.connect(self._handle_price_tick)
        self._price_signals.ws_status.connect(self._handle_ws_status)

        self.setWindowTitle(f"Trade Ready Mode — {symbol}")
        self.resize(1280, 860)

        central = QWidget()
        layout = QVBoxLayout()
        layout.addWidget(self._build_header())
        layout.addWidget(self._build_main_splitter())
        central.setLayout(layout)
        self.setCentralWidget(central)

        self._update_market_context()
        self._apply_state(TradeReadyState.IDLE)
        self._start_price_feed()

    def _build_header(self) -> QWidget:
        header = QWidget()
        layout = QHBoxLayout()

        self._symbol_label = QLabel(f"{self._symbol}")
        self._symbol_label.setStyleSheet("font-weight: 600; font-size: 16px;")
        layout.addWidget(self._symbol_label)

        self._exchange_label = QLabel(f"Exchange: {self._exchange}")
        layout.addWidget(self._exchange_label)

        self._last_price_label = QLabel(self._format_price_label())
        layout.addWidget(self._last_price_label)

        self._source_label = QLabel("Источник: —")
        layout.addWidget(self._source_label)

        self._latency_label = QLabel("Latency: —")
        layout.addWidget(self._latency_label)

        self._age_label = QLabel("Age: —")
        layout.addWidget(self._age_label)

        self._ws_status_label = QLabel("WS: LOST")
        layout.addWidget(self._ws_status_label)

        self._mode_badge = QLabel("Trade Ready Mode")
        self._mode_badge.setStyleSheet(
            "padding: 4px 10px; border-radius: 10px; background: #111827; color: #f9fafb;"
        )
        layout.addWidget(self._mode_badge)

        layout.addStretch()

        self._status_label = QLabel()
        self._status_label.setStyleSheet(
            "padding: 4px 10px; border-radius: 10px; background: #e5e7eb; color: #111827;"
        )
        layout.addWidget(QLabel("Status:"))
        layout.addWidget(self._status_label)

        header.setLayout(layout)
        return header

    def _start_price_feed(self) -> None:
        if not self._price_feed_manager:
            return
        self._price_feed_manager.register_symbol(self._symbol)
        self._price_feed_manager.subscribe(self._symbol, self._emit_price_tick)
        self._price_feed_manager.subscribe_status(self._symbol, self._emit_ws_status)
        self._price_feed_manager.start()

    def _emit_price_tick(self, update: PriceUpdate) -> None:
        self._price_signals.price_tick.emit(update)

    def _emit_ws_status(self, status: str, _: str) -> None:
        self._price_signals.ws_status.emit(status)

    def _handle_price_tick(self, update: PriceUpdate) -> None:
        if update.last_price is None:
            return
        self._last_price = f"{update.last_price:.6f}"
        self._last_price_label.setText(self._format_price_label())
        self._source_label.setText(f"Источник: {update.source}")
        latency = "—" if update.latency_ms is None else f"{update.latency_ms} ms"
        age = "—" if update.price_age_ms is None else f"{update.price_age_ms} ms"
        self._latency_label.setText(f"Latency: {latency}")
        self._age_label.setText(f"Age: {age}")

    def _handle_ws_status(self, status: str) -> None:
        label = status.replace("WS_", "")
        if status == WS_CONNECTED:
            self._ws_status_label.setText(f"WS: {label}")
            self._ws_status_label.setStyleSheet("color: #16a34a;")
            return
        if status == WS_DEGRADED:
            self._ws_status_label.setText(f"WS: {label}")
            self._ws_status_label.setStyleSheet("color: #f59e0b;")
            return
        if status == WS_LOST:
            self._ws_status_label.setText(f"WS: {label}")
            self._ws_status_label.setStyleSheet("color: #dc2626;")

    def _build_main_splitter(self) -> QSplitter:
        splitter = QSplitter(Qt.Vertical)
        splitter.setChildrenCollapsible(False)

        top_splitter = QSplitter(Qt.Horizontal)
        top_splitter.setChildrenCollapsible(False)

        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setWidget(self._build_left_column())

        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setWidget(self._build_right_column())

        top_splitter.addWidget(left_scroll)
        top_splitter.addWidget(right_scroll)
        top_splitter.setStretchFactor(0, 1)
        top_splitter.setStretchFactor(1, 1)

        bottom_scroll = QScrollArea()
        bottom_scroll.setWidgetResizable(True)
        bottom_scroll.setWidget(self._build_bottom_zone())

        splitter.addWidget(top_splitter)
        splitter.addWidget(bottom_scroll)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 2)
        return splitter

    def _build_left_column(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout()
        layout.addWidget(self._build_market_context())
        layout.addWidget(self._build_analysis_controls())
        layout.addWidget(self._build_strategy_form())
        layout.addStretch()
        container.setLayout(layout)
        return container

    def _build_right_column(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout()
        layout.addWidget(self._build_ai_summary())
        layout.addWidget(self._build_trade_variants())
        layout.addWidget(self._build_ai_chat())
        layout.addStretch()
        container.setLayout(layout)
        return container

    def _build_bottom_zone(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout()

        history_group = QGroupBox("История AI-событий")
        history_layout = QVBoxLayout()
        self._history_list = QListWidget()
        history_layout.addWidget(self._history_list)
        history_group.setLayout(history_layout)
        layout.addWidget(history_group)

        raw_group = QGroupBox("Raw AI JSON")
        raw_layout = QVBoxLayout()
        self._raw_toggle = QCheckBox("Показать raw JSON ответа AI")
        self._raw_toggle.stateChanged.connect(self._toggle_raw_json)
        raw_layout.addWidget(self._raw_toggle)
        self._raw_json = QTextEdit()
        self._raw_json.setReadOnly(True)
        self._raw_json.setVisible(False)
        raw_layout.addWidget(self._raw_json)
        raw_group.setLayout(raw_layout)
        layout.addWidget(raw_group)

        container.setLayout(layout)
        return container

    def _build_market_context(self) -> QWidget:
        group = QGroupBox("Контекст рынка (read-only)")
        form = QFormLayout()

        self._price_value = QLabel("--")
        self._range_value = QLabel("— (placeholder)")
        self._volatility_value = QLabel("— (placeholder)")
        self._spread_value = QLabel("— (placeholder)")
        self._fee_value = QLabel("Стандартная комиссия")

        form.addRow(QLabel("Текущая цена"), self._price_value)
        form.addRow(QLabel("Диапазон"), self._range_value)
        form.addRow(QLabel("Волатильность"), self._volatility_value)
        form.addRow(QLabel("Спред"), self._spread_value)
        form.addRow(QLabel("Комиссия"), self._fee_value)

        group.setLayout(form)
        return group

    def _build_analysis_controls(self) -> QWidget:
        group = QGroupBox("Параметры анализа")
        form = QFormLayout()

        self._period_combo = QComboBox()
        self._period_combo.addItems(["1d", "3d", "7d", "14d", "30d"])
        self._period_combo.setCurrentText("7d")

        self._depth_combo = QComboBox()
        self._depth_combo.addItems(["Normal", "Deep"])

        self._monitoring_combo = QComboBox()
        self._monitoring_combo.addItems(["10s", "30s", "1m", "5m"])

        form.addRow(QLabel("Period"), self._period_combo)
        form.addRow(QLabel("Analysis depth"), self._depth_combo)
        form.addRow(QLabel("Monitoring interval"), self._monitoring_combo)

        self._analyze_button = QPushButton("▶ ИИ, работай")
        self._analyze_button.clicked.connect(self._start_analysis)

        wrapper = QWidget()
        wrapper_layout = QVBoxLayout()
        wrapper_layout.addLayout(form)
        wrapper_layout.addWidget(self._analyze_button)
        wrapper.setLayout(wrapper_layout)

        group_layout = QVBoxLayout()
        group_layout.addWidget(wrapper)
        group.setLayout(group_layout)
        return group

    def _build_strategy_form(self) -> QWidget:
        group = QGroupBox("Форма стратегии (после принятия)")
        form = QFormLayout()

        self._strategy_type = QComboBox()
        self._strategy_type.addItems(["Grid", "Convert", "Range", "Auto"])

        self._runtime_mode = QComboBox()
        self._runtime_mode.addItems(["DRY-RUN", "LIVE"])
        self._runtime_mode.setCurrentText("DRY-RUN")

        self._budget_input = QDoubleSpinBox()
        self._budget_input.setRange(10.0, 1_000_000.0)
        self._budget_input.setDecimals(2)

        self._grid_step_input = QDoubleSpinBox()
        self._grid_step_input.setRange(0.1, 10.0)
        self._grid_step_input.setDecimals(2)
        self._grid_step_input.setSuffix(" %")

        self._range_low_input = QDoubleSpinBox()
        self._range_low_input.setRange(0.1, 50.0)
        self._range_low_input.setDecimals(2)
        self._range_low_input.setSuffix(" %")

        self._range_high_input = QDoubleSpinBox()
        self._range_high_input.setRange(0.1, 50.0)
        self._range_high_input.setDecimals(2)
        self._range_high_input.setSuffix(" %")

        self._risk_limit_input = QDoubleSpinBox()
        self._risk_limit_input.setRange(0.1, 5.0)
        self._risk_limit_input.setDecimals(2)
        self._risk_limit_input.setSuffix(" x")

        form.addRow(QLabel("Strategy type"), self._strategy_type)
        form.addRow(QLabel("Runtime mode"), self._runtime_mode)
        form.addRow(QLabel("Budget"), self._budget_input)
        form.addRow(QLabel("Grid step"), self._grid_step_input)
        form.addRow(QLabel("Range low / high"), self._range_low_input)
        form.addRow(QLabel(""), self._range_high_input)
        form.addRow(QLabel("Risk limit"), self._risk_limit_input)

        self._open_runtime_button = QPushButton("Перейти к торговле (Runtime)")
        self._open_runtime_button.clicked.connect(self._open_runtime_window)

        group_layout = QVBoxLayout()
        group_layout.addLayout(form)
        group_layout.addWidget(self._open_runtime_button)
        group.setLayout(group_layout)
        group.setEnabled(False)
        self._strategy_group = group
        return group

    def _build_ai_summary(self) -> QWidget:
        group = QGroupBox("AI Analysis Summary")
        layout = QVBoxLayout()

        self._summary_status = QLabel("Статус: —")
        self._summary_confidence = QLabel("Confidence: —")
        self._summary_text = QLabel("Проведите анализ для получения отчёта.")
        self._summary_text.setWordWrap(True)
        self._summary_reasons = QListWidget()

        layout.addWidget(self._summary_status)
        layout.addWidget(self._summary_confidence)
        layout.addWidget(self._summary_text)
        layout.addWidget(QLabel("Причины:"))
        layout.addWidget(self._summary_reasons)
        group.setLayout(layout)
        return group

    def _build_trade_variants(self) -> QWidget:
        group = QGroupBox("AI Trade Variants")
        layout = QVBoxLayout()
        self._variants_placeholder = QLabel("AI ещё не подготовил варианты.")
        self._variants_placeholder.setStyleSheet("color: #6b7280;")
        layout.addWidget(self._variants_placeholder)
        self._variants_container = QVBoxLayout()
        layout.addLayout(self._variants_container)
        layout.addStretch()
        group.setLayout(layout)
        return group

    def _build_ai_chat(self) -> QWidget:
        group = QGroupBox("AI Chat")
        layout = QVBoxLayout()

        self._chat_history = QTextEdit()
        self._chat_history.setReadOnly(True)
        self._chat_input = QLineEdit()
        self._chat_input.setPlaceholderText("Например: Хочу быстрее, но с риском")
        self._chat_send = QPushButton("Send")
        self._chat_send.clicked.connect(self._send_chat_message)

        layout.addWidget(self._chat_history)
        layout.addWidget(self._chat_input)
        layout.addWidget(self._chat_send)
        group.setLayout(layout)
        return group

    def _format_price_label(self) -> str:
        if not self._last_price or str(self._last_price).strip() in {"--", ""}:
            return "Last price: --"
        return f"Last price: {self._last_price}"

    def _update_market_context(self) -> None:
        self._price_value.setText(self._last_price or "--")
        if self._has_zero_fee():
            self._fee_value.setText("0% fee")

    def _has_zero_fee(self) -> bool:
        if self._market_state is None:
            return False
        zero_fee = getattr(self._market_state, "zero_fee_symbols", None)
        if zero_fee is None:
            return False
        return self._symbol in zero_fee

    def _toggle_raw_json(self) -> None:
        self._raw_json.setVisible(self._raw_toggle.isChecked())

    def _apply_state(self, state: TradeReadyState) -> None:
        self._state = state
        self._status_label.setText(state.value)
        palette = {
            TradeReadyState.IDLE: ("#e5e7eb", "#111827"),
            TradeReadyState.AI_ANALYZING: ("#fde68a", "#92400e"),
            TradeReadyState.REPORT_READY: ("#bbf7d0", "#166534"),
            TradeReadyState.PLAN_READY: ("#c7d2fe", "#3730a3"),
        }
        bg, fg = palette[state]
        self._status_label.setStyleSheet(
            f"padding: 4px 10px; border-radius: 10px; background: {bg}; color: {fg};"
        )

        is_analyzing = state == TradeReadyState.AI_ANALYZING
        self._analyze_button.setEnabled(not is_analyzing)
        self._chat_send.setEnabled(not is_analyzing)
        self._update_variant_buttons_enabled(state == TradeReadyState.REPORT_READY)
        self._open_runtime_button.setEnabled(state == TradeReadyState.PLAN_READY)
        if state == TradeReadyState.IDLE:
            self._strategy_group.setEnabled(False)

    def _update_variant_buttons_enabled(self, enabled: bool) -> None:
        for idx in range(self._variants_container.count()):
            item = self._variants_container.itemAt(idx)
            if not item or not item.widget():
                continue
            widget = item.widget()
            for button in widget.findChildren(QPushButton):
                button.setEnabled(enabled)

    def _log_event(self, message: str) -> None:
        self._history_list.addItem(QListWidgetItem(message))
        self._history_list.scrollToBottom()

    def _start_analysis(self) -> None:
        datapack = {
            "symbol": self._symbol,
            "exchange": self._exchange,
            "period": self._period_combo.currentText(),
            "depth": self._depth_combo.currentText(),
            "monitoring_interval": self._monitoring_combo.currentText(),
            "last_price": self._last_price,
        }
        self._log_event("Анализ запущен: сформирован datapack.")
        self._apply_state(TradeReadyState.AI_ANALYZING)
        self._summary_text.setText("AI анализирует рынок...")
        self._summary_reasons.clear()
        self._variants = []
        self._clear_variants()
        self._raw_json.setPlainText("")

        if self._analysis_timer:
            self._analysis_timer.stop()
        self._analysis_timer = QTimer(self)
        self._analysis_timer.setSingleShot(True)
        self._analysis_timer.timeout.connect(lambda: self._finish_analysis(datapack))
        self._analysis_timer.start(900)

    def _finish_analysis(self, datapack: dict[str, Any]) -> None:
        report = self._mock_ai_report(datapack)
        self._latest_ai_report = report
        self._raw_json.setPlainText(self._format_json(report))
        self._render_summary(report)
        self._render_variants(report.get("variants", []))
        self._apply_state(TradeReadyState.REPORT_READY)
        self._log_event("Отчёт AI получен.")

    def _mock_ai_report(self, datapack: dict[str, Any]) -> dict[str, Any]:
        depth = datapack.get("depth")
        confidence = 0.72 if depth == "Normal" else 0.81
        summary = (
            "Рынок выглядит стабильным, наблюдается аккуратное восстановление после просадки."
            if depth == "Normal"
            else "Глубокий анализ подтверждает баланс спроса и предложения, риски контролируемы."
        )
        return {
            "status": "SAFE",
            "confidence": confidence,
            "summary": summary,
            "reasons": [
                "Спред остаётся в допустимом диапазоне.",
                "Волатильность умеренная, без резких всплесков.",
                "Ликвидность позволяет работать сеткой.",
            ],
            "variants": [
                {
                    "title": "Консервативная сетка",
                    "expected_profit_pct": 1.2,
                    "expected_profit_usdt": 18.5,
                    "risk_level": "низкий",
                    "strategy": {
                        "strategy_type": "Grid",
                        "budget": 800.0,
                        "grid_step_pct": 0.4,
                        "range_low_pct": 1.2,
                        "range_high_pct": 2.2,
                        "risk_limit": 1.2,
                    },
                },
                {
                    "title": "Быстрый диапазон",
                    "expected_profit_pct": 2.4,
                    "expected_profit_usdt": 42.0,
                    "risk_level": "средний",
                    "strategy": {
                        "strategy_type": "Range",
                        "budget": 1000.0,
                        "grid_step_pct": 0.6,
                        "range_low_pct": 1.6,
                        "range_high_pct": 3.0,
                        "risk_limit": 1.6,
                    },
                },
            ],
        }

    def _render_summary(self, report: dict[str, Any]) -> None:
        self._summary_status.setText(f"Статус: {report.get('status', '—')}")
        confidence = report.get("confidence")
        confidence_text = f"{confidence:.2f}" if isinstance(confidence, (float, int)) else "—"
        self._summary_confidence.setText(f"Confidence: {confidence_text}")
        self._summary_text.setText(report.get("summary", "—"))
        self._summary_reasons.clear()
        for reason in report.get("reasons", []):
            self._summary_reasons.addItem(str(reason))

    def _clear_variants(self) -> None:
        while self._variants_container.count():
            item = self._variants_container.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()
        self._variants_placeholder.setVisible(True)

    def _render_variants(self, variants: list[dict[str, Any]]) -> None:
        self._variants = [
            TradeVariant(
                title=variant["title"],
                expected_profit_pct=variant["expected_profit_pct"],
                expected_profit_usdt=variant["expected_profit_usdt"],
                risk_level=variant["risk_level"],
                strategy=variant["strategy"],
            )
            for variant in variants
        ]
        self._clear_variants()
        if not self._variants:
            return
        self._variants_placeholder.setVisible(False)
        for variant in self._variants:
            card = self._build_variant_card(variant)
            self._variants_container.addWidget(card)

    def _build_variant_card(self, variant: TradeVariant) -> QWidget:
        card = QFrame()
        card.setFrameShape(QFrame.StyledPanel)
        card_layout = QVBoxLayout()

        title = QLabel(variant.title)
        title.setStyleSheet("font-weight: 600; font-size: 13px;")
        card_layout.addWidget(title)
        card_layout.addWidget(
            QLabel(
                f"Ожидаемый профит: {variant.expected_profit_pct:.2f}% (~{variant.expected_profit_usdt:.2f} USDT)"
            )
        )
        card_layout.addWidget(QLabel(f"Риск: {variant.risk_level}"))

        buttons = QHBoxLayout()
        buttons.addStretch()
        accept_btn = QPushButton("Принять")
        decline_btn = QPushButton("Отклонить")
        accept_btn.clicked.connect(lambda: self._accept_variant(variant))
        decline_btn.clicked.connect(lambda: self._decline_variant(variant, card))
        buttons.addWidget(accept_btn)
        buttons.addWidget(decline_btn)
        card_layout.addLayout(buttons)

        card.setLayout(card_layout)
        return card

    def _accept_variant(self, variant: TradeVariant) -> None:
        if self._state != TradeReadyState.REPORT_READY:
            return
        self._selected_variant = variant
        self._fill_strategy_form(variant.strategy)
        self._apply_state(TradeReadyState.PLAN_READY)
        self._log_event(f"Вариант принят: {variant.title}.")

    def _decline_variant(self, variant: TradeVariant, card: QWidget) -> None:
        if self._state != TradeReadyState.REPORT_READY:
            return
        for button in card.findChildren(QPushButton):
            button.setEnabled(False)
        card.setStyleSheet("background: #f3f4f6; color: #6b7280;")
        self._log_event(f"Вариант отклонён: {variant.title}.")

    def _fill_strategy_form(self, strategy: dict[str, Any]) -> None:
        self._strategy_group.setEnabled(True)
        self._strategy_type.setCurrentText(strategy.get("strategy_type", "Grid"))
        self._budget_input.setValue(float(strategy.get("budget", 0.0)))
        self._grid_step_input.setValue(float(strategy.get("grid_step_pct", 0.0)))
        self._range_low_input.setValue(float(strategy.get("range_low_pct", 0.0)))
        self._range_high_input.setValue(float(strategy.get("range_high_pct", 0.0)))
        self._risk_limit_input.setValue(float(strategy.get("risk_limit", 1.0)))

    def _open_runtime_window(self) -> None:
        if self._state != TradeReadyState.PLAN_READY:
            return
        snapshot = self._build_strategy_snapshot()
        mode = self._runtime_mode.currentText()
        runtime = TradingRuntimeWindow(
            symbol=self._symbol,
            exchange=self._exchange,
            strategy_snapshot=snapshot,
            mode=mode,
            trade_ready_window=self,
            price_feed_manager=self._price_feed_manager,
            parent=self.parentWidget(),
        )
        runtime.show()
        self._runtime_windows.append(runtime)
        self._log_event("Открыто окно Trading Runtime.")
        self._logger.info("Opened Trading Runtime window", extra={"symbol": self._symbol})

    def _build_strategy_snapshot(self) -> dict[str, Any]:
        range_low = self._range_low_input.value()
        range_high = self._range_high_input.value()
        range_text = f"{range_low:.2f}% / {range_high:.2f}%"
        strategy_id = self._selected_variant.title if self._selected_variant else "AUTO-STRATEGY"
        return {
            "strategy_id": strategy_id,
            "strategy_type": self._strategy_type.currentText(),
            "budget": self._budget_input.value(),
            "grid_step_pct": self._grid_step_input.value(),
            "range_low_pct": self._range_low_input.value(),
            "range_high_pct": self._range_high_input.value(),
            "grid_step": f"{self._grid_step_input.value():.2f}%",
            "range": range_text,
            "risk_limit": f"{self._risk_limit_input.value():.2f}x",
        }

    def _send_chat_message(self) -> None:
        message = self._chat_input.text().strip()
        if not message:
            return
        self._chat_input.clear()
        self._append_chat("Вы", message)
        response = self._mock_chat_response(message)
        self._append_chat("AI", response)
        self._log_event("Корректировка через чат: получено сообщение AI.")

    def _append_chat(self, speaker: str, message: str) -> None:
        self._chat_history.append(f"<b>{speaker}:</b> {message}")

    def _mock_chat_response(self, message: str) -> str:
        lowered = message.lower()
        if "минимальный" in lowered or "спред" in lowered:
            return (
                "Понял. Могу предложить снизить шаг сетки и уменьшить риск, "
                "но итоговое решение остаётся за вами."
            )
        if "быстр" in lowered and "риск" in lowered:
            return (
                "Если нужен быстрый результат с риском, можно расширить диапазон и повысить лимит риска. "
                "Скажите, подтвердить эти параметры?"
            )
        if "безопасн" in lowered:
            return "Усилю консервативные настройки и уменьшу риск. Сообщите, если хотите принять изменения."
        return "Записал пожелание. Могу скорректировать стратегию после вашего подтверждения."

    def _format_json(self, report: dict[str, Any]) -> str:
        try:
            import json

            return json.dumps(report, ensure_ascii=False, indent=2)
        except Exception:
            return str(report)

    def closeEvent(self, event: object) -> None:  # noqa: N802
        if self._analysis_timer:
            self._analysis_timer.stop()
        if self._price_feed_manager is not None:
            self._price_feed_manager.unsubscribe(self._symbol, self._emit_price_tick)
            self._price_feed_manager.unsubscribe_status(self._symbol, self._emit_ws_status)
            self._price_feed_manager.unregister_symbol(self._symbol)
        super().closeEvent(event)
