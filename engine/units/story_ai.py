"""Ветка нейрогенерации Auto Visual Story (25.08).

Режиссёр второго этапа метит сцену `source: "ai"`, когда кадра нет ни в одном
архиве или когда постановочный кадр служит сцене лучше найденного. Такие клипы
не идут ни в один поиск: им пишутся промпты (отдельным вызовом ТОЛЬКО по этим
клипам, оператором режиссёра — как Pass 2 в стандартном режиме), затем работает
обычная генерация с настройками из GUI:

    картинки: PIPELINE_IMG_GEN   = fastgen | veononstop | cinai | skip
    видео:    PIPELINE_VIDEO_MODE = i2v | t2v | first_last | reference |
                                    components | skip

Картинки есть, видео `skip` — это слайдшоу: кадр остаётся статиком в
`generated/`, склейка сама добавит ему движение. Обе стадии выключены (галка
«Non-AI sources only») — ветка не запускается вовсе.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

import params as config
logger = logging.getLogger(__name__)

AI_SOURCE = "ai"
_PROMPTS_FILENAME = "{project}_prompts_ai.txt"


def ai_plans(plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Все планы под нейрогенерацию, включая продолжения сцены.

    Копировать кадр донора у AI-сцен НЕЛЬЗЯ: если сцена продолжается и снова
    требует генерации, ей нужен СВОЙ кадр — тот же герой, место и свет, но
    другой момент. Поэтому «продолжение» здесь означает не переиспользование
    файла, а отдельную генерацию с преемственностью в промпте (25.08)."""
    return [
        plan for plan in plans
        if str(plan.get("preferred_source") or "").lower() == AI_SOURCE
    ]


def image_provider() -> str:
    return str(getattr(config, "PIPELINE_IMG_GEN", "veononstop") or "veononstop").strip().lower()


def video_mode() -> str:
    return str(getattr(config, "PIPELINE_VIDEO_MODE", "i2v") or "i2v").strip().lower()


def images_enabled() -> bool:
    return image_provider() not in ("", "skip", "off", "none")


def videos_enabled() -> bool:
    return video_mode() not in ("", "skip", "off", "none")


def is_slideshow() -> bool:
    """Картинки генерим, видео — нет: кадр остаётся статиком."""
    return images_enabled() and not videos_enabled()


def _scene_payload(plan: dict[str, Any]) -> dict[str, Any]:
    continuity = plan.get("continuity") or {}
    payload = {
        "scene_id": int(plan["scene_id"]),
        "description": str(plan.get("description") or plan.get("visual_intent") or ""),
        "chapter_theme": str(plan.get("chapter_theme") or ""),
        "entities": list(plan.get("entities") or []),
        "must_not_appear": [
            str(x).replace("_", " ")
            for x in (continuity.get("must_not_reveal") or plan.get("negative_constraints") or [])
        ][:8],
    }
    # Продолжение сцены: тот же мир, но отдельный кадр — не копия донора.
    if plan.get("visual_strategy") == "reuse_previous" and plan.get("reuse_clip_id"):
        payload["continues_scene"] = int(plan["reuse_clip_id"])
    same_location = continuity.get("same_location_as_scene")
    if same_location:
        payload["same_location_as_scene"] = int(same_location)
    keep = [str(x).replace("_", " ") for x in (continuity.get("keep_entities") or [])][:6]
    if keep:
        payload["keep_looking_the_same"] = keep
    return payload


def build_prompt_request(
    template: str,
    plans: list[dict[str, Any]],
    *,
    project_name: str,
    theme: str,
    style_guide: str,
) -> str:
    scenes = [_scene_payload(plan) for plan in plans]
    return (
        template
        .replace("{{PROJECT_NAME}}", project_name)
        .replace("{{THEME}}", theme or "—")
        .replace("{{STYLE_GUIDE}}", (style_guide or "").strip() or "—")
        .replace("{{SCENES_JSON}}", json.dumps(scenes, ensure_ascii=False, indent=1))
    )


def _prompts_from_payload(payload: Any) -> dict[int, str]:
    items = payload.get("prompts") if isinstance(payload, dict) else payload
    result: dict[int, str] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        try:
            scene_id = int(item.get("scene_id"))
        except (TypeError, ValueError):
            logger.warning("   [AI/промпты] пропущен ответ без номера сцены: %r",
                           str(item)[:100])
            continue
        text = str(item.get("prompt") or "").strip()
        if text:
            result[scene_id] = text
    return result


def write_prompts_file(project_dir: Path, project_name: str, prompts: dict[int, str]) -> Path:
    """Файл промптов в формате, который читает `extract_all_prompts`.

    Префикс `[CLIP N]` обязателен: без него нумерация считается плотной 1..N и
    промпты уедут не на те клипы (у нас в плане только часть сцен)."""
    path = Path(project_dir) / _PROMPTS_FILENAME.format(project=project_name)
    # Дописываем к уже написанному, а не затираем: добор пишет промпт ОДНОЙ
    # спасённой сцене, и полная перезапись сносила промпты всех остальных
    # ai-клипов — файл оставался с единственным `[CLIP 4]` (улов 25.08).
    merged: dict[int, str] = {}
    if path.exists():
        try:
            for block in re.split(r"\n(?=\[CLIP \d+\])", path.read_text(encoding="utf-8-sig")):
                head = re.match(r"\[CLIP (\d+)\]\s*(.*)", block.strip(), re.S)
                if head:
                    merged[int(head.group(1))] = head.group(2).strip()
        except (OSError, ValueError) as exc:
            logger.warning("   [AI/промпты] прежний файл промптов не прочитан (%s) — пишу заново", exc)
    merged.update({int(clip_id): str(text).strip() for clip_id, text in prompts.items()})
    blocks = [f"[CLIP {clip_id}]\n{text}" for clip_id, text in sorted(merged.items())]
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
    return path


def _style_blocks(style_guide: str, project_dir: Path) -> tuple[str, str]:
    """(style_block, negative_block) из стайлгайда — как в Pass 2 стандартного режима.

    Стиль и негатив выносятся отдельными блоками и дописываются к каждому
    промпту постобработкой (`append_style_tail`/`append_negative_tail`), а не
    переписываются моделью в каждом кадре: так они всегда полные и одинаковые
    по всему ролику. Кэш `style_blocks.json` общий с Pass 2 (25.08)."""
    if not (style_guide or "").strip():
        return "", ""
    try:
        from units.style_blocks import resolve_style_blocks

        blocks = resolve_style_blocks(style_guide, project_dir) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("   [AI/промпты] стиль-блок не выделен: %s", exc)
        return "", ""
    style = str(blocks.get("style_block") or "").strip()
    negative = str(blocks.get("negative_block") or "").strip()
    if style or negative:
        logger.info("   [AI/промпты] стайлгайд: стиль-блок %s симв., негатив %s симв.",
                    len(style), len(negative))
    return style, negative


def apply_style_tails(prompts: dict[int, str], style: str, negative: str) -> dict[int, str]:
    """Дописать стиль и негатив в хвост каждого промпта — как Pass 2."""
    if not (style or negative):
        return prompts
    from units.codex_director import append_negative_tail, append_style_tail

    out: dict[int, str] = {}
    for clip_id, text in prompts.items():
        if style:
            text = append_style_tail(text, style)
        if negative:
            text = append_negative_tail(text, negative)
        out[clip_id] = text
    return out


def create_ai_prompts(
    transport,
    plans: list[dict[str, Any]],
    *,
    project_dir: Path,
    project_name: str,
    theme: str,
    style_guide: str,
    mock: bool = False,
) -> dict[int, str]:
    """Промпты для ai-клипов одним вызовом оператора режиссёра.

    Стайлгайд участвует дважды, как в стандартном режиме: целиком в задании
    (чтобы модель держала медиум, палитру и обработку) и отдельным стиль-блоком
    в хвосте каждого промпта."""
    selected = ai_plans(plans)
    if not selected:
        return {}
    logger.info("   [AI/промпты] сцен под генерацию %s: %s",
                len(selected), [int(p["scene_id"]) for p in selected])
    if mock:
        mocked = {
            int(p["scene_id"]): f"MOCK AI prompt for scene {p['scene_id']}: "
                                f"{str(p.get('description') or '')[:120]}"
            for p in selected
        }
        style, negative = _style_blocks(style_guide, Path(project_dir))
        return apply_style_tails(mocked, style, negative)
    template = (Path(__file__).parent.parent / "instructions" / "visual_story_ai_prompts.txt").read_text(
        encoding="utf-8")
    prompt = build_prompt_request(
        template, selected, project_name=project_name, theme=theme, style_guide=style_guide)
    payload = transport.call_json(
        phase="ai_prompts", prompt=prompt, schema_file="visual_story_ai_prompts.json")
    prompts = _prompts_from_payload(payload)
    missing = [int(p["scene_id"]) for p in selected if int(p["scene_id"]) not in prompts]
    if missing:
        logger.warning(
            "[visual-story/ai] без промпта остались клипы %s — повторный вызов", missing)
        repair = (
            prompt
            + "\n\nYour previous answer skipped these scene_id values: "
            + ", ".join(str(x) for x in missing)
            + ". Return prompts for EVERY scene listed above."
        )
        payload = transport.call_json(
            phase="ai_prompts_repair", prompt=repair,
            schema_file="visual_story_ai_prompts.json")
        prompts.update(_prompts_from_payload(payload))
    style, negative = _style_blocks(style_guide, Path(project_dir))
    prompts = apply_style_tails(prompts, style, negative)
    logger.info("   [AI/промпты] готово %s/%s%s", len(prompts), len(selected),
                " (стиль и негатив дописаны)" if (style or negative) else "")
    return prompts


def compact_query(text: str, *, limit: int = 7) -> str:
    """Ужать описание сцены до сути: главный объект и эпоха.

    Нужно для повторной попытки источника, когда нейрогенерации в прогоне нет
    (решение пользователя 25.08): длинная фраза режиссёра ищется плохо, а вот
    «Polish concert hall 1980s» находит. Служебные и связующие слова
    выбрасываются, год/десятилетие сохраняется, порядок остальных слов —
    исходный."""
    import re

    stop = {
        "a", "an", "the", "of", "in", "on", "at", "to", "for", "with", "and",
        "or", "as", "by", "from", "into", "over", "under", "between", "that",
        "this", "these", "those", "is", "are", "was", "were", "be", "being",
        "been", "its", "their", "his", "her", "unnamed", "unmarked", "showing",
        "shows", "seen", "visible", "while", "where", "which", "who", "whose",
        "still", "shot", "frame", "footage", "photograph", "photo", "image",
        "view", "scene", "moment", "close", "wide", "mid",
    }
    words = re.findall(r"[\w'-]+", str(text or ""))
    era, kept = [], []
    for word in words:
        low = word.lower()
        if re.fullmatch(r"(1[6-9]\d\d|20\d\d)s?", low):
            if low not in era:
                era.append(low)
            continue
        if low in stop or len(low) < 3:
            continue
        if low not in [k.lower() for k in kept]:
            kept.append(word)
    room = max(2, limit - len(era))
    return " ".join(kept[:room] + era).strip()


def generate_ai_assets(
    project_dir: Path,
    clip_ids: list[int],
    *,
    prompts_file: Optional[Path] = None,
) -> dict[str, Any]:
    """Картинки и (если включено) видео ТОЛЬКО для перечисленных клипов.

    `prompts_file` доводится до видеогенератора (он принимает путь явно). Для
    картинок такого параметра нет — `imggen` сам берёт самый свежий
    `*_prompts_*.txt` по времени правки, поэтому лишних файлов промптов в папке
    проекта быть не должно. Раньше параметр не использовался вовсе (аудит
    25.08): переданный путь молча игнорировался и на видеопути тоже.
    """
    summary: dict[str, Any] = {
        "clips": sorted(clip_ids),
        "image_provider": image_provider(),
        "video_mode": video_mode(),
        "slideshow": is_slideshow(),
        "images": {},
        "videos": {},
    }
    if not clip_ids:
        return summary
    project = Path(project_dir)

    ids = sorted(clip_ids)
    aspect = str(getattr(config, "VEONONSTOP_ASPECT_RATIO", "16:9") or "16:9")

    logger.info("   [AI/генерация] клипов %s | картинки: %s | видео: %s",
                len(ids), image_provider() if images_enabled() else "выкл",
                video_mode() if videos_enabled() else "выкл (слайдшоу)")
    if images_enabled():
        provider = image_provider()
        try:
            # Настройки те же, что у стандартного режима: провайдер, аспект,
            # параллельность, апскейл — всё из config/GUI, ничего своего.
            if provider == "fastgen":
                from units.fastgen_engine import generate_all_images as _fastgen_images
                summary["images"] = _fastgen_images(
                    project_dir=str(project), clip_ids=ids, silent=True) or {}
            elif provider == "cinai":
                from units.cinai_stills import generate_all_images as _cinai_images
                summary["images"] = _cinai_images(
                    project_dir=str(project), clip_ids=ids,
                    max_parallel=getattr(config, "CINAI_MAX_PARALLEL", 6)) or {}
            else:
                from units.veo_stills import generate_all_images as _veo_images
                summary["images"] = _veo_images(
                    project_dir=str(project), clip_ids=ids,
                    mode=getattr(config, "VEONONSTOP_IMGGEN_MODE", "imagen"),
                    max_parallel=getattr(config, "VEONONSTOP_MAX_PARALLEL", None),
                    aspect_ratio=aspect,
                    upscale_resolution=(getattr(config, "VEONONSTOP_IMAGE_UPSCALE", "off") or None),
                    silent=True) or {}
            logger.info("   [AI/генерация] картинки готовы: %s",
                        {k: v for k, v in (summary.get("images") or {}).items()
                         if k in ("generated", "failed", "total_clips")} or "—")
        except Exception as exc:  # noqa: BLE001
            # «Нечего генерировать» — не падение: на повторном заходе все кадры
            # уже лежат на диске, и генератор просто отфильтровывает готовое.
            # Раньше это писалось как «картинки упали» рядом со строкой
            # «получено 9 из 9» (улов прогона 25.08).
            if "no prompts to generate" in str(exc).lower():
                logger.info("   [AI/генерация] генерировать нечего — кадры уже на диске")
                summary["images"] = {"skipped": "already generated"}
            else:
                logger.warning("   [AI/генерация] картинки упали: %s", exc)
                summary["images"] = {"error": str(exc)}
    else:
        summary["images"] = {"skipped": "PIPELINE_IMG_GEN=skip"}

    if videos_enabled():
        try:
            from units.veo_motion import generate_all_videos
            summary["videos"] = generate_all_videos(
                project_dir=str(project), mode=video_mode(), clip_ids=ids,
                max_parallel=getattr(config, "VEONONSTOP_MAX_PARALLEL", None),
                aspect_ratio=aspect, silent=True,
                prompts_file_path=str(prompts_file) if prompts_file else None) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("[visual-story/ai] генерация видео упала: %s", exc)
            summary["videos"] = {"error": str(exc)}
    else:
        summary["videos"] = {"skipped": "слайдшоу: PIPELINE_VIDEO_MODE=skip"}
        logger.info(
            "  [visual-story/ai] видео выключено — %s клипов остаются статиками "
            "(склейка добавит движение)", len(clip_ids))
    return summary
