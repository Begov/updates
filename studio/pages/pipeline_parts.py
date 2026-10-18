"""Нестандартные элементы вкладки «Конвейер».

Каждая функция получает страницу (у неё есть store, monitors, log()) и
возвращает готовый виджет, связанный с хранилищем значений.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QInputDialog,
    QLabel, QMessageBox, QProgressBar, QPushButton, QSizePolicy, QToolButton,
    QVBoxLayout, QWidget,
)
from ..ui.widgets import LABEL_GAP, Combo, Toggle

from .. import engine_link, paths, theme
from ..i18n import T, tr
from ..services import monitors as mon
from ..services import presets as preset_store
from ..services import procs
from ..pipeline.defaults import MIX_KEYS, mix_active
from ..pipeline.defaults import source_mix as source_mix_values
from ..ui.widgets import Chip, MixBar, button, label, repolish, set_tip


class _Relay(QObject):
    """Передать результат фоновой задачи в поток интерфейса."""
    done = Signal(object)


def bind_signal(signal, fn, owner: QWidget) -> None:
    """Подписать функцию на сигнал и отключить её, когда виджет-владелец уничтожен.

    Обычные функции (замыкания) сами не отключаются: после пересоздания окна они
    продолжали бы обращаться к удалённым виджетам и падали с ошибкой Qt."""
    signal.connect(fn)

    def drop(*_):
        try:
            signal.disconnect(fn)
        except (RuntimeError, TypeError):
            pass
    owner.destroyed.connect(drop)


def run_bg(fn, on_done=None):
    relay = _Relay()
    if on_done:
        relay.done.connect(on_done)

    def work():
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001
            result = exc
        relay.done.emit(result)

    # Держим ссылку, пока задача не закончится, иначе сборщик мусора удалит relay
    keep = run_bg.__dict__.setdefault("keep", set())
    keep.add(relay)
    relay.done.connect(lambda _r: keep.discard(relay))
    threading.Thread(target=work, daemon=True).start()
    return relay


# ================================================================ пресеты
def preset_bar(page) -> QWidget:
    box = QWidget()
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(6)
    row = QHBoxLayout()
    combo = Combo()
    combo.setMinimumWidth(220)
    combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    set_tip(combo, T("Сохранённые наборы настроек всего конвейера. Выберите пресет — он применится разом.",
                     "Saved sets of all pipeline settings. Pick a preset to apply it at once."))

    def refill(select: str = ""):
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(tr(T("— выберите пресет —", "— choose a preset —")), "")
        for name in preset_store.names():
            combo.addItem(name, name)
        idx = combo.findData(select)
        combo.setCurrentIndex(max(0, idx))
        combo.blockSignals(False)

    def apply(_i=None):
        name = combo.currentData()
        if name:
            page.apply_preset(name)

    def save():
        current = combo.currentData() or ""
        name, ok = QInputDialog.getText(box, tr(T("Сохранить пресет", "Save preset")),
                                        tr(T("Имя пресета:", "Preset name:")), text=current)
        name = (name or "").strip()
        if ok and name:
            preset_store.put(name, page.store.snapshot())
            refill(name)
            page.log(tr(T(f"Пресет сохранён: {name}", f"Preset saved: {name}")), "SUCCESS")

    def delete():
        name = combo.currentData()
        if not name:
            return
        if QMessageBox.question(box, tr(T("Удалить пресет", "Delete preset")),
                                tr(T(f"Удалить пресет «{name}»?", f"Delete preset '{name}'?"))) == QMessageBox.Yes:
            preset_store.delete(name)
            refill()
            page.log(tr(T(f"Пресет удалён: {name}", f"Preset deleted: {name}")), "INFO")

    def export():
        name = combo.currentData()
        if not name:
            return
        target, _ = QFileDialog.getSaveFileName(box, tr(T("Экспорт пресета", "Export preset")),
                                                f"{name}.json", "JSON (*.json)")
        if target:
            preset_store.export_to(Path(target), name)

    def import_():
        source, _ = QFileDialog.getOpenFileName(box, tr(T("Импорт пресета", "Import preset")), "", "JSON (*.json)")
        if source:
            try:
                added = preset_store.import_from(Path(source))
                refill(added[0] if added else "")
                page.log(tr(T(f"Импортировано пресетов: {len(added)}", f"Presets imported: {len(added)}")), "SUCCESS")
            except Exception as exc:  # noqa: BLE001
                QMessageBox.warning(box, "Preset", str(exc))

    combo.activated.connect(apply)
    row.addWidget(combo, 1)
    row.addWidget(button(T("Сохранить", "Save"), "small", T("Сохранить текущие настройки как пресет", "Save current settings as a preset"), save))
    row.addWidget(button(T("Удалить", "Delete"), "small", None, delete))
    lay.addLayout(row)
    row2 = QHBoxLayout()
    row2.addWidget(button(T("Импорт…", "Import…"), "small", T("Загрузить пресет из файла JSON", "Load a preset from a JSON file"), import_))
    row2.addWidget(button(T("Экспорт…", "Export…"), "small", T("Сохранить выбранный пресет в файл", "Save the selected preset to a file"), export))
    row2.addWidget(button(T("Сбросить к умолчаниям", "Reset to defaults"), "small",
                          T("Вернуть все значения к сохранённым по умолчанию (engine/.env)",
                            "Return every value to the saved defaults (engine/.env)"), page.reset_defaults))
    row2.addStretch(1)
    lay.addLayout(row2)
    refill()
    page.preset_combo_refill = refill
    return box


# ============================================================ квота GPT
def quota_panel(page) -> QWidget:
    frame = QFrame()
    frame.setObjectName("Panel")
    grid = QGridLayout(frame)
    grid.setContentsMargins(12, 10, 12, 10)
    grid.setHorizontalSpacing(10)
    grid.setVerticalSpacing(6)
    title = label(T("Квота подписки GPT", "GPT subscription quota"), "section",
                  T("Остаток лимитов подписки ChatGPT, через которую работает Codex: сколько израсходовано\n"
                    "за 5-часовое окно и за неделю и когда лимит обнулится. Обновляется автоматически.",
                    "Remaining limits of the ChatGPT subscription used by Codex: how much is spent in the\n"
                    "5-hour window and this week, and when it resets. Refreshes automatically."))
    plan = QLabel("…")
    plan.setProperty("role", "dim")
    refresh = QToolButton()
    refresh.setText("↻")
    set_tip(refresh, T("Обновить сейчас", "Refresh now"))
    refresh.clicked.connect(page.monitors.refresh_codex)
    bars = {}
    for row, (key, text) in enumerate((("p", T("5 часов", "5 hours")), ("s", T("Неделя", "Week"))), start=1):
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setFixedHeight(9)
        val = QLabel("—")
        val.setProperty("role", "dim")
        grid.addWidget(label(text), row, 0)
        grid.addWidget(bar, row, 1)
        grid.addWidget(val, row, 2)
        bars[key] = (bar, val)
    grid.addWidget(title, 0, 0, 1, 2)
    grid.addWidget(plan, 0, 2)
    grid.addWidget(refresh, 0, 3)
    grid.setColumnStretch(1, 1)

    def update(data: dict):
        info = mon.quota_texts(data)
        if not info.get("ok"):
            plan.setText(tr(T("нет данных — войдите в Codex в «Настройках»", "no data — sign in to Codex in Settings"))
                         if "credentials" in info.get("error", "").lower() else "✗ " + info.get("error", "")[:60])
            for bar, val in bars.values():
                bar.setValue(0)
                val.setText("—")
            return
        src = {"api": "live", "session": "session"}.get(info["source"], info["source"])
        plan.setText(f"{info['plan']} · {src}")
        for key in ("p", "s"):
            pct = info[f"{key}_pct"]
            bar, val = bars[key]
            bar.setValue(pct)
            tone = mon.tone_for_pct(pct)
            color = {"ok": theme.c("green"), "warn": theme.c("yellow"), "err": theme.c("red")}[tone]
            bar.setStyleSheet(f"QProgressBar::chunk {{ background: {color}; border-radius: 4px; }}")
            val.setText(tr(T(f"{pct}% · сброс через {info[key + '_reset']}", f"{pct}% · resets in {info[key + '_reset']}")))

    bind_signal(page.monitors.codex_quota, update, frame)
    return frame


def director_badge(page) -> QWidget:
    box = QWidget()
    lay = QHBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    chip = Chip("OpenAI · Codex", "ok")
    status = QLabel("")
    status.setProperty("role", "muted")
    lay.addWidget(chip)
    lay.addWidget(status)
    lay.addStretch(1)

    def update(data: dict):
        if data and data.get("ok"):
            status.setText(tr(T("вход выполнен", "signed in")))
        else:
            codex = engine_link.cfg("CODEX_EXE_PATH", "")
            status.setText(tr(T("Codex не найден — установите в «Настройках»", "Codex not found — install in Settings"))
                           if not codex else tr(T("нужен вход в Codex («Настройки»)", "sign in to Codex (Settings)")))

    bind_signal(page.monitors.codex_quota, update, box)
    update({})
    return box


# ======================================================= ключи VeoNonStop
class VeoKeyInfo(QWidget):
    """Сведения о ключе VeoNonStop (тариф, занятые слоты) и, для видео, переключатель длительности."""

    def __init__(self, page, title, tip, with_duration: bool = False):
        super().__init__()
        self.page = page
        self.version = 2 if "/api/v2" in str(engine_link.cfg("VEONONSTOP_API_BASE", "") or "") else 1
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(LABEL_GAP)
        lay.addWidget(label(title, "label", tip))
        has_key = bool(str(engine_link.cfg("VEONONSTOP_API_KEY", "") or "").strip())
        self.info = label(T("загружаю данные ключа…", "loading key data…") if has_key else
                          T("Ключ VeoNonStop не задан — «Настройки» → VeoNonStop.",
                            "No VeoNonStop key — Settings → VeoNonStop."),
                          "dim" if has_key else "warn")
        lay.addWidget(self.info)
        self.duration = None
        if with_duration:
            self.duration = Toggle(tr(T("Длительность под сцену (4–8 с)", "Duration per scene (4–8 s)")))
            set_tip(self.duration, T("Просить клип той длины, что нужна сцене: 4, 5, 6 или 8 секунд (ближайшее сверху).\n"
                                     "Без галки клип всегда 8 секунд, лишнее подрезается. Работает в режиме\n"
                                     "«картинка → видео»; в остальных режимах клип 8 секунд.",
                                     "Request a clip as long as the scene needs: 4, 5, 6 or 8 seconds (next up).\n"
                                     "Without it every clip is 8 seconds and trimmed. Works in image-to-video\n"
                                     "mode; other modes always give 8 seconds."))
            self.duration.setChecked(bool(page.store.get("custom_duration")))
            self.duration.toggled.connect(lambda c: page.store.set("custom_duration", bool(c)))
            wrap = QHBoxLayout()
            wrap.addWidget(self.duration)
            wrap.addStretch(1)
            lay.addLayout(wrap)
        page.key_versions["1"] = self.version
        page.monitors.veo_key.connect(self._on_key)
        page.store.changed.connect(self._on_store)
        self._gate_duration()

    def _on_store(self, key, value):
        if key == "custom_duration" and self.duration is not None:
            self.duration.blockSignals(True)
            self.duration.setChecked(bool(value))
            self.duration.blockSignals(False)

    def _on_key(self, slot, text, limit, version):
        if slot != "1":
            return
        self.version = version
        self.page.key_limits[slot] = limit
        self.page.key_versions[slot] = version
        self.info.setText(text)
        self._gate_duration()

    def _gate_duration(self):
        if self.duration is not None:
            self.duration.setEnabled(True)


def banana_quota(page) -> QWidget:
    box = QWidget()
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(2)
    rows: dict[str, QLabel] = {}

    def update(data: dict):
        mod = engine_link.unit("veo_accounts")
        for slot, item in sorted((data or {}).get("slots", {}).items()):
            lab = rows.get(slot)
            if lab is None:
                lab = QLabel()
                lab.setProperty("role", "dim")
                lab.setCursor(Qt.PointingHandCursor)
                lab.mousePressEvent = lambda _e: page.monitors.refresh_veo()
                lay.addWidget(lab)
                rows[slot] = lab
            lab.setText("🍌 " + mod.quota_row_text(item))
            lab.setToolTip(mod.tooltip_text(item))
            tone = "ok"
            if not item.get("ok"):
                tone = "warn" if item.get("error_kind") == "unavailable" else "err"
            elif item.get("image_remaining") is not None and item.get("image_limit"):
                ratio = float(item["image_remaining"]) / max(1.0, float(item["image_limit"]))
                tone = "err" if ratio <= 0.1 else ("warn" if ratio <= 0.25 else "ok")
            lab.setProperty("role", {"ok": "ok", "warn": "warn", "err": "err"}[tone])
            repolish(lab)

    bind_signal(page.monitors.veo_accounts, update, box)
    return box


def img_keys(page) -> QWidget:
    box = QWidget()
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(4)
    lay.addWidget(VeoKeyInfo(page, T("Ключ картинок", "Image key"),
                             T("Ключ VeoNonStop, которым генерируются картинки. Задаётся в «Настройках».",
                               "The VeoNonStop key that generates images. Set in Settings.")))
    lay.addWidget(banana_quota(page))
    return box


def video_keys(page) -> QWidget:
    return VeoKeyInfo(page, T("Ключ видео", "Video key"),
                      T("Ключ VeoNonStop, которым генерируется видео. Задаётся в «Настройках».",
                        "The VeoNonStop key that generates video. Set in Settings."),
                      with_duration=True)


# ========================================================= состав видео
# (ключ, подпись в полосе, подпись в легенде, цвет палитры)
MIX_PARTS = (
    ("mix_stock_video", T("Сток", "Stock"), T("Сток-видео", "Stock video"), "blue"),
    ("mix_stock_photo", T("Фото", "Photo"), T("Сток-фото", "Stock photo"), "amber"),
    ("mix_infographic", T("Инфо", "Info"), T("Инфографика", "Infographics"), "green"),
    ("mix_motion", T("Моушн", "Motion"), T("Моушн-графика", "Motion graphics"), "red"),
    ("ai", T("ИИ", "AI"), T("Нейрогенерация", "AI generation"), "accent"),
)


def source_mix(page) -> QWidget:
    """Полоса долей стока, инфографики и нейрогенерации (остаток) с легендой под ней."""
    box = QWidget()
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(10)
    bar = MixBar()
    tip_active = T("Тяните ручки между цветными частями. Доли — от всех клипов ролика, остаток уходит\n"
                   "в нейрогенерацию. Доли стока и графики — предпочтение, а не квота: режиссёр берёт\n"
                   "их только там, где они подходят сцене, и не больше заданного. Недобор займёт\n"
                   "нейрогенерация. Если её в составе нет, режиссёр сам раскладывает оставшиеся сцены\n"
                   "по подходящим источникам, а сцене, которой не подходит ничего, достаётся кадр соседа.",
                   "Drag the handles between the coloured parts. Shares are of all clips in the video; the\n"
                   "rest goes to AI generation. Stock and graphics shares are a preference, not a quota: the\n"
                   "director uses them only where they suit the scene, and never above the share. AI\n"
                   "generation fills the shortfall. Without AI generation the director assigns the remaining\n"
                   "scenes to the sources that suit them, and a scene nothing suits gets a neighbouring shot.")
    tip_single = T("Весь ролик — нейрогенерация. Включите сток, инфографику или моушн-графику,\n"
                   "чтобы делить клипы между источниками.",
                   "The whole video is AI generation. Turn on stock, infographics or motion graphics\n"
                   "to split the clips between sources.")
    legend = QHBoxLayout()
    legend.setSpacing(8)
    chips = {}
    for key, _short, name, color in MIX_PARTS:
        chip = Chip()
        chip.setStyleSheet(f"QLabel {{ border-color: {theme.c(color)}; }}")
        chip.setTextFormat(Qt.RichText)
        chips[key] = (chip, name, color)
        legend.addWidget(chip)
    legend.addStretch(1)
    lay.addWidget(bar)
    lay.addLayout(legend)

    def show_legend(values: dict) -> None:
        for key, (chip, name, color) in chips.items():
            chip.setText(f"<span style='color:{theme.c(color)}'>●</span>&nbsp; {tr(name)} {values.get(key, 0)}%")

    def render() -> None:
        v = page.store.values
        active = set(mix_active(v)) | {"ai"}
        mix = source_mix_values(v)
        bar.set_parts([(key, short, theme.c(color)) for key, short, _n, color in MIX_PARTS if key in active], mix)
        # одна часть (только нейрогенерация) — делить нечего, ползунок недоступен
        bar.setEnabled(len(active) > 1)
        set_tip(bar, tip_active if len(active) > 1 else tip_single)
        for key, (chip, *_rest) in chips.items():
            chip.setVisible(key in active)
        show_legend(mix)

    def commit(values: dict) -> None:
        page.store.update({k: int(values[k]) for k in MIX_KEYS if k in values})

    bar.moved.connect(show_legend)
    bar.changed.connect(commit)
    watched = set(MIX_KEYS) | {"stock_enabled", "infographic_enabled", "motion_enabled"}
    bind_signal(page.store.changed, lambda k, _v: render() if k in watched else None, box)
    render()
    return box


# ======================================================== моушн-графика
def remotion_status(page) -> QWidget:
    """Состояние Remotion (Node.js, пакеты, браузер для рендера) и кнопка установки.

    Стадия и сама поставит Remotion при первом запуске, но это ~2–3 минуты и
    ~400 МБ — удобнее сделать заранее и сразу увидеть, если нет Node.js."""
    box = QWidget()
    lay = QHBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(10)
    chip = Chip(tr(T("проверяю…", "checking…")))
    install = button(T("Установить Remotion", "Install Remotion"), "small", None, None)
    lay.addWidget(chip)
    lay.addWidget(install)
    lay.addStretch(1)

    def check():
        from helpers import remotion_setup
        return remotion_setup.status()

    def show(st) -> None:
        if isinstance(st, Exception):
            chip.show_state(tr(T("не удалось проверить", "check failed")), "err", str(st))
            return
        if not st.get("node"):
            chip.show_state(tr(T("нет Node.js", "no Node.js")), "err",
                            T("Remotion работает на Node.js. Установите его с nodejs.org (LTS) и перезапустите\n"
                              "программу. Без него клипы графики уйдут в нейрогенерацию.",
                              "Remotion runs on Node.js. Install it from nodejs.org (LTS) and restart the app.\n"
                              "Without it, graphics clips go to AI generation."))
            install.setEnabled(False)
        elif st.get("ready"):
            chip.show_state(f"Remotion ✓ · Node {st['node']}", "ok",
                            T(f"Установлен в {st['workspace']}", f"Installed in {st['workspace']}"))
            install.setText(tr(T("Переустановить", "Reinstall")))
            install.setEnabled(True)
        else:
            chip.show_state(tr(T("Remotion не установлен", "Remotion not installed")), "warn",
                            T("Поставится сам при первом запуске (~2–3 мин, ~400 МБ) или кнопкой рядом.",
                              "Installs itself on the first run (~2–3 min, ~400 MB) or with the button."))
            install.setEnabled(True)

    progress = _Relay()
    progress.done.connect(lambda text: chip.show_state(text, "warn"))

    def run_install():
        install.setEnabled(False)
        chip.show_state(tr(T("устанавливаю…", "installing…")), "warn")
        page.log(tr(T("Remotion: установка…", "Remotion: installing…")), "INFO")

        def work():
            proc = procs.popen([paths.python_exe(), str(paths.REMOTION_SETUP)], str(paths.ENGINE),
                               dict(os.environ, PYTHONIOENCODING="utf-8"))
            last = ""
            for raw in proc.stdout:
                line = raw.strip()
                if line.startswith("STEP:"):
                    progress.done.emit(line[5:].strip())
                if line:
                    last = line
            return proc.wait() == 0, last

        def finished(result):
            ok, last = result if isinstance(result, tuple) else (False, str(result))
            page.log(f"Remotion: {last}", "SUCCESS" if ok else "ERROR")
            run_bg(check, show)

        run_bg(work, finished)

    install.clicked.connect(run_install)
    run_bg(check, show)
    return box
