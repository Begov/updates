"""Deterministic post-Pass-1 routing for optional external sources."""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any

import params as config
_PROTECTED = {
    "real_photo_pending",
    "real_photo",
    "real_video",
    "stock_pending",
    "stock_photo",
    "stock_video",
    "infographic",
    "youtube",
    "youtube_video",
    "motion_graphics_pending",
    "motion_graphics",
}

def _enabled(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _non_ai_enabled() -> bool:
    value = (
        __import__("os").environ.get(
            "NON_AI_IMAGE_MODE",
            "1" if getattr(config, "NON_AI_IMAGE_MODE", False) else "0",
        )
    )
    return _enabled(value)


def _clip_id(clip: dict[str, Any]) -> int:
    """Номер клипа; нечисловой id не должен ронять маршрутизацию Pass 1."""
    try:
        return int(clip.get("id", 0))
    except (TypeError, ValueError):
        return 0


def _scene_line(clip: dict[str, Any]) -> str:
    """Короткое описание сцены для оператора: чем клип занят."""
    for key in ("visual_intent", "description", "voiceover", "text", "narration"):
        value = str(clip.get(key) or "").strip()
        if value:
            return value[:300]
    return ""


def _topup_by_operator(free: list[dict[str, Any]], need: int) -> list[int]:
    """Спросить оператора, какие из свободных сцен стоит закрыть видео.

    Зовётся только при НЕДОБОРЕ: режиссёр разметил меньше доли, а бросать её
    незаполненной незачем. Критерия два, и оба обязательны:

      • сцена держится на ДВИЖЕНИИ — действие, процесс, речь. «Головка сыра на
        полке» ничего не теряет в фотографии, «рабочий переворачивает головку»
        без движения превращается в непонятную позу;
      • такое РЕАЛЬНО снимают. Описания Pass 2 — это постановочные мизансцены
        для генератора, с выдуманным реквизитом и расставленными людьми; искать
        их на ютубе бессмысленно, вижн такие кадры всё равно отвергнет.

    Раньше здесь работал словарь «слов движения» (traffic, crowd, работ…) — он
    подбирал по слову, а не по смыслу, и был удалён 26.08 вместе с добором.
    Возвращает id сцен в порядке пригодности, не длиннее `need`.
    """
    if need <= 0 or not free:
        return []
    lines = []
    for clip in free:
        text = _scene_line(clip)
        if not text:
            continue
        lines.append({"id": _clip_id(clip), "scene": text})
    if not lines:
        return []
    try:
        from units.operator_hub import ask_json, resolve_text_operator

        provider, model = resolve_text_operator("youtube")
        if not provider:
            logging.info("[маршрутизация] оператор не настроен — долю ютуба не добираю")
            return []
        task = (
            "Some scenes below could be covered with REAL video footage from "
            "YouTube instead of generated imagery. Pick up to "
            f"{need} of them — the ones that gain the most from real video.\n\n"
            "A scene qualifies only when BOTH are true:\n"
            "1. It lives on MOVEMENT — an action, a process, speech. A still "
            "photograph would lose the point of the shot. A wheel of cheese "
            "resting on a shelf loses nothing as a photo; hands turning that "
            "wheel become a meaningless pose without motion.\n"
            "2. Someone really films this. The descriptions are written for an "
            "image generator and often stage a shot — invented props, people "
            "placed just so, a moment arranged for effect. Such a frame does not "
            "exist as footage, and searching for it is wasted effort.\n\n"
            "Skip anything abstract, imagined, or built for the camera. Fewer is "
            "fine — return an empty list if none of the scenes qualify.\n\n"
            f"SCENES:\n{json.dumps(lines, ensure_ascii=False)}\n\n"
            'Answer as JSON: {"clip_ids": [12, 7]} — best first.'
        )
        data = ask_json(provider, model, task, phase="youtube-topup",
                        schema_file="youtube_topup.json", max_tokens=300, timeout=180)
        picked = []
        known = {_clip_id(clip) for clip in free}
        for value in (data or {}).get("clip_ids") or []:
            try:
                clip_id = int(value)
            except (TypeError, ValueError):
                continue
            if clip_id in known and clip_id not in picked:
                picked.append(clip_id)
        logging.info("[маршрутизация] недобор ютуба: оператор выбрал %s из %s свободных — %s",
                     len(picked), len(free), picked[:need])
        return picked[:need]
    except Exception as exc:                             # noqa: BLE001
        logging.warning("[маршрутизация] добор ютуба не удался (%s) — доля останется неполной", exc)
        return []


def apply_youtube_ratio_policy(montage_path: str | Path) -> dict[str, Any]:
    """Проверить долю ютуба в разметке Pass 1, не трогая другие источники.

    Набор клипов — дело РЕЖИССЁРА, как у стока и Real Photo: он читает озвучку и
    видит, что вообще снимали на видео. Здесь остаётся только контроль доли:
    снять лишние метки, если режиссёр перебрал максимум.

    Раньше эта функция ещё и ДОБИРАЛА долю сама: брала клипы без маркера и
    сортировала их по числу «слов движения» из ручного словаря (traffic, crowd,
    machinery, работ, улиц…). Признак был неверным — ютуб ценен не движением, а
    тем, что там есть съёмка конкретного: названного человека, события, узкого
    процесса. Вдобавок добор шёл только по клипам БЕЗ маркера, то есть самое
    ютубное (названные люди) забирал Real Photo ещё в Pass 1, а ютубу доставались
    остатки. Признак убран 26.08; директива разметки переписана под настоящий
    критерий (`youtube_markup.py`).

    Non-AI allocation remains authoritative in ``non_ai_image_policy.py``.
    Existing Stock/EPF markers are preserved.
    """
    path = Path(montage_path)
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    plan = raw.get("montage_plan", raw) if isinstance(raw, dict) else raw
    clips = list(plan.get("clips") or [])

    if not getattr(config, "YOUTUBE_FOOTAGE_ENABLED", False) or _non_ai_enabled():
        removed = 0
        if not getattr(config, "YOUTUBE_FOOTAGE_ENABLED", False):
            for clip in clips:
                if clip.get("source") == "youtube_pending":
                    clip.pop("source", None)
                    removed += 1
            # Снятые метки НАДО записать: раньше здесь стоял ранний return, а
            # единственная запись файла — в самом конце функции, поэтому правки
            # оставались только в памяти. Клип с мёртвым «youtube_pending» потом
            # выбрасывали генераторы (он числится внешним источником), а
            # ютуб-стадия его не брала — она выключена (аудит 25.08).
            if removed:
                path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
                logging.info("[маршрутизация] ютуб выключен — снято меток: %s", removed)
        return {"enabled": False, "assigned": 0, "total": len(clips), "cleared": removed}

    ratio = max(0.0, min(1.0, float(getattr(config, "YOUTUBE_RATIO_TARGET", 0.0) or 0.0)))
    ratio_max = max(ratio, min(1.0, float(getattr(config, "YOUTUBE_RATIO_MAX", 1.0) or 1.0)))
    target = min(len(clips), int(round(len(clips) * ratio)))
    maximum = (
        0
        if ratio <= 0
        else min(len(clips), int(math.ceil(len(clips) * ratio_max)))
    )

    marked = [
        clip for clip in clips
        if clip.get("source") in {"youtube_pending", "youtube", "youtube_video"}
    ]
    if len(marked) > maximum:
        # Оставляем и снимаем ОДИН И ТОТ ЖЕ набор. Раньше метки снимались с
        # клипов, отсортированных по id, а `marked` урезался в порядке плана —
        # в итоге аудит перечислял клипы без метки и терял те, что метку
        # сохранили (аудит 25.08). Лишнее режем с конца: раньше по времени —
        # важнее.
        ordered = sorted(marked, key=_clip_id)
        keep, drop = ordered[:maximum], ordered[maximum:]
        for clip in drop:
            if clip.get("source") == "youtube_pending":
                clip.pop("source", None)
        marked = keep

    # Недобор: режиссёр пометил меньше доли. Своих меток маршрутизатор не
    # ставит, но и бросать долю незаполненной незачем — спрашиваем оператора,
    # какие из СВОБОДНЫХ сцен выигрывают от живого видео.
    topped_up: list[int] = []
    if len(marked) < target:
        free = [clip for clip in clips if not clip.get("source")]
        for clip_id in _topup_by_operator(free, target - len(marked)):
            for clip in free:
                if _clip_id(clip) == clip_id and not clip.get("source"):
                    clip["source"] = "youtube_pending"
                    marked.append(clip)
                    topped_up.append(clip_id)
                    break

    audit = {
        "enabled": True,
        "topped_up": topped_up,
        "target_ratio": ratio,
        "max_ratio": ratio_max,
        "target_count": target,
        "assigned": len(marked),
        "clip_ids": [_clip_id(clip) for clip in marked if clip.get("id") is not None],
    }
    plan["youtube_routing"] = audit
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    return audit
