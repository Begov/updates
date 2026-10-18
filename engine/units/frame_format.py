"""
frame_format.py — формат кадра ролика (16:9 / 9:16) для всех генераторов.

Правило пользователя (10.10.2026): формат кадра передаётся КАЖДОЙ модели картинок
и видео — в конце промпта всегда, а в теле запроса там, где модель такой параметр
понимает. Часть моделей поле формата не принимает вовсе, и тогда строка в конце
промпта — единственный способ сказать модели, какой кадр нужен. Формат 1:1 из
программы убран: он (и 'square') сводится к формату ролика.

Формат на весь ролик один (VEONONSTOP_ASPECT_RATIO — его ставит интерфейс): по
нему же идёт склейка, поэтому кадры разных генераторов совпадают.

Строка дописывается в точке отправки запроса каждого клиента — после неё к
промпту уже ничего не добавляется, и она гарантированно последняя. Повторный
вызов (переписывание промпта, повтор запроса) старую строку заменяет, а не
дублирует.
"""
from __future__ import annotations

import os
import re
from typing import Optional

FORMATS = ('16:9', '9:16')

_DESCRIPTION = {
    '16:9': 'horizontal widescreen frame, wider than tall',
    '9:16': 'vertical portrait frame, taller than wide',
}
_ALIASES = {
    'landscape': '16:9', 'portrait': '9:16',
    'image_aspect_ratio_landscape': '16:9', 'image_aspect_ratio_portrait': '9:16',
    'landscape_16_9': '16:9', 'portrait_16_9': '9:16',
}
_TAIL = re.compile(r'\s*Aspect ratio:\s*(?:16:9|9:16|1:1)\b[^\n]*\s*$', re.IGNORECASE)


def normalize(value: Optional[str]) -> str:
    """'16:9' / 'landscape' / 'IMAGE_ASPECT_RATIO_LANDSCAPE' → '16:9'; неизвестное → ''."""
    text = str(value or '').strip()
    if text in FORMATS:
        return text
    return _ALIASES.get(text.lower(), '')


def project_format(fallback: Optional[str] = None) -> str:
    """Формат кадра ролика: из запуска (интерфейс), иначе из engine/.env, иначе 16:9."""
    value = normalize(os.environ.get('VEONONSTOP_ASPECT_RATIO'))
    if not value:
        try:
            import params as config
            value = normalize(getattr(config, 'VEONONSTOP_ASPECT_RATIO', ''))
        except Exception:                                          # noqa: BLE001
            value = ''
    return value or normalize(fallback) or '16:9'


def resolve(aspect: Optional[str] = None) -> str:
    """Явно переданный формат, если он один из двух; иначе формат ролика."""
    return normalize(aspect) or project_format()


def with_frame_format(prompt: str, aspect: Optional[str] = None) -> str:
    """Промпт со строкой формата кадра в самом конце (старая строка заменяется)."""
    fmt = resolve(aspect)
    base = _TAIL.sub('', str(prompt or '').rstrip())
    return f"{base}\n\nAspect ratio: {fmt} — {_DESCRIPTION[fmt]}."
