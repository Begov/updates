"""Оформление: палитры тёмной/светлой темы в духе Windows 11 (Fluent) и таблица стилей Qt.

Оболочку (окно, навигация, кнопки, переключатели, сегменты) рисует библиотека
qfluentwidgets, остальные виджеты Qt стилизуются здесь тем же языком:
скругления 8 px, тонкие рамки, акцентный цвет, воздух между элементами.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import (
    QAbstractSpinBox, QApplication, QComboBox, QDoubleSpinBox, QLineEdit, QSpinBox, QWidget,
)

from . import paths, state
from .ui import smooth

PALETTES = {
    "dark": {
        "bg": "#1a1c22",
        "surface": "#20232a",
        "card": "#262a32",
        "card_head": "#2b3039",
        "input": "#2e333d",
        "input_hover": "#363c48",
        "border": "#343a46",
        "border_strong": "#4a5262",
        "text": "#f1f3f7",
        "dim": "#b4bccb",
        "muted": "#7f8899",
        "accent": "#7b7cff",
        "accent_hover": "#9394ff",
        "accent_text": "#ffffff",
        "accent_soft": "#2f3163",
        "amber": "#f5b041",
        "green": "#4bd08b",
        "red": "#ff7a7a",
        "yellow": "#f4c75a",
        "blue": "#62aaff",
        "selection": "#3a3d7a",
        "log_bg": "#15171c",
    },
    "light": {
        "bg": "#f3f4f8",
        "surface": "#ffffff",
        "card": "#ffffff",
        "card_head": "#f7f8fb",
        "input": "#f3f4f8",
        "input_hover": "#e9ebf2",
        "border": "#e1e4ec",
        "border_strong": "#c8cddb",
        "text": "#1c2030",
        "dim": "#525b70",
        "muted": "#8a93a6",
        "accent": "#5b5ce6",
        "accent_hover": "#4a4bd0",
        "accent_text": "#ffffff",
        "accent_soft": "#e6e7fd",
        "amber": "#c77d06",
        "green": "#1f9d63",
        "red": "#d64545",
        "yellow": "#b98a06",
        "blue": "#2f74d0",
        "selection": "#d5d6fa",
        "log_bg": "#fbfbfd",
    },
}

_current = state.get("theme", "dark") if state.get("theme", "dark") in PALETTES else "dark"


def name() -> str:
    return _current


def colors() -> dict:
    return PALETTES[_current]


def c(key: str) -> str:
    return PALETTES[_current].get(key, "#ff00ff")


def set_theme(theme_name: str) -> None:
    global _current
    _current = theme_name if theme_name in PALETTES else "dark"
    state.put(theme=_current)


def stylesheet() -> str:
    p = colors()
    return f"""
* {{ outline: none; }}
QWidget {{ color: {p['text']}; font-size: 9.5pt; }}
QMainWindow, QDialog {{ background: {p['bg']}; }}
QWidget#Transparent, QScrollArea#Transparent, QScrollArea#Transparent > QWidget > QWidget,
QTreeWidget#Transparent, QListWidget#Transparent {{ background: transparent; border: none; }}
QScrollArea {{ border: none; background: transparent; }}
QToolTip {{
    background: {p['card_head']}; color: {p['text']}; border: 1px solid {p['border_strong']};
    border-radius: 8px; padding: 8px 10px; font-size: 9.5pt;
}}

/* ---------- подписи ---------- */
QLabel {{ background: transparent; }}
QLabel[role="label"] {{ color: {p['dim']}; }}
QLabel[role="dim"] {{ color: {p['dim']}; }}
QLabel[role="muted"] {{ color: {p['muted']}; font-size: 9pt; }}
QLabel[role="section"] {{ color: {p['accent']}; font-weight: 600; font-size: 9.5pt; padding-top: 6px; }}
QLabel[role="warn"] {{ color: {p['yellow']}; }}
QLabel[role="err"] {{ color: {p['red']}; }}
QLabel[role="ok"] {{ color: {p['green']}; }}
QLabel[role="big"] {{ font-size: 16pt; font-weight: 700; }}
QLabel#PageTitle {{ font-size: 18pt; font-weight: 700; background: transparent; }}
QLabel#PageHint {{ color: {p['muted']}; background: transparent; }}
QLabel#Brand {{ font-size: 15pt; font-weight: 700; }}
QLabel#TopTitle {{ font-size: 15pt; font-weight: 700; background: transparent; }}
QLabel#TopHint {{ color: {p['muted']}; font-size: 9pt; background: transparent; }}
QLabel#NavBrand {{ font-size: 12pt; font-weight: 700; background: transparent; }}
QLabel#BrandSub {{ color: {p['muted']}; font-size: 9pt; }}

/* ---------- чипы состояния ---------- */
QLabel[chip="true"] {{
    background: {p['card']}; color: {p['dim']}; border: 1px solid {p['border']};
    border-radius: 11px; padding: 4px 12px; font-size: 9pt; font-weight: 600;
}}
QLabel[chip="true"][compact="true"] {{ padding: 3px 9px; }}
QLabel[chip="true"][tone="ok"] {{ color: {p['green']}; border-color: {p['green']}; background: transparent; }}
QLabel[chip="true"][tone="warn"] {{ color: {p['yellow']}; border-color: {p['yellow']}; background: transparent; }}
QLabel[chip="true"][tone="err"] {{ color: {p['red']}; border-color: {p['red']}; background: transparent; }}

/* ---------- карточки ---------- */
QFrame#Card {{
    background: {p['card']}; border: 1px solid {p['border']}; border-radius: 12px;
}}
QFrame#Panel {{
    background: {p['surface']}; border: 1px solid {p['border']}; border-radius: 12px;
}}
QFrame#CardHead, QFrame#CardBody {{ background: transparent; border: none; }}
QLabel#CardTitle {{ font-size: 11.5pt; font-weight: 600; background: transparent; }}
QLabel#CardIcon {{ background: transparent; }}
QLabel#CardSummary {{ color: {p['muted']}; font-size: 9pt; background: transparent; }}
QFrame#Divider {{ background: {p['border']}; max-height: 1px; min-height: 1px; border: none; }}
QFrame#FormRow {{ background: transparent; border: none; }}

/* ---------- поля ввода ---------- */
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background: {p['input']}; border: 1px solid {p['border']}; border-radius: 8px;
    padding: 6px 10px; selection-background-color: {p['accent']}; selection-color: white;
}}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover {{
    background: {p['input_hover']}; border-color: {p['border_strong']};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus,
QPlainTextEdit:focus, QTextEdit:focus {{ border: 1px solid {p['accent']}; }}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{
    color: {p['muted']}; background: transparent;
}}
QComboBox {{ padding-right: 28px; min-height: 22px; }}
QComboBox::drop-down {{ border: none; width: 26px; subcontrol-position: center right; }}
QComboBox::down-arrow {{ image: none; width: 0; height: 0;
    border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-top: 5px solid {p['dim']}; margin-right: 10px; }}
QComboBox QAbstractItemView {{
    background: {p['card_head']}; border: 1px solid {p['border_strong']}; border-radius: 8px;
    padding: 4px; selection-background-color: {p['accent_soft']}; selection-color: {p['text']}; outline: none;
}}
QComboBox QAbstractItemView::item {{ min-height: 30px; padding: 0 10px; border-radius: 6px; }}
QPlainTextEdit#LogView {{
    background: {p['log_bg']}; border: 1px solid {p['border']}; border-radius: 10px;
    font-family: "Cascadia Mono", "Consolas", monospace; font-size: 9pt; padding: 10px;
}}
QPlainTextEdit#Editor {{
    background: {p['log_bg']}; font-family: "Cascadia Mono", "Consolas", monospace; font-size: 10pt; padding: 12px;
    border-radius: 10px;
}}

/* ---------- кнопки Qt (fluent-кнопки красятся библиотекой) ---------- */
QPushButton, QToolButton {{
    background: {p['input']}; border: 1px solid {p['border']}; border-radius: 8px; padding: 6px 16px;
}}
QPushButton:hover, QToolButton:hover {{ background: {p['input_hover']}; border-color: {p['border_strong']}; }}
QPushButton:pressed, QToolButton:pressed {{ background: {p['card_head']}; }}
QPushButton:disabled, QToolButton:disabled {{ color: {p['muted']}; background: transparent; }}
QToolButton {{ padding: 5px 9px; }}
QCheckBox {{ spacing: 10px; background: transparent; }}
QCheckBox::indicator {{ width: 18px; height: 18px; border-radius: 5px; border: 1px solid {p['border_strong']};
    background: {p['input']}; }}
QCheckBox::indicator:hover {{ border-color: {p['accent']}; }}
QCheckBox::indicator:checked {{ background: {p['accent']}; border-color: {p['accent']}; }}

/* ---------- таблицы, списки, деревья ---------- */
QTableWidget, QTableView, QListWidget, QTreeWidget {{
    background: {p['card']}; alternate-background-color: {p['surface']}; border: 1px solid {p['border']};
    border-radius: 10px; gridline-color: transparent; selection-background-color: {p['accent_soft']};
    selection-color: {p['text']};
}}
QListWidget::item, QTreeWidget::item {{ padding: 7px 8px; border-radius: 6px; margin: 1px 4px; }}
QListWidget::item:hover, QTreeWidget::item:hover {{ background: {p['input_hover']}; }}
QListWidget::item:selected, QTreeWidget::item:selected {{ background: {p['accent_soft']}; color: {p['text']}; }}
QTableWidget::item {{ padding: 6px 10px; border: none; }}
QTableWidget::item:selected {{ background: {p['accent_soft']}; color: {p['text']}; }}
QHeaderView {{ background: transparent; }}
QHeaderView::section {{
    background: {p['card_head']}; color: {p['dim']}; border: none; border-bottom: 1px solid {p['border']};
    padding: 9px 10px; font-weight: 600; font-size: 9pt;
}}
QTableCornerButton::section {{ background: {p['card_head']}; border: none; }}

/* ---------- прогресс ---------- */
QProgressBar {{ background: {p['input']}; border: none; border-radius: 4px; text-align: center; min-height: 8px;
    max-height: 8px; color: transparent; }}
QProgressBar::chunk {{ background: {p['accent']}; border-radius: 4px; }}

/* ---------- скроллбары: тонкие, скруглённые, светлеют при наведении ---------- */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 4px 2px; }}
QScrollBar::handle:vertical {{ background: {p['border_strong']}; min-height: 40px; border-radius: 3px; margin: 0 1px; }}
QScrollBar::handle:vertical:hover {{ background: {p['accent']}; margin: 0; }}
QScrollBar::handle:vertical:pressed {{ background: {p['accent_hover']}; margin: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px 4px; }}
QScrollBar::handle:horizontal {{ background: {p['border_strong']}; min-width: 40px; border-radius: 3px; margin: 1px 0; }}
QScrollBar::handle:horizontal:hover {{ background: {p['accent']}; margin: 0; }}
QScrollBar::handle:horizontal:pressed {{ background: {p['accent_hover']}; margin: 0; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; background: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ---------- меню, сплиттер, диалоги ---------- */
QMenu {{
    background: {p['card_head']}; border: 1px solid {p['border_strong']}; border-radius: 10px; padding: 6px;
}}
QMenu::item {{ padding: 8px 28px 8px 14px; border-radius: 6px; }}
QMenu::item:selected {{ background: {p['accent_soft']}; }}
QMenu::separator {{ height: 1px; background: {p['border']}; margin: 5px 8px; }}
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:horizontal {{ width: 10px; }}
QSplitter::handle:vertical {{ height: 10px; }}
QMessageBox {{ background: {p['bg']}; }}
QDialogButtonBox QPushButton {{ min-width: 90px; }}
QDialogButtonBox QPushButton:default {{
    background: {p['accent']}; color: white; border-color: {p['accent']};
}}
"""


CONTROL_HEIGHT = 32   # единая высота кнопок, списков и полей ввода


def _flatten_fluent_borders() -> None:
    """Убирает у полей Fluent нижнюю линию (акцентную при фокусе) — остаётся ровная рамка."""
    from PySide6.QtWidgets import QLineEdit as _QLineEdit
    from qfluentwidgets import LineEdit
    from qfluentwidgets.components.widgets import spin_box

    LineEdit.paintEvent = lambda self, e: _QLineEdit.paintEvent(self, e)
    spin_box.SpinBoxBase._drawBorderBottom = lambda self: None


def _tidy_fluent_scrollbars() -> None:
    """Полоса прокрутки Fluent: тонкая, с отступом от края, без стрелок, акцентная при наведении.

    Штатная полоса рисуется вплотную к краю карточки, а при наведении выезжает на
    подложку со стрелками. Здесь она ужата до пилюли, прижатой к краю с зазором;
    цвет берётся из палитры текущей темы, а при наведении и перетаскивании —
    акцентный; толщина не меняется.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QPainter
    from qfluentwidgets.components.widgets import scroll_bar as sb

    if getattr(sb.ScrollBar, "_bego_tidy", False):
        return
    sb.ScrollBar._bego_tidy = True

    def _rgba(hexcolor: str, alpha: int) -> QColor:
        color = QColor(hexcolor)
        color.setAlpha(alpha)
        return color

    orig_init = sb.ScrollBar.__init__

    def init(self, orient, parent):
        orig_init(self, orient, parent)
        self.groove.upButton.hide()          # стрелки не нужны: есть колесо и перетаскивание
        self.groove.downButton.hide()
        self._padding = 10
        self.groove.setLightBackgroundColor(QColor(0, 0, 0, 0))   # подложки нет вовсе
        self.groove.setDarkBackgroundColor(QColor(0, 0, 0, 0))
        self.handle.setFixedWidth(5) if orient == Qt.Vertical else self.handle.setFixedHeight(5)

    def adjust_pos(self, size):
        """Зазор 4 px от края и по 8 px сверху и снизу — полоса не упирается в рамки."""
        if self.orientation() == Qt.Vertical:
            self.resize(12, max(0, size.height() - 16))
            self.move(size.width() - 16, 8)
        else:
            self.resize(max(0, size.width() - 16), 12)
            self.move(8, size.height() - 16)

    def on_opacity(self):
        """Штатно полоса утолщается при наведении/клике — здесь толщина постоянная."""
        if self.orientation() == Qt.Vertical:
            self.handle.setFixedWidth(5)
        else:
            self.handle.setFixedHeight(5)
        self._adjustHandlePos()

    def handle_paint(self, _event):
        bar = self.parent()
        live = bool(getattr(bar, "_isEnter", False) or getattr(bar, "_isPressed", False))
        pal = colors()
        painter = QPainter(self)
        painter.setRenderHints(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setOpacity(self.opacity)
        if live:
            painter.setBrush(_rgba(pal["accent"], 230))
        else:
            painter.setBrush(_rgba(pal["text"], 70 if _current == "dark" else 80))
        radius = (self.width() if self.orient == Qt.Vertical else self.height()) / 2
        painter.drawRoundedRect(self.rect(), radius, radius)

    sb.ScrollBar.__init__ = init
    sb.ScrollBar._adjustPos = adjust_pos
    sb.ScrollBar._onOpacityAniValueChanged = on_opacity
    sb.ScrollBarHandle.paintEvent = handle_paint


def _flat_style(p: dict) -> str:
    """Ровная рамка со всех сторон, при фокусе — акцентная."""
    rules = []
    for cls in ("LineEdit", "SpinBox", "DoubleSpinBox", "ComboBox"):
        rules.append(f"{cls} {{ border: 1px solid {p['border']}; border-bottom: 1px solid {p['border']}; }}")
        rules.append(f"{cls}:hover {{ border: 1px solid {p['border_strong']}; border-bottom: 1px solid {p['border_strong']}; }}")
        # у Fluent есть «тяжёлые» правила с атрибутом transparent — перекрываем их тем же весом
        focus = f"{cls}:focus, {cls}[transparent=true]:focus, {cls}[transparent=false]:focus"
        rules.append(f"{focus} {{ border: 1px solid {p['accent']}; border-bottom: 1px solid {p['accent']}; }}")
    return "\n".join(rules)


_POLISH, _WHEEL = QEvent.Type.Polish, QEvent.Type.Wheel   # фильтр видит каждое событие — без лишних поисков


class _HeightGuard(QObject):
    """Выравнивает высоту всех контролов: у Fluent и нативных виджетов она по умолчанию разная.

    Этот же общий фильтр отдаёт колесо мыши плавной прокрутке (ui/smooth.py): второй
    фильтр на всё приложение стоил бы лишнего вызова Python на каждое событие."""

    def __init__(self):
        super().__init__()
        from qfluentwidgets import ComboBox, DoubleSpinBox, LineEdit, PushButton, SpinBox, SwitchButton, ToolButton
        self._switch = SwitchButton
        self._fluent = (PushButton, ComboBox, LineEdit, SpinBox, DoubleSpinBox)
        self._tool = ToolButton
        self._native = (QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox)
        self._flat_types = (LineEdit, ComboBox, SpinBox, DoubleSpinBox)

    def eventFilter(self, obj, event):  # noqa: N802 (Qt API)
        kind = event.type()
        if kind == _WHEEL:
            return smooth.wheel(obj, event)
        if kind == _POLISH and isinstance(obj, QWidget) and not obj.property("keepHeight"):
            parent = obj.parent()
            if isinstance(parent, (QAbstractSpinBox, QComboBox, self._switch)):
                return False                                   # внутренние части составных виджетов
            if isinstance(obj, self._tool):
                if not obj.text():
                    obj.setFixedSize(CONTROL_HEIGHT, CONTROL_HEIGHT)
                else:
                    obj.setFixedHeight(CONTROL_HEIGHT)
            elif isinstance(obj, self._fluent + self._native):
                obj.setFixedHeight(CONTROL_HEIGHT)
                if isinstance(obj, self._flat_types) and not obj.property("flatDone"):
                    obj.setProperty("flatDone", True)
                    obj.setStyleSheet(obj.styleSheet() + "\n" + _flat_style(colors()))
        return False


_guard = None
_FONT_FAMILY = None


def _load_fonts() -> str:
    """Подключить шрифт Inter из assets/fonts; без файлов остаётся Segoe UI."""
    global _FONT_FAMILY
    if _FONT_FAMILY is None:
        _FONT_FAMILY = "Segoe UI"
        loaded = False
        for f in sorted((paths.ASSETS / "fonts").glob("Inter-*.ttf")):
            loaded = QFontDatabase.addApplicationFont(str(f)) >= 0 or loaded
        if loaded and "Inter" in QFontDatabase.families():
            _FONT_FAMILY = "Inter"
    return _FONT_FAMILY


def apply(app: QApplication) -> None:
    """Применить тему: Fluent-тема библиотеки + палитра Qt + общий QSS."""
    from qfluentwidgets import Theme, setTheme, setThemeColor
    app.setStyle("Fusion")
    p = colors()
    setTheme(Theme.DARK if _current == "dark" else Theme.LIGHT)
    setThemeColor(QColor(p["accent"]))
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(p["bg"]))
    pal.setColor(QPalette.WindowText, QColor(p["text"]))
    pal.setColor(QPalette.Base, QColor(p["input"]))
    pal.setColor(QPalette.AlternateBase, QColor(p["card"]))
    pal.setColor(QPalette.Text, QColor(p["text"]))
    pal.setColor(QPalette.Button, QColor(p["input"]))
    pal.setColor(QPalette.ButtonText, QColor(p["text"]))
    pal.setColor(QPalette.Highlight, QColor(p["accent"]))
    pal.setColor(QPalette.HighlightedText, QColor(p["accent_text"]))
    pal.setColor(QPalette.ToolTipBase, QColor(p["card_head"]))
    pal.setColor(QPalette.ToolTipText, QColor(p["text"]))
    pal.setColor(QPalette.PlaceholderText, QColor(p["muted"]))
    app.setPalette(pal)
    family = _load_fonts()
    font = QFont(family)
    font.setPointSizeF(9.5)
    font.setHintingPreference(QFont.PreferNoHinting)   # мягкие контуры вместо «рубленых» пикселей
    font.setStyleStrategy(QFont.PreferAntialias)
    app.setFont(font)
    try:
        from qfluentwidgets import setFontFamilies
        setFontFamilies([family, "Segoe UI", "Microsoft YaHei"])
    except Exception:  # noqa: BLE001
        pass
    app.setStyleSheet(stylesheet())
    global _guard
    if _guard is None:
        _flatten_fluent_borders()
        _tidy_fluent_scrollbars()
        smooth.cache_svg_renderers()
        _guard = _HeightGuard()
        app.installEventFilter(_guard)
