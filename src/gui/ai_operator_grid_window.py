from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
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
        self._last_strategy_patch: AiOperatorStrategyPatch | None = None

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

        self._analysis_state_label = QLabel("State: —")
        self._analysis_state_label.setStyleSheet("font-weight: 600;")
        layout.addWidget(self._analysis_state_label)

        layout.addWidget(QLabel("Summary"))
        self._analysis_summary = QPlainTextEdit()
        self._analysis_summary.setReadOnly(True)
        self._analysis_summary.setPlaceholderText("AI summary will appear here.")
        layout.addWidget(self._analysis_summary, stretch=1)

        layout.addWidget(QLabel("Risks"))
        self._analysis_risks = QPlainTextEdit()
        self._analysis_risks.setReadOnly(True)
        self._analysis_risks.setPlaceholderText("AI risks will appear here.")
        layout.addWidget(self._analysis_risks)

        layout.addWidget(QLabel("Suggested actions"))
        self._actions_list = QListWidget()
        self._actions_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self._actions_list.itemSelectionChanged.connect(self._update_approve_button_state)
        layout.addWidget(self._actions_list)

        bottom_actions = QHBoxLayout()
        self._approve_button = QPushButton("Approve")
        self._approve_button.setEnabled(False)
        self._approve_button.clicked.connect(self._approve_ai_action)
        bottom_actions.addWidget(self._approve_button)

        pause_button = QPushButton("Pause (AI)")
        pause_button.clicked.connect(self._handle_pause)
        bottom_actions.addWidget(pause_button)
        bottom_actions.addStretch()
        layout.addLayout(bottom_actions)

        return group

    def _handle_ai_analyze(self) -> None:
        if not self._app_state.openai_key_present:
            self._append_log("[AI] analyze skipped: missing OpenAI key.", "WARN")
            self._show_ai_error("Не задан ключ OpenAI.")
            return
        datapack = self._build_ai_datapack()
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
        self._last_strategy_patch = parsed.strategy_patch
        self._analysis_state_label.setText(f"State: {parsed.analysis_result.state}")
        self._analysis_summary.setPlainText(parsed.analysis_result.summary or "—")
        risks_text = "\n".join(parsed.analysis_result.risks) if parsed.analysis_result.risks else "—"
        self._analysis_risks.setPlainText(risks_text)
        self._render_actions(parsed.actions_suggested)
        self._apply_plan_button.setEnabled(self._strategy_patch_has_values(parsed.strategy_patch))
        self._append_log("[AI] response received", "INFO")

    def _handle_ai_analyze_error(self, message: str) -> None:
        self._set_ai_busy(False)
        self._append_log(f"[AI] response invalid: {message}", "WARN")
        self._show_ai_error(message)

    def _apply_ai_plan(self) -> None:
        if not self._last_strategy_patch or not self._strategy_patch_has_values(self._last_strategy_patch):
            self._append_log("[AI] apply plan skipped: no strategy patch.", "WARN")
            return
        self._apply_strategy_patch_to_form(self._last_strategy_patch)
        self._append_log("[AI] strategy_patch applied", "INFO")

    def _approve_ai_action(self) -> None:
        item = self._actions_list.currentItem()
        if item is None:
            self._append_log("[AI] approve skipped: no action selected.", "WARN")
            return
        action = item.text().strip().upper()
        if action == "START":
            self._handle_start()
        elif action == "REBUILD_GRID":
            self._handle_stop()
            self._handle_start()
        elif action == "PAUSE":
            self._handle_pause()
        elif action == "WAIT":
            pass
        else:
            self._append_log(f"[AI] approve ignored: unknown action {action}.", "WARN")
            return
        self._append_log(f"[AI] approved action: {action}", "INFO")

    def _render_actions(self, actions: list[str]) -> None:
        self._actions_list.clear()
        for action in actions:
            if not action:
                continue
            self._actions_list.addItem(action)
        if self._actions_list.count() > 0:
            self._actions_list.setCurrentRow(0)
        self._update_approve_button_state()

    def _update_approve_button_state(self) -> None:
        self._approve_button.setEnabled(self._actions_list.currentItem() is not None)

    def _set_ai_busy(self, busy: bool) -> None:
        self._analyze_button.setEnabled(not busy)

    def _show_ai_error(self, message: str) -> None:
        self._analysis_state_label.setText("State: ERROR")
        self._analysis_summary.setPlainText(message)
        self._analysis_risks.setPlainText("—")
        self._actions_list.clear()
        self._apply_plan_button.setEnabled(False)
        self._update_approve_button_state()

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
            self._append_log("[AI] max_exposure provided but not applied in Lite UI.", "WARN")

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
