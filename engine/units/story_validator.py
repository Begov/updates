"""Validation gates for Auto Visual Story artifacts."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable


DurationPolicy = Callable[[float], tuple[float, float, float]]


def validate_scenes(
    scenes: list[dict[str, Any]],
    word_count: int,
    audio_duration: float,
    *,
    min_duration: float = 3.0,
    max_duration: float = 12.0,
    duration_policy: DurationPolicy | None = None,
) -> None:
    if not scenes:
        raise ValueError("Scene plan is empty")
    expected_word = 0
    previous_end = 0.0
    for expected_id, scene in enumerate(scenes, 1):
        if scene.get("id") != expected_id:
            raise ValueError(f"Expected scene id {expected_id}, got {scene.get('id')}")
        if int(scene.get("word_start_index", -1)) != expected_word:
            raise ValueError(f"Scene {expected_id} does not continue word coverage")
        start = float(scene["startTime"])
        end = float(scene["endTime"])
        if abs(start - previous_end) > 0.011:
            raise ValueError(
                f"Scene {expected_id} creates a timeline gap/overlap: "
                f"{previous_end:.3f} -> {start:.3f}"
            )
        duration = end - start
        scene_min, _, scene_max = (
            duration_policy(start)
            if duration_policy
            else (min_duration, 0.0, max_duration)
        )
        if duration < scene_min - 0.011 or duration > scene_max + 0.011:
            # Как у стандартного режиссёра: выход за мин/макс — замечание в
            # лог, а не смерть прогона (валидатор montage_plan копит те же
            # строки в errors, и gpt_director печатает «N замечаний»).
            # Жёсткими остаются покрытие слов и непрерывность таймлайна.
            logging.warning(
                "Visual Story: сцена %s длительностью %.3fс вне диапазона "
                "%.1f..%.1fс (замечание, не ошибка)",
                expected_id, duration, scene_min, scene_max,
            )
        expected_word = int(scene["word_end_index"]) + 1
        previous_end = end
    if expected_word != word_count:
        raise ValueError(f"Scene plan covers {expected_word}/{word_count} words")
    if abs(previous_end - audio_duration) > 0.011:
        raise ValueError(
            f"Scene plan ends at {previous_end:.3f}, audio ends at {audio_duration:.3f}"
        )


def validate_narrative_state(
    state: dict[str, Any],
    scene_count: int | None = None,
) -> None:
    chapters = state.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise ValueError("Narrative state must contain at least one chapter")
    seen = set()
    for item in chapters:
        chapter_id = str(item.get("id") or "").strip()
        if not chapter_id or chapter_id in seen:
            raise ValueError(f"Invalid or duplicate chapter id: {chapter_id!r}")
        seen.add(chapter_id)

    # Пер-сценный слой непрерывности. Его может не быть вовсе (старые артефакты
    # и резюме по ним), но если он пришёл — номера сцен обязаны быть настоящими:
    # запись про несуществующую сцену молча потерялась бы при сборке пакетов.
    overrides = state.get("scene_overrides")
    if overrides is None:
        return
    if not isinstance(overrides, list):
        raise ValueError("scene_overrides must be a list when present")
    numbers = set()
    for item in overrides:
        if not isinstance(item, dict):
            raise ValueError("scene_overrides entries must be objects")
        try:
            scene_id = int(item.get("scene_id"))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"scene_overrides entry without a valid scene_id: {item!r}"
            ) from exc
        if scene_id in numbers:
            raise ValueError(f"Duplicate scene_overrides entry for scene {scene_id}")
        numbers.add(scene_id)
        if scene_count is not None and not 1 <= scene_id <= scene_count:
            raise ValueError(
                f"scene_overrides references scene {scene_id}, "
                f"but the plan has {scene_count} scenes"
            )


def validate_visual_plans(
    plans: list[dict[str, Any]],
    scene_count: int,
    *,
    allow_reuse: bool = True,
) -> None:
    if len(plans) != scene_count:
        raise ValueError(f"Expected {scene_count} visual plans, got {len(plans)}")
    for expected_id, plan in enumerate(plans, 1):
        if int(plan.get("scene_id", 0)) != expected_id:
            raise ValueError(f"Visual plan order mismatch at scene {expected_id}")
        strategy = plan.get("visual_strategy")
        if strategy not in {"new_search", "reuse_previous"}:
            raise ValueError(f"Scene {expected_id}: invalid strategy {strategy!r}")
        if strategy == "reuse_previous" and not allow_reuse:
            raise ValueError(f"Scene {expected_id}: reuse_previous is disabled")
        if not str(plan.get("visual_intent") or "").strip():
            raise ValueError(f"Scene {expected_id}: visual_intent is empty")
        # Поисковые запросы пишут стадии по маркерам (этап ③), у плана их может
        # не быть — требование search_query для new_search снято 25.08.
        if strategy != "new_search":
            reuse_id = int(plan.get("reuse_clip_id") or expected_id - 1)
            if expected_id == 1 or reuse_id < 1 or reuse_id >= expected_id:
                raise ValueError(
                    f"Scene {expected_id}: invalid reuse_clip_id {reuse_id}"
                )


def validate_montage_compatibility(
    montage: dict[str, Any],
    audio_duration: float,
    *,
    min_duration: float = 3.0,
    max_duration: float = 12.0,
) -> None:
    from units.plan_validator import validate_montage_plan_json

    valid, errors = validate_montage_plan_json(
        montage,
        audio_duration,
        min_duration=min_duration,
        max_duration=max_duration,
    )
    if not valid:
        raise ValueError("Montage compatibility failed:\n- " + "\n- ".join(errors))


def final_asset_gate(
    project_dir: str | Path,
    montage: dict[str, Any],
    *,
    allow_unresolved: bool = False,
) -> dict[str, Any]:
    from PIL import Image
    from units.source_ledger import VIDEO_SOURCES

    project = Path(project_dir)
    generated = project / "generated"
    missing: list[int] = []
    unreadable: list[int] = []
    pending: list[int] = []
    resolved: list[int] = []
    for clip in montage.get("clips", []):
        clip_id = int(clip["id"])
        if str(clip.get("source") or "").endswith("_pending"):
            pending.append(clip_id)
        # Промежуточный файл импорта («007.new.jpg») под глоб «007.*» подходит,
        # но активом сцены не является: прогон, прерванный на записи, оставлял
        # его на диске, и сцена считалась закрытой (аудит 25.08).
        candidates = [
            path for path in sorted(generated.glob(f"{clip_id:03d}.*"))
            if not path.name.endswith(".new.jpg")
        ]
        # В vid_img/ лежат ВСЕ видеоисточники, не только ютуб: `stock_video` и
        # `real_video` кладут туда же (см. реестр в units/source_ledger.py).
        # Проверка одного ютуба роняла прогон на готовом стоковом видео.
        if str(clip.get("source") or "") in VIDEO_SOURCES:
            candidates += sorted((project / "vid_img").glob(f"{clip_id:03d}.mp4"))
        if not candidates:
            missing.append(clip_id)
            continue
        if candidates[0].suffix.lower() == ".mp4":
            if candidates[0].stat().st_size > 1024:
                resolved.append(clip_id)
            else:
                unreadable.append(clip_id)
        else:
            try:
                with Image.open(candidates[0]) as image:
                    image.verify()
                resolved.append(clip_id)
            except Exception as exc:                     # noqa: BLE001
                logging.warning("[visual-story/гейт] клип %s: файл %s не открывается (%s)",
                                clip_id, candidates[0].name, exc)
                unreadable.append(clip_id)
    summary = {
        "resolved": resolved,
        "missing": missing,
        "unreadable": unreadable,
        "pending": pending,
        "ok": not missing and not unreadable and not pending,
    }
    if not summary["ok"] and not allow_unresolved:
        raise RuntimeError(
            "Auto Visual Story final gate failed: "
            f"missing={missing}, unreadable={unreadable}, pending={pending}"
        )
    return summary
