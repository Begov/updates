"""Двуязычный интерфейс: русский (по умолчанию) и английский.

Тексты хранятся парами прямо в местах использования: T("Русский", "English").
Функция tr() выбирает вариант по текущему языку. Так подписи и подсказки
лежат рядом с кодом, который их показывает, и не теряются при правках.
"""
from __future__ import annotations

from typing import Any

from . import state

LANGUAGES = (("ru", "Русский"), ("en", "English"))

_lang = state.get("language", "ru") if state.get("language", "ru") in ("ru", "en") else "ru"


def T(ru: str, en: str) -> tuple[str, str]:
    """Пара «русский / английский»."""
    return (ru, en)


def language() -> str:
    return _lang


def set_language(lang: str) -> None:
    global _lang
    _lang = lang if lang in ("ru", "en") else "ru"
    state.put(language=_lang)


def tr(text: Any) -> str:
    """Вернуть текст на текущем языке.

    Принимает пару (ru, en), словарь {'ru': .., 'en': ..} или обычную строку.
    """
    if text is None:
        return ""
    if isinstance(text, tuple) and len(text) == 2:
        return text[0] if _lang == "ru" else text[1]
    if isinstance(text, dict):
        return str(text.get(_lang) or text.get("ru") or "")
    return str(text)


def is_ru() -> bool:
    return _lang == "ru"
