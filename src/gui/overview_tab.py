from __future__ import annotations

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
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)
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
        on_open_pair: Callable[[str], None],
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
        self._account_last_error: str | None = None
        self._account_timer = QTimer(self)
        self._account_timer.setInterval(10_000)
        self._account_timer.timeout.connect(self._refresh_account_balances)

        layout = QVBoxLayout()

        top_widget = QWidget()
        top_layout = QVBoxLayout(top_widget)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(6)
        top_layout.addLayout(self._build_status_row())
        top_layout.addLayout(self._build_filters())

        counts_row = QHBoxLayout()
        self._shown_label = QLabel("Shown 0 / Total 0")
        self._filter_status_label = QLabel("")
        self._filter_status_label.setStyleSheet("color: #ef4444;")
        counts_row.addWidget(self._shown_label)
        counts_row.addStretch()
        counts_row.addWidget(self._filter_status_label)
        top_layout.addLayout(counts_row)

        self._table = QTableView()
        self._table.setModel(self._proxy_model)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setSelectionBehavior(QTableView.SelectRows)
        self._table.setEditTriggers(QTableView.NoEditTriggers)
        self._table.doubleClicked.connect(self._handle_double_click)
        self._table.selectionModel().selectionChanged.connect(self._update_selected_pair)
        self._table.setSortingEnabled(True)

        splitter = QSplitter(Qt.Vertical)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(top_widget)
        splitter.addWidget(self._table)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        layout.addWidget(splitter)

        self.setLayout(layout)
        self.refresh_account_status()

    def _build_status_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addLayout(self._build_status_badges())
        row.addStretch()

        self._self_check_button = QPushButton("Self-check")
        self._self_check_button.clicked.connect(self._run_self_check)
        row.addWidget(self._self_check_button, alignment=Qt.AlignRight)
        return row

    def _build_status_badges(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)
        self._status_badges: dict[str, QLabel] = {}
        for name in ["Config", "Binance", "WS", "Account", "Cache"]:
            badge = QLabel()
            badge.setStyleSheet(
                "QLabel {"
                "border: 1px solid #e5e7eb;"
                "border-radius: 10px;"
                "padding: 2px 8px;"
                "font-size: 11px;"
                "}"
            )
            row.addWidget(badge)
            self._status_badges[name] = badge
        self._set_status_badge("Config", "✔", "OK", "#16a34a")
        self._set_status_badge("Binance", "✖", "LOST", "#dc2626")
        self._set_status_badge("WS", "✖", "LOST", "#dc2626")
        self._set_status_badge("Account", "✖", "LOST", "#dc2626")
        self._set_status_badge("Cache", "●", "DEGRADED", "#6b7280")
        return row

    def _set_status_badge(self, name: str, icon: str, text: str, color: str) -> None:
        badge = self._status_badges.get(name)
        if not badge:
            return
        badge.setText(f"{icon} {name} {text}")
        badge.setStyleSheet(
            "QLabel {"
            "border: 1px solid #e5e7eb;"
            "border-radius: 10px;"
            "padding: 2px 8px;"
            "font-size: 11px;"
            f"color: {color};"
            "}"
        )

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

        layout.addWidget(QLabel("Search"))
        layout.addWidget(self._search_input)
        layout.addWidget(QLabel("Quote"))
        layout.addWidget(self._quote_combo)
        layout.addWidget(self._load_button)
        layout.addWidget(self._refresh_button)
        layout.addStretch()
        return layout

    def _update_selected_pair(self) -> None:
        selected = self._table.selectionModel().selectedRows()
        if not selected:
            self._stop_selected_symbol_stream()
            return
        proxy_index = selected[0]
        source_index = self._proxy_model.mapToSource(proxy_index)
        symbol = self._pairs_model.symbol_for_row(source_index.row())
        if not symbol:
            return
        self._start_selected_symbol_stream(symbol)

    def _handle_double_click(self, _: QModelIndex) -> None:
        selected = self._table.selectionModel().selectedRows()
        if not selected:
            return
        proxy_index = selected[0]
        source_index = self._proxy_model.mapToSource(proxy_index)
        symbol = self._pairs_model.symbol_for_row(source_index.row())
        if symbol:
            self._on_open_pair(symbol)

    def _run_self_check(self) -> None:
        worker = _Worker(self._http_client.get_time)
        worker.signals.success.connect(self._handle_self_check_success)
        worker.signals.error.connect(self._handle_self_check_error)
        self._thread_pool.start(worker)
        self._refresh_account_balances()

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
        api_key, api_secret = self._app_state.get_binance_keys()
        if not api_key or not api_secret:
            self._account_client = None
            self._account_timer.stop()
            self._set_status_badge("Account", "✖", "NOT CONNECTED", "#dc2626")
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
        self._set_status_badge("Account", "●", "DEGRADED", "#6b7280")
        self._account_timer.start()
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
        self._set_status_badge("Account", "✔", "OK", "#16a34a")
        self._account_last_error = None

    def _handle_account_error(self, message: str) -> None:
        summary = self._summarize_error(message)
        self._logger.error("Account read-only error: %s", summary)
        self._account_last_error = summary
        message_lower = message.lower()
        if (
            "401" in message_lower
            or "403" in message_lower
            or "unauthorized" in message_lower
            or "forbidden" in message_lower
        ):
            self._set_status_badge("Account", "✖", "NO PERMISSION", "#dc2626")
            return
        self._set_status_badge("Account", "⚠", "WARNING", "#f59e0b")



    def _handle_load_pairs(self) -> None:
        self._set_pairs_buttons_enabled(False)
        self._pairs_model.set_pairs([])
        self._all_pairs = []
        self._cache_partial = False
        self._logger.info("Loading pairs...")
        self._set_status_badge("Binance", "●", "DEGRADED", "#6b7280")
        self._set_status_badge("Cache", "●", "DEGRADED", "#6b7280")
        worker = _Worker(self._markets_service.load_pairs_cached_all)
        worker.signals.success.connect(self._handle_pairs_loaded_cached)
        worker.signals.error.connect(self._handle_pairs_error)
        self._thread_pool.start(worker)

    def _handle_refresh_pairs(self) -> None:
        self._set_pairs_buttons_enabled(False)
        self._pairs_model.set_pairs([])
        self._all_pairs = []
        self._cache_partial = False
        self._logger.info("Refreshing pairs...")
        self._set_status_badge("Binance", "●", "DEGRADED", "#6b7280")
        self._set_status_badge("Cache", "●", "DEGRADED", "#6b7280")
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
        if source == "CACHE" and self._cache_partial:
            self._set_status_badge("Cache", "⚠", "DEGRADED", "#f59e0b")
        elif source == "CACHE":
            self._set_status_badge("Cache", "✔", "OK", "#16a34a")
        else:
            self._set_status_badge("Cache", "✔", "OK", "#16a34a")
        self._set_binance_status_connected()

    def _handle_pairs_error(self, message: str) -> None:
        self._logger.error("Failed to load pairs: %s", message)
        self._set_binance_status_error(message)
        self._set_status_badge("Cache", "✖", "LOST", "#dc2626")
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

    def _stop_selected_symbol_stream(self) -> None:
        if not self._selected_symbol:
            self._price_timer.stop()
            return
        symbol = self._selected_symbol
        self._price_manager.unsubscribe(symbol, self._handle_price_update)
        self._price_manager.unsubscribe_status(symbol, self._emit_ws_status)
        self._price_manager.unregister_symbol(symbol)
        self._selected_symbol = None
        self._latest_price_update = None
        self._price_timer.stop()

    def _set_pairs_buttons_enabled(self, enabled: bool) -> None:
        self._load_button.setEnabled(enabled)
        self._refresh_button.setEnabled(enabled)

    def _set_binance_status_connected(self) -> None:
        self._set_status_badge("Binance", "✔", "OK", "#16a34a")

    def _set_binance_status_error(self, message: str) -> None:
        summary = self._summarize_error(message)
        self._set_status_badge("Binance", "✖", "LOST", "#dc2626")
        self._logger.warning("Binance status error: %s", summary)

    def _emit_ws_status(self, status: str, message: str | None) -> None:
        self._ws_status_signals.status.emit(status, message or status)

    def _handle_ws_status(self, status: str, message: str) -> None:
        if status == WS_CONNECTED:
            self._set_status_badge("WS", "✔", "OK", "#16a34a")
            return
        if status == WS_DEGRADED:
            self._set_status_badge("WS", "⚠", "DEGRADED", "#f59e0b")
            return
        if status == WS_LOST:
            self._set_status_badge("WS", "✖", "LOST", "#dc2626")
            return
        if status == "ERROR":
            summary = self._summarize_error(message)
            self._set_status_badge("WS", "✖", "LOST", "#dc2626")
            self._logger.warning("WS status error: %s", summary)

    def _handle_quote_change(self, quote: str) -> None:
        self._app_state.default_quote = quote
        self._persist_app_state()
        self._apply_filters()

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
        self._stop_selected_symbol_stream()

    @staticmethod
    def _summarize_error(message: str, max_len: int = 80) -> str:
        summary = " ".join(message.split())
        if len(summary) > max_len:
            return summary[: max_len - 3] + "..."
        return summary
