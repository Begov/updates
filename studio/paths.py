"""Все пути приложения в одном месте."""
from __future__ import annotations

import sys
from pathlib import Path

# Корень установки BegoFrame
ROOT = Path(__file__).resolve().parent.parent


def script(path: Path) -> Path:
    """Путь к запускаемому скрипту движка.

    В установленной версии исходников нет — рядом лежит байткод (`cli.pyc` вместо `cli.py`),
    и python.exe запускает его так же.
    """
    if not path.exists() and path.with_suffix(".pyc").exists():
        return path.with_suffix(".pyc")
    return path


# Движок (бывший backend) и его файлы
ENGINE = ROOT / "engine"
ENV_FILE = ENGINE / ".env"
ENV_EXAMPLE = ENGINE / ".env.example"
CLI = script(ENGINE / "cli.py")
ASSEMBLER = script(ENGINE / "assemble.py")
STAGE_RUNNER = script(ENGINE / "stage_runner.py")
FINISH_RUNNER = script(ENGINE / "finish_runner.py")
CODEX_SETUP = script(ENGINE / "helpers" / "codex_setup.py")
REMOTION_SETUP = script(ENGINE / "helpers" / "remotion_setup.py")
INSTRUCTIONS = ENGINE / "instructions"
INSTRUCTIONS_DEFAULT = ENGINE / "instructions_default"
TEMPLATES = ENGINE / "templates"

# Рабочие данные пользователя
PROJECTS = ROOT / "projects"
INBOX = ROOT / "inbox"
RUNTIME = ROOT / "runtime"
STAGING = RUNTIME / "staging"
STAGING_LOGOS = STAGING / "logos"
CODEX_DIR = RUNTIME / "codex"
USERDATA = ROOT / "userdata"
PRESETS_FILE = USERDATA / "presets.json"
UI_STATE_FILE = USERDATA / "ui_state.json"
CONCAT_LOG = USERDATA / "concat_queue.log"
LOGS_DIR = USERDATA / "logs"

# Обновления: состав установленной версии и рабочая папка загрузок
MANIFEST = ROOT / "manifest.json"        # есть только в установленной версии (кладёт сборка)
UPDATES_DIR = RUNTIME / "updates"
LAUNCHER = ROOT / "BegoFrame.exe"

ASSETS = Path(__file__).resolve().parent / "assets"


def python_exe() -> str:
    """Интерпретатор для дочерних процессов.

    GUI запускается через pythonw.exe (без консоли), а дочерним процессам
    нужен обычный python.exe — у pythonw нет stdout, и вывод стадий терялся бы.
    """
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        candidate = exe.with_name("python.exe")
        if candidate.is_file():
            return str(candidate)
    return str(exe)


def ensure_dirs() -> None:
    """Создать рабочие папки при первом запуске."""
    for folder in (PROJECTS, INBOX, RUNTIME, STAGING, USERDATA, LOGS_DIR):
        folder.mkdir(parents=True, exist_ok=True)
