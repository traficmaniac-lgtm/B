from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QMainWindow, QVBoxLayout, QWidget


class AdvancedAIModeWindow(QMainWindow):
    def __init__(self, symbol: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._symbol = symbol

        self.setWindowTitle(f"Advanced AI Mode — {symbol}")
        self.resize(720, 420)

        central = QWidget()
        layout = QVBoxLayout()

        label = QLabel(
            "В разработке.\n"
            "Здесь будет расширенный анализ и альтернативные стратегии."
        )
        label.setAlignment(Qt.AlignCenter)
        label.setStyleSheet("font-size: 14px; color: #6b7280;")
        layout.addStretch()
        layout.addWidget(label)
        layout.addStretch()

        central.setLayout(layout)
        self.setCentralWidget(central)
