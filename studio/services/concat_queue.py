"""Фоновая очередь склейки («хвост» конвейера).

Голова конвейера (транскрипция → режиссура → картинки → видео) работает с
сетью, хвост (склейка) — с видеокартой и диском. Хвост уходит сюда, и очередь
генерации сразу берёт следующий файл. Воркер строго один: две склейки дерутся
за кодировщик и вместе медленнее, чем по очереди.

Настройки склейки читаются с диска (pipeline_config.json → concat), поэтому
очередь не зависит от того, что сейчас выставлено в интерфейсе.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Signal

from .. import paths
from ..pipeline.runplan import concat_command
from . import procs
from .readiness import load_project_config, wait_ready

_PCT_RE = re.compile(r"(?:кодирование|Сборка|encoding|Assembly):\s*(\d+)%", re.I)
_NM_RE = re.compile(r"PROGRESS:(\d+)/(\d+)")


class ConcatQueue(QObject):
    """Очередь склеек. Сигналы приходят в поток интерфейса автоматически."""

    changed = Signal()
    log = Signal(str, str)
    job_finished = Signal(str, str)      # имя проекта, статус

    def __init__(self):
        super().__init__()
        self._jobs: list[dict] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._shutdown = False
        self._proc = None
        self._cancel_evt = None
        self._next_id = 1
        threading.Thread(target=self._loop, daemon=True, name="concat-queue").start()

    # ------------------------------------------------------------ API
    def enqueue(self, project: str) -> int:
        job = {"id": 0, "project": str(project), "name": Path(project).name, "status": "pending",
               "step": "в очереди / queued", "pct": None, "error": "", "started": None, "finished": None}
        with self._lock:
            if self._shutdown:
                raise RuntimeError("queue is shut down")
            job["id"] = self._next_id
            self._next_id += 1
            self._jobs.append(job)
        self._wake.set()
        self.changed.emit()
        return job["id"]

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [dict(j) for j in self._jobs]

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for j in self._jobs if j["status"] in ("pending", "running"))

    def cancel(self, job_id: int) -> None:
        proc = None
        with self._lock:
            for j in self._jobs:
                if j["id"] != job_id:
                    continue
                if j["status"] == "pending":
                    j.update(status="cancelled", step="отменено / cancelled", finished=time.time())
                elif j["status"] == "running":
                    if self._cancel_evt is not None:
                        self._cancel_evt.set()
                    proc = self._proc
        if proc is not None:
            procs.kill_tree(proc)
        self.changed.emit()

    def cancel_running(self) -> bool:
        running = next((j for j in self.snapshot() if j["status"] == "running"), None)
        if running:
            self.cancel(running["id"])
            return True
        return False

    def clear_finished(self) -> None:
        with self._lock:
            self._jobs = [j for j in self._jobs if j["status"] in ("pending", "running")]
        self.changed.emit()

    def shutdown(self) -> None:
        """При закрытии окна новые задачи не берём; текущая склейка доработает сама."""
        with self._lock:
            self._shutdown = True
        self._wake.set()

    # --------------------------------------------------------- воркер
    def _set(self, job: dict, **kw) -> None:
        with self._lock:
            job.update(kw)
        self.changed.emit()

    def _next(self):
        with self._lock:
            if self._shutdown:
                return None
            return next((j for j in self._jobs if j["status"] == "pending"), None)

    def _loop(self) -> None:
        while True:
            self._wake.wait(timeout=2)
            self._wake.clear()
            with self._lock:
                if self._shutdown:
                    return
            while True:
                job = self._next()
                if job is None:
                    break
                self._run(job)

    def _run(self, job: dict) -> None:
        project = Path(job["project"])
        cancel_evt = threading.Event()
        with self._lock:
            job.update(status="running", started=time.time(), step="подготовка / preparing")
            self._cancel_evt = cancel_evt
        self.changed.emit()
        handles = []
        try:
            paths.USERDATA.mkdir(parents=True, exist_ok=True)
            handles.append(open(paths.CONCAT_LOG, "a", encoding="utf-8", buffering=1))
            stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            handles.append(open(project / f"stage_ConcatQueue_{stamp}.log", "w", encoding="utf-8", buffering=1))
            handles.append(open(project / "pipeline_full.log", "a", encoding="utf-8", buffering=1))
        except OSError:
            pass

        def write(line: str, level: str = "INFO") -> None:
            self.log.emit(f"[{job['name']}] {line}", level)
            for fh in handles:
                try:
                    fh.write(line + "\n")
                except Exception:
                    pass

        status = "failed"
        try:
            write("── Очередь склейки: старт / concat queue: start ──")
            self._set(job, step="проверка готовности / readiness check")
            wait_ready(project, write, cancel=cancel_evt.is_set, terminal=True)
            section = load_project_config(project).get("concat") or {}
            if not section:
                write("Слепок concat не найден — берутся значения по умолчанию / no concat snapshot, using defaults",
                      "WARNING")
                section = {"render_mode": "720p", "bitrate": "20M", "fps": 60, "aspect": "16:9"}
            env = os.environ.copy()
            env.update({"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "TQDM_DISABLE": "1",
                        "ZOOM_SUPERSAMPLE": str(section.get("zoom_supersample", 2) or 2)})
            cmd = concat_command(project, section, paths.python_exe())
            if cancel_evt.is_set():
                raise RuntimeError("cancelled")
            self._set(job, step="склейка / rendering", pct=0)
            write(f"── Concat: {job['name']} ({section.get('render_mode')}, {section.get('bitrate')}) ──")
            proc = procs.popen(cmd, str(project), env)
            with self._lock:
                self._proc = proc
            last_emit = 0.0
            try:
                for raw in proc.stdout:
                    if cancel_evt.is_set():
                        procs.kill_tree(proc)
                        break
                    line = raw.rstrip()
                    if not line:
                        continue
                    pct = None
                    m = _NM_RE.match(line)
                    if m:
                        c, t = int(m.group(1)), int(m.group(2))
                        if 0 <= c <= t and t > 0:
                            pct = int(c * 100 / t)
                    else:
                        m2 = _PCT_RE.search(line)
                        if m2:
                            pct = min(100, int(m2.group(1)))
                    if pct is not None:
                        with self._lock:
                            job["pct"] = pct
                        if time.time() - last_emit > 0.5:
                            last_emit = time.time()
                            self.changed.emit()
                        continue
                    write(line, procs.classify(line))
            except OSError:
                pass
            finally:
                proc.wait()
                with self._lock:
                    self._proc = None
            rc = proc.returncode
            if cancel_evt.is_set() or (rc is not None and rc < 0):
                status = "cancelled"
                self._set(job, status=status, step="отменено / cancelled", finished=time.time())
                write("── Склейка отменена / cancelled ──", "WARNING")
            elif rc == 0:
                status = "done"
                took = int(time.time() - (job["started"] or time.time()))
                m, s = divmod(took, 60)
                self._set(job, status=status, pct=100, step=f"готово за {m}м {s:02d}с / done in {m}m {s:02d}s",
                          finished=time.time())
                write(f"── Склейка готова / done: {job['name']} ──", "SUCCESS")
            else:
                self._set(job, status="failed", step=f"ошибка (exit {rc}) / error", error=f"exit {rc}",
                          finished=time.time())
                write(f"── Склейка упала / failed: exit {rc} ──", "ERROR")
        except Exception as exc:  # noqa: BLE001
            if cancel_evt.is_set() or str(exc) == "cancelled":
                status = "cancelled"
                self._set(job, status=status, step="отменено / cancelled", finished=time.time())
            else:
                self._set(job, status="failed", step="ошибка / error", error=str(exc)[:200], finished=time.time())
                write(f"── Ошибка очереди склейки / concat error: {exc} ──", "ERROR")
        finally:
            with self._lock:
                self._cancel_evt = None
                self._proc = None
            for fh in handles:
                try:
                    fh.close()
                except Exception:
                    pass
            self.job_finished.emit(job["name"], status)
            self.changed.emit()
