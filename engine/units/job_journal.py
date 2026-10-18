"""
Журнал задач в работе — для провайдеров, у которых задачу можно снять только по id.

«Стоп» сначала убивает процессы генерации, и id их задач, живущие в памяти, теряются.
Поэтому клиент такого провайдера пишет id каждой принятой задачи сюда и вычёркивает,
когда задача закончилась; «Стоп» (stage_runner `cancel_all`) читает журнал, снимает
оставшиеся задачи и стирает журнал.

Файл свой у каждого процесса (runtime/active_jobs/<провайдер>_<pid>.json): процессы
друг другу не мешают, а журнал убитого процесса остаётся на диске до «Стопа».
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Dict, List

JOURNAL_DIR = Path(__file__).resolve().parents[2] / 'runtime' / 'active_jobs'

_LOCK = threading.Lock()
_MINE: Dict[str, Dict[str, None]] = {}          # провайдер -> id задач этого процесса


def _own_file(provider: str) -> Path:
    return JOURNAL_DIR / f"{provider}_{os.getpid()}.json"


def _flush(provider: str) -> None:
    """Записать список задач процесса (под _LOCK). Пустой — файл убрать."""
    path = _own_file(provider)
    ids = list(_MINE.get(provider) or {})
    try:
        if ids:
            JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix('.tmp')
            tmp.write_text(json.dumps(ids), encoding='utf-8')
            tmp.replace(path)
        else:
            path.unlink(missing_ok=True)
    except OSError as exc:
        logging.debug("job journal %s: %s", provider, exc)


def record(provider: str, task_id: str) -> None:
    """Задача принята сервером."""
    with _LOCK:
        _MINE.setdefault(provider, {})[str(task_id)] = None
        _flush(provider)


def forget(provider: str, task_id: str) -> None:
    """Задача закончилась (готова, упала или снята)."""
    with _LOCK:
        mine = _MINE.get(provider, {})
        if str(task_id) not in mine:
            return
        del mine[str(task_id)]
        _flush(provider)


def pending(provider: str) -> List[str]:
    """id задач провайдера из журналов ВСЕХ процессов (живых и убитых)."""
    ids: Dict[str, None] = {}
    for path in sorted(JOURNAL_DIR.glob(f"{provider}_*.json")):
        try:
            for jid in json.loads(path.read_text(encoding='utf-8')) or []:
                ids[str(jid)] = None
        except (OSError, ValueError):
            continue
    return list(ids)


def clear(provider: str) -> None:
    """Стереть журналы провайдера (после того как «Стоп» снял задачи)."""
    for path in JOURNAL_DIR.glob(f"{provider}_*.json"):
        path.unlink(missing_ok=True)
