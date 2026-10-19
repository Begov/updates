"""Главное окно в стиле Windows 11 (Fluent): навигация слева, статусы в заголовке, страницы справа."""
from __future__ import annotations

import subprocess
import threading

from PySide6.QtCore import QEvent, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QKeySequence, QLinearGradient, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QMenu, QMessageBox, QScrollArea, QStackedWidget, QSystemTrayIcon,
    QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    FluentIcon, FluentWindow, InfoBar, InfoBarPosition, NavigationAvatarWidget, NavigationItemPosition,
)

from . import APP_NAME, APP_VERSION, engine_link, i18n, paths, state, theme
from .i18n import T, tr
from .services import monitors as mon
from .services import procs
from .ui.widgets import Chip, set_tip

NAV_GAP = 10   # px между боковой панелью и областью контента
# Минимальный размер окна: все страницы перестраиваются под него без наложений.
# Высота с запасом под ноутбуки 1366×768 и 1920×1080 при масштабе 150 % (за вычетом панели задач).
MIN_WIDTH, MIN_HEIGHT = 1024, 640
# Уже этой ширины боковая панель сворачивается в иконки (по кнопке-бургеру раскрывается поверх)
NAV_EXPAND_WIDTH = 1400
# Уже этой ширины подзаголовок страницы прячется — чипам состояния нужно место
TOP_HINT_WIDTH = 1280

PAGES = [
    ("pipeline", "PLAY", T("Конвейер", "Pipeline"),
     T("Очередь файлов → расшифровка → режиссура → картинки и видео → склейка",
       "File queue → transcription → direction → images and video → render")),
    ("control", "DEVELOPER_TOOLS", T("Панель управления", "Control Panel"),
     T("Проекты, отдельные стадии, очередь склейки и доводка проектов",
       "Projects, single stages, concat queue and finishing projects")),
    ("prompts", "EDIT", T("Промпты", "Prompts"),
     T("Инструкции для ИИ и шаблоны субтитров", "AI instructions and subtitle templates")),
    ("settings", "SETTING", T("Настройки", "Settings"),
     T("Вход в Codex, ключи сервисов, параметры и внешний вид", "Codex sign-in, service keys, parameters and appearance")),
]


def _icon(*names):
    for name in names:
        icon = getattr(FluentIcon, name, None)
        if icon is not None:
            return icon
    return FluentIcon.TAG


def app_icon() -> QIcon:
    """Иконка приложения рисуется кодом — отдельные файлы не нужны."""
    icon_file = paths.ASSETS / "icon.ico"
    if icon_file.exists():
        return QIcon(str(icon_file))
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        pix = QPixmap(size, size)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        grad = QLinearGradient(0, 0, size, size)
        grad.setColorAt(0, QColor("#8b8cff"))
        grad.setColorAt(1, QColor("#5b5ce6"))
        p.setBrush(grad)
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(QRect(0, 0, size, size), size * 0.24, size * 0.24)
        p.setPen(QColor("white"))
        font = QFont("Segoe UI", max(6, int(size * 0.5)))
        font.setBold(True)
        p.setFont(font)
        p.drawText(QRect(0, 0, size, size), Qt.AlignCenter, "B")
        p.end()
        icon.addPixmap(pix)
    return icon


class PageHost(QWidget):
    """Оболочка страницы: сама страница создаётся позже (сборка дорогая).

    Страница лежит в области прокрутки: если окно меньше, чем нужно странице, появляется
    прокрутка, а не наложение блоков друг на друга. Обычно страница помещается целиком —
    у колонок свои прокрутки, — и полос не видно.
    """

    def __init__(self, key: str):
        super().__init__()
        self.setObjectName(f"host_{key}")
        self.page = None
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(12, 6, 12, 0)
        self._lay.setSpacing(0)
        self._scroll = QScrollArea()
        self._scroll.setObjectName("Transparent")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._lay.addWidget(self._scroll, 1)

    def set_page(self, page: QWidget) -> None:
        self.page = page
        page.setObjectName(page.objectName() or "Transparent")
        self._scroll.setWidget(page)


class MainWindow(FluentWindow):
    toast_requested = Signal(str)

    def __init__(self, monitors, concat_queue, restore: dict | None = None):
        super().__init__()
        self.monitors = monitors
        self.concat_queue = concat_queue
        self._busy: dict[str, bool] = {}
        self._last_veo: dict = {}
        self._last_quota: dict = {}
        self._last_cinai: dict = {}
        self._rebuilding = False
        self._updating = False
        self._theme_at_build = theme.name()
        first_window = not restore
        restore = restore or {}
        self._last_quota = restore.get("quota", {})
        self._last_veo = restore.get("veo", {})
        self._last_cinai = restore.get("cinai", {})

        self.setWindowTitle(f"{APP_NAME}")
        self.setWindowIcon(app_icon())
        self.setMinimumSize(MIN_WIDTH, MIN_HEIGHT)
        # фон задаём сами — вид не зависит от того, включён ли Mica в системе
        self.setMicaEffectEnabled(False)
        self.setCustomBackgroundColor(QColor(theme.c("bg")), QColor(theme.c("bg")))
        self.navigationInterface.setExpandWidth(250)
        self.navigationInterface.setMinimumExpandWidth(NAV_EXPAND_WIDTH)
        try:
            self.navigationInterface.setReturnButtonVisible(False)
        except Exception:  # noqa: BLE001
            pass
        self.toast_requested.connect(self._show_toast)

        self._build_tray()
        self._build_sidebar_header()
        self._build_title_area()
        self._build_titlebar_chips()

        monitors.codex_quota.connect(self._on_quota)
        monitors.veo_accounts.connect(self._on_veo)
        monitors.cinai_status.connect(self._on_cinai)
        monitors.note.connect(self._on_note)
        concat_queue.job_finished.connect(self._on_concat_done)
        concat_queue.changed.connect(self._refresh_busy)

        from .services import update_ui
        self.updates = update_ui.UpdateController(self)
        if first_window:
            self.updates.start()      # проверка обновлений — один раз за запуск, не при смене языка/темы

        self._build_pages(restore)
        self._nav_opened = False
        geo = restore.get("geometry")
        if geo is not None:
            self.setGeometry(geo)
            if restore.get("maximized"):
                self.showMaximized()
        else:
            saved = state.get("geometry")
            try:
                if saved and self.restoreGeometry(bytes.fromhex(saved)):
                    pass
                else:
                    self.resize(1520, 940)
            except ValueError:
                self.resize(1520, 940)

    # ================================================================ сборка
    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(app_icon(), self)
        self.tray.setToolTip(APP_NAME)
        menu = QMenu()
        menu.addAction(QAction(tr(T("Показать окно", "Show window")), self, triggered=self._raise))
        menu.addAction(QAction(tr(T("Выход", "Quit")), self, triggered=self.close))
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self._raise() if reason == QSystemTrayIcon.Trigger else None)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

    def _build_sidebar_header(self) -> None:
        """Бургер, иконка и название — в шапке боковой панели, а не в заголовке окна."""
        self.titleBar.iconLabel.hide()
        self.titleBar.titleLabel.hide()
        panel = self.navigationInterface.panel
        panel.vBoxLayout.setContentsMargins(0, 10, 0, 5)  # шапка на уровне заголовка страницы
        top = panel.topLayout
        index = top.indexOf(panel.menuButton)
        top.removeWidget(panel.menuButton)
        header = QWidget()
        row = QHBoxLayout(header)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(panel.menuButton, 0, Qt.AlignLeft | Qt.AlignVCenter)
        self._brand = QWidget()
        brand = QHBoxLayout(self._brand)
        brand.setContentsMargins(4, 0, 0, 0)
        brand.setSpacing(10)
        logo = QLabel()
        logo.setPixmap(app_icon().pixmap(QSize(24, 24)))
        name = QLabel(APP_NAME)
        name.setObjectName("NavBrand")
        brand.addWidget(logo)
        brand.addWidget(name)
        brand.addStretch(1)
        row.addWidget(self._brand, 1)
        row.addStretch(0)
        self._brand.setVisible(False)
        top.insertWidget(max(0, index), header)
        # в узком (свёрнутом) режиме остаётся один бургер
        self.navigationInterface.displayModeChanged.connect(lambda _m: self._sync_brand())
        panel.expandAni.finished.connect(self._sync_brand)

    def _sync_brand(self) -> None:
        panel = self.navigationInterface.panel
        self._brand.setVisible(panel.displayMode.name != "COMPACT" and not panel.isCollapsed())

    def _build_title_area(self) -> None:
        """Заголовок страницы стоит в верхней строке — на одном уровне с бургером и логотипом."""
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self._title_gap = QWidget()
        self._title_gap.setFixedWidth(0)
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        self._top_title = QLabel("")
        self._top_title.setObjectName("TopTitle")
        self._top_hint = QLabel("")
        self._top_hint.setObjectName("TopHint")
        col.addWidget(self._top_title)
        col.addWidget(self._top_hint)
        lay.addWidget(self._title_gap)
        lay.addLayout(col)
        self.titleBar.hBoxLayout.insertWidget(0, box, 0, Qt.AlignVCenter)
        self.titleBar.setFixedHeight(56)
        self.widgetLayout.setContentsMargins(NAV_GAP, 56, 0, 0)   # зазор между панелью и областью контента
        # отступ слева равен ширине панели: заголовок стоит над левым краем карточек
        self.stackedWidget.installEventFilter(self)

    def eventFilter(self, obj, event):  # noqa: N802 (Qt API)
        stacked = getattr(self, "stackedWidget", None)
        if obj is stacked and getattr(self, "_title_gap", None) is not None \
                and event.type() in (QEvent.Move, QEvent.Resize, QEvent.Show):
            self._sync_title_gap()
        return super().eventFilter(obj, event)

    def _sync_title_gap(self) -> None:
        # заголовок начинается в x=46 (там начинается строка окна) — ставим его вплотную к краю панели
        left = self.stackedWidget.mapTo(self, self.stackedWidget.rect().topLeft()).x()
        self._title_gap.setFixedWidth(max(0, left - 46))
        self._top_hint.setVisible(bool(self._top_hint.text()) and self.width() >= TOP_HINT_WIDTH)

    def _build_titlebar_chips(self) -> None:
        """Чипы состояния сервисов живут в заголовке окна и видны на любой странице."""
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 12, 0)
        lay.setSpacing(8)
        self.busy_chip = Chip("")
        self.busy_chip.hide()
        self.gpt_chip = Chip("GPT …")
        self.veo_chip = Chip("Veo …")
        self.cinai_chip = Chip("CinAI …")
        for chip, tip, slot in (
            (self.gpt_chip, T("Квота подписки ChatGPT для Codex: окно 5 ч / неделя. Клик — обновить.",
                              "ChatGPT subscription quota for Codex: 5 h window / week. Click to refresh."),
             self.monitors.refresh_codex),
            (self.veo_chip, T("Остаток лимитов VeoNonStop по ключам. Клик — обновить.",
                              "VeoNonStop remaining limits per key. Click to refresh."), self.monitors.refresh_veo),
            (self.cinai_chip, T("Сессия и баланс CinAI. Клик — обновить.", "CinAI session and balance. Click to refresh."),
             self.monitors.refresh_cinai)):
            set_tip(chip, tip)
            chip.setCursor(Qt.PointingHandCursor)
            chip.mouseReleaseEvent = lambda _e, s=slot: s()
        for chip in (self.busy_chip, self.gpt_chip, self.veo_chip, self.cinai_chip):
            lay.addWidget(chip)
        bar = self.titleBar.hBoxLayout
        bar.insertWidget(max(0, bar.count() - 1), box, 0, Qt.AlignVCenter)
        self._chips_box = box

    def _build_pages(self, restore: dict) -> None:
        from .pages.control_page import ControlPage
        from .pages.pipeline_page import PipelinePage
        from .pages.prompts_page import PromptsPage
        from .pages.settings_page import SettingsPage
        self._restore_pipeline = restore.get("pipeline")
        self.pages: dict = {}
        self.hosts: dict = {}
        for key, icon, title, hint in PAGES:
            host = PageHost(key)
            self.hosts[key] = host
            position = NavigationItemPosition.BOTTOM if key == "settings" else NavigationItemPosition.TOP
            item = self.addSubInterface(host, _icon(icon), tr(title), position)
            item.setToolTip(tr(hint))

        # быстрые действия внизу панели
        nav = self.navigationInterface
        bottom = NavigationItemPosition.BOTTOM
        nav.addItem("open_projects", _icon("FOLDER"), tr(T("Папка проектов", "Projects folder")),
                    lambda: procs.open_path(paths.PROJECTS), False, bottom)
        nav.addItem("open_inbox", _icon("DOWNLOAD", "ADD_TO"), tr(T("Входящие", "Inbox")),
                    lambda: procs.open_path(paths.INBOX), False, bottom)
        nav.addItem("open_logs", _icon("DOCUMENT"), tr(T("Журналы", "Logs")),
                    lambda: procs.open_path(paths.LOGS_DIR), False, bottom)
        nav.addItem("toggle_lang", _icon("LANGUAGE", "GLOBE"), "English" if i18n.is_ru() else "Русский",
                    lambda: self.apply_appearance("en" if i18n.is_ru() else "ru", theme.name()), False, bottom,
                    tr(T("Переключить язык интерфейса", "Switch interface language")))
        nav.addItem("toggle_theme", _icon("CONSTRACT", "BRIGHTNESS"),
                    tr(T("Светлая тема", "Light theme")) if theme.name() == "dark" else tr(T("Тёмная тема", "Dark theme")),
                    lambda: self.apply_appearance(i18n.language(), "light" if theme.name() == "dark" else "dark"),
                    False, bottom, tr(T("Светлая / тёмная тема", "Light / dark theme")))

        for i, (key, *_r) in enumerate(PAGES):
            QShortcut(QKeySequence(f"Ctrl+{i + 1}"), self, activated=lambda k=key: self.show_page(k))
        first = state.get("page", "pipeline")
        self.show_page(first)
        self._build_rest(["pipeline"] + [k for k, *_r in PAGES if k not in ("pipeline", first)]
                         if first != "pipeline" else [k for k, *_r in PAGES if k != "pipeline"])
        if self._last_quota:
            self._on_quota(self._last_quota)
        if self._last_veo:
            self._on_veo(self._last_veo)
        if self._last_cinai:
            self._on_cinai(self._last_cinai)
        self._refresh_busy()

    # ============================================================ навигация
    def _page_class(self, key: str):
        from .pages.control_page import ControlPage
        from .pages.pipeline_page import PipelinePage
        from .pages.prompts_page import PromptsPage
        from .pages.settings_page import SettingsPage
        return {"pipeline": PipelinePage, "control": ControlPage, "prompts": PromptsPage,
                "settings": SettingsPage}[key]

    def page(self, key: str):
        """Страница по ключу; создаётся при первом обращении (сборка страниц дорогая)."""
        if key not in self.pages:
            page = self._page_class(key)(self)
            self.pages[key] = page
            self.hosts[key].set_page(page)
            if key == "pipeline" and self._restore_pipeline:
                page.import_state(self._restore_pipeline)
        return self.pages[key]

    def _build_rest(self, queue: list) -> None:
        """Достроить остальные страницы в паузах между событиями интерфейса."""
        if self._rebuilding or not queue:
            return
        self.page(queue[0])
        QTimer.singleShot(30, self, lambda: self._build_rest(queue[1:]))

    def show_page(self, key: str) -> None:
        if key not in self.hosts:
            key = "pipeline"
        self.page(key)
        self.switchTo(self.hosts[key])

    def switchTo(self, interface) -> None:  # noqa: N802 (переопределение метода FluentWindow)
        """Мгновенное переключение страниц: анимация выезда Fluent на тяжёлых страницах рывками."""
        key = interface.objectName().removeprefix("host_")
        if key in self.hosts:
            self.page(key)                                   # страница может быть ещё не создана
        QStackedWidget.setCurrentWidget(self.stackedWidget.view, interface)
        meta = next((p for p in PAGES if p[0] == key), None)
        if meta:
            self._top_title.setText(tr(meta[2]))
            self._top_hint.setText(tr(meta[3]))
            self._sync_title_gap()
            state.put(page=key)

    def _raise(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # ======================================================== API для страниц
    def toast(self, text: str) -> None:
        """Можно вызывать из любого потока."""
        self.toast_requested.emit(str(text))

    def _show_toast(self, text: str) -> None:
        InfoBar.success(title="", content=text, orient=Qt.Horizontal, isClosable=True,
                        position=InfoBarPosition.BOTTOM_RIGHT, duration=3200, parent=self)

    def _on_note(self, text: str, _level: str) -> None:
        self.toast(text)

    def notify(self, title: str, text: str) -> None:
        """Системное уведомление (если окно не в фокусе) + всплывашка."""
        self.toast(f"{title}: {text}")
        if self.tray.isVisible() and not self.isActiveWindow():
            self.tray.showMessage(title, text, app_icon(), 6000)

    def set_busy(self, key: str, busy: bool) -> None:
        self._busy[key] = busy
        self._refresh_busy()

    def is_busy(self) -> bool:
        return any(self._busy.values())

    def _refresh_busy(self, *_) -> None:
        names = {"pipeline": T("конвейер", "pipeline"), "control": T("задачи", "tasks")}
        active = [tr(names.get(k, (k, k))) for k, v in self._busy.items() if v]
        if self.concat_queue.active_count():
            active.append(tr(T("склейка", "render")))
        if active:
            self.busy_chip.show_state("● " + tr(T("в работе: ", "running: ")) + ", ".join(active), "warn")
            self.busy_chip.show()
        else:
            self.busy_chip.hide()

    def restart_key(self) -> None:
        """Снять задачи VeoNonStop на сервере, не останавливая генерацию (воркеры повторят)."""
        from .pages.control_page import stage_job

        self.toast(tr(T("Снимаю задачи VeoNonStop…", "Cancelling VeoNonStop tasks…")))

        def work():
            try:
                out = subprocess.run(stage_job("cancel_veo", {}), cwd=str(paths.ENGINE), capture_output=True,
                                     text=True, encoding="utf-8", errors="replace", timeout=180,
                                     creationflags=procs.CREATE_NO_WINDOW)
                last = [l for l in (out.stdout or "").splitlines() if l.strip()]
                self.toast(last[-1] if last else tr(T("Готово", "Done")))
            except Exception as exc:  # noqa: BLE001
                self.toast(f"Restart key: {exc}")
            self.monitors.refresh_veo_keys()
        threading.Thread(target=work, daemon=True).start()

    def apply_appearance(self, lang: str, theme_name: str) -> bool:
        """Сменить язык/тему: окно пересоздаётся (только когда ничего не выполняется)."""
        if lang == i18n.language() and theme_name == theme.name():
            return True
        if self.is_busy():
            QMessageBox.information(self, APP_NAME, tr(T("Дождитесь окончания задач — окно пересоздаётся при смене языка "
                                                         "или темы.", "Wait until tasks finish — the window is recreated "
                                                         "when the language or theme changes.")))
            return False
        for page in ("settings", "prompts"):
            if not self._updating and page in self.pages and self.pages[page].has_unsaved():
                if QMessageBox.question(self, APP_NAME, tr(T("Есть несохранённые изменения. Продолжить без сохранения?",
                                                             "There are unsaved changes. Continue without saving?"))) \
                        != QMessageBox.Yes:
                    return False
                break
        i18n.set_language(lang)
        theme.set_theme(theme_name)
        QTimer.singleShot(0, self._rebuild)
        return True

    def _rebuild(self) -> None:
        """Создать новое окно с тем же содержимым и закрыть старое без вопросов.

        Старое окно уничтожается ДО смены темы: иначе Qt перекрашивает все его виджеты
        (несколько секунд) ради окна, которое всё равно исчезнет.
        """
        theme_changed = self._theme_at_build != theme.name()
        restore = {
            "pipeline": self.page("pipeline").export_state(),
            "quota": self._last_quota, "veo": self._last_veo, "cinai": self._last_cinai,
            "geometry": self.geometry(), "maximized": self.isMaximized(),
        }
        self.page("pipeline").shutdown()
        QApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.setQuitOnLastWindowClosed(False)
        try:
            if theme_changed:
                # смена темы: сначала убрать старое окно, иначе Qt перекрашивает и его виджеты
                self._dispose()
                theme.apply(QApplication.instance())
            fresh = MainWindow(self.monitors, self.concat_queue, restore)
            QApplication.instance().setProperty("main_window", fresh)
            fresh.show()
            if not theme_changed:
                # смена языка: старое окно исчезает только когда новое уже готово — без мигания
                QApplication.processEvents()
                self._dispose()
        finally:
            QApplication.setQuitOnLastWindowClosed(True)
            QApplication.restoreOverrideCursor()

    def _dispose(self) -> None:
        self._rebuilding = True
        self.tray.hide()
        self.hide()
        self.close()
        self.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.DeferredDelete)

    # ============================================================ статусы
    def _on_quota(self, data: dict) -> None:
        if data.get("ok"):
            self._last_quota = data
        q = mon.quota_texts(data)
        if q["ok"]:
            pct = max(q["p_pct"], q["s_pct"])
            self.gpt_chip.show_state(f"GPT  5h {q['p_pct']}% · 7d {q['s_pct']}%", mon.tone_for_pct(pct),
                                     tr(T(f"План: {q['plan']}\n5 ч: {q['p_pct']}% — сброс через {q['p_reset']}\n"
                                          f"Неделя: {q['s_pct']}% — сброс через {q['s_reset']}",
                                          f"Plan: {q['plan']}\n5 h: {q['p_pct']}% — resets in {q['p_reset']}\n"
                                          f"Week: {q['s_pct']}% — resets in {q['s_reset']}")))
        elif not self._last_quota:
            self.gpt_chip.show_state(tr(T("GPT: нет входа", "GPT: not signed in")), "err", q.get("error", ""))

    def _on_veo(self, data: dict) -> None:
        self._last_veo = data
        slots = data.get("slots") or {}
        if not slots:
            self.veo_chip.show_state(tr(T("Veo: нет ключей", "Veo: no keys")), "")
            return
        mod = engine_link.unit("veo_accounts")
        ok = [s for s in slots.values() if s.get("ok")]
        remaining = sum(int(s.get("image_remaining") or 0) for s in ok)
        tone = "ok" if len(ok) == len(slots) else ("warn" if ok else "err")
        tip = "\n".join(mod.quota_row_text(s) for s in slots.values())
        self.veo_chip.show_state(f"Veo {len(ok)}/{len(slots)} · 🍌 {remaining:,}", tone, tip)

    def _on_cinai(self, data: dict) -> None:
        self._last_cinai = data
        if data.get("missing"):
            self.cinai_chip.hide()
            return
        self.cinai_chip.show()
        if data.get("ok"):
            bal = data.get("balance") or {}
            amount = bal.get("balance", bal.get("credits", bal.get("amount", "?")))
            self.cinai_chip.show_state(f"CinAI · {amount}", "ok")
        else:
            self.cinai_chip.show_state("CinAI ✖", "err", data.get("error", ""))

    def _on_concat_done(self, name: str, status: str) -> None:
        self._refresh_busy()
        if status == "done":
            self.notify(tr(T("Склейка готова", "Render complete")), name)
        elif status == "failed":
            self.notify(tr(T("Склейка не удалась", "Render failed")), name)

    def _expand_nav(self) -> None:
        # в узком окне панель остаётся иконками: раскрытая, она легла бы поверх страницы
        if not self._rebuilding and self.width() >= NAV_EXPAND_WIDTH:
            self.navigationInterface.panel.expand(False)
        self._sync_brand()

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        super().resizeEvent(event)
        if getattr(self, "_top_hint", None) is not None:
            self._sync_title_gap()
        # окно снова стало широким (например, развернули) — панель, свёрнутую из-за узости,
        # раскрываем обратно; свёрнутую вручную в широком окне не трогаем
        was = event.oldSize().width()
        if getattr(self, "_nav_opened", False) and 0 < was < NAV_EXPAND_WIDTH <= self.width():
            QTimer.singleShot(0, self, self._expand_nav)

    def showEvent(self, event) -> None:  # noqa: N802 (Qt API)
        super().showEvent(event)
        if not self._nav_opened:
            # панель навигации открываем сразу с подписями, а не «иконками»
            self._nav_opened = True
            QTimer.singleShot(120, self, self._expand_nav)
            QTimer.singleShot(400, self, self._sync_title_gap)

    # ================================================================ выход
    def quit_for_update(self) -> None:
        """Выйти без вопросов: обновление ставится, когда процесс программы завершится."""
        self._updating = True
        self.close()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if self._rebuilding:
            event.accept()
            return
        busy = self.is_busy()
        renders = self.concat_queue.active_count()
        if (busy or renders) and not self._updating:
            text = tr(T("Идут задачи. Закрыть программу и остановить их?", "Tasks are running. Quit and stop them?"))
            if renders:
                text += "\n\n" + tr(T("Текущая склейка доработает в фоне; ожидающие в очереди будут пропущены.",
                                      "The current render finishes in the background; queued ones are skipped."))
            if QMessageBox.question(self, APP_NAME, text) != QMessageBox.Yes:
                event.ignore()
                return
        for page in ("settings", "prompts"):
            if not self._updating and page in self.pages and self.pages[page].has_unsaved():
                answer = QMessageBox.question(self, APP_NAME, tr(T("Есть несохранённые изменения. Выйти без сохранения?",
                                                                   "There are unsaved changes. Quit without saving?")))
                if answer != QMessageBox.Yes:
                    event.ignore()
                    return
                break
        try:
            # как «Стоп»: процессы убиваются, серверные задачи всех провайдеров снимаются
            pipeline_running = "pipeline" in self.pages and self.pages["pipeline"].runner.is_running()
            if pipeline_running:
                self.pages["pipeline"].runner.stop()          # снимает и серверные задачи
            if "control" in self.pages and self.pages["control"].tasks.stop_all(force=True) and not pipeline_running:
                from .pipeline.runner import start_cancel_all
                start_cancel_all()
        except Exception:  # noqa: BLE001
            pass
        if "pipeline" in self.pages:
            self.pages["pipeline"].shutdown()
        state.put(geometry=bytes(self.saveGeometry()).hex())
        self.concat_queue.shutdown()
        self.monitors.stop()
        self.tray.hide()
        event.accept()
        QApplication.instance().quit()
