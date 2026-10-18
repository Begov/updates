"""
VeoNonStop veo_video — batch-генерация видео (этап 3.5 пайплайна)

Режимы:
  i2v        — image-to-video: оживление картинок из generated/
  t2v        — text-to-video: видео прямо из промптов (без картинок)
  components — multi-image-to-video: видео из промптов + референсы из ref/

Результат: vid_img/{id:03d}.mp4  (далее video_concat ретаймит по montage_plan)
"""

import os
import sys
import json
import time
import random
import logging
import shutil
import subprocess

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
import threading
import requests
from pathlib import Path
from typing import Optional, Dict, List, Any
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED, as_completed

# Ensure agent/ and agent/modules/ dirs are on sys.path
_agent_dir = str(Path(__file__).resolve().parent.parent)
_modules_dir = str(Path(__file__).resolve().parent)
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)
if _modules_dir not in sys.path:
    sys.path.insert(0, _modules_dir)

from tqdm import tqdm
import colorama
from ref_remixer import remix_ref, remix_i2v
from motion_claims import acquire_clip_claim, clip_media_exists, clip_media_path
from colorama import Fore, Style

colorama.init(autoreset=True)

# Force flush on every print — prevents buffered stdout from hiding errors in pipe/subprocess
import functools as _ft
print = _ft.partial(print, flush=True)  # noqa: A001

_YELLOW = Fore.YELLOW
_RED    = Fore.RED
_GREEN  = Fore.GREEN
_CYAN   = Fore.CYAN
_RESET  = Style.RESET_ALL

# ======================================================================
# Soft-start (защита от одновременного старта всех воркеров) — УДАЛЕНО
# ======================================================================
# Теперь как у юзера (2.py) — отправляем задачи сразу без stagger
_HUNG_TASK_TIMEOUT = 300  # секунд в session_init/uploading до cancel + retry

_started_workers: set = set()   # воркеры, уже прошедшие первый стаггер
_started_workers_lock = threading.Lock()


# ======================================================================
# Graceful shutdown (Ctrl+C)
# ======================================================================
# Флаг: воркеры проверяют перед каждым submit/poll
_shutdown_requested = threading.Event()
# Текущие task_id в полёте — для cancel через API
_inflight_tasks: Dict[int, str] = {}  # clip_id -> task_id
_inflight_lock = threading.Lock()
# Путь к active_tasks.json — устанавливается в generate_all_videos()
_active_tasks_path: Optional[Path] = None
# Submit semaphore: ограничивает одновременные POST-запросы (submit), poll/download без ограничений
_submit_semaphore: Optional[threading.Semaphore] = None
_video_download_semaphore: Optional[threading.Semaphore] = None
# Active clients list for external stop (Control Panel Stop button)
_active_clients: list = []
# Текущий max_parallel — для slot-wait при slots_full retry
_max_parallel_global: int = 0
# Per-key inflight: label -> set of clip_ids (для dual-key slot-wait по каждому ключу отдельно)
_inflight_by_key: Dict[str, set] = {}
_inflight_by_key_lock = threading.Lock()
# Per-key max_parallel: label -> int
_max_parallel_by_key: Dict[str, int] = {}

# Per-key submit rate limiter: метка ключа -> ts следующего разрешённого submit'а
# Гарантирует минимальный gap между sem.acquire()'ами чтобы куки на сервере
# успевали cleanup между submit'ами (защита от slots_full при синхронных завершениях)
_last_acquire_ts_by_key: Dict[str, float] = {}
_last_acquire_lock = threading.Lock()
_MIN_SUBMIT_GAP_SEC = 2.0  # минимальный интервал между submit'ами на один ключ
_server_submit_gate_locks: Dict[str, threading.Lock] = {}
_server_submit_gate_locks_guard = threading.Lock()
_SERVER_USAGE_POLL_SEC = 5.0
_SERVER_USAGE_WAIT_TIMEOUT_SEC = 180.0


def _download_video_limited(client, task_id: str, output_path: str, context: str = '') -> None:
    """Download a video through the shared relay bandwidth limiter."""
    if _video_download_semaphore is None:
        client.download_video(task_id, output_path)
        return

    wait_started = time.time()
    _video_download_semaphore.acquire()
    try:
        waited = round(time.time() - wait_started, 1)
        if waited >= 1.0:
            logging.info(
                "Video download slot waited %.1fs%s",
                waited,
                f" ({context})" if context else '',
            )
        client.download_video(task_id, output_path)
    finally:
        _video_download_semaphore.release()


# ======================================================================
# CHAIN MODE HELPERS (seamless video continuation)
# ======================================================================

def _save_active_tasks() -> None:
    """Записывает _inflight_tasks в active_tasks.json. Вызывать под _inflight_lock."""
    if _active_tasks_path is None:
        return
    try:
        _active_tasks_path.write_text(
            json.dumps(_inflight_tasks, ensure_ascii=False), encoding='utf-8'
        )
    except Exception:
        pass

import params as config
from prompt_extract import extract_all_prompts, find_prompts_file, remap_dense_prompts_for_montage
from veo_client import (
    VeoNonStopClient,
    VeoNonStopError,
    VeoNonStopTimeoutError,
    VeoNonStopGenerationError,
    VeoNonStopRateLimitError,
    VeoNonStopHungError,
    IMAGE_EXTENSIONS,
    _load_ref_images,
)
from fastgen_engine import FastGenClient


# ======================================================================
# Классификация ошибок
# ======================================================================

_SLOTS_FULL_PATTERNS = [
    'cookie slots full',
    'slots full',
    '0 cookies (limit:',              # "Allocated 0 new + 0 existing = 0 cookies (limit: 6)"
    'allocated 0 new + 0 existing',   # более точный паттерн того же формата
]


def _is_slots_full_error(err_lower: str) -> bool:
    """Детектит все варианты 'все куки заняты' ошибок VeoNonStop."""
    return any(p in err_lower for p in _SLOTS_FULL_PATTERNS)


_ERROR_PATTERNS = [
    ('invalid_argument', ['invalid argument']),
    ('status_failed',    ['media_generation_status_failed']),
    ('recaptcha',        ['recaptcha', 'forbidden']),
    ('slots_full',       _SLOTS_FULL_PATTERNS),
    ('ratelimit',        ['rate limit', '429']),
    ('timeout',          ['timeout', 'timed out']),
    ('censored',         ['censor', 'policy', 'safety']),
    ('person',           ['person', 'prominent']),
]


def _count_error(error_counts: dict, error_msg: str) -> str:
    """
    Классифицирует ошибку по тексту и инкрементирует счётчик.
    Returns: ключ категории
    """
    msg_lower = error_msg.lower()
    for key, patterns in _ERROR_PATTERNS:
        if any(p in msg_lower for p in patterns):
            error_counts[key] = error_counts.get(key, 0) + 1
            return key
    # Неизвестная ошибка — первые 30 символов
    short = error_msg[:30].replace(' ', '_').lower()
    error_counts[short] = error_counts.get(short, 0) + 1
    return short


# ======================================================================
# Детекторы цензуры VeoNonStop
# ======================================================================

def _is_content_policy_error(error_msg: str) -> bool:
    """Цензура по содержанию промпта (насилие, нагота и т.п.)"""
    msg = error_msg.lower()
    return any(p in msg for p in [
        'content policy', 'policy violation', 'safety filter',
        'blocked by safety', 'harmful content', 'unsafe content',
        'violates', 'not allowed', 'prohibited', 'moderation',
        # VeoNonStop API error codes
        'public_error_danger_filter',
        'public_error_unsafe_generation',
        'public_error_ip_input_image',
        'public_error_sexual',
        'public_error_child',
        'public_error_minor',
        # NOTE: public_error_audio_filtered EXCLUDED — audio issue, not prompt issue; just retry
    ])


def _is_generation_timeout_error(error_msg: str) -> bool:
    """Google finished the task with a terminal video-generation timeout."""
    if not error_msg:
        return False
    msg = error_msg.lower()
    return (
        'public_error_video_generation_timed_out' in msg
        or 'video_generation_timed_out' in msg
        or 'video generation timed out' in msg
    )


# Google-side error reasons → targeted image-remix hints (skip Vision, Google already classified)
_GOOGLE_REMIX_HINTS = {
    'PUBLIC_ERROR_MINOR_UPLOAD':    "age the subject to clearly adult 35-50 years old, mature facial features, visible crow's feet around eyes, slight grey hairs, adult body proportions, no youthful or childlike features",
    'PUBLIC_ERROR_CHILD':           "age the subject to clearly adult 35-50 years old, mature facial features, visible crow's feet around eyes, slight grey hairs, adult body proportions, no youthful or childlike features",
    'PUBLIC_ERROR_SEXUAL':          "remove any suggestive poses, revealing or tight clothing; fully clothed in neutral modest attire (long sleeves, closed collar), no skin exposure beyond face and hands",
    'PUBLIC_ERROR_DANGER_FILTER':   "remove any weapons, violence, blood, or dangerous actions; neutral peaceful composition with no harmful elements",
    'PUBLIC_ERROR_UNSAFE_GENERATION':"remove any weapons, violence, blood, or dangerous actions; neutral peaceful composition with no harmful elements",
    'PUBLIC_ERROR_IP_INPUT_IMAGE':  "remove any visible trademarks, brand logos, copyrighted characters, or recognizable commercial designs; replace with generic equivalents",
    # NOTE: PUBLIC_ERROR_AUDIO_FILTERED removed — audio issue, not prompt/image issue; simple retry is sufficient
}

# Google-side reasons that signal rate-limit / anti-abuse (NO remix, just cooldown)
_GOOGLE_COOLDOWN_REASONS = {
    'PUBLIC_ERROR_UNUSUAL_ACTIVITY',
    'PUBLIC_ERROR_UNUSUAL_ACTIVITY_TOO_MUCH_TRAFFIC',
    'PUBLIC_ERROR_RATELIMIT',
}


def _extract_google_error_reason(error_msg: str) -> Optional[str]:
    """Вытащить 'reason': 'PUBLIC_ERROR_*' из raw backend ответа.
    Backend возвращает текст с dict'ом Google error; парсим reason по regex.
    """
    if not error_msg:
        return None
    import re
    m = re.search(r"['\"]reason['\"]\s*:\s*['\"](PUBLIC_ERROR_[A-Z_]+)['\"]", error_msg)
    return m.group(1) if m else None


def _is_desk_transient_error(error_msg: str) -> bool:
    """Транзиентные сетевые/бекенд ошибки: desk в cooldown, axios timeout, TCP сброшен.
    Требуют паузы (backend восстанавливается), не ретрайя сразу, не remix'а."""
    if not error_msg:
        return False
    msg = error_msg.lower()
    return any(p in msg for p in [
        'timeout of 30000ms exceeded',  # axios inside desk backend
        'timeout of 60000ms exceeded',
        'read econnreset',               # TCP reset (proxy/desk dropped)
        'econnreset',
        'socket hang up',
        'etimedout',
        'network error',
    ])


def _is_prominent_person_error(error_msg: str) -> bool:
    """Цензура из-за упоминания известной личности по имени"""
    msg = error_msg.lower()
    return any(p in msg for p in [
        'prominent people', 'well-known individuals', 'known individuals',
        'celebrity', 'public figure', 'real person', 'named individual',
        'person by name',
        # VeoNonStop / Google API error codes
        'prominent_people_filter',
        'prominent_people_upload',
        'public_error_prominent_people',
    ])


def _rewrite_blocked_prompt(original_prompt: str, for_person: bool = False) -> str:
    """
    Rewrite a blocked prompt via the shared rewriter (REWRITE_MODEL).
    for_person arg kept for call-site compatibility but ignored — unified prompt handles both cases.
    Instruction loaded from agent/prompts/rewrite_prompt.txt (user-editable).
    """
    from prompt_softener import rewrite_prompt as _gr
    return _gr(original_prompt)


# ======================================================================
# Генерация одного клипа
# ======================================================================

class ShutdownRequested(Exception):
    """Raised when graceful shutdown is requested (Ctrl+C)."""
    pass


def _submit_and_download(
    client: VeoNonStopClient,
    clip_id: int,
    prompt: str,
    mode: str,
    image_path: Optional[str],
    ref_dir: Optional[str],
    output_path: Path,
    aspect_ratio: Optional[str],
    end_image_path: Optional[str] = None,
    submit_sem=None,
    key_label: str = 'Key1',
    phase_label: str = 'phase1',
    scene_seconds: Optional[float] = None,
) -> tuple:
    """
    Один цикл submit → poll → download.
    Возвращает (task_id, t_upload_sec, t_gen_sec, t_dl_sec).
    Бросает исключения при любой ошибке.

    `scene_seconds` — длина сцены из монтажного плана; по ней выбирается
    `duration` (4/5/6/8 с): v1 — только i2v, v2 — все режимы. None — параметр не отправляется.
    """
    import random

    # Проверка shutdown ПЕРЕД ожиданием слота
    if _shutdown_requested.is_set():
        raise ShutdownRequested(f"Clip #{clip_id}: shutdown before submit")

    try:
        # Микроджиттер: минимальная разброска для избежания thundering herd
        jitter = random.uniform(0.05, 0.1)
        time.sleep(jitter)

        # Gen-slot semaphore: N задач макс на сервере одновременно (held from submit до gen done)
        _sem = submit_sem if submit_sem is not None else _submit_semaphore
        _sem_released = False
        if _sem is not None:
            _sem.acquire()

        # Serialize usage check + submit per key. This prevents a burst of workers
        # from all seeing the same free server slot and pushing tasks into queue.
        _gate_lock = _get_server_submit_gate_lock(key_label)
        _gate_lock.acquire()
        _gate_released = False

        _wait_for_server_capacity_before_submit(
            client=client,
            clip_id=clip_id,
            tag=f"[{key_label}]",
            phase_label='pre_submit',
            key_label=key_label,
        )

        # Rate limiter: минимум _MIN_SUBMIT_GAP_SEC между submit'ами на один ключ.
        # Защита от slots_full при синхронных завершениях — когда 5 задач одновременно
        # release sem, ждущие воркеры иначе стартуют все за ~10ms, бьют сервер серией
        # submit'ов на куки ещё не освободившиеся от cleanup.
        with _last_acquire_lock:
            _now_ts = time.time()
            _last_ts = _last_acquire_ts_by_key.get(key_label, 0.0)
            _wait_until = max(_now_ts, _last_ts + _MIN_SUBMIT_GAP_SEC)
            _last_acquire_ts_by_key[key_label] = _wait_until   # reserve slot
        _gap = _wait_until - time.time()
        if _gap > 0:
            if not _shutdown_requested.is_set():
                time.sleep(_gap)
        if _shutdown_requested.is_set():
            if not _gate_released:
                _gate_lock.release()
                _gate_released = True
            if _sem is not None and not _sem_released:
                _sem.release()
                _sem_released = True
            raise ShutdownRequested(f"Clip #{clip_id}: shutdown before submit")

        try:
            t_upload_start = time.time()
            # Длительность под сцену — когда включено в интерфейсе. Клиент сам
            # решает, отправлять ли её: на v1 её знает только image-to-video.
            # Правило выбора — `duration_for_scene` (ближайшее сверху из
            # 4/5/6/8).
            _clip_duration = None
            if scene_seconds and _custom_duration_enabled():
                from units.veo_client import duration_for_scene
                _clip_duration = duration_for_scene(scene_seconds)
                # `key_label` — параметр ЭТОЙ функции; `tag` живёт в
                # `_process_single_clip` и сюда не передаётся (поймано аудитом
                # исполнения 01.09.2026 — вторая ошибка того же рода, что и
                # `scene_seconds`).
                logging.info(
                    f"Clip #{clip_id} [{key_label}] [{phase_label}]: "
                    f"сцена {scene_seconds:.1f}s → duration={_clip_duration}")
            if mode == 'i2v':
                if not image_path or not Path(image_path).exists():
                    raise FileNotFoundError(f"Image not found: {image_path}")
                task_id = client.submit_image_to_video(
                    image_path=image_path,
                    prompt=prompt,
                    aspect_ratio=aspect_ratio,
                    duration=_clip_duration,
                )
            elif mode == 't2v':
                task_id = client.submit_text_to_video(
                    prompt=prompt,
                    aspect_ratio=aspect_ratio,
                    duration=_clip_duration,
                )
            elif mode == 'components':
                if not ref_dir:
                    raise ValueError("ref_dir required for components mode")
                task_id = client.submit_multi_image_to_video_from_ref_dir(
                    ref_dir=ref_dir,
                    prompt=prompt,
                    aspect_ratio=aspect_ratio,
                )
            elif mode == 'batch_frame':
                if not image_path or not Path(image_path).exists():
                    raise FileNotFoundError(f"Start image not found: {image_path}")
                # end_image fallback: если конца нет — используем start (zoom/motion effect)
                end_path = end_image_path if (end_image_path and Path(end_image_path).exists()) else image_path
                task_id = client.submit_batch_frame(
                    start_image_path=image_path,
                    end_image_path=end_path,
                    prompt=prompt,
                    aspect_ratio=aspect_ratio,
                    duration=_clip_duration,
                )
            else:
                raise ValueError(f"Unknown mode: {mode}")
            t_upload = round(time.time() - t_upload_start, 1)
        except:
            if not _gate_released:
                _gate_lock.release()
                _gate_released = True
            # Submit failed — release sem immediately so next worker can proceed
            if _sem is not None and not _sem_released:
                _sem.release()
                _sem_released = True
            raise
        if not _gate_released:
            _gate_lock.release()
            _gate_released = True

        logging.info(f"Clip #{clip_id}: submitted ({mode}) -> taskId={task_id}")

        # Регистрируем task_id для возможного cancel + per-key inflight
        with _inflight_lock:
            _inflight_tasks[clip_id] = task_id
            _save_active_tasks()
        with _inflight_by_key_lock:
            if key_label not in _inflight_by_key:
                _inflight_by_key[key_label] = set()
            _inflight_by_key[key_label].add(clip_id)

        try:
            # Проверка shutdown ПОСЛЕ submit (можем ещё cancel)
            if _shutdown_requested.is_set():
                client.cancel_task(task_id)
                raise ShutdownRequested(f"Clip #{clip_id}: shutdown after submit, task cancelled")

            t_gen_start = time.time()
            try:
                _poll_data = client.poll_video_status(task_id, hung_timeout=_HUNG_TASK_TIMEOUT)
            except VeoNonStopHungError:
                # Таск завис в session_init/uploading >2мин — отменяем и бросаем выше
                logging.warning(f"Clip #{clip_id}: task {task_id} hung >{_HUNG_TASK_TIMEOUT}s, cancelling")
                client.cancel_task(task_id)
                raise
            t_gen = round(time.time() - t_gen_start, 1)

            # Gen done — release server slot, download runs independently.
            # Защита от slots_full теперь через _last_acquire_ts_by_key rate limiter
            # на стороне acquire, а не sleep перед release.
            if _sem is not None and not _sem_released:
                _sem.release()
                _sem_released = True

            # Extract mediaGenerationId for video upscale
            # Рядом лежит и ССЫЛКА на готовый файл — её берём для апскейла на
            # API v2: замер 01.09.2026 дал 50с против 71с без неё. Ссылка
            # подписанная и живёт недолго, поэтому годится только пока клип
            # свежий — апскейл идёт сразу после генерации, это как раз тот
            # случай (просроченная даёт NOT_FOUND).
            _media_gen_id = None
            _media_url = None
            if _poll_data and isinstance(_poll_data, dict):
                _vids = _poll_data.get('videos', [])
                if _vids and isinstance(_vids, list):
                    _media_gen_id = _vids[0].get('mediaGenerationId')
                    _media_url = (_vids[0].get('fifeUrl')
                                  or _vids[0].get('url')
                                  or _vids[0].get('video_url'))
                if not _media_gen_id:
                    _media_gen_id = _poll_data.get('mediaGenerationId')

            # Проверка shutdown ПОСЛЕ poll (видео готово — скачиваем)
            if _shutdown_requested.is_set():
                logging.info(f"Clip #{clip_id}: shutdown requested but video ready, downloading...")

            t_dl_start = time.time()
            _download_video_limited(
                client, task_id, str(output_path), context=f"clip #{clip_id} {phase_label}"
            )
            t_dl = round(time.time() - t_dl_start, 1)

            return task_id, t_upload, t_gen, t_dl, _media_gen_id, _media_url
        finally:
            # Safety: release sem if still held (e.g. exception during gen)
            if _sem is not None and not _sem_released:
                _sem.release()
                _sem_released = True
            with _inflight_lock:
                _inflight_tasks.pop(clip_id, None)
                _save_active_tasks()
            with _inflight_by_key_lock:
                _inflight_by_key.get(key_label, set()).discard(clip_id)
    except Exception:
        raise


def _wait_for_slot(clip_id: int, tag: str, phase_label: str, count: int, key_label: str = '', client=None) -> None:
    """Поллит /account/usage пока есть свободный cookie-slot.

    Главный счётчик — `cookies_allocated` (по доке VeoNonStop: cookies × 4 tasks each).
    `active+queued` оставлен как fallback на случай если сервер не отдаёт cookies_allocated
    (например старая версия API).

    Объяснение бага который это чинит:
    active=0 queued=0 НЕ значит что cookie-pool свободен. После failed/completed task
    cookie ещё ~15-50s держится сервером в allocated-состоянии. Submit получит slotsfull
    хотя active+queued=0. Поэтому ждём по cookies_allocated.
    """
    if key_label and key_label in _max_parallel_by_key:
        mp = _max_parallel_by_key[key_label]
    else:
        mp = _max_parallel_global
    if mp <= 0:
        time.sleep(15)
        return
    t_start = time.time()
    deadline = t_start + 120
    _server_active = '?'
    _server_queued = '?'
    _server_max = mp
    _server_cookies_allocated = '?'
    _limit = max(1, int(mp or 1))
    # По доке: max_concurrent_tasks = cookies × 4. У 12-плана → 3 cookies, у VIP-24 → 6.
    _cookies_total = max(1, _limit // 4) if _limit >= 4 else 1
    while time.time() < deadline:
        if _shutdown_requested.is_set():
            return
        try:
            _d = client.get_account_usage() if hasattr(client, 'get_account_usage') else {}
            _server_active = int(_d.get('active_tasks', 0) or 0)
            _server_queued = int(_d.get('queued_tasks', 0) or 0)
            _server_max = int(_d.get('max_concurrent_tasks', _limit) or _limit)
            # cookies_allocated может отсутствовать на старом API → None → fallback на active+queued
            _ca_raw = _d.get('cookies_allocated', None)
            _server_cookies_allocated = int(_ca_raw) if _ca_raw is not None else '?'
            # /account/usage may report a conservative/temporary max_concurrent_tasks=1
            # even when /account/info says the key has many slots. The local per-key
            # cap was already derived from account/info, so usage is trusted only for
            # active/queued counters here.
            if isinstance(_server_cookies_allocated, int):
                # cookies_allocated есть — это authoritative счётчик для cookie-pool.
                if _server_cookies_allocated < _cookies_total:
                    break
            else:
                # fallback к старой логике для серверов без cookies_allocated
                if _server_active + _server_queued < _limit:
                    break
        except Exception:
            pass
        time.sleep(5)
    waited = round(time.time() - t_start, 1)
    logging.info(
        f"Clip #{clip_id} {tag} [{phase_label}]: slots full ({count}/10), waited {waited:.0f}s, "
        f"active={_server_active}, queued={_server_queued}, cookies={_server_cookies_allocated}/{_cookies_total}, "
        f"limit={_limit}, usage_max={_server_max}"
    )
    if waited > 0:
        print(
            f"\n  {_YELLOW}[WAIT]{_RESET} #{clip_id} {tag}: waited {waited:.0f}s for slot "
            f"(active={_server_active}, queued={_server_queued}, cookies={_server_cookies_allocated}/{_cookies_total}, "
            f"limit={_limit}, usage_max={_server_max})"
        )


def _get_server_submit_gate_lock(key_label: str) -> threading.Lock:
    label = key_label or 'Key1'
    with _server_submit_gate_locks_guard:
        lock = _server_submit_gate_locks.get(label)
        if lock is None:
            lock = threading.Lock()
            _server_submit_gate_locks[label] = lock
        return lock


def _wait_for_server_capacity_before_submit(
    client: VeoNonStopClient,
    clip_id: int,
    tag: str,
    phase_label: str,
    key_label: str,
) -> None:
    """Poll /account/usage before POST so we do not add queued tasks when slots are full."""
    if client is None:
        return
    local_cap = _max_parallel_by_key.get(key_label, _max_parallel_global) if key_label else _max_parallel_global
    local_cap = max(1, int(local_cap or 1))
    # По доке: max_concurrent_tasks = cookies × 4 → cookies_total = local_cap // 4 (минимум 1)
    cookies_total = max(1, local_cap // 4) if local_cap >= 4 else 1
    deadline = time.time() + _SERVER_USAGE_WAIT_TIMEOUT_SEC
    waited_logged = False
    last_active = '?'
    last_queued = '?'
    last_limit = local_cap
    last_usage_max = local_cap
    last_cookies = '?'

    while time.time() < deadline:
        if _shutdown_requested.is_set():
            return
        try:
            usage = client.get_account_usage()
            server_max = int(usage.get('max_concurrent_tasks', local_cap) or local_cap)
            active = int(usage.get('active_tasks', 0) or 0)
            queued = int(usage.get('queued_tasks', 0) or 0)
            _ca_raw = usage.get('cookies_allocated', None)
            cookies_alloc = int(_ca_raw) if _ca_raw is not None else None
            # Do not clamp to usage.max_concurrent_tasks here. Some VeoNonStop
            # usage responses report max_concurrent_tasks=1 while account/info
            # correctly reports a 24-slot VIP key. The per-key local_cap is the
            # authoritative cap already computed from account/info/config; usage
            # is used for active/queued counters only.
            limit = local_cap
            last_active, last_queued, last_limit, last_usage_max = active, queued, limit, server_max
            last_cookies = cookies_alloc if cookies_alloc is not None else '?'
            # 1 cookie = 4 потока, поэтому limit (= cookies × 4) — реальная ёмкость ПОТОКОВ.
            # Гейт по active+queued < limit (потоки), НЕ по cookies_alloc < cookies_total:
            # последнее душило параллельность до ЧИСЛА COOKIE (limit//4 = 3 при limit 12),
            # теряя множитель ×4 → active застревал на 1-2, total клипа 25-50 мин.
            _slot_free = active + queued < limit
            if _slot_free:
                if waited_logged:
                    print(
                        f"\n  {_YELLOW}[SLOT OK]{_RESET} #{clip_id} {tag}: "
                        f"active={active}, queued={queued}, cookies={last_cookies}/{cookies_total}, "
                        f"limit={limit}, usage_max={server_max}"
                    )
                return
            if not waited_logged:
                print(f"\n  {_YELLOW}[WAIT]{_RESET} #{clip_id} {tag}: server slots full before submit "
                      f"(active={active}, queued={queued}, cookies={last_cookies}/{cookies_total}, "
                      f"limit={limit}, usage_max={server_max})")
                waited_logged = True
            logging.info(
                f"Clip #{clip_id} {tag} [{phase_label}]: pre-submit wait "
                f"active={active}, queued={queued}, cookies={last_cookies}/{cookies_total}, "
                f"limit={limit}, usage_max={server_max}"
            )
        except Exception as e:
            logging.warning(f"Clip #{clip_id} {tag} [{phase_label}]: account usage pre-submit unavailable: {e}")
            return
        time.sleep(_SERVER_USAGE_POLL_SEC)

    print(f"\n  {_YELLOW}[WAIT]{_RESET} #{clip_id} {tag}: pre-submit usage wait timeout "
          f"(active={last_active}, queued={last_queued}, limit={last_limit}, usage_max={last_usage_max}); trying submit")


def _run_phase(
    client: VeoNonStopClient,
    clip_id: int,
    prompt: str,
    mode: str,
    image_path: Optional[str],
    ref_dir: Optional[str],
    output_path: Path,
    aspect_ratio: Optional[str],
    max_retries: int,
    phase_label: str,
    worker_id: int = 0,
    clip_start_time: str = '',
    end_image_path: Optional[str] = None,
    submit_sem=None,
    key_label: str = 'Key1',
    clients_cfg_list=None,
    _selected_cfg=None,
    scene_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Одна фаза: до max_retries попыток.
    Returns:
        {
            'ok': True/False,
            'task_id': str (if ok),
            'elapsed': float,           # суммарное время фазы
            'upload_sec': float,        # время POST-запроса (отправка)
            'gen_sec': float,           # время poll (генерация на сервере)
            'dl_sec': float,            # время скачивания
            'retries': int,             # количество ретраев (attempts - 1)
            'retry_time': float,        # время на ретраи
            'error_counts': dict,       # ошибки по типам
            'error': str,               # текст ошибки (если failed)
            'censored': bool,           # если цензура
            'prominent_person': bool,   # если известная личность
        }
    """
    censored = False
    prominent_person = False
    ip_image_blocked = False
    generation_timeout = False
    censorship_streak = 0
    last_error = ''
    remix_hint: Optional[str] = None     # target remix instruction from Google error reason (if any)
    google_reason: Optional[str] = None  # raw Google reason code (PUBLIC_ERROR_*)
    status_failed_count = 0        # счётчик MEDIA_GENERATION_STATUS_FAILED суммарно (не подряд)
    invalid_arg_count = 0          # счётчик "invalid argument" подряд
    slots_full_count = 0           # счётчик slots_full (макс 10, не тратит попытку)
    ratelimit_count = 0            # счётчик ratelimit 429 (макс 10, не тратит попытку)
    recaptcha_retry_count = 0      # счётчик reCAPTCHA per task (resets on resubmit)
    resubmit_count = 0             # счётчик cancel+resubmit (макс config.VEONONSTOP_MAX_RESUBMITS)
    error_counts: Dict[str, int] = {}
    retry_time = 0.0
    total_elapsed = 0.0

    tag = f"[W{worker_id}]"

    attempt = 0
    while attempt < max_retries:
        # (key_dead check moved to _process_single_clip level)
        attempt += 1
        try:
            start = time.time()
            task_id, t_upload, t_gen, t_dl, _media_gen_id, _media_url = _submit_and_download(
                client, clip_id, prompt, mode, image_path, ref_dir, output_path, aspect_ratio,
                end_image_path=end_image_path,
                submit_sem=submit_sem,
                key_label=key_label,
                phase_label=phase_label,
                scene_seconds=scene_seconds,
            )
            elapsed = round(time.time() - start, 1)
            retries = attempt - 1
            return {
                'ok': True, 'task_id': task_id, 'elapsed': elapsed,
                'upload_sec': t_upload, 'gen_sec': t_gen, 'dl_sec': t_dl,
                'retries': retries, 'retry_time': round(retry_time, 1),
                'error_counts': error_counts,
                'error': '', 'censored': False, 'prominent_person': False,
                'ip_image_blocked': False, 'generation_timeout': False,
                'censorship_streak': 0,
                'media_generation_id': _media_gen_id,
                'media_url': _media_url,
            }

        except VeoNonStopRateLimitError:
            if _shutdown_requested.is_set():
                logging.info(f"Clip #{clip_id} {tag} [{phase_label}]: shutdown — exiting ratelimit retry")
                break
            ratelimit_count += 1
            _rl_wait = 10 + random.uniform(1, 5)
            msg = f"#{clip_id} {tag} [{phase_label}] attempt {attempt}: rate limit ({ratelimit_count}/10), waiting {_rl_wait:.1f}s..."
            logging.warning(msg)
            print(f"\n  {_YELLOW}[RATELIMIT]{_RESET} {msg}")
            _count_error(error_counts, 'rate limit 429')
            if ratelimit_count >= 10:
                logging.error(f"Clip #{clip_id} {tag} [{phase_label}]: ratelimit 10x — giving up")
                print(f"\n  {_RED}[RATELIMIT]{_RESET} #{clip_id} {tag}: 10 retries exhausted — failed")
                break
            time.sleep(_rl_wait)
            attempt -= 1  # не считается за попытку

        except VeoNonStopHungError as e:
            if _shutdown_requested.is_set():
                logging.info(f"Clip #{clip_id} {tag} [{phase_label}]: shutdown — exiting hung retry")
                break
            # Таск завис в session_init/uploading >2мин — cancel уже сделан в _submit_and_download
            attempt_elapsed = time.time() - start
            retry_time += attempt_elapsed
            last_error = str(e)
            _count_error(error_counts, 'hung')
            logging.warning(f"Clip #{clip_id} {tag} [{phase_label}] attempt {attempt}/{max_retries}: HUNG — {e}")
            print(f"\n  {_YELLOW}[HUNG]{_RESET}  #{clip_id} {tag} [{phase_label}] attempt {attempt}/{max_retries}: task stuck >2min, cancelled, retrying...")
            # Небольшая пауза перед ретраем
            time.sleep(10)

        except VeoNonStopGenerationError as e:
            if _shutdown_requested.is_set():
                logging.info(f"Clip #{clip_id} {tag} [{phase_label}]: shutdown — exiting generation error retry")
                break
            attempt_elapsed = time.time() - start
            retry_time += attempt_elapsed
            last_error = str(e)
            _count_error(error_counts, last_error)
            err_lower = last_error.lower()
            # Raw backend error — pipeline_full.log + per-run log
            print(f"[RAW_ERROR] Clip #{clip_id} {tag} [{phase_label}]: {last_error[:500]}")
            logging.info(f"Clip #{clip_id} {tag} [{phase_label}] raw_error: {last_error[:500]}")
            # Extract Google reason → targeted remix hint (if applicable)
            _reason = _extract_google_error_reason(last_error)
            if _reason:
                google_reason = _reason
                if _reason in _GOOGLE_REMIX_HINTS:
                    remix_hint = _GOOGLE_REMIX_HINTS[_reason]
                    print(f"  [GOOGLE REASON] #{clip_id} {tag}: {_reason} → targeted remix hint set")
                    logging.info(f"Clip #{clip_id} {tag}: Google reason {_reason} mapped to targeted remix hint")
                elif _reason in _GOOGLE_COOLDOWN_REASONS:
                    # Anti-abuse — cooldown without remix
                    _cd = 60 + random.uniform(0, 30)
                    logging.warning(f"Clip #{clip_id} {tag} [{phase_label}]: Google {_reason} — cooldown {_cd:.0f}s (no remix)")
                    print(f"\n  {_YELLOW}[COOLDOWN]{_RESET} #{clip_id} {tag}: Google {_reason} — waiting {_cd:.0f}s before retry")
                    if not _shutdown_requested.is_set():
                        time.sleep(_cd)
                    attempt -= 1
                    continue
            # Desk-side transient (axios timeout, TCP reset) — pause, don't waste attempts
            if _is_desk_transient_error(last_error):
                _cd = 45 + random.uniform(0, 20)
                logging.warning(f"Clip #{clip_id} {tag} [{phase_label}]: transient desk error — cooldown {_cd:.0f}s")
                print(f"\n  {_YELLOW}[COOLDOWN]{_RESET} #{clip_id} {tag}: transient desk error — waiting {_cd:.0f}s")
                if not _shutdown_requested.is_set():
                    time.sleep(_cd)
                attempt -= 1
                continue

            # reCAPTCHA — 3 retries per task, then cancel + resubmit (fresh task)
            if 'recaptcha' in err_lower:
                recaptcha_retry_count += 1
                if recaptcha_retry_count <= 3:
                    # Retry same task (may still be alive)
                    _rc_delay = getattr(config, 'VEONONSTOP_RECAPTCHA_DELAY', 8.0) + random.uniform(1.0, max(1.0, getattr(config, 'VEONONSTOP_RECAPTCHA_JITTER', 3.0)))
                    logging.warning(f"Clip #{clip_id} {tag} [{phase_label}]: reCAPTCHA retry {recaptcha_retry_count}/3 in {_rc_delay:.1f}s")
                    print(f"\n  {_YELLOW}[RECAPTCHA]{_RESET} #{clip_id} {tag}: retry {recaptcha_retry_count}/3 in {_rc_delay:.1f}s...")
                    time.sleep(_rc_delay)
                    attempt -= 1
                    continue
                # 3 retries exhausted on this task → cancel + resubmit
                resubmit_count += 1
                if resubmit_count > config.VEONONSTOP_MAX_RESUBMITS:
                    logging.error(f"Clip #{clip_id} {tag} [{phase_label}]: reCAPTCHA — {resubmit_count - 1} resubmits exhausted — giving up")
                    print(f"\n  {_RED}[RECAPTCHA]{_RESET} #{clip_id} {tag}: {resubmit_count - 1} resubmits exhausted — failed")
                    break
                _failed_tid = _inflight_tasks.get(clip_id)
                if _failed_tid:
                    try:
                        client.cancel_task(_failed_tid)
                    except Exception:
                        pass
                recaptcha_retry_count = 0  # reset for fresh task
                logging.warning(f"Clip #{clip_id} {tag} [{phase_label}]: reCAPTCHA — cancel + resubmit {resubmit_count}/{config.VEONONSTOP_MAX_RESUBMITS}")
                print(f"\n  {_YELLOW}[RECAPTCHA RESUBMIT]{_RESET} #{clip_id} {tag}: cancel + resubmit {resubmit_count}/{config.VEONONSTOP_MAX_RESUBMITS}")
                time.sleep(4)
                attempt -= 1
                continue

            # Cookie slots full — ждём освобождения реального слота, не тратим попытку, макс 10 раз
            if _is_slots_full_error(err_lower):
                if _shutdown_requested.is_set():
                    break
                slots_full_count += 1
                _count_error(error_counts, 'slots_full')
                if slots_full_count > 10:
                    logging.error(f"Clip #{clip_id} {tag} [{phase_label}]: slots full 10x — giving up")
                    print(f"\n  {_RED}[SLOTS FULL]{_RESET} #{clip_id} {tag}: 10 retries exhausted — failed")
                    break
                # Ждём пока сервер реально освободит слот
                _wait_for_slot(clip_id, tag, phase_label, slots_full_count, key_label=key_label, client=client)
                attempt -= 1  # не тратим попытку
                continue

            # "Cancelled by user (cancel-all)" — внешний cancel_all (новый watcher при старте).
            # Не тратим попытку — задача была жива, просто убита извне.
            if 'cancelled by user' in err_lower:
                if _shutdown_requested.is_set():
                    logging.info(f"Clip #{clip_id} {tag} [{phase_label}]: cancelled by user stop — exiting")
                    break
                logging.warning(f"Clip #{clip_id} {tag} [{phase_label}] attempt {attempt}: cancelled by cancel-all — retrying (not counted)")
                print(f"\n  {_YELLOW}[CANCELLED]{_RESET} #{clip_id} {tag}: task cancelled by cancel-all — retrying...")
                attempt -= 1
                time.sleep(5)
                continue

            # The Google task itself ended with a terminal generation timeout.
            # Leave this phase immediately and let the configured Skipper mode
            # choose static fallback, rewrites, or the full rewrite/remix chain.
            if _is_generation_timeout_error(last_error):
                generation_timeout = True
                censorship_streak += 1
                _count_error(error_counts, 'generation_timeout')
                limit = config.VEONONSTOP_CENSOR_STREAK_LIMIT
                if censorship_streak < limit:
                    logging.warning(
                        f"Clip #{clip_id} {tag} [{phase_label}]: terminal generation timeout "
                        f"({censorship_streak}/{limit}) - possible hidden censorship, retrying"
                    )
                    continue
                censored = True
                logging.warning(
                    f"Clip #{clip_id} {tag} [{phase_label}]: terminal generation timeout "
                    f"— routing to configured Skipper chain"
                )
                print(
                    f"\n  {_RED}[GEN TIMEOUT]{_RESET} #{clip_id} {tag}: "
                    f"server generation timed out — going to Skipper fallback chain"
                )
                break

            # IP filter на картинке. В i2v картинку подаём мы — rewrite промпта
            # бесполезен, нужна новая картинка (ветка ip_image_blocked ниже).
            # В t2v картинки нет вовсе: кадр шлюз рождает из промпта, и правка
            # промпта — ЕДИНСТВЕННЫЙ рычаг. Раньше t2v-клип тут просто умирал:
            # ip_image_blocked не входит в набор censored/person/timeout, поэтому
            # уходил в «обычная техническая ошибка» без единой попытки спасения.
            # Прогон HADES 07.08.2026: 99 клипов из 229 не сделаны, все пять
            # циклов ретраев легли в одну точку.
            if 'ip_input_image' in err_lower:
                if mode == 't2v':
                    censored = True
                    logging.warning(
                        f"Clip #{clip_id} {tag} [{phase_label}]: IP_INPUT_IMAGE (t2v) "
                        f"— кадр шлюза заблокирован, идём на rewrite промпта")
                    print(f"\n  {_RED}[IP IMAGE]{_RESET} #{clip_id} {tag}: "
                          f"frame blocked by IP filter — will rewrite prompt")
                else:
                    ip_image_blocked = True
                    logging.warning(f"Clip #{clip_id} {tag} [{phase_label}]: IP_INPUT_IMAGE — input image blocked by IP filter")
                    print(f"\n  {_RED}[IP IMAGE]{_RESET} #{clip_id} {tag}: input image blocked by IP filter")
                break

            # Проверяем тип цензуры
            if _is_prominent_person_error(last_error):
                prominent_person = True
                logging.warning(f"Clip #{clip_id} {tag} [{phase_label}]: PROMINENT PERSON — will rewrite")
                print(f"\n  {_RED}[PERSON NAME]{_RESET} #{clip_id} {tag}: prompt mentions a famous person — will rewrite with appearance description")
                break  # нет смысла ретраить
            elif _is_content_policy_error(last_error):
                censorship_streak += 1
                limit = config.VEONONSTOP_CENSOR_STREAK_LIMIT
                if censorship_streak < limit:
                    logging.warning(
                        f"Clip #{clip_id} {tag} [{phase_label}]: censorship response "
                        f"({censorship_streak}/{limit}) - confirming with one retry"
                    )
                    continue
                censored = True
                logging.warning(f"Clip #{clip_id} {tag} [{phase_label}] attempt {attempt}: CENSORED — {e}")
                print(f"\n  {_RED}[CENSORED]{_RESET} #{clip_id} {tag} [{phase_label}] attempt {attempt}/{max_retries}")
                break  # нет смысла ретраить с тем же промптом

            # Считаем MEDIA_GENERATION_STATUS_FAILED суммарно (не подряд)
            if 'media_generation_status_failed' in err_lower:
                status_failed_count += 1
                _sf_threshold = max_retries  # порог = max_retries, чтобы не превысить счётчик попыток
                if status_failed_count >= _sf_threshold:
                    censored = True
                    logging.warning(f"Clip #{clip_id} {tag} [{phase_label}]: {status_failed_count}x STATUS_FAILED total — treating as hidden censorship, will rewrite")
                    print(f"\n  {_RED}[HIDDEN CENSOR?]{_RESET} #{clip_id} {tag}: {status_failed_count}x STATUS_FAILED total — treating as censorship, will rewrite prompt")
                    break
                # Паузы: 5с, 10с, 15с
                _sf_delay = status_failed_count * 5
                logging.info(f"Clip #{clip_id} {tag} [{phase_label}]: STATUS_FAILED ({status_failed_count}/{_sf_threshold}) — retrying in {_sf_delay}s")
                print(f"\n  {_YELLOW}[STATUS_FAILED]{_RESET} #{clip_id} {tag}: ({status_failed_count}/{_sf_threshold}) — retrying in {_sf_delay}s...")
                time.sleep(_sf_delay)
                continue

            # "invalid argument" — Google Veo маскирует цензуру реальных людей
            # под этой generic ошибкой. 1 раз = сразу person rewrite
            if 'invalid argument' in err_lower:
                prominent_person = True
                logging.warning(f"Clip #{clip_id} {tag} [{phase_label}]: 'invalid argument' — treating as person censorship, will rewrite")
                print(f"\n  {_RED}[INVALID ARG → PERSON?]{_RESET} #{clip_id} {tag}: 'invalid argument' — likely person name, will rewrite")
                break

            # Server 5xx — don't count as attempt, cancel-all at threshold
            _5xx_kw = ['500', '502', '503', 'server error', 'internal server error',
                       'service unavailable', 'bad gateway']
            if any(kw in err_lower for kw in _5xx_kw):
                attempt -= 1
                _5xx_wait = min(10 + attempt * 5, 30)
                msg = f"#{clip_id} {tag} [{key_label}] [{phase_label}] attempt {attempt}: server 5xx, waiting {_5xx_wait}s..."
                logging.warning(msg)
                print(f"\n  {_YELLOW}[SERVER]{_RESET}  {msg}")
                if clients_cfg_list and _selected_cfg and '5xx_count' in _selected_cfg:
                    _selected_cfg['5xx_count'] = _selected_cfg.get('5xx_count', 0) + 1
                    if _selected_cfg['5xx_count'] >= 5 and not _selected_cfg.get('5xx_cancelled'):
                        _selected_cfg['5xx_cancelled'] = True
                        _lbl = _selected_cfg.get('label', '?')
                        print(f"\n  {_YELLOW}[RECONNECT]{_RESET} [{_lbl}] 5+ workers hit 5xx — cancel-all to force fresh cookies...")
                        logging.warning(f"[RECONNECT] {_lbl}: 5xx threshold reached, cancel-all")
                        try:
                            _selected_cfg['client'].cancel_all_tasks()
                        except Exception as _ce:
                            logging.warning(f"[RECONNECT] {_lbl} cancel-all failed: {_ce}")
                        _selected_cfg['5xx_count'] = 0
                        _selected_cfg['5xx_cancelled'] = False
                time.sleep(_5xx_wait)
                continue

            # Fatal auth errors — no point retrying, poison the key
            _fatal_kw = ['license_expired', 'license expired', 'auth.license_expired',
                         'credits insufficient', 'account suspended']
            if any(kw in err_lower for kw in _fatal_kw):
                logging.error(f"Clip #{clip_id} {tag} [{phase_label}]: FATAL auth error — {e}")
                print(f"\n  {_RED}[AUTH FATAL]{_RESET} #{clip_id} {tag} [{key_label}]: {e}")
                print(f"  {_RED}[AUTH FATAL]{_RESET} Skipping all retries — key is dead")
                if clients_cfg_list and _selected_cfg:
                    _selected_cfg['key_dead'] = True
                    _lbl = _selected_cfg.get('label', '?')
                    print(f"  {_RED}[AUTH FATAL]{_RESET} [{_lbl}] Key poisoned — all workers on this key will skip")
                    logging.error(f"[AUTH FATAL] {_lbl}: key poisoned due to {err_lower[:80]}")
                last_error = str(e)
                break

            logging.error(f"Clip #{clip_id} {tag} [{phase_label}] attempt {attempt}/{max_retries}: generation error — {e}")
            print(f"\n  {_RED}[FAILED]{_RESET}  #{clip_id} {tag} [{phase_label}] {attempt}/{max_retries}: {e}")
            if attempt < max_retries:
                _retry_delays = [5, 5, 10]
                delay = _retry_delays[min(attempt - 1, len(_retry_delays) - 1)]
                time.sleep(delay)

        except VeoNonStopTimeoutError as e:
            if _shutdown_requested.is_set():
                logging.info(f"Clip #{clip_id} {tag} [{phase_label}]: shutdown — exiting timeout retry")
                break
            attempt_elapsed = time.time() - start
            retry_time += attempt_elapsed
            last_error = str(e)
            err_str_to = str(e).lower()
            _count_error(error_counts, last_error)
            # Server error via timeout — same 5xx logic
            if 'server error' in err_str_to:
                attempt -= 1
                _5xx_wait = min(10 + attempt * 5, 30)
                msg = f"#{clip_id} {tag} [{key_label}] [{phase_label}] attempt {attempt}: server 5xx (timeout), waiting {_5xx_wait}s..."
                logging.warning(msg)
                print(f"\n  {_YELLOW}[SERVER]{_RESET}  {msg}")
                if clients_cfg_list and _selected_cfg and '5xx_count' in _selected_cfg:
                    _selected_cfg['5xx_count'] = _selected_cfg.get('5xx_count', 0) + 1
                    if _selected_cfg['5xx_count'] >= 5 and not _selected_cfg.get('5xx_cancelled'):
                        _selected_cfg['5xx_cancelled'] = True
                        _lbl = _selected_cfg.get('label', '?')
                        print(f"\n  {_YELLOW}[RECONNECT]{_RESET} [{_lbl}] 5+ workers hit 5xx — cancel-all to force fresh cookies...")
                        logging.warning(f"[RECONNECT] {_lbl}: 5xx threshold reached, cancel-all")
                        try:
                            _selected_cfg['client'].cancel_all_tasks()
                        except Exception as _ce:
                            logging.warning(f"[RECONNECT] {_lbl} cancel-all failed: {_ce}")
                        _selected_cfg['5xx_count'] = 0
                        _selected_cfg['5xx_cancelled'] = False
                time.sleep(_5xx_wait)
                continue
            logging.warning(f"Clip #{clip_id} {tag} [{phase_label}] attempt {attempt}/{max_retries}: timeout")
            print(f"\n  {_YELLOW}[TIMEOUT]{_RESET} #{clip_id} {tag} [{phase_label}] {attempt}/{max_retries}")
            if attempt < max_retries:
                _retry_delays = [5, 5, 10]
                delay = _retry_delays[min(attempt - 1, len(_retry_delays) - 1)]
                print(f"  {_YELLOW}[RETRY]{_RESET}   waiting {delay}s...")
                time.sleep(delay)

        except ShutdownRequested:
            raise  # пробрасываем наверх — не ловим как обычную ошибку

        except Exception as e:
            if _shutdown_requested.is_set():
                logging.info(f"Clip #{clip_id} {tag} [{phase_label}]: shutdown — exiting generic retry")
                break
            attempt_elapsed = time.time() - start
            retry_time += attempt_elapsed
            last_error = str(e)
            _count_error(error_counts, last_error)
            err_lower_ex = last_error.lower()
            # Raw backend error — goes to pipeline_full.log via stdout capture + per-run log
            print(f"[RAW_ERROR] Clip #{clip_id} {tag} [{phase_label}]: {last_error[:500]}")
            logging.info(f"Clip #{clip_id} {tag} [{phase_label}] raw_error: {last_error[:500]}")
            # Extract Google reason → targeted remix hint (if applicable)
            _reason = _extract_google_error_reason(last_error)
            if _reason:
                google_reason = _reason
                if _reason in _GOOGLE_REMIX_HINTS:
                    remix_hint = _GOOGLE_REMIX_HINTS[_reason]
                    print(f"  [GOOGLE REASON] #{clip_id} {tag}: {_reason} → targeted remix hint set")
                    logging.info(f"Clip #{clip_id} {tag}: Google reason {_reason} mapped to targeted remix hint")
                elif _reason in _GOOGLE_COOLDOWN_REASONS:
                    _cd = 60 + random.uniform(0, 30)
                    logging.warning(f"Clip #{clip_id} {tag} [{phase_label}]: Google {_reason} — cooldown {_cd:.0f}s (no remix)")
                    print(f"\n  {_YELLOW}[COOLDOWN]{_RESET} #{clip_id} {tag}: Google {_reason} — waiting {_cd:.0f}s before retry")
                    if not _shutdown_requested.is_set():
                        time.sleep(_cd)
                    attempt -= 1
                    continue
            # Desk-side transient (axios timeout, TCP reset) — pause, don't waste attempts
            if _is_desk_transient_error(last_error):
                _cd = 45 + random.uniform(0, 20)
                logging.warning(f"Clip #{clip_id} {tag} [{phase_label}]: transient desk error — cooldown {_cd:.0f}s")
                print(f"\n  {_YELLOW}[COOLDOWN]{_RESET} #{clip_id} {tag}: transient desk error — waiting {_cd:.0f}s")
                if not _shutdown_requested.is_set():
                    time.sleep(_cd)
                attempt -= 1
                continue
            # Slots full может прийти сюда если VeoNonStopGenerationError не поймался (module identity)
            if _is_slots_full_error(err_lower_ex):
                slots_full_count += 1
                _count_error(error_counts, 'slots_full')
                if slots_full_count > 10:
                    logging.error(f"Clip #{clip_id} {tag} [{phase_label}]: slots full 10x — giving up")
                    print(f"\n  {_RED}[SLOTS FULL]{_RESET} #{clip_id} {tag}: 10 retries exhausted — failed")
                    break
                _wait_for_slot(clip_id, tag, phase_label, slots_full_count, key_label=key_label, client=client)
                attempt -= 1
                continue
            # Cancelled by user (cancel-all) — module identity path
            if 'cancelled by user' in err_lower_ex:
                if _shutdown_requested.is_set():
                    logging.info(f"Clip #{clip_id} {tag} [{phase_label}]: cancelled by user stop — exiting")
                    break
                logging.warning(f"Clip #{clip_id} {tag} [{phase_label}] attempt {attempt}: cancelled by cancel-all (Exception path) — retrying")
                print(f"\n  {_YELLOW}[CANCELLED]{_RESET} #{clip_id} {tag}: task cancelled by cancel-all — retrying...")
                attempt -= 1
                time.sleep(5)
                continue
            # Module-identity fallback for the same terminal Google error.
            if _is_generation_timeout_error(last_error):
                generation_timeout = True
                censorship_streak += 1
                _count_error(error_counts, 'generation_timeout')
                limit = config.VEONONSTOP_CENSOR_STREAK_LIMIT
                if censorship_streak < limit:
                    logging.warning(
                        f"Clip #{clip_id} {tag} [{phase_label}]: terminal generation timeout "
                        f"(Exception path, {censorship_streak}/{limit}) - retrying"
                    )
                    continue
                censored = True
                logging.warning(
                    f"Clip #{clip_id} {tag} [{phase_label}]: terminal generation timeout "
                    f"(Exception path) — routing to configured Skipper chain"
                )
                print(
                    f"\n  {_RED}[GEN TIMEOUT]{_RESET} #{clip_id} {tag}: "
                    f"server generation timed out — going to Skipper fallback chain"
                )
                break
            # Module identity fallback: STATUS_FAILED суммарно (не подряд)
            if 'media_generation_status_failed' in err_lower_ex:
                status_failed_count += 1
                _sf_threshold = max_retries
                if status_failed_count >= _sf_threshold:
                    censored = True
                    logging.warning(f"Clip #{clip_id} {tag} [{phase_label}]: {status_failed_count}x STATUS_FAILED total — treating as hidden censorship")
                    print(f"\n  {_RED}[HIDDEN CENSOR?]{_RESET} #{clip_id} {tag}: {status_failed_count}x STATUS_FAILED total — treating as censorship, will rewrite prompt")
                    break
                _sf_delay = status_failed_count * 5
                logging.info(f"Clip #{clip_id} {tag} [{phase_label}]: STATUS_FAILED ({status_failed_count}/{_sf_threshold}) — retrying in {_sf_delay}s")
                print(f"\n  {_YELLOW}[STATUS_FAILED]{_RESET} #{clip_id} {tag}: ({status_failed_count}/{_sf_threshold}) — retrying in {_sf_delay}s...")
                time.sleep(_sf_delay)
                continue
            # reCAPTCHA / RESOURCE_EXHAUSTED — 3 retries per task, then cancel + resubmit
            if 'recaptcha' in err_lower_ex or 'resource_exhausted' in err_lower_ex:
                recaptcha_retry_count += 1
                if recaptcha_retry_count <= 3:
                    _rc_delay = getattr(config, 'VEONONSTOP_RECAPTCHA_DELAY', 8.0) + random.uniform(1.0, max(1.0, getattr(config, 'VEONONSTOP_RECAPTCHA_JITTER', 3.0)))
                    logging.warning(f"Clip #{clip_id} {tag} [{phase_label}]: reCAPTCHA (Exception) retry {recaptcha_retry_count}/3 in {_rc_delay:.1f}s")
                    print(f"\n  {_YELLOW}[RECAPTCHA]{_RESET} #{clip_id} {tag}: retry {recaptcha_retry_count}/3 in {_rc_delay:.1f}s...")
                    time.sleep(_rc_delay)
                    attempt -= 1
                    continue
                resubmit_count += 1
                if resubmit_count > config.VEONONSTOP_MAX_RESUBMITS:
                    logging.error(f"Clip #{clip_id} {tag} [{phase_label}]: reCAPTCHA — {resubmit_count - 1} resubmits exhausted (Exception path)")
                    print(f"\n  {_RED}[RECAPTCHA]{_RESET} #{clip_id} {tag}: {resubmit_count - 1} resubmits exhausted — failed")
                    break
                _failed_tid = _inflight_tasks.get(clip_id)
                if _failed_tid:
                    try:
                        client.cancel_task(_failed_tid)
                    except Exception:
                        pass
                recaptcha_retry_count = 0
                logging.warning(f"Clip #{clip_id} {tag} [{phase_label}]: reCAPTCHA (Exception) — cancel + resubmit {resubmit_count}/{config.VEONONSTOP_MAX_RESUBMITS}")
                print(f"\n  {_YELLOW}[RECAPTCHA RESUBMIT]{_RESET} #{clip_id} {tag}: cancel + resubmit {resubmit_count}/{config.VEONONSTOP_MAX_RESUBMITS}")
                time.sleep(4)
                attempt -= 1
                continue
            # Person / prominent people filter — Exception path
            if _is_prominent_person_error(err_lower_ex) or 'invalid argument' in err_lower_ex:
                prominent_person = True
                logging.warning(f"Clip #{clip_id} {tag} [{phase_label}]: person/invalid_arg (Exception path) — will rewrite")
                print(f"\n  {_RED}[PERSON]{_RESET} #{clip_id} {tag}: person filter — will rewrite with appearance description")
                break
            # Content policy / censorship — Exception path
            if _is_content_policy_error(err_lower_ex):
                censorship_streak += 1
                limit = config.VEONONSTOP_CENSOR_STREAK_LIMIT
                if censorship_streak < limit:
                    logging.warning(
                        f"Clip #{clip_id} {tag} [{phase_label}]: censorship response "
                        f"(Exception path, {censorship_streak}/{limit}) - confirming"
                    )
                    continue
                censored = True
                logging.warning(f"Clip #{clip_id} {tag} [{phase_label}]: censored (Exception path) — will rewrite")
                print(f"\n  {_RED}[CENSORED]{_RESET} #{clip_id} {tag}: content policy — will rewrite")
                break
            logging.error(f"Clip #{clip_id} {tag} [{phase_label}] attempt {attempt}/{max_retries}: {e}")
            print(f"\n  {_YELLOW}[ERROR]{_RESET}   #{clip_id} {tag} [{phase_label}] {attempt}/{max_retries}: {e}")
            if attempt < max_retries:
                _retry_delays = [5, 5, 10]
                delay = _retry_delays[min(attempt - 1, len(_retry_delays) - 1)]
                time.sleep(delay)

    return {
        'ok': False,
        'elapsed': round(retry_time, 1),
        'retries': attempt,
        'retry_time': round(retry_time, 1),
        'error_counts': error_counts,
        'error': last_error,
        'censored': censored,
        'prominent_person': prominent_person,
        'ip_image_blocked': ip_image_blocked,
        'generation_timeout': generation_timeout,
        'censorship_streak': censorship_streak,
        'google_reason': google_reason,
        'remix_hint': remix_hint,
    }


def _image_remix(image_path: str, clip_id: int, output_dir: Path, remix_hint: Optional[str] = None) -> Optional[str]:
    """
    Ремикс изображения через настроенный движок (REMIX_ENGINE).
    Используется когда VeoNonStop цензурирует клип — проблема в изображении.
    Промпт берётся из i2v_remix_prompt.txt.

    Args:
        remix_hint: targeted instruction from Google error reason (skips Vision, appends as guidance).
                    If None → fallback to Vision-checks-based addition.

    Результат сохраняется в project/remixed/ (не в vid_img/) — остаётся навсегда
    чтобы пользователь мог просмотреть ремиксы.

    Returns:
        Путь к ремикснутому изображению или None при ошибке.
    """
    import params as _cfg
    _engine = _cfg.REMIX_ENGINE.upper()
    try:
        remixed_dir = output_dir.parent / 'remixed'
        remixed_dir.mkdir(exist_ok=True)
        _remix_sfx = _engine.lower() if _engine else 'remix'
        remix_path = remixed_dir / f"{clip_id:03d}_{_remix_sfx}_remix.png"
        log_path = str(output_dir.parent / 'remix_debug.log')
        remix_i2v(image_path, str(remix_path), log_path=log_path, hint=remix_hint)
        if remix_hint:
            logging.info(f"Clip #{clip_id}: {_engine} i2v remix (targeted hint) saved -> remixed/{remix_path.name}")
        else:
            logging.info(f"Clip #{clip_id}: {_engine} i2v remix saved -> remixed/{remix_path.name}")
        return str(remix_path)
    except Exception as e:
        logging.warning(f"Clip #{clip_id}: {_engine} i2v remix failed — {e}")
        return None


def _custom_duration_enabled() -> bool:
    """Включена ли подгонка длительности клипа под сцену (галка в интерфейсе).

    Умолчание — ВКЛЮЧЕНО. По актуальной доке API v1 параметр `duration`
    принимает image-to-video (4/5/6/8 с); для остальных режимов v1 клип всегда
    8 секунд — клиент решает это сам.

    Выключается явно: `VEONONSTOP_CUSTOM_DURATION=off` (а также 0/false/no).
    """
    raw = os.environ.get('VEONONSTOP_CUSTOM_DURATION')
    if raw is None:
        raw = getattr(config, 'VEONONSTOP_CUSTOM_DURATION', '')
    value = str(raw or '').strip().lower()
    if not value:
        return True                      # умолчание: включено
    return value not in ('0', 'off', 'false', 'no')


def _process_single_clip(
    client,   # VeoNonStopClient or None (None when clients_cfg_list provided)
    clip_id: int,
    prompt: str,
    mode: str,
    image_path: Optional[str],
    ref_dir: Optional[str],
    output_dir: Path,
    aspect_ratio: Optional[str] = None,
    max_retries: int = None,
    worker_id_fn=None,
    single_prompt_mode: bool = False,
    end_image_path: Optional[str] = None,
    submit_sem=None,
    clients_cfg_list=None,  # if provided: worker-assignment — client/sem/stagger by worker_id
    ok_run: Optional[dict] = None,  # shared thread-safe stats for running avg: {'n':0,'total':0.0,'lock':Lock}
    upscale_video: bool = False,
    image_regen_prompt: Optional[str] = None,
    scene_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Генерация одного видеоклипа с двухфазной логикой:

    Фаза 1: до max_retries попыток с оригинальным промптом.
    При цензуре/известной личности → rewrite промпта через FastGen Prompt API.
    Фаза 2: до max_retries попыток с переписанным промптом.

    Returns:
        {'id': int, 'status': 'ok'|'error'|'censored', 'file': str|None,
         'error': str|None, 'duration_sec': float, 'task_id': str|None,
         'rewritten': bool, 'phase': int,
         'worker_id': int, 'retries': int, 'retry_time': float,
         'error_counts': dict}
    """
    if max_retries is None:
        max_retries = config.VEONONSTOP_MAX_RETRIES
    # worker_id определяется ВНУТРИ воркера (в потоке ThreadPool)
    worker_id = worker_id_fn() if worker_id_fn else 0
    clip_start_time = datetime.now().strftime('%H:%M:%S')
    clip_start_ts = time.time()
    tag = f"[W{worker_id}]"

    result = {
        'id': clip_id,
        'status': 'error',
        'file': None,
        'error': None,
        'duration_sec': 0,
        'task_id': None,
        'rewritten': False,
        'phase': 1,
        'worker_id': worker_id,
        'key_label': 'Key1',
        'retries': 0,
        'retry_time': 0.0,
        'error_counts': {},
        'upload_sec': 0.0,
        'gen_sec': 0.0,
        'dl_sec': 0.0,
        'media_generation_id': None,
    }

    output_path = output_dir / f"{clip_id:03d}.mp4"

    # ── Проверка shutdown ─────────────────────────────────────────────
    if _shutdown_requested.is_set():
        result['status'] = 'error'
        result['error'] = 'shutdown requested'
        return result

    # ── Worker-assignment: определяем client/sem по worker_id (если dual-key) ──
    _key_label = 'Key1'
    if clients_cfg_list:
        cumulative = 0
        _selected_cfg = clients_cfg_list[-1]
        _group_start = 0
        for _cfg in clients_cfg_list:
            if worker_id < cumulative + _cfg['mp']:
                _selected_cfg = _cfg
                _group_start = cumulative
                break
            cumulative += _cfg['mp']
        client = _selected_cfg['client']
        submit_sem = _selected_cfg['sem']
        _key_label = _selected_cfg['label']
        _local_worker_id = worker_id - _group_start
    else:
        _local_worker_id = worker_id

    # Обновляем result['key_label'] после определения ключа
    result['key_label'] = _key_label

    # ── Key dead check: skip immediately if key was poisoned by another worker ──
    if clients_cfg_list and _selected_cfg and _selected_cfg.get('key_dead'):
        result['status'] = 'error'
        result['error'] = f'key {_key_label} is dead (auth fatal)'
        logging.warning(f"Clip #{clip_id} [W{worker_id}]: skipped — key {_key_label} is dead")
        return result

    # ── Stagger: только при ПЕРВОМ запуске воркера (разброс старта первой волны) ──
    # При dual-key — stagger по локальному ID внутри группы (обе группы стартуют параллельно)
    # В метке рабочего указывается и ВЕРСИЯ API ключа: при двух ключах разных
    # версий в одном прогоне по логу должно быть видно, кто чем работал, —
    # иначе разбирать поведение (длительность, отмену, картинки) невозможно.
    # Метка идёт во ВСЕ строки клипа: [START], [OK], [WAIT], [FAILED].
    _key_ver = getattr(client, 'api_version', 1) if client is not None else 1
    tag = f"[W{worker_id}/{_key_label}·v{_key_ver}]"
    result['api_version'] = _key_ver
    with _started_workers_lock:
        is_first_run = worker_id not in _started_workers
        _started_workers.add(worker_id)
    if is_first_run:
        stagger = _local_worker_id * config.VEONONSTOP_REQUEST_DELAY
        jitter = random.uniform(0.05, 0.1)
    else:
        stagger = 0.0
        jitter = random.uniform(0.05, 0.1)
    total_delay = stagger + jitter
    logging.info(f"Clip #{clip_id} {tag}: stagger {stagger}s + jitter {jitter:.1f}s (first={is_first_run})")
    print(f"  {_YELLOW}[START]{_RESET}  #{clip_id} {tag}: launching in {total_delay:.1f}s...")
    time.sleep(total_delay)

    def _inline_upscale():
        """Inline video upscale right after successful generation."""
        if not upscale_video or not result.get('media_generation_id'):
            return
        _ups_client = client if not clients_cfg_list else _selected_cfg['client']
        try:
            _ups_t0 = time.time()
            _ups_tmp = output_path.with_suffix('.ups.mp4')
            # Ссылка на готовый файл ускоряет апскейл на v2 (замер 01.09.2026:
            # 50с против 71с), но она подписанная и живёт недолго. Клип с
            # повторами (капча, цензура) генерируется минуты, и к моменту
            # апскейла ссылка от ПЕРВОЙ попытки уже мертва — сервис отвечает
            # NOT_FOUND (поймано живым прогоном клипа). Поэтому: пробуем со
            # ссылкой, а на отказе повторяем без неё — без ссылки апскейл
            # работает всегда, просто медленнее.
            _ups_tid = None
            for _ups_url in ((result.get('media_url'), None)
                             if result.get('media_url') else (None,)):
                try:
                    _ups_tid = _ups_client.submit_upsample_video(
                        media_generation_id=result['media_generation_id'],
                        aspect_ratio=aspect_ratio,
                        video_url=_ups_url,
                    )
                    _ups_client.poll_video_status(_ups_tid)
                    break
                except Exception as _ups_try_err:
                    if _ups_url is None:
                        raise
                    logging.info(
                        "Clip #%s: апскейл со ссылкой не прошёл (%s) — "
                        "повтор без неё", clip_id, str(_ups_try_err)[:80])
                    _ups_tid = None
            if _ups_tid is None:
                raise VeoNonStopError('upsample: не удалось отправить задачу')
            _download_video_limited(
                _ups_client, _ups_tid, str(_ups_tmp), context=f"clip #{clip_id} inline upscale"
            )
            _ups_valid = False
            try:
                _ups_probe = subprocess.run(
                    ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                     '-of', 'default=noprint_wrappers=1:nokey=1', str(_ups_tmp)],
                    capture_output=True, text=True, timeout=10)
                _ups_valid = bool(_ups_probe.stdout.strip()) and float(_ups_probe.stdout.strip()) > 0.1
            except Exception:
                pass
            if _ups_valid:
                _ups_tmp.replace(output_path)
                _ups_elapsed = round(time.time() - _ups_t0, 1)
                print(f"  {_GREEN}[UPSCALE]{_RESET} #{clip_id} {tag}: {_ups_elapsed}s")
                logging.info(f"Clip #{clip_id} {tag}: upscaled inline ({_ups_elapsed}s)")
                result['upscaled'] = True
                result['upscale_sec'] = _ups_elapsed
            else:
                _ups_tmp.unlink(missing_ok=True)
                print(f"  {_YELLOW}[UPSCALE]{_RESET} #{clip_id} {tag}: corrupt after upscale, keeping 720p")
                logging.warning(f"Clip #{clip_id} {tag}: upscale file corrupt, keeping 720p")
        except Exception as _ups_err:
            print(f"  {_YELLOW}[UPSCALE]{_RESET} #{clip_id} {tag}: failed ({_ups_err})")
            logging.warning(f"Clip #{clip_id} {tag}: inline upscale failed: {_ups_err}")

    # ── Фаза 1: оригинальный промпт ──────────────────────────────────
    try:
        phase1 = _run_phase(
            client, clip_id, prompt, mode, image_path, ref_dir,
            output_path, aspect_ratio, max_retries, 'phase1',
            worker_id=worker_id, clip_start_time=clip_start_time,
            end_image_path=end_image_path,
            submit_sem=submit_sem,
            key_label=_key_label,
            clients_cfg_list=clients_cfg_list,
            _selected_cfg=_selected_cfg if clients_cfg_list else None,
            scene_seconds=scene_seconds,
        )
    except ShutdownRequested:
        result['status'] = 'error'
        result['error'] = 'shutdown requested'
        result['duration_sec'] = round(time.time() - clip_start_ts, 1)
        logging.info(f"Clip #{clip_id} {tag}: shutdown during phase1")
        return result

    if phase1['ok']:
        # Validate downloaded video via ffprobe
        _valid = False
        if output_path.exists() and output_path.stat().st_size > 1000:
            try:
                _probe = subprocess.run(
                    ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                     '-of', 'default=noprint_wrappers=1:nokey=1', str(output_path)],
                    capture_output=True, text=True, timeout=10)
                _dur = _probe.stdout.strip()
                _valid = bool(_dur) and float(_dur) > 0.1
            except Exception:
                _valid = False
        if not _valid:
            # Try re-download (Google may have finished processing)
            logging.warning(f"Clip #{clip_id} {tag}: downloaded file invalid, re-downloading...")
            print(f"\n  {_YELLOW}[REDOWNLOAD]{_RESET} #{clip_id} {tag}: file invalid, re-downloading...")
            try:
                _re_client = client if not clients_cfg_list else _selected_cfg['client']
                _download_video_limited(
                    _re_client,
                    phase1['task_id'],
                    str(output_path),
                    context=f"clip #{clip_id} re-download",
                )
                _probe2 = subprocess.run(
                    ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                     '-of', 'default=noprint_wrappers=1:nokey=1', str(output_path)],
                    capture_output=True, text=True, timeout=10)
                _dur2 = _probe2.stdout.strip()
                _valid = bool(_dur2) and float(_dur2) > 0.1
            except Exception:
                _valid = False
            if _valid:
                print(f"  {_GREEN}[REDOWNLOAD]{_RESET} #{clip_id} {tag}: OK after re-download")
                logging.info(f"Clip #{clip_id} {tag}: valid after re-download")
            else:
                print(f"  {_RED}[CORRUPT]{_RESET} #{clip_id} {tag}: still invalid after re-download — marking as failed")
                logging.error(f"Clip #{clip_id} {tag}: corrupt video after re-download")
                phase1['ok'] = False
                phase1['error'] = 'corrupt video file after download + re-download'

    if phase1['ok']:
        elapsed = phase1['elapsed']
        t_up = phase1.get('upload_sec', 0.0)
        t_gen = phase1.get('gen_sec', 0.0)
        t_dl = phase1.get('dl_sec', 0.0)
        retries = phase1['retries']
        retry_time = phase1['retry_time']
        error_counts = phase1['error_counts']
        result.update({
            'status': 'ok', 'file': str(output_path),
            'duration_sec': elapsed, 'task_id': phase1['task_id'], 'phase': 1,
            'retries': retries, 'retry_time': retry_time, 'error_counts': error_counts,
            'upload_sec': t_up, 'gen_sec': t_gen, 'dl_sec': t_dl,
            'media_generation_id': phase1.get('media_generation_id'),
            'media_url': phase1.get('media_url'),
        })
        # Формат лога по LOGGING_REQUIREMENTS
        log_line = f"Clip #{clip_id} {tag}: OK phase1 (total={elapsed}s | upload={t_up}s | gen={t_gen}s | dl={t_dl}s"
        if retries > 0:
            log_line += f" | retries={retries} retry_time={retry_time}s errors={error_counts}"
        log_line += ")"
        print(f"  {_GREEN}[OK]{_RESET} {log_line}")
        logging.info(log_line)
        if ok_run is not None:
            with ok_run['lock']:
                ok_run['n'] += 1
                ok_run['total'] += elapsed
                _avg = ok_run['total'] / ok_run['n']
                _n   = ok_run['n']
            _wall = time.time() - ok_run['wall_start']
            _wall_cpm = _n / (_wall / 60) if _wall > 0 else 0
            _wall_spc = 60.0 / _wall_cpm if _wall_cpm > 0 else 0
            print(f"  {_CYAN}[SPEED]{_RESET} worker avg={_avg:.0f}s/clip  |  pool {_wall_spc:.1f}s/clip ({_wall_cpm:.1f} clip/min)")
        _inline_upscale()
        return result

    # ── IP image blocked: проблема в картинке, не в промпте ──────────
    is_ip_blocked = phase1.get('ip_image_blocked', False)
    if is_ip_blocked and mode == 'i2v' and image_path:
        _ip_combined_errors: Dict[str, int] = {}
        for k, v in phase1.get('error_counts', {}).items():
            _ip_combined_errors[k] = _ip_combined_errors.get(k, 0) + v
        _ip_total_retries = phase1.get('retries', 0)
        _ip_total_retry_time = phase1.get('retry_time', 0.0)

        def _ip_accumulate(ph: dict) -> None:
            nonlocal _ip_total_retries, _ip_total_retry_time
            for k, v in ph.get('error_counts', {}).items():
                _ip_combined_errors[k] = _ip_combined_errors.get(k, 0) + v
            _ip_total_retries += ph.get('retries', 0)
            _ip_total_retry_time += ph.get('retry_time', 0.0)

        # Шаг 1: перегенерить картинку через Banana по исходному image prompt.
        # В single-prompt i2v основной prompt является motion-инструкцией для VEO
        # ("animate provided image"), а не описанием кадра для image generation.
        _regen_path = output_dir / f"{clip_id:03d}_ipregen.png"
        if image_regen_prompt:
            try:
                print(f"  {_RED}[IP REGEN]{_RESET} #{clip_id} {tag}: regenerating image via banana (per-clip prompt)...")
                logging.info(f"Clip #{clip_id} {tag}: IP_INPUT_IMAGE — regenerating image via banana using per-clip prompt")
                client.generate_banana(image_regen_prompt, str(_regen_path), aspect_ratio=aspect_ratio)
            except Exception as _bex:
                logging.warning(f"Clip #{clip_id} {tag}: banana regen failed ({_bex})")
                _regen_path = None
        else:
            print(f"  {_YELLOW}[IP REGEN SKIP]{_RESET} #{clip_id} {tag}: no per-clip image prompt; keeping original image")
            logging.warning(f"Clip #{clip_id} {tag}: IP_INPUT_IMAGE — banana regen skipped, no per-clip image prompt")
            _regen_path = None

        if _regen_path and _regen_path.exists():
            try:
                _ph = _run_phase(
                    client, clip_id, prompt, mode, str(_regen_path), ref_dir,
                    output_path, aspect_ratio, max_retries, 'ip_regen',
                    worker_id=worker_id, clip_start_time=clip_start_time,
                    end_image_path=end_image_path,
                    submit_sem=submit_sem, key_label=_key_label,
                    clients_cfg_list=clients_cfg_list,
                    _selected_cfg=_selected_cfg if clients_cfg_list else None,
                    scene_seconds=scene_seconds,
                )
            except ShutdownRequested:
                result['status'] = 'error'
                result['error'] = 'shutdown requested'
                result['duration_sec'] = round(time.time() - clip_start_ts, 1)
                return result
            _ip_accumulate(_ph)
            if _ph['ok']:
                elapsed = round(time.time() - clip_start_ts, 1)
                result.update({
                    'status': 'ok', 'file': str(output_path),
                    'duration_sec': elapsed, 'task_id': _ph['task_id'], 'phase': 2,
                    'retries': _ip_total_retries, 'retry_time': round(_ip_total_retry_time, 1),
                    'error_counts': _ip_combined_errors,
                    'upload_sec': _ph.get('upload_sec', 0.0),
                    'gen_sec': _ph.get('gen_sec', 0.0),
                    'dl_sec': _ph.get('dl_sec', 0.0),
                    'media_generation_id': _ph.get('media_generation_id'),
                    'media_url': _ph.get('media_url'),
                })
                print(f"  {_GREEN}[OK]{_RESET} Clip #{clip_id} {tag}: OK (ip_regen) total={elapsed}s")
                logging.info(f"Clip #{clip_id} {tag}: OK after IP image regen ({elapsed}s)")
                return result

        # Шаг 2: remix картинки через движок проекта → retry i2v
        _remix_engine_label = config.REMIX_ENGINE.upper()
        print(f"  {_RED}[IP REMIX]{_RESET} #{clip_id} {tag}: remixing image via {_remix_engine_label}...")
        logging.info(f"Clip #{clip_id} {tag}: IP_INPUT_IMAGE — remixing image via {_remix_engine_label}")
        _remixed = _image_remix(image_path, clip_id, output_dir, remix_hint=_GOOGLE_REMIX_HINTS.get('PUBLIC_ERROR_IP_INPUT_IMAGE'))
        _use_img = _remixed if _remixed else image_path

        try:
            _ph = _run_phase(
                client, clip_id, prompt, mode, _use_img, ref_dir,
                output_path, aspect_ratio, max_retries, 'ip_remix',
                worker_id=worker_id, clip_start_time=clip_start_time,
                end_image_path=end_image_path,
                submit_sem=submit_sem, key_label=_key_label,
                clients_cfg_list=clients_cfg_list,
                _selected_cfg=_selected_cfg if clients_cfg_list else None,
                scene_seconds=scene_seconds,
            )
        except ShutdownRequested:
            result['status'] = 'error'
            result['error'] = 'shutdown requested'
            result['duration_sec'] = round(time.time() - clip_start_ts, 1)
            return result
        _ip_accumulate(_ph)
        if _ph['ok']:
            elapsed = round(time.time() - clip_start_ts, 1)
            result.update({
                'status': 'ok', 'file': str(output_path),
                'duration_sec': elapsed, 'task_id': _ph['task_id'], 'phase': 2,
                'retries': _ip_total_retries, 'retry_time': round(_ip_total_retry_time, 1),
                'error_counts': _ip_combined_errors,
                'upload_sec': _ph.get('upload_sec', 0.0),
                'gen_sec': _ph.get('gen_sec', 0.0),
                'dl_sec': _ph.get('dl_sec', 0.0),
                'media_generation_id': _ph.get('media_generation_id'),
                'media_url': _ph.get('media_url'),
            })
            print(f"  {_GREEN}[OK]{_RESET} Clip #{clip_id} {tag}: OK (ip_remix) total={elapsed}s")
            logging.info(f"Clip #{clip_id} {tag}: OK after IP image remix ({elapsed}s)")
            return result

        # Шаг 3: всё не помогло → static fallback
        print(f"  {_YELLOW}[IP STATIC]{_RESET} #{clip_id} {tag}: banana regen + remix both failed → static fallback")
        logging.warning(f"Clip #{clip_id} {tag}: IP_INPUT_IMAGE — all attempts failed, static fallback")
        # Используем _static_fallback из phase2 chain — но нужно инициализировать зависимости
        ext = Path(image_path).suffix
        dest = output_dir / f"{clip_id:03d}{ext}"
        try:
            shutil.copy(image_path, str(dest))
            elapsed = round(time.time() - clip_start_ts, 1)
            result.update({
                'status': 'ok', 'file': str(dest),
                'duration_sec': elapsed, 'task_id': None, 'phase': 2,
                'retries': _ip_total_retries, 'retry_time': round(_ip_total_retry_time, 1),
                'error_counts': _ip_combined_errors,
            })
            print(f"  {_YELLOW}[STATIC]{_RESET} Clip #{clip_id} {tag}: copied original image to vid_img/ ({elapsed}s)")
            logging.warning(f"Clip #{clip_id} {tag}: IP static fallback — copied original image ({elapsed}s)")
        except Exception as _cpe:
            elapsed = round(time.time() - clip_start_ts, 1)
            result.update({
                'status': 'error', 'error': f'ip static fallback failed: {_cpe}',
                'duration_sec': elapsed, 'retries': _ip_total_retries,
                'retry_time': round(_ip_total_retry_time, 1), 'error_counts': _ip_combined_errors,
            })
            logging.error(f"Clip #{clip_id}: IP static fallback copy failed: {_cpe}")
        return result

    # ── Skipper fallback chain: censorship / public person / generation timeout ──
    is_person = phase1.get('prominent_person', False)
    is_censored = phase1.get('censored', False)
    is_generation_timeout = phase1.get('generation_timeout', False)
    _phase1_remix_hint = phase1.get('remix_hint')  # Google-reason-based targeted hint, if any

    # Обычная техническая ошибка — rewrite не поможет, сразу error
    if not is_person and not is_censored and not is_generation_timeout:
        elapsed = round(time.time() - clip_start_ts, 1)
        retries = phase1['retries']
        retry_time = phase1['retry_time']
        error_counts = phase1['error_counts']
        result.update({
            'status': 'error', 'error': phase1.get('error'),
            'duration_sec': elapsed,
            'retries': retries, 'retry_time': retry_time, 'error_counts': error_counts,
        })
        log_line = f"Clip #{clip_id} {tag}: FAILED phase1 ({elapsed}s gen time) retries={retries} retry_time={retry_time}s errors={error_counts}: {phase1.get('error', '')[:100]}"
        logging.error(log_line)
        print(f"  {_YELLOW}[FAILED]{_RESET} {log_line}")
        return result

    # ── Phase 2: multi-cycle retry (remix + prompt rewrite) ──────────

    result['phase'] = 2
    combined_errors: Dict[str, int] = {}
    for k, v in phase1.get('error_counts', {}).items():
        combined_errors[k] = combined_errors.get(k, 0) + v
    total_retries = phase1.get('retries', 0)
    total_retry_time = phase1.get('retry_time', 0.0)

    _MINIMALISTIC_PROMPT = (
        "Animate a frame in a very minimalistic way without changing the logic of the scene."
    )

    def _accumulate(ph: dict) -> None:
        nonlocal total_retries, total_retry_time
        for k, v in ph.get('error_counts', {}).items():
            combined_errors[k] = combined_errors.get(k, 0) + v
        total_retries += ph.get('retries', 0)
        total_retry_time += ph.get('retry_time', 0.0)

    def _ok_return(phase_result: dict, suffix: str = '') -> dict:
        elapsed = round(time.time() - clip_start_ts, 1)
        t_up = phase_result.get('upload_sec', 0.0)
        t_gen = phase_result.get('gen_sec', 0.0)
        t_dl  = phase_result.get('dl_sec', 0.0)
        result.update({
            'status': 'ok', 'file': str(output_path),
            'duration_sec': elapsed, 'task_id': phase_result['task_id'], 'phase': 2,
            'retries': total_retries, 'retry_time': round(total_retry_time, 1),
            'error_counts': combined_errors,
            'upload_sec': t_up, 'gen_sec': t_gen, 'dl_sec': t_dl,
            'media_generation_id': phase_result.get('media_generation_id'),
            'media_url': phase_result.get('media_url'),
        })
        log_line = (
            f"Clip #{clip_id} {tag}: OK phase2 (total={elapsed}s | "
            f"upload={t_up}s | gen={t_gen}s | dl={t_dl}s{suffix}"
        )
        if total_retries > 0:
            log_line += f" | retries={total_retries} retry_time={round(total_retry_time,1)}s errors={combined_errors}"
        log_line += ")"
        print(f"  {_GREEN}[OK]{_RESET} {log_line}")
        logging.info(log_line)
        if ok_run is not None:
            with ok_run['lock']:
                ok_run['n'] += 1
                ok_run['total'] += elapsed
                _avg = ok_run['total'] / ok_run['n']
                _n   = ok_run['n']
            _wall = time.time() - ok_run['wall_start']
            _wall_cpm = _n / (_wall / 60) if _wall > 0 else 0
            _wall_spc = 60.0 / _wall_cpm if _wall_cpm > 0 else 0
            print(f"  {_CYAN}[SPEED]{_RESET} worker avg={_avg:.0f}s/clip  |  pool {_wall_spc:.1f}s/clip ({_wall_cpm:.1f} clip/min)")
        _inline_upscale()
        return result

    def _static_fallback() -> dict:
        """Статический фелбек: для i2v копирует оригинал, для t2v генерит картинку через Banana.

        В t2v картинка — не финал, а материал: banana рисует кадр (её фильтр
        отдельный от видео-фильтра Veo), и этот кадр отправляется на оживление
        уже как i2v. Раньше клип оставался неподвижной картинкой, хотя
        оживить её было чем.
        """
        if image_path is None:
            # T2V: нет картинки — генерируем через Banana и кладём в vid_img/
            dest = output_dir / f"{clip_id:03d}.jpg"
            try:
                client.generate_banana(
                    prompt,
                    str(dest),
                    aspect_ratio=aspect_ratio,
                    model_key=config.VEONONSTOP_STATIC_FALLBACK_MODEL,
                )
                elapsed = round(time.time() - clip_start_ts, 1)
                log_line = f"Clip #{clip_id} {tag}: T2V STATIC FALLBACK — banana image generated -> vid_img/ ({elapsed}s)"
                print(f"  {_YELLOW}[STATIC T2V]{_RESET} {log_line}")
                logging.warning(log_line)

                # Оживляем полученный кадр: сначала исходным промптом (режиссура
                # сохраняется), затем минималистичным — он почти не даёт фильтру
                # за что зацепиться, а движение уже описано самой картинкой.
                for _anim_prompt, _anim_lbl in (
                    (prompt, 'phase2_t2v_animate'),
                    (_MINIMALISTIC_PROMPT, 'phase2_t2v_animate_min'),
                ):
                    print(f"  {_CYAN}[T2V ANIMATE]{_RESET} #{clip_id} {tag}: "
                          f"оживляю кадр banana через i2v ({_anim_lbl})...")
                    logging.info(
                        f"Clip #{clip_id} {tag}: t2v fallback — animating banana "
                        f"frame via i2v ({_anim_lbl})")
                    try:
                        _ph = _phase2_attempt(_anim_prompt, str(dest), _anim_lbl,
                                              override_mode='i2v')
                    except ShutdownRequested:
                        raise
                    except Exception as _ae:
                        logging.warning(
                            f"Clip #{clip_id} {tag}: t2v animate ({_anim_lbl}) "
                            f"failed: {_ae}")
                        break
                    _accumulate(_ph)
                    if _ph['ok']:
                        result['rewritten'] = True
                        return _ok_return(_ph, ' (t2v → banana → i2v)')

                # Оживить не вышло — остаётся кадр, он уже лежит в vid_img/.
                logging.warning(
                    f"Clip #{clip_id} {tag}: t2v fallback — оживление не удалось, "
                    f"остаётся статичный кадр")
                result.update({
                    'status': 'ok', 'file': str(dest),
                    'duration_sec': round(time.time() - clip_start_ts, 1),
                    'task_id': None, 'phase': 2,
                    'retries': total_retries, 'retry_time': round(total_retry_time, 1),
                    'error_counts': combined_errors,
                })
            except ShutdownRequested:
                result['status'] = 'error'
                result['error'] = 'shutdown requested'
                result['duration_sec'] = round(time.time() - clip_start_ts, 1)
            except Exception as _fe:
                logging.error(f"Clip #{clip_id}: T2V static fallback (banana) failed ({_fe})")
                elapsed = round(time.time() - clip_start_ts, 1)
                result.update({
                    'status': 'error', 'error': f't2v static fallback failed: {_fe}',
                    'duration_sec': elapsed, 'retries': total_retries,
                    'retry_time': round(total_retry_time, 1), 'error_counts': combined_errors,
                })
            return result

        # i2v / components: копируем оригинальную картинку
        ext = Path(image_path).suffix
        dest = output_dir / f"{clip_id:03d}{ext}"
        try:
            shutil.copy(image_path, str(dest))
            elapsed = round(time.time() - clip_start_ts, 1)
            log_line = f"Clip #{clip_id} {tag}: STATIC FALLBACK — copied original image to vid_img/ ({elapsed}s)"
            print(f"  {_YELLOW}[STATIC]{_RESET} {log_line}")
            logging.warning(log_line)
            result.update({
                'status': 'ok', 'file': str(dest),
                'duration_sec': elapsed, 'task_id': None, 'phase': 2,
                'retries': total_retries, 'retry_time': round(total_retry_time, 1),
                'error_counts': combined_errors,
            })
        except Exception as _fe:
            logging.error(f"Clip #{clip_id}: static fallback copy failed ({_fe})")
            elapsed = round(time.time() - clip_start_ts, 1)
            result.update({
                'status': 'error', 'error': f'static fallback failed: {_fe}',
                'duration_sec': elapsed, 'retries': total_retries,
                'retry_time': round(total_retry_time, 1), 'error_counts': combined_errors,
            })
        return result

    def _phase2_attempt(use_prompt: str, use_image: Optional[str], label: str,
                        override_ref_dir: Optional[str] = None,
                        override_mode: Optional[str] = None) -> dict:
        effective_rd = override_ref_dir if override_ref_dir is not None else ref_dir
        # override_mode нужен t2v-фелбеку: там мы сами делаем картинку через
        # banana и дальше идём уже как i2v, оживляя её.
        effective_mode = override_mode or mode
        return _run_phase(
            client, clip_id, use_prompt, effective_mode, use_image, effective_rd,
            output_path, aspect_ratio, max_retries, label,
            worker_id=worker_id, clip_start_time=clip_start_time,
            end_image_path=end_image_path,
            submit_sem=submit_sem,
            key_label=_key_label,
            clients_cfg_list=clients_cfg_list,
            _selected_cfg=_selected_cfg if clients_cfg_list else None,
            scene_seconds=scene_seconds,
        )

    if (
        phase1.get('censored')
        and phase1.get('censorship_streak', 0)
        >= config.VEONONSTOP_CENSOR_STREAK_LIMIT
    ):
        logging.warning(
            f"Clip #{clip_id} {tag}: confirmed Veo censorship "
            f"({phase1.get('censorship_streak')} consecutive responses) - static fallback"
        )
        return _static_fallback()

    def _binary_search_blocked_refs(ref_files: list) -> list:
        """
        Бинарный поиск блокирующих рефов через VeoNonStop.
        Возвращает список Path-объектов заблокированных рефов.
        Каждый тест — реальный VeoNonStop-вызов (последний резерв после 5 реврайтов).
        """
        import tempfile
        import shutil as _shutil
        if not ref_files:
            return []
        if len(ref_files) == 1:
            tmp = tempfile.mkdtemp(prefix=f'veo_bs_{clip_id}_')
            try:
                _shutil.copy2(str(ref_files[0]), tmp)
                _ph = _phase2_attempt(prompt, None, f'bsearch_{ref_files[0].stem}',
                                      override_ref_dir=tmp)
                _accumulate(_ph)
                return [ref_files[0]] if _ph.get('censored') else []
            except ShutdownRequested:
                raise
            except Exception as _e:
                logging.warning(f"Clip #{clip_id}: bsearch test failed for {ref_files[0].name}: {_e}")
                return []
            finally:
                _shutil.rmtree(tmp, ignore_errors=True)
        mid = len(ref_files) // 2
        bad: list = []
        for half, lbl in ((ref_files[:mid], 'L'), (ref_files[mid:], 'R')):
            tmp = tempfile.mkdtemp(prefix=f'veo_bs_{clip_id}_')
            try:
                for f in half:
                    _shutil.copy2(str(f), tmp)
                _ph = _phase2_attempt(prompt, None, f'bsearch_{lbl}', override_ref_dir=tmp)
                _accumulate(_ph)
                if _ph.get('censored'):
                    bad.extend(_binary_search_blocked_refs(half))
            except ShutdownRequested:
                raise
            except Exception as _e:
                logging.warning(f"Clip #{clip_id}: bsearch half {lbl} failed: {_e}")
            finally:
                _shutil.rmtree(tmp, ignore_errors=True)
        return bad

    # ── FAST_CENSOR: ultrafast → сразу static fallback ──
    _fast_censor = config.VEONONSTOP_FAST_CENSOR
    if _fast_censor == 'ultrafast':
        logging.info(f"Clip #{clip_id} {tag}: FAST_CENSOR=ultrafast — skipping chain, static fallback")
        print(f"  {_YELLOW}[ULTRAFAST]{_RESET} #{clip_id} {tag}: skipping rewrite/remix chain → static fallback")
        return _static_fallback()

    # Single-prompt mode has no rewrite stage. In this mode after_rewrites
    # therefore means skipping image remixes and using the static fallback.
    if single_prompt_mode and _fast_censor == 'after_rewrites':
        logging.info(
            f"Clip #{clip_id} {tag}: FAST_CENSOR=after_rewrites in single-prompt mode "
            f"— no rewrite stage, skipping remixes and using static fallback"
        )
        print(
            f"  {_YELLOW}[FAST CENSOR]{_RESET} #{clip_id} {tag}: "
            f"after_rewrites — skipping remixes → static fallback"
        )
        return _static_fallback()

    # ── Single-prompt + i2v: проблема в изображении → 3 цикла remix ──
    if single_prompt_mode and mode == 'i2v' and image_path:
        _remix_engine_label = config.REMIX_ENGINE.upper()
        for _remix_n in range(1, 4):
            print(f"  {_RED}[IMG REMIX #{_remix_n}/3]{_RESET} #{clip_id} {tag}: remixing image via {_remix_engine_label}...")
            logging.info(f"Clip #{clip_id} {tag}: single-prompt i2v censorship — {_remix_engine_label} remix cycle {_remix_n}/3")
            _remixed = _image_remix(image_path, clip_id, output_dir, remix_hint=_phase1_remix_hint)
            _use_img = _remixed if _remixed else image_path
            try:
                _ph = _phase2_attempt(prompt, _use_img, f'phase2_remix{_remix_n}')
            except ShutdownRequested:
                result['status'] = 'error'
                result['error'] = 'shutdown requested'
                result['duration_sec'] = round(time.time() - clip_start_ts, 1)
                logging.info(f"Clip #{clip_id} {tag}: shutdown during phase2 remix {_remix_n}")
                return result
            _accumulate(_ph)
            result['rewritten'] = True
            if _ph['ok']:
                return _ok_return(_ph, f' (img remix #{_remix_n})')
            # Любой результат (цензура или техн. ошибка) — переходим к следующему ремиксу
        # Все 3 ремикса не помогли — статический кадр-фелбек.
        return _static_fallback()

    # ── Per-clip промпт режим: 2 реврайта → 2 ремикса → минималистик → статик ──
    else:
        # Шаг 1-2: реврайты промпта (2 шт.)
        for _rw_n in range(1, 3):
            _lbl = 'PERSON REWRITE' if is_person else 'REWRITE'
            print(f"  {_RED}[{_lbl} #{_rw_n}/2]{_RESET} #{clip_id} {tag}: rewriting prompt...")
            logging.info(f"Clip #{clip_id} {tag}: prompt rewrite #{_rw_n}/2 (for_person={is_person})")
            try:
                _rw_prompt = _rewrite_blocked_prompt(prompt, for_person=is_person)
            except Exception as _e:
                logging.warning(f"Clip #{clip_id} {tag}: rewrite #{_rw_n} failed ({_e}), using original")
                _rw_prompt = prompt
            # Option C: if rewrite returned empty / identical — GPT refuses or quota'd.
            # Skip remaining rewrite cycles, go straight to image remix (no point in retrying same null path).
            if not _rw_prompt or _rw_prompt == prompt:
                logging.warning(f"Clip #{clip_id} {tag}: rewrite #{_rw_n} produced no change — skipping remaining rewrites, going to image remix")
                print(f"  {_YELLOW}[SKIP REWRITE]{_RESET} #{clip_id} {tag}: GPT returned no change — jumping to image remix")
                break
            _use_prompt = _rw_prompt
            try:
                _ph = _phase2_attempt(_use_prompt, image_path, f'phase2_rw{_rw_n}')
            except ShutdownRequested:
                result['status'] = 'error'
                result['error'] = 'shutdown requested'
                result['duration_sec'] = round(time.time() - clip_start_ts, 1)
                logging.info(f"Clip #{clip_id} {tag}: shutdown during phase2 rewrite {_rw_n}")
                return result
            _accumulate(_ph)
            result['rewritten'] = True
            if _ph['ok']:
                return _ok_return(_ph, f' (rewrite #{_rw_n})')
            # Любой результат (цензура или техн. ошибка) — переходим к следующему шагу

        # FAST_CENSOR: after_rewrites → static fallback после реврайтов
        if _fast_censor == 'after_rewrites':
            logging.info(f"Clip #{clip_id} {tag}: FAST_CENSOR=after_rewrites — skipping remixes, static fallback")
            print(f"  {_YELLOW}[FAST CENSOR]{_RESET} #{clip_id} {tag}: after_rewrites — skipping remixes → static fallback")
            return _static_fallback()

        # Шаг 3-4: ремиксы (2 шт.) — только для i2v с картинкой
        if mode == 'i2v' and image_path:
            _remix_engine_label = config.REMIX_ENGINE.upper()
            for _remix_n in range(1, 3):
                print(f"  {_RED}[IMG REMIX #{_remix_n}/2]{_RESET} #{clip_id} {tag}: remixing original image via {_remix_engine_label}...")
                logging.info(f"Clip #{clip_id} {tag}: post-rewrite {_remix_engine_label} remix cycle {_remix_n}/2")
                _remixed = _image_remix(image_path, clip_id, output_dir, remix_hint=_phase1_remix_hint)
                _use_img = _remixed if _remixed else image_path
                try:
                    _ph = _phase2_attempt(prompt, _use_img, f'phase2_remix{_remix_n}')
                except ShutdownRequested:
                    result['status'] = 'error'
                    result['error'] = 'shutdown requested'
                    result['duration_sec'] = round(time.time() - clip_start_ts, 1)
                    return result
                _accumulate(_ph)
                if _ph['ok']:
                    return _ok_return(_ph, f' (img remix #{_remix_n})')
                # Любой результат — переходим к следующему шагу

            # Шаг 5: Минималистик + ремикс
            print(f"  {_RED}[MINIMALISTIC]{_RESET} #{clip_id} {tag}: final attempt with minimalistic prompt + {config.REMIX_ENGINE.upper()} remix...")
            logging.info(f"Clip #{clip_id} {tag}: minimalistic final attempt")
            _remixed = _image_remix(image_path, clip_id, output_dir, remix_hint=_phase1_remix_hint)
            _use_img = _remixed if _remixed else image_path
            try:
                _ph = _phase2_attempt(_MINIMALISTIC_PROMPT, _use_img, 'phase2_minimalistic')
            except ShutdownRequested:
                result['status'] = 'error'
                result['error'] = 'shutdown requested'
                result['duration_sec'] = round(time.time() - clip_start_ts, 1)
                return result
            _accumulate(_ph)
            if _ph['ok']:
                return _ok_return(_ph, ' (minimalistic)')

        # Components mode: бинарный поиск блокирующего рефа вместо ремиксов
        if mode == 'components' and ref_dir:
            print(f"  {_RED}[COMPONENTS BSEARCH]{_RESET} #{clip_id} {tag}: rewrites exhausted — searching for blocked ref...")
            logging.info(f"Clip #{clip_id} {tag}: components — binary searching for blocked refs in {ref_dir}")
            ref_path = Path(ref_dir)
            _ref_files = sorted(
                [f for f in ref_path.iterdir()
                 if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS],
                key=lambda p: p.name.lower()
            )
            try:
                _bad_refs = _binary_search_blocked_refs(_ref_files)
            except ShutdownRequested:
                result['status'] = 'error'
                result['error'] = 'shutdown requested'
                result['duration_sec'] = round(time.time() - clip_start_ts, 1)
                return result
            if _bad_refs:
                _bad_names = [f.name for f in _bad_refs]
                print(f"  {_RED}[BSEARCH]{_RESET} #{clip_id} {tag}: blocking refs: {_bad_names} — remixing in-place...")
                logging.warning(f"Clip #{clip_id} {tag}: blocking refs found: {_bad_names}")
                for _bad_f in _bad_refs:
                    try:
                        remix_ref(str(_bad_f), str(_bad_f))
                        logging.info(f"Clip #{clip_id}: remixed {_bad_f.name} in-place")
                    except Exception as _rem_err:
                        logging.error(f"Clip #{clip_id}: remix failed for {_bad_f.name}: {_rem_err}")
                        print(f"  {_RED}[BSEARCH]{_RESET} #{clip_id}: remix failed for {_bad_f.name}: {_rem_err}")
                print(f"  {_YELLOW}[COMPONENTS RETRY]{_RESET} #{clip_id} {tag}: retrying with remixed refs...")
                logging.info(f"Clip #{clip_id} {tag}: retrying components after ref remix")
                try:
                    _ph = _phase2_attempt(prompt, None, 'phase2_comp_postremix')
                except ShutdownRequested:
                    result['status'] = 'error'
                    result['error'] = 'shutdown requested'
                    result['duration_sec'] = round(time.time() - clip_start_ts, 1)
                    return result
                _accumulate(_ph)
                if _ph['ok']:
                    return _ok_return(_ph, ' (components ref remix)')
            else:
                print(f"  {_YELLOW}[BSEARCH]{_RESET} #{clip_id} {tag}: no blocking refs isolated — cannot recover")
                logging.warning(f"Clip #{clip_id} {tag}: binary search found no blocking refs")

        # Шаг 6: static fallback — цепочка исчерпана
        return _static_fallback()


# ======================================================================
# Chain Mode Parallel Scheduler
# ======================================================================

# ======================================================================
# Основная функция: batch-генерация видео
# ======================================================================

def apply_key_limits(clients_cfg: List[Dict[str, Any]],
                     max_parallel: Optional[int] = None) -> None:
    """Записать лимиты ключей в модульное состояние — ОДИН раз на набор клиентов.

    `_max_parallel_by_key` и `_inflight_by_key` описывают АККАУНТ, а не вызов:
    слоты у ключа общие, и учёт задач в полёте должен быть один. Пока эта
    установка жила внутри `generate_all_videos`, второй одновременный вызов
    (например, параллакс рядом со стадией i2v) обнулял учёт первого, и оба
    начинали считать слоты свободными — отсюда slots_full и лишние 429.
    """
    global _max_parallel_global, _max_parallel_by_key, _inflight_by_key
    total = (int(max_parallel) if max_parallel
             else sum(cfg['mp'] for cfg in clients_cfg) if clients_cfg else 0)
    _max_parallel_global = total
    if clients_cfg:
        _max_parallel_by_key = {cfg['label']: cfg['mp'] for cfg in clients_cfg}
        with _inflight_by_key_lock:
            _inflight_by_key = {cfg['label']: set() for cfg in clients_cfg}
    else:
        _max_parallel_by_key = {'Key1': total}
        with _inflight_by_key_lock:
            _inflight_by_key = {'Key1': set()}
    logging.info("VeoNonStop: лимиты ключей — %s (всего %d)",
                 _max_parallel_by_key, total)


def build_clients(active_keys: List[int], *, no_cancel: bool = False) -> List[Dict[str, Any]]:
    """Клиенты VeoNonStop по слотам ключей: чистка зомби, лимиты, семафоры.

    Вынесено из `generate_all_videos` БЕЗ изменений логики, чтобы тем же
    набором мог пользоваться не только сам генератор, но и другой поставщик
    задач — наблюдатель параллакса. Иначе ему пришлось бы поднимать своих
    клиентов и считать слоты отдельно, а слоты у ключа общие.

    Возвращает список словарей `{client, mp, sem, sem_slots, label}`.
    """
    # Провайдер видео — Vertex AI, RoyalTechno или VARAG: один клиент вместо ключей VeoNonStop.
    from units.vertex_client import build_vertex_cfg, vertex_selected
    if vertex_selected('video'):
        return build_vertex_cfg('video')
    from units.royaltechno_client import build_royaltechno_cfg, royaltechno_selected
    if royaltechno_selected():
        return build_royaltechno_cfg()
    from units.varag_client import build_varag_cfg, varag_selected
    if varag_selected('video'):
        return build_varag_cfg('video')

    _KEY_DEFS = [
        (config.VEONONSTOP_API_KEY,   config.VEONONSTOP_MAX_PARALLEL,   config.VEONONSTOP_KEY1_NAME or 'Key1', config.VEONONSTOP_API_BASE),
        (config.VEONONSTOP_API_KEY_2, config.VEONONSTOP_MAX_PARALLEL_2, config.VEONONSTOP_KEY2_NAME or 'Key2', config.VEONONSTOP_API_BASE_2),
        (config.VEONONSTOP_API_KEY_3, config.VEONONSTOP_MAX_PARALLEL_3, config.VEONONSTOP_KEY3_NAME or 'Key3', config.VEONONSTOP_API_BASE_3),
        (config.VEONONSTOP_API_KEY_4, config.VEONONSTOP_MAX_PARALLEL_4, config.VEONONSTOP_KEY4_NAME or 'Key4', config.VEONONSTOP_API_BASE_4),
    ]
    clients_cfg: List[Dict[str, Any]] = []
    _any_cancelled = False
    for idx in (active_keys or [1]):
        key, mp_cfg, label, base_url = _KEY_DEFS[idx - 1]
        if not key:
            continue
        c = VeoNonStopClient(api_key=key, base_url=base_url)
        if no_cancel:
            print(f"\n  {_YELLOW}[CLEANUP]{_RESET} [{label}] --no-cancel: skipping zombie task cleanup.")
            logging.info(f"Startup cancel-all skipped for {label} (--no-cancel)")
        else:
            print(f"\n  {_YELLOW}[CLEANUP]{_RESET} [{label}] Cancelling zombie tasks...")
            cancelled = c.cancel_all_tasks()
            if cancelled > 0:
                print(f"  {_YELLOW}[CLEANUP]{_RESET} [{label}] Cancelled {cancelled} zombie task(s).")
                logging.info(f"Startup cancel-all {label}: cancelled {cancelled} tasks")
                _any_cancelled = True
            else:
                print(f"  {_YELLOW}[CLEANUP]{_RESET} [{label}] No active tasks found.")
        _acc_ok = False
        for _att in range(1, 4):
            try:
                acc = c.get_account_info()
                ct = acc.get('concurrent_tasks', mp_cfg)
                mp = min(mp_cfg, ct)
                # Check active tasks via /account/usage
                _active_now = '?'
                try:
                    _u_resp = requests.get(f"{c.base_url}/account/usage", headers=c.headers, timeout=10)
                    if _u_resp.status_code == 200:
                        _u_data = _u_resp.json().get('data', {})
                        _active_now = _u_data.get('active_tasks', '?')
                except Exception:
                    pass
                print(f"  [{label}] Plan: {acc.get('plan', '?')} | concurrent_tasks: {ct} | active: {_active_now} | using: {mp}")
                logging.info(f"Account {label}: plan={acc.get('plan')}, concurrent_tasks={ct}, active={_active_now}, mp={mp}")
                _acc_ok = True
                break
            except Exception as e:
                logging.warning(f"Could not get account info ({label}), attempt {_att}/3: {e}")
                if _att < 3:
                    _delay = 30 if _att == 2 else 10
                    print(f"  [{label}] account info failed ({_att}/3), retry in {_delay}s...")
                    time.sleep(_delay)
        if not _acc_ok:
            mp = 12
            print(f"  [{label}] account info unavailable — fallback mp={mp}")
            logging.warning(f"Account info failed 3x for {label}, fallback mp={mp}")
        sem_slots = max(1, mp)
        sem = threading.Semaphore(sem_slots)  # Full concurrent capacity, not mp // 4
        clients_cfg.append({'client': c, 'mp': mp, 'sem': sem, 'sem_slots': sem_slots, 'label': label})

    if _any_cancelled:
        # Poll /account/usage until active_tasks drops (max 30s)
        _poll_client = clients_cfg[0]['client'] if clients_cfg else None
        if _poll_client:
            _poll_deadline = time.time() + 30
            _prev_active = None
            while time.time() < _poll_deadline:
                try:
                    _usage_url = f"{_poll_client.base_url}/account/usage"
                    _usage_resp = requests.get(_usage_url, headers=_poll_client.headers, timeout=10)
                    if _usage_resp.status_code == 200:
                        _usage = _usage_resp.json().get('data', {})
                        _active = _usage.get('active_tasks', 0)
                        if _prev_active is None:
                            print(f"  {_YELLOW}[CLEANUP]{_RESET} Waiting for server to release slots (active={_active})...")
                        if _active == 0:
                            print(f"  {_YELLOW}[CLEANUP]{_RESET} All slots free.")
                            break
                        _prev_active = _active
                except Exception:
                    pass
                time.sleep(2)
            else:
                print(f"  {_YELLOW}[CLEANUP]{_RESET} Timeout 30s, proceeding (active={_prev_active})...")
        else:
            time.sleep(3)
    return clients_cfg


def generate_all_videos(
    project_dir: str,
    mode: str = 'i2v',
    max_parallel: Optional[int] = None,
    clip_ids: Optional[List[int]] = None,
    single_prompt: Optional[str] = None,
    aspect_ratio: Optional[str] = None,
    ref_dir: Optional[str] = None,
    silent: bool = False,
    no_cancel: bool = False,
    active_keys: Optional[List[int]] = None,
    upscale_video: Optional[bool] = None,
    prompts_file_path: Optional[str] = None,
    allow_external_image_clip_ids: Optional[List[int]] = None,
    parallax_only: bool = False,
    queue=None,
) -> Dict[str, Any]:
    """
    Batch-генерация видеоклипов через VeoNonStop.

    Args:
        project_dir:    Путь к папке проекта
        mode:           'i2v' | 't2v' | 'components' | 'batch_frame'
        max_parallel:   Макс. параллельных задач (из config если None)
        clip_ids:       Список ID клипов (если None — все из montage_plan)
        single_prompt:  Один промпт на все клипы (если None — per-clip из prompts.txt)
        aspect_ratio:   Соотношение сторон
        ref_dir:        Папка с референсами (для components). Если None — PROJECT/ref/
        silent:         Авто-режим (без интерактивных вопросов):
                        до 5 авто-циклов ретрая упавших клипов
        active_keys:    Список 1-based индексов ключей [1], [1,2], [1,2,3,4] и т.д.
        allow_external_image_clip_ids:
                        External photo clips allowed through I2V. Used only by
                        Auto Visual Story parallax; video footage stays skipped.
        parallax_only:  Use the selected parallax prompt directly, without
                        requiring a per-clip prompts file.
        queue:          Общая очередь прогона (`units.motion_queue.VideoQueue`).
                        Передана — задачи идут в неё, свой пул не создаётся:
                        рабочие наняты один раз, слоты ключа считаются в одном
                        месте, и параллакс кладёт задачи туда же. Не передана —
                        поведение прежнее, со своим пулом на вызов.

    Returns:
        Сводка генерации
    """
    project_path = Path(project_dir)
    _shutdown_requested.clear()  # Reset from previous run
    max_parallel = max_parallel or config.VEONONSTOP_MAX_PARALLEL
    global _video_download_semaphore
    _download_parallel = max(
        1,
        min(
            int(getattr(config, 'VEONONSTOP_MAX_DOWNLOADS', 12)),
            max(1, int(max_parallel)),
        ),
    )
    _video_download_semaphore = threading.Semaphore(_download_parallel)
    if upscale_video is None:
        upscale_video = config.VEONONSTOP_VIDEO_UPSCALE

    _KEY_DEFS = [
        (config.VEONONSTOP_API_KEY,   config.VEONONSTOP_MAX_PARALLEL,   config.VEONONSTOP_KEY1_NAME or 'Key1', config.VEONONSTOP_API_BASE),
        (config.VEONONSTOP_API_KEY_2, config.VEONONSTOP_MAX_PARALLEL_2, config.VEONONSTOP_KEY2_NAME or 'Key2', config.VEONONSTOP_API_BASE_2),
        (config.VEONONSTOP_API_KEY_3, config.VEONONSTOP_MAX_PARALLEL_3, config.VEONONSTOP_KEY3_NAME or 'Key3', config.VEONONSTOP_API_BASE_3),
        (config.VEONONSTOP_API_KEY_4, config.VEONONSTOP_MAX_PARALLEL_4, config.VEONONSTOP_KEY4_NAME or 'Key4', config.VEONONSTOP_API_BASE_4),
    ]
    _active_keys = active_keys if active_keys else [1]

    # Подавить DEBUG-спам от urllib3/requests
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('requests').setLevel(logging.WARNING)

    # Лог-файл (skip if path too long for Windows MAX_PATH)
    log_ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    gen_log_path = project_path / f'veo_{log_ts}.log'
    file_handler = None
    try:
        file_handler = logging.FileHandler(str(gen_log_path), encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter('%(asctime)s | %(levelname)-8s | %(message)s'))
        logging.getLogger().addHandler(file_handler)
    except OSError:
        logging.warning(f"Cannot create log file (path too long?): {gen_log_path}")
    logging.info(f"=== VeoNonStop img2video started for {project_path.name} (mode={mode}) ===")
    logging.info(
        "Video download concurrency limited to %s (generation workers=%s)",
        _download_parallel,
        max_parallel,
    )

    print("\n" + "=" * 60)
    print(f"  IMG2VIDEO: VeoNonStop ({mode.upper()})")
    print("=" * 60)
    print(f"  Log: {gen_log_path}")

    # ------------------------------------------------------------------
    # 1. Чтение montage_plan.json (определяет список clip_id)
    # ------------------------------------------------------------------
    montage_plan_path = project_path / 'montage_plan.json'
    if not montage_plan_path.exists():
        raise FileNotFoundError(f"montage_plan.json not found in {project_path}")

    with open(montage_plan_path, 'r', encoding='utf-8-sig') as f:
        montage_data = json.load(f)

    # Handle both {"montage_plan": {"clips": [...]}} and {"clips": [...]}
    if 'montage_plan' in montage_data:
        montage_plan = montage_data['montage_plan']
    else:
        montage_plan = montage_data

    clips = montage_plan.get('clips', [])
    all_clip_ids = [c['id'] for c in clips]

    # Длина каждой сцены — для выбора `duration` (4/5/6/8 с).
    # В плане НЕТ поля `duration`: у клипа лежат `startTime` и `endTime`,
    # длительность считается разностью (проверено на живом montage_plan.json).
    _scene_seconds: Dict[int, float] = {}
    for _c in clips:
        try:
            _dur = float(_c.get('endTime', 0)) - float(_c.get('startTime', 0))
        except (TypeError, ValueError):
            continue
        if _dur > 0:
            _scene_seconds[int(_c['id'])] = _dur

    # External image sources normally stay in concat. Auto Visual Story may
    # deliberately send selected EPF/stock photos through I2V parallax.
    from units.source_ledger import (
        ANIMATABLE_STILL_SOURCES,
        VIDEO_SKIP_SOURCES,
    )
    _requested_external_ids = {
        int(clip_id)
        for clip_id in (allow_external_image_clip_ids or [])
        if str(clip_id).strip().isdigit()
    }
    # Обложка разрешает себя САМА. Список `allow_external_image_clip_ids`
    # передаёт только наблюдатель параллакса, а из пайплайна стадия зовётся в
    # восьми местах без него — и обложка отсекалась как обычный внешний
    # источник (поймано живым прогоном 02.09.2026: «Skipping 1 external
    # sources», 13 → 12, до вео она не дошла).
    if mode == 'i2v':
        try:
            from units import cover_opener as _ch

            _cover_clip = _ch.cover_clip_id(project_path)
            if _cover_clip is not None and _ch.hook_seconds() > 0:
                _requested_external_ids.add(int(_cover_clip))
        except Exception as _cover_gate_exc:                # noqa: BLE001
            logging.debug('cover_hook: клип не разрешён (%s)', _cover_gate_exc)
    _allowed_external_image_ids = {
        int(clip['id'])
        for clip in clips
        if clip.get('source') in ANIMATABLE_STILL_SOURCES
        and int(clip.get('id', -1)) in _requested_external_ids
    }
    _static_ids = {
        c['id']
        for c in clips
        if c.get('source') in VIDEO_SKIP_SOURCES
        and c.get('id') not in _allowed_external_image_ids
    }
    if _static_ids:
        all_clip_ids = [cid for cid in all_clip_ids if cid not in _static_ids]
        print(f"  [Static sources] Skipping {len(_static_ids)} clips reserved for non-video output")

    if clip_ids:
        all_clip_ids = [cid for cid in all_clip_ids if cid in clip_ids]

    print(f"  Total clips: {len(all_clip_ids)}")

    # ------------------------------------------------------------------
    # 2. Чтение промптов
    # ------------------------------------------------------------------
    prompts_map: Dict[int, str] = {}
    image_regen_prompts_map: Dict[int, str] = {}

    # Per-clip image prompts are needed for IP_INPUT_IMAGE fallback.
    # Even when VEO uses a single motion prompt, Banana regen must use the
    # original per-frame image prompt from *_prompts_*.txt.
    regen_prompts_file = find_prompts_file(str(project_path))
    if regen_prompts_file:
        image_regen_prompts_map = extract_all_prompts(regen_prompts_file)
        image_regen_prompts_map, _remapped = remap_dense_prompts_for_montage(image_regen_prompts_map, clips)
        if _remapped:
            print("  [Prompts] Remapped dense image-regeneration prompt file to real montage clip IDs")
        logging.info(f"Image regen prompts file: {regen_prompts_file} ({len(image_regen_prompts_map)} prompts)")
    else:
        logging.warning("Image regen prompts file not found; IP banana regen will be skipped")

    if parallax_only:
        if mode != 'i2v':
            raise ValueError("parallax_only is only valid for i2v")
        if not getattr(config, 'VEONONSTOP_I2V_PARALLAX', False):
            raise ValueError("parallax_only requires VEONONSTOP_I2V_PARALLAX")
        from units.parallax_core import (parallax_method_for_clip,
                                      parallax_method_name,
                                      selected_parallax_prompt)
        _method = getattr(config, 'VEONONSTOP_I2V_PARALLAX_METHOD', 1)
        # При «вперемешку» шаблон свой у каждого клипа, при обычном выборе — общий.
        _per_clip = {cid: parallax_method_for_clip(cid, _method)
                     for cid in all_clip_ids}
        _cache: dict[int, str] = {}
        prompts_map = {}
        for cid, method in _per_clip.items():
            if method not in _cache:
                _template = selected_parallax_prompt(method)
                if _template is None:
                    # Без шаблонов этот режим существовать не может: другого
                    # промпта у параллакс-прохода нет. Ошибку ловит вызывающий
                    # (`auto_visual_parallax`) — кадры останутся картинками и
                    # получат зум в склейке, прогон не падает.
                    raise RuntimeError(
                        "Шаблоны параллакса не найдены — положи файл "
                        "agent/prompts/parallax_prompts.txt (или "
                        "«паралакс промпты.txt» в корень программы)")
                _cache[method] = _template
            prompts_map[cid] = _cache[method]
        _by_template = {}
        for cid, method in sorted(_per_clip.items()):
            _by_template.setdefault(method, []).append(cid)
        _spread = "; ".join(
            f"{m} ({parallax_method_name(m)}): {ids}"
            for m, ids in sorted(_by_template.items())
        )
        print(f"  [Параллакс] режим {_method} ({parallax_method_name(_method)}) → "
              f"{len(prompts_map)} фото-клип(ов) — {_spread}")
        # В ФАЙЛ лога тоже: раньше эта ветка писала только в консоль, и по
        # `veo_*.log` выходило, будто параллакс не применялся вовсе — разбор
        # прогона 27.08 из-за этого пошёл по ложному следу.
        logging.info(
            "I2V parallax (Auto Visual Story): режим %s (%s), клипов %s — %s",
            _method, parallax_method_name(_method), len(prompts_map), _spread,
        )
        for method in sorted(_cache):
            logging.info("I2V parallax prompt %s: %s", method, _cache[method][:250])
    elif single_prompt:
        if single_prompt == 'default':
            # Взять первый промпт из prompts.txt для всех клипов
            prompts_file = prompts_file_path or find_prompts_file(str(project_path))
            if not prompts_file:
                raise FileNotFoundError(
                    f"Prompts file not found in {project_path} "
                    f"(expected *_prompts_*.txt)"
                )
            all_prompts = extract_all_prompts(prompts_file)
            all_prompts, _remapped = remap_dense_prompts_for_montage(all_prompts, clips)
            if _remapped:
                print("  [Prompts] Remapped dense prompt file to real montage clip IDs")
            if all_prompts:
                first_prompt = list(all_prompts.values())[0]
                for cid in all_clip_ids:
                    prompts_map[cid] = first_prompt
                print(f"  Prompt mode: single prompt (first from {Path(prompts_file).name}) for all clips")
                logging.info(f"Prompts file: {prompts_file}")
            else:
                raise ValueError("No prompts found in prompts.txt")
        else:
            # Один промпт на все (передан явно)
            for cid in all_clip_ids:
                prompts_map[cid] = single_prompt
            print(f"  Prompt mode: single prompt for all clips")
    else:
        # Per-clip из prompts.txt (или adapted файл если передан)
        prompts_file = prompts_file_path or find_prompts_file(str(project_path))
        if not prompts_file:
            raise FileNotFoundError(
                f"Prompts file not found in {project_path} "
                f"(expected *_prompts_*.txt)"
            )
        all_prompts = extract_all_prompts(prompts_file)
        all_prompts, _remapped = remap_dense_prompts_for_montage(all_prompts, clips)
        if _remapped:
            print("  [Prompts] Remapped dense prompt file to real montage clip IDs")
        for cid in all_clip_ids:
            if cid in all_prompts:
                prompts_map[cid] = all_prompts[cid]
            else:
                logging.warning(f"Clip #{cid}: no prompt found, skipping")
        print(f"  Prompt mode: per-clip from {Path(prompts_file).name}")
        print(f"  Prompts loaded: {len(prompts_map)}")
        logging.info(f"Prompts file: {prompts_file}")

    if not prompts_map:
        raise ValueError("No prompts available for generation")

    from units.i2v_overrides import apply_i2v_prompt_overrides
    apply_i2v_prompt_overrides(project_path, prompts_map, mode, parallax_only=parallax_only)

    # ------------------------------------------------------------------
    # 3. Подготовка путей
    # ------------------------------------------------------------------
    output_dir = project_path / 'vid_img'
    output_dir.mkdir(exist_ok=True)

    generated_dir = project_path / 'generated'
    effective_ref_dir = ref_dir or str(project_path / 'ref')

    # Пропуск уже готовых
    def _get_video_height(path: str) -> int:
        """Returns video height in pixels, 0 on error."""
        try:
            r = subprocess.run(
                ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                 '-show_entries', 'stream=height', '-of', 'csv=p=0', str(path)],
                capture_output=True, text=True, timeout=10
            )
            h = r.stdout.strip()
            return int(h) if h else 0
        except Exception:
            return 0

    skipped = []
    _skipped_needs_upscale = 0
    pending_ids = []
    for cid in sorted(prompts_map.keys()):
        existing_media = clip_media_path(output_dir, cid)
        if existing_media:
            if upscale_video:
                if existing_media.suffix.lower() != '.mp4':
                    skipped.append(cid)
                    continue
                # Если апскейл включён — пропускаем только уже 1080p клипы.
                # 720p клипы (упавший апскейл) отправляем на перегенерацию,
                # чтобы получить media_generation_id и доапскейлить.
                _h = _get_video_height(str(existing_media))
                if _h >= 1080:
                    skipped.append(cid)
                else:
                    pending_ids.append(cid)
                    _skipped_needs_upscale += 1
            else:
                skipped.append(cid)
        else:
            pending_ids.append(cid)

    if skipped:
        print(f"  Skipped (already exist): {len(skipped)}")
    if _skipped_needs_upscale:
        print(f"  Re-generating for upscale: {_skipped_needs_upscale} clips (720p -> 1080p)")
    print(f"  To generate: {len(pending_ids)}")

    if not pending_ids:
        print("  All videos already exist. Nothing to generate.")
        return {'generated': len(skipped), 'failed': 0, 'results': []}

    # Проверка generated/ для i2v и batch_frame
    def _find_image(cid: int) -> Optional[str]:
        for ext in ('.png', '.jpg', '.jpeg', '.webp'):
            p = generated_dir / f"{cid:03d}{ext}"
            if p.exists():
                return str(p)
        return None

    if mode in ('i2v', 'batch_frame'):
        missing_images = [cid for cid in pending_ids if not _find_image(cid)]
        if missing_images:
            print(f"\n  {_RED}WARNING:{_RESET} {len(missing_images)} images missing in generated/:")
            print(f"  IDs: {', '.join(map(str, missing_images[:20]))}")
            print(f"  These clips will be skipped in {mode} mode.")
            pending_ids = [cid for cid in pending_ids if cid not in missing_images]

    # Для batch_frame: строим карту end_image (клип N → изображение клипа N+1)
    end_image_map: Dict[int, str] = {}
    if mode == 'batch_frame':
        ordered = sorted(pending_ids)
        for i, cid in enumerate(ordered):
            next_cid = ordered[i + 1] if i + 1 < len(ordered) else cid
            img = _find_image(next_cid) or _find_image(cid)
            if img:
                end_image_map[cid] = img
        print(f"  Batch-frame pairs built: {len(end_image_map)}")

    # Проверка ref/ для components
    if mode == 'components':
        ref_path = Path(effective_ref_dir)
        if not ref_path.exists() or not any(
            f.suffix.lower() in IMAGE_EXTENSIONS
            for f in ref_path.iterdir() if f.is_file()
        ):
            raise FileNotFoundError(
                f"No reference images found in {effective_ref_dir}. "
                f"Components mode requires ref/ folder with images."
            )
        ref_count = sum(1 for f in ref_path.iterdir()
                        if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS)
        print(f"  References: {ref_count} images in {ref_path}")

        # ── Предполётная проверка рефов через VeoNonStop Banana Pro ──
        _skip_pf = os.environ.get('BEGO_SKIP_PREFLIGHT', '0') == '1'
        if _skip_pf:
            print(f"  [PREFLIGHT] Skipped (user disabled)")
            logging.info("PREFLIGHT components: skipped by user (BEGO_SKIP_PREFLIGHT=1)")
        elif config.FASTGEN_PREFLIGHT_REFS and config.REMIX_AVAILABLE:
            import tempfile, os as _os
            print(f"  [PREFLIGHT] Testing components refs via VeoNonStop Banana...")
            logging.info("PREFLIGHT components: testing refs via VeoNonStop Banana")
            try:
                _pf_key_def = _KEY_DEFS[_active_keys[0] - 1]
                _pf_client = VeoNonStopClient(api_key=_pf_key_def[0], base_url=_pf_key_def[3])
                _pf_aspect = config.VEONONSTOP_ASPECT_RATIO
                _pf_refs = _load_ref_images(effective_ref_dir)
                _pf_test_num = [0]

                _PF_NET_KW = ['connection', 'reset', 'aborted', 'timeout', 'refused', 'network', 'socket',
                             '503', '502', '429', '500', 'service unavailable', 'bad gateway',
                             'too many requests', 'internal server error']
                _CENS_KW = ['invalid argument', 'safety', 'blocked', 'censored',
                            'harmful', 'policy', 'prohibited', 'violat',
                            'prominent person', 'dangerous', 'inappropriate']

                def _pf_is_cens(s):
                    lo = s.lower()
                    return any(k in lo for k in _CENS_KW)

                def _pf_is_net(s):
                    lo = s.lower()
                    return any(k in lo for k in _PF_NET_KW)

                _pf_last_fail_reason = [None]  # 'network' | 'censorship' | 'unknown'

                def _pf_banana_ok(refs_subset, label='', _retries=3):
                    if not refs_subset:
                        return True
                    _pf_test_num[0] += 1
                    tag = f"[{_pf_test_num[0]}]" if not label else f"[{_pf_test_num[0]}] {label}"
                    desc = f"  [PREFLIGHT] {tag} {len(refs_subset)} ref(s)"
                    for attempt in range(1, _retries + 2):
                        _exc: list = [None]
                        _t0 = [time.time()]
                        try:
                            with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
                                tmp_path = tmp.name
                        except Exception:
                            return True

                        def _run():
                            try:
                                _pf_client.generate_banana(
                                    "abstract cinematic background, no people",
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

                        retry_label = desc if attempt == 1 else f"{desc} [retry {attempt-1}/{_retries}]"
                        t = threading.Thread(target=_run, daemon=True)
                        t.start()
                        with tqdm(total=150, desc=retry_label, unit="s",
                                  bar_format="{desc}: {bar}| {n:.0f}/{total}s", leave=False) as pbar:
                            while t.is_alive():
                                time.sleep(1)
                                pbar.update(1)
                        t.join()
                        elapsed = int(time.time() - _t0[0])
                        if _exc[0] is None:
                            names = ', '.join(r.get('name', '?') for r in refs_subset)
                            print(f"  {_GREEN}[PREFLIGHT]{_RESET} {tag} {len(refs_subset)} ref(s): {names} -> OK ({elapsed}s)", flush=True)
                            logging.info(f"PREFLIGHT components [{tag}]: OK")
                            return True
                        err_str = str(_exc[0])
                        if _pf_is_cens(err_str):
                            names = ', '.join(r.get('name', '?') for r in refs_subset)
                            print(f"  {_RED}[PREFLIGHT]{_RESET} {tag} {len(refs_subset)} ref(s): {names} -> BLOCKED ({elapsed}s)", flush=True)
                            logging.warning(f"PREFLIGHT components [{tag}]: BLOCKED — {_exc[0]}")
                            _pf_last_fail_reason[0] = 'censorship'
                            return False
                        if _pf_is_net(err_str) and attempt <= _retries:
                            wait = 15 * attempt
                            print(f"  {_YELLOW}[PREFLIGHT]{_RESET} {tag}: network error (attempt {attempt}) — {err_str[:80]}", flush=True)
                            time.sleep(wait)
                            continue
                        print(f"  {_RED}[PREFLIGHT]{_RESET} {tag}: error ({elapsed}s): {err_str[:120]}", flush=True)
                        logging.error(f"PREFLIGHT components [{tag}]: unrecognized error — {_exc[0]}")
                        _pf_last_fail_reason[0] = 'unknown'
                        return False
                    # all retries exhausted on network errors
                    print(f"  {_RED}[PREFLIGHT]{_RESET} {tag}: API unreachable after {_retries} retries", flush=True)
                    logging.error(f"PREFLIGHT components [{tag}]: API unreachable after {_retries} retries")
                    _pf_last_fail_reason[0] = 'network'
                    return False

                _pf_all_ok = _pf_banana_ok(_pf_refs, label='ALL') if _pf_refs else True
                if not _pf_all_ok and _pf_last_fail_reason[0] in ('network', 'unknown'):
                    # Banana API is down but components uses VeoNonStop — skip preflight, try anyway
                    print(f"  {_YELLOW}[PREFLIGHT]{_RESET} Banana API unavailable — skipping preflight (components uses VeoNonStop, not Banana)")
                    logging.warning("PREFLIGHT components: Banana API down, skipping — components will try VeoNonStop directly")
                elif not _pf_all_ok and _pf_last_fail_reason[0] == 'censorship':
                    print(f"  {_RED}[PREFLIGHT]{_RESET} BLOCKED — binary search for bad ref(s)...")
                    logging.warning("PREFLIGHT components: BLOCKED — isolating via binary search")

                    def _pf_isolate(refs_list, offset=0):
                        if not refs_list:
                            return []
                        if len(refs_list) == 1:
                            lbl = f"single: {refs_list[0].get('name', '?')}"
                            return [offset] if not _pf_banana_ok(refs_list, label=lbl) else []
                        mid = len(refs_list) // 2
                        bad = []
                        if not _pf_banana_ok(refs_list[:mid], label=f"left {offset+1}-{offset+mid}"):
                            bad.extend(_pf_isolate(refs_list[:mid], offset))
                        if not _pf_banana_ok(refs_list[mid:], label=f"right {offset+mid+1}-{offset+len(refs_list)}"):
                            bad.extend(_pf_isolate(refs_list[mid:], offset + mid))
                        return bad

                    bad_idxs = _pf_isolate(_pf_refs)
                    bad_names = [_pf_refs[i].get('name', f'ref_{i}') for i in bad_idxs]
                    print(f"  {_RED}[PREFLIGHT]{_RESET} Bad refs: {bad_names}")
                    logging.warning(f"PREFLIGHT components: bad refs: {bad_names}")

                    remixed_any = False
                    for idx in bad_idxs:
                        ref_name = _pf_refs[idx].get('name', '')
                        orig_file = None
                        for ext in ('.jpg', '.jpeg', '.png', '.webp'):
                            candidate = ref_path / (ref_name + ext)
                            if candidate.exists():
                                orig_file = candidate
                                break
                        if orig_file is None:
                            logging.warning(f"PREFLIGHT components: file not found for ref '{ref_name}'")
                            print(f"  {_YELLOW}[PREFLIGHT]{_RESET} File not found for ref '{ref_name}' — skipping remix")
                            continue
                        try:
                            remix_ref(str(orig_file), str(orig_file))
                            remixed_any = True
                            logging.info(f"PREFLIGHT components: {config.REMIX_ENGINE.upper()} remixed {orig_file.name} in-place")
                        except Exception as e:
                            logging.error(f"PREFLIGHT components: {config.REMIX_ENGINE.upper()} remix failed for {orig_file.name}: {e}")
                            print(f"  {_RED}[PREFLIGHT]{_RESET} {config.REMIX_ENGINE.upper()} remix failed for {orig_file.name}: {e}")

                    if remixed_any:
                        print(f"  {_YELLOW}[PREFLIGHT]{_RESET} Re-testing with remixed refs...")
                        _pf_refs = _load_ref_images(effective_ref_dir)
                        if not _pf_banana_ok(_pf_refs, label='ALL (remixed)'):
                            logging.error(f"PREFLIGHT components: still blocked after {config.REMIX_ENGINE.upper()} remix — stopping")
                            raise RuntimeError(
                                f"PREFLIGHT FAILED: components refs blocked even after {config.REMIX_ENGINE.upper()} remix.\n"
                                "Fix: replace blocked ref images manually."
                            )
                        else:
                            print(f"  {_GREEN}[PREFLIGHT]{_RESET} OK after {config.REMIX_ENGINE.upper()} remix — continuing with remixed refs")
                            logging.info(f"PREFLIGHT components: OK after {config.REMIX_ENGINE.upper()} remix")
                    else:
                        logging.error(f"PREFLIGHT components: no refs remixed ({config.REMIX_ENGINE.upper()} unavailable?) — stopping")
                        raise RuntimeError(
                            f"PREFLIGHT FAILED: components refs blocked and {config.REMIX_ENGINE.upper()} remix unavailable.\n"
                            "Fix: check REMIX_ENGINE API key (FASTGEN_API_KEY or CinAI session) in .env."
                        )
                else:
                    logging.info("PREFLIGHT components: refs OK")

            except RuntimeError:
                raise
            except Exception as e:
                logging.error(f"PREFLIGHT components: failed: {e}")
                print(f"  {_RED}[PREFLIGHT]{_RESET} Failed: {e}")
                raise RuntimeError(f"PREFLIGHT FAILED: {e}. VeoNonStop API may be down.")

    # ------------------------------------------------------------------
    # 4. Инициализация клиента + сброс зомби-тасков + семафор
    # ------------------------------------------------------------------
    global _active_tasks_path
    _active_tasks_path = Path(__file__).parent.parent / 'active_tasks.json'

    # ------------------------------------------------------------------
    # Multi-key init: loop over active_keys, create client per key
    # ------------------------------------------------------------------
    # Клиенты по ключам, чистка зомби и лимиты аккаунта — общая функция:
    # ею же пользуется наблюдатель параллакса, чтобы не поднимать своих
    # клиентов и не считать слоты отдельно (слоты у ключа общие).
    #
    # Если пайплайн уже создал клиентов и положил их в общую очередь — берём
    # ГОТОВЫХ. Второй набор на те же ключи не только лишний (лишние
    # cancel-all и запросы к аккаунту), но и опасен: `_max_parallel_by_key` и
    # `_inflight_by_key` модульные, и повторная инициализация обнулила бы учёт
    # задач, которые в этот момент уже в полёте у другого поставщика.
    _shared_cfg = None
    if queue is None:
        try:
            from units.motion_queue import get_queue as _get_video_queue
            queue = _get_video_queue(_active_keys)
        except Exception as _vq_exc:                       # noqa: BLE001
            logging.debug("Общая очередь недоступна (%s) — свой пул", _vq_exc)
            queue = None
    if queue is not None:
        _shared_cfg = getattr(queue, 'clients_cfg', None)
    if _shared_cfg:
        clients_cfg = _shared_cfg
        logging.info("VeoNonStop: клиенты взяты из общей очереди (%s)",
                     ', '.join(c['label'] for c in clients_cfg))
    else:
        clients_cfg = build_clients(_active_keys, no_cancel=no_cancel)
        apply_key_limits(clients_cfg)
        _active_tasks_path.unlink(missing_ok=True)

    if not clients_cfg:
        raise ValueError("None of the specified VeoNonStop keys are configured in .env")

    # Expose clients for external stop (Control Panel Stop button)
    _active_clients.clear()
    _active_clients.extend(cfg['client'] for cfg in clients_cfg)

    # Сумма по ключам — это и есть выбор человека, а не его подмена: значение
    # из поля «Parallel» уже применено к КАЖДОМУ ключу в `build_clients`
    # (`mp = min(mp_cfg, ct)`). Выбрал 12 при ключах на 24 и 12 — получит 12 и
    # 12, всего 24 рабочих, по двенадцать на ключ. Раскладка по ключам ниже
    # идёт по номеру рабочего и опирается ровно на эти `mp`.
    max_parallel = sum(cfg['mp'] for cfg in clients_cfg)

    if queue is not None:
        # Общая очередь прогона: рабочие наняты один раз, слоты ключа считаются
        # в ОДНОМ месте вместе с параллаксом. Нет очереди (стадию запустили
        # отдельным BAT, мимо пайплайна) — работаем своим пулом, как раньше.
        print(f"  Очередь прогона: общая, рабочих {queue.workers}")
        logging.info("VideoQueue: стадия %s работает через общую очередь "
                     "(рабочих %d, ключи %s)", mode, queue.workers, _active_keys)

    global _submit_semaphore
    _submit_semaphore = None
    for cfg in clients_cfg:
        print(f"  [{cfg['label']}] Gen-slot semaphore: {cfg['sem_slots']} server slots | pool threads: {cfg['mp']}")

    # Per-key 5xx tracker: cancel-all when 5+ workers hit 5xx on same key
    _5XX_CANCEL_THRESHOLD = 5
    for _cfg in clients_cfg:
        _cfg['5xx_count'] = 0
        _cfg['5xx_cancelled'] = False

    print(f"\n  Mode: {mode.upper()}")
    print(f"  Aspect ratio: {aspect_ratio or config.VEONONSTOP_ASPECT_RATIO}")
    print(f"  Parallel workers: {max_parallel}")
    # Длительность под сцену: одна СВОДКА на стадию, а не строка на клип.
    # Подробности по каждому клипу пишутся в файл лога, но человеку, который
    # включил галку, подтверждение нужно там, где он смотрит прогон, — иначе
    # настройка выглядит не сработавшей (замечание пользователя 01.09.2026).
    _dur_v2 = list(clients_cfg) if (mode == 'i2v' or any(
        getattr(cfg.get('client'), 'api_version', 1) >= 2
        for cfg in clients_cfg)) else []
    if _dur_v2 and _custom_duration_enabled() and _scene_seconds:
        from units.veo_client import duration_for_scene as _dur_for_scene

        _dur_counts: Dict[str, int] = {}
        for _cid in sorted(prompts_map):
            _sec = _scene_seconds.get(_cid)
            if _sec:
                _picked = _dur_for_scene(_sec)
                _dur_counts[_picked] = _dur_counts.get(_picked, 0) + 1
        if _dur_counts:
            _spread = ", ".join(f"{_cnt}×{_val}"
                                for _val, _cnt in sorted(_dur_counts.items()))
            print(f"  Длительность под сцену: вкл — {_spread}")
            logging.info("Custom duration: раскладка по стадии %s — %s",
                         mode, _spread)
    elif _dur_v2 and not _custom_duration_enabled():
        print("  Длительность под сцену: выкл — все клипы по 8с")
    print("=" * 60)

    # ------------------------------------------------------------------
    # 5. Генерация с retry-loop
    # ------------------------------------------------------------------
    # Лимиты на ключ ставятся ОДИН раз — вместе с созданием клиентов
    # (`apply_key_limits` в ветке выше). Повторно здесь их не трогаем: при
    # общей очереди второй поставщик задач обнулил бы `_inflight_by_key`
    # первому, и оба начали бы считать слоты свободными.
    if not _shared_cfg:
        apply_key_limits(clients_cfg, max_parallel=max_parallel)
    accumulated_ok: Dict[int, Dict] = {}
    all_clip_results: List[Dict] = []  # ВСЕ результаты для статистики
    pending_prompts = {cid: prompts_map[cid] for cid in pending_ids}
    _tech_fail_cycles: Dict[int, int] = {}  # clip_id -> кол-во подряд циклов с техн. ошибкой в Phase1

    # worker_id counter — потокобезопасный
    _worker_counter = {'next': 0}
    _worker_lock = threading.Lock()
    _worker_map: Dict[int, int] = {}  # thread_id -> worker_id

    def _get_worker_id() -> int:
        tid = threading.get_ident()
        with _worker_lock:
            if tid not in _worker_map:
                _worker_map[tid] = _worker_counter['next']
                _worker_counter['next'] += 1
            return _worker_map[tid]

    cycle_num = 0
    start_total = time.time()
    was_interrupted = False

    # ── Graceful shutdown handler ─────────────────────────────────────
    import signal

    _original_sigint = signal.getsignal(signal.SIGINT)

    def _graceful_shutdown(signum, frame):
        """Ctrl+C handler: ставим флаг + отменяем in-flight задачи."""
        if _shutdown_requested.is_set():
            # Второй Ctrl+C — жёсткий выход
            print(f"\n\n  {_RED}[FORCE QUIT]{_RESET} Second Ctrl+C — exiting immediately!")
            signal.signal(signal.SIGINT, _original_sigint)
            raise KeyboardInterrupt
        _shutdown_requested.set()
        print(f"\n\n  {_YELLOW}{'='*60}{_RESET}")
        print(f"  {_YELLOW}[SHUTDOWN]{_RESET} Ctrl+C received! Graceful shutdown...")
        print(f"  {_YELLOW}[SHUTDOWN]{_RESET} No new tasks will be submitted.")
        print(f"  {_YELLOW}[SHUTDOWN]{_RESET} Cancelling in-flight tasks via API...")

        # Cancel all tasks for all clients (cancel_all_tasks per key)
        total_cancelled = 0
        for cfg in clients_cfg:
            try:
                n = cfg['client'].cancel_all_tasks()
                total_cancelled += n
                print(f"  {_YELLOW}[SHUTDOWN]{_RESET} [{cfg['label']}] cancelled {n} task(s) on server")
            except Exception as e:
                print(f"  {_YELLOW}[SHUTDOWN]{_RESET} [{cfg['label']}] cancel failed: {e}")

        print(f"  {_YELLOW}[SHUTDOWN]{_RESET} Total cancelled: {total_cancelled} tasks.")
        print(f"  {_YELLOW}[SHUTDOWN]{_RESET} Waiting for workers to finish...")
        print(f"  {_YELLOW}[SHUTDOWN]{_RESET} (Press Ctrl+C again for force quit)")
        print(f"  {_YELLOW}{'='*60}{_RESET}\n")

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, _graceful_shutdown)

    # Shared running-average stats across all cycles (thread-safe)
    _ok_run = {'n': 0, 'total': 0.0, 'lock': threading.Lock(), 'wall_start': time.time()}

    def _external_ok_result(cid: int, file_path: Optional[Path] = None, reason: str = 'external') -> Dict[str, Any]:
        return {
            'id': int(cid),
            'status': 'ok',
            'file': str(file_path) if file_path else None,
            'task_id': None,
            'duration_sec': 0,
            'worker_id': -1,
            'key_label': reason,
            'retries': 0,
            'retry_time': 0,
            'error_counts': {},
            'rewritten': False,
            'external': reason,
        }

    def _run_claimed_single(claim, **kwargs):
        try:
            cid = int(kwargs.get('clip_id'))
            existing = clip_media_path(output_dir, cid)
            if existing:
                return _external_ok_result(cid, existing, 'existing_before_run')
            return _process_single_clip(**kwargs)
        finally:
            claim.release()

    try:
        while True:
            if _shutdown_requested.is_set():
                was_interrupted = True
                break

            cycle_num += 1
            cycle_ids = sorted(pending_prompts.keys())
            print(f"\n  [Cycle {cycle_num}] Generating {len(cycle_ids)} videos...")

            cycle_results: List[Dict] = []

            # ── Порционная подача задач в ThreadPool ──────────────────────
            # НЕ сабмитим все 700 сразу. Держим в полёте max_parallel задач,
            # новую подаём только когда предыдущая завершилась.
            # Reset worker map, stagger tracking, and 5xx counters for new cycle
            with _worker_lock:
                _worker_map.clear()
            for _cfg in clients_cfg:
                _cfg['5xx_count'] = 0
                _cfg['5xx_cancelled'] = False
                _worker_counter['next'] = 0
            with _started_workers_lock:
                _started_workers.clear()

            with ThreadPoolExecutor(max_workers=max_parallel) as executor:
                task_queue = list(cycle_ids)  # копия для потребления
                active_futures: Dict = {}    # future -> clip_id

                def _submit_one():
                    """Подать одну задачу из очереди, если есть."""
                    while task_queue and not _shutdown_requested.is_set():
                        cid = task_queue.pop(0)
                        existing = clip_media_path(output_dir, cid)
                        if existing:
                            cycle_results.append(_external_ok_result(cid, existing, 'existing_before_submit'))
                            continue
                        claim = acquire_clip_claim(output_dir, cid, owner=f'veo_video:{mode}')
                        if not claim:
                            existing = clip_media_path(output_dir, cid)
                            cycle_results.append(_external_ok_result(cid, existing, 'claimed_elsewhere'))
                            continue
                        break
                    else:
                        return
                    image_path = None
                    if mode in ('i2v', 'batch_frame'):
                        image_path = _find_image(cid)
                    # Worker-assignment: клиент определяется ВНУТРИ воркера по worker_id
                    _task_kwargs = dict(
                        client=None,
                        clip_id=cid,
                        prompt=pending_prompts[cid],
                        mode=mode,
                        image_path=image_path,
                        ref_dir=effective_ref_dir if mode == 'components' else None,
                        output_dir=output_dir,
                        aspect_ratio=aspect_ratio,
                        worker_id_fn=_get_worker_id,
                        single_prompt_mode=bool(single_prompt),
                        end_image_path=end_image_map.get(cid) if mode == 'batch_frame' else None,
                        clients_cfg_list=clients_cfg,
                        ok_run=_ok_run,
                        upscale_video=upscale_video,
                        image_regen_prompt=image_regen_prompts_map.get(cid),
                        scene_seconds=_scene_seconds.get(cid),
                    )
                    if queue is not None:
                        # Общая очередь прогона: рабочие наняты один раз, слоты
                        # ключа считаются в одном месте, и параллакс кладёт сюда
                        # же. Свой пул при этом не используется вовсе.
                        fut = queue.put(_run_claimed_single, claim,
                                        label=f'clip {cid}', **_task_kwargs)
                    else:
                        fut = executor.submit(_run_claimed_single, claim,
                                              **_task_kwargs)
                    active_futures[fut] = cid

                # Начальная порция — ровно max_parallel задач под реальные server slots.
                for _ in range(min(max_parallel, len(task_queue))):
                    _submit_one()

                _cycle_ok_durs = []
                _cycle_total = len(cycle_ids)
                _cycle_done = 0
                _cycle_ok = 0
                if IS_PIPE:
                    print(f"  Cycle {cycle_num}: 0/{_cycle_total} clips", flush=True)
                _tqdm_ctx = tqdm(total=_cycle_total, desc=f"  Cycle {cycle_num}", unit="clip", smoothing=0) if not IS_PIPE else None
                while active_futures:
                    # Ждём завершения ПЕРВОГО из текущих (timeout=2s для responsiveness)
                    done, _ = wait(active_futures.keys(), timeout=2, return_when=FIRST_COMPLETED)

                    if not done:
                        # Timeout — проверяем shutdown
                        if _shutdown_requested.is_set():
                            # Drain: ждём все оставшиеся futures
                            if _tqdm_ctx:
                                _tqdm_ctx.set_postfix_str("shutting down...")
                            remaining = list(active_futures.keys())
                            for fut in remaining:
                                try:
                                    clip_result = fut.result(timeout=300)
                                    cid = active_futures.pop(fut)
                                    cycle_results.append(clip_result)
                                except Exception:
                                    cid = active_futures.pop(fut)
                                    cycle_results.append({
                                        'id': cid, 'status': 'error',
                                        'error': 'shutdown', 'file': None,
                                        'task_id': None, 'duration_sec': 0,
                                        'worker_id': -1, 'retries': 0,
                                        'retry_time': 0, 'error_counts': {},
                                        'rewritten': False,
                                    })
                                _cycle_done += 1
                                if _tqdm_ctx:
                                    _tqdm_ctx.update(1)
                                if IS_PIPE:
                                    print(f"PROGRESS:{_cycle_done}/{_cycle_total}", flush=True)
                            break
                        continue

                    for future in done:
                        cid = active_futures.pop(future)
                        try:
                            clip_result = future.result()
                            cycle_results.append(clip_result)
                            icon = '+' if clip_result['status'] == 'ok' else 'X'
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
                                'worker_id': -1, 'retries': 0, 'retry_time': 0,
                                'error_counts': {}, 'rewritten': False,
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

                        # Подаём следующую задачу (если нет shutdown)
                        if not _shutdown_requested.is_set():
                            _submit_one()
                if _tqdm_ctx:
                    _tqdm_ctx.close()

            # Разбор результатов
            all_clip_results.extend(cycle_results)
            for r in cycle_results:
                cid = r['id']
                if r['status'] == 'ok':
                    accumulated_ok[cid] = r
                    pending_prompts.pop(cid, None)
                    _tech_fail_cycles.pop(cid, None)
                else:
                    # Трекаем подряд идущие циклы с техн. ошибкой (Phase1)
                    _tech_fail_cycles[cid] = _tech_fail_cycles.get(cid, 0) + 1
                    if _tech_fail_cycles[cid] >= config.VEONONSTOP_MAX_SILENT_CYCLES:
                        # 2 цикла подряд техн. ошибка — static fallback
                        _img_path = _find_image(cid)
                        if _img_path and Path(_img_path).is_file():
                            _ext = Path(_img_path).suffix
                            _dest = output_dir / f"{cid:03d}{_ext}"
                            try:
                                shutil.copy(_img_path, str(_dest))
                                logging.warning(f"Clip #{cid}: {config.VEONONSTOP_MAX_SILENT_CYCLES} cycles tech error — static fallback (copied image)")
                                print(f"  {_YELLOW}[STATIC]{_RESET} #{cid}: {config.VEONONSTOP_MAX_SILENT_CYCLES} cycles of tech errors — static fallback")
                                accumulated_ok[cid] = {'id': cid, 'status': 'ok', 'file': str(_dest)}
                                pending_prompts.pop(cid, None)
                            except Exception as _cpe:
                                logging.error(f"Clip #{cid}: static fallback copy failed: {_cpe}")
                        else:
                            logging.warning(f"Clip #{cid}: {config.VEONONSTOP_MAX_SILENT_CYCLES} cycles tech error — no image for static fallback")
                            print(f"  {_YELLOW}[GIVE UP]{_RESET} #{cid}: {config.VEONONSTOP_MAX_SILENT_CYCLES} cycles of tech errors, no image available")
                            pending_prompts.pop(cid, None)

            cycle_ok = sum(1 for r in cycle_results if r['status'] == 'ok')
            failed_ids = sorted(pending_prompts.keys())

            print(f"\n  [Cycle {cycle_num}] OK: {cycle_ok},  still missing: {len(failed_ids)}")

            if not failed_ids:
                break

            if _shutdown_requested.is_set():
                was_interrupted = True
                break

            # ── Silent mode: авто-ретрай до N циклов ─────────────
            if silent:
                _max_cycles = config.VEONONSTOP_MAX_SILENT_CYCLES
                if cycle_num < _max_cycles:
                    print(f"\n  {_YELLOW}[SILENT]{_RESET} {len(failed_ids)} clips failed, auto-retrying (cycle {cycle_num + 1}/{_max_cycles})...")
                    logging.info(f"Silent mode: {len(failed_ids)} failed after cycle {cycle_num}, auto-retrying ({cycle_num + 1}/{_max_cycles})")
                    continue
                else:
                    print(f"\n  {_YELLOW}[SILENT]{_RESET} {len(failed_ids)} clips still failed after {cycle_num} cycles. Giving up.")
                    logging.warning(f"Silent mode: {len(failed_ids)} failed after {cycle_num} cycles (max {_max_cycles}), stopping")
                    break

            # ── Interactive mode: спрашиваем юзера ────────────────
            print("\n" + "=" * 60)
            print(f"  {_RED}MISSING CLIPS ({len(failed_ids)} total):{_RESET}")
            for cid in failed_ids:
                last_r = next((r for r in reversed(cycle_results) if r['id'] == cid), None)
                err = (last_r.get('error', '') if last_r else '')[:80]
                print(f"  #{cid:03d}: {err}")
            print("=" * 60)

            print(f"\n  {_YELLOW}What would you like to do?{_RESET}")
            print(f"    [1] Retry missing clips ({len(failed_ids)})")
            print(f"    [2] Finish — skip failed clips")
            print()

            # Auto-retry when running as subprocess (no TTY / stdin closed) OR inside begoframe GUI
            # (in-process thread — stdin может быть привязан к скрытой CMD-консоли, юзер не видит)
            _is_gui = os.environ.get('BEGO_GUI_MODE') == '1'
            _is_interactive = (
                not _is_gui
                and sys.stdin is not None
                and hasattr(sys.stdin, 'isatty')
                and sys.stdin.isatty()
            )
            if not _is_interactive:
                _reason = 'GUI mode' if _is_gui else 'No TTY'
                print(f"  [AUTO] {_reason} — auto-selecting [1] Retry")
                choice = '1'
            else:
                while True:
                    try:
                        choice = input("  Select [1/2]: ").strip()
                    except EOFError:
                        print("  [AUTO] EOF on stdin — auto-selecting [1] Retry")
                        choice = '1'
                        break
                    if choice == '1':
                        break
                    elif choice == '2':
                        failed_ids = []
                        break
                    print("  Enter 1 or 2")

            if not failed_ids or choice == '2':
                break

    finally:
        # Восстанавливаем оригинальный signal handler
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGINT, _original_sigint)
        _shutdown_requested.clear()
        with _inflight_lock:
            _inflight_tasks.clear()
        if _active_tasks_path is not None:
            _active_tasks_path.unlink(missing_ok=True)

    if was_interrupted:
        print(f"\n  {_YELLOW}[SHUTDOWN]{_RESET} Generation interrupted by user (Ctrl+C).")
        print(f"  {_YELLOW}[SHUTDOWN]{_RESET} Partial results saved. Already generated videos are in vid_img/.")

    # ------------------------------------------------------------------
    # 5b. Video upscale pass (optional)
    # ------------------------------------------------------------------
    _upscale_ok = 0
    _upscale_fail = 0
    if upscale_video and accumulated_ok and not was_interrupted:
        _ups_candidates = [
            (cid, r) for cid, r in accumulated_ok.items()
            if r.get('media_generation_id') and not r.get('upscaled')
        ]
        if _ups_candidates:
            print(f"\n{'='*60}")
            print(f"  VIDEO UPSCALE (1080p): {len(_ups_candidates)} clips")
            print(f"{'='*60}\n")

            def _upscale_one_video(_cid, _r):
                _mgid = _r['media_generation_id']
                _out = output_dir / f"{_cid:03d}.mp4"
                _cl = clients_cfg[_cid % len(clients_cfg)]['client']
                _max_attempts = 2
                for _attempt in range(_max_attempts):
                    try:
                        # Ссылка ускоряет апскейл (50с против 71с), но живёт
                        # ограниченное время: этот проход идёт ПОСЛЕ всей
                        # генерации, и у ранних клипов она может протухнуть —
                        # тогда сервис отвечает NOT_FOUND. Поэтому пробуем со
                        # ссылкой, а на второй попытке идём без неё.
                        _ups_tid = _cl.submit_upsample_video(
                            media_generation_id=_mgid,
                            aspect_ratio=aspect_ratio,
                            video_url=(_r.get('media_url')
                                       if _attempt == 0 else None),
                        )
                        _cl.poll_video_status(_ups_tid)
                        _download_video_limited(
                            _cl, _ups_tid, str(_out), context=f"clip #{_cid} upscale"
                        )
                        return True
                    except Exception as _ue:
                        logging.warning(f"Clip #{_cid}: video upscale failed (attempt {_attempt + 1}/{_max_attempts}): {_ue}")
                        if _attempt < _max_attempts - 1:
                            time.sleep(5)
                return False

            _ups_par = min(max_parallel, len(_ups_candidates))
            with ThreadPoolExecutor(max_workers=_ups_par) as _ups_exec:
                _ups_futs = {_ups_exec.submit(_upscale_one_video, c, r): c for c, r in _ups_candidates}
                _ups_tqdm = tqdm(total=len(_ups_futs), desc="  Upscaling", unit="clip") if not IS_PIPE else None
                for _f in as_completed(_ups_futs):
                    _c = _ups_futs[_f]
                    try:
                        if _f.result():
                            _upscale_ok += 1
                            print(f"  {_GREEN}[UPSCALE]{_RESET} #{_c}: OK")
                        else:
                            _upscale_fail += 1
                            print(f"  {_YELLOW}[UPSCALE]{_RESET} #{_c}: failed (kept original)")
                    except Exception:
                        _upscale_fail += 1
                    if _ups_tqdm:
                        _ups_tqdm.update(1)
                if _ups_tqdm:
                    _ups_tqdm.close()
            print(f"\n  Video upscale: {_upscale_ok} OK, {_upscale_fail} failed")
        elif upscale_video:
            print(f"\n  {_YELLOW}[UPSCALE]{_RESET} No clips have media_generation_id — skipping upscale")

    # ------------------------------------------------------------------
    # 6. Финальная статистика (по LOGGING_REQUIREMENTS)
    # ------------------------------------------------------------------
    elapsed_total = round(time.time() - start_total, 1)
    total_ok = len(accumulated_ok)
    total_failed = len(pending_prompts)
    total_clips = total_ok + total_failed

    # Время
    ok_results = [r for r in all_clip_results if r['status'] == 'ok']
    ok_durations = [r.get('duration_sec', 0) for r in ok_results]
    clean_durations = [r.get('duration_sec', 0) for r in ok_results if r.get('retries', 0) == 0]

    avg_time_per_clip = round(sum(ok_durations) / len(ok_durations), 1) if ok_durations else 0
    avg_time_clean = round(sum(clean_durations) / len(clean_durations), 1) if clean_durations else 0

    total_upload_sec = round(sum(r.get('upload_sec', 0) for r in ok_results), 1)
    total_gen_sec    = round(sum(r.get('gen_sec', 0)    for r in ok_results), 1)
    total_dl_sec     = round(sum(r.get('dl_sec', 0)     for r in ok_results), 1)

    # Ретраи
    total_retries = sum(r.get('retries', 0) for r in all_clip_results)
    total_retry_time = round(sum(r.get('retry_time', 0) for r in all_clip_results), 1)
    total_rewrites = sum(1 for r in all_clip_results if r.get('rewritten'))

    # Worker stats
    worker_stats: Dict[int, Dict] = {}
    for r in all_clip_results:
        wid = r.get('worker_id', -1)
        if wid not in worker_stats:
            worker_stats[wid] = {'ok': 0, 'failed': 0, 'total': 0,
                                  'upload_sec': 0.0, 'gen_sec': 0.0, 'dl_sec': 0.0}
        worker_stats[wid]['total'] += 1
        if r['status'] == 'ok':
            worker_stats[wid]['ok'] += 1
            worker_stats[wid]['upload_sec'] += r.get('upload_sec', 0.0)
            worker_stats[wid]['gen_sec']    += r.get('gen_sec', 0.0)
            worker_stats[wid]['dl_sec']     += r.get('dl_sec', 0.0)
        else:
            worker_stats[wid]['failed'] += 1

    # Errors by type
    error_totals: Dict[str, int] = {}
    for r in all_clip_results:
        for k, v in r.get('error_counts', {}).items():
            error_totals[k] = error_totals.get(k, 0) + v

    # Print
    print("\n" + "=" * 80)
    print(f"  IMG2VIDEO COMPLETE")
    print(f"  Generated: {total_ok}")
    print(f"  Failed:    {total_failed}")
    print(f"  Total:     {total_clips}")
    print(f"  Skipped:   {len(skipped)}")
    print(f"  Total run time: {elapsed_total}s ({elapsed_total/60:.1f} min)")
    print(f"  Output: {output_dir}")
    print()
    print(f"  [TIME STATS]")
    print(f"  Avg time per clip (all ok): {avg_time_per_clip}s")
    print(f"  Avg time per clip (clean, no retries): {avg_time_clean}s")
    print(f"  Total upload time (sum):  {total_upload_sec}s")
    print(f"  Total gen time (sum):     {total_gen_sec}s")
    print(f"  Total download time (sum):{total_dl_sec}s")
    print()
    print(f"  [RETRY STATS]")
    print(f"  Total retries: {total_retries}")
    print(f"  Total retry time: {total_retry_time}s")
    print(f"  Rewrites: {total_rewrites}")
    print()
    print(f"  [WORKER STATS]")
    for wid in sorted(worker_stats.keys()):
        ws = worker_stats[wid]
        print(f"    Worker {wid}: {ws['ok']}/{ws['total']} ok ({ws['failed']} failed)"
              f" | upload={ws['upload_sec']:.1f}s gen={ws['gen_sec']:.1f}s dl={ws['dl_sec']:.1f}s")

    if error_totals:
        print()
        print(f"  [ERRORS BY TYPE]")
        for etype, cnt in sorted(error_totals.items(), key=lambda x: -x[1]):
            print(f"    {etype}: {cnt}")

    print("=" * 80)

    # Log to file too
    logging.info(f"=== img2video complete: {total_ok} ok, {total_failed} failed, {elapsed_total}s ===")
    logging.info(f"  Avg time (all ok): {avg_time_per_clip}s, clean: {avg_time_clean}s")
    logging.info(f"  Total upload: {total_upload_sec}s, gen: {total_gen_sec}s, dl: {total_dl_sec}s")
    logging.info(f"  Total retries: {total_retries}, retry time: {total_retry_time}s, rewrites: {total_rewrites}")
    for wid in sorted(worker_stats.keys()):
        ws = worker_stats[wid]
        logging.info(f"  Worker {wid}: {ws['ok']}/{ws['total']} ok ({ws['failed']} failed)"
                     f" | upload={ws['upload_sec']:.1f}s gen={ws['gen_sec']:.1f}s dl={ws['dl_sec']:.1f}s")
    if error_totals:
        logging.info(f"  Errors by type: {error_totals}")

    # Запись стадии в общий лог прогона проекта. Раньше видеогенерация — самая
    # долгая и дорогая стадия — в `<проект>_run_log.txt` не попадала вовсе: там
    # была только транскрипция (улов 01.09.2026). Свой veo_*.log у стадии есть,
    # но он про технику, а run_log читают, чтобы понять, что вообще делал прогон.
    try:
        from units.inbox_manager import append_run_log
        _rl_params = {
            'Режим:':      mode.upper(),
            'Соотношение:': aspect_ratio or config.VEONONSTOP_ASPECT_RATIO,
            'Рабочих:':    max_parallel,
            # Ключ + версия API: при двух ключах разных версий это единственное
            # место в сводке, где видно, кто работал.
            'Ключи:':      ', '.join(
                f"{cfg['label']}·v{getattr(cfg.get('client'), 'api_version', 1)}"
                for cfg in clients_cfg) or '—',
            'Клипов:':     total_clips,
            'Готово:':     total_ok,
            'Провалено:':  total_failed,
            'Пропущено:':  len(skipped),
            'Время:':      f'{int(elapsed_total) // 60}м {int(elapsed_total) % 60:02d}с',
            'Повторов:':   f'{total_retries} ({total_retry_time}с)',
        }
        if total_rewrites:
            _rl_params['Переписано промптов:'] = total_rewrites
        if error_totals:
            _rl_params['Ошибки:'] = ', '.join(
                f'{k}×{v}' for k, v in sorted(error_totals.items(), key=lambda x: -x[1]))
        append_run_log(project_path, project_path.name, 'ВИДЕОГЕНЕРАЦИЯ', _rl_params)
    except Exception as _rl_exc:                                   # noqa: BLE001
        logging.debug("run_log: запись видеогенерации не удалась: %s", _rl_exc)

    # Удаляем file handler
    logging.getLogger().removeHandler(file_handler)
    file_handler.close()

    return {
        'generated': total_ok,
        'failed': total_failed,
        'skipped': len(skipped),
        'elapsed_sec': elapsed_total,
        'results': list(accumulated_ok.values()),
        'avg_time': avg_time_per_clip,
        'avg_time_clean': avg_time_clean,
        'total_retries': total_retries,
        'total_retry_time': total_retry_time,
        'total_rewrites': total_rewrites,
        'worker_stats': worker_stats,
        'error_totals': error_totals,
    }


# ======================================================================
# CLI
# ======================================================================

