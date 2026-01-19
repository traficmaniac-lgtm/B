from __future__ import annotations

import asyncio
import json
from typing import Callable

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.ai.models import AiResponseEnvelope, fallback_do_not_trade
from src.ai.openai_client import OpenAIClient
from src.core.logging import get_logger
from src.gui.models.app_state import AppState
from src.gui.pair_workspace_tab import PairWorkspaceTab


class _AiObserverSignals(QObject):
    success = Signal(object, str)
    error = Signal(str)


class _AiObserverWorker(QRunnable):
    def __init__(self, fn: Callable[[], tuple[AiResponseEnvelope, str]]) -> None:
        super().__init__()
        self.signals = _AiObserverSignals()
        self._fn = fn

    def run(self) -> None:
        try:
            envelope, raw = self._fn()
        except Exception as exc:  # noqa: BLE001
            self.signals.error.emit(str(exc))
            return
        self.signals.success.emit(envelope, raw)


class TradingWorkspaceWindow(QMainWindow):
    def __init__(
        self,
        pair_workspace: PairWorkspaceTab,
        app_state: AppState,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._pair_workspace = pair_workspace
        self._app_state = app_state
        self._logger = get_logger(f"gui.trading_workspace.{pair_workspace.symbol.lower()}")
        self._thread_pool = QThreadPool.globalInstance()
        self._observer_in_flight = False
        self._last_ai_response: AiResponseEnvelope | None = None
        self._last_ai_raw: str | None = None

        self.setWindowTitle(f"Trading Workspace — {pair_workspace.symbol}")
        self.resize(1200, 820)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_ai_panel())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        central = QWidget()
        layout = QVBoxLayout()
        layout.addWidget(splitter)
        central.setLayout(layout)
        self.setCentralWidget(central)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(1000)
        self._refresh_timer.timeout.connect(self._refresh_snapshot)
        self._refresh_timer.start()

        self._observer_timer = QTimer(self)
        self._observer_timer.timeout.connect(self._trigger_observer_check)
        self._update_observer_interval()
        self._observer_timer.start()

        self._refresh_snapshot()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._refresh_timer.stop()
        self._observer_timer.stop()
        super().closeEvent(event)

    def _build_left_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout()

        runtime_box = QGroupBox("Runtime")
        runtime_layout = QVBoxLayout()
        self._runtime_symbol = QLabel("Symbol: -")
        self._runtime_mode = QLabel("Mode: -")
        self._runtime_state = QLabel("State: -")
        self._runtime_datapack = QLabel("Datapack: -")
        runtime_layout.addWidget(self._runtime_symbol)
        runtime_layout.addWidget(self._runtime_mode)
        runtime_layout.addWidget(self._runtime_state)
        runtime_layout.addWidget(self._runtime_datapack)
        runtime_box.setLayout(runtime_layout)

        orders_box = QGroupBox("Orders")
        orders_layout = QVBoxLayout()
        self._orders_table = QTableWidget(0, 4)
        self._orders_table.setHorizontalHeaderLabels(["Side", "Price", "Qty", "% from mid"])
        self._orders_table.horizontalHeader().setStretchLastSection(True)
        self._orders_table.setEditTriggers(QTableWidget.NoEditTriggers)
        orders_layout.addWidget(self._orders_table)
        orders_box.setLayout(orders_layout)

        position_box = QGroupBox("Position / PnL")
        position_layout = QVBoxLayout()
        self._position_label = QLabel("Position: -")
        self._pnl_label = QLabel("PnL: -")
        position_layout.addWidget(self._position_label)
        position_layout.addWidget(self._pnl_label)
        position_box.setLayout(position_layout)

        risk_box = QGroupBox("Risk")
        risk_layout = QVBoxLayout()
        self._risk_label = QLabel("Risk: -")
        risk_layout.addWidget(self._risk_label)
        risk_box.setLayout(risk_layout)

        layout.addWidget(runtime_box)
        layout.addWidget(orders_box)
        layout.addWidget(position_box)
        layout.addWidget(risk_box)
        layout.addStretch()
        panel.setLayout(layout)
        return panel

    def _build_ai_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout()

        observer_box = QGroupBox("AI Observer")
        observer_layout = QVBoxLayout()
        self._observer_status = QLabel("Observer: idle")
        self._observer_last = QLabel("Last check: -")
        observer_layout.addWidget(self._observer_status)
        observer_layout.addWidget(self._observer_last)

        self._observer_summary = QPlainTextEdit()
        self._observer_summary.setReadOnly(True)
        self._observer_summary.setPlaceholderText("AI observer summary will appear here.")
        observer_layout.addWidget(self._observer_summary)

        self._observer_raw = QPlainTextEdit()
        self._observer_raw.setReadOnly(True)
        self._observer_raw.setPlaceholderText("Raw AI JSON will appear here.")
        observer_layout.addWidget(self._observer_raw)

        actions_row = QHBoxLayout()
        self._approve_pause = QPushButton("Approve Pause")
        self._approve_rebuild = QPushButton("Approve Rebuild")
        self._approve_cancel = QPushButton("Approve Cancel Orders")
        self._approve_close = QPushButton("Approve Close Position")
        self._apply_patch = QPushButton("Apply Patch")
        for button in (
            self._approve_pause,
            self._approve_rebuild,
            self._approve_cancel,
            self._approve_close,
            self._apply_patch,
        ):
            button.setEnabled(False)
            actions_row.addWidget(button)

        self._approve_pause.clicked.connect(lambda: self._confirm_action("PAUSE"))
        self._approve_rebuild.clicked.connect(lambda: self._confirm_action("REBUILD"))
        self._approve_cancel.clicked.connect(lambda: self._confirm_action("CANCEL_ORDERS"))
        self._approve_close.clicked.connect(lambda: self._confirm_action("CLOSE_POSITION"))
        self._apply_patch.clicked.connect(lambda: self._confirm_action("APPLY_PATCH"))

        observer_layout.addLayout(actions_row)
        observer_box.setLayout(observer_layout)

        layout.addWidget(observer_box)
        panel.setLayout(layout)
        return panel

    def _refresh_snapshot(self) -> None:
        snapshot = self._pair_workspace.get_trading_snapshot()
        self._runtime_symbol.setText(f"Symbol: {snapshot['symbol']}")
        mode_label = "Dry-run" if snapshot["dry_run"] else "Live"
        self._runtime_mode.setText(f"Mode: {mode_label}")
        self._runtime_state.setText(f"State: {snapshot['state']} ({snapshot['last_reason']})")
        if snapshot["datapack_summary"]:
            summary = snapshot["datapack_summary"]
            self._runtime_datapack.setText(
                f"Datapack: {summary.get('period')} {summary.get('quality')} | "
                f"Last price: {summary.get('last_price')}"
            )
        else:
            self._runtime_datapack.setText("Datapack: -")
        self._render_orders(snapshot["open_orders"])
        self._position_label.setText(f"Position: {snapshot['position']['status']}")
        self._pnl_label.setText(f"PnL: {snapshot['position']['pnl']:.2f} USDT")
        risk = snapshot["risk"]
        risk_text = "Risk: -"
        if risk["hard_stop_pct"] is not None:
            risk_text = (
                f"Risk: hard_stop={risk['hard_stop_pct']}% | "
                f"cooldown={risk['cooldown_minutes']}m | "
                f"volatility={risk['volatility_mode']}"
            )
        self._risk_label.setText(risk_text)
        self._update_observer_interval(snapshot["recheck_interval_sec"])

    def _render_orders(self, orders: list[dict[str, str]]) -> None:
        self._orders_table.setRowCount(len(orders))
        for row, order in enumerate(orders):
            for col, key in enumerate(("side", "price", "qty", "pct_from_mid")):
                item = QTableWidgetItem(order.get(key, "--"))
                item.setTextAlignment(Qt.AlignCenter)
                self._orders_table.setItem(row, col, item)

    def _update_observer_interval(self, interval_sec: int | None = None) -> None:
        if interval_sec is None:
            interval_sec = self._pair_workspace.get_trading_snapshot()["recheck_interval_sec"]
        interval_ms = max(int(interval_sec), 5) * 1000
        self._observer_timer.setInterval(interval_ms)
        self._observer_status.setText(f"Observer: interval {interval_sec}s")

    def _trigger_observer_check(self) -> None:
        if self._observer_in_flight:
            return
        if not self._app_state.openai_key_present:
            self._observer_status.setText("Observer: OpenAI key missing")
            return
        datapack = self._pair_workspace.build_monitor_datapack()

        def _run() -> tuple[AiResponseEnvelope, str]:
            client = OpenAIClient(
                api_key=self._app_state.openai_api_key,
                model=self._app_state.openai_model,
                timeout_s=25.0,
                retries=1,
            )
            return asyncio.run(client.monitor_datapack(datapack))

        self._observer_in_flight = True
        self._observer_status.setText("Observer: checking...")
        worker = _AiObserverWorker(_run)
        worker.signals.success.connect(self._handle_observer_success)
        worker.signals.error.connect(self._handle_observer_error)
        self._thread_pool.start(worker)

    def _handle_observer_success(self, envelope: object, raw: str) -> None:
        self._observer_in_flight = False
        if not isinstance(envelope, AiResponseEnvelope):
            self._handle_observer_error("AI observer response invalid.")
            return
        self._last_ai_response = envelope
        self._last_ai_raw = raw
        summary = {
            "status": envelope.status,
            "confidence": envelope.confidence,
            "reason_codes": envelope.reason_codes,
            "message": envelope.message,
        }
        self._observer_summary.setPlainText(json.dumps(summary, ensure_ascii=False, indent=2))
        self._observer_raw.setPlainText(raw or "Empty response.")
        self._observer_last.setText("Last check: OK")
        self._observer_status.setText(f"Observer: {envelope.status}")
        self._logger.info("AI observer summary: %s", summary)
        if raw:
            self._logger.info("AI observer raw: %s", raw)
        self._set_action_buttons(envelope)

    def _handle_observer_error(self, message: str) -> None:
        self._observer_in_flight = False
        fallback = fallback_do_not_trade(message)
        self._last_ai_response = fallback
        self._last_ai_raw = None
        self._observer_summary.setPlainText(json.dumps({"status": "ERROR", "message": message}, indent=2))
        self._observer_raw.setPlainText("No valid JSON response.")
        self._observer_last.setText("Last check: ERROR")
        self._observer_status.setText("Observer: ERROR")
        self._logger.warning("AI observer error: %s", message)
        self._set_action_buttons(fallback)

    def _set_action_buttons(self, envelope: AiResponseEnvelope) -> None:
        has_actions = False
        if envelope.analysis_result and envelope.analysis_result.actions:
            has_actions = True
        self._approve_pause.setEnabled(has_actions)
        self._approve_rebuild.setEnabled(has_actions)
        self._approve_cancel.setEnabled(has_actions)
        self._approve_close.setEnabled(has_actions)
        self._apply_patch.setEnabled(envelope.strategy_patch is not None)

    def _confirm_action(self, action: str) -> None:
        if self._last_ai_response is None:
            return
        reply = QMessageBox.question(
            self,
            "Confirm action",
            f"Approve AI suggested action: {action}?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        self._logger.info("USER_APPROVED: %s", action)
        self._observer_summary.appendPlainText(f"USER_APPROVED: {action}")
