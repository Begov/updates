"""Быстрая проверка: в каком состоянии CinAI прямо сейчас.

Провайдер то отдаёт клип за 40 секунд, то уходит в шторм — и тогда тот же клип
считается 4-5 минут или падает (`Provider halcion unavailable`, HTTP 520/524).
Понять это можно только по его же журналу запусков, поэтому читаем
`GET /api/executions` и считаем медиану и долю сбоев за последние минуты.

Пользы две: не начинать долгий прогон в разгар шторма и не гадать «почему
сегодня так медленно».
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import requests

import params as config
_API = "https://api.cinai.tv/api/executions"


def _parse(stamp: Optional[str]) -> Optional[datetime]:
    if not stamp:
        return None
    try:
        return datetime.strptime(str(stamp)[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def fetch_recent(limit: int = 60, minutes: int = 20,
                 token: Optional[str] = None) -> List[Dict[str, Any]]:
    """Запуски аккаунта за последние `minutes` минут (по часам сервера)."""
    access = token or getattr(config, "CINAI_ACCESS_TOKEN", "")
    if not access:
        return []
    try:
        response = requests.get(
            _API,
            headers={"Authorization": f"Bearer {access}",
                     "X-App-Id": "runway-workflows", "Accept": "application/json"},
            params={"limit": limit}, timeout=30)
        items = (response.json() or {}).get("items") or []
    except (requests.RequestException, ValueError):
        return []
    stamps = [s for s in (_parse(i.get("created_date")) for i in items) if s]
    if not stamps:
        return []
    newest = max(stamps)
    window = timedelta(minutes=max(1, minutes))
    return [i for i in items
            if (_parse(i.get("created_date")) or newest) >= newest - window]


def snapshot(minutes: int = 20) -> Dict[str, Any]:
    """Сводка: медиана времени клипа, доля сбоев, самые частые ошибки."""
    items = fetch_recent(minutes=minutes)
    if not items:
        return {"known": False}
    done, failed, durations = 0, 0, []
    reasons: Dict[str, int] = {}
    for item in items:
        status = str(item.get("status") or "")
        if status == "completed":
            done += 1
            start, end = _parse(item.get("started_at")), _parse(item.get("completed_at"))
            if start and end:
                durations.append((end - start).total_seconds())
        elif status == "failed":
            failed += 1
            text = str(item.get("error") or "")[:60]
            if text:
                reasons[text] = reasons.get(text, 0) + 1
    median = statistics.median(durations) if durations else None
    total = done + failed
    return {
        "known": True,
        "window_minutes": minutes,
        "completed": done,
        "failed": failed,
        "fail_rate": (failed / total) if total else 0.0,
        "median_seconds": median,
        "slowest_seconds": max(durations) if durations else None,
        "top_errors": sorted(reasons.items(), key=lambda kv: -kv[1])[:3],
    }


def describe(minutes: int = 20) -> str:
    """Одна строка для лога/GUI: понятно даже без цифр в голове."""
    data = snapshot(minutes)
    if not data.get("known"):
        return "CinAI: состояние неизвестно (журнал запусков недоступен)"
    median = data["median_seconds"]
    if median is None:
        return (f"CinAI: за {minutes} мин успешных запусков нет, "
                f"сбоев {data['failed']}")
    if median <= 90 and data["fail_rate"] < 0.15:
        verdict = "норма"
    elif median <= 180 and data["fail_rate"] < 0.3:
        verdict = "медленно"
    else:
        verdict = "штормит"
    line = (f"CinAI: {verdict} — клип за {median:.0f}с (медиана), "
            f"успешных {data['completed']}, сбоев {data['failed']} "
            f"за {minutes} мин")
    if data["top_errors"]:
        line += f" | чаще всего: {data['top_errors'][0][0]}"
    return line
