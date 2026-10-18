"""Этап ② Auto Visual Story: сочиняет, что на экране — параллельно по главам.

Вход: сцены этапа ① (границы, роль, глава, текст) и карта истории (тема,
главы, реестр сущностей). Чанк = одна глава (длинная делится по сценам кодом),
у каждого чанка — весь реестр и сводки соседних глав, поэтому «она» в сцене 200
разрешается по реестру, а не по хвосту предыдущей пачки. Чанки независимы и
идут одновременно.

Выход — планы в той же форме, что читают `_allocate_source_plans` и
`_build_montage`: поисковых запросов здесь НЕТ, их пишут стадии по маркерам
(как в стандартном режиме).
"""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import params as config
from units.director_modes import coerce_json_list

logger = logging.getLogger(__name__)

# "ai" — кадр, которого нет ни в одном архиве (постановочная мизансцена):
# он уходит в обычную нейрогенерацию, а не в поиск (решение пользователя 25.08).
SOURCE_NAMES = ("epf", "stock", "youtube", "ai")

# Гейт шаблонных описаний (решение 25.08): ниже этой доли уникальных описаний
# в чанке — повтор; одно описание у стольких сцен подряд — повтор.
TEMPLATE_MIN_UNIQUE_RATIO = 0.6
TEMPLATE_MAX_RUN = 3


# --------------------------------------------------------------------- чанки
def split_into_chunks(
    scenes: list[dict[str, Any]],
    story_map: dict[str, Any],
    *,
    max_scenes: int,
) -> list[dict[str, Any]]:
    """Чанк = глава в порядке сцен; глава длиннее max_scenes режется кодом."""
    max_scenes = max(1, int(max_scenes))
    by_id = {str(c.get("id")): c for c in story_map.get("chapters") or []}
    order: list[str] = []
    groups: dict[str, list[dict[str, Any]]] = {}
    for scene in scenes:
        chapter_id = str(scene.get("chapter_id") or "")
        if chapter_id not in groups:
            groups[chapter_id] = []
            order.append(chapter_id)
        groups[chapter_id].append(scene)

    chunks: list[dict[str, Any]] = []
    for position, chapter_id in enumerate(order):
        group = groups[chapter_id]
        parts = [group[i:i + max_scenes] for i in range(0, len(group), max_scenes)]
        previous = by_id.get(order[position - 1], {}) if position > 0 else {}
        following = by_id.get(order[position + 1], {}) if position + 1 < len(order) else {}
        for part_index, part in enumerate(parts, 1):
            chunks.append({
                "index": len(chunks) + 1,
                "chapter_id": chapter_id,
                "summary": str(by_id.get(chapter_id, {}).get("summary") or ""),
                "part": (part_index, len(parts)),
                "previous_summary": str(previous.get("summary") or ""),
                "next_summary": str(following.get("summary") or ""),
                "scenes": part,
            })
    return chunks


def forbidden_entities(story_map: dict[str, Any], last_scene_id: int) -> list[str]:
    """Сущности, которые впервые появляются ПОСЛЕ чанка — показывать нельзя."""
    result = []
    for entity in story_map.get("entities") or []:
        first = entity.get("first_appearance_scene")
        if isinstance(first, int) and first > int(last_scene_id):
            result.append(str(entity.get("id")))
    return result


# ------------------------------------------------------------------ проверки
def template_verdict(plans: list[dict[str, Any]]) -> str:
    """Пустая строка — описания живые; иначе причина (шаблон/пустота)."""
    descriptions = [str(p.get("description") or "").strip() for p in plans]
    if not descriptions:
        return "no plans"
    empty = sum(1 for d in descriptions if not d)
    if empty:
        return f"{empty} scene(s) have an empty description"
    unique_ratio = len(set(descriptions)) / len(descriptions)
    if len(descriptions) >= 3 and unique_ratio < TEMPLATE_MIN_UNIQUE_RATIO:
        return (
            f"only {len(set(descriptions))} distinct descriptions for "
            f"{len(descriptions)} scenes — the same sentence is reused"
        )
    run = 1
    for previous, current in zip(descriptions, descriptions[1:]):
        run = run + 1 if current == previous else 1
        if run >= TEMPLATE_MAX_RUN:
            return f"one description repeated for {run} consecutive scenes"
    return ""


def ids_verdict(plans: list[dict[str, Any]], chunk: dict[str, Any]) -> str:
    expected = [int(s["id"]) for s in chunk["scenes"]]
    try:
        actual = [int(p.get("scene_id")) for p in plans]
    except (TypeError, ValueError) as exc:
        logger.debug("[visual-story/chapter] нечитаемый scene_id в ответе: %s", exc)
        return "a plan has no valid scene_id"
    if actual != expected:
        return f"scene_ids {actual} do not match the chunk {expected}"
    return ""


# ------------------------------------------------------------- нормализация
def _string_list(value: Any, *, limit: int = 12) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value if str(item).strip()][:limit]


def entity_labels(story_map: dict[str, Any]) -> dict[str, str]:
    """id сущности → человекочитаемая метка из реестра этапа ①."""
    labels: dict[str, str] = {}
    for entity in story_map.get("entities") or []:
        key = str(entity.get("id") or "").strip()
        label = str(entity.get("label") or "").strip()
        if key and label:
            labels[key] = label
    return labels


def _delabel(text: str, labels: dict[str, str]) -> str:
    """Заменить id сущностей на их метки прямо в тексте описания.

    Оператор иногда пишет в описание сам идентификатор («several
    recognisable_artists entering a venue») — а описание уходит стадиям как
    основа поискового запроса, и такое слово там мусор: оно не гуглится и
    съедает место, где должен стоять субъект. Промпт это запрещает, но
    полагаться на одно лишь указание нельзя — правим и в коде (улов прогона
    25.08). Длинные id идут первыми, чтобы «home_city» не съел «home_city_hall».
    """
    if not text or not labels:
        return text
    for key in sorted(labels, key=len, reverse=True):
        # Только id-подобные ключи: «home_city» в тексте — мусор, а «border» —
        # обычное английское слово, и слепая замена ломала здоровую фразу
        # («a border crossing» → «a border separating foreign popularity…
        # crossing»). Ключи без подчёркивания трогать нельзя.
        if "_" not in key or key not in text:
            continue
        # Метка реестра пишется как справка («Colour entertainment press,
        # mid-1980s»); в живую фразу идёт только её ядро до запятой, иначе
        # период задваивается прямо посреди предложения.
        label = labels[key].split(",")[0].strip()
        if not label:
            continue

        def _put(match: "re.Match[str]", label: str = label) -> str:
            head = text[:match.start()].rstrip()
            # Метка нередко начинается с артикля («The female artist's home
            # city»), а в тексте артикль уже стоит: «past the home_city» дало бы
            # «past the the female artist's…». Свой артикль тогда снимаем.
            if re.search(r"\b(the|a|an|this|that|these|those)$", head, re.I):
                label = re.sub(r"^(?:the|a|an)\s+", "", label, flags=re.I)
            starts_sentence = not head or head[-1] in ".!?:;"
            if starts_sentence:
                return label[0].upper() + label[1:]
            return label[0].lower() + label[1:] if label[:1].isupper() else label

        text = re.sub(rf"(?<![\w-]){re.escape(key)}(?![\w-])", _put, text)
    return text


def _normalize(raw: dict[str, Any], scene: dict[str, Any], chunk_ids: set[int],
               *, allow_reuse: bool, labels: dict[str, str] | None = None) -> dict[str, Any]:
    scene_id = int(scene["id"])
    description = str(raw.get("description") or "").strip() or str(scene.get("text") or "").strip()
    description = _delabel(description, labels or {})
    source = str(raw.get("source") or "epf").strip().lower()
    if source not in SOURCE_NAMES:
        source = "epf"
    if source == "ai" and not ai_generation_available():
        source = "epf"          # нейрогенерации в прогоне нет — искать придётся
    media = "photo" if str(raw.get("preferred_media") or "").strip().lower() == "photo" else "video"
    reuse_of = raw.get("reuse_of")
    try:
        reuse_of = int(reuse_of) if reuse_of is not None else None
    except (TypeError, ValueError):   # нечисловой номер донора в ответе — реюза не будет
        reuse_of = None
    # Реюз — только внутри главы и только на более раннюю сцену (решение О3).
    if reuse_of is not None and (
        not allow_reuse or reuse_of >= scene_id or reuse_of not in chunk_ids
    ):
        reuse_of = None
    same_location = raw.get("same_location_as_scene")
    try:
        same_location = int(same_location) if same_location is not None else None
    except (TypeError, ValueError):   # нечисловая ссылка на локацию — связь не ставим
        same_location = None
    if same_location is not None and same_location >= scene_id:
        same_location = None
    return {
        "scene_id": scene_id,
        "visual_strategy": "reuse_previous" if reuse_of is not None else "new_search",
        "reuse_clip_id": reuse_of,
        "visual_intent": description,
        "description": description,
        # Запросы пишут стадии по маркерам — здесь их нет.
        "search_query": "",
        "search_queries": [],
        "fallback_queries": [],
        "youtube_queries": [],
        "must_have": [],
        "preferred_source": source,
        "media_type": media,
        "entities": _string_list(raw.get("entities")),
        "continuity": {
            "same_location_as_scene": same_location,
            "keep_entities": _string_list(raw.get("keep_entities")),
            "must_not_reveal": _string_list(raw.get("must_not_reveal")),
        },
        "negative_constraints": _string_list(raw.get("must_not_reveal")),
        "confidence": 0.5,
        "reason": "",
    }


def _mock_plan(scene: dict[str, Any]) -> dict[str, Any]:
    return {
        "scene_id": int(scene["id"]),
        "description": str(scene.get("text") or "").strip() or f"scene {scene['id']}",
        "entities": [],
        "source": "epf",
        "preferred_media": "photo",
        "reuse_of": None,
        "same_location_as_scene": None,
        "keep_entities": [],
        "must_not_reveal": [],
    }


# ------------------------------------------------------------------- промпт
def ai_generation_available() -> bool:
    """Доступна ли обычная нейрогенерация кадра в этом прогоне.

    В режиме «только не-AI источники» её нет — значит сцену НЕЛЬЗЯ отдать
    источнику "ai", и описание обязано быть таким, какое реально существует
    в архивах (решение пользователя 25.08)."""
    try:
        from units.real_source_policy import enabled as non_ai_enabled
        return not non_ai_enabled()
    except Exception:  # noqa: BLE001
        return not bool(getattr(config, "NON_AI_IMAGE_MODE", False))


def source_policy_text() -> str:
    lines = []
    # Постановочные кадры (составная мизансцена, выдуманные детали) в архивах
    # не существуют: вижн такие кандидаты честно бракует, и клип уходит в пустоту.
    # Поэтому либо отдаём их нейрогенерации, либо — когда её нет — требуем
    # переписать описание в архивно-достижимое.
    if ai_generation_available():
        lines.append(
            "AI image generation is available in this run. Give a scene source "
            "\"ai\" in either of two cases, and describe it freely there:\n"
            "  (1) the frame you have in mind is a STAGED composition no archive "
            "would hold — a made-up arrangement of objects, invented captions, "
            "dates or labels visible in the frame, several elements combined for "
            "effect;\n"
            "  (2) a staged frame simply SERVES THE STORY BETTER than any real "
            "photograph could — it carries the drama of the moment, pulls the "
            "viewer inside the scene, or makes the point land faster. A found "
            "photo of roughly the right subject is not automatically better than "
            "a frame built for this exact beat.\n"
            "Never send such a frame to \"epf\", \"stock\" or \"youtube\": it "
            "will be searched for, rejected and lost. Keep using the real sources "
            "for what they are good at — named people, real places, documented "
            "events, authentic period material."
        )
    else:
        lines.append(
            "AI image generation is DISABLED in this run: source \"ai\" is "
            "forbidden. Every scene must be findable as a real photograph or "
            "footage, so never describe a staged composition. Write what a real "
            "archive frame shows — the named subject, the place, the period, "
            "the kind of shot — and drop invented details (exact wording on a "
            "poster, specific dates side by side, objects arranged for effect)."
        )
    if getattr(config, "STOCK_ENABLED", False):
        share = float(getattr(config, "STOCK_RATIO_TARGET", 0.30) or 0.30)
        lines.append(
            f"Stock is enabled: give about {share:.0%} of the scenes source "
            "\"stock\" where generic modern footage fits."
        )
    else:
        lines.append("Stock is disabled: do not use source \"stock\".")
    if getattr(config, "YOUTUBE_FOOTAGE_ENABLED", False):
        share = float(getattr(config, "YOUTUBE_RATIO_TARGET", 0.0) or 0.0)
        lines.append(
            f"YouTube footage is enabled: give about {share:.0%} of the scenes "
            "source \"youtube\". Ask one question of the scene — did anyone ever "
            "FILM this? People film almost everything: named people and their "
            "speeches, events and newsreel, processes and crafts, machinery at "
            "work, animals, nature, streets, interiors. A scene can suit both "
            "this source and a photo archive; pick per scene by what the moment "
            "needs, and let the shares balance the rest."
        )
    else:
        lines.append("YouTube footage is disabled: do not use source \"youtube\".")
    lines.append("Everything else is source \"epf\".")
    return "\n".join(lines)


def build_chunk_prompt(
    template: str,
    chunk: dict[str, Any],
    story_map: dict[str, Any],
    *,
    project_name: str,
    style_guide: str,
) -> str:
    scenes_payload = [
        {
            "scene_id": int(s["id"]),
            "text": s.get("text", ""),
            "scene_role": s.get("scene_role", "event"),
            "startTime": s.get("startTime"),
            "endTime": s.get("endTime"),
        }
        for s in chunk["scenes"]
    ]
    last_id = int(chunk["scenes"][-1]["id"])
    forbidden = forbidden_entities(story_map, last_id)
    part_note = ""
    if chunk["part"][1] > 1:
        part_note = f" (part {chunk['part'][0]} of {chunk['part'][1]})"
    return (
        template
        .replace("{{PLAN_COUNT}}", str(len(scenes_payload)))
        .replace("{{PROJECT_NAME}}", project_name)
        .replace("{{THEME}}", str(story_map.get("theme") or ""))
        .replace("{{CHAPTER_ID}}", f"{chunk['chapter_id']}{part_note}")
        .replace("{{CHAPTER_SUMMARY}}", chunk["summary"] or "(no summary)")
        .replace("{{PREVIOUS_CHAPTER_SUMMARY}}", chunk["previous_summary"] or "(none — this is the first chapter)")
        .replace("{{NEXT_CHAPTER_SUMMARY}}", chunk["next_summary"] or "(none — this is the last chapter)")
        .replace("{{ENTITIES_JSON}}", json.dumps(story_map.get("entities") or [], ensure_ascii=False))
        .replace("{{FORBIDDEN_ENTITIES}}", json.dumps(forbidden, ensure_ascii=False) if forbidden else "(none)")
        .replace("{{SOURCE_POLICY}}", source_policy_text())
        .replace("{{STYLE_GUIDE}}", (style_guide or "")[:8000])
        .replace("{{SCENES_JSON}}", json.dumps(scenes_payload, ensure_ascii=False))
    )


# --------------------------------------------------------------------- вызов
def _plans_from_payload(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or "plans" not in payload:
        raise ValueError("Chapter response must be an object with a 'plans' list")
    return [p for p in coerce_json_list(payload["plans"], "plans") if isinstance(p, dict)]


def run_chunk(
    transport,
    template: str,
    chunk: dict[str, Any],
    story_map: dict[str, Any],
    *,
    raw_dir: Path,
    project_name: str,
    style_guide: str,
    allow_reuse: bool,
    mock: bool = False,
) -> list[dict[str, Any]]:
    """Один чанк: кэш на диске → вызов → гейты → один повтор → нормализация."""
    cache = raw_dir / f"chapter_{chunk['index']:02d}.json"
    if cache.exists():
        try:
            cached = json.loads(cache.read_text(encoding="utf-8-sig"))
            if isinstance(cached, dict) and len(cached.get("plans") or []) == len(chunk["scenes"]):
                logger.info("[visual-story/chapter] чанк %s взят с диска", chunk["index"])
                return cached["plans"]
        except (OSError, ValueError) as exc:
            logger.warning("[visual-story/chapter] кэш чанка %s не прочитан (%s) — "
                           "спрашиваю оператора заново", chunk["index"], exc)

    chunk_ids = {int(s["id"]) for s in chunk["scenes"]}
    if mock:
        raw_plans = [_mock_plan(s) for s in chunk["scenes"]]
    else:
        prompt = build_chunk_prompt(
            template, chunk, story_map, project_name=project_name, style_guide=style_guide,
        )
        phase = f"chapter_{chunk['index']:02d}"
        logger.info("   [этап ②] чанк %s (%s): сцен %s → оператору",
                    chunk["index"], chunk.get("chapter_id", "?"), len(chunk["scenes"]))
        raw_plans = _plans_from_payload(transport.call_json(
            phase=phase, prompt=prompt, schema_file="visual_story_chapter_plan.json",
        ))
        verdict = ids_verdict(raw_plans, chunk) or template_verdict(raw_plans)
        if verdict:
            logging.warning(
                "Visual Story: чанк %s (%s) — %s; повторный вызов",
                chunk["index"], chunk["chapter_id"], verdict,
            )
            repair = (
                prompt
                + "\n\nYour preceding answer is invalid: " + verdict
                + ". Return one plan per scene, in order, with the exact scene_ids "
                "listed, and write a DISTINCT description for every scene from its "
                "own text."
            )
            raw_plans = _plans_from_payload(transport.call_json(
                phase=phase + "_repair", prompt=repair, schema_file="visual_story_chapter_plan.json",
            ))
            verdict = ids_verdict(raw_plans, chunk)
            if verdict:
                raise ValueError(f"Chapter chunk {chunk['index']}: {verdict}")
            soft = template_verdict(raw_plans)
            if soft:
                logging.warning(
                    "Visual Story: чанк %s и после повтора — %s (замечание, едем дальше)",
                    chunk["index"], soft,
                )

    labels = entity_labels(story_map)
    plans = [
        _normalize(raw, scene, chunk_ids, allow_reuse=allow_reuse, labels=labels)
        for raw, scene in zip(raw_plans, chunk["scenes"])
    ]
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps({"chapter_id": chunk["chapter_id"], "plans": plans}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return plans


def create_chapter_plans(
    *,
    scenes: list[dict[str, Any]],
    story_map: dict[str, Any],
    transport,
    prompt_template: str,
    story_dir: str | Path,
    style_guide: str = "",
    project_name: str = "Untitled",
    workers: int | None = None,
    max_scenes: int | None = None,
    allow_reuse: bool = True,
    mock: bool = False,
) -> list[dict[str, Any]]:
    """Все главы параллельно; результат — планы в порядке сцен."""
    story = Path(story_dir)
    raw_dir = story / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    workers = max(1, int(workers or getattr(config, "VISUAL_STORY_CHAPTER_WORKERS", 4) or 4))
    max_scenes = int(max_scenes or getattr(config, "VISUAL_STORY_CHAPTER_MAX_SCENES", 60) or 60)
    chunks = split_into_chunks(scenes, story_map, max_scenes=max_scenes)
    logger.info(
        "[visual-story/chapter] %d чанков по %d главам, воркеров %d",
        len(chunks), len(story_map.get("chapters") or []), workers,
    )

    def _one(chunk: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
        plans = run_chunk(
            transport, prompt_template, chunk, story_map,
            raw_dir=raw_dir, project_name=project_name, style_guide=style_guide,
            allow_reuse=allow_reuse, mock=mock,
        )
        from collections import Counter

        logger.info("   [этап ②] чанк %s готов: планов %s, источники %s",
                    chunk["index"], len(plans),
                    # После `_normalize` источник живёт в preferred_source; чтение
                    # сырого `source` показывало прочерк на здоровых планах.
                    dict(Counter(str(p.get("preferred_source") or "—") for p in plans)))
        return chunk["index"], plans

    results: dict[int, list[dict[str, Any]]] = {}
    if workers > 1 and len(chunks) > 1 and not mock:
        with ThreadPoolExecutor(max_workers=min(workers, len(chunks))) as pool:
            futures = {pool.submit(_one, chunk): chunk["index"] for chunk in chunks}
            for future in as_completed(futures):
                index, plans = future.result()
                results[index] = plans
    else:
        for chunk in chunks:
            index, plans = _one(chunk)
            results[index] = plans

    plans: list[dict[str, Any]] = []
    for chunk in chunks:
        plans.extend(results[chunk["index"]])
    if len(plans) != len(scenes):
        raise ValueError(f"Expected {len(scenes)} chapter plans, got {len(plans)}")
    for expected, plan in zip(scenes, plans):
        if int(plan["scene_id"]) != int(expected["id"]):
            raise ValueError(
                f"Chapter plans out of order at scene {expected['id']} (got {plan['scene_id']})"
            )

    with (story / "visual_plans.jsonl").open("w", encoding="utf-8") as stream:
        for plan in plans:
            stream.write(json.dumps(plan, ensure_ascii=False) + "\n")
    (story / "visual_plans.json").write_text(
        json.dumps({"version": 2, "plans": plans}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return plans
