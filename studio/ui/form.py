"""Декларативные формы: описание параметра -> виджет, привязанный к хранилищу.

Каждый параметр описывается один раз (F): ключ, тип, подпись и подсказка на
двух языках, диапазон, варианты, условия видимости/доступности. Построитель
создаёт виджеты, связывает их с хранилищем значений и сам пересчитывает
зависимости (видимость, доступность, списки моделей) при любом изменении.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QHBoxLayout, QLabel, QLayout, QLineEdit,
    QSizePolicy, QSpinBox, QVBoxLayout, QWidget,
)
from qfluentwidgets import DoubleSpinBox, LineEdit, SpinBox

from .. import theme
from ..i18n import tr
from .widgets import LABEL_GAP, Combo, FlowBox, PathPicker, Segmented, Toggle, set_tip, tip_html



# ----------------------------------------------------------------- описание
@dataclass
class F:
    """Описание одного параметра формы."""
    key: str
    kind: str                         # bool|int|float|combo|seg|text|file|dir
    label: Any = None
    tip: Any = None
    options: Any = None               # список или функция(values) -> список
    lo: float = 0
    hi: float = 100
    step: float = 1
    decimals: int = 1
    suffix: Any = None
    visible: Optional[Callable[[dict], bool]] = None
    enabled: Optional[Callable[[dict], bool]] = None
    width: Optional[int] = None
    editable: bool = False
    placeholder: Any = None
    filters: str = ""
    inline_label: bool = False        # подпись слева даже внутри Row


@dataclass
class Row:
    """Несколько элементов в одной строке."""
    items: tuple
    label: Any = None
    tip: Any = None
    visible: Optional[Callable[[dict], bool]] = None

    def __init__(self, *items, label=None, tip=None, visible=None):
        self.items = items
        self.label = label
        self.tip = tip
        self.visible = visible


@dataclass
class Note:
    """Поясняющий текст."""
    text: Any
    role: str = "muted"
    visible: Optional[Callable[[dict], bool]] = None
    dynamic: Optional[Callable[[dict], Any]] = None


@dataclass
class Custom:
    """Произвольный виджет: factory(builder) -> QWidget."""
    factory: Callable
    visible: Optional[Callable[[dict], bool]] = None
    key: str = ""


@dataclass
class Group:
    """Подраздел с заголовком и отступом. enabled — когда весь подраздел доступен."""
    title: Any
    items: list
    visible: Optional[Callable[[dict], bool]] = None
    tip: Any = None
    indent: int = 14
    enabled: Optional[Callable[[dict], bool]] = None


@dataclass
class _Bound:
    spec: F
    widget: QWidget
    container: QWidget
    label: Optional[QLabel] = None


# ----------------------------------------------------------------- хранилище
class Store(QObject):
    """Хранилище значений формы с сигналом об изменении ключа."""

    changed = Signal(str, object)

    def __init__(self, values: dict | None = None):
        super().__init__()
        self.values: dict = dict(values or {})

    def get(self, key: str, default=None):
        return self.values.get(key, default)

    def set(self, key: str, value) -> None:
        if key in self.values and self.values[key] == value and type(self.values[key]) is type(value):
            return
        self.values[key] = value
        self.changed.emit(key, value)

    def update(self, values: dict) -> None:
        for key, value in values.items():
            self.set(key, value)

    def snapshot(self) -> dict:
        import copy
        return copy.deepcopy(self.values)


# ----------------------------------------------------------------- построитель
def _norm_options(options) -> list[tuple]:
    out = []
    for item in options or []:
        if isinstance(item, tuple):
            out.append((item[0], item[1]))
        else:
            out.append((item, item))
    return out


class FormBuilder(QObject):
    """Строит виджеты по описаниям и поддерживает их в согласии с хранилищем."""

    def __init__(self, store: Store, parent=None):
        super().__init__(parent)
        self.store = store
        self.bound: dict[str, _Bound] = {}
        # Повторные поля того же ключа (одно значение в нескольких блоках — например,
        # «Формат кадра»): живут в хранилище одним значением, обновляются вместе.
        self.extra: list[_Bound] = []
        self._visible: list[tuple[QWidget, Callable]] = []
        self._enabled: list[tuple[QWidget, Callable]] = []      # доступность подразделов (Group)
        self._notes: list[tuple[QLabel, Callable]] = []
        self._refreshing = False
        self.custom: dict[str, QWidget] = {}
        store.changed.connect(self._on_store_changed)

    # ----- публичное API
    def build(self, items: list, layout: QLayout) -> None:
        for item in items:
            widget = self._make(item)
            if widget is not None:
                layout.addWidget(widget)

    def widget(self, key: str) -> Optional[QWidget]:
        bound = self.bound.get(key)
        return bound.widget if bound else None

    def show_when(self, widget: QWidget, rule: Callable[[dict], bool]) -> None:
        """Показывать внешний виджет (например, карточку раздела) по правилу от значений формы."""
        self._visible.append((widget, rule))
        widget.setVisible(bool(rule(self.store.values)))

    def refresh(self) -> None:
        """Пересчитать видимость, доступность, списки и значения всех полей."""
        if self._refreshing:
            return
        self._refreshing = True
        try:
            values = self.store.values
            # сначала подразделы: от их доступности зависит, как показывать поля внутри
            for widget, rule in self._enabled:
                widget.setEnabled(bool(rule(values)))
            for bound in list(self.bound.values()) + self.extra:
                spec = bound.spec
                if callable(spec.options):
                    self._apply_options(bound, spec.options(values))
                if spec.enabled is not None:
                    bound.widget.setEnabled(bool(spec.enabled(values)))
                    if bound.label is not None:
                        bound.label.setEnabled(bool(spec.enabled(values)))
                self._show_value(bound)
                if spec.visible is not None:
                    bound.container.setVisible(bool(spec.visible(values)))
            for widget, rule in self._visible:
                widget.setVisible(bool(rule(values)))
            for label, fn in self._notes:
                label.setText(tr(fn(values)))
        finally:
            self._refreshing = False

    # ----- внутреннее
    def _on_store_changed(self, _key, _value) -> None:
        self.refresh()

    def _make(self, item) -> Optional[QWidget]:
        if isinstance(item, F):
            return self._make_field_row(item)
        if isinstance(item, Row):
            return self._make_row(item)
        if isinstance(item, Note):
            lab = QLabel(tr(item.text) if item.dynamic is None else "")
            lab.setProperty("role", item.role)
            lab.setWordWrap(True)
            if item.dynamic is not None:
                self._notes.append((lab, item.dynamic))
            if item.visible is not None:
                self._visible.append((lab, item.visible))
            return lab
        if isinstance(item, Custom):
            widget = item.factory(self)
            if item.key:
                self.custom[item.key] = widget
            if item.visible is not None:
                self._visible.append((widget, item.visible))
            return widget
        if isinstance(item, Group):
            box = QWidget()
            lay = QVBoxLayout(box)
            lay.setContentsMargins(item.indent, 6, 0, 4)
            lay.setSpacing(12)
            if item.title:
                head = QLabel(tr(item.title))
                head.setProperty("role", "section")
                head.setIndent(0)   # убрать лишний отступ слева
                if item.tip:
                    set_tip(head, item.tip)
                lay.addWidget(head)
            self.build(item.items, lay)
            if item.visible is not None:
                self._visible.append((box, item.visible))
            if item.enabled is not None:
                self._enabled.append((box, item.enabled))
            return box
        if isinstance(item, QWidget):
            return item
        return None

    @staticmethod
    def _caption(text, tip=None, wrap: bool = False) -> QLabel:
        label = QLabel(tr(text))
        label.setProperty("role", "label")
        label.setWordWrap(wrap)
        if tip:
            set_tip(label, tip)
        return label

    def _control_line(self, spec: F, widget: QWidget) -> QHBoxLayout:
        """Строка «поле + единица измерения»."""
        line = QHBoxLayout()
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(8)
        stretchy = spec.kind in ("text", "file", "dir")
        line.addWidget(widget, 1 if stretchy else 0)
        if spec.suffix:
            suf = QLabel(tr(spec.suffix))
            suf.setProperty("role", "muted")
            line.addWidget(suf)
        if not stretchy:
            line.addStretch(1)
        return line

    def _make_field_row(self, spec: F) -> QWidget:
        """Подпись сверху, поле ввода под ней."""
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(LABEL_GAP)
        label = None
        if spec.kind != "bool" and spec.label is not None:
            label = self._caption(spec.label, spec.tip, wrap=True)
            lay.addWidget(label)
        widget = self._make_widget(spec)
        lay.addLayout(self._control_line(spec, widget))
        self._bind(_Bound(spec, widget, box, label))
        return box

    def _make_row(self, row: Row) -> QWidget:
        """Несколько полей в строке: у каждого своя подпись сверху, поля выровнены по нижнему краю.

        Если в ширину не помещаются, поля переносятся на следующую строку — так форма
        не распирает колонку и не прячет параметры за горизонтальной прокруткой.
        """
        box = QWidget()
        outer = QVBoxLayout(box)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(LABEL_GAP)
        if row.label is not None:
            outer.addWidget(self._caption(row.label, row.tip, wrap=True))
        flow = FlowBox(spacing=20, vspacing=12, align="bottom")
        outer.addWidget(flow)
        for item in row.items:
            if item == "stretch":
                continue
            if isinstance(item, F):
                part = QWidget()
                pl = QVBoxLayout(part)
                pl.setContentsMargins(0, 0, 0, 0)
                pl.setSpacing(LABEL_GAP)
                label = None
                if item.kind != "bool" and item.label is not None:
                    label = self._caption(item.label, item.tip)
                    pl.addWidget(label)
                widget = self._make_widget(item)
                pl.addLayout(self._control_line(item, widget))
                self._bind(_Bound(item, widget, part, label))
                if item.kind in ("text", "file", "dir"):
                    # путь и текст забирают свободное место строки
                    part.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
                    part.setMinimumWidth(220)
                flow.add(part)
            else:
                widget = self._make(item)
                if widget is not None:
                    flow.add(widget)
        if row.visible is not None:
            self._visible.append((box, row.visible))
        return box

    def _bind(self, bound: _Bound) -> None:
        """Первое поле ключа — основное (его отдаёт widget()), повторные — в extra."""
        if bound.spec.key in self.bound:
            self.extra.append(bound)
        else:
            self.bound[bound.spec.key] = bound

    def _make_widget(self, spec: F) -> QWidget:
        value = self.store.get(spec.key)
        kind = spec.kind
        if kind == "bool":
            w = Toggle(tr(spec.label) if spec.label is not None else "")
            w.setFixedHeight(theme.CONTROL_HEIGHT)   # та же высота, что у полей рядом: центр на одной линии
            w.setChecked(bool(value))
            w.checkedChanged.connect(lambda checked, k=spec.key: self._user_set(k, bool(checked)))
        elif kind == "int":
            w = SpinBox()
            w.setRange(int(spec.lo), int(spec.hi))
            w.setSingleStep(int(spec.step or 1))
            w.setValue(int(value if value is not None else spec.lo))
            w.setKeyboardTracking(False)
            w.setFixedWidth(spec.width or 128)
            w.valueChanged.connect(lambda v, k=spec.key: self._user_set(k, int(v)))
        elif kind == "float":
            w = DoubleSpinBox()
            w.setDecimals(spec.decimals)
            w.setRange(float(spec.lo), float(spec.hi))
            w.setSingleStep(float(spec.step or 0.1))
            w.setValue(float(value if value is not None else spec.lo))
            w.setKeyboardTracking(False)
            w.setFixedWidth(spec.width or 136)
            w.valueChanged.connect(lambda v, k=spec.key: self._user_set(k, float(v)))
        elif kind == "combo":
            if spec.editable:
                w = QComboBox()
                w.setEditable(True)
                w.setSizeAdjustPolicy(QComboBox.AdjustToContents)
            else:
                w = Combo()
            w.setMinimumWidth(spec.width or 150)
            opts = spec.options(self.store.values) if callable(spec.options) else spec.options
            self._fill(w, opts)
            self._select(w, value, spec.editable)
            if spec.editable:
                w.activated.connect(lambda _i, cw=w, k=spec.key: self._user_set(k, cw.currentData()))
                w.lineEdit().editingFinished.connect(
                    lambda cw=w, k=spec.key: self._user_set(k, cw.currentText().strip()))
            else:
                w.currentIndexChanged.connect(lambda _i, cw=w, k=spec.key: self._user_set(k, cw.currentData()))
        elif kind == "seg":
            opts = spec.options(self.store.values) if callable(spec.options) else spec.options
            w = Segmented(_norm_options(opts), value)
            w.valueChanged.connect(lambda v, k=spec.key: self._user_set(k, v))
        elif kind == "text":
            w = LineEdit()
            w.setText("" if value is None else str(value))
            if spec.placeholder:
                w.setPlaceholderText(tr(spec.placeholder))
            if spec.width:
                w.setFixedWidth(spec.width)
            w.editingFinished.connect(lambda ww=w, k=spec.key: self._user_set(k, ww.text().strip()))
        elif kind in ("file", "dir"):
            w = PathPicker("dir" if kind == "dir" else "file", spec.filters, spec.placeholder)
            w.set_path(str(value or ""))
            w.pathChanged.connect(lambda p, k=spec.key: self._user_set(k, p))
        else:
            raise ValueError(f"unknown field kind {kind}")
        if spec.tip:
            w.setToolTip(tip_html(spec.tip))
        w.setSizePolicy(QSizePolicy.Preferred if kind not in ("text", "file", "dir") else QSizePolicy.Expanding,
                        QSizePolicy.Fixed)
        return w

    def _user_set(self, key: str, value) -> None:
        if self._refreshing:
            return
        self.store.set(key, value)

    @staticmethod
    def _fill(combo, options) -> None:
        combo.blockSignals(True)
        combo.clear()
        for value, text in _norm_options(options):
            combo.addItem(tr(text), value)
        combo.blockSignals(False)

    @staticmethod
    def _select(combo, value, editable: bool) -> None:
        combo.blockSignals(True)
        idx = combo.findData(value)
        if idx >= 0:
            combo.setCurrentIndex(idx)
        elif editable:
            combo.setEditText("" if value is None else str(value))
        combo.blockSignals(False)

    def _apply_options(self, bound: _Bound, options) -> None:
        """Обновить варианты списка; недопустимое значение сменить на первое."""
        norm = _norm_options(options)
        values = [v for v, _ in norm]
        w = bound.widget
        key = bound.spec.key
        if isinstance(w, Segmented):
            if values != w.values():
                w.set_options(norm)
        elif isinstance(w, (QComboBox, Combo)):
            current = [w.itemData(i) for i in range(w.count())]
            if current != values:
                self._fill(w, norm)
        if values and self.store.get(key) not in values and not bound.spec.editable:
            self.store.values[key] = values[0]
            self.store.changed.emit(key, values[0])
        elif not values and not bound.spec.editable and self.store.get(key):
            self.store.values[key] = ""
            self.store.changed.emit(key, "")

    def _show_value(self, bound: _Bound) -> None:
        """Показать в виджете значение из хранилища (без обратного сигнала)."""
        w = bound.widget
        value = self.store.get(bound.spec.key)
        w.blockSignals(True)
        try:
            if isinstance(w, (QCheckBox, Toggle)):
                # недоступный переключатель показывается выключенным: серый «включён»
                # сбивает с толку. Сохранённое значение при этом не меняется.
                w.setChecked(bool(value) and w.isEnabled())
            elif isinstance(w, QSpinBox):
                try:
                    w.setValue(int(value))
                except (TypeError, ValueError):
                    pass
            elif isinstance(w, QDoubleSpinBox):
                try:
                    w.setValue(float(value))
                except (TypeError, ValueError):
                    pass
            elif isinstance(w, (QComboBox, Combo)):
                self._select(w, value, bound.spec.editable)
            elif isinstance(w, Segmented):
                w.set_value(value)
            elif isinstance(w, QLineEdit):
                if w.text() != str(value or ""):
                    w.setText(str(value or ""))
            elif isinstance(w, PathPicker):
                if w.path() != str(value or ""):
                    w.set_path(str(value or ""))
        finally:
            w.blockSignals(False)
