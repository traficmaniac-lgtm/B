from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from time import perf_counter
from typing import Callable

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.ai.openai_client import OpenAIClient
from src.binance.http_client import BinanceHttpClient
from src.binance.ws_client import BinanceWsClient
from src.core.config import Config
from src.core.logging import get_logger
from src.core.models import Pair
from src.gui.models.app_state import AppState
from src.services.markets_service import MarketsService
from src.services.price_service import PriceService


class _WorkerSignals(QObject):
    success = Signal(object, int)
    error = Signal(str)


class _Worker(QRunnable):
    def __init__(self, fn: Callable[[], object]) -> None:
        super().__init__()
        self.signals = _WorkerSignals()
        self._fn = fn

    def run(self) -> None:
        start = perf_counter()
        try:
            result = self._fn()
        except Exception as exc:
            self.signals.error.emit(str(exc))
            return
        latency_ms = int((perf_counter() - start) * 1000)
        self.signals.success.emit(result, latency_ms)


class _WsStatusSignals(QObject):
    status = Signal(str, str)


@dataclass(frozen=True)
class _TableColumns:
    symbol: int = 0
    price: int = 1
    source: int = 2
    updated_ms: int = 3


class OverviewTab(QWidget):
    def __init__(
        self,
        config: Config,
        app_state: AppState,
        on_open_pair: Callable[[str], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._app_state = app_state
        self._logger = get_logger("gui.overview")
        self._on_open_pair = on_open_pair
        self._http_client = BinanceHttpClient(
            base_url=config.binance.base_url,
            timeout_s=config.http.timeout_s,
            retries=config.http.retries,
            backoff_base_s=config.http.backoff_base_s,
            backoff_max_s=config.http.backoff_max_s,
        )
        self._markets_service = MarketsService(self._http_client)
        self._ws_status_signals = _WsStatusSignals()
        self._ws_status_signals.status.connect(self._handle_ws_status)
        self._ws_client = BinanceWsClient(
            ws_url=config.binance.ws_url,
            on_tick=self._handle_ws_tick,
            on_status=self._emit_ws_status,
        )
        self._price_service = PriceService(
            http_client=self._http_client,
            ws_client=self._ws_client,
            ttl_ms=self._app_state.price_ttl_ms,
            refresh_interval_ms=self._app_state.price_refresh_ms,
            fallback_enabled=config.prices.fallback_enabled,
        )
        self._thread_pool = QThreadPool.globalInstance()
        self._price_timer = QTimer(self)
        self._price_timer.setInterval(self._app_state.price_refresh_ms)
        self._price_timer.timeout.connect(self._refresh_prices)
        self._price_update_in_flight = False
        self._symbol_rows: dict[str, int] = {}
        self._symbols_order: list[str] = []
        self._table_columns = _TableColumns()

        layout = QVBoxLayout()
        layout.addLayout(self._build_status_row())
        layout.addLayout(self._build_filters())

        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["Symbol", "Price", "Source", "Updated(ms)"])
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.itemSelectionChanged.connect(self._update_selected_pair)
        self._table.itemDoubleClicked.connect(self._handle_double_click)
        layout.addWidget(self._table)

        self._selected_label = QLabel("Selected pair: -")
        layout.addWidget(self._selected_label)

        self.setLayout(layout)
        self._set_ai_status()

    def _build_status_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addLayout(self._build_status_cards())

        self._self_check_button = QPushButton("Run Self-check")
        self._self_check_button.clicked.connect(self._run_self_check)
        row.addWidget(self._self_check_button, alignment=Qt.AlignTop)
        return row

    def _build_status_cards(self) -> QGridLayout:
        grid = QGridLayout()

        config_box = QGroupBox("Config")
        config_layout = QVBoxLayout()
        config_layout.addWidget(QLabel("Config loaded: YES"))
        config_box.setLayout(config_layout)

        binance_box = QGroupBox("Binance")
        binance_layout = QVBoxLayout()
        self._binance_status_label = QLabel("Binance: NOT CONNECTED")
        self._ws_status_label = QLabel("WS: NOT CONNECTED")
        binance_layout.addWidget(self._binance_status_label)
        binance_layout.addWidget(self._ws_status_label)
        binance_box.setLayout(binance_layout)

        ai_box = QGroupBox("AI")
        ai_layout = QVBoxLayout()
        self._ai_status_label = QLabel("AI: NOT CONNECTED")
        ai_layout.addWidget(self._ai_status_label)
        ai_box.setLayout(ai_layout)

        balance_box = QGroupBox("Balance / PnL")
        balance_layout = QVBoxLayout()
        self._balance_label = QLabel("Balance (USDT): Not connected")
        self._pnl_period_combo = QComboBox()
        self._pnl_period_combo.addItems(["1h", "4h", "24h", "7d"])
        self._pnl_period_combo.setCurrentText(self._app_state.pnl_period)
        self._pnl_period_combo.currentTextChanged.connect(self._handle_pnl_period_change)
        self._pnl_label = QLabel("PnL: --")
        balance_layout.addWidget(self._balance_label)
        balance_layout.addWidget(QLabel("PnL period"))
        balance_layout.addWidget(self._pnl_period_combo)
        balance_layout.addWidget(self._pnl_label)
        balance_box.setLayout(balance_layout)

        grid.addWidget(config_box, 0, 0)
        grid.addWidget(binance_box, 0, 1)
        grid.addWidget(ai_box, 0, 2)
        grid.addWidget(balance_box, 0, 3)
        return grid

    def _build_filters(self) -> QHBoxLayout:
        layout = QHBoxLayout()

        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("Search symbol")

        self._quote_combo = QComboBox()
        self._quote_combo.addItems(["USDT", "USDC", "FDUSD", "EUR"])
        self._quote_combo.setCurrentText(self._app_state.default_quote)
        self._quote_combo.currentTextChanged.connect(self._handle_quote_change)

        self._load_button = QPushButton("Load Pairs")
        self._load_button.clicked.connect(self._handle_load_pairs)
        self._refresh_button = QPushButton("Refresh")
        self._refresh_button.clicked.connect(self._handle_refresh_pairs)

        layout.addWidget(QLabel("Search"))
        layout.addWidget(self._search_input)
        layout.addWidget(QLabel("Quote"))
        layout.addWidget(self._quote_combo)
        layout.addWidget(self._load_button)
        layout.addWidget(self._refresh_button)
        layout.addStretch()
        return layout

    def _update_selected_pair(self) -> None:
        selected_items = self._table.selectedItems()
        if not selected_items:
            return
        symbol = selected_items[0].text()
        self._selected_label.setText(f"Selected pair: {symbol}")

    def _handle_double_click(self, _: QTableWidgetItem) -> None:
        selected_items = self._table.selectedItems()
        if not selected_items:
            return
        symbol = selected_items[0].text()
        self._on_open_pair(symbol)

    def _run_self_check(self) -> None:
        worker = _Worker(self._http_client.get_time)
        worker.signals.success.connect(self._handle_self_check_success)
        worker.signals.error.connect(self._handle_self_check_error)
        self._thread_pool.start(worker)
        self._run_ai_self_check()

    def _run_ai_self_check(self) -> None:
        if not self._app_state.openai_key_present:
            self._app_state.ai_connected = False
            self._app_state.ai_checked = False
            self._set_ai_status()
            self._logger.info("AI self-check skipped (missing OpenAI key).")
            return
        self._ai_status_label.setText("AI: CHECKING")
        client = OpenAIClient(
            api_key=self._app_state.openai_api_key,
            model=self._app_state.openai_model,
            timeout_s=25.0,
            retries=1,
        )
        worker = _Worker(lambda: asyncio.run(client.self_check()))
        worker.signals.success.connect(self._handle_ai_self_check_success)
        worker.signals.error.connect(self._handle_ai_self_check_error)
        self._thread_pool.start(worker)

    def _handle_self_check_success(self, result: object, latency_ms: int) -> None:
        if isinstance(result, dict) and "serverTime" in result:
            self._logger.info("self-check ok (serverTime=%s, %sms)", result["serverTime"], latency_ms)
        else:
            self._logger.info("self-check ok (%sms)", latency_ms)
        self._set_binance_status_connected()

    def _handle_self_check_error(self, message: str) -> None:
        self._logger.error("self-check failed: %s", message)
        self._set_binance_status_error(message)

    def _handle_ai_self_check_success(self, result: object, latency_ms: int) -> None:
        if not isinstance(result, tuple) or len(result) != 2:
            self._logger.error("Unexpected AI self-check response: %s", result)
            self._app_state.ai_connected = False
            self._app_state.ai_checked = True
            self._set_ai_status()
            return
        ok, message = result
        self._app_state.ai_checked = True
        if ok:
            self._app_state.ai_connected = True
            self._logger.info("AI self-check ok (%sms): %s", latency_ms, message)
        else:
            self._app_state.ai_connected = False
            self._logger.error("AI self-check failed: %s", message)
        self._set_ai_status()

    def _handle_ai_self_check_error(self, message: str) -> None:
        self._app_state.ai_connected = False
        self._app_state.ai_checked = True
        self._logger.error("AI self-check failed: %s", message)
        self._set_ai_status()

    def _handle_load_pairs(self) -> None:
        self._set_pairs_buttons_enabled(False)
        self._symbol_rows.clear()
        self._symbols_order.clear()
        self._table.setRowCount(0)
        quote = self._quote_combo.currentText().strip()
        self._logger.info("Loading pairs...")
        self._binance_status_label.setText("Binance: LOADING")
        worker = _Worker(lambda: self._markets_service.load_pairs_cached(quote))
        worker.signals.success.connect(self._handle_pairs_loaded_cached)
        worker.signals.error.connect(self._handle_pairs_error)
        self._thread_pool.start(worker)

    def _handle_refresh_pairs(self) -> None:
        self._set_pairs_buttons_enabled(False)
        self._symbol_rows.clear()
        self._symbols_order.clear()
        self._table.setRowCount(0)
        quote = self._quote_combo.currentText().strip()
        self._logger.info("Refreshing pairs...")
        self._binance_status_label.setText("Binance: REFRESHING")
        worker = _Worker(lambda: self._markets_service.refresh_pairs(quote))
        worker.signals.success.connect(self._handle_pairs_loaded_http)
        worker.signals.error.connect(self._handle_pairs_error)
        self._thread_pool.start(worker)

    def _handle_pairs_loaded_cached(self, payload: object, _: int) -> None:
        if not isinstance(payload, tuple) or len(payload) != 2:
            self._logger.error("Unexpected cached pairs response: %s", payload)
            self._set_pairs_buttons_enabled(True)
            return
        pairs, from_cache = payload
        source = "CACHE" if from_cache else "HTTP"
        self._handle_pairs_loaded(pairs, source)

    def _handle_pairs_loaded_http(self, pairs: object, _: int) -> None:
        self._handle_pairs_loaded(pairs, "HTTP")

    def _handle_pairs_loaded(self, pairs: object, source: str) -> None:
        self._set_binance_status_connected()
        if not isinstance(pairs, Iterable):
            self._logger.error("Unexpected pairs response: %s", pairs)
            self._set_pairs_buttons_enabled(True)
            return
        pair_list = [pair for pair in pairs if isinstance(pair, Pair)]
        self._populate_table(pair_list)
        self._set_pairs_buttons_enabled(True)
        self._price_service.set_symbols([])
        self._logger.info("Loaded %s pairs from %s.", len(pair_list), source)
        self._binance_status_label.setText(f"Binance: CONNECTED ({len(pair_list)} pairs, {source})")

    def _handle_pairs_error(self, message: str) -> None:
        self._logger.error("Failed to load pairs: %s", message)
        self._set_binance_status_error(message)
        self._set_pairs_buttons_enabled(True)

    def _populate_table(self, pairs: list[Pair]) -> None:
        self._table.setRowCount(len(pairs))
        for row_index, pair in enumerate(pairs):
            self._symbol_rows[pair.symbol] = row_index
            self._symbols_order.append(pair.symbol)
            self._set_table_item(row_index, self._table_columns.symbol, pair.symbol)
            self._set_table_item(row_index, self._table_columns.price, "--")
            self._set_table_item(row_index, self._table_columns.source, "--")
            self._set_table_item(row_index, self._table_columns.updated_ms, "--")
        if pairs:
            self._table.selectRow(0)
            self._selected_label.setText(f"Selected pair: {pairs[0].symbol}")
        else:
            self._selected_label.setText("Selected pair: -")

    def _refresh_prices(self) -> None:
        if self._price_update_in_flight or not self._symbol_rows:
            return
        self._price_update_in_flight = True
        symbols_to_update = self._symbols_order[:200]
        snapshot = self._price_service.snapshot(symbols_to_update)
        for symbol in symbols_to_update:
            price = snapshot.get(symbol)
            if price is None:
                continue
            row_index = self._symbol_rows.get(symbol)
            if row_index is None:
                continue
            self._set_table_item(row_index, self._table_columns.price, f"{price.price:.8f}")
            self._set_table_item(row_index, self._table_columns.source, price.source)
            self._set_table_item(row_index, self._table_columns.updated_ms, str(price.age_ms))
        self._price_update_in_flight = False

    def _set_table_item(self, row: int, column: int, value: str) -> None:
        item = self._table.item(row, column)
        if item is None:
            item = QTableWidgetItem()
            item.setTextAlignment(Qt.AlignCenter)
            self._table.setItem(row, column, item)
        item.setText(value)

    def _set_pairs_buttons_enabled(self, enabled: bool) -> None:
        self._load_button.setEnabled(enabled)
        self._refresh_button.setEnabled(enabled)

    def _set_binance_status_connected(self) -> None:
        self._binance_status_label.setText("Binance: CONNECTED")

    def _set_binance_status_error(self, message: str) -> None:
        summary = self._summarize_error(message)
        self._binance_status_label.setText(f"Binance: ERROR ({summary})")

    def _handle_ws_tick(self, symbol: str, price: float, timestamp_ms: int) -> None:
        self._price_service.on_ws_tick(symbol, price, timestamp_ms)

    def _emit_ws_status(self, status: str, message: str | None) -> None:
        self._ws_status_signals.status.emit(status, message or status)

    def _handle_ws_status(self, status: str, message: str) -> None:
        if status == "CONNECTED":
            self._ws_status_label.setText("WS: CONNECTED")
            return
        if status == "RECONNECTING":
            self._ws_status_label.setText("WS: RECONNECTING")
            return
        if status == "ERROR":
            summary = self._summarize_error(message)
            self._ws_status_label.setText(f"WS: ERROR ({summary})")

    def _handle_quote_change(self, quote: str) -> None:
        self._app_state.default_quote = quote
        self._persist_app_state()

    def _handle_pnl_period_change(self, period: str) -> None:
        self._app_state.pnl_period = period
        self._persist_app_state()

    def _persist_app_state(self) -> None:
        if self._app_state.user_config_path is None:
            return
        try:
            self._app_state.save(self._app_state.user_config_path)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("Failed to persist UI state: %s", exc)

    def shutdown(self) -> None:
        self._price_timer.stop()
        self._price_service.stop()

    @staticmethod
    def _summarize_error(message: str, max_len: int = 80) -> str:
        summary = " ".join(message.split())
        if len(summary) > max_len:
            return summary[: max_len - 3] + "..."
        return summary

    def refresh_ai_status(self) -> None:
        self._set_ai_status()

    def _set_ai_status(self) -> None:
        if not self._app_state.openai_key_present:
            self._ai_status_label.setText("AI: NOT CONNECTED")
            return
        if self._app_state.ai_connected:
            self._ai_status_label.setText("AI: CONNECTED")
            return
        if self._app_state.ai_checked:
            self._ai_status_label.setText("AI: ERROR")
            return
        self._ai_status_label.setText("AI: NOT CONNECTED")
