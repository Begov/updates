"""Обновления в интерфейсе: проверка при запуске, окно «Доступно обновление», загрузка с прогрессом."""
from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication, QDialog, QHBoxLayout, QLabel, QMessageBox, QTextBrowser, QVBoxLayout
from qfluentwidgets import ProgressBar

from .. import APP_NAME, APP_VERSION, state
from ..i18n import T, tr
from ..ui.widgets import button
from . import updater

log = logging.getLogger(__name__)


def _mb(n: int) -> str:
    return f"{n / 1048576:.1f} МБ" if n else "?"


class UpdateDialog(QDialog):
    """Что нового + «Скачать и установить»; во время загрузки — прогресс и «Отмена»."""

    progress = Signal(int, int)
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, parent, rel: updater.Release, controller: "UpdateController"):
        super().__init__(parent)
        self.rel = rel
        self.controller = controller
        self.cancel = threading.Event()
        self.setWindowTitle(tr(T("Обновление BegoFrame", "BegoFrame update")))
        self.setMinimumSize(560, 420)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(10)
        head = QLabel(tr(T(f"Доступна версия {rel.version}", f"Version {rel.version} is available")))
        head.setObjectName("PageTitle")
        lay.addWidget(head)
        size = int(rel.asset.get("size") or 0)
        kind = (T("полный установщик (обновилась среда Python)", "full installer (Python runtime changed)")
                if rel.needs_installer else T("обновление кода", "code update"))
        sub = QLabel(tr(T(f"Установлена {APP_VERSION}", f"Installed {APP_VERSION}")) + "  ·  "
                     + tr(kind) + f"  ·  {_mb(size)}" + (f"  ·  {rel.date}" if rel.date else ""))
        sub.setProperty("role", "muted")
        lay.addWidget(sub)
        notes = QTextBrowser()
        notes.setOpenExternalLinks(True)
        notes.setMarkdown(rel.notes or tr(T("Описание изменений не указано.", "No release notes.")))
        lay.addWidget(notes, 1)
        keep = QLabel(tr(T("Проекты, настройки, ключи и изменённые вами промпты сохранятся. "
                           "После установки программа перезапустится.",
                           "Projects, settings, keys and prompts you edited are kept. "
                           "The program restarts after installing.")))
        keep.setWordWrap(True)
        keep.setProperty("role", "muted")
        lay.addWidget(keep)
        self.bar = ProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.hide()
        lay.addWidget(self.bar)
        self.status = QLabel("")
        self.status.setProperty("role", "muted")
        lay.addWidget(self.status)
        row = QHBoxLayout()
        self.skip_btn = button(T("Пропустить версию", "Skip this version"), "ghost",
                               T("Не напоминать об этой версии при запуске.", "Don't remind about this version on start."),
                               self._skip)
        self.later_btn = button(T("Позже", "Later"), "", None, self.reject)
        self.go_btn = button(T("Скачать и установить", "Download and install"), "primary", None, self._start)
        row.addWidget(self.skip_btn)
        row.addStretch(1)
        row.addWidget(self.later_btn)
        row.addWidget(self.go_btn)
        lay.addLayout(row)
        self.progress.connect(self._on_progress)
        self.finished_ok.connect(self._on_done)
        self.failed.connect(self._on_failed)

    def _skip(self) -> None:
        state.put(update_skip=self.rel.version)
        self.reject()

    def _start(self) -> None:
        why = self.controller.blocked_reason()
        if why:
            QMessageBox.information(self, APP_NAME, tr(why))
            return
        self.go_btn.setEnabled(False)
        self.skip_btn.setEnabled(False)
        self.later_btn.setText(tr(T("Отмена", "Cancel")))
        self.later_btn.clicked.disconnect()
        self.later_btn.clicked.connect(self.cancel.set)
        self.bar.show()
        self.status.setText(tr(T("Загрузка…", "Downloading…")))

        def work():
            try:
                path = updater.download(self.rel, lambda d, t: self.progress.emit(d, t), self.cancel)
                self.finished_ok.emit(path)
            except Exception as exc:  # noqa: BLE001
                log.warning("update download: %s", exc)
                self.failed.emit(str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _on_progress(self, done: int, total: int) -> None:
        if total:
            self.bar.setValue(int(done * 1000 / total))
        self.status.setText(f"{_mb(done)} / {_mb(total)}")

    def _on_done(self, path) -> None:
        self.bar.setValue(1000)
        why = self.controller.blocked_reason()      # пока качалось, могли запустить задачи
        if why:
            self._on_failed(tr(T("Скачано, но установка отложена: ", "Downloaded, install postponed: ")) + tr(why))
            self.go_btn.setText(tr(T("Установить", "Install")))
            return
        self.status.setText(tr(T("Установка… программа перезапустится.", "Installing… the program will restart.")))
        QApplication.processEvents()
        try:
            updater.launch_apply(self.rel, path)
        except Exception as exc:  # noqa: BLE001
            log.exception("update apply")
            self._on_failed(str(exc))
            return
        self.accept()
        self.controller.quit_for_update()

    def _on_failed(self, text: str) -> None:
        self.bar.hide()
        self.status.setText("✖ " + text)
        self.go_btn.setEnabled(True)
        self.go_btn.setText(tr(T("Повторить", "Retry")))
        self.later_btn.setText(tr(T("Закрыть", "Close")))
        try:
            self.later_btn.clicked.disconnect()
        except (RuntimeError, TypeError):
            pass
        self.later_btn.clicked.connect(self.reject)
        self.cancel.clear()


class UpdateController(QObject):
    """Один на программу: проверка при запуске и по кнопке в «Настройках»."""

    checked = Signal(object, str, bool)    # (Release | None, ошибка, вручную)
    status = Signal(str)                   # текст состояния для «Настроек»

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.latest: updater.Release | None = None
        self._busy = False
        self.checked.connect(self._on_checked)

    # ----------------------------------------------------------- проверка
    def auto_check(self) -> bool:
        return bool(state.get("update_autocheck", True))

    def set_auto_check(self, on: bool) -> None:
        state.put(update_autocheck=bool(on))

    def start(self) -> None:
        """Тихая проверка через несколько секунд после запуска."""
        try:
            updater.cleanup()
        except OSError:
            pass
        if self.auto_check():
            QTimer.singleShot(6000, self, lambda: self.check(manual=False))

    def check(self, manual: bool = True) -> None:
        if self._busy:
            return
        self._busy = True
        self.status.emit(tr(T("проверяю…", "checking…")))

        def work():
            try:
                self.checked.emit(updater.check(), "", manual)
            except Exception as exc:  # noqa: BLE001
                self.checked.emit(None, str(exc), manual)

        threading.Thread(target=work, daemon=True).start()

    def _on_checked(self, rel, error: str, manual: bool) -> None:
        self._busy = False
        self.latest = rel
        if error:
            self.status.emit("✖ " + error)
            log.info("update check: %s", error)
            if manual:
                QMessageBox.warning(self.window, APP_NAME, error)
            return
        if rel is None:
            self.status.emit(tr(T("установлена последняя версия", "you have the latest version")))
            if manual:
                QMessageBox.information(self.window, APP_NAME,
                                        tr(T(f"Установлена последняя версия ({APP_VERSION}).",
                                             f"You have the latest version ({APP_VERSION}).")))
            return
        self.status.emit(tr(T(f"доступна версия {rel.version}", f"version {rel.version} available")))
        if not manual and state.get("update_skip") == rel.version:
            return
        self.offer(rel)

    def offer(self, rel: updater.Release | None = None) -> None:
        rel = rel or self.latest
        if rel is None:
            return
        if not updater.is_installed():
            QMessageBox.information(
                self.window, APP_NAME,
                tr(T(f"Доступна версия {rel.version}.\n\nЭто рабочая копия (папка с git), а не установленная "
                     "программа, поэтому обновление здесь не ставится. Установите BegoFrame-Setup.exe или "
                     "обновите исходники через git.",
                     f"Version {rel.version} is available.\n\nThis is a working copy (git folder), not an "
                     "installed program, so updates are not applied here. Install BegoFrame-Setup.exe or "
                     "update the sources with git.")))
            return
        UpdateDialog(self.window, rel, self).exec()

    # ----------------------------------------------------------- установка
    def blocked_reason(self):
        if self.window.is_busy() or self.window.concat_queue.active_count():
            return T("Идут задачи (конвейер, стадии или склейка). Дождитесь окончания или остановите их, "
                     "затем установите обновление.",
                     "Tasks are running (pipeline, stages or render). Wait or stop them, then install the update.")
        for page in ("settings", "prompts"):
            if page in self.window.pages and self.window.pages[page].has_unsaved():
                return T("Есть несохранённые изменения в «Настройках» или «Промптах». Сохраните их.",
                         "There are unsaved changes in Settings or Prompts. Save them first.")
        return None

    def quit_for_update(self) -> None:
        """Закрыть программу без вопросов: установщик ждёт её выхода."""
        self.window.quit_for_update()


def setup(window) -> UpdateController:
    ctl = UpdateController(window)
    ctl.start()
    return ctl
