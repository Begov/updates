"""Плавность интерфейса: прокрутка и переходы с частотой монитора.

Qt и qfluentwidgets двигают всё шагами по 16 мс (60 Гц), а плавная прокрутка Fluent
ещё и фиксированными порциями: если кадр рисуется дольше, прокрутка замедляется и
дёргается. Здесь положение считается по времени («где контент должен быть сейчас»),
а кадры идут с частотой монитора — на 120–180 Гц движение заметно плавнее, а
медленный кадр не тормозит движение, а только пропускается.
"""
from __future__ import annotations

import time

from PySide6.QtCore import QObject, QPoint, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QGuiApplication, QPainter, QPixmap, QRegion
from PySide6.QtWidgets import QAbstractItemView, QAbstractScrollArea, QApplication, QScrollArea, QTextEdit, QWidget

SCROLL_DURATION = 0.30      # с — щелчок колеса доезжает за это время (у Fluent ~0.4 с)
SCROLL_PER_NOTCH = 40       # px на «строку» колеса: 3 строки × 40 = 120 px, как раньше
PAGE_DURATION = 0.26        # с — смена страниц
PAGE_OUT = 0.30             # доля перехода, за которую гаснет старая страница
PAGE_IN_DELAY = 0.15        # доля перехода до начала проявления новой
PAGE_SHIFT = 22             # px — страница всплывает снизу на столько


def _ease_out(p: float) -> float:
    """Быстрый старт, мягкая остановка (кубическая)."""
    return 1.0 - (1.0 - p) ** 3


class _Clock(QObject):
    """Один общий точный таймер на частоте монитора; работает, только пока что-то движется."""

    def __init__(self):
        super().__init__()
        self._subs: list = []
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.timeout.connect(self._tick)

    @staticmethod
    def _interval() -> int:
        rates = [s.refreshRate() for s in QGuiApplication.screens()] or [60.0]
        rate = min(max(max(rates), 60.0), 240.0)
        return max(4, int(1000 / rate))

    def subscribe(self, step) -> None:
        """step(now) -> bool: False — движение закончено, отписать."""
        if step not in self._subs:
            self._subs.append(step)
        if not self._timer.isActive():
            self._timer.start(self._interval())

    def _tick(self) -> None:
        now = time.perf_counter()
        for step in list(self._subs):
            try:
                alive = step(now)
            except RuntimeError:            # виджет уже удалён
                alive = False
            if not alive and step in self._subs:
                self._subs.remove(step)
        if not self._subs:
            self._timer.stop()


_clock: _Clock | None = None


def clock() -> _Clock:
    global _clock
    if _clock is None:
        _clock = _Clock()
    return _clock


# ================================================================ прокрутка
class _Scroller:
    """Плавное движение одной полосы прокрутки к цели; новые щелчки колеса продлевают цель."""

    def __init__(self, bar):
        self.bar = bar
        self.active = False
        self.pos = self.start = self.target = 0.0
        self.t0 = 0.0
        self.last = None

    def _moving(self) -> bool:
        return self.active and self.bar.value() == self.last

    def add(self, delta: float) -> bool:
        """Сдвинуть цель на delta px. False — двигать некуда (край)."""
        moving = self._moving()
        cur = self.pos if moving else float(self.bar.value())
        if not self.go((self.target if moving else cur) + delta):
            return moving                    # упёрлись в край: колесо достаётся внешней прокрутке
        return True

    def go(self, target: float) -> bool:
        """Плавно доехать до target от текущего положения (с ходу, если уже едем)."""
        bar = self.bar
        cur = self.pos if self._moving() else float(bar.value())
        target = min(max(target, float(bar.minimum())), float(bar.maximum()))
        if abs(target - cur) < 0.5:
            return False
        self.start, self.target, self.pos = cur, target, cur
        self.t0 = time.perf_counter()
        self.last = bar.value()
        if not self.active:
            self.active = True
            clock().subscribe(self.step)
        return True

    def step(self, now: float) -> bool:
        bar = self.bar
        if bar.value() != self.last or bar.isSliderDown():
            self.active = False              # полосу двинули иначе (перетаскивание, переход к разделу)
            return False
        p = min(1.0, (now - self.t0) / SCROLL_DURATION)
        self.pos = self.start + (self.target - self.start) * _ease_out(p)
        value = round(self.pos)
        if value != bar.value():
            bar.setValue(value)
        self.last = bar.value()
        if p >= 1.0:
            self.active = False
            return False
        return True


_scrollers: dict = {}


def _scroller(bar) -> _Scroller:
    sc = _scrollers.get(bar)
    if sc is None:
        sc = _scrollers[bar] = _Scroller(bar)
        bar.destroyed.connect(lambda *_a, b=bar: _scrollers.pop(b, None))
    return sc


def scroll_to(bar, value: int) -> None:
    """Плавно прокрутить полосу к значению (переход к разделу по кнопке)."""
    _scroller(bar).go(float(value))


def _pixel_scrolled(area: QAbstractScrollArea) -> bool:
    """Полосы, у которых единица — пиксель (у QPlainTextEdit и таблиц «по строкам» — строка)."""
    if isinstance(area, (QScrollArea, QTextEdit)):
        return True
    return isinstance(area, QAbstractItemView) and area.verticalScrollMode() == QAbstractItemView.ScrollPerPixel


def wheel(obj, event) -> bool:
    """Колесо мыши над областью прокрутки → плавная прокрутка. True — событие обработано.

    Вызывается глобальным фильтром событий. Тачпады и колёса с мелким шагом, Ctrl/Shift
    и горизонталь остаются штатными: у них и так плавно или своё назначение.
    """
    area = obj.parent() if isinstance(obj, QWidget) else None
    if not isinstance(area, QAbstractScrollArea) or area.viewport() is not obj or not _pixel_scrolled(area):
        return False
    dy = event.angleDelta().y()
    if not dy or event.angleDelta().x() or dy % 120 or not event.pixelDelta().isNull() \
            or event.modifiers() != Qt.NoModifier:
        return False
    bar = area.verticalScrollBar()
    if bar.maximum() <= bar.minimum() or not bar.isEnabled():
        return False
    distance = -dy / 120 * QApplication.wheelScrollLines() * SCROLL_PER_NOTCH
    distance = max(-bar.pageStep(), min(bar.pageStep(), distance))
    if not _scroller(bar).add(distance):
        return False
    event.accept()
    return True


# ================================================================ смена страниц
class PageFade(QWidget):
    """Переход между страницами, как в Windows 11: старая быстро гаснет до фона, новая
    проявляется и чуть всплывает снизу.

    Рисуются готовые снимки, поэтому тяжёлая страница не перерисовывается на каждом
    кадре (штатный выезд Fluent двигает саму страницу и идёт рывками). Живая новая
    страница уже стоит под накладкой и видна, как только накладка исчезает.
    """

    def __init__(self, host: QWidget, old: QPixmap):
        super().__init__(host)
        self._old = old
        self._new = snapshot(host)
        self._bg = backdrop(host)
        self._p = 0.0
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_OpaquePaintEvent)      # страница под накладкой не перерисовывается
        self.setGeometry(host.rect())
        self.raise_()
        self.show()
        self._t0 = time.perf_counter()
        clock().subscribe(self._step)

    def _step(self, now: float) -> bool:
        if self._p >= 1.0:
            return False
        self._p = min(1.0, (now - self._t0) / PAGE_DURATION)
        if self._p >= 1.0:
            self.finish()
            return False
        self.update()
        return True

    def finish(self) -> None:
        self._p = 1.0
        self.hide()
        self.deleteLater()

    def paintEvent(self, _event):  # noqa: N802 (Qt API)
        out = 1.0 - min(1.0, self._p / PAGE_OUT)                        # старая гаснет
        e = _ease_out(max(0.0, (self._p - PAGE_IN_DELAY) / (1.0 - PAGE_IN_DELAY)))   # новая проявляется
        p = QPainter(self)
        p.drawPixmap(0, 0, self._bg)
        if out > 0.0:
            p.setOpacity(out)
            p.drawPixmap(0, 0, self._old)
        if e > 0.0:
            p.setOpacity(e)
            p.drawPixmap(QPoint(0, round(PAGE_SHIFT * (1.0 - e))), self._new)


def _canvas(widget: QWidget) -> QPixmap:
    dpr = widget.devicePixelRatioF()
    pix = QPixmap(widget.size() * dpr)
    pix.setDevicePixelRatio(dpr)
    background = widget.window().palette().window().color()
    background.setAlpha(255)
    pix.fill(background)                # непрозрачный снимок рисуется заметно быстрее прозрачного
    return pix


def snapshot(widget: QWidget) -> QPixmap:
    """Снимок того, что окно показывает на месте виджета, — с фоном окна и подложками под ним."""
    win = widget.window()
    pix = _canvas(widget)
    win.render(pix, QPoint(), QRegion(QRect(widget.mapTo(win, QPoint()), widget.size())))
    return pix


def backdrop(widget: QWidget) -> QPixmap:
    """Фон под виджетом без него самого: окно и подложки предков (у Fluent своя под страницами)."""
    chain = []
    w = widget.parentWidget()
    while w is not None:
        chain.append(w)
        if w.isWindow():
            break
        w = w.parentWidget()
    pix = _canvas(widget)
    for anc in reversed(chain):
        area = QRect(widget.mapTo(anc, QPoint()), widget.size())
        # фон палитрой — только у окна: у остальных он закрыл бы подложки под ними
        flags = QWidget.RenderFlag.DrawWindowBackground if anc.isWindow() else QWidget.RenderFlag(0)
        anc.render(pix, QPoint(), QRegion(area), flags)
    return pix


# ================================================================ иконки Fluent
def cache_svg_renderers() -> None:
    """qfluentwidgets разбирает SVG иконки заново на каждой перерисовке (стрелки числовых полей,
    значки кнопок — десятки за кадр прокрутки). Разобранный SVG кэшируется; рисуется он
    по-прежнему векторно, так что вид не меняется."""
    from PySide6.QtSvg import QSvgRenderer
    from qfluentwidgets.common import icon as fluent_icon
    from qfluentwidgets.components.widgets import info_bar

    if getattr(fluent_icon, "_bego_svg_cache", False):
        return
    fluent_icon._bego_svg_cache = True
    renderers: dict = {}

    def draw_svg_icon(icon, painter, rect):
        key = icon if isinstance(icon, (str, bytes)) else bytes(icon)
        renderer = renderers.get(key)
        if renderer is None:
            if len(renderers) > 400:
                renderers.clear()
            renderer = renderers[key] = QSvgRenderer(icon)
        renderer.render(painter, QRectF(rect))

    fluent_icon.drawSvgIcon = draw_svg_icon
    info_bar.drawSvgIcon = draw_svg_icon
