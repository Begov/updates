"""Связь интерфейса с движком: доступ к параметрам (params.py) и модулям units.

Интерфейс читает значения по умолчанию из тех же параметров, что и движок,
поэтому пустые поля всегда совпадают с тем, чем движок работал бы сам.
"""
from __future__ import annotations

import importlib
import os
import sys
from typing import Any

from . import paths

if str(paths.ENGINE) not in sys.path:
    sys.path.insert(0, str(paths.ENGINE))

# tqdm внутри модулей движка не должен рисовать прогресс в процессе GUI
os.environ.setdefault("TQDM_DISABLE", "1")

_params = None


def params():
    """Модуль параметров движка (engine/params.py), загружается один раз."""
    global _params
    if _params is None:
        _params = importlib.import_module("params")
    return _params


def reload_params():
    """Перечитать .env и параметры после сохранения настроек."""
    global _params
    try:
        from dotenv import load_dotenv
        load_dotenv(str(paths.ENV_FILE), override=True)
    except Exception:
        pass
    _params = importlib.reload(params())
    return _params


def cfg(name: str, default: Any = None) -> Any:
    """Значение параметра движка с запасным вариантом."""
    try:
        value = getattr(params(), name)
    except Exception:
        return default
    return default if value is None else value


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def env_text(name: str, default: str = "") -> str:
    raw = os.getenv(name)
    return default if raw is None else str(raw).strip()


def unit(name: str):
    """Импорт модуля движка по имени: unit('veo_client')."""
    return importlib.import_module(f"units.{name}")
