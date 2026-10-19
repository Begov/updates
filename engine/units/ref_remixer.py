"""
ref_remixer.py — ремиксер кадров, отклонённых модерацией.

Маршрутизация по config.REMIX_ENGINE:
  - cinai — CinAI, бесплатная (безлимитная на тарифе) модель из CINAI_REMIX_MODEL;
  - off — ремикс выключен.

Публичный API: remix_ref() — ремикс референса, remix_i2v() — ремикс кадра для i2v.
"""

import logging
from pathlib import Path

import params as config

# ──────────────────────────────────────────────────────────────────────────────
# Дефолтные промпты (если файл не найден)
# ──────────────────────────────────────────────────────────────────────────────
_DEFAULT_REF_REMIX = (
    "Remake this photo in graphic novel / comic book art style. "
    "Preserve the exact facial features, face structure and likeness precisely. "
    "Change only the art rendering style, not the face or identity."
)
_DEFAULT_I2V_REMIX = (
    "Same scene from a different camera angle. "
    "Use medium shot or wide shot. Avoid direct face close-up. "
    "Keep same lighting, mood and composition."
)


# ──────────────────────────────────────────────────────────────────────────────
# Вспомогательные функции
# ──────────────────────────────────────────────────────────────────────────────

def _load_prompt(filename: str, default: str) -> str:
    """Читает промпт из instructions/{filename}. Fallback — default."""
    p = Path(__file__).parent.parent / 'instructions' / filename
    try:
        return p.read_text('utf-8').strip() if p.exists() else default
    except Exception:
        return default


def _save_remix_log(log_path: str, image_name: str, flags: dict,
                    prompt: str, output_path: str, error: str = None) -> None:
    """Дописывает запись о ремиксе в remix_debug.log проекта."""
    try:
        import datetime
        ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(f"\n{'=' * 60}\n")
            f.write(f"[{ts}] {image_name}\n")
            if flags:
                for k, v in flags.items():
                    f.write(f"{k}: {'YES' if v else 'NO'}\n")
            else:
                f.write("flags: none\n")
            f.write(f"Prompt: {prompt}\n")
            if error:
                f.write(f"Result: FAIL — {error}\n")
            else:
                f.write(f"Result: OK -> {Path(output_path).name}\n")
    except Exception as _e:
        logging.debug(f"remix log write failed: {_e}")


# ──────────────────────────────────────────────────────────────────────────────
# Routing API — маршрутизация по config.REMIX_ENGINE
# ──────────────────────────────────────────────────────────────────────────────

def _safe_err(e: Exception, max_len: int = 300) -> str:
    """Обрезает HTML/JS мусор из сообщения исключения перед логированием."""
    s = str(e)
    if '<!DOCTYPE' in s or '<html' in s.lower():
        s = s[:200].split('<')[0].strip() or 'server returned HTML'
        s += ' (HTML truncated)'
    return s[:max_len]


def remix_enabled() -> bool:
    """Whether fallback stages are allowed to invoke an image remixer."""
    return str(getattr(config, 'REMIX_ENGINE', 'cinai')).lower() != 'off'


def _cinai_remix(image_path: str, output_path: str, prompt: str, model: str) -> str:
    # remix_image сам выбирает транспорт (workflow по умолчанию, как у картинок)
    from units.cinai_stills import remix_image
    return remix_image(
        image_path=image_path,
        prompt=prompt,
        output_path=output_path,
        model=model,
        aspect_ratio=getattr(config, 'CINAI_REMIX_ASPECT_RATIO', None),
        quality=getattr(config, 'CINAI_REMIX_QUALITY', None),
    )


def remix_ref(image_path: str, output_path: str) -> str:
    """Ремикс референса через CinAI (REMIX_ENGINE=cinai)."""
    if not remix_enabled():
        raise RuntimeError("Image remix disabled by REMIX_ENGINE=off")
    prompt = _load_prompt('ref_remix_prompt.txt', _DEFAULT_REF_REMIX)
    return _cinai_remix(image_path, output_path, prompt, config.CINAI_REMIX_MODEL)


def remix_i2v(image_path: str, output_path: str, log_path: str = None, hint: str = None) -> str:
    """
    Ремикс кадра для i2v через CinAI (REMIX_ENGINE=cinai).

    Args:
        hint: targeted remix instruction derived from Google error reason,
              appended to the remix prompt when provided.
    """
    if not remix_enabled():
        raise RuntimeError("Image remix disabled by REMIX_ENGINE=off")
    prompt = _load_prompt('i2v_remix_prompt.txt', _DEFAULT_I2V_REMIX)
    flags = {}
    if hint:
        prompt, flags = prompt + ' ' + hint, {'google_hint': True}
    try:
        result = _cinai_remix(image_path, output_path, prompt, config.CINAI_REMIX_MODEL)
    except Exception as exc:
        if log_path:
            _save_remix_log(log_path, Path(image_path).name, flags, prompt, output_path, error=_safe_err(exc))
        raise
    if log_path:
        _save_remix_log(log_path, Path(image_path).name, flags, prompt, result)
    return result
