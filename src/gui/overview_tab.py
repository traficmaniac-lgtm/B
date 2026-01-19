from __future__ import annotations

import asyncio
from collections.abc import Iterable
from time import perf_counter
from typing import Callable

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QRunnable,
    QSortFilterProxyModel,
    Qt,
    QThreadPool,
    QTimer,
    Signal,
)
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from src.ai.openai_client import OpenAIClient
from src.binance.account_client import BinanceAccountClient
from src.binance.http_client import BinanceHttpClient
from src.core.config import Config
from src.core.logging import get_logger
from src.core.models import Pair
from src.gui.models.app_state import AppState
from src.services.markets_service import MarketsService
from src.services.price_feed_manager import PriceFeedManager, PriceUpdate, WS_CONNECTED, WS_DEGRADED, WS_LOST


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


class _PairsTableModel(QAbstractTableModel):
    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pairs: list[Pair] = []
        self._row_by_symbol: dict[str, int] = {}
        self._price_data: dict[str, tuple[str, str, str]] = {}
        self._headers = ["Symbol", "Price", "Source", "Updated(ms)"]

    def rowCount(self, parent: QModelIndex | None = None) -> int:  # noqa: N802
        if parent is not None and parent.isValid():
            return 0
        return len(self._pairs)

    def columnCount(self, parent: QModelIndex | None = None) -> int:  # noqa: N802
        if parent is not None and parent.isValid():
            return 0
        return len(self._headers)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> str | None:
        if not index.isValid():
            return None
        pair = self._pairs[index.row()]
        if role == Qt.DisplayRole:
            if index.column() == 0:
                return pair.symbol
            if index.column() == 1:
                return self._price_data.get(pair.symbol, ("--", "--", "--"))[0]
            if index.column() == 2:
                return self._price_data.get(pair.symbol, ("--", "--", "--"))[1]
            if index.column() == 3:
                return self._price_data.get(pair.symbol, ("--", "--", "--"))[2]
        if role == Qt.UserRole:
            return pair.symbol
        if role == Qt.UserRole + 1:
            return pair.quote_asset
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole) -> str | None:  # noqa: N802
        if role != Qt.DisplayRole or orientation != Qt.Horizontal:
            return None
        if 0 <= section < len(self._headers):
            return self._headers[section]
        return None

    def set_pairs(self, pairs: list[Pair]) -> None:
        self.beginResetModel()
        self._pairs = pairs
        self._row_by_symbol = {pair.symbol: idx for idx, pair in enumerate(pairs)}
        self._price_data = {}
        self.endResetModel()

    def update_price(self, symbol: str, price: str, source: str, age_ms: str) -> None:
        row = self._row_by_symbol.get(symbol)
        if row is None:
            return
        self._price_data[symbol] = (price, source, age_ms)
        top_left = self.index(row, 1)
        bottom_right = self.index(row, 3)
        self.dataChanged.emit(top_left, bottom_right, [Qt.DisplayRole])

    def symbol_for_row(self, row: int) -> str | None:
        if 0 <= row < len(self._pairs):
            return self._pairs[row].symbol
        return None

    def last_price_for_symbol(self, symbol: str) -> str | None:
        price = self._price_data.get(symbol, ("--", "--", "--"))[0]
        if price in {"--", ""}:
            return None
        return price


class _PairsFilterProxyModel(QSortFilterProxyModel):
    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._quote_filter = ""
        self._search_text = ""

    def set_quote_filter(self, quote: str) -> None:
        self._quote_filter = quote.strip().upper()
        self.invalidateFilter()

    def set_search_text(self, text: str) -> None:
        self._search_text = text.strip().upper()
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:  # noqa: N802
        model = self.sourceModel()
        if model is None:
            return False
        index = model.index(source_row, 0, source_parent)
        symbol = model.data(index, Qt.DisplayRole) or ""
        quote = model.data(index, Qt.UserRole + 1) or ""
        if self._quote_filter:
            if quote:
                if quote.upper() != self._quote_filter:
                    return False
            else:
                if not str(symbol).upper().endswith(self._quote_filter):
                    return False
        if self._search_text and self._search_text not in str(symbol).upper():
            return False
        return True


class OverviewTab(QWidget):
    def __init__(
        self,
        config: Config,
        app_state: AppState,
        on_open_pair: Callable[[str, str | None], None],
        price_feed_manager: PriceFeedManager | None = None,
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
        self._price_manager = price_feed_manager or PriceFeedManager.get_instance(config)
        self._thread_pool = QThreadPool.globalInstance()
        self._price_timer = QTimer(self)
        self._price_timer.setInterval(self._app_state.price_refresh_ms)
        self._price_timer.timeout.connect(self._refresh_prices)
        self._price_update_in_flight = False
        self._selected_symbol: str | None = None
        self._latest_price_update: PriceUpdate | None = None
        self._all_pairs: list[Pair] = []
        self._cache_partial = False
        self._pairs_model = _PairsTableModel(self)
        self._proxy_model = _PairsFilterProxyModel(self)
        self._proxy_model.setSourceModel(self._pairs_model)
        self._account_client: BinanceAccountClient | None = None
        self._account_balances: list[dict[str, object]] = []
        self._account_balances_display: list[str] = []
        self._account_last_error: str | None = None
        self._account_timer = QTimer(self)
        self._account_timer.setInterval(10_000)
        self._account_timer.timeout.connect(self._refresh_account_balances)
        self._open_orders_timer = QTimer(self)
        self._open_orders_timer.setInterval(1500)
        self._open_orders_timer.timeout.connect(self._refresh_open_orders)

        layout = QVBoxLayout()
        layout.addLayout(self._build_status_row())
        layout.addLayout(self._build_filters())

        counts_row = QHBoxLayout()
        self._shown_label = QLabel("Shown 0 / Total 0")
        self._filter_status_label = QLabel("")
        self._filter_status_label.setStyleSheet("color: #ef4444;")
        counts_row.addWidget(self._shown_label)
        counts_row.addStretch()
        counts_row.addWidget(self._filter_status_label)
        layout.addLayout(counts_row)

        self._table = QTableView()
        self._table.setModel(self._proxy_model)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setSelectionBehavior(QTableView.SelectRows)
        self._table.setEditTriggers(QTableView.NoEditTriggers)
        self._table.doubleClicked.connect(self._handle_double_click)
        self._table.selectionModel().selectionChanged.connect(self._update_selected_pair)
        layout.addWidget(self._table)

        self._selected_label = QLabel("Selected pair: -")
        layout.addWidget(self._selected_label)

        self.setLayout(layout)
        self._set_ai_status()
        self.refresh_account_status()

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

        account_box = QGroupBox("Account (read-only)")
        account_layout = QVBoxLayout()
        self._account_status_label = QLabel("Account: ключи не заданы")
        self._account_balances_label = QLabel("Balances: —")
        self._account_balances_label.setWordWrap(True)
        self._account_show_all_btn = QPushButton("Показать все")
        self._account_show_all_btn.clicked.connect(self._show_all_balances)
        self._account_show_all_btn.setEnabled(False)
        self._open_orders_label = QLabel("Open orders: —")
        self._open_orders_model = QStandardItemModel(0, 7, self)
        self._open_orders_model.setHorizontalHeaderLabels(
            ["ID", "Side", "Price", "OrigQty", "ExecQty", "Status", "Time"]
        )
        self._open_orders_table = QTableView()
        self._open_orders_table.setModel(self._open_orders_model)
        self._open_orders_table.horizontalHeader().setStretchLastSection(True)
        self._open_orders_table.setEditTriggers(QTableView.NoEditTriggers)
        account_layout.addWidget(self._account_status_label)
        account_layout.addWidget(self._account_balances_label)
        account_layout.addWidget(self._account_show_all_btn)
        account_layout.addWidget(self._open_orders_label)
        account_layout.addWidget(self._open_orders_table)
        account_box.setLayout(account_layout)

        grid.addWidget(config_box, 0, 0)
        grid.addWidget(binance_box, 0, 1)
        grid.addWidget(ai_box, 0, 2)
        grid.addWidget(balance_box, 0, 3)
        grid.addWidget(account_box, 0, 4)
        return grid

    def _build_filters(self) -> QHBoxLayout:
        layout = QHBoxLayout()

        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("Search symbol")
        self._search_input.textChanged.connect(self._handle_search_text)

        self._quote_combo = QComboBox()
        self._quote_combo.addItems(["USDT", "USDC", "FDUSD", "EUR"])
        self._quote_combo.setCurrentText(self._app_state.default_quote)
        self._quote_combo.currentTextChanged.connect(self._handle_quote_change)

        self._load_button = QPushButton("Load Pairs")
        self._load_button.clicked.connect(self._handle_load_pairs)
        self._refresh_button = QPushButton("Force refresh from exchange")
        self._refresh_button.clicked.connect(self._handle_refresh_pairs)
        self._cache_status_label = QLabel("Cache: -")
        self._cache_status_label.setStyleSheet("color: #6b7280;")

        layout.addWidget(QLabel("Search"))
        layout.addWidget(self._search_input)
        layout.addWidget(QLabel("Quote"))
        layout.addWidget(self._quote_combo)
        layout.addWidget(self._load_button)
        layout.addWidget(self._refresh_button)
        layout.addWidget(self._cache_status_label)
        layout.addStretch()
        return layout

    def _update_selected_pair(self) -> None:
        selected = self._table.selectionModel().selectedRows()
        if not selected:
            self._selected_label.setText("Selected pair: -")
            self._stop_selected_symbol_stream()
            return
        proxy_index = selected[0]
        source_index = self._proxy_model.mapToSource(proxy_index)
        symbol = self._pairs_model.symbol_for_row(source_index.row())
        if not symbol:
            return
        self._selected_label.setText(f"Selected pair: {symbol}")
        self._start_selected_symbol_stream(symbol)

    def _handle_double_click(self, _: QModelIndex) -> None:
        selected = self._table.selectionModel().selectedRows()
        if not selected:
            return
        proxy_index = selected[0]
        source_index = self._proxy_model.mapToSource(proxy_index)
        symbol = self._pairs_model.symbol_for_row(source_index.row())
        if symbol:
            last_price = self._pairs_model.last_price_for_symbol(symbol)
            self._on_open_pair(symbol, last_price)

    def _run_self_check(self) -> None:
        worker = _Worker(self._http_client.get_time)
        worker.signals.success.connect(self._handle_self_check_success)
        worker.signals.error.connect(self._handle_self_check_error)
        self._thread_pool.start(worker)
        self._run_ai_self_check()
        self._refresh_account_balances()

    def _run_ai_self_check(self) -> None:
        if not self._app_state.openai_key_present:
            self._app_state.ai_connected = False
            self._app_state.ai_checked = False
            self._set_ai_status()
            self._logger.info("AI self-check skipped (missing OpenAI key).")
            return
        self._ai_status_label.setText("AI: CHECKING")
        try:
            client = OpenAIClient(
                api_key=self._app_state.openai_api_key,
                model=self._app_state.openai_model,
                timeout_s=25.0,
                retries=1,
            )
        except RuntimeError as exc:
            self._app_state.ai_connected = False
            self._app_state.ai_checked = True
            self._logger.error("AI self-check failed: %s", exc)
            self._set_ai_status()
            return
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

    def refresh_account_status(self) -> None:
        api_key = self._app_state.binance_api_key.strip()
        api_secret = self._app_state.binance_api_secret.strip()
        if not api_key or not api_secret:
            self._account_client = None
            self._account_status_label.setText("Account: ключи не заданы")
            self._account_balances_label.setText("Balances: —")
            self._account_show_all_btn.setEnabled(False)
            self._open_orders_label.setText("Open orders: —")
            self._open_orders_model.removeRows(0, self._open_orders_model.rowCount())
            self._account_timer.stop()
            self._open_orders_timer.stop()
            return
        self._account_client = BinanceAccountClient(
            base_url=self._config.binance.base_url,
            api_key=api_key,
            api_secret=api_secret,
            recv_window=self._config.binance.recv_window,
            timeout_s=self._config.http.timeout_s,
            retries=self._config.http.retries,
            backoff_base_s=self._config.http.backoff_base_s,
            backoff_max_s=self._config.http.backoff_max_s,
        )
        self._account_status_label.setText("Account: CHECKING")
        self._account_timer.start()
        if self._selected_symbol:
            self._open_orders_timer.start()
        self._refresh_account_balances()

    def _refresh_account_balances(self) -> None:
        if not self._account_client:
            return
        worker = _Worker(self._account_client.get_account_info)
        worker.signals.success.connect(self._handle_account_info)
        worker.signals.error.connect(self._handle_account_error)
        self._thread_pool.start(worker)

    def _handle_account_info(self, result: object, _: int) -> None:
        if not isinstance(result, dict):
            self._handle_account_error("Unexpected account response")
            return
        balances = result.get("balances", [])
        if not isinstance(balances, list):
            balances = []
        parsed: list[dict[str, object]] = []
        for item in balances:
            if not isinstance(item, dict):
                continue
            asset = str(item.get("asset", "")).upper()
            try:
                free = float(item.get("free", 0))
                locked = float(item.get("locked", 0))
            except (TypeError, ValueError):
                continue
            total = free + locked
            parsed.append({"asset": asset, "free": free, "locked": locked, "total": total})
        self._account_balances = parsed
        display = self._select_top_balances(parsed)
        self._account_balances_display = display
        self._account_status_label.setText("Account: CONNECTED")
        self._account_show_all_btn.setEnabled(bool(parsed))
        self._account_balances_label.setText("Balances: " + ", ".join(display) if display else "Balances: —")
        self._account_last_error = None

    def _handle_account_error(self, message: str) -> None:
        summary = self._summarize_error(message)
        self._logger.error("Account read-only error: %s", summary)
        self._account_last_error = summary
        self._account_status_label.setText(f"Account: ERROR ({summary})")

    def _select_top_balances(self, balances: list[dict[str, object]]) -> list[str]:
        priority_assets = ["USDT", "USDC", "FDUSD", "EURI", "EUR", "BTC"]
        by_asset = {item["asset"]: item for item in balances if item.get("asset")}
        selected: list[dict[str, object]] = []
        for asset in priority_assets:
            item = by_asset.get(asset)
            if item and float(item.get("total", 0)) > 0:
                selected.append(item)
        if len(selected) < 6:
            remaining = [
                item for item in balances if item.get("asset") not in {s["asset"] for s in selected}
            ]
            remaining.sort(key=lambda x: float(x.get("total", 0)), reverse=True)
            selected.extend(remaining[: 6 - len(selected)])
        return [f"{item['asset']}: {item['total']:.4f}" for item in selected]

    def _show_all_balances(self) -> None:
        if not self._account_balances:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Все балансы (spot)")
        layout = QVBoxLayout()
        table = QTableView(dialog)
        model = QStandardItemModel(0, 4, dialog)
        model.setHorizontalHeaderLabels(["Asset", "Free", "Locked", "Total"])
        for item in self._account_balances:
            asset = str(item.get("asset", ""))
            free = float(item.get("free", 0))
            locked = float(item.get("locked", 0))
            total = float(item.get("total", 0))
            row = [
                QStandardItem(asset),
                QStandardItem(f"{free:.6f}"),
                QStandardItem(f"{locked:.6f}"),
                QStandardItem(f"{total:.6f}"),
            ]
            model.appendRow(row)
        table.setModel(model)
        table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(table)
        dialog.setLayout(layout)
        dialog.resize(520, 420)
        dialog.exec()

    def _refresh_open_orders(self) -> None:
        if not self._account_client or not self._selected_symbol:
            return
        worker = _Worker(lambda: self._account_client.get_open_orders(self._selected_symbol))
        worker.signals.success.connect(self._handle_open_orders)
        worker.signals.error.connect(self._handle_open_orders_error)
        self._thread_pool.start(worker)

    def _handle_open_orders(self, result: object, _: int) -> None:
        if not isinstance(result, list):
            self._handle_open_orders_error("Unexpected open orders response")
            return
        self._open_orders_model.removeRows(0, self._open_orders_model.rowCount())
        for order in result:
            if not isinstance(order, dict):
                continue
            order_id = str(order.get("orderId", ""))
            side = str(order.get("side", ""))
            price = str(order.get("price", ""))
            orig_qty = str(order.get("origQty", ""))
            exec_qty = str(order.get("executedQty", ""))
            status = str(order.get("status", ""))
            time_raw = order.get("time")
            time_str = self._format_time(time_raw)
            row = [
                QStandardItem(order_id),
                QStandardItem(side),
                QStandardItem(price),
                QStandardItem(orig_qty),
                QStandardItem(exec_qty),
                QStandardItem(status),
                QStandardItem(time_str),
            ]
            self._open_orders_model.appendRow(row)
        self._open_orders_label.setText(f"Open orders: {len(result)}")

    def _handle_open_orders_error(self, message: str) -> None:
        summary = self._summarize_error(message)
        self._logger.warning("Open orders error: %s", summary)
        self._open_orders_label.setText(f"Open orders: ERROR ({summary})")

    @staticmethod
    def _format_time(value: object) -> str:
        if not isinstance(value, int):
            return "—"
        try:
            from datetime import datetime, timezone

            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).strftime("%H:%M:%S")
        except Exception:
            return "—"

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
        self._pairs_model.set_pairs([])
        self._all_pairs = []
        self._cache_partial = False
        self._cache_status_label.setText("Cache: -")
        self._logger.info("Loading pairs...")
        self._binance_status_label.setText("Binance: LOADING")
        worker = _Worker(self._markets_service.load_pairs_cached_all)
        worker.signals.success.connect(self._handle_pairs_loaded_cached)
        worker.signals.error.connect(self._handle_pairs_error)
        self._thread_pool.start(worker)

    def _handle_refresh_pairs(self) -> None:
        self._set_pairs_buttons_enabled(False)
        self._pairs_model.set_pairs([])
        self._all_pairs = []
        self._cache_partial = False
        self._cache_status_label.setText("Cache: Refreshing...")
        self._logger.info("Refreshing pairs...")
        self._binance_status_label.setText("Binance: REFRESHING")
        worker = _Worker(self._markets_service.refresh_pairs_all)
        worker.signals.success.connect(self._handle_pairs_loaded_http)
        worker.signals.error.connect(self._handle_pairs_error)
        self._thread_pool.start(worker)

    def _handle_pairs_loaded_cached(self, payload: object, _: int) -> None:
        if not isinstance(payload, tuple) or len(payload) != 3:
            self._logger.error("Unexpected cached pairs response: %s", payload)
            self._set_pairs_buttons_enabled(True)
            return
        pairs, from_cache, partial = payload
        source = "CACHE" if from_cache else "HTTP"
        self._cache_partial = partial
        self._handle_pairs_loaded(pairs, source)

    def _handle_pairs_loaded_http(self, pairs: object, _: int) -> None:
        self._cache_partial = False
        self._handle_pairs_loaded(pairs, "HTTP")

    def _handle_pairs_loaded(self, pairs: object, source: str) -> None:
        self._set_binance_status_connected()
        if not isinstance(pairs, Iterable):
            self._logger.error("Unexpected pairs response: %s", pairs)
            self._set_pairs_buttons_enabled(True)
            return
        pair_list = [pair for pair in pairs if isinstance(pair, Pair)]
        self._all_pairs = pair_list
        self._pairs_model.set_pairs(pair_list)
        self._apply_filters()
        self._set_pairs_buttons_enabled(True)
        self._logger.info("Loaded %s pairs from %s.", len(pair_list), source)
        self._binance_status_label.setText(f"Binance: CONNECTED ({len(pair_list)} pairs, {source})")
        if source == "CACHE" and self._cache_partial:
            self._cache_status_label.setText("Cache: PARTIAL (force refresh recommended)")
            self._cache_status_label.setStyleSheet("color: #f59e0b;")
        elif source == "CACHE":
            self._cache_status_label.setText("Cache: OK")
            self._cache_status_label.setStyleSheet("color: #16a34a;")
        else:
            self._cache_status_label.setText("Cache: Fresh from exchange")
            self._cache_status_label.setStyleSheet("color: #16a34a;")

    def _handle_pairs_error(self, message: str) -> None:
        self._logger.error("Failed to load pairs: %s", message)
        self._set_binance_status_error(message)
        self._set_pairs_buttons_enabled(True)

    def _apply_filters(self) -> None:
        quote = self._quote_combo.currentText().strip()
        search_text = self._search_input.text().strip()
        self._proxy_model.set_quote_filter(quote)
        self._proxy_model.set_search_text(search_text)
        total_symbols = self._pairs_model.rowCount()
        filtered_symbols = self._proxy_model.rowCount()
        self._shown_label.setText(f"Shown {filtered_symbols} / Total {total_symbols}")
        self._filter_status_label.setText(
            "Pair not present on exchange or filtered" if search_text and filtered_symbols == 0 else ""
        )
        self._logger.info(
            "Pairs filter: total=%s filtered=%s quote=%s search=%s",
            total_symbols,
            filtered_symbols,
            quote,
            search_text,
        )
        if filtered_symbols:
            self._table.selectRow(0)
        else:
            self._selected_label.setText("Selected pair: -")
            self._stop_selected_symbol_stream()

    def _refresh_prices(self) -> None:
        selected = self._table.selectionModel().selectedRows()
        if self._price_update_in_flight or not selected:
            return
        self._price_update_in_flight = True
        if self._latest_price_update and self._latest_price_update.last_price is not None:
            symbol = self._latest_price_update.symbol
            self._pairs_model.update_price(
                symbol,
                f"{self._latest_price_update.last_price:.8f}",
                self._latest_price_update.source,
                str(self._latest_price_update.price_age_ms or "--"),
            )
        self._price_update_in_flight = False

    def _handle_price_update(self, update: PriceUpdate) -> None:
        if self._selected_symbol != update.symbol:
            return
        self._latest_price_update = update

    def _start_selected_symbol_stream(self, symbol: str) -> None:
        if self._selected_symbol == symbol:
            return
        if self._selected_symbol:
            self._stop_selected_symbol_stream()
        self._selected_symbol = symbol
        self._latest_price_update = None
        self._price_manager.register_symbol(symbol)
        self._price_manager.subscribe(symbol, self._handle_price_update)
        self._price_manager.subscribe_status(symbol, self._emit_ws_status)
        self._price_manager.start()
        if not self._price_timer.isActive():
            self._price_timer.start()
        if self._account_client:
            self._open_orders_timer.start()

    def _stop_selected_symbol_stream(self) -> None:
        if not self._selected_symbol:
            self._price_timer.stop()
            self._open_orders_timer.stop()
            return
        symbol = self._selected_symbol
        self._price_manager.unsubscribe(symbol, self._handle_price_update)
        self._price_manager.unsubscribe_status(symbol, self._emit_ws_status)
        self._price_manager.unregister_symbol(symbol)
        self._selected_symbol = None
        self._latest_price_update = None
        self._price_timer.stop()
        self._open_orders_timer.stop()

    def _set_pairs_buttons_enabled(self, enabled: bool) -> None:
        self._load_button.setEnabled(enabled)
        self._refresh_button.setEnabled(enabled)

    def _set_binance_status_connected(self) -> None:
        self._binance_status_label.setText("Binance: CONNECTED")

    def _set_binance_status_error(self, message: str) -> None:
        summary = self._summarize_error(message)
        self._binance_status_label.setText(f"Binance: ERROR ({summary})")

    def _emit_ws_status(self, status: str, message: str | None) -> None:
        self._ws_status_signals.status.emit(status, message or status)

    def _handle_ws_status(self, status: str, message: str) -> None:
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
            return
        if status == "ERROR":
            summary = self._summarize_error(message)
            self._ws_status_label.setText(f"WS: ERROR ({summary})")

    def _handle_quote_change(self, quote: str) -> None:
        self._app_state.default_quote = quote
        self._persist_app_state()
        self._apply_filters()

    def _handle_pnl_period_change(self, period: str) -> None:
        self._app_state.pnl_period = period
        self._persist_app_state()

    def _handle_search_text(self, _: str) -> None:
        self._apply_filters()

    def _persist_app_state(self) -> None:
        if self._app_state.user_config_path is None:
            return
        try:
            self._app_state.save(self._app_state.user_config_path)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("Failed to persist UI state: %s", exc)

    def shutdown(self) -> None:
        self._price_timer.stop()
        self._account_timer.stop()
        self._open_orders_timer.stop()
        self._stop_selected_symbol_stream()

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
