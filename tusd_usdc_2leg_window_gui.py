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


def simulate_2leg(direction: str, amount_in: float, books: dict[str, Book],
                  filters: dict[str, SymbolFilters], fee_bps: float, slip_bps: float) -> dict:
    amount_in_d = d(amount_in)
    if amount_in_d <= 0:
        return {
            "ok": False,
            "out_amount": 0.0,
            "out_per_1": 0.0,
            "bps": float("-inf"),
            "reason": "zero_amount",
            "legs": [],
        }
    fee_factor = d(1) - d(fee_bps + slip_bps) / d(10000)

    if direction == "A":
        sell_symbol = SYMBOL_USDCUSDT
        buy_symbol = SYMBOL_TUSDUSDT
    else:
        sell_symbol = SYMBOL_TUSDUSDT
        buy_symbol = SYMBOL_USDCUSDT

    if sell_symbol not in books or buy_symbol not in books:
        return {
            "ok": False,
            "out_amount": 0.0,
            "out_per_1": 0.0,
            "bps": float("-inf"),
            "reason": "missing_book",
            "legs": [],
        }

    sell_book = books[sell_symbol]
    buy_book = books[buy_symbol]
    sell_filters = filters[sell_symbol]
    buy_filters = filters[buy_symbol]

    sell_price = floor_to_tick(d(sell_book.bid), sell_filters.tick_size)
    if sell_price <= 0:
        return {
            "ok": False,
            "out_amount": 0.0,
            "out_per_1": 0.0,
            "bps": float("-inf"),
            "reason": "sell_price_zero",
            "legs": [],
        }

    qty_sell = floor_to_step(amount_in_d, sell_filters.step_size)
    if qty_sell <= 0:
        return {
            "ok": False,
            "out_amount": 0.0,
            "out_per_1": 0.0,
            "bps": float("-inf"),
            "reason": "rounding_zero_qty_leg1",
            "legs": [],
        }
    if qty_sell < sell_filters.min_qty:
        return {
            "ok": False,
            "out_amount": 0.0,
            "out_per_1": 0.0,
            "bps": float("-inf"),
            "reason": "minQty_leg1",
            "legs": [],
        }

    notional_sell = qty_sell * sell_price
    if notional_sell < sell_filters.min_notional:
        return {
            "ok": False,
            "out_amount": 0.0,
            "out_per_1": 0.0,
            "bps": float("-inf"),
            "reason": "minNotional_leg1",
            "legs": [],
        }

    proceeds_quote = notional_sell * fee_factor

    buy_price = floor_to_tick(d(buy_book.ask), buy_filters.tick_size)
    if buy_price <= 0:
        return {
            "ok": False,
            "out_amount": 0.0,
            "out_per_1": 0.0,
            "bps": float("-inf"),
            "reason": "buy_price_zero",
            "legs": [],
        }

    theoretical_qty = proceeds_quote / buy_price
    qty_buy = floor_to_step(theoretical_qty, buy_filters.step_size)
    if qty_buy <= 0:
        return {
            "ok": False,
            "out_amount": 0.0,
            "out_per_1": 0.0,
            "bps": float("-inf"),
            "reason": "rounding_zero_qty_leg2",
            "legs": [],
        }
    if qty_buy < buy_filters.min_qty:
        return {
            "ok": False,
            "out_amount": 0.0,
            "out_per_1": 0.0,
            "bps": float("-inf"),
            "reason": "minQty_leg2",
            "legs": [],
        }

    notional_buy = qty_buy * buy_price
    if notional_buy < buy_filters.min_notional:
        return {
            "ok": False,
            "out_amount": 0.0,
            "out_per_1": 0.0,
            "bps": float("-inf"),
            "reason": "minNotional_leg2",
            "legs": [],
        }

    leftover_quote = proceeds_quote - notional_buy

    out_amount = qty_buy
    out_per_1 = out_amount / amount_in_d
    bps = (out_per_1 - d(1)) * d(10000)
    return {
        "ok": True,
        "out_amount": float(out_amount),
        "out_per_1": float(out_per_1),
        "bps": float(bps),
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
            reason = f"{exc} (backoff {self._backoff:.1f}s)"
            if self._last_error != reason:
                self._last_error = reason
                self.fetch_error.emit(reason)
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

        controls.addStretch()
        layout.addLayout(controls)

        summary_layout = QtWidgets.QHBoxLayout()
        self.tradable_best_label = QtWidgets.QLabel("Best Tradable: NO")
        self.best_dir_label = QtWidgets.QLabel("Best Dir: -")
        self.sim_bps_label = QtWidgets.QLabel("Sim Bps: N/A")
        self.reason_label = QtWidgets.QLabel("Reason: -")
        summary_layout.addWidget(self.tradable_best_label)
        summary_layout.addWidget(self.best_dir_label)
        summary_layout.addWidget(self.sim_bps_label)
        summary_layout.addWidget(self.reason_label)
        summary_layout.addStretch()
        layout.addLayout(summary_layout)

        self.table = QtWidgets.QTableWidget(2, 8)
        self.table.setHorizontalHeaderLabels([
            "Direction",
            "Tradable",
            "Reason",
            "Sim Amount",
            "Out per 1",
            "Bps",
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

        for row, direction in enumerate(["A", "B"]):
            result = simulate_2leg(direction, sim_amount, self.books, self.filters, fee_bps, slip_bps)
            results[direction] = result
            tradable = "YES" if result.get("ok") else "NO"
            reason = result.get("reason", "")
            out_per_1 = result.get("out_per_1", 0.0)
            bps = result.get("bps", float("-inf"))

            if result.get("ok") and bps > best_bps:
                best_bps = bps
                best_dir = direction

            self.table.setItem(row, 1, QtWidgets.QTableWidgetItem(tradable))
            self.table.setItem(row, 2, QtWidgets.QTableWidgetItem(reason))
            self.table.setItem(row, 3, QtWidgets.QTableWidgetItem(f"{sim_amount:.4f}"))
            self.table.setItem(row, 4, QtWidgets.QTableWidgetItem(f"{out_per_1:.8f}"))
            if bps == float("-inf"):
                self.table.setItem(row, 5, QtWidgets.QTableWidgetItem("N/A"))
            else:
                self.table.setItem(row, 5, QtWidgets.QTableWidgetItem(f"{bps:.2f}"))

            legs = result.get("legs", [])
            leg1 = legs[0] if len(legs) > 0 else None
            leg2 = legs[1] if len(legs) > 1 else None
            leg1_text = "" if not leg1 else (
                f"{leg1['symbol']} qty={leg1['qty']:.6f} px={leg1['price']:.6f}"
            )
            leg2_text = "" if not leg2 else (
                f"{leg2['symbol']} qty={leg2['qty']:.6f} px={leg2['price']:.6f}"
            )
            self.table.setItem(row, 6, QtWidgets.QTableWidgetItem(leg1_text))
            self.table.setItem(row, 7, QtWidgets.QTableWidgetItem(leg2_text))

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
            self.sim_bps_label.setText(f"Sim Bps: {best_bps:.2f}")
            self.reason_label.setText("Reason: -")
        else:
            reason_a = results.get("A", {}).get("reason", "-")
            reason_b = results.get("B", {}).get("reason", "-")
            reason = reason_a if reason_a == reason_b else f"A:{reason_a} B:{reason_b}"
            self.tradable_best_label.setText("Best Tradable: NO")
            self.best_dir_label.setText("Best Dir: -")
            self.sim_bps_label.setText("Sim Bps: N/A")
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
    filters = fetch_exchange_info_filters(SYMBOLS)
    app = QtWidgets.QApplication(sys.argv)
    window = MainWindow(filters)
    window.resize(1100, 600)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

# Manual test checklist
# - Launch the app and confirm filters log once with tick/step/minQty/minNotional per symbol.
# - Click Start and verify the GUI remains responsive while updates arrive.
# - Confirm data_not_ok logs only once when bid/ask is missing, then clears on recovery.
# - Validate network_error backoff logs increase on repeated failures and resolve on recovery.
# - Adjust Sim amount upward/downward to trigger minQty and minNotional failures.
# - Verify tradable state change logs only when a direction flips between YES/NO.
# - Confirm Best Tradable/Best Dir/Sim Bps fields update with simulation results.
# - Ensure window OPEN/CLOSE logs follow sim_bps threshold and ok==true only.
# - Click Stop and confirm updates halt without thread errors.
# - Close the window and confirm clean shutdown without event loop warnings.
