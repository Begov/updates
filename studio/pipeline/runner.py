"""Оркестратор очереди конвейера.

Очередь — это серия одиночных запусков: для каждого файла берётся его слепок
настроек (свой ★-снимок, пресет или текущие значения формы на момент старта
этого файла), собираются фазы и запускается движок. Файлы можно добавлять в
очередь во время работы — оркестратор подхватит их после текущего.

Изменения формы во время прогона применяются к текущему файлу на ходу (если он идёт по
форме, а не по ★-снимку или пресету): генерация перезапускается с места маршрутом
«Довести проект», склейка берёт новые параметры, настройки режиссуры после готового
плана действуют со следующего файла (см. `PipelineRunner.update_settings`).
"""
from __future__ import annotations

import concurrent.futures as cf
import copy
import json
import os
import re
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, Qt, Signal, Slot

from .. import paths
from ..i18n import T, tr
from ..services import procs
from ..services.readiness import upsert_concat, wait_ready
from .runplan import (FileBinding, apply_env, build_run, common_env, concat_command, concat_section,
                      generation_inputs, live_change, post_action_for)

STAGE_MAP = [
    ("STAGE 1", 1), ("transcrib", 1), ("транскрипц", 1), ("загрузка аудио", 1),
    ("STAGE 2", 2), ("direction", 2), ("director", 2), ("режиссерск", 2), ("монтажный план", 2),
    ("проход 1/2", 2), ("отправка запроса", 2),
    ("STAGE 3:", 3), ("image gen", 3), ("fastgen", 3), ("imggen", 3),
    ("STAGE 3.5", 4), ("video gen", 4), ("veo_video", 4), ("img2video", 4), ("i2v", 4),
    ("STAGE 4", 5), ("concat", 5), ("assemble", 5),
]
_RATIO_RE = re.compile(r"\b(\d+)\s*/\s*(\d+)\b")
_PROGRESS_RE = re.compile(r"PROGRESS:(\d+)/(\d+)")


class _Invoker(QObject):
    """Выполнить функцию в потоке интерфейса и вернуть результат рабочему потоку."""

    call = Signal(object)

    def __init__(self):
        super().__init__()
        self.call.connect(self._run, Qt.QueuedConnection)

    @Slot(object)
    def _run(self, job) -> None:
        fn, box, event = job
        try:
            box["result"] = fn()
        except Exception as exc:  # noqa: BLE001
            box["error"] = exc
        finally:
            event.set()

    def run(self, fn: Callable, timeout: float = 30):
        box: dict = {}
        event = threading.Event()
        self.call.emit((fn, box, event))
        if not event.wait(timeout):
            raise RuntimeError("GUI thread did not answer")
        if "error" in box:
            raise box["error"]
        return box.get("result")


@dataclass
class _Live:
    """Настройки файла, который идёт сейчас, и где он находится — для применения изменений на ходу."""
    values: dict                    # действующие настройки (последние применённые)
    live: bool                      # файл идёт по форме (не ★-снимок и не пресет) — изменения применяются
    project: str = ""               # папка проекта (движок сообщает PROJECT_DIR)
    phase: str = "prep"             # prep (расшифровка, режиссура) → generation → done (передан в склейку)
    restart: bool = False           # процесс генерации убит ради перезапуска с новыми настройками
    pending: bool = False           # перезапуск ждёт конца режиссуры
    running_gen: dict = field(default_factory=dict)   # с чем работает генерация (`generation_inputs`)
    lock: threading.Lock = field(default_factory=threading.Lock)


# Подписи изменений, которые стоит показать в журнале словами
_LIVE_LABELS = {
    "img_gen": T("Картинки", "Images"), "video_provider": T("Видео", "Video"), "video_mode": T("Режим видео", "Video mode"),
    "infographic_provider": T("Инфографика", "Infographics"), "veo_ratio": T("Формат", "Format"),
}


class PipelineRunner(QObject):
    log = Signal(str, str)
    stage = Signal(int)
    task = Signal(int, int)
    current = Signal(str, int, int)
    spinner = Signal(str)
    running = Signal(bool)
    item_status = Signal(str, str)       # имя файла, статус (running/done/failed/cancelled)
    finished = Signal(bool, bool, int)   # успех, отменено, сколько файлов

    def __init__(self, take_next: Callable, concat_queue):
        super().__init__()
        self._take_next = take_next
        self._concat_queue = concat_queue
        self._invoker = _Invoker()
        self._cancel = threading.Event()
        self._proc = None
        self._thread: Optional[threading.Thread] = None
        self._live: Optional[_Live] = None

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self, files: list[Path]) -> None:
        if self.is_running():
            return
        self._cancel.clear()
        # Изолированная папка входящих: от прошлого запуска ничего не остаётся
        if paths.INBOX.exists():
            shutil.rmtree(paths.INBOX, ignore_errors=True)
        paths.INBOX.mkdir(parents=True, exist_ok=True)
        for src in files:
            try:
                shutil.copy2(src, paths.INBOX / src.name)
            except OSError as exc:
                self.log.emit(f"{src.name}: {exc}", "ERROR")
        self._thread = threading.Thread(target=self._worker, daemon=True, name="pipeline")
        self._thread.start()
        self.running.emit(True)

    def stop(self, cancel_server: bool = True) -> None:
        """Остановить конвейер: убить процесс движка со всеми дочерними и снять серверные
        задачи всех провайдеров (`cancel_server=False` — только процессы)."""
        self._cancel.set()
        procs.kill_tree(self._proc, force=True)
        self.log.emit(tr(T("Конвейер остановлен, снимаю серверные задачи…",
                           "Pipeline stopped, cancelling server tasks…")), "WARNING")
        if cancel_server:
            start_cancel_all(lambda line: self.log.emit(line, procs.classify(line)))

    # ------------------------------------------------- изменения настроек на ходу
    def update_settings(self, values: dict) -> None:
        """Форма изменилась во время прогона (зовётся из потока интерфейса).

        Генерацию текущего файла перезапускает с места с новыми настройками; склейке этого
        файла отдаёт новые параметры склейки; настройки режиссуры после готового плана
        действуют со следующего файла. Файлы со ★-снимком или пресетом не трогаются.
        """
        st = self._live
        if st is None or not self.is_running() or self._cancel.is_set():
            return
        with st.lock:
            keys, gen, cat, direction = live_change(st.values, values)
            if not (gen or cat or direction):
                return
            if not st.live:
                self.log.emit(tr(T("Настройки изменены, но у текущего файла свои (★ или пресет) — "
                                   "они применятся к следующим файлам.",
                                   "Settings changed, but the current file has its own (★ or preset) — "
                                   "they apply to the next files.")), "WARNING")
                return
            old, st.values = st.values, copy.deepcopy(values)
            phase, project = st.phase, st.project
            # перезапуск нужен, только если процесс работает не с тем, что теперь в форме
            # (переключили и вернули обратно — перезапуска нет)
            gen = generation_inputs(values) != st.running_gen
            if phase == "prep":
                st.pending = gen
        self.log.emit(tr(T("Настройки изменены: ", "Settings changed: ")) + self._describe(old, values, keys), "WARNING")
        if cat:
            if phase == "done" and project:
                try:
                    upsert_concat(Path(project), concat_section(values, str(values.get("logo_path") or "")))
                except Exception as exc:  # noqa: BLE001
                    self.log.emit(f"Concat settings: {exc}", "WARNING")
                self.log.emit(tr(T("  склейка этого файла возьмёт новые параметры, если ещё не началась",
                                   "  this file's render takes the new settings unless it has already started")),
                              "INFO")
            else:
                self.log.emit(tr(T("  склейка этого файла пойдёт с новыми параметрами",
                                   "  this file will be rendered with the new settings")), "INFO")
        if direction and not gen and phase != "prep":
            self.log.emit(tr(T("  план этого файла уже готов — настройки режиссуры действуют со следующего файла",
                               "  this file's plan is ready — direction settings apply from the next file")), "INFO")
        if not gen:
            return
        if phase == "prep":
            self.log.emit(tr(T("  идёт режиссура — генерация начнётся сразу с новыми настройками",
                               "  direction is running — generation will start with the new settings")), "INFO")
        elif phase == "generation":
            self._restart_generation()
        else:
            self.log.emit(tr(T("  генерация этого файла уже закончена — новые настройки со следующего файла",
                               "  this file's generation is finished — the new settings apply from the next file")),
                          "INFO")

    @staticmethod
    def _describe(old: dict, new: dict, keys: list[str]) -> str:
        parts = [f"{tr(_LIVE_LABELS[k])}: {old.get(k)} → {new.get(k)}" for k in keys if k in _LIVE_LABELS]
        rest = [k for k in keys if k not in _LIVE_LABELS]
        if rest:
            parts.append(", ".join(rest[:8]) + ("…" if len(rest) > 8 else ""))
        return "; ".join(parts)

    def _restart_generation(self) -> None:
        """Убить процесс генерации; воркер снимет задачи и продолжит с места с новыми настройками."""
        st = self._live
        with st.lock:
            if st.phase != "generation" or st.restart:
                return
            st.restart = True
        self.log.emit(tr(T("  перезапускаю генерацию с места: процесс останавливается, задачи в работе снимаются",
                           "  restarting generation in place: stopping the process, cancelling running tasks")),
                      "WARNING")
        procs.kill_tree(self._proc, force=True)

    def _cancel_server_tasks_sync(self) -> None:
        """Снять серверные задачи и ДОЖДАТЬСЯ этого: новые задачи не должны попасть под снятие."""
        import subprocess

        from ..pages.control_page import stage_job
        try:
            out = subprocess.run(stage_job("cancel_all", {}), cwd=str(paths.ENGINE), capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=300,
                                 env={**os.environ, "PYTHONIOENCODING": "utf-8"}, creationflags=procs.CREATE_NO_WINDOW)
            for line in (out.stdout or "").splitlines():
                if line.strip():
                    self.log.emit("  " + line.strip(), procs.classify(line))
        except Exception as exc:  # noqa: BLE001
            self.log.emit(f"Cancel tasks: {exc}", "WARNING")

    def _run_generation(self, on_line: Callable[[str], None]) -> int:
        """Генерация проекта с места — маршрутом «Довести проект» с текущими настройками."""
        st = self._live
        from .stages import finish_settings
        with st.lock:
            v, project = copy.deepcopy(st.values), st.project
            st.running_gen = generation_inputs(v)
        settings = finish_settings(v, Path(project), ["Generate"])
        fd, path = tempfile.mkstemp(prefix="bego_finish_", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(settings, fh, ensure_ascii=False)
        env = os.environ.copy()
        env.update({"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "TQDM_DISABLE": "1"})
        apply_env(env, common_env(v, post_action_for(v), "pipeline"))
        self.log.emit(tr(T(f"── Генерация с места с новыми настройками: {Path(project).name} ──",
                           f"── Generation resumed with the new settings: {Path(project).name} ──")), "INFO")
        try:
            return procs.run_streamed([paths.python_exe(), str(paths.FINISH_RUNNER), path], str(paths.ENGINE), env,
                                      on_line, self._cancel.is_set, on_start=lambda p: setattr(self, "_proc", p))
        finally:
            self._proc = None
            Path(path).unlink(missing_ok=True)

    # ---------------------------------------------------------------- воркер
    def _worker(self) -> None:
        processed: set = set()
        ok_all = True
        count = 0
        try:
            while not self._cancel.is_set():
                item = self._invoker.run(lambda: self._take_next(set(processed)))
                if not item:
                    break
                source, snapshot, binding, tag, live = item
                processed.add(source.name)
                count += 1
                self.log.emit(f"══ {tr(T('Очередь', 'Queue'))} {count}: {source.name} ({tag}) ══", "INFO")
                inbox_copy = paths.INBOX / source.name
                if not inbox_copy.exists() and source.exists():
                    shutil.copy2(source, inbox_copy)
                self.item_status.emit(source.name, "running")
                ok = self._run_one(source, snapshot, binding, tag, live)
                self.item_status.emit(source.name, "cancelled" if self._cancel.is_set() else ("done" if ok else "failed"))
                ok_all = ok_all and ok
        except Exception as exc:  # noqa: BLE001
            ok_all = False
            self.log.emit(f"Queue orchestrator error: {exc}", "ERROR")
        finally:
            self._live = None
            self.spinner.emit("")
            self.current.emit("", 0, 0)
            self.running.emit(False)
            self.finished.emit(ok_all and not self._cancel.is_set(), self._cancel.is_set(), count)

    def _run_one(self, source: Path, v: dict, binding: FileBinding, tag: str, live: bool = True) -> bool:
        try:
            plan = build_run(v, source, binding, paths.python_exe(), tag)
        except Exception as exc:  # noqa: BLE001
            self.log.emit(f"Run plan error: {exc}", "ERROR")
            return False
        st = self._live = _Live(values=copy.deepcopy(v), live=live, running_gen=generation_inputs(v))
        self.log.emit("━" * 60, "INFO")
        for line in plan.summary:
            self.log.emit("  " + line, "INFO")
        for i, (args, _env) in enumerate(plan.phases, 1):
            self.log.emit(f"  [{i}] {' '.join(Path(a).name if os.sep in a else a for a in args[:4])} …", "DIM")
        self.log.emit("━" * 60, "INFO")
        self.stage.emit(0)

        failed = False
        concat_pool = cf.ThreadPoolExecutor(max_workers=1)
        concat_futs = []
        handed = [0]
        eta = {"stage": 0, "start": None, "base": 0}
        queue_total = 1
        queue_index = 0

        def do_concat(project: str) -> bool:
            proj = Path(project)
            try:
                wait_ready(proj, lambda m, l="INFO": self.log.emit(m, l), cancel=self._cancel.is_set, terminal=True)
            except Exception as exc:  # noqa: BLE001
                self.log.emit(f"Concat guard: {exc}", "ERROR")
                return False
            self.stage.emit(5)
            with st.lock:                                    # параметры склейки — последние из формы
                cv = copy.deepcopy(st.values)
            section = concat_section(cv, str(cv.get("logo_path") or ""))
            cmd = concat_command(proj, section, paths.python_exe())
            env = apply_env(dict(plan.env), common_env(cv, post_action_for(cv), tag))
            self.log.emit(f"── Concat: {proj.name} ({section['render_mode']}, {section['bitrate']}) ──", "INFO")
            with open(proj / "pipeline_full.log", "a", encoding="utf-8") as full_log:
                def on_line(line: str) -> None:
                    m = _PROGRESS_RE.match(line)
                    if m:
                        self.task.emit(int(m.group(1)), int(m.group(2)))
                        return
                    pct = re.search(r"(?:кодирование|Сборка):\s*(\d+)%", line)
                    if pct:
                        self.task.emit(int(pct.group(1)), 100)
                    self.log.emit(line, procs.classify(line))
                    full_log.write(line + "\n")
                rc = procs.run_streamed(cmd, str(proj), env, on_line, self._cancel.is_set,
                                        on_start=lambda p: setattr(self, "_proc", p))
            return rc == 0

        def hand_off(project: str) -> None:
            with st.lock:
                st.phase = "done"
                cv = copy.deepcopy(st.values)
            self._hand_off(project, cv, do_concat, concat_pool, concat_futs, handed)

        def on_line(line: str) -> None:
            nonlocal queue_total, queue_index
            stripped = line.strip()
            if stripped and stripped[0] in procs.SPINNER_CHARS and "✅" not in line:
                self.spinner.emit(stripped)
                m = _RATIO_RE.search(stripped)
                if m and 0 < int(m.group(2)) <= 50000 and int(m.group(1)) <= int(m.group(2)):
                    self.task.emit(int(m.group(1)), int(m.group(2)))
                return
            if "✅" in line:
                self.spinner.emit("")
            if line.startswith("PROJECT_DIR:"):
                with st.lock:
                    st.project = line.split(":", 1)[1].strip()
                return
            if line.startswith("DIRECTION_DONE:"):
                with st.lock:
                    st.phase = "generation"
                    restart = st.pending
                    st.pending = False
                if restart:                                  # настройки менялись во время режиссуры
                    self._restart_generation()
                return
            if line.startswith("PROJECT_DONE:"):
                project = line.split(":", 1)[1].strip()
                if project:
                    hand_off(project)
                return
            if line.startswith("QUEUE_UPDATE:"):
                m = re.match(r"QUEUE_UPDATE:\s*(\d+)", line)
                if m:
                    queue_total = int(m.group(1))
                return
            if "ПАЙПЛАЙН:" in line or "PIPELINE:" in line:
                queue_index += 1
                m = re.search(r"(?:ПАЙПЛАЙН|PIPELINE):\s*(.+)", line)
                self.current.emit(m.group(1).strip() if m else "", queue_index, max(queue_total, queue_index))
            m = _PROGRESS_RE.match(line)
            if m:
                cur, total = int(m.group(1)), int(m.group(2))
                if 0 <= cur <= total <= 50000:
                    self.task.emit(cur, total)
                    self._eta(eta, cur, total)
                return
            low = line.lower()
            for key, idx in STAGE_MAP:
                if key.lower() in low:
                    if idx != eta["stage"]:
                        eta.update(stage=idx, start=None, base=0)
                    self.stage.emit(idx)
                    break
            m = _RATIO_RE.search(line)
            if m and eta["stage"] not in (2, 3, 4):
                cur, total = int(m.group(1)), int(m.group(2))
                if 0 < total <= 50000 and cur <= total:
                    self.task.emit(cur, total)
            self.log.emit(line, procs.classify(line))

        for index, (args, env) in enumerate(plan.phases, 1):
            if self._cancel.is_set():
                break
            if len(plan.phases) > 1:
                self.log.emit(f"── Phase {index}/{len(plan.phases)}: {Path(args[1]).name} ──", "INFO")
            rc = procs.run_streamed(args, str(paths.ROOT), env, on_line, self._cancel.is_set,
                                    on_start=lambda p: setattr(self, "_proc", p))
            self._proc = None
            # Настройки менялись во время генерации: процесс убит ради перезапуска. Снимаем его
            # задачи, затем продолжаем тот же проект с места с новыми настройками — столько раз,
            # сколько настройки менялись.
            while not self._cancel.is_set():
                with st.lock:
                    if not st.restart:
                        break
                    st.restart = False
                self._cancel_server_tasks_sync()
                if self._cancel.is_set():
                    break
                rc = self._run_generation(on_line)
                with st.lock:
                    again = st.restart
                if not again and rc == 0 and not self._cancel.is_set():
                    hand_off(st.project)
            if rc != 0 and not self._cancel.is_set():
                self.log.emit(tr(T(f"Фаза завершилась с ошибкой (код {rc})", f"Phase failed (exit code {rc})")), "ERROR")
                failed = True
                break

        self.spinner.emit("")
        concat_pool.shutdown(wait=True)
        for fut in concat_futs:
            try:
                if not fut.result():
                    failed = True
            except Exception as exc:  # noqa: BLE001
                self.log.emit(f"Concat error: {exc}", "ERROR")
                failed = True
        if self._cancel.is_set():
            self.log.emit(tr(T("Конвейер остановлен.", "Pipeline cancelled.")), "WARNING")
            return False
        if not failed:
            if handed[0]:
                self.log.emit(tr(T("Голова конвейера готова — склейка идёт в фоновой очереди (Панель управления).",
                                   "Pipeline head finished — rendering continues in the background queue "
                                   "(Control Panel).")), "SUCCESS")
            else:
                self.log.emit(tr(T("Конвейер завершён!", "Pipeline complete!")), "SUCCESS")
            self.stage.emit(6)
        return not failed

    def _hand_off(self, project: str, v: dict, do_concat, pool, futs, handed) -> None:
        """Проект готов к склейке: в фоновую очередь или сразу."""
        if v.get("concat_queue_enabled") and self._concat_queue is not None:
            try:
                upsert_concat(Path(project), concat_section(v, str(v.get("logo_path") or "")))
                self._concat_queue.enqueue(project)
                handed[0] += 1
                self.log.emit(tr(T(f"── Проект передан в очередь склейки: {Path(project).name} ──",
                                   f"── Project handed to the concat queue: {Path(project).name} ──")), "INFO")
                return
            except Exception as exc:  # noqa: BLE001
                self.log.emit(f"Concat queue: {exc} — rendering inline", "WARNING")
        futs.append(pool.submit(do_concat, project))

    def _eta(self, eta: dict, cur: int, total: int) -> None:
        if eta["stage"] not in (3, 4) or cur <= 0 or total <= 1:
            return
        now = time.time()
        if eta["start"] is None:
            eta["start"], eta["base"] = now, cur - 1
        done = cur - eta["base"]
        elapsed = now - eta["start"]
        if done > 0 and elapsed > 0:
            avg = elapsed / done
            left = max(0.0, avg * (total - cur))
            m, s = divmod(int(left), 60)
            eta_s = f"~{m}м {s:02d}с" if m else f"~{s}с"
            self.spinner.emit(tr(T(f"Осталось {eta_s}  ({cur}/{total}, ~{avg:.0f} с/клип)",
                                   f"ETA {eta_s}  ({cur}/{total}, ~{avg:.0f} s/clip)")))


def start_cancel_all(on_line: Callable[[str], None] | None = None) -> None:
    """Запустить снятие серверных задач всех провайдеров (stage_runner `cancel_all`).

    Процесс стартует сразу (Popen), вывод читается в фоне: даже если программа тут же
    закрывается, задачи всё равно будут сняты."""
    import subprocess

    from ..pages.control_page import stage_job

    proc = subprocess.Popen(stage_job("cancel_all", {}), cwd=str(paths.ENGINE), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                            env={**os.environ, "PYTHONIOENCODING": "utf-8"}, creationflags=procs.CREATE_NO_WINDOW)

    def pump():
        for line in proc.stdout:
            if on_line and line.strip():
                on_line(line.rstrip())
    threading.Thread(target=pump, daemon=True, name="cancel-all").start()
