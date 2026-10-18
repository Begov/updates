"""
stage_log.py — свой лог-файл у КАЖДОЙ стадии, рядом с проектом.

Зачем. Общий лог прогона (`<проект>_full.log`) — это тысячи строк, где строки
стадии перемешаны с сотней чужих. Разбирать «почему сцена ушла в
нейрогенерацию» приходилось грепом по всему файлу, а часть стадий вообще не
писала ничего своего: EPF пришлось разбирать по `bridge_manifest.json`, у
ютуба файла не было ни в каком режиме (замечание пользователя 01.09).

Приём тот же, что давно стоит у генераторов (`imggen`, `fastgen`, `veo_video`):
временный `FileHandler` на корневом логгере, уровень DEBUG. Отличие — общая
точка правды: раньше каждый модуль повторял эти пятнадцать строк по-своему,
и стадии, добавленные позже, про них просто забывали.

Использование:

    from units.stage_journal import stage_log

    with stage_log(project_dir, 'youtube') as path:
        ...                       # всё, что залогировано внутри, попадёт в файл

Файл называется `<стадия>_<ГГГГММДД_ЧЧММСС>.log` и лежит в папке проекта —
рядом с `montage_plan.json`, а не в общей `agent/logs`, чтобы лог уезжал
вместе с проектом.

Хендлер ОБЯЗАТЕЛЬНО снимается на выходе: GUI живёт часами, и оставленный
хендлер продолжил бы писать в файл прошлого проекта — так уже случалось с
`imggen`, где второй обработчик на тот же путь спорил с первым.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

_FORMAT = '%(asctime)s | %(levelname)-8s | %(message)s'


def stage_log_path(project_dir, stage: str) -> Path:
    """Куда ляжет лог стадии. Имя со временем — прогоны не затирают друг друга."""
    return Path(project_dir) / f'{stage}_{datetime.now():%Y%m%d_%H%M%S}.log'


def attach(project_dir, stage: str) -> tuple[Optional[logging.Handler], Optional[Path]]:
    """Подключить файл стадии к корневому логгеру. `(хендлер, путь)`.

    Не удалось (длинный путь Windows, права) — возвращаем `(None, None)`: лог
    стадии приятен, но ронять из-за него прогон нельзя.
    """
    path = stage_log_path(project_dir, stage)
    try:
        handler = logging.FileHandler(str(path), encoding='utf-8')
    except OSError as exc:
        logging.getLogger(__name__).warning(
            "лог стадии «%s» не завёлся (%s): %s", stage, type(exc).__name__, path)
        return None, None
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(_FORMAT))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.DEBUG)
    logging.info("=== лог стадии «%s» → %s ===", stage, path)
    return handler, path


def detach(handler: Optional[logging.Handler]) -> None:
    """Снять хендлер и закрыть файл. Молча терпит None и повторный вызов."""
    if handler is None:
        return
    try:
        logging.getLogger().removeHandler(handler)
        handler.close()
    except Exception:                                   # noqa: BLE001
        pass


@contextmanager
def stage_log(project_dir, stage: str) -> Iterator[Optional[Path]]:
    """Лог стадии на время блока. Отдаёт путь к файлу (или None)."""
    handler, path = attach(project_dir, stage)
    try:
        yield path
    finally:
        if path is not None:
            logging.info("=== лог стадии «%s» закрыт ===", stage)
        detach(handler)
