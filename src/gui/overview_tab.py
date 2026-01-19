from __future__ import annotations

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

from src.binance.http_client import BinanceHttpClient
from src.core.config import Config
from src.core.logging import get_logger
from src.core.models import Pair
from src.services.markets_service import MarketsService


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
        on_open_pair: Callable[[str], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
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
        self._thread_pool = QThreadPool.globalInstance()
        self._price_timer = QTimer(self)
        self._price_timer.setInterval(self._config.prices.ttl_ms)
        self._price_timer.timeout.connect(self._refresh_prices)
        self._price_update_in_flight = False
        self._symbol_rows: dict[str, int] = {}
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
        binance_layout.addWidget(self._binance_status_label)
        binance_box.setLayout(binance_layout)

        ai_box = QGroupBox("AI")
        ai_layout = QVBoxLayout()
        ai_layout.addWidget(QLabel("AI: NOT CONNECTED"))
        ai_box.setLayout(ai_layout)

        grid.addWidget(config_box, 0, 0)
        grid.addWidget(binance_box, 0, 1)
        grid.addWidget(ai_box, 0, 2)
        return grid

    def _build_filters(self) -> QHBoxLayout:
        layout = QHBoxLayout()

        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("Search symbol")

        self._quote_combo = QComboBox()
        self._quote_combo.addItems(["USDT", "USDC", "FDUSD", "EUR"])

        self._load_button = QPushButton("Load Pairs")
        self._load_button.clicked.connect(self._handle_load_pairs)

        layout.addWidget(QLabel("Search"))
        layout.addWidget(self._search_input)
        layout.addWidget(QLabel("Quote"))
        layout.addWidget(self._quote_combo)
        layout.addWidget(self._load_button)
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

    def _handle_self_check_success(self, result: object, latency_ms: int) -> None:
        if isinstance(result, dict) and "serverTime" in result:
            self._logger.info("self-check ok (serverTime=%s, %sms)", result["serverTime"], latency_ms)
        else:
            self._logger.info("self-check ok (%sms)", latency_ms)
        self._set_binance_status_connected()

    def _handle_self_check_error(self, message: str) -> None:
        self._logger.error("self-check failed: %s", message)
        self._set_binance_status_error(message)

    def _handle_load_pairs(self) -> None:
        self._load_button.setEnabled(False)
        self._symbol_rows.clear()
        self._table.setRowCount(0)
        quote = self._quote_combo.currentText().strip()
        worker = _Worker(lambda: self._markets_service.load_pairs(quote))
        worker.signals.success.connect(self._handle_pairs_loaded)
        worker.signals.error.connect(self._handle_pairs_error)
        self._thread_pool.start(worker)

    def _handle_pairs_loaded(self, pairs: object, _: int) -> None:
        self._set_binance_status_connected()
        if not isinstance(pairs, Iterable):
            self._logger.error("Unexpected pairs response: %s", pairs)
            self._load_button.setEnabled(True)
            return
        pair_list = [pair for pair in pairs if isinstance(pair, Pair)]
        self._populate_table(pair_list)
        self._load_button.setEnabled(True)
        self._refresh_prices()
        if not self._price_timer.isActive():
            self._price_timer.start()

    def _handle_pairs_error(self, message: str) -> None:
        self._logger.error("Failed to load pairs: %s", message)
        self._set_binance_status_error(message)
        self._load_button.setEnabled(True)

    def _populate_table(self, pairs: list[Pair]) -> None:
        self._table.setRowCount(len(pairs))
        for row_index, pair in enumerate(pairs):
            self._symbol_rows[pair.symbol] = row_index
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
        worker = _Worker(self._http_client.get_ticker_prices)
        worker.signals.success.connect(self._handle_prices_loaded)
        worker.signals.error.connect(self._handle_prices_error)
        self._thread_pool.start(worker)

    def _handle_prices_loaded(self, prices: object, latency_ms: int) -> None:
        self._price_update_in_flight = False
        if not isinstance(prices, dict):
            self._logger.error("Unexpected prices payload: %s", prices)
            return
        self._set_binance_status_connected()
        for symbol, row_index in self._symbol_rows.items():
            price = prices.get(symbol)
            if price is None:
                continue
            self._set_table_item(row_index, self._table_columns.price, str(price))
            self._set_table_item(row_index, self._table_columns.source, "HTTP")
            self._set_table_item(row_index, self._table_columns.updated_ms, str(latency_ms))

    def _handle_prices_error(self, message: str) -> None:
        self._price_update_in_flight = False
        self._logger.error("Failed to refresh prices: %s", message)
        self._set_binance_status_error(message)

    def _set_table_item(self, row: int, column: int, value: str) -> None:
        item = self._table.item(row, column)
        if item is None:
            item = QTableWidgetItem()
            item.setTextAlignment(Qt.AlignCenter)
            self._table.setItem(row, column, item)
        item.setText(value)

    def _set_binance_status_connected(self) -> None:
        self._binance_status_label.setText("Binance: CONNECTED")

    def _set_binance_status_error(self, message: str) -> None:
        summary = self._summarize_error(message)
        self._binance_status_label.setText(f"Binance: ERROR ({summary})")

    @staticmethod
    def _summarize_error(message: str, max_len: int = 80) -> str:
        summary = " ".join(message.split())
        if len(summary) > max_len:
            return summary[: max_len - 3] + "..."
        return summary
