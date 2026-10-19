"""Страница «Конвейер»: настройки, очередь файлов, запуск и журнал."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QMenu, QSizePolicy,
    QMessageBox, QProgressBar, QScrollArea, QSplitter, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)
from qfluentwidgets import FluentIcon, SmoothScrollArea, ToolButton, TransparentToolButton
from ..ui.widgets import Table, Toggle, attach_hint, card_icon

from .. import catalog, engine_link, envfile, paths, state, theme
from ..i18n import T, tr
from ..pipeline import runplan
from ..pipeline.defaults import (
    CHEAPVIBE_QUALITY_PAIRS, defaults, normalize_links, source_mix, sync_links, upgrade_values,
)
from ..pipeline.runner import PipelineRunner
from ..pipeline.runplan import FileBinding
from ..pipeline.sections import sections
from ..services import presets as preset_store
from ..services import procs
from ..ui.form import FormBuilder, Store
from ..ui.widgets import Card, Chip, Column, FlowBox, LogView, Segmented, button, label, repolish, set_tip
from . import pipeline_parts as parts

AUDIO_EXT = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
SESSION_FILE = paths.USERDATA / "session.json"
STAGES = [T("Очередь", "Queue"), T("Транскрипция", "Transcribe"), T("Режиссура", "Direction"),
          T("Картинки", "Images"), T("Видео", "Video"), T("Склейка", "Concat")]

COL_STAR, COL_FILE, COL_STYLE, COL_PRESET, COL_LOGO, COL_COVER, COL_STATUS = range(7)
SUMMARY_TWO_COLUMNS = 560   # px: с этой ширины сводка «Запуска» идёт в две колонки


def _show_text(label: QLabel, text: str) -> None:
    """Подпись видна, только когда ей есть что сказать: пустые строки не занимают место."""
    label.setText(text)
    label.setVisible(bool(text))


class QueueItem:
    def __init__(self, path: Path):
        self.path = path
        self.binding = FileBinding()
        self.snapshot: dict | None = None
        self.status = ""


class QueueTable(Table):
    """Таблица очереди, принимающая файлы перетаскиванием."""

    filesDropped = Signal(list)

    def __init__(self):
        super().__init__(0, 7)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DropOnly)

    def dragEnterEvent(self, event):  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):  # noqa: N802
        if event.mimeData().hasUrls():
            files = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]
            self.filesDropped.emit(files)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)


class PipelinePage(QWidget):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.monitors = app.monitors
        self.key_limits: dict = {}
        self.key_versions: dict = {}
        self.queue: list[QueueItem] = []
        self._rules_busy = False
        # «Пропустить» у провайдера видео: что вернуть, когда видео снова включат
        self._video_mode_before_skip = "i2v"
        self._video_provider_before_skip = "veononstop"
        # провайдер картинок до «Пропустить» (t2v): его вернут слайдшоу и вислайд, им нужны картинки
        self._img_before_skip = "veononstop"
        self.preset_combo_refill = None

        values = defaults()
        values.update(self._load_session(values))
        normalize_links(values)
        # Связи параметров (defaults.sync_links): значения до последнего изменения и память
        # о том, что было у выключенных зависимых. Пока форма заполняется пачкой (пресет,
        # сброс, пересборка окна), связи не применяются — значения согласуются в конце.
        self._link_prev = dict(values)
        self._link_memory: dict = {}
        self._links_paused = False
        self.store = Store(values)
        self.builder = FormBuilder(self.store, self)
        self.runner = PipelineRunner(self._take_next, app.concat_queue)
        self._build()
        self.store.changed.connect(self._on_change)
        # Изменения формы во время прогона уходят текущему файлу — через паузу после последнего
        # щелчка, чтобы серия щелчков (потоки +1 +1 +1) дала один перезапуск, а не пачку
        self._live_timer = QTimer(self)
        self._live_timer.setSingleShot(True)
        self._live_timer.setInterval(2000)
        self._live_timer.timeout.connect(lambda: self.runner.update_settings(self.store.snapshot()))
        self.store.changed.connect(lambda *_: self.runner.is_running() and self._live_timer.start())
        self._on_change("", None)          # состояние кнопок по загруженным значениям, до первого изменения
        self.builder.refresh()
        self._refresh_summary()
        self._wire_runner()

    # ================================================================ сборка
    def _build(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        root.addWidget(splitter)

        # ---------- левая колонка: навигация + настройки
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(18, 14, 10, 14)
        ll.setSpacing(8)
        nav = QHBoxLayout()
        nav.setSpacing(4)
        nav_icons = FlowBox(spacing=2)
        self._nav_buttons = []
        self.cards: dict[str, Card] = {}
        scroll = SmoothScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setObjectName("Transparent")
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)   # поля переносятся, а не уезжают вбок
        holder = QWidget()
        holder.setObjectName("Transparent")
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(0, 0, 18, 0)
        hl.setSpacing(12)
        for sec in sections(self):
            card = Card(sec["title"], sec.get("icon", ""), key=f"pipe_{sec['key']}", tip=sec.get("tip"))
            self.builder.build(sec["items"], card.body_layout)
            hl.addWidget(card)
            self.cards[sec["key"]] = card
            nav_btn = TransparentToolButton(card_icon(sec["key"]))
            set_tip(nav_btn, sec["title"])
            nav_btn.clicked.connect(lambda _c=False, c=card: self._jump(scroll, c))
            nav_icons.add(nav_btn)
            if sec.get("visible") is not None:
                self.builder.show_when(card, sec["visible"])
                self.builder.show_when(nav_btn, sec["visible"])
        hl.addStretch(1)
        scroll.setWidget(holder)
        nav.addWidget(nav_icons, 1, Qt.AlignTop)
        collapse = button(T("Свернуть все", "Collapse all"), "ghost", None,
                          lambda: [c.set_collapsed(True) for c in self.cards.values()])
        expand = button(T("Развернуть все", "Expand all"), "ghost", None,
                        lambda: [c.set_collapsed(False) for c in self.cards.values()])
        nav.addWidget(collapse, 0, Qt.AlignTop)
        nav.addWidget(expand, 0, Qt.AlignTop)
        ll.addLayout(nav)
        ll.addWidget(scroll, 1)
        left.setMinimumWidth(490)
        splitter.addWidget(left)

        # ---------- правая колонка: очередь, сводка, запуск, журнал
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(10, 14, 18, 14)
        rl.setSpacing(12)
        rsplit = QSplitter(Qt.Vertical)
        rsplit.setChildrenCollapsible(False)
        top = Column()
        tl = QVBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(10)
        tl.addWidget(self._build_queue_card())
        tl.addWidget(self._build_progress_card())
        rsplit.addWidget(top)
        log_card = QFrame()
        log_card.setObjectName("Card")
        lcl = QVBoxLayout(log_card)
        lcl.setContentsMargins(12, 10, 12, 12)
        self.logview = LogView(title=T("Журнал конвейера", "Pipeline log"))
        lcl.addWidget(self.logview)
        log_card.setMinimumHeight(240)
        rsplit.addWidget(log_card)
        rsplit.setSizes([520, 380])
        rscroll = SmoothScrollArea()
        rscroll.setWidgetResizable(True)
        rscroll.setObjectName("Transparent")
        rscroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        rsplit.setObjectName("Transparent")
        rscroll.setWidget(rsplit)
        rl.addWidget(rscroll)
        right.setMinimumWidth(440)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes(state.get("pipeline_split2", [760, 560]))
        splitter.splitterMoved.connect(lambda *_: state.put(pipeline_split2=splitter.sizes()))

        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self.start)

    def _jump(self, scroll: SmoothScrollArea, card: Card) -> None:
        card.set_collapsed(False)
        scroll.ensureWidgetVisible(card, 0, 0)
        QTimer.singleShot(0, lambda: scroll.verticalScrollBar().setValue(card.y()))

    def _build_queue_card(self) -> QWidget:
        card = Card(T("Очередь файлов", "File queue"), "☰", key="pipe_queue", collapsible=False,
                    tip=T("Исходники: аудио или текстовые сценарии. Обрабатываются по порядку, каждый — отдельным\n"
                          "прогоном со своими настройками. Файлы можно перетаскивать сюда мышью и добавлять во\n"
                          "время работы. Двойной щелчок по ячейке — назначить стайлгайд, пресет, логотип или\n"
                          "обложку этому файлу.",
                          "Sources: audio or text scripts. Processed in order, each as a separate run with its own\n"
                          "settings. Drag files here, you can add more while running. Double-click a cell to assign\n"
                          "a style guide, preset, logo or cover to that file."))
        bar = FlowBox(spacing=6)
        bar.add(button(T("Добавить…", "Add…"), "primary", T("Добавить аудио или видео в очередь.",
                                                            "Add audio or video to the queue."),
                       self.add_files, icon="ADD"))
        bar.add(button(T("Убрать", "Remove"), "small", T("Убрать выделенные файлы из очереди.",
                                                         "Remove the selected files from the queue."),
                       self.remove_selected, icon="REMOVE"))
        bar.add(button(T("Очистить", "Clear"), "small", T("Убрать из очереди все файлы.", "Remove all files from the queue."),
                       self.clear_queue, icon="DELETE"))
        up = ToolButton(FluentIcon.UP)
        set_tip(up, T("Поднять выделенный файл в очереди", "Move the selected file up"))
        up.clicked.connect(lambda: self.move_selected(-1))
        down = ToolButton(FluentIcon.DOWN)
        set_tip(down, T("Опустить выделенный файл в очереди", "Move the selected file down"))
        down.clicked.connect(lambda: self.move_selected(1))
        bar.add(up)
        bar.add(down)
        star = button(T("★ Запомнить настройки", "★ Pin settings"), "small",
                      T("Запомнить ВСЕ текущие настройки для выделенного файла: он будет обработан именно с ними,\n"
                        "даже если потом вы поменяете форму для следующих файлов.",
                        "Pin ALL current settings to the selected file: it will run with exactly these settings,\n"
                        "even if you change the form for the next files."), self.pin_selected)
        bar.add(star)
        self.queue_count = QLabel("")
        self.queue_count.setProperty("role", "muted")
        card.head_extra.addWidget(self.queue_count)
        card.add(bar)

        self.table = QueueTable()
        attach_hint(self.table, T("Очередь пуста.\nПеретащите сюда аудио или видео либо нажмите «Добавить…».",
                                  "The queue is empty.\nDrop audio or video here or press “Add…”."))
        headers = ["★", T("Файл", "File"), T("Стайлгайд", "Style guide"), T("Пресет", "Preset"),
                   T("Логотип", "Logo"), T("Обложка", "Cover"), T("Статус", "Status")]
        self.table.setHorizontalHeaderLabels([tr(h) for h in headers])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        hh.setSectionResizeMode(COL_FILE, QHeaderView.Stretch)
        for col, width in ((COL_STAR, 30), (COL_STYLE, 120), (COL_PRESET, 110),
                           (COL_LOGO, 90), (COL_COVER, 90), (COL_STATUS, 70)):
            self.table.setColumnWidth(col, width)
        self.table.setMinimumHeight(150)
        self.table.cellDoubleClicked.connect(self._cell_action)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.filesDropped.connect(self.add_paths)
        card.add(self.table)

        # стайлгайд
        card.add(label(T("Стайлгайд", "Style guide"), "label",
                           T("Документ с описанием визуального стиля и персонажей — без него режиссура не запустится.\n"
                             "• Один на все — общий для всей очереди.\n"
                             "• Для каждого — свой у каждого файла (столбец «Стайлгайд»).",
                             "A document describing the visual style and characters — direction won't start without it.\n"
                             "• One for all — shared by the whole queue.\n"
                             "• Per file — own for each file ('Style guide' column).")))
        # переключатель и поле файла; в узкой колонке поле уходит на свою строку во всю ширину
        sg = FlowBox(spacing=8, vspacing=8)
        self.sg_mode = Segmented([("one_for_all", T("один на все", "one for all")),
                                  ("individual", T("для каждого", "per file"))], self.store.get("sg_mode"))
        self.sg_mode.valueChanged.connect(lambda v: self.store.set("sg_mode", v))
        sg.add(self.sg_mode)
        from ..ui.widgets import PathPicker
        self.sg_path = PathPicker("file", "Text (*.txt *.md);;All (*.*)", T("файл стайлгайда", "style guide file"))
        self.sg_path.set_path(self.store.get("sg_global_path", ""))
        self.sg_path.pathChanged.connect(lambda p: self.store.set("sg_global_path", p))
        self.sg_path.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.sg_path.setMinimumWidth(260)
        sg.add(self.sg_path)
        card.add(sg)
        sg2 = FlowBox(spacing=12)
        self.sg_adapt = Toggle(tr(T("Адаптировать стайлгайд под сюжет (GPT)", "Adapt style guide to the story (GPT)")))
        set_tip(self.sg_adapt, T("Подогнать общий стайлгайд под сюжет конкретного ролика через GPT: подставить нужных\n"
                                 "персонажей, места, детали. Можно переопределить для файла (меню по правой кнопке).",
                                 "Tailor the master style guide to each video's story with GPT: characters, places,\n"
                                 "details. Can be overridden per file (right-click menu)."))
        self.sg_adapt.setChecked(bool(self.store.get("sg_adapt")))
        self.sg_adapt.toggled.connect(lambda c: self.store.set("sg_adapt", bool(c)))
        sg2.add(self.sg_adapt)
        sg2.add(button(T("Открыть адаптированный", "Open adapted"), "small",
                       T("Открыть последний адаптированный стайлгайд.", "Open the latest adapted style guide."),
                       self._open_adapted))
        card.add(sg2)
        self._refresh_table()
        return card

    def _build_progress_card(self) -> QWidget:
        card = Card(T("Запуск", "Run"), "⚡", key="pipe_run", collapsible=False)
        self.summary = QLabel("")
        self.summary.setProperty("role", "dim")
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.RichText)
        self.summary.installEventFilter(self)
        card.add(self.summary)
        steps = FlowBox(spacing=3, vspacing=6)
        self.stage_labels = []
        for i, name in enumerate(STAGES):
            chip = Chip(tr(name))
            chip.setProperty("compact", True)
            self.stage_labels.append(chip)
            if not i:
                steps.add(chip)
                continue
            # стрелка едет вместе со следующим этапом: при переносе она не повисает в конце строки
            pair = QWidget()
            pl = QHBoxLayout(pair)
            pl.setContentsMargins(0, 0, 0, 0)
            pl.setSpacing(3)
            arrow = QLabel("›")
            arrow.setProperty("role", "muted")
            arrow.setFixedWidth(8)
            arrow.setAlignment(Qt.AlignCenter)
            pl.addWidget(arrow)
            pl.addWidget(chip)
            steps.add(pair)
        card.add(steps)
        self.current_file = QLabel("")
        self.current_file.setProperty("role", "dim")
        self.current_file.setWordWrap(True)
        self.current_file.hide()
        card.add(self.current_file)
        prog = QHBoxLayout()
        self.task_bar = QProgressBar()
        self.task_bar.setRange(0, 100)
        self.task_bar.setValue(0)
        self.task_label = QLabel("")
        self.task_label.setProperty("role", "muted")
        self.task_label.setFixedWidth(90)
        prog.addWidget(self.task_bar, 1)
        prog.addWidget(self.task_label)
        card.add(prog)
        self.spinner = QLabel("")
        self.spinner.setProperty("role", "muted")
        self.spinner.hide()
        card.add(self.spinner)

        btns = QHBoxLayout()
        btns.setSpacing(8)
        self.start_btn = button(T("Запустить конвейер", "Start pipeline"), "primary",
                                T("Запустить обработку всей очереди с текущими настройками (Ctrl+Enter).",
                                  "Process the whole queue with the current settings (Ctrl+Enter)."), self.start,
                                icon="PLAY")
        self.stop_btn = button(T("Стоп", "Stop"), "danger",
                               T("Прибить процессы конвейера и снять серверные задачи всех провайдеров.",
                                 "Kill the pipeline processes and cancel server tasks of every provider."),
                               self.runner_stop, icon="CLOSE")
        self.stop_btn.setEnabled(False)
        btns.addWidget(self.start_btn)
        btns.addWidget(self.stop_btn)
        btns.addStretch(1)
        more = button(T("Ещё", "More"), "small", T("Другие действия", "More actions"), None, icon="MORE")
        menu = QMenu(more)
        menu.addAction(tr(T("Сохранить как значения по умолчанию", "Save as defaults")), self.save_defaults)
        menu.addAction(tr(T("Перезапустить задачи ключа VeoNonStop", "Restart VeoNonStop key tasks")), self.app.restart_key)
        menu.addAction(tr(T("Обновить данные ключей", "Refresh key data")), self._refresh_keys)
        menu.addSeparator()
        menu.addAction(tr(T("Открыть папку проектов", "Open projects folder")), lambda: procs.open_path(paths.PROJECTS))
        # Меню открываем сами, а не через setMenu: встроенная стрелка кнопки
        # дублировала «▾» из подписи, а список выравнивался по левому краю
        # кнопки и уезжал за правый край окна. Здесь он прижат к правому краю.
        def show_menu():
            width = menu.sizeHint().width()
            menu.exec(more.mapToGlobal(QPoint(more.width() - width, more.height() + 4)))

        more.clicked.connect(show_menu)
        btns.addWidget(more)
        card.add(btns)
        return card

    # ======================================== элементы, которые строят разделы
    def build_preset_bar(self, _b):
        return parts.preset_bar(self)

    def build_quota_panel(self, _b):
        return parts.quota_panel(self)

    def build_director_badge(self, _b):
        return parts.director_badge(self)

    def build_img_keys(self, _b):
        return parts.img_keys(self)

    def build_video_keys(self, _b):
        return parts.video_keys(self)

    def build_source_mix(self, _b):
        return parts.source_mix(self)

    def build_remotion_status(self, _b):
        return parts.remotion_status(self)

    # ================================================================ журнал
    def log(self, text: str, level: str = "INFO") -> None:
        self.logview.append(text, level)

    # ======================================================== правила связей
    def _on_change(self, key: str, value) -> None:
        if self._rules_busy:
            return
        self._rules_busy = True
        try:
            self._apply_rules(key, value)
            if not self._links_paused:
                for k, v in sync_links(self._link_prev, self.store.values, self._link_memory).items():
                    self.store.set(k, v)
                self._link_prev = dict(self.store.values)
            self._refresh_buttons()
        finally:
            self._rules_busy = False
        self._refresh_summary()
        if key in ("sg_mode", "logo_mode", "watermark", "img_gen", "video_mode"):
            self._refresh_table()
        if key == "sg_mode":
            self.sg_mode.set_value(value)
            self.sg_path.setVisible(value == "one_for_all")
        if key == "sg_global_path" and self.sg_path.path() != (value or ""):
            self.sg_path.set_path(value or "")
        if key == "sg_adapt":
            self.sg_adapt.setChecked(bool(value))

    def _apply_rules(self, key: str, value) -> None:
        s = self.store
        if key == "video_provider":
            # «Пропустить» стоит среди провайдеров, а движку уходит режим видео `skip`
            if value == "skip":
                if s.get("video_mode") != "skip":
                    self._video_mode_before_skip = s.get("video_mode") or "i2v"
                    s.set("video_mode", "skip")
            else:
                self._video_provider_before_skip = value
                if s.get("video_mode") == "skip":
                    mode = self._video_mode_before_skip or "i2v"
                    if mode == "i2v" and s.get("img_gen") == "skip":
                        mode = "t2v"           # без картинок оживлять нечего
                    s.set("video_mode", mode)
        elif key == "video_mode":
            if value == "skip" and s.get("video_provider") != "skip":
                s.set("video_provider", "skip")
            elif value != "skip" and s.get("video_provider") == "skip":
                s.set("video_provider", self._video_provider_before_skip or "veononstop")
            if value == "t2v" and s.get("img_gen") != "skip":
                self._img_before_skip = s.get("img_gen")
                s.set("img_gen", "skip")
                self.log(tr(T("Видео t2v: картинки не нужны — генерация картинок переключена в «Пропустить».",
                              "Video t2v: images are not needed — image generation switched to Skip.")), "INFO")
        elif key == "img_gen" and value == "skip":
            if self._link_prev.get("img_gen") not in (None, "skip"):
                self._img_before_skip = self._link_prev["img_gen"]
            if s.get("video_mode") == "i2v":
                s.set("video_mode", "t2v")
        elif key == "clip_mode" and value in ("slideshow", "vislide"):
            if value == "vislide" and s.get("video_mode") == "t2v":
                s.set("video_mode", "i2v")
                self.log(tr(T("Вислайд несовместим с t2v (статичным клипам нужны картинки) — видео переключено на i2v.",
                              "Vislide is incompatible with t2v (still clips need images) — video switched to i2v.")),
                         "WARNING")
            if s.get("img_gen") == "skip":
                # слайдам нужны картинки: возвращаем провайдера, который был до «Пропустить»
                s.set("img_gen", self._img_before_skip or "veononstop")
                self.log(tr(T("Слайдам нужны картинки — генерация картинок снова включена.",
                              "Slides need images — image generation is back on.")), "INFO")
        elif key in dict(CHEAPVIBE_QUALITY_PAIRS):
            # у Banana 2 есть только low: недоступное модели качество сводим к доступному
            quality_key = dict(CHEAPVIBE_QUALITY_PAIRS)[key]
            s.set(quality_key, catalog.cheapvibe_quality(value, s.get(quality_key)))
        elif key == "video_upscale" and value:
            s.set("render_mode", "720p")
        elif key == "render_mode" and value in ("1080p", "4k") and s.get("video_upscale"):
            s.set("render_mode", "720p")

    def _refresh_buttons(self) -> None:
        """Доступность отдельных кнопок переключателей — по итоговым значениям (после правил и связей)."""
        s = self.store
        # Кнопки 1080p/4K недоступны при апскейле видео
        seg = self.builder.widget("render_mode")
        if isinstance(seg, Segmented):
            for mode in ("1080p", "4k"):
                seg.set_button_enabled(mode, not s.get("video_upscale"))
        vm = self.builder.widget("video_mode")
        if isinstance(vm, Segmented):
            vm.set_button_enabled("i2v", s.get("img_gen") != "skip")
            vm.set_button_enabled("t2v", s.get("clip_mode") != "vislide")
        ig = self.builder.widget("img_gen")
        if isinstance(ig, Segmented):
            # слайдшоу и вислайд показывают картинки — без их генерации ролик собрать не из чего
            ig.set_button_enabled("skip", s.get("clip_mode") not in ("slideshow", "vislide"))
        for model_key, quality_key in CHEAPVIBE_QUALITY_PAIRS:
            cq = self.builder.widget(quality_key)
            if isinstance(cq, Segmented):
                allowed = catalog.cheapvibe_qualities(s.get(model_key))
                for quality in catalog.CHEAPVIBE_QUALITIES:
                    cq.set_button_enabled(quality, quality in allowed)

    # ================================================================ сводка
    def _refresh_summary(self) -> None:
        if not hasattr(self, "summary"):
            return
        v = self.store.values
        files = len(self.queue)
        img = {"veononstop": f"VeoNonStop · {dict(catalog.BANANA_MODELS).get(v['veo_banana_model'], v['veo_banana_model'])}",
               "vertex": f"Vertex · {dict(catalog.VERTEX_IMAGE_MODELS).get(v['vertex_image_model'], v['vertex_image_model'])}",
               "cheapvibe": f"CheapVibeCode · {dict(catalog.CHEAPVIBE_MODELS).get(v['cheapvibe_model'], v['cheapvibe_model'])}"
                            f" · {v['cheapvibe_size']} · {v['cheapvibe_quality']}",
               "varag": f"VARAG · ×{v['varag_image_parallel']}",
               "cinai": f"CinAI · {v['cinai_model']}", "skip": tr(T("нет", "none"))}.get(v["img_gen"], v["img_gen"])
        if v["video_mode"] == "skip":
            video = tr(T("нет", "none"))
        elif v.get("video_provider") == "vertex":
            video = f"Vertex · {v['video_mode']} · {v['veo_ratio']} · ×{v['vertex_video_parallel']}"
        elif v.get("video_provider") == "cinai":
            model = dict(catalog.CINAI_VIDEO_MODELS).get(v["cinai_video_model"], v["cinai_video_model"])
            video = f"CinAI · {model} · {v['video_mode']} · {v['veo_ratio']} · ×{v['cinai_video_parallel']}"
        elif v.get("video_provider") == "royaltechno":
            video = (f"RoyalTechno · Veo 3.1 720p · {v['video_mode']} · {v['veo_ratio']}"
                     f" · ×{v['royaltechno_video_parallel']}")
        elif v.get("video_provider") == "varag":
            video = f"VARAG · {v['video_mode']} · {v['veo_ratio']} · ×{v['varag_video_parallel']}"
        else:
            video = f"{v['video_mode']} · {v['veo_ratio']} · ×{v['veo_parallel']}"
        sg = (Path(v["sg_global_path"]).name if v.get("sg_global_path") else tr(T("не выбран", "not set"))) \
            if v["sg_mode"] == "one_for_all" else tr(T("для каждого файла", "per file"))
        extras, mix = [], source_mix(v)
        if v.get("stock_enabled"):
            extras.append(tr(T("Сток", "Stock")) + f" {mix['mix_stock_video'] + mix['mix_stock_photo']}%")
        if v.get("infographic_enabled"):
            extras.append(tr(T("Инфографика", "Infographics")) + f" {mix['mix_infographic']}%")
        if v.get("motion_enabled"):
            extras.append(tr(T("Моушн", "Motion")) + f" {mix['mix_motion']}%")
        if extras:
            extras.append(tr(T("ИИ", "AI")) + f" {mix['ai']}%")
        rows = [
            (T("Файлов", "Files"), str(files)),
            (T("Стайлгайд", "Style guide"), sg),
            (T("Старт", "Start"), v["start_point"]),
            (T("Режиссура", "Direction"), f"Codex {v['gpt_model']} · {v['direction_mode']}"),
            (T("Клипы", "Clips"), f"{v['clip_mode']} {v['clip_min']}–{v['clip_max']}s"),
            (T("Картинки", "Images"), img),
            (T("Видео", "Video"), video),
            (T("Склейка", "Render"), f"{v['render_mode']} · {v['concat_bitrate']} · {v['concat_fps']} fps"),
        ]
        if extras:
            rows.append((T("Источники", "Sources"), " · ".join(extras)))
        self._summary_rows = [(tr(k), val) for k, val in rows]
        self._render_summary()

    def _render_summary(self) -> None:
        """Сводка таблицей: в широкой колонке — в две пары столбцов, в узкой — в одну."""
        rows = getattr(self, "_summary_rows", [])
        pairs = 2 if self.summary.width() >= SUMMARY_TWO_COLUMNS else 1
        self._summary_pairs = pairs
        key = f"color:{theme.c('muted')};padding-right:10px"
        half = (len(rows) + pairs - 1) // pairs
        lines = []
        for i in range(half):
            parts = []
            for col in range(pairs):
                j = i + col * half
                gap = "padding-left:28px;" if col else ""
                if j < len(rows):
                    parts.append(f"<td style='{gap}{key}'>{rows[j][0]}</td><td>{rows[j][1]}</td>")
            lines.append("<tr>" + "".join(parts) + "</tr>")
        self.summary.setText(f"<table cellspacing='0' cellpadding='1'>{''.join(lines)}</table>")

    def eventFilter(self, obj, event):  # noqa: N802 (Qt API)
        # колонка стала шире или уже — сводка перестраивается в одну или две пары столбцов
        if obj is getattr(self, "summary", None) and event.type() == QEvent.Resize:
            pairs = 2 if self.summary.width() >= SUMMARY_TWO_COLUMNS else 1
            if pairs != getattr(self, "_summary_pairs", 0):
                QTimer.singleShot(0, self._render_summary)
        return super().eventFilter(obj, event)

    # =============================================================== очередь
    def _item_row(self, item: QueueItem, row: int) -> None:
        b = item.binding

        def cell(col, text, tip="", color=None, align=Qt.AlignLeft | Qt.AlignVCenter):
            it = QTableWidgetItem(text)
            it.setTextAlignment(align)
            if tip:
                it.setToolTip(tip)
            if color:
                it.setForeground(QColor(color))
            self.table.setItem(row, col, it)

        cell(COL_STAR, "★" if item.snapshot else "", tr(T("Свои настройки", "Pinned settings")) if item.snapshot else "",
             theme.c("amber"), Qt.AlignCenter)
        cell(COL_FILE, item.path.name, str(item.path))
        cell(COL_STYLE, Path(b.style).name if b.style else "—", b.style)
        cell(COL_PRESET, b.preset or tr(T("(текущие)", "(current)")))
        cell(COL_LOGO, Path(b.logo).name if b.logo else "—", b.logo)
        cell(COL_COVER, Path(b.cover).name if b.cover else "—", b.cover)
        icon = {"running": "⏳", "done": "✅", "failed": "❌", "cancelled": "⛔"}.get(item.status, "")
        cell(COL_STATUS, icon, item.status, align=Qt.AlignCenter)

    def _refresh_table(self) -> None:
        selected = {i.row() for i in self.table.selectedIndexes()}
        self.table.setRowCount(len(self.queue))
        for row, item in enumerate(self.queue):
            self._item_row(item, row)
        # столбцы «Стайлгайд» и «Логотип» нужны, только когда они задаются для каждого файла
        v = self.store.values
        self.table.setColumnHidden(COL_STYLE, v.get("sg_mode") != "individual")
        self.table.setColumnHidden(COL_LOGO, not (v.get("watermark") == "logo" and v.get("logo_mode") == "individual"))
        for row in selected:
            if row < len(self.queue):
                self.table.selectRow(row)
        pinned = sum(1 for it in self.queue if it.snapshot)
        text = tr(T(f"{len(self.queue)} файл(ов)", f"{len(self.queue)} file(s)"))
        if pinned:
            text += f" · ★ {pinned}"
        self.queue_count.setText(text)
        self._refresh_summary()

    def _allowed_ext(self) -> set:
        return AUDIO_EXT

    def add_files(self) -> None:
        flt = "Audio/Video (*.mp3 *.wav *.flac *.m4a *.aac *.mp4 *.mov *.mkv *.webm *.avi *.m4v);;All (*.*)"
        start = str(self.queue[-1].path.parent) if self.queue else str(paths.ROOT)
        files, _ = QFileDialog.getOpenFileNames(self, tr(T("Исходные файлы", "Source files")), start, flt)
        self.add_paths([Path(f) for f in files])

    def add_paths(self, files: list[Path]) -> None:
        allowed = self._allowed_ext()
        added, skipped = 0, 0
        names = {it.path.name for it in self.queue}
        for f in files:
            if not f.is_file():
                continue
            if f.suffix.lower() not in allowed:
                skipped += 1
                continue
            if f.name in names:
                continue
            self.queue.append(QueueItem(f))
            names.add(f.name)
            added += 1
        if skipped:
            self.log(tr(T(f"Пропущено файлов неподходящего типа: {skipped} (нужны аудио или видео)",
                          f"Skipped files of a wrong type: {skipped} (audio or video required)")), "WARNING")
        if added:
            self._refresh_table()

    def _selected_rows(self) -> list[int]:
        return sorted({i.row() for i in self.table.selectedIndexes()})

    def remove_selected(self) -> None:
        for row in reversed(self._selected_rows()):
            if self.queue[row].status != "running":
                self.queue.pop(row)
        self._refresh_table()

    def clear_queue(self) -> None:
        self.queue = [it for it in self.queue if it.status == "running"]
        self._refresh_table()

    def move_selected(self, delta: int) -> None:
        rows = self._selected_rows()
        if len(rows) != 1:
            return
        row, new = rows[0], rows[0] + delta
        if 0 <= new < len(self.queue):
            self.queue[row], self.queue[new] = self.queue[new], self.queue[row]
            self._refresh_table()
            self.table.selectRow(new)

    def pin_selected(self) -> None:
        rows = self._selected_rows() or ([len(self.queue) - 1] if self.queue else [])
        if not rows:
            QMessageBox.information(self, "★", tr(T("Сначала добавьте файл в очередь.", "Add a file to the queue first.")))
            return
        for row in rows:
            self.queue[row].snapshot = self.store.snapshot()
            self.log(tr(T(f"★ Настройки запомнены для {self.queue[row].path.name}",
                          f"★ Settings pinned to {self.queue[row].path.name}")), "SUCCESS")
        self._refresh_table()

    def _choose(self, title, flt) -> str:
        path, _ = QFileDialog.getOpenFileName(self, tr(title), "", flt)
        return path or ""

    def _cell_action(self, row: int, col: int) -> None:
        if not (0 <= row < len(self.queue)):
            return
        item = self.queue[row]
        b = item.binding
        if col == COL_STAR:
            item.snapshot = None if item.snapshot else self.store.snapshot()
        elif col == COL_STYLE:
            b.style = self._choose(T("Стайлгайд", "Style guide"), "Text (*.txt *.md);;All (*.*)") or b.style
        elif col == COL_PRESET:
            self._preset_menu(row)
            return
        elif col == COL_LOGO:
            b.logo = self._choose(T("Логотип", "Logo"), "Images (*.png *.jpg *.jpeg *.webp)") or b.logo
        elif col == COL_COVER:
            b.cover = self._choose(T("Обложка", "Cover"), "Images (*.png *.jpg *.jpeg *.webp)") or b.cover
        self._refresh_table()

    def _preset_menu(self, row: int) -> None:
        menu = QMenu(self)
        menu.addAction(tr(T("(текущие настройки)", "(current settings)")), lambda: self._set_preset(row, ""))
        for name in preset_store.names():
            menu.addAction(name, lambda n=name: self._set_preset(row, n))
        menu.exec(self.table.viewport().mapToGlobal(self.table.visualRect(self.table.model().index(row, COL_PRESET)).center()))

    def _set_preset(self, row: int, name: str) -> None:
        self.queue[row].binding.preset = name
        self._refresh_table()

    def _context_menu(self, pos) -> None:
        row = self.table.rowAt(pos.y())
        if row < 0:
            return
        item = self.queue[row]
        b = item.binding
        menu = QMenu(self)
        menu.addAction(tr(T("★ Запомнить текущие настройки", "★ Pin current settings")), lambda: self._pin(row, True))
        if item.snapshot:
            menu.addAction(tr(T("Снять ★", "Unpin ★")), lambda: self._pin(row, False))
        menu.addSeparator()
        menu.addAction(tr(T("Стайлгайд…", "Style guide…")), lambda: self._cell_action(row, COL_STYLE))
        menu.addAction(tr(T("Пресет…", "Preset…")), lambda: self._preset_menu(row))
        menu.addAction(tr(T("Логотип…", "Logo…")), lambda: self._cell_action(row, COL_LOGO))
        menu.addAction(tr(T("Обложка…", "Cover…")), lambda: self._cell_action(row, COL_COVER))
        adapt = menu.addMenu(tr(T("Адаптация стайлгайда", "Style guide adaptation")))
        for value, text in ((None, T("как общая галка", "same as global")), (True, T("включить", "on")),
                            (False, T("выключить", "off"))):
            act = adapt.addAction(tr(text), lambda v=value: setattr(b, "adapt", v))
            act.setCheckable(True)
            act.setChecked(b.adapt is value)
        menu.addSeparator()
        clear = menu.addMenu(tr(T("Очистить", "Clear")))
        clear.addAction(tr(T("стайлгайд", "style guide")), lambda: (setattr(b, "style", ""), self._refresh_table()))
        clear.addAction(tr(T("логотип", "logo")), lambda: (setattr(b, "logo", ""), self._refresh_table()))
        clear.addAction(tr(T("обложку", "cover")), lambda: (setattr(b, "cover", ""), self._refresh_table()))
        menu.addAction(tr(T("Показать файл", "Show file")), lambda: procs.open_path(item.path.parent))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _pin(self, row: int, on: bool) -> None:
        self.queue[row].snapshot = self.store.snapshot() if on else None
        self._refresh_table()

    def _open_adapted(self) -> None:
        found = sorted(paths.PROJECTS.glob("*/style_guide_adapted.txt"), key=lambda p: p.stat().st_mtime) \
            if paths.PROJECTS.exists() else []
        if found:
            procs.open_path(found[-1])
        else:
            QMessageBox.information(self, "Style guide", tr(T("Адаптированный стайлгайд ещё не создавался.",
                                                              "No adapted style guide yet.")))

    # ================================================================ пресеты
    def apply_preset(self, name: str) -> None:
        preset = preset_store.get(name)
        if not preset:
            return
        base = defaults()
        values = {k: v for k, v in upgrade_values(dict(preset)).items() if k in base}
        self._bulk_update(values)
        self.builder.refresh()
        self.log(tr(T(f"Пресет применён: {name}", f"Preset applied: {name}")), "SUCCESS")

    def _bulk_update(self, values: dict) -> None:
        """Записать пачку значений (пресет, сброс, пересборка окна) и согласовать связи один раз:
        по ходу записи значения временно расходятся, и связи сработали бы не по делу."""
        self._links_paused = True
        try:
            self.store.update(values)
            fixed = normalize_links(dict(self.store.values))
            self.store.update({k: v for k, v in fixed.items() if self.store.values.get(k) != v})
        finally:
            self._links_paused = False
        self._link_prev = dict(self.store.values)
        self._link_memory.clear()

    def reset_defaults(self) -> None:
        engine_link.reload_params()
        fresh = defaults()
        for key in ("sg_global_path", "single_prompt_path", "logo_path"):
            fresh.pop(key, None)
        self._bulk_update(fresh)
        self.builder.refresh()
        self.log(tr(T("Значения сброшены к сохранённым по умолчанию.", "Values reset to the saved defaults.")), "INFO")

    def save_defaults(self) -> None:
        try:
            envfile.write_env(runplan.defaults_env(self.store.values), section_title="Pipeline defaults")
            engine_link.reload_params()
            self.log(tr(T("Значения по умолчанию сохранены в engine/.env", "Defaults saved to engine/.env")), "SUCCESS")
            self.app.toast(tr(T("Значения по умолчанию сохранены", "Defaults saved")))
        except Exception as exc:  # noqa: BLE001
            self.log(f"Save defaults: {exc}", "ERROR")

    def _refresh_keys(self) -> None:
        self.monitors.refresh_veo_keys()
        self.monitors.refresh_veo()

    # ====================================================== запуск конвейера
    def _validate(self) -> bool:
        v = self.store.values
        pending = [it for it in self.queue if it.status not in ("done",)]
        if not pending:
            QMessageBox.warning(self, tr(T("Очередь пуста", "Queue is empty")),
                                tr(T("Добавьте исходные файлы в очередь.", "Add source files to the queue.")))
            return False
        if v["sg_mode"] == "one_for_all":
            if not (v.get("sg_global_path") and Path(v["sg_global_path"]).is_file()):
                QMessageBox.warning(self, tr(T("Нужен стайлгайд", "Style guide required")),
                                    tr(T("Стайлгайд не выбран. Без него режиссура не запустится и проект создастся пустым.",
                                         "No style guide selected. Direction cannot start without it and the project "
                                         "would be empty.")))
                return False
        else:
            missing = [it.path.name for it in pending if not (it.binding.style and Path(it.binding.style).is_file())]
            if missing:
                QMessageBox.warning(self, tr(T("Нужен стайлгайд", "Style guide required")),
                                    tr(T("Назначьте стайлгайд каждому файлу:\n\n", "Assign a style guide to every file:\n\n"))
                                    + "\n".join(f"• {n}" for n in missing[:12]))
                return False
        return True

    def start(self) -> None:
        if self.runner.is_running():
            return
        if not self._validate():
            return
        for it in self.queue:
            if it.status != "done":
                it.status = ""
        self._refresh_table()
        self.logview.clear()
        for chip in self.stage_labels:
            chip.set_tone("")
        self.task_bar.setValue(0)
        self._save_session()
        self.runner.start([it.path for it in self.queue if it.status != "done"])

    def runner_stop(self) -> None:
        self.runner.stop()

    def _take_next(self, processed: set):
        """Вызывается в потоке интерфейса: следующий файл и его слепок настроек."""
        for item in self.queue:
            if item.path.name in processed or item.status == "done":
                continue
            # live — файл идёт по форме: её изменения во время прогона применяются к нему на ходу
            live = False
            if item.snapshot:
                snap, tag = copy.deepcopy(item.snapshot), tr(T("свои ★", "pinned ★"))
            elif item.binding.preset and preset_store.get(item.binding.preset):
                snap = self.store.snapshot()
                preset = upgrade_values(dict(preset_store.get(item.binding.preset)))
                snap.update({k: v for k, v in preset.items() if k in snap})
                tag = tr(T(f"пресет {item.binding.preset}", f"preset {item.binding.preset}"))
            else:
                snap, tag, live = self.store.snapshot(), tr(T("форма", "form")), True
            # привязки стайлгайда берутся актуальные, а не из старого снимка
            snap["sg_mode"] = self.store.get("sg_mode")
            snap["sg_global_path"] = self.store.get("sg_global_path")
            return item.path, snap, copy.deepcopy(item.binding), tag, live
        return None

    def _wire_runner(self) -> None:
        r = self.runner
        r.log.connect(self.log)
        r.stage.connect(self._set_stage)
        r.task.connect(self._set_task)
        r.current.connect(lambda n, p, t: _show_text(self.current_file, f"{n}  ({p}/{t})" if n else ""))
        r.spinner.connect(lambda text: _show_text(self.spinner, text))
        r.running.connect(self._set_running)
        r.item_status.connect(self._set_item_status)
        r.finished.connect(self._finished)

    def _set_stage(self, index: int) -> None:
        for i, chip in enumerate(self.stage_labels):
            chip.set_tone("ok" if i < index else ("warn" if i == index else ""))
        self.task_bar.setValue(0)
        self.task_label.setText("")

    def _set_task(self, cur: int, total: int) -> None:
        if total > 0:
            self.task_bar.setRange(0, total)
            self.task_bar.setValue(cur)
            self.task_label.setText(f"{cur} / {total}")

    def _set_running(self, running: bool) -> None:
        self.start_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)
        self.app.set_busy("pipeline", running)

    def _set_item_status(self, name: str, status: str) -> None:
        for item in self.queue:
            if item.path.name == name:
                item.status = status
        self._refresh_table()

    def _finished(self, ok: bool, cancelled: bool, count: int) -> None:
        if cancelled:
            self.app.notify(tr(T("Конвейер остановлен", "Pipeline stopped")), "")
        elif ok:
            self.app.notify(tr(T("Конвейер завершён", "Pipeline finished")),
                            tr(T(f"Обработано файлов: {count}", f"Files processed: {count}")))
        else:
            self.app.notify(tr(T("Конвейер завершён с ошибками", "Pipeline finished with errors")),
                            tr(T("Подробности — в журнале.", "See the log for details.")))

    # ================================================================ сессия
    def _load_session(self, base: dict) -> dict:
        try:
            data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        values = {k: v for k, v in upgrade_values(data).items() if k in base and type(v) is type(base[k])}
        if values.get("gpt_model") not in catalog.CODEX_MODELS:
            values.pop("gpt_model", None)                 # модели больше нет в Codex — берём значение по умолчанию
        if values.get("motion_model") not in catalog.CODEX_MODELS:
            values.pop("motion_model", None)
        if values.get("rewrite_model") not in catalog.REWRITE_SPECS:
            values.pop("rewrite_model", None)
        if not values.get("stock_vision_provider"):
            values.pop("stock_vision_provider", None)     # раньше пусто = «как у Real Photo»; теперь выбор обязателен
            values.pop("stock_vision_model", None)
        return values

    def _save_session(self) -> None:
        try:
            paths.USERDATA.mkdir(parents=True, exist_ok=True)
            SESSION_FILE.write_text(json.dumps(self.store.values, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass

    def shutdown(self) -> None:
        self._save_session()

    def export_state(self) -> dict:
        """Состояние для пересборки окна (смена языка/темы)."""
        return {"values": self.store.snapshot(), "queue": self.queue}

    def import_state(self, data: dict) -> None:
        base = defaults()
        self._bulk_update({k: v for k, v in data.get("values", {}).items() if k in base})
        self.queue = data.get("queue", [])
        self.builder.refresh()
        self._refresh_table()
