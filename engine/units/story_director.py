"""Auto Visual Story profile orchestrator."""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Optional

import params as config
from units.director_modes import DirectorProfileTransport, coerce_json_list
from units.text_io_utils import read_user_text
from units.story_context import (
    ContextWindowBuilder,
    DurationPolicy,
    materialize_scenes,
    normalize_words,
    write_context_packets,
)
from units.story_chapters import create_chapter_plans
from units.story_epf import materialize_visual_assets
from units.story_validator import (
    final_asset_gate,
    validate_visual_plans,
    validate_montage_compatibility,
    validate_narrative_state,
    validate_scenes,
)


def _json_read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _json_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _input_hash(*parts: Any) -> str:
    digest = hashlib.sha256()
    for part in parts:
        if isinstance(part, Path):
            digest.update(part.read_bytes())
        else:
            digest.update(
                json.dumps(part, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
            )
    return digest.hexdigest()


class StoryState:
    def __init__(self, story_dir: Path, input_hash: str):
        self.path = story_dir / "state.json"
        if self.path.exists():
            try:
                self.data = _json_read(self.path)
            except Exception as exc:                     # noqa: BLE001
                logging.warning("[visual-story] состояние прогона не прочитано (%s) — "
                                "начинаю с чистого state.json", exc)
                self.data = {}
        else:
            self.data = {}
        if self.data.get("input_hash") != input_hash:
            self.data = {
                "schema_version": 1,
                "director_profile": "auto_visual_story",
                "input_hash": input_hash,
                "stages": {},
            }
        self.save()

    def save(self) -> None:
        _json_write(self.path, self.data)

    def status(self, stage: str) -> str:
        value = self.data.get("stages", {}).get(stage, {})
        return value.get("status", "pending") if isinstance(value, dict) else str(value)

    def mark(self, stage: str, status: str, **fields: Any) -> None:
        self.data.setdefault("stages", {})[stage] = {
            "status": status,
            **fields,
        }
        self.save()


def _mock_scene_plan(
    words: list[dict[str, Any]],
    audio_duration: float,
    target_duration: float,
) -> dict[str, Any]:
    # Keep mock output deliberately coarse. ``materialize_scenes`` owns the
    # real split and knows the active range plus the separate intro limits.
    # Pre-splitting here could leave an unmergeable short tail at that boundary.
    if not words:
        return {"theme": "", "scenes": [], "chapters": [], "entities": []}
    return {
        "theme": "Deterministic mock theme",
        "scenes": [{
            "word_start_index": 0,
            "word_end_index": len(words) - 1,
            "scene_role": "event",
            "chapter_id": "chapter_1",
        }],
        "chapters": [{"id": "chapter_1", "summary": "Deterministic mock chapter"}],
        "entities": [],
    }


def _load_prompt(name: str) -> str:
    path = Path(__file__).parents[1] / "instructions" / name
    return path.read_text(encoding="utf-8")


def _scene_plan_degeneracy(
    words: list[dict[str, Any]],
    raw_scenes: list[dict[str, Any]],
    audio_duration: float,
    *,
    max_duration: float,
) -> str:
    """Пустая строка — план приемлем; иначе причина, почему он выродился.

    Ловим только ГРУБОЕ игнорирование диапазона: сцены втрое длиннее
    максимума либо сцен меньше половины того минимума, который вообще
    возможен при таком максимуме. Мелкие промахи по-прежнему правит
    сплиттер, как и задумано.
    """
    if not raw_scenes or not words:
        return "the plan contains no scenes"
    max_duration = max(0.1, float(max_duration))
    gross = 0
    longest = 0.0
    for scene in raw_scenes:
        try:
            start_index = int(scene.get("word_start_index"))
            end_index = int(scene.get("word_end_index"))
            start = float(words[max(0, min(start_index, len(words) - 1))]["start"])
            end = float(words[max(0, min(end_index, len(words) - 1))]["end"])
        except (TypeError, ValueError, KeyError, IndexError):   # кривая запись плана — сцена считается пустой
            continue
        length = max(0.0, end - start)
        longest = max(longest, length)
        if length > 3.0 * max_duration:
            gross += 1
    floor_count = int(audio_duration // max_duration)
    if gross:
        return (
            f"{gross} scene(s) run far beyond the maximum (longest "
            f"{longest:.0f} s against a {max_duration:g} s limit)"
        )
    if floor_count >= 4 and len(raw_scenes) < floor_count / 2:
        return (
            f"only {len(raw_scenes)} scene(s) for {audio_duration:.0f} s of "
            f"narration — the {max_duration:g} s limit is ignored"
        )
    return ""


def _request_scene_plan(
    transport,
    words: list[dict[str, Any]],
    audio_duration: float,
    *,
    project_name: str,
    duration_min: float,
    duration_target: float,
    duration_max: float,
    duration_profile: dict[str, Any],
    words_path: str | Path | None = None,
    reuse_raw: bool = False,
) -> dict[str, Any]:
    """Этап ①: попросить модель порезать озвучку на сцены.

    Вынесено из оркестратора, чтобы изолированный тест гонял ровно боевой
    код: сборку промпта, вызов и гейт на вырождение с одним повтором.
    reuse_raw — при повторном запуске взять сохранённый сырой ответ модели
    (`raw/scene_plan_result.json`), если он есть и прошёл гейт: падение
    ПОСЛЕ ответа (сплиттер, валидатор) не должно стоить вызова заново.
    """
    if reuse_raw:
        cached = getattr(transport, "raw_dir", None)
        cached = (Path(cached) / "scene_plan_result.json") if cached else None
        if cached and cached.exists():
            try:
                payload = json.loads(cached.read_text(encoding="utf-8-sig"))
                if isinstance(payload, dict) and not _scene_plan_degeneracy(
                    words,
                    coerce_json_list(payload.get("scenes") or [], "scenes"),
                    audio_duration,
                    max_duration=duration_max,
                ):
                    logging.info("Visual Story: план сцен взят с диска (%s)", cached)
                    return payload
            except (OSError, ValueError):   # файл состояния битый — стадия пойдёт заново
                pass
    # Слова — самый тяжёлый вход (5000 слов ≈ 470 К символов с отступами).
    # Агенту с файловым контрактом (Codex) даём ПУТЬ, как Pass 1 даёт
    # транскрипцию; чат-провайдеру — компактный JSON без отступов.
    if getattr(transport, "wants_files", False) and words_path is not None:
        words_block = (
            f"Read the words from this file: {Path(words_path).as_posix()}\n"
            "It is JSON with a top-level \"words\" list; every item has index, "
            "text, start, end (seconds). Read it completely before cutting."
        )
    else:
        words_block = json.dumps(words, ensure_ascii=False, separators=(",", ":"))
    prompt = (
        _load_prompt("visual_story_scene_plan.txt")
        .replace("{{PROJECT_NAME}}", project_name)
        .replace("{{WORDS_BLOCK}}", words_block)
        # duration_min/max — ГЛОБАЛЬНЫЕ границы (с учётом вступления); для
        # правила «обычная сцена от…до…» нужен именно основной диапазон,
        # иначе промпт объявит 2-9 при основном 6-9 (улов теста 23.08).
        .replace(
            "{{MIN_DURATION}}",
            f"{float(duration_profile['active']['min']):g}",
        )
        .replace("{{TARGET_DURATION}}", f"{float(duration_target):g}")
        .replace(
            "{{MAX_DURATION}}",
            f"{float(duration_profile['active']['max']):g}",
        )
        .replace(
            "{{DURATION_PROFILE_JSON}}",
            json.dumps(duration_profile, ensure_ascii=False, indent=2),
        )
        .replace(
            "{{INTRO_BOUNDARY}}",
            f"{float(duration_profile['intro']['boundary']):g}",
        )
        .replace(
            "{{INTRO_MIN}}",
            f"{float(duration_profile['intro']['min']):g}",
        )
        .replace(
            "{{INTRO_MAX}}",
            f"{float(duration_profile['intro']['max']):g}",
        )
    )
    raw_scene_plan = transport.call_json(
        phase="scene_plan",
        prompt=prompt,
        schema_file="visual_story_scene_plan.json",
    )
    # Гейт на вырождение (улов 23.08: Codex вернул ОДНУ сцену на 47 минут,
    # сплиттер молча настрогал 378 осколков с одной фразой). Модели не шлём
    # число сцен — только диапазон, как в Pass 1; здесь лишь ловим план,
    # который диапазон грубо игнорирует, и просим перерезать один раз.
    verdict = _scene_plan_degeneracy(
        words,
        coerce_json_list(raw_scene_plan.get("scenes") or [], "scenes"),
        audio_duration,
        max_duration=duration_max,
    )
    if not verdict:
        return raw_scene_plan
    logging.warning(
        "Visual Story: план сцен выродился (%s) — повторный вызов", verdict,
    )
    repair_prompt = (
        prompt
        + "\n\nYour preceding plan is invalid: "
        + verdict
        + f". Every regular scene must be "
        f"{float(duration_profile['active']['min']):g}-"
        f"{float(duration_profile['active']['max']):g} seconds of narration "
        "measured by word "
        "times. Re-cut the ENTIRE narration scene by scene within that range "
        "and return the complete plan."
    )
    raw_scene_plan = transport.call_json(
        phase="scene_plan_repair",
        prompt=repair_prompt,
        schema_file="visual_story_scene_plan.json",
    )
    verdict = _scene_plan_degeneracy(
        words,
        coerce_json_list(raw_scene_plan.get("scenes") or [], "scenes"),
        audio_duration,
        max_duration=duration_max,
    )
    if verdict:
        logging.warning(
            "Visual Story: план сцен выродился и после повтора (%s) — режем "
            "механически, описания сцен будут бедными", verdict,
        )
    return raw_scene_plan


def _build_story_map(
    scenes: list[dict[str, Any]],
    raw_scene_plan: dict[str, Any],
) -> dict[str, Any]:
    """Карта истории по выходу этапа ①: тема, главы со списками сцен, реестр.

    Главы и сущности приходят от модели; номера сцен считаем САМИ уже после
    сплиттера (он мог склеить или разрезать сцены, ординалы модели не годятся).
    `chapter_id` переживает сплиттер вместе со сценой; первое появление
    сущности модель даёт индексом СЛОВА — он не зависит от нарезки.
    """
    chapters_raw = coerce_json_list(raw_scene_plan.get("chapters") or [], "chapters")
    entities_raw = coerce_json_list(raw_scene_plan.get("entities") or [], "entities")
    theme = str(raw_scene_plan.get("theme") or "").strip()

    chapter_scenes: dict[str, list[int]] = {}
    for scene in scenes:
        chapter_scenes.setdefault(str(scene.get("chapter_id") or ""), []).append(int(scene["id"]))

    chapters: list[dict[str, Any]] = []
    seen_chapters: set[str] = set()
    for item in chapters_raw:
        if not isinstance(item, dict):
            continue
        chapter_id = str(item.get("id") or "").strip()
        if not chapter_id or chapter_id in seen_chapters:
            continue
        seen_chapters.add(chapter_id)
        chapters.append({
            "id": chapter_id,
            "summary": str(item.get("summary") or "").strip(),
            "scene_ids": chapter_scenes.get(chapter_id, []),
        })

    def _scene_for_word(word_index: Any) -> int | None:
        try:
            index = int(word_index)
        except (TypeError, ValueError):   # нечисловое время сцены — берём значение по умолчанию
            return None
        for scene in scenes:
            if int(scene["word_start_index"]) <= index <= int(scene["word_end_index"]):
                return int(scene["id"])
        return None

    entities: list[dict[str, Any]] = []
    seen_entities: set[str] = set()
    for item in entities_raw:
        if not isinstance(item, dict):
            continue
        entity_id = str(item.get("id") or "").strip()
        if not entity_id or entity_id in seen_entities:
            continue
        seen_entities.add(entity_id)
        word_index = item.get("first_appearance_word_index")
        entities.append({
            "id": entity_id,
            "type": str(item.get("type") or "abstract").strip() or "abstract",
            "label": str(item.get("label") or entity_id).strip(),
            "first_appearance_word_index": (
                int(word_index) if isinstance(word_index, int) else None
            ),
            "first_appearance_scene": _scene_for_word(word_index),
        })

    return {
        "schema_version": 2,
        "theme": theme,
        "chapters": chapters,
        "entities": entities,
    }


def _check_story_map(
    scenes: list[dict[str, Any]],
    story_map: dict[str, Any],
) -> None:
    """Жёсткая проверка: без неё второй этап нельзя разложить по главам.

    Каждая сцена ссылается на существующую главу; главы идут подряд и без
    дыр (сцены главы — непрерывный отрезок); каждая глава непуста.
    """
    chapter_ids = [str(item["id"]) for item in story_map.get("chapters") or []]
    if not chapter_ids:
        raise ValueError("Scene plan has no chapters")
    known = set(chapter_ids)
    order: list[str] = []
    for scene in scenes:
        chapter_id = str(scene.get("chapter_id") or "")
        if chapter_id not in known:
            raise ValueError(
                f"Scene {scene.get('id')} references unknown chapter {chapter_id!r}"
            )
        if not order or order[-1] != chapter_id:
            order.append(chapter_id)
    if len(order) != len(set(order)):
        raise ValueError(
            "Chapters are not contiguous: a chapter's scenes are interleaved "
            f"with another chapter's ({order})"
        )
    empty = [cid for cid in chapter_ids if cid not in set(order)]
    if empty:
        raise ValueError(f"Chapters without scenes: {empty}")


def _narrative_from_story_map(
    story_map: dict[str, Any],
    plans: list[dict[str, Any]],
) -> dict[str, Any]:
    """Карта истории в форме прежнего narrative_state — для пакетов контекста.

    Главы и реестр — из этапа ①, пер-сценная непрерывность — из этапа ②.
    Скрытые/раскрытые сущности главы считаются по первому появлению.
    """
    chapters_out: list[dict[str, Any]] = []
    entities = list(story_map.get("entities") or [])
    plans_by_id = {int(p["scene_id"]): p for p in plans}
    for chapter in story_map.get("chapters") or []:
        scene_ids = [int(s) for s in chapter.get("scene_ids") or []]
        last_id = max(scene_ids) if scene_ids else 0
        first_id = min(scene_ids) if scene_ids else 0
        active: list[str] = []
        for scene_id in scene_ids:
            for entity_id in plans_by_id.get(scene_id, {}).get("entities") or []:
                if entity_id not in active:
                    active.append(str(entity_id))
        hidden = [
            str(e["id"]) for e in entities
            if isinstance(e.get("first_appearance_scene"), int)
            and e["first_appearance_scene"] > last_id
        ]
        revealed = [
            str(e["id"]) for e in entities
            if isinstance(e.get("first_appearance_scene"), int)
            and first_id <= e["first_appearance_scene"] <= last_id
        ]
        chapters_out.append({
            "id": str(chapter.get("id")),
            "summary": str(chapter.get("summary") or ""),
            "scene_ids": scene_ids,
            "active_entities": active,
            "hidden_entities": hidden,
            "revealed_entities": revealed,
        })
    overrides = []
    for plan in plans:
        continuity = plan.get("continuity") or {}
        overrides.append({
            "scene_id": int(plan["scene_id"]),
            "resolved_references": [],
            "continuity_constraints": {
                "keep_entities": list(continuity.get("keep_entities") or []),
                "same_location_as_scene": continuity.get("same_location_as_scene"),
                "time_of_day": None,
                "must_not_reveal": list(continuity.get("must_not_reveal") or []),
                "notes": None,
            },
        })
    return {
        "global_summary": str(story_map.get("theme") or ""),
        "chapters": chapters_out,
        "entities": [
            {
                "id": str(e.get("id")),
                "type": str(e.get("type") or "abstract"),
                "label": str(e.get("label") or e.get("id")),
                "description": "",
                "first_appearance_scene": e.get("first_appearance_scene"),
            }
            for e in entities
        ],
        "scene_overrides": overrides,
    }


def _active_duration_profile() -> tuple[
    float, float, float, DurationPolicy, dict[str, Any]
]:
    """Use the active GUI/process clip ranges, including the intro range."""
    active_min = float(getattr(
        config,
        "ACTIVE_CLIP_MIN_DURATION",
        getattr(config, "VIDEO_CLIP_MIN_DURATION", 4),
    ))
    active_max = float(getattr(
        config,
        "ACTIVE_CLIP_MAX_DURATION",
        getattr(config, "VIDEO_CLIP_MAX_DURATION", 8),
    ))
    active_min = max(0.1, active_min)
    active_max = max(active_min, active_max)

    intro_boundary = max(0.0, float(getattr(config, "INTRO_BLOCK_DURATION", 0)))
    try:
        intro_min = float(config.get_intro_clip_min())
        intro_max = float(config.get_intro_clip_max())
    except AttributeError:                               # старый config без интро-профиля
        logging.debug("[visual-story] в config нет длительностей интро — беру общие")
        intro_min, intro_max = active_min, active_max
    intro_min = max(0.1, intro_min)
    intro_max = max(intro_min, intro_max)

    def policy(start_time: float) -> tuple[float, float, float]:
        if intro_boundary > 0 and start_time < intro_boundary:
            low, high = intro_min, intro_max
        else:
            low, high = active_min, active_max
        return low, (low + high) / 2.0, high

    global_min = min(active_min, intro_min if intro_boundary > 0 else active_min)
    global_max = max(active_max, intro_max if intro_boundary > 0 else active_max)
    global_target = (global_min + global_max) / 2.0
    profile = {
        "active": {"min": active_min, "max": active_max},
        "intro": {
            "boundary": intro_boundary,
            "min": intro_min,
            "max": intro_max,
        },
    }
    return global_min, global_target, global_max, policy, profile


def _attach_chapter_theme(
    plans: list[dict[str, Any]],
    scenes: list[dict[str, Any]],
    story_map: dict[str, Any],
) -> list[dict[str, Any]]:
    """Положить в каждый план тему ЕГО главы (эпоха + место из `chapters[].summary`).

    Её читают писатели запросов стадий и вижн-отбор: без периода и страны
    безымянная сцена («певица на сцене») находит и одобряет кого угодно —
    в живом тесте 25.08 вижн выбрал Уитни Хьюстон на польскую сцену 1980-х.
    Тема главы, а не всего ролика: глава про сегодняшний день внутри фильма
    про 1980-е должна искаться как сегодняшний день."""
    summary_by_chapter = {
        str(chapter.get("id") or ""): str(chapter.get("summary") or "").strip()
        for chapter in (story_map.get("chapters") or [])
    }
    chapter_by_scene = {
        int(scene["id"]): str(scene.get("chapter_id") or "")
        for scene in scenes if scene.get("id") is not None
    }
    for plan in plans:
        try:
            scene_id = int(plan.get("scene_id"))
        except (TypeError, ValueError):   # нечисловая длительность в настройках — профиль по умолчанию
            continue
        chapter_id = chapter_by_scene.get(scene_id, "")
        plan["chapter_id"] = chapter_id
        plan["chapter_theme"] = summary_by_chapter.get(chapter_id, "")
    return plans


def _build_montage(
    *,
    scenes: list[dict[str, Any]],
    plans: list[dict[str, Any]],
    audio_duration: float,
    project_name: str,
    theme: str = "",
    chapters: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    plans_by_id = {int(plan["scene_id"]): plan for plan in plans}
    # Тема ГЛАВЫ, а не всего ролика: внутри фильма про 1980-е бывает глава про
    # сегодняшний день, и общая тема сбила бы и запрос, и проверку кадра
    # (решение пользователя 25.08).
    chapter_summary = {
        str(chapter.get("id") or ""): str(chapter.get("summary") or "").strip()
        for chapter in (chapters or [])
    }
    clips = []
    for scene in scenes:
        clip_id = int(scene["id"])
        plan = plans_by_id[clip_id]
        clips.append({
            "id": clip_id,
            "file": f"generated/{clip_id:03d}.jpg",
            "startTime": scene["startTime"],
            "endTime": scene["endTime"],
            # Описание сцены пишет этап ② (с именованным субъектом). У сцены
            # этапа ① своего описания нет — там только текст диктора, и он
            # остаётся ЯВНЫМ запасным вариантом, если этап ② почему-то смолчал.
            "description": str(
                plan.get("description") or scene.get("description") or scene["text"]
            ),
            "voiceover": scene["text"],
            # "ai" → обычная нейрогенерация: маркера стадии нет вовсе, клип
            # проходит стандартным путём генерации кадра.
            "source": (
                ""
                if str(plan.get("preferred_source") or "").lower() == "ai"
                else "youtube_pending"
                if str(plan.get("preferred_source") or "").lower() == "youtube"
                else (
                    "stock_pending"
                    if str(plan.get("preferred_source") or "").lower()
                    in {"stock", "local", "stock_photo", "stock_video"}
                    else "real_photo_pending"
                )
            ),
            "scene_role": scene.get("scene_role", "event"),
            "entities": plan.get("entities") or scene.get("entities") or [],
            "visual_intent": plan.get("visual_intent", ""),
            "search_queries": plan.get("search_queries")
            or ([plan.get("search_query")] if plan.get("search_query") else []),
            # Отдельным полем, не подменой search_queries: у ютуба свой язык
            # поиска, а фотозапрос обязан пережить сцену как вход фолбэка.
            "youtube_queries": plan.get("youtube_queries") or [],
            "must_have": plan.get("must_have") or [],
            "avoid": plan.get("avoid")
            or plan.get("negative_constraints")
            or [],
            "candidate_windows": plan.get("candidate_windows")
            or plan.get("youtube_candidate_windows")
            or [],
            "chain_id": clip_id,
            "visual_story": {
                "schema_version": 1,
                "word_start_index": scene["word_start_index"],
                "word_end_index": scene["word_end_index"],
                "strategy": plan["visual_strategy"],
                "reuse_clip_id": plan.get("reuse_clip_id"),
                "visual_intent": plan["visual_intent"],
                "query": plan.get("search_query", ""),
                "continuity": plan.get("continuity") or {},
                "chapter_id": scene.get("chapter_id", ""),
                # Визуальный контекст главы (эпоха + место) — его получают
                # писатели запросов и вижн-отбор всех источников.
                "chapter_theme": chapter_summary.get(str(scene.get("chapter_id") or ""), ""),
            },
        })
    return {
        "project": project_name,
        "audioDuration": audio_duration,
        "director_profile": "auto_visual_story",
        # Тема ролика — её читают писатели запросов стадий (карточки клипов),
        # как `theme` стандартного Pass 1.
        "theme": theme,
        "clips": clips,
    }


def _allocate_source_plans(plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Держать доли стока и ютуба, не выбирая сцены за режиссёра.

    Источник для сцены выбирает планировщик глав — он читает её содержание.
    Здесь остаётся арифметика: сколько сцен каждому источнику положено по доле
    из GUI, и что делать с лишними.

    Ютуб раньше выбирали здесь двумя словарями терминов: один поднимал сцену за
    отдельные слова — вплоть до названий конкретной страны и её городов, взятых
    из тестового ролика; второй опускал сцену за слова «человек», «люди»,
    «говорит», «выступление». Оба удалены 26.08. Через эти модули идут ролики
    любых тем, поэтому отбор не может опираться на списки слов: они работают
    только на том материале, из которого выписаны. Вдобавок второй словарь бил
    по речам и людям — по сильной стороне ютуба.
    """
    stock_names = {"stock", "local", "stock_photo", "stock_video"}
    if not getattr(config, "STOCK_ENABLED", False):
        for plan in plans:
            if plan.get("preferred_source") in stock_names:
                plan["preferred_source"] = "epf"
    if not getattr(config, "YOUTUBE_FOOTAGE_ENABLED", False):
        for plan in plans:
            if plan.get("preferred_source") == "youtube":
                plan["preferred_source"] = "epf"
    # Кадр-мизансцена, которого нет ни в одном архиве, помечается "ai" и идёт в
    # нейрогенерацию (решение пользователя 25.08). Если генерации в прогоне нет,
    # такой сцене деваться некуда — возвращаем её архивам, а промпт этапа ② в
    # этом режиме и не даёт писать постановочные описания.
    from units.story_chapters import ai_generation_available

    if not ai_generation_available():
        for plan in plans:
            if plan.get("preferred_source") == "ai":
                plan["preferred_source"] = "epf"

    # Сцены под нейрогенерацию не участвуют в квотах сток/ютуб.
    new_plans = [
        p for p in plans
        if p.get("visual_strategy") == "new_search" and p.get("preferred_source") != "ai"
    ]
    youtube_target = 0
    if getattr(config, "YOUTUBE_FOOTAGE_ENABLED", False):
        youtube_target = min(
            len(new_plans),
            max(0, int(round(len(new_plans) * float(
                getattr(config, "YOUTUBE_RATIO_TARGET", 0.0) or 0.0
            )))),
        )
    # Берём те сцены, которые планировщик САМ отдал ютубу, в порядке ролика.
    # Своих сцен не добавляем: если он выбрал меньше доли — значит в материале
    # столько и нашлось, а добор чужими сценами уводил бы кадр не туда.
    youtube_explicit = [p for p in new_plans if p.get("preferred_source") == "youtube"]
    selected_youtube = {id(p) for p in youtube_explicit[:youtube_target]}

    stock_target = 0
    if getattr(config, "STOCK_ENABLED", False):
        stock_target = min(
            len(new_plans) - len(selected_youtube),
            max(0, int(round(len(new_plans) * float(
                getattr(config, "STOCK_RATIO_TARGET", 0.30) or 0.30
            )))),
        )
    stock_explicit = [
        p for p in new_plans
        if id(p) not in selected_youtube and p.get("preferred_source") in stock_names
    ]
    selected_stock = {id(p) for p in stock_explicit[:stock_target]}
    for plan in new_plans:
        if len(selected_stock) >= stock_target:
            break
        if id(plan) not in selected_youtube and id(plan) not in selected_stock:
            selected_stock.add(id(plan))

    for plan in new_plans:
        if id(plan) in selected_youtube:
            plan["preferred_source"] = "youtube"
        elif id(plan) in selected_stock:
            plan["preferred_source"] = "stock"
        elif plan.get("preferred_source") in stock_names or plan.get("preferred_source") == "youtube":
            plan["preferred_source"] = "epf"
    return plans


def _allocate_stock_plans(plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Backward-compatible name for callers outside this module."""
    return _allocate_source_plans(plans)


def analyze_visual_story_project(
    *,
    director,
    transcription_path: str,
    style_guide_path: str,
    output_dir: str,
    project_name: str = "Untitled",
    resume: bool = False,
    step: str = "full",
    mock: bool = False,
    mock_assets: bool = False,
) -> dict[str, Any]:
    step = (step or "full").strip().lower()
    if step not in {"analyze", "search", "finalize", "full"}:
        raise ValueError(f"Unknown Auto Visual Story step: {step}")

    project = Path(output_dir).resolve()
    project.mkdir(parents=True, exist_ok=True)
    story = project / "visual_story"
    story.mkdir(parents=True, exist_ok=True)
    transcription_file = Path(transcription_path).resolve()
    style_file = Path(style_guide_path).resolve()
    # Стайлгайд нужен ОБЕИМ веткам: этапу ② и промптеру ai на анализе — и стадии
    # материализации, которая добирает провалившиеся сцены нейрогенерацией. Пока
    # он заводился внутри ветки анализа, запуск отдельным шагом `search`
    # (анализ берётся с диска) падал на UnboundLocalError — улов прогона 25.08.
    style_guide = "" if mock else read_user_text(style_file)
    duration_min, duration_target, duration_max, duration_policy, duration_profile = (
        _active_duration_profile()
    )
    settings = {
        "duration_profile": duration_profile,
        "before": config.VISUAL_STORY_CONTEXT_BEFORE,
        "after": config.VISUAL_STORY_CONTEXT_AFTER,
        "history": config.VISUAL_STORY_VISUAL_HISTORY,
        "chapter_workers": getattr(config, "VISUAL_STORY_CHAPTER_WORKERS", 4),
        "chapter_max_scenes": getattr(config, "VISUAL_STORY_CHAPTER_MAX_SCENES", 60),
        "allow_reuse": config.VISUAL_STORY_ALLOW_REUSE,
        "fallback": config.VISUAL_STORY_FALLBACK,
        "stock_enabled": bool(getattr(config, "STOCK_ENABLED", False)),
        "stock_sources": list(getattr(config, "STOCK_SOURCES", []) or []),
        "stock_ratio": float(getattr(config, "STOCK_RATIO_TARGET", 0.30) or 0.30),
    }
    run_hash = _input_hash(transcription_file, style_file, settings)
    state = StoryState(story, run_hash)
    transport = DirectorProfileTransport(director, project)

    transcription = _json_read(transcription_file)
    audio_duration = float(transcription.get("audioDuration") or 0)
    if audio_duration <= 0:
        raise ValueError("transcription.json has no positive audioDuration")

    words_path = project / "transcription_words.json"
    scene_path = story / "scene_plan.json"
    story_map_path = story / "story_map.json"
    narrative_path = story / "narrative_state.json"
    plans_path = story / "visual_plans.json"
    montage_path = project / "montage_plan.json"

    analysis_artifacts = (
        words_path,
        scene_path,
        story_map_path,
        narrative_path,
        plans_path,
        montage_path,
    )
    reuse_analysis = bool(
        resume
        and step in {"analyze", "full"}
        and all(path.exists() for path in analysis_artifacts)
    )

    if step in {"analyze", "full"} and not reuse_analysis:
        state.mark("words", "running")
        words = normalize_words(transcription)
        _json_write(words_path, {
            "schema_version": 1,
            "audioDuration": audio_duration,
            "language": transcription.get("language"),
            "words": words,
        })
        state.mark("words", "done", words=len(words), artifact=str(words_path))
        logging.info("[visual-story] слов %s, длительность %.0f с, сцена %.0f-%.0f с",
                     len(words), audio_duration, duration_min, duration_max)

        state.mark("scenes", "running")
        logging.info("[visual-story] этап ①: режу озвучку на сцены (оператор %s)",
                     getattr(director, "model_name", "") or "—")
        if mock:
            raw_scene_plan = _mock_scene_plan(
                words,
                audio_duration,
                duration_target,
            )
        else:
            raw_scene_plan = _request_scene_plan(
                transport,
                words,
                audio_duration,
                project_name=project_name,
                duration_min=duration_min,
                duration_target=duration_target,
                duration_max=duration_max,
                duration_profile=duration_profile,
                words_path=words_path,
                reuse_raw=bool(resume),
            )
        scenes = materialize_scenes(
            words,
            coerce_json_list(raw_scene_plan.get("scenes") or [], "scenes"),
            audio_duration,
            min_duration=duration_min,
            target_duration=duration_target,
            max_duration=duration_max,
            duration_policy=duration_policy,
        )
        validate_scenes(
            scenes,
            len(words),
            audio_duration,
            min_duration=duration_min,
            max_duration=duration_max,
            duration_policy=duration_policy,
        )
        # Карта истории (этап ① v2): тема, главы со сценами, реестр сущностей.
        # Проверка глав жёсткая — без неё второй этап не разложить по чанкам.
        story_map = _build_story_map(scenes, raw_scene_plan)
        logging.info(
            "[visual-story] этап ① готов: сцен %s, глав %s, сущностей %s | тема: %s",
            len(scenes), len(story_map.get("chapters") or []),
            len(story_map.get("entities") or []),
            str(story_map.get("theme") or "—")[:90])
        _check_story_map(scenes, story_map)
        _json_write(scene_path, {
            "schema_version": 2,
            "audioDuration": audio_duration,
            "scenes": scenes,
        })
        _json_write(story_map_path, story_map)
        state.mark(
            "scenes", "done",
            scenes=len(scenes),
            chapters=len(story_map["chapters"]),
            entities=len(story_map["entities"]),
            artifact=str(scene_path),
        )

        # Этап ② (25.08): сочиняет, что на экране, — параллельно по главам.
        # Нарратив отдельным вызовом и пачки запросов по 40 сцен упразднены:
        # главы и реестр даёт этап ①, запросы пишут стадии по маркерам.
        state.mark("chapters", "running")
        plans = create_chapter_plans(
            scenes=scenes,
            story_map=story_map,
            transport=transport,
            prompt_template=_load_prompt("visual_story_chapter.txt"),
            story_dir=story,
            style_guide=style_guide,
            project_name=project_name,
            allow_reuse=config.VISUAL_STORY_ALLOW_REUSE,
            mock=mock,
        )
        # Гейт планов этапа ② написан, но в боевом профиле не вызывался: планы,
        # поднятые из кэша чанка на диске, не проходят даже `_normalize`
        # (аудит 25.08). Проверка мягкая — замечание, а не смерть прогона:
        # состав уже собран, и терять его из-за одной кривой строки нельзя.
        try:
            validate_visual_plans(plans, len(scenes),
                                  allow_reuse=config.VISUAL_STORY_ALLOW_REUSE)
        except ValueError as exc:
            logging.warning("[visual-story] гейт планов этапа ②: %s "
                            "(замечание, прогон продолжается)", exc)
        plans = _attach_chapter_theme(plans, scenes, story_map)
        state.mark("chapters", "done", plans=len(plans), artifact=str(plans_path))
        from collections import Counter as _Counter
        logging.info(
            "[visual-story] этап ② готов: планов %s | источники: %s | реюз: %s",
            len(plans),
            dict(_Counter(str(p.get("preferred_source") or "—") for p in plans)),
            sum(1 for p in plans if p.get("visual_strategy") == "reuse_previous"))

        # Сцены под нейрогенерацию: промпты пишет ОТДЕЛЬНЫЙ вызов только по ним,
        # оператором режиссёра и по его же стайлгайду — как Pass 2 в стандартном
        # режиме (решение пользователя 25.08). Файл промптов читает imggen.
        from units.story_ai import ai_plans, create_ai_prompts, write_prompts_file

        selected_ai = ai_plans(plans)
        if selected_ai:
            state.mark("ai_prompts", "running", clips=len(selected_ai))
            try:
                ai_prompts = create_ai_prompts(
                    transport, plans,
                    project_dir=project,
                    project_name=project_name,
                    theme=str(story_map.get("theme") or ""),
                    style_guide=style_guide,
                    mock=mock,
                )
                prompts_path = write_prompts_file(project, project_name, ai_prompts)
                logging.info(
                    "[visual-story/ai] промптов написано %s/%s -> %s",
                    len(ai_prompts), len(selected_ai), prompts_path.name)
                state.mark("ai_prompts", "done", clips=len(ai_prompts),
                           artifact=str(prompts_path))
            except Exception as exc:  # noqa: BLE001
                # Без промптов клипы просто останутся пустыми — пайплайн не роняем.
                logging.warning("[visual-story/ai] промптер упал: %s", exc)
                state.mark("ai_prompts", "error", error=str(exc))

        # Карта в форме нарратива — её читают пакеты контекста (ютуб-стадия
        # берёт их как context.json). Собирается кодом из этапов ① и ②.
        narrative = _narrative_from_story_map(story_map, plans)
        validate_narrative_state(narrative, len(scenes))
        _json_write(narrative_path, narrative)

        state.mark("context", "running")
        # `description` в плане сцен — техническая копия ОЗВУЧКИ (этап ① его не
        # пишет вовсе, схема такого поля не знает). В пакеты контекста должно
        # уходить ВИЗУАЛЬНОЕ описание кадра из этапа ②, иначе стадии и вижн
        # видят в «что было до/после» польские фразы диктора вместо картин —
        # ровно это лежало в задании вижну клипа 4 (улов пользователя 25.08).
        visual_by_scene = {
            int(plan["scene_id"]): str(plan.get("description") or plan.get("visual_intent") or "").strip()
            for plan in plans
        }
        scenes_for_context = [
            dict(scene, description=visual_by_scene.get(int(scene["id"])) or scene.get("description", ""))
            for scene in scenes
        ]
        packets = ContextWindowBuilder(
            config.VISUAL_STORY_CONTEXT_BEFORE,
            config.VISUAL_STORY_CONTEXT_AFTER,
            config.VISUAL_STORY_VISUAL_HISTORY,
        ).build_all(scenes_for_context, narrative)
        context_path = write_context_packets(story, packets)
        state.mark(
            "context",
            "done",
            packets=len(packets),
            artifact=str(context_path),
        )

        plans = _allocate_source_plans(plans)
        state.mark("queries", "done", plans=len(plans), artifact=str(plans_path))

        montage = _build_montage(
            scenes=scenes,
            plans=plans,
            audio_duration=audio_duration,
            project_name=project_name,
            theme=str(story_map.get("theme") or ""),
            chapters=story_map.get("chapters") or [],
        )
        validate_montage_compatibility(
            montage,
            audio_duration,
            min_duration=0.0,
            max_duration=float("inf"),
        )
        _json_write(montage_path, montage)
        state.mark("montage", "done", artifact=str(montage_path))
    else:
        if not all(path.exists() for path in (scene_path, narrative_path, plans_path, montage_path)):
            raise FileNotFoundError(
                "Auto Visual Story analyze artifacts are incomplete; run step=analyze first"
            )
        scenes = _json_read(scene_path)["scenes"]
        plans = _json_read(plans_path)["plans"]
        montage = _json_read(montage_path)
        validate_scenes(
            scenes,
            len(normalize_words(transcription)),
            audio_duration,
            min_duration=duration_min,
            max_duration=duration_max,
            duration_policy=duration_policy,
        )
        validate_montage_compatibility(
            montage,
            audio_duration,
            min_duration=0.0,
            max_duration=float("inf"),
        )
        if reuse_analysis:
            state.mark(
                "analysis",
                "resumed",
                artifacts=[str(path) for path in analysis_artifacts],
            )

    selection = None
    if step in {"search", "full"}:
        state.mark("assets", "running")
        selection = materialize_visual_assets(
            project_dir=project,
            plans=plans,
            fallback=config.VISUAL_STORY_FALLBACK,
            provider=getattr(config, "REAL_PHOTO_VISION_PROVIDER", None),
            model=getattr(config, "REAL_PHOTO_VISION_MODEL", None),
            mock_assets=mock_assets,
            transport=transport,
            style_guide=style_guide if not mock else "",
        )
        state.mark(
            "assets",
            "done" if not selection.get("unresolved") else "partial",
            resolved=len(selection.get("resolved") or []),
            unresolved=len(selection.get("unresolved") or []),
            artifact=str(story / "selection_manifest.json"),
        )
        montage = _json_read(montage_path)

    final_gate = None
    if step in {"finalize", "full"}:
        state.mark("final_gate", "running")
        montage = _json_read(montage_path)
        final_gate = final_asset_gate(project, montage)
        state.mark("final_gate", "done", **final_gate)

    result = {
        "profile": "auto_visual_story",
        "provider": transport.provider_label,
        "step": step,
        "montage_plan": str(montage_path),
        "state": str(state.path),
        "num_clips": len(montage.get("clips", [])),
        "num_prompts": 0,
        "prompts": None,
        "duration_profile": duration_profile,
        "selection": selection,
        "final_gate": final_gate,
        "cost": {
            "total_cost": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "note": f"Tracked by selected Director transport ({transport.provider_label})",
        },
    }
    _json_write(story / "run_result.json", result)
    return result
