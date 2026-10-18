"""Установка код-обновления после выхода программы (запускается из runtime/updates).

    python apply_update.py --app <папка программы> --stage <распакованное обновление> --pid <pid> --log <файл>

Только стандартная библиотека: модули программы в это время заменяются.
Порядок: дождаться выхода программы → заменённые и удалённые файлы переложить в
runtime/updates/backup → разложить новые → записать manifest.json → запустить программу.
При любой ошибке всё возвращается из backup и запускается прежняя версия.
Данные пользователя (projects, inbox, runtime, userdata, engine/.env, промпты) не трогаются.
"""
from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROTECTED = ("projects/", "inbox/", "runtime/", "userdata/", "python/", "engine/.env",
             "engine/instructions/", "engine/templates/", "engine/logs/", "manifest.json")

LOG: Path | None = None


def say(text: str) -> None:
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {text}"
    if LOG:
        try:
            LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(LOG, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


def safe(rel: str) -> bool:
    """Путь внутри программы и не из данных пользователя (папки — по префиксу, файлы — точно)."""
    rel = rel.replace("\\", "/")
    return bool(rel) and not rel.startswith("/") and ".." not in rel.split("/") and ":" not in rel \
        and not any(rel == p.rstrip("/") or (p.endswith("/") and rel.startswith(p)) for p in PROTECTED)


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def wait_pid(pid: int, timeout: float = 180) -> None:
    """Дождаться выхода процесса программы."""
    if sys.platform != "win32" or pid <= 0:
        time.sleep(3)
        return
    SYNCHRONIZE = 0x00100000
    handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if handle:
        ctypes.windll.kernel32.WaitForSingleObject(handle, int(timeout * 1000))
        ctypes.windll.kernel32.CloseHandle(handle)
    time.sleep(1.0)


def move_retry(src: Path, dst: Path, tries: int = 40) -> None:
    """Переместить файл; если он ещё занят (антивирус, догорающий процесс) — подождать."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    for i in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.5)


def load_manifest(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def apply(app: Path, stage: Path, backup: Path, done: list[tuple[str, str]]) -> None:
    """Разложить файлы. `done` — журнал действий для отката (действие, путь); пишется по ходу,
    чтобы при сбое посередине откатить уже сделанное."""
    new = load_manifest(stage / "manifest.json")
    old = load_manifest(app / "manifest.json")
    new_files: dict = new.get("files", {})
    old_files: dict = old.get("files", {})

    # 1) файлы, которых в новой версии нет, — в backup
    for rel in sorted(set(old_files) - set(new_files)):
        if not safe(rel):
            continue
        target = app / rel
        if target.is_file():
            move_retry(target, backup / rel)
            done.append(("moved", rel))

    # 2) новые и изменённые файлы
    for rel, digest in sorted(new_files.items()):
        if not safe(rel):
            say(f"skip protected path {rel}")
            continue
        target = app / rel
        if target.is_file():
            if sha(target) == digest:
                continue
            move_retry(target, backup / rel)
            done.append(("moved", rel))
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".new")
        shutil.copy2(stage / rel, tmp)
        move_retry(tmp, target)
        done.append(("added", rel))

    # 3) манифест — последним: по нему следующая версия поймёт, что установлено
    if (app / "manifest.json").is_file():
        shutil.copy2(app / "manifest.json", backup / "manifest.json")
    tmp = app / "manifest.json.new"
    shutil.copy2(stage / "manifest.json", tmp)
    move_retry(tmp, app / "manifest.json")


def rollback(app: Path, backup: Path, done: list[tuple[str, str]]) -> None:
    for action, rel in reversed(done):
        try:
            target = app / rel
            if action == "added" and target.exists():
                target.unlink()
            if (backup / rel).exists():
                move_retry(backup / rel, target)
        except Exception as exc:  # noqa: BLE001
            say(f"rollback {rel}: {exc}")
    if (backup / "manifest.json").exists():
        shutil.copy2(backup / "manifest.json", app / "manifest.json")


def relaunch(app: Path) -> None:
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    launcher = app / "BegoFrame.exe"
    if launcher.exists():
        subprocess.Popen([str(launcher)], cwd=str(app), creationflags=flags, close_fds=True)
        return
    entry = app / "bego.pyc" if (app / "bego.pyc").exists() else app / "bego.py"
    pyw = app / "python" / "pythonw.exe"
    subprocess.Popen([str(pyw if pyw.exists() else sys.executable), str(entry)], cwd=str(app),
                     creationflags=flags, close_fds=True)


def main() -> int:
    global LOG
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", required=True)
    ap.add_argument("--stage", required=True)
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--log", default="")
    ap.add_argument("--no-relaunch", action="store_true")
    a = ap.parse_args()
    LOG = Path(a.log) if a.log else None
    app, stage = Path(a.app).resolve(), Path(a.stage).resolve()
    version = load_manifest(stage / "manifest.json").get("version", "?")
    say(f"update to {version}: waiting for pid {a.pid}")
    wait_pid(a.pid)

    old_version = load_manifest(app / "manifest.json").get("version", "old")
    backup = stage.parent.parent / f"backup-{old_version}"
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)
    backup.mkdir(parents=True, exist_ok=True)
    done: list[tuple[str, str]] = []
    ok = False
    try:
        apply(app, stage, backup, done)
        ok = True
        say(f"update to {version}: {len(done)} file operations, OK")
    except Exception as exc:  # noqa: BLE001
        say(f"update to {version} FAILED: {exc!r}; rolling back")
        rollback(app, backup, done)
    if ok:
        shutil.rmtree(stage, ignore_errors=True)
        # оставляем только последний backup — для ручного отката
        for item in stage.parent.parent.glob("backup-*"):
            if item != backup:
                shutil.rmtree(item, ignore_errors=True)
    if not a.no_relaunch:
        relaunch(app)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
