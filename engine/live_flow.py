"""
live_flow.py — Inflight watcher for FastGen Flow Ultra keyframes (i2v).

Full parity with live_veo.py but for Flow Ultra engine instead of VeoNonStop.
Watches `generated/` as ImageGen produces images, animates them via FastGen Flow Ultra
keyframes endpoint, writes to `vid_img/`.

Supports:
  - dual-key (Key1 + FASTGEN_API_KEY_2) with weighted worker pool by flow_ultra_threads_allowed
  - single-prompt / per-clip prompt modes (auto-picks *_veo_adapt_*.txt > *_prompts_*.txt)
  - vislide interval filter (animate every N-th + intro block, static copy rest)
  - proactive retry queue with MAX_CLIP_RETRIES
  - proactive remix on invalid_argument exhaustion
  - Level-2 remix after all retries exhausted
  - Level-3 static fallback cascade
  - FAST_CENSOR=ultrafast → skip remix → immediate static
  - graceful Ctrl+C shutdown (cancel_all_tasks on all active keys)
"""
import sys
# Force UTF-8 output before colorama wraps stdout (avoids cp1251 crash on Cyrillic/NFD paths)
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
import shutil
import time
import json
import signal
import logging
import argparse
import threading
from pathlib import Path
from typing import Optional, Dict, Set, List
from concurrent.futures import ThreadPoolExecutor

# Add agent dir to path
sys.path.insert(0, str(Path(__file__).parent))

import params as config
from prompt_extract import find_prompts_file, extract_all_prompts, remap_dense_prompts_for_montage
try:
    from units.fastgen_engine import (
        FastGenClient,
        _flowultra_shutdown_event as _shutdown_requested,
        _flowultra_active_clients,
    )
except Exception:
    from fastgen_engine import (
        FastGenClient,
        _flowultra_shutdown_event as _shutdown_requested,
        _flowultra_active_clients,
    )
from units.flow_ultra_engine import (
    _process_single_clip_fu,
    _get_flow_ultra_threads,
    parse_prompts_file as _fu_parse_prompts_file,
    find_prompts_file as _fu_find_prompts_file,
)
from units.motion_claims import acquire_clip_claim, clip_media_exists
# Reuse engine-agnostic image remix helper
from units.veo_motion import _image_remix

IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp'}
MIN_FILE_SIZE    = 5_000   # bytes
FILE_STABLE_SECS = 3       # seconds without modification = file complete

_YELLOW = '\033[33m'
_GREEN  = '\033[32m'
_RED    = '\033[31m'
_RESET  = '\033[0m'


def _sleep_interruptible(seconds: float, step: float = 0.25) -> bool:
    if seconds <= 0:
        return _shutdown_requested.is_set()
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _shutdown_requested.is_set():
            return True
        time.sleep(min(step, max(0.0, deadline - time.time())))
    return _shutdown_requested.is_set()


def _find_image(generated_dir: Path, cid: int) -> Optional[str]:
    for ext in IMAGE_EXTENSIONS:
        p = generated_dir / f"{cid:03d}{ext}"
        if p.exists() and p.stat().st_size > MIN_FILE_SIZE:
            return str(p)
    return None


def _is_stable(image_path: str) -> bool:
    age = time.time() - Path(image_path).stat().st_mtime
    return age >= FILE_STABLE_SECS


def _has_video(vid_img_dir: Path, cid: int) -> bool:
    return clip_media_exists(vid_img_dir, cid)


def _init_fu_client(
    api_key: Optional[str],
    max_parallel: int,
    label: str,
    no_cancel: bool = False,
):
    """Create FastGen client and clamp parallel to Flow Ultra account threads when usage is available."""
    client = FastGenClient(api_key=api_key)
    print(f"  [{label}] startup cancel-all disabled.")
    mp_default = max(1, int(max_parallel))
    try:
        fu_usage = client.get_flow_ultra_usage()
        threads_allowed = int(fu_usage.get('threads_allowed', 0) or 0)
    except Exception as e:
        threads_allowed = None
        logging.warning(f"{label}: Flow Ultra usage unavailable ({e}); using configured mp={mp_default}")
    if threads_allowed == 0:
        print(f"  [{label}] no Flow Ultra subscription (threads=0)")
        logging.warning(f"{label}: flow_ultra_threads_allowed=0")
        return None, 0, None
    mp = min(mp_default, threads_allowed) if threads_allowed is not None else mp_default
    if threads_allowed is None:
        print(f"  [{label}] Flow Ultra configured parallel: {mp}")
        logging.info(f"{label}: flow_ultra configured mp={mp}")
    else:
        print(f"  [{label}] Flow Ultra threads: {threads_allowed} | using: {mp}")
        logging.info(f"{label}: flow_ultra threads={threads_allowed}, using mp={mp}")
    sem = threading.Semaphore(max(1, mp))
    return client, mp, sem


def main():
    parser = argparse.ArgumentParser(description='Watch generated/ and animate with Flow Ultra keyframes')
    parser.add_argument('--project',  required=True, help='Project name or full path')
    parser.add_argument('--parallel', type=int, default=None)
    parser.add_argument('--aspect',   default=None, help='16:9 / 9:16')
    parser.add_argument('--poll',     type=int, default=15, help='Poll interval seconds')
    parser.add_argument('--api-key',  default=None, help='FastGen API key (overrides .env)')
    parser.add_argument('--key-slot', default='both',
                        help='Which API key(s): 1 / 2 / 1,2 / both (default)')
    parser.add_argument('--no-cancel', action='store_true', default=False,
                        help='Skip cancel-all-tasks on startup (parallel generation)')
    parser.add_argument('--single-prompt', action='store_true',
                        help='Use single_prompt.txt from project dir for all clips')
    parser.add_argument('--vislide-interval', type=int, default=0, metavar='N',
                        help='Vislide mode: animate only every N-th clip (0 = animate all)')
    parser.add_argument('--prompts-file', default=None,
                        help='Explicit prompts file path (e.g. VEO-adapted prompts)')
    parser.add_argument('--end-image-dir', default=None,
                        help='Optional dir with end-frame images per clip (ends keyframes mode)')
    args = parser.parse_args()

    # ── Resolve project path ────────────────────────────────────────────
    agent_dir    = Path(__file__).parent
    project_root = agent_dir.parent / 'projects'
    project_path = Path(args.project) if Path(args.project).is_absolute() \
                   else project_root / args.project
    if not project_path.exists():
        print(f"[ERROR] Project not found: {project_path}")
        sys.exit(1)

    generated_dir = project_path / 'generated'
    vid_img_dir   = project_path / 'vid_img'
    vid_img_dir.mkdir(exist_ok=True)

    # ── Logging ─────────────────────────────────────────────────────────
    log_ts   = time.strftime('%Y%m%d_%H%M%S')
    log_path = project_path / f'watch_i2v_flowultra_{log_ts}.log'
    logging.basicConfig(
        level=logging.INFO,
        handlers=[logging.FileHandler(str(log_path), encoding='utf-8')],
        format='%(asctime)s | %(levelname)-8s | %(message)s',
    )
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('requests').setLevel(logging.WARNING)

    # ── Load montage_plan ──────────────────────────────────────────────
    plan_path = project_path / 'montage_plan.json'
    if not plan_path.exists():
        print(f"[ERROR] montage_plan.json not found in {project_path}")
        sys.exit(1)
    with open(plan_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    plan         = data.get('montage_plan', data)
    clips_data   = plan.get('clips', [])
    all_clip_ids = [c['id'] for c in clips_data]
    if not all_clip_ids:
        print("[ERROR] No clips in montage_plan.json")
        sys.exit(1)
    from units.source_ledger import VIDEO_SKIP_SOURCES
    real_photo_ids = {
        c['id'] for c in clips_data
        if c.get('source') in VIDEO_SKIP_SOURCES
    }
    if real_photo_ids:
        all_clip_ids = [cid for cid in all_clip_ids if cid not in real_photo_ids]
        print(f"  [Real Photo] Skipping {len(real_photo_ids)} clips claimed by archival stage")

    # ── Vislide filter: каждый N-й клип + intro block → i2v ───────────
    vislide_interval = args.vislide_interval
    if vislide_interval >= 2:
        animated_ids = {cid for i, cid in enumerate(all_clip_ids) if (i + 1) % vislide_interval == 0}
        intro_dur = config.INTRO_BLOCK_DURATION
        intro_ids = {c['id'] for c in clips_data if c.get('startTime', 0) < intro_dur}
        intro_ids -= real_photo_ids
        animated_ids |= intro_ids
        print(f"  [Vislide] interval={vislide_interval}, intro={intro_dur}s ({len(intro_ids)} clips): "
              f"{len(animated_ids)}/{len(all_clip_ids)} clips will be animated")
        logging.info(f"Vislide: interval={vislide_interval}, intro_ids={sorted(intro_ids)}, "
                     f"animated_ids={sorted(animated_ids)}")
    else:
        animated_ids = set(all_clip_ids)

    # ── Load prompts ────────────────────────────────────────────────────
    prompts_map: Dict[int, str] = {}
    single_prompt_mode = args.single_prompt

    if single_prompt_mode:
        sp_path = project_path / 'single_prompt.txt'
        if not sp_path.exists():
            print(f"[ERROR] single_prompt.txt not found in {project_path}")
            sys.exit(1)
        single_prompt_text = sp_path.read_text(encoding='utf-8').strip()
        if not single_prompt_text:
            print(f"[ERROR] single_prompt.txt is empty")
            sys.exit(1)
        for cid in all_clip_ids:
            prompts_map[cid] = single_prompt_text
        print(f"  Prompt mode: SINGLE — \"{single_prompt_text[:80]}{'...' if len(single_prompt_text)>80 else ''}\"")
    else:
        prompts_file = args.prompts_file or find_prompts_file(str(project_path))
        if prompts_file:
            prompts_map = extract_all_prompts(prompts_file)
            prompts_map, remapped = remap_dense_prompts_for_montage(prompts_map, clips_data)
            if remapped:
                print("  [Prompts] Remapped dense prompt file to real montage clip IDs")
            print(f"  Prompt mode: PER-CLIP — {len(prompts_map)} prompts from {Path(prompts_file).name}")
        else:
            # Fallback to FU parser
            _pf = _fu_find_prompts_file(project_path)
            if _pf:
                prompts_map = extract_all_prompts(str(_pf)) or _fu_parse_prompts_file(_pf)
                prompts_map, remapped = remap_dense_prompts_for_montage(prompts_map, clips_data)
                if remapped:
                    print("  [Prompts] Remapped dense prompt file to real montage clip IDs")
                print(f"  Prompt mode: PER-CLIP (fallback parser) — {len(prompts_map)} from {_pf.name}")
            else:
                print("  [WARN] Prompts file not found — using empty prompts")

    # ── Init clients ────────────────────────────────────────────────────
    # FastGen has 2 keys: FASTGEN_API_KEY + FASTGEN_API_KEY_2
    max_parallel_default = args.parallel or getattr(config, 'FASTGEN_MAX_PARALLEL', 25)

    slot = args.key_slot
    if slot not in ('1', 'key1', 'main'):
        print(f"  [WARN] Flow Ultra supports only FastGen Key1 here; ignoring --key-slot {slot}")
        logging.warning(f"Flow Ultra pinned to Key1; ignoring --key-slot {slot}")
    slot_labels = {'Key1'}

    ALL_KEY_CONFIGS = [
        ('Key1', args.api_key or getattr(config, 'FASTGEN_API_KEY', ''),   max_parallel_default),
        ('Key2', getattr(config, 'FASTGEN_API_KEY_2', ''),                  getattr(config, 'FASTGEN_MAX_PARALLEL_2', max_parallel_default)),
    ]

    clients_cfg: List[Dict] = []
    for label, api_key, mp_default in ALL_KEY_CONFIGS:
        if not api_key:
            continue
        if slot_labels is not None and label not in slot_labels:
            continue
        client, mp, sem = _init_fu_client(api_key, mp_default, label, no_cancel=args.no_cancel)
        if client is None or mp == 0:
            continue
        clients_cfg.append({
            'client': client, 'mp': mp, 'sem': sem, 'label': label,
            '5xx_count': 0, '5xx_cancelled': False, 'key_dead': False,
        })

    if not clients_cfg:
        print(f"[ERROR] No valid Flow Ultra keys configured for slot={slot}")
        sys.exit(1)

    print(f"  Key slot: {', '.join(c['label'] for c in clients_cfg)}")

    # Register for Hard Stop (Control Panel Stop button signals via _flowultra_shutdown_event)
    for cfg in clients_cfg:
        if cfg['client'] not in _flowultra_active_clients:
            _flowultra_active_clients.append(cfg['client'])

    total_parallel = sum(cfg['mp'] for cfg in clients_cfg)
    aspect = args.aspect or getattr(config, 'VEONONSTOP_ASPECT_RATIO', '16:9')

    # ── Worker ID tracking ──────────────────────────────────────────────
    _worker_counter = {'next': 0}
    _worker_lock    = threading.Lock()
    _worker_map: Dict[int, int] = {}

    def _get_worker_id() -> int:
        tid = threading.get_ident()
        with _worker_lock:
            if tid not in _worker_map:
                _worker_map[tid] = _worker_counter['next']
                _worker_counter['next'] += 1
            return _worker_map[tid]

    # ── Ctrl+C handler ─────────────────────────────────────────────────
    _original_sigint = signal.getsignal(signal.SIGINT)

    def _graceful_shutdown(signum, frame):
        if _shutdown_requested.is_set():
            print(f"\n\n  {_RED}[FORCE QUIT]{_RESET} Second Ctrl+C — exiting!")
            signal.signal(signal.SIGINT, _original_sigint)
            raise KeyboardInterrupt
        _shutdown_requested.set()
        print(f"\n\n  {_YELLOW}[SHUTDOWN]{_RESET} Ctrl+C — cancelling ops...")
        for cfg in clients_cfg:
            try:
                _can = cfg['client'].cancel_all_tasks()
                _tc = _can.get('total_cancelled', 0) if isinstance(_can, dict) else 0
                print(f"  {_YELLOW}[SHUTDOWN]{_RESET} [{cfg['label']}] cancelled {_tc} op(s)")
            except Exception as e:
                print(f"  {_YELLOW}[SHUTDOWN]{_RESET} [{cfg['label']}] cancel failed: {e}")

    signal.signal(signal.SIGINT, _graceful_shutdown)

    # ── Print summary ──────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  PARALLEL FLOW ULTRA KEYFRAMES WATCHER")
    print(f"{'='*60}")
    print(f"  Project:  {project_path.name}")
    print(f"  Watch:    {generated_dir}")
    print(f"  Output:   {vid_img_dir}")
    print(f"  Clips:    {len(all_clip_ids)}")
    print(f"  Parallel: {total_parallel}" + (
        f" ({'+'.join(str(c['mp']) for c in clients_cfg)} keys)" if len(clients_cfg) > 1 else ""
    ))
    print(f"  Aspect:   {aspect}")
    print(f"  Poll:     every {args.poll}s")
    print(f"  Log:      {log_path.name}")
    print(f"{'='*60}")
    print(f"\n  Waiting for images in generated/ ...\n  (Ctrl+C to stop)\n")

    submitted: Set[int] = set()
    active_futures: Dict = {}
    _ok_durations: List[float] = []
    clip_retries: Dict[int, int] = {}
    clip_remixed_img: Dict[int, str] = {}
    clip_remixed: Set[int] = set()
    clip_static_done: Set[int] = set()
    clip_remix_pending: Set[int] = set()
    MAX_CLIP_RETRIES = getattr(config, 'VEONONSTOP_MAX_RETRIES', 5)

    # Pre-mark already done
    for cid in all_clip_ids:
        if _has_video(vid_img_dir, cid):
            submitted.add(cid)
    if submitted:
        print(f"  Already done: {len(submitted)} clips (skipping)")

    # Shared running avg for _process_single_clip_fu
    _ok_run = {'n': 0, 'total': 0.0, 'lock': threading.Lock(), 'wall_start': time.time()}

    def _run_claimed_flowultra(claim, **kwargs):
        _res = None
        try:
            cid = kwargs.get('clip_id')
            if cid is not None and _has_video(vid_img_dir, int(cid)):
                return {
                    'id': int(cid), 'status': 'ok', 'file': None,
                    'duration_sec': 0, 'key_label': 'external',
                }
            _res = _process_single_clip_fu(**kwargs)
            return _res
        finally:
            claim.release()
            # No ultra video accounts: claim освобождён выше — даём VeoNonStop 10с
            # окно забрать клип, пока этот воркер на паузе (иначе FU мгновенно переклеймит).
            if _res and _res.get('no_accounts'):
                logging.info(f"Clip #{kwargs.get('clip_id')}: no ultra accounts — 10s yield window for VeoNonStop")
                time.sleep(10)

    with ThreadPoolExecutor(max_workers=total_parallel) as executor:
        try:
            while not _shutdown_requested.is_set():

                # ── Collect completed futures ─────────────────────────
                done_futs = [f for f in list(active_futures) if f.done()]
                for fut in done_futs:
                    cid = active_futures.pop(fut)
                    try:
                        r      = fut.result()
                        status = r.get('status', '?')
                        icon   = '+' if status == 'ok' else 'X'
                        dur    = r.get('duration_sec', 0)
                        key    = r.get('key_label', '')
                        if status == 'ok' and dur > 0:
                            _ok_durations.append(dur)
                        avg_str = f'  avg={sum(_ok_durations)/len(_ok_durations):.0f}s/{len(_ok_durations)}' if _ok_durations else ''
                        key_str = f' [{key}]' if key else ''
                        _done_color = _GREEN if status == 'ok' else _YELLOW
                        print(f"  {_done_color}[DONE]{_RESET} #{cid:03d} [{icon}]{key_str} {status}  ({dur:.0f}s){avg_str}")
                        logging.info(f"Clip #{cid}: {status} [{key}] ({dur:.0f}s){avg_str}")
                        # Static fallback: status='ok' but no .mp4 — image was copied
                        if status == 'ok' and not _has_video(vid_img_dir, cid):
                            clip_static_done.add(cid)
                            logging.info(f"Clip #{cid}: static fallback detected")
                        # Proactive remix: invalid_argument exhausted → queue remix
                        if (status != 'ok'
                                and r.get('error_counts', {}).get('invalid_argument', 0) > 0
                                and clip_retries.get(cid, 0) >= MAX_CLIP_RETRIES
                                and cid not in clip_remixed
                                and cid not in clip_remix_pending):
                            clip_remix_pending.add(cid)
                            logging.warning(f"Clip #{cid}: invalid_argument exhausted — queued proactive remix")
                    except Exception as e:
                        print(f"  {_RED}[DONE]{_RESET} #{cid:03d} [X] error: {e}")
                        logging.error(f"Clip #{cid}: exception: {e}")

                # ── Proactive retry: re-queue failed clips ────────────
                _in_flight_cids = set(active_futures.values())
                for cid in all_clip_ids:
                    if (cid in submitted
                            and cid not in _in_flight_cids
                            and cid not in clip_static_done
                            and not _has_video(vid_img_dir, cid)
                            and clip_retries.get(cid, 0) < MAX_CLIP_RETRIES
                            and bool(clip_remixed_img.get(cid) or _find_image(generated_dir, cid))):
                        clip_retries[cid] = clip_retries.get(cid, 0) + 1
                        submitted.discard(cid)
                        print(f"  {_YELLOW}[RETRY]{_RESET} #{cid:03d}: re-queuing (attempt {clip_retries[cid]}/{MAX_CLIP_RETRIES})")
                        logging.warning(f"Watch FU: re-queuing clip #{cid} {clip_retries[cid]}/{MAX_CLIP_RETRIES}")

                # ── Proactive remix for invalid_argument ─────
                _fast_censor = getattr(config, 'VEONONSTOP_FAST_CENSOR', 'off')
                if _fast_censor == 'ultrafast':
                    # Move all pending to static fallback (skip remix)
                    for cid in list(clip_remix_pending):
                        clip_remix_pending.discard(cid)
                        clip_remixed.add(cid)
                        orig_img = _find_image(generated_dir, cid)
                        if orig_img:
                            claim = acquire_clip_claim(vid_img_dir, cid, owner='watch_flowultra:ultrafast_static')
                            if not claim:
                                if _has_video(vid_img_dir, cid):
                                    clip_static_done.add(cid)
                                    submitted.add(cid)
                                continue
                            dest = vid_img_dir / f"{cid:03d}{Path(orig_img).suffix}"
                            try:
                                shutil.copy(orig_img, str(dest))
                                clip_static_done.add(cid)
                                print(f"  {_YELLOW}[ULTRAFAST STATIC]{_RESET} #{cid:03d}: skip remix → static")
                            except Exception:
                                clip_static_done.add(cid)
                            finally:
                                claim.release()
                if clip_remix_pending:
                    _in_flight_cids_now = set(active_futures.values())
                    for cid in list(clip_remix_pending):
                        if cid in _in_flight_cids_now:
                            continue
                        clip_remix_pending.discard(cid)
                        orig_img = _find_image(generated_dir, cid)
                        if not orig_img:
                            clip_static_done.add(cid)
                            continue
                        print(f"  {_YELLOW}[WATCH REMIX]{_RESET} #{cid:03d}: invalid_argument exhausted — remixing via the remixer...")
                        logging.warning(f"Watch FU: proactive remix #{cid} (invalid_argument)")
                        _remixed_path = _image_remix(orig_img, cid, vid_img_dir)
                        if _remixed_path:
                            clip_remixed_img[cid] = _remixed_path
                            clip_remixed.add(cid)
                            clip_retries[cid] = 0
                            submitted.discard(cid)
                            logging.info(f"Clip #{cid}: proactive remix OK -> {_remixed_path}")
                        else:
                            clip_remixed.add(cid)
                            ext = Path(orig_img).suffix
                            claim = acquire_clip_claim(vid_img_dir, cid, owner='watch_flowultra:proactive_static')
                            if not claim:
                                if _has_video(vid_img_dir, cid):
                                    clip_static_done.add(cid)
                                    submitted.add(cid)
                                continue
                            dest = vid_img_dir / f"{cid:03d}{ext}"
                            try:
                                shutil.copy(orig_img, str(dest))
                                clip_static_done.add(cid)
                                print(f"  {_YELLOW}[STATIC]{_RESET} #{cid:03d}: remix failed — static fallback")
                                logging.warning(f"Clip #{cid}: remix failed, static -> {dest.name}")
                            except Exception as _fe:
                                logging.error(f"Clip #{cid}: static copy failed: {_fe}")
                                clip_static_done.add(cid)
                            finally:
                                claim.release()

                # ── Check for new images — portioned submit ──────────
                for cid in all_clip_ids:
                    if cid in submitted or cid in clip_static_done or _shutdown_requested.is_set():
                        continue
                    if _has_video(vid_img_dir, cid):
                        submitted.add(cid)
                        continue
                    if len(active_futures) >= total_parallel:
                        continue
                    _remixed = clip_remixed_img.get(cid)
                    if _remixed and Path(_remixed).exists():
                        img = _remixed
                    else:
                        img = _find_image(generated_dir, cid)
                    if img and _is_stable(img):
                        # Vislide: non-animated clips → static fallback immediately
                        if cid not in animated_ids:
                            claim = acquire_clip_claim(vid_img_dir, cid, owner='watch_flowultra:vislide_static')
                            if not claim:
                                if _has_video(vid_img_dir, cid):
                                    clip_static_done.add(cid)
                                    submitted.add(cid)
                                continue
                            ext = Path(img).suffix
                            dest = vid_img_dir / f"{cid:03d}{ext}"
                            try:
                                shutil.copy(img, str(dest))
                                clip_static_done.add(cid)
                                submitted.add(cid)
                                sz_kb = Path(img).stat().st_size // 1024
                                print(f"  [STATIC] #{cid:03d} ({sz_kb}KB) → static (vislide)")
                                logging.info(f"Clip #{cid}: vislide static -> {dest.name}")
                            except Exception as _fe:
                                logging.error(f"Clip #{cid}: vislide static copy failed: {_fe}")
                            finally:
                                claim.release()
                            continue
                        prompt = prompts_map.get(cid, '')
                        claim = acquire_clip_claim(vid_img_dir, cid, owner='watch_flowultra:keyframes')
                        if not claim:
                            if _has_video(vid_img_dir, cid):
                                submitted.add(cid)
                            continue
                        # End image (optional, for paired keyframes)
                        end_img_path = None
                        if args.end_image_dir:
                            _end_dir = Path(args.end_image_dir)
                            try:
                                _next_cid = all_clip_ids[all_clip_ids.index(cid) + 1]
                            except (ValueError, IndexError):
                                _next_cid = cid
                            for _ext in IMAGE_EXTENSIONS:
                                _cand = _end_dir / f"{_next_cid:03d}{_ext}"
                                if _cand.exists():
                                    end_img_path = str(_cand)
                                    break
                        # Submit FU keyframes generation (worker-assignment via clients_cfg_list)
                        fut = executor.submit(
                            _run_claimed_flowultra,
                            claim,
                            client=None,
                            clip_id=cid,
                            prompt=prompt,
                            mode='keyframes',
                            image_path=img,
                            ref_paths=None,
                            end_image_path=end_img_path,
                            output_dir=vid_img_dir,
                            aspect_ratio=aspect,
                            worker_id_fn=_get_worker_id,
                            single_prompt_mode=single_prompt_mode,
                            clients_cfg_list=clients_cfg,
                            ok_run=_ok_run,
                        )
                        active_futures[fut] = cid
                        submitted.add(cid)
                        sz_kb = Path(img).stat().st_size // 1024
                        print(f"  {_YELLOW}[NEW]{_RESET} #{cid:03d} ({sz_kb}KB) → Flow Ultra keyframes...")
                        logging.info(f"Clip #{cid}: submitted from {Path(img).name}")

                # ── Status line ──────────────────────────────────────
                done_count = sum(1 for cid in all_clip_ids if _has_video(vid_img_dir, cid))
                in_flight  = len(active_futures)
                waiting    = len([cid for cid in all_clip_ids
                                  if cid not in submitted
                                  and cid not in clip_static_done
                                  and not _has_video(vid_img_dir, cid)])
                static_n   = len(clip_static_done)
                static_sfx = f'  static={static_n}' if static_n else ''
                print(f"  [STATUS] done={done_count}/{len(all_clip_ids)}{static_sfx}  in-flight={in_flight}  waiting-for-image={waiting}")

                # ── Exit conditions ─────────────────────────────────
                if done_count + len(clip_static_done) >= len(all_clip_ids) and in_flight == 0:
                    print(f"\n  {_GREEN}[WATCH]{_RESET} All {len(all_clip_ids)} clips complete!")
                    break

                if waiting == 0 and in_flight == 0 and len(submitted) + len(clip_static_done) >= len(all_clip_ids):
                    failed_ids = [cid for cid in all_clip_ids
                                  if not _has_video(vid_img_dir, cid) and cid not in clip_static_done]
                    if not failed_ids:
                        print(f"\n  {_GREEN}[WATCH]{_RESET} All submitted and completed!")
                        break

                    # ── Level 2: remix для clips not yet remixed ──
                    _fast_censor = getattr(config, 'VEONONSTOP_FAST_CENSOR', 'off')
                    if _fast_censor == 'ultrafast':
                        remix_candidates = []
                    else:
                        remix_candidates = [
                            cid for cid in failed_ids
                            if cid not in clip_remixed
                            and bool(_find_image(generated_dir, cid))
                        ]
                    if remix_candidates:
                        print(f"\n  {_YELLOW}[WATCH REMIX]{_RESET} {len(remix_candidates)} clip(s) failed — applying remix...")
                        logging.warning(f"Watch FU: remix for: {remix_candidates}")
                        for cid in remix_candidates:
                            orig_img = _find_image(generated_dir, cid)
                            print(f"  {_YELLOW}[WATCH REMIX]{_RESET} #{cid:03d}: remixing via the remixer...")
                            _remixed_path = _image_remix(orig_img, cid, vid_img_dir)
                            if _remixed_path:
                                clip_remixed_img[cid] = _remixed_path
                                clip_remixed.add(cid)
                                clip_retries[cid] = 0
                                submitted.discard(cid)
                                logging.info(f"Clip #{cid}: remix OK -> {_remixed_path}")
                            else:
                                clip_remixed.add(cid)
                                ext = Path(orig_img).suffix
                                claim = acquire_clip_claim(vid_img_dir, cid, owner='watch_flowultra:remix_static')
                                if not claim:
                                    if _has_video(vid_img_dir, cid):
                                        clip_static_done.add(cid)
                                        submitted.add(cid)
                                    continue
                                dest = vid_img_dir / f"{cid:03d}{ext}"
                                try:
                                    shutil.copy(orig_img, str(dest))
                                    clip_static_done.add(cid)
                                    print(f"  {_YELLOW}[STATIC]{_RESET} #{cid:03d}: remix failed — copied original")
                                    logging.warning(f"Clip #{cid}: remix failed, static -> {dest.name}")
                                except Exception as _fe:
                                    logging.error(f"Clip #{cid}: static copy failed: {_fe}")
                                    clip_static_done.add(cid)
                                finally:
                                    claim.release()
                        continue  # loop re-submits remixed ones

                    # ── Level 3: static fallback для ostalnyh ──
                    still_failed = [cid for cid in failed_ids if cid not in clip_static_done]
                    if still_failed:
                        print(f"\n  {_YELLOW}[WATCH STATIC]{_RESET} {len(still_failed)} clip(s) exhausted — static fallback...")
                        logging.warning(f"Watch FU: static fallback for: {still_failed}")
                        for cid in still_failed:
                            _src = clip_remixed_img.get(cid) or _find_image(generated_dir, cid)
                            if _src and Path(_src).exists():
                                ext = Path(_src).suffix
                                claim = acquire_clip_claim(vid_img_dir, cid, owner='watch_flowultra:final_static')
                                if not claim:
                                    if _has_video(vid_img_dir, cid):
                                        clip_static_done.add(cid)
                                        submitted.add(cid)
                                    continue
                                dest = vid_img_dir / f"{cid:03d}{ext}"
                                try:
                                    shutil.copy(_src, str(dest))
                                    clip_static_done.add(cid)
                                    print(f"  {_YELLOW}[STATIC]{_RESET} #{cid:03d}: copied to vid_img/")
                                    logging.warning(f"Clip #{cid}: static -> {dest.name}")
                                except Exception as _fe:
                                    logging.error(f"Clip #{cid}: static copy failed: {_fe}")
                                finally:
                                    claim.release()
                            clip_static_done.add(cid)
                    break

                if _sleep_interruptible(args.poll):
                    break

        except KeyboardInterrupt:
            print("\n  [WATCH] Interrupted.")

    # Unregister clients from Hard Stop pool
    for cfg in clients_cfg:
        try:
            _flowultra_active_clients.remove(cfg['client'])
        except (ValueError, Exception):
            pass

    done_count = sum(1 for cid in all_clip_ids if _has_video(vid_img_dir, cid))
    failed_ids = [cid for cid in all_clip_ids
                  if not _has_video(vid_img_dir, cid) and cid not in clip_static_done]
    print(f"\n{'='*60}")
    print(f"  WATCH FLOW ULTRA KEYFRAMES DONE")
    print(f"  Animated: {done_count}/{len(all_clip_ids)}")
    if clip_static_done:
        print(f"  Static fallback: {sorted(clip_static_done)}")
    if failed_ids:
        print(f"  FAILED clips: {failed_ids}")
    print(f"{'='*60}")
    logging.info(f"Watch FU done: {done_count}/{len(all_clip_ids)} done, static={sorted(clip_static_done)}, failed: {failed_ids}")
    signal.signal(signal.SIGINT, _original_sigint)


if __name__ == '__main__':
    main()
