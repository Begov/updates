"""Пресеты: сохранённые наборы настроек конвейера."""
from __future__ import annotations

import json
from pathlib import Path

from .. import paths
from ..pipeline.defaults import NON_PRESET_KEYS


def load_all() -> dict:
    try:
        data = json.loads(paths.PRESETS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_all(data: dict) -> None:
    paths.USERDATA.mkdir(parents=True, exist_ok=True)
    paths.PRESETS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def names() -> list[str]:
    return sorted(load_all(), key=str.lower)


def get(name: str) -> dict | None:
    preset = load_all().get(name)
    return preset if isinstance(preset, dict) else None


def to_preset(values: dict) -> dict:
    """Значения формы -> пресет (без путей к конкретным файлам)."""
    return {k: v for k, v in values.items() if k not in NON_PRESET_KEYS}


def put(name: str, values: dict) -> None:
    data = load_all()
    data[name] = to_preset(values)
    save_all(data)


def delete(name: str) -> None:
    data = load_all()
    if data.pop(name, None) is not None:
        save_all(data)


def export_to(path: Path, name: str) -> None:
    preset = get(name)
    if preset is None:
        raise KeyError(name)
    Path(path).write_text(json.dumps({name: preset}, ensure_ascii=False, indent=2), encoding="utf-8")


def import_from(path: Path) -> list[str]:
    """Импорт пресетов из файла; возвращает имена добавленных."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("wrong preset file")
    # Файл может содержать один пресет без имени — тогда имя берётся из файла
    if data and all(not isinstance(v, dict) for v in data.values()):
        data = {Path(path).stem: data}
    store = load_all()
    added = []
    for name, preset in data.items():
        if isinstance(preset, dict):
            store[name] = preset
            added.append(name)
    save_all(store)
    return added
