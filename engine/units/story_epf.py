"""EPF, archive fallback and asset materialization for Auto Visual Story."""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import params as config
logger = logging.getLogger(__name__)


def _generated_asset(project: Path, clip_id: int) -> Path | None:
    files = sorted((project / "generated").glob(f"{clip_id:03d}.*"))
    return files[0] if files else None


def _copy_reused_asset(project: Path, source_id: int, target_id: int) -> Path:
    # Из штатного пути переиспользования копирование УБРАНО (H8, 23.08):
    # ведомая сцена ищет заново по запросу донора. Функция осталась только у
    # терминального фолбэка VISUAL_STORY_FALLBACK=reuse_previous — последнего
    # средства для сцены, у которой не вышло вообще ничего.
    source = _generated_asset(project, source_id)
    if source is None:
        raise FileNotFoundError(
            f"reuse_previous source asset is missing for clip {source_id}"
        )
    target = project / "generated" / f"{target_id:03d}{source.suffix.lower()}"
    target.parent.mkdir(parents=True, exist_ok=True)
    for old in target.parent.glob(f"{target_id:03d}.*"):
        old.unlink()
    try:
        os.link(source, target)
    except OSError as exc:
        # жёсткая ссылка не вышла (другой том, ФС без ссылок) — копируем
        logger.debug("  [visual-story] жёсткая ссылка не создана (%s) — копирую файл", exc)
        shutil.copy2(source, target)
    return target


def _resolve_donor_root(
    plans_by_id: dict[int, dict[str, Any]],
    plan: dict[str, Any],
) -> int | None:
    """Номер сцены с НАСТОЯЩИМ поиском в конце цепочки доноров, либо None.

    Цепочка 58 → 57 → 56 законна (валидатор требует лишь «раньше меня»),
    а видео-источники и запрос есть только у сцены с new_search — до неё и
    разматываем. Обрыв, цикл или выход за границы = донор не разрешился.
    """
    current = plan
    seen: set[int] = set()
    while True:
        scene_id = int(current["scene_id"])
        if scene_id in seen:
            return None
        seen.add(scene_id)
        try:
            reuse_id = int(current.get("reuse_clip_id") or scene_id - 1)
        except (TypeError, ValueError):   # нечисловой номер донора — цепочка реюза обрывается
            reuse_id = scene_id - 1
        if reuse_id < 1 or reuse_id >= scene_id:
            return None
        donor = plans_by_id.get(reuse_id)
        if donor is None:
            return None
        if donor.get("visual_strategy") != "reuse_previous":
            return reuse_id
        current = donor


def _donor_video_sources(project: Path, donor_id: int) -> list[dict[str, Any]]:
    """Ролики донора из его selection.json — кандидаты без нового поиска.

    ``selected_parts`` несут url и лицензию; ролики, оставшиеся только в
    ``candidate_parts``, получают url по video_id (лицензию они уже прошли на
    discovery донора, но подтверждения в файле нет — лицензия unknown).
    """
    path = (
        project / "youtube" / "clips" / f"{donor_id:03d}" / "selection.json"
    )
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):   # выбор донора не прочитан — кандидатов у него нет
        return []
    sources: list[dict[str, Any]] = []
    seen: set[str] = set()
    for part in list(data.get("selected_parts") or []) + list(
        data.get("candidate_parts") or []
    ):
        video_id = str(part.get("video_id") or "").strip()
        if not video_id or video_id in seen:
            continue
        seen.add(video_id)
        sources.append({
            "video_id": video_id,
            "title": "",
            "url": str(
                part.get("url")
                or f"https://www.youtube.com/watch?v={video_id}"
            ),
            "source": "youtube",
            "license": str(part.get("license") or "unknown"),
            "license_verified": bool(part.get("license_verified", False)),
        })
    return sources


def _plan_photo_query(plan: dict[str, Any]) -> str:
    """Фотозапрос плана: search_query → первый youtube-запрос → намерение.

    Ютуб-сцена по H5 несёт фотонабор одной строкой в ``search_query``; если
    планировщик её не дал, лучше искать по видеозапросу, чем по прозе намерения.
    """
    for candidate in (
        plan.get("search_query"),
        next(iter(plan.get("youtube_queries") or []), None),
    ):
        text = str(candidate or "").strip()
        if text:
            return text
    # Готового запроса нет — остаётся описание сцены, но целым предложением
    # искать нельзя: поисковик на абзац отдаёт что угодно (улов 25.08). Запрос
    # пишет промптер архивов — он читает сцену и называет её предмет. Прежнее
    # механическое сжатие брало ПЕРВЫЕ значимые слова, а описание начинается с
    # места действия, и предмет в запрос не попадал вовсе (улов 26.08).
    from units.archive_photos import archive_query_from_scene
    from units.story_ai import compact_query

    for candidate in (plan.get("description"), plan.get("visual_intent")):
        text = str(candidate or "").strip()
        if not text:
            continue
        written = archive_query_from_scene(
            text,
            voiceover=str(plan.get("voiceover") or ""),
            clip_id=plan.get("scene_id"),
        )
        if written:
            return written
        short = compact_query(text)
        if short and short != text:
            logger.warning("  [visual-story] клип %s: запрос не написан — сжал описание "
                           "до «%s»", plan.get("scene_id"), short[:70])
        return short or text
    return ""


def _clip_selection(plan: dict[str, Any]):
    from units.archive_photos import ClipSelection

    return ClipSelection(
        clip_id=int(plan["scene_id"]),
        search_query=_plan_photo_query(plan),
        search_query_local="",
        preferred_type="photo",
        preferred_sources=list(getattr(
            config,
            "REAL_PHOTO_SOURCES",
            ["wikimedia", "nasa", "loc", "archive"],
        )),
        reasoning=str(plan.get("reason") or ""),
        confidence=str(plan.get("confidence") or ""),
        query_provider="auto_visual_story",
        query_path="primary",
    )


def _context_text(plan: dict[str, Any], chapter_theme: str = "") -> str:
    """Описание сцены + контекст главы + запреты для вижн-отбора.

    Тема ГЛАВЫ (эпоха и место, `chapters[].summary` этапа ①) идёт первой
    строкой: без неё вижн судит кадр «в вакууме» и на безымянной сцене берёт
    любую подходящую по составу знаменитость — в живом тесте 25.08 на сцену
    про польскую певицу 1980-х он выбрал Уитни Хьюстон. Тема главы, а не всего
    ролика: внутри фильма про 1980-е бывает глава про сегодняшний день."""
    description = str(plan.get("description") or plan.get("visual_intent") or "").strip()
    continuity = plan.get("continuity") or {}
    forbidden = [
        str(item).strip()
        for item in (continuity.get("must_not_reveal") or plan.get("negative_constraints") or [])
        if str(item).strip()
    ]
    chapter_theme = str(chapter_theme or plan.get("chapter_theme") or "").strip()
    text = f"Chapter context (period and place): {chapter_theme} " if chapter_theme else ""
    text += description
    if forbidden:
        # Идентификаторы реестра (snake_case) — в читаемый вид; список
        # ограничен: вижну хватит главного, длинный перечень только шумит.
        readable = [item.replace("_", " ") for item in forbidden[:8]]
        text += " Must NOT appear: " + ", ".join(readable) + "."
    return text.strip()


def _epf_selections(project: Path, plans: list[dict[str, Any]]) -> list:
    """Этап ③ для фотопути: запросы пишет ПИСАТЕЛЬ Real Photo стадии по карточке
    клипа (озвучка + описание сцены + тема + соседи), как в стандартном режиме.
    Раньше в EPF уходила одна строка из плана (`_clip_selection`).

    Написанный запрос кладётся в клип (`search_query`) — его читает реюз, а
    вижн-отбор получает описание сцены и запреты через `context_text`.
    """
    if not plans:
        return []
    from units.source_ledger import apply_clip_source_delta
    from units.archive_photos import RealPhotoProcessor

    processor = RealPhotoProcessor(
        project_dir=project,
        target_ratio=1.0,
        min_ratio=1.0,
        max_ratio=1.0,
        sources=list(getattr(
            config, "REAL_PHOTO_SOURCES", ["wikimedia", "nasa", "loc", "archive"],
        )),
        target_w=1920,
        target_h=1080,
        fps=getattr(config, "VIDEO_FPS", 60),
    )
    by_id = {int(p["scene_id"]): p for p in plans}
    try:
        cards, meta = processor._build_clip_cards()
        selections = processor.select_queries_for_marked(sorted(by_id), cards, meta)
    except Exception as exc:
        logger.warning(
            "[visual-story/epf] писатель запросов упал: %s — запрос из описания", exc,
        )
        selections = []
    written = {int(sel.clip_id) for sel in selections}
    for clip_id, plan in by_id.items():
        if clip_id not in written:
            selections.append(_clip_selection(plan))
    for sel in selections:
        plan = by_id.get(int(sel.clip_id)) or {}
        if not str(sel.search_query or "").strip():
            sel.search_query = _plan_photo_query(plan)
        sel.context_text = _context_text(plan)
    delta = {
        int(sel.clip_id): {"search_query": sel.search_query}
        for sel in selections if str(sel.search_query or "").strip()
    }
    if delta:
        apply_clip_source_delta(
            project / "montage_plan.json", delta,
            log=lambda message: logger.info("[visual-story/epf] %s", message),
        )
    return selections


def _apply_epf_metadata(
    montage: dict[str, Any],
    plans_by_id: dict[int, dict[str, Any]],
    epf_summary: dict[str, Any],
) -> None:
    ingested = {
        int(item["clip_id"]): item
        for item in epf_summary.get("ingest", {}).get("results", [])
        if item.get("status") == "ingested"
    }
    for clip in montage.get("clips", []):
        clip_id = int(clip["id"])
        if clip_id not in ingested:
            continue
        plan = plans_by_id[clip_id]
        clip["source"] = "real_photo"
        clip["file"] = f"generated/{clip_id:03d}.jpg"
        clip["real_meta"] = {
            "archive": "extreme_picture_finder",
            "title": str(clip.get("search_query") or plan.get("search_query") or ""),
            "license": "unknown",
            "author": "",
            "page_url": "",
            "credit_text": "Extreme Picture Finder",
        }


def _run_archive_fallback(
    project: Path,
    unresolved_plans: list[dict[str, Any]],
) -> dict[str, Any]:
    if not unresolved_plans:
        return {"rendered": 0, "selected": 0}
    from units.archive_photos import RealPhotoProcessor

    selections = [_clip_selection(plan) for plan in unresolved_plans]
    processor = RealPhotoProcessor(
        project_dir=project,
        target_ratio=1.0,
        min_ratio=1.0,
        max_ratio=1.0,
        sources=list(getattr(
            config,
            "REAL_PHOTO_SOURCES",
            ["wikimedia", "nasa", "loc", "archive"],
        )),
        target_w=1920,
        target_h=1080,
        fps=getattr(config, "VIDEO_FPS", 60),
    )
    old_bridge = getattr(config, "REAL_PHOTO_EPF_BRIDGE", False)
    config.REAL_PHOTO_EPF_BRIDGE = False
    try:
        return processor.process_phase(selections)
    finally:
        config.REAL_PHOTO_EPF_BRIDGE = old_bridge


def _scene_asset_exists(project: Path, clip_id: int) -> bool:
    return (
        _generated_asset(project, clip_id) is not None
        or (project / "vid_img" / f"{clip_id:03d}.mp4").exists()
    )


def _rescue_unresolved(
    project: Path,
    plans: list[dict[str, Any]],
    *,
    transport=None,
    project_name: str = "",
    style_guide: str = "",
) -> dict[str, Any]:
    """Клипы, которых не закрыл ни один источник: AI либо повтор с кратким запросом."""
    if not plans:
        return {"selected": 0}
    from units.story_ai import (
        compact_query, create_ai_prompts, generate_ai_assets,
        images_enabled, write_prompts_file,
    )

    ids = [int(plan["scene_id"]) for plan in plans]
    summary: dict[str, Any] = {"selected": len(ids), "clips": ids}

    if images_enabled():
        logger.info("  [visual-story/rescue] %s клипов не нашлись — отдаю нейрогенерации: %s",
                    len(ids), ids)
        rescued = [dict(plan, preferred_source="ai") for plan in plans]
        prompts: dict[int, str] = {}
        if transport is not None:
            try:
                prompts = create_ai_prompts(
                    transport, rescued, project_dir=project,
                    project_name=project_name or project.name,
                    theme=str((_load_montage(project) or {}).get("theme") or ""),
                    # Стайлгайд обязателен и здесь: спасённый кадр должен
                    # выглядеть как остальной ролик, а не как чужая вставка.
                    style_guide=style_guide,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("  [visual-story/rescue] промптер добора упал: %s", exc)
        for plan in rescued:                      # без промптера — описание сцены как промпт
            clip_id = int(plan["scene_id"])
            prompts.setdefault(clip_id, str(plan.get("description") or plan.get("visual_intent") or ""))
        write_prompts_file(project, project_name or project.name, prompts)
        summary["mode"] = "ai"
        # Снимаем ВНЕШНИЙ маркер до генерации. `imggen` выбрасывает из работы
        # клипы, чей source значится внешним источником (`stock_pending`,
        # `youtube_pending`, …) — сцена, отданная в добор с таким маркером,
        # молча отфильтровывалась, и добор отчитывался «закрыл 0 из 1»
        # (улов 25.08, клип 4).
        _montage_delta(project, {clip_id: {"source": "ai"} for clip_id in ids}, "rescue")
        summary["result"] = generate_ai_assets(project, ids)
        # Маркер источника ставит и добор: спасённая сцена — такой же ai-кадр,
        # как задуманные, и в плане должна выглядеть так же. Раньше у неё
        # оставался прежний маркер («youtube_pending») или пустое поле, хотя
        # картинка уже лежала на диске (замечание пользователя 25.08).
        delta: dict[int, dict[str, Any]] = {}
        for clip_id in ids:
            asset = _generated_asset(project, clip_id)
            if asset is not None:
                delta[clip_id] = {"source": "ai", "file": f"generated/{asset.name}"}
        _montage_delta(project, delta, "rescue")
        summary["marked"] = sorted(delta)
        logger.info("  [visual-story/rescue] нейрогенерация закрыла %s из %s: %s",
                    len(delta), len(ids), sorted(delta))
        # Кого не закрыла (цензура провайдера, исчерпанные попытки) — тем всё
        # ещё нужен кадр: пустая дыра в ролике хуже повтора соседнего кадра.
        # Раньше эта ветка выходила безусловно, и последний рубеж был
        # недостижим при включённой генерации (аудит 25.08).
        left = [clip_id for clip_id in ids if _generated_asset(project, clip_id) is None]
        if left:
            borrowed = _borrow_neighbour_frames(project, left)
            summary["borrowed"] = borrowed
            if borrowed:
                logger.info("  [visual-story/rescue] кадр соседа растянут на сцены: %s",
                            ", ".join(f"{k}←{v}" for k, v in borrowed.items()))
            else:
                logger.warning("  [visual-story/rescue] сцены %s остались без кадра: "
                               "ни генерация, ни сосед не помогли", left)
        return summary

    # Нейрогенерации нет: пробуем те же источники ещё раз, но коротким запросом.
    from units.archive_photos import RealPhotoProcessor

    retry_selections = []
    for plan in plans:
        selection = _clip_selection(plan)
        # Повтор идёт с ПЕРЕПИСАННЫМ запросом, и пишет его промптер архивов.
        # Сжатие оставлено запасным: оно берёт начало описания, то есть место
        # действия, и предмет сцены в запрос не попадает (улов 26.08).
        from units.archive_photos import archive_query_from_scene

        scene = plan.get("description") or plan.get("visual_intent") or ""
        short = archive_query_from_scene(
            scene,
            voiceover=str(plan.get("voiceover") or ""),
            clip_id=plan.get("scene_id"),
        ) or compact_query(scene)
        if short:
            selection.search_query = short
        selection.context_text = _context_text(plan)
        retry_selections.append(selection)
    logger.info("  [visual-story/rescue] нейрогенерации нет — повтор источников с кратким "
                "запросом: %s", [s.search_query for s in retry_selections][:4])
    processor = RealPhotoProcessor(
        project_dir=project, target_ratio=1.0, min_ratio=1.0, max_ratio=1.0,
        sources=list(getattr(config, "REAL_PHOTO_SOURCES", ["wikimedia", "nasa", "loc", "archive"])),
        target_w=1920, target_h=1080, fps=getattr(config, "VIDEO_FPS", 60),
    )
    summary["mode"] = "retry_short_query"
    summary["queries"] = {int(s.clip_id): s.search_query for s in retry_selections}
    try:
        summary["result"] = processor.process_phase(retry_selections)
    except Exception as exc:  # noqa: BLE001
        logger.warning("  [visual-story/rescue] повтор источников упал: %s", exc)
        summary["result"] = {"error": str(exc)}

    # Последний рубеж: и краткий запрос не дал кадра — берём ближайшую
    # ПРЕДЫДУЩУЮ картинку и растягиваем её на длину этой сцены (растяжение
    # делает склейка: у статика длительность задаётся клипом). Пустая дыра в
    # ролике хуже повтора соседнего кадра (решение пользователя 25.08).
    still_empty = [int(p["scene_id"]) for p in plans
                   if _generated_asset(project, int(p["scene_id"])) is None]
    if still_empty:
        borrowed = _borrow_neighbour_frames(project, still_empty)
        summary["borrowed"] = borrowed
        if borrowed:
            logger.info("  [visual-story/rescue] кадр соседа растянут на сцены: %s",
                        ", ".join(f"{k}←{v}" for k, v in borrowed.items()))
    return summary


def _borrow_neighbour_frames(project: Path, clip_ids: list[int]) -> dict[int, int]:
    """Скопировать ближайший ПРЕДЫДУЩИЙ кадр-картинку в пустые сцены.

    Возвращает {клип: донор}. Ищем строго назад и только картинки: видеофрагмент
    соседа нельзя растянуть без пересборки, а статик склейка сама разведёт на
    нужную длительность."""
    montage = _load_montage(project)
    generated = project / "generated"
    borrowed: dict[int, int] = {}
    for clip_id in sorted(clip_ids):
        donor_id, donor_file = None, None
        for candidate in range(clip_id - 1, 0, -1):
            found = sorted(generated.glob(f"{candidate:03d}.*"))
            found = [f for f in found if f.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp')]
            if found:
                donor_id, donor_file = candidate, found[0]
                break
        if donor_file is None:
            continue
        target = generated / f"{clip_id:03d}{donor_file.suffix}"
        try:
            shutil.copy2(donor_file, target)
        except OSError as exc:
            logger.warning("  [visual-story/rescue] кадр соседа не скопировался (%s): %s",
                           clip_id, exc)
            continue
        clip = montage["clips"][clip_id - 1]
        donor_clip = montage["clips"][donor_id - 1]
        clip["source"] = donor_clip.get("source") or "real_photo"
        clip["file"] = f"generated/{target.name}"
        clip["borrowed_from_clip"] = donor_id
        borrowed[clip_id] = donor_id
    if borrowed:
        _save_montage(project, montage)
    return borrowed


def _route_reuse_plans(
    project: Path,
    plans: list[dict[str, Any]],
    plans_by_id: dict[int, dict[str, Any]],
    montage: dict[str, Any],
    *,
    provider: str | None,
    model: str | None,
) -> dict[str, Any]:
    """Переиспользование = переиспользование ПОИСКА, не файла (H8, 23.08).

    Фото-донор: ведомая сцена уходит в новый полноценный EPF/архив-поиск с
    запросом донора (дедуп по адресам гарантирует другой кадр). Ютуб-донор:
    сцена уходит в ютуб-этап с роликами донора вместо поиска — новый отрезок
    под СВОЮ длительность, с оглядкой на леджер. Донор не разрешился — сцена
    ищет сама по своему описанию (``visual_intent``).
    """
    reuse_plans = [
        plan for plan in plans
        if plan.get("visual_strategy") == "reuse_previous"
        # AI-сцена не переиспользует ни файл, ни поиск: продолжение снимается
        # своей генерацией со своим промптом (решение пользователя 25.08).
        and str(plan.get("preferred_source") or "").lower() != "ai"
        and not _scene_asset_exists(project, int(plan["scene_id"]))
    ]
    summary: dict[str, Any] = {
        "selected": len(reuse_plans),
        "youtube": [],
        "photo": [],
        "self_search": [],
        "errors": {},
    }
    if not reuse_plans:
        return summary

    from units.source_ledger import YOUTUBE_SOURCES, apply_clip_source_delta

    photo_plans: list[dict[str, Any]] = []
    youtube_sources: dict[int, list[dict[str, Any]]] = {}
    youtube_delta: dict[int, dict[str, Any]] = {}
    for plan in reuse_plans:
        clip_id = int(plan["scene_id"])
        clip = montage["clips"][clip_id - 1]
        root_id = _resolve_donor_root(plans_by_id, plan)
        story_meta = dict(clip.get("visual_story") or {})
        story_meta["reuse_clip_id"] = root_id or plan.get("reuse_clip_id")

        if root_id is None:
            logger.info(
                "[visual-story/reuse] сцена %s: донор не разрешился — "
                "поиск по описанию сцены", clip_id,
            )
            pseudo = dict(plan)
            pseudo["search_query"] = str(plan.get("visual_intent") or "").strip()
            story_meta["reuse_route"] = "self_search"
            clip["visual_story"] = story_meta
            photo_plans.append(pseudo)
            summary["self_search"].append(clip_id)
            continue

        root_plan = plans_by_id[root_id]
        donor_clip = montage["clips"][root_id - 1]
        donor_source = str(donor_clip.get("source") or "")
        donor_videos = (
            _donor_video_sources(project, root_id)
            if donor_source in YOUTUBE_SOURCES
            else []
        )
        if donor_videos:
            logger.info(
                "[visual-story/reuse] сцена %s: донор %s (ютуб) — новый "
                "отрезок в роликах донора (%d шт.)",
                clip_id, root_id, len(donor_videos),
            )
            story_meta["reuse_route"] = "youtube"
            clip["visual_story"] = story_meta
            youtube_sources[clip_id] = donor_videos
            youtube_delta[clip_id] = {
                "source": "youtube_pending",
                # Запросы донора: и вижн-контракту, и запасному discovery,
                # если ролики донора выберут квоту.
                "youtube_queries": list(
                    donor_clip.get("youtube_queries")
                    or root_plan.get("youtube_queries")
                    or []
                ),
                "search_queries": list(
                    donor_clip.get("search_queries")
                    or root_plan.get("search_queries")
                    or []
                ),
                "visual_story": story_meta,
            }
            summary["youtube"].append(clip_id)
        else:
            # Запрос донора — тот, что реально написала стадия (лежит в клипе
            # после этапа ③); план его не несёт. Фолбэк — описание донора.
            donor_query = (
                str(donor_clip.get("search_query") or "").strip()
                or _plan_photo_query(root_plan)
            )
            pseudo = dict(plan)
            pseudo["search_query"] = (
                donor_query or str(plan.get("visual_intent") or "").strip()
            )
            logger.info(
                "[visual-story/reuse] сцена %s: донор %s (фото) — новый "
                "поиск по запросу донора: %s",
                clip_id, root_id, pseudo["search_query"],
            )
            story_meta["reuse_route"] = "photo"
            clip["visual_story"] = story_meta
            photo_plans.append(pseudo)
            summary["photo"].append(clip_id)

    _save_montage(project, montage)

    if youtube_sources:
        apply_clip_source_delta(
            project / "montage_plan.json",
            youtube_delta,
            log=lambda message: logger.info(
                "[visual-story/reuse] %s", message
            ),
        )
        try:
            from units.tube_runner import run_youtube_stage

            summary["youtube_result"] = run_youtube_stage(
                project,
                target_w=1920,
                target_h=1080,
                fps=getattr(config, "VIDEO_FPS", 60),
                deadline_ts=None,
                sources_by_clip=youtube_sources,
            )
        except Exception as exc:
            logger.warning(
                "[visual-story/reuse] ютуб-маршрут упал: %s", exc
            )
            summary["errors"]["youtube"] = str(exc)

    if photo_plans:
        selections = []
        for plan in photo_plans:
            sel = _clip_selection(plan)
            sel.context_text = _context_text(plan)
            selections.append(sel)
        try:
            from engine.helpers.epf_link import (
                run_automatic_job,
            )
        except ImportError:                           # запуск не пакетом, а из agent/
            logger.debug("  [visual-story] мост EPF импортирован как tools.*")
            from helpers.epf_link import run_automatic_job
        try:
            epf_summary = run_automatic_job(
                project,
                selections,
                provider=provider,
                model=model,
                timeout_sec=getattr(config, "VISUAL_STORY_EPF_TIMEOUT", 180),
                deadline_ts=time.time() + (
                    getattr(config, "VISUAL_STORY_EPF_TIMEOUT", 180)
                    * max(1, len(selections))
                ),
            )
            fresh = _load_montage(project)
            _apply_epf_metadata(
                fresh,
                {int(plan["scene_id"]): plan for plan in photo_plans},
                epf_summary,
            )
            _save_montage(project, fresh)
            summary["photo_result"] = {
                "ingested": len(epf_summary.get("ingest", {}).get("results", [])),
            }
        except Exception as exc:
            logger.warning(
                "[visual-story/reuse] фото-маршрут EPF упал: %s", exc
            )
            summary["errors"]["photo"] = str(exc)
        still_missing = [
            plan for plan in photo_plans
            if _generated_asset(project, int(plan["scene_id"])) is None
        ]
        if still_missing:
            try:
                summary["archive_result"] = _run_archive_fallback(
                    project, still_missing
                )
            except Exception as exc:                  # noqa: BLE001
                logger.warning("  [visual-story] архивный запасной путь упал (%s) — "
                               "клипы %s уходят в добор", str(exc)[:160], still_missing)
                summary["errors"]["archive"] = str(exc)

    return summary


def _load_montage(project: Path) -> dict[str, Any]:
    return json.loads(
        (project / "montage_plan.json").read_text(encoding="utf-8-sig")
    )


def _save_montage(project: Path, montage: dict[str, Any]) -> None:
    (project / "montage_plan.json").write_text(
        json.dumps(montage, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _run_youtube_for_visual_story(
    project: Path,
    youtube_plans: list[dict[str, Any]],
    youtube_summary: dict[str, Any],
) -> set[int]:
    """Ютуб-стадия целиком. Возвращает номера сцен, которые она НЕ закрыла.

    План правится только дельтами: стадия работает одновременно со стоком и
    нейрогенерацией, и снимок целого файла затёр бы их клипы.
    """
    if not youtube_plans:
        return set()
    ids = [int(plan["scene_id"]) for plan in youtube_plans]
    if not getattr(config, "YOUTUBE_FOOTAGE_ENABLED", False):
        logger.info("  [visual-story/youtube] стадия выключена — %s сцен уходят архивам", len(ids))
        _montage_delta(project, {cid: {"source": "real_photo_pending"} for cid in ids}, "youtube")
        return set(ids)
    try:
        from units.tube_runner import run_youtube_stage

        # Метку «в работе» ставим ТОЛЬКО тем, у кого кадра ещё нет: стадия
        # набирает клипы именно по ней, а на готовой сцене такая метка означала
        # бы незавершённость там, где всё сделано.
        _montage_delta(
            project,
            {cid: {"source": "youtube_pending"} for cid in ids
             if not _scene_asset_exists(project, cid)},
            "youtube",
        )
        logger.info("  [visual-story/youtube] старт: %s сцен", len(ids))
        yt_result = run_youtube_stage(
            project,
            target_w=1920,
            target_h=1080,
            fps=getattr(config, "VIDEO_FPS", 60),
            deadline_ts=None,
        )
        youtube_summary.update(yt_result)
        resolved = [
            clip_id for clip_id in ids
            if (project / "vid_img" / f"{clip_id:03d}.mp4").exists()
        ]
        youtube_summary["resolved"] = resolved
        unresolved = {clip_id for clip_id in ids if clip_id not in resolved}
        # Снимаем «youtube_pending» с тех, у кого фрагмент на диске. Метку
        # ставим всем сценам стадии заранее, а сама стадия могла клип
        # пропустить (кадр уже был готов) — тогда маркер незавершённости висел
        # на здоровой сцене, и её считали незакрытой: `imggen` выбрасывал её как
        # внешнюю, гейт записывал в нерешённые (улов 25.08, клипы 3 и 16).
        _montage_delta(
            project,
            {clip_id: {"source": "youtube", "file": f"vid_img/{clip_id:03d}.mp4"}
             for clip_id in resolved},
            "youtube",
        )
        logger.info("  [visual-story/youtube] итог: закрыто %s из %s%s",
                    len(resolved), len(ids),
                    f", не закрыто {sorted(unresolved)}" if unresolved else "")
        return unresolved
    except Exception as exc:                          # noqa: BLE001
        logger.warning("  [visual-story/youtube] стадия упала (%s) — %s сцен уходят архивам",
                       exc, len(ids))
        youtube_summary["error"] = str(exc)
        _montage_delta(project, {cid: {"source": "real_photo_pending"} for cid in ids}, "youtube")
        return set(ids)


def _run_ai_for_visual_story(project: Path, plans: list[dict[str, Any]]) -> dict[str, Any]:
    """Нейрогенерация сцен с маркером «ai».

    Промпты им написаны ещё на этапе анализа, поиска здесь нет вовсе — стадия ни
    от чего не зависит и идёт одновременно со стоком и ютубом.
    """
    from units.story_ai import ai_plans, generate_ai_assets, is_slideshow

    selected = ai_plans(plans)
    summary: dict[str, Any] = {"selected": len(selected)}
    if not selected:
        return summary
    ids = [int(plan["scene_id"]) for plan in selected]
    logger.info("  [visual-story/ai] нейрогенерация: %s клипов%s",
                len(ids), " (слайдшоу)" if is_slideshow() else "")
    summary.update(generate_ai_assets(project, ids))
    delta: dict[int, dict[str, Any]] = {}
    for clip_id in ids:
        asset = _generated_asset(project, clip_id)
        if asset is not None:
            delta[clip_id] = {"source": "ai", "file": f"generated/{Path(asset).name}"}
    _montage_delta(project, delta, "ai")
    logger.info("  [visual-story/ai] итог: получено %s из %s", len(delta), len(ids))
    return summary


def _montage_delta(project: Path, delta: dict[int, dict[str, Any]], stage: str = "") -> None:
    """Записать правки СВОИХ клипов, не трогая чужие.

    Стадии идут одновременно (сток, ютуб, нейрогенерация), а `_save_montage`
    пишет файл целым снимком — параллельная стадия затёрла бы чужие клипы
    устаревшим состоянием. `apply_clip_source_delta` берёт filelock,
    перечитывает свежий файл и меняет только перечисленные клипы.
    """
    if not delta:
        return
    from units.source_ledger import apply_clip_source_delta

    apply_clip_source_delta(
        project / "montage_plan.json", delta,
        log=lambda message: logger.info("[visual-story/%s] %s", stage or "план", message),
    )


def _run_stock_for_visual_story(
    project: Path,
    plans: list[dict[str, Any]],
    montage: dict[str, Any],
) -> tuple[dict[str, Any], set[int]]:
    """Run only the Stock slice explicitly allocated by Auto Visual Story."""
    if not getattr(config, "STOCK_ENABLED", False):
        return {"enabled": False, "selected": 0, "resolved": [], "unresolved": []}, set()

    from units.stock_queries import StockQuery
    from units.stock_lookup import _scan_local_library
    from units.stock_runner import (
        StockProcessor,
        _enabled_sources,
        _match_local_by_words,
    )

    sources = list(_enabled_sources())
    stock_plans = [
        p for p in plans
        if p.get("visual_strategy") == "new_search"
        and str(p.get("preferred_source") or "").lower()
        in {"stock", "local", "stock_photo", "stock_video"}
        # Готовую сцену не переискиваем — см. тот же приём у ютуба.
        and not _scene_asset_exists(project, int(p["scene_id"]))
    ]
    if not stock_plans or not sources:
        return {
            "enabled": True,
            "sources": sources,
            "selected": len(stock_plans),
            "resolved": [],
            "unresolved": [int(p["scene_id"]) for p in stock_plans],
        }, {int(p["scene_id"]) for p in stock_plans}

    processor = StockProcessor(
        project,
        target_w=1920,
        target_h=1080,
        fps=getattr(config, "VIDEO_FPS", 60),
    )
    # Запросы пишет СТОК-СТАДИЯ своим промптером по карточке клипа (озвучка +
    # описание сцены + тема + соседи), как в стандартном режиме (этап ③, 25.08).
    # Промптер недоступен → запрос = описание сцены (деградация, не падение).
    stock_ids = {int(p["scene_id"]) for p in stock_plans}
    stock_clips = [c for c in montage["clips"] if int(c["id"]) in stock_ids]
    try:
        processor.queries = processor.build_queries(stock_clips)
    except Exception as exc:
        logger.warning("[visual-story/stock] промптер стока упал: %s — запрос из описания", exc)
        processor.queries = {}
    for p in stock_plans:
        clip_id = int(p["scene_id"])
        if clip_id not in processor.queries:
            processor.queries[clip_id] = StockQuery(
                clip_id=clip_id,
                query=str(p.get("description") or p.get("visual_intent") or "").strip(),
                media="photo" if str(p.get("media_type") or "").lower() == "photo" else "video",
                query_by="auto_visual_story",
                query_path="description_fallback",
            )

    library = str(getattr(config, "STOCK_LIBRARY_DIR", "") or "").strip().strip('"')
    if library and "local" in sources:
        items = _scan_local_library(Path(library).expanduser())
        for clip_id, local_file in _match_local_by_words(
            list(processor.queries.values()), items, Path(library).expanduser()
        ).items():
            processor.queries[clip_id].local_file = local_file

    resolved: list[int] = []
    unresolved: set[int] = set()
    delta: dict[int, dict[str, Any]] = {}
    by_id = {int(c["id"]): c for c in montage["clips"]}

    # Сцены идут параллельно, как в штатной стадии (28.08). Раньше AVS ходил
    # мимо `run_stock_stage` своим последовательным циклом, и распараллеленный
    # сток обычного режима его не касался: вижн на сцену ~40 с, десяток сцен —
    # почти десять минут. Уникальность футажа держит `_reserve_url` внутри
    # `process_clip`, поэтому два потока один ролик не заберут.
    import threading

    results_lock = threading.Lock()
    max_parallel = max(1, min(int(getattr(config, "STOCK_PARALLEL", 12) or 12),
                              len(stock_plans)))
    if max_parallel > 1:
        logger.info("[visual-story/stock] сцен разом: %d", max_parallel)

    def _handle_plan(p: dict[str, Any]) -> None:
        clip_id = int(p["scene_id"])
        clip = by_id.get(clip_id)
        if clip is None:
            logger.warning("[visual-story/stock] клип %s есть в планах, но не в montage_plan "
                           "— пропускаю", clip_id)
            with results_lock:
                unresolved.add(clip_id)
            return
        preferred = str(p.get("preferred_source") or "").lower()
        primary = "local" if preferred == "local" and "local" in sources else sources[0]
        try:
            result = processor.process_clip(clip, primary, sources)
        except Exception as exc:                      # noqa: BLE001
            # Падение на ОДНОМ клипе не должно валить всю стадию: иначе её
            # сцены минуют EPF и архивы (их отбирают по `stock_unresolved`).
            logger.warning("[visual-story/stock] клип %s упал (%s) — уйдёт архивам", clip_id, exc)
            result = {"status": "error"}
        with results_lock:
            if result.get("status") == "ok":
                resolved.append(clip_id)
                patch = {"source": clip.get("source")}
                for key in ("file", "stock_source", "stock_page"):
                    if key in clip:
                        patch[key] = clip[key]
                delta[clip_id] = patch
            else:
                unresolved.add(clip_id)
                clip.pop("source", None)
                delta[clip_id] = {"source": None}

    if max_parallel == 1:
        for p in stock_plans:
            _handle_plan(p)
    else:
        with ThreadPoolExecutor(max_workers=max_parallel,
                                thread_name_prefix="avs-stock") as pool:
            list(pool.map(_handle_plan, stock_plans))
    processor.write_credits()
    # Пишем ДЕЛЬТОЙ, а не снимком всего плана: стадия работает одновременно с
    # ютубом и нейрогенерацией, и снимок, снятый до старта пула, откатывал их
    # уже записанные клипы к «pending» — готовое видео выпадало из плана
    # (аудит 25.08; штатная стоковая стадия давно пишет дельтой).
    _montage_delta(project, delta, "stock")
    return {
        "enabled": True,
        "sources": sources,
        "selected": len(stock_plans),
        "resolved": resolved,
        "unresolved": sorted(unresolved),
    }, unresolved


def materialize_visual_assets(
    *,
    project_dir: str | Path,
    plans: list[dict[str, Any]],
    fallback: str,
    provider: str | None = None,
    model: str | None = None,
    mock_assets: bool = False,
    transport=None,
    style_guide: str = "",
) -> dict[str, Any]:
    project = Path(project_dir).resolve()
    story = project / "visual_story"
    generated = project / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    montage = _load_montage(project)
    plans_by_id = {int(plan["scene_id"]): plan for plan in plans}
    epf_summary: dict[str, Any] = {}

    if mock_assets:
        from PIL import Image, ImageDraw

        for plan in plans:
            clip_id = int(plan["scene_id"])
            target = generated / f"{clip_id:03d}.jpg"
            image = Image.new("RGB", (1280, 720), (26 + clip_id * 17 % 120, 48, 72))
            draw = ImageDraw.Draw(image)
            draw.text((40, 40), f"Visual Story clip {clip_id}", fill="white")
            image.save(target, quality=90)
            clip = montage["clips"][clip_id - 1]
            clip["source"] = "real_photo"
            clip["file"] = f"generated/{clip_id:03d}.jpg"
            clip["real_meta"] = {
                "archive": "mock",
                "title": plan.get("visual_intent", ""),
                "license": "test-only",
                "credit_text": "",
            }
        _save_montage(project, montage)
        summary = {
            "mode": "mock",
            "resolved": sorted(plans_by_id),
            "unresolved": [],
        }
        (story / "selection_manifest.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return summary

    # Сток, ютуб и нейрогенерация работают КАЖДАЯ СО СВОИМИ клипами и ничем друг
    # другу не мешают: разные сервисы, разные файлы, а план правится атомарными
    # дельтами под filelock. Раньше они шли подряд, и нейрогенерация — которой
    # промпты написаны ещё на этапе анализа — просто ждала конца поиска
    # (замечание пользователя 25.08). EPF идёт ПОСЛЕ: он доедает то, что сток и
    # ютуб не закрыли, и должен видеть их итог.
    # Уже закрытые сцены стадии не трогают: при повторном заходе (добор одного
    # клипа, продолжение прерванного прогона) ютуб иначе идёт искать заново для
    # всех своих сцен, хотя кадры у них давно на диске — улов 25.08.
    youtube_plans = [
        plan for plan in plans
        if plan.get("visual_strategy") == "new_search"
        and str(plan.get("preferred_source") or "").lower() == "youtube"
        and not _scene_asset_exists(project, int(plan["scene_id"]))
    ]
    youtube_summary: dict[str, Any] = {
        "enabled": bool(getattr(config, "YOUTUBE_FOOTAGE_ENABLED", False)),
        "selected": len(youtube_plans),
        "resolved": [],
        "unresolved": [],
    }
    youtube_unresolved: set[int] = set()

    def _stage_stock():
        return _run_stock_for_visual_story(project, plans, montage)

    def _stage_youtube():
        return _run_youtube_for_visual_story(project, youtube_plans, youtube_summary)

    def _stage_ai():
        return _run_ai_for_visual_story(project, plans)

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            "stock": pool.submit(_stage_stock),
            "youtube": pool.submit(_stage_youtube),
            "ai": pool.submit(_stage_ai),
        }
        results: dict[str, Any] = {}
        for name, future in futures.items():
            try:
                results[name] = future.result()
            except Exception as exc:                  # noqa: BLE001
                logger.warning("  [visual-story] стадия «%s» упала целиком (%s) — "
                               "её клипы уйдут в добор", name, exc)
                results[name] = None

    stock_summary, stock_unresolved = results.get("stock") or ({}, set())
    youtube_unresolved = results.get("youtube") or set()
    ai_summary = results.get("ai") or {"selected": 0}
    montage = _load_montage(project)

    # Реюз ждёт ТОЛЬКО своего донора, а не конца всего прогона (замечание
    # пользователя 25.08). Доноры стока, ютуба и нейрогенерации уже закрыты —
    # значит их ведомые сцены можно делать прямо сейчас, а не спустя двадцать
    # минут после EPF, архивов и добора. Кому донор ещё не готов — подхватит
    # вторая волна в конце.
    reuse_summary = _route_reuse_plans(
        project, plans, plans_by_id, montage, provider=provider, model=model,
    )
    if reuse_summary.get("selected"):
        logger.info("  [visual-story/реюз] ранняя волна: сцен %s (доноры готовы)",
                    reuse_summary.get("selected"))
    montage = _load_montage(project)

    search_plans = [
        plan for plan in plans
        if plan.get("visual_strategy") == "new_search"
        and str(plan.get("preferred_source") or "").lower() != "ai"
        and (
            str(plan.get("preferred_source") or "").lower()
            not in {"stock", "local", "stock_photo", "stock_video", "youtube"}
            or int(plan["scene_id"]) in stock_unresolved
            or int(plan["scene_id"]) in youtube_unresolved
        )
        and _generated_asset(project, int(plan["scene_id"])) is None
    ]
    if search_plans:
        # Запросы пишет писатель Real Photo стадии по карточкам клипов;
        # вижн-отбор получает описание сцены и запреты (этап ③, 25.08).
        selections = _epf_selections(project, search_plans)
        try:
            from engine.helpers.epf_link import run_automatic_job
        except ImportError:                           # запуск не пакетом, а из agent/
            logger.debug("  [visual-story] мост EPF импортирован как tools.*")
            from helpers.epf_link import run_automatic_job
        try:
            epf_summary = run_automatic_job(
                project,
                selections,
                provider=provider,
                model=model,
                timeout_sec=getattr(config, "VISUAL_STORY_EPF_TIMEOUT", 180),
                deadline_ts=time.time() + (
                    getattr(config, "VISUAL_STORY_EPF_TIMEOUT", 180)
                    * max(1, len(selections))
                ),
            )
        except Exception as exc:
            logger.warning("Auto Visual Story EPF failed: %s", exc)
            epf_summary = {"error": str(exc), "ingest": {"results": []}}
        montage = _load_montage(project)
        _apply_epf_metadata(montage, plans_by_id, epf_summary)
        _save_montage(project, montage)

    unresolved_new = [
        plan for plan in search_plans
        if _generated_asset(project, int(plan["scene_id"])) is None
    ]
    archive_summary: dict[str, Any] = {}
    if unresolved_new and fallback in {"archive", "reuse_previous"}:
        archive_summary = _run_archive_fallback(project, unresolved_new)

    # Спасение провалившихся сцен (25.08). Вижн честно забраковал всё, что нашли,
    # и клип остался бы дырой в склейке. Два пути:
    #   • нейрогенерация включена → сцена достаётся ей (для этого она и есть);
    #   • выключена → повтор ТОГО ЖЕ источника с ужатым до сути запросом
    #     (главный объект + эпоха): длинная фраза режиссёра ищется плохо.
    rescue_summary = _rescue_unresolved(
        project,
        [plan for plan in search_plans
         if _generated_asset(project, int(plan["scene_id"])) is None],
        transport=transport,
        project_name=project.name,
        style_guide=style_guide,
    )

    # Вторая волна реюза: доноры, закрывшиеся уже после ранней волны — на
    # поиске, в архивах или в доборе. Сцены, сделанные ранней волной, сюда не
    # попадут: `_route_reuse_plans` берёт только тех, у кого файла ещё нет.
    late_reuse = _route_reuse_plans(
        project,
        plans,
        plans_by_id,
        _load_montage(project),
        provider=provider,
        model=model,
    )

    # Последний добор — по ФАКТУ отсутствия файла, а не по стратегии плана.
    # Первый добор берёт только `new_search` без «ai», и мимо него проходили:
    # сцены с реюзом (донор мог не закрыться) и ai-сцены, у которых сорвалась
    # генерация — например по цензуре провайдера. Финальная проверка их просто
    # записывала в нерешённые (замечание пользователя 25.08). Здесь ждать уже
    # некого: реюз отработал, генерация тоже.
    still_empty = [
        plan for plan in plans
        if not _scene_asset_exists(project, int(plan["scene_id"]))
    ]
    late_rescue: dict[str, Any] = {"selected": 0}
    if still_empty:
        logger.info("  [visual-story/rescue] финальный добор: сцен без файла %s",
                    [int(p["scene_id"]) for p in still_empty])
        late_rescue = _rescue_unresolved(
            project, still_empty, transport=transport,
            project_name=project.name, style_guide=style_guide,
        )

    montage = _load_montage(project)
    unresolved: list[dict[str, Any]] = []
    for plan in plans:
        clip_id = int(plan["scene_id"])
        clip = montage["clips"][clip_id - 1]
        if _generated_asset(project, clip_id) is None:
            # В vid_img/ кладут ВСЕ видеоисточники (ютуб, сток, архивное видео),
            # а проверялся только ютуб: готовое стоковое видео считалось дырой и
            # его перетирали кадром соседа (аудит 25.08).
            from units.source_ledger import VIDEO_SOURCES

            if (
                str(clip.get("source") or "") in VIDEO_SOURCES
                and (project / "vid_img" / f"{clip_id:03d}.mp4").exists()
            ):
                clip["file"] = f"vid_img/{clip_id:03d}.mp4"
                continue
            if fallback == "reuse_previous" and clip_id > 1:
                try:
                    target = _copy_reused_asset(project, clip_id - 1, clip_id)
                    previous = montage["clips"][clip_id - 2]
                    clip["source"] = previous.get("source") or "real_photo"
                    clip["file"] = f"generated/{target.name}"
                    if previous.get("real_meta"):
                        clip["real_meta"] = dict(previous["real_meta"])
                    clip["visual_story"]["fallback"] = "reuse_previous"
                    continue
                except Exception as exc:              # noqa: BLE001
                    logger.warning("  [visual-story] клип %s: кадр донора не скопирован "
                                   "(%s) — сцена уходит в нерешённые", clip_id, exc)
            unresolved.append({
                "clip_id": clip_id,
                "query": plan.get("search_query", ""),
                "error": "No EPF or archive asset was materialized",
            })
        else:
            asset = _generated_asset(project, clip_id)
            clip["file"] = f"generated/{asset.name}"
            if str(clip.get("source") or "").endswith("_pending"):
                clip["source"] = "real_photo"

    _save_montage(project, montage)
    # Ютуб-сцены (в т.ч. переиспользование по ютуб-маршруту) кладут материал в
    # vid_img/, а не в generated/ — учитываем оба места.
    resolved = [
        int(plan["scene_id"])
        for plan in plans
        if _scene_asset_exists(project, int(plan["scene_id"]))
    ]
    summary = {
        "mode": "live",
        "fallback": fallback,
        "resolved": resolved,
        "unresolved": unresolved,
        "ai": ai_summary,
        "rescue": rescue_summary,
        "stock": stock_summary,
        "youtube": youtube_summary,
        "epf": epf_summary,
        "archive": archive_summary,
        "reuse": reuse_summary,
        "reuse_late": late_reuse,
        "rescue_late": late_rescue,
    }
    (story / "selection_manifest.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (story / "unresolved.json").write_text(
        json.dumps({"items": unresolved}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return summary
