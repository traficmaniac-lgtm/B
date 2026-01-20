from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from queue import Queue
from typing import Any

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.core.logging import get_logger
from src.runtime.engine import PnLSnapshot, RuntimeEngine
from src.runtime.runtime_state import RuntimeState
from src.services.price_feed_manager import PriceFeedManager, PriceUpdate, WS_CONNECTED, WS_DEGRADED, WS_LOST


@dataclass(frozen=True)
class RuntimeRecommendation:
    rec_type: str
    reason: str
    confidence: float
    expected_effect: str
    can_apply_patch: bool = False


class _PriceFeedSignals(QObject):
    price_tick = Signal(object)
    ws_status = Signal(str, str)


class TradingRuntimeWindow(QMainWindow):
    def __init__(
        self,
        symbol: str,
        exchange: str,
        strategy_snapshot: dict[str, Any],
        mode: str,
        price_feed_manager: PriceFeedManager | None = None,
        trade_ready_window: QWidget | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._symbol = symbol
        self._exchange = exchange
        self._strategy_snapshot = strategy_snapshot
        self._mode = mode
        self._trade_ready_window = trade_ready_window
        self._state = RuntimeState.IDLE
        self._uptime_seconds = 0
        self._logger = get_logger(f"gui.trading_runtime.{symbol.lower()}")
        self._price_feed_manager = price_feed_manager
        self._engine = RuntimeEngine(
            symbol=symbol,
            strategy_snapshot=strategy_snapshot,
            price_feed_manager=self._price_feed_manager,
        )
        self._event_queue: Queue[tuple[str, Any]] = Queue()
        self._price_signals = _PriceFeedSignals()
        self._price_signals.price_tick.connect(self._update_price_display)
        self._price_signals.ws_status.connect(self._update_ws_status)

        self.setWindowTitle(f"Trading Runtime — {symbol}")
        self.resize(1380, 900)

        central = QWidget()
        layout = QVBoxLayout()
        layout.addWidget(self._build_header())
        layout.addWidget(self._build_main_splitter())
        central.setLayout(layout)
        self.setCentralWidget(central)

        self._uptime_timer = QTimer(self)
        self._uptime_timer.setInterval(1000)
        self._uptime_timer.timeout.connect(self._tick_uptime)
        self._uptime_timer.start()

        self._event_timer = QTimer(self)
        self._event_timer.setInterval(200)
        self._event_timer.timeout.connect(self._process_event_queue)
        self._event_timer.start()

        self._orders_refresh_timer = QTimer(self)
        self._orders_refresh_timer.setInterval(1000)
        self._orders_refresh_timer.timeout.connect(self._refresh_orders_table)
        self._orders_refresh_timer.start()

        self._observer_timer = QTimer(self)
        self._observer_timer.timeout.connect(self._run_observer_cycle)
        self._update_observer_interval(self._observer_interval.currentText())

        self._engine.subscribe("order_created", lambda order: self._event_queue.put(("order_created", order)))
        self._engine.subscribe("order_filled", lambda order: self._event_queue.put(("order_filled", order)))
        self._engine.subscribe("pnl_updated", lambda pnl: self._event_queue.put(("pnl_updated", pnl)))
        self._engine.subscribe("state_changed", lambda state: self._event_queue.put(("state_changed", state)))

        self._apply_state(self._engine.get_state())

        if self._price_feed_manager is not None:
            self._price_feed_manager.register_symbol(self._symbol)
            self._price_feed_manager.subscribe(self._symbol, self._handle_price_tick)
            self._price_feed_manager.subscribe_status(self._symbol, self._handle_ws_status)
            self._price_feed_manager.start()

    def _build_header(self) -> QWidget:
        header = QWidget()
        layout = QHBoxLayout()

        symbol_label = QLabel(self._symbol)
        symbol_label.setStyleSheet("font-weight: 600; font-size: 16px;")
        layout.addWidget(symbol_label)

        exchange_label = QLabel(f"Exchange: {self._exchange}")
        layout.addWidget(exchange_label)

        self._mode_label = QLabel(f"Mode: {self._mode}")
        self._mode_label.setStyleSheet("padding: 4px 8px; border-radius: 8px; background: #0f172a; color: #f8fafc;")
        layout.addWidget(self._mode_label)

        self._state_badge = QLabel(self._state.value)
        layout.addWidget(self._state_badge)

        self._session_pnl = QLabel("Session PnL: —")
        layout.addWidget(self._session_pnl)

        self._price_label = QLabel("Price: —")
        layout.addWidget(self._price_label)

        self._latency_label = QLabel("Latency: —")
        layout.addWidget(self._latency_label)

        self._age_label = QLabel("Age: —")
        layout.addWidget(self._age_label)

        self._source_label = QLabel("Source: —")
        layout.addWidget(self._source_label)

        self._ws_status_label = QLabel("WS: LOST")
        layout.addWidget(self._ws_status_label)

        self._uptime_label = QLabel("Uptime: 00:00:00")
        layout.addWidget(self._uptime_label)

        layout.addStretch()

        self._start_button = QPushButton("Start")
        self._start_button.clicked.connect(self._confirm_start)
        layout.addWidget(self._start_button)

        self._pause_button = QPushButton("Pause")
        self._pause_button.clicked.connect(self._pause_runtime)
        layout.addWidget(self._pause_button)

        self._stop_button = QPushButton("Stop")
        self._stop_button.clicked.connect(self._stop_runtime)
        layout.addWidget(self._stop_button)

        self._emergency_button = QPushButton("Emergency Stop")
        self._emergency_button.setStyleSheet("background: #b91c1c; color: #fff; font-weight: 600;")
        self._emergency_button.clicked.connect(self._confirm_emergency_stop)
        layout.addWidget(self._emergency_button)

        self._close_position_button = QPushButton("Close Position")
        self._close_position_button.setEnabled(False)
        layout.addWidget(self._close_position_button)

        header.setLayout(layout)
        return header

    def _build_main_splitter(self) -> QSplitter:
        splitter = QSplitter(Qt.Vertical)
        splitter.setChildrenCollapsible(False)

        top_splitter = QSplitter(Qt.Horizontal)
        top_splitter.setChildrenCollapsible(False)
        top_splitter.addWidget(self._build_left_column())
        top_splitter.addWidget(self._build_right_column())
        top_splitter.setStretchFactor(0, 3)
        top_splitter.setStretchFactor(1, 2)

        splitter.addWidget(top_splitter)
        splitter.addWidget(self._build_bottom_tabs())
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 2)
        return splitter

    def _build_left_column(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout()

        orders_group = QGroupBox("Orders")
        orders_layout = QVBoxLayout()
        self._orders_hint = QLabel("Нет активных ордеров")
        self._orders_table = QTableWidget(0, 6)
        self._orders_table.setHorizontalHeaderLabels(
            ["Order ID", "Side", "Price", "Qty", "Status", "Age"]
        )
        self._orders_table.horizontalHeader().setStretchLastSection(True)
        self._orders_table.setEditTriggers(QTableWidget.NoEditTriggers)
        orders_layout.addWidget(self._orders_hint)
        orders_layout.addWidget(self._orders_table)
        orders_group.setLayout(orders_layout)

        position_group = QGroupBox("Position / Inventory")
        position_layout = QFormLayout()
        self._position_label = QLabel("FLAT")
        self._avg_price_label = QLabel("—")
        self._unrealized_label = QLabel("—")
        self._realized_label = QLabel("—")
        self._exposure_label = QLabel("—")
        position_layout.addRow(QLabel("Position"), self._position_label)
        position_layout.addRow(QLabel("Avg price"), self._avg_price_label)
        position_layout.addRow(QLabel("Unrealized PnL"), self._unrealized_label)
        position_layout.addRow(QLabel("Realized PnL"), self._realized_label)
        position_layout.addRow(QLabel("Exposure"), self._exposure_label)
        position_group.setLayout(position_layout)

        snapshot_group = QGroupBox("Strategy Snapshot (read-only)")
        snapshot_layout = QFormLayout()
        self._snapshot_labels: dict[str, QLabel] = {}
        for label, key in [
            ("Strategy ID", "strategy_id"),
            ("Type", "strategy_type"),
            ("Grid step", "grid_step"),
            ("Range", "range"),
            ("Risk limit", "risk_limit"),
        ]:
            value = QLabel(str(self._strategy_snapshot.get(key, "—")))
            snapshot_layout.addRow(QLabel(label), value)
            self._snapshot_labels[key] = value

        self._return_button = QPushButton("Вернуться к Trade Ready")
        self._return_button.clicked.connect(self._return_to_trade_ready)
        snapshot_layout.addRow(self._return_button)
        snapshot_group.setLayout(snapshot_layout)

        layout.addWidget(orders_group)
        layout.addWidget(position_group)
        layout.addWidget(snapshot_group)
        layout.addStretch()
        panel.setLayout(layout)
        return panel

    def _build_right_column(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout()
        layout.addWidget(self._build_ai_observer())
        layout.addWidget(self._build_recommendations())
        layout.addWidget(self._build_ai_chat())
        layout.addStretch()
        panel.setLayout(layout)
        return panel

    def _build_ai_observer(self) -> QWidget:
        group = QGroupBox("AI Observer")
        layout = QVBoxLayout()

        header_row = QHBoxLayout()
        header_row.addWidget(QLabel("Observer interval"))
        self._observer_interval = QComboBox()
        self._observer_interval.addItems(["10s", "30s", "1m", "5m"])
        self._observer_interval.currentTextChanged.connect(self._update_observer_interval)
        header_row.addWidget(self._observer_interval)
        header_row.addStretch()
        layout.addLayout(header_row)

        self._observer_last = QLabel("Last check: —")
        layout.addWidget(self._observer_last)

        self._observer_badge = QLabel("SAFE")
        self._observer_badge.setStyleSheet(
            "padding: 4px 10px; border-radius: 10px; background: #bbf7d0; color: #166534;"
        )
        layout.addWidget(self._observer_badge)

        self._observer_summary = QLabel("Рынок стабилен. Рекомендуется продолжить наблюдение.")
        self._observer_summary.setWordWrap(True)
        layout.addWidget(self._observer_summary)

        group.setLayout(layout)
        return group

    def _build_recommendations(self) -> QWidget:
        group = QGroupBox("AI Recommendations Queue")
        layout = QVBoxLayout()
        self._recommendations_container = QVBoxLayout()
        layout.addLayout(self._recommendations_container)
        layout.addStretch()
        group.setLayout(layout)
        return group

    def _build_ai_chat(self) -> QWidget:
        group = QGroupBox("AI Chat (Runtime)")
        layout = QVBoxLayout()
        self._chat_history = QTextEdit()
        self._chat_history.setReadOnly(True)
        self._chat_input = QLineEdit()
        self._chat_input.setPlaceholderText("Например: Почему ты предлагаешь паузу?")
        self._chat_send = QPushButton("Send")
        self._chat_send.clicked.connect(self._send_chat)
        layout.addWidget(self._chat_history)
        layout.addWidget(self._chat_input)
        layout.addWidget(self._chat_send)
        group.setLayout(layout)
        return group

    def _build_bottom_tabs(self) -> QTabWidget:
        tabs = QTabWidget()

        self._runtime_log = QListWidget()
        tabs.addTab(self._runtime_log, "Runtime Log")

        metrics_tab = QWidget()
        metrics_layout = QFormLayout()
        metrics_layout.addRow(QLabel("Trades count"), QLabel("—"))
        metrics_layout.addRow(QLabel("Winrate"), QLabel("—"))
        metrics_layout.addRow(QLabel("Avg spread"), QLabel("—"))
        metrics_tab.setLayout(metrics_layout)
        tabs.addTab(metrics_tab, "Metrics")

        self._raw_ai_json = QTextEdit()
        self._raw_ai_json.setReadOnly(True)
        self._raw_ai_json.setPlainText("Последний raw AI response появится здесь.")
        tabs.addTab(self._raw_ai_json, "AI Raw JSON")

        self._audit_trail = QListWidget()
        tabs.addTab(self._audit_trail, "Audit Trail")
        return tabs

    def _apply_state(self, state: RuntimeState) -> None:
        self._state = state
        palette = {
            RuntimeState.IDLE: ("#e2e8f0", "#1e293b"),
            RuntimeState.STOPPED: ("#e5e7eb", "#111827"),
            RuntimeState.RUNNING: ("#bbf7d0", "#166534"),
            RuntimeState.PAUSED: ("#fde68a", "#92400e"),
        }
        bg, fg = palette[state]
        self._state_badge.setText(state.value)
        self._state_badge.setStyleSheet(
            f"padding: 4px 10px; border-radius: 10px; background: {bg}; color: {fg};"
        )

        self._start_button.setEnabled(state in {RuntimeState.IDLE, RuntimeState.STOPPED, RuntimeState.PAUSED})
        self._pause_button.setEnabled(state == RuntimeState.RUNNING)
        self._stop_button.setEnabled(state in {RuntimeState.RUNNING, RuntimeState.PAUSED})
        self._emergency_button.setEnabled(state not in {RuntimeState.IDLE, RuntimeState.STOPPED})

    def _confirm_start(self) -> None:
        if self._state == RuntimeState.RUNNING:
            return
        message = "Запустить торговый runtime? Это демонстрационный режим."
        if QMessageBox.question(self, "Подтвердите запуск", message) != QMessageBox.Yes:
            return
        self._engine.start()
        self._log_event("Runtime запущен.")
        self._logger.info("Runtime started", extra={"symbol": self._symbol})

    def _pause_runtime(self) -> None:
        if self._state != RuntimeState.RUNNING:
            return
        self._engine.pause()
        self._log_event("Runtime поставлен на паузу.")
        self._logger.info("Runtime paused", extra={"symbol": self._symbol})

    def _stop_runtime(self) -> None:
        if self._state in {RuntimeState.IDLE, RuntimeState.STOPPED}:
            return
        self._engine.stop()
        self._log_event("Runtime остановлен.")
        self._logger.info("Runtime stopped", extra={"symbol": self._symbol})

    def _confirm_emergency_stop(self) -> None:
        if self._state in {RuntimeState.IDLE, RuntimeState.STOPPED}:
            return
        message = "Выполнить EMERGENCY STOP? Это немедленно остановит runtime."
        if QMessageBox.warning(self, "Emergency Stop", message, QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        self._engine.stop(cancel_orders=True)
        self._log_event("EMERGENCY STOP выполнен.")
        self._logger.warning("Emergency stop", extra={"symbol": self._symbol})

    def _tick_uptime(self) -> None:
        if self._state != RuntimeState.RUNNING:
            return
        self._uptime_seconds += 1
        self._update_uptime_label()

    def _update_uptime_label(self) -> None:
        hours = self._uptime_seconds // 3600
        minutes = (self._uptime_seconds % 3600) // 60
        seconds = self._uptime_seconds % 60
        self._uptime_label.setText(f"Uptime: {hours:02d}:{minutes:02d}:{seconds:02d}")

    def _log_event(self, message: str) -> None:
        self._runtime_log.addItem(message)
        self._runtime_log.scrollToBottom()

    def _log_audit(self, message: str) -> None:
        self._audit_trail.addItem(message)
        self._audit_trail.scrollToBottom()

    def _process_event_queue(self) -> None:
        while not self._event_queue.empty():
            event, payload = self._event_queue.get_nowait()
            if event == "state_changed":
                self._handle_state_changed(payload)
            elif event == "order_created":
                self._refresh_orders_table()
                self._log_event(f"Order created: {payload.id}")
            elif event == "order_filled":
                self._refresh_orders_table()
                self._log_event(f"Order filled: {payload.id}")
            elif event == "pnl_updated":
                self._update_pnl(payload)

    def _handle_state_changed(self, state: RuntimeState) -> None:
        self._apply_state(state)
        if state in {RuntimeState.IDLE, RuntimeState.STOPPED}:
            self._uptime_seconds = 0
            self._update_uptime_label()
        if state == RuntimeState.PAUSED:
            self._log_event("Runtime paused by engine.")
        elif state == RuntimeState.RUNNING:
            self._log_event("Runtime running.")

    def _refresh_orders_table(self) -> None:
        orders = self._engine.get_orders()
        self._orders_table.setRowCount(len(orders))
        now = datetime.now(timezone.utc)
        for row, order in enumerate(orders):
            age_seconds = int((now - order.created_at).total_seconds())
            values = [
                order.id,
                order.side.value,
                f"{order.price:.4f}",
                f"{order.qty:.4f}",
                order.status.value,
                f"{age_seconds}s",
            ]
            for col, value in enumerate(values):
                self._orders_table.setItem(row, col, QTableWidgetItem(value))
        self._orders_hint.setVisible(len(orders) == 0)

    def _update_pnl(self, pnl: PnLSnapshot) -> None:
        total = pnl.realized + pnl.unrealized
        self._session_pnl.setText(f"Session PnL: {total:.2f}")
        position_label = "FLAT"
        if pnl.position_qty > 0:
            position_label = f"LONG (+{pnl.position_qty:.4f})"
        elif pnl.position_qty < 0:
            position_label = f"SHORT ({pnl.position_qty:.4f})"
        self._position_label.setText(position_label)
        self._avg_price_label.setText(f"{pnl.avg_price:.4f}" if pnl.position_qty else "—")
        self._unrealized_label.setText(f"{pnl.unrealized:.2f}")
        self._realized_label.setText(f"{pnl.realized:.2f}")
        self._exposure_label.setText(f"{pnl.exposure:.2f}")

    def _run_observer_cycle(self) -> None:
        if self._state == RuntimeState.IDLE:
            return
        snapshot = self._engine.get_observer_snapshot()
        recommendations_raw = self._engine.get_recommendations()
        recommendations = [RuntimeRecommendation(**rec) for rec in recommendations_raw]
        self._render_recommendations(recommendations)
        now = datetime.now(timezone.utc)
        self._observer_last.setText(f"Last check: {now.strftime('%H:%M:%S')} UTC")

        badge_style = "padding: 4px 10px; border-radius: 10px; background: #bbf7d0; color: #166534;"
        badge_text = "SAFE"
        if any(rec.rec_type == "PAUSE_TRADING" for rec in recommendations):
            badge_style = "padding: 4px 10px; border-radius: 10px; background: #fecaca; color: #991b1b;"
            badge_text = "DANGER"
        elif any(rec.rec_type in {"ADJUST_PARAMS", "REBUILD_GRID"} for rec in recommendations):
            badge_style = "padding: 4px 10px; border-radius: 10px; background: #fde68a; color: #92400e;"
            badge_text = "WARNING"
        self._observer_badge.setStyleSheet(badge_style)
        self._observer_badge.setText(badge_text)

        self._observer_summary.setText(recommendations[0].reason if recommendations else "—")
        payload = {"snapshot": snapshot, "recommendations": recommendations_raw}
        self._raw_ai_json.setPlainText(self._format_json(payload))

    def _update_observer_interval(self, value: str) -> None:
        mapping = {"10s": 10_000, "30s": 30_000, "1m": 60_000, "5m": 300_000}
        interval = mapping.get(value, 30_000)
        self._observer_timer.setInterval(interval)
        self._observer_timer.start()
        self._log_event(f"Observer interval set to {value}.")

    def _handle_price_tick(self, tick: PriceUpdate) -> None:
        self._price_signals.price_tick.emit(tick)

    def _handle_ws_status(self, status: str, details: str) -> None:
        self._price_signals.ws_status.emit(status, details)

    def _update_price_display(self, update: PriceUpdate) -> None:
        if update.last_price is None:
            return
        self._price_label.setText(f"Price: {update.last_price:.6f}")
        latency = "—" if update.latency_ms is None else f"{update.latency_ms} ms"
        age = "—" if update.price_age_ms is None else f"{update.price_age_ms} ms"
        self._latency_label.setText(f"Latency: {latency}")
        self._age_label.setText(f"Age: {age}")
        self._source_label.setText(f"Source: {update.source}")

    def _update_ws_status(self, status: str, details: str) -> None:
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

    def _format_json(self, payload: dict[str, Any]) -> str:
        try:
            import json

            return json.dumps(payload, ensure_ascii=False, indent=2)
        except Exception:
            return str(payload)

    def _render_recommendations(self, recommendations: list[RuntimeRecommendation]) -> None:
        while self._recommendations_container.count():
            item = self._recommendations_container.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()
        for rec in recommendations:
            self._recommendations_container.addWidget(self._build_recommendation_card(rec))

    def _build_recommendation_card(self, rec: RuntimeRecommendation) -> QWidget:
        card = QFrame()
        card.setFrameShape(QFrame.StyledPanel)
        layout = QVBoxLayout()

        title = QLabel(rec.rec_type)
        title.setStyleSheet("font-weight: 600; font-size: 13px;")
        layout.addWidget(title)
        layout.addWidget(QLabel(rec.reason))
        layout.addWidget(QLabel(f"Confidence: {rec.confidence:.2f}"))
        layout.addWidget(QLabel(f"Expected effect: {rec.expected_effect}"))

        actions = QHBoxLayout()
        actions.addStretch()
        approve_btn = QPushButton("Approve")
        approve_btn.clicked.connect(lambda: self._approve_recommendation(rec))
        reject_btn = QPushButton("Reject")
        reject_btn.clicked.connect(lambda: self._reject_recommendation(rec))
        apply_btn = QPushButton("Apply Patch")
        apply_btn.setEnabled(rec.can_apply_patch)
        apply_btn.clicked.connect(lambda: self._apply_patch(rec))
        actions.addWidget(approve_btn)
        actions.addWidget(reject_btn)
        actions.addWidget(apply_btn)

        layout.addLayout(actions)
        card.setLayout(layout)
        return card

    def _approve_recommendation(self, rec: RuntimeRecommendation) -> None:
        self._log_event(f"AI recommendation approved: {rec.rec_type}")
        self._log_audit(f"Approved {rec.rec_type} by user")
        self._logger.info("Recommendation approved", extra={"type": rec.rec_type})

    def _reject_recommendation(self, rec: RuntimeRecommendation) -> None:
        self._log_event(f"AI recommendation rejected: {rec.rec_type}")
        self._log_audit(f"Rejected {rec.rec_type} by user")
        self._logger.info("Recommendation rejected", extra={"type": rec.rec_type})

    def _apply_patch(self, rec: RuntimeRecommendation) -> None:
        self._log_event(f"AI patch applied (mock): {rec.rec_type}")
        self._log_audit(f"Patch applied for {rec.rec_type}")
        self._logger.info("Recommendation patch applied", extra={"type": rec.rec_type})

    def _send_chat(self) -> None:
        message = self._chat_input.text().strip()
        if not message:
            return
        self._chat_input.clear()
        self._append_chat("Вы", message)
        response = self._mock_chat_response(message)
        self._append_chat("AI", response)
        self._log_event("AI runtime chat message received.")

    def _append_chat(self, speaker: str, message: str) -> None:
        self._chat_history.append(f"<b>{speaker}:</b> {message}")

    def _mock_chat_response(self, message: str) -> str:
        lowered = message.lower()
        if "пау" in lowered:
            return "Пауза предлагается из-за увеличенной волатильности. Решение за вами."
        if "консерватив" in lowered:
            return "Могу снизить риск, увеличив шаг и сузив диапазон. Подтвердите, если нужно."
        return "Принято. Готов дать рекомендации после следующей проверки."

    def _return_to_trade_ready(self) -> None:
        if not self._trade_ready_window:
            return
        self._trade_ready_window.raise_()
        self._trade_ready_window.activateWindow()
        self._log_event("Фокус возвращён на Trade Ready.")

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt naming
        self._uptime_timer.stop()
        self._event_timer.stop()
        self._orders_refresh_timer.stop()
        self._observer_timer.stop()
        self._engine.stop(cancel_orders=True)
        if self._price_feed_manager is not None:
            self._price_feed_manager.unsubscribe(self._symbol, self._handle_price_tick)
            self._price_feed_manager.unsubscribe_status(self._symbol, self._handle_ws_status)
            self._price_feed_manager.unregister_symbol(self._symbol)
        super().closeEvent(event)
