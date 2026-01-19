from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.gui.models.app_state import AppState
from src.gui.pair_workspace_window import PairWorkspaceWindow


class MarketsTab(QWidget):
    def __init__(self, app_state: AppState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._app_state = app_state
        self._workspace_windows: list[PairWorkspaceWindow] = []

        main_layout = QVBoxLayout()
        main_layout.addLayout(self._build_controls())

        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["Symbol", "Price", "Source", "Updated(ms)"])
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.itemSelectionChanged.connect(self._update_selected_pair)
        self._table.itemDoubleClicked.connect(self._open_pair_workspace)
        main_layout.addWidget(self._table)

        self._selected_label = QLabel("Selected pair: BTCUSDT")
        main_layout.addWidget(self._selected_label)

        self.setLayout(main_layout)
        self._load_demo_rows()

    def _build_controls(self) -> QHBoxLayout:
        layout = QHBoxLayout()

        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("Search symbol")

        self._quote_combo = QComboBox()
        self._quote_combo.addItems(["USDT", "USDC", "FDUSD", "EUR"])

        self._load_button = QPushButton("Load Pairs")
        self._load_button.setEnabled(False)

        layout.addWidget(QLabel("Search"))
        layout.addWidget(self._search_input)
        layout.addWidget(QLabel("Quote"))
        layout.addWidget(self._quote_combo)
        layout.addWidget(self._load_button)
        layout.addStretch()
        return layout

    def _load_demo_rows(self) -> None:
        demo_rows = [
            ("BTCUSDT", "68250.12", "DEMO", "120"),
            ("ETHUSDT", "3580.45", "DEMO", "210"),
            ("BNBUSDT", "604.80", "DEMO", "98"),
            ("SOLUSDT", "148.25", "DEMO", "305"),
            ("ADAUSDT", "0.4521", "DEMO", "412"),
            ("XRPUSDT", "0.6123", "DEMO", "255"),
            ("DOGEUSDT", "0.1581", "DEMO", "160"),
            ("AVAXUSDT", "38.44", "DEMO", "142"),
        ]
        self._table.setRowCount(len(demo_rows))
        for row_index, row in enumerate(demo_rows):
            for column_index, value in enumerate(row):
                item = QTableWidgetItem(value)
                item.setTextAlignment(Qt.AlignCenter)
                self._table.setItem(row_index, column_index, item)

    def _update_selected_pair(self) -> None:
        selected_items = self._table.selectedItems()
        if not selected_items:
            return
        symbol = selected_items[0].text()
        self._selected_label.setText(f"Selected pair: {symbol}")

    def _open_pair_workspace(self) -> None:
        selected_items = self._table.selectedItems()
        if not selected_items:
            return
        symbol = selected_items[0].text()
        window = PairWorkspaceWindow(symbol=symbol, app_state=self._app_state, parent=self)
        window.show()
        self._workspace_windows.append(window)
