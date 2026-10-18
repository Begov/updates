"""Запуск дочерних процессов движка с построчной передачей вывода."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import Callable, Optional

# Процессы движка не должны открывать свои консольные окна под Windows
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if sys.platform == "win32" else 0

_ZERO_COUNTER = re.compile(r"\b(errors?|fails?|failures?|failed)\s*[=:]\s*0\b", re.I)
_NO_ERRORS = re.compile(r"\bno\s+(errors?|fails?|failures?|failed|traceback)\b", re.I)
SPINNER_CHARS = set("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")


def is_false_error(line: str) -> bool:
    """Строки со словом error, которые на деле сообщают об отсутствии ошибок."""
    low = line.lower()
    if "traceback" in low and "no traceback" not in low:
        return False
    return bool(_ZERO_COUNTER.search(line) or _NO_ERRORS.search(line))


def is_warning(line: str) -> bool:
    low = line.lower()
    stripped = low.strip()
    if stripped.startswith("[args]"):
        return False
    if "[warn]" in low or "[warning]" in low or stripped.startswith(("warn:", "warning:", "warn ", "warning ", "⚠")):
        return True
    return bool(re.search(r"\b(?:retrying|retry\s+\d+|retry\s+(?:in|after)\b)", low))


def classify(line: str) -> str:
    """Уровень строки журнала: ERROR / WARNING / SUCCESS / INFO."""
    low = line.lower()
    stripped = low.strip()
    if stripped.startswith("[ok]") or stripped.startswith("✅"):
        return "SUCCESS"
    if any(x in low for x in ("error", "fail", "traceback")) and not is_false_error(line):
        return "ERROR"
    if is_warning(line):
        return "WARNING"
    if any(x in low for x in ("[ok]", "success", "complete", "saved", "готов")):
        return "SUCCESS"
    return "INFO"


def popen(args: list, cwd: str, env: dict) -> subprocess.Popen:
    return subprocess.Popen(
        args, cwd=cwd, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        creationflags=CREATE_NO_WINDOW,
    )


def run_streamed(args: list, cwd: str, env: dict, on_line: Callable[[str], None],
                 should_stop: Optional[Callable[[], bool]] = None,
                 on_start: Optional[Callable[[subprocess.Popen], None]] = None) -> int:
    """Запустить процесс и отдавать каждую непустую строку в on_line."""
    proc = popen(args, cwd, env)
    if on_start:
        on_start(proc)
    try:
        for raw in proc.stdout:
            if should_stop and should_stop():
                kill_tree(proc)
                break
            line = raw.rstrip()
            if line:
                on_line(line)
    except (OSError, ValueError):
        pass
    return proc.wait()


def kill_tree(proc: Optional[subprocess.Popen], force: bool = True) -> bool:
    """Остановить процесс вместе с дочерними (под Windows — через taskkill /T)."""
    if proc is None or proc.poll() is not None:
        return False
    try:
        if os.name == "nt":
            if not force:
                proc.terminate()
                try:
                    proc.wait(timeout=1.5)
                    return True
                except subprocess.TimeoutExpired:
                    pass
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                           creationflags=CREATE_NO_WINDOW)
        else:
            proc.kill() if force else proc.terminate()
        return True
    except Exception:
        try:
            proc.kill()
            return True
        except Exception:
            return False


def open_path(path) -> None:
    """Открыть файл или папку программой по умолчанию."""
    target = str(path)
    if sys.platform == "win32":
        os.startfile(target)  # noqa: S606
    else:
        subprocess.Popen(["xdg-open", target])
