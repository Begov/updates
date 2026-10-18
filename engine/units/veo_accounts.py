"""Fetch and format VeoNonStop account limits for GUI status widgets."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any


def _slot_value(config_module, slot: int, base_name: str, default: str = "") -> str:
    suffix = "" if slot == 1 else f"_{slot}"
    return str(getattr(config_module, f"{base_name}{suffix}", default) or "").strip()


def configured_accounts(config_module) -> list[dict[str, Any]]:
    accounts = []
    default_base = str(
        getattr(config_module, "VEONONSTOP_API_BASE", "https://veononstop.org/api/v1")
    ).strip()
    for slot in range(1, 5):
        api_key = _slot_value(config_module, slot, "VEONONSTOP_API_KEY")
        if not api_key:
            continue
        name = str(
            getattr(config_module, f"VEONONSTOP_KEY{slot}_NAME", f"Key{slot}")
            or f"Key{slot}"
        ).strip()
        base_url = _slot_value(
            config_module, slot, "VEONONSTOP_API_BASE", default_base
        ) or default_base
        accounts.append(
            {
                "slot": str(slot),
                "name": name,
                "api_key": api_key,
                "base_url": base_url,
            }
        )
    return accounts


def _classify_error(exc: Exception) -> str:
    message = str(exc).lower()
    if "expired" in message:
        return "expired"
    if "revoked" in message:
        return "revoked"
    if "401" in message or "unauthorized" in message or "invalid api key" in message:
        return "unauthorized"
    if "503" in message or "unavailable" in message or "timeout" in message:
        return "unavailable"
    return "error"


def fetch_accounts(config_module) -> dict[str, Any]:
    from units.veo_client import VeoNonStopClient

    slots: dict[str, dict[str, Any]] = {}
    for account in configured_accounts(config_module):
        slot = account["slot"]
        item: dict[str, Any] = {
            "slot": slot,
            "name": account["name"],
            "ok": False,
            "updated_at": time.time(),
        }
        try:
            client = VeoNonStopClient(
                api_key=account["api_key"],
                base_url=account["base_url"],
            )
            info = client.get_account_info()
            usage = client.get_account_usage()
            # API v2 отдаёт те же числа ДРУГИМИ полями, а старые приходят
            # пустыми или нулём. Без этого шапка показывала «0» на живом ключе
            # с полутора тысячами картинок в запасе (замечание пользователя
            # 02.09.2026): лимит картинок лежит в `image_daily_quota`, а
            # предел параллели — в `admission_usage.limit`.
            _img = usage.get("image_daily_quota")
            _img = _img if isinstance(_img, dict) else {}
            _adm = usage.get("admission_usage")
            _adm = _adm if isinstance(_adm, dict) else {}

            def _first(*values):
                """Первое НЕПУСТОЕ значение: ноль на v2 означает «не заполнено»."""
                for value in values:
                    if value not in (None, 0, ''):
                        return value
                return values[-1] if values else None

            item.update(
                {
                    "ok": True,
                    "plan": info.get("plan"),
                    "concurrent_tasks": info.get("concurrent_tasks"),
                    "expires_at": info.get("expires_at"),
                    "max_concurrent_tasks": _first(
                        usage.get("max_concurrent_tasks"),
                        _adm.get("limit"),
                        info.get("concurrent_tasks")),
                    "active_tasks": _first(usage.get("active_tasks"),
                                           _adm.get("active_admissions"), 0),
                    "queued_tasks": usage.get("queued_tasks"),
                    "cookies_allocated": _first(usage.get("cookies_allocated"),
                                                _adm.get("available_units")),
                    "video_used": usage.get("video_used"),
                    "video_limit": usage.get("video_limit"),
                    "video_remaining": usage.get("video_remaining"),
                    "image_used": _first(usage.get("image_used"), _img.get("used")),
                    "image_limit": _first(usage.get("image_limit"), _img.get("limit")),
                    "image_remaining": _first(usage.get("image_remaining"),
                                              _img.get("remaining")),
                    "unlimited": bool(usage.get("unlimited")),
                    "resets_in_seconds": usage.get("resets_in_seconds"),
                }
            )
        except Exception as exc:
            item.update(
                {
                    "error_kind": _classify_error(exc),
                    "error": str(exc)[:240],
                }
            )
        slots[slot] = item
    return {
        "ok": any(item.get("ok") for item in slots.values()),
        "slots": slots,
        "updated_at": time.time(),
    }


def live_image_slots(config_module, exclude_slots: tuple = ()) -> list[dict[str, Any]]:
    """Слоты VeoNonStop, у которых РЕАЛЬНО осталась квота картинок.

    Общая точка правды для обоих направлений фолбэка: fastgen.py спрашивает её
    перед тем как отдать остаток бананы, imggen.py — когда его собственный ключ
    выжег дневную квоту и надо найти живой запасной.

    Живой = unlimited, либо image_remaining > 0. Слот, который не ответил или
    отдал 0, не возвращается: перемаппить остаток на мёртвый ключ хуже, чем
    честно об этом сказать (прогон «пикассо» 17.08.2026 — VeoNonStop banana
    выжгла дневной лимит и 4 цикла молотила 429 впустую).

    exclude_slots — слоты, которые уже признаны выжженными в текущем прогоне.
    """
    from units.veo_client import VeoNonStopClient

    excluded = {int(s) for s in exclude_slots}
    live: list[dict[str, Any]] = []
    for account in configured_accounts(config_module):
        slot = int(account['slot'])
        name = account['name']
        if slot in excluded:
            continue
        try:
            usage = VeoNonStopClient(
                api_key=account['api_key'], base_url=account['base_url'],
            ).get_account_usage()
        except Exception as exc:
            logging.warning(f"[VEO CHECK] slot {slot} ({name}): usage request failed — {exc}")
            print(f"  [VEO CHECK] {name}: usage request failed ({str(exc)[:100]})")
            continue

        unlimited = bool(usage.get('unlimited'))
        remaining = usage.get('image_remaining')
        try:
            remaining_int = int(remaining) if remaining is not None else None
        except (TypeError, ValueError):
            remaining_int = None

        # remaining=None при успешном запросе — сервер не отдал счётчик картинок.
        # Считаем слот живым: ключ отвечает, а вердикт «мёртв» без данных был бы
        # выдумкой (движок всё равно упрётся в 429 и вернёт клипы обратно).
        if unlimited or remaining_int is None or remaining_int > 0:
            live.append({
                'slot': slot,
                'name': name,
                'remaining': 'unlimited' if unlimited else (
                    '?' if remaining_int is None else remaining_int),
            })
            logging.info(f"[VEO CHECK] slot {slot} ({name}): alive, image_remaining={remaining}")
        else:
            logging.warning(f"[VEO CHECK] slot {slot} ({name}): image quota exhausted (remaining=0)")
            print(f"  [VEO CHECK] {name}: image quota exhausted (0 left)")
    return live


def format_duration(seconds: Any) -> str:
    try:
        value = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return "?"
    days, value = divmod(value, 86400)
    hours, value = divmod(value, 3600)
    minutes = value // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def format_expiration(value: Any) -> str:
    if not value:
        return "unknown"
    text = str(value).strip().replace("Z", "+00:00")
    try:
        expires = datetime.fromisoformat(text)
        return expires.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(value)


def compact_text(item: dict[str, Any]) -> str:
    name = str(item.get("name") or f"Key{item.get('slot', '?')}")[:8]
    if not item.get("ok"):
        status = {
            "expired": "expired",
            "revoked": "revoked",
            "unauthorized": "invalid",
            "unavailable": "offline",
        }.get(item.get("error_kind"), "error")
        return f"{name}: {status}"
    remaining = item.get("image_remaining")
    reset = format_duration(item.get("resets_in_seconds"))
    if remaining is not None:
        return f"{name} \U0001f34c{int(remaining):,} {reset}"
    used = item.get("video_used")
    return f"{name} V:{int(used or 0):,} {reset}"


def quota_row_text(item: dict[str, Any]) -> str:
    name = str(item.get("name") or f"Key{item.get('slot', '?')}")
    if not item.get("ok"):
        status = {
            "expired": "expired",
            "revoked": "revoked",
            "unauthorized": "invalid key",
            "unavailable": "temporarily unavailable",
        }.get(item.get("error_kind"), "request error")
        return f"{name}: {status}"
    remaining = item.get("image_remaining")
    limit = item.get("image_limit")
    reset = format_duration(item.get("resets_in_seconds"))
    remaining_text = f"{int(remaining):,}" if remaining is not None else "?"
    limit_text = f"{int(limit):,}" if limit is not None else "unlimited"
    return f"{name}: \U0001f34c {remaining_text} / {limit_text} | reset {reset}"


def slot_usage_text(item: dict[str, Any]) -> str:
    """Format current server concurrency without inventing missing counters."""
    limit = item.get("max_concurrent_tasks")
    if limit is None:
        limit = item.get("concurrent_tasks")
    limit_text = str(int(limit)) if isinstance(limit, (int, float)) else str(limit or "n/a")

    active = item.get("active_tasks")
    if active is None:
        return f"slots n/a/{limit_text}"

    active_text = str(int(active))
    queued = item.get("queued_tasks")
    if queued not in (None, 0, "0"):
        return f"{active_text}/{limit_text} +{int(queued)}q"
    return f"{active_text}/{limit_text}"


def tooltip_text(item: dict[str, Any]) -> str:
    name = str(item.get("name") or f"Key{item.get('slot', '?')}")
    if not item.get("ok"):
        return (
            f"VEONONSTOP KEY {item.get('slot', '?')}: {name}\n"
            f"Status: {item.get('error_kind', 'error')}\n"
            f"{item.get('error', 'Request failed')}\n"
            "Click to refresh all VeoNonStop keys."
        )
    active = item.get("active_tasks")
    if active is None:
        concurrency = (
            f"Concurrency: {slot_usage_text(item)}\n"
            "The current /account/usage response does not include active task counters.\n"
        )
    else:
        concurrency = (
            f"Concurrency: {slot_usage_text(item)}"
            f" (queued: {int(item.get('queued_tasks') or 0)})\n"
        )
    return (
        f"VEONONSTOP KEY {item.get('slot')}: {name}\n"
        f"Plan: {str(item.get('plan') or '?').upper()}\n"
        f"{concurrency}"
        f"Images: {item.get('image_remaining', '?')} remaining"
        f" / {item.get('image_limit', '?')} limit"
        f" ({item.get('image_used', '?')} used)\n"
        f"Videos used: {item.get('video_used', '?')}\n"
        f"Quota reset: {format_duration(item.get('resets_in_seconds'))}\n"
        f"Key expires: {format_expiration(item.get('expires_at'))}\n"
        "Click to refresh all VeoNonStop keys."
    )
