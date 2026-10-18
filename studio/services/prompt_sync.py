"""Доставка заводских промптов и шаблонов субтитров после обновления.

Сборка кладёт свежие заводские файлы только в engine/instructions_default/{instructions,templates};
рабочие engine/instructions и engine/templates пользователь правит во вкладке «Промпты»,
поэтому обновление их не трогает. При старте программы:

• файла нет — копируется заводской;
• файл не менялся пользователем (совпадает с заводским, который был выдан в прошлый раз) —
  заменяется новым заводским;
• файл изменён пользователем — остаётся как есть (вернуть новый можно кнопкой «Заводской вариант»).

Какой заводской вариант выдан последним, помнит userdata/prompt_defaults.json (sha256 по файлам).
Свои файлы пользователя в engine/instructions не трогаются никогда.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

from .. import paths

STATE_FILE = paths.USERDATA / "prompt_defaults.json"
log = logging.getLogger(__name__)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(dst: Path, data: bytes) -> None:
    """Записать целиком или никак (как остальные файлы программы)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, dst)


def sync() -> list[str]:
    """Разложить заводские файлы в рабочие папки. Возвращает список обновлённых файлов."""
    state = _load_state()
    changed: list[str] = []
    for folder in (paths.INSTRUCTIONS, paths.TEMPLATES):
        source = paths.INSTRUCTIONS_DEFAULT / folder.name
        if not source.is_dir():
            continue
        for default in sorted(source.iterdir()):
            if not default.is_file() or default.suffix in (".bak", ".tmp"):
                continue
            key = f"{folder.name}/{default.name}"
            new_sha = _sha(default)
            target = folder / default.name
            if not target.exists():
                _write(target, default.read_bytes())
                state[key] = new_sha
                changed.append(key)
                continue
            current = _sha(target)
            if current == new_sha:
                state[key] = new_sha
            elif state.get(key) and current == state[key]:
                # пользователь не менял файл — выдаём новую заводскую версию
                _write(target, default.read_bytes())
                state[key] = new_sha
                changed.append(key)
            # иначе файл изменён пользователем — не трогаем
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        log.warning("prompt_defaults.json: %s", exc)
    if changed:
        log.info("Prompt defaults delivered: %s", ", ".join(changed))
    return changed
