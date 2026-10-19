"""Страница «Промпты»: просмотр и правка всех инструкций движка и шаблонов субтитров."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QKeySequence, QShortcut, QTextCursor
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QSplitter, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)
from qfluentwidgets import LineEdit, TreeWidget

from .. import paths
from ..i18n import T, tr
from ..services import procs
from ..ui.widgets import button, label, set_tip, smooth_scroll

I = "instructions"
# (группа, [(имя файла, подпись, подсказка)])
CATALOG = [
    (T("Режиссура", "Direction"), [
        (I, "director_instruction.txt", T("Режиссёр — промпты (проход 2)", "Director — prompts (pass 2)"),
         T("Главная инструкция: как писать промпты кадров по монтажному плану.", "Main instruction: how to write frame prompts from the plan.")),
        (I, "director_instruction_pass1.txt", T("Режиссёр — план (проход 1)", "Director — plan (pass 1)"),
         T("Нарезка расшифровки на сцены и тайминги.", "Cutting the transcript into scenes and timings.")),
        (I, "director_instruction_t2v.txt", T("Режиссёр — text-to-video", "Director — text-to-video"), None),
        (I, "gpt_director_pass2.txt", T("Режиссёр Codex — проход 2", "Codex director — pass 2"),
         T("Обёртка второго прохода для Codex (OpenAI).", "Second-pass wrapper for Codex (OpenAI).")),
        (I, "director_pause_guide.txt", T("Паузы в речи", "Speech pauses"), T("Как обрабатывать паузы при нарезке.", "How pauses are handled when cutting.")),
        (I, "sg_adapt_prompt.txt", T("Адаптация стайлгайда (GPT)", "Style guide adaptation (GPT)"), None),
    ]),
    (T("Генерация и видео", "Generation & video"), [
        (I, "veo_adapt_prompt.txt", T("Адаптация промптов под VEO (GPT)", "VEO prompt adaptation (GPT)"), None),
        (I, "veo_i2v_universal_style.md", T("Универсальный стайлгайд i2v", "Universal i2v style guide"),
         T("Стайлгайд движения по умолчанию для адаптации промптов.", "Default motion style guide for prompt adaptation.")),
        (I, "rewrite_prompt.txt", T("Переписывание при цензуре", "Rewrite on censorship"),
         T("Как смягчать промпт, отклонённый модерацией.", "How to soften a prompt rejected by moderation.")),
        (I, "ref_remix_prompt.txt", T("Ремиксер: референсы", "Remixer: references"), None),
        (I, "i2v_remix_prompt.txt", T("Ремиксер: i2v", "Remixer: i2v"), None),
        (I, "parallax_prompts.txt", T("Шаблоны параллакса i2v", "i2v parallax templates"), None),
        (I, "cover_hook_prompts.txt", T("Оживление обложки", "Cover hook animation"),
         T("Пять режимов оживления первого кадра: наезд, отъезд, дрейф, залёт, оживание.",
           "Five first-frame animation modes: push-in, pull-out, drift, fly-in, come-alive.")),
        (I, "infographic_default_style.txt", T("Стиль инфографики", "Infographics style"), None),
        (I, "mograph_scene.txt", T("Моушн-графика: задание Codex", "Motion graphics: Codex task"),
         T("Что Codex получает для каждой сцены Remotion. Слова в {фигурных скобках} программа\n"
           "подставляет сама: длину клипа, формат, текст озвучки, стайлгайд.",
           "What Codex gets for each Remotion scene. Words in {curly braces} are filled in by the\n"
           "program: clip length, format, narration, style guide.")),
    ]),
    (T("Сток", "Stock"), [
        (I, "vision_house_rules.txt", T("Вижн — свои запреты", "Vision — house rules"),
         T("Ваши правила отсева кадров для vision-проверки. Пустой файл ничего не меняет.",
           "Your own rejection rules for vision checks. An empty file changes nothing.")),
    ]),
    (T("Шаблоны субтитров (.ass)", "Subtitle templates (.ass)"), [
        ("templates", "subtitles_720p.ass", "720p", None),
        ("templates", "subtitles_720p_center.ass", T("720p — по центру", "720p — center"), None),
        ("templates", "subtitles_1080p.ass", "1080p", None),
        ("templates", "subtitles_1080p_center.ass", T("1080p — по центру", "1080p — center"), None),
        ("templates", "subtitles_4k.ass", "4K", None),
        ("templates", "subtitles_4k_center.ass", T("4K — по центру", "4K — center"), None),
    ]),
]


def _root(kind: str) -> Path:
    return paths.INSTRUCTIONS if kind == I else paths.TEMPLATES


def _default(path: Path) -> Path:
    """Заводская копия файла (engine/instructions_default/<папка>/<файл>)."""
    return paths.INSTRUCTIONS_DEFAULT / path.parent.name / path.name


def ensure_defaults() -> None:
    """При первом запуске сохранить заводские копии всех промптов."""
    for folder in (paths.INSTRUCTIONS, paths.TEMPLATES):
        target = paths.INSTRUCTIONS_DEFAULT / folder.name
        if not folder.exists():
            continue
        for f in folder.iterdir():
            dst = target / f.name
            if f.is_file() and not dst.exists():
                target.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(f.read_bytes())


class PromptsPage(QWidget):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.current: Path | None = None
        self.modified = False
        try:
            ensure_defaults()
        except OSError:
            pass
        self._build()
        QTimer.singleShot(50, self._select_first)

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(8)

        top = QHBoxLayout()
        self.save_btn = button(T("Сохранить", "Save"), "primary", T("Записать файл (Ctrl+S).", "Write the file (Ctrl+S)."), self.save, icon="SAVE")
        top.addWidget(self.save_btn)
        top.addWidget(button(T("Перечитать", "Reload"), "", T("Отменить несохранённые правки.", "Discard unsaved edits."), self.reload))
        top.addWidget(button(T("Заводской вариант", "Restore default"), "",
                             T("Вернуть исходный текст промпта, поставлявшийся с программой.\nТекущая версия сохранится рядом как .bak.",
                               "Restore the prompt text shipped with the app.\nThe current version is kept next to it as .bak."),
                             self.restore_default))
        top.addWidget(button(T("Открыть папку", "Open folder"), "ghost", None,
                             lambda: procs.open_path(self.current.parent if self.current else paths.INSTRUCTIONS)))
        self.status = label("", "muted")
        top.addWidget(self.status)
        top.addStretch(1)
        self.file_label = label("", "dim")
        top.addWidget(self.file_label)
        root.addLayout(top)

        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)

        left = QFrame()
        left.setObjectName("Panel")
        ll = QVBoxLayout(left)
        ll.setContentsMargins(8, 8, 8, 8)
        self.search = LineEdit()
        self.search.setPlaceholderText(tr(T("Поиск по названию и тексту…", "Search names and text…")))
        self.search.setClearButtonEnabled(True)
        set_tip(self.search, T("Ищет и в названиях, и внутри текстов промптов.", "Searches both names and prompt contents."))
        self.search.textChanged.connect(self._filter)
        ll.addWidget(self.search)
        self.tree = TreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setObjectName("Transparent")
        self.tree.currentItemChanged.connect(self._on_select)
        ll.addWidget(self.tree, 1)
        split.addWidget(left)

        right = QFrame()
        right.setObjectName("Card")
        rl = QVBoxLayout(right)
        rl.setContentsMargins(10, 10, 10, 10)
        self.title = QLabel("")
        self.title.setObjectName("CardTitle")
        rl.addWidget(self.title)
        self.hint = label("", "muted", wrap=True)
        rl.addWidget(self.hint)
        self.editor = QPlainTextEdit()
        self.editor.setObjectName("Editor")
        font = QFont("Cascadia Mono")
        font.setStyleHint(QFont.Monospace)
        font.setPointSize(10)
        self.editor.setFont(font)
        self.editor.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        smooth_scroll(self.editor)
        self.editor.textChanged.connect(self._on_changed)
        self.editor.cursorPositionChanged.connect(self._update_pos)
        rl.addWidget(self.editor, 1)
        find = QHBoxLayout()
        self.find = LineEdit()
        self.find.setPlaceholderText(tr(T("Найти в тексте (Ctrl+F, Enter — дальше)", "Find in text (Ctrl+F, Enter — next)")))
        self.find.returnPressed.connect(self.find_next)
        find.addWidget(self.find, 1)
        self.pos = label("", "dim")
        find.addWidget(self.pos)
        rl.addLayout(find)
        split.addWidget(right)
        split.setSizes([300, 900])
        # при сужении окна сжимается редактор, а список промптов сохраняет ширину
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        left.setMinimumWidth(260)
        root.addWidget(split, 1)

        QShortcut(QKeySequence.Save, self, activated=self.save)
        QShortcut(QKeySequence.Find, self, activated=lambda: (self.find.setFocus(), self.find.selectAll()))
        self._fill_tree()

    def _fill_tree(self) -> None:
        self.tree.clear()
        known = set()
        for group, items in CATALOG:
            head = QTreeWidgetItem([tr(group)])
            head.setFlags(Qt.ItemIsEnabled)
            head.setFirstColumnSpanned(True)
            font = head.font(0)
            font.setBold(True)
            head.setFont(0, font)
            self.tree.addTopLevelItem(head)
            for kind, fname, text, tip in items:
                path = _root(kind) / fname
                known.add(path.name)
                if not path.exists():
                    continue
                item = QTreeWidgetItem([tr(text)])
                item.setData(0, Qt.UserRole, str(path))
                item.setData(0, Qt.UserRole + 1, tr(tip) if tip else "")
                item.setToolTip(0, (tr(tip) + "\n\n" if tip else "") + fname)
                head.addChild(item)
            head.setExpanded(True)
        # файлы, которых нет в каталоге (добавленные пользователем), — в отдельную группу
        extra = [f for f in sorted(paths.INSTRUCTIONS.glob("*")) if f.is_file() and f.suffix in (".txt", ".md")
                 and f.name not in known]
        if extra:
            head = QTreeWidgetItem([tr(T("Дополнительные", "Additional"))])
            head.setFlags(Qt.ItemIsEnabled)
            self.tree.addTopLevelItem(head)
            for f in extra:
                item = QTreeWidgetItem([f.stem])
                item.setData(0, Qt.UserRole, str(f))
                item.setToolTip(0, f.name)
                head.addChild(item)
            head.setExpanded(True)

    def _select_first(self) -> None:
        top = self.tree.topLevelItem(0)
        if top and top.childCount():
            self.tree.setCurrentItem(top.child(0))

    def _filter(self, text: str) -> None:
        q = text.strip().lower()
        for i in range(self.tree.topLevelItemCount()):
            head = self.tree.topLevelItem(i)
            shown = 0
            for j in range(head.childCount()):
                item = head.child(j)
                match = not q or q in item.text(0).lower()
                if q and not match:
                    try:
                        match = q in Path(item.data(0, Qt.UserRole)).read_text(encoding="utf-8-sig").lower()
                    except OSError:
                        match = False
                item.setHidden(not match)
                shown += match
            head.setHidden(shown == 0)

    # ============================================================ файлы
    def _on_select(self, item, previous) -> None:
        if not item or not item.data(0, Qt.UserRole):
            return
        path = Path(item.data(0, Qt.UserRole))
        if path == self.current:
            return
        if self.modified and self.current:
            answer = QMessageBox.question(
                self, tr(T("Несохранённые правки", "Unsaved edits")),
                tr(T(f"Сохранить изменения в {self.current.name}?", f"Save changes to {self.current.name}?")),
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
            if answer == QMessageBox.Cancel:
                self.tree.blockSignals(True)
                self.tree.setCurrentItem(previous)
                self.tree.blockSignals(False)
                return
            if answer == QMessageBox.Save:
                self.save()
        self.load(path, item.text(0), item.data(0, Qt.UserRole + 1) or "")

    def load(self, path: Path, title: str = "", hint: str = "") -> None:
        self.current = path
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            text = f"# {exc}"
        self.editor.blockSignals(True)
        self.editor.setPlainText(text)
        self.editor.blockSignals(False)
        self.modified = False
        self.title.setText(title or path.stem)
        self.hint.setText(hint)
        self.hint.setVisible(bool(hint))
        try:
            rel = path.relative_to(paths.ROOT)
        except ValueError:
            rel = path
        self.file_label.setText(str(rel))
        self._show_status()
        self._update_pos()
        q = self.search.text().strip()
        if q:
            self.find.setText(q)
            self.find_next()

    def _differs_from_default(self) -> bool:
        if not self.current:
            return False
        default = _default(self.current)
        try:
            return default.exists() and default.read_text(encoding="utf-8-sig") != self.editor.toPlainText()
        except OSError:
            return False

    def _show_status(self, text: str = "", role: str = "muted") -> None:
        if not text:
            if self.modified:
                text, role = tr(T("● не сохранено", "● unsaved")), "warn"
            elif self._differs_from_default():
                text, role = tr(T("изменён относительно заводского", "differs from default")), "muted"
        self.status.setText(text)
        self.status.setProperty("role", role)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

    def _on_changed(self) -> None:
        if not self.modified:
            self.modified = True
            self._show_status()

    def _update_pos(self) -> None:
        cur = self.editor.textCursor()
        doc = self.editor.document()
        self.pos.setText(tr(T(f"стр. {cur.blockNumber() + 1}, стлб {cur.positionInBlock() + 1} · {doc.blockCount()} строк · "
                              f"{len(self.editor.toPlainText())} симв.",
                              f"ln {cur.blockNumber() + 1}, col {cur.positionInBlock() + 1} · {doc.blockCount()} lines · "
                              f"{len(self.editor.toPlainText())} chars")))

    def save(self) -> None:
        if not self.current:
            return
        try:
            self.current.write_text(self.editor.toPlainText(), encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, tr(T("Ошибка", "Error")), str(exc))
            return
        self.modified = False
        self._show_status(tr(T(f"Сохранено: {self.current.name}", f"Saved: {self.current.name}")), "ok")
        QTimer.singleShot(3000, lambda: self._show_status())

    def reload(self) -> None:
        if self.current:
            item = self.tree.currentItem()
            self.load(self.current, item.text(0) if item else "", (item.data(0, Qt.UserRole + 1) if item else "") or "")

    def restore_default(self) -> None:
        if not self.current:
            return
        default = _default(self.current)
        if not default.exists():
            QMessageBox.information(self, tr(T("Заводской вариант", "Default")),
                                    tr(T("Для этого файла нет заводской копии.", "There is no factory copy for this file.")))
            return
        if QMessageBox.question(self, tr(T("Заводской вариант", "Default")),
                                tr(T(f"Вернуть заводской текст {self.current.name}?\nТекущий сохранится как .bak.",
                                     f"Restore the factory text of {self.current.name}?\nThe current one is kept as .bak."))) \
                != QMessageBox.Yes:
            return
        try:
            if self.current.exists():
                self.current.with_suffix(self.current.suffix + ".bak").write_bytes(self.current.read_bytes())
            self.current.write_bytes(default.read_bytes())
        except OSError as exc:
            QMessageBox.critical(self, tr(T("Ошибка", "Error")), str(exc))
            return
        self.reload()
        self._show_status(tr(T("Восстановлен заводской вариант", "Default restored")), "ok")

    def find_next(self) -> None:
        text = self.find.text()
        if not text:
            return
        if not self.editor.find(text):
            self.editor.moveCursor(QTextCursor.Start)
            if not self.editor.find(text):
                self._show_status(tr(T("не найдено", "not found")), "warn")

    def has_unsaved(self) -> bool:
        return self.modified
