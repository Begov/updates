"""Список моделей Codex: берётся у самого Codex, а не хранится в программе.

Источники по порядку:
1. кэш программы `userdata/codex_models.json` (его обновляет `refresh()` командой `codex debug models`);
2. кэш самого Codex `~/.codex/models_cache.json`;
3. встроенный запасной список.

В список попадают только модели с `visibility: list` — те, что Codex сам показывает пользователю
(служебные `hide`-модели вроде `codex-auto-review` пропускаются). Порядок — по приоритету Codex.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from . import paths

CACHE_FILE = paths.USERDATA / "codex_models.json"

# Запасной список — на случай, когда Codex недоступен (не установлен, нет сети при первом запуске).
FALLBACK: list[tuple[str, str]] = [
    ("gpt-6.1-sol", "GPT-6.1-Sol"), ("gpt-6-astra", "GPT-6-Astra"), ("gpt-6-sol", "GPT-6-Sol"),
    ("gpt-6-luna", "GPT-6-Luna"), ("gpt-5.6-sol", "GPT-5.6-Sol"), ("gpt-5.6-terra", "GPT-5.6-Terra"),
    ("gpt-5.6-luna", "GPT-5.6-Luna"), ("gpt-5.5", "GPT-5.5"),
]

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def parse_catalog(data) -> list[tuple[str, str]]:
    """[(slug, отображаемое имя)] из каталога моделей Codex, по приоритету; только видимые."""
    models = data.get("models", []) if isinstance(data, dict) else (data or [])
    shown = [m for m in models if isinstance(m, dict) and m.get("slug") and m.get("visibility", "list") == "list"]
    shown.sort(key=lambda m: m.get("priority", 10 ** 6))
    out, seen = [], set()
    for m in shown:
        slug = str(m["slug"])
        if slug not in seen:
            seen.add(slug)
            out.append((slug, str(m.get("display_name") or slug)))
    return out


def _read(path: Path) -> list[tuple[str, str]]:
    try:
        return parse_catalog(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return []


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def load() -> list[tuple[str, str]]:
    """Модели без обращения к сети: быстрый вызов для старта программы."""
    return _read(CACHE_FILE) or _read(codex_home() / "models_cache.json") or list(FALLBACK)


def find_exe() -> str | None:
    from . import engine_link
    configured = str(engine_link.cfg("CODEX_EXE_PATH", "") or "")
    if configured and Path(configured).is_file():
        return configured
    own = paths.CODEX_DIR / "codex-x86_64-pc-windows-msvc.exe"
    if own.is_file():
        return str(own)
    return shutil.which("codex")


def fetch(timeout: int = 45) -> list[tuple[str, str]]:
    """Спросить каталог у Codex (`codex debug models`). Токены не расходуются. Пусто — не вышло."""
    exe = find_exe()
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "debug", "models"], capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=timeout, creationflags=_NO_WINDOW)
        if out.returncode != 0:
            return []
        return parse_catalog(json.loads(out.stdout))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        logging.info("codex debug models: %s", exc)
        return []


def refresh(on_change=None) -> None:
    """В фоне обновить список: сохранить в кэш программы и сообщить, если он изменился."""
    def work():
        models = fetch()
        if not models:
            return
        changed = models != load()
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_text(json.dumps([{"slug": s, "display_name": n, "visibility": "list", "priority": i}
                                              for i, (s, n) in enumerate(models)], ensure_ascii=False, indent=1),
                                  encoding="utf-8")
        except OSError as exc:
            logging.warning("codex_models: не удалось записать кэш: %s", exc)
        if changed and on_change:
            on_change(models)

    threading.Thread(target=work, daemon=True, name="codex-models").start()
