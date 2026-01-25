from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QDialog, QHBoxLayout, QPlainTextEdit, QPushButton, QVBoxLayout


class PilotLogWindow(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Pilot Log")
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self.setMinimumSize(520, 320)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(800)
        fixed_font = QFont()
        fixed_font.setStyleHint(QFont.Monospace)
        fixed_font.setFixedPitch(True)
        self._log_view.setFont(fixed_font)
        layout.addWidget(self._log_view)

        buttons = QHBoxLayout()
        buttons.addStretch()
        clear_button = QPushButton("Clear")
        copy_button = QPushButton("Copy all")
        clear_button.clicked.connect(self._log_view.clear)
        copy_button.clicked.connect(self._copy_all)
        buttons.addWidget(clear_button)
        buttons.addWidget(copy_button)
        layout.addLayout(buttons)

    def append_line(self, message: str) -> None:
        self._log_view.appendPlainText(message)

    def _copy_all(self) -> None:
        self._log_view.selectAll()
        self._log_view.copy()
        self._log_view.moveCursor(self._log_view.textCursor().End)
