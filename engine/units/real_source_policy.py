"""Deterministic non-AI image-source allocation for montage plans.

The director still receives the visual brief and is asked to mark sources, but
this module is the final authority when the user enables NON_AI_IMAGE_MODE.
It guarantees that every clip has one of the enabled source markers and that
the ordinary image/video generators are never used as a fallback.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

import params as config
SOURCE_ORDER = ("real_photo", "infographic", "stock", "youtube")
PENDING_MARKERS = {
    "real_photo": "real_photo_pending",
    "infographic": "infographic",
    "stock": "stock_pending",
    "youtube": "youtube_pending",
}
COMPLETED_MARKERS = {
    "real_photo": {"real_photo", "real_video"},
    "infographic": {"infographic"},
    "stock": {"stock_photo", "stock_video"},
    "youtube": {"youtube", "youtube_video"},
}


def enabled() -> bool:
    value = os.environ.get(
        "NON_AI_IMAGE_MODE",
        "1" if getattr(config, "NON_AI_IMAGE_MODE", False) else "0",
    )
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _percent(name: str, default: int) -> int:
    raw = os.environ.get(name, getattr(config, name, default))
    try:
        return max(0, min(100, int(float(raw))))
    except (TypeError, ValueError):
        return default


_SOURCE_PERCENT_KEYS = {
    "real_photo": ("NON_AI_REAL_PHOTO_PERCENT", 50),
    "infographic": ("NON_AI_INFOGRAPHIC_PERCENT", 25),
    "stock": ("NON_AI_STOCK_PERCENT", 25),
    "youtube": ("NON_AI_YOUTUBE_PERCENT", 25),
}


def enabled_sources() -> list[str]:
    """Источники, включённые галкой И имеющие ненулевую долю.

    Ноль означает НОЛЬ (правило пользователя 25.08): источник с долей 0 не
    попадает ни в распределение, ни в директиву режиссёру. Раньше он оставался
    в списке со строкой «infographic=0%», и режиссёр всё равно им пользовался."""
    result: list[str] = []
    flags = {
        "real_photo": getattr(config, "REAL_PHOTO_ENABLED", False),
        "infographic": getattr(config, "INFOGRAPHIC_ENABLED", False),
        "stock": getattr(config, "STOCK_ENABLED", False),
        "youtube": getattr(config, "YOUTUBE_FOOTAGE_ENABLED", False),
    }
    for name in ("real_photo", "infographic", "stock", "youtube"):
        if not flags[name]:
            continue
        key, default = _SOURCE_PERCENT_KEYS[name]
        if _percent(key, default) <= 0:
            continue
        result.append(name)
    return result


def allocation() -> dict[str, int]:
    """Return configured shares, normalized over enabled sources only."""
    configured = {
        "real_photo": _percent("NON_AI_REAL_PHOTO_PERCENT", 50),
        "infographic": _percent("NON_AI_INFOGRAPHIC_PERCENT", 25),
        "stock": _percent("NON_AI_STOCK_PERCENT", 25),
        "youtube": _percent("NON_AI_YOUTUBE_PERCENT", 25),
    }
    active = enabled_sources()
    if not active:
        raise RuntimeError(
            "NON_AI_IMAGE_MODE requires at least one enabled source: "
            "REAL_PHOTO, INFOGRAPHIC, STOCK, or YOUTUBE"
        )
    if "youtube" in active and "real_photo" not in active:
        raise RuntimeError(
            "NON_AI_IMAGE_MODE requires REAL_PHOTO_ENABLED when "
            "YOUTUBE_FOOTAGE_ENABLED is enabled: failed YouTube clips "
            "fallback to EPF/Real Photo"
        )
    total = sum(configured[name] for name in active)
    if total <= 0:
        raise RuntimeError(
            "NON_AI_IMAGE_MODE requires a positive percentage for at least "
            "one enabled source"
        )
    return {
        name: (configured[name] if name in active else 0)
        for name in SOURCE_ORDER
    }


def _counts(total: int, shares: dict[str, int], active: list[str]) -> dict[str, int]:
    """Largest-remainder allocation, deterministic and exact."""
    raw = {name: total * shares[name] / sum(shares[n] for n in active)
           for name in active}
    result = {name: int(raw[name]) for name in active}
    remaining = total - sum(result.values())
    ranked = sorted(
        active,
        key=lambda name: (raw[name] - result[name], -SOURCE_ORDER.index(name)),
        reverse=True,
    )
    for name in ranked[:remaining]:
        result[name] += 1
    return {name: result.get(name, 0) for name in SOURCE_ORDER}


def _plan_container(raw: dict[str, Any]) -> dict[str, Any]:
    return raw.get("montage_plan", raw) if isinstance(raw, dict) else raw


def apply_policy(
    montage_path: str | Path,
    *,
    printer: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Assign all clips to non-AI sources and persist the policy audit."""
    path = Path(montage_path)
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    plan = _plan_container(raw)
    clips = list(plan.get("clips") or [])
    active = enabled_sources()
    shares = allocation()
    counts = _counts(len(clips), shares, active)

    # Keep already materialized source clips in their source bucket. Fresh
    # plans are normally unmaterialized, so this mainly protects reruns.
    buckets: dict[str, list[dict[str, Any]]] = {name: [] for name in SOURCE_ORDER}
    unassigned: list[dict[str, Any]] = []
    for clip in clips:
        current = str(clip.get("source") or "")
        bucket = next(
            (name for name in active if current in COMPLETED_MARKERS[name]),
            None,
        )
        if bucket:
            buckets[bucket].append(clip)
        else:
            unassigned.append(clip)

    # If a rerun has more completed clips than the requested share, keep them
    # and allocate the remainder in source order. A new plan follows exact
    # counts from _counts().
    for name in SOURCE_ORDER:
        overflow = max(0, len(buckets[name]) - counts[name])
        if overflow:
            unassigned.extend(buckets[name][-overflow:])
            buckets[name] = buckets[name][:-overflow]

    for name in SOURCE_ORDER:
        need = max(0, counts[name] - len(buckets[name]))
        buckets[name].extend(unassigned[:need])
        unassigned = unassigned[need:]

    if unassigned:
        # This can only happen when completed media forced a bucket over its
        # target. Preserve the no-AI invariant by assigning leftovers to the
        # first enabled source.
        buckets[active[0]].extend(unassigned)

    source_by_id: dict[str, str] = {}
    for name in SOURCE_ORDER:
        marker = PENDING_MARKERS[name]
        for clip in buckets[name]:
            clip["source"] = (
                clip.get("source")
                if clip.get("source") in COMPLETED_MARKERS[name]
                else marker
            )
            source_by_id[str(clip.get("id"))] = name

    audit = {
        "enabled": True,
        "active_sources": active,
        "configured_percent": shares,
        "assigned_counts": {name: len(buckets[name]) for name in SOURCE_ORDER},
        "clip_ids": source_by_id,
    }
    plan["image_source_policy"] = audit
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    printer(
        "  [Non-AI] all clips assigned: "
        + " | ".join(
            f"{name}={audit['assigned_counts'][name]}"
            for name in SOURCE_ORDER
            if name in active
        )
    )
    return audit


def director_directive() -> str:
    if not enabled():
        return ""
    active = enabled_sources()
    shares = allocation()
    labels = ", ".join(f"{name}={shares[name]}%" for name in active)
    return f"""

---

## NON-AI IMAGE SOURCE MODE

The user disabled ordinary AI scene-image and AI video generation. Every clip
MUST be marked with exactly one enabled source marker: {", ".join(active)}.
Configured shares (normalized over enabled sources): {labels}.
Use `source: "real_photo_pending"` for archival material, `source:
"stock_pending"` for stock footage/photo, `source: "youtube_pending"` for real
moving YouTube/user footage, and `source: "infographic"` for a static
explanatory graphic. If a YouTube clip fails, the pipeline routes it to
`real_photo_pending` so EPF can resolve it. Do not leave any clip without a
source field and do not invent an AI fallback. The deterministic pipeline
validator will enforce the final counts after Pass 1, so keep the visual
choice meaningful even when the percentage is a hard allocation.
"""


def protect_unresolved(montage_path: str | Path, *, printer: Callable[[str], None] = print) -> int:
    """Restore pending markers for failed non-AI clips before concat."""
    if not enabled():
        return 0
    path = Path(montage_path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        plan = _plan_container(raw)
        policy = plan.get("image_source_policy") or {}
        ids = policy.get("clip_ids") or {}
        from units.source_ledger import clip_media_ready, final_marker_for

        project = path.parent
        changed = 0
        finalized = 0
        for clip in plan.get("clips") or []:
            cid = str(clip.get("id"))
            name = ids.get(cid)
            if not name:
                continue
            current = str(clip.get("source") or "")
            if current in COMPLETED_MARKERS[name]:
                continue
            # Сцена с готовым файлом НЕ нерешённая — возвращать ей `*_pending`
            # нельзя: склейка перестаёт узнавать архивный кадр (нет подписи
            # источника и blur-заливки), а повторный запуск шлёт стадию искать
            # заново поверх готового файла. Клип 10 прогона 26.08: EPF закрыл
            # сцену, защита откатила маркер, ролик вышел без атрибуции.
            if clip_media_ready(project, clip):
                # Маркер, которым стадия закрыла бы сцену сама. Стадию берём ту,
                # что реально дала файл: ютуб отдаёт незакрытые сцены архивам,
                # поэтому у него финал архивный.
                stage = "real_photo" if name == "youtube" else name
                final = final_marker_for(project, clip, stage)
                if final and current != final:
                    clip["source"] = final
                    finalized += 1
                    changed += 1
                continue
            marker = (
                "real_photo_pending"
                if name == "youtube"
                else PENDING_MARKERS[name]
            )
            if current != marker:
                clip["source"] = marker
                changed += 1
        if changed:
            path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
            protected = changed - finalized
            if protected:
                printer(f"  [Non-AI] protected {protected} unresolved clip(s) from AI fallback")
            if finalized:
                printer(f"  [Non-AI] {finalized} готовых клип(ов) получили финальный "
                        f"маркер вместо отката в ожидание")
        return changed
    except Exception as exc:
        printer(f"  [Non-AI] protection warning: {exc}")
        return 0
