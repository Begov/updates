"""
VeoNonStop imggen — batch-генерация изображений (альтернатива FastGen)

Режимы:
  imagen       — Imagen 3.5 text-to-image
  banana       — Banana Pro text-to-image       (model: GEM_PIX_2)
  banana_ref   — Banana Pro + style-reference   (model: GEM_PIX_2)
  banana2      — Banana 2 text-to-image         (model: NARWHAL)
  banana2_ref  — Banana 2 + style-reference     (model: NARWHAL)

Результат: generated/{id:03d}.png  (совместимо с i2v и Ken Burns)
"""

import sys
import time
import random
import re
import logging
import threading

def _stream_is_tty(stream) -> bool:
    """Это интерактивная консоль? Безопасно при её отсутствии.

    Под `pythonw` у процесса нет stdout вовсе (`sys.stdout is None`), и прямой
    `sys.stdout.isatty()` на уровне модуля ронял импорт с AttributeError —
    стадия не запускалась совсем (проверено 30.08). Нет потока или он не умеет
    отвечать — считаем, что консоли нет: это же режим subprocess/GUI.
    """
    try:
        return bool(stream is not None and stream.isatty())
    except Exception:                                          # noqa: BLE001
        return False


IS_PIPE = not _stream_is_tty(sys.stdout)  # True когда запущен как subprocess (pipeline mode)
if IS_PIPE and hasattr(sys.stdout, 'reconfigure'):
    # Настоящий subprocess (pipe к файлу) — Python block-buffered, принудительно line-buffered
    sys.stdout.reconfigure(line_buffering=True)
import signal
import requests
import contextlib
from pathlib import Path
from typing import Optional, Dict, List, Any
from datetime import datetime
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor, as_completed

# Ensure agent/ and agent/modules/ dirs are on sys.path
_agent_dir = str(Path(__file__).resolve().parent.parent)
_modules_dir = str(Path(__file__).resolve().parent)
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)
if _modules_dir not in sys.path:
    sys.path.insert(0, _modules_dir)

from tqdm import tqdm
import colorama
from colorama import Fore, Style
from units.still_claims import acquire_image_claim

colorama.init(autoreset=True)

_YELLOW = Fore.YELLOW
_RED    = Fore.RED
_GREEN  = Fore.GREEN
_CYAN   = Fore.CYAN
_RESET  = Style.RESET_ALL

import os
import params as config
from prompt_extract import extract_all_prompts, find_prompts_file, remap_dense_prompts_for_montage
from veo_client import (
    VeoNonStopClient,
    VeoNonStopError,
    VeoNonStopTimeoutError,
    VeoNonStopGenerationError,
    VeoNonStopRateLimitError,
    VeoNonStopTokenExpiredError,
    _load_ref_images,
)


# ======================================================================
# SynthID inflight watermark removal
# ======================================================================



# ======================================================================
# Rewrite промпта при цензуре
# ======================================================================

def _rewrite_prompt(original_prompt: str, project_dir=None, clip_id=None) -> str:
    """
    Rewrite a blocked prompt via the shared rewriter (REWRITE_MODEL).
    Instruction is loaded from agent/prompts/rewrite_prompt.txt (user-editable).
    """
    from prompt_softener import rewrite_prompt as _rewrite_blocked_prompt
    return _rewrite_blocked_prompt(original_prompt, project_dir=project_dir, clip_id=clip_id)


# ======================================================================
# Генерация одного изображения
# ======================================================================

# Нумерация воркеров: thread_id → worker_id (для красивого лога)
_imggen_worker_map: Dict[int, int] = {}        # thread_id → worker_id
_imggen_worker_task_count: Dict[int, int] = {} # thread_id → tasks done by this worker
_imggen_worker_counter = [0]
_imggen_worker_lock = threading.Lock()
_banana_submit_sem: Optional[threading.Semaphore] = None  # лимит одновременных banana-запросов
_shutdown_event = threading.Event()
_active_clients: list = []  # list of VeoNonStopClient for Ctrl+C cancel
# Legacy alias for single-client access
_active_client: list = [None]


# Server-usage gate parameters (см. _wait_for_image_slot / _wait_for_image_capacity_before_submit)
_USAGE_POLL_SEC = 5            # как часто опрашиваем /account/usage пока ждём слот
_USAGE_WAIT_TIMEOUT_SEC = 300  # макс. сколько ждём один раз (5 мин)
_PRESUBMIT_USAGE_TIMEOUT_SEC = 120  # макс. ожидание перед submit (короче — чтобы не залипал бесконечно)
_usage_request_lock = threading.Lock()


def _wait_for_image_slot(client, clip_id: int, worker_id: int, key_label: str, mp: int,
                          timeout_sec: int = _USAGE_WAIT_TIMEOUT_SEC, reason: str = 'slots_full') -> bool:
    """
    Поллит /account/usage пока server-side active + queued < mp.
    Используется после slotsfull-ответа: ждать реального освобождения серверного cookie slot.
    Returns True если слот реально освободился, False по timeout/error (fail-soft).
    """
    if client is None or mp <= 0 or not hasattr(client, 'get_account_usage'):
        time.sleep(15)
        return False
    t_start = time.time()
    deadline = t_start + max(15, int(timeout_sec))
    last_active = '?'
    last_queued = '?'
    last_max = mp
    waited_logged = False
    poll_errors = 0
    while time.time() < deadline:
        if _shutdown_event.is_set():
            return False
        try:
            # Не устраивать burst из десятков одновременных /account/usage:
            # relay отвечает этому advisory endpoint заметно медленнее submit/status.
            with _usage_request_lock:
                usage = client.get_account_usage() or {}
            active = int(usage.get('active_tasks', 0) or 0)
            queued = int(usage.get('queued_tasks', 0) or 0)
            server_max = int(usage.get('max_concurrent_tasks', mp) or mp)
            last_active, last_queued, last_max = active, queued, server_max
            poll_errors = 0
            # active+queued — сколько фактически занято/в очереди на сервере для этого ключа
            if active + queued < mp:
                if waited_logged:
                    waited = round(time.time() - t_start, 1)
                    print(
                        f"\n  {_GREEN}[SLOT OK]{_RESET} #{clip_id} [W{worker_id}]{key_label}: "
                        f"waited {waited:.0f}s active={active} queued={queued} limit={mp} usage_max={server_max}",
                        flush=True,
                    )
                return True
            if not waited_logged:
                print(
                    f"\n  {_YELLOW}[WAIT]{_RESET} #{clip_id} [W{worker_id}]{key_label} ({reason}): "
                    f"active={active} queued={queued} limit={mp} usage_max={server_max}",
                    flush=True,
                )
                waited_logged = True
        except Exception as e:
            poll_errors += 1
            if poll_errors >= max(
                1, int(getattr(config, 'VEONONSTOP_USAGE_FAIL_SOFT_ERRORS', 1))
            ):
                logging.debug(
                    f"Clip #{clip_id} [W{worker_id}]{key_label}: "
                    f"account usage unavailable; submit will continue: {e}"
                )
                # сервер недоступен — fail-soft: даём попробовать submit
                return False
        time.sleep(_USAGE_POLL_SEC)
    waited = round(time.time() - t_start, 1)
    print(
        f"\n  {_YELLOW}[WAIT]{_RESET} #{clip_id} [W{worker_id}]{key_label}: "
        f"usage wait timeout {waited:.0f}s (last active={last_active} queued={last_queued} limit={mp} usage_max={last_max}) — trying submit anyway",
        flush=True,
    )
    return False


def _wait_for_image_capacity_before_submit(client, clip_id: int, worker_id: int, key_label: str, mp: int) -> None:
    """
    Pre-submit gate: перед фактическим вызовом generate_banana проверяем что на сервере
    есть свободный слот. Если нет — поллим /account/usage пока active+queued < mp.
    Fail-soft: при ошибках polling-а сразу возвращаемся (даём submit'у попробовать).
    """
    if client is None or mp <= 0 or not hasattr(client, 'get_account_usage'):
        return
    _wait_for_image_slot(client, clip_id, worker_id, key_label, mp,
                         timeout_sec=_PRESUBMIT_USAGE_TIMEOUT_SEC, reason='pre-submit')


def _slot_hold_delay(err: Exception) -> int:
    """Delay semaphore release after errors that may leave a server cookie busy."""
    text = str(err).lower()
    if 'api key validation service unavailable' in text:
        return 2
    if 'server error (503)' in text or 'server error (502)' in text or 'server error (504)' in text:
        return 1
    if 'server error (500)' in text:
        return 1
    if 'recaptcha' in text or 'forbidden' in text:
        return 3
    if _is_censorship_error(text):
        return 1
    return 0


def _run_with_submit_slot(sem, worker_id: int, key_label: str, fn,
                          client=None, key_mp: int = 0, clip_id: int = 0):
    if sem is None:
        # Без семафора — всё равно делаем pre-flight gate если есть client+mp
        if client is not None and key_mp > 0:
            _wait_for_image_capacity_before_submit(client, clip_id, worker_id, key_label, key_mp)
        return fn()
    sem.acquire()
    # Pre-submit gate: реальный server usage может отличаться от нашего семафора
    # (другая сессия с тем же ключом, недавние fail'ы оставили cookie занятым 15-50с).
    if client is not None and key_mp > 0:
        _wait_for_image_capacity_before_submit(client, clip_id, worker_id, key_label, key_mp)
    hold_delay = 0
    hold_reason = ''
    try:
        return fn()
    except (VeoNonStopTimeoutError, VeoNonStopGenerationError, VeoNonStopError) as e:
        hold_delay = _slot_hold_delay(e)
        hold_reason = str(e)[:120]
        raise
    finally:
        if hold_delay > 0 and not _shutdown_event.is_set():
            print(
                f"  {_YELLOW}[SLOT-HOLD]{_RESET} #{worker_id}{key_label}: "
                f"holding submit slot {hold_delay}s after {hold_reason}",
                flush=True,
            )
            time.sleep(hold_delay)
        sem.release()


def _sigint_handler(sig, frame):
    if _shutdown_event.is_set():
        return
    _shutdown_event.set()
    print(f"\n\n  [CANCELLING] Ctrl+C — sending cancel-all to server...", flush=True)
    for c in _active_clients:
        try:
            cancelled = c.cancel_all_tasks()
            print(f"  [CANCELLING] {cancelled} task(s) cancelled on server", flush=True)
        except Exception as e:
            print(f"  [CANCELLING] cancel-all failed: {e}", flush=True)
    print(f"  [CANCELLING] Stopping workers...\n", flush=True)

_CENSORSHIP_KEYWORDS = [
    'invalid argument', 'safety', 'blocked', 'censored',
    'harmful', 'policy', 'prohibited', 'violat',
    'prominent person', 'dangerous', 'inappropriate',
    # VeoNonStop API error codes
    'prominent_people_filter',
    'public_error_danger_filter',
    'public_error_unsafe_generation',
    'public_error_ip_input_image',
    'public_error_audio_filtered',
]


def _is_censorship_error(err_str: str) -> bool:
    """Проверяет, является ли ошибка цензурной."""
    err_lower = err_str.lower()
    return any(kw in err_lower for kw in _CENSORSHIP_KEYWORDS)


# ── Два РАЗНЫХ терминальных состояния, которые легко спутать ──────────────────
#
# 1) Дневная квота НАШЕГО ключа: HTTP 429 «Daily image generation limit exceeded».
#    Проверяется через /account/usage → image_remaining == 0 (живой ответ ключа
#    17.08.2026: image_used 505 / image_limit 3030 / resets_in_seconds 33298).
#    Лечится сменой ключа или уходом к другому провайдеру.
#
# 2) Пул cookies СЕРВИСА под конкретную модель: HTTP 500
#    «ALL_COOKIES_EXHAUSTED_FOR_MODEL: NARWHAL. All cookies have hit daily quota».
#    Это дневная квота Google-аккаунтов на стороне VeoNonStop, а НЕ наша: у ключа
#    квота при этом остаётся. Смена ключа не помогает (пул общий), помогает смена
#    МОДЕЛИ: у HARBOR_SEAL / NARWHAL / GEM_PIX_2 пулы разные. В доке
#    veononstop.com/api-docs.html эта ошибка не описана вообще — установлено
#    по живым ответам сервера (NARWHAL отдал 200 за 25.8с при нашей квоте 2525).
#
# Ретраи и реврайты бессмысленны в обоих случаях: прогон «пикассо» 17.08.2026 —
# 4786 ответов с дневным лимитом, 4 цикла по 400+ клипов, 1582 холостых реврайта,
# ~1.5 часа работы впустую на 11 сделанных картинок.
_DAILY_QUOTA_MARKERS = (
    'daily image generation limit',
    'daily limit exceeded',
)

_MODEL_POOL_MARKERS = (
    'all_cookies_exhausted_for_model',
)

# Порядок перебора banana-моделей, когда пул текущей выжжен.
_BANANA_MODEL_CHAIN_DEFAULT = ('GEM_PIX_2', 'NARWHAL', 'HARBOR_SEAL')
_BANANA_MODEL_NAMES = {
    'GEM_PIX_2': 'Banana Pro',
    'NARWHAL': 'Banana 2',
    'HARBOR_SEAL': 'Banana 2 Lite',
}


def _is_daily_quota_error(err_str: str) -> bool:
    """True — дневная квота НАШЕГО ключа исчерпана (429)."""
    err_lower = (err_str or '').lower()
    return any(kw in err_lower for kw in _DAILY_QUOTA_MARKERS)


def _is_model_pool_exhausted(err_str: str) -> bool:
    """True only for the provider's explicit per-model exhaustion code."""
    err_lower = (err_str or '').lower()
    return any(kw in err_lower for kw in _MODEL_POOL_MARKERS)


def _exhausted_model_name(err_str: str) -> str:
    """Имя модели из «ALL_COOKIES_EXHAUSTED_FOR_MODEL: NARWHAL. …» ('' если нет)."""
    m = re.search(r'all_cookies_exhausted_for_model:\s*([A-Za-z0-9_]+)',
                  err_str or '', re.IGNORECASE)
    return (m.group(1) if m else '').upper()


def _banana_model_chain() -> List[str]:
    """Цепочка моделей для перебора (env VEONONSTOP_BANANA_MODEL_CHAIN)."""
    raw = str(getattr(config, 'VEONONSTOP_BANANA_MODEL_CHAIN', '') or '').strip()
    chain = [m.strip().upper() for m in raw.split(',') if m.strip()] if raw else []
    if not chain:
        return list(_BANANA_MODEL_CHAIN_DEFAULT)
    # A custom order may be used, but never silently remove one of the
    # documented Banana models from fallback.
    return chain + [m for m in _BANANA_MODEL_CHAIN_DEFAULT if m not in chain]


def _veo_fastgen_fallback_enabled() -> bool:
    """Разрешено ли уводить остаток на FastGen при исчерпании дневной квоты."""
    return bool(getattr(config, 'VEONONSTOP_QUOTA_FALLBACK_FASTGEN', True))


def _fastgen_key_configured() -> bool:
    """Return whether at least one FastGen key is configured."""
    return any(
        str(getattr(config, key, '') or '').strip()
        for key in (
            'FASTGEN_API_KEY',
            'FASTGEN_API_KEY_BACKUP',
            'FASTGEN_API_KEY_2',
            'FASTGEN_API_KEY_3',
            'FASTGEN_API_KEY_4',
        )
    )


def _veo_no_capacity_should_fallback_to_t2v() -> bool:
    """Resolve the terminal action after Veo ImageGen capacity is exhausted."""
    return (
        not _fastgen_key_configured()
        or bool(getattr(config, 'VEONONSTOP_QUOTA_FALLBACK_T2V', False))
    )


# Гасит попытки воркеров, когда квота признана исчерпанной на уровне провайдера.
# Отдельно от _shutdown_event (тот — Ctrl+C и общий выход), сбрасывается в начале
# каждого цикла.
_quota_stop_event = threading.Event()


def _try_generate(
    client: VeoNonStopClient,
    clip_id: int,
    prompt: str,
    mode: str,
    output_path: Path,
    aspect_ratio: Optional[str] = None,
    ref_images: Optional[List[Dict[str, str]]] = None,
    max_retries: int = 3,
    phase_label: str = "phase1",
    worker_id: int = 0,
    submit_sem=None,
    ok_run: Optional[dict] = None,
    upscale_resolution: Optional[str] = None,
    key_label: str = '',
    key_mp: int = 0,
    banana_model_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Внутренний цикл генерации (одна фаза).
    Возвращает {'status': 'ok'|'blocked'|'error', 'block_count': int, ...}
    """
    result = {
        'id': clip_id,
        'status': 'error',
        'file': None,
        'error': None,
        'duration_sec': 0,
        'task_id': None,
        'block_count': 0,
        'quota_exhausted': False,        # дневная квота НАШЕГО ключа
        'model_pool_exhausted': False,   # cookies сервиса под текущую МОДЕЛЬ
        'exhausted_model': '',
    }

    attempt = 0
    _5xx_count = 0
    while attempt < max_retries:
        if _shutdown_event.is_set():
            result['error'] = 'shutdown requested'
            return result
        # Квота уже признана исчерпанной другим воркером — не тратим попытку
        # и не ждём: решение принимается на уровне провайдера, выше.
        if _quota_stop_event.is_set():
            result['error'] = result['error'] or 'provider quota exhausted'
            result['quota_exhausted'] = True
            return result
        attempt += 1
        try:
            start = time.time()
            # Ветка imagen результата не возвращает, а апскейл ниже читает его
            # атрибуты: без инициализации это UnboundLocalError у всех, у кого
            # VEONONSTOP_IMAGE_UPSCALE не off (аудит 25.08).
            _gen_result = None

            if mode == 'imagen':
                client.generate_imagen(
                    prompt=prompt,
                    output_path=str(output_path),
                    aspect_ratio=aspect_ratio,
                )
            elif mode in ('banana', 'banana_ref'):
                _sem = submit_sem if submit_sem is not None else _banana_submit_sem
                _gen_result = _run_with_submit_slot(
                    _sem, worker_id, key_label,
                    lambda: client.generate_banana(
                        prompt=prompt,
                        output_path=str(output_path),
                        aspect_ratio=aspect_ratio,
                        num_images=1,
                        reference_images=ref_images if mode == 'banana_ref' else None,
                        use_all_ref_images=(config.VEONONSTOP_REF_MODE == 'use_all'),
                        model_key=banana_model_key,
                    ),
                    client=client, key_mp=key_mp, clip_id=clip_id,
                )
            elif mode in ('banana2', 'banana2_ref'):
                _sem = submit_sem if submit_sem is not None else _banana_submit_sem
                _gen_result = _run_with_submit_slot(
                    _sem, worker_id, key_label,
                    lambda: client.generate_banana(
                        prompt=prompt,
                        output_path=str(output_path),
                        aspect_ratio=aspect_ratio,
                        num_images=1,
                        reference_images=ref_images if mode == 'banana2_ref' else None,
                        model_key=banana_model_key or 'NARWHAL',
                    ),
                    client=client, key_mp=key_mp, clip_id=clip_id,
                )
            else:
                raise ValueError(f"Unknown imggen mode: {mode}")

            elapsed = round(time.time() - start, 1)
            result.update({
                'status': 'ok',
                'file': str(output_path),
                'duration_sec': elapsed,
            })
            print(f"  {_GREEN}[OK]{_RESET} #{clip_id} [W{worker_id}]: {elapsed}s", flush=True)

            # ── Image upscale (in-place, overwrites original) ──
            if upscale_resolution and hasattr(_gen_result, 'media_id') and _gen_result.media_id:
                try:
                    _t_ups = time.time()
                    client.upscale_banana(
                        media_id=_gen_result.media_id,
                        project_id=_gen_result.project_id,
                        output_path=str(output_path),
                        target_resolution=upscale_resolution,
                    )
                    _t_ups = round(time.time() - _t_ups, 1)
                    print(f"  {_GREEN}[UPSCALE]{_RESET} #{clip_id} [W{worker_id}]: {_t_ups}s", flush=True)
                    logging.info(f"Clip #{clip_id}: upscaled ({_t_ups}s)")
                except Exception as _ups_err:
                    print(f"  {_YELLOW}[UPSCALE]{_RESET} #{clip_id}: failed ({_ups_err})", flush=True)
                    logging.warning(f"Clip #{clip_id}: upscale failed: {_ups_err}")

            if ok_run is not None:
                with ok_run['lock']:
                    ok_run['n'] += 1
                    ok_run['total'] += elapsed
                    _avg = ok_run['total'] / ok_run['n']
                    _n   = ok_run['n']
                _wall = time.time() - ok_run['wall_start']
                _wall_cpm = _n / (_wall / 60) if _wall > 0 else 0
                _wall_spc = 60.0 / _wall_cpm if _wall_cpm > 0 else 0
                print(f"  {_CYAN}[SPEED]{_RESET} worker avg={_avg:.0f}s/clip  |  pool {_wall_spc:.1f}s/clip ({_wall_cpm:.1f} clip/min)", flush=True)
            logging.info(f"Clip #{clip_id}: imggen OK ({elapsed}s) [{phase_label}]")
            return result

        except VeoNonStopTokenExpiredError:
            # Не тратим попытку, не считаем блоком — временная ошибка сервера
            attempt -= 1
            msg = f"#{clip_id} [W{worker_id}] [{phase_label}] attempt {attempt}: token expired, waiting 30s..."
            logging.warning(f"Clip {msg}")
            print(f"\n  {_YELLOW}[TOKEN]{_RESET} {msg}", flush=True)
            time.sleep(30)

        except VeoNonStopRateLimitError as e:
            # 429 идёт через ОБЩУЮ логику ретраев: ТРАТИТ попытку + нарастающий
            # кулдаун (как generic-фейл ниже). Раньше был attempt -= 1 (не тратил)
            # + фикс 10с — и при ПОСТОЯННОМ рейтлимите клип крутился бесконечно, не
            # возвращая терминальный статус: цикл не докручивался → MAX_SILENT_CYCLES
            # и FastGen-фоллбэк не срабатывали (юзеру приходилось жать Stop). Теперь
            # после max_retries клип падает как error → внешний цикл добьёт его.
            result['error'] = f'rate limit (429): {e}'
            # Дневная квота ключа — это не рейтлимит: ретраить нечего, сегодня всё.
            # Поднимаем флаг наверх, там решают: запасной ключ / FastGen / ожидание.
            if _is_daily_quota_error(str(e)):
                result['quota_exhausted'] = True
                _quota_stop_event.set()
                msg = (f"#{clip_id} [W{worker_id}]{key_label} [{phase_label}]: DAILY QUOTA "
                       f"exhausted — no retries, escalating to provider level | "
                       f"server: {str(e)[:160]}")
                logging.warning(f"Clip {msg}")
                print(f"\n  {_RED}[DAILY QUOTA]{_RESET} {msg}", flush=True)
                return result
            _rl_wait = min(10 * attempt, 60)
            msg = f"#{clip_id} [W{worker_id}] [{phase_label}] attempt {attempt}/{max_retries}: rate limit, waiting {_rl_wait}s... | server: {e}"
            logging.warning(f"Clip {msg}")
            print(f"\n  {_YELLOW}[RATELIMIT]{_RESET} {msg}", flush=True)
            if attempt < max_retries:
                time.sleep(_rl_wait)

        except VeoNonStopTimeoutError as e:
            err_str = str(e)
            result['error'] = err_str
            # Server 5xx
            if 'Server error' in err_str:
                _err_lower = err_str.lower()
                # ALL_COOKIES_EXHAUSTED_FOR_MODEL приходит как 500, но это не сбой
                # сервера: у сервиса кончились cookies под ЭТУ модель. Ресабмиты той
                # же моделью только жгут время — выходим, клип пробует другую модель.
                if _is_model_pool_exhausted(_err_lower):
                    result['model_pool_exhausted'] = True
                    result['exhausted_model'] = _exhausted_model_name(err_str)
                    msg = (f"#{clip_id} [W{worker_id}]{key_label} [{phase_label}]: MODEL POOL "
                           f"exhausted ({result['exhausted_model'] or '?'}) — no resubmits, "
                           f"switching model for this clip | server: {err_str[:160]}")
                    logging.warning(msg)
                    print(f"\n  {_YELLOW}[MODEL POOL]{_RESET} {msg}", flush=True)
                    return result
                # Дневная квота НАШЕГО ключа может прилететь и внутри 5xx.
                if _is_daily_quota_error(_err_lower):
                    result['quota_exhausted'] = True
                    _quota_stop_event.set()
                    msg = (f"#{clip_id} [W{worker_id}]{key_label} [{phase_label}]: DAILY QUOTA "
                           f"in 5xx — no resubmits, escalating to provider level | "
                           f"server: {err_str[:160]}")
                    logging.warning(msg)
                    print(f"\n  {_RED}[DAILY QUOTA]{_RESET} {msg}", flush=True)
                    return result
                # Cookie slots full внутри 5xx — server pacing, NOT real 5xx.
                # Image API синхронен (15-50с на cookie). Поллим /account/usage пока active+queued < mp,
                # т.е. ждём реального освобождения серверного слота (а не слепые 60с).
                # attempt не тратим, 5xx counter не трогаем.
                if 'cookie slots full' in _err_lower or 'slots full' in _err_lower:
                    attempt -= 1
                    msg = f"#{clip_id} [W{worker_id}]{key_label} [{phase_label}]: SLOTS FULL — polling server usage..."
                    logging.warning(msg)
                    print(f"\n  {_YELLOW}[SLOTS FULL]{_RESET} {msg}", flush=True)
                    _wait_for_image_slot(client, clip_id, worker_id, key_label, key_mp, reason='5xx-slots-full')
                    continue
                # reCAPTCHA/FORBIDDEN внутри 5xx — сервер блокирует, нужна долгая пауза
                if 'recaptcha' in _err_lower or 'forbidden' in _err_lower:
                    attempt -= 1
                    msg = f"#{clip_id} [W{worker_id}]{key_label} [{phase_label}] attempt {attempt}: reCAPTCHA in 5xx — {err_str[:120]} — waiting 30s..."
                    logging.warning(msg)
                    print(f"\n  {_YELLOW}[RECAPTCHA]{_RESET} {msg}", flush=True)
                    time.sleep(15)
                    continue
                # invalid argument / censorship внутри 5xx — считаем как BLOCKED
                if _is_censorship_error(_err_lower):
                    result['block_count'] += 1
                    msg = f"#{clip_id} [W{worker_id}]{key_label} [{phase_label}] attempt {attempt}/{max_retries}: BLOCKED in 5xx (block #{result['block_count']}) — {err_str[:120]}"
                    logging.warning(msg)
                    print(f"\n  {_RED}[BLOCKED]{_RESET} {msg}", flush=True)
                    if attempt < max_retries:
                        time.sleep(5)
                    continue
                # Обычный server error — быстрый resubmit с лимитом
                _5xx_count += 1
                _5XX_PER_WORKER_MAX = 10
                if _5xx_count <= _5XX_PER_WORKER_MAX:
                    attempt -= 1
                    wait_5xx = min(5 * _5xx_count, 60)
                    msg = f"#{clip_id} [W{worker_id}]{key_label} [{phase_label}] 5xx #{_5xx_count} — {err_str[:120]} — retry in {wait_5xx}s"
                    logging.warning(msg)
                    print(f"\n  {_YELLOW}[5XX-RESUB]{_RESET} {msg}", flush=True)
                    time.sleep(wait_5xx)
                    continue
                else:
                    _5xx_count = 0
                    msg = f"#{clip_id} [W{worker_id}]{key_label} [{phase_label}] 5xx limit reached — counting as failed attempt"
                    logging.warning(msg)
                    print(f"\n  {_RED}[5XX-FAIL]{_RESET} {msg}", flush=True)
            else:
                logging.warning(f"Clip #{clip_id} [W{worker_id}] [{phase_label}] attempt {attempt}/{max_retries}: timeout")
                print(f"\n  {_YELLOW}[TIMEOUT]{_RESET} #{clip_id} [{phase_label}] {attempt}/{max_retries}", flush=True)
                if attempt < max_retries:
                    time.sleep(min(10 * attempt, 60))

        except (VeoNonStopGenerationError, VeoNonStopError) as e:
            err_str = str(e).lower()
            result['error'] = str(e)

            # Оба терминальных состояния могут прилететь и generic-ошибкой.
            if _is_model_pool_exhausted(err_str):
                result['model_pool_exhausted'] = True
                result['exhausted_model'] = _exhausted_model_name(str(e))
                msg = (f"#{clip_id} [W{worker_id}]{key_label} [{phase_label}]: MODEL POOL "
                       f"exhausted ({result['exhausted_model'] or '?'}) — switching model "
                       f"for this clip | server: {str(e)[:160]}")
                logging.warning(msg)
                print(f"\n  {_YELLOW}[MODEL POOL]{_RESET} {msg}", flush=True)
                return result
            if _is_daily_quota_error(err_str):
                result['quota_exhausted'] = True
                _quota_stop_event.set()
                msg = (f"#{clip_id} [W{worker_id}]{key_label} [{phase_label}]: DAILY QUOTA "
                       f"— no retries, escalating to provider level | server: {str(e)[:160]}")
                logging.warning(msg)
                print(f"\n  {_RED}[DAILY QUOTA]{_RESET} {msg}", flush=True)
                return result

            # Цензура / invalid argument — считаем блокировки
            if _is_censorship_error(err_str):
                result['block_count'] += 1
                logging.warning(f"Clip #{clip_id} [W{worker_id}] [{phase_label}] attempt {attempt}/{max_retries}: BLOCKED ({result['block_count']}x) — {e}")
                print(f"\n  {_RED}[BLOCKED]{_RESET}  #{clip_id} [{phase_label}] {attempt}/{max_retries} (block #{result['block_count']})", flush=True)
                if attempt < max_retries:
                    time.sleep(5)
                continue

            # reCAPTCHA / FORBIDDEN — не тратим попытку
            if 'recaptcha' in err_str or 'forbidden' in err_str:
                attempt -= 1
                msg = f"#{clip_id} [W{worker_id}] [{phase_label}] attempt {attempt}: reCAPTCHA/FORBIDDEN — {e} — waiting 15s..."
                logging.warning(msg)
                print(f"\n  {_YELLOW}[RECAPTCHA]{_RESET} {msg}", flush=True)
                time.sleep(15)
                continue

            # Cookie slots full в GenerationError-ветке — поллим /account/usage до реального освобождения.
            # Не тратим попытку.
            if 'cookie slots full' in err_str or 'slots full' in err_str:
                attempt -= 1
                logging.warning(f"Clip #{clip_id} [W{worker_id}]{key_label} [{phase_label}]: slots full — polling server usage")
                print(f"\n  {_YELLOW}[SLOTS FULL]{_RESET} #{clip_id} [W{worker_id}]{key_label}: polling usage", flush=True)
                _wait_for_image_slot(client, clip_id, worker_id, key_label, key_mp, reason='gen-slots-full')
                continue

            logging.error(f"Clip #{clip_id} [W{worker_id}] [{phase_label}] attempt {attempt}/{max_retries}: {e}")
            print(f"\n  {_RED}[FAILED]{_RESET}  #{clip_id} [{phase_label}] {attempt}/{max_retries}: {e}", flush=True)
            if attempt < max_retries:
                time.sleep(min(10 * attempt, 60))

        except (requests.exceptions.ProxyError, requests.exceptions.ConnectionError) as e:
            # Не тратим попытку
            attempt -= 1
            msg = f"#{clip_id} [W{worker_id}] [{phase_label}] {attempt}/{max_retries}: proxy/connection error — waiting 30s..."
            logging.warning(msg)
            print(f"\n  {_YELLOW}[PROXY]{_RESET}   {msg}", flush=True)
            time.sleep(30)

        except Exception as e:
            result['error'] = str(e)
            _ex_lower = str(e).lower()
            # reCAPTCHA / RESOURCE_EXHAUSTED — module identity fallback
            if 'recaptcha' in _ex_lower or 'resource_exhausted' in _ex_lower:
                attempt -= 1
                msg = f"#{clip_id} [W{worker_id}] [{phase_label}] attempt {attempt}: reCAPTCHA (Exception path), waiting 30s..."
                logging.warning(msg)
                print(f"\n  {_YELLOW}[RECAPTCHA]{_RESET} {msg}", flush=True)
                time.sleep(30)
                continue
            logging.error(f"Clip #{clip_id} [W{worker_id}] [{phase_label}] attempt {attempt}/{max_retries}: {e}")
            print(f"\n  {_RED}[ERROR]{_RESET}   #{clip_id} [{phase_label}] {attempt}/{max_retries}: {e}", flush=True)
            if attempt < max_retries:
                time.sleep(min(10 * attempt, 60))

    # Если были только блокировки — отмечаем
    if result['block_count'] > 0 and result['status'] != 'ok':
        result['status'] = 'blocked'
    return result


def _filter_refs_by_prompt_tokens(prompt: str, ref_images: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
    """Возвращает только те ref_images, чьё имя (stem без расширения) реально упомянуто
    в промпте как токен [name]. Гарантирует что в Banana уходит ТОЛЬКО нужные refs
    для конкретного клипа — иначе серверный парсер VeoNonStop иногда ошибается на
    длинных промптах и берёт не тот ref.

    Активируется только при VEONONSTOP_REF_MODE == 'name_match'. Для 'use_all' — no-op.
    Если в промпте нет ни одного матчинг-токена — fallback к полному списку (старое поведение),
    чтобы не сломать промпты без токенов.
    Отключить можно env-флагом VEONONSTOP_PER_CLIP_REF_FILTER=0.
    """
    if not ref_images or not prompt:
        return ref_images
    if getattr(config, 'VEONONSTOP_REF_MODE', 'name_match') != 'name_match':
        return ref_images
    if os.getenv('VEONONSTOP_PER_CLIP_REF_FILTER', '1').lower() in ('0', 'false', 'no', 'off'):
        return ref_images

    # Парсим токены вида [drev_ref] и [drev_ref.png] из промпта.
    tokens_in_prompt = {
        t.lower()
        for t in re.findall(r'\[([A-Za-z0-9_]+_ref(?:\.(?:png|jpg|jpeg|webp))?)\]', prompt, re.I)
    }
    if not tokens_in_prompt:
        # Промпт без токенов — кадр окружения/предмета без персонажей.
        # Refs не нужны: они только засорят style. Отдаём None → чистый text-to-image.
        return None

    filtered = []
    for img in ref_images:
        if isinstance(img, dict):
            name = img.get('name', '') or img.get('path', '') or ''
        else:
            name = str(img)
        path_name = Path(name).name.lower()
        stem = Path(name).stem.lower()  # 'sael_ref.png' -> 'sael_ref'
        if stem in tokens_in_prompt or path_name in tokens_in_prompt:
            filtered.append(img)

    # Токены в промпте есть, но имена refs с ними не совпали (rare случай — например
    # автор промпта ошибся в имени токена). Возвращаем None — без refs, не сбиваем по чужим.
    return filtered if filtered else None


def _generate_single_image(
    client: VeoNonStopClient,
    clip_id: int,
    prompt: str,
    mode: str,
    output_dir: Path,
    aspect_ratio: Optional[str] = None,
    ref_images: Optional[List[Dict[str, str]]] = None,
    max_retries: int = 3,
    clients_cfg=None,   # if provided: dual-key worker-assignment overrides client
    ok_run: Optional[dict] = None,
    upscale_resolution: Optional[str] = None,
    banana_model_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Генерация одного изображения с двумя фазами:
    Фаза 1: до max_retries попыток с оригинальным промптом.
    Фаза 2 (при блокировке): rewrite промпта → ещё max_retries попыток.
    """
    output_path = output_dir / f"{clip_id:03d}.png"

    # Назначаем worker_id по thread_id (для красивого лога)
    tid = threading.get_ident()
    with _imggen_worker_lock:
        if tid not in _imggen_worker_map:
            _imggen_worker_map[tid] = _imggen_worker_counter[0]
            _imggen_worker_counter[0] += 1
            _imggen_worker_task_count[tid] = 0
        worker_id = _imggen_worker_map[tid]
        task_num = _imggen_worker_task_count[tid]
        _imggen_worker_task_count[tid] += 1

    # ── Worker-assignment: dual-key — выбираем client+sem по worker_id ──
    _submit_sem = None
    _key_label = ''
    _key_mp = 0
    if clients_cfg:
        cumulative = 0
        _selected = clients_cfg[-1]
        _group_start = 0
        for _cfg in clients_cfg:
            if worker_id < cumulative + _cfg['mp']:
                _selected = _cfg
                _group_start = cumulative
                break
            cumulative += _cfg['mp']
        client = _selected['client']
        _submit_sem = _selected['sem']
        _key_label = f" [{_selected['label']}]"
        _key_mp = int(_selected.get('mp', 0) or 0)
        local_worker_id = worker_id - _group_start
    else:
        local_worker_id = worker_id
        # Single-key path: достаём mp из глобального config
        try:
            _key_mp = int(getattr(config, 'VEONONSTOP_MAX_PARALLEL', 0) or 0)
        except Exception:
            _key_mp = 0

    # Stagger: первая задача — волновой запуск 3с на воркер, последующие — 1с + jitter
    jitter = random.uniform(0.5, 1.0)
    if task_num == 0:
        stagger = local_worker_id * 3.0
    else:
        stagger = 1.0
    total_delay = stagger + jitter
    logging.info(f"Clip #{clip_id} [W{worker_id}]{_key_label} task#{task_num}: stagger {stagger}s + jitter {jitter:.1f}s")
    print(f"  Clip #{clip_id} [W{worker_id}]{_key_label} task#{task_num}: stagger {stagger}s + jitter {jitter:.1f}s", flush=True)
    time.sleep(total_delay)

    # ── Фаза 1: оригинальный промпт ──
    # Пул cookies выжигается ПО МОДЕЛИ, поэтому перебор моделей — здесь, на клипе:
    # поймал ALL_COOKIES_EXHAUSTED_FOR_MODEL → пробуем следующую модель этим же
    # клипом (ключ менять смысла нет — пул общий у сервиса).
    _model_now = banana_model_key or (
        'NARWHAL' if mode in ('banana2', 'banana2_ref')
        else getattr(config, 'VEONONSTOP_BANANA_MODEL', 'GEM_PIX_2'))
    _models_to_try = [_model_now] + [m for m in _banana_model_chain() if m != _model_now]
    _pool_dead: List[str] = []

    phase1 = None
    for _mi, _model in enumerate(_models_to_try):
        _label = 'phase1' if _mi == 0 else f'phase1_model{_mi}'
        if _mi > 0:
            print(f"  {_YELLOW}[MODEL SWITCH]{_RESET} #{clip_id} [W{worker_id}]: "
                  f"{', '.join(_pool_dead)} pool empty → trying {_model}", flush=True)
            logging.warning(f"Clip #{clip_id}: model pool {_pool_dead} empty — trying {_model}")
        phase1 = _try_generate(
            client, clip_id, prompt, mode, output_path,
            aspect_ratio=aspect_ratio, ref_images=ref_images,
            max_retries=max_retries, phase_label=_label, worker_id=worker_id,
            submit_sem=_submit_sem, ok_run=ok_run, upscale_resolution=upscale_resolution,
            key_label=_key_label, key_mp=_key_mp, banana_model_key=_model,
        )
        if phase1['status'] == 'ok':
            if _mi > 0:
                phase1['fallback_model'] = _model
            break
        if not phase1.get('model_pool_exhausted'):
            break     # другая ошибка — дальше обычная логика (rewrite и т.д.)
        _pool_dead.append(phase1.get('exhausted_model') or _model)
    else:
        # Ни одной модели с живым пулом не осталось — это уже уровень провайдера.
        phase1['all_models_exhausted'] = True
        print(f"  {_RED}[MODEL POOL]{_RESET} #{clip_id} [W{worker_id}]: all banana models "
              f"exhausted ({', '.join(_pool_dead)})", flush=True)
        logging.error(f"Clip #{clip_id}: all banana model pools exhausted: {_pool_dead}")

    if phase1['status'] == 'ok':
        return phase1

    # Дневная квота — реврайт не поможет (промпт тут ни при чём). Раньше именно
    # этот путь давал 1582 холостых реврайта за прогон: «silent fail» на пустой
    # квоте уходил в rewrite, тот возвращал тот же промпт, и клип падал.
    if phase1.get('quota_exhausted') or phase1.get('all_models_exhausted'):
        _why = 'daily key quota' if phase1.get('quota_exhausted') else 'all model pools empty'
        print(f"  {_RED}[NO CAPACITY]{_RESET} #{clip_id} [W{worker_id}]: "
              f"skipping rewrite — {_why}, not a prompt problem", flush=True)
        logging.warning(f"Clip #{clip_id}: {_why} — no rewrite, escalating")
        return phase1

    # Если явная техническая ошибка (сервер, сеть) — rewrite не поможет
    _err_str = str(phase1.get('error', '')).lower()
    _is_hard_tech = any(k in _err_str for k in ('timeout', '5xx', 'connection', 'socket', 'network'))
    if phase1['status'] != 'blocked' and _is_hard_tech:
        print(f"  {_RED}[FAILED]{_RESET}   #{clip_id} [W{worker_id}]: {phase1.get('error', 'unknown error')}")
        logging.error(f"Clip #{clip_id}: FAILED (technical error): {phase1.get('error')}")
        return phase1

    # ── Rewrite промпта (при блокировке ИЛИ при молчаливом фейле) ──
    _reason = f"blocked {phase1['block_count']}x" if phase1['status'] == 'blocked' else "silent fail"
    print(f"  {_RED}[REWRITE]{_RESET}  #{clip_id}: {_reason}, rewriting prompt...")
    logging.info(f"Clip #{clip_id}: {_reason}, starting rewrite")

    new_prompt = _rewrite_prompt(prompt, project_dir=output_dir.parent, clip_id=clip_id)
    if new_prompt == prompt:
        print(f"  {_YELLOW}[REWRITE]{_RESET}  #{clip_id}: rewrite returned same prompt, giving up")
        logging.warning(f"Clip #{clip_id}: rewrite returned same prompt")
        phase1['status'] = 'blocked'
        return phase1

    print(f"  {_RED}[REWRITE]{_RESET}  #{clip_id}: prompt rewritten, starting phase2...")
    logging.info(f"Clip #{clip_id}: new prompt: {new_prompt[:100]}...")

    # ── Фаза 2: переписанный промпт ──
    phase2 = _try_generate(
        client, clip_id, new_prompt, mode, output_path,
        aspect_ratio=aspect_ratio, ref_images=ref_images,
        max_retries=max_retries, phase_label="phase2", worker_id=worker_id,
        submit_sem=_submit_sem, ok_run=ok_run, upscale_resolution=upscale_resolution,
        key_label=_key_label, key_mp=_key_mp, banana_model_key=banana_model_key,
    )

    # Накапливаем block_count обеих фаз
    phase2['block_count'] = phase2.get('block_count', 0) + phase1.get('block_count', 0)

    if phase2['status'] == 'ok':
        return phase2

    # Обе фазы провалились
    if phase2['status'] == 'blocked':
        print(f"  {_RED}[SKIP]{_RESET}     #{clip_id}: blocked after rewrite (total blocks: {phase2['block_count']}), giving up")
        logging.error(f"Clip #{clip_id}: BLOCKED after both phases (total blocks: {phase2['block_count']})")
    else:
        print(f"  {_RED}[FAILED]{_RESET}   #{clip_id}: failed after rewrite")
        logging.error(f"Clip #{clip_id}: FAILED after both phases: {phase2.get('error')}")

    phase2['status'] = 'blocked' if phase2.get('block_count', 0) > 0 else 'error'
    return phase2


# ======================================================================
# Основная функция: batch-генерация изображений
# ======================================================================

def generate_all_images(
    project_dir: str,
    mode: str = 'imagen',
    max_parallel: Optional[int] = None,
    clip_ids: Optional[List[int]] = None,
    aspect_ratio: Optional[str] = None,
    ref_dir: Optional[str] = None,
    silent: bool = False,
    preflight: bool = True,
    max_cycles: Optional[int] = None,
    active_keys: Optional[List[int]] = None,
    upscale_resolution: Optional[str] = None,
    include_infographics: bool = False,
    banana_model_key: Optional[str] = None,
    use_claims: bool = False,
    claim_owner: str = "veononstop",
    client_provider: Optional[str] = None,
    client_options: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Batch-генерация изображений через VeoNonStop (Imagen 3.5 / Banana Pro).

    Args:
        client_provider: чем генерировать — 'veononstop' | 'vertex' | 'cheapvibe' | 'varag'. None —
                        как у основных картинок (BEGO_IMAGE_PROVIDER). Явный провайдер нужен
                        стадиям со своим выбором (инфографика): иначе её VeoNonStop ушёл бы
                        в провайдера основных картинок.
        client_options: модель/размер/качество этого провайдера ({'model', 'size', 'quality'}).
        project_dir:    Путь к папке проекта
        mode:           'imagen' | 'banana' | 'banana_ref' | 'banana2' | 'banana2_ref'
        max_parallel:   Макс. параллельных задач
        clip_ids:       Список ID клипов (если None — все из промптов)
        aspect_ratio:   Соотношение сторон
        ref_dir:        Папка с style-reference (для banana_ref). Если None — PROJECT/ref/
        silent:         Авто-режим (без интерактивных вопросов):
                        2 авто-цикла → если ещё остались failed →
                        фоллбэк на FastGen для остатков
        active_keys:    Список 1-based индексов ключей [1], [1,2], [1,2,3,4] и т.д.

    Returns:
        Сводка генерации
    """
    project_path = Path(project_dir)
    # Явно заданное число потоков («Генерация картинок» → «Потоков») — на каждый ключ VeoNonStop.
    # Раньше оно терялось: ниже max_parallel пересчитывается из _KEY_DEFS, где у ключа 1 стоит
    # VEONONSTOP_MAX_PARALLEL — то есть число потоков ВИДЕО.
    _requested_parallel = int(max_parallel) if max_parallel else 0
    max_parallel = max_parallel or config.VEONONSTOP_MAX_PARALLEL

    _KEY_DEFS = [
        (config.VEONONSTOP_API_KEY,   config.VEONONSTOP_MAX_PARALLEL,   config.VEONONSTOP_KEY1_NAME or 'Key1', config.VEONONSTOP_API_BASE),
        (config.VEONONSTOP_API_KEY_2, config.VEONONSTOP_MAX_PARALLEL_2, config.VEONONSTOP_KEY2_NAME or 'Key2', config.VEONONSTOP_API_BASE_2 or config.VEONONSTOP_API_BASE),
        (config.VEONONSTOP_API_KEY_3, config.VEONONSTOP_MAX_PARALLEL_3, config.VEONONSTOP_KEY3_NAME or 'Key3', config.VEONONSTOP_API_BASE_3 or config.VEONONSTOP_API_BASE),
        (config.VEONONSTOP_API_KEY_4, config.VEONONSTOP_MAX_PARALLEL_4, config.VEONONSTOP_KEY4_NAME or 'Key4', config.VEONONSTOP_API_BASE_4 or config.VEONONSTOP_API_BASE),
    ]
    _active_keys = active_keys if active_keys else [1]

    # Подавить DEBUG-спам
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('requests').setLevel(logging.WARNING)

    # Лог-файл (skip if path too long for Windows MAX_PATH)
    log_ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    gen_log_path = project_path / f'imggen_{log_ts}.log'
    file_handler = None
    try:
        file_handler = logging.FileHandler(str(gen_log_path), encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter('%(asctime)s | %(levelname)-8s | %(message)s'))
        logging.getLogger().addHandler(file_handler)
    except OSError:
        logging.warning(f"Cannot create log file (path too long?): {gen_log_path}")
    logging.info(f"=== VeoNonStop imggen started for {project_path.name} (mode={mode}) ===")

    print("\n" + "=" * 60)
    print(f"  IMGGEN: VeoNonStop ({mode.upper()})")
    print("=" * 60)
    print(f"  Log: {gen_log_path}")

    # ------------------------------------------------------------------
    # 1. Чтение промптов
    # ------------------------------------------------------------------
    prompts_file = find_prompts_file(str(project_path))
    if not prompts_file:
        raise FileNotFoundError(
            f"Prompts file not found in {project_path} "
            f"(expected *_prompts_*.txt)"
        )

    _mp_clips_for_prompts = []
    try:
        _mp_path_for_prompts = project_path / 'montage_plan.json'
        if _mp_path_for_prompts.exists():
            import json as _json
            _mp_for_prompts = _json.loads(_mp_path_for_prompts.read_text(encoding='utf-8-sig'))
            _mp_clips_for_prompts = _mp_for_prompts.get('clips', [])
    except Exception as _e:
        logging.warning(f"Could not load montage plan for prompt ID remap: {_e}")

    all_prompts = extract_all_prompts(prompts_file)
    if _mp_clips_for_prompts:
        all_prompts, _remapped = remap_dense_prompts_for_montage(all_prompts, _mp_clips_for_prompts)
        if _remapped:
            print("  [Prompts] Remapped dense prompt file to real montage clip IDs")
    if not all_prompts:
        raise ValueError(f"No prompts found in {prompts_file}")

    print(f"\n  Prompts file: {Path(prompts_file).name}")
    print(f"  Total prompts: {len(all_prompts)}")

    # Фильтрация по ID
    if clip_ids:
        all_prompts = {k: v for k, v in all_prompts.items() if k in clip_ids}
        print(f"  Selected clips: {len(all_prompts)}")

    # Chain heads only filter — генерим картинки только для head клипов цепочек,
    # extend клипы возьмут last_frame из mp4 предыдущего клипа цепочки.
    # Экономит ~85% imggen в типичном chain mode проекте.
    if (not include_infographics
            and getattr(config, 'STORY_MODE', 'off') == 'chain'
            and getattr(config, 'CHAIN_IMGGEN_HEADS_ONLY', True)):
        _mp_path = project_path / 'montage_plan.json'
        if _mp_path.exists():
            try:
                import json as _json
                _mp = _json.loads(_mp_path.read_text(encoding='utf-8-sig'))
                _mp_clips = _mp.get('clips', [])
                if _mp_clips:
                    from itertools import groupby as _groupby
                    _head_ids = set()
                    for _, _grp in _groupby(_mp_clips, key=lambda c: c.get('chain_id', c['id'])):
                        _chain = list(_grp)
                        if _chain:
                            _head_ids.add(_chain[0]['id'])
                    _before = len(all_prompts)
                    _filtered = {k: v for k, v in all_prompts.items() if k in _head_ids}
                    if _filtered:
                        all_prompts = _filtered
                        print(f"  {_YELLOW}[CHAIN HEADS ONLY]{_RESET} Filtered {_before} → {len(all_prompts)} clips (chain heads only)")
                        logging.info(f"Chain heads filter: {_before} → {len(all_prompts)}")
                    else:
                        logging.warning("Chain heads filter would empty the prompt list — falling back to all clips")
            except Exception as _e:
                logging.warning(f"Could not apply chain heads filter: {_e} — generating all clips")

    # Real Photo skip filter: clips with `source: real_photo` or
    # `real_photo_pending` get their image from archival download (not AI gen).
    # Real_photo writes directly to generated/{id}.png in parallel with this stage.
    try:
        _mp_path = project_path / 'montage_plan.json'
        if _mp_path.exists():
            import json as _json
            _mp = _json.loads(_mp_path.read_text(encoding='utf-8-sig'))
            _mp_clips = _mp.get('montage_plan', _mp).get('clips', [])
            from units.source_ledger import EXTERNAL_SOURCES, INFOGRAPHIC_SOURCES
            _external_ids = {c['id'] for c in _mp_clips
                             if c.get('source') in EXTERNAL_SOURCES}
            _infographic_ids = {c['id'] for c in _mp_clips
                                if c.get('source') in INFOGRAPHIC_SOURCES}
            _skip_ids = _external_ids if include_infographics else (_external_ids | _infographic_ids)
            if _skip_ids:
                _before = len(all_prompts)
                all_prompts = {k: v for k, v in all_prompts.items() if k not in _skip_ids}
                _label = 'external sources' if include_infographics else 'external sources + infographics'
                print(f"  [Source filter] Skipping {len(_skip_ids)} {_label} "
                      f"({_before} -> {len(all_prompts)})")
    except Exception as _e:
        logging.warning(f"Real Photo filter failed: {_e}")

    if not all_prompts:
        raise ValueError("No prompts to generate after filtering")

    # ------------------------------------------------------------------
    # 2. Подготовка
    # ------------------------------------------------------------------
    output_dir = project_path / 'generated'
    output_dir.mkdir(exist_ok=True)

    def _generate_claimed(**kwargs):
        clip_id = int(kwargs['clip_id'])
        if not use_claims:
            return _generate_single_image(**kwargs)
        claim = acquire_image_claim(output_dir, clip_id, owner=claim_owner)
        if claim is None:
            return {
                'id': clip_id,
                'status': 'claimed_elsewhere',
                'file': None,
                'task_id': None,
                'duration_sec': 0,
            }
        try:
            return _generate_single_image(**kwargs)
        finally:
            claim.release()

    # Чей клиент: явный провайдер вызова или провайдер основных картинок из окружения
    _provider = (client_provider or os.environ.get('BEGO_IMAGE_PROVIDER', '') or 'veononstop').strip().lower()
    _client_opts = dict(client_options or {})

    # Vertex, CheapVibeCode и VARAG генерируют картинки только по тексту: режим с рефами сводим к обычному
    if _provider in ('vertex', 'cheapvibe', 'varag') and mode in ('banana_ref', 'banana2_ref'):
        logging.info("Свой клиент картинок: референсы не используются — режим %s заменён на banana", mode)
        mode = 'banana'

    # Загрузка style-reference для banana_ref / banana2_ref
    ref_images = None
    if mode in ('banana_ref', 'banana2_ref'):
        effective_ref_dir = ref_dir or str(project_path / 'ref')
        ref_images = _load_ref_images(effective_ref_dir)
        if not ref_images:
            raise FileNotFoundError(
                f"Mode is {mode} but no reference images found in: {effective_ref_dir}\n"
                f"Add images to project/ref/ or switch to banana/imagen mode."
            )
        else:
            print(f"  Style references: {len(ref_images)} images from {effective_ref_dir}")

    # Пропуск уже существующих (.png, .jpeg, .jpg, .webp)
    skipped = []
    for cid in list(all_prompts.keys()):
        found = False
        for ext in ('.png', '.jpeg', '.jpg', '.webp'):
            p = output_dir / f"{cid:03d}{ext}"
            if p.exists() and p.stat().st_size > 0:
                found = True
                break
        if found:
            skipped.append(cid)
            del all_prompts[cid]

    if skipped:
        print(f"  Skipped (already exist): {len(skipped)}, remaining: {len(all_prompts)}")

    if not all_prompts:
        print("  All images already exist. Nothing to generate.")
        return {'generated': len(skipped), 'failed': 0, 'results': []}

    # ------------------------------------------------------------------
    # 3. Инициализация клиента + сброс зомби-тасков
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Multi-key init: loop over active_keys, create client per key
    # ------------------------------------------------------------------
    def _init_clients(active_keys: List[int]) -> List[Dict[str, Any]]:
        """Собрать клиентов по номерам слотов: cancel-all зомби, план, семафор.

        Вынесено в функцию, потому что набор ключей меняется на ходу: когда
        дневная квота слота кончается, мы пересобираем клиентов на живой слот.
        """
        cfgs: List[Dict[str, Any]] = []
        any_cancelled = False
        # Провайдер картинок — Vertex AI, CheapVibeCode или VARAG: один клиент, ключи VeoNonStop не нужны.
        from units.vertex_client import build_vertex_cfg
        from units.cheapvibe_client import build_cheapvibe_cfg
        from units.varag_client import build_varag_cfg
        if _provider == 'vertex':
            cfgs = build_vertex_cfg('image', image_model=_client_opts.get('model'))
            active_keys = []
        elif _provider == 'cheapvibe':
            cfgs = build_cheapvibe_cfg(model=_client_opts.get('model'), size=_client_opts.get('size'),
                                       quality=_client_opts.get('quality'))
            active_keys = []
        elif _provider == 'varag':
            cfgs = build_varag_cfg('image')
            active_keys = []
        for idx in active_keys:
            key, mp_cfg, label, base_url = _KEY_DEFS[idx - 1]
            if not key:
                continue
            if _requested_parallel > 0:
                mp_cfg = _requested_parallel        # потолок — лимит ключа (concurrent_tasks ниже)
            c = VeoNonStopClient(api_key=key, base_url=base_url)
            print(f"  {_YELLOW}[CLEANUP]{_RESET} [{label}] Cancelling zombie tasks...")
            cancelled = c.cancel_all_tasks()
            if cancelled > 0:
                print(f"  {_YELLOW}[CLEANUP]{_RESET} [{label}] Cancelled {cancelled} zombie task(s).")
                logging.info(f"Startup cancel-all {label}: {cancelled} tasks cancelled")
                any_cancelled = True
            else:
                print(f"  {_YELLOW}[CLEANUP]{_RESET} [{label}] No active tasks found.")
            try:
                acc = c.get_account_info()
                ct = acc.get('concurrent_tasks', mp_cfg)
                mp = min(mp_cfg, ct)
                print(f"  [{label}] Plan: {acc.get('plan', '?')} | concurrent_tasks: {ct} | using: {mp}")
                logging.info(f"Account {label}: plan={acc.get('plan')}, concurrent_tasks={ct}, mp={mp}")
            except Exception as e:
                mp = mp_cfg
                logging.warning(f"Could not get account info ({label}): {e}")
            sem = threading.Semaphore(max(1, mp))  # Full concurrent capacity (matches veo_video.py)
            cfgs.append({'client': c, 'mp': mp, 'sem': sem, 'label': label, 'slot': idx})

        if any_cancelled:
            print(f"  {_YELLOW}[CLEANUP]{_RESET} Waiting 3s for server to release slots...")
            time.sleep(3)

        if not cfgs:
            raise ValueError("None of the specified VeoNonStop keys are configured in .env")

        _active_client[0] = cfgs[0]['client']
        _active_clients.clear()
        _active_clients.extend(cfg['client'] for cfg in cfgs)

        for cfg in cfgs:
            print(f"  [{cfg['label']}] {cfg['mp']} workers | semaphore: {cfg['sem']._value} slots")
            # Per-key 5xx tracker: cancel-all when 5+ workers hit 5xx on same key
            cfg['5xx_count'] = 0
            cfg['5xx_cancelled'] = False
        return cfgs

    # Ctrl+C handler (signal only works in main thread)
    _shutdown_event.clear()
    clients_cfg = _init_clients(_active_keys)
    _old_sigint = None
    if threading.current_thread() is threading.main_thread():
        _old_sigint = signal.signal(signal.SIGINT, _sigint_handler)

    max_parallel = sum(cfg['mp'] for cfg in clients_cfg)
    _5xx_key_lock = threading.Lock()
    _5XX_CANCEL_THRESHOLD = 5

    print(f"\n  Mode: {mode.upper()}")
    print(f"  Aspect ratio: {aspect_ratio or config.VEONONSTOP_ASPECT_RATIO}")
    print(f"  Parallel workers: {max_parallel}")
    if upscale_resolution:
        _ups_label = '4K' if '4K' in upscale_resolution else '2K'
        print(f"  Image upscale: {_ups_label}")
    print("=" * 60)

    # ------------------------------------------------------------------
    # 3b. Предполётная проверка рефов для banana_ref (Preflight)
    # ------------------------------------------------------------------
    if mode in ('banana_ref', 'banana2_ref') and ref_images and preflight and config.FASTGEN_PREFLIGHT_REFS and config.REMIX_AVAILABLE:
        import tempfile, os as _os

        _pf_aspect = aspect_ratio or config.VEONONSTOP_ASPECT_RATIO
        # Берём реальный промпт из проекта — он точнее ловит блокировки, чем абстрактный тест
        _pf_prompt = next(iter(all_prompts.values()), "abstract cinematic background, no people")
        _pf_test_num = [0]   # счётчик тестов для прогресса

        _PF_NET_KEYWORDS = ['connection', 'reset', 'aborted', 'timeout', 'refused', 'network', 'socket', 'recaptcha']
        _PF_RATELIMIT_KEYWORDS = ['429', 'rate limit', 'too many requests']

        def _is_network_error(err_str: str) -> bool:
            low = err_str.lower()
            return any(kw in low for kw in _PF_NET_KEYWORDS)

        def _is_ratelimit_error(err_str: str) -> bool:
            low = err_str.lower()
            return any(kw in low for kw in _PF_RATELIMIT_KEYWORDS)

        def _banana_ref_ok(refs_subset, label='', _retry_on_net=5, _retry_on_ratelimit=3):
            """
            Тест подмножества рефов. True=OK, False=цензура.
            Сетевые ошибки (ConnectionReset, timeout и т.п.) ретраим до _retry_on_net раз.
            Rate limit (429) ретраим до _retry_on_ratelimit раз — если все исчерпаны,
            шлём рефы воркерам без проверки (return True).
            """
            if not refs_subset:
                return True
            _pf_test_num[0] += 1
            names = ', '.join(r.get('name', '?') for r in refs_subset)
            tag = f"[{_pf_test_num[0]}]" if not label else f"[{_pf_test_num[0]}] {label}"
            desc = f"  [PREFLIGHT] {tag} {len(refs_subset)} ref(s)"

            net_errors = []
            _ratelimit_attempts = 0

            for attempt in range(1, _retry_on_net + 2):  # +2: initial + retries
                _exc: list = [None]
                _t0 = [time.time()]

                try:
                    with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
                        tmp_path = tmp.name
                except Exception:
                    return True

                _pf_client = clients_cfg[0]['client']

                def _run():
                    try:
                        _pf_client.generate_banana(
                            _pf_prompt,
                            output_path=tmp_path,
                            aspect_ratio=_pf_aspect,
                            reference_images=refs_subset,
                        )
                    except Exception as e:
                        _exc[0] = e
                    finally:
                        if _os.path.exists(tmp_path):
                            try:
                                _os.unlink(tmp_path)
                            except Exception:
                                pass

                retry_label = desc if attempt == 1 else f"{desc} [retry {attempt - 1}/{_retry_on_net}]"
                t = threading.Thread(target=_run, daemon=True)
                t.start()
                _last_hb = time.time()
                print(f"  [PREFLIGHT] {tag} running... (timeout 300s)", flush=True)
                with tqdm(total=150, desc=retry_label, unit="s",
                          bar_format="{desc}: {bar}| {n:.0f}/{total}s", leave=False) as pbar:
                    while t.is_alive():
                        # Hard Stop / shutdown — прервать preflight немедленно
                        if _shutdown_event.is_set():
                            logging.warning("PREFLIGHT: shutdown signal — aborting")
                            print(f"  [PREFLIGHT] {tag}: aborted by shutdown signal", flush=True)
                            return True  # не блокируем дальнейший пайплайн
                        time.sleep(1)
                        pbar.update(1)
                        # Heartbeat-print каждые 15с: tqdm использует \r, LogRedirector в GUI
                        # буферизует до \n. Без этого GUI выглядит зависшим на 5 минут.
                        if time.time() - _last_hb >= 15:
                            _el = int(time.time() - _t0[0])
                            print(f"  [PREFLIGHT] {tag} still running... ({_el}s elapsed)", flush=True)
                            _last_hb = time.time()
                t.join()

                elapsed = int(time.time() - _t0[0])

                if _exc[0] is None:
                    print(f"  {_GREEN}[PREFLIGHT]{_RESET} {tag} {len(refs_subset)} ref(s): {names}  → OK ({elapsed}s)", flush=True)
                    logging.info(f"PREFLIGHT [{tag}]: OK — {names}")
                    return True

                err_str = str(_exc[0])
                blocked = _is_censorship_error(err_str)

                # Если уже видели 429 — сервер перегружен, не доверяем "invalid argument" как цензуре
                if blocked and _ratelimit_attempts > 0:
                    print(f"  {_YELLOW}[PREFLIGHT]{_RESET} {tag}: blocked-like error after rate limit — assuming server overload, skipping preflight", flush=True)
                    logging.warning(f"PREFLIGHT [{tag}]: blocked-like after rate limit ({_exc[0]}) — assuming OK (server overload)")
                    return True

                if blocked:
                    print(f"  {_RED}[PREFLIGHT]{_RESET} {tag} {len(refs_subset)} ref(s): {names}  → BLOCKED ({elapsed}s)", flush=True)
                    logging.warning(f"PREFLIGHT [{tag}]: BLOCKED — {_exc[0]}")
                    return False  # censorship confirmed, no retry

                if _is_network_error(err_str):
                    net_errors.append(err_str)
                    print(f"  {_YELLOW}[PREFLIGHT]{_RESET} {tag}: network error (attempt {attempt}) — {err_str[:80]}", flush=True)
                    logging.warning(f"PREFLIGHT [{tag}] attempt {attempt}: network error — {_exc[0]}")
                    if attempt <= _retry_on_net:
                        wait = 15 * attempt
                        print(f"  {_YELLOW}[PREFLIGHT]{_RESET} Waiting {wait}s before retry...", flush=True)
                        # Interruptible sleep — проверяем shutdown каждую секунду
                        for _ in range(wait):
                            if _shutdown_event.is_set():
                                print(f"  [PREFLIGHT] {tag}: aborted during retry wait", flush=True)
                                return True
                            time.sleep(1)
                        continue
                    # All retries exhausted — proceed with warning, cannot determine ref safety
                    logging.warning(
                        f"PREFLIGHT [{tag}]: all {_retry_on_net + 1} network retries exhausted — "
                        "assuming OK, cannot confirm ref safety (network issue)"
                    )
                    print(
                        f"  {_YELLOW}[PREFLIGHT]{_RESET} {tag}: network retries exhausted — "
                        f"assuming OK (cannot confirm due to connection issues)", flush=True
                    )
                    return True

                # Rate limit (429) — retry up to _retry_on_ratelimit times, then assume OK
                if _is_ratelimit_error(err_str):
                    _ratelimit_attempts += 1
                    print(f"  {_YELLOW}[PREFLIGHT]{_RESET} {tag}: rate limit 429 (attempt {_ratelimit_attempts}/{_retry_on_ratelimit}) — {err_str[:60]}", flush=True)
                    logging.warning(f"PREFLIGHT [{tag}] rate limit attempt {_ratelimit_attempts}: {_exc[0]}")
                    if _ratelimit_attempts < _retry_on_ratelimit:
                        wait = 30 * _ratelimit_attempts
                        print(f"  {_YELLOW}[PREFLIGHT]{_RESET} Waiting {wait}s before retry...", flush=True)
                        for _ in range(wait):
                            if _shutdown_event.is_set():
                                print(f"  [PREFLIGHT] {tag}: aborted during rate-limit wait", flush=True)
                                return True
                            time.sleep(1)
                        continue
                    # All rate limit retries exhausted — proceed without preflight
                    print(f"  {_YELLOW}[PREFLIGHT]{_RESET} {tag}: rate limit retries exhausted — sending refs to workers without preflight check", flush=True)
                    logging.warning(f"PREFLIGHT [{tag}]: rate limit exhausted after {_ratelimit_attempts} retries — assuming OK, skipping preflight")
                    return True

                # Other non-censorship, non-network error
                print(f"  {_YELLOW}[PREFLIGHT]{_RESET} {tag} {len(refs_subset)} ref(s): {names}  → error ({elapsed}s): {err_str[:80]}", flush=True)
                logging.warning(f"PREFLIGHT [{tag}]: non-censorship error — {_exc[0]}")
                if len(refs_subset) == 1:
                    # Single ref with any non-network 500 → treat as blocked;
                    # API sometimes returns truncated error instead of full "invalid argument"
                    logging.warning(f"PREFLIGHT [{tag}]: single ref non-network error — treating as BLOCKED for safety")
                    return False
                return True

        print(f"\n{'─'*60}")
        print(f"  {_YELLOW}[PREFLIGHT]{_RESET} Testing {len(ref_images)} banana_ref reference(s)...")
        print(f"{'─'*60}", flush=True)
        logging.info(f"PREFLIGHT banana_ref: testing {len(ref_images)} refs")

        if not _banana_ref_ok(ref_images, label='ALL'):
            print(f"\n  {_RED}[PREFLIGHT]{_RESET} BLOCKED — binary search for bad ref(s)...")
            logging.warning("PREFLIGHT banana_ref: BLOCKED — isolating via binary search")

            def _isolate_banana(refs_list, offset=0):
                if len(refs_list) == 0:
                    return []
                if len(refs_list) == 1:
                    lbl = f"single: {refs_list[0].get('name', '?')}"
                    return [offset] if not _banana_ref_ok(refs_list, label=lbl) else []
                mid = len(refs_list) // 2
                bad = []
                if not _banana_ref_ok(refs_list[:mid], label=f"left {offset+1}–{offset+mid}"):
                    bad.extend(_isolate_banana(refs_list[:mid], offset))
                if not _banana_ref_ok(refs_list[mid:], label=f"right {offset+mid+1}–{offset+len(refs_list)}"):
                    bad.extend(_isolate_banana(refs_list[mid:], offset + mid))
                return bad

            # Ремикс через remix engine (REMIX_ENGINE) — до 2 раундов: бинарный поиск → ремикс → ретест
            from ref_remixer import remix_ref
            effective_ref_dir_path = Path(effective_ref_dir)

            for _remix_round in range(1, 3):
                _rnd_sfx = f" (round {_remix_round})" if _remix_round > 1 else ""
                bad_idxs = _isolate_banana(ref_images)
                bad_names = [ref_images[i].get('name', f'ref_{i}') for i in bad_idxs]
                print(f"\n  {_RED}[PREFLIGHT]{_RESET} Bad refs identified{_rnd_sfx}: {bad_names}")
                logging.warning(f"PREFLIGHT banana_ref{_rnd_sfx}: bad refs: {bad_names}")

                if not bad_idxs:
                    # Binary search found no bad refs — initial ALL failure was transient (reCAPTCHA, fluke).
                    print(f"  {_GREEN}[PREFLIGHT]{_RESET} No bad refs found individually — transient error. Continuing with original refs.")
                    logging.info("PREFLIGHT banana_ref: bad_idxs empty — transient error in ALL test, continuing")
                    break

                remixed_any = False
                for idx in bad_idxs:
                    ref_name = ref_images[idx].get('name', '')
                    orig_file = None
                    for ext in ('.jpg', '.jpeg', '.png', '.webp'):
                        candidate = effective_ref_dir_path / (ref_name + ext)
                        if candidate.exists():
                            orig_file = candidate
                            break
                    if orig_file is None:
                        logging.warning(f"PREFLIGHT: could not find file for ref '{ref_name}'")
                        print(f"  {_YELLOW}[PREFLIGHT]{_RESET} File not found for ref '{ref_name}' — skipping remix")
                        continue
                    try:
                        remix_ref(str(orig_file), str(orig_file))
                        remixed_any = True
                        logging.info(f"PREFLIGHT banana_ref{_rnd_sfx}: remixed {orig_file.name}")
                    except Exception as e:
                        logging.error(f"PREFLIGHT banana_ref{_rnd_sfx}: remix failed for {orig_file.name}: {e}")
                        print(f"  {_RED}[PREFLIGHT]{_RESET} Remix failed for {orig_file.name}: {e}")

                if not remixed_any:
                    logging.error("PREFLIGHT banana_ref: no refs could be remixed (remix engine unavailable?) — stopping")
                    raise RuntimeError(
                        "PREFLIGHT FAILED: reference images are blocked and remix engine is unavailable.\n"
                        "Generation stopped — without refs the character will not match.\n"
                        "Fix: check the remixer (REMIX_ENGINE) settings, replace blocked ref images, or switch to banana/imagen mode."
                    )

                retest_label = f"ALL (remixed r{_remix_round})"
                print(f"\n  {_YELLOW}[PREFLIGHT]{_RESET} Re-testing with remixed refs{_rnd_sfx}...")
                ref_images = _load_ref_images(effective_ref_dir)
                if _banana_ref_ok(ref_images, label=retest_label):
                    print(f"  {_GREEN}[PREFLIGHT]{_RESET} OK after remix{_rnd_sfx} — continuing with remixed refs")
                    logging.info(f"PREFLIGHT banana_ref: OK after remix{_rnd_sfx}")
                    break
                elif _remix_round < 2:
                    logging.warning(f"PREFLIGHT: still blocked after round {_remix_round} — starting round {_remix_round + 1} binary search")
                    print(f"\n  {_RED}[PREFLIGHT]{_RESET} Still blocked after round {_remix_round} — starting round {_remix_round + 1} binary search...")
                else:
                    logging.error("PREFLIGHT banana_ref: still blocked after 2 remix rounds — stopping")
                    raise RuntimeError(
                        "PREFLIGHT FAILED: reference images are blocked by censorship even after 2 remix rounds.\n"
                        "Generation stopped — without refs the character will not match.\n"
                        "Fix: replace blocked ref images manually, or switch to banana/imagen mode."
                    )
        else:
            pass  # OK — уже распечатано в _banana_ref_ok

        print(f"{'─'*60}\n", flush=True)

    # ------------------------------------------------------------------
    # 4. Генерация с retry-loop
    # ------------------------------------------------------------------
    accumulated_ok: Dict[int, Dict] = {}
    pending_prompts = dict(all_prompts)
    t2v_fallback_required = False
    t2v_fallback_reason = ''

    cycle_num = 0
    start_total = time.time()

    # Слоты, у которых дневная квота уже кончилась в этом прогоне.
    _quota_dead_slots: set = set()

    # Shared running-average stats across all cycles (thread-safe)
    _ok_run = {'n': 0, 'total': 0.0, 'lock': threading.Lock(), 'wall_start': time.time()}

    def _fastgen_has_quota() -> bool:
        """Есть ли у FastGen часовая квота прямо сейчас (для решения о фолбэке)."""
        try:
            # units.fastgen_engine, НЕ плоский fastgen: плоский импорт даёт второй
            # экземпляр модуля со своими модульными состояниями (в т.ч. стоп-событием),
            # и Stop из GUI такой экземпляр не гасит.
            from units.fastgen_engine import FastGenClient, parse_usage_remaining
            _fg_key = str(getattr(config, 'FASTGEN_API_KEY', '') or '').strip()
            if not _fg_key:
                return False
            _rem = parse_usage_remaining(FastGenClient(api_key=_fg_key).get_usage())
        except Exception as exc:
            logging.warning(f"[FG CHECK] FastGen usage request failed — {exc}")
            print(f"  {_YELLOW}[FG CHECK]{_RESET} FastGen usage request failed "
                  f"({str(exc)[:100]}) — assuming it can try")
            return True   # не смогли спросить — пусть FastGen сам решит на живом запросе
        print(f"  [FG CHECK] FastGen hourly quota: {'?' if _rem is None else _rem}")
        return _rem is None or _rem > 0

    def _delegate_to_fastgen(reason: str) -> int:
        """Отдать ВЕСЬ остаток FastGen одним вызовом (провайдер-уровневый переход).

        allow_veo_fallback=False: наши ключи VeoNonStop уже мертвы, звать их
        обратно из FastGen нельзя. Кулдаун по часовой квоте FastGen при этом
        остаётся его заботой — он умеет ждать границу часа сам.
        """
        _ids = sorted(pending_prompts.keys())
        if not _ids:
            return 0
        print(f"\n  {_YELLOW}[FASTGEN FALLBACK]{_RESET} {reason} — handing "
              f"{len(_ids)} clip(s) to FastGen (full auto-chain)")
        logging.warning(f"Delegating {len(_ids)} clips to FastGen: {reason}")
        if mode in ('banana_ref', 'banana2_ref'):
            print(f"  {_YELLOW}[FASTGEN FALLBACK]{_RESET} {mode}: FastGen will auto-load "
                  f"refs from project/ref/")
        try:
            from units.fastgen_engine import generate_all_images as _fastgen_generate
            _fastgen_generate(
                project_dir=str(project_path),
                clip_ids=_ids,
                silent=True,
                allow_veo_fallback=False,
            )
        except Exception as exc:
            logging.exception(f"FastGen fallback failed: {exc}")
            print(f"  {_RED}[FASTGEN FALLBACK]{_RESET} failed: {str(exc)[:200]}")

        # Источник правды — диск: FastGen писал файлы сам.
        _moved = 0
        for cid in list(_ids):
            for ext in ('.png', '.jpeg', '.jpg', '.webp'):
                p = output_dir / f"{cid:03d}{ext}"
                try:
                    _exists = p.exists() and p.stat().st_size > 0
                except OSError:
                    _exists = False
                if _exists:
                    accumulated_ok[cid] = {
                        'id': cid, 'status': 'ok', 'file': str(p), 'error': None,
                        'duration_sec': 0, 'task_id': None, 'block_count': 0,
                        'fallback_provider': 'fastgen',
                    }
                    pending_prompts.pop(cid, None)
                    _moved += 1
                    break
        print(f"  {_GREEN}[FASTGEN FALLBACK]{_RESET} FastGen generated {_moved}/{len(_ids)} "
              f"clip(s); still missing: {len(pending_prompts)}")
        logging.warning(f"FastGen fallback result: {_moved}/{len(_ids)} generated, "
                        f"{len(pending_prompts)} still missing")
        return _moved

    while True:
        cycle_num += 1
        cycle_ids = sorted(pending_prompts.keys())
        _quota_stop_event.clear()
        print(f"\n  [Cycle {cycle_num}] Generating {len(cycle_ids)} images...")

        # Reset worker map и 5xx counters для нового цикла
        with _imggen_worker_lock:
            _imggen_worker_map.clear()
        for _cfg in clients_cfg:
            _cfg['5xx_count'] = 0
            _cfg['5xx_cancelled'] = False
            _imggen_worker_task_count.clear()
            _imggen_worker_counter[0] = 0

        # Семафор на одновременные banana-запросы (single-key: глобальный; multi-key: per-cfg)
        global _banana_submit_sem
        _multi_key = len(clients_cfg) > 1
        if not _multi_key and mode in ('banana', 'banana_ref', 'banana2', 'banana2_ref'):
            _banana_submit_sem = clients_cfg[0]['sem']
        else:
            _banana_submit_sem = None

        _clients_cfg_arg = clients_cfg if _multi_key else None

        cycle_results: List[Dict] = []

        with ThreadPoolExecutor(max_workers=max_parallel) as executor:
            futures = {}
            for cid in cycle_ids:
                _clip_prompt = pending_prompts[cid]
                _clip_refs = _filter_refs_by_prompt_tokens(_clip_prompt, ref_images)
                future = executor.submit(
                    _generate_claimed,
                    client=clients_cfg[0]['client'],
                    clip_id=cid,
                    prompt=_clip_prompt,
                    mode=mode,
                    output_dir=output_dir,
                    aspect_ratio=aspect_ratio,
                    ref_images=_clip_refs,
                    clients_cfg=_clients_cfg_arg,
                    ok_run=_ok_run,
                    upscale_resolution=upscale_resolution,
                    banana_model_key=banana_model_key,
                )
                futures[future] = cid

            # Спиннер пока воркеры загружают рефы и стартуют (до первого completed)
            _spinner_stop = threading.Event()
            _is_tty = sys.stdout.isatty()
            label = "  [Uploading refs to workers]" if mode == 'banana_ref' else "  [Submitting tasks to workers]"
            if not _is_tty:
                print(f"{label}...", flush=True)
            def _run_spinner():
                frames = ['|', '/', '-', '\\']
                i = 0
                while not _spinner_stop.is_set():
                    sys.stdout.write(f"\r{label} {frames[i % 4]}  ")
                    sys.stdout.flush()
                    i += 1
                    time.sleep(0.15)
                sys.stdout.write("\r" + " " * 60 + "\r")
                sys.stdout.flush()
            spinner_thread = threading.Thread(target=_run_spinner, daemon=True)
            if _is_tty:
                spinner_thread.start()

            _cycle_ok_durs = []
            _cycle_total = len(futures)
            _cycle_done = 0
            _cycle_ok = 0
            if IS_PIPE:
                print(f"  Cycle {cycle_num}: 0/{_cycle_total} imgs", flush=True)
            _tqdm_ctx = tqdm(total=_cycle_total, desc=f"  Cycle {cycle_num}", unit="img", smoothing=0) if not IS_PIPE else None
            pending = dict(futures)  # future -> cid
            try:
                while pending and not _shutdown_event.is_set():
                    done, _ = concurrent.futures.wait(
                        list(pending.keys()),
                        timeout=1.0,
                        return_when=concurrent.futures.FIRST_COMPLETED,
                    )
                    for future in done:
                        cid = pending.pop(future)
                        _spinner_stop.set()
                        try:
                            clip_result = future.result()
                            cycle_results.append(clip_result)
                            icon = {
                                'ok': '+',
                                'claimed_elsewhere': '=',
                            }.get(clip_result['status'], 'X')
                            dur = clip_result.get('duration_sec', 0)
                            if clip_result['status'] == 'ok' and dur > 0:
                                _cycle_ok_durs.append(dur)
                            if _tqdm_ctx:
                                avg_s = f" avg={sum(_cycle_ok_durs)/len(_cycle_ok_durs):.0f}s" if _cycle_ok_durs else ""
                                _tqdm_ctx.set_postfix_str(f"#{cid}: {icon}{avg_s}")
                        except Exception as e:
                            clip_result = {
                                'id': cid, 'status': 'error', 'error': str(e),
                                'file': None, 'task_id': None, 'duration_sec': 0,
                            }
                            cycle_results.append(clip_result)
                            if _tqdm_ctx:
                                _tqdm_ctx.set_postfix_str(f"#{cid}: X")
                        _cycle_done += 1
                        if clip_result.get('status') == 'ok':
                            _cycle_ok += 1
                        if _tqdm_ctx:
                            _tqdm_ctx.update(1)
                        if IS_PIPE:
                            print(f"PROGRESS:{_cycle_ok}/{_cycle_total}", flush=True)
            except KeyboardInterrupt:
                _shutdown_event.set()
                print(f"\n\n  [CANCELLING] Ctrl+C — sending cancel-all to server...", flush=True)
                for _cc in _active_clients:
                    try:
                        cancelled = _cc.cancel_all_tasks()
                        print(f"  [CANCELLING] {cancelled} task(s) cancelled on server", flush=True)
                    except Exception as e:
                        print(f"  [CANCELLING] cancel-all failed: {e}", flush=True)
                print(f"  [CANCELLING] Stopping workers...\n", flush=True)
            if _shutdown_event.is_set():
                for f in pending:
                    f.cancel()
            if _tqdm_ctx:
                _tqdm_ctx.close()

        if _shutdown_event.is_set():
            break

        # Разбор результатов
        for r in cycle_results:
            if r['status'] in ('ok', 'claimed_elsewhere'):
                if r['status'] == 'ok':
                    accumulated_ok[r['id']] = r
                pending_prompts.pop(r['id'], None)

        cycle_ok = sum(1 for r in cycle_results if r['status'] == 'ok')
        failed_ids = sorted(pending_prompts.keys())

        print(f"\n  [Cycle {cycle_num}] OK: {cycle_ok},  still missing: {len(failed_ids)}")
        if failed_ids:
            _fids = ', '.join(f'#{fid}' for fid in failed_ids[:20])
            if len(failed_ids) > 20:
                _fids += f' ... (+{len(failed_ids)-20})'
            print(f"  Failed clips: {_fids}")

        if not failed_ids:
            break

        # ── Терминальная нехватка ёмкости — решение уровня ПРОВАЙДЕРА ─────────
        # Два случая, и оба уже отработали свои локальные средства:
        #   quota_exhausted      — дневная квота НАШЕГО ключа (429). Лечится
        #                          запасным ключом или другим провайдером.
        #   all_models_exhausted — клип перебрал ВСЕ banana-модели, у сервиса нет
        #                          cookies ни под одну (смена ключа не поможет).
        # Ретраить и докручивать циклы в обоих случаях бессмысленно.
        _key_quota_dead = any(r.get('quota_exhausted') for r in cycle_results)
        _all_pools_dead = any(r.get('all_models_exhausted') for r in cycle_results)
        if _key_quota_dead or _all_pools_dead:
            if _key_quota_dead:
                _quota_dead_slots.update(int(c['slot']) for c in clients_cfg)
                print(f"\n  {_RED}[DAILY QUOTA]{_RESET} VeoNonStop image quota exhausted "
                      f"(slots: {sorted(_quota_dead_slots)}) with {len(failed_ids)} clip(s) left")
                logging.warning(f"VeoNonStop daily image quota exhausted on slots "
                                f"{sorted(_quota_dead_slots)}; {len(failed_ids)} clips left")
            else:
                print(f"\n  {_RED}[MODEL POOL]{_RESET} all banana model pools exhausted "
                      f"with {len(failed_ids)} clip(s) left — switching provider")
                logging.warning(f"All banana model pools exhausted; {len(failed_ids)} clips left")

            # Запасной ключ ищем ТОЛЬКО когда кончилась квота ключа. Если пусты пулы
            # cookies под модели — они общие у сервиса, другой ключ упрётся в то же.
            _live_veo = []
            if _key_quota_dead:
                try:
                    from units import veo_accounts as _vau
                    _live_veo = _vau.live_image_slots(
                        config, exclude_slots=tuple(_quota_dead_slots))
                except Exception as exc:
                    logging.warning(f"VeoNonStop spare-key probe failed: {exc}")
                    print(f"  {_YELLOW}[VEO CHECK]{_RESET} spare-key probe failed: "
                          f"{str(exc)[:160]}")

            if _live_veo:
                _new_keys = [s['slot'] for s in _live_veo]
                _slot_text = ', '.join(f"{s['name']}({s['remaining']})" for s in _live_veo)
                print(f"  {_GREEN}[VEO SWITCH]{_RESET} spare key(s) with real quota: {_slot_text} "
                      f"— rebuilding clients and continuing on VeoNonStop")
                logging.warning(f"VeoNonStop switching to spare slots {_new_keys}")
                try:
                    clients_cfg = _init_clients(_new_keys)
                    _active_keys = _new_keys
                    max_parallel = sum(cfg['mp'] for cfg in clients_cfg)
                    continue
                except Exception as exc:
                    logging.warning(f"VeoNonStop spare-key init failed: {exc}")
                    print(f"  {_YELLOW}[VEO SWITCH]{_RESET} spare key init failed "
                          f"({str(exc)[:120]}) — falling back to FastGen")

            _reason = ('VeoNonStop daily image quota exhausted' if _key_quota_dead
                       else 'all VeoNonStop banana model pools exhausted')
            if _veo_no_capacity_should_fallback_to_t2v():
                t2v_fallback_required = True
                t2v_fallback_reason = _reason
                print(
                    f"  {_YELLOW}[T2V FALLBACK]{_RESET} {_reason}; "
                    f"{len(failed_ids)} remaining clip(s) will use T2V"
                )
                logging.warning(
                    "%s; requesting T2V fallback (%s)",
                    _reason,
                    "no FastGen key" if not _fastgen_key_configured()
                    else "GUI policy",
                )
                break
            if not _veo_fastgen_fallback_enabled():
                print(f"  {_YELLOW}[NO CAPACITY]{_RESET} {_reason}; FastGen fallback disabled "
                      f"in settings — stopping with {len(failed_ids)} missing")
                logging.error(f"{_reason}: FastGen fallback disabled "
                              f"(VEONONSTOP_QUOTA_FALLBACK_FASTGEN=off)")
                break

            print(f"  {_YELLOW}[NO CAPACITY]{_RESET} {_reason} — switching provider to FastGen")
            _fastgen_has_quota()      # печатаем состояние часовой квоты в лог
            _delegate_to_fastgen(_reason)
            if not pending_prompts:
                break
            # FastGen тоже не смог (его собственный кулдаун уже отработал внутри) —
            # дальше крутить VeoNonStop нечем: дневная квота/пулы вернутся завтра.
            print(f"  {_RED}[NO CAPACITY]{_RESET} {len(pending_prompts)} clip(s) still missing "
                  f"after FastGen; VeoNonStop capacity returns tomorrow")
            logging.error(f"After FastGen fallback {len(pending_prompts)} clips still missing; "
                          f"VeoNonStop capacity resets tomorrow")
            break

        # ── Silent mode: авто-ретрай ──────────────────────────────
        if silent:
            if max_cycles is not None:
                # Limited-cycles mode (used as fallback from fastgen.py — no nested FastGen fallback)
                if cycle_num < max_cycles:
                    print(f"\n  {_YELLOW}[SILENT]{_RESET} {len(failed_ids)} images failed, auto-retry ({cycle_num + 1}/{max_cycles})...")
                    logging.info(f"Silent max_cycles={max_cycles}: {len(failed_ids)} failed after cycle {cycle_num}, retrying")
                    continue
                else:
                    print(f"\n  {_YELLOW}[SILENT]{_RESET} {len(failed_ids)} images failed after {cycle_num} cycle(s).")
                    logging.info(f"Silent max_cycles={max_cycles}: stopping after {cycle_num} cycle(s), {len(failed_ids)} still failed")
                    break
            elif cycle_num < config.VEONONSTOP_MAX_SILENT_CYCLES:
                # Авто-ретрай: до N циклов VeoNonStop перед фоллбэком на FastGen
                _sc = config.VEONONSTOP_MAX_SILENT_CYCLES
                print(f"\n  {_YELLOW}[SILENT]{_RESET} {len(failed_ids)} images failed, auto-retrying (cycle {cycle_num + 1}/{_sc})...")
                logging.info(f"Silent mode: {len(failed_ids)} failed after cycle {cycle_num}, auto-retrying ({cycle_num + 1}/{_sc})")
                continue
            else:
                # После 3 циклов VeoNonStop — фоллбэк на FastGen.
                # FastGen auto-scans project/ref/ через scan_references() → рефы подхватываются автоматически,
                # персонаж сохраняется даже в banana_ref режиме.
                print(f"\n  {_YELLOW}[SILENT]{_RESET} {len(failed_ids)} images still failed after {cycle_num} cycles.")
                if mode in ('banana_ref', 'banana2_ref'):
                    print(f"  {_YELLOW}[SILENT]{_RESET} {mode} mode: FastGen will auto-load refs from project/ref/")
                    logging.info(f"Silent mode {mode}: falling back to FastGen — refs will be picked up from project/ref/")
                print(f"  {_YELLOW}[SILENT]{_RESET} Falling back to FastGen for remaining {len(failed_ids)} images...")
                logging.info(f"Silent mode: {len(failed_ids)} failed after {cycle_num} cycles, falling back to FastGen")

                try:
                    # units.fastgen_engine — тот же экземпляр, что видит GUI (стоп-событие,
                    # реестр активных клиентов). Плоский `import fastgen` заводил второй.
                    from units.fastgen_engine import generate_all_images as fastgen_generate

                    print(f"\n  {_YELLOW}[FASTGEN FALLBACK]{_RESET} Delegating {len(failed_ids)} clips to FastGen (full auto-chain)...")
                    logging.info(f"Delegating {len(failed_ids)} clips to FastGen with full fallback chain (silent=True)")

                    fg_result = fastgen_generate(
                        project_dir=str(project_path),
                        clip_ids=list(failed_ids),
                        silent=True,
                        # Мы — вызывающая сторона: FastGen не должен звать наш
                        # imggen обратно при своей квоте (кольцо провайдеров).
                        allow_veo_fallback=False,
                    )

                    # Sync results — check files on disk (most reliable)
                    fg_ok = fg_result.get('generated', 0)
                    print(f"  {_GREEN}[FASTGEN FALLBACK]{_RESET} Generated {fg_ok} images")
                    logging.info(f"FastGen fallback done: {fg_ok} generated")

                    for cid in list(pending_prompts.keys()):
                        if any((output_dir / f"{cid:03d}{ext}").exists()
                               for ext in ('.png', '.jpg', '.jpeg', '.webp')):
                            accumulated_ok[cid] = {'id': cid, 'status': 'ok'}
                            pending_prompts.pop(cid, None)

                except Exception as e:
                    print(f"  {_RED}[FASTGEN FALLBACK]{_RESET} FastGen fallback failed: {e}")
                    logging.error(f"FastGen fallback failed: {e}")
                break

        # ── Interactive mode: спрашиваем юзера ────────────────────
        print("\n" + "=" * 60)
        print(f"  {_RED}MISSING IMAGES ({len(failed_ids)} total):{_RESET}")
        for cid in failed_ids:
            last_r = next((r for r in reversed(cycle_results) if r['id'] == cid), None)
            err = (last_r.get('error', '') if last_r else '')[:80]
            prompt_short = pending_prompts.get(cid, '')[:90].replace('\n', ' ')
            print(f"  #{cid:03d}: {prompt_short}")
            if err:
                print(f"         err: {err}")
        print("=" * 60)

        print(f"\n  {_YELLOW}What would you like to do?{_RESET}")
        print(f"    [1] Retry missing images ({len(failed_ids)})")
        print(f"    [2] Finish — skip failed")
        print(f"    [3] Edit prompt manually and retry")
        print()

        while True:
            choice = input("  Select [1/2/3]: ").strip()
            if choice in ('1', '2', '3'):
                break
            print("  Enter 1, 2 or 3")

        if choice == '2':
            failed_ids = []
            break

        if choice == '3':
            # Ручной ввод промпта для каждого пропущенного клипа
            for cid in list(failed_ids):
                old_prompt = pending_prompts.get(cid, '')
                print(f"\n  {_YELLOW}--- Clip #{cid:03d} ---{_RESET}")
                print(f"  Current prompt:")
                print(f"    {old_prompt[:200]}")
                print()
                new_text = input("  New prompt (Enter = keep current, 's' = skip this clip): ").strip()
                if new_text.lower() == 's':
                    failed_ids.remove(cid)
                    pending_prompts.pop(cid, None)
                    print(f"  #{cid:03d} skipped")
                elif new_text:
                    pending_prompts[cid] = new_text
                    print(f"  #{cid:03d} prompt updated")
                else:
                    print(f"  #{cid:03d} keeping current prompt")

        if not failed_ids:
            break

    # ------------------------------------------------------------------
    # 5. Итоги
    # ------------------------------------------------------------------
    elapsed_total = round(time.time() - start_total, 1)
    total_ok = len(accumulated_ok)
    total_failed = len(pending_prompts)

    print("\n" + "=" * 60)
    print(f"  IMGGEN COMPLETE")
    print(f"  Generated: {total_ok}")
    print(f"  Failed:    {total_failed}")
    print(f"  Skipped:   {len(skipped)}")
    print(f"  Total time: {elapsed_total}s")
    print(f"  Output: {output_dir}")
    print("=" * 60)

    logging.info(f"=== imggen complete: {total_ok} ok, {total_failed} failed, {elapsed_total}s ===")

    logging.getLogger().removeHandler(file_handler)
    file_handler.close()

    if threading.current_thread() is threading.main_thread() and _old_sigint is not None:
        signal.signal(signal.SIGINT, _old_sigint)
    _active_client[0] = None
    _active_clients.clear()

    return {
        'generated': total_ok,
        'failed': total_failed,
        'failed_ids': sorted(pending_prompts.keys()),
        'skipped': len(skipped),
        'elapsed_sec': elapsed_total,
        'results': list(accumulated_ok.values()),
        't2v_fallback_required': t2v_fallback_required,
        't2v_fallback_reason': t2v_fallback_reason,
    }


# ======================================================================
# CLI
# ======================================================================

