"""
ref_remixer.py — ремиксер кадров, отклонённых модерацией.

Маршрутизация по config.REMIX_ENGINE:
  - grok / flower / openai — FastGen (Grok edit, Flower img2img, GPT Image edit);
  - cinai — CinAI, бесплатная (безлимитная на тарифе) модель из CINAI_REMIX_MODEL;
  - off — ремикс выключен.

Публичный API: remix_ref() — ремикс референса, remix_i2v() — ремикс кадра для i2v.
"""

import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# Авто-установка Pillow (нужен FastGen для resize/сжатия рефов)
try:
    from PIL import Image as _pil_check  # noqa: F401
except ImportError:
    subprocess.run([sys.executable, '-m', 'pip', 'install', 'pillow', '-q'], check=False)

import params as config

# ──────────────────────────────────────────────────────────────────────────────
# Консольные цвета
# ──────────────────────────────────────────────────────────────────────────────
_YELLOW = '\033[33m'
_RED    = '\033[31m'
_GREEN  = '\033[32m'
_RESET  = '\033[0m'

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
    return str(getattr(config, 'REMIX_ENGINE', 'grok')).lower() != 'off'


def _grok_ref_remix_with_retry(image_path: str, output_path: str, max_attempts: int = 3,
                                 api_key: Optional[str] = None, key_label: str = 'Key1') -> str:
    """Grok ref remix with retries on a specific API key. Raises on total failure."""
    from fastgen_engine import FastGenClient, _flowultra_shutdown_event
    fc = FastGenClient(api_key=api_key) if api_key else FastGenClient()
    last_err = None
    for attempt in range(1, max_attempts + 1):
        if _flowultra_shutdown_event.is_set():
            raise InterruptedError(f"Grok REF REMIX [{key_label}] aborted by shutdown")
        try:
            print(f"  {_YELLOW}[GROK REF REMIX {key_label}]{_RESET} {Path(image_path).name} (attempt {attempt}/{max_attempts})")
            result = fc.remix_ref_grok(image_path, output_path)
            logging.info(f"Grok REF REMIX [{key_label}]: {Path(image_path).name} -> {output_path} OK")
            return result
        except Exception as e:
            last_err = e
            err_str = str(e)
            # "cancelled by user" из cancel_all_tasks — не ретраим
            if 'cancelled by user' in err_str.lower() or _flowultra_shutdown_event.is_set():
                raise InterruptedError(f"Grok REF REMIX [{key_label}] cancelled by user")
            if '403' in err_str and ('DOCTYPE' in err_str or 'html' in err_str.lower()):
                raise  # Cloudflare down — caller handles fallback
            if attempt < max_attempts:
                wait = 5 * attempt
                logging.warning(f"Grok REF REMIX [{key_label}] attempt {attempt}/{max_attempts} failed: {_safe_err(e)}. Retry in {wait}s...")
                print(f"  {_YELLOW}[GROK REF REMIX {key_label}]{_RESET} attempt {attempt}/{max_attempts} failed: {_safe_err(e)}. Retry in {wait}s...")
                # Interruptible sleep
                for _ in range(wait):
                    if _flowultra_shutdown_event.is_set():
                        raise InterruptedError(f"Grok REF REMIX [{key_label}] aborted during retry wait")
                    time.sleep(1)
    raise RuntimeError(f"Grok REF REMIX [{key_label}] failed after {max_attempts} attempts: {_safe_err(last_err)}")


def _grok_ref_remix_key_chain(image_path: str, output_path: str) -> str:
    """
    Grok ref remix fallback chain:
      Key1 (3 attempts) → pause 10s → Key2 (3 attempts) → pause 30s → Key1 (3 attempts) → raise.

    Если Key2 не сконфигурирован — fallback сразу падает с исключением Key1.
    Caller (remix_ref) дальше ловит и переключается на Flower.
    """
    from fastgen_engine import _flowultra_shutdown_event
    key1 = config.FASTGEN_API_KEY
    key2 = getattr(config, 'FASTGEN_API_KEY_2', '')

    def _interruptible_sleep(secs: int, tag: str):
        for _ in range(secs):
            if _flowultra_shutdown_event.is_set():
                raise InterruptedError(f"Grok chain aborted during {tag} pause")
            time.sleep(1)

    # 2 фазы: Key1 → 1s → Key2 (Key1-retry убран — если оба упали, идём в Flower fallback)
    # Phase 1: Key1
    if _flowultra_shutdown_event.is_set():
        raise InterruptedError("Grok chain aborted before Key1")
    try:
        return _grok_ref_remix_with_retry(image_path, output_path, max_attempts=1, api_key=key1, key_label='Key1')
    except InterruptedError:
        raise
    except Exception as e1:
        # e1 живёт только внутри except-блока (Python его удаляет на выходе) —
        # сохраняем текст, иначе строка ниже падала NameError вместо реальной ошибки
        _e1_msg = _safe_err(e1)
        logging.warning(f"Grok chain phase 1 (Key1) failed: {_e1_msg}")

    if not key2 or key2 == key1:
        raise RuntimeError(f"Grok Key1 failed and no Key2 configured: {_e1_msg}")

    # Phase 2: 1s pause → Key2
    print(f"  {_YELLOW}[GROK CHAIN]{_RESET} Key1 failed → pause 1s → Key2...")
    logging.info("Grok chain: phase 1 failed, waiting 1s before Key2")
    _interruptible_sleep(1, '1s-before-Key2')
    try:
        return _grok_ref_remix_with_retry(image_path, output_path, max_attempts=1, api_key=key2, key_label='Key2')
    except InterruptedError:
        raise
    except Exception as e2:
        logging.error(f"Grok chain phase 2 (Key2) failed: {_safe_err(e2)}")
        raise  # both Grok keys exhausted → caller falls back to Flower


def _flower_ref_remix(image_path: str, output_path: str) -> str:
    """Flower img2img remix через FastGenClient (альтернатива Grok edit)."""
    from fastgen_engine import FastGenClient
    fc = FastGenClient()
    return fc.remix_ref_flower(image_path, output_path)


def _openai_ref_remix(image_path: str, output_path: str) -> str:
    """GPT Image edit through FastGen V6."""
    from fastgen_engine import FastGenClient
    return FastGenClient().remix_ref_openai(image_path, output_path)


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
    """
    Route ref remix to configured engine (REMIX_ENGINE).

    REMIX_ENGINE=grok: Grok chain (Key1 → Key2) → Flower img2img → raise.
    """
    if not remix_enabled():
        raise RuntimeError("Image remix disabled by REMIX_ENGINE=off")
    if config.REMIX_ENGINE == 'grok':
        try:
            return _grok_ref_remix_key_chain(image_path, output_path)
        except InterruptedError:
            raise
        except Exception as e:
            logging.warning(f"Grok chain exhausted: {_safe_err(e)}. Fallback → Flower img2img")
            print(f"  {_YELLOW}[GROK→FLOWER FALLBACK]{_RESET} {Path(image_path).name}")
            # Flower — альтернативный FastGen endpoint (не идёт через xAI Cloudflare proxy)
            return _flower_ref_remix(image_path, output_path)
    elif config.REMIX_ENGINE == 'flower':
        return _flower_ref_remix(image_path, output_path)
    elif config.REMIX_ENGINE == 'openai':
        return _openai_ref_remix(image_path, output_path)
    elif config.REMIX_ENGINE == 'cinai':
        prompt = _load_prompt('ref_remix_prompt.txt', _DEFAULT_REF_REMIX)
        return _cinai_remix(image_path, output_path, prompt, config.CINAI_REMIX_MODEL)
    raise RuntimeError(f"Unknown REMIX_ENGINE: {config.REMIX_ENGINE}")


def _grok_i2v_remix_with_retry(image_path: str, output_path: str, prompt: str,
                                flags=None, log_path: str = None, max_attempts: int = 3,
                                api_key: Optional[str] = None, key_label: str = 'Key1') -> str:
    """Grok i2v remix with retries on a specific API key. Raises on total failure."""
    from fastgen_engine import FastGenClient, _flowultra_shutdown_event
    fc = FastGenClient(api_key=api_key) if api_key else FastGenClient()
    last_err = None
    for attempt in range(1, max_attempts + 1):
        if _flowultra_shutdown_event.is_set():
            raise InterruptedError(f"Grok I2V REMIX [{key_label}] aborted by shutdown")
        try:
            print(f"  {_YELLOW}[GROK I2V REMIX {key_label}]{_RESET} {Path(image_path).name} (attempt {attempt}/{max_attempts})")
            result = fc._grok_edit_and_save(image_path, output_path, prompt)
            logging.info(f"Grok I2V REMIX [{key_label}]: {Path(image_path).name} -> {output_path} OK")
            if log_path and flags is not None:
                _save_remix_log(log_path, Path(image_path).name, flags, prompt, result)
            return result
        except Exception as e:
            last_err = e
            err_str = str(e)
            if 'cancelled by user' in err_str.lower() or _flowultra_shutdown_event.is_set():
                raise InterruptedError(f"Grok I2V REMIX [{key_label}] cancelled by user")
            if '403' in err_str and ('DOCTYPE' in err_str or 'html' in err_str.lower()):
                raise  # Cloudflare down — caller handles fallback
            if attempt < max_attempts:
                wait = 5 * attempt
                logging.warning(f"Grok I2V REMIX [{key_label}] attempt {attempt}/{max_attempts} failed: {_safe_err(e)}. Retry in {wait}s...")
                print(f"  {_YELLOW}[GROK I2V REMIX {key_label}]{_RESET} attempt {attempt}/{max_attempts} failed: {_safe_err(e)}. Retry in {wait}s...")
                for _ in range(wait):
                    if _flowultra_shutdown_event.is_set():
                        raise InterruptedError(f"Grok I2V REMIX [{key_label}] aborted during retry wait")
                    time.sleep(1)
    if log_path and flags is not None:
        _save_remix_log(log_path, Path(image_path).name, flags, prompt, output_path, error=_safe_err(last_err))
    raise RuntimeError(f"Grok I2V REMIX [{key_label}] failed after {max_attempts} attempts: {_safe_err(last_err)}")


def _grok_i2v_remix_key_chain(image_path: str, output_path: str, prompt: str,
                               flags=None, log_path: str = None) -> str:
    """I2V remix: Grok Key1 ×1 → 1s → Grok Key2 ×1 → raise.
    Caller делает fallback на Flower."""
    from fastgen_engine import _flowultra_shutdown_event
    key1 = config.FASTGEN_API_KEY
    key2 = getattr(config, 'FASTGEN_API_KEY_2', '')

    def _interruptible_sleep(secs: int, tag: str):
        for _ in range(secs):
            if _flowultra_shutdown_event.is_set():
                raise InterruptedError(f"Grok i2v chain aborted during {tag} pause")
            time.sleep(1)

    if _flowultra_shutdown_event.is_set():
        raise InterruptedError("Grok i2v chain aborted before Key1")
    try:
        return _grok_i2v_remix_with_retry(image_path, output_path, prompt, flags, log_path,
                                           max_attempts=1, api_key=key1, key_label='Key1')
    except InterruptedError:
        raise
    except Exception as e1:
        # e1 живёт только внутри except-блока — сохраняем текст (см. фикс выше)
        _e1_msg = _safe_err(e1)
        logging.warning(f"Grok i2v chain phase 1 (Key1) failed: {_e1_msg}")

    if not key2 or key2 == key1:
        raise RuntimeError(f"Grok i2v Key1 failed and no Key2 configured: {_e1_msg}")

    print(f"  {_YELLOW}[GROK I2V CHAIN]{_RESET} Key1 failed → pause 1s → Key2...")
    _interruptible_sleep(1, '1s-before-Key2')
    try:
        return _grok_i2v_remix_with_retry(image_path, output_path, prompt, flags, log_path,
                                           max_attempts=1, api_key=key2, key_label='Key2')
    except InterruptedError:
        raise
    except Exception as e2:
        logging.error(f"Grok i2v chain phase 2 (Key2) failed: {_safe_err(e2)}")
        raise


def remix_i2v(image_path: str, output_path: str, log_path: str = None, hint: str = None) -> str:
    """
    Route i2v remix to configured engine (REMIX_ENGINE).
    REMIX_ENGINE=grok: Grok chain (Key1 → Key2) → Flower img2img → raise.

    Args:
        hint: targeted remix instruction derived from Google error reason,
              appended to the remix prompt when provided.
    """
    def _build_prompt_and_flags():
        """Return (prompt, flags_dict) for the remix log."""
        base = _load_prompt('i2v_remix_prompt.txt', _DEFAULT_I2V_REMIX)
        if hint:
            return base + ' ' + hint, {'google_hint': True}
        return base, {}

    if not remix_enabled():
        raise RuntimeError("Image remix disabled by REMIX_ENGINE=off")
    if config.REMIX_ENGINE == 'grok':
        prompt, flags = _build_prompt_and_flags()
        try:
            return _grok_i2v_remix_key_chain(image_path, output_path, prompt, flags, log_path)
        except InterruptedError:
            raise
        except Exception as e:
            logging.warning(f"Grok i2v chain exhausted: {_safe_err(e)}. Fallback → Flower img2img")
            print(f"  {_YELLOW}[GROK→FLOWER FALLBACK]{_RESET} {Path(image_path).name}")
            from fastgen_engine import FastGenClient
            _fc = FastGenClient()
            result = _fc.remix_i2v_flower(image_path, output_path, prompt)
            if log_path and flags is not None:
                _save_remix_log(log_path, Path(image_path).name, flags, prompt, result)
            return result
    elif config.REMIX_ENGINE == 'flower':
        prompt, flags = _build_prompt_and_flags()
        from fastgen_engine import FastGenClient
        result = FastGenClient().remix_i2v_flower(image_path, output_path, prompt)
        if log_path:
            _save_remix_log(log_path, Path(image_path).name, flags, prompt, result)
        return result
    elif config.REMIX_ENGINE == 'openai':
        prompt, flags = _build_prompt_and_flags()
        from fastgen_engine import FastGenClient
        result = FastGenClient().remix_i2v_openai(image_path, output_path, prompt)
        if log_path:
            _save_remix_log(log_path, Path(image_path).name, flags, prompt, result)
        return result
    elif config.REMIX_ENGINE == 'cinai':
        prompt, flags = _build_prompt_and_flags()
        result = _cinai_remix(image_path, output_path, prompt, config.CINAI_REMIX_MODEL)
        if log_path:
            _save_remix_log(log_path, Path(image_path).name, flags, prompt, result)
        return result
    raise RuntimeError(f"Unknown REMIX_ENGINE: {config.REMIX_ENGINE}")
