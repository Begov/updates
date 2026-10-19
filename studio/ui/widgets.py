"""Общие виджеты интерфейса."""
from __future__ import annotations

import html
import re
import time
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLayout,
    QLineEdit, QPlainTextEdit, QPushButton, QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

from qfluentwidgets import (
    ComboBox, FluentIcon, IconWidget, LineEdit, PrimaryPushButton, PushButton, SearchLineEdit,
    SmoothScrollDelegate, SwitchButton, TableWidget, ToolButton, TransparentPushButton, TransparentToolButton,
    setFont,
)

from .. import state, theme
from ..i18n import T, tr


LABEL_GAP = 8   # px между подписью и полем под ней


# ------------------------------------------------------------------ подсказки
def tip_html(text) -> str:
    """Текст подсказки -> rich text: переносы строк сохраняются, длинные строки
    переносятся автоматически (Qt переносит только rich text)."""
    raw = tr(text)
    if not raw:
        return ""
    body = html.escape(raw).replace("\n", "<br>")
    body = re.sub(r"(^|<br>)• ", r"\1&nbsp;• ", body)
    return f"<div style='max-width:460px'>{body}</div>"


def set_tip(widget: QWidget, text) -> None:
    widget.setToolTip(tip_html(text))


def repolish(widget: QWidget) -> None:
    """Применить изменившееся динамическое свойство к стилю."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def label(text, role: str = "label", tip=None, wrap: bool = False) -> QLabel:
    lab = QLabel(tr(text))
    lab.setProperty("role", role)
    if role == "section":
        lab.setIndent(0)    # со стилем (padding) Qt добавляет отступ слева ≈ полсимвола
    if wrap:
        lab.setWordWrap(True)
    if tip:
        set_tip(lab, tip)
    return lab


def button(text, kind: str = "", tip=None, slot: Callable | None = None, icon: str | None = None) -> QPushButton:
    """Fluent-кнопка: primary — акцентная, danger — красная, ghost — прозрачная, small — компактная."""
    cls = {"primary": PrimaryPushButton, "ghost": TransparentPushButton}.get(kind, PushButton)
    fluent_icon = getattr(FluentIcon, icon, None) if icon else None
    btn = cls(fluent_icon, tr(text)) if fluent_icon is not None else cls(tr(text))
    btn.setProperty("kind", kind)
    if kind == "danger":
        btn.setStyleSheet(f"PushButton {{ color: {theme.c('red')}; border: 1px solid {theme.c('red')};"
                          f" border-radius: 5px; background: transparent; padding: 5px 14px 5px {38 if fluent_icon is not None else 14}px; }}"
                          f"PushButton:hover {{ background: {theme.c('red')}; color: white; }}")
    if tip:
        set_tip(btn, tip)
    if slot:
        btn.clicked.connect(slot)
    btn.setCursor(Qt.PointingHandCursor)
    return btn


class _HintFilter(QObject):
    """Держит подсказку по центру области просмотра."""

    def __init__(self, viewport, label, refresh):
        super().__init__(viewport)
        self._label, self._refresh = label, refresh
        viewport.installEventFilter(self)

    def eventFilter(self, obj, event):  # noqa: N802 (Qt API)
        if event.type() in (QEvent.Resize, QEvent.Show):
            self._refresh()
        return False


def attach_hint(view, text) -> QLabel:
    """Показывает в пустой таблице или журнале понятную подсказку, что делать дальше."""
    viewport = view.viewport()
    lab = QLabel(tr(text), viewport)
    lab.setAlignment(Qt.AlignCenter)
    lab.setWordWrap(True)
    lab.setProperty("role", "muted")
    lab.setAttribute(Qt.WA_TransparentForMouseEvents)

    def is_empty() -> bool:
        if hasattr(view, "rowCount"):
            return view.rowCount() == 0
        return view.document().isEmpty()

    def refresh(*_):
        try:
            _place()
        except RuntimeError:        # виджеты уже удалены (окно пересоздаётся)
            pass

    def _place():
        lab.setGeometry(viewport.rect().adjusted(32, 40, -32, 0) if hasattr(view, "rowCount") else viewport.rect().adjusted(32, 0, -32, 0))
        lab.setVisible(is_empty())
        lab.raise_()

    if hasattr(view, "rowCount"):
        model = view.model()
        for sig in (model.rowsInserted, model.rowsRemoved, model.modelReset, model.layoutChanged):
            sig.connect(refresh)
    else:
        view.document().contentsChanged.connect(refresh)
    view._hint_filter = _HintFilter(viewport, lab, refresh)
    refresh()
    return lab


def smooth_scroll(view) -> None:
    """Тонкая накладная полоса прокрутки Fluent вместо системной."""
    view._smooth_delegate = SmoothScrollDelegate(view, useAni=False)


def hline() -> QFrame:
    line = QFrame()
    line.setObjectName("Divider")
    line.setFrameShape(QFrame.NoFrame)
    return line


class Chip(QLabel):
    """Маленький статус-индикатор с цветом состояния."""

    def __init__(self, text: str = "", tone: str = ""):
        super().__init__(text)
        self.setProperty("chip", True)
        self.set_tone(tone)

    def set_tone(self, tone: str) -> None:
        self.setProperty("tone", tone)
        repolish(self)

    def show_state(self, text: str, tone: str = "", tip: str = "") -> None:
        self.setText(text)
        self.set_tone(tone)
        if tip:
            self.setToolTip(tip_html(tip))


# ------------------------------------------------------------ сегменты
SEGMENT_GAP = 6   # px между кнопками переключателя
# элементы с такой политикой в строке с переносом забирают свободное место
_GROW_POLICIES = (QSizePolicy.Expanding, QSizePolicy.MinimumExpanding)


class _FlowRow(QLayout):
    """Раскладка с переносом на следующую строку, когда не хватает ширины.

    Своя, а не FlowLayout библиотеки: та объявляет желаемой шириной ширину ОДНОЙ
    кнопки (в ряду с другими полями переключатель сжимался в столбик), а скрытые
    кнопки отсеивает по isVisible() — до показа окна это все кнопки, и высота
    получалась нулевой. Здесь желаемая ширина — все элементы в одну строку, а
    пропускаются только явно скрытые (isHidden).

    align — как элементы разной высоты стоят в строке: "center" (по середине), "top" или
    "bottom" (поля с подписью сверху и переключатели без подписи — на одной линии).
    Растягиваемые по ширине элементы (Expanding) делят между собой свободное место строки.
    """

    def __init__(self, parent=None, spacing: int = 6, vspacing: int | None = None, align: str = "top"):
        super().__init__(parent)
        self._items: list = []
        self._gap = spacing
        self._vgap = spacing if vspacing is None else vspacing
        self._align = align
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):  # noqa: N802 (Qt API)
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):  # noqa: N802 (Qt API)
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):  # noqa: N802 (Qt API)
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def _shown(self):
        return [i for i in self._items if not (i.widget() and i.widget().isHidden())]

    @staticmethod
    def _hint(item) -> QSize:
        return item.sizeHint().expandedTo(item.minimumSize())

    def expandingDirections(self):  # noqa: N802 (Qt API)
        return Qt.Orientation(0)

    def hasHeightForWidth(self):  # noqa: N802 (Qt API)
        # Нет: высоту под число строк виджет-владелец выставляет себе сам минимальной
        # (_fit_height). Иначе раскладки-родители считают её по-своему и при переносе
        # строк дают блоку меньше, чем нужно, — соседние элементы налезают.
        return False

    def heightForWidth(self, width):  # noqa: N802 (Qt API)
        return self._place(QRect(0, 0, width, 0), move=False)

    def setGeometry(self, rect):  # noqa: N802 (Qt API)
        super().setGeometry(rect)
        self._place(rect, move=True)

    def sizeHint(self):  # noqa: N802 (Qt API)
        items = self._shown()
        width = sum(self._hint(i).width() for i in items) + self._gap * max(0, len(items) - 1)
        height = max((self._hint(i).height() for i in items), default=0)
        return QSize(width, height)

    def minimumSize(self):  # noqa: N802 (Qt API)
        # Высота — одна строка: реальную высоту при данной ширине раскладка родителя берёт
        # из heightForWidth. Если считать минимум «все кнопки столбиком», карточки
        # резервируют эту высоту и в них появляются пустые полосы.
        items = self._shown()
        width = max((i.minimumSize().width() for i in items), default=0)
        height = max((self._hint(i).height() for i in items), default=0)
        return QSize(width, height)

    def _lines(self, width: int) -> list[list[tuple]]:
        """Разбить элементы на строки: [(элемент, ширина)] для каждой строки."""
        lines, line, used = [], [], 0
        for item in self._shown():
            w = min(self._hint(item).width(), max(1, width))
            if line and used + self._gap + w > width:
                lines.append(line)
                line, used = [], 0
            used += (self._gap if line else 0) + w
            line.append((item, w))
        if line:
            lines.append(line)
        return lines

    def _place(self, rect, move: bool) -> int:
        y = rect.y()
        for n, line in enumerate(self._lines(rect.width())):
            free = rect.width() - sum(w for _, w in line) - self._gap * (len(line) - 1)
            grow = [item for item, _ in line
                    if item.widget() and item.widget().sizePolicy().horizontalPolicy() in _GROW_POLICIES]
            sized = []
            for item, w in line:
                if free > 0 and item in grow:
                    w += free // len(grow)
                h = item.heightForWidth(w) if item.hasHeightForWidth() else self._hint(item).height()
                sized.append((item, w, max(h, 0)))
            line_h = max(h for *_, h in sized)
            if n:
                y += self._vgap
            if move:
                x = rect.x()
                for item, w, h in sized:
                    top = y + {"bottom": line_h - h, "center": (line_h - h) // 2}.get(self._align, 0)
                    item.setGeometry(QRect(QPoint(x, top), QSize(w, h)))
                    x += w + self._gap
            y += line_h
        return y - rect.y()


class Column(QWidget):
    """Колонка блоков, чей минимум по высоте учитывает перенос текста при текущей ширине.

    Разделитель (QSplitter) и область прокрутки берут минимум без учёта переноса строк:
    блок с переносимым текстом оказывается выше заявленного, и соседям достаётся меньше
    их минимума — они налезают друг на друга. Здесь минимум считается при текущей ширине.
    """

    def minimumSizeHint(self):  # noqa: N802 (Qt API)
        base = super().minimumSizeHint()
        lay = self.layout()
        if lay is not None and lay.hasHeightForWidth() and self.width() > 0:
            return QSize(base.width(), max(base.height(), lay.totalMinimumHeightForWidth(self.width())))
        return base

    def resizeEvent(self, event):  # noqa: N802 (Qt API)
        super().resizeEvent(event)
        if event.oldSize().width() != event.size().width():
            self.updateGeometry()                  # с новой шириной меняется и нужная высота


class FlowBox(QWidget):
    """Ряд виджетов, который переносится на новые строки, когда не хватает ширины.

    Высоту под нужное число строк выставляет сам: вложенные раскладки и области
    прокрутки высоту-по-ширине (heightForWidth) не учитывают.
    """

    def __init__(self, spacing: int = 6, vspacing: int | None = None, align: str = "center", parent=None):
        super().__init__(parent)
        self._flow = _FlowRow(self, spacing, vspacing, align)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)

    def add(self, widget: QWidget) -> QWidget:
        self._flow.addWidget(widget)
        return widget

    def _fit_height(self) -> None:
        need = self._flow.heightForWidth(max(1, self.width()))
        if need and need != self.minimumHeight():
            self.setMinimumHeight(need)

    def resizeEvent(self, event):  # noqa: N802 (Qt API)
        super().resizeEvent(event)
        self._fit_height()

    def event(self, event):  # noqa: D401 (Qt API)
        # элемент скрыли/показали или он сменил размер — пересчитать число строк
        if event.type() == QEvent.LayoutRequest:
            self._fit_height()
        return super().event(event)


class _SegmentButton(PushButton):
    """РљРЅРѕРїРєР° РїРµСЂРµРєР»СЋС‡Р°С‚РµР»СЏ: РѕС‚РґРµР»СЊРЅР°СЏ, СЃРѕ СЃРІРѕРµР№ СЂР°РјРєРѕР№ вЂ” РєР°Рє РѕР±С‹С‡РЅС‹Рµ РєРЅРѕРїРєРё РїСЂРѕРіСЂР°РјРјС‹."""

    def __init__(self, text: str = "", parent=None):
        # РєРѕРЅСЃС‚СЂСѓРєС‚РѕСЂ PushButton Р±РёР±Р»РёРѕС‚РµРєРё РїРµСЂРµРіСЂСѓР¶РµРЅ Рё СЃР°Рј РІС‹Р·С‹РІР°РµС‚ __init__(parent=вЂ¦),
        # РїРѕСЌС‚РѕРјСѓ С‚РµРєСЃС‚ вЂ” РѕС‚РґРµР»СЊРЅРѕ, РЅРµ С‡РµСЂРµР· super().__init__(text, parent)
        super().__init__(parent)
        self.setText(text)
        self.isSelected = False
        setFont(self, 14)
        self.setFixedSize(self.fontMetrics().horizontalAdvance(text) + 32, theme.CONTROL_HEIGHT)

    def setSelected(self, selected: bool) -> None:  # noqa: N802 (РєР°Рє Сѓ РєРЅРѕРїРѕРє Р±РёР±Р»РёРѕС‚РµРєРё)
        if self.isSelected != selected:
            self.isSelected = selected
            self.update()

    def paintEvent(self, _event):  # noqa: N802 (Qt API)
        p = theme.colors()
        if not self.isEnabled() and self.isSelected:
            # недоступный, но выбранный: видно, какой вариант вернётся, когда поле снова доступно
            bg = QColor(p["accent_soft"])
            bg.setAlpha(110)
            bg, border, fg = bg, QColor(p["border_strong"]), QColor(p["muted"])
        elif not self.isEnabled():
            bg, border, fg = QColor(0, 0, 0, 0), QColor(p["border"]), QColor(p["muted"])
        elif self.isSelected:
            bg, border, fg = QColor(p["accent_soft"]), QColor(p["accent"]), QColor(p["text"])
        elif self.isDown():
            bg, border, fg = QColor(p["card_head"]), QColor(p["border_strong"]), QColor(p["text"])
        elif self.underMouse():
            bg, border, fg = QColor(p["input_hover"]), QColor(p["border_strong"]), QColor(p["text"])
        else:
            bg, border, fg = QColor(p["input"]), QColor(p["border"]), QColor(p["dim"])
        painter = QPainter(self)
        painter.setRenderHints(QPainter.Antialiasing)
        painter.setPen(QPen(border, 1))
        painter.setBrush(bg)
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)
        painter.setPen(fg)
        painter.setFont(self.font())
        painter.drawText(self.rect(), Qt.AlignCenter, self.text())


class Segmented(QWidget):
    """Переключатель из отдельных кнопок (выбрана одна). Значения могут быть любого типа.

    Кнопки переносятся на следующую строку, если в ширину не помещаются, — так
    кнопками можно выбирать и из длинных списков (модели Codex, переписывание).
    """

    valueChanged = Signal(object)

    def __init__(self, options: list, value=None, parent=None):
        super().__init__(parent)
        self._map: dict = {}
        self._value = None
        self.items: dict = {}
        self._flow = _FlowRow(self, SEGMENT_GAP)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self.set_options(options)
        if value is not None:
            self.set_value(value)

    def _key_of(self, value):
        for key, val in self._map.items():
            if val == value and type(val) is type(value):
                return key
        return None

    def _fit_height(self) -> None:
        """Высота — ровно под столько строк кнопок, сколько нужно при текущей ширине.

        Вложенные раскладки формы высоту-по-ширине (heightForWidth) не учитывают, поэтому
        переключатель сам выставляет себе минимальную высоту, когда меняется ширина."""
        need = self._flow.heightForWidth(max(1, self.width()))
        if need and need != self.minimumHeight():
            self.setMinimumHeight(need)

    def resizeEvent(self, event):  # noqa: N802 (Qt API)
        super().resizeEvent(event)
        self._fit_height()

    def set_options(self, options: list) -> None:
        for button in self.items.values():
            self._flow.removeWidget(button)
            button.deleteLater()
        self.items, self._map = {}, {}
        for i, opt in enumerate(options):
            value, text = opt if isinstance(opt, tuple) else (opt, opt)
            key = f"s{i}"
            self._map[key] = value
            button = _SegmentButton(tr(text), self)
            button.clicked.connect(lambda _=False, k=key: self._on_click(k))
            self.items[key] = button
            self._flow.addWidget(button)
        self._mark()
        self._fit_height()
        self.updateGeometry()

    def _mark(self) -> None:
        current = self._key_of(self._value)
        for key, button in self.items.items():
            button.setSelected(key == current)

    def set_button_enabled(self, value, enabled: bool) -> None:
        key = self._key_of(value)
        if key:
            self.items[key].setEnabled(enabled)

    def values(self) -> list:
        return list(self._map.values())

    def value(self):
        return self._value

    def set_value(self, value) -> None:
        self._value = value
        self._mark()

    def _on_click(self, key: str) -> None:
        value = self._map.get(key)
        if value != self._value or type(value) is not type(self._value):
            self._value = value
            self._mark()
            self.valueChanged.emit(value)


# ------------------------------------------------------------ доли
class MixBar(QWidget):
    """Полоса долей с ручками на границах: 100 % делятся между частями, остаток — последней.

    parts — [(key, короткая подпись, цвет)], последняя часть — остаток (её значение не
    хранится, оно = 100 − сумма остальных). Ручка двигает границу только между двумя
    соседями, остальные доли не меняются. Во время перетаскивания шлёт moved(dict),
    после отпускания — changed(dict).
    """

    moved = Signal(dict)
    changed = Signal(dict)

    BAR_H = 34
    HANDLE_W = 10
    GRAB = 9          # px вокруг ручки, в которых её можно схватить

    def __init__(self, parent=None):
        super().__init__(parent)
        self._parts: list[tuple] = []
        self._values: dict = {}
        self._drag: int | None = None
        self._grab: list[int] = []       # ручки в одной точке: какую тянуть, решит направление
        self._press_x = 0.0
        self.setMouseTracking(True)
        self.setFixedHeight(self.BAR_H + 4)
        self.setMinimumWidth(260)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    # ----- данные
    def set_parts(self, parts: list, values: dict) -> None:
        self._parts = list(parts)
        self._values = {key: max(0, int(values.get(key, 0))) for key, *_ in self._parts[:-1]}
        self._drag = None
        self.update()

    def values(self) -> dict:
        out = dict(self._values)
        if self._parts:
            out[self._parts[-1][0]] = 100 - sum(self._values.values())
        return out

    def _bounds(self) -> list[int]:
        """Позиции ручек в процентах (накопленные суммы долей)."""
        acc, out = 0, []
        for key, *_ in self._parts[:-1]:
            acc += self._values[key]
            out.append(acc)
        return out

    # ----- геометрия
    def _track(self) -> QRectF:
        return QRectF(1, 2, self.width() - 2, self.BAR_H)

    def _x_of(self, pct: float) -> float:
        t = self._track()
        return t.left() + t.width() * pct / 100

    def _pct_at(self, x: float) -> int:
        t = self._track()
        return max(0, min(100, round((x - t.left()) / max(1.0, t.width()) * 100)))

    def _handles_at(self, x: float) -> list[int]:
        return [i for i, b in enumerate(self._bounds()) if abs(self._x_of(b) - x) <= self.GRAB]

    def _move(self, index: int, pct: int) -> None:
        bounds = self._bounds()
        lo = bounds[index - 1] if index > 0 else 0
        hi = bounds[index + 1] if index + 1 < len(bounds) else 100
        pct = max(lo, min(hi, pct))
        keys = [key for key, *_ in self._parts[:-1]]
        self._values[keys[index]] = pct - lo
        if index + 1 < len(keys):
            self._values[keys[index + 1]] = hi - pct
        self.update()
        self.moved.emit(self.values())

    # ----- мышь
    def mousePressEvent(self, e):  # noqa: N802 (Qt API)
        if e.button() != Qt.LeftButton:
            return
        x = e.position().x()
        self._grab = self._handles_at(x)
        self._press_x = x
        self._drag = self._grab[0] if len(self._grab) == 1 else None

    def mouseMoveEvent(self, e):  # noqa: N802 (Qt API)
        x = e.position().x()
        if self._grab and self._drag is None and abs(x - self._press_x) >= 2:
            # ручки слиплись (доля 0): вправо тянем последнюю из них, влево — первую
            self._drag = self._grab[-1] if x > self._press_x else self._grab[0]
        if self._drag is not None:
            self._move(self._drag, self._pct_at(x))
        else:
            self.setCursor(Qt.SizeHorCursor if self._handles_at(x) else Qt.ArrowCursor)

    def mouseReleaseEvent(self, _e):  # noqa: N802 (Qt API)
        if self._drag is not None:
            self.changed.emit(self.values())
        self._drag = None
        self._grab = []

    def changeEvent(self, e):  # noqa: N802 (Qt API)
        if e.type() == QEvent.EnabledChange:
            self._drag = None
            self.unsetCursor()
            self.update()
        super().changeEvent(e)

    # ----- отрисовка
    @staticmethod
    def _ink(color: QColor) -> QColor:
        """Тёмный текст на светлой заливке, белый — на тёмной."""
        lum = (0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue()) / 255
        return QColor("#14161b") if lum > 0.6 else QColor("#ffffff")

    def paintEvent(self, _event):  # noqa: N802 (Qt API)
        if not self._parts:
            return
        p = theme.colors()
        painter = QPainter(self)
        painter.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        if not self.isEnabled():
            painter.setOpacity(0.45)         # недоступна: видно состав, но двигать нечего
        track = self._track()
        clip = QPainterPath()
        clip.addRoundedRect(track, 8, 8)
        painter.setClipPath(clip)
        painter.fillRect(track, QColor(p["input"]))
        font = QFont(self.font())
        font.setPointSizeF(8.5)
        font.setWeight(QFont.DemiBold)
        painter.setFont(font)
        fm = painter.fontMetrics()
        values, left = self.values(), 0
        for key, text, color in self._parts:
            share = values.get(key, 0)
            if share <= 0:
                continue
            rect = QRectF(self._x_of(left), track.top(), self._x_of(left + share) - self._x_of(left), track.height())
            fill = QColor(color)
            painter.fillRect(rect, fill)
            painter.setPen(self._ink(fill))
            for caption in (f"{tr(text)}  {share}%", f"{share}%"):
                if fm.horizontalAdvance(caption) + 2 * self.HANDLE_W + 6 <= rect.width():
                    painter.drawText(rect, Qt.AlignCenter, caption)
                    break
            left += share
        painter.setClipping(False)
        # ручки на границах
        painter.setPen(QPen(QColor(p["border_strong"]), 1))
        painter.setBrush(QColor(p["text"]))
        for b in self._bounds():
            x = min(max(self._x_of(b), track.left() + self.HANDLE_W / 2), track.right() - self.HANDLE_W / 2)
            painter.drawRoundedRect(QRectF(x - self.HANDLE_W / 2, track.top() + 5,
                                           self.HANDLE_W, track.height() - 10), 4, 4)


class Table(TableWidget):
    """Fluent-таблица с конструктором как у QTableWidget(строки, столбцы)."""

    def __init__(self, rows: int = 0, cols: int = 0, parent=None):
        super().__init__(parent)
        self.setRowCount(rows)
        self.setColumnCount(cols)
        self.setBorderVisible(True)
        self.setBorderRadius(10)


class Toggle(SwitchButton):
    """Переключатель с постоянной подписью (вместо галочки)."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self.setOnText(text)
        self.setOffText(text)
        self.setCursor(Qt.PointingHandCursor)

    @property
    def toggled(self):
        """Совместимость с QCheckBox."""
        return self.checkedChanged


class Combo(ComboBox):
    """Fluent-список с интерфейсом, близким к QComboBox (данные, поиск по данным)."""

    def addItem(self, text, userData=None, *_):  # noqa: N802 (Qt API)
        super().addItem(text, None, userData)

    def itemData(self, index: int):  # noqa: N802
        return self.items[index].userData if 0 <= index < len(self.items) else None

    def findData(self, data) -> int:  # noqa: N802
        for i, item in enumerate(self.items):
            if item.userData == data:
                return i
        return -1

    def setSizeAdjustPolicy(self, *_):  # noqa: N802
        pass


# ------------------------------------------------------------- карточка
CARD_ICONS = {
    "preset": ("TAG",), "start": ("PLAY",), "direction": ("EDIT",), "intro": ("MOVIE", "VIDEO"),
    "stock": ("LIBRARY",), "infographics": ("ALBUM",), "motion_graphics": ("MARKET",), "imagegen": ("BRUSH",),
    "remixer": ("SYNC",), "videogen": ("VIDEO",), "source_mix": ("PIE_SINGLE",),
    "render_concat": ("CAMERA",), "cp_projects": ("FOLDER",), "cp_stages": ("DEVELOPER_TOOLS", "SETTING"),
    "cp_queue": ("HISTORY",), "cp_finalize": ("SEND",), "set_look": ("PALETTE",), "set_updates": ("UPDATE", "SYNC"), "set_codex": ("ROBOT",),
    "set_cinai": ("CLOUD",), "set_ai": ("CHAT",), "set_veo": ("VIDEO",), "set_vertex": ("CLOUD",),
    "set_cheapvibe": ("PHOTO",), "set_varag": ("SEND",),
    "set_stock": ("LIBRARY",),
}


CARD_ICONS.update({"queue": ("DOCUMENT",), "run": ("PLAY",)})


def card_icon(key: str):
    key = key[5:] if key.startswith("pipe_") else key
    for name in CARD_ICONS.get(key, ("TAG",)):
        icon = getattr(FluentIcon, name, None)
        if icon is not None:
            return icon
    return FluentIcon.TAG


class Card(QFrame):
    """Карточка-раздел с иконкой и заголовком; может сворачиваться (состояние помнится)."""

    toggled = Signal(bool)

    def __init__(self, title, icon: str = "", key: str = "", collapsible: bool = True,
                 tip=None, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self._key = key
        self._collapsible = collapsible
        outer = QVBoxLayout(self)
        outer.setContentsMargins(22, 18, 22, 20)
        outer.setSpacing(14)

        head = QFrame()
        head.setObjectName("CardHead")
        hl = QHBoxLayout(head)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(12)
        self.icon = IconWidget(card_icon(key))
        self.icon.setFixedSize(20, 20)
        self.title = QLabel(tr(title))
        self.title.setObjectName("CardTitle")
        if tip:
            set_tip(self.title, tip)
        self.summary = QLabel("")
        self.summary.setObjectName("CardSummary")
        self.summary.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.head_extra = QHBoxLayout()
        self.head_extra.setSpacing(8)
        self._arrow = IconWidget(FluentIcon.DOWN)
        self._arrow.setFixedSize(12, 12)
        hl.addWidget(self.icon)
        hl.addWidget(self.title)
        hl.addWidget(self.summary, 1)
        hl.addLayout(self.head_extra)
        if collapsible:
            hl.addWidget(self._arrow)
        else:
            self._arrow.hide()
        outer.addWidget(head)
        self._head = head

        self.body = QFrame()
        self.body.setObjectName("CardBody")
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(12)
        self.body_layout.addWidget(hline())   # тонкая линия между заголовком и содержимым
        outer.addWidget(self.body)

        if collapsible:
            head.setCursor(Qt.PointingHandCursor)
            head.installEventFilter(self)
            collapsed = bool((state.get("collapsed", {}) or {}).get(key, False)) if key else False
            self.set_collapsed(collapsed, save=False)

    def eventFilter(self, obj, event):
        if obj is self._head and event.type() == QEvent.MouseButtonRelease:
            self.set_collapsed(self.body.isVisible())
            return True
        return super().eventFilter(obj, event)

    def set_collapsed(self, collapsed: bool, save: bool = True) -> None:
        self.body.setVisible(not collapsed)
        self._arrow.setIcon(FluentIcon.RIGHT_ARROW if collapsed else FluentIcon.DOWN)
        if save and self._key:
            data = dict(state.get("collapsed", {}) or {})
            data[self._key] = collapsed
            state.put(collapsed=data)
        self.toggled.emit(collapsed)

    def add(self, item) -> None:
        if isinstance(item, QWidget):
            self.body_layout.addWidget(item)
        else:
            self.body_layout.addLayout(item)


# ------------------------------------------------------------ путь к файлу
class PathPicker(QWidget):
    """Поле пути + кнопка «Обзор» (+ «Очистить»)."""

    pathChanged = Signal(str)

    def __init__(self, mode: str = "file", filters: str = "", placeholder=None,
                 title=None, parent=None):
        super().__init__(parent)
        self._mode = mode
        self._filters = filters
        self._title = title
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.edit = LineEdit()
        if placeholder:
            self.edit.setPlaceholderText(tr(placeholder))
        self.edit.editingFinished.connect(lambda: self.pathChanged.emit(self.path()))
        browse = ToolButton(FluentIcon.FOLDER)
        browse.setToolTip(tr(T("Выбрать", "Browse")))
        browse.clicked.connect(self._browse)
        clear = TransparentToolButton(FluentIcon.CLOSE)
        clear.setToolTip(tr(T("Очистить", "Clear")))
        clear.clicked.connect(lambda: self.set_path("", emit=True))
        lay.addWidget(self.edit, 1)
        lay.addWidget(browse)
        lay.addWidget(clear)

    def path(self) -> str:
        return self.edit.text().strip()

    def set_path(self, value: str, emit: bool = False) -> None:
        self.edit.setText(value or "")
        if emit:
            self.pathChanged.emit(self.path())

    def _browse(self) -> None:
        start = self.path() or ""
        if start and Path(start).is_file():
            start = str(Path(start).parent)
        title = tr(self._title or T("Выбор", "Choose"))
        if self._mode == "dir":
            chosen = QFileDialog.getExistingDirectory(self, title, start)
        else:
            chosen, _ = QFileDialog.getOpenFileName(self, title, start, self._filters)
        if chosen:
            self.set_path(str(Path(chosen)), emit=True)


# --------------------------------------------------------------- журнал
LEVEL_COLORS = {
    "ERROR": "red", "WARNING": "yellow", "SUCCESS": "green", "INFO": "text", "DIM": "muted",
}


class LogView(QWidget):
    """Журнал с цветом по уровням, фильтром, поиском и сохранением в файл."""

    MAX_LINES = 20000

    def __init__(self, parent=None, title=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        # Панель журнала: в широкой колонке — одна строка, в узкой фильтр и поиск
        # уходят на вторую строку (см. resizeEvent), чтобы ничего не сминалось.
        self._bar = QHBoxLayout()
        self._bar.setSpacing(6)
        self._bar2 = QHBoxLayout()
        self._bar2.setSpacing(6)
        self._head = QLabel(tr(title)) if title else None
        if self._head is not None:
            self._head.setObjectName("CardTitle")
            self._bar.addWidget(self._head)
        self.level = Combo()
        for code, text in (("", T("Все", "All")), ("WARNING", T("Предупр. и ошибки", "Warnings+errors")),
                           ("ERROR", T("Только ошибки", "Errors only"))):
            self.level.addItem(tr(text), code)
        set_tip(self.level, T("Фильтр строк журнала по важности.", "Filter log lines by severity."))
        self.search = SearchLineEdit()
        self.search.setPlaceholderText(tr(T("Поиск в журнале…", "Search log…")))
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(140)
        self._tools = QWidget()
        tools = QHBoxLayout(self._tools)
        tools.setContentsMargins(0, 0, 0, 0)
        tools.setSpacing(6)
        tools.addWidget(self.level)
        tools.addWidget(self.search, 1)
        self._follow_text = tr(T("Автопрокрутка", "Auto-scroll"))
        self.follow = Toggle(self._follow_text)
        self.follow.setChecked(True)
        set_tip(self.follow, T("Держать журнал прокрученным к последней строке.",
                               "Keep the log scrolled to the newest line."))
        save = TransparentToolButton(FluentIcon.SAVE)
        set_tip(save, T("Сохранить журнал в файл", "Save log to file"))
        save.clicked.connect(self._save)
        clear = TransparentToolButton(FluentIcon.DELETE)
        set_tip(clear, T("Очистить журнал", "Clear log"))
        clear.clicked.connect(self.clear)
        # сначала — узкий вид (фильтр и поиск второй строкой): у широкого большая минимальная
        # ширина, и журнал не смог бы сузиться до размера, при котором переходит в узкий
        self._bar.addStretch(1)
        self._bar.addWidget(self.follow)
        self._bar.addWidget(save)
        self._bar.addWidget(clear)
        self._bar2.addWidget(self._tools, 1)
        self._wide = False
        self._follow_full = self.follow.sizeHint().width()
        lay.addLayout(self._bar)
        lay.addLayout(self._bar2)
        self.setMinimumWidth(300)

        self.view = QPlainTextEdit()
        self.view.setObjectName("LogView")
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(self.MAX_LINES)
        self.view.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        lay.addWidget(self.view, 1)
        smooth_scroll(self.view)
        attach_hint(self.view, T("Журнал пуст — здесь появится ход выполнения.", "The log is empty — progress will appear here."))
        self._lines: list[tuple[str, str]] = []
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(250)
        self._filter_timer.timeout.connect(self._refilter)
        self.search.textChanged.connect(lambda _t: self._filter_timer.start())
        self.level.currentIndexChanged.connect(lambda _i: self._refilter())
        self.level.setMinimumWidth(140)

    def _head_width(self) -> int:
        return self._head.sizeHint().width() + 6 if self._head is not None else 0

    def resizeEvent(self, event):  # noqa: N802 (Qt API)
        """Широкая колонка — всё в одну строку; узкая — фильтр и поиск второй строкой;
        совсем узкая — у переключателя автопрокрутки остаётся только сам переключатель."""
        super().resizeEvent(event)
        icons = 2 * 38 + 18
        wide = self.width() >= self._head_width() + self.level.minimumWidth() + 160 + self._follow_full + icons
        roomy = wide or self.width() >= self._head_width() + self._follow_full + icons + 24
        text = self._follow_text if roomy else ""
        if self.follow.onText != text:
            self.follow.setOnText(text)
            self.follow.setOffText(text)
        if wide == self._wide:
            return
        self._wide = wide
        stretch = 1 if self._head is not None else 0
        if wide:
            self._bar2.removeWidget(self._tools)
            self._bar.takeAt(stretch)                      # растяжка узкого вида
            self._bar.insertWidget(stretch, self._tools, 1)
        else:
            self._bar.removeWidget(self._tools)
            self._bar.insertStretch(stretch, 1)
            self._bar2.addWidget(self._tools, 1)

    def _passes(self, text: str, level: str) -> bool:
        need = self.level.currentData()
        if need == "ERROR" and level != "ERROR":
            return False
        if need == "WARNING" and level not in ("ERROR", "WARNING"):
            return False
        query = self.search.text().strip().lower()
        return not query or query in text.lower()

    def _fmt(self, level: str) -> QTextCharFormat:
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(theme.c(LEVEL_COLORS.get(level, "text"))))
        if level == "ERROR":
            fmt.setFontWeight(QFont.DemiBold)
        return fmt

    def _append_line(self, text: str, level: str) -> None:
        cursor = self.view.textCursor()
        cursor.movePosition(QTextCursor.End)
        if not self.view.document().isEmpty():
            cursor.insertBlock()
        cursor.insertText(text, self._fmt(level))
        if self.follow.isChecked():
            self.view.verticalScrollBar().setValue(self.view.verticalScrollBar().maximum())

    def append(self, text: str, level: str = "INFO") -> None:
        stamp = time.strftime("%H:%M:%S")
        line = f"[{stamp}] {text}"
        self._lines.append((line, level))
        if len(self._lines) > self.MAX_LINES:
            del self._lines[: len(self._lines) - self.MAX_LINES]
        if self._passes(line, level):
            self._append_line(line, level)

    def _refilter(self) -> None:
        self.view.clear()
        for line, level in self._lines:
            if self._passes(line, level):
                self._append_line(line, level)

    def clear(self) -> None:
        self._lines.clear()
        self.view.clear()

    def text(self) -> str:
        return "\n".join(line for line, _ in self._lines)

    def _save(self) -> None:
        name = time.strftime("log_%Y%m%d_%H%M%S.txt")
        target, _ = QFileDialog.getSaveFileName(self, tr(T("Сохранить журнал", "Save log")), name,
                                                "Text (*.txt)")
        if target:
            Path(target).write_text(self.text(), encoding="utf-8")


