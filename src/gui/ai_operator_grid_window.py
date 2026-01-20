from __future__ import annotations

import asyncio
import time
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
    QVBoxLayout,
    QWidget,
)

from src.ai.openai_client import OpenAIClient
from src.ai.operator_models import AiOperatorResponse, AiOperatorStrategyPatch, parse_ai_operator_response
from src.core.config import Config
from src.gui.lite_grid_window import LiteGridWindow, TradeGate
from src.gui.models.app_state import AppState
from src.services.price_feed_manager import PriceFeedManager


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

    def _build_body(self) -> QSplitter:
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)

        left_stack = QSplitter(Qt.Vertical)
        left_stack.setChildrenCollapsible(False)
        left_stack.addWidget(self._build_ai_panel())
        left_stack.addWidget(super()._build_market_panel())
        left_stack.setStretchFactor(0, 1)
        left_stack.setStretchFactor(1, 1)

        splitter.addWidget(left_stack)
        splitter.addWidget(super()._build_grid_panel())
        splitter.addWidget(super()._build_runtime_panel())
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
        layout = QVBoxLayout(group)
        layout.setSpacing(4)

        top_actions = QHBoxLayout()
        self._analyze_button = QPushButton("AI Analyze")
        self._analyze_button.clicked.connect(self._handle_ai_analyze)
        top_actions.addWidget(self._analyze_button)

        self._apply_plan_button = QPushButton("Apply Plan")
        self._apply_plan_button.setEnabled(False)
        self._apply_plan_button.clicked.connect(self._apply_ai_plan)
        top_actions.addWidget(self._apply_plan_button)
        top_actions.addStretch()
        layout.addLayout(top_actions)

        self._chat_history = QPlainTextEdit()
        self._chat_history.setReadOnly(True)
        self._chat_history.setPlaceholderText("AI chat history will appear here.")
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

    def _handle_ai_analyze(self) -> None:
        if not self._app_state.openai_key_present:
            self._append_log("[AI] analyze skipped: missing OpenAI key.", "WARN")
            self._append_chat_line("AI", "Не задан ключ OpenAI.")
            return
        datapack = self._build_ai_datapack()
        self._append_chat_line("YOU", "AI Analyze")
        self._append_log("[AI] datapack prepared", "INFO")
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
        self._last_ai_response = parsed
        self._last_ai_result_json = response
        self._last_strategy_patch = parsed.strategy_patch
        self._last_actions_suggested = list(parsed.actions_suggested)
        self._append_ai_response_to_chat(parsed)
        self._render_actions(parsed.actions_suggested)
        self._apply_plan_button.setEnabled(self._strategy_patch_has_values(parsed.strategy_patch))
        self._append_log("[AI] response received", "INFO")

    def _handle_ai_analyze_error(self, message: str) -> None:
        self._set_ai_busy(False)
        self._append_log(f"[AI] response invalid: {message}", "WARN")
        self._append_chat_line("AI", f"Ошибка AI: {message}")
        self._actions_combo.clear()
        self._apply_plan_button.setEnabled(False)
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
        datapack = self._build_ai_datapack()
        current_ui_params = self.dump_settings()
        self._set_ai_busy(True)
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
        self._last_ai_response = parsed
        self._last_ai_result_json = response
        self._last_strategy_patch = parsed.strategy_patch
        self._last_actions_suggested = list(parsed.actions_suggested)
        self._append_ai_response_to_chat(parsed)
        self._render_actions(parsed.actions_suggested)
        self._apply_plan_button.setEnabled(self._strategy_patch_has_values(parsed.strategy_patch))
        self._append_log("[AI] chat response received", "INFO")

    def _handle_ai_chat_error(self, message: str) -> None:
        self._set_ai_busy(False)
        self._append_log(f"[AI] chat response invalid: {message}", "WARN")
        self._append_chat_line("AI", f"Ошибка AI: {message}")

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
        self._analyze_button.setEnabled(not busy)
        self._chat_input.setEnabled(not busy)
        self._send_button.setEnabled(not busy)

    def _append_chat_line(self, author: str, message: str) -> None:
        if not message:
            return
        prefix = "Ты" if author.upper() in {"YOU", "USER", "ТЫ"} else "AI"
        self._append_chat_block([f"{prefix}: {message}"])

    def _append_ai_response_to_chat(self, response: AiOperatorResponse) -> None:
        summary = response.analysis_result.summary or "—"
        lines = [f"AI:"]
        lines.append(f"Summary: {summary}")
        lines.append(f"State: {response.analysis_result.state}")
        risks = response.analysis_result.risks[:3]
        risks_text = "; ".join(risks) if risks else "—"
        lines.append(f"Risks: {risks_text}")
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
                f"Plan: step={step} range={range_text} levels={levels} tp={tp} bias={bias}"
            )
        actions = ", ".join(response.actions_suggested) if response.actions_suggested else "—"
        lines.append(f"Actions: {actions}")
        if response.need_data:
            lines.append(f"Нужно: {', '.join(response.need_data)}")
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

    def _build_ai_datapack(self) -> dict[str, Any]:
        snapshot = self._price_feed_manager.get_snapshot(self._symbol)
        last_price = self._last_price or (snapshot.last_price if snapshot else None)
        best_bid = snapshot.best_bid if snapshot else None
        best_ask = snapshot.best_ask if snapshot else None
        spread_pct = snapshot.spread_pct if snapshot else None
        if spread_pct is None and best_bid and best_ask and best_bid > 0:
            spread_pct = (best_ask - best_bid) / best_bid * 100
        atr_pct, micro_vol_pct = self._compute_volatility_metrics()
        maker_fee, taker_fee = self._trade_fees
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
        orderbook_snapshot = {
            "bids": [{"price": best_bid, "qty": None}] if best_bid is not None else [],
            "asks": [{"price": best_ask, "qty": None}] if best_ask is not None else [],
            "depth": 1,
            "source": snapshot.source if snapshot else None,
        }
        orderbook_top_count = len(orderbook_snapshot["bids"]) + len(orderbook_snapshot["asks"])
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
            "orderbook_snapshot": orderbook_snapshot,
            "rules": {
                "tick": self._exchange_rules.get("tick"),
                "step": self._exchange_rules.get("step"),
                "min_qty": self._exchange_rules.get("min_qty"),
                "min_notional": self._exchange_rules.get("min_notional"),
            },
            "balances": {
                "quote": {
                    "asset": quote_asset,
                    "free": quote_free,
                    "used": quote_locked,
                },
                "base": {
                    "asset": base_asset,
                    "free": base_free,
                    "used": base_locked,
                },
            },
            "grid_settings": self.dump_settings(),
            "runtime": {
                "state": self._state,
                "engine": self._engine_state,
                "trade_enabled": self._trade_gate == TradeGate.TRADE_OK,
                "trade_gate": self._trade_gate.value,
            },
            "fees": {
                "maker": maker_fee,
                "taker": taker_fee,
                "maker_fee": maker_fee,
                "taker_fee": taker_fee,
                "is_zero_fee": is_zero_fee,
            },
            "liquidity_hints": {
                "orderbook_topN_count": orderbook_top_count,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread_pct": spread_pct,
            },
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "price_age_ms": snapshot.price_age_ms if snapshot else None,
            "latency_ms": snapshot.ws_latency_ms if snapshot else None,
        }

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
