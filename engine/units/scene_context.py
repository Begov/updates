"""Deterministic context packets for non-ImageGen visual source stages.

This module is intentionally independent from the ImageGen prompt contract.
It only prepares compact disambiguation context for Real Photo/EPF, Stock,
and future Auto Visual Story flows.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional


def _text(value: Any, limit: int = 320) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit].strip()


def _clip_id(clip: Dict[str, Any]) -> Optional[int]:
    try:
        return int(clip.get("id"))
    except (TypeError, ValueError):
        return None


def _voiceover(clip: Dict[str, Any], fragments: Iterable[Dict[str, Any]]) -> str:
    for key in ("voiceover", "text", "narration", "transcript"):
        value = _text(clip.get(key), 600)
        if value:
            return value
    try:
        start = float(clip.get("startTime", 0) or 0)
        end = float(clip.get("endTime", 0) or 0)
    except (TypeError, ValueError):
        start, end = 0.0, 0.0
    parts = []
    for fragment in fragments:
        if not isinstance(fragment, dict):
            continue
        try:
            f_start = float(fragment.get("start", 0) or 0)
            f_end = float(fragment.get("end", 0) or 0)
        except (TypeError, ValueError):
            continue
        if f_start < end and f_end > start:
            text = _text(fragment.get("text"), 600)
            if text:
                parts.append(text)
    return _text(" ".join(parts), 600)


def _existing_facts(clip: Dict[str, Any]) -> Dict[str, Any]:
    """Copy only facts already present in the plan; never infer new entities."""
    facts: Dict[str, Any] = {}
    for key in ("entities", "resolved_entities", "resolved_facts", "hidden_entities",
                "revealed_entities", "scene_role", "chain_id", "reveal_state"):
        value = clip.get(key)
        if value not in (None, "", [], {}):
            facts[key] = value
    return facts


def _visual_reference(clip: Dict[str, Any]) -> str:
    for key in ("visual_intent", "visual_description", "visual_prompt",
                "prompt", "description"):
        value = _text(clip.get(key), 260)
        if value:
            return value
    return ""


def build_context_cards(
    clips: List[Dict[str, Any]],
    fragments: Optional[Iterable[Dict[str, Any]]] = None,
    montage: Optional[Dict[str, Any]] = None,
    *,
    before: int = 1,
    after: int = 1,
) -> List[Dict[str, Any]]:
    """Return source-stage cards with compact neighboring-scene context."""
    fragments_list = list(fragments or [])
    ordered = list(clips or [])
    theme = ""
    if isinstance(montage, dict):
        theme = _text(montage.get("theme"), 260)
        if not theme and isinstance(montage.get("montage_plan"), dict):
            theme = _text(montage["montage_plan"].get("theme"), 260)

    voices = [_voiceover(clip, fragments_list) for clip in ordered]
    result: List[Dict[str, Any]] = []
    for index, clip in enumerate(ordered):
        card = dict(clip)
        voice = voices[index]
        if voice and not _text(card.get("voiceover")):
            card["voiceover"] = voice

        previous = [
            {"clip_id": _clip_id(ordered[pos]), "voiceover": voices[pos]}
            for pos in range(max(0, index - before), index)
            if _clip_id(ordered[pos]) is not None and voices[pos]
        ]
        following = [
            {"clip_id": _clip_id(ordered[pos]), "voiceover": voices[pos]}
            for pos in range(index + 1, min(len(ordered), index + 1 + after))
            if _clip_id(ordered[pos]) is not None and voices[pos]
        ]
        context: Dict[str, Any] = {
            "context_before": previous,
            "context_after": following,
        }
        if theme:
            context["theme"] = theme
        facts = _existing_facts(clip)
        if facts:
            context["resolved_facts"] = facts
        # Описание ТЕКУЩЕЙ сцены от режиссёра (Auto Visual Story, этап ②):
        # раньше до чат-писателя запросов доезжала только озвучка (улов 25.08).
        # Только режиссёрские поля — художественный prompt Pass 2 писателю
        # запросов не даём (см. real_photo._build_clip_cards).
        brief = ""
        for key in ("visual_intent", "visual_description", "description"):
            brief = _text(clip.get(key), 260)
            if brief:
                break
        if brief:
            context["visual_brief"] = brief
        # Тема ГЛАВЫ (эпоха + место) — якорь запроса точнее общей темы ролика:
        # глава про сегодняшний день внутри фильма про 1980-е ищется как
        # сегодняшний день (решение пользователя 25.08).
        chapter_theme = _text((clip.get("visual_story") or {}).get("chapter_theme"), 200)
        if chapter_theme:
            context["chapter_theme"] = chapter_theme
        if index:
            reference = _visual_reference(ordered[index - 1])
            if reference:
                context["previous_visual"] = reference
        card["context"] = context
        result.append(card)
    return result


def compact_context(card: Dict[str, Any]) -> Dict[str, Any]:
    """Return only fields safe to expose to a source-query LLM."""
    context = card.get("context")
    if not isinstance(context, dict):
        return {}
    allowed = ("theme", "chapter_theme", "visual_brief", "context_before",
               "context_after", "previous_visual", "resolved_facts")
    return {key: context[key] for key in allowed if context.get(key) not in (None, "", [], {})}
