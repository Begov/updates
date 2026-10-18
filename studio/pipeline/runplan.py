"""Сборка запуска движка из снимка настроек.

Чистые функции без виджетов: на входе — словарь значений формы и привязки
конкретного файла очереди, на выходе — фазы (команды процесса), окружение и
слепок настроек склейки. Та же функция stage_env() используется кнопкой
«Сохранить по умолчанию», поэтому сохранённое и запущенное не расходятся.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .. import catalog, paths
from ..i18n import T, tr
from .defaults import image_provider, source_mix


def _b(value) -> str:
    return "true" if value else "false"


def _on(value) -> str:
    return "on" if value else "off"


def _clamp(value, lo, hi) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return lo


def transition_styles(v: dict) -> str:
    if v.get("transition_mode") == "random":
        return ",".join(catalog.TRANSITION_STYLES)
    return v.get("transition_style_fixed") or "fade"


def zoom_styles(v: dict) -> str:
    if v.get("zoom_mode") == "random":
        return ",".join(catalog.ZOOM_STYLES)
    return v.get("zoom_style_fixed") or "in-center"


def subtitles_template(v: dict) -> str:
    """Путь к шаблону .ass для выбранного разрешения и положения."""
    if not v.get("subtitles_enabled"):
        return ""
    if v.get("subtitles_custom"):
        return str(v.get("subtitles_path") or "")
    res = "1080p" if str(v.get("render_mode")) in ("1080p", "4k") else "720p"
    name = f"subtitles_{res}_center.ass" if v.get("subtitles_preset") == "center" else f"subtitles_{res}.ass"
    path = paths.TEMPLATES / name
    if path.exists():
        return str(path)
    found = sorted(paths.TEMPLATES.glob("*.ass"))
    return str(found[0]) if found else ""


def stage_env(v: dict) -> dict:
    """Настройки стадий (сток / инфографика) -> env."""
    m: dict = {}
    # Возможностей «Реальные фото», карточек персон/мест и атрибуции в программе нет.
    # Выключаем их явно, чтобы старые значения из engine/.env не включили их обратно.
    m["REAL_PHOTO_ENABLED"] = "false"
    m["REAL_PHOTO_EPF_BRIDGE"] = "0"
    m["NON_AI_IMAGE_MODE"] = "false"
    m["ATTRIBUTION_OVERLAY"] = "false"
    m["BEGO_LIVE_ANIMATE"] = "1" if v.get("video_live_animate") else "0"
    m["VIDEO_LIVE_ANIMATE"] = m["BEGO_LIVE_ANIMATE"]
    # доли источников — из блока «Состав видео» (проценты от всех клипов)
    mix = source_mix(v)
    stock = mix["mix_stock_video"] + mix["mix_stock_photo"]
    # сток: ноль в доле = стадия выключена целиком
    sources = [name for name, key in (("local", "stock_src_local"), ("pexels", "stock_src_pexels"),
                                      ("pixabay", "stock_src_pixabay")) if v.get(key)]
    m["STOCK_ENABLED"] = _b(v.get("stock_enabled") and sources and stock > 0)
    if sources:
        m["STOCK_SOURCES"] = ",".join(sources)
    m["STOCK_VISION_PER_SOURCE"] = str(max(0, int(v.get("stock_vision_per_source", 2))))
    m["STOCK_LIBRARY_DIR"] = str(v.get("stock_library_dir") or "").strip()
    m["STOCK_RATIO_TARGET"] = f"{stock / 100:.3f}"
    m["STOCK_START_SEC"] = str(max(0, int(v.get("stock_gate_sec", 0))))
    m["STOCK_VIDEO_SHARE"] = f"{mix['mix_stock_video'] / stock if stock else 0.7:.3f}"
    m["STOCK_PARALLEL"] = str(max(1, int(v.get("stock_parallel", 12))))
    m["STOCK_PIPELINE_WAIT_TIMEOUT"] = str(max(0, int(v.get("stock_tail_wait", 500))))
    m["STOCK_VISION_CANDIDATES"] = str(max(0, int(v.get("stock_vision_candidates", 3))))
    sv = v.get("stock_vision_provider") or "codex"
    m["STOCK_VISION_PROVIDER"] = sv
    m["STOCK_VISION_MODEL"] = catalog.operator_model_to_id(sv, v.get("stock_vision_model", ""))
    m["STOCK_MUTE_AUDIO"] = _b(v.get("stock_mute_audio"))
    m["STOCK_EXCLUDE_AI"] = _b(v.get("stock_exclude_ai"))
    m["STOCK_CLIP_START_SEC"] = f"{float(v.get('stock_start_sec', 0)):.2f}"
    m["STOCK_CLIP_START_JITTER"] = _b(v.get("stock_jitter"))
    m["STOCK_CLIP_START_JITTER_SEC"] = f"{float(v.get('stock_jitter_sec', 2)):.2f}"
    m["STOCK_WAIT_TIMEOUT"] = str(max(60, int(v.get("stock_wait_timeout", 30)) * 60))
    sp = v.get("stock_prompt_provider") or "auto"
    m["STOCK_PROMPT_PROVIDER"] = "" if sp == "auto" else sp
    m["STOCK_PROMPT_MODEL"] = "" if sp == "auto" else catalog.operator_model_to_id(sp, v.get("stock_prompt_model", ""))
    # ютуб-стадия в BegoFrame не используется
    m["YOUTUBE_FOOTAGE_ENABLED"] = "false"
    m["YOUTUBE_RATIO_TARGET"] = "0.000"
    # инфографика: ноль в доле = стадия выключена целиком; заданная доля — она же потолок
    target = mix["mix_infographic"]
    m["INFOGRAPHIC_ENABLED"] = _b(v.get("infographic_enabled") and target > 0)
    m["INFOGRAPHIC_RATIO_TARGET"] = f"{target / 100:.3f}"
    m["INFOGRAPHIC_START_SEC"] = str(max(0, int(v.get("infographic_gate_sec", 0))))
    m["INFOGRAPHIC_RATIO_MAX"] = f"{target / 100:.3f}"
    m["INFOGRAPHIC_IMG_PROVIDER"] = v.get("infographic_provider") or "fastgen"
    m["INFOGRAPHIC_FASTGEN_MODEL"] = v.get("infographic_fastgen_model") or "NARWHAL"
    m["INFOGRAPHIC_CINAI_MODEL"] = v.get("infographic_cinai_model") or catalog.CINAI_MODEL_DEFAULT
    m["INFOGRAPHIC_CINAI_ASPECT_RATIO"] = v.get("veo_ratio") or "16:9"      # формат ролика — один
    m["INFOGRAPHIC_CINAI_QUALITY"] = v.get("infographic_cinai_quality") or "1K"
    m["INFOGRAPHIC_VEONONSTOP_BANANA_MODEL"] = v.get("infographic_veo_model") or "GEM_PIX_2"
    m["INFOGRAPHIC_VERTEX_MODEL"] = v.get("infographic_vertex_model") or catalog.VERTEX_IMAGE_DEFAULT
    cv_model = v.get("infographic_cheapvibe_model") or "gpt-image-2"
    m["INFOGRAPHIC_CHEAPVIBE_MODEL"] = cv_model
    m["INFOGRAPHIC_CHEAPVIBE_SIZE"] = catalog.cheapvibe_size(v.get("veo_ratio") or "16:9",
                                                             v.get("infographic_cheapvibe_size") or "")
    m["INFOGRAPHIC_CHEAPVIBE_QUALITY"] = catalog.cheapvibe_quality(cv_model, v.get("infographic_cheapvibe_quality")
                                                                   or "medium")
    m["INFOGRAPHIC_STATIC_CONCAT"] = _b(v.get("infographic_static_concat"))
    # моушн-графика (Remotion + Codex): ноль в доле = стадия выключена
    motion = mix["mix_motion"]
    m["MOTION_GRAPHICS_ENABLED"] = _b(v.get("motion_enabled") and motion > 0)
    m["MOTION_GRAPHICS_RATIO_TARGET"] = f"{motion / 100:.3f}"
    m["MOTION_GRAPHICS_START_SEC"] = str(max(0, int(v.get("motion_gate_sec", 0))))
    m["MOTION_GRAPHICS_MODEL"] = str(v.get("motion_model") or "")
    m["MOTION_GRAPHICS_PARALLEL"] = str(_clamp(v.get("motion_parallel", 2), 1, 8))
    # «+ потоки CinAI» к другому провайдеру убраны: пул выключен явно, чтобы старое
    # значение из engine/.env не включило его без ведома пользователя
    m["CINAI_IMAGE_POOL_ENABLED"] = "false"
    # функции, которых нет в BegoFrame, явно выключены
    # Референсов нет: движок не копирует их в проект (даже из inbox/ref/) и не берёт из ref/ проекта
    m["BEGO_REFS_DISABLED"] = "1"
    m["STYLE_EXTRACT_MODE"] = "off"
    m.update(cinai_video_env(v))
    return m


def cinai_video(v: dict) -> bool:
    """Видео делает CinAI (бесплатные модели), а не VeoNonStop / Vertex / RoyalTechno / VARAG."""
    return v.get("video_provider") == "cinai" and v.get("video_mode") != "skip"


def cinai_video_env(v: dict) -> dict:
    """Видео CinAI -> env. Перекрывает движок veononstop, заданный выше."""
    if not cinai_video(v):
        return {"CINAI_VIDEO_ENABLED": "false"}
    return {
        "CINAI_VIDEO_ENABLED": "true",
        "BEGO_VIDEO_ENGINE": "cinai",
        "PIPELINE_VIDEO_ENGINE": "cinai",
        "CINAI_VIDEO_MODEL": v.get("cinai_video_model") or catalog.CINAI_VIDEO_MODELS[0][0],
        "CINAI_VIDEO_MODE": "t2v" if v.get("video_mode") == "t2v" else "i2v",
        "CINAI_VIDEO_ASPECT_RATIO": v.get("veo_ratio") or "16:9",
        # общая длина клипа: ближайшая допустимая не короче самого длинного клипа.
        # С «Длительностью под сцену» у каждого клипа своя (CINAI_VIDEO_DIRECT_TIMING)
        "CINAI_VIDEO_DURATION": str(max(3, int(v.get("clip_max", 8)))),
        "CINAI_VIDEO_RESOLUTION": "",          # у бесплатных моделей разрешение не выбирается
        "CINAI_VIDEO_AUDIO": "",
        "CINAI_VIDEO_MAX_PARALLEL": str(_clamp(v.get("cinai_video_parallel", 3), 1, 6)),
    }


def own_video_provider(v: dict) -> str:
    """Свой клиент видео на маршруте VeoNonStop ('vertex', 'royaltechno', 'varag') или ''."""
    provider = v.get("video_provider")
    return provider if provider in catalog.OWN_VIDEO_PROVIDERS else ""


def fastgen_slot(selection: str) -> str:
    return {"main": "1", "backup": "2", "key3": "3", "key4": "4", "both": "all"}.get(selection or "main", "1")


def cheapvibe_env(v: dict) -> dict:
    """Настройки картинок CheapVibeCode (GPT Image 2 / Grok Imagine / Banana 2) -> env."""
    return {
        "CHEAPVIBE_IMAGE_MODEL": v["cheapvibe_model"],
        "CHEAPVIBE_IMAGE_SIZE": catalog.cheapvibe_size(v["veo_ratio"], v["cheapvibe_size"]),
        "CHEAPVIBE_IMAGE_QUALITY": catalog.cheapvibe_quality(v["cheapvibe_model"], v["cheapvibe_quality"]),
        "CHEAPVIBE_IMAGE_PARALLEL": str(_clamp(v["cheapvibe_parallel"], 1, 16)),
    }


def varag_env(v: dict) -> dict:
    """Потоки VARAG (картинки и видео) -> env. Модель и длину клипа VARAG не принимает."""
    return {
        "VARAG_IMAGE_PARALLEL": str(_clamp(v["varag_image_parallel"], 1, 16)),
        "VARAG_VIDEO_PARALLEL": str(_clamp(v["varag_video_parallel"], 1, 16)),
    }


def defaults_env(v: dict) -> dict:
    """Полный набор ключей для «Сохранить по умолчанию» (запись в engine/.env)."""
    u = {
        "PIPELINE_START_POINT": v["start_point"],
        "ASSEMBLYAI_LANGUAGE_CODE": v["language"],
        "DIRECTOR_TWO_PASS": _b(v["direction_mode"] == "2-pass"),
        "DIRECTOR_BACKEND": "gpt",
        "GPT_PROVIDER": "openai",
        "GPT_MODEL": v["gpt_model"],
        "GPT_PASS2_CHUNK_SIZE": str(v["gpt_chunk_size"] if v.get("pass2_chunk_enabled") else 0),
        "GPT_PASS2_PARALLEL": str(v["gpt_parallel"]),
        "PIPELINE_CLIP_MODE": v["clip_mode"],
        "PIPELINE_VISLIDE_INTERVAL": str(v["vislide_interval"]),
        "VIDEO_CLIP_MIN_DURATION": str(v["clip_min"]),
        "VIDEO_CLIP_MAX_DURATION": str(v["clip_max"]),
        # Vertex, CheapVibeCode и VARAG движок знает как «veononstop» + провайдер: так работают все
        # готовые маршруты
        "PIPELINE_IMG_GEN": "veononstop" if image_provider(v) else v["img_gen"],
        "BEGO_IMAGE_PROVIDER": image_provider(v),
        **cheapvibe_env(v),
        **varag_env(v),
        "BEGO_VIDEO_PROVIDER": own_video_provider(v),
        "ROYALTECHNO_VIDEO_PARALLEL": str(v["royaltechno_video_parallel"]),
        "VERTEX_IMAGE_MODEL": v["vertex_image_model"],
        "VERTEX_IMAGE_PARALLEL": str(v["vertex_image_parallel"]),
        "VERTEX_VIDEO_MODEL": catalog.VERTEX_VIDEO_MODEL,
        "VERTEX_VIDEO_RESOLUTION": v["vertex_video_resolution"],
        "VERTEX_VIDEO_PARALLEL": str(v["vertex_video_parallel"]),
        "FASTGEN_MODEL": v["fastgen_model"],
        "FASTGEN_ASPECT_RATIO": catalog.fastgen_ratio(v["veo_ratio"]),
        "FASTGEN_MAX_PARALLEL": str(v["fastgen_parallel"]),
        "FASTGEN_QUOTA_FALLBACK_VEONONSTOP": _on(v["fg_quota_veo_fallback"]),
        "VEONONSTOP_QUOTA_FALLBACK_FASTGEN": _on(v["veo_quota_fg_fallback"]),
        "VEONONSTOP_QUOTA_FALLBACK_T2V": _on(v["veo_quota_t2v_fallback"]),
        "VEONONSTOP_I2V_PARALLAX": _on(v["i2v_parallax"]),
        "VEONONSTOP_I2V_PARALLAX_METHOD": str(v["i2v_parallax_method"])[:1],
        "VEONONSTOP_I2V_PARALLAX_MIX_PERCLIP": _on(v["i2v_parallax_mix_perclip"]),
        "VEONONSTOP_I2V_PARALLAX_EVERY_N": str(max(1, int(v["i2v_parallax_every_n"]))),
        "VEONONSTOP_I2V_PARALLAX_AI": _on(v["i2v_parallax_ai"]),
        "VEONONSTOP_I2V_PARALLAX_STOCK": _on(v["i2v_parallax_stock"]),
        "VEONONSTOP_I2V_PARALLAX_REAL_PHOTO": "off",
        "CINAI_MODEL": v["cinai_model"],
        "CINAI_ASPECT_RATIO": v["veo_ratio"],
        "CINAI_QUALITY": v["cinai_quality"],
        "CINAI_MAX_PARALLEL": str(_clamp(v["cinai_parallel"], 1, 6)),
        "VEONONSTOP_IMGGEN_MODE": "banana",            # у VeoNonStop только Banana
        "VEONONSTOP_BANANA_MODEL": v["veo_banana_model"],
        "PIPELINE_VIDEO_MODE": v["video_mode"],
        "PIPELINE_VIDEO_ENGINE": "veononstop",
        "VEONONSTOP_ASPECT_RATIO": v["veo_ratio"],
        "VEONONSTOP_MAX_PARALLEL": str(v["veo_parallel"]),
        "VEONONSTOP_REQUEST_DELAY": str(v["veo_request_delay"]),
        "VEONONSTOP_RECAPTCHA_DELAY": str(v["veo_recaptcha_delay"]),
        "VEONONSTOP_RECAPTCHA_JITTER": str(v["veo_recaptcha_jitter"]),
        "VEONONSTOP_MAX_RETRIES": str(v["veo_max_retries"]),
        "VEONONSTOP_MAX_SILENT_CYCLES": str(v["veo_max_silent_cycles"]),
        "VEONONSTOP_FAST_CENSOR": v["veo_fast_censor"],
        # «Длительность под сцену»: VeoNonStop и Vertex заказывают клип длиной со сцену,
        # CinAI — тоже (у бесплатных моделей действует в t2v); RoyalTechno всегда 8 с,
        # VARAG длину не принимает
        "VEONONSTOP_CUSTOM_DURATION": _on(v["custom_duration"]),
        "CINAI_VIDEO_DIRECT_TIMING": "1" if v["custom_duration"] else "0",
        "BEGO_IMGGEN_KEY_SLOT": str(v["veo_imggen_key"]),
        "BEGO_VIDEO_KEY_SLOT": str(v["veo_video_key"]),
        "VEONONSTOP_IMGGEN_KEY_SLOT": str(v["veo_imggen_key"]),
        "VEONONSTOP_VIDEO_KEY_SLOT": str(v["veo_video_key"]),
        "PIPELINE_INFLIGHT_MODE": _b(v["inflight_mode"]),
        "PIPELINE_PROMPT_MODE": v["prompt_mode"],
        "PIPELINE_RENDER_MODE": v["render_mode"],
        "PIPELINE_WATERMARK": v["watermark"],
        "PIPELINE_KEEP_FILES": _b(v["keep_files"]),
        "PIPELINE_KEEP_VEO_SOUND": _b(v["keep_veo_sound"]),
        "REMIX_ENGINE": v["remix_engine"],
        "CINAI_REMIX_ASPECT_RATIO": v["veo_ratio"],
        "CINAI_REMIX_QUALITY": v["cinai_remix_quality"],
        "CINAI_REMIX_MODEL": v["cinai_remix_model"],
        "VEONONSTOP_IMAGE_UPSCALE": v["image_upscale"],
        "BEGO_IMGGEN_PARALLEL": str(v["veo_imggen_parallel"]),
        "VEONONSTOP_VIDEO_UPSCALE": _b(v["video_upscale"]),
        "INTRO_BLOCK_DURATION": str(v["intro_duration"]),
        "INTRO_CLIP_MIN_DURATION": str(v["intro_clip_min"]),
        "INTRO_CLIP_MAX_DURATION": str(v["intro_clip_max"]),
        "COVER_HOOK_SEC": str(v["cover_hook_sec"]),
        "COVER_HOOK_MODE": v["cover_hook_mode"],
        "REWRITE_MODEL": v["rewrite_model"],
        "VIDEO_FPS": str(v["concat_fps"]),
        "CONCAT_BITRATE": v["concat_bitrate"],
        "CONCAT_QUEUE_ENABLED": _b(v["concat_queue_enabled"]),
        "PIPELINE_TRANSITIONS": _b(v["transitions_enabled"]),
        "PIPELINE_TRANSITION_DURATION": str(v["transition_duration"]),
        "PIPELINE_TRANSITION_STYLES": transition_styles(v),
        "PIPELINE_ZOOM_STYLES": zoom_styles(v),
        "BEGO_TRANSITION_FIXED": v["transition_style_fixed"],
        "BEGO_ZOOM_FIXED": v["zoom_style_fixed"],
        "ZOOM_SUPERSAMPLE": str(v["zoom_supersample"]),
        "SUBTITLE_PHRASE_UNIT": v["subtitle_phrase_unit"],
        "SUBTITLE_PHRASE_LEN": str(v["subtitle_phrase_len"]),
        "BEGO_SUBTITLES_ENABLED": "1" if v["subtitles_enabled"] else "0",
        "BEGO_SUBTITLES_PRESET": v["subtitles_preset"],
        "BEGO_SUBTITLES_CUSTOM": "1" if v["subtitles_custom"] else "0",
        "BEGO_SUBTITLES_PATH": str(v.get("subtitles_path") or ""),
        "BEGO_SKIP_PREFLIGHT": "1" if v["skip_preflight"] else "0",
        "BEGO_CODEX_ADAPT": "1" if v["codex_adapt_var"] else "0",
        "BEGO_FASTGEN_KEY_SLOT": v["fastgen_api_key"],
        "BEGO_SG_MODE": v["sg_mode"],
        "BEGO_SG_ADAPT": "1" if v["sg_adapt"] else "0",
        "BEGO_LOGO_MODE": v["logo_mode"],
        "BEGO_VEO_ADV_VISIBLE": "1" if v["veo_advanced_visible"] else "0",
    }
    u.update(stage_env(v))
    return u


def post_action_for(v: dict) -> str:
    """Какой маршрут генерации запросить у движка (--post-action)."""
    ig = str(v.get("img_gen") or "skip")
    vm = str(v.get("video_mode") or "skip")
    action = "skip"
    if ig == "fastgen" and vm == "i2v" and v.get("video_provider") != "cinai":
        action = "fastgen_i2v"          # этот маршрут оживляет только через VeoNonStop
    elif ig == "fastgen" and vm == "i2v":
        action = "veo_i2v"
    elif ig in ("cinai", "veononstop", "vertex", "cheapvibe", "varag") and vm == "i2v":
        action = "veo_i2v"
    elif vm == "t2v":
        action = "t2v"
    # Без видео делаем КАРТИНКИ: слайдшоу или видеогенерация выключена
    if v.get("clip_mode") == "slideshow" or vm == "skip":
        action = {"fastgen": "fastgen", "veononstop": "imggen", "vertex": "imggen", "cheapvibe": "imggen",
                  "varag": "imggen", "cinai": "cinai"}.get(ig, "skip")
    return action


def concat_section(v: dict, logo_path: str = "") -> dict:
    """Слепок настроек склейки — читается очередью склейки с диска."""
    logo = ""
    if v.get("logo_mode") == "one_for_all" and v.get("watermark") == "logo" and logo_path and Path(logo_path).exists():
        logo = logo_path
    return {
        "render_mode": v["render_mode"],
        "bitrate": v["concat_bitrate"],
        "fps": int(v["concat_fps"]),
        "aspect": v["veo_ratio"],
        "keep_veo_sound": bool(v["keep_veo_sound"]),
        "keep_files": bool(v["keep_files"]),
        "infographic_static": bool(v["infographic_static_concat"]),
        "veo_audio_inline": False,
        "watermark": v["watermark"],
        "logo_path": logo,
        "subtitles_template": subtitles_template(v),
        "transitions": {
            "enabled": bool(v["transitions_enabled"]),
            "duration": float(v["transition_duration"]),
            "styles": transition_styles(v),
        },
        "zoom_styles": zoom_styles(v),
        "zoom_supersample": str(v.get("zoom_supersample") or 2),
    }


def concat_command(project: Path, section: dict, python: str) -> list[str]:
    """Команда склейки проекта по слепку настроек."""
    render = section.get("render_mode", "720p")
    cmd = [python, str(paths.ASSEMBLER), "montage_plan.json", "-o", f"{project.name}.mp4",
           "-b", str(section.get("bitrate", "20M")), "--fps", str(section.get("fps", 60)),
           "--aspect", str(section.get("aspect", "16:9"))]
    if render == "1080p":
        cmd += ["-r", "1080"]
    elif render == "4k":
        cmd += ["-r", "4k"]
    tr_ = section.get("transitions") or {}
    if tr_.get("enabled"):
        cmd += ["--transitions", "--transition-duration", str(tr_.get("duration", 0.5)),
                "--transition-styles", str(tr_.get("styles", "fade"))]
    zs = section.get("zoom_styles") or ""
    if zs and zs != "in-center":
        cmd += ["--zoom-styles", zs]
    cmd.append("--static-infographics" if section.get("infographic_static", True) else "--animate-infographics")
    logo = section.get("logo_path") or ""
    if logo and Path(logo).exists():
        cmd += ["--logo", logo]
    elif section.get("watermark", "logo") != "logo":
        cmd.append("--no-logo")          # «без лого»: logo.png прошлого прогона в проекте не накладывать
    if section.get("keep_veo_sound"):
        cmd.append("--keep-veo-sound")
    if section.get("veo_audio_inline"):
        cmd.append("--veo-audio-inline")
    subs = section.get("subtitles_template") or ""
    if subs:
        cmd += ["--subtitles", subs]
    if section.get("keep_files"):
        cmd.append("--no-cleanup")
    return cmd


@dataclass
class FileBinding:
    """Привязки одного файла очереди (стайлгайд, логотип, обложка, пресет)."""
    style: str = ""
    logo: str = ""
    cover: str = ""
    preset: str = ""
    adapt: bool | None = None


@dataclass
class RunPlan:
    phases: list
    env: dict
    post_action: str
    summary: list
    project_key: str


def common_env(v: dict, post_action: str, settings_source: str = "pipeline") -> dict:
    """Окружение движка из значений формы «Конвейер» (то, что не зависит от конкретного файла).

    Единая точка: её используют и очередь конвейера (`build_run`), и «Отдельные стадии»,
    и «Довести проект» — поэтому настройки везде одни и те же. Значение `None` у ключа
    означает «убрать переменную из окружения».
    """
    env: dict = {}
    ig = v["img_gen"]
    routed_img = image_provider(v)          # vertex / cheapvibe / varag / ''
    own_vid = own_video_provider(v)        # vertex / royaltechno / varag / ''
    vid_parallel = {"vertex": v["vertex_video_parallel"],
                    "royaltechno": v["royaltechno_video_parallel"],
                    "varag": v["varag_video_parallel"]}.get(own_vid, v["veo_parallel"])
    if routed_img:
        ig = "veononstop"          # движок: маршрут VeoNonStop + свой клиент (см. BEGO_IMAGE_PROVIDER)
    fg_sel = v.get("fastgen_api_key") or "main"
    env.update({
        "VEONONSTOP_MAX_PARALLEL": str(v["veo_parallel"]),
        "VEONONSTOP_ASPECT_RATIO": v["veo_ratio"],
        # «Длительность под сцену»: VeoNonStop и Vertex заказывают клип длиной со сцену,
        # CinAI — тоже (у бесплатных моделей действует в t2v); RoyalTechno всегда 8 с,
        # VARAG длину не принимает
        "VEONONSTOP_CUSTOM_DURATION": _on(v["custom_duration"]),
        "CINAI_VIDEO_DIRECT_TIMING": "1" if v["custom_duration"] else "0",
        "ZOOM_SUPERSAMPLE": str(v["zoom_supersample"]),
        "VEONONSTOP_REQUEST_DELAY": str(v["veo_request_delay"]),
        "VEONONSTOP_RECAPTCHA_DELAY": str(v["veo_recaptcha_delay"]),
        "VEONONSTOP_RECAPTCHA_JITTER": str(v["veo_recaptcha_jitter"]),
        "VEONONSTOP_MAX_RETRIES": str(v["veo_max_retries"]),
        "VEONONSTOP_MAX_SILENT_CYCLES": str(v["veo_max_silent_cycles"]),
        "VEONONSTOP_FAST_CENSOR": v["veo_fast_censor"],
        # апскейл — функция VeoNonStop по media_id, у своих клиентов его нет
        "VEONONSTOP_IMAGE_UPSCALE": "off" if routed_img else v["image_upscale"],
        "VEONONSTOP_VIDEO_UPSCALE": "true" if v["video_upscale"] and not own_vid else "",
        "BEGO_IMGGEN_KEY_SLOT": str(v["veo_imggen_key"]),
        "BEGO_VIDEO_KEY_SLOT": str(v["veo_video_key"]),
        "BEGO_IMGGEN_PARALLEL": str(v["veo_imggen_parallel"]),
        "BEGO_VIDEO_PARALLEL": str(vid_parallel),
        "BEGO_IMAGE_PROVIDER": routed_img,
        "BEGO_VIDEO_PROVIDER": own_vid,
        "ROYALTECHNO_VIDEO_PARALLEL": str(v["royaltechno_video_parallel"]),
        "VERTEX_IMAGE_MODEL": v["vertex_image_model"],
        "VERTEX_IMAGE_PARALLEL": str(v["vertex_image_parallel"]),
        "VERTEX_VIDEO_MODEL": catalog.VERTEX_VIDEO_MODEL,
        "VERTEX_VIDEO_RESOLUTION": v["vertex_video_resolution"],
        "VERTEX_VIDEO_PARALLEL": str(v["vertex_video_parallel"]),
        **cheapvibe_env(v),
        **varag_env(v),
        "BEGO_INFLIGHT_MODE": "1" if v["inflight_mode"] else "0",
        "BEGO_SKIP_PREFLIGHT": "1" if v["skip_preflight"] else "0",
        "PIPELINE_IMG_GEN": ig,
        # режим видео — из формы, а не из engine/.env: по нему движок решает, разрешена ли
        # нейрогенерация и нужна ли очередь видео (см. units/source_quota.py)
        "PIPELINE_VIDEO_MODE": v["video_mode"],
        "BEGO_PROMPT_TARGET": "t2v" if post_action == "t2v" else "image",
        "FASTGEN_QUOTA_FALLBACK_VEONONSTOP": _on(v["fg_quota_veo_fallback"]),
        "VEONONSTOP_QUOTA_FALLBACK_FASTGEN": _on(v["veo_quota_fg_fallback"]),
        "VEONONSTOP_QUOTA_FALLBACK_T2V": _on(v["veo_quota_t2v_fallback"]),
        "VEONONSTOP_I2V_PARALLAX": _on(v["i2v_parallax"]),
        "VEONONSTOP_I2V_PARALLAX_METHOD": str(v["i2v_parallax_method"])[:1],
        "VEONONSTOP_I2V_PARALLAX_MIX_PERCLIP": _on(v["i2v_parallax_mix_perclip"]),
        "VEONONSTOP_I2V_PARALLAX_EVERY_N": str(max(1, int(v["i2v_parallax_every_n"]))),
        "VEONONSTOP_I2V_PARALLAX_AI": _on(v["i2v_parallax_ai"]),
        "VEONONSTOP_I2V_PARALLAX_STOCK": _on(v["i2v_parallax_stock"]),
        "VEONONSTOP_I2V_PARALLAX_REAL_PHOTO": "off",
        "CINAI_MODEL": v["cinai_model"],
        "CINAI_ASPECT_RATIO": v["veo_ratio"],
        "CINAI_QUALITY": v["cinai_quality"],
        "CINAI_MAX_PARALLEL": str(_clamp(v["cinai_parallel"], 1, 6)),
        "CINAI_VIDEO_ENABLED": "0",
        "CINAI_VIDEO_MODE": "i2v",
        "BEGO_VIDEO_ENGINE": "veononstop",
        "FASTGEN_MODEL": v["fastgen_model"],
        "FASTGEN_ASPECT_RATIO": catalog.fastgen_ratio(v["veo_ratio"]),
        "FASTGEN_MAX_PARALLEL": str(v["fastgen_parallel"]),
        "BEGO_SETTINGS_SOURCE": settings_source,
        "VIDEO_CLIP_MIN_DURATION": str(v["clip_min"]),
        "VIDEO_CLIP_MAX_DURATION": str(v["clip_max"]),
        "ASSEMBLYAI_LANGUAGE_CODE": v["language"],
        "SUBTITLE_PHRASE_UNIT": v["subtitle_phrase_unit"],
        "SUBTITLE_PHRASE_LEN": str(v["subtitle_phrase_len"]),
        "DIRECTOR_BACKEND": "gpt",
        "GPT_PROVIDER": "openai",
        "GPT_MODEL": v["gpt_model"],
        "GPT_PASS2_CHUNK_SIZE": str(v["gpt_chunk_size"] if v.get("pass2_chunk_enabled") else 0),
        "GPT_PASS2_PARALLEL": str(v["gpt_parallel"]),
        "INTRO_BLOCK_DURATION": str(v["intro_duration"]),
        "INTRO_CLIP_MIN_DURATION": str(v["intro_clip_min"]),
        "INTRO_CLIP_MAX_DURATION": str(v["intro_clip_max"]),
        # оживление обложки: обложка уже лежит в проекте, оживить её может и «Довести проект»
        "COVER_HOOK_SEC": str(v["cover_hook_sec"]),
        "COVER_HOOK_MODE": v["cover_hook_mode"],
        "REMIX_ENGINE": v["remix_engine"],
        "CINAI_REMIX_ASPECT_RATIO": v["veo_ratio"],
        "CINAI_REMIX_QUALITY": v["cinai_remix_quality"],
        "CINAI_REMIX_MODEL": v["cinai_remix_model"],
        "REWRITE_MODEL": v["rewrite_model"],
    })
    if v["clip_mode"] == "vislide":
        env["BEGO_VISLIDE_INTERVAL"] = str(v["vislide_interval"])
    if ig == "fastgen":
        env["BEGO_IMGGEN_KEY_SLOT"] = fastgen_slot(fg_sel)
        env["BEGO_FASTGEN_KEY_SELECTION"] = fg_sel
        if fg_sel == "both":
            env["FASTGEN_DUAL_KEY"] = "1"
        else:
            env["FASTGEN_DUAL_KEY"] = None
    else:
        env["FASTGEN_DUAL_KEY"] = None
    if routed_img:
        # Vertex, CheapVibeCode и VARAG генерируют картинки только по тексту: референсы им не передаются
        env["VEONONSTOP_IMGGEN_MODE"] = "banana"
        for name in ("FASTGEN_QUOTA_FALLBACK_VEONONSTOP", "VEONONSTOP_QUOTA_FALLBACK_FASTGEN",
                     "VEONONSTOP_QUOTA_FALLBACK_T2V"):
            env[name] = _on(False)         # их квоту через API не узнать — запасных путей нет
    elif ig == "veononstop":
        env["VEONONSTOP_IMGGEN_MODE"] = "banana"
        env["VEONONSTOP_BANANA_MODEL"] = v["veo_banana_model"]
    if v["watermark"] == "logo":
        env["WATERMARK_MODE"] = "logo"
    if v["logo_mode"] != "individual" and v.get("logo_path") and Path(v["logo_path"]).exists():
        env["LOGO_PATH"] = str(v["logo_path"])
    if v["prompt_mode"] == "single" and v.get("single_prompt_path") and Path(v["single_prompt_path"]).exists():
        env["SINGLE_PROMPT_FILE"] = str(v["single_prompt_path"])
    env["BEGO_REF_STAGING"] = None          # референсов нет — старые значения из окружения убрать
    env["BEGO_REF_MAP"] = None
    env.update(stage_env(v))
    return env


# ---------------------------------------------- изменение настроек во время прогона
# Переменные, которые читает только режиссура: после готового плана их смена на файл не влияет
DIRECTION_ONLY_ENV = frozenset({
    "GPT_MODEL", "GPT_PROVIDER", "GPT_PASS2_CHUNK_SIZE", "GPT_PASS2_PARALLEL", "DIRECTOR_BACKEND",
    "ASSEMBLYAI_LANGUAGE_CODE", "INTRO_BLOCK_DURATION", "INTRO_CLIP_MIN_DURATION",
    "INTRO_CLIP_MAX_DURATION", "VIDEO_CLIP_MIN_DURATION", "VIDEO_CLIP_MAX_DURATION",
})
# Переменные, которые читает только склейка (она берёт их отдельно, см. `concat_inputs`)
CONCAT_ONLY_ENV = frozenset({"ZOOM_SUPERSAMPLE", "SUBTITLE_PHRASE_UNIT", "SUBTITLE_PHRASE_LEN",
                             "INFOGRAPHIC_STATIC_CONCAT", "BEGO_SETTINGS_SOURCE"})


def generation_inputs(v: dict) -> dict:
    """Всё, что получает маршрут генерации (окружение + маршрут, промпты, адаптация)."""
    env = common_env(v, post_action_for(v))
    inputs = {k: val for k, val in env.items() if k not in DIRECTION_ONLY_ENV and k not in CONCAT_ONLY_ENV}
    inputs.update({
        "~post_action": post_action_for(v), "~prompt_mode": v.get("prompt_mode"),
        "~codex_adapt": bool(v.get("codex_adapt_var")) and v.get("prompt_mode") == "perclip",
        "~single_prompt": str(v.get("single_prompt_path") or "") if v.get("prompt_mode") == "single" else "",
    })
    return inputs


def concat_inputs(v: dict) -> dict:
    """Всё, что получает склейка."""
    return {**concat_section(v, str(v.get("logo_path") or "")),
            **{k: str(v.get(k)) for k in ("zoom_supersample", "subtitle_phrase_unit", "subtitle_phrase_len")}}


def direction_inputs(v: dict) -> dict:
    """Всё, что получает режиссура."""
    env = common_env(v, post_action_for(v))
    return {**{k: env.get(k) for k in DIRECTION_ONLY_ENV},
            **{k: v.get(k) for k in ("direction_mode", "sg_mode", "sg_global_path", "sg_adapt", "clip_mode")}}


def live_change(old: dict, new: dict) -> tuple[list[str], bool, bool, bool]:
    """(изменённые ключи формы, изменилась ли генерация, склейка, режиссура).

    Ключи без последствий для прогона (раскрыть/свернуть блок и т.п.) дают три False.
    """
    keys = sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))
    if not keys:
        return [], False, False, False
    return (keys, generation_inputs(old) != generation_inputs(new), concat_inputs(old) != concat_inputs(new),
            direction_inputs(old) != direction_inputs(new))


def apply_env(target: dict, env: dict) -> dict:
    """Применить `common_env` к словарю окружения: None удаляет ключ."""
    for key, value in env.items():
        if value is None:
            target.pop(key, None)
        else:
            target[key] = value
    return target


def build_run(v: dict, source: Path, binding: FileBinding, python: str,
              settings_source: str) -> RunPlan:
    """Собрать фазы и окружение для одного файла очереди."""
    two_pass = v["direction_mode"] == "2-pass"
    post_action = post_action_for(v)
    inbox = paths.INBOX
    audio_key = source.name

    sg_path = str(v.get("sg_global_path") or "") if v.get("sg_mode") == "one_for_all" else binding.style
    process = [python, str(paths.CLI), "process", "--run",
               "--language", v["language"], "--clip-mode", v["clip_mode"],
               "--clip-min", str(v["clip_min"]), "--clip-max", str(v["clip_max"]),
               "--post-action", post_action, "--prompt-mode", v["prompt_mode"]]
    if v["clip_mode"] == "vislide":
        process += ["--vislide-interval", str(v["vislide_interval"])]
    if two_pass:
        process.append("--two-pass")
    if v.get("codex_adapt_var") and v["prompt_mode"] == "perclip":
        process.append("--codex-adapt")
    adapt = v.get("sg_adapt") if binding.adapt is None else binding.adapt
    if adapt:
        process.append("--adapt-sg")
    if v.get("sg_mode") == "one_for_all" and sg_path:
        process += ["--style", sg_path]
    elif v.get("sg_mode") == "individual" and sg_path:
        process += ["--style-map", json.dumps({audio_key: sg_path}, ensure_ascii=False)]
    process += ["--file-list", json.dumps([str(inbox / audio_key)], ensure_ascii=False),
                "--queue-dir", str(inbox)]

    phases = [process]

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["TQDM_DISABLE"] = "1"
    ig = "veononstop" if image_provider(v) else v["img_gen"]
    apply_env(env, common_env(v, post_action, settings_source))
    # зависящее от файла очереди
    if v["logo_mode"] == "individual" and binding.logo and Path(binding.logo).exists():
        env["BEGO_LOGO_MAP"] = json.dumps({audio_key: binding.logo}, ensure_ascii=False)
    if binding.cover and Path(binding.cover).exists():
        env["BEGO_COVER_MAP"] = json.dumps({audio_key: binding.cover}, ensure_ascii=False)

    # Слепок первого запуска для возобновления и для очереди склейки
    pipe_cfg = {
        "clip_mode": v["clip_mode"],
        "vislide_interval": int(v["vislide_interval"]) if v["clip_mode"] == "vislide" else 0,
        "intro_block_duration": int(v["intro_duration"]),
        "intro_clip_min": int(v["intro_clip_min"]),
        "intro_clip_max": int(v["intro_clip_max"]),
        "clip_min": int(v["clip_min"]),
        "clip_max": int(v["clip_max"]),
        "gpt_model": v["gpt_model"],
        "director_provider": "codex",
        "director_model": v["gpt_model"],
        "two_pass": bool(two_pass),
        "prompt_mode": v["prompt_mode"],
        "post_action": post_action,
        "img_gen": ig,
        "video_mode": v["video_mode"],
        "video_engine": "cinai" if cinai_video(v) else "veononstop",
        "i2v_parallax": bool(v["i2v_parallax"]),
        "i2v_parallax_method": str(v["i2v_parallax_method"])[:1],
        "i2v_parallax_mix_perclip": bool(v["i2v_parallax_mix_perclip"]),
        "i2v_parallax_every_n": int(v["i2v_parallax_every_n"]),
        "i2v_parallax_ai": bool(v["i2v_parallax_ai"]),
        "i2v_parallax_stock": bool(v["i2v_parallax_stock"]),
        "language": v["language"],
        "concat": concat_section(v, str(v.get("logo_path") or "")),
    }
    env["BEGO_PIPELINE_CONFIG_JSON"] = json.dumps(pipe_cfg, ensure_ascii=False)

    summary = [
        tr(T(f"Старт: аудио  |  Файл: {source.name}", f"Start: audio  |  File: {source.name}")),
        tr(T(f"Язык: {v['language']}  |  Режиссура: {v['direction_mode']} · Codex {v['gpt_model']}",
             f"Language: {v['language']}  |  Direction: {v['direction_mode']} · Codex {v['gpt_model']}")),
        tr(T(f"Клипы: {v['clip_mode']} {v['clip_min']}-{v['clip_max']} с  |  Интро: {v['intro_duration']} с "
             f"({v['intro_clip_min']}-{v['intro_clip_max']} с)",
             f"Clips: {v['clip_mode']} {v['clip_min']}-{v['clip_max']}s  |  Intro: {v['intro_duration']}s "
             f"({v['intro_clip_min']}-{v['intro_clip_max']}s)")),
        tr(T(f"Маршрут: {post_action}  |  Картинки: {ig}  |  Видео: {v['video_mode']} {v['veo_ratio']} x{v['veo_parallel']}",
             f"Route: {post_action}  |  Images: {ig}  |  Video: {v['video_mode']} {v['veo_ratio']} x{v['veo_parallel']}")),
        tr(T(f"Склейка: {v['render_mode']} {v['concat_bitrate']} {v['concat_fps']} fps  |  Настройки: {settings_source}",
             f"Render: {v['render_mode']} {v['concat_bitrate']} {v['concat_fps']} fps  |  Settings: {settings_source}")),
    ]
    if v.get("stock_enabled"):
        mix = source_mix(v)
        summary.append(tr(T("Сток", "Stock")) + f": {mix['mix_stock_video'] + mix['mix_stock_photo']}%"
                       f"  |  {env.get('STOCK_SOURCES', '')}")
    phases_env = [(list(ph), dict(env)) for ph in phases]
    return RunPlan(phases_env, env, post_action, summary, audio_key)
