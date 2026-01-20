from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
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
from src.services.price_feed_manager import (
    PriceFeedManager,
    PriceUpdate,
    WS_CONNECTED,
    WS_DEGRADED,
    WS_DEGRADED_AFTER_MS,
)
from src.services.rate_limiter import RateLimiter


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
    source: str
    age_ms: int | None
    stale: bool = False


@dataclass
class SnapshotStore:
    snapshot_analyze: MarketSnapshot | None = None
    snapshot_fetched: MarketSnapshot | None = None
    snapshot_active: MarketSnapshot | None = None


@dataclass
class MarketSnapshotCache:
    last_good_snapshot: MarketSnapshot | None = None


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
        self._last_ai_result_id: str | None = None
        self._last_applied_ai_result_id: str | None = None
        self._last_approve_ts: float = 0.0
        self._last_apply_ts: float = 0.0
        self._ai_busy = False
        self._runtime_stopping = False
        self._last_full_pack: dict[str, Any] | None = None
        self._last_strategy_patch: AiOperatorStrategyPatch | None = None
        self._last_actions_suggested: list[str] = []
        self._max_exposure_warned = False
        self._data_cache = DataCache()
        self._http_client = BinanceHttpClient(
            base_url=self._config.binance.base_url,
            timeout_s=self._config.http.timeout_s,
            retries=self._config.http.retries,
            backoff_base_s=self._config.http.backoff_base_s,
            backoff_max_s=self._config.http.backoff_max_s,
        )
        self._rate_limiter = RateLimiter()

        self._last_ai_datapack_signature: str | None = None
        self._snapshot_store = SnapshotStore()
        self._snapshot_cache = MarketSnapshotCache()
        self._snapshot_sequence = 0
        self._user_intent: dict[str, Any] = {}
        self._cache_ttls = {
            "exchange_info": 3600.0,
            "book_ticker": 2.0,
            "ticker_24h": 10.0,
            "orderbook_depth_50": 3.0,
            "recent_trades_1m": 3.0,
            "klines_1m": 60.0,
            "klines_15m": 60.0,
            "klines_1h": 60.0,
            "klines_1d": 60.0,
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

        top_actions.addStretch()
        layout.addLayout(top_actions)

        profile_row = QHBoxLayout()
        profile_row.addWidget(QLabel("Profile"))
        self._profile_combo = QComboBox()
        self._profile_combo.addItem("Aggressive", "AGGRESSIVE")
        self._profile_combo.addItem("Balanced", "BALANCED")
        self._profile_combo.addItem("Conservative", "CONSERVATIVE")
        self._profile_combo.currentIndexChanged.connect(self._update_apply_plan_state)
        profile_row.addWidget(self._profile_combo, stretch=1)
        layout.addLayout(profile_row)


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
        frame.setMinimumHeight(240)
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
        datapack = self._build_full_market_pack()
        self._append_chat_line("YOU", "AI Analyze")
        self._append_log("[AI] fullpack prepared", "INFO")
        self._log_ai_fullpack_snapshot(datapack)
        self._update_ai_snapshot_label(datapack)
        self._last_full_pack = datapack
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
        parsed = parse_ai_operator_response(response)
        parsed = self._enforce_action_patch_requirements(parsed)
        self._store_ai_result(parsed, response)
        self._append_ai_response_to_chat(parsed)
        self._render_actions(parsed.actions)
        self._apply_plan_button.setEnabled(self._can_apply_ai_patch(parsed.strategy_patch))
        self._append_log("[AI] response received", "INFO")

    def _handle_ai_analyze_error(self, message: str) -> None:
        self._set_ai_busy(False)
        self._append_log(f"[AI] response invalid: {message}", "WARN")
        self._append_chat_line("AI", f"Ошибка AI: {message}")
        self._last_ai_response = None
        self._last_ai_result_json = None
        self._last_ai_result_id = None
        self._last_applied_ai_result_id = None
        self._actions_combo.clear()
        self._apply_plan_button.setEnabled(False)
        self._update_approve_button_state()

    def _apply_ai_plan(self) -> None:
        patch = self._get_selected_profile_patch()
        if not patch or not self._strategy_patch_has_values(patch):
            self._append_log("[AI] patch empty, nothing to apply", "INFO")
            self._append_chat_line("AI", "patch empty, nothing to apply")
            return
        if self._last_ai_result_id and self._last_ai_result_id == self._last_applied_ai_result_id:
            self._append_log("[AI] patch already applied for latest result.", "INFO")
            self._apply_plan_button.setEnabled(False)
            return
        now = time.monotonic()
        if now - self._last_apply_ts < 0.3:
            return
        self._last_apply_ts = now
        self._apply_plan_button.setEnabled(False)
        QTimer.singleShot(400, self._restore_apply_plan_state)
        apply_to_form = getattr(self, "_apply_strategy_patch_to_form", None)
        if callable(apply_to_form):
            apply_to_form(patch)
        else:
            self._append_log("[AI] apply patch skipped: form handler missing", "WARN")
        self._apply_strategy_patch_to_ui(patch)
        self._append_log("[AI] strategy_patch applied", "INFO")
        self._append_chat_line("AI", "Strategy patch applied via Apply Plan.")
        self._last_applied_ai_result_id = self._last_ai_result_id

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
        self._update_user_intent_from_message(message)
        snapshot = self._get_ai_snapshot()
        if snapshot is None:
            snapshot = self._refresh_ai_snapshot(reason="chat")
        datapack = self._build_ai_datapack(snapshot)
        current_ui_params = self.dump_settings()
        self._set_ai_busy(True)
        self._log_ai_fullpack_snapshot(datapack)
        self._last_full_pack = datapack
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
        parsed = parse_ai_operator_response(response)
        parsed = self._enforce_action_patch_requirements(parsed)
        self._store_ai_result(parsed, response)
        self._append_ai_response_to_chat(parsed)
        self._render_actions(parsed.actions)
        self._apply_plan_button.setEnabled(self._can_apply_ai_patch(parsed.strategy_patch))
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

    def _clear_actions(self) -> None:
        self._actions_combo.clear()
        self._update_approve_button_state()

    def _apply_recommended_profile(self, recommended: str) -> None:
        index = self._profile_combo.findData(recommended)
        if index >= 0:
            self._profile_combo.setCurrentIndex(index)

    def _get_selected_profile_patch(self) -> AiOperatorStrategyPatch | None:
        if not self._last_ai_response:
            return None
        key = self._profile_combo.currentData()
        if not isinstance(key, str):
            key = self._last_ai_response.recommended_profile
        profile = self._last_ai_response.profiles.get(key)
        return profile.strategy_patch if profile else None

    def _update_apply_plan_state(self) -> None:
        if not hasattr(self, "_apply_plan_button"):
            return
        patch = self._get_selected_profile_patch()
        self._apply_plan_button.setEnabled(self._can_apply_ai_patch(patch) and not self._ai_busy)

    def _set_ai_busy(self, busy: bool) -> None:
        self._ai_busy = busy
        self._analyze_button.setEnabled(not busy)
        self._chat_input.setEnabled(not busy)
        self._send_button.setEnabled(not busy)
        self._update_apply_plan_state()


    def _append_chat_line(self, author: str, message: str) -> None:
        if not message:
            return
        prefix = "Ты" if author.upper() in {"YOU", "USER", "ТЫ"} else "AI"
        self._append_chat_block([f"{prefix}: {message}"])

    def _append_ai_response_to_chat(self, response: AiOperatorResponse) -> None:
        lines = ["AI:"]
        lines.append(f"State: {response.state}")
        lines.append(f"Reason: {response.reason_short or '—'}")
        lines.append(f"Profile: {response.recommended_profile}")
        actions_text = ", ".join(response.actions) if response.actions else "—"
        lines.append(f"Actions: {actions_text}")
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
            tp = f"{patch.tp_pct}" if patch.tp_pct is not None else "—"
            bias = patch.bias or "—"
            lines.append(f"Patch: step={step} range={range_text} levels={levels} tp={tp} bias={bias}")
        else:
            lines.append("Patch: —")
        forecast = response.forecast
        lines.append(
            f"Forecast: {forecast.bias} {forecast.confidence:.2f} ({forecast.horizon_min}m) "
            f"{forecast.comment or '—'}"
        )
        risks_text = "; ".join(response.risks[:4]) if response.risks else "—"
        lines.append(f"Risks: {risks_text}")
        self._append_chat_block(lines)

    def _append_chat_block(self, lines: list[str]) -> None:
        if not lines:
            return
        if self._chat_history.toPlainText().strip():
            self._chat_history.appendPlainText("")
        for line in lines:
            self._chat_history.appendPlainText(line)

    def _restore_apply_plan_state(self) -> None:
        patch = self._get_selected_profile_patch()
        self._apply_plan_button.setEnabled(self._can_apply_ai_patch(patch))

    def _can_apply_ai_patch(self, patch: AiOperatorStrategyPatch | None) -> bool:
        if not self._strategy_patch_has_values(patch):
            return False
        if self._last_ai_result_id and self._last_ai_result_id == self._last_applied_ai_result_id:
            return False
        return True

    def _store_ai_result(self, response: AiOperatorResponse, raw_json: str) -> None:
        response_id = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()
        self._last_ai_response = response
        self._last_ai_result_json = raw_json
        self._last_ai_result_id = response_id
        self._last_applied_ai_result_id = None
        self._last_strategy_patch = response.strategy_patch
        self._last_actions_suggested = list(response.actions)

    def _strategy_patch_has_values(self, patch: AiOperatorStrategyPatch | None) -> bool:
        if patch is None:
            return False
        values = [
            patch.bias,
            patch.step_pct,
            patch.range_down_pct,
            patch.range_up_pct,
            patch.levels,
            patch.tp_pct,
            patch.max_active_orders,
        ]
        return any(value is not None for value in values)

    def _enforce_action_patch_requirements(self, response: AiOperatorResponse) -> AiOperatorResponse:
        actions = [action.upper() for action in response.actions if action]
        requires_patch = any(action in {"START", "REBUILD_GRID"} for action in actions)
        patch = response.strategy_patch
        has_patch = self._strategy_patch_has_values(patch)
        intent_required = bool(self._user_intent)
        if requires_patch:
            action = next((item for item in actions if item in {"START", "REBUILD_GRID"}), "START")
            if has_patch:
                self._append_log(f"[AI] action={action} requires patch -> ok", "INFO")
            else:
                self._append_log(f"[AI] action={action} requires patch -> invalid", "WARN")
                actions = [item for item in actions if item not in {"START", "REBUILD_GRID"}]
                if "WAIT" not in actions:
                    actions.append("WAIT")
        if intent_required and not has_patch:
            self._append_log(
                f"[AI] user_intent requires patch -> invalid intent={list(self._user_intent.keys())}",
                "WARN",
            )
            actions = [item for item in actions if item not in {"START", "REBUILD_GRID"}]
            if "WAIT" not in actions:
                actions.append("WAIT")
        response.actions = actions
        return response

    def _update_user_intent_from_message(self, message: str) -> None:
        lowered = message.lower()
        updates: dict[str, float | int] = {}
        budget = self._extract_keyword_number(lowered, r"бюджет")
        if budget is not None:
            updates["budget_usdt"] = float(budget)
        step = self._extract_keyword_number(lowered, r"(шаг|step)")
        if step is not None:
            updates["step_pct"] = float(step)
        tp = self._extract_keyword_number(lowered, r"(tp|тейк|take)")
        if tp is not None:
            updates["take_profit_pct"] = float(tp)
        levels = self._extract_keyword_number(lowered, r"(уровн|levels)")
        if levels is not None:
            updates["levels"] = int(levels)
        if updates:
            self._user_intent.update(updates)

    @staticmethod
    def _extract_keyword_number(text: str, keyword_pattern: str) -> float | None:
        match = re.search(rf"{keyword_pattern}\\D*(\\d+(?:[.,]\\d+)?)", text)
        if not match:
            return None
        value = match.group(1).replace(",", ".")
        try:
            return float(value)
        except ValueError:
            return None

    def _apply_strategy_patch_to_ui(self, patch: AiOperatorStrategyPatch) -> None:
        applied_fields: list[str] = []
        missing_fields: list[str] = []
        if patch.bias:
            if not hasattr(self, "_direction_combo"):
                self._append_log("[AI] patch field skipped: bias", "INFO")
                missing_fields.append("bias")
            else:
                mapping = {
                    "NEUTRAL": "Neutral",
                    "FLAT": "Neutral",
                    "LONG": "Long-biased",
                    "UP": "Long-biased",
                    "SHORT": "Short-biased",
                    "DOWN": "Short-biased",
                }
                direction_value = mapping.get(patch.bias, "Neutral")
                index = self._direction_combo.findData(direction_value)
                if index >= 0:
                    self._direction_combo.setCurrentIndex(index)
                    applied_fields.append("bias")
        if patch.levels is not None:
            if not hasattr(self, "_grid_count_input"):
                self._append_log("[AI] patch field skipped: levels", "INFO")
                missing_fields.append("levels")
            else:
                self._grid_count_input.setValue(patch.levels)
                applied_fields.append("levels")
        if patch.budget is not None:
            if not hasattr(self, "_budget_input"):
                self._append_log("[AI] patch field skipped: budget", "INFO")
                missing_fields.append("budget")
            else:
                self._budget_input.setValue(patch.budget)
                applied_fields.append("budget")
        if patch.step_pct is not None:
            if not hasattr(self, "_grid_step_mode_combo") or not hasattr(self, "_grid_step_input"):
                self._append_log("[AI] patch field skipped: step_pct", "INFO")
                missing_fields.append("step_pct")
            else:
                self._grid_step_mode_combo.setCurrentIndex(self._grid_step_mode_combo.findData("MANUAL"))
                self._grid_step_input.setValue(patch.step_pct)
                applied_fields.append("step_pct")
        if patch.range_down_pct is not None or patch.range_up_pct is not None:
            if not hasattr(self, "_range_mode_combo"):
                self._append_log("[AI] patch field skipped: range_mode", "INFO")
                missing_fields.append("range_mode")
            else:
                self._range_mode_combo.setCurrentIndex(self._range_mode_combo.findData("Manual"))
                applied_fields.append("range_mode")
        if patch.range_down_pct is not None:
            if not hasattr(self, "_range_low_input"):
                self._append_log("[AI] patch field skipped: range_down_pct", "INFO")
                missing_fields.append("range_down_pct")
            else:
                self._range_low_input.setValue(patch.range_down_pct)
                applied_fields.append("range_down_pct")
        if patch.range_up_pct is not None:
            if not hasattr(self, "_range_high_input"):
                self._append_log("[AI] patch field skipped: range_up_pct", "INFO")
                missing_fields.append("range_up_pct")
            else:
                self._range_high_input.setValue(patch.range_up_pct)
                applied_fields.append("range_up_pct")
        if patch.tp_pct is not None:
            if not hasattr(self, "_take_profit_input"):
                self._append_log("[AI] patch field skipped: tp_pct", "INFO")
                missing_fields.append("tp_pct")
            else:
                self._take_profit_input.setValue(patch.tp_pct)
                applied_fields.append("tp_pct")
        if patch.max_active_orders is not None:
            if not hasattr(self, "_max_orders_input"):
                self._append_log("[AI] patch field skipped: max_active_orders", "INFO")
                missing_fields.append("max_active_orders")
            else:
                self._max_orders_input.setValue(patch.max_active_orders)
                applied_fields.append("max_active_orders")
        self._append_log(
            f"[AI] ui_patch_applied fields={applied_fields} missing={missing_fields}",
            "INFO",
        )

    def _apply_strategy_patch_to_form(self, patch: AiOperatorStrategyPatch) -> None:
        if patch.bias:
            mapping = {
                "NEUTRAL": "Neutral",
                "FLAT": "Neutral",
                "LONG": "Long-biased",
                "UP": "Long-biased",
                "SHORT": "Short-biased",
                "DOWN": "Short-biased",
            }
            direction_value = mapping.get(patch.bias, "Neutral")
            self._update_setting("direction", direction_value)
        if patch.levels is not None:
            self._update_setting("grid_count", patch.levels)
        if patch.budget is not None:
            self._update_setting("budget", patch.budget)
        if patch.step_pct is not None:
            self._update_setting("grid_step_mode", "MANUAL")
            self._update_setting("grid_step_pct", patch.step_pct)
        if patch.range_down_pct is not None or patch.range_up_pct is not None:
            self._update_setting("range_mode", "Manual")
        if patch.range_down_pct is not None:
            self._update_setting("range_low_pct", patch.range_down_pct)
        if patch.range_up_pct is not None:
            self._update_setting("range_high_pct", patch.range_up_pct)
        if patch.tp_pct is not None:
            self._update_setting("take_profit_pct", patch.tp_pct)
        if patch.max_active_orders is not None:
            self._update_setting("max_active_orders", patch.max_active_orders)

    def _handle_start(self) -> None:
        self._runtime_stopping = False
        super()._handle_start()

    def _handle_stop(self) -> None:
        self._runtime_stopping = True
        super()._handle_stop()

    def _get_cached_entry(self, data_type: str) -> tuple[Any, float] | None:
        return self._data_cache.get(self._symbol, data_type)

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

    def _get_cached_data(self, data_type: str) -> Any:
        cached = self._get_cached_entry(data_type)
        if not cached:
            return None
        return cached[0]

    def _fetch_orderbook_depth_cached(self) -> Any:
        return self._fetch_http_block(
            "orderbook_depth_50",
            lambda: self._http_client.get_orderbook_depth(self._symbol, limit=50),
            2.5,
        )[0]

    def _fetch_recent_trades_cached(self) -> Any:
        return self._fetch_http_block(
            "recent_trades_1m",
            lambda: self._http_client.get_recent_trades(self._symbol, limit=500),
            2.5,
        )[0]

    def _compute_orderbook_summary(
        self,
        orderbook: Any,
        best_bid: float | None,
        best_ask: float | None,
    ) -> dict[str, float | int | None]:
        if not isinstance(orderbook, dict):
            return {
                "bid_count": 0,
                "ask_count": 0,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread_pct": self._compute_spread_pct(best_bid, best_ask),
                "is_valid": False,
            }
        bids = self._normalize_depth_side(orderbook.get("bids"))
        asks = self._normalize_depth_side(orderbook.get("asks"))
        best_bid_val = best_bid or self._coerce_float(bids[0][0]) if bids else best_bid
        best_ask_val = best_ask or self._coerce_float(asks[0][0]) if asks else best_ask
        spread_pct = self._compute_spread_pct(best_bid_val, best_ask_val)
        is_valid = self._is_good_orderbook(
            {
                "bid_count": len(bids),
                "ask_count": len(asks),
                "best_bid": best_bid_val,
                "best_ask": best_ask_val,
            }
        )
        return {
            "bid_count": len(bids),
            "ask_count": len(asks),
            "best_bid": best_bid_val,
            "best_ask": best_ask_val,
            "spread_pct": spread_pct,
            "is_valid": is_valid,
        }

    @staticmethod
    def _compute_spread_pct(best_bid: float | None, best_ask: float | None) -> float | None:
        if not isinstance(best_bid, (int, float)) or not isinstance(best_ask, (int, float)):
            return None
        if best_bid <= 0:
            return None
        return (best_ask - best_bid) / best_bid * 100

    def _log_ai_fullpack_snapshot(self, datapack: dict[str, Any]) -> None:
        self._log_ai_datapack_snapshot(datapack)

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
        meta = datapack.get("meta") or {}
        ws_ok = meta.get("ws_ok")
        ws_age = meta.get("ws_age_ms")
        liquidity = datapack.get("liquidity") or {}
        trades = datapack.get("trades_1m") or {}
        snapshot = datapack.get("snapshot") or {}
        flags = snapshot.get("flags") or {}
        has_orderbook = flags.get("has_orderbook")
        has_trades = flags.get("has_trades_1m")
        fees = datapack.get("fees") or {}
        is_zero_fee = fees.get("is_zero_fee")
        orderbook_summary = datapack.get("orderbook_summary") or {}
        trades_summary = datapack.get("trades_1m_summary") or {}
        bids = orderbook_summary.get("bid_count") or 0
        asks = orderbook_summary.get("ask_count") or 0
        spread_pct = orderbook_summary.get("spread_pct")
        trades_count = trades_summary.get("count") or 0
        source = snapshot.get("source") or "—"
        stale = snapshot.get("stale")
        fee_text = self._format_fee(
            fees.get("maker_fee_pct") or fees.get("maker"),
            fees.get("taker_fee_pct") or fees.get("taker"),
        )
        spread_text = f"{spread_pct:.4f}%" if isinstance(spread_pct, (int, float)) else "—"
        self._append_log(
            (
                "[AI] fullpack built "
                f"ws_ok={ws_ok} ws_age={ws_age} "
                f"depth50(b/a)={liquidity.get('depth_usd_bid')}/{liquidity.get('depth_usd_ask')} "
                f"trades_1m=n{trades_summary.get('count') or 0}"
            ),
            "INFO",
        )
        self._append_log(
            (
                "[AI] fullpack: "
                f"src={source} stale={stale} bids={bids} asks={asks} "
                f"trades_1m={trades_count} spread={spread_text} fee={fee_text}"
            ),
            "INFO",
        )

    def _handle_refresh_snapshot(self) -> None:
        datapack = self._build_full_market_pack()
        self._last_full_pack = datapack
        self._update_ai_snapshot_label(datapack)
        self._log_ai_fullpack_snapshot(datapack)
        self._append_log("[AI] snapshot refreshed", "INFO")

    def _build_full_market_pack(self) -> dict[str, Any]:
        total_start = time.perf_counter()
        ws_start = time.perf_counter()
        ws_snapshot = self._price_feed_manager.get_snapshot(self._symbol)
        ws_ms = (time.perf_counter() - ws_start) * 1000
        ws_ok = bool(ws_snapshot and ws_snapshot.ws_status in {WS_CONNECTED, WS_DEGRADED})
        ws_age_ms = ws_snapshot.price_age_ms if ws_snapshot else None
        ws_stale = isinstance(ws_age_ms, int) and ws_age_ms > WS_DEGRADED_AFTER_MS
        last_update_ts = None
        now_ms = utc_ms()
        if isinstance(ws_age_ms, int):
            last_update_ts = now_ms - ws_age_ms
        last_ticks = self._price_history[-60:]
        micro_vola_pct, micro_trend = self._compute_micro_metrics(last_ticks)

        http_payload, http_timings, http_errors = self._fetch_http_market_data()
        http_latency_ms = int(sum(http_timings.values()))
        http_ok = not any(http_errors.values())

        exchange_info = http_payload.get("exchange_info")
        book_ticker = http_payload.get("book_ticker")
        ticker_24h = http_payload.get("ticker_24h")
        depth50 = http_payload.get("orderbook_depth_50")
        trades = http_payload.get("recent_trades_1m")
        klines_1m = http_payload.get("klines_1m")
        klines_15m = http_payload.get("klines_15m")
        klines_1h = http_payload.get("klines_1h")
        klines_1d = http_payload.get("klines_1d")

        last_price = ws_snapshot.last_price if ws_snapshot else None
        bid = ws_snapshot.best_bid if ws_snapshot else None
        ask = ws_snapshot.best_ask if ws_snapshot else None
        mid = ws_snapshot.mid_price if ws_snapshot else None
        spread_pct = ws_snapshot.spread_pct if ws_snapshot else None
        if not ws_ok:
            ws_reason = "ws unavailable"
        elif ws_stale:
            ws_reason = f"ws stale: age={ws_age_ms}ms -> HTTP price"
        else:
            ws_reason = ""
        book_bid, book_ask = self._extract_book_ticker(book_ticker)
        if not ws_ok or ws_stale or bid is None or ask is None:
            bid = book_bid if book_bid is not None else bid
            ask = book_ask if book_ask is not None else ask
        if last_price is None or ws_stale:
            last_price = self._coerce_float(ticker_24h.get("lastPrice")) if isinstance(ticker_24h, dict) else None
        if last_price is None and bid is not None and ask is not None:
            last_price = (bid + ask) / 2
        if mid is None and bid is not None and ask is not None:
            mid = (bid + ask) / 2
        if spread_pct is None and bid is not None and ask is not None and bid > 0:
            spread_pct = (ask - bid) / bid * 100

        if bid == 0 or ask == 0:
            reason_detail = "unknown"
            if not ws_ok and book_bid is None and book_ask is None:
                reason_detail = "no ws data and bookTicker missing"
            elif bid == 0 or ask == 0:
                reason_detail = "invalid bid/ask values"
            self._append_log(f"[AI] bid/ask=0 detected ({reason_detail})", "WARN")

        rules = self._parse_exchange_rules(exchange_info)
        liquidity = self._compute_liquidity(depth50)
        trades_1m = self._compute_trades_1m_summary(trades)
        stats24h = {
            "vol_quote": self._coerce_float(ticker_24h.get("quoteVolume")) if isinstance(ticker_24h, dict) else None,
            "high": self._coerce_float(ticker_24h.get("highPrice")) if isinstance(ticker_24h, dict) else None,
            "low": self._coerce_float(ticker_24h.get("lowPrice")) if isinstance(ticker_24h, dict) else None,
            "change_pct": self._coerce_float(ticker_24h.get("priceChangePercent"))
            if isinstance(ticker_24h, dict)
            else None,
        }
        kline_aggs = self._compute_kline_aggs(
            klines_1m=klines_1m,
            klines_15m=klines_15m,
            klines_1h=klines_1h,
            klines_1d=klines_1d,
        )

        snapshot = self._refresh_ai_snapshot(reason="refresh")
        self._update_ai_snapshot_label_from_snapshot(snapshot)
        base_free = self._balances.get(self._base_asset or "", (0.0, 0.0))[0]
        quote_free = self._balances.get(self._quote_asset or "", (0.0, 0.0))[0]
        equity_quote = quote_free + (base_free * last_price) if last_price else None
        return self._prepare_ai_datapack(
            snapshot,
            grid_settings=self.dump_settings(),
            runtime={
                "quote_free": quote_free,
                "base_free": base_free,
                "equity_quote": equity_quote,
                "open_orders_count": len(self._open_orders),
                "engine_state": self._engine_state,
                "trade_enabled": self._trade_gate == TradeGate.TRADE_OK,
                "dry_run": bool(self._dry_run_toggle.isChecked()),
                "current_grid_params": self.dump_settings(),
            },
            volatility=self._compute_volatility_metrics(),
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            user_intent=dict(self._user_intent),
            ws_ok=ws_ok,
            ws_age_ms=ws_age_ms,
            last_update_ts=last_update_ts,
            http_latency_ms=http_latency_ms,
            ws_ms=ws_ms,
            http_ok=http_ok,
            ws_reason=ws_reason,
            http_timings=http_timings,
            http_errors=http_errors,
            stats24h=stats24h,
            liquidity=liquidity,
            kline_aggs=kline_aggs,
            last_ticks=last_ticks,
            micro_vola_pct=micro_vola_pct,
            micro_trend=micro_trend,
            bid=bid,
            ask=ask,
            mid=mid,
            spread_pct=spread_pct,
        )

    def _build_ai_datapack(self, snapshot: MarketSnapshot) -> dict[str, Any]:
        base_free = snapshot.balances.get("base_free", 0.0)
        quote_free = snapshot.balances.get("quote_free", 0.0)
        equity_quote = snapshot.balances.get("equity_quote")
        return self._prepare_ai_datapack(
            snapshot,
            grid_settings=self.dump_settings(),
            runtime={
                "quote_free": quote_free,
                "base_free": base_free,
                "equity_quote": equity_quote,
                "open_orders_count": len(self._open_orders),
                "engine_state": self._engine_state,
                "trade_enabled": self._trade_gate == TradeGate.TRADE_OK,
                "dry_run": bool(self._dry_run_toggle.isChecked()),
                "current_grid_params": self.dump_settings(),
            },
            volatility=self._compute_volatility_metrics(),
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            user_intent=dict(self._user_intent),
            ws_ok=True,
            ws_age_ms=snapshot.ws_age_ms,
            last_update_ts=None,
            http_latency_ms=0,
            ws_ms=0.0,
            http_ok=True,
            ws_reason="",
            http_timings={},
            http_errors={},
            stats24h={},
            liquidity=self._compute_liquidity(snapshot.orderbook_depth_50),
            kline_aggs={},
            last_ticks=self._price_history[-60:],
            micro_vola_pct=None,
            micro_trend=None,
            bid=snapshot.best_bid,
            ask=snapshot.best_ask,
            mid=None,
            spread_pct=snapshot.spread_pct,
        )

    def _get_ai_snapshot(self) -> MarketSnapshot | None:
        return self._snapshot_store.snapshot_active

    def _next_snapshot_id(self) -> str:
        self._snapshot_sequence += 1
        return f"{self._symbol}-{utc_ms()}-{self._snapshot_sequence}"

    def _refresh_ai_snapshot(self, reason: str) -> MarketSnapshot:
        if reason in {"analyze", "chat", "refresh"}:
            self._fetch_orderbook_depth_cached()
            self._fetch_recent_trades_cached()
        if reason == "refresh":
            return self._refresh_active_snapshot()
        snapshot = self._create_market_snapshot(
            prior_snapshot=self._snapshot_store.snapshot_active,
            reuse_depth_trades=True,
        )
        self._register_snapshot(snapshot, reason=reason)
        self._log_snapshot_refresh(snapshot, reason)
        return snapshot

    def _register_snapshot(self, snapshot: MarketSnapshot, *, reason: str) -> None:
        if reason in {"analyze", "chat"}:
            self._snapshot_store.snapshot_analyze = snapshot
            self._snapshot_store.snapshot_active = snapshot
            return
        if reason == "fetch":
            self._snapshot_store.snapshot_fetched = snapshot
            self._snapshot_store.snapshot_active = snapshot
            return
        self._snapshot_store.snapshot_active = snapshot

    def _log_snapshot_refresh(self, snapshot: MarketSnapshot, reason: str) -> None:
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
        prefix = (
            f"[AI] snapshot(use)={snapshot.source}"
            if snapshot.source == "CACHE"
            else f"[AI] snapshot({reason})"
        )
        self._append_log(
            (
                f"{prefix} "
                f"id={snapshot.snapshot_id} bids={bids_count} "
                f"asks={asks_count} trades_1m={trades_count} "
                f"fee(m/t)={self._format_fee(snapshot.maker_fee_pct, snapshot.taker_fee_pct)} "
                f"is_zero_fee={snapshot.is_zero_fee} ws_age={snapshot.ws_age_ms} "
                f"source={snapshot.source} age_ms={snapshot.age_ms} "
                f"stale={snapshot.stale}"
            ),
            "INFO",
        )

    def _format_fee(self, maker_fee_pct: float | None, taker_fee_pct: float | None) -> str:
        if isinstance(maker_fee_pct, (int, float)) and isinstance(taker_fee_pct, (int, float)):
            return f"{maker_fee_pct:.4f}%/{taker_fee_pct:.4f}%"
        return "—"

    def _refresh_active_snapshot(self) -> MarketSnapshot:
        active = self._snapshot_store.snapshot_active
        snapshot = self._create_market_snapshot(
            prior_snapshot=active,
            reuse_depth_trades=True,
        )
        self._snapshot_store.snapshot_active = snapshot
        self._log_snapshot_refresh(snapshot, "refresh")
        return snapshot

    def _create_market_snapshot(
        self,
        *,
        prior_snapshot: MarketSnapshot | None = None,
        reuse_depth_trades: bool = False,
    ) -> MarketSnapshot:
        snapshot = self._price_feed_manager.get_snapshot(self._symbol)
        last_price = self._last_price or (snapshot.last_price if snapshot else None)
        best_bid = snapshot.best_bid if snapshot else None
        best_ask = snapshot.best_ask if snapshot else None
        spread_pct = snapshot.spread_pct if snapshot else None
        if spread_pct is None and best_bid and best_ask and best_bid > 0:
            spread_pct = (best_ask - best_bid) / best_bid * 100
        orderbook = self._get_cached_data("orderbook_depth_50")
        trades = self._get_cached_data("recent_trades_1m")
        stale = False
        source = "HTTP" if isinstance(orderbook, dict) or isinstance(trades, dict) else "WS"
        if isinstance(trades, list):
            trades = {"items": trades, "count": len(trades)}
        if reuse_depth_trades and prior_snapshot:
            if orderbook is None and prior_snapshot.orderbook_depth_50:
                orderbook = prior_snapshot.orderbook_depth_50
                stale = True
                source = "CACHE"
            if trades is None and prior_snapshot.trades_1m:
                trades = prior_snapshot.trades_1m
                stale = True
                source = "CACHE"
        orderbook_summary = self._compute_orderbook_summary(orderbook, best_bid, best_ask)
        trades_summary = self._compute_trades_summary(trades)
        is_good_snapshot = self._is_snapshot_good(orderbook_summary, trades_summary)
        if not is_good_snapshot and self._snapshot_cache.last_good_snapshot:
            cached = self._snapshot_cache.last_good_snapshot
            if cached.orderbook_depth_50:
                orderbook = cached.orderbook_depth_50
                trades = cached.trades_1m
                stale = True
                source = "CACHE"
                orderbook_summary = self._compute_orderbook_summary(orderbook, best_bid, best_ask)
                trades_summary = self._compute_trades_summary(trades)
                is_good_snapshot = self._is_snapshot_good(orderbook_summary, trades_summary)
        stale = stale or not is_good_snapshot
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
        base_free, _ = self._balances.get(base_asset, (0.0, 0.0))
        quote_free, _ = self._balances.get(quote_asset, (0.0, 0.0))
        equity_quote = quote_free + (base_free * last_price) if last_price else None
        base_free, base_locked = self._balances.get(base_asset, (0.0, 0.0))
        quote_free, quote_locked = self._balances.get(quote_asset, (0.0, 0.0))
        market_snapshot = MarketSnapshot(
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
                "equity_quote": equity_quote,
            },
            open_orders_count=len(self._open_orders),
            source=source,
            age_ms=snapshot.price_age_ms if snapshot else None,
            stale=stale,
        )
        if is_good_snapshot:
            self._snapshot_cache.last_good_snapshot = market_snapshot
        return market_snapshot

    def _prepare_ai_datapack(
        self,
        snapshot: MarketSnapshot,
        *,
        grid_settings: dict[str, Any],
        runtime: dict[str, Any],
        volatility: tuple[float | None, float | None],
        timestamp_utc: str,
        user_intent: dict[str, Any],
        ws_ok: bool,
        ws_age_ms: int | None,
        last_update_ts: int | None,
        http_latency_ms: int,
        ws_ms: float,
        http_ok: bool,
        ws_reason: str,
        http_timings: dict[str, float],
        http_errors: dict[str, str],
        stats24h: dict[str, Any],
        liquidity: dict[str, Any],
        kline_aggs: dict[str, Any],
        last_ticks: list[float],
        micro_vola_pct: float | None,
        micro_trend: float | None,
        bid: float | None,
        ask: float | None,
        mid: float | None,
        spread_pct: float | None,
    ) -> dict[str, Any]:
        last_price = snapshot.last_price
        atr_pct, micro_vol_pct = volatility
        maker_fee_pct = snapshot.maker_fee_pct
        taker_fee_pct = snapshot.taker_fee_pct
        is_zero_fee = snapshot.is_zero_fee
        orderbook = snapshot.orderbook_depth_50
        trades_1m = snapshot.trades_1m
        orderbook_summary = self._compute_orderbook_summary(orderbook, bid, ask)
        trades_summary = self._compute_trades_summary(trades_1m)
        has_orderbook = bool(orderbook_summary.get("is_valid"))
        has_trades_1m = (trades_summary.get("count") or 0) >= 3
        has_fees = maker_fee_pct is not None and taker_fee_pct is not None
        has_balances = bool(snapshot.balances)
        has_rules = bool(snapshot.rules)
        datapack_snapshot = {
            "snapshot_id": snapshot.snapshot_id,
            "ts_ms": snapshot.ts_ms,
            "price_last": last_price,
            "price_source": snapshot.price_source,
            "ws_age_ms": ws_age_ms,
            "source": snapshot.source,
            "stale": snapshot.stale,
            "age_ms": snapshot.age_ms,
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
        now_ms = utc_ms()
        pack = {
            "meta": {
                "symbol": self._symbol,
                "ts": now_ms,
                "ws_ok": ws_ok,
                "ws_age_ms": ws_age_ms,
                "last_update_ts": last_update_ts,
                "http_latency_ms": http_latency_ms,
                "pack_source": {"ws": "OK" if ws_ok else "NO", "http": "OK" if http_ok else "WARN"},
                "timestamp_utc": timestamp_utc,
            },
            "price": {
                "last": last_price,
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "spread_pct": spread_pct,
            },
            "micro": {
                "last_ticks": last_ticks[-60:],
                "micro_vola_pct": micro_vola_pct,
                "micro_trend": micro_trend,
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
            "user_intent": user_intent,
            "fees": {
                "maker": maker_fee_pct,
                "taker": taker_fee_pct,
                "is_zero_fee": is_zero_fee,
            },
            "rules": snapshot.rules,
            "liquidity": liquidity,
            "stats24h": stats24h,
            "kline_aggs": kline_aggs,
            "account_runtime": runtime,
        }
        total_ms = ws_ms + http_latency_ms
        self._append_log(
            (
                "[AI] pack_source "
                f"ws={'OK' if ws_ok else 'NO'} http={'OK' if http_ok else 'WARN'}"
            ),
            "INFO",
        )
        self._append_log(
            (
                "[AI] timings: "
                f"ws={ws_ms:.1f}ms book={http_timings.get('book_ticker', 0):.1f}ms "
                f"depth={http_timings.get('orderbook_depth_50', 0):.1f}ms "
                f"trades={http_timings.get('recent_trades_1m', 0):.1f}ms "
                f"klines={http_timings.get('klines', 0):.1f}ms total={total_ms:.1f}ms"
            ),
            "INFO",
        )
        if http_errors:
            errors = "; ".join([f"{key}={err}" for key, err in http_errors.items() if err])
            if errors:
                self._append_log(f"[AI] http errors: {errors}", "WARN")
        if ws_reason:
            self._append_log(f"[AI] ws status: {ws_reason}", "WARN")
        return pack

    def _fetch_http_market_data(self) -> tuple[dict[str, Any], dict[str, float], dict[str, str]]:
        results: dict[str, Any] = {}
        timings: dict[str, float] = {}
        errors: dict[str, str] = {}
        tasks = {
            "exchange_info": lambda: self._http_client.get_exchange_info_symbol(self._symbol),
            "book_ticker": lambda: self._http_client.get_book_ticker(self._symbol),
            "ticker_24h": lambda: self._http_client.get_ticker_24h(self._symbol),
            "orderbook_depth_50": lambda: self._http_client.get_orderbook_depth(self._symbol, limit=50),
            "recent_trades_1m": lambda: self._http_client.get_recent_trades(self._symbol, limit=500),
            "klines_1m": lambda: self._http_client.get_klines(self._symbol, interval="1m", limit=120),
            "klines_15m": lambda: self._http_client.get_klines(self._symbol, interval="15m", limit=192),
            "klines_1h": lambda: self._http_client.get_klines(self._symbol, interval="1h", limit=336),
            "klines_1d": lambda: self._http_client.get_klines(self._symbol, interval="1d", limit=120),
        }
        limits = {
            "exchange_info": 600.0,
            "book_ticker": 2.0,
            "ticker_24h": 10.0,
            "orderbook_depth_50": 2.5,
            "recent_trades_1m": 2.5,
            "klines_1m": 60.0,
            "klines_15m": 60.0,
            "klines_1h": 60.0,
            "klines_1d": 60.0,
        }
        with ThreadPoolExecutor(max_workers=6) as executor:
            future_map = {
                executor.submit(self._fetch_http_block, name, tasks[name], limits[name]): name
                for name in tasks
            }
            for future in as_completed(future_map):
                name = future_map[future]
                data, duration_ms, error = future.result()
                results[name] = data
                timings[name] = duration_ms
                if error:
                    errors[name] = error
        timings["klines"] = (
            timings.get("klines_1m", 0)
            + timings.get("klines_15m", 0)
            + timings.get("klines_1h", 0)
            + timings.get("klines_1d", 0)
        )

        return results, timings, errors

    def _fetch_http_block(
        self,
        name: str,
        fetch_fn: Callable[[], Any],
        min_interval_s: float,
    ) -> tuple[Any, float, str | None]:
        cached = self._get_cached_entry(name)
        ttl = self._cache_ttls.get(name)
        if cached:
            data, saved_at = cached
            if ttl is not None and self._data_cache.is_fresh(saved_at, ttl):
                return data, 0.0, None
            if not self._rate_limiter.allow(name, min_interval_s):
                return data, 0.0, None
        start = time.perf_counter()
        try:
            data = fetch_fn()
        except Exception as exc:  # noqa: BLE001
            return (cached[0] if cached else None), (time.perf_counter() - start) * 1000, str(exc)
        duration_ms = (time.perf_counter() - start) * 1000
        self._data_cache.set(self._symbol, name, data)
        return data, duration_ms, None

    def _format_fee(self, maker_fee_pct: float | None, taker_fee_pct: float | None) -> str:
        if isinstance(maker_fee_pct, (int, float)) and isinstance(taker_fee_pct, (int, float)):
            return f"{maker_fee_pct:.4f}%/{taker_fee_pct:.4f}%"
        return "—"

    def _compute_micro_metrics(self, prices: list[float]) -> tuple[float | None, float | None]:
        if len(prices) < 2:
            return None, None
        returns: list[float] = []
        for idx in range(1, len(prices)):
            prev = prices[idx - 1]
            if prev <= 0:
                continue
            returns.append((prices[idx] / prev) - 1)
        if not returns:
            return None, None
        avg_abs = sum(abs(r) for r in returns) / len(returns)
        trend = (prices[-1] / prices[0] - 1) * 100 if prices[0] > 0 else None
        return round(avg_abs * 100, 6), round(trend, 6) if trend is not None else None

    def _coerce_float(self, value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _parse_exchange_rules(self, exchange_info: Any) -> dict[str, Any]:
        if not isinstance(exchange_info, dict):
            return {
                "tick_size": self._exchange_rules.get("tick"),
                "step_size": self._exchange_rules.get("step"),
                "min_qty": self._exchange_rules.get("min_qty"),
                "min_notional": self._exchange_rules.get("min_notional"),
            }
        symbols = exchange_info.get("symbols")
        if not isinstance(symbols, list) or not symbols:
            return {
                "tick_size": None,
                "step_size": None,
                "min_qty": None,
                "min_notional": None,
            }
        symbol_info = symbols[0]
        if not isinstance(symbol_info, dict):
            return {
                "tick_size": None,
                "step_size": None,
                "min_qty": None,
                "min_notional": None,
            }
        tick_size = None
        step_size = None
        min_qty = None
        min_notional = None
        filters = symbol_info.get("filters", [])
        if isinstance(filters, list):
            for entry in filters:
                if not isinstance(entry, dict):
                    continue
                if entry.get("filterType") == "PRICE_FILTER":
                    tick_size = self._coerce_float(entry.get("tickSize"))
                elif entry.get("filterType") == "LOT_SIZE":
                    step_size = self._coerce_float(entry.get("stepSize"))
                    min_qty = self._coerce_float(entry.get("minQty"))
                elif entry.get("filterType") in {"MIN_NOTIONAL", "NOTIONAL"}:
                    min_notional = self._coerce_float(entry.get("minNotional"))
        return {
            "tick_size": tick_size,
            "step_size": step_size,
            "min_qty": min_qty,
            "min_notional": min_notional,
        }

    def _extract_book_ticker(self, book_ticker: Any) -> tuple[float | None, float | None]:
        if not isinstance(book_ticker, dict):
            return None, None
        bid = self._coerce_float(book_ticker.get("bidPrice"))
        ask = self._coerce_float(book_ticker.get("askPrice"))
        return bid, ask

    def _compute_liquidity(self, orderbook: Any) -> dict[str, Any]:
        if not isinstance(orderbook, dict):
            return {
                "top_bid": None,
                "top_ask": None,
                "depth_usd_bid": None,
                "depth_usd_ask": None,
                "imbalance": None,
                "levels": 0,
            }
        bids = self._normalize_depth_side(orderbook.get("bids"))
        asks = self._normalize_depth_side(orderbook.get("asks"))
        top_bid = None
        top_ask = None
        if bids:
            top_bid = self._coerce_float(bids[0][0])
        if asks:
            top_ask = self._coerce_float(asks[0][0])
        depth_bid = 0.0
        depth_ask = 0.0
        for price, qty in bids:
            price_val = self._coerce_float(price)
            qty_val = self._coerce_float(qty)
            if price_val is None or qty_val is None:
                continue
            depth_bid += price_val * qty_val
        for price, qty in asks:
            price_val = self._coerce_float(price)
            qty_val = self._coerce_float(qty)
            if price_val is None or qty_val is None:
                continue
            depth_ask += price_val * qty_val
        total = depth_bid + depth_ask
        imbalance = (depth_bid - depth_ask) / total if total > 0 else None
        levels = 50 if bids or asks else 0
        return {
            "top_bid": top_bid,
            "top_ask": top_ask,
            "depth_usd_bid": depth_bid if depth_bid > 0 else None,
            "depth_usd_ask": depth_ask if depth_ask > 0 else None,
            "imbalance": imbalance,
            "levels": levels,
        }

    def _compute_trades_1m_summary(self, trades: Any) -> dict[str, Any]:
        if not isinstance(trades, list):
            return {"n": None, "vwap": None, "buy_ratio": None, "sell_ratio": None, "volume_quote": None}
        now_ms = utc_ms()
        cutoff = now_ms - 60_000
        buy_quote = 0.0
        sell_quote = 0.0
        vwap_num = 0.0
        vwap_den = 0.0
        count = 0
        for trade in trades:
            if not isinstance(trade, dict):
                continue
            trade_ts = trade.get("time")
            if not isinstance(trade_ts, int) or trade_ts < cutoff:
                continue
            price = self._coerce_float(trade.get("price"))
            qty = self._coerce_float(trade.get("qty"))
            if price is None or qty is None:
                continue
            quote = price * qty
            if trade.get("isBuyerMaker"):
                sell_quote += quote
            else:
                buy_quote += quote
            vwap_num += quote
            vwap_den += qty
            count += 1
        volume_quote = buy_quote + sell_quote
        vwap = vwap_num / vwap_den if vwap_den > 0 else None
        return {
            "n": count,
            "vwap": vwap,
            "buy_ratio": buy_quote / volume_quote if volume_quote > 0 else None,
            "sell_ratio": sell_quote / volume_quote if volume_quote > 0 else None,
            "volume_quote": volume_quote if volume_quote > 0 else None,
        }

    @staticmethod
    def _is_good_orderbook(summary: dict[str, float | int | None]) -> bool:
        bid_count = summary.get("bid_count") or 0
        ask_count = summary.get("ask_count") or 0
        best_bid = summary.get("best_bid")
        best_ask = summary.get("best_ask")
        return (
            bid_count >= 10
            and ask_count >= 10
            and isinstance(best_bid, (int, float))
            and isinstance(best_ask, (int, float))
            and best_bid > 0
            and best_ask > 0
        )

    def _is_snapshot_good(
        self,
        orderbook_summary: dict[str, float | int | None],
        trades_summary: dict[str, float | int | None],
    ) -> bool:
        trades_count = trades_summary.get("count") or 0
        return self._is_good_orderbook(orderbook_summary) and trades_count >= 1

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
        for trade in items:
            if not isinstance(trade, dict):
                continue
            trade_ts = trade.get("time")
            if isinstance(trade_ts, int):
                last_ts = max(last_ts or 0, trade_ts)
            price = self._coerce_float(trade.get("price"))
            qty = self._coerce_float(trade.get("qty"))
            if price is None or qty is None:
                continue
            quote = price * qty
            vwap_numerator += quote
            vwap_denominator += qty
            count += 1
        vwap = vwap_numerator / vwap_denominator if vwap_denominator > 0 else None
        return {"count": count, "last_ts": last_ts, "vwap_1m": vwap}

    def _extract_ohlc(self, klines: Any) -> list[tuple[float, float, float, float]]:
        if not isinstance(klines, list):
            return []
        series: list[tuple[float, float, float, float]] = []
        for item in klines:
            if not isinstance(item, list) or len(item) < 5:
                continue
            open_val = self._coerce_float(item[1])
            high_val = self._coerce_float(item[2])
            low_val = self._coerce_float(item[3])
            close_val = self._coerce_float(item[4])
            if None in (open_val, high_val, low_val, close_val):
                continue
            series.append((open_val, high_val, low_val, close_val))
        return series

    def _compute_vola_pct(self, series: list[tuple[float, float, float, float]]) -> float | None:
        if len(series) < 2:
            return None
        returns: list[float] = []
        prev_close = series[0][3]
        for _, _, _, close in series[1:]:
            if prev_close <= 0:
                prev_close = close
                continue
            returns.append(abs(close / prev_close - 1))
            prev_close = close
        if not returns:
            return None
        return round(sum(returns) / len(returns) * 100, 6)

    def _compute_atr_pct(self, series: list[tuple[float, float, float, float]]) -> float | None:
        if len(series) < 2:
            return None
        trs: list[float] = []
        prev_close = series[0][3]
        for _, high, low, close in series[1:]:
            if prev_close <= 0:
                prev_close = close
                continue
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            trs.append(tr / prev_close)
            prev_close = close
        if not trs:
            return None
        return round(sum(trs) / len(trs) * 100, 6)

    def _compute_trend_pct(self, series: list[tuple[float, float, float, float]]) -> float | None:
        if len(series) < 2:
            return None
        first_close = series[0][3]
        last_close = series[-1][3]
        if first_close <= 0:
            return None
        return round((last_close / first_close - 1) * 100, 6)

    def _compute_range_pct(self, series: list[tuple[float, float, float, float]]) -> float | None:
        if not series:
            return None
        highs = [high for _, high, _, _ in series]
        lows = [low for _, _, low, _ in series]
        if not highs or not lows:
            return None
        min_low = min(lows)
        max_high = max(highs)
        if min_low <= 0:
            return None
        return round((max_high - min_low) / min_low * 100, 6)

    def _compute_kline_aggs(
        self,
        *,
        klines_1m: Any,
        klines_15m: Any,
        klines_1h: Any,
        klines_1d: Any,
    ) -> dict[str, Any]:
        series_1m = self._extract_ohlc(klines_1m)
        series_15m = self._extract_ohlc(klines_15m)
        series_1h = self._extract_ohlc(klines_1h)
        series_1d = self._extract_ohlc(klines_1d)
        range_1h = self._compute_range_pct(series_1h[-24:]) if series_1h else None
        range_7d = self._compute_range_pct(series_1d[-7:]) if series_1d else None
        range_30d = self._compute_range_pct(series_1d[-30:]) if series_1d else None
        return {
            "vola_1m": self._compute_vola_pct(series_1m),
            "atr_1m": self._compute_atr_pct(series_1m),
            "vola_15m": self._compute_vola_pct(series_15m),
            "atr_15m": self._compute_atr_pct(series_15m),
            "trend_15m": self._compute_trend_pct(series_15m),
            "trend_1h": self._compute_trend_pct(series_1h),
            "range_1h_pct": range_1h,
            "trend_1d": self._compute_trend_pct(series_1d),
            "range_7d_pct": range_7d,
            "range_30d_pct": range_30d,
        }

    def _update_ai_snapshot_label(self, datapack: dict[str, Any]) -> None:
        orderbook_summary = datapack.get("orderbook_summary") or {}
        trades_summary = datapack.get("trades_1m_summary") or {}
        fees = datapack.get("fees") or {}
        meta = datapack.get("meta") or {}
        snapshot_meta = datapack.get("snapshot", {})
        ws_age_ms = snapshot_meta.get("ws_age_ms")
        source = snapshot_meta.get("source", "—")
        stale = snapshot_meta.get("stale")
        snapshot_id = snapshot_meta.get("snapshot_id", "—")
        bid = orderbook_summary.get("best_bid")
        ask = orderbook_summary.get("best_ask")
        spread_pct = orderbook_summary.get("spread_pct")
        trades_count = trades_summary.get("count")
        maker_fee_pct = fees.get("maker_fee_pct") or fees.get("maker")
        taker_fee_pct = fees.get("taker_fee_pct") or fees.get("taker")
        bid_text = f"{bid:.8f}" if isinstance(bid, (int, float)) else "—"
        ask_text = f"{ask:.8f}" if isinstance(ask, (int, float)) else "—"
        spread_text = f"{spread_pct:.4f}%" if isinstance(spread_pct, (int, float)) else "—"
        trades_text = str(trades_count or 0)
        fee_text = self._format_fee(maker_fee_pct, taker_fee_pct)
        ws_age_ms = meta.get("ws_age_ms")
        ws_age_text = f"{ws_age_ms}ms" if isinstance(ws_age_ms, int) else "—"
        stale_text = "stale" if stale else "fresh"
        self._ai_snapshot_label.setText(
            (
                f"snapshot={meta.get('ts', '—')} bid={bid_text} ask={ask_text} spread={spread_text} "

                f"snapshot={snapshot_id} src={source} {stale_text} "
                f"bid={bid_text} ask={ask_text} spread={spread_text} "
                f"trades1m={trades_text} fee(m/t)={fee_text} ws_age={ws_age_text}"
            )
        )

    def _update_ai_snapshot_label_from_snapshot(
        self,
        snapshot: MarketSnapshot,
        *,
        live_update: PriceUpdate | None = None,
    ) -> None:
        best_bid = snapshot.best_bid
        best_ask = snapshot.best_ask
        ws_age_ms = snapshot.ws_age_ms
        if live_update:
            best_bid = live_update.best_bid or best_bid
            best_ask = live_update.best_ask or best_ask
            if live_update.price_age_ms is not None:
                ws_age_ms = live_update.price_age_ms
        orderbook_summary = self._compute_orderbook_summary(snapshot.orderbook_depth_50, best_bid, best_ask)
        trades_summary = self._compute_trades_summary(snapshot.trades_1m)
        fees = {
            "maker_fee_pct": snapshot.maker_fee_pct,
            "taker_fee_pct": snapshot.taker_fee_pct,
        }
        snapshot_meta = {
            "ws_age_ms": ws_age_ms,
            "snapshot_id": snapshot.snapshot_id,
            "source": snapshot.source,
            "stale": snapshot.stale,
        }
        self._update_ai_snapshot_label(
            {
                "orderbook_summary": orderbook_summary,
                "trades_1m_summary": trades_summary,
                "fees": fees,
                "snapshot": snapshot_meta,
            }
        )

    def _apply_price_update(self, update: PriceUpdate) -> None:
        super()._apply_price_update(update)

        snapshot = self._get_ai_snapshot()
        if snapshot is None:
            return
        self._update_ai_snapshot_label_from_snapshot(snapshot, live_update=update)

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
