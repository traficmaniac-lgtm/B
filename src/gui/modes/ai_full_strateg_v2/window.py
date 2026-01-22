from __future__ import annotations

from typing import Any
from uuid import uuid4

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.core.logging import get_logger
from src.core.strategies.manual_runtime import check_manual_start
from src.core.strategies.manual_strategies import (
    BreakoutPullbackParams,
    MeanReversionBandsParams,
    ScalpMicroSpreadParams,
    StrategyContext,
    TrendFollowGridParams,
)
from src.core.strategies.registry import get_strategy, get_strategy_definitions
from src.gui.ai_operator_grid_window import AiOperatorGridWindow
from src.gui.lite_grid_window import GridPlannedOrder, TradeGate
from src.ai.operator_runtime import cancel_all_bot_orders, pause_state, stop_state


class AiFullStrategV2Window(AiOperatorGridWindow):
    STRATEGY_IDS = [
        "GRID_CLASSIC",
        "GRID_BIASED_LONG",
        "GRID_BIASED_SHORT",
        "MEAN_REVERSION_BANDS",
        "BREAKOUT_PULLBACK",
        "SCALP_MICRO_SPREAD",
        "TREND_FOLLOW_GRID",
    ]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._logger = get_logger("gui.ai_full_strateg_v2")
        self._manual_mode = False
        self._manual_order_keys: set[str] = set()
        self._manual_order_ids: set[str] = set()
        self._manual_intents_sent: list[GridPlannedOrder] = []
        self._manual_session_id: str | None = None
        self._strategy_stack_map: dict[str, int] = {}
        self._manual_fields: dict[str, dict[str, QWidget]] = {}
        super().__init__(*args, **kwargs)
        self.setWindowTitle(f"AI Full Strateg v2.0 — {self._symbol}")
        self._logger.info("[MODE] selected=AI_FULL_STRATEG_V2 symbol=%s", self._symbol)

    def _build_body(self) -> QSplitter:
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        ai_panel = self._build_ai_panel()
        ai_panel.setMinimumWidth(420)
        ai_panel.setMaximumWidth(520)
        params_panel = self._build_strategy_panel()
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
        group = QGroupBox("AI Option")
        group.setStyleSheet(
            "QGroupBox { border: 1px solid #e5e7eb; border-radius: 6px; margin-top: 6px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 8px; }"
        )
        group.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout = QVBoxLayout(group)
        layout.setSpacing(4)

        tabs = QTabWidget()
        tabs.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        tabs.addTab(self._build_ai_analysis_tab(), "Анализ")
        tabs.addTab(self._build_ai_observation_tab(), "Наблюдение")
        layout.addWidget(tabs)
        return group

    def _build_ai_analysis_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
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

        lookback_row = QHBoxLayout()
        lookback_row.addWidget(QLabel("Lookback"))
        self._lookback_combo = QComboBox()
        for days in (1, 7, 30, 365):
            self._lookback_combo.addItem(f"{days}d", days)
        self._lookback_combo.setCurrentIndex(0)
        lookback_row.addWidget(self._lookback_combo, stretch=1)
        layout.addLayout(lookback_row)

        self._ai_state_label = QLabel(f"state: {self._ai_state_machine.state.value}")
        self._ai_state_label.setStyleSheet("color: #111827; font-size: 12px; font-weight: 600;")
        layout.addWidget(self._ai_state_label)

        self._ai_snapshot_label = QLabel("snapshot: —")
        self._ai_snapshot_label.setStyleSheet("color: #374151; font-size: 12px;")
        layout.addWidget(self._ai_snapshot_label)
        self._ai_data_quality_label = QLabel("data quality: —")
        self._ai_data_quality_label.setStyleSheet("color: #6b7280; font-size: 11px;")
        layout.addWidget(self._ai_data_quality_label)

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
        self._pause_ai_button = QPushButton("Pause (AI)")
        self._pause_ai_button.clicked.connect(self._handle_pause)
        actions_row.addWidget(self._pause_ai_button)
        layout.addLayout(actions_row)
        return tab

    def _build_ai_observation_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(6)

        info_label = QLabel("Наблюдение: панель метрик будет добавлена позже.")
        info_label.setStyleSheet("color: #6b7280; font-size: 11px;")
        layout.addWidget(info_label)

        metrics_group = QGroupBox("Future metrics panel")
        metrics_group.setStyleSheet(
            "QGroupBox { border: 1px dashed #d1d5db; border-radius: 6px; margin-top: 6px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 8px; }"
        )
        metrics_layout = QVBoxLayout(metrics_group)
        metrics_layout.addWidget(QLabel("Not implemented yet"))
        metrics_layout.addStretch()
        layout.addWidget(metrics_group)
        layout.addStretch()
        return tab

    def _build_strategy_panel(self) -> QWidget:
        group = QGroupBox("Параметры стратегии")
        group.setStyleSheet(
            "QGroupBox { border: 1px solid #e5e7eb; border-radius: 6px; margin-top: 6px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 8px; }"
        )
        layout = QVBoxLayout(group)
        layout.setSpacing(6)

        strategy_row = QHBoxLayout()
        strategy_row.addWidget(QLabel("Strategy"))
        self._strategy_combo = QComboBox()
        registry_ids = {definition.strategy_id for definition in get_strategy_definitions()}
        for strategy_id in self.STRATEGY_IDS:
            label = strategy_id
            if strategy_id not in registry_ids:
                label = f"{strategy_id}"
            self._strategy_combo.addItem(label, strategy_id)
        self._strategy_combo.currentIndexChanged.connect(self._handle_strategy_change)
        strategy_row.addWidget(self._strategy_combo, stretch=1)
        layout.addLayout(strategy_row)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Mode"))
        self._mode_combo = QComboBox()
        self._mode_combo.addItem("AI", False)
        self._mode_combo.addItem("Manual", True)
        self._mode_combo.currentIndexChanged.connect(self._handle_mode_change)
        mode_row.addWidget(self._mode_combo, stretch=1)
        layout.addLayout(mode_row)

        self._strategy_stack = QStackedWidget()
        grid_panel = super()._build_grid_panel()
        grid_panel.setTitle("")
        grid_panel.setFlat(True)
        grid_panel.setStyleSheet("QGroupBox { border: none; margin-top: 0px; }")
        grid_index = self._strategy_stack.addWidget(grid_panel)
        for grid_strategy in ("GRID_CLASSIC", "GRID_BIASED_LONG", "GRID_BIASED_SHORT"):
            self._strategy_stack_map[grid_strategy] = grid_index

        mean_rev_index = self._strategy_stack.addWidget(self._build_mean_reversion_panel())
        self._strategy_stack_map["MEAN_REVERSION_BANDS"] = mean_rev_index

        breakout_index = self._strategy_stack.addWidget(self._build_breakout_panel())
        self._strategy_stack_map["BREAKOUT_PULLBACK"] = breakout_index

        scalp_index = self._strategy_stack.addWidget(self._build_scalp_panel())
        self._strategy_stack_map["SCALP_MICRO_SPREAD"] = scalp_index

        trend_index = self._strategy_stack.addWidget(self._build_trend_panel())
        self._strategy_stack_map["TREND_FOLLOW_GRID"] = trend_index
        layout.addWidget(self._strategy_stack)

        self._handle_strategy_change()
        return group

    def _build_strategy_placeholder(self) -> QWidget:
        placeholder = QWidget()
        layout = QVBoxLayout(placeholder)
        layout.addStretch()
        label = QLabel("Not implemented yet")
        label.setAlignment(Qt.AlignCenter)
        label.setStyleSheet("color: #6b7280; font-size: 12px;")
        layout.addWidget(label)
        layout.addStretch()
        return placeholder

    def _build_mean_reversion_panel(self) -> QWidget:
        panel = QWidget()
        form = QFormLayout(panel)
        fields: dict[str, QWidget] = {}

        budget = QDoubleSpinBox()
        budget.setRange(10.0, 1_000_000.0)
        budget.setValue(100.0)
        budget.setDecimals(2)
        fields["budget"] = budget
        form.addRow("Budget", budget)

        lookback = QSpinBox()
        lookback.setRange(10, 500)
        lookback.setValue(120)
        fields["lookback"] = lookback
        form.addRow("Lookback", lookback)

        band_k = QDoubleSpinBox()
        band_k.setRange(1.0, 4.0)
        band_k.setValue(2.0)
        band_k.setDecimals(2)
        fields["band_k"] = band_k
        form.addRow("Band K", band_k)

        atr_period = QSpinBox()
        atr_period.setRange(5, 50)
        atr_period.setValue(14)
        fields["atr_period"] = atr_period
        form.addRow("ATR Period", atr_period)

        base_step = QDoubleSpinBox()
        base_step.setRange(0.05, 5.0)
        base_step.setValue(0.3)
        base_step.setDecimals(3)
        fields["base_step_pct"] = base_step
        form.addRow("Base Step %", base_step)

        range_pct = QDoubleSpinBox()
        range_pct.setRange(0.2, 10.0)
        range_pct.setValue(1.5)
        range_pct.setDecimals(3)
        fields["range_pct"] = range_pct
        form.addRow("Range %", range_pct)

        tp_pct = QDoubleSpinBox()
        tp_pct.setRange(0.1, 10.0)
        tp_pct.setValue(0.8)
        tp_pct.setDecimals(3)
        fields["tp_pct"] = tp_pct
        form.addRow("TP %", tp_pct)

        max_active = QSpinBox()
        max_active.setRange(2, 200)
        max_active.setValue(8)
        fields["max_active_orders"] = max_active
        form.addRow("Max Active", max_active)

        self._manual_fields["MEAN_REVERSION_BANDS"] = fields
        return panel

    def _build_breakout_panel(self) -> QWidget:
        panel = QWidget()
        form = QFormLayout(panel)
        fields: dict[str, QWidget] = {}

        budget = QDoubleSpinBox()
        budget.setRange(10.0, 1_000_000.0)
        budget.setValue(100.0)
        budget.setDecimals(2)
        fields["budget"] = budget
        form.addRow("Budget", budget)

        window = QSpinBox()
        window.setRange(5, 200)
        window.setValue(50)
        fields["breakout_window"] = window
        form.addRow("Breakout Window", window)

        pullback = QDoubleSpinBox()
        pullback.setRange(0.1, 5.0)
        pullback.setValue(0.4)
        pullback.setDecimals(3)
        fields["pullback_pct"] = pullback
        form.addRow("Pullback %", pullback)

        confirm = QSpinBox()
        confirm.setRange(1, 20)
        confirm.setValue(3)
        fields["confirm_trades_n"] = confirm
        form.addRow("Confirm trades N", confirm)

        tp_pct = QDoubleSpinBox()
        tp_pct.setRange(0.1, 10.0)
        tp_pct.setValue(1.0)
        tp_pct.setDecimals(3)
        fields["tp_pct"] = tp_pct
        form.addRow("TP %", tp_pct)

        sl_pct = QDoubleSpinBox()
        sl_pct.setRange(0.0, 10.0)
        sl_pct.setValue(0.0)
        sl_pct.setDecimals(3)
        fields["sl_pct"] = sl_pct
        form.addRow("SL % (optional)", sl_pct)

        max_active = QSpinBox()
        max_active.setRange(1, 200)
        max_active.setValue(6)
        fields["max_active_orders"] = max_active
        form.addRow("Max Active", max_active)

        self._manual_fields["BREAKOUT_PULLBACK"] = fields
        return panel

    def _build_scalp_panel(self) -> QWidget:
        panel = QWidget()
        form = QFormLayout(panel)
        fields: dict[str, QWidget] = {}

        budget = QDoubleSpinBox()
        budget.setRange(10.0, 1_000_000.0)
        budget.setValue(100.0)
        budget.setDecimals(2)
        fields["budget"] = budget
        form.addRow("Budget", budget)

        min_edge = QDoubleSpinBox()
        min_edge.setRange(0.01, 2.0)
        min_edge.setValue(0.08)
        min_edge.setDecimals(3)
        fields["min_edge_pct"] = min_edge
        form.addRow("Min edge %", min_edge)

        max_edge = QDoubleSpinBox()
        max_edge.setRange(0.05, 3.0)
        max_edge.setValue(0.15)
        max_edge.setDecimals(3)
        fields["max_edge_pct"] = max_edge
        form.addRow("Max edge %", max_edge)

        refresh_ms = QSpinBox()
        refresh_ms.setRange(100, 10_000)
        refresh_ms.setValue(1500)
        fields["refresh_ms"] = refresh_ms
        form.addRow("Refresh ms", refresh_ms)

        max_position = QDoubleSpinBox()
        max_position.setRange(0.0, 1_000_000.0)
        max_position.setValue(0.0)
        max_position.setDecimals(4)
        fields["max_position"] = max_position
        form.addRow("Max position", max_position)

        tp_pct = QDoubleSpinBox()
        tp_pct.setRange(0.05, 5.0)
        tp_pct.setValue(0.3)
        tp_pct.setDecimals(3)
        fields["tp_pct"] = tp_pct
        form.addRow("TP %", tp_pct)

        maker_only = QCheckBox("Maker only")
        maker_only.setChecked(True)
        fields["maker_only"] = maker_only
        form.addRow("", maker_only)

        max_active = QSpinBox()
        max_active.setRange(2, 200)
        max_active.setValue(4)
        fields["max_active_orders"] = max_active
        form.addRow("Max Active", max_active)

        self._manual_fields["SCALP_MICRO_SPREAD"] = fields
        return panel

    def _build_trend_panel(self) -> QWidget:
        panel = QWidget()
        form = QFormLayout(panel)
        fields: dict[str, QWidget] = {}

        budget = QDoubleSpinBox()
        budget.setRange(10.0, 1_000_000.0)
        budget.setValue(100.0)
        budget.setDecimals(2)
        fields["budget"] = budget
        form.addRow("Budget", budget)

        trend_tf = QComboBox()
        for tf in ("1m", "5m", "15m", "1h", "4h", "1d"):
            trend_tf.addItem(tf, tf)
        fields["trend_tf"] = trend_tf
        form.addRow("Trend TF", trend_tf)

        trend_threshold = QDoubleSpinBox()
        trend_threshold.setRange(0.01, 2.0)
        trend_threshold.setValue(0.2)
        trend_threshold.setDecimals(3)
        fields["trend_threshold"] = trend_threshold
        form.addRow("Trend threshold %", trend_threshold)

        bias_strength = QDoubleSpinBox()
        bias_strength.setRange(0.05, 0.9)
        bias_strength.setValue(0.3)
        bias_strength.setDecimals(2)
        fields["bias_strength"] = bias_strength
        form.addRow("Bias strength", bias_strength)

        adaptive_step = QCheckBox("Adaptive step")
        adaptive_step.setChecked(True)
        fields["adaptive_step"] = adaptive_step
        form.addRow("", adaptive_step)

        max_active = QSpinBox()
        max_active.setRange(2, 200)
        max_active.setValue(8)
        fields["max_active_orders"] = max_active
        form.addRow("Max Active", max_active)

        self._manual_fields["TREND_FOLLOW_GRID"] = fields
        return panel

    def _handle_strategy_change(self) -> None:
        strategy_id = self._strategy_combo.currentData()
        index = self._strategy_stack_map.get(strategy_id)
        if index is not None:
            self._strategy_stack.setCurrentIndex(index)
        else:
            self._strategy_stack.setCurrentIndex(0)

    def _handle_mode_change(self) -> None:
        self._manual_mode = bool(self._mode_combo.currentData())

    def _handle_start(self) -> None:
        strategy_id = self._strategy_combo.currentData()
        dry_run = self._dry_run_toggle.isChecked() if hasattr(self, "_dry_run_toggle") else False
        self._logger.info(
            "[ORDERS] Start pressed (mode=AI_FULL_STRATEG_V2 manual=%s strategy=%s dry_run=%s)",
            self._manual_mode,
            strategy_id,
            dry_run,
        )
        if not self._manual_mode:
            super()._handle_start()
            return
        self._handle_manual_start(strategy_id, dry_run)

    def _handle_pause(self) -> None:
        if not self._manual_mode:
            super()._handle_pause()
            return
        self._handle_manual_pause()

    def _handle_stop(self) -> None:
        if not self._manual_mode:
            super()._handle_stop()
            return
        self._handle_manual_stop()

    def _handle_manual_start(self, strategy_id: str, dry_run: bool) -> None:
        if self._state in {"RUNNING", "PLACING_GRID", "PAUSED", "WAITING_FILLS"}:
            self._append_log("Start ignored: engine already running.", kind="WARN")
            return
        self._refresh_balances(force=True)
        if not self._rules_loaded:
            self._append_log("Start blocked: rules not loaded.", kind="WARN")
            self._refresh_exchange_rules(force=True)
            return
        decision = check_manual_start(dry_run=dry_run, trade_gate=self._trade_gate.value)
        if not decision.ok:
            reason = decision.reason
            self._append_log(f"Start blocked: TRADE DISABLED (reason={reason}).", kind="WARN")
            return
        if not dry_run and self._trade_gate == TradeGate.TRADE_DISABLED_READONLY:
            reason = self._trade_gate_reason()
            self._append_log(f"Start blocked: TRADE DISABLED (reason={reason}).", kind="WARN")
            return

        strategy = get_strategy(strategy_id)
        if not strategy:
            self._append_log(f"[STRAT] validate ok=false reason=unknown_strategy {strategy_id}", kind="WARN")
            return
        anchor_price = self.get_anchor_price(self._symbol)
        if anchor_price is None:
            self._append_log("Start blocked: no price available.", kind="WARN")
            return
        snapshot = self._price_feed_manager.get_snapshot(self._symbol)
        best_bid = snapshot.best_bid if snapshot else None
        best_ask = snapshot.best_ask if snapshot else None
        cached_book = self._get_http_cached("book_ticker")
        if best_bid is None and isinstance(cached_book, dict):
            best_bid = self._coerce_float(str(cached_book.get("bidPrice", "")))
        if best_ask is None and isinstance(cached_book, dict):
            best_ask = self._coerce_float(str(cached_book.get("askPrice", "")))
        if best_bid is None or best_ask is None:
            try:
                book = self._http_client.get_book_ticker(self._symbol)
            except Exception as exc:  # noqa: BLE001
                self._append_log(f"BOOK: HTTP failed ({exc})", kind="WARN")
                book = {}
            if isinstance(book, dict):
                best_bid = best_bid or self._coerce_float(str(book.get("bidPrice", "")))
                best_ask = best_ask or self._coerce_float(str(book.get("askPrice", "")))
                self._set_http_cache("book_ticker", book)

        params, budget, max_active_orders, tp_pct = self._collect_manual_params(strategy_id)
        self._append_log(
            (
                f"[STRAT] start strategy={strategy_id} mode=MANUAL dry_run={str(dry_run).lower()} "
                f"budget={budget:.2f}"
            ),
            kind="ORDERS",
        )
        ctx = StrategyContext(
            symbol=self._symbol,
            budget_usdt=budget,
            rules=self._exchange_rules,
            current_price=anchor_price,
            best_bid=best_bid,
            best_ask=best_ask,
            fee_maker=self._trade_fees[0],
            fee_taker=self._trade_fees[1],
            dry_run=dry_run,
            balances={
                "base_free": float(self._balance_snapshot().get("base_free", 0)),
                "quote_free": float(self._balance_snapshot().get("quote_free", 0)),
            },
            max_active_orders=max_active_orders,
            params=params,
            klines=self._resolve_klines(strategy_id, params),
            klines_by_tf=self._resolve_klines_by_tf(strategy_id),
        )
        ok, reason = strategy.validate(ctx)
        self._append_log(f"[STRAT] validate ok={str(ok).lower()} reason={reason}", kind="INFO")
        if not ok:
            self._append_log(f"Start blocked: {reason}.", kind="WARN")
            return
        intents = strategy.build_orders(ctx)
        if not intents:
            self._append_log("Start blocked: no orders built.", kind="WARN")
            return
        prices = [intent.price for intent in intents]
        low_price = min(prices) if prices else anchor_price
        high_price = max(prices) if prices else anchor_price
        step_hint = self._resolve_step_hint(params)
        self._append_log(
            (
                f"[STRAT] intents n={len(intents)} price_range={low_price:.8f}..{high_price:.8f} "
                f"step={step_hint} tp={tp_pct:.4f}"
            ),
            kind="INFO",
        )
        if not dry_run and not self._account_client:
            self._append_log("Start blocked: no account client.", kind="WARN")
            return
        self._manual_order_keys.clear()
        self._manual_order_ids.clear()
        self._manual_intents_sent = []
        self._manual_session_id = uuid4().hex[:8]
        self._change_state("PLACING_GRID")
        planned: list[GridPlannedOrder] = []
        for idx, intent in enumerate(intents, start=1):
            planned.append(
                GridPlannedOrder(
                    side=intent.side,
                    price=intent.price,
                    qty=intent.qty,
                    level_index=idx,
                )
            )
        self._manual_intents_sent = planned
        if dry_run:
            for intent in intents:
                self._append_log(
                    (
                        f"[ORDERS] place side={intent.side} price={intent.price:.8f} qty={intent.qty:.8f} "
                        f"tag={intent.tag}"
                    ),
                    kind="ORDERS",
                )
            self._render_sim_orders(planned)
            self._change_state("RUNNING")
            return

        placed = 0
        for idx, intent in enumerate(intents, start=1):
            self._append_log(
                (
                    f"[ORDERS] place side={intent.side} price={intent.price:.8f} qty={intent.qty:.8f} "
                    f"tag={intent.tag}"
                ),
                kind="ORDERS",
            )
            client_id = self._manual_client_order_id(idx)
            response, error = self._place_limit(
                intent.side,
                self.as_decimal(intent.price),
                self.as_decimal(intent.qty),
                client_id,
                "manual",
            )
            if error:
                self._append_log(str(error), kind="WARN")
            if response:
                order_id = str(response.get("orderId", ""))
                if order_id:
                    self._manual_order_ids.add(order_id)
                self._manual_order_keys.add(
                    self._order_key(intent.side, self.as_decimal(intent.price), self.as_decimal(intent.qty))
                )
                placed += 1
        if placed == 0:
            self._append_log("Start blocked: orders rejected by filters.", kind="WARN")
            self._change_state("IDLE")
            return
        self._change_state("RUNNING")
        self._refresh_open_orders(force=True)

    def _handle_manual_pause(self) -> None:
        self._append_log("Pause pressed.", kind="ORDERS")
        if self._dry_run_toggle.isChecked() or not self._account_client:
            self._append_log(
                f"[ORDERS] cancel_all strategy={self._strategy_combo.currentData()} n={len(self._manual_intents_sent)}",
                kind="ORDERS",
            )
            self._clear_manual_registry()
            self._change_state("PAUSED")
            return
        prefix = self._manual_order_prefix()

        def _cancel() -> Any:
            return cancel_all_bot_orders(
                symbol=self._symbol,
                account_client=self._account_client,
                order_ids=self._manual_order_ids,
                client_order_id_prefix=prefix,
                timeout_s=3.0,
            )

        state, result = pause_state(_cancel)
        self._append_log(
            f"[ORDERS] cancel_all strategy={self._strategy_combo.currentData()} n={result.canceled_count}",
            kind="ORDERS",
        )
        self._clear_manual_registry()
        self._change_state(state)

    def _handle_manual_stop(self) -> None:
        self._append_log("Stop pressed.", kind="ORDERS")
        if self._dry_run_toggle.isChecked() or not self._account_client:
            self._append_log(
                f"[ORDERS] cancel_all strategy={self._strategy_combo.currentData()} n={len(self._manual_intents_sent)}",
                kind="ORDERS",
            )
            self._clear_manual_registry()
            self._change_state("IDLE")
            return
        prefix = self._manual_order_prefix()

        def _cancel() -> Any:
            return cancel_all_bot_orders(
                symbol=self._symbol,
                account_client=self._account_client,
                order_ids=self._manual_order_ids,
                client_order_id_prefix=prefix,
                timeout_s=3.0,
            )

        state, result = stop_state(_cancel)
        self._append_log(
            f"[ORDERS] cancel_all strategy={self._strategy_combo.currentData()} n={result.canceled_count}",
            kind="ORDERS",
        )
        self._clear_manual_registry()
        self._change_state("IDLE" if state == "STOPPED" else state)

    def _manual_order_prefix(self) -> str:
        if not self._manual_session_id:
            return ""
        return f"BBOT_MANUAL_{self._symbol}_{self._manual_session_id}_"

    def _manual_client_order_id(self, idx: int) -> str:
        return self._limit_client_order_id(f"{self._manual_order_prefix()}MANUAL_{idx}")

    def _clear_manual_registry(self) -> None:
        self._manual_order_keys.clear()
        self._manual_order_ids.clear()
        self._manual_intents_sent = []
        self._manual_session_id = None
        self._open_orders = []
        self._open_orders_all = []
        self._render_open_orders()

    @staticmethod
    def _resolve_step_hint(params: dict[str, Any]) -> str:
        for key in ("step_pct", "base_step_pct", "min_edge_pct", "pullback_pct"):
            value = params.get(key)
            if isinstance(value, (int, float)) and value > 0:
                return f"{value:.4f}%"
        return "—"

    def _collect_manual_params(self, strategy_id: str) -> tuple[dict[str, Any], float, int, float]:
        if strategy_id in {"GRID_CLASSIC", "GRID_BIASED_LONG", "GRID_BIASED_SHORT"}:
            settings = self._resolve_start_settings()
            params = {
                "levels": settings.grid_count,
                "step_pct": settings.grid_step_pct,
                "range_down_pct": settings.range_low_pct,
                "range_up_pct": settings.range_high_pct,
                "tp_pct": settings.take_profit_pct,
                "max_active_orders": settings.max_active_orders,
                "size_mode": settings.order_size_mode,
                "bias": settings.direction,
            }
            return params, settings.budget, settings.max_active_orders, settings.take_profit_pct
        fields = self._manual_fields.get(strategy_id, {})
        budget_widget = fields.get("budget")
        budget = float(budget_widget.value()) if isinstance(budget_widget, QDoubleSpinBox) else 0.0
        max_active_widget = fields.get("max_active_orders")
        max_active = int(max_active_widget.value()) if isinstance(max_active_widget, QSpinBox) else 1
        tp_widget = fields.get("tp_pct")
        tp_pct = float(tp_widget.value()) if isinstance(tp_widget, QDoubleSpinBox) else 0.0
        params: dict[str, Any] = {"max_active_orders": max_active}
        for key, widget in fields.items():
            if isinstance(widget, QSpinBox):
                params[key] = int(widget.value())
            elif isinstance(widget, QDoubleSpinBox):
                params[key] = float(widget.value())
            elif isinstance(widget, QCheckBox):
                params[key] = bool(widget.isChecked())
            elif isinstance(widget, QComboBox):
                params[key] = widget.currentData()
        if strategy_id == "MEAN_REVERSION_BANDS":
            params = MeanReversionBandsParams(**params).__dict__
        elif strategy_id == "BREAKOUT_PULLBACK":
            sl_raw = params.get("sl_pct")
            sl_pct = sl_raw if sl_raw and sl_raw > 0 else None
            params["sl_pct"] = sl_pct
            params = BreakoutPullbackParams(**params).__dict__
        elif strategy_id == "SCALP_MICRO_SPREAD":
            params = ScalpMicroSpreadParams(**params).__dict__
        elif strategy_id == "TREND_FOLLOW_GRID":
            params = TrendFollowGridParams(**params).__dict__
        return params, budget, max_active, tp_pct

    def _resolve_klines(self, strategy_id: str, params: dict[str, Any]) -> list[Any] | None:
        if strategy_id not in {"MEAN_REVERSION_BANDS", "BREAKOUT_PULLBACK"}:
            return None
        interval = "1m" if strategy_id == "MEAN_REVERSION_BANDS" else "5m"
        name = f"klines_{interval}"
        data = self._get_cached_data(name)
        if data is None:
            data, _, _ = self._fetch_http_block(
                name,
                lambda: self._http_client.get_klines(self._symbol, interval=interval, limit=200),
                2.5,
            )
        if isinstance(data, list):
            return data
        return None

    def _resolve_klines_by_tf(self, strategy_id: str) -> dict[str, list[Any]] | None:
        if strategy_id != "TREND_FOLLOW_GRID":
            return None
        fields = self._manual_fields.get(strategy_id, {})
        tf_widget = fields.get("trend_tf")
        interval = tf_widget.currentData() if isinstance(tf_widget, QComboBox) else "1m"
        name = f"klines_{interval}"
        data = self._get_cached_data(name)
        if data is None:
            data, _, _ = self._fetch_http_block(
                name,
                lambda: self._http_client.get_klines(self._symbol, interval=interval, limit=200),
                2.5,
            )
        if isinstance(data, list):
            return {interval: data}
        return None
