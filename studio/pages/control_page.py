"""Страница «Панель управления»: проекты, отдельные стадии, «Довести проект», очередь склейки, журнал."""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
    QMenu, QMessageBox, QProgressBar, QSplitter,
    QTableWidgetItem, QVBoxLayout, QWidget,
)
from qfluentwidgets import LineEdit
from ..ui.widgets import Table

from .. import engine_link, paths, state, theme
from ..pipeline import stages
from ..i18n import T, tr
from ..services import procs
from ..services.readiness import wait_ready
from ..ui.widgets import attach_hint, Card, Chip, LogView, button

PROJECT_COLS = ["#", T("Проект", "Project"), T("Аудио", "Audio"), T("Текст", "Transcript"), T("План", "Plan"),
                T("Картинки", "Images"), T("Видео", "Videos"), "MP4", T("Статус", "Status")]
STATUS_ORDER = {"done": 0, "concat": 1, "video": 2, "images": 3, "direction": 4, "transcribe": 5, "empty": 6}
STATUS_TEXT = {
    "done": T("Готов", "Complete"), "concat": T("К склейке", "Ready for concat"),
    "video": T("К видео", "Ready for video"), "images": T("К картинкам", "Ready for images"),
    "direction": T("К режиссуре", "Ready for direction"), "transcribe": T("К расшифровке", "Ready for transcription"),
    "empty": T("Пусто", "Empty"),
}
MEDIA_GLOBS = ("*.mp3", "*.wav", "*.flac", "*.m4a", "*.aac", "*.mp4", "*.mov", "*.mkv", "*.webm", "*.avi", "*.m4v")


def scan_project(p: Path) -> dict:
    has_audio = any(any(p.glob(g)) for g in MEDIA_GLOBS)
    has_tr = (p / "transcription.json").exists()
    has_plan = (p / "montage_plan.json").exists()
    gen, vid = p / "generated", p / "vid_img"
    imgs = len(list(gen.glob("*.png")) + list(gen.glob("*.jpg"))) if gen.exists() else 0
    vids = len(list(vid.glob("*.mp4"))) if vid.exists() else 0
    has_mp4 = (p / f"{p.name}.mp4").exists() or any(f.parent == p and f.stem == p.name for f in p.glob("*.mp4"))
    if has_mp4:
        status = "done"
    elif vids:
        status = "concat"
    elif imgs:
        status = "video"
    elif has_plan:
        status = "images"
    elif has_tr:
        status = "direction"
    elif has_audio:
        status = "transcribe"
    else:
        status = "empty"
    return dict(name=p.name, audio=has_audio, tr=has_tr, plan=has_plan, imgs=imgs, vids=vids, mp4=has_mp4,
                status=status, mtime=p.stat().st_mtime)


class TaskRunner(QObject):
    """Фоновые задачи Панели: процессы с потоковым журналом и учётом для «Стоп»."""

    log = Signal(str, str)
    changed = Signal(int)
    finished = Signal(str, bool)

    def __init__(self):
        super().__init__()
        self.active = 0
        self._lock = threading.Lock()
        self._procs: set = set()
        self.stopping = threading.Event()        # «Стоп»: прервать фоновые ожидания

    def _inc(self, delta: int) -> None:
        with self._lock:
            self.active = max(0, self.active + delta)
            count = self.active
        self.changed.emit(count)

    def run(self, args: list, name: str, cwd: Path | None = None, env: dict | None = None,
            project: Path | None = None, before=None, after=None) -> None:
        """Запустить процесс в фоне. before() выполняется в потоке до старта (например, ожидание готовности)."""
        self.stopping.clear()
        self._inc(1)
        self.log.emit(tr(T(f"Старт: {name}", f"Started: {name}")), "INFO")

        def work():
            started = time.time()
            handles = []
            ok = False
            try:
                if project:
                    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
                    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:50]
                    handles.append(open(project / f"stage_{safe}_{stamp}.log", "w", encoding="utf-8", buffering=1))
                    full = open(project / "pipeline_full.log", "a", encoding="utf-8", buffering=1)
                    full.write(f"\n{'=' * 60}\n=== {name} | {dt.datetime.now()} ===\n{'=' * 60}\n\n")
                    handles.append(full)
                if before:
                    before(lambda m, l="INFO": self.log.emit(m, l))
                run_env = dict(env or os.environ)
                run_env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1", "TQDM_DISABLE": "1"})

                def on_line(line: str) -> None:
                    self.log.emit(line, procs.classify(line))
                    for fh in handles:
                        try:
                            fh.write(line + "\n")
                        except Exception:  # noqa: BLE001
                            pass

                def on_start(proc):
                    with self._lock:
                        self._procs.add(proc)

                rc = procs.run_streamed(args, str(cwd or paths.ROOT), run_env, on_line, on_start=on_start)
                with self._lock:
                    self._procs = {p for p in self._procs if p.poll() is None}
                m, s = divmod(int(time.time() - started), 60)
                ok = rc == 0
                self.log.emit(tr(T(f"Готово: {name} ({m}м {s}с)", f"Done: {name} ({m}m {s}s)")) if ok
                              else tr(T(f"Ошибка: {name} (код {rc}, {m}м {s}с)", f"Failed: {name} (exit {rc}, {m}m {s}s)")),
                              "SUCCESS" if ok else "ERROR")
                if ok and after:
                    after()
            except Exception as exc:  # noqa: BLE001
                self.log.emit(f"{name}: {exc}", "ERROR")
            finally:
                for fh in handles:
                    try:
                        fh.close()
                    except Exception:  # noqa: BLE001
                        pass
                self._inc(-1)
                self.finished.emit(name, ok)

        threading.Thread(target=work, daemon=True, name=f"task-{name}").start()

    def stop_all(self, force: bool) -> int:
        with self._lock:
            items = list(self._procs)
        stopped = sum(1 for p in items if procs.kill_tree(p, force=force))
        return stopped


def stage_job(task: str, args: dict, config: dict | None = None, env: dict | None = None) -> list:
    """Команда запуска stage_runner.py с временным файлом задания."""
    job = {"task": task, "args": args, "config": config or {}, "env": env or {}}
    fd, path = tempfile.mkstemp(prefix=f"bego_{task}_", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(job, fh, ensure_ascii=False)
    return [paths.python_exe(), str(paths.STAGE_RUNNER), path]


class ControlPage(QWidget):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.tasks = TaskRunner()
        self.tasks.log.connect(self.log)
        self.tasks.changed.connect(self._on_active)
        self.tasks.finished.connect(lambda *_: QTimer.singleShot(300, self.refresh_projects))
        self._sort_col, self._sort_desc = 0, True
        self._build()
        app.concat_queue.changed.connect(self._render_queue)
        app.concat_queue.log.connect(self.log)
        QTimer.singleShot(200, self.refresh_projects)

    # ================================================================ сборка
    def _build(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        root.addWidget(split)

        # левая часть: проекты + стадии + очередь склейки
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 8, 0)
        ll.setSpacing(10)
        proj = Card(T("Проекты", "Projects"), "▤", key="cp_projects", collapsible=False)
        bar = QHBoxLayout()
        self.search = LineEdit()
        self.search.setPlaceholderText(tr(T("Поиск проекта…", "Search projects…")))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _t: self.refresh_projects())
        bar.addWidget(self.search, 1)
        bar.addWidget(button(T("Обновить", "Refresh"), "small", None, self.refresh_projects, icon="SYNC"))
        bar.addWidget(button(T("Удалить", "Delete"), "danger", T("Удалить выделенные проекты с диска", "Delete selected projects from disk"),
                             self.delete_projects))
        proj.add(bar)
        self.table = Table(0, len(PROJECT_COLS))
        self.table.setHorizontalHeaderLabels([tr(c) for c in PROJECT_COLS])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Interactive)   # название проекта: своя ширина, не сжимается
        hh.setStretchLastSection(True)
        self.table.setColumnWidth(1, 190)
        hh.sectionClicked.connect(self._sort)
        self.table.doubleClicked.connect(lambda _i: self.open_project())
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._project_menu)
        self.table.setMinimumHeight(220)
        attach_hint(self.table, T("Проектов пока нет.\nДобавьте файлы во вкладке «Конвейер» или создайте проект кнопкой «Расшифровка».",
                                  "No projects yet.\nAdd files on the Pipeline tab or create a project with “Transcribe”."))
        proj.add(self.table)
        ll.addWidget(proj, 3)

        stage_card = Card(T("Отдельные стадии", "Single stages"), "⚙", key="cp_stages", collapsible=False,
                      tip=T("Запуск одной стадии для выделенного проекта (или создание проекта из файла).\n"
                            "Окон нет: берутся последние настройки вкладки «Конвейер».",
                            "Run a single stage for the selected project (or create a project from a file).\n"
                            "No dialogs: the latest settings of the Pipeline tab are used."))
        grid = QGridLayout()
        grid.setSpacing(6)
        items = [
            (T("Расшифровка", "Transcribe"), self.run_transcribe,
             T("Создать проект из аудио/видео и расшифровать речь (AssemblyAI).",
               "Create a project from audio/video and transcribe speech (AssemblyAI).")),
            (T("Режиссура", "Scene director"), self.run_direct,
             T("Построить монтажный план и промпты для выделенного проекта.",
               "Build the montage plan and prompts for the selected project.")),
            (T("Картинки", "ImageGen"), self.run_imagegen, T("Сгенерировать кадры сцен (генератор — как в «Конвейере»).", "Generate scene images (generator as in the Pipeline).")),
            (T("Видео", "VideoGen"), self.run_videogen, T("Оживить сцены (провайдер и режим — как в «Конвейере»).", "Animate scenes (provider and mode as in the Pipeline).")),
            (T("Склейка", "Concat"), self.run_concat, T("Собрать финальный ролик.", "Assemble the final video.")),
        ]
        for i, (text, slot, tip) in enumerate(items):
            grid.addWidget(button(text, "", tip, slot), i // 3, i % 3)
        stage_card.add(grid)
        status = QHBoxLayout()
        status2 = QHBoxLayout()
        self.status = Chip(tr(T("Готово", "Ready")))
        status.addWidget(self.status)
        self.busy = QProgressBar()
        self.busy.setRange(0, 1)
        self.busy.setFixedHeight(6)
        status.addWidget(self.busy, 1)
        self.stop_btn = button(T("Стоп", "Stop"), "danger",
                               T("Прибить все процессы Панели и снять серверные задачи всех провайдеров: FastGen и\n"
                                 "VeoNonStop — на всех ключах, VARAG — все задачи ключа, RoyalTechno — запущенные\n"
                                 "программой. Работает и когда ничего не запущено (зависшие задачи на сервере).",
                                 "Kill all Panel processes and cancel server tasks of every provider: FastGen and\n"
                                 "VeoNonStop — on all keys, VARAG — all tasks of the key, RoyalTechno — those started\n"
                                 "by the app. Works even when nothing runs (stuck server tasks)."), self.stop_tasks)
        status2.addWidget(self.stop_btn)
        status2.addWidget(button(T("Перезапуск ключа", "Restart key"), "small",
                                T("Снять задачи VeoNonStop на сервере, не останавливая генерацию: воркеры повторят.",
                                  "Cancel VeoNonStop server tasks without stopping generation: workers retry."),
                                self.app.restart_key))
        status2.addWidget(button(T("Данные ключей", "Key data"), "small",
                                T("Обновить тариф и занятость слотов ключей.", "Refresh plan and slot usage of keys."),
                                lambda: (self.app.monitors.refresh_veo_keys(), self.app.monitors.refresh_veo())))
        status2.addStretch(1)
        stage_card.add(status)
        stage_card.add(status2)
        ll.addWidget(stage_card)

        queue = Card(T("Очередь склейки", "Concat queue"), "⧗", key="cp_queue", collapsible=False,
                     tip=T("Фоновые склейки готовых проектов. Идут строго по одной.",
                           "Background renders of finished projects. Strictly one at a time."))
        qbar = QHBoxLayout()
        self.queue_state = QLabel(tr(T("пусто", "empty")))
        self.queue_state.setProperty("role", "muted")
        qbar.addWidget(self.queue_state)
        qbar.addStretch(1)
        qbar.addWidget(button(T("Отменить текущую", "Cancel current"), "small", None, self._cancel_concat))
        qbar.addWidget(button(T("Очистить завершённые", "Clear finished"), "small", None,
                              self.app.concat_queue.clear_finished))
        queue.add(qbar)
        self.queue_table = Table(0, 4)
        self.queue_table.setHorizontalHeaderLabels(["", tr(T("Проект", "Project")), tr(T("Прогресс", "Progress")),
                                                    tr(T("Шаг", "Step"))])
        self.queue_table.verticalHeader().setVisible(False)
        self.queue_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.queue_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.queue_table.setColumnWidth(0, 28)
        self.queue_table.setColumnWidth(1, 200)
        self.queue_table.setColumnWidth(2, 120)
        self.queue_table.setFixedHeight(150)
        attach_hint(self.queue_table, T("Склеек пока нет — здесь появятся готовые к сборке проекты.",
                                       "No renders yet — projects ready to render will appear here."))
        queue.add(self.queue_table)
        ll.addWidget(queue)
        split.addWidget(left)

        # правая часть: журнал
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(8, 0, 0, 0)
        rl.setSpacing(10)
        rl.addWidget(self._build_finalize())
        log_card = QFrame()
        log_card.setObjectName("Card")
        lcl = QVBoxLayout(log_card)
        lcl.setContentsMargins(12, 10, 12, 12)
        self.logview = LogView(title=T("Журнал задач", "Task log"))
        lcl.addWidget(self.logview)
        rl.addWidget(log_card)
        split.addWidget(right)
        split.setSizes(state.get("control_split2", [540, 760]))
        split.splitterMoved.connect(lambda *_: state.put(control_split2=split.sizes()))

    def log(self, text: str, level: str = "INFO") -> None:
        self.logview.append(text, level)

    # =============================================================== проекты
    def _sort(self, col: int) -> None:
        if col == self._sort_col:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_col, self._sort_desc = col, col in (0, 5, 6)
        self.refresh_projects()

    def refresh_projects(self) -> None:
        selected = set(self.selected_names())
        paths.PROJECTS.mkdir(parents=True, exist_ok=True)
        query = self.search.text().strip().lower()
        infos = []
        for p in paths.PROJECTS.iterdir():
            if p.is_dir() and not p.name.startswith("_") and (not query or query in p.name.lower()):
                try:
                    infos.append(scan_project(p))
                except OSError:
                    continue
        key = {0: lambda i: i["mtime"], 1: lambda i: i["name"].lower(), 5: lambda i: i["imgs"],
               6: lambda i: i["vids"], 8: lambda i: STATUS_ORDER.get(i["status"], 9)}.get(self._sort_col,
                                                                                         lambda i: i["mtime"])
        infos.sort(key=key, reverse=self._sort_desc)
        self.table.setRowCount(len(infos))
        yes, no = "●", "·"
        for row, info in enumerate(infos):
            values = [str(row + 1), info["name"], yes if info["audio"] else no, yes if info["tr"] else no,
                      yes if info["plan"] else no, str(info["imgs"] or no), str(info["vids"] or no),
                      yes if info["mp4"] else no, tr(STATUS_TEXT[info["status"]])]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignCenter if col not in (1, 8) else Qt.AlignLeft | Qt.AlignVCenter)
                if col == 8:
                    item.setForeground(QColor(theme.c("green" if info["status"] == "done" else "dim")))
                self.table.setItem(row, col, item)
            if info["name"] in selected:
                self.table.selectRow(row)

    def selected_names(self) -> list[str]:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self.table.item(r, 1).text() for r in rows if self.table.item(r, 1)]

    def selected_project(self, warn: bool = True) -> Path | None:
        names = self.selected_names()
        if not names:
            if warn:
                QMessageBox.information(self, tr(T("Проект", "Project")),
                                        tr(T("Сначала выберите проект в таблице.", "Select a project in the table first.")))
            return None
        return paths.PROJECTS / names[0]

    def open_project(self) -> None:
        p = self.selected_project(False)
        if p and p.exists():
            procs.open_path(p)

    def delete_projects(self) -> None:
        names = self.selected_names()
        if not names:
            return
        text = "\n".join(f"• {n}" for n in names[:15])
        if QMessageBox.question(self, tr(T("Удалить проекты", "Delete projects")),
                                tr(T(f"Удалить с диска без возможности восстановления?\n\n{text}",
                                     f"Delete from disk permanently?\n\n{text}"))) != QMessageBox.Yes:
            return
        for name in names:
            try:
                shutil.rmtree(paths.PROJECTS / name)
            except OSError as exc:
                self.log(f"{name}: {exc}", "ERROR")
        self.refresh_projects()

    def _project_menu(self, pos) -> None:
        p = self.selected_project(False)
        if not p:
            return
        menu = QMenu(self)
        menu.addAction(tr(T("Открыть папку", "Open folder")), lambda: procs.open_path(p))
        video = p / f"{p.name}.mp4"
        if video.exists():
            menu.addAction(tr(T("Открыть готовое видео", "Open final video")), lambda: procs.open_path(video))
        log = p / "pipeline_full.log"
        if log.exists():
            menu.addAction(tr(T("Открыть журнал проекта", "Open project log")), lambda: procs.open_path(log))
        menu.addSeparator()
        menu.addAction(tr(T("Довести проект", "Finish the project")), self.finalize)
        menu.addSeparator()
        menu.addAction(tr(T("Удалить", "Delete")), self.delete_projects)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    # ============================================================ статус
    def _on_active(self, count: int) -> None:
        self.busy.setRange(0, 0 if count else 1)
        self.status.show_state(tr(T(f"Выполняется: {count}", f"Running: {count}")) if count else tr(T("Готово", "Ready")),
                               "warn" if count else "")
        self.app.set_busy("control", count > 0)

    def stop_tasks(self) -> None:
        stopped = self.tasks.stop_all(force=True)
        self.log(tr(T(f"СТОП: прибито процессов {stopped}; снимаю серверные задачи…",
                      f"STOP: killed processes {stopped}; cancelling server tasks…")), "WARNING")
        self.tasks.run(stage_job("cancel_all", {}), tr(T("Снятие задач", "Cancel tasks")))
        # флаг — после run(): run() его сбрасывает, а фоновые ожидания (готовность проекта
        # перед склейкой) должны его увидеть
        self.tasks.stopping.set()

    # ======================================================= очередь склейки
    def _render_queue(self) -> None:
        jobs = self.app.concat_queue.snapshot()[-8:]
        running = sum(1 for j in jobs if j["status"] == "running")
        pending = sum(1 for j in jobs if j["status"] == "pending")
        self.queue_state.setText(tr(T(f"в работе: {running} · ждут: {pending} · всего: {len(jobs)}",
                                      f"running: {running} · waiting: {pending} · total: {len(jobs)}"))
                                 if jobs else tr(T("пусто", "empty")))
        self.queue_table.setRowCount(len(jobs))
        icons = {"pending": "⏳", "running": "▶", "done": "✅", "failed": "❌", "cancelled": "⛔"}
        for row, job in enumerate(jobs):
            self.queue_table.setItem(row, 0, QTableWidgetItem(icons.get(job["status"], "·")))
            self.queue_table.setItem(row, 1, QTableWidgetItem(job["name"]))
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(int(job.get("pct") or 0))
            bar.setFixedHeight(8)
            self.queue_table.setCellWidget(row, 2, bar)
            step = job.get("step") or ""
            if job["status"] == "failed" and job.get("error"):
                step += f": {job['error']}"
            self.queue_table.setItem(row, 3, QTableWidgetItem(step))

    def _cancel_concat(self) -> None:
        if not self.app.concat_queue.cancel_running():
            QMessageBox.information(self, tr(T("Очередь склейки", "Concat queue")),
                                    tr(T("Сейчас ничего не склеивается.", "Nothing is rendering now.")))

    # ------------------------------------------------ отдельные стадии
    # Диалогов нет: каждая кнопка берёт ПОСЛЕДНИЕ настройки формы «Конвейер»
    # (`pipeline/stages.py`), так что стадия работает так же, как внутри конвейера.
    def _pipe_values(self) -> dict:
        return stages.values_of(self.app)

    def _stage_cmd(self, task: str, args: dict, conf: dict, v: dict) -> list:
        return stage_job(task, args, config=conf, env=stages.job_env(v))

    def _stage_off(self, what: str) -> None:
        QMessageBox.information(self, "BegoFrame", tr(T(
            f"В «Конвейере» {what}. Измените настройку там и запустите стадию снова.",
            f"In the Pipeline {what}. Change it there and run the stage again.")))

    def run_transcribe(self) -> None:
        audio, _ = QFileDialog.getOpenFileName(
            self, tr(T("Аудио или видео", "Audio or video")), "",
            "Audio/Video (*.mp3 *.wav *.flac *.m4a *.aac *.mp4 *.mov *.mkv *.webm *.avi *.m4v);;All (*.*)")
        if not audio:
            return
        v = self._pipe_values()
        src = Path(audio)
        project = paths.PROJECTS / src.stem
        project.mkdir(parents=True, exist_ok=True)
        dst = project / src.name
        if not dst.exists():
            shutil.copy2(src, dst)
        args, conf = stages.transcribe_job(v, dst, project / "transcription.json")
        self.tasks.run(self._stage_cmd("transcribe", args, conf, v), f"Transcribe {project.name}",
                       cwd=paths.ENGINE, project=project)
        QTimer.singleShot(300, self.refresh_projects)

    def run_direct(self) -> None:
        p = self.selected_project()
        if not p:
            return
        if not (p / "transcription.json").exists():
            QMessageBox.warning(self, "Direction", tr(T("Нет transcription.json.", "No transcription.json.")))
            return
        v = self._pipe_values()
        job = stages.direct_job(v, p)
        if job is None:
            QMessageBox.warning(self, "Direction", tr(T(
                "Нет стайлгайда: выберите его в «Конвейере» (один на все) или запустите проект через конвейер.",
                "No style guide: pick one in the Pipeline (one for all) or start the project through the pipeline.")))
            return
        args, conf = job
        if engine_link.unit("media_io").has_media(p):
            answer = QMessageBox.question(self, "Direction", tr(T(
                "В проекте уже есть картинки, видео или ролик прошлого плана. Новый план с ними не совпадёт.\n\n"
                "Перенести их в папку проекта old_<дата>/ и режиссировать заново? Ничего не удаляется.",
                "The project already has images, videos or a video of the previous plan. A new plan won't match them."
                "\n\nMove them to the project's old_<date>/ folder and direct again? Nothing is deleted.")))
            if answer != QMessageBox.Yes:
                return
            args["archive_old"] = True
        self.tasks.run(self._stage_cmd("direct", args, conf, v), f"Direct {p.name}", cwd=paths.ENGINE, project=p)

    def run_imagegen(self) -> None:
        p = self.selected_project()
        if not p:
            return
        v = self._pipe_values()
        job = stages.imagegen_job(v, p)
        if job is None:
            self._stage_off(tr(T("для картинок выбрано «Пропустить» и инфографика выключена",
                                 "images are set to “Skip” and infographics are off")))
            return
        args, conf = job
        self.tasks.run(self._stage_cmd("imagegen", args, conf, v), f"ImageGen {p.name}", cwd=paths.ENGINE, project=p)

    def run_videogen(self) -> None:
        p = self.selected_project()
        if not p:
            return
        v = self._pipe_values()
        job = stages.videogen_job(v, p)
        if job is None:
            self._stage_off(tr(T("видео выключено", "video generation is off")))
            return
        args, conf = job
        env = dict(stages.job_env(v))
        self.tasks.run(stage_job("videogen", args, config=conf, env=env), f"VideoGen {p.name}", cwd=paths.ENGINE,
                       project=p)

    def run_concat(self) -> None:
        p = self.selected_project()
        if not p:
            return
        if not (p / "montage_plan.json").exists():
            QMessageBox.warning(self, "Concat", tr(T("Нет montage_plan.json.", "No montage_plan.json.")))
            return
        v = self._pipe_values()
        cmd = stages.concat_cmd(v, p)

        def guard(log):
            # обрезанные прошлыми запусками файлы — в *.broken: склейка сообщит, каких клипов нет
            engine_link.unit("media_io").quarantine_broken(p, log)
            wait_ready(p, log, cancel=self.tasks.stopping.is_set, terminal=True)

        self.tasks.run(cmd, f"Concat {p.name}", cwd=p, env=stages.process_env(v), project=p, before=guard)

    # ====================================================== доводка проекта
    def _build_finalize(self) -> QWidget:
        card = Card(T("Довести проект", "Finish the project"), "⏩", key="cp_finalize", collapsible=False)
        note = QLabel(tr(T(
            "Продолжает выделенный проект с того места, где он остановился, по последним настройкам «Конвейера» "
            "и тем же маршрутом: режиссура (если плана нет), источники кадра (сток, инфографика, моушн-графика, "
            "обложка), картинки, видео, склейка. Готовое не пересоздаётся.",
            "Continues the selected project from where it stopped, with the latest Pipeline settings and the same "
            "route: direction (if there is no plan), frame sources (stock, infographics, motion graphics, cover), "
            "images, video, assembly. Finished work is not regenerated.")))
        note.setWordWrap(True)
        note.setProperty("role", "muted")
        card.add(note)
        card.add(button(T("Довести проект  →", "Finish the project  →"), "primary", None, self.finalize))
        return card

    def finalize(self) -> None:
        """«Довести проект»: продолжить с того места, где остановился, по последним настройкам «Конвейера»."""
        p = self.selected_project()
        if not p:
            return
        title = tr(T("Довести проект", "Finish the project"))
        v = self._pipe_values()
        plan = stages.resume_plan(p, v)
        if plan.blocked:
            QMessageBox.warning(self, title, plan.notes[0])
            return
        for note in plan.notes:
            self.log(f"{p.name}: {note}", "INFO")
        if not plan.stages:
            QMessageBox.information(self, title, plan.notes[0])
            return
        self._start_finalize(p, stages.finish_settings(v, p, plan.stages), v)

    def _start_finalize(self, project: Path, settings: dict, v: dict) -> None:
        fd, path = tempfile.mkstemp(prefix="bego_finish_", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(settings, fh, ensure_ascii=False)
        self.log(tr(T(f"Доводка {project.name}: {', '.join(settings['stages'])}",
                      f"Finishing {project.name}: {', '.join(settings['stages'])}")), "INFO")
        self.tasks.run([paths.python_exe(), str(paths.FINISH_RUNNER), path], f"Finalize {project.name}",
                       cwd=paths.ENGINE, env=stages.process_env(v), project=project)
