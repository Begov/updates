"""Значения параметров конвейера по умолчанию.

Берутся из параметров движка (engine/params.py + engine/.env), поэтому форма
при запуске показывает ровно то, с чем движок работал бы без интерфейса.
Ключи совпадают с именами, которые понимают пресеты и сборщик запуска.
"""
from __future__ import annotations

from .. import catalog
from ..engine_link import cfg, env_flag, env_text


def _int(name, default):
    try:
        return int(float(cfg(name, default)))
    except (TypeError, ValueError):
        return int(default)


def _int_text(name, default):
    """Целое из окружения (.env) — для ключей, которых нет в params.py."""
    try:
        return int(float(env_text(name, "") or default))
    except (TypeError, ValueError):
        return int(default)


def _float(name, default):
    try:
        return float(cfg(name, default))
    except (TypeError, ValueError):
        return float(default)


def _bool(name, default):
    value = cfg(name, default)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _on(name, default):
    """Параметры вида on/off в params.py."""
    value = cfg(name, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("0", "off", "false", "no", "")


def _choice(value, allowed, fallback):
    return value if value in allowed else fallback


def defaults() -> dict:
    """Полный набор значений формы «Конвейер»."""
    d: dict = {}
    # ---------------------------------------------------------- старт
    d["start_point"] = "audio"          # единственная точка старта: готовая озвучка

    # ------------------------------------------- транскрипция и режиссура
    d["language"] = _choice(str(cfg("ASSEMBLYAI_LANGUAGE_CODE", "ru")), catalog.LANGUAGES, "ru")
    d["gpt_model"] = _choice(str(cfg("GPT_MODEL", "")), catalog.CODEX_MODELS, catalog.default_codex_model())
    d["direction_mode"] = "2-pass" if _bool("DIRECTOR_TWO_PASS", False) else "1-pass"
    chunk = _int("GPT_PASS2_CHUNK_SIZE", 80)
    d["pass2_chunk_enabled"] = chunk > 0
    d["gpt_chunk_size"] = max(50, chunk or 150)
    d["gpt_parallel"] = _int("GPT_PASS2_PARALLEL", 6)
    d["clip_mode"] = _choice(str(cfg("PIPELINE_CLIP_MODE", "video")), ("video", "slideshow", "vislide"), "video")
    d["vislide_interval"] = _int("PIPELINE_VISLIDE_INTERVAL", 3)
    d["clip_min"] = _int("VIDEO_CLIP_MIN_DURATION", 4)
    d["clip_max"] = _int("VIDEO_CLIP_MAX_DURATION", 8)

    # ---------------------------------------------------------- интро
    d["intro_duration"] = _int("INTRO_BLOCK_DURATION", 30)
    d["intro_clip_min"] = _int("INTRO_CLIP_MIN_DURATION", 3)
    d["intro_clip_max"] = _int("INTRO_CLIP_MAX_DURATION", 6)
    d["cover_hook_sec"] = _float("COVER_HOOK_SEC", 2.0) or 2.0
    d["cover_hook_mode"] = _choice(str(cfg("COVER_HOOK_MODE", "zoom_in")),
                                   ("zoom_in", "zoom_out", "drift", "fly_in", "awaken"), "zoom_in")

    # --------------------------------------------------------------- сток
    stock_sources = cfg("STOCK_SOURCES", ["local", "pexels", "pixabay"])
    if isinstance(stock_sources, str):
        stock_sources = [s.strip() for s in stock_sources.split(",") if s.strip()]
    d["stock_enabled"] = _bool("STOCK_ENABLED", False)
    d["stock_src_local"] = "local" in stock_sources
    d["stock_src_pexels"] = "pexels" in stock_sources
    d["stock_src_pixabay"] = "pixabay" in stock_sources
    d["stock_library_dir"] = str(cfg("STOCK_LIBRARY_DIR", "") or "")
    d["stock_parallel"] = _int("STOCK_PARALLEL", 12) or 12
    d["stock_tail_wait"] = _int("STOCK_PIPELINE_WAIT_TIMEOUT", 500)
    d["stock_vision_candidates"] = max(0, _int("STOCK_VISION_CANDIDATES", 3))
    d["stock_vision_per_source"] = max(0, _int("STOCK_VISION_PER_SOURCE", 2))
    d["stock_vision_provider"] = "codex"     # вижн стока — Codex (подписка GPT)
    d["stock_vision_model"] = _choice(str(cfg("STOCK_VISION_MODEL", "")), catalog.CODEX_MODELS,
                                      catalog.default_codex_model())
    d["stock_gate_sec"] = _int("STOCK_START_SEC", 0)
    d["stock_start_sec"] = _float("STOCK_CLIP_START_SEC", 0.0)
    d["stock_jitter"] = _bool("STOCK_CLIP_START_JITTER", False)
    d["stock_jitter_sec"] = _float("STOCK_CLIP_START_JITTER_SEC", 2.0) or 2.0
    d["stock_wait_timeout"] = max(1, _int("STOCK_WAIT_TIMEOUT", 1800) // 60)
    d["stock_prompt_provider"] = _choice(str(cfg("STOCK_PROMPT_PROVIDER", "") or "auto"),
                                         [p for p, _ in catalog.STOCK_PROMPT_PROVIDERS], "auto")
    d["stock_prompt_model"] = str(cfg("STOCK_PROMPT_MODEL", "") or "")
    d["stock_mute_audio"] = _bool("STOCK_MUTE_AUDIO", True)
    d["stock_exclude_ai"] = _bool("STOCK_EXCLUDE_AI", True)

    # ------------------------------------------------------- состав видео
    d.update(_mix_stored({"stock_ratio": _float("STOCK_RATIO_TARGET", 0.30) * 100,
                          "stock_video_share": _float("STOCK_VIDEO_SHARE", 0.70) * 100,
                          "infographic_target": _float("INFOGRAPHIC_RATIO_TARGET", 0.15) * 100}))

    d["mix_motion"] = int(round(_float("MOTION_GRAPHICS_RATIO_TARGET", 0.10) * 100))

    # --------------------------------------------------------- моушн-графика
    d["motion_enabled"] = _bool("MOTION_GRAPHICS_ENABLED", False)
    d["motion_model"] = _choice(str(cfg("MOTION_GRAPHICS_MODEL", "") or ""), catalog.CODEX_MODELS,
                                catalog.default_codex_model())
    d["motion_parallel"] = max(1, min(8, _int("MOTION_GRAPHICS_PARALLEL", 2)))
    d["motion_gate_sec"] = _int("MOTION_GRAPHICS_START_SEC", 0)

    # --------------------------------------------------------- инфографика
    d["infographic_enabled"] = _bool("INFOGRAPHIC_ENABLED", False)
    d["infographic_gate_sec"] = _int("INFOGRAPHIC_START_SEC", 0)
    d["infographic_provider"] = _choice(str(cfg("INFOGRAPHIC_IMG_PROVIDER", "fastgen")),
                                        [p for p, _ in catalog.IMAGE_PROVIDERS], "fastgen")
    d["infographic_vertex_model"] = _choice(str(cfg("INFOGRAPHIC_VERTEX_MODEL", catalog.VERTEX_IMAGE_DEFAULT)),
                                            [m for m, _ in catalog.VERTEX_IMAGE_MODELS], catalog.VERTEX_IMAGE_DEFAULT)
    d["infographic_cheapvibe_model"] = _choice(str(cfg("INFOGRAPHIC_CHEAPVIBE_MODEL", "gpt-image-2")),
                                               [m for m, _ in catalog.CHEAPVIBE_MODELS], "gpt-image-2")
    d["infographic_cheapvibe_size"] = _choice(str(cfg("INFOGRAPHIC_CHEAPVIBE_SIZE", "1536x1024")),
                                              [s for s, _ in catalog.CHEAPVIBE_SIZES], "1536x1024")
    d["infographic_cheapvibe_quality"] = _choice(str(cfg("INFOGRAPHIC_CHEAPVIBE_QUALITY", "medium")),
                                                 catalog.CHEAPVIBE_QUALITIES, "medium")
    d["infographic_fastgen_model"] = _choice(str(cfg("INFOGRAPHIC_FASTGEN_MODEL", "NARWHAL")),
                                             [m for m, _ in catalog.FASTGEN_MODELS], "NARWHAL")
    d["infographic_cinai_model"] = _choice(str(cfg("INFOGRAPHIC_CINAI_MODEL", cfg("CINAI_MODEL", ""))),
                                           [m for m, _ in catalog.CINAI_MODELS], catalog.CINAI_MODEL_DEFAULT)
    d["infographic_cinai_quality"] = "1K"     # у бесплатных моделей CinAI разрешение не выбирается
    d["infographic_veo_model"] = _choice(str(cfg("INFOGRAPHIC_VEONONSTOP_BANANA_MODEL", "GEM_PIX_2")),
                                         [m for m, _ in catalog.BANANA_MODELS], "GEM_PIX_2")

    # ------------------------------------------------------- картинки
    # Vertex, CheapVibeCode и VARAG движок знает как «veononstop» + BEGO_IMAGE_PROVIDER: так работают
    # все готовые маршруты картинок, поэтому в .env они сохраняются именно так.
    provider = env_text("BEGO_IMAGE_PROVIDER", "")
    ig = provider if provider in ROUTED_IMAGE_PROVIDERS else str(cfg("PIPELINE_IMG_GEN", "veononstop"))
    d["img_gen"] = _choice(ig, ("fastgen", "veononstop", "vertex", "cheapvibe", "varag", "cinai", "skip"),
                           "veononstop")
    d["varag_image_parallel"] = max(1, min(16, _int("VARAG_IMAGE_PARALLEL", 4)))
    d["cheapvibe_size"] = _choice(str(cfg("CHEAPVIBE_IMAGE_SIZE", "1536x1024")),
                                  [s for s, _ in catalog.CHEAPVIBE_SIZES], "1536x1024")
    d["cheapvibe_model"] = _choice(str(cfg("CHEAPVIBE_IMAGE_MODEL", "gpt-image-2")),
                                   [m for m, _ in catalog.CHEAPVIBE_MODELS], "gpt-image-2")
    d["cheapvibe_quality"] = _choice(str(cfg("CHEAPVIBE_IMAGE_QUALITY", "medium")),
                                     catalog.CHEAPVIBE_QUALITIES, "medium")
    d["cheapvibe_parallel"] = max(1, min(16, _int("CHEAPVIBE_IMAGE_PARALLEL", 2)))
    d["vertex_image_model"] = _choice(str(cfg("VERTEX_IMAGE_MODEL", catalog.VERTEX_IMAGE_DEFAULT)),
                                      [m for m, _ in catalog.VERTEX_IMAGE_MODELS], catalog.VERTEX_IMAGE_DEFAULT)
    d["vertex_image_parallel"] = max(1, _int("VERTEX_IMAGE_PARALLEL", 2))
    d["video_provider"] = _choice(env_text("BEGO_VIDEO_PROVIDER", ""), catalog.OWN_VIDEO_PROVIDERS, "veononstop")
    d["cinai_video_parallel"] = max(1, min(6, _int("CINAI_VIDEO_MAX_PARALLEL", 3)))
    d["cinai_video_model"] = _choice(str(cfg("CINAI_VIDEO_MODEL", "")), [m for m, _ in catalog.CINAI_VIDEO_MODELS],
                                     catalog.CINAI_VIDEO_MODELS[0][0])
    d["royaltechno_video_parallel"] = max(1, min(20, _int("ROYALTECHNO_VIDEO_PARALLEL", 4)))
    d["varag_video_parallel"] = max(1, min(16, _int("VARAG_VIDEO_PARALLEL", 4)))
    d["vertex_video_resolution"] = _choice(str(cfg("VERTEX_VIDEO_RESOLUTION", "")),
                                           [m for m, _ in catalog.VERTEX_RESOLUTIONS], "")
    d["vertex_video_parallel"] = max(1, _int("VERTEX_VIDEO_PARALLEL", 2))
    d["fg_quota_veo_fallback"] = _on("FASTGEN_QUOTA_FALLBACK_VEONONSTOP", True)
    d["veo_quota_fg_fallback"] = _on("VEONONSTOP_QUOTA_FALLBACK_FASTGEN", True)
    d["veo_quota_t2v_fallback"] = _on("VEONONSTOP_QUOTA_FALLBACK_T2V", False)
    d["fastgen_model"] = _choice(str(cfg("FASTGEN_MODEL", "FLOWER")), [m for m, _ in catalog.FASTGEN_MODELS], "FLOWER")
    d["fastgen_api_key"] = env_text("BEGO_FASTGEN_KEY_SLOT", "main") or "main"
    d["fastgen_parallel"] = _int("FASTGEN_MAX_PARALLEL", 4)
    d["veo_banana_model"] = _choice(str(cfg("VEONONSTOP_BANANA_MODEL", "GEM_PIX_2")),
                                    [m for m, _ in catalog.BANANA_MODELS], "GEM_PIX_2")
    d["veo_imggen_key"] = env_text("BEGO_IMGGEN_KEY_SLOT", "") or env_text("VEONONSTOP_IMGGEN_KEY_SLOT", "1") or "1"
    # своё число потоков картинок; VEONONSTOP_MAX_PARALLEL — потоки видео (старые .env знали только его)
    d["veo_imggen_parallel"] = _int_text("BEGO_IMGGEN_PARALLEL", _int("VEONONSTOP_MAX_PARALLEL", 4))
    # params.py приводит значение к нижнему регистру ('2k'), а кнопки — '2K'
    d["image_upscale"] = {"2k": "2K", "4k": "4K"}.get(str(cfg("VEONONSTOP_IMAGE_UPSCALE", "off")).strip().lower(), "off")
    d["cinai_model"] = _choice(str(cfg("CINAI_MODEL", "")), [m for m, _ in catalog.CINAI_MODELS],
                               catalog.CINAI_MODEL_DEFAULT)
    d["cinai_parallel"] = max(1, min(6, _int("CINAI_MAX_PARALLEL", 6)))
    d["cinai_quality"] = "1K"

    # --------------------------------------------------------- ремиксер
    d["remix_engine"] = _choice(str(cfg("REMIX_ENGINE", "grok")), [e for e, _ in catalog.REMIX_ENGINES], "grok")
    d["cinai_remix_quality"] = "1K"
    d["cinai_remix_model"] = _choice(str(cfg("CINAI_REMIX_MODEL", "")), [m for m, _ in catalog.CINAI_REMIX_MODELS],
                                     catalog.CINAI_REMIX_MODELS[0][0])

    # ------------------------------------------------------------ видео
    mode = str(cfg("PIPELINE_VIDEO_MODE", "i2v"))
    d["video_mode"] = _choice(mode, VIDEO_MODES, "i2v")
    d["video_engine"] = "veononstop"
    d["veo_ratio"] = _choice(str(cfg("VEONONSTOP_ASPECT_RATIO", "16:9")), catalog.FRAME_FORMATS, "16:9")
    d["veo_video_key"] = env_text("BEGO_VIDEO_KEY_SLOT", "") or env_text("VEONONSTOP_VIDEO_KEY_SLOT", "1") or "1"
    d["custom_duration"] = _on("VEONONSTOP_CUSTOM_DURATION", True)
    d["inflight_mode"] = env_flag("PIPELINE_INFLIGHT_MODE", False)
    d["skip_preflight"] = env_flag("BEGO_SKIP_PREFLIGHT", False)
    d["i2v_parallax"] = _on("VEONONSTOP_I2V_PARALLAX", False)
    d["i2v_parallax_method"] = _choice(str(cfg("VEONONSTOP_I2V_PARALLAX_METHOD", 1))[:1], ("0", "1", "2", "3"), "1")
    d["i2v_parallax_mix_perclip"] = _on("VEONONSTOP_I2V_PARALLAX_MIX_PERCLIP", False)
    d["i2v_parallax_ai"] = _on("VEONONSTOP_I2V_PARALLAX_AI", True)
    d["i2v_parallax_stock"] = _on("VEONONSTOP_I2V_PARALLAX_STOCK", False)
    d["i2v_parallax_every_n"] = max(1, _int("VEONONSTOP_I2V_PARALLAX_EVERY_N", 2))
    d["video_live_animate"] = env_flag("BEGO_LIVE_ANIMATE", _bool("VIDEO_LIVE_ANIMATE", False))
    d["video_upscale"] = _bool("VEONONSTOP_VIDEO_UPSCALE", False)
    d["veo_parallel"] = _int("VEONONSTOP_MAX_PARALLEL", 12)
    d["veo_request_delay"] = _float("VEONONSTOP_REQUEST_DELAY", 10.0)
    d["veo_advanced_visible"] = env_flag("BEGO_VEO_ADV_VISIBLE", False)
    d["veo_recaptcha_delay"] = _float("VEONONSTOP_RECAPTCHA_DELAY", 8.0)
    d["veo_recaptcha_jitter"] = _float("VEONONSTOP_RECAPTCHA_JITTER", 3.0)
    d["veo_max_retries"] = _int("VEONONSTOP_MAX_RETRIES", 3)
    d["veo_max_silent_cycles"] = _int("VEONONSTOP_MAX_SILENT_CYCLES", 3)
    d["veo_fast_censor"] = _choice(str(cfg("VEONONSTOP_FAST_CENSOR", "off")),
                                   ("off", "after_rewrites", "ultrafast"), "off")
    d["prompt_mode"] = _choice(str(cfg("PIPELINE_PROMPT_MODE", "perclip")), ("single", "perclip"), "perclip")
    d["single_prompt_path"] = ""
    d["codex_adapt_var"] = env_flag("BEGO_CODEX_ADAPT", False)
    spec = str(cfg("REWRITE_MODEL", ""))
    d["rewrite_model"] = spec if spec in catalog.REWRITE_SPECS else catalog.default_rewrite_spec()

    # ------------------------------------------------------ склейка
    d["render_mode"] = _choice(str(cfg("PIPELINE_RENDER_MODE", "720p")), ("720p", "1080p", "4k"), "720p")
    # CONCAT_BITRATE и VIDEO_FPS движок из .env не читает (склейка получает их флагами) — берём из окружения
    d["concat_bitrate"] = env_text("CONCAT_BITRATE", "") or "20M"
    d["concat_fps"] = _int_text("VIDEO_FPS", 60)
    d["concat_queue_enabled"] = _bool("CONCAT_QUEUE_ENABLED", True)
    d["infographic_static_concat"] = _bool("INFOGRAPHIC_STATIC_CONCAT", True)
    d["transitions_enabled"] = _bool("PIPELINE_TRANSITIONS", False)
    d["transition_duration"] = _float("PIPELINE_TRANSITION_DURATION", 0.5)
    tstyles = str(cfg("PIPELINE_TRANSITION_STYLES", "fade") or "fade")
    d["transition_mode"] = "random" if len([s for s in tstyles.split(",") if s.strip()]) >= 2 else "fixed"
    # выбранный фиксированный стиль помним и при режиме «случайно» (BEGO_TRANSITION_FIXED)
    d["transition_style_fixed"] = _choice(env_text("BEGO_TRANSITION_FIXED", "") or tstyles.split(",")[0].strip(),
                                          catalog.TRANSITION_STYLES, "fade")
    zstyles = str(cfg("PIPELINE_ZOOM_STYLES", "in-center") or "in-center")
    d["zoom_mode"] = "random" if len([s for s in zstyles.split(",") if s.strip()]) >= 2 else "fixed"
    d["zoom_style_fixed"] = _choice(env_text("BEGO_ZOOM_FIXED", "") or zstyles.split(",")[0].strip(),
                                    catalog.ZOOM_STYLES, "in-center")
    d["zoom_supersample"] = _choice(str(cfg("ZOOM_SUPERSAMPLE", 2) or 2), ("1", "2", "3"), "2")
    d["watermark"] = "logo" if str(cfg("PIPELINE_WATERMARK", "as_is")) == "logo" else "as_is"
    d["logo_mode"] = _choice(env_text("BEGO_LOGO_MODE", "one_for_all") or "one_for_all",
                             ("one_for_all", "individual"), "one_for_all")
    d["logo_path"] = ""
    d["keep_files"] = _bool("PIPELINE_KEEP_FILES", False)
    d["keep_veo_sound"] = _bool("PIPELINE_KEEP_VEO_SOUND", False)
    d["subtitles_enabled"] = env_flag("BEGO_SUBTITLES_ENABLED", False)
    d["subtitles_preset"] = _choice(env_text("BEGO_SUBTITLES_PRESET", "bottom") or "bottom",
                                    ("bottom", "center"), "bottom")
    d["subtitles_custom"] = env_flag("BEGO_SUBTITLES_CUSTOM", False)
    d["subtitles_path"] = env_text("BEGO_SUBTITLES_PATH", "")
    d["subtitle_phrase_unit"] = "words" if str(cfg("SUBTITLE_PHRASE_UNIT", "chars")) == "words" else "chars"
    d["subtitle_phrase_len"] = _int("SUBTITLE_PHRASE_LEN", 50) or 50

    # -------------------------------------------------- стайлгайд и очередь
    d["sg_mode"] = _choice(env_text("BEGO_SG_MODE", "one_for_all") or "one_for_all",
                           ("one_for_all", "individual"), "one_for_all")
    d["sg_global_path"] = ""
    d["sg_adapt"] = env_flag("BEGO_SG_ADAPT", False)
    return sync_frame_format(sync_cheapvibe_quality(sync_video_skip(d)))


# Формат кадра один на весь ролик (veo_ratio: 16:9 или 9:16); у CheapVibeCode формат задаётся размером.
CHEAPVIBE_SIZE_KEYS = ("cheapvibe_size", "infographic_cheapvibe_size")
# прежние отдельные форматы CinAI (картинки, инфографика, ремиксер) — теперь общий формат
_OLD_FORMAT_KEYS = ("cinai_ratio", "infographic_cinai_ratio", "cinai_remix_ratio")
# убранный сюжетный режим (batch / morph / chain) и его настройки
_OLD_STORY_KEYS = ("story_mode", "chain_trim_frames", "chain_frame_offset", "chain_mute_audio",
                   "chain_imggen_heads_only", "chain_max_chain", "chain_cascade_recovery", "chain_max_fails")


def sync_frame_format(values: dict) -> dict:
    """Формат кадра — только из доступных (убранный 1:1 -> 16:9); размеры CheapVibeCode —
    только подходящие формату (на месте)."""
    if "veo_ratio" in values and values["veo_ratio"] not in catalog.FRAME_FORMATS:
        values["veo_ratio"] = "16:9"
    fmt = values.get("veo_ratio") or "16:9"
    for key in CHEAPVIBE_SIZE_KEYS:
        if key in values:
            values[key] = catalog.cheapvibe_size(fmt, values[key])
    return values


# Режимы видео. «components» (кадр из референсов) убран вместе с референсами.
VIDEO_MODES = ("i2v", "t2v", "skip")


# Пары «модель CheapVibeCode → её качество» в блоках «Генерация картинок» и «Инфографика»
CHEAPVIBE_QUALITY_PAIRS = (("cheapvibe_model", "cheapvibe_quality"),
                           ("infographic_cheapvibe_model", "infographic_cheapvibe_quality"))


def sync_cheapvibe_quality(values: dict) -> dict:
    """Качество CheapVibeCode — только из доступных выбранной модели (у Banana 2 — low)."""
    for model_key, quality_key in CHEAPVIBE_QUALITY_PAIRS:
        if model_key in values and quality_key in values:
            values[quality_key] = catalog.cheapvibe_quality(values[model_key], values[quality_key])
    return values


def sync_video_skip(values: dict) -> dict:
    """«Пропустить» видео живёт кнопкой среди провайдеров, а движку по-прежнему уходит
    режим `skip`. Держим оба значения согласованными (на месте): пропуск в любом из них
    означает пропуск в обоих."""
    if values.get("video_mode") == "skip" or values.get("video_provider") == "skip":
        if "video_mode" in values:
            values["video_mode"] = "skip"
        if "video_provider" in values:
            values["video_provider"] = "skip"
    return values


# Провайдеры картинок со своим клиентом, но движком VeoNonStop: интерфейс ставит
# PIPELINE_IMG_GEN=veononstop и BEGO_IMAGE_PROVIDER=<провайдер>, клиент подменяет стадия.
ROUTED_IMAGE_PROVIDERS = ("vertex", "cheapvibe", "varag")


def image_provider(v: dict) -> str:
    """Провайдер картинок поверх маршрута VeoNonStop ('vertex', 'cheapvibe', 'varag') или ''."""
    ig = v.get("img_gen")
    return ig if ig in ROUTED_IMAGE_PROVIDERS else ""


# ------------------------------------------------------- состав видео
# Доли источников задаются в процентах от всех клипов; остаток — нейрогенерация.
MIX_KEYS = ("mix_stock_video", "mix_stock_photo", "mix_infographic", "mix_motion")
_LEGACY_MIX = ("stock_ratio", "stock_video_share", "infographic_target")


def _pct(value, default: int) -> int:
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return default


def _mix_stored(v: dict) -> dict:
    """Сохранённые доли. Старые пресеты и сессии хранили «долю стока + из них видео» и
    «цель/потолок инфографики» — пересчитываем их в проценты от всех клипов. Доли,
    которых в сохранённом наборе ещё не было (моушн-графика), берут значение по умолчанию."""
    if any(k in v for k in MIX_KEYS) or not any(k in v for k in _LEGACY_MIX):
        out = {"mix_stock_video": _pct(v.get("mix_stock_video", 21), 21),
               "mix_stock_photo": _pct(v.get("mix_stock_photo", 9), 9),
               "mix_infographic": _pct(v.get("mix_infographic", 15), 15)}
    else:
        stock = _pct(v.get("stock_ratio", 30), 30)
        video = round(stock * _pct(v.get("stock_video_share", 70), 70) / 100)
        out = {"mix_stock_video": video, "mix_stock_photo": stock - video,
               "mix_infographic": _pct(v.get("infographic_target", 15), 15)}
    out["mix_motion"] = _pct(v.get("mix_motion", 10), 10)
    return out


def mix_active(v: dict) -> tuple[str, ...]:
    """Какие доли сейчас участвуют: сток (видео + фото), инфографика, моушн-графика."""
    keys: tuple[str, ...] = ()
    if v.get("stock_enabled"):
        keys += ("mix_stock_video", "mix_stock_photo")
    if v.get("infographic_enabled"):
        keys += ("mix_infographic",)
    if v.get("motion_enabled"):
        keys += ("mix_motion",)
    return keys


def source_mix(v: dict) -> dict:
    """Действующие доли включённых источников (в сумме не больше 100) и остаток «ai».

    Выключенный источник получает 0, но его сохранённое значение не теряется: включите
    его снова — доля вернётся (урезанная, если остальным уже отдано больше).
    """
    stored = _mix_stored(v)
    out, left = {k: 0 for k in MIX_KEYS}, 100
    for key in mix_active(v):
        out[key] = min(stored[key], left)
        left -= out[key]
    out["ai"] = left
    return out


def upgrade_values(values: dict) -> dict:
    """Привести значения из старых пресетов и сессий к текущей форме (на месте)."""
    if any(k in values for k in _LEGACY_MIX + MIX_KEYS):
        values.update(_mix_stored(values))
    for key in _LEGACY_MIX + ("infographic_max",):
        values.pop(key, None)
    # Референсов в программе больше нет; режим видео из компонентов работает как i2v.
    # Смешанный режим «интро через i2v, остальное через t2v» убран — тоже i2v.
    if values.get("video_mode") in ("components", "intro_i2v_t2v"):
        values["video_mode"] = "i2v"
    # убранный переключатель «Интро через i2v, остальное через t2v»
    values.pop("intro_i2v_t2v", None)
    # Gemini и KIE.ai убраны: вижн стока — Codex, ремиксер KieAI -> FastGen Grok,
    # переписывание промптов моделью Gemini -> модель Codex по умолчанию
    if values.get("stock_vision_provider") not in (None, *(p for p, _ in catalog.VISION_PROVIDERS)):
        values["stock_vision_provider"] = "codex"
        values.pop("stock_vision_model", None)
    if values.get("remix_engine") == "kieai":
        values["remix_engine"] = "grok"
    if str(values.get("rewrite_model", "")).startswith("gemini:"):
        values.pop("rewrite_model", None)
    # CinAI: только бесплатные модели картинок; платные/квотные -> значение по умолчанию.
    # Разрешение у бесплатных моделей не выбирается — всегда 1K.
    if values.get("remix_engine") in ("cinai_grok", "cinai_gpt"):
        values["remix_engine"] = "cinai"
    cinai_free = {m for m, _ in catalog.CINAI_MODELS}
    for key in ("cinai_model", "infographic_cinai_model"):
        if key in values and values[key] not in cinai_free:
            values.pop(key)
    if "cinai_remix_model" in values and values["cinai_remix_model"] not in {m for m, _ in catalog.CINAI_REMIX_MODELS}:
        values.pop("cinai_remix_model")
    # из двух «Veo 3.1 Fast» у CinAI оставлен вариант v2
    if values.get("cinai_video_model") == "veo-3-1-fast-v3":
        values["cinai_video_model"] = "veo-3-1-fast-v2"
    if "cinai_video_model" in values and values["cinai_video_model"] not in {m for m, _ in catalog.CINAI_VIDEO_MODELS}:
        values.pop("cinai_video_model")
    for key in ("cinai_quality", "infographic_cinai_quality", "cinai_remix_quality"):
        if key in values:
            values[key] = "1K"
    # убранные настройки: референсы, «+ потоки CinAI», режим VeoNonStop (остался только Banana)
    for key in ("ref_mode", "ref_images", "cinai_image_pool", "veo_imggen_mode", "infographic_veo_mode"):
        values.pop(key, None)
    # у Vertex только Banana Pro / 2 / 2 Lite: preview и 2.5 -> актуальные, остальное (Imagen) -> 2 Lite
    vertex_models = [m for m, _ in catalog.VERTEX_IMAGE_MODELS]
    for key in ("vertex_image_model", "infographic_vertex_model"):
        if key in values and values[key] not in vertex_models:
            values[key] = catalog.VERTEX_IMAGE_RENAMED.get(values[key], catalog.VERTEX_IMAGE_DEFAULT)
    # GEM_PIX у FastGen — та же Banana 2, что и NARWHAL
    for key in ("fastgen_model", "infographic_fastgen_model"):
        if values.get(key) == "GEM_PIX":
            values[key] = "NARWHAL"
    for key in _OLD_FORMAT_KEYS + _OLD_STORY_KEYS:
        values.pop(key, None)
    return sync_frame_format(sync_cheapvibe_quality(sync_video_skip(values)))


# Ключи, которые не относятся к настройкам генерации и не попадают в пресет
NON_PRESET_KEYS = {"single_prompt_path", "logo_path", "sg_global_path"}
