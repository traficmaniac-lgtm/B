from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QHBoxLayout,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


class LogEmitter(QObject):
    message = Signal(str)


class QtLogHandler(logging.Handler):
    def __init__(self, emitter: LogEmitter) -> None:
        super().__init__()
        self._emitter = emitter

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            message = record.getMessage()
        self._emitter.message.emit(message)


class LogDock(QDockWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Logs", parent)
        self.setObjectName("LogsDock")

        self._text = QTextEdit()
        self._text.setReadOnly(True)

        self._clear_button = QPushButton("Clear")
        self._copy_button = QPushButton("Copy all")

        button_row = QHBoxLayout()
        button_row.addWidget(self._clear_button)
        button_row.addWidget(self._copy_button)
        button_row.addStretch()

        layout = QVBoxLayout()
        layout.addLayout(button_row)
        layout.addWidget(self._text)

        container = QWidget()
        container.setLayout(layout)
        self.setWidget(container)

        self._emitter = LogEmitter()
        self._handler = QtLogHandler(self._emitter)

        self._emitter.message.connect(self._append_message)
        self._clear_button.clicked.connect(self._text.clear)
        self._copy_button.clicked.connect(self._copy_all)

    @property
    def handler(self) -> QtLogHandler:
        return self._handler

    def _append_message(self, message: str) -> None:
        self._text.append(message)

    def _copy_all(self) -> None:
        clipboard = QApplication.clipboard()
        clipboard.setText(self._text.toPlainText())
