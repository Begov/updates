"""Запуск интерфейса BegoFrame."""
from __future__ import annotations

import logging
import os
import sys
import traceback


def _setup_logging() -> None:
    from . import paths
    paths.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(paths.LOGS_DIR / "studio.log"), level=logging.INFO, encoding="utf-8",
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _excepthook(exc_type, exc, tb) -> None:
    """Непойманные ошибки — в журнал и в окно, а не молчаливое падение."""
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    logging.error("Unhandled exception:\n%s", text)
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        if QApplication.instance():
            box = QMessageBox(QMessageBox.Critical, "BegoFrame", str(exc) or exc_type.__name__)
            box.setDetailedText(text)
            box.exec()
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from . import APP_NAME, envfile, paths, theme

    paths.ensure_dirs()
    _setup_logging()
    sys.excepthook = _excepthook
    envfile.ensure_env_file()
    try:
        # после обновления: новые заводские промпты — в те файлы, которые пользователь не менял
        from .services import prompt_sync
        prompt_sync.sync()
    except Exception:  # noqa: BLE001
        logging.exception("prompt sync failed")

    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_NAME)
    app.setQuitOnLastWindowClosed(True)
    theme.apply(app)

    if sys.platform == "win32":
        # собственная иконка на панели задач, а не иконка python.exe
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("BegoFrame.Studio")
        except Exception:  # noqa: BLE001
            pass

    from .main_window import MainWindow
    from .services.concat_queue import ConcatQueue
    from .services.monitors import Monitors

    monitors = Monitors()
    queue = ConcatQueue()
    window = MainWindow(monitors, queue)
    app.setProperty('main_window', window)  # держим ссылку, пока окно живо
    window.showMaximized()                   # программа открывается на весь экран
    monitors.start()

    # Список моделей Codex обновляется у самого Codex в фоне; изменения подхватываются на лету.
    from PySide6.QtCore import QObject, Signal, Slot

    from . import catalog, codex_models

    class _ModelsBridge(QObject):
        changed = Signal(object)

        @Slot(object)
        def apply(self, models) -> None:
            catalog.set_codex_models(models)
            window.page("pipeline").builder.refresh()

    bridge = _ModelsBridge()
    bridge.changed.connect(bridge.apply)
    app.setProperty("codex_models_bridge", bridge)
    codex_models.refresh(on_change=bridge.changed.emit)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
