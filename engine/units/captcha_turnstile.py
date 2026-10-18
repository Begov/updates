"""
turnstile_solver.py — прохождение Cloudflare Turnstile сторонним сервисом.

Один решатель на весь агент: им пользуются и Filesta (платная загрузка стока), и
Suno (генерация музыки). Раньше код жил внутри `filesta_envato`, и музыке
пришлось бы его копировать — а копия неизбежно разъезжается с оригиналом.

Два поддержанных сервиса ходят одинаково (`createTask` → опрос
`getTaskResult`) и различаются только хостом и названием задачи. Ключ и
провайдер берутся из `.env` (`CAPTCHA_SOLVER_KEY`, `CAPTCHA_SOLVER_PROVIDER`).

Ступень НЕОБЯЗАТЕЛЬНАЯ. Ключа нет — молча возвращаем пусто и НЕ трогаем сеть:
у Filesta капчу в большинстве случаев снимает свой рендер в браузере, а у Suno
вызывающая сторона решает сама, что делать дальше. Отсутствие ключа никогда не
должно ронять прогон.

Оговорка про адрес. Сервис решает капчу на СВОЁМ адресе и своём браузере, и
Cloudflare такой токен иногда не принимает для чужой сессии. Тогда сайт просто
ответит отказом ещё раз — потерь никаких, ступень не сработает.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
from typing import Any, Dict, Optional

import params as config
logger = logging.getLogger(__name__)

# (адрес API, имя задачи для Turnstile без прокси)
SOLVERS = {
    'capmonster': ('https://api.capmonster.cloud', 'TurnstileTaskProxyless'),
    'capsolver': ('https://api.capsolver.com', 'AntiTurnstileTaskProxyLess'),
}

# Ключи виджетов Suno. Лежат в открытом скрипте сайта под говорящими именами
# (`NEXT_PUBLIC_CLOUDFLARE_TURNSTILE_SITE_KEY_GEN` и соседи), поэтому добыты
# без единого потраченного кредита. Разные действия — разные ключи, и для
# генерации годится ТОЛЬКО `_GEN`.
SUNO_SITE_KEY_GEN = '0x4AAAAAADI7xDNyj-3LcIbi'
SUNO_SITE_KEY_AUTH = '0x4AAAAAABtnpJo7aKMs9JLQ'
SUNO_SITE_KEY_COMMON = '0x4AAAAAABd64Cd9aq5C--VE'


def configured() -> bool:
    """Есть ли ключ сервиса. Нет — ступень просто отсутствует."""
    return bool(str(getattr(config, 'CAPTCHA_SOLVER_KEY', '') or '').strip())


def solve(site_key: str, page_url: str, *, timeout_sec: int = 120) -> str:
    """Решить Turnstile сервисом. Пустая строка — не вышло (или ключа нет)."""
    api_key = str(getattr(config, 'CAPTCHA_SOLVER_KEY', '') or '').strip()
    if not api_key:
        logger.info("капча: сервис-решатель не подключён (CAPTCHA_SOLVER_KEY пуст)")
        return ''
    if not site_key:
        logger.warning("капча: не знаем ключ виджета — решать нечего")
        return ''
    provider = str(getattr(config, 'CAPTCHA_SOLVER_PROVIDER', 'capmonster')
                   or 'capmonster').strip().lower()
    setup = SOLVERS.get(provider)
    if setup is None:
        logger.warning("капча: решатель «%s» не поддержан (есть: %s)",
                       provider, ', '.join(sorted(SOLVERS)))
        return ''
    base, task_type = setup

    def _post(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        request = urllib.request.Request(
            f'{base}/{path}', data=json.dumps(body).encode('utf-8'),
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode('utf-8'))

    try:
        created = _post('createTask', {
            'clientKey': api_key,
            'task': {'type': task_type, 'websiteURL': page_url, 'websiteKey': site_key},
        })
        task_id = created.get('taskId')
        if not task_id:
            logger.warning("капча: задача не принята — %s",
                           str(created.get('errorDescription') or created)[:160])
            return ''
        deadline = time.time() + max(30, timeout_sec)
        while time.time() < deadline:
            time.sleep(3)
            result = _post('getTaskResult', {'clientKey': api_key, 'taskId': task_id})
            if result.get('errorId'):
                logger.warning("капча: решатель вернул ошибку — %s",
                               str(result.get('errorDescription'))[:160])
                return ''
            if result.get('status') == 'ready':
                solution = result.get('solution') or {}
                # CapSolver кладёт ответ в `token`, CapMonster — исторически
                # ещё и в `gRecaptchaResponse`.
                token = str(solution.get('token')
                            or solution.get('gRecaptchaResponse') or '')
                if token:
                    logger.info("капча решена сервисом (%s), токен %d символов",
                                provider, len(token))
                return token
        logger.warning("капча: решатель не уложился в %d с", timeout_sec)
    except Exception as exc:                                # noqa: BLE001
        logger.warning("капча: решатель недоступен (%s) — иду дальше", exc)
    return ''


def solve_for_suno(page_url: str = 'https://suno.com/create') -> str:
    """Токен под ГЕНЕРАЦИЮ в Suno — у неё свой ключ, не общий и не от входа."""
    return solve(SUNO_SITE_KEY_GEN, page_url)
