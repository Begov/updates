"""User-editable I2V parallax prompt templates and deterministic selection."""

from __future__ import annotations

import logging
import re
import zlib
from pathlib import Path

import params as config
logger = logging.getLogger(__name__)

# Точка правды ОДНА: agent/prompts/parallax_prompts.txt. Оттуда файл едет с
# обновлением и не затирает правки пользователя (updater.KEEP_IF_EXISTS).
#
# Историческое имя ниже — файл, который лежал в КОРНЕ установки и правился
# руками. Он не попадал в раздачу (корневые .txt не синкуются) и апдейтером не
# возился, поэтому 29.08 его перестали считать единственным местом. Какое-то
# время он ещё читался ПЕРВЫМ, чтобы не перебивать ручные правки, — и вышло
# ровно наоборот: побеждал файл, который никто не обновляет, а новые шаблоны до
# таких пользователей не доезжали вовсе. Теперь он не читается; апдейтер при
# обновлении отставляет его в сторону с суффиксом `.bak_migrated`, а тому, кто
# не обновлялся, о нём говорит предупреждение ниже.
LEGACY_FILENAME = "паралакс промпты.txt"
PROMPTS_FILENAME = "parallax_prompts.txt"

_legacy_warned = False


def legacy_template_path() -> Path:
    """Старый файл в корне установки. Больше не читается, только замечается."""
    return Path(config.BASE_DIR).parent / LEGACY_FILENAME


def parallax_template_paths() -> list[Path]:
    """Где ищем шаблоны. Одно место — чтобы не было двух точек правды."""
    return [Path(config.BASE_DIR) / "instructions" / PROMPTS_FILENAME]


def _warn_about_legacy_file() -> None:
    """Сказать один раз, что файл в корне больше не в игре."""
    global _legacy_warned
    if _legacy_warned:
        return
    legacy = legacy_template_path()
    try:
        if not legacy.is_file():
            return
    except OSError:
        return
    _legacy_warned = True
    logger.warning(
        "Файл шаблонов в корне больше не читается: %s. Рабочий файл — %s; "
        "если в корневом были ваши правки, перенесите их туда.",
        legacy, parallax_template_paths()[0])


def load_parallax_templates(path: Path | None = None) -> dict[int, str]:
    """Read numbered templates from the shared editable text file.

    Файла нет ни в одном месте → пустой словарь, а НЕ исключение: параллакс это
    украшение, и его отсутствие не должно ронять всю видеогенерацию.
    """
    candidates = [path] if path else parallax_template_paths()
    if path is None:
        # Кто не обновлялся — увидит в логе, что его файл в корне вне игры.
        _warn_about_legacy_file()
    prompt_path = next((p for p in candidates if p and p.is_file()), None)
    if prompt_path is None:
        logger.warning(
            "Шаблоны параллакса не найдены (искал: %s) — параллакс будет пропущен",
            ", ".join(str(p) for p in candidates if p))
        return {}
    text = prompt_path.read_text(encoding="utf-8-sig")
    parts = re.split(r"\n-{20,}\s*\n", text)
    templates: dict[int, str] = {}
    for index, part in enumerate(parts[:-1]):
        match = re.search(r"ПРОМПТ\s+([1-3])", part, flags=re.IGNORECASE)
        if not match:
            continue
        template = parts[index + 1].strip()
        if template:
            templates[int(match.group(1))] = template
    return templates


# «Случайно»: клипы получают разные шаблоны вперемешку, а не один на всех.
PARALLAX_RANDOM = 0

PARALLAX_NAMES = {
    PARALLAX_RANDOM: 'вперемешку',
    1: 'сбалансированный',
    2: 'строгий (двигается только камера)',
    3: 'механический',
}


def parallax_method_name(method: int) -> str:
    try:
        return PARALLAX_NAMES.get(int(method), '?')
    except (TypeError, ValueError):
        return '?'


def parallax_method_for_clip(clip_id: int, method: int) -> int:
    """Какой шаблон достаётся клипу.

    При обычном выборе — он же для всех. При «вперемешку» шаблон выводится из
    номера клипа: разные клипы получают разные, но КОНКРЕТНЫЙ клип всегда один
    и тот же. Настоящий `random` тут не годится — при повторе генерации того же
    клипа шаблон бы сменился, и результат перестал бы воспроизводиться.
    """
    try:
        method = int(method)
    except (TypeError, ValueError):
        return 1
    if method != PARALLAX_RANDOM:
        return method
    try:
        seed = int(clip_id)
    except (TypeError, ValueError):
        return 1
    # crc32 вместо остатка от деления: остаток дал бы ровную карусель
    # 1-2-3-1-2-3 по соседним клипам, а нужен именно разнобой.
    return zlib.crc32(str(seed).encode('utf-8')) % 3 + 1


def selected_parallax_prompt(method: int, path: Path | None = None) -> str | None:
    """Текст шаблона или None, если шаблонов нет.

    Раньше здесь было исключение, и оно уходило наверх нетронутым: у клиента без
    файла падала ВСЯ стадия veo_i2v, а не только параллакс — пять сцен остались
    без медиа и доехали до склейки статикой (прогон 29.08). Теперь вызывающий
    сам решает, как быть без шаблона; обе точки в veo_video просто идут дальше
    обычным промптом.
    """
    templates = load_parallax_templates(path)
    if not templates:
        return None
    try:
        method = int(method)
    except (TypeError, ValueError):
        method = 1
    if method not in templates:
        method = 1
    return templates.get(method) or None


def use_parallax_for_clip(
    clip_id: int,
    mix_with_perclip: bool,
    *,
    every_n: int = 2,
) -> bool:
    """Use every Nth clip in mixed mode; stable across retries."""
    if not mix_with_perclip:
        return True
    try:
        every_n = max(1, int(every_n))
    except (TypeError, ValueError):
        every_n = 2
    return int(clip_id) % every_n == 0


