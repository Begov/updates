"""Состояние интерфейса между запусками (язык, тема, свёрнутые блоки, размеры окна).

Хранится отдельно от настроек конвейера: это вид окна, а не параметры генерации.
"""
from __future__ import annotations

import json
import threading
from typing import Any

from . import paths

_lock = threading.Lock()
_cache: dict | None = None


def _load() -> dict:
    global _cache
    if _cache is None:
        try:
            data = json.loads(paths.UI_STATE_FILE.read_text(encoding="utf-8"))
            _cache = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            _cache = {}
    return _cache


def get(key: str, default: Any = None) -> Any:
    with _lock:
        return _load().get(key, default)


def put(**values: Any) -> None:
    """Обновить ключи и сразу записать файл (ошибки записи не критичны)."""
    with _lock:
        data = _load()
        data.update(values)
        try:
            paths.USERDATA.mkdir(parents=True, exist_ok=True)
            paths.UI_STATE_FILE.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
