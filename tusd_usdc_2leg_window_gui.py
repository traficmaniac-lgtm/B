import json
import sys
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR

import requests
from PySide6 import QtCore, QtWidgets

API_BASE = "https://api.binance.com"
SYMBOL_USDCUSDT = "USDCUSDT"
SYMBOL_TUSDUSDT = "TUSDUSDT"
SYMBOLS = [SYMBOL_USDCUSDT, SYMBOL_TUSDUSDT]


@dataclass
class SymbolFilters:
    symbol: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    min_notional: Decimal


@dataclass
class Book:
    bid: float
    ask: float


def d(value) -> Decimal:
    return Decimal(str(value))


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    floored = (value / step).to_integral_value(rounding=ROUND_FLOOR) * step
    return floored


def floor_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    return floor_to_step(price, tick)


def fetch_exchange_info_filters(symbols: list[str]) -> dict[str, SymbolFilters]:
    url = f"{API_BASE}/api/v3/exchangeInfo"
    params = {"symbols": json.dumps(symbols)}
    response = requests.get(url, params=params, timeout=10)
    response.raise_for_status()
    data = response.json()
    filters_map: dict[str, SymbolFilters] = {}
    for symbol_info in data.get("symbols", []):
        symbol = symbol_info.get("symbol")
        tick_size = d(0)
        step_size = d(0)
        min_qty = d(0)
        min_notional = d(0)
        for filt in symbol_info.get("filters", []):
            filt_type = filt.get("filterType")
            if filt_type == "PRICE_FILTER":
                tick_size = d(filt.get("tickSize", 0))
            elif filt_type == "LOT_SIZE":
                step_size = d(filt.get("stepSize", 0))
                min_qty = d(filt.get("minQty", 0))
            elif filt_type in {"MIN_NOTIONAL", "NOTIONAL"}:
                min_notional = d(filt.get("minNotional", filt.get("notional", 0)))
        filters_map[symbol] = SymbolFilters(
            symbol=symbol,
            tick_size=tick_size,
            step_size=step_size,
            min_qty=min_qty,
            min_notional=min_notional,
        )
    return filters_map


def get_two_books_strict(session: requests.Session) -> dict[str, Book]:
    books: dict[str, Book] = {}
    for symbol in SYMBOLS:
        url = f"{API_BASE}/api/v3/ticker/bookTicker"
        response = session.get(url, params={"symbol": symbol}, timeout=10)
        response.raise_for_status()
        data = response.json()
        bid = float(data.get("bidPrice", 0))
        ask = float(data.get("askPrice", 0))
        if bid <= 0 or ask <= 0:
            continue
        books[symbol] = Book(bid=bid, ask=ask)
    return books


def _empty_sim(reason: str) -> dict:
    return {
        "ok": False,
        "out_amount": 0.0,
        "out_per_1": 0.0,
        "raw_bps": float("-inf"),
        "value_in_usdt": 0.0,
        "value_out_usdt": 0.0,
        "leftover_usdt": 0.0,
        "profit_usdt": 0.0,
        "profit_bps": float("-inf"),
        "reason": reason,
        "legs": [],
    }


def simulate_2leg(direction: str, amount_in: float, books: dict[str, Book],
                  filters: dict[str, SymbolFilters], fee_bps: float, slip_bps: float) -> dict:
    amount_in_d = d(amount_in)
    if amount_in_d <= 0:
        return _empty_sim("zero_amount")
    fee_factor = d(1) - d(fee_bps + slip_bps) / d(10000)

    if direction == "A":
        sell_symbol = SYMBOL_USDCUSDT
        buy_symbol = SYMBOL_TUSDUSDT
        output_symbol = SYMBOL_TUSDUSDT
    else:
        sell_symbol = SYMBOL_TUSDUSDT
        buy_symbol = SYMBOL_USDCUSDT
        output_symbol = SYMBOL_USDCUSDT

    if sell_symbol not in books or buy_symbol not in books:
        return _empty_sim("missing_book")

    sell_book = books[sell_symbol]
    buy_book = books[buy_symbol]
    sell_filters = filters[sell_symbol]
    buy_filters = filters[buy_symbol]

    sell_price = floor_to_tick(d(sell_book.bid), sell_filters.tick_size)
    if sell_price <= 0:
        return _empty_sim("sell_price_zero")

    qty_sell = floor_to_step(amount_in_d, sell_filters.step_size)
    if qty_sell <= 0:
        return _empty_sim("rounding_zero_qty_leg1")
    if qty_sell < sell_filters.min_qty:
        return _empty_sim("minQty_leg1")

    notional_sell = qty_sell * sell_price
    if notional_sell < sell_filters.min_notional:
        return _empty_sim("minNotional_leg1")

    proceeds_quote = notional_sell * fee_factor

    buy_price = floor_to_tick(d(buy_book.ask), buy_filters.tick_size)
    if buy_price <= 0:
        return _empty_sim("buy_price_zero")

    theoretical_qty = proceeds_quote / buy_price
    qty_buy = floor_to_step(theoretical_qty, buy_filters.step_size)
    if qty_buy <= 0:
        return _empty_sim("rounding_zero_qty_leg2")
    if qty_buy < buy_filters.min_qty:
        return _empty_sim("minQty_leg2")

    notional_buy = qty_buy * buy_price
    if notional_buy < buy_filters.min_notional:
        return _empty_sim("minNotional_leg2")

    leftover_quote = proceeds_quote - notional_buy

    out_amount = qty_buy
    out_per_1 = out_amount / amount_in_d
    raw_bps = (out_per_1 - d(1)) * d(10000)

    output_filters = filters[output_symbol]
    output_bid = floor_to_tick(d(books[output_symbol].bid), output_filters.tick_size)
    if output_bid <= 0:
        return _empty_sim("output_bid_zero")

    value_in_usdt = amount_in_d * sell_price
    value_out_usdt = out_amount * output_bid + leftover_quote
    profit_usdt = value_out_usdt - value_in_usdt
    profit_bps = (profit_usdt / value_in_usdt * d(10000)) if value_in_usdt > 0 else d(0)

    return {
        "ok": True,
        "out_amount": float(out_amount),
        "out_per_1": float(out_per_1),
        "raw_bps": float(raw_bps),
        "value_in_usdt": float(value_in_usdt),
        "value_out_usdt": float(value_out_usdt),
        "leftover_usdt": float(leftover_quote),
        "profit_usdt": float(profit_usdt),
        "profit_bps": float(profit_bps),
        "reason": "",
        "legs": [
            {
                "symbol": sell_symbol,
                "side": "SELL",
                "qty": float(qty_sell),
                "price": float(sell_price),
                "notional": float(notional_sell),
                "leftover": 0.0,
            },
            {
                "symbol": buy_symbol,
                "side": "BUY",
                "qty": float(qty_buy),
                "price": float(buy_price),
                "notional": float(notional_buy),
                "leftover": float(leftover_quote),
            },
        ],
    }


class BookWorker(QtCore.QObject):
    books_ready = QtCore.Signal(dict, float, float)
    data_not_ok = QtCore.Signal(str)
    fetch_error = QtCore.Signal(str)
    fetch_recovered = QtCore.Signal()

    def __init__(self) -> None:
        super().__init__()
        self.session = requests.Session()
        self.active = False
        self._busy = False
        self._backoff = 0.5
        self._max_backoff = 5.0
        self._next_allowed = 0.0
        self._last_error = None
        self._had_error = False

    @QtCore.Slot()
    def request_tick(self) -> None:
        now = time.time()
        if not self.active or self._busy:
            return
        if now < self._next_allowed:
            return
        self._busy = True
        start = time.time()
        try:
            books = get_two_books_strict(self.session)
            if len(books) < 2:
                self.data_not_ok.emit("missing_bid_ask")
            else:
                age_ms = (time.time() - start) * 1000.0
                self.books_ready.emit(books, age_ms, age_ms)
                if self._had_error:
                    self._had_error = False
                    self._last_error = None
                    self._backoff = 0.5
                    self.fetch_recovered.emit()
        except Exception as exc:
            self._had_error = True
            error_text = str(exc)
            if self._last_error != error_text:
                self._last_error = error_text
                self.fetch_error.emit(f"{error_text} (backoff {self._backoff:.1f}s)")
            self._next_allowed = time.time() + self._backoff
            self._backoff = min(self._backoff * 2, self._max_backoff)
        finally:
            self._busy = False


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, filters: dict[str, SymbolFilters]) -> None:
        super().__init__()
        self.setWindowTitle("USDC/TUSD 2-Leg Window Scanner")
        self.filters = filters
        self.books: dict[str, Book] = {}
        self.window_open = False
        self.last_tradable = {"A": None, "B": None}
        self.last_data_issue = None
        self.last_fetch_error = None

        self._build_ui()
        self._init_worker()
        self._log_filters()

    def _build_ui(self) -> None:
        central = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(central)

        controls = QtWidgets.QHBoxLayout()
        self.start_button = QtWidgets.QPushButton("Start")
        self.stop_button = QtWidgets.QPushButton("Stop")
        self.stop_button.setEnabled(False)
        controls.addWidget(self.start_button)
        controls.addWidget(self.stop_button)

        self.min_bps = QtWidgets.QDoubleSpinBox()
        self.min_bps.setDecimals(2)
        self.min_bps.setRange(-5000, 5000)
        self.min_bps.setValue(0.5)
        self.min_bps.setSuffix(" bps min")
        controls.addWidget(self.min_bps)

        self.fee_bps = QtWidgets.QDoubleSpinBox()
        self.fee_bps.setDecimals(2)
        self.fee_bps.setRange(0, 100)
        self.fee_bps.setValue(1.0)
        self.fee_bps.setSuffix(" bps fee")
        controls.addWidget(self.fee_bps)

        self.slip_bps = QtWidgets.QDoubleSpinBox()
        self.slip_bps.setDecimals(2)
        self.slip_bps.setRange(0, 100)
        self.slip_bps.setValue(0.5)
        self.slip_bps.setSuffix(" bps slip")
        controls.addWidget(self.slip_bps)

        self.sim_amount = QtWidgets.QDoubleSpinBox()
        self.sim_amount.setDecimals(4)
        self.sim_amount.setRange(0.0, 1_000_000.0)
        self.sim_amount.setValue(1000.0)
        self.sim_amount.setSuffix(" sim amount")
        controls.addWidget(self.sim_amount)

        self.show_raw = QtWidgets.QCheckBox("Show raw vs realizable")
        self.show_raw.setChecked(False)
        controls.addWidget(self.show_raw)

        controls.addStretch()
        layout.addLayout(controls)

        summary_layout = QtWidgets.QHBoxLayout()
        self.tradable_best_label = QtWidgets.QLabel("Best Tradable: NO")
        self.best_dir_label = QtWidgets.QLabel("Best Dir: -")
        self.best_profit_bps_label = QtWidgets.QLabel("Best Profit Bps: N/A")
        self.best_profit_usdt_label = QtWidgets.QLabel("Best Profit USDT: N/A")
        self.reason_label = QtWidgets.QLabel("Reason: -")
        summary_layout.addWidget(self.tradable_best_label)
        summary_layout.addWidget(self.best_dir_label)
        summary_layout.addWidget(self.best_profit_bps_label)
        summary_layout.addWidget(self.best_profit_usdt_label)
        summary_layout.addWidget(self.reason_label)
        summary_layout.addStretch()
        layout.addLayout(summary_layout)

        self.table = QtWidgets.QTableWidget(2, 12)
        self.table.setHorizontalHeaderLabels([
            "Direction",
            "Tradable",
            "Reason",
            "Value In USDT",
            "Value Out USDT",
            "Leftover USDT",
            "Profit USDT",
            "Profit Bps",
            "Raw Out/1",
            "Raw Bps",
            "Leg1",
            "Leg2",
        ])
        for row, direction in enumerate(["A", "B"]):
            self.table.setItem(row, 0, QtWidgets.QTableWidgetItem(direction))
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)

        self.log_box = QtWidgets.QTextEdit()
        self.log_box.setReadOnly(True)
        layout.addWidget(self.log_box)

        self.setCentralWidget(central)

        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(1000)

        self.start_button.clicked.connect(self._start)
        self.stop_button.clicked.connect(self._stop)
        self.timer.timeout.connect(self._request_tick)
        self.show_raw.toggled.connect(self._toggle_raw_columns)
        self._toggle_raw_columns(self.show_raw.isChecked())

    def _toggle_raw_columns(self, show: bool) -> None:
        self.table.setColumnHidden(8, not show)
        self.table.setColumnHidden(9, not show)

    def _init_worker(self) -> None:
        self.worker_thread = QtCore.QThread(self)
        self.worker = BookWorker()
        self.worker.moveToThread(self.worker_thread)
        self.worker.books_ready.connect(self._on_books_ready)
        self.worker.data_not_ok.connect(self._on_data_not_ok)
        self.worker.fetch_error.connect(self._on_fetch_error)
        self.worker.fetch_recovered.connect(self._on_fetch_recovered)
        self.worker_thread.start()

    def _log_filters(self) -> None:
        for symbol, filt in self.filters.items():
            self._log(
                f"filters {symbol}: tick={filt.tick_size} step={filt.step_size} "
                f"minQty={filt.min_qty} minNotional={filt.min_notional}"
            )

    def _log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.log_box.append(f"[{timestamp}] {message}")

    def _start(self) -> None:
        self.worker.active = True
        self.timer.start()
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self._log("scanner started")

    def _stop(self) -> None:
        self.timer.stop()
        self.worker.active = False
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self._log("scanner stopped")

    def _request_tick(self) -> None:
        QtCore.QMetaObject.invokeMethod(self.worker, "request_tick", QtCore.Qt.QueuedConnection)

    def _on_data_not_ok(self, reason: str) -> None:
        if self.last_data_issue != reason:
            self.last_data_issue = reason
            self._log(f"data_not_ok: {reason}")

    def _on_fetch_error(self, reason: str) -> None:
        if self.last_fetch_error != reason:
            self.last_fetch_error = reason
            self._log(f"network_error: {reason}")

    def _on_fetch_recovered(self) -> None:
        if self.last_fetch_error is not None:
            self.last_fetch_error = None
            self._log("network_error resolved")

    def _on_books_ready(self, books: dict[str, Book], age_ms_usdc: float, age_ms_tusd: float) -> None:
        self.books = books
        self.last_data_issue = None
        self._refresh_simulations()

    def _refresh_simulations(self) -> None:
        sim_amount = self.sim_amount.value()
        fee_bps = self.fee_bps.value()
        slip_bps = self.slip_bps.value()
        min_bps = self.min_bps.value()

        results = {}
        best_bps = float("-inf")
        best_dir = None
        best_profit_usdt = 0.0

        for row, direction in enumerate(["A", "B"]):
            result = simulate_2leg(direction, sim_amount, self.books, self.filters, fee_bps, slip_bps)
            results[direction] = result
            tradable = "YES" if result.get("ok") else "NO"
            reason = result.get("reason", "")
            value_in = result.get("value_in_usdt", 0.0)
            value_out = result.get("value_out_usdt", 0.0)
            leftover = result.get("leftover_usdt", 0.0)
            profit_usdt = result.get("profit_usdt", 0.0)
            profit_bps = result.get("profit_bps", float("-inf"))
            raw_out_per_1 = result.get("out_per_1", 0.0)
            raw_bps = result.get("raw_bps", float("-inf"))

            if result.get("ok") and profit_bps > best_bps:
                best_bps = profit_bps
                best_dir = direction
                best_profit_usdt = profit_usdt

            self.table.setItem(row, 1, QtWidgets.QTableWidgetItem(tradable))
            self.table.setItem(row, 2, QtWidgets.QTableWidgetItem(reason))
            self.table.setItem(row, 3, QtWidgets.QTableWidgetItem(f"{value_in:.6f}"))
            self.table.setItem(row, 4, QtWidgets.QTableWidgetItem(f"{value_out:.6f}"))
            self.table.setItem(row, 5, QtWidgets.QTableWidgetItem(f"{leftover:.6f}"))
            self.table.setItem(row, 6, QtWidgets.QTableWidgetItem(f"{profit_usdt:.6f}"))
            if profit_bps == float("-inf"):
                self.table.setItem(row, 7, QtWidgets.QTableWidgetItem("N/A"))
            else:
                self.table.setItem(row, 7, QtWidgets.QTableWidgetItem(f"{profit_bps:.2f}"))

            self.table.setItem(row, 8, QtWidgets.QTableWidgetItem(f"{raw_out_per_1:.8f}"))
            if raw_bps == float("-inf"):
                self.table.setItem(row, 9, QtWidgets.QTableWidgetItem("N/A"))
            else:
                self.table.setItem(row, 9, QtWidgets.QTableWidgetItem(f"{raw_bps:.2f}"))

            legs = result.get("legs", [])
            leg1 = legs[0] if len(legs) > 0 else None
            leg2 = legs[1] if len(legs) > 1 else None
            leg1_text = "" if not leg1 else (
                f"{leg1['symbol']} qty={leg1['qty']:.6f} px={leg1['price']:.6f}"
            )
            leg2_text = "" if not leg2 else (
                f"{leg2['symbol']} qty={leg2['qty']:.6f} px={leg2['price']:.6f}"
            )
            self.table.setItem(row, 10, QtWidgets.QTableWidgetItem(leg1_text))
            self.table.setItem(row, 11, QtWidgets.QTableWidgetItem(leg2_text))

            prev = self.last_tradable.get(direction)
            if prev is None:
                self.last_tradable[direction] = result.get("ok")
            elif prev != result.get("ok"):
                state = "tradable" if result.get("ok") else "untradable"
                self._log(f"direction {direction} became {state}")
                self.last_tradable[direction] = result.get("ok")

        if best_dir is not None:
            self.tradable_best_label.setText("Best Tradable: YES")
            self.best_dir_label.setText(f"Best Dir: {best_dir}")
            self.best_profit_bps_label.setText(f"Best Profit Bps: {best_bps:.2f}")
            self.best_profit_usdt_label.setText(f"Best Profit USDT: {best_profit_usdt:.6f}")
            self.reason_label.setText("Reason: -")
        else:
            reason_a = results.get("A", {}).get("reason", "-")
            reason_b = results.get("B", {}).get("reason", "-")
            reason = reason_a if reason_a == reason_b else f"A:{reason_a} B:{reason_b}"
            self.tradable_best_label.setText("Best Tradable: NO")
            self.best_dir_label.setText("Best Dir: -")
            self.best_profit_bps_label.setText("Best Profit Bps: N/A")
            self.best_profit_usdt_label.setText("Best Profit USDT: N/A")
            self.reason_label.setText(f"Reason: {reason}")

        should_open = best_dir is not None and best_bps >= min_bps
        if should_open and not self.window_open:
            self.window_open = True
            self._log(f"window OPEN dir={best_dir} bps={best_bps:.2f}")
        elif not should_open and self.window_open:
            self.window_open = False
            self._log("window CLOSE")

    def closeEvent(self, event) -> None:
        self.timer.stop()
        self.worker.active = False
        self.worker_thread.quit()
        self.worker_thread.wait(3000)
        super().closeEvent(event)


def main() -> None:
    try:
        filters = fetch_exchange_info_filters(SYMBOLS)
        app = QtWidgets.QApplication(sys.argv)
        window = MainWindow(filters)
        window.resize(1300, 600)
        window.show()
        print("BOOT OK: USDC/TUSD Window Scanner started")
        sys.exit(app.exec())
    except Exception as exc:
        print(f"BOOT FAIL: {exc}")
        raise


if __name__ == "__main__":
    main()

# Manual test checklist
# - Launch the app and confirm filters log once with tick/step/minQty/minNotional per symbol.
# - Confirm BOOT OK prints after the window shows.
# - Click Start and verify the GUI remains responsive while updates arrive.
# - Confirm data_not_ok logs only once when bid/ask is missing, then clears on recovery.
# - Validate network_error backoff logs only on error changes and resolve on recovery.
# - Adjust Sim amount upward/downward to trigger minQty and minNotional failures.
# - Verify tradable state change logs only when a direction flips between YES/NO.
# - Confirm Value In/Out, Leftover, Profit USDT, Profit Bps update with realizable results.
# - Toggle "Show raw vs realizable" to compare raw vs realizable bps.
# - Ensure window OPEN/CLOSE logs follow Profit Bps threshold and ok==true only.
# - Click Stop and confirm updates halt without thread errors.
# - Close the window and confirm clean shutdown without event loop warnings.
