"""Диалог параметров стадии, построенный из описаний F."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QVBoxLayout, QWidget
from qfluentwidgets import SmoothScrollArea

from ..i18n import T, tr
from .form import FormBuilder, Store


class StageDialog(QDialog):
    """Окно настроек перед запуском: поля с подсказками, «Запустить»/«Отмена»."""

    def __init__(self, parent, title, items: list, values: dict, note=None):
        super().__init__(parent)
        self.setWindowTitle(tr(title))
        self.setMinimumWidth(560)
        self.store = Store(values)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(10)
        head = QLabel(tr(title))
        head.setObjectName("PageTitle")
        head.setWordWrap(True)
        lay.addWidget(head)
        if note:
            hint = QLabel(tr(note))
            hint.setProperty("role", "muted")
            hint.setWordWrap(True)
            lay.addWidget(hint)
        area = SmoothScrollArea()
        area.setWidgetResizable(True)
        area.setObjectName("Transparent")
        body = QWidget()
        body.setObjectName("Transparent")
        bl = QVBoxLayout(body)
        bl.setContentsMargins(0, 0, 16, 0)
        bl.setSpacing(8)
        self.builder = FormBuilder(self.store, self)
        self.builder.build(items, bl)
        bl.addStretch(1)
        area.setWidget(body)
        lay.addWidget(area, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText(tr(T("Запустить", "Run")))
        buttons.button(QDialogButtonBox.Ok).setProperty("kind", "primary")
        buttons.button(QDialogButtonBox.Cancel).setText(tr(T("Отмена", "Cancel")))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.builder.refresh()
        self.resize(620, min(760, 180 + 44 * len(items)))

    def values(self) -> dict:
        return dict(self.store.values)


def ask(parent, title, items: list, values: dict, note=None) -> dict | None:
    dlg = StageDialog(parent, title, items, values, note)
    return dlg.values() if dlg.exec() == QDialog.Accepted else None
