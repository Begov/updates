"""Чтение и запись файла настроек движка engine/.env.

Запись бережная: существующие строки и комментарии остаются на местах,
меняются только значения нужных ключей, новые ключи дописываются в конец.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import paths


def ensure_env_file() -> Path:
    """Создать engine/.env из примера при первом запуске."""
    if not paths.ENV_FILE.exists():
        if paths.ENV_EXAMPLE.exists():
            shutil.copy2(paths.ENV_EXAMPLE, paths.ENV_FILE)
        else:
            paths.ENV_FILE.write_text("", encoding="utf-8")
    return paths.ENV_FILE


def read_env(path: Path | None = None) -> dict[str, str]:
    """Прочитать пары ключ=значение (комментарии пропускаются)."""
    path = path or paths.ENV_FILE
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def write_env(updates: dict[str, str], path: Path | None = None,
              section_title: str = "BegoFrame") -> None:
    """Обновить ключи в .env и продублировать их в os.environ."""
    path = path or ensure_env_file()
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        lines = []
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}")
                continue
        out.append(line)
    if remaining:
        if out and out[-1].strip():
            out.append("")
        out.append(f"# ---- {section_title} ----")
        for key in sorted(remaining):
            out.append(f"{key}={remaining[key]}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    for key, value in updates.items():
        os.environ[key] = str(value)
