from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime, timezone
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, Qt, Signal, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.ai.openai_client import OpenAIClient
from src.ai.operator_models import AiOperatorResponse, AiOperatorStrategyPatch, parse_ai_operator_response
from src.binance.http_client import BinanceHttpClient
from src.core.config import Config
from src.core.timeutil import utc_ms
from src.gui.lite_grid_window import LiteGridWindow, TradeGate
from src.gui.models.app_state import AppState
from src.services.data_cache import DataCache
from src.services.price_feed_manager import PriceFeedManager, PriceUpdate


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


@dataclass
class MarketSnapshot:
    snapshot_id: str
    symbol: str
    ts_ms: int
    last_price: float | None
    best_bid: float | None
    best_ask: float | None
    spread_pct: float | None
    ws_age_ms: int | None
    latency_ms: int | None
    price_source: str | None
    orderbook_depth_50: dict[str, Any] | None
    trades_1m: dict[str, Any] | None
    maker_fee_pct: float | None
    taker_fee_pct: float | None
    is_zero_fee: bool
    rules: dict[str, Any]
    balances: dict[str, float]
    open_orders_count: int


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
        self._last_ai_response: AiOperatorResponse | None = None
        self._last_ai_result_json: str | None = None
        self._last_strategy_patch: AiOperatorStrategyPatch | None = None
        self._last_actions_suggested: list[str] = []
        self._last_approve_action: str | None = None
        self._last_approve_ts: float = 0.0
        self._last_apply_ts: float = 0.0
        self._max_exposure_warned = False
        self._ai_busy = False
        self._runtime_stopping = False
        self._last_ai_datapack_signature: str | None = None
        self._market_snapshot: MarketSnapshot | None = None
        self._snapshot_sequence = 0
        self._data_request_resolved = False
        self._data_cache = DataCache()
        self._http_client = BinanceHttpClient()
        self._cache_ttls = {
            "orderbook_depth_50": 2.0,
            "recent_trades_1m": 2.0,
        }
        self._apply_ai_layout_policies()

    def _apply_ai_layout_policies(self) -> None:
        central = self.centralWidget()
        if not central:
            return
        layout = central.layout()
        if isinstance(layout, QVBoxLayout):
            layout.setStretch(0, 0)
            layout.setStretch(1, 3)
            layout.setStretch(2, 1)

    def _build_body(self) -> QSplitter:
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        ai_panel = self._build_ai_panel()
        ai_panel.setMinimumWidth(420)
        ai_panel.setMaximumWidth(520)
        params_panel = super()._build_grid_panel()
        runtime_panel = super()._build_runtime_panel()
        runtime_panel.setMinimumWidth(480)
        runtime_panel.setMaximumWidth(640)

        splitter.addWidget(ai_panel)
        splitter.addWidget(params_panel)
        splitter.addWidget(runtime_panel)
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
        group.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout = QVBoxLayout(group)
        layout.setSpacing(4)

        top_actions = QHBoxLayout()
        self._analyze_button = QPushButton("AI Analyze")
        self._analyze_button.clicked.connect(self._handle_ai_analyze)
        top_actions.addWidget(self._analyze_button)

        self._refresh_snapshot_button = QPushButton("Refresh snapshot")
        self._refresh_snapshot_button.clicked.connect(self._handle_refresh_snapshot)
        top_actions.addWidget(self._refresh_snapshot_button)

        self._apply_plan_button = QPushButton("Apply Plan")
        self._apply_plan_button.setEnabled(False)
        self._apply_plan_button.clicked.connect(self._apply_ai_plan)
        top_actions.addWidget(self._apply_plan_button)

        self._fetch_data_button = QPushButton("Fetch requested data")
        self._fetch_data_button.setVisible(False)
        self._fetch_data_button.setEnabled(False)
        self._fetch_data_button.clicked.connect(self._handle_fetch_requested_data)
        top_actions.addWidget(self._fetch_data_button)

        top_actions.addStretch()
        layout.addLayout(top_actions)

        self._ai_request_hint = QLabel("AI просит данные — нажмите Fetch requested data.")
        self._ai_request_hint.setStyleSheet("color: #6b7280; font-size: 12px;")
        self._ai_request_hint.setVisible(False)
        layout.addWidget(self._ai_request_hint)

        self._ai_snapshot_label = QLabel("snapshot: —")
        self._ai_snapshot_label.setStyleSheet("color: #374151; font-size: 12px;")
        layout.addWidget(self._ai_snapshot_label)

        market_panel = super()._build_market_panel()
        market_panel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        layout.addWidget(market_panel)

        self._chat_history = QPlainTextEdit()
        self._chat_history.setReadOnly(True)
        self._chat_history.setPlaceholderText("AI chat history will appear here.")
        self._chat_history.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self._chat_history, stretch=1)

        input_row = QHBoxLayout()
        self._chat_input = QLineEdit()
        self._chat_input.setPlaceholderText("Введите сообщение для AI...")
        self._chat_input.returnPressed.connect(self._handle_ai_send)
        input_row.addWidget(self._chat_input, stretch=1)
        self._send_button = QPushButton("Send")
        self._send_button.clicked.connect(self._handle_ai_send)
        input_row.addWidget(self._send_button)
        layout.addLayout(input_row)

        actions_row = QHBoxLayout()
        actions_row.addWidget(QLabel("Action"))
        self._actions_combo = QComboBox()
        self._actions_combo.currentIndexChanged.connect(self._update_approve_button_state)
        actions_row.addWidget(self._actions_combo, stretch=1)
        self._approve_button = QPushButton("Approve")
        self._approve_button.setEnabled(False)
        self._approve_button.clicked.connect(self._approve_ai_action)
        actions_row.addWidget(self._approve_button)
        pause_button = QPushButton("Pause (AI)")
        pause_button.clicked.connect(self._handle_pause)
        actions_row.addWidget(pause_button)
        layout.addLayout(actions_row)

        return group

    def _build_logs(self) -> QWidget:
        frame = super()._build_logs()
        frame.setMinimumHeight(220)
        frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        if hasattr(self, "_log_view"):
            self._log_view.setMinimumHeight(140)
            self._log_view.setMaximumHeight(16_777_215)
            self._log_view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        return frame

    def _handle_ai_analyze(self) -> None:
        if not self._app_state.openai_key_present:
            self._append_log("[AI] analyze skipped: missing OpenAI key.", "WARN")
            self._append_chat_line("AI", "Не задан ключ OpenAI.")
            return
        self._data_request_resolved = False
        snapshot = self._refresh_ai_snapshot(reason="analyze")
        datapack = self._build_ai_datapack(snapshot)
        self._append_chat_line("YOU", "AI Analyze")
        self._append_log("[AI] datapack prepared", "INFO")
        self._log_ai_datapack_snapshot(datapack)
        self._update_ai_snapshot_label(datapack)
        self._last_ai_datapack_signature = self._datapack_signature(datapack)
        self._set_ai_busy(True)
        worker = _AiWorker(lambda: self._run_ai_analyze(datapack))
        worker.signals.success.connect(self._handle_ai_analyze_success)
        worker.signals.error.connect(self._handle_ai_analyze_error)
        self._thread_pool.start(worker)
        self._append_log("[AI] request sent", "INFO")

    def _run_ai_analyze(self, datapack: dict[str, Any]) -> str:
        client = OpenAIClient(
            api_key=self._app_state.openai_api_key,
            model=self._app_state.openai_model,
            timeout_s=25.0,
            retries=2,
        )
        return asyncio.run(client.analyze_operator(datapack))

    def _handle_ai_analyze_success(self, response: object) -> None:
        self._set_ai_busy(False)
        if not isinstance(response, str):
            self._handle_ai_analyze_error("AI response invalid or empty.")
            return
        try:
            parsed = parse_ai_operator_response(response)
        except ValueError as exc:
            self._handle_ai_analyze_error(str(exc))
            return
        parsed = self._apply_request_more_data_policy(parsed)
        self._last_ai_response = parsed
        self._last_ai_result_json = response
        self._last_strategy_patch = parsed.strategy_patch
        self._last_actions_suggested = list(parsed.actions_suggested)
        self._append_ai_response_to_chat(parsed)
        self._render_actions(parsed.actions_suggested)
        self._apply_plan_button.setEnabled(self._strategy_patch_has_values(parsed.strategy_patch))
        self._update_fetch_data_button()
        self._append_log("[AI] response received", "INFO")

    def _handle_ai_analyze_error(self, message: str) -> None:
        self._set_ai_busy(False)
        self._append_log(f"[AI] response invalid: {message}", "WARN")
        self._append_chat_line("AI", f"Ошибка AI: {message}")
        self._last_ai_response = None
        self._actions_combo.clear()
        self._apply_plan_button.setEnabled(False)
        self._update_fetch_data_button()
        self._update_approve_button_state()

    def _apply_ai_plan(self) -> None:
        if not self._last_strategy_patch or not self._strategy_patch_has_values(self._last_strategy_patch):
            self._append_log("[AI] patch empty, nothing to apply", "INFO")
            self._append_chat_line("AI", "patch empty, nothing to apply")
            return
        now = time.monotonic()
        if now - self._last_apply_ts < 0.3:
            return
        self._last_apply_ts = now
        self._apply_plan_button.setEnabled(False)
        QTimer.singleShot(400, self._restore_apply_plan_state)
        self._apply_strategy_patch_to_form(self._last_strategy_patch)
        self._append_log("[AI] strategy_patch applied", "INFO")
        self._append_chat_line("AI", "Strategy patch applied via Apply Plan.")

    def _handle_ai_send(self) -> None:
        message = self._chat_input.text().strip()
        if not message:
            return
        if not self._app_state.openai_key_present:
            self._append_log("[AI] chat skipped: missing OpenAI key.", "WARN")
            self._append_chat_line("AI", "Не задан ключ OpenAI.")
            return
        self._append_chat_line("YOU", message)
        self._chat_input.clear()
        snapshot = self._get_ai_snapshot()
        if snapshot is None:
            snapshot = self._refresh_ai_snapshot(reason="chat")
        datapack = self._build_ai_datapack(snapshot)
        current_ui_params = self.dump_settings()
        self._set_ai_busy(True)
        self._log_ai_datapack_snapshot(datapack)
        self._last_ai_datapack_signature = self._datapack_signature(datapack)
        worker = _AiWorker(
            lambda: self._run_ai_chat_adjust(
                datapack=datapack,
                message=message,
                last_ai_json=self._last_ai_result_json,
                current_ui_params=current_ui_params,
            )
        )
        worker.signals.success.connect(self._handle_ai_chat_success)
        worker.signals.error.connect(self._handle_ai_chat_error)
        self._thread_pool.start(worker)
        self._append_log("[AI] chat request sent", "INFO")

    def _run_ai_chat_adjust(
        self,
        datapack: dict[str, Any],
        message: str,
        last_ai_json: str | None,
        current_ui_params: dict[str, Any],
    ) -> str:
        client = OpenAIClient(
            api_key=self._app_state.openai_api_key,
            model=self._app_state.openai_model,
            timeout_s=25.0,
            retries=2,
        )
        return asyncio.run(
            client.chat_operator(
                datapack=datapack,
                user_message=message,
                last_ai_json=last_ai_json,
                current_ui_params=current_ui_params,
            )
        )

    def _handle_ai_chat_success(self, response: object) -> None:
        self._set_ai_busy(False)
        if not isinstance(response, str):
            self._handle_ai_chat_error("AI response invalid or empty.")
            return
        try:
            parsed = parse_ai_operator_response(response)
        except ValueError as exc:
            self._handle_ai_chat_error(str(exc))
            return
        parsed = self._apply_request_more_data_policy(parsed)
        self._last_ai_response = parsed
        self._last_ai_result_json = response
        self._last_strategy_patch = parsed.strategy_patch
        self._last_actions_suggested = list(parsed.actions_suggested)
        self._append_ai_response_to_chat(parsed)
        self._render_actions(parsed.actions_suggested)
        self._apply_plan_button.setEnabled(self._strategy_patch_has_values(parsed.strategy_patch))
        self._update_fetch_data_button()
        self._append_log("[AI] chat response received", "INFO")

    def _handle_ai_chat_error(self, message: str) -> None:
        self._set_ai_busy(False)
        self._append_log(f"[AI] chat response invalid: {message}", "WARN")
        self._append_chat_line("AI", f"Ошибка AI: {message}")
        self._update_fetch_data_button()

    def _approve_ai_action(self) -> None:
        action = self._actions_combo.currentText().strip().upper()
        if not action:
            self._append_log("[AI] approve skipped: no action selected.", "WARN")
            return
        now = time.monotonic()
        if now - self._last_approve_ts < 1.0:
            self._append_log(f"[AI] approve ignored: action {action} debounced.", "WARN")
            return
        self._last_approve_action = action
        self._last_approve_ts = now
        if action == "START":
            self._handle_start()
        elif action == "REBUILD_GRID":
            self._handle_stop()
            self._handle_start()
        elif action == "PAUSE":
            self._handle_pause()
        elif action == "WAIT":
            self._append_log("[AI] approved WAIT (no-op)", "INFO")
            return
        else:
            self._append_log(f"[AI] approve ignored: unknown action {action}.", "WARN")
            return
        self._append_log(f"[AI] approved action: {action}", "INFO")
        self._append_chat_line("AI", f"approved action: {action}")

    def _render_actions(self, actions: list[str]) -> None:
        self._actions_combo.clear()
        for action in actions:
            if not action:
                continue
            self._actions_combo.addItem(action)
        if self._actions_combo.count() > 0:
            self._actions_combo.setCurrentIndex(0)
        self._update_approve_button_state()

    def _update_approve_button_state(self) -> None:
        self._approve_button.setEnabled(self._actions_combo.count() > 0)

    def _set_ai_busy(self, busy: bool) -> None:
        self._ai_busy = busy
        self._analyze_button.setEnabled(not busy)
        self._chat_input.setEnabled(not busy)
        self._send_button.setEnabled(not busy)
        self._update_fetch_data_button()

    def _append_chat_line(self, author: str, message: str) -> None:
        if not message:
            return
        prefix = "Ты" if author.upper() in {"YOU", "USER", "ТЫ"} else "AI"
        self._append_chat_block([f"{prefix}: {message}"])

    def _append_ai_response_to_chat(self, response: AiOperatorResponse) -> None:
        summary = response.analysis_result.summary or "—"
        lines = ["AI:"]
        lines.append(f"Summary: {summary}")
        lines.append(f"State: {response.analysis_result.state}")
        risks = response.analysis_result.risks[:3]
        risks_text = "; ".join(risks) if risks else "—"
        lines.append(f"Risks: {risks_text}")
        suggested_action = response.actions_suggested[0] if response.actions_suggested else "—"
        lines.append(f"Suggested Action: {suggested_action}")
        patch = response.strategy_patch
        if patch and self._strategy_patch_has_values(patch):
            step = f"{patch.step_pct}" if patch.step_pct is not None else "—"
            levels = f"{patch.levels}" if patch.levels is not None else "—"
            if patch.range_down_pct is not None or patch.range_up_pct is not None:
                range_down = patch.range_down_pct if patch.range_down_pct is not None else "—"
                range_up = patch.range_up_pct if patch.range_up_pct is not None else "—"
                range_text = f"{range_down}..{range_up}"
            else:
                range_text = "—"
            tp = f"{patch.take_profit_pct}" if patch.take_profit_pct is not None else "—"
            bias = patch.bias or "—"
            lines.append(
                f"Patch: step={step} range={range_text} levels={levels} tp={tp} bias={bias}"
            )
        else:
            lines.append("Patch: —")
        self._append_chat_block(lines)

    def _append_chat_block(self, lines: list[str]) -> None:
        if not lines:
            return
        if self._chat_history.toPlainText().strip():
            self._chat_history.appendPlainText("")
        for line in lines:
            self._chat_history.appendPlainText(line)

    def _restore_apply_plan_state(self) -> None:
        self._apply_plan_button.setEnabled(self._strategy_patch_has_values(self._last_strategy_patch))

    def _strategy_patch_has_values(self, patch: AiOperatorStrategyPatch | None) -> bool:
        if patch is None:
            return False
        values = [
            patch.budget,
            patch.bias,
            patch.levels,
            patch.step_pct,
            patch.range_down_pct,
            patch.range_up_pct,
            patch.take_profit_pct,
            patch.max_exposure,
        ]
        return any(value is not None for value in values)

    def _apply_strategy_patch_to_form(self, patch: AiOperatorStrategyPatch) -> None:
        if patch.bias:
            mapping = {
                "NEUTRAL": "Neutral",
                "LONG": "Long-biased",
                "SHORT": "Short-biased",
            }
            direction_value = mapping.get(patch.bias, "Neutral")
            index = self._direction_combo.findData(direction_value)
            if index >= 0:
                self._direction_combo.setCurrentIndex(index)
        if patch.budget is not None:
            self._budget_input.setValue(patch.budget)
        if patch.levels is not None:
            self._grid_count_input.setValue(patch.levels)
        if patch.step_pct is not None:
            self._grid_step_mode_combo.setCurrentIndex(self._grid_step_mode_combo.findData("MANUAL"))
            self._grid_step_input.setValue(patch.step_pct)
        if patch.range_down_pct is not None or patch.range_up_pct is not None:
            self._range_mode_combo.setCurrentIndex(self._range_mode_combo.findData("Manual"))
        if patch.range_down_pct is not None:
            self._range_low_input.setValue(patch.range_down_pct)
        if patch.range_up_pct is not None:
            self._range_high_input.setValue(patch.range_up_pct)
        if patch.take_profit_pct is not None:
            self._take_profit_input.setValue(patch.take_profit_pct)
        if patch.max_exposure is not None:
            if not self._max_exposure_warned:
                self._append_log("[AI] max_exposure ignored (UI field not present)", "INFO")
                self._max_exposure_warned = True

    def _update_fetch_data_button(self) -> None:
        if not hasattr(self, "_fetch_data_button"):
            return
        should_show = self._can_fetch_requested_data()
        self._fetch_data_button.setVisible(should_show)
        self._fetch_data_button.setEnabled(should_show and not self._ai_busy)
        if hasattr(self, "_ai_request_hint"):
            self._ai_request_hint.setVisible(should_show)

    def _can_fetch_requested_data(self) -> bool:
        if not self._last_ai_response:
            return False
        if self._data_request_resolved:
            return False
        actions = {action.upper() for action in self._last_ai_response.actions_suggested}
        if "REQUEST_MORE_DATA" not in actions:
            return False
        return any(item.strip() for item in self._last_ai_response.need_data)

    def _handle_fetch_requested_data(self) -> None:
        if not self._app_state.openai_key_present:
            self._append_log("[AI] fetch skipped: missing OpenAI key.", "WARN")
            self._append_chat_line("AI", "Не задан ключ OpenAI.")
            return
        if not self._can_fetch_requested_data():
            self._append_log("[AI] fetch skipped: no requested data.", "WARN")
            return
        requested = [item for item in self._last_ai_response.need_data if item]
        self._set_ai_busy(True)
        worker = _AiWorker(lambda: self._run_fetch_requested_data(requested))
        worker.signals.success.connect(self._handle_fetch_requested_data_success)
        worker.signals.error.connect(self._handle_fetch_requested_data_error)
        self._thread_pool.start(worker)
        self._append_log("[AI] requested data fetch started", "INFO")

    def _apply_request_more_data_policy(self, response: AiOperatorResponse) -> AiOperatorResponse:
        actions = [action.upper() for action in response.actions_suggested if action]
        if "REQUEST_MORE_DATA" in actions and self._data_request_resolved:
            actions = [action for action in actions if action != "REQUEST_MORE_DATA"]
            if "WAIT" not in actions:
                actions.append("WAIT")
            response.actions_suggested = actions
            response.need_data = []
            self._append_log("[AI] request_more_data suppressed (already resolved)", "INFO")
        return response

    def _run_fetch_requested_data(
        self,
        requested: list[str],
    ) -> dict[str, tuple[bool, str | None]]:
        results: dict[str, tuple[bool, str | None]] = {}
        if "orderbook_depth_50" in requested:
            results["orderbook_depth_50"] = self._fetch_orderbook_depth_cached()
        if "recent_trades_1m" in requested:
            results["recent_trades_1m"] = self._fetch_recent_trades_cached()
        return results

    def _handle_fetch_requested_data_success(self, payload: object) -> None:
        if not isinstance(payload, dict):
            self._handle_fetch_requested_data_error("Unexpected fetch response.")
            return
        results: dict[str, tuple[bool, str | None]] = payload
        _, orderbook_err = results.get("orderbook_depth_50", (False, None))
        _, trades_err = results.get("recent_trades_1m", (False, None))
        self._data_request_resolved = True
        if orderbook_err:
            self._append_log(f"[AI] orderbook fetch failed: {orderbook_err}", "WARN")
        if trades_err:
            self._append_log(f"[AI] trades fetch failed: {trades_err}", "WARN")
        snapshot = self._refresh_ai_snapshot(reason="fetch")
        datapack = self._build_ai_datapack(snapshot)
        orderbook_summary = datapack.get("orderbook_summary") or {}
        trades_summary = datapack.get("trades_1m_summary") or {}
        top_bid = orderbook_summary.get("best_bid")
        top_ask = orderbook_summary.get("best_ask")
        bid_count = orderbook_summary.get("bid_count")
        ask_count = orderbook_summary.get("ask_count")
        self._append_log(
            (
                "[AI] fetch depth50: "
                f"bids={bid_count} asks={ask_count} "
                f"top_bid={top_bid if isinstance(top_bid, (int, float)) else '—'} "
                f"top_ask={top_ask if isinstance(top_ask, (int, float)) else '—'}"
            ),
            "INFO",
        )
        vwap = trades_summary.get("vwap_1m")
        trades_count = trades_summary.get("count")
        vwap_text = f"{vwap:.6f}" if isinstance(vwap, (int, float)) else "—"
        self._append_log(
            f"[AI] fetch trades_1m: n={trades_count or 0} vwap={vwap_text}",
            "INFO",
        )
        bids = orderbook_summary.get("bid_count")
        asks = orderbook_summary.get("ask_count")
        spread_pct = orderbook_summary.get("spread_pct")
        trades_count = trades_summary.get("count")
        spread_text = f"{spread_pct:.4f}%" if isinstance(spread_pct, (int, float)) else "—"
        self._append_log(
            (
                "[AI] requested data fetched: "
                f"bids={bids} asks={asks} spread={spread_text} trades_1m={trades_count or 0}"
            ),
            "INFO",
        )
        self._append_chat_line(
            "AI",
            (
                "Получены данные: "
                f"bids={bids} asks={asks} spread={spread_text} trades_1m={trades_count or 0}"
            ),
        )
        self._update_ai_snapshot_label(datapack)
        self._log_ai_datapack_snapshot(datapack)
        signature = self._datapack_signature(datapack)
        if signature == self._last_ai_datapack_signature:
            self._set_ai_busy(False)
            self._append_log("[AI] follow-up skipped (dedup/no changes)", "INFO")
            self._update_fetch_data_button()
            return
        self._last_ai_datapack_signature = signature
        worker = _AiWorker(lambda: self._run_ai_follow_up(datapack))
        worker.signals.success.connect(self._handle_ai_follow_up_success)
        worker.signals.error.connect(self._handle_ai_follow_up_error)
        self._thread_pool.start(worker)
        self._append_log("[AI] follow-up sent", "INFO")

    def _handle_fetch_requested_data_error(self, message: str) -> None:
        self._set_ai_busy(False)
        self._data_request_resolved = True
        self._append_log(f"[AI] requested data fetch failed: {message}", "WARN")
        self._append_chat_line("AI", f"Ошибка AI: {message}")
        self._update_fetch_data_button()

    def _run_ai_follow_up(self, datapack: dict[str, Any]) -> str:
        client = OpenAIClient(
            api_key=self._app_state.openai_api_key,
            model=self._app_state.openai_model,
            timeout_s=25.0,
            retries=2,
        )
        return asyncio.run(
            client.analyze_operator(
                datapack,
                follow_up_note="Follow-up: attached requested data",
            )
        )

    def _handle_ai_follow_up_success(self, response: object) -> None:
        self._set_ai_busy(False)
        if not isinstance(response, str):
            self._handle_ai_follow_up_error("AI response invalid or empty.")
            return
        try:
            parsed = parse_ai_operator_response(response)
        except ValueError as exc:
            self._handle_ai_follow_up_error(str(exc))
            return
        parsed = self._apply_request_more_data_policy(parsed)
        self._last_ai_response = parsed
        self._last_ai_result_json = response
        self._last_strategy_patch = parsed.strategy_patch
        self._last_actions_suggested = list(parsed.actions_suggested)
        self._append_ai_response_to_chat(parsed)
        self._render_actions(parsed.actions_suggested)
        self._apply_plan_button.setEnabled(self._strategy_patch_has_values(parsed.strategy_patch))
        self._update_fetch_data_button()
        self._append_log("[AI] follow-up received", "INFO")

    def _handle_ai_follow_up_error(self, message: str) -> None:
        self._set_ai_busy(False)
        self._append_log(f"[AI] follow-up response invalid: {message}", "WARN")
        self._append_chat_line("AI", f"Ошибка AI: {message}")
        self._update_fetch_data_button()

    def _handle_start(self) -> None:
        self._runtime_stopping = False
        super()._handle_start()

    def _handle_stop(self) -> None:
        self._runtime_stopping = True
        super()._handle_stop()

    def _fetch_orderbook_depth_cached(self) -> tuple[bool, str | None]:
        cached = self._get_cached_data("orderbook_depth_50")
        if cached is not None:
            return True, None
        try:
            depth = self._http_client.get_orderbook_depth(self._symbol, limit=50)
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        bids = self._normalize_depth_side(depth.get("bids"))
        asks = self._normalize_depth_side(depth.get("asks"))
        payload = {
            "bids": bids,
            "asks": asks,
            "level_count": len(bids) + len(asks),
            "ts": utc_ms(),
        }
        self._data_cache.set(self._symbol, "orderbook_depth_50", payload)
        return True, None

    def _fetch_recent_trades_cached(self) -> tuple[bool, str | None]:
        cached = self._get_cached_data("recent_trades_1m")
        if cached is not None:
            return True, None
        try:
            trades = self._http_client.get_recent_trades(self._symbol, limit=500)
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        now_ms = utc_ms()
        cutoff_ms = now_ms - 60_000
        items: list[dict[str, Any]] = []
        for trade in trades:
            trade_ts = trade.get("time")
            if not isinstance(trade_ts, int):
                continue
            if trade_ts < cutoff_ms:
                continue
            items.append(
                {
                    "price": trade.get("price"),
                    "qty": trade.get("qty"),
                    "isBuyerMaker": trade.get("isBuyerMaker"),
                    "ts": trade_ts,
                }
            )
        payload = {
            "items": items,
            "count": len(items),
            "ts": now_ms,
        }
        self._data_cache.set(self._symbol, "recent_trades_1m", payload)
        return True, None

    def _get_cached_data(self, data_type: str) -> Any | None:
        cached = self._data_cache.get(self._symbol, data_type)
        if not cached:
            return None
        data, saved_at = cached
        ttl = self._cache_ttls.get(data_type)
        if ttl is None or not self._data_cache.is_fresh(saved_at, ttl):
            return None
        return data

    def _normalize_depth_side(self, raw: Any) -> list[list[float | str]]:
        if not isinstance(raw, list):
            return []
        normalized: list[list[float | str]] = []
        for item in raw:
            if not isinstance(item, list) or len(item) < 2:
                continue
            price = self._coerce_depth_value(item[0])
            qty = self._coerce_depth_value(item[1])
            normalized.append([price, qty])
        return normalized

    def _coerce_depth_value(self, value: Any) -> float | str:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return value
        return str(value)

    def _datapack_signature(self, datapack: dict[str, Any]) -> str:
        snapshot = datapack.get("snapshot") or {}
        payload = {
            "orderbook_summary": datapack.get("orderbook_summary"),
            "trades_1m_summary": datapack.get("trades_1m_summary"),
            "flags": snapshot.get("flags"),
            "fees": datapack.get("fees"),
            "snapshot_id": snapshot.get("snapshot_id"),
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def _log_ai_datapack_snapshot(self, datapack: dict[str, Any]) -> None:
        snapshot = datapack.get("snapshot") or {}
        flags = snapshot.get("flags") or {}
        has_orderbook = flags.get("has_orderbook")
        has_trades = flags.get("has_trades_1m")
        fees = datapack.get("fees") or {}
        is_zero_fee = fees.get("is_zero_fee")
        self._append_log(
            (
                "[AI] datapack snapshot: "
                f"has_orderbook={has_orderbook} has_trades_1m={has_trades} is_zero_fee={is_zero_fee}"
            ),
            "INFO",
        )

    def _handle_refresh_snapshot(self) -> None:
        snapshot = self._refresh_ai_snapshot(reason="refresh")
        datapack = self._build_ai_datapack(snapshot)
        self._update_ai_snapshot_label(datapack)
        self._append_log("[AI] snapshot refreshed", "INFO")

    def _get_ai_snapshot(self) -> MarketSnapshot | None:
        return self._market_snapshot

    def _next_snapshot_id(self) -> str:
        self._snapshot_sequence += 1
        return f"{self._symbol}-{utc_ms()}-{self._snapshot_sequence}"

    def _refresh_ai_snapshot(self, reason: str) -> MarketSnapshot:
        snapshot = self._create_market_snapshot()
        self._market_snapshot = snapshot
        bids_count = (
            len(snapshot.orderbook_depth_50.get("bids", []))
            if snapshot.orderbook_depth_50
            else 0
        )
        asks_count = (
            len(snapshot.orderbook_depth_50.get("asks", []))
            if snapshot.orderbook_depth_50
            else 0
        )
        trades_count = snapshot.trades_1m.get("count", 0) if snapshot.trades_1m else 0
        self._append_log(
            (
                f"[AI] snapshot({reason}) "
                f"id={snapshot.snapshot_id} bids={bids_count} "
                f"asks={asks_count} trades_1m={trades_count} "
                f"fee(m/t)={self._format_fee(snapshot.maker_fee_pct, snapshot.taker_fee_pct)} "
                f"is_zero_fee={snapshot.is_zero_fee} ws_age={snapshot.ws_age_ms}"
            ),
            "INFO",
        )
        return snapshot

    def _format_fee(self, maker_fee_pct: float | None, taker_fee_pct: float | None) -> str:
        if isinstance(maker_fee_pct, (int, float)) and isinstance(taker_fee_pct, (int, float)):
            return f"{maker_fee_pct:.4f}%/{taker_fee_pct:.4f}%"
        return "—"

    def _create_market_snapshot(self) -> MarketSnapshot:
        snapshot = self._price_feed_manager.get_snapshot(self._symbol)
        last_price = self._last_price or (snapshot.last_price if snapshot else None)
        best_bid = snapshot.best_bid if snapshot else None
        best_ask = snapshot.best_ask if snapshot else None
        spread_pct = snapshot.spread_pct if snapshot else None
        if spread_pct is None and best_bid and best_ask and best_bid > 0:
            spread_pct = (best_ask - best_bid) / best_bid * 100
        orderbook = self._get_cached_data("orderbook_depth_50")
        trades = self._get_cached_data("recent_trades_1m")
        maker_fee, taker_fee = self._trade_fees
        maker_fee_pct = maker_fee * 100 if maker_fee is not None else None
        taker_fee_pct = taker_fee * 100 if taker_fee is not None else None
        is_zero_fee = bool(
            maker_fee is not None
            and taker_fee is not None
            and maker_fee == 0
            and taker_fee == 0
        )
        base_asset = self._base_asset or ""
        quote_asset = self._quote_asset or ""
        base_free, base_locked = self._balances.get(base_asset, (0.0, 0.0))
        quote_free, quote_locked = self._balances.get(quote_asset, (0.0, 0.0))
        return MarketSnapshot(
            snapshot_id=self._next_snapshot_id(),
            symbol=self._symbol,
            ts_ms=utc_ms(),
            last_price=last_price,
            best_bid=best_bid,
            best_ask=best_ask,
            spread_pct=spread_pct,
            ws_age_ms=snapshot.price_age_ms if snapshot else None,
            latency_ms=snapshot.ws_latency_ms if snapshot else None,
            price_source=snapshot.source if snapshot else None,
            orderbook_depth_50=orderbook if isinstance(orderbook, dict) else None,
            trades_1m=trades if isinstance(trades, dict) else None,
            maker_fee_pct=maker_fee_pct,
            taker_fee_pct=taker_fee_pct,
            is_zero_fee=is_zero_fee,
            rules={
                "tickSize": self._exchange_rules.get("tick"),
                "stepSize": self._exchange_rules.get("step"),
                "minQty": self._exchange_rules.get("min_qty"),
                "minNotional": self._exchange_rules.get("min_notional"),
            },
            balances={
                "quote_free": quote_free,
                "base_free": base_free,
                "quote_locked": quote_locked,
                "base_locked": base_locked,
            },
            open_orders_count=len(self._open_orders),
        )

    def _prepare_ai_datapack(
        self,
        snapshot: MarketSnapshot,
        *,
        grid_settings: dict[str, Any],
        runtime: dict[str, Any],
        volatility: tuple[float | None, float | None],
        timestamp_utc: str,
    ) -> dict[str, Any]:
        last_price = snapshot.last_price
        best_bid = snapshot.best_bid
        best_ask = snapshot.best_ask
        spread_pct = snapshot.spread_pct
        atr_pct, micro_vol_pct = volatility
        maker_fee_pct = snapshot.maker_fee_pct
        taker_fee_pct = snapshot.taker_fee_pct
        is_zero_fee = snapshot.is_zero_fee
        orderbook = snapshot.orderbook_depth_50
        trades_1m = snapshot.trades_1m
        orderbook_summary = self._compute_orderbook_summary(orderbook, best_bid, best_ask)
        trades_summary = self._compute_trades_summary(trades_1m)
        has_orderbook = bool(orderbook_summary.get("is_valid"))
        has_trades_1m = (trades_summary.get("count") or 0) >= 3
        has_fees = maker_fee_pct is not None and taker_fee_pct is not None
        has_balances = bool(snapshot.balances)
        has_rules = bool(snapshot.rules)
        ws_age_ms = snapshot.ws_age_ms
        datapack_snapshot = {
            "snapshot_id": snapshot.snapshot_id,
            "ts_ms": snapshot.ts_ms,
            "price_last": last_price,
            "price_source": snapshot.price_source,
            "ws_age_ms": ws_age_ms,
            "fees": {
                "maker_fee_pct": maker_fee_pct,
                "taker_fee_pct": taker_fee_pct,
                "is_zero_fee": is_zero_fee,
            },
            "orderbook_summary": orderbook_summary,
            "trades_1m_summary": trades_summary,
            "balances": snapshot.balances,
            "open_orders_count": snapshot.open_orders_count,
            "rules": snapshot.rules,
            "flags": {
                "has_orderbook": has_orderbook,
                "has_trades_1m": has_trades_1m,
                "has_fees": has_fees,
                "has_balances": has_balances,
                "has_rules": has_rules,
                "has_price": last_price is not None,
            },
        }
        return {
            "symbol": self._symbol,
            "market": {
                "last_price": last_price,
                "bid": best_bid,
                "ask": best_ask,
                "spread_pct": spread_pct,
                "atr_pct": atr_pct,
                "micro_vol_pct": micro_vol_pct,
            },
            "orderbook_summary": orderbook_summary,
            "trades_1m_summary": trades_summary,
            "orderbook_depth_50": orderbook,
            "trades_1m": trades_1m,
            "snapshot": datapack_snapshot,
            "grid_settings": grid_settings,
            "runtime": runtime,
            "fees": {
                "maker_fee_pct": maker_fee_pct,
                "taker_fee_pct": taker_fee_pct,
                "is_zero_fee": is_zero_fee,
            },
            "timestamp_utc": timestamp_utc,
            "price_age_ms": snapshot.ws_age_ms,
            "latency_ms": snapshot.latency_ms,
        }

    def _build_ai_datapack(self, snapshot: MarketSnapshot) -> dict[str, Any]:
        runtime = {
            "state": self._state,
            "engine": self._engine_state,
            "trade_enabled": self._trade_gate == TradeGate.TRADE_OK,
            "trade_gate": self._trade_gate.value,
        }
        return self._prepare_ai_datapack(
            snapshot,
            grid_settings=self.dump_settings(),
            runtime=runtime,
            volatility=self._compute_volatility_metrics(),
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
        )

    def _compute_orderbook_summary(
        self,
        orderbook: Any,
        fallback_bid: float | None,
        fallback_ask: float | None,
    ) -> dict[str, float | int | None]:
        has_depth = isinstance(orderbook, dict)
        bids_raw = orderbook.get("bids") if has_depth else None
        asks_raw = orderbook.get("asks") if has_depth else None
        bids = bids_raw if isinstance(bids_raw, list) else []
        asks = asks_raw if isinstance(asks_raw, list) else []
        best_bid = None
        best_ask = None
        if bids:
            first_bid = bids[0]
            if isinstance(first_bid, list) and first_bid:
                best_bid = self._coerce_depth_value(first_bid[0])
        if asks:
            first_ask = asks[0]
            if isinstance(first_ask, list) and first_ask:
                best_ask = self._coerce_depth_value(first_ask[0])
        if isinstance(best_bid, str):
            best_bid = None
        if isinstance(best_ask, str):
            best_ask = None
        if best_bid is None:
            best_bid = fallback_bid
        if best_ask is None:
            best_ask = fallback_ask
        bid_count = len(bids) if has_depth else 0
        ask_count = len(asks) if has_depth else 0
        mid = None
        spread_pct = None
        is_valid = (
            bid_count >= 1
            and ask_count >= 1
            and isinstance(best_bid, (int, float))
            and isinstance(best_ask, (int, float))
            and best_bid > 0
            and best_ask > 0
            and best_ask >= best_bid
        )
        if is_valid:
            mid = (best_bid + best_ask) / 2
            spread_pct = (best_ask - best_bid) / best_bid * 100
        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "bid_count": bid_count,
            "ask_count": ask_count,
            "mid": mid,
            "spread_pct": spread_pct,
            "is_valid": is_valid,
        }

    def _compute_trades_summary(self, trades_1m: Any) -> dict[str, float | int | None]:
        if not isinstance(trades_1m, dict):
            return {"count": None, "last_ts": None, "vwap_1m": None}
        items = trades_1m.get("items")
        if not isinstance(items, list):
            return {"count": trades_1m.get("count"), "last_ts": None, "vwap_1m": None}
        vwap_numerator = 0.0
        vwap_denominator = 0.0
        last_ts = None
        count = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            price = item.get("price")
            qty = item.get("qty")
            ts = item.get("ts")
            try:
                price_val = float(price)
                qty_val = float(qty)
            except (TypeError, ValueError):
                continue
            vwap_numerator += price_val * qty_val
            vwap_denominator += qty_val
            count += 1
            if isinstance(ts, int):
                last_ts = ts if last_ts is None else max(last_ts, ts)
        vwap = vwap_numerator / vwap_denominator if vwap_denominator > 0 else None
        return {"count": count, "last_ts": last_ts, "vwap_1m": vwap}

    def _update_ai_snapshot_label(self, datapack: dict[str, Any]) -> None:
        orderbook_summary = datapack.get("orderbook_summary") or {}
        trades_summary = datapack.get("trades_1m_summary") or {}
        fees = datapack.get("fees") or {}
        snapshot_meta = datapack.get("snapshot", {})
        ws_age_ms = snapshot_meta.get("ws_age_ms")
        snapshot_id = snapshot_meta.get("snapshot_id", "—")
        bid = orderbook_summary.get("best_bid")
        ask = orderbook_summary.get("best_ask")
        spread_pct = orderbook_summary.get("spread_pct")
        trades_count = trades_summary.get("count")
        maker_fee_pct = fees.get("maker_fee_pct")
        taker_fee_pct = fees.get("taker_fee_pct")
        bid_text = f"{bid:.8f}" if isinstance(bid, (int, float)) else "—"
        ask_text = f"{ask:.8f}" if isinstance(ask, (int, float)) else "—"
        spread_text = f"{spread_pct:.4f}%" if isinstance(spread_pct, (int, float)) else "—"
        trades_text = str(trades_count or 0)
        fee_text = self._format_fee(maker_fee_pct, taker_fee_pct)
        ws_age_text = f"{ws_age_ms}ms" if isinstance(ws_age_ms, int) else "—"
        self._ai_snapshot_label.setText(
            (
                f"snapshot={snapshot_id} bid={bid_text} ask={ask_text} spread={spread_text} "
                f"trades1m={trades_text} fee(m/t)={fee_text} ws_age={ws_age_text}"
            )
        )

    def _apply_price_update(self, update: PriceUpdate) -> None:
        super()._apply_price_update(update)
        snapshot = self._get_ai_snapshot()
        if snapshot is None:
            return
        datapack = self._build_ai_datapack(snapshot)
        self._update_ai_snapshot_label(datapack)

    def _compute_volatility_metrics(self) -> tuple[float | None, float | None]:
        prices = self._price_history[-200:]
        if len(prices) < 2:
            return None, None
        returns: list[float] = []
        for idx in range(1, len(prices)):
            prev = prices[idx - 1]
            if prev <= 0:
                continue
            returns.append(abs(prices[idx] / prev - 1))
        if not returns:
            return None, None
        avg_return = sum(returns) / len(returns)
        micro_window = returns[-20:] if len(returns) >= 20 else returns
        micro_avg = sum(micro_window) / len(micro_window)
        return round(avg_return * 100, 6), round(micro_avg * 100, 6)

    def _place_limit(
        self,
        side: str,
        price: Decimal,
        qty: Decimal,
        client_id: str,
        reason: str,
        *,
        ignore_order_id: str | None = None,
        ignore_keys: set[str] | None = None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        if self._runtime_stopping:
            self._signals.log_append.emit("[LIVE] place skipped: stopping=true", "WARN")
            return None, None
        if not self._account_client:
            return None, "[LIVE] place skipped: no account client"
        tick = self._rule_decimal(self._exchange_rules.get("tick"))
        step = self._rule_decimal(self._exchange_rules.get("step"))
        min_notional = self._rule_decimal(self._exchange_rules.get("min_notional"))
        min_qty = self._rule_decimal(self._exchange_rules.get("min_qty"))
        max_qty = self._rule_decimal(self._exchange_rules.get("max_qty"))
        price = self.q_price(price, tick)
        qty = self.q_qty(qty, step)
        if min_qty is not None and qty < min_qty:
            self._signals.log_append.emit(
                (
                    "[LIVE] place skipped: minQty "
                    f"side={side} price={self.fmt_price(price, tick)} qty={self.fmt_qty(qty, step)} "
                    f"minQty={self.fmt_qty(min_qty, step)}"
                ),
                "WARN",
            )
            return None, None
        if max_qty is not None and qty > max_qty:
            qty = self.q_qty(max_qty, step)
        notional = price * qty
        if min_notional is not None and price > 0 and notional < min_notional:
            target_qty = self.ceil_to_step(min_notional / price, step)
            qty = target_qty
            if max_qty is not None and qty > max_qty:
                qty = self.q_qty(max_qty, step)
            notional = price * qty
            if (min_qty is not None and qty < min_qty) or notional < min_notional:
                self._signals.log_append.emit(
                    (
                        "[LIVE] place skipped: minNotional"
                        f" side={side} price={self.fmt_price(price, tick)} qty={self.fmt_qty(qty, step)} "
                        f"notional={self.fmt_price(notional, None)} minNotional={self.fmt_price(min_notional, None)}"
                    ),
                    "WARN",
                )
                return None, None
        if price <= 0 or qty <= 0:
            self._signals.log_append.emit(
                (
                    "[LIVE] place skipped: invalid "
                    f"side={side} price={self.fmt_price(price, tick)} qty={self.fmt_qty(qty, step)}"
                ),
                "WARN",
            )
            return None, None
        if not self._passes_balance_guard(side, price, qty):
            return None, None
        key = self._order_key(side, price, qty)
        if self._has_duplicate_order(
            side,
            price,
            qty,
            tolerance_ticks=0,
            ignore_order_id=ignore_order_id,
            ignore_keys=ignore_keys,
        ):
            self._signals.log_append.emit(
                (
                    f"[LIVE] SKIP duplicate key {key} "
                    f"side={side} price={self.fmt_price(price, tick)} qty={self.fmt_qty(qty, step)}"
                ),
                "WARN",
            )
            return None, None
        log_message = (
            f"[LIVE] place {reason} side={side} price={self.fmt_price(price, tick)} qty={self.fmt_qty(qty, step)} "
            f"notional={self.fmt_price(notional, None)} tick={self._format_rule(tick)} "
            f"step={self._format_rule(step)} minNotional={self._format_rule(min_notional)}"
        )
        self._signals.log_append.emit(log_message, "ORDERS")
        try:
            response = self._account_client.place_limit_order(
                symbol=self._symbol,
                side=side,
                price=self.fmt_price(price, tick),
                quantity=self.fmt_qty(qty, step),
                time_in_force="GTC",
                new_client_order_id=client_id,
            )
        except Exception as exc:  # noqa: BLE001
            status, code, message, response_body = self._parse_binance_exception(exc)
            if code == -2010:
                reason_tag = self._classify_2010_reason(message)
                if reason_tag == "DUPLICATE":
                    self._signals.log_append.emit(
                        (
                            "[LIVE] duplicate order skipped "
                            f"status={status} code={code} msg={message} response={response_body}"
                        ),
                        "WARN",
                    )
                    return None, None
                if reason_tag == "INSUFFICIENT_BALANCE":
                    self._signals.log_append.emit(
                        (
                            "[LIVE] order rejected: reason=INSUFFICIENT_BALANCE "
                            f"status={status} code={code} msg={message} response={response_body}"
                        ),
                        "WARN",
                    )
                    return None, None
                self._signals.log_append.emit(
                    (
                        "[LIVE] order rejected: reason=UNKNOWN_2010 "
                        f"status={status} code={code} msg={message} response={response_body}"
                    ),
                    "WARN",
                )
                return None, None
            message = self._format_binance_exception(
                exc,
                context=f"place {reason}",
                side=side,
                price=price,
                qty=qty,
                notional=notional,
            )
            return None, message
        return response, None

    def _passes_balance_guard(self, side: str, price: Decimal, qty: Decimal) -> bool:
        base_asset = self._base_asset
        quote_asset = self._quote_asset
        base_free = 0.0
        quote_free = 0.0
        if base_asset:
            base_free, _ = self._balances.get(base_asset, (0.0, 0.0))
        if quote_asset:
            quote_free, _ = self._balances.get(quote_asset, (0.0, 0.0))
        taker_fee = self._trade_fees[1] or 0.0
        required_quote = float(price * qty) * (1 + taker_fee)
        safety_quote = max(required_quote * 0.005, 1.0)
        if side.upper() == "BUY":
            if quote_free < required_quote + safety_quote:
                self._signals.log_append.emit(
                    (
                        "[LIVE] order rejected: reason=INSUFFICIENT_BALANCE "
                        f"side=BUY required={required_quote:.6f} {quote_asset} "
                        f"free={quote_free:.6f} buffer={safety_quote:.6f}"
                    ),
                    "WARN",
                )
                return False
            return True
        required_base = float(qty)
        min_base_buffer = (1.0 / float(price)) if price > 0 else 0.0
        safety_base = max(required_base * 0.005, min_base_buffer)
        if base_free < required_base + safety_base:
            self._signals.log_append.emit(
                (
                    "[LIVE] order rejected: reason=INSUFFICIENT_BALANCE "
                    f"side=SELL required={required_base:.6f} {base_asset} "
                    f"free={base_free:.6f} buffer={safety_base:.6f}"
                ),
                "WARN",
            )
            return False
        return True

    @staticmethod
    def _classify_2010_reason(message: str) -> str:
        lowered = (message or "").lower()
        if "duplicate" in lowered:
            return "DUPLICATE"
        if "insufficient balance" in lowered:
            return "INSUFFICIENT_BALANCE"
        return "UNKNOWN_2010"
