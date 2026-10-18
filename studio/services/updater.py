"""Обновление программы из GitHub Releases (репозиторий Begov/updates).

Каждый выпуск — Release `v<версия>` с тремя файлами:
  latest.json                    — версия, что нового, sha256 и размеры файлов, id среды Python;
  BegoFrame-update-<версия>.zip  — только код программы (несколько МБ) + manifest.json;
  BegoFrame-Setup-<версия>.exe   — полный установщик (с Python и библиотеками).

Если среда Python (версия Python + requirements.txt) не менялась, ставится zip: его раскладывает
apply_update.py после выхода программы, затем программа запускается снова. Если менялась —
скачивается установщик и запускается в тихом режиме поверх текущей установки (Inno Setup
сам закрывает и перезапускает программу).

Данные пользователя (projects, inbox, runtime, userdata, engine/.env, свои промпты) не входят
ни в zip, ни в установщик и при обновлении не трогаются.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .. import APP_VERSION, paths

REPO = "Begov/updates"
LATEST_URL = f"https://github.com/{REPO}/releases/latest/download/latest.json"
ASSET_URL = "https://github.com/{repo}/releases/download/v{version}/{file}"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"

# Эти пути обновление не трогает никогда — даже если по ошибке окажутся в манифесте
PROTECTED = ("projects/", "inbox/", "runtime/", "userdata/", "python/", "engine/.env",
             "engine/instructions/", "engine/templates/", "engine/logs/", "manifest.json")

log = logging.getLogger(__name__)


class UpdateError(RuntimeError):
    pass


@dataclass
class Release:
    version: str
    notes: str
    date: str
    runtime: str
    update: dict | None      # {"file", "size", "sha256"}
    installer: dict | None

    @property
    def needs_installer(self) -> bool:
        """Код-обновления мало, если среда Python та же, что установлена."""
        local = installed_manifest()
        return not self.update or not local or local.get("runtime") != self.runtime

    @property
    def asset(self) -> dict:
        asset = self.installer if self.needs_installer else self.update
        if not asset:
            raise UpdateError("В выпуске нет нужного файла")
        return asset

    def url(self, asset: dict) -> str:
        return ASSET_URL.format(repo=REPO, version=self.version, file=asset["file"])


# ------------------------------------------------------------------ версия
def version_tuple(text: str) -> tuple:
    parts = []
    for chunk in str(text).lstrip("vV").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts + [0] * (3 - len(parts)))


def installed_manifest() -> dict | None:
    """Манифест установленной версии; в рабочей копии (git) его нет."""
    try:
        data = json.loads(paths.MANIFEST.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def is_installed() -> bool:
    return installed_manifest() is not None and not (paths.ROOT / ".git").exists()


# ------------------------------------------------------------------ сеть
def _session():
    import requests
    s = requests.Session()
    s.headers["User-Agent"] = f"BegoFrame/{APP_VERSION}"
    return s


def fetch_latest(timeout: float = 15) -> Release:
    try:
        resp = _session().get(LATEST_URL, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        raise UpdateError(f"Нет связи с GitHub: {exc}") from exc
    if resp.status_code == 404:
        raise UpdateError("Выпусков пока нет")
    if resp.status_code != 200:
        raise UpdateError(f"GitHub ответил {resp.status_code}")
    try:
        data = resp.json()
        return Release(version=str(data["version"]), notes=str(data.get("notes") or ""),
                       date=str(data.get("date") or ""), runtime=str(data.get("runtime") or ""),
                       update=data.get("update"), installer=data.get("installer"))
    except (ValueError, KeyError, TypeError) as exc:
        raise UpdateError(f"Повреждён latest.json: {exc}") from exc


def check() -> Release | None:
    """Новый выпуск или None, если стоит последняя версия."""
    rel = fetch_latest()
    return rel if version_tuple(rel.version) > version_tuple(APP_VERSION) else None


def download(rel: Release, on_progress: Callable[[int, int], None] | None = None,
             cancel: threading.Event | None = None) -> Path:
    """Скачать файл выпуска с проверкой размера и sha256. Готовый файл повторно не качается."""
    asset = rel.asset
    folder = paths.UPDATES_DIR / rel.version
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / asset["file"]
    if target.exists() and _sha(target) == asset.get("sha256"):
        return target
    part = target.with_name(target.name + ".part")
    digest = hashlib.sha256()
    total = int(asset.get("size") or 0)
    done = 0
    try:
        with _session().get(rel.url(asset), stream=True, timeout=30) as resp:
            if resp.status_code != 200:
                raise UpdateError(f"Загрузка не удалась: HTTP {resp.status_code}")
            total = int(resp.headers.get("Content-Length") or total)
            with open(part, "wb") as fh:
                for chunk in resp.iter_content(1 << 16):
                    if cancel and cancel.is_set():
                        raise UpdateError("Загрузка отменена")
                    fh.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if on_progress:
                        on_progress(done, total)
    except UpdateError:
        part.unlink(missing_ok=True)
        raise
    except Exception as exc:  # noqa: BLE001
        part.unlink(missing_ok=True)
        raise UpdateError(f"Загрузка прервалась: {exc}") from exc
    if asset.get("sha256") and digest.hexdigest() != asset["sha256"]:
        part.unlink(missing_ok=True)
        raise UpdateError("Контрольная сумма не совпала — файл повреждён, попробуйте ещё раз")
    os.replace(part, target)
    return target


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------- установка
def _safe(rel: str) -> bool:
    """Путь внутри программы и не из данных пользователя (папки — по префиксу, файлы — точно)."""
    rel = rel.replace("\\", "/")
    return bool(rel) and not rel.startswith("/") and ".." not in rel.split("/") and ":" not in rel \
        and not any(rel == p.rstrip("/") or (p.endswith("/") and rel.startswith(p)) for p in PROTECTED)


def _python(windowless: bool) -> str:
    """Python установки (python/), иначе текущий интерпретатор."""
    name = "pythonw.exe" if windowless else "python.exe"
    bundled = paths.ROOT / "python" / name
    return str(bundled) if bundled.exists() else paths.python_exe()


def stage_zip(rel: Release, archive: Path) -> Path:
    """Распаковать код-обновление во временную папку и проверить каждый файл по манифесту."""
    stage = paths.UPDATES_DIR / rel.version / "stage"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    with zipfile.ZipFile(archive) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        for name in names:
            if name != "manifest.json" and not _safe(name):
                raise UpdateError(f"Недопустимый путь в архиве: {name}")
        zf.extractall(stage)
    manifest = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("version") != rel.version:
        raise UpdateError("Версия в архиве не совпадает с выпуском")
    for name, sha in manifest.get("files", {}).items():
        f = stage / name
        if not f.is_file() or _sha(f) != sha:
            raise UpdateError(f"Файл обновления повреждён: {name}")
    return stage


def launch_apply(rel: Release, file: Path) -> None:
    """Запустить установку отдельным процессом; вызывающий после этого закрывает программу."""
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    if file.suffix.lower() == ".exe":
        # Inno Setup: тихо поверх текущей установки, сам закроет программу и запустит её снова
        args = [str(file), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS",
                f"/DIR={paths.ROOT}", "/RELAUNCH=1", f"/LOG={paths.LOGS_DIR / 'update_setup.log'}"]
        subprocess.Popen(args, cwd=str(file.parent), creationflags=flags, close_fds=True)
        return
    stage = stage_zip(rel, file)
    # скрипт установки копируется из программы в runtime/updates: свои файлы он будет заменять
    source = paths.script(Path(__file__).with_name("apply_update.py"))
    runner = paths.UPDATES_DIR / ("apply_update" + source.suffix)
    shutil.copy2(source, runner)
    args = [_python(windowless=True), str(runner), "--app", str(paths.ROOT), "--stage", str(stage),
            "--pid", str(os.getpid()), "--log", str(paths.LOGS_DIR / "update.log")]
    subprocess.Popen(args, cwd=str(paths.UPDATES_DIR), creationflags=flags, close_fds=True)
    log.info("Update %s staged, apply_update started", rel.version)


def cleanup(keep_version: str | None = None) -> None:
    """Убрать старые загрузки (вызывается при старте новой версии)."""
    if not paths.UPDATES_DIR.is_dir():
        return
    for item in paths.UPDATES_DIR.iterdir():
        if item.is_dir() and item.name != keep_version and not item.name.startswith("backup"):
            shutil.rmtree(item, ignore_errors=True)
