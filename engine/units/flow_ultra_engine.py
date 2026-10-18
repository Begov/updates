"""
flowultra.py — Full-parity FastGen Flow Ultra video orchestrator.

Mirrors units/veo_motion.py for:
  - mode='keyframes'     (i2v — start_image + optional end_image)
  - mode='t2v'           (text → video, no image)
  - mode='ingredients'   (t2v + 1-3 ref images, analog components)

Public API:
  generate_all_videos_flow_ultra(project_dir, mode, max_parallel, ...) -> dict
      Same contract as veo_video.generate_all_videos — can be swapped at call site.

Internal structure (mirrors veo_video.py top-to-bottom for diff-ability):
  _submit_and_generate_fu   — one submit+poll+download cycle (blocking)
  _run_phase_fu             — retry loop with censor/person/transient classification
  _process_single_clip_fu   — Phase1 → Phase2 chain (rewrite×2 + remix×2 + minimalistic + static)
  generate_all_videos_flow_ultra — batch orchestrator with cycle retry, tech_fail fallback

Shared chain helpers (engine-agnostic) imported from units.veo_motion:
  _is_content_policy_error, _is_prominent_person_error, _is_desk_transient_error,
  _extract_google_error_reason, _GOOGLE_REMIX_HINTS, _GOOGLE_COOLDOWN_REASONS,
  _rewrite_blocked_prompt, _image_remix, _count_error, ShutdownRequested.

FastGen Flow Ultra differences vs VeoNonStop (not applicable in FU):
  - No cookie slots_full (no cookie pool in FastGen)
  - No reCAPTCHA resubmit chain
  - No hung detection (poll_timeout built into client method)
  - No 5xx cancel-all per key (FastGen more stable, but keep generic 5xx cooldown)
  - No banana regen step in IP image chain (FastGenClient has no generate_banana)
  - No video upscale pass (no FU endpoint)
  - T2V static fallback uses PIL placeholder (no blocking banana API)

Shutdown event + active_clients registry shared with standalone Control Panel via fastgen.py.
"""

from __future__ import annotations

import os
import sys
import json
import re
import time
import random
import logging
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional, Dict, List, Any, Tuple
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED, as_completed

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


IS_PIPE = not _stream_is_tty(sys.stdout)

# Ensure agent/ + agent/modules/ on sys.path for cross-module imports
_agent_dir = str(Path(__file__).resolve().parent.parent)
_modules_dir = str(Path(__file__).resolve().parent)
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)
if _modules_dir not in sys.path:
    sys.path.insert(0, _modules_dir)

from tqdm import tqdm

import params as config
try:
    from units.fastgen_engine import (
        FastGenClient,
        GenerationError,
        FastGenUsageError,
        _flowultra_active_clients,
        _flowultra_shutdown_event,
    )
except Exception:
    from fastgen_engine import (
        FastGenClient,
        GenerationError,
        FastGenUsageError,
        _flowultra_active_clients,
        _flowultra_shutdown_event,
    )
# Shared classifiers + chain helpers — engine-agnostic logic from veo_video
from veo_motion import (
    ShutdownRequested,
    _is_content_policy_error,
    _is_prominent_person_error,
    _is_desk_transient_error,
    _extract_google_error_reason,
    _GOOGLE_REMIX_HINTS,
    _GOOGLE_COOLDOWN_REASONS,
    _rewrite_blocked_prompt,
    _image_remix,
    _count_error,
)
from motion_claims import acquire_clip_claim, clip_media_path

try:
    from prompt_extract import extract_all_prompts, remap_dense_prompts_for_montage
except Exception:
    extract_all_prompts = None  # type: ignore
    def remap_dense_prompts_for_montage(prompts, clips):  # type: ignore
        return prompts, False

# ANSI colors (match veo_video for visual parity)
try:
    import colorama
    from colorama import Fore, Style
    colorama.init(autoreset=True)
    _YELLOW = Fore.YELLOW
    _RED = Fore.RED
    _GREEN = Fore.GREEN
    _CYAN = Fore.CYAN
    _RESET = Style.RESET_ALL
except Exception:
    _YELLOW = _RED = _GREEN = _CYAN = _RESET = ''

# Force flush on every print
import functools as _ft
print = _ft.partial(print, flush=True)  # noqa: A001


# ==============================================================================
# Shared state (worker stagger tracking — mirroring veo_video)
# ==============================================================================

_started_workers: set = set()
_started_workers_lock = threading.Lock()


def _sleep_interruptible(seconds: float, step: float = 0.25) -> bool:
    """Sleep in small chunks. Returns True if Flow Ultra shutdown was requested."""
    if seconds <= 0:
        return _flowultra_shutdown_event.is_set()
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _flowultra_shutdown_event.is_set():
            return True
        time.sleep(min(step, max(0.0, deadline - time.time())))
    return _flowultra_shutdown_event.is_set()


# ==============================================================================
# Prompt/ref helpers
# ==============================================================================

def parse_prompts_file(pf_path: Path) -> Dict[int, str]:
    """Parse prompts file. Two formats:
      A) blank-line separated paragraphs (no prefix) → line N = clip N
      B) prefixed lines '[N] ...', 'N: ...', '[CLIP N] ...'
    Returns {clip_id: prompt_text}.
    """
    raw = pf_path.read_text(encoding='utf-8', errors='replace')
    prompts_map: Dict[int, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        m = re.match(r'^\s*(?:\[CLIP\s*)?\[?(\d+)\]?[\s:\.\)\-]+\s*(.+)$', line, re.IGNORECASE)
        if m:
            cid = int(m.group(1))
            txt = m.group(2).strip()
            if txt and cid not in prompts_map:
                prompts_map[cid] = txt
    if not prompts_map:
        blocks = re.split(r'\n\s*\n', raw)
        seq = 1
        for blk in blocks:
            blk = blk.strip()
            if not blk or blk.startswith('#'):
                continue
            txt = ' '.join(ln.strip() for ln in blk.splitlines() if ln.strip())
            if txt:
                prompts_map[seq] = txt
                seq += 1
    return prompts_map


def find_prompts_file(project_dir: Path) -> Optional[Path]:
    """Auto-pick newest *_veo_adapt_*.txt > *_prompts_*.txt (as veo_video does)."""
    for pat in ('*_veo_adapt_*.txt', '*_prompts_*.txt'):
        cand = sorted(project_dir.glob(pat), key=lambda f: f.stat().st_mtime)
        if cand:
            return cand[-1]
    return None


_IMG_EXTS = ('.jpg', '.jpeg', '.png', '.webp')


def _collect_ref_images(ref_dir: Optional[str], limit: int = 3) -> List[str]:
    """Return list of reference image paths from ref_dir (max `limit`)."""
    if not ref_dir:
        return []
    d = Path(ref_dir)
    if not d.is_dir():
        return []
    refs = sorted([str(p) for p in d.iterdir() if p.suffix.lower() in _IMG_EXTS])
    return refs[:limit]


def _find_image_for_clip(directory: Path, clip_id: int) -> Optional[Path]:
    """Find image for clip N in directory (NNN.<ext>)."""
    for ext in _IMG_EXTS:
        cand = directory / f"{clip_id:03d}{ext}"
        if cand.exists():
            return cand
    return None


def _has_flow_ultra_subscription(client: FastGenClient) -> bool:
    """Check via account usage that api_key has Flow Ultra threads > 0."""
    try:
        usage = client.get_usage()
        threads = (usage.get('account_limits', {})
                        .get('flow_ultra_threads_allowed', 0))
        return int(threads) > 0
    except Exception as e:
        logging.warning(f"flowultra: failed to check subscription: {e}")
        return False


def _get_flow_ultra_threads(client: FastGenClient) -> Optional[int]:
    """Return flow_ultra_threads_allowed, or None when usage preflight is unavailable."""
    try:
        usage = client.get_usage()
        return int(usage.get('account_limits', {}).get('flow_ultra_threads_allowed', 0) or 0)
    except FastGenUsageError:
        raise
    except Exception as e:
        logging.warning(f"flowultra: threads lookup failed: {e}")
        raise FastGenUsageError(f"Flow Ultra threads lookup failed: {e}") from e


def _is_flowultra_protected_image_error(error_msg: str) -> bool:
    """FastGen-specific image policy block: logos, trademarks, protected content."""
    if not error_msg:
        return False
    msg = error_msg.lower()
    image_markers = (
        'image error',
        'image contains',
        'input image',
        'start image',
        'reference image',
    )
    protected_markers = (
        'protected content',
        'logo',
        'trademark',
        'copyright',
        'copyrighted',
        'commercial design',
        'brand mark',
    )
    return any(p in msg for p in image_markers) and any(p in msg for p in protected_markers)


def _is_flowultra_generation_timeout(error_msg: str) -> bool:
    """Server spent a full Flow Ultra generation and returned timeout."""
    if not error_msg:
        return False
    msg = error_msg.lower()
    return (
        'public_error_video_generation_timed_out' in msg
        or 'video_generation_timed_out' in msg
        or 'video generation timed out' in msg
    )


# ==============================================================================
# T2V static fallback placeholder (PIL-based — FastGen has no blocking banana API)
# ==============================================================================

def _fu_placeholder_image(prompt: str, output_path: Path, aspect_ratio: str = '16:9') -> bool:
    """Generate a dark placeholder with prompt snippet overlay.
    Used as last-resort T2V static fallback when all chain steps exhausted.
    Returns True on success."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return False
    sizes = {'16:9': (1280, 720), '9:16': (720, 1280), '1:1': (1024, 1024)}
    w, h = sizes.get(aspect_ratio, (1280, 720))
    try:
        img = Image.new('RGB', (w, h), color=(18, 18, 18))
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype('arial.ttf', 28)
        except Exception:
            try:
                font = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 28)
            except Exception:
                font = ImageFont.load_default()
        txt = (prompt or '(no prompt)')[:240]
        # naive wrap
        words = txt.split()
        lines: List[str] = []
        line = ''
        for word in words:
            if len(line) + len(word) + 1 <= 60:
                line = f"{line} {word}".strip()
            else:
                lines.append(line)
                line = word
        if line:
            lines.append(line)
        y = 40
        for ln in lines[:14]:
            draw.text((40, y), ln, fill=(200, 200, 200), font=font)
            y += 36
        output_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(str(output_path), 'PNG')
        return True
    except Exception as e:
        logging.warning(f"_fu_placeholder_image: save failed: {e}")
        return False


# ==============================================================================
# Low-level: one submit+poll+download cycle
# ==============================================================================

def _submit_and_generate_fu(
    client: FastGenClient,
    clip_id: int,
    prompt: str,
    mode: str,                      # 'keyframes' | 't2v' | 'ingredients'
    image_path: Optional[str],
    ref_paths: Optional[List[str]],
    end_image_path: Optional[str],
    output_path: Path,
    aspect_ratio: str,
) -> float:
    """One Flow Ultra submit+poll+download cycle (blocking).
    Returns t_gen_sec. Raises ShutdownRequested or GenerationError."""
    if _flowultra_shutdown_event.is_set():
        raise ShutdownRequested(f"Clip #{clip_id}: shutdown before submit")
    # Micro-jitter to avoid thundering-herd at phase start
    if _sleep_interruptible(random.uniform(0.05, 0.1)):
        raise ShutdownRequested(f"Clip #{clip_id}: shutdown before submit")
    t0 = time.time()
    # V6: единый POST /api/v6/generations с operation flow_ultra_video_from_*.
    # V4/V5 удалены сервером (404). Ingredients — named refs (stem файла → токен в промпте);
    # keyframes — позиционные кадры (старт/энд, внутри generate_v6_video).
    if FastGenClient._v6_video_operation(mode) is None:
        raise ValueError(f"Unknown Flow Ultra mode: {mode!r}")
    reference_pairs: List[Tuple[str, str]] = []
    if mode == 'keyframes':
        if not image_path or not Path(image_path).exists():
            raise GenerationError(f"no start_image for clip #{clip_id:03d}: {image_path}")
        # Keyframes: старт-кадр [+ энд-кадр] — позиционно (порядок задаёт start/end).
        start_uri = client.upload_reference(str(image_path))
        reference_pairs.append(('start_frame', start_uri))
        if end_image_path and Path(end_image_path).exists():
            end_uri = client.upload_reference(str(end_image_path))
            reference_pairs.append(('end_frame', end_uri))
    elif mode == 'ingredients':
        if not ref_paths:
            raise GenerationError(f"ingredients mode requires refs (clip #{clip_id:03d})")
        for p in ref_paths:
            if not Path(p).exists():
                continue
            uri = client.upload_reference(str(p))
            # Имя файла С расширением → токен [name_ref] в промпте (substring match на сервере).
            reference_pairs.append((Path(p).name, uri))
        if not reference_pairs:
            raise GenerationError(f"ingredients: no valid refs uploaded (clip #{clip_id:03d})")
    # t2v: empty reference_pairs
    response = client.generate_v6_video(
        prompt=prompt,
        mode=mode,
        reference_pairs=reference_pairs,
        aspect_ratio=aspect_ratio,
        output_path=str(output_path),
        # poll_timeout как у veononstop videogen (config/.env, не хардкод 900с=15мин).
        poll_timeout=int(getattr(config, 'VEONONSTOP_POLL_TIMEOUT', 180) or 180),
    )
    if response.get('status') != 'succeeded':
        err = response.get('error') or response.get('status') or 'unknown v6 video failure'
        raise GenerationError(f"V6 video failed ({mode}): {err}")
    return round(time.time() - t0, 1)


# ==============================================================================
# Retry phase — aligned with veo_video._run_phase, FU-applicable branches only
# ==============================================================================

def _run_phase_fu(
    client: FastGenClient,
    clip_id: int,
    prompt: str,
    mode: str,
    image_path: Optional[str],
    ref_paths: Optional[List[str]],
    end_image_path: Optional[str],
    output_path: Path,
    aspect_ratio: str,
    max_retries: int,
    phase_label: str,
    worker_id: int = 0,
    key_label: str = 'Key1',
) -> Dict[str, Any]:
    """Retry phase for Flow Ultra. Returns same dict shape as veo_video._run_phase."""
    censored = False
    prominent_person = False
    ip_image_blocked = False
    no_accounts = False
    last_error = ''
    remix_hint: Optional[str] = None
    google_reason: Optional[str] = None
    status_failed_count = 0
    ratelimit_count = 0
    generation_timeout_count = 0
    error_counts: Dict[str, int] = {}
    retry_time = 0.0
    tag = f"[W{worker_id}/{key_label}]"

    attempt = 0
    while attempt < max_retries:
        attempt += 1
        start = time.time()
        try:
            if _flowultra_shutdown_event.is_set():
                break
            t_gen = _submit_and_generate_fu(
                client, clip_id, prompt, mode, image_path, ref_paths,
                end_image_path, output_path, aspect_ratio,
            )
            elapsed = round(time.time() - start, 1)
            return {
                'ok': True, 'task_id': None, 'elapsed': elapsed,
                'upload_sec': 0.0, 'gen_sec': t_gen, 'dl_sec': 0.0,
                'retries': attempt - 1, 'retry_time': round(retry_time, 1),
                'error_counts': error_counts, 'error': '',
                'censored': False, 'prominent_person': False,
                'ip_image_blocked': False,
                'google_reason': None, 'remix_hint': None,
                'media_generation_id': None,
            }
        except ShutdownRequested:
            raise
        except Exception as e:
            if _flowultra_shutdown_event.is_set():
                break
            attempt_elapsed = time.time() - start
            retry_time += attempt_elapsed
            last_error = str(e)
            _count_error(error_counts, last_error)
            err_lower = last_error.lower()
            print(f"[RAW_ERROR] Clip #{clip_id} {tag} [{phase_label}]: {last_error[:500]}")
            logging.info(f"Clip #{clip_id} {tag} [{phase_label}] raw_error: {last_error[:500]}")

            # Google reason → targeted remix hint OR anti-abuse cooldown
            _reason = _extract_google_error_reason(last_error)
            if _reason:
                google_reason = _reason
                if _reason in _GOOGLE_REMIX_HINTS:
                    remix_hint = _GOOGLE_REMIX_HINTS[_reason]
                    print(f"  [GOOGLE REASON] #{clip_id} {tag}: {_reason} → targeted remix hint")
                    logging.info(f"Clip #{clip_id} {tag}: Google reason {_reason} mapped")
                elif _reason in _GOOGLE_COOLDOWN_REASONS:
                    _cd = 60 + random.uniform(0, 30)
                    print(f"\n  {_YELLOW}[COOLDOWN]{_RESET} #{clip_id} {tag}: Google {_reason} — waiting {_cd:.0f}s")
                    logging.warning(f"Clip #{clip_id} {tag} [{phase_label}]: Google {_reason} cooldown {_cd:.0f}s")
                    if _sleep_interruptible(_cd):
                        raise ShutdownRequested(f"Clip #{clip_id}: shutdown during cooldown")
                    attempt -= 1
                    continue

            # Desk-side transient (axios timeout, TCP reset)
            if _is_desk_transient_error(last_error):
                _cd = 45 + random.uniform(0, 20)
                print(f"\n  {_YELLOW}[COOLDOWN]{_RESET} #{clip_id} {tag}: transient error — waiting {_cd:.0f}s")
                logging.warning(f"Clip #{clip_id} {tag} [{phase_label}]: transient cooldown {_cd:.0f}s")
                if _sleep_interruptible(_cd):
                    raise ShutdownRequested(f"Clip #{clip_id}: shutdown during transient cooldown")
                attempt -= 1
                continue

            # 429 ratelimit
            if '429' in err_lower or 'rate limit' in err_lower or 'too many requests' in err_lower:
                ratelimit_count += 1
                _rl_wait = 10 + random.uniform(1, 5)
                print(f"\n  {_YELLOW}[RATELIMIT]{_RESET} #{clip_id} {tag}: 429 ({ratelimit_count}/10), waiting {_rl_wait:.1f}s")
                logging.warning(f"Clip #{clip_id} {tag}: ratelimit 429 ({ratelimit_count}/10)")
                _count_error(error_counts, 'rate limit 429')
                if ratelimit_count >= 10:
                    print(f"\n  {_RED}[RATELIMIT]{_RESET} #{clip_id} {tag}: 10 retries exhausted")
                    break
                if _sleep_interruptible(_rl_wait):
                    raise ShutdownRequested(f"Clip #{clip_id}: shutdown during ratelimit cooldown")
                attempt -= 1
                continue

            # FastGen image policy block: deterministic, do not retry same image 5 times.
            if _is_flowultra_protected_image_error(last_error):
                ip_image_blocked = True
                remix_hint = _GOOGLE_REMIX_HINTS.get('PUBLIC_ERROR_IP_INPUT_IMAGE')
                print(f"\n  {_RED}[IP IMAGE]{_RESET} #{clip_id} {tag}: protected logo/trademark content in image")
                logging.warning(f"Clip #{clip_id} {tag} [{phase_label}]: protected image content — go remix/static")
                break

            # Server-side Flow Ultra generation timeout already consumed a full run.
            if _is_flowultra_generation_timeout(last_error):
                generation_timeout_count += 1
                _count_error(error_counts, 'flow_ultra_generation_timeout')
                _to_max = int(os.getenv('FLOWULTRA_MAX_TIMEOUT_RETRIES', '1') or '1')
                print(f"\n  {_YELLOW}[TIMEOUT]{_RESET} #{clip_id} {tag}: Flow Ultra generation timed out ({generation_timeout_count}/{_to_max})")
                logging.warning(
                    f"Clip #{clip_id} {tag} [{phase_label}]: Flow Ultra generation timed out "
                    f"({generation_timeout_count}/{_to_max})"
                )
                if generation_timeout_count >= _to_max:
                    break
                if _sleep_interruptible(5):
                    raise ShutdownRequested(f"Clip #{clip_id}: shutdown during FU timeout retry")
                continue

            # Polling timeout
            if 'polling timeout' in err_lower or 'timeout' in err_lower:
                _count_error(error_counts, 'timeout')
                print(f"\n  {_YELLOW}[TIMEOUT]{_RESET} #{clip_id} {tag}: poll timeout — retry")
                logging.warning(f"Clip #{clip_id} {tag}: polling timeout — retry")
                if attempt < max_retries:
                    if _sleep_interruptible(5):
                        raise ShutdownRequested(f"Clip #{clip_id}: shutdown during timeout retry")
                continue

            # No ultra video accounts — рантайм-деградация FastGen (ultra-аккаунты заняты).
            # НЕ ретраим (аккаунтов нет — 4 ретрая × cooldown = 740с впустую держат claim).
            # Сразу break → claim освобождается → veononstop забирает клип (в both-режиме).
            if 'no accounts available' in err_lower or 'ultra accounts only' in err_lower:
                no_accounts = True
                print(f"\n  {_YELLOW}[NO ULTRA ACCT]{_RESET} #{clip_id} {tag}: video accounts unavailable — yielding to VeoNonStop")
                logging.warning(f"Clip #{clip_id} {tag}: no ultra video accounts — break+release, yield to veononstop")
                break

            # IP image blocked (content fingerprint)
            if 'ip_input_image' in err_lower or 'public_error_ip_input_image' in err_lower:
                ip_image_blocked = True
                print(f"\n  {_RED}[IP IMAGE]{_RESET} #{clip_id} {tag}: input image blocked by IP filter")
                logging.warning(f"Clip #{clip_id} {tag}: IP_INPUT_IMAGE")
                break

            # Prominent person
            if _is_prominent_person_error(last_error):
                prominent_person = True
                print(f"\n  {_RED}[PERSON]{_RESET} #{clip_id} {tag}: prominent person filter")
                logging.warning(f"Clip #{clip_id} {tag} [{phase_label}]: PROMINENT PERSON")
                break

            # Content policy
            if _is_content_policy_error(last_error):
                censored = True
                print(f"\n  {_RED}[CENSORED]{_RESET} #{clip_id} {tag} [{phase_label}] {attempt}/{max_retries}")
                logging.warning(f"Clip #{clip_id} {tag} [{phase_label}]: CENSORED — {e}")
                break

            # status_failed (hidden censorship via Google)
            if 'status_failed' in err_lower or 'media_generation_status_failed' in err_lower:
                status_failed_count += 1
                _sf_threshold = max_retries
                if status_failed_count >= _sf_threshold:
                    censored = True
                    print(f"\n  {_RED}[HIDDEN CENSOR?]{_RESET} #{clip_id} {tag}: {status_failed_count}x STATUS_FAILED — treat as censorship")
                    logging.warning(f"Clip #{clip_id} {tag}: {status_failed_count}x STATUS_FAILED treated as censorship")
                    break
                _sf_delay = status_failed_count * 5
                print(f"\n  {_YELLOW}[STATUS_FAILED]{_RESET} #{clip_id} {tag}: ({status_failed_count}/{_sf_threshold}) retry in {_sf_delay}s")
                if _sleep_interruptible(_sf_delay):
                    raise ShutdownRequested(f"Clip #{clip_id}: shutdown during status_failed retry")
                continue

            # invalid argument — hidden person censorship from Google
            if 'invalid argument' in err_lower:
                prominent_person = True
                print(f"\n  {_RED}[INVALID ARG → PERSON?]{_RESET} #{clip_id} {tag}: treat as person censorship")
                logging.warning(f"Clip #{clip_id} {tag}: invalid argument → person")
                break

            # Fatal auth — poison the operation
            _fatal_kw = ['license_expired', 'license expired', 'credits insufficient',
                         'account suspended', 'unauthorized', '401', '403']
            if any(kw in err_lower for kw in _fatal_kw):
                print(f"\n  {_RED}[AUTH FATAL]{_RESET} #{clip_id} {tag}: {e}")
                logging.error(f"Clip #{clip_id} {tag} [{phase_label}]: FATAL auth — {e}")
                break

            # 5xx server — cooldown, don't count as attempt
            _5xx_kw = ['500', '502', '503', 'server error', 'internal server error',
                       'service unavailable', 'bad gateway']
            if any(kw in err_lower for kw in _5xx_kw):
                _5xx_wait = min(10 + attempt * 5, 30)
                print(f"\n  {_YELLOW}[SERVER]{_RESET} #{clip_id} {tag}: 5xx, waiting {_5xx_wait}s")
                logging.warning(f"Clip #{clip_id} {tag} [{phase_label}]: 5xx waiting {_5xx_wait}s")
                if _sleep_interruptible(_5xx_wait):
                    raise ShutdownRequested(f"Clip #{clip_id}: shutdown during server cooldown")
                attempt -= 1
                continue

            # Generic error — retry with standard delays
            print(f"\n  {_YELLOW}[ERROR]{_RESET} #{clip_id} {tag} [{phase_label}] {attempt}/{max_retries}: {str(e)[:150]}")
            logging.error(f"Clip #{clip_id} {tag} [{phase_label}] attempt {attempt}/{max_retries}: {e}")
            if attempt < max_retries:
                _delay = [5, 5, 10][min(attempt - 1, 2)]
                if _sleep_interruptible(_delay):
                    raise ShutdownRequested(f"Clip #{clip_id}: shutdown during retry delay")

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
        'no_accounts': no_accounts,
        'google_reason': google_reason,
        'remix_hint': remix_hint,
    }


# ==============================================================================
# Clip orchestrator — Phase1 → Phase2 chain
# ==============================================================================

def _process_single_clip_fu(
    client: Optional[FastGenClient],
    clip_id: int,
    prompt: str,
    mode: str,
    image_path: Optional[str],
    ref_paths: Optional[List[str]],
    end_image_path: Optional[str],
    output_dir: Path,
    aspect_ratio: str = '16:9',
    max_retries: Optional[int] = None,
    worker_id_fn=None,
    single_prompt_mode: bool = False,
    clients_cfg_list=None,           # if provided: worker-assignment by worker_id
    ok_run: Optional[dict] = None,   # shared running-avg stats
) -> Dict[str, Any]:
    """Generate one clip with full Phase1 → Phase2 chain (matching veo_video._process_single_clip).
    Returns same-shape dict as veo_video version."""
    if max_retries is None:
        max_retries = getattr(config, 'VEONONSTOP_MAX_RETRIES', 5)
    worker_id = worker_id_fn() if worker_id_fn else 0
    clip_start_ts = time.time()
    clip_start_time = datetime.now().strftime('%H:%M:%S')
    tag = f"[W{worker_id}]"

    result: Dict[str, Any] = {
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

    if _flowultra_shutdown_event.is_set():
        result['error'] = 'shutdown requested'
        return result

    # Worker-assignment: pick client/sem/label by worker_id (dual-key)
    _key_label = 'Key1'
    _selected_cfg = None
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
        _key_label = _selected_cfg['label']
        _local_worker_id = worker_id - _group_start
    else:
        _local_worker_id = worker_id
    result['key_label'] = _key_label

    # Key dead check (poisoned by another worker's auth fatal)
    if clients_cfg_list and _selected_cfg and _selected_cfg.get('key_dead'):
        result['error'] = f'key {_key_label} is dead'
        logging.warning(f"Clip #{clip_id} [W{worker_id}]: skip — key {_key_label} dead")
        return result

    # Stagger — only first-run per worker
    tag = f"[W{worker_id}/{_key_label}]"
    with _started_workers_lock:
        is_first_run = worker_id not in _started_workers
        _started_workers.add(worker_id)
    if is_first_run:
        stagger = _local_worker_id * getattr(config, 'VEONONSTOP_REQUEST_DELAY', 0.0)
    else:
        stagger = 0.0
    jitter = random.uniform(0.05, 0.1)
    total_delay = stagger + jitter
    logging.info(f"Clip #{clip_id} {tag}: stagger {stagger}s + jitter {jitter:.1f}s (first={is_first_run})")
    print(f"  {_YELLOW}[START]{_RESET}  #{clip_id} {tag}: launching in {total_delay:.1f}s...")
    if _sleep_interruptible(total_delay):
        result['error'] = 'shutdown requested'
        result['duration_sec'] = round(time.time() - clip_start_ts, 1)
        logging.info(f"Clip #{clip_id} {tag}: shutdown during stagger")
        return result

    # ── Phase 1: original prompt ─────────────────────────────────────────
    try:
        phase1 = _run_phase_fu(
            client, clip_id, prompt, mode, image_path, ref_paths,
            end_image_path, output_path, aspect_ratio, max_retries,
            'phase1', worker_id=worker_id, key_label=_key_label,
        )
    except ShutdownRequested:
        result['status'] = 'error'
        result['error'] = 'shutdown requested'
        result['duration_sec'] = round(time.time() - clip_start_ts, 1)
        logging.info(f"Clip #{clip_id} {tag}: shutdown during phase1")
        return result

    # Validate downloaded video (FU writes directly via base64 decode — no redownload path)
    if phase1['ok']:
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
            _sz = output_path.stat().st_size if output_path.exists() else 0
            print(f"  {_RED}[CORRUPT]{_RESET} #{clip_id} {tag}: invalid video ({_sz} bytes) — treat as fail")
            logging.error(f"Clip #{clip_id} {tag}: corrupt video after phase1 ({_sz} bytes)")
            phase1['ok'] = False
            phase1['error'] = 'corrupt video after download'

    if phase1['ok']:
        elapsed = phase1['elapsed']
        t_gen = phase1.get('gen_sec', 0.0)
        retries = phase1['retries']
        retry_time = phase1['retry_time']
        error_counts = phase1['error_counts']
        result.update({
            'status': 'ok', 'file': str(output_path),
            'duration_sec': elapsed, 'task_id': None, 'phase': 1,
            'retries': retries, 'retry_time': retry_time, 'error_counts': error_counts,
            'gen_sec': t_gen,
        })
        log_line = f"Clip #{clip_id} {tag}: OK phase1 (total={elapsed}s | gen={t_gen}s"
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
                _n = ok_run['n']
            _wall = time.time() - ok_run['wall_start']
            _wall_cpm = _n / (_wall / 60) if _wall > 0 else 0
            _wall_spc = 60.0 / _wall_cpm if _wall_cpm > 0 else 0
            print(f"  {_CYAN}[SPEED]{_RESET} worker avg={_avg:.0f}s/clip | pool {_wall_spc:.1f}s/clip ({_wall_cpm:.1f} clip/min)")
        return result

    # ── No ultra video accounts: не тратим время на remix/static, сразу отдаём.
    # claim освободится в обёртке → veononstop забирает клип (both-режим). ──
    if phase1.get('no_accounts'):
        result.update({
            'status': 'error',
            'error': phase1.get('error', 'no ultra video accounts'),
            'no_accounts': True,
            'duration_sec': round(time.time() - clip_start_ts, 1),
            'retries': phase1.get('retries', 0),
            'error_counts': phase1.get('error_counts', {}),
        })
        return result

    # ── IP image blocked: skip banana regen (FU has no banana), go remix → static ──
    is_ip_blocked = phase1.get('ip_image_blocked', False)
    if is_ip_blocked and mode == 'keyframes' and image_path:
        _ip_combined_errors: Dict[str, int] = dict(phase1.get('error_counts', {}))
        _ip_total_retries = phase1.get('retries', 0)
        _ip_total_retry_time = phase1.get('retry_time', 0.0)

        # Fast-censor modes must not spend another full Flow Ultra run on a
        # deterministic image block (logo/trademark/protected content).
        _fast_censor = config.VEONONSTOP_FAST_CENSOR
        if _fast_censor in ('ultrafast', 'after_rewrites'):
            print(f"  {_YELLOW}[IP STATIC]{_RESET} #{clip_id} {tag}: FAST_CENSOR={_fast_censor} → static")
            logging.warning(f"Clip #{clip_id} {tag}: IP image block + FAST_CENSOR={_fast_censor} → static")
            ext = Path(image_path).suffix
            dest = output_dir / f"{clip_id:03d}{ext}"
            try:
                shutil.copy(image_path, str(dest))
                elapsed = round(time.time() - clip_start_ts, 1)
                result.update({
                    'status': 'ok', 'file': str(dest),
                    'duration_sec': elapsed, 'phase': 2,
                    'retries': _ip_total_retries, 'retry_time': round(_ip_total_retry_time, 1),
                    'error_counts': _ip_combined_errors,
                })
                print(f"  {_YELLOW}[STATIC]{_RESET} Clip #{clip_id} {tag}: copied original ({elapsed}s)")
                logging.warning(f"Clip #{clip_id} {tag}: IP fast static — copied original ({elapsed}s)")
            except Exception as _cpe:
                elapsed = round(time.time() - clip_start_ts, 1)
                result.update({
                    'status': 'error', 'error': f'ip fast static failed: {_cpe}',
                    'duration_sec': elapsed, 'retries': _ip_total_retries,
                    'retry_time': round(_ip_total_retry_time, 1), 'error_counts': _ip_combined_errors,
                })
                logging.error(f"Clip #{clip_id}: IP fast static copy failed: {_cpe}")
            return result

        def _ip_accumulate(ph: dict) -> None:
            nonlocal _ip_total_retries, _ip_total_retry_time
            for k, v in ph.get('error_counts', {}).items():
                _ip_combined_errors[k] = _ip_combined_errors.get(k, 0) + v
            _ip_total_retries += ph.get('retries', 0)
            _ip_total_retry_time += ph.get('retry_time', 0.0)

        # Step 1: remix (banana regen step skipped — FastGenClient has no blocking banana API)
        _remix_engine_label = config.REMIX_ENGINE.upper()
        print(f"  {_RED}[IP REMIX]{_RESET} #{clip_id} {tag}: IP blocked — remixing via {_remix_engine_label}...")
        logging.info(f"Clip #{clip_id} {tag}: IP_INPUT_IMAGE — remix via {_remix_engine_label}")
        _remixed = _image_remix(image_path, clip_id, output_dir,
                                       remix_hint=_GOOGLE_REMIX_HINTS.get('PUBLIC_ERROR_IP_INPUT_IMAGE'))
        _use_img = _remixed if _remixed else image_path
        try:
            _ph = _run_phase_fu(
                client, clip_id, prompt, mode, _use_img, ref_paths,
                end_image_path, output_path, aspect_ratio, max_retries,
                'ip_remix', worker_id=worker_id, key_label=_key_label,
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
                'duration_sec': elapsed, 'phase': 2,
                'retries': _ip_total_retries, 'retry_time': round(_ip_total_retry_time, 1),
                'error_counts': _ip_combined_errors,
                'gen_sec': _ph.get('gen_sec', 0.0),
            })
            print(f"  {_GREEN}[OK]{_RESET} Clip #{clip_id} {tag}: OK (ip_remix) total={elapsed}s")
            logging.info(f"Clip #{clip_id} {tag}: OK after IP remix ({elapsed}s)")
            return result

        # Step 2: static fallback (copy original image)
        print(f"  {_YELLOW}[IP STATIC]{_RESET} #{clip_id} {tag}: IP remix failed → static")
        logging.warning(f"Clip #{clip_id} {tag}: IP_INPUT_IMAGE — remix failed, static fallback")
        ext = Path(image_path).suffix
        dest = output_dir / f"{clip_id:03d}{ext}"
        try:
            shutil.copy(image_path, str(dest))
            elapsed = round(time.time() - clip_start_ts, 1)
            result.update({
                'status': 'ok', 'file': str(dest),
                'duration_sec': elapsed, 'phase': 2,
                'retries': _ip_total_retries, 'retry_time': round(_ip_total_retry_time, 1),
                'error_counts': _ip_combined_errors,
            })
            print(f"  {_YELLOW}[STATIC]{_RESET} Clip #{clip_id} {tag}: copied original ({elapsed}s)")
            logging.warning(f"Clip #{clip_id} {tag}: IP static fallback — copied original ({elapsed}s)")
        except Exception as _cpe:
            elapsed = round(time.time() - clip_start_ts, 1)
            result.update({
                'status': 'error', 'error': f'ip static fallback failed: {_cpe}',
                'duration_sec': elapsed, 'retries': _ip_total_retries,
                'retry_time': round(_ip_total_retry_time, 1), 'error_counts': _ip_combined_errors,
            })
            logging.error(f"Clip #{clip_id}: IP static copy failed: {_cpe}")
        return result

    # ── Rewrite only on person/censored — others are plain tech errors ──
    is_person = phase1.get('prominent_person', False)
    is_censored = phase1.get('censored', False)
    _phase1_remix_hint = phase1.get('remix_hint')

    if not is_person and not is_censored:
        elapsed = round(time.time() - clip_start_ts, 1)
        retries = phase1['retries']
        retry_time = phase1['retry_time']
        error_counts = phase1['error_counts']
        result.update({
            'status': 'error', 'error': phase1.get('error'),
            'duration_sec': elapsed,
            'retries': retries, 'retry_time': retry_time, 'error_counts': error_counts,
        })
        log_line = (f"Clip #{clip_id} {tag}: FAILED phase1 ({elapsed}s) retries={retries} "
                    f"errors={error_counts}: {(phase1.get('error') or '')[:100]}")
        logging.error(log_line)
        print(f"  {_YELLOW}[FAILED]{_RESET} {log_line}")
        return result

    # ── Phase 2: multi-cycle recovery (rewrite / remix / minimalistic / static) ──

    result['phase'] = 2
    combined_errors: Dict[str, int] = dict(phase1.get('error_counts', {}))
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
        t_gen = phase_result.get('gen_sec', 0.0)
        result.update({
            'status': 'ok', 'file': str(output_path),
            'duration_sec': elapsed, 'phase': 2,
            'retries': total_retries, 'retry_time': round(total_retry_time, 1),
            'error_counts': combined_errors,
            'gen_sec': t_gen,
        })
        log_line = f"Clip #{clip_id} {tag}: OK phase2 (total={elapsed}s | gen={t_gen}s{suffix}"
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
                _n = ok_run['n']
            _wall = time.time() - ok_run['wall_start']
            _wall_cpm = _n / (_wall / 60) if _wall > 0 else 0
            _wall_spc = 60.0 / _wall_cpm if _wall_cpm > 0 else 0
            print(f"  {_CYAN}[SPEED]{_RESET} worker avg={_avg:.0f}s/clip | pool {_wall_spc:.1f}s/clip ({_wall_cpm:.1f} clip/min)")
        return result

    def _static_fallback() -> dict:
        """Static fallback: copy image (keyframes/ingredients) or PIL placeholder (t2v)."""
        if mode == 'keyframes' and image_path:
            ext = Path(image_path).suffix
            dest = output_dir / f"{clip_id:03d}{ext}"
            try:
                shutil.copy(image_path, str(dest))
                elapsed = round(time.time() - clip_start_ts, 1)
                log_line = f"Clip #{clip_id} {tag}: STATIC FALLBACK — copied original image ({elapsed}s)"
                print(f"  {_YELLOW}[STATIC]{_RESET} {log_line}")
                logging.warning(log_line)
                result.update({
                    'status': 'ok', 'file': str(dest),
                    'duration_sec': elapsed, 'phase': 2,
                    'retries': total_retries, 'retry_time': round(total_retry_time, 1),
                    'error_counts': combined_errors,
                })
            except Exception as _fe:
                elapsed = round(time.time() - clip_start_ts, 1)
                result.update({
                    'status': 'error', 'error': f'static fallback failed: {_fe}',
                    'duration_sec': elapsed, 'retries': total_retries,
                    'retry_time': round(total_retry_time, 1), 'error_counts': combined_errors,
                })
                logging.error(f"Clip #{clip_id}: static fallback copy failed ({_fe})")
            return result
        if mode == 'ingredients' and ref_paths:
            ref_img = ref_paths[0]
            ext = Path(ref_img).suffix
            dest = output_dir / f"{clip_id:03d}{ext}"
            try:
                shutil.copy(ref_img, str(dest))
                elapsed = round(time.time() - clip_start_ts, 1)
                print(f"  {_YELLOW}[STATIC]{_RESET} Clip #{clip_id} {tag}: ingredients fallback — first ref ({elapsed}s)")
                logging.warning(f"Clip #{clip_id} {tag}: ingredients static — copied first ref")
                result.update({
                    'status': 'ok', 'file': str(dest),
                    'duration_sec': elapsed, 'phase': 2,
                    'retries': total_retries, 'retry_time': round(total_retry_time, 1),
                    'error_counts': combined_errors,
                })
            except Exception as _fe:
                elapsed = round(time.time() - clip_start_ts, 1)
                result.update({
                    'status': 'error', 'error': f'ingredients static failed: {_fe}',
                    'duration_sec': elapsed, 'retries': total_retries,
                    'retry_time': round(total_retry_time, 1), 'error_counts': combined_errors,
                })
            return result
        # T2V: PIL placeholder
        dest = output_dir / f"{clip_id:03d}.png"
        if _fu_placeholder_image(prompt, dest, aspect_ratio):
            elapsed = round(time.time() - clip_start_ts, 1)
            print(f"  {_YELLOW}[STATIC T2V]{_RESET} Clip #{clip_id} {tag}: PIL placeholder ({elapsed}s)")
            logging.warning(f"Clip #{clip_id} {tag}: T2V PIL placeholder fallback")
            result.update({
                'status': 'ok', 'file': str(dest),
                'duration_sec': elapsed, 'phase': 2,
                'retries': total_retries, 'retry_time': round(total_retry_time, 1),
                'error_counts': combined_errors,
            })
        else:
            elapsed = round(time.time() - clip_start_ts, 1)
            result.update({
                'status': 'error', 'error': 't2v PIL placeholder failed',
                'duration_sec': elapsed, 'retries': total_retries,
                'retry_time': round(total_retry_time, 1), 'error_counts': combined_errors,
            })
            logging.error(f"Clip #{clip_id}: t2v PIL placeholder failed")
        return result

    def _phase2_attempt(use_prompt: str, use_image: Optional[str], label: str,
                        override_ref_paths: Optional[List[str]] = None) -> dict:
        effective_refs = override_ref_paths if override_ref_paths is not None else ref_paths
        return _run_phase_fu(
            client, clip_id, use_prompt, mode, use_image, effective_refs,
            end_image_path, output_path, aspect_ratio, max_retries, label,
            worker_id=worker_id, key_label=_key_label,
        )

    def _binary_search_blocked_refs(ref_files_list: List[str]) -> List[str]:
        """Binary search blocked refs via FU ingredients. Each test = real API call."""
        if not ref_files_list:
            return []
        if len(ref_files_list) == 1:
            try:
                _ph = _phase2_attempt(prompt, None, f'bsearch_{Path(ref_files_list[0]).stem}',
                                      override_ref_paths=[ref_files_list[0]])
                _accumulate(_ph)
                return [ref_files_list[0]] if _ph.get('censored') else []
            except ShutdownRequested:
                raise
            except Exception as _e:
                logging.warning(f"Clip #{clip_id}: bsearch test failed {Path(ref_files_list[0]).name}: {_e}")
                return []
        mid = len(ref_files_list) // 2
        bad: List[str] = []
        for half, lbl in ((ref_files_list[:mid], 'L'), (ref_files_list[mid:], 'R')):
            try:
                _ph = _phase2_attempt(prompt, None, f'bsearch_{lbl}',
                                      override_ref_paths=list(half))
                _accumulate(_ph)
                if _ph.get('censored'):
                    bad.extend(_binary_search_blocked_refs(list(half)))
            except ShutdownRequested:
                raise
            except Exception as _e:
                logging.warning(f"Clip #{clip_id}: bsearch half {lbl} failed: {_e}")
        return bad

    # FAST_CENSOR=ultrafast → immediate static
    _fast_censor = config.VEONONSTOP_FAST_CENSOR
    if _fast_censor == 'ultrafast':
        print(f"  {_YELLOW}[ULTRAFAST]{_RESET} #{clip_id} {tag}: skip chain → static")
        logging.info(f"Clip #{clip_id} {tag}: FAST_CENSOR=ultrafast → static")
        return _static_fallback()

    # Single-prompt + keyframes: 3× image remix → static
    if single_prompt_mode and mode == 'keyframes' and image_path:
        _remix_engine_label = config.REMIX_ENGINE.upper()
        for _remix_n in range(1, 4):
            print(f"  {_RED}[IMG REMIX #{_remix_n}/3]{_RESET} #{clip_id} {tag}: remixing via {_remix_engine_label}...")
            logging.info(f"Clip #{clip_id} {tag}: single-prompt remix cycle {_remix_n}/3")
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
            result['rewritten'] = True
            if _ph['ok']:
                return _ok_return(_ph, f' (img remix #{_remix_n})')
        return _static_fallback()

    # Per-clip prompt: 2× rewrite → 2× remix → minimalistic → static
    # Step 1-2: prompt rewrites (2)
    for _rw_n in range(1, 3):
        _lbl = 'PERSON REWRITE' if is_person else 'REWRITE'
        print(f"  {_RED}[{_lbl} #{_rw_n}/2]{_RESET} #{clip_id} {tag}: rewriting prompt...")
        logging.info(f"Clip #{clip_id} {tag}: prompt rewrite #{_rw_n}/2 (for_person={is_person})")
        try:
            _rw_prompt = _rewrite_blocked_prompt(prompt, for_person=is_person)
        except Exception as _e:
            logging.warning(f"Clip #{clip_id} {tag}: rewrite #{_rw_n} failed ({_e}), using original")
            _rw_prompt = prompt
        if not _rw_prompt or _rw_prompt == prompt:
            print(f"  {_YELLOW}[SKIP REWRITE]{_RESET} #{clip_id} {tag}: no change — jump to image remix")
            logging.warning(f"Clip #{clip_id} {tag}: rewrite #{_rw_n} no change — skipping rest")
            break
        try:
            _ph = _phase2_attempt(_rw_prompt, image_path, f'phase2_rw{_rw_n}')
        except ShutdownRequested:
            result['status'] = 'error'
            result['error'] = 'shutdown requested'
            result['duration_sec'] = round(time.time() - clip_start_ts, 1)
            return result
        _accumulate(_ph)
        result['rewritten'] = True
        if _ph['ok']:
            return _ok_return(_ph, f' (rewrite #{_rw_n})')

    # FAST_CENSOR=after_rewrites → static
    if _fast_censor == 'after_rewrites':
        print(f"  {_YELLOW}[FAST CENSOR]{_RESET} #{clip_id} {tag}: after_rewrites → static")
        logging.info(f"Clip #{clip_id} {tag}: FAST_CENSOR=after_rewrites → static")
        return _static_fallback()

    # Step 3-4: image remixes (keyframes only)
    if mode == 'keyframes' and image_path:
        _remix_engine_label = config.REMIX_ENGINE.upper()
        for _remix_n in range(1, 3):
            print(f"  {_RED}[IMG REMIX #{_remix_n}/2]{_RESET} #{clip_id} {tag}: remixing via {_remix_engine_label}...")
            logging.info(f"Clip #{clip_id} {tag}: post-rewrite remix {_remix_n}/2")
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

        # Step 5: minimalistic + remix
        print(f"  {_RED}[MINIMALISTIC]{_RESET} #{clip_id} {tag}: final attempt minimalistic+remix...")
        logging.info(f"Clip #{clip_id} {tag}: minimalistic final")
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

    # Ingredients mode: binary search blocked refs → in-place remix → retry
    if mode == 'ingredients' and ref_paths:
        print(f"  {_RED}[INGREDIENTS BSEARCH]{_RESET} #{clip_id} {tag}: rewrites exhausted — searching blocked refs...")
        logging.info(f"Clip #{clip_id} {tag}: ingredients — binary searching blocked refs")
        try:
            _bad_refs = _binary_search_blocked_refs(list(ref_paths))
        except ShutdownRequested:
            result['status'] = 'error'
            result['error'] = 'shutdown requested'
            result['duration_sec'] = round(time.time() - clip_start_ts, 1)
            return result
        if _bad_refs:
            _bad_names = [Path(f).name for f in _bad_refs]
            print(f"  {_RED}[BSEARCH]{_RESET} #{clip_id} {tag}: blocking refs: {_bad_names} — remixing in-place...")
            logging.warning(f"Clip #{clip_id} {tag}: blocking refs: {_bad_names}")
            try:
                from ref_remixer import remix_ref
                _remix_ref_fn = remix_ref
            except Exception:
                _remix_ref_fn = None
                logging.error(f"Clip #{clip_id}: remix_ref import failed — cannot remix refs")
            if _remix_ref_fn:
                for _bad_f in _bad_refs:
                    try:
                        _remix_ref_fn(str(_bad_f), str(_bad_f))
                        logging.info(f"Clip #{clip_id}: remixed {Path(_bad_f).name} in-place")
                    except Exception as _rem_err:
                        logging.error(f"Clip #{clip_id}: ref remix failed {Path(_bad_f).name}: {_rem_err}")
                        print(f"  {_RED}[BSEARCH]{_RESET} #{clip_id}: remix failed {Path(_bad_f).name}: {_rem_err}")
            print(f"  {_YELLOW}[INGREDIENTS RETRY]{_RESET} #{clip_id} {tag}: retrying with remixed refs...")
            logging.info(f"Clip #{clip_id} {tag}: retrying ingredients after ref remix")
            try:
                _ph = _phase2_attempt(prompt, None, 'phase2_ing_postremix')
            except ShutdownRequested:
                result['status'] = 'error'
                result['error'] = 'shutdown requested'
                result['duration_sec'] = round(time.time() - clip_start_ts, 1)
                return result
            _accumulate(_ph)
            if _ph['ok']:
                return _ok_return(_ph, ' (ingredients ref remix)')
        else:
            print(f"  {_YELLOW}[BSEARCH]{_RESET} #{clip_id} {tag}: no blocking refs isolated")
            logging.warning(f"Clip #{clip_id} {tag}: binary search no blocking refs")

    # Step 6: static fallback
    return _static_fallback()


# ==============================================================================
# Chain Mode Sequential Scheduler (FlowUltra variant — MVP Phase 1)
# ==============================================================================

def _process_single_chain_flow_ultra(
    chain_idx: int,
    total_chains: int,
    chain: list,
    project_path: Path,
    prompts_map: Dict[int, str],
    mode: str,
    output_dir: Path,
    generated_dir: Path,
    effective_ref_dir: str,
    aspect_ratio: str,
    single_prompt: Optional[str],
    client,
    max_chain_len: int,
    cascade_enabled: bool,
    max_same_clip_fails: int,
    frame_offset: float,
    end_image_dir: Optional[Path],
) -> Dict[str, Any]:
    """
    Обрабатывает один чейн последовательно (FlowUltra variant).
    Вызывается параллельно для разных чейнов.

    Returns: {'generated': int, 'failed': int, 'results': list}
    """
    from helpers.last_frame import get_last_frame_path

    chain_results = []
    chain_generated = 0
    chain_failed = 0

    chain_size = len(chain)
    chain_id_val = chain[0].get('chain_id', chain[0]['id'])
    clip_ids_str = ','.join(str(c['id']) for c in chain)

    print(f"\n  {_YELLOW}[CHAIN {chain_idx}/{total_chains}]{_RESET} chain_id={chain_id_val}, {chain_size} clip(s): [{clip_ids_str}]")

    # Safety: если chain слишком длинный и есть лимит — обрезать
    if max_chain_len > 0 and chain_size > max_chain_len:
        print(f"  {_YELLOW}WARNING:{_RESET} Chain too long ({chain_size} > {max_chain_len}), truncating")
        chain = chain[:max_chain_len]
        chain_size = len(chain)

    # Track clip failures для forced sub-chain break
    clip_fail_counts = {c['id']: 0 for c in chain}

    # Process chain sequentially (clips inside chain depend on each other)
    for clip_pos, clip_data in enumerate(chain):
        if _flowultra_shutdown_event.is_set():
            break

        clip_id = clip_data['id']
        is_head = (clip_pos == 0)
        is_extend = not is_head

        # Check if already exists
        output_path = output_dir / f"{clip_id:03d}.mp4"
        if output_path.exists() and output_path.stat().st_size > 1000:
            print(f"  {_GREEN}[SKIP]{_RESET} #{clip_id}: already exists")
            chain_results.append({'id': clip_id, 'status': 'ok', 'file': str(output_path), 'skipped': True})
            chain_generated += 1
            continue

        prompt = prompts_map.get(clip_id)
        if not prompt:
            print(f"  {_RED}[ERROR]{_RESET} #{clip_id}: no prompt available")
            chain_results.append({'id': clip_id, 'status': 'error', 'error': 'no prompt', 'file': None})
            chain_failed += 1
            continue

        # Determine image_path and mode
        image_path = None
        end_image_path = None
        effective_mode = mode

        if is_head:
            # Head clip: use mode as-is
            if mode == 'keyframes':
                # Look for generated image
                for ext in ('.png', '.jpg', '.jpeg', '.webp'):
                    img_p = generated_dir / f"{clip_id:03d}{ext}"
                    if img_p.exists():
                        image_path = str(img_p)
                        break
                if not image_path:
                    print(f"  {_RED}[ERROR]{_RESET} #{clip_id} (head): no image in generated/")
                    chain_results.append({'id': clip_id, 'status': 'error', 'error': 'no image', 'file': None})
                    chain_failed += 1
                    break  # Can't continue chain without head
                # Check for end_image if end_image_dir provided
                if end_image_dir:
                    for ext in ('.png', '.jpg', '.jpeg', '.webp'):
                        end_p = end_image_dir / f"{clip_id:03d}{ext}"
                        if end_p.exists():
                            end_image_path = str(end_p)
                            break
            elif mode == 't2v':
                # Pure t2v for head
                pass
            elif mode == 'ingredients':
                # ingredients = components — ref_dir used
                pass
        else:
            # Extend clip: extract last_frame from previous clip
            prev_clip_id = chain[clip_pos - 1]['id']
            prev_mp4 = output_dir / f"{prev_clip_id:03d}.mp4"

            if not prev_mp4.exists():
                print(f"  {_RED}[ERROR]{_RESET} #{clip_id} (extend): prev clip #{prev_clip_id} missing")
                chain_results.append({'id': clip_id, 'status': 'error', 'error': 'prev clip missing', 'file': None})
                chain_failed += 1
                break  # Can't extend from missing clip

            # Extract last frame
            try:
                last_frame_path = get_last_frame_path(project_path, prev_clip_id, offset=frame_offset)
                if not last_frame_path or not last_frame_path.exists():
                    print(f"  {_RED}[ERROR]{_RESET} #{clip_id}: last_frame extraction failed for #{prev_clip_id}")
                    chain_results.append({'id': clip_id, 'status': 'error', 'error': 'last_frame extraction failed', 'file': None})
                    chain_failed += 1
                    break
                image_path = str(last_frame_path)
                effective_mode = 'keyframes'  # Force keyframes (i2v analog) for extends
                print(f"  {_YELLOW}[EXTEND]{_RESET} #{clip_id}: using last_frame from #{prev_clip_id}")
            except Exception as e:
                print(f"  {_RED}[ERROR]{_RESET} #{clip_id}: last_frame extraction exception: {e}")
                chain_results.append({'id': clip_id, 'status': 'error', 'error': str(e), 'file': None})
                chain_failed += 1
                break

        # Generate clip
        print(f"  {_YELLOW}[GEN]{_RESET} #{clip_id} ({'HEAD' if is_head else 'EXTEND'}): {effective_mode}")

        # Prepare ref_paths for ingredients mode
        ref_paths = None
        if effective_mode == 'ingredients' and effective_ref_dir:
            from pathlib import Path as P
            _ref_path = P(effective_ref_dir)
            if _ref_path.exists():
                _IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp'}
                ref_paths = [str(f) for f in _ref_path.iterdir()
                             if f.is_file() and f.suffix.lower() in _IMAGE_EXTS]

        result = _process_single_clip_fu(
            client=client,
            clip_id=clip_id,
            prompt=prompt,
            mode=effective_mode,
            image_path=image_path,
            ref_paths=ref_paths,
            end_image_path=end_image_path,
            output_dir=output_dir,
            aspect_ratio=aspect_ratio,
            worker_id_fn=lambda: 0,  # Single worker per chain (chains are parallel)
            single_prompt_mode=bool(single_prompt),
            clients_cfg_list=None,  # Single-key for MVP
            ok_run=None,
        )

        chain_results.append(result)

        if result['status'] == 'ok':
            chain_generated += 1
            icon = 'OK'
            print(f"  {_GREEN}[OK]{_RESET} #{clip_id}: {icon} ({result.get('duration_sec', 0)}s)")
        else:
            chain_failed += 1
            clip_fail_counts[clip_id] += 1
            print(f"  {_RED}[FAIL]{_RESET} #{clip_id}: {result.get('error', 'unknown error')}")

            # Cascade recovery logic
            if cascade_enabled and is_extend and clip_fail_counts[clip_id] < max_same_clip_fails:
                prev_clip_id = chain[clip_pos - 1]['id']
                print(f"  {_YELLOW}[CASCADE]{_RESET} #{clip_id} failed — will invalidate & regen #{prev_clip_id}")

                # Invalidate prev clip (delete mp4 + last_frame png)
                prev_mp4 = output_dir / f"{prev_clip_id:03d}.mp4"
                prev_frame = project_path / 'vid_img' / f"{prev_clip_id:03d}_last_frame.png"
                try:
                    if prev_mp4.exists():
                        prev_mp4.unlink()
                        print(f"  {_YELLOW}├─{_RESET} Deleted {prev_mp4.name}")
                    if prev_frame.exists():
                        prev_frame.unlink()
                        print(f"  {_YELLOW}-{_RESET} Deleted {prev_frame.name}")
                except Exception as del_err:
                    print(f"  {_YELLOW}WARNING:{_RESET} Failed to delete prev clip files: {del_err}")

                print(f"  {_YELLOW}NOTE:{_RESET} Full cascade walk-back not implemented (MVP) — breaking chain")
                break
            elif clip_fail_counts[clip_id] >= max_same_clip_fails:
                print(f"  {_YELLOW}[FORCE BREAK]{_RESET} #{clip_id} failed {max_same_clip_fails} times — forced sub-chain head")
                break
            else:
                # No cascade or head failed — break chain
                break

    return {
        'generated': chain_generated,
        'failed': chain_failed,
        'results': chain_results,
    }


def _generate_all_videos_flow_ultra_chain_mode(
    project_path: Path,
    clips: list,
    prompts_map: Dict[int, str],
    mode: str,
    output_dir: Path,
    generated_dir: Path,
    effective_ref_dir: str,
    aspect_ratio: str,
    story_mode: str,
    single_prompt: Optional[str],
    client,  # FastGenClient
    max_parallel: int,
    end_image_dir: Optional[Path],
) -> Dict[str, Any]:
    """
    Parallel chain scheduler для STORY_MODE='chain' или 'morph' (FlowUltra variant).

    Логика аналогична veo_video._generate_all_videos_chain_mode():
    - Группирует клипы по chain_id через _build_chains()
    - Запускает обработку разных чейнов ПАРАЛЛЕЛЬНО через ThreadPoolExecutor
    - Для каждого chain:
      * head clip — генерируется как обычный (t2v/keyframes/ingredients)
      * extend clips — извлекается last_frame из prev mp4, mode='keyframes' (i2v analog)
      * обрабатывается ПОСЛЕДОВАТЕЛЬНО внутри чейна (extend зависит от предыдущего клипа)
    - Cascade recovery: при fail clip N → invalidate N-1 → regen N-1 → retry N

    Returns: {'generated': int, 'failed': int, 'results': list}
    """
    # Import chain helper from veo_video module
    import sys
    from pathlib import Path as P
    _veo_module_dir = str(P(__file__).resolve().parent)
    if _veo_module_dir not in sys.path:
        sys.path.insert(0, _veo_module_dir)
    from veo_motion import _build_chains
    import params as config
    chains = _build_chains(clips, story_mode)

    print(f"\n  {_YELLOW}[CHAIN MODE -- FLOW ULTRA]{_RESET} Parallel scheduler active")
    print(f"  {_YELLOW}\\-{_RESET} {len(chains)} chain(s) detected")

    max_chain_len = getattr(config, 'VIDEO_EXTEND_MAX_CHAIN', 10)
    cascade_enabled = getattr(config, 'CHAIN_CASCADE_RECOVERY', True)
    max_same_clip_fails = getattr(config, 'CHAIN_MAX_SAME_CLIP_FAILS', 3)
    frame_offset = getattr(config, 'VIDEO_EXTEND_FRAME_OFFSET', 0.5)

    # Calculate optimal workers: min(chains, max_parallel)
    chain_workers = min(len(chains), max_parallel)
    print(f"  {_YELLOW}\\-{_RESET} Launching {chain_workers} parallel chain workers")

    all_results = []
    total_generated = 0
    total_failed = 0

    # Launch chains in parallel
    with ThreadPoolExecutor(max_workers=chain_workers) as executor:
        futures = []
        for chain_idx, chain in enumerate(chains, start=1):
            if _flowultra_shutdown_event.is_set():
                print(f"\n  {_RED}[SHUTDOWN]{_RESET} FlowUltra chain scheduler stopped")
                break

            future = executor.submit(
                _process_single_chain_flow_ultra,
                chain_idx=chain_idx,
                total_chains=len(chains),
                chain=chain,
                project_path=project_path,
                prompts_map=prompts_map,
                mode=mode,
                output_dir=output_dir,
                generated_dir=generated_dir,
                effective_ref_dir=effective_ref_dir,
                aspect_ratio=aspect_ratio,
                single_prompt=single_prompt,
                client=client,
                max_chain_len=max_chain_len,
                cascade_enabled=cascade_enabled,
                max_same_clip_fails=max_same_clip_fails,
                frame_offset=frame_offset,
                end_image_dir=end_image_dir,
            )
            futures.append(future)

        # Collect results as they complete
        for future in as_completed(futures):
            if _flowultra_shutdown_event.is_set():
                break
            try:
                chain_result = future.result()
                all_results.extend(chain_result['results'])
                total_generated += chain_result['generated']
                total_failed += chain_result['failed']
            except Exception as e:
                print(f"  {_RED}[ERROR]{_RESET} FlowUltra chain processing exception: {e}")
                total_failed += 1

    print(f"\n  {_YELLOW}[CHAIN MODE DONE — FLOW ULTRA]{_RESET}")
    print(f"  - Generated: {total_generated}, Failed: {total_failed}")

    return {
        'generated': total_generated,
        'failed': total_failed,
        'results': all_results,
    }


# ==============================================================================
# Batch orchestrator — full parity with veo_video.generate_all_videos
# ==============================================================================

def generate_all_videos_flow_ultra(
    project_dir: str,
    mode: str = 't2v',                 # 't2v' | 'ingredients' | 'keyframes'
    max_parallel: Optional[int] = None,
    clip_ids: Optional[List[int]] = None,
    single_prompt: Optional[str] = None,
    aspect_ratio: Optional[str] = None,
    ref_dir: Optional[str] = None,
    silent: bool = False,
    no_cancel: bool = False,
    api_key: Optional[str] = None,
    dual_key: bool = False,
    end_image_dir: Optional[str] = None,
    prompts_file_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Batch Flow Ultra video generation — full parity with veo_video.generate_all_videos.

    Args:
      project_dir:       path to project dir (must contain montage_plan.json, prompts file)
      mode:              't2v' | 'ingredients' | 'keyframes'
      max_parallel:      workers (clamped by flow_ultra_threads_allowed per key)
      clip_ids:          filter to specific clips (None = all)
      single_prompt:     use one prompt for all; 'default' = first from prompts file
      aspect_ratio:      '16:9' / '9:16' (default '16:9')
      ref_dir:           ingredients mode refs (default project/ref/)
      silent:            auto-retry up to VEONONSTOP_MAX_SILENT_CYCLES cycles
      no_cancel:         skip startup zombie cancel_all_tasks
      api_key:           override FASTGEN_API_KEY
      dual_key:          use Key1 + FASTGEN_API_KEY_2 weighted by flow_ultra_threads_allowed
      end_image_dir:     for keyframes mode — dir to fetch end frame per clip
      prompts_file_path: override auto-pick

    Returns: {'generated': N, 'failed': N, 'results': [...]}
    """
    project_path = Path(project_dir)
    _flowultra_shutdown_event.clear()
    if aspect_ratio is None:
        aspect_ratio = '16:9'
    if aspect_ratio in ('landscape', 'horizontal'):
        aspect_ratio = '16:9'
    elif aspect_ratio in ('portrait', 'vertical'):
        aspect_ratio = '9:16'
    if aspect_ratio not in ('16:9', '9:16'):
        raise ValueError(f"Flow Ultra v4 supports aspect_ratio 16:9 or 9:16, got {aspect_ratio!r}")

    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('requests').setLevel(logging.WARNING)

    log_ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_path = project_path / f'flowultra_{log_ts}.log'
    file_handler = None
    try:
        file_handler = logging.FileHandler(str(log_path), encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter('%(asctime)s | %(levelname)-8s | %(message)s'))
        logging.getLogger().addHandler(file_handler)
    except OSError:
        logging.warning(f"Cannot create log file (path too long?): {log_path}")
    logging.info(f"=== Flow Ultra video started for {project_path.name} (mode={mode}) ===")

    print("\n" + "=" * 60)
    print(f"  FASTGEN FLOW ULTRA ({mode.upper()})")
    print("=" * 60)
    print(f"  Log: {log_path}")

    # 1. Montage plan
    montage_plan_path = project_path / 'montage_plan.json'
    if not montage_plan_path.exists():
        raise FileNotFoundError(f"montage_plan.json not found in {project_path}")
    with open(montage_plan_path, 'r', encoding='utf-8-sig') as f:
        montage_data = json.load(f)
    montage_plan = montage_data.get('montage_plan', montage_data)
    clips = montage_plan.get('clips', [])
    all_clip_ids = [c['id'] for c in clips]

    # Real Photo skip filter: direct Flow Ultra must mirror veo_video.py and
    # live_flow.py. These clips are handled by archival stills in
    # generated/ and final concat turns them into zoom videos with credits.
    from units.source_ledger import VIDEO_SKIP_SOURCES
    _static_ids = {
        c['id'] for c in clips
        if c.get('source') in VIDEO_SKIP_SOURCES
    }
    if _static_ids:
        all_clip_ids = [cid for cid in all_clip_ids if cid not in _static_ids]
        print(f"  [Static sources] Skipping {len(_static_ids)} clips reserved for non-video output")

    if clip_ids:
        all_clip_ids = [cid for cid in all_clip_ids if cid in clip_ids]
    print(f"  Total clips: {len(all_clip_ids)}")

    # 2. Prompts
    prompts_map: Dict[int, str] = {}
    if single_prompt:
        if single_prompt == 'default':
            pf = Path(prompts_file_path) if prompts_file_path else find_prompts_file(project_path)
            if not pf:
                raise FileNotFoundError(f"Prompts file not found in {project_path}")
            _parsed = extract_all_prompts(str(pf)) if extract_all_prompts else {}
            if not _parsed:
                _parsed = parse_prompts_file(pf)
            _parsed, _remapped = remap_dense_prompts_for_montage(_parsed, clips)
            if _remapped:
                print("  [Prompts] Remapped dense prompt file to real montage clip IDs")
            if _parsed:
                first_prompt = list(_parsed.values())[0]
                for cid in all_clip_ids:
                    prompts_map[cid] = first_prompt
                print(f"  Prompt mode: single prompt (first from {pf.name})")
            else:
                raise ValueError("No prompts found in prompts file")
        else:
            for cid in all_clip_ids:
                prompts_map[cid] = single_prompt
            print(f"  Prompt mode: single prompt for all clips")
    else:
        pf = Path(prompts_file_path) if prompts_file_path else find_prompts_file(project_path)
        if not pf:
            raise FileNotFoundError(f"Prompts file not found in {project_path}")
        _parsed = extract_all_prompts(str(pf)) if extract_all_prompts else {}
        if not _parsed:
            _parsed = parse_prompts_file(pf)
        _parsed, _remapped = remap_dense_prompts_for_montage(_parsed, clips)
        if _remapped:
            print("  [Prompts] Remapped dense prompt file to real montage clip IDs")
        for cid in all_clip_ids:
            if cid in _parsed:
                prompts_map[cid] = _parsed[cid]
            else:
                logging.warning(f"Clip #{cid}: no prompt found, skipping")
        print(f"  Prompt mode: per-clip from {pf.name}  ({len(prompts_map)} loaded)")
        logging.info(f"Prompts file: {pf}")
    if not prompts_map:
        raise ValueError("No prompts available")

    # 3. Paths + skip existing
    output_dir = project_path / 'vid_img'
    output_dir.mkdir(exist_ok=True)
    generated_dir = project_path / 'generated'
    effective_ref_dir = ref_dir or str(project_path / 'ref')

    skipped: List[int] = []
    pending_ids: List[int] = []
    for cid in sorted(prompts_map.keys()):
        if clip_media_path(output_dir, cid):
            skipped.append(cid)
        else:
            pending_ids.append(cid)
    if skipped:
        print(f"  Skipped (already exist): {len(skipped)}")
    print(f"  To generate: {len(pending_ids)}")
    if not pending_ids:
        print("  All videos already exist.")
        return {'generated': len(skipped), 'failed': 0, 'results': []}

    # 4. Images for keyframes mode + end_image pairs
    def _find_image(cid: int) -> Optional[str]:
        for ext in _IMG_EXTS:
            p = generated_dir / f"{cid:03d}{ext}"
            if p.exists():
                return str(p)
        return None

    if mode == 'keyframes':
        missing_images = [cid for cid in pending_ids if not _find_image(cid)]
        if missing_images:
            print(f"\n  {_RED}WARNING:{_RESET} {len(missing_images)} images missing in generated/:")
            print(f"  IDs: {', '.join(map(str, missing_images[:20]))}")
            pending_ids = [cid for cid in pending_ids if cid not in missing_images]
        if not pending_ids:
            raise FileNotFoundError(
                f"keyframes mode: no images in generated/ for any pending clip. "
                f"Run ImageGen first or switch to t2v/ingredients."
            )

    end_image_map: Dict[int, str] = {}
    if mode == 'keyframes' and end_image_dir:
        _end_dir = Path(end_image_dir)
        ordered = sorted(pending_ids)
        for i, cid in enumerate(ordered):
            next_cid = ordered[i + 1] if i + 1 < len(ordered) else cid
            for ext in _IMG_EXTS:
                cand = _end_dir / f"{next_cid:03d}{ext}"
                if cand.exists():
                    end_image_map[cid] = str(cand)
                    break
        print(f"  End-image pairs: {len(end_image_map)}")

    # Ref images for ingredients mode
    ref_paths: List[str] = []
    if mode == 'ingredients':
        ref_paths = _collect_ref_images(effective_ref_dir, limit=3)
        if not ref_paths:
            raise FileNotFoundError(
                f"No reference images in {effective_ref_dir}. "
                f"Ingredients mode requires 1-3 images in ref/."
            )
        print(f"  References: {len(ref_paths)} — {[Path(r).name for r in ref_paths]}")
        # NOTE: preflight через FU ingredients сам по себе = тест реального submit'а.
        # Binary-search + in-place remix запускаются из _process_single_clip_fu.ingredients path
        # при первом цензурном фейле — это дешевле чем отдельный preflight cycle.

    # 5. Clients + weighted pool
    _k1 = api_key or getattr(config, 'FASTGEN_API_KEY', '')
    _k2 = getattr(config, 'FASTGEN_API_KEY_2', '') or ''
    _mp_default1 = max_parallel or getattr(config, 'FASTGEN_MAX_PARALLEL', 4)
    _mp_default2 = getattr(config, 'FASTGEN_MAX_PARALLEL_2', _mp_default1)

    clients_cfg: List[Dict] = []
    _any_cancelled = False
    _candidates: List[Tuple[str, str, int]] = [('Key1', _k1, _mp_default1)]
    if dual_key:
        print(f"  {_YELLOW}[WARN]{_RESET} Flow Ultra is pinned to FastGen Key1; ignoring dual_key/Key2")
        logging.warning("Flow Ultra pinned to FastGen Key1; ignoring dual_key/Key2")

    for label, key, mp_default in _candidates:
        if not key:
            continue
        c = FastGenClient(api_key=key)
        try:
            _threads = _get_flow_ultra_threads(c)
        except FastGenUsageError as e:
            _threads = None
            print(f"  {_YELLOW}[WARN]{_RESET} {label}: usage preflight unavailable ({e}) - using dialog parallel={mp_default}")
            logging.warning(f"{label}: flow_ultra usage preflight unavailable: {e}; using mp_default={mp_default}")
        if _threads == 0:
            print(f"  {_YELLOW}[WARN]{_RESET} {label}: no Flow Ultra subscription (threads=0) — skipping")
            logging.warning(f"{label}: flow_ultra_threads_allowed=0, skipping")
            continue
        print(f"  {_YELLOW}[CLEANUP]{_RESET} [{label}] startup cancel-all disabled")
        logging.info(f"Startup cancel-all disabled for {label}")
        if _threads is None:
            _mp = max_parallel or mp_default
        else:
            _mp = min(max_parallel or mp_default, _threads) if max_parallel else min(mp_default, _threads)
        _sem = threading.Semaphore(max(1, _mp))
        clients_cfg.append({
            'client': c, 'mp': _mp, 'sem': _sem,
            'label': label, 'threads_allowed': _threads if _threads is not None else _mp,
            '5xx_count': 0, '5xx_cancelled': False, 'key_dead': False,
        })
        if _threads is None:
            print(f"  [{label}] Flow Ultra v4 configured parallel: {_mp}")
        else:
            print(f"  [{label}] Flow Ultra threads: {_threads} | using: {_mp}")

    if not clients_cfg:
        raise ValueError("No valid FastGen API key configured for Flow Ultra")

    # Register for external Hard Stop (Control Panel Stop button)
    for cfg in clients_cfg:
        if cfg['client'] not in _flowultra_active_clients:
            _flowultra_active_clients.append(cfg['client'])

    if _any_cancelled:
        time.sleep(3)  # let server clean up

    total_parallel = sum(cfg['mp'] for cfg in clients_cfg)

    print(f"\n  Mode: {mode.upper()}")
    print(f"  Aspect: {aspect_ratio}")
    print(f"  Parallel workers: {total_parallel}" + (
        f" ({'+'.join(str(c['mp']) for c in clients_cfg)} per key)"
        if len(clients_cfg) > 1 else ""
    ))
    print("=" * 60)

    # 6. Retry cycle loop
    accumulated_ok: Dict[int, Dict] = {}
    all_clip_results: List[Dict] = []
    pending_prompts = {cid: prompts_map[cid] for cid in pending_ids if cid in prompts_map}
    _tech_fail_cycles: Dict[int, int] = {}

    _worker_counter = {'next': 0}
    _worker_lock = threading.Lock()
    _worker_map: Dict[int, int] = {}

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

    # Graceful shutdown (main thread only)
    import signal
    _original_sigint = signal.getsignal(signal.SIGINT) if threading.current_thread() is threading.main_thread() else None

    def _graceful_shutdown(signum, frame):
        if _flowultra_shutdown_event.is_set():
            print(f"\n\n  {_RED}[FORCE QUIT]{_RESET} Second Ctrl+C — exiting!")
            if _original_sigint:
                signal.signal(signal.SIGINT, _original_sigint)
            raise KeyboardInterrupt
        _flowultra_shutdown_event.set()
        print(f"\n\n  {_YELLOW}{'='*60}{_RESET}")
        print(f"  {_YELLOW}[SHUTDOWN]{_RESET} Ctrl+C — graceful shutdown...")
        for cfg in clients_cfg:
            try:
                _can = cfg['client'].cancel_all_tasks()
                if isinstance(_can, dict):
                    _tc = _can.get('total_cancelled', 0)
                    print(f"  {_YELLOW}[SHUTDOWN]{_RESET} [{cfg['label']}] cancelled {_tc} ops")
            except Exception as e:
                print(f"  {_YELLOW}[SHUTDOWN]{_RESET} [{cfg['label']}] cancel failed: {e}")
        print(f"  {_YELLOW}[SHUTDOWN]{_RESET} Waiting workers to finish...")
        print(f"  {_YELLOW}{'='*60}{_RESET}\n")

    if threading.current_thread() is threading.main_thread():
        try:
            signal.signal(signal.SIGINT, _graceful_shutdown)
        except Exception:
            pass

    # ── CHAIN MODE PROCESSING (FlowUltra) ────────────────────────────────
    story_mode = getattr(config, 'STORY_MODE', 'off')
    if story_mode in ('chain', 'morph'):
        if not clients_cfg:
            raise ValueError("Chain mode requires at least one active API key")

        # Chain mode: sequential scheduler (MVP Phase 1)
        # Takes first client (single-key support for MVP)
        chain_client = clients_cfg[0]['client']

        # Prepare end_image_dir if provided
        end_image_dir_path = Path(end_image_dir) if end_image_dir else None

        return _generate_all_videos_flow_ultra_chain_mode(
            project_path=project_path,
            clips=clips,
            prompts_map=prompts_map,
            mode=mode,
            output_dir=output_dir,
            generated_dir=generated_dir,
            effective_ref_dir=effective_ref_dir,
            aspect_ratio=aspect_ratio,
            story_mode=story_mode,
            single_prompt=single_prompt,
            client=chain_client,
            max_parallel=max_parallel or 4,
            end_image_dir=end_image_dir_path,
        )

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

    def _run_claimed_flowultra(claim, **kwargs):
        try:
            cid = int(kwargs.get('clip_id'))
            existing = clip_media_path(output_dir, cid)
            if existing:
                return _external_ok_result(cid, existing, 'existing_before_run')
            return _process_single_clip_fu(**kwargs)
        finally:
            claim.release()

    try:
        while True:
            if _flowultra_shutdown_event.is_set():
                was_interrupted = True
                break
            cycle_num += 1
            cycle_ids = sorted(pending_prompts.keys())
            print(f"\n  [Cycle {cycle_num}] Generating {len(cycle_ids)} videos...")
            cycle_results: List[Dict] = []

            # Reset per-cycle state
            with _worker_lock:
                _worker_map.clear()
            for _cfg in clients_cfg:
                _cfg['5xx_count'] = 0
                _cfg['5xx_cancelled'] = False
            _worker_counter['next'] = 0
            with _started_workers_lock:
                _started_workers.clear()

            with ThreadPoolExecutor(max_workers=total_parallel) as executor:
                task_queue = list(cycle_ids)
                active_futures: Dict = {}

                def _submit_one():
                    while task_queue and not _flowultra_shutdown_event.is_set():
                        cid = task_queue.pop(0)
                        existing = clip_media_path(output_dir, cid)
                        if existing:
                            cycle_results.append(_external_ok_result(cid, existing, 'existing_before_submit'))
                            continue
                        claim = acquire_clip_claim(output_dir, cid, owner=f'flowultra:{mode}')
                        if not claim:
                            existing = clip_media_path(output_dir, cid)
                            cycle_results.append(_external_ok_result(cid, existing, 'claimed_elsewhere'))
                            continue
                        break
                    else:
                        return
                    image_path = _find_image(cid) if mode == 'keyframes' else None
                    end_img = end_image_map.get(cid) if mode == 'keyframes' else None
                    fut = executor.submit(
                        _run_claimed_flowultra,
                        claim,
                        client=None,
                        clip_id=cid,
                        prompt=pending_prompts[cid],
                        mode=mode,
                        image_path=image_path,
                        ref_paths=ref_paths if mode == 'ingredients' else None,
                        end_image_path=end_img,
                        output_dir=output_dir,
                        aspect_ratio=aspect_ratio,
                        worker_id_fn=_get_worker_id,
                        single_prompt_mode=bool(single_prompt),
                        clients_cfg_list=clients_cfg,
                        ok_run=_ok_run,
                    )
                    active_futures[fut] = cid

                for _ in range(min(total_parallel, len(task_queue))):
                    _submit_one()

                _cycle_ok_durs: List[float] = []
                _cycle_total = len(cycle_ids)
                _cycle_done = 0
                _cycle_ok = 0
                if IS_PIPE:
                    print(f"  Cycle {cycle_num}: 0/{_cycle_total} clips", flush=True)
                _tqdm_ctx = tqdm(total=_cycle_total, desc=f"  Cycle {cycle_num}",
                                 unit="clip", smoothing=0) if not IS_PIPE else None
                while active_futures:
                    done, _ = wait(active_futures.keys(), timeout=2, return_when=FIRST_COMPLETED)
                    if not done:
                        if _flowultra_shutdown_event.is_set():
                            if _tqdm_ctx:
                                _tqdm_ctx.set_postfix_str("shutting down...")
                            for fut, cid in list(active_futures.items()):
                                fut.cancel()
                                cycle_results.append({
                                    'id': cid, 'status': 'error', 'error': 'shutdown',
                                    'file': None, 'task_id': None, 'duration_sec': 0,
                                    'worker_id': -1, 'retries': 0, 'retry_time': 0,
                                    'error_counts': {}, 'rewritten': False,
                                })
                                _cycle_done += 1
                                if _tqdm_ctx:
                                    _tqdm_ctx.update(1)
                                if IS_PIPE:
                                    print(f"PROGRESS:{_cycle_done}/{_cycle_total}", flush=True)
                            active_futures.clear()
                            break
                        continue
                    for future in done:
                        cid = active_futures.pop(future)
                        try:
                            cr = future.result()
                            cycle_results.append(cr)
                            icon = '+' if cr['status'] == 'ok' else 'X'
                            dur = cr.get('duration_sec', 0)
                            if cr['status'] == 'ok' and dur > 0:
                                _cycle_ok_durs.append(dur)
                            if _tqdm_ctx:
                                avg_s = f" avg={sum(_cycle_ok_durs)/len(_cycle_ok_durs):.0f}s" if _cycle_ok_durs else ""
                                _tqdm_ctx.set_postfix_str(f"#{cid}: {icon}{avg_s}")
                        except Exception as e:
                            cr = {
                                'id': cid, 'status': 'error', 'error': str(e),
                                'file': None, 'task_id': None, 'duration_sec': 0,
                                'worker_id': -1, 'retries': 0, 'retry_time': 0,
                                'error_counts': {}, 'rewritten': False,
                            }
                            cycle_results.append(cr)
                            if _tqdm_ctx:
                                _tqdm_ctx.set_postfix_str(f"#{cid}: X")
                        _cycle_done += 1
                        if cr.get('status') == 'ok':
                            _cycle_ok += 1
                        if _tqdm_ctx:
                            _tqdm_ctx.update(1)
                        if IS_PIPE:
                            print(f"PROGRESS:{_cycle_ok}/{_cycle_total}", flush=True)
                        if not _flowultra_shutdown_event.is_set():
                            _submit_one()
                if _tqdm_ctx:
                    _tqdm_ctx.close()

            all_clip_results.extend(cycle_results)
            for r in cycle_results:
                cid = r['id']
                if r['status'] == 'ok':
                    accumulated_ok[cid] = r
                    pending_prompts.pop(cid, None)
                    _tech_fail_cycles.pop(cid, None)
                else:
                    _tech_fail_cycles[cid] = _tech_fail_cycles.get(cid, 0) + 1
                    _max_sc = getattr(config, 'VEONONSTOP_MAX_SILENT_CYCLES', 5)
                    _err_text = str(r.get('error') or '')
                    _err_counts = r.get('error_counts', {}) or {}
                    _timeoutish = (
                        _is_flowultra_generation_timeout(_err_text)
                        or 'flow_ultra_generation_timeout' in _err_counts
                        or 'timeout' in _err_counts
                    )
                    if _timeoutish:
                        _timeout_sc = int(os.getenv('FLOWULTRA_TIMEOUT_STATIC_CYCLES', '1') or '1')
                        _max_sc = min(_max_sc, max(1, _timeout_sc))
                    if _tech_fail_cycles[cid] >= _max_sc:
                        _img = _find_image(cid)
                        if _img and Path(_img).is_file():
                            _ext = Path(_img).suffix
                            _dest = output_dir / f"{cid:03d}{_ext}"
                            try:
                                shutil.copy(_img, str(_dest))
                                print(f"  {_YELLOW}[STATIC]{_RESET} #{cid}: {_max_sc} cycles tech error — static fallback")
                                logging.warning(f"Clip #{cid}: {_max_sc} cycles tech error — static fallback")
                                accumulated_ok[cid] = {'id': cid, 'status': 'ok', 'file': str(_dest)}
                                pending_prompts.pop(cid, None)
                            except Exception as _cpe:
                                logging.error(f"Clip #{cid}: static fallback copy failed: {_cpe}")
                        else:
                            print(f"  {_YELLOW}[GIVE UP]{_RESET} #{cid}: {_max_sc} cycles tech, no image")
                            logging.warning(f"Clip #{cid}: {_max_sc} cycles tech, no image for static")
                            pending_prompts.pop(cid, None)

            cycle_ok = sum(1 for r in cycle_results if r['status'] == 'ok')
            failed_ids = sorted(pending_prompts.keys())
            print(f"\n  [Cycle {cycle_num}] OK: {cycle_ok},  still missing: {len(failed_ids)}")

            if not failed_ids:
                break
            if _flowultra_shutdown_event.is_set():
                was_interrupted = True
                break

            # Silent cycles (auto-retry)
            if silent:
                _max_cycles = getattr(config, 'VEONONSTOP_MAX_SILENT_CYCLES', 5)
                if cycle_num < _max_cycles:
                    print(f"\n  {_YELLOW}[SILENT]{_RESET} {len(failed_ids)} failed — retry cycle {cycle_num+1}/{_max_cycles}")
                    logging.info(f"Silent mode: {len(failed_ids)} failed after cycle {cycle_num}, retry")
                    continue
                else:
                    print(f"\n  {_YELLOW}[SILENT]{_RESET} {len(failed_ids)} still failed after {cycle_num} cycles")
                    logging.warning(f"Silent mode: {len(failed_ids)} failed after {cycle_num} cycles, stopping")
                    break

            # Interactive mode
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
            _is_gui = os.environ.get('BEGO_GUI_MODE') == '1'
            _is_interactive = (not _is_gui and sys.stdin is not None
                               and hasattr(sys.stdin, 'isatty') and sys.stdin.isatty())
            if not _is_interactive:
                _reason = 'GUI mode' if _is_gui else 'No TTY'
                print(f"  [AUTO] {_reason} — auto-selecting [1] Retry")
                choice = '1'
            else:
                while True:
                    try:
                        choice = input("  Select [1/2]: ").strip()
                    except EOFError:
                        choice = '1'
                        break
                    if choice in ('1', '2'):
                        break
                    print("  Enter 1 or 2")
            if choice == '2':
                break

    finally:
        if threading.current_thread() is threading.main_thread() and _original_sigint is not None:
            try:
                signal.signal(signal.SIGINT, _original_sigint)
            except Exception:
                pass
        # Don't clear shutdown event — next call clears it on entry
        for cfg in clients_cfg:
            try:
                _flowultra_active_clients.remove(cfg['client'])
            except (ValueError, Exception):
                pass
        if file_handler is not None:
            try:
                logging.getLogger().removeHandler(file_handler)
                file_handler.close()
            except Exception:
                pass

    if was_interrupted:
        print(f"\n  {_YELLOW}[SHUTDOWN]{_RESET} Generation interrupted. Partial results in vid_img/.")

    # Summary — count actual files in vid_img/ (.mp4 + static fallback images)
    _generated = 0
    _failed = 0
    for cid in prompts_map:
        mp4 = output_dir / f"{cid:03d}.mp4"
        if mp4.exists() and mp4.stat().st_size > 1000:
            _generated += 1
            continue
        _found_static = False
        for ext in _IMG_EXTS + ('.png',):
            if (output_dir / f"{cid:03d}{ext}").exists():
                _generated += 1
                _found_static = True
                break
        if not _found_static:
            _failed += 1

    total_time = round(time.time() - start_total, 1)
    print("\n" + "=" * 60)
    print(f"  FLOW ULTRA DONE (total={total_time}s)")
    print(f"  Generated: {_generated}/{len(prompts_map)}")
    if _failed:
        print(f"  Failed:    {_failed}")
    print("=" * 60 + "\n")
    logging.info(f"Flow Ultra done: generated={_generated}/{len(prompts_map)} failed={_failed} total={total_time}s")

    return {
        'generated': _generated,
        'failed': _failed,
        'results': all_clip_results,
    }
