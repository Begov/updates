"""Один вердикт о прокси Codex на весь процесс.

Раньше проверка жила ровно один вызов: перед КАЖДЫМ запуском Codex код лез
сокетом на прокси. Если прокси лежит, это стоило до четырёх секунд на вызов
(TimeoutError отрабатывает по полному таймауту) и заваливало журнал одинаковым
предупреждением — на каждом этапе, на каждом чанке.

Здесь вердикт запоминается:

* прокси мёртв → до конца процесса идём НАПРЯМУЮ, больше не пробуем и молчим;
  предупреждение печатается ровно один раз;
* прокси жив → результат кэшируется на минуту, чтобы не платить сетевой
  проверкой за каждый чанк, но смерть прокси посреди прогона всё равно
  замечается на следующей минуте.

Модули (gpt_director, music_director, real_photo) делали по своей копии этой
логики — три копии неизбежно разъезжаются, поэтому логика одна и здесь.
"""
from __future__ import annotations

import logging
import socket
import threading
import time
import urllib.parse

import params as config
# Ключи окружения, которыми Codex узнаёт о прокси.
PROXY_ENV_KEYS = (
    'CODEX_PROXY',
    'HTTPS_PROXY', 'https_proxy',
    'HTTP_PROXY', 'http_proxy',
    'ALL_PROXY', 'all_proxy',
)

PROBE_TIMEOUT = 4.0
# Живой прокси перепроверяем не чаще раза в минуту: проверка стоит сетевого
# похода, а этапов в прогоне десятки.
ALIVE_TTL = 60.0

_LOCK = threading.Lock()
_STATE = {
    'proxy': '',        # для какого адреса вынесен вердикт
    'dead_reason': '',  # непусто — прокси признан мёртвым
    'announced': False,  # предупреждение уже показано
    'alive_until': 0.0,
}


def env_without_proxy(env: dict) -> dict:
    """Копия окружения без всякого упоминания прокси."""
    direct = dict(env)
    for key in PROXY_ENV_KEYS:
        direct.pop(key, None)
    return direct


def reset() -> None:
    """Забыть вердикт (нужно тестам и ручному «проверить ещё раз»)."""
    with _LOCK:
        _STATE.update({'proxy': '', 'dead_reason': '', 'announced': False,
                       'alive_until': 0.0})


def _probe(proxy: str) -> str:
    """Пустая строка — прокси отвечает; иначе причина отказа."""
    try:
        parsed = urllib.parse.urlsplit(proxy)
        host, port = parsed.hostname, parsed.port
        if not host or not port:
            raise ValueError('proxy host/port missing')
        with socket.create_connection((host, port), timeout=PROBE_TIMEOUT):
            pass
        return ''
    except Exception as exc:                      # noqa: BLE001
        return f'{type(exc).__name__}: {exc}'


def prepare() -> tuple[dict, str, bool]:
    """Окружение для запуска Codex.

    Возвращает (env, reason, proxy_in_use):
      * reason непустая ТОЛЬКО в первый раз, когда прокси признан мёртвым —
        чтобы предупреждение прозвучало один раз, а не на каждом этапе;
      * proxy_in_use=False означает, что запуск идёт напрямую и запасной
        «прямой» повтор при ошибке не нужен — он был бы дублем той же попытки.
    """
    env = config.codex_env_for_provider()
    proxy = (getattr(config, 'CODEX_PROXY', '') or '').strip()
    if not proxy:
        return env, '', False

    now = time.monotonic()
    with _LOCK:
        known = _STATE['proxy'] == proxy
        if known and _STATE['dead_reason']:
            reason = '' if _STATE['announced'] else _STATE['dead_reason']
            _STATE['announced'] = True
            return env_without_proxy(env), reason, False
        if known and now < _STATE['alive_until']:
            return env, '', True

    reason = _probe(proxy)
    with _LOCK:
        _STATE['proxy'] = proxy
        if reason:
            _STATE['dead_reason'] = reason
            _STATE['announced'] = True
            _STATE['alive_until'] = 0.0
            logging.warning(
                'Codex proxy мёртв (%s) — весь процесс идёт напрямую, '
                'повторных проверок не будет', reason)
            return env_without_proxy(env), reason, False
        _STATE['dead_reason'] = ''
        _STATE['announced'] = False
        _STATE['alive_until'] = now + ALIVE_TTL
    return env, '', True
