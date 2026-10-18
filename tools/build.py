"""Сборка установщика и пакета обновления BegoFrame.

    python tools/build.py              # среда Python + код + BegoFrame.exe + установщик + zip + latest.json
    python tools/build.py --no-setup   # без установщика (быстрее: проверить код-обновление)

Результат — в dist/<версия>/:
    BegoFrame-Setup-<версия>.exe   полный установщик (Inno Setup)
    BegoFrame-update-<версия>.zip  только код + manifest.json (для обновления «на месте»)
    latest.json                    описание выпуска для программы (версия, что нового, sha256)

Версия берётся из studio/__init__.py (APP_VERSION), описание изменений — из CHANGELOG.md
(раздел «## <версия>»). Публикация — tools/release.py.

Среда Python — официальный пакет CPython с NuGet (полный, переносимый) + requirements.txt.
Она кэшируется в build/cache и пересобирается только при смене PYTHON_VERSION или requirements.txt.
От этого же зависит id среды (runtime): если он поменялся, программа обновляется полным
установщиком, иначе — маленьким zip.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
CACHE = BUILD / "cache"
DIST = ROOT / "dist"

PYTHON_VERSION = "3.14.8"      # среда Python установленной программы (NuGet-пакет «python»)
NUGET_URL = "https://api.nuget.org/v3-flatcontainer/python/{v}/python.{v}.nupkg"

# Что из репозитория НЕ попадает в программу
EXCLUDE_EXACT = {"CLAUDE.md", "CHANGELOG.md", ".gitignore", "setup.bat", "start.bat", "errors.json",
                 "requirements.txt", "README.md", "studio/assets/icon.png"}
EXCLUDE_PREFIX = ("tools/", "installer/", "build/", "dist/", ".claude/", ".vscode/",
                  # рабочие промпты и шаблоны — данные пользователя; заводские едут в instructions_default
                  "engine/instructions/", "engine/templates/", "engine/instructions_default/")
# Лишнее из стандартной библиотеки
STDLIB_PRUNE = ("test", "idlelib", "turtledemo", "tkinter", "lib2to3", "ensurepip")
STDLIB_PRUNE_DLL = ("_tkinter.pyd", "tcl86t.dll", "tk86t.dll")
# PySide6 ставится целиком (~650 МБ, с WebEngine, QML, 3D). Программе нужны только эти модули Qt —
# остальные не копируются в сборку. Новый модуль Qt в коде (import PySide6.QtXxx) — добавить сюда.
QT_KEEP = ("Core", "Gui", "Widgets", "Svg", "SvgWidgets", "Xml", "Network", "OpenGL", "OpenGLWidgets",
           "PrintSupport", "Concurrent", "DBus")
QT_PLUGINS_KEEP = ("platforms", "styles", "imageformats", "iconengines", "tls", "networkinformation", "generic",
                   "platforminputcontexts")
QT_DROP_DIRS = ("qml", "resources", "translations", "metatypes", "include", "typesystems", "glue", "scripts",
                "doc", "examples")
QT_DROP_FILES = ("qpdf.dll", "qtvirtualkeyboardplugin.dll")
QT_DROP_PREFIX = ("avcodec", "avformat", "avutil", "swresample", "swscale")

ISCC_CANDIDATES = [
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
    Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
    Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
]
CSC = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe")


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def say(text: str) -> None:
    print(text, flush=True)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def app_version() -> str:
    text = (ROOT / "studio" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', text)
    if not m:
        raise SystemExit("APP_VERSION не найден в studio/__init__.py")
    return m.group(1)


def release_notes(version: str) -> str:
    """Раздел «## <версия>» из CHANGELOG.md."""
    path = ROOT / "CHANGELOG.md"
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    m = re.search(rf"^##\s+{re.escape(version)}\b.*?$(.*?)(?=^##\s|\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else ""


def base_id() -> str:
    """Кэш среды: версия Python + requirements.txt."""
    req = (ROOT / "requirements.txt").read_bytes()
    return hashlib.sha256(PYTHON_VERSION.encode() + b"\0" + req).hexdigest()[:16]


def runtime_id() -> str:
    """Id среды в установленной программе: сменился — обновление идёт полным установщиком."""
    prune = repr((STDLIB_PRUNE, STDLIB_PRUNE_DLL, QT_KEEP, QT_PLUGINS_KEEP, QT_DROP_DIRS, QT_DROP_FILES,
                  QT_DROP_PREFIX)).encode()
    return hashlib.sha256(base_id().encode() + b"\0" + prune).hexdigest()[:16]


def _qt_module(name: str) -> str | None:
    """«Qt6Widgets.dll» / «QtWidgets.pyd» / «QtWidgets.pyi» → «Widgets»; иначе None."""
    stem = name.split(".", 1)[0]
    if name.endswith(".dll") and stem.startswith("Qt6"):
        return stem[3:]
    if name.endswith((".pyd", ".pyi")) and stem.startswith("Qt"):
        return stem[2:]
    return None


def runtime_ignore(src: str, names: list[str]) -> set[str]:
    """Фильтр копирования среды в сборку: лишнее из stdlib и PySide6."""
    folder = Path(src)
    drop: set[str] = {".complete"}
    if folder.name == "Lib" and folder.parent.name.startswith("runtime-"):
        drop |= {n for n in names if n in STDLIB_PRUNE}
    if folder.name == "DLLs":
        drop |= {n for n in names if n in STDLIB_PRUNE_DLL}
    if folder.name == "PySide6" and folder.parent.name == "site-packages":
        for n in names:
            mod = _qt_module(n)
            if (mod is not None and mod not in QT_KEEP) or n.endswith((".exe", ".pyi")) \
                    or n in QT_DROP_DIRS or n.startswith(QT_DROP_PREFIX):
                drop.add(n)
    if folder.name == "plugins" and folder.parent.name == "PySide6":
        drop |= {n for n in names if n not in QT_PLUGINS_KEEP}
    if folder.parent.name == "plugins":
        drop |= {n for n in names if n in QT_DROP_FILES}
    return drop


# ------------------------------------------------------------- среда Python
def build_runtime() -> Path:
    rid = base_id()
    target = CACHE / f"runtime-{rid}"
    python = target / "python.exe"
    if (target / ".complete").exists():
        say(f"[runtime] из кэша: {target.name}")
        return target
    if target.exists():
        shutil.rmtree(target)
    CACHE.mkdir(parents=True, exist_ok=True)
    pkg = CACHE / f"python.{PYTHON_VERSION}.nupkg"
    if not pkg.exists():
        url = NUGET_URL.format(v=PYTHON_VERSION)
        say(f"[runtime] скачиваю Python {PYTHON_VERSION}: {url}")
        urllib.request.urlretrieve(url, pkg.with_suffix(".part"))
        pkg.with_suffix(".part").replace(pkg)
    say("[runtime] распаковываю")
    with zipfile.ZipFile(pkg) as zf:
        for name in zf.namelist():
            if name.startswith("tools/") and not name.endswith("/"):
                dst = target / name[len("tools/"):]
                dst.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(name) as src, open(dst, "wb") as out:
                    shutil.copyfileobj(src, out)
    if not python.exists():
        raise SystemExit("В пакете NuGet нет tools/python.exe")
    env = dict(os.environ, PYTHONNOUSERSITE="1", PIP_DISABLE_PIP_VERSION_CHECK="1")
    if not (target / "Lib" / "site-packages" / "pip").exists():
        subprocess.run([str(python), "-m", "ensurepip", "--default-pip"], check=True, env=env)
    say("[runtime] ставлю зависимости из requirements.txt")
    subprocess.run([str(python), "-m", "pip", "install", "--no-warn-script-location", "--no-cache-dir",
                    "-r", str(ROOT / "requirements.txt")], check=True, env=env)
    # заранее скомпилировать байткод библиотек — быстрее первый запуск, и программа не пишет в python/
    subprocess.run([str(python), "-m", "compileall", "-q", "-j", "0", str(target / "Lib")], env=env)
    (target / ".complete").write_text(rid, encoding="utf-8")
    return target


# ------------------------------------------------------------------- код
def source_files() -> list[str]:
    """Файлы репозитория (отслеживаемые и новые, кроме игнорируемых)."""
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                         cwd=ROOT, capture_output=True, check=True).stdout.decode("utf-8")
    files = []
    for rel in filter(None, out.split("\0")):
        rel = rel.replace("\\", "/")
        if rel in EXCLUDE_EXACT or rel.startswith(EXCLUDE_PREFIX) or not (ROOT / rel).is_file():
            continue
        if rel.endswith((".pyc", ".log", ".bak", ".tmp")) or "/__pycache__/" in rel:
            continue
        files.append(rel)
    return sorted(files)


def check_secrets(stage: Path) -> None:
    """Страховка: в сборку не должны попасть ключи."""
    for bad in (stage / "engine" / ".env",):
        if bad.exists():
            raise SystemExit(f"В сборку попал {bad} — остановлено")


def build_launcher(out: Path) -> None:
    if not CSC.exists():
        raise SystemExit(f"Не найден компилятор C#: {CSC}")
    subprocess.run([str(CSC), "/nologo", "/codepage:65001", "/target:winexe", "/optimize+",
                    f"/win32icon:{ROOT / 'studio' / 'assets' / 'icon.ico'}", "/r:System.Windows.Forms.dll",
                    f"/out:{out}", str(ROOT / "installer" / "launcher" / "BegoFrame.cs")], check=True)


def stage_app(version: str, runtime: Path) -> Path:
    stage = BUILD / "stage" / "BegoFrame"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    files = source_files()
    say(f"[stage] файлов из репозитория: {len(files)}")
    for rel in files:
        dst = stage / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dst)
    # заводские промпты и шаблоны — из рабочих файлов репозитория (они и есть «последняя версия»)
    for folder in ("instructions", "templates"):
        src = ROOT / "engine" / folder
        dst = stage / "engine" / "instructions_default" / folder
        dst.mkdir(parents=True, exist_ok=True)
        for f in sorted(src.iterdir()):
            if f.is_file() and f.suffix not in (".bak", ".tmp"):
                shutil.copy2(f, dst / f.name)
    check_secrets(stage)

    # байткод вместо исходников (рядом с местом .py: python запускает и импортирует его так же)
    say("[stage] компилирую .py → .pyc")
    subprocess.run([str(runtime / "python.exe"), "-m", "compileall", "-b", "-q", "-f", str(stage)], check=True)
    for py in stage.rglob("*.py"):
        if py.with_suffix(".pyc").exists():
            py.unlink()
    for cache in stage.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    leftovers = list(stage.rglob("*.py"))
    if leftovers:
        raise SystemExit(f"Не скомпилировались: {leftovers[:5]}")

    say("[stage] собираю BegoFrame.exe")
    build_launcher(stage / "BegoFrame.exe")

    manifest = {"version": version, "runtime": runtime_id(), "python": PYTHON_VERSION,
                "built": dt.datetime.now().isoformat(timespec="seconds"), "files": {}}
    for f in sorted(stage.rglob("*")):
        if f.is_file():
            manifest["files"][f.relative_to(stage).as_posix()] = sha256(f)
    (stage / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")

    say("[stage] копирую среду Python")
    shutil.copytree(runtime, stage / "python", ignore=runtime_ignore)
    return stage


# --------------------------------------------------------------- выпуск
def make_zip(stage: Path, out: Path) -> None:
    manifest = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for rel in manifest["files"]:
            zf.write(stage / rel, rel)
        zf.write(stage / "manifest.json", "manifest.json")


def make_setup(stage: Path, version: str, out_dir: Path) -> Path:
    iscc = next((p for p in ISCC_CANDIDATES if p.exists()), None)
    if not iscc:
        raise SystemExit("Не найден Inno Setup (ISCC.exe): winget install JRSoftware.InnoSetup")
    say("[setup] Inno Setup — сжатие займёт несколько минут")
    subprocess.run([str(iscc), "/Q", f"/DAppVersion={version}", f"/DStageDir={stage}", f"/DOutputDir={out_dir}",
                    f"/DIconFile={ROOT / 'studio' / 'assets' / 'icon.ico'}",
                    str(ROOT / "installer" / "BegoFrame.iss")], check=True)
    return out_dir / f"BegoFrame-Setup-{version}.exe"


def asset(path: Path) -> dict:
    return {"file": path.name, "size": path.stat().st_size, "sha256": sha256(path)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-setup", action="store_true", help="не собирать установщик")
    a = ap.parse_args()
    version = app_version()
    notes = release_notes(version)
    if not notes:
        say(f"[!] В CHANGELOG.md нет раздела «## {version}» — в окне обновления будет пусто")
    say(f"=== BegoFrame {version}, runtime {runtime_id()} ===")
    runtime = build_runtime()
    stage = stage_app(version, runtime)
    out_dir = DIST / version
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    update_zip = out_dir / f"BegoFrame-update-{version}.zip"
    make_zip(stage, update_zip)
    info = {"version": version, "date": dt.date.today().isoformat(), "notes": notes,
            "runtime": runtime_id(), "python": PYTHON_VERSION, "update": asset(update_zip), "installer": None}
    if not a.no_setup:
        info["installer"] = asset(make_setup(stage, version, out_dir))
    (out_dir / "latest.json").write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    say(f"\nГотово: {out_dir}")
    for f in sorted(out_dir.iterdir()):
        say(f"  {f.name:40} {f.stat().st_size / 1048576:8.1f} МБ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
