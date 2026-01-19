from __future__ import annotations

from PySide6.QtWidgets import QPlainTextEdit, QVBoxLayout, QWidget


class PairLogsPanel(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._text = QPlainTextEdit()
        self._text.setReadOnly(True)

        layout = QVBoxLayout()
        layout.addWidget(self._text)
        self.setLayout(layout)

    def append(self, message: str) -> None:
        self._text.appendPlainText(message)

    def clear(self) -> None:
        self._text.clear()
