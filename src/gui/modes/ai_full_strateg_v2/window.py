from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.core.logging import get_logger
from src.core.strategies.registry import get_strategy_definitions
from src.gui.ai_operator_grid_window import AiOperatorGridWindow


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
        self._strategy_stack.addWidget(grid_panel)
        self._strategy_stack.addWidget(self._build_strategy_placeholder())
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

    def _handle_strategy_change(self) -> None:
        strategy_id = self._strategy_combo.currentData()
        if strategy_id == "GRID_CLASSIC":
            self._strategy_stack.setCurrentIndex(0)
        else:
            self._strategy_stack.setCurrentIndex(1)

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
        super()._handle_start()
