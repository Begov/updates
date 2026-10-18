"""
live_veo.py — Watch generated/ and animate images as they appear via VeoNonStop i2v.

Run in parallel with FastGen to animate images without waiting for all to finish.
Supports dual-key and single-prompt / per-clip prompt modes.
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
from units.veo_client import VeoNonStopClient
from units.veo_motion import _process_single_clip, _shutdown_requested, _image_remix
from units.motion_claims import acquire_clip_claim, clip_media_exists
import units.veo_motion as _vv

IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp'}
MIN_FILE_SIZE    = 5_000   # bytes
FILE_STABLE_SECS = 3       # seconds without modification = file complete

_YELLOW = '\033[33m'
_GREEN  = '\033[32m'
_RED    = '\033[31m'
_RESET  = '\033[0m'


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


def _init_client(api_key: Optional[str], max_parallel: int, label: str, no_cancel: bool = False, base_url: Optional[str] = None):
    """Create client, cancel zombies, cap by concurrent_tasks, return (client, mp, sem)."""
    client = VeoNonStopClient(api_key=api_key, base_url=base_url)
    # Отменяем зомби-задачи от предыдущих прогонов
    # no_cancel=True пропускает (когда параллельно идёт другая генерация)
    if no_cancel:
        print(f"  [{label}] --no-cancel: skipping zombie task cleanup.")
    else:
        try:
            n = client.cancel_all_tasks()
            if n:
                print(f"  [{label}] Cancelled {n} zombie task(s). Waiting 3s...")
                time.sleep(3)
            else:
                print(f"  [{label}] No zombie tasks.")
        except Exception as e:
            print(f"  [{label}] cancel_all_tasks failed: {e}")
    try:
        account = client.get_account_info()
        ct = account.get('concurrent_tasks', max_parallel)
        mp = min(max_parallel, ct)
        print(f"  [{label}] Plan: {account.get('plan','?')} | concurrent_tasks: {ct} | using: {mp}")
        logging.info(f"{label}: plan={account.get('plan')}, concurrent_tasks={ct}, mp={mp}")
    except Exception as e:
        mp = max_parallel
        logging.warning(f"{label}: could not get account info: {e}")
    sem = threading.Semaphore(max(1, mp))  # Full server-slot capacity (was mp//4, bumped to match videogen path)
    return client, mp, sem


def main():
    parser = argparse.ArgumentParser(description='Watch generated/ and animate with i2v')
    parser.add_argument('--project',  required=True, help='Project name or full path')
    parser.add_argument('--parallel', type=int, default=None)
    parser.add_argument('--aspect',   default=None)
    parser.add_argument('--poll',     type=int, default=15, help='Poll interval seconds')
    parser.add_argument('--api-key',  default=None, help='VeoNonStop API key (overrides .env)')
    parser.add_argument('--key-slot', default='both',
                        help='Which API key(s): 1-4, comma-separated (1,2), or both/all (default)')
    parser.add_argument('--no-cancel', action='store_true', default=False,
                        help='Skip cancel-all-tasks on startup (use when another generation runs in parallel)')
    parser.add_argument('--single-prompt', action='store_true',
                        help='Use single_prompt.txt from project dir for all clips')
    parser.add_argument('--vislide-interval', type=int, default=0, metavar='N',
                        help='Vislide mode: animate only every N-th clip (0 = animate all)')
    parser.add_argument('--prompts-file', default=None,
                        help='Explicit prompts file path (e.g. VEO-adapted prompts)')
    args = parser.parse_args()

    # --- Resolve project path ---
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

    # --- Logging ---
    log_ts   = time.strftime('%Y%m%d_%H%M%S')
    log_path = project_path / f'watch_i2v_{log_ts}.log'
    logging.basicConfig(
        level=logging.INFO,
        handlers=[logging.FileHandler(str(log_path), encoding='utf-8')],
        format='%(asctime)s | %(levelname)-8s | %(message)s',
    )
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('requests').setLevel(logging.WARNING)

    # --- Load montage_plan ---
    plan_path = project_path / 'montage_plan.json'
    if not plan_path.exists():
        print(f"[ERROR] montage_plan.json not found in {project_path}")
        sys.exit(1)
    with open(plan_path, 'r', encoding='utf-8-sig') as f:
        data = json.load(f)
    plan         = data.get('montage_plan', data)
    clips_data   = plan.get('clips', [])
    all_clip_ids = [c['id'] for c in clips_data]
    if not all_clip_ids:
        print("[ERROR] No clips in montage_plan.json")
        sys.exit(1)
    from units.source_ledger import VIDEO_SKIP_SOURCES
    external_ids = {
        c['id'] for c in clips_data
        if c.get('source') in VIDEO_SKIP_SOURCES
    }
    if external_ids:
        all_clip_ids = [cid for cid in all_clip_ids if cid not in external_ids]
        print(f"  [External] Skipping {len(external_ids)} clips with ready-made sources (stock, graphics)")

    # --- Vislide filter: каждый N-й клип + intro block → i2v ---
    vislide_interval = args.vislide_interval
    if vislide_interval >= 2:
        animated_ids = {cid for i, cid in enumerate(all_clip_ids) if (i + 1) % vislide_interval == 0}
        # Force intro block clips
        intro_dur = config.INTRO_BLOCK_DURATION
        intro_ids = {c['id'] for c in clips_data if c.get('startTime', 0) < intro_dur}
        intro_ids -= external_ids
        animated_ids |= intro_ids
        print(f"  [Vislide] interval={vislide_interval}, intro={intro_dur}s ({len(intro_ids)} clips): "
              f"{len(animated_ids)}/{len(all_clip_ids)} clips will be animated")
        logging.info(f"Vislide mode: interval={vislide_interval}, intro_ids={sorted(intro_ids)}, "
                     f"animated_ids={sorted(animated_ids)}")
    else:
        animated_ids = set(all_clip_ids)

    # --- Load prompts ---
    prompts_map: Dict[int, str] = {}
    single_prompt_mode = args.single_prompt

    if single_prompt_mode:
        sp_path = project_path / 'single_prompt.txt'
        if not sp_path.exists():
            print(f"[ERROR] single_prompt.txt not found in {project_path}")
            sys.exit(1)
        single_prompt_text = sp_path.read_text(encoding='utf-8-sig').strip()
        if not single_prompt_text:
            print(f"[ERROR] single_prompt.txt is empty")
            sys.exit(1)
        for cid in all_clip_ids:
            prompts_map[cid] = single_prompt_text
        print(f"  Prompt mode: SINGLE — \"{single_prompt_text[:80]}{'...' if len(single_prompt_text)>80 else ''}\"")
    else:
        # Explicit prompts file (e.g. VEO-adapted) takes priority over auto-detect
        prompts_file = args.prompts_file or find_prompts_file(str(project_path))
        if prompts_file:
            prompts_map = extract_all_prompts(prompts_file)
            prompts_map, remapped = remap_dense_prompts_for_montage(prompts_map, clips_data)
            if remapped:
                print("  [Prompts] Remapped dense prompt file to real montage clip IDs")
            print(f"  Prompt mode: PER-CLIP — {len(prompts_map)} prompts from {Path(prompts_file).name}")
        else:
            print("  [WARN] Prompts file not found — using empty prompts")

    # --- Init clients ---
    max_parallel = args.parallel or config.VEONONSTOP_MAX_PARALLEL

    # Parse --key-slot: '1', '2', '3', '4', '1,2', '1,2,3', 'both', 'all'
    slot = args.key_slot
    if slot in ('both', 'all'):
        slot_labels = None  # all configured keys
    else:
        slot_labels = {f'Key{s.strip()}' for s in slot.split(',')}

    ALL_KEY_CONFIGS = [
        ('Key1', args.api_key or config.VEONONSTOP_API_KEY,   max_parallel,                      config.VEONONSTOP_API_BASE),
        ('Key2', config.VEONONSTOP_API_KEY_2, config.VEONONSTOP_MAX_PARALLEL_2, config.VEONONSTOP_API_BASE_2),
        ('Key3', config.VEONONSTOP_API_KEY_3, config.VEONONSTOP_MAX_PARALLEL_3, config.VEONONSTOP_API_BASE_3),
        ('Key4', config.VEONONSTOP_API_KEY_4, config.VEONONSTOP_MAX_PARALLEL_4, config.VEONONSTOP_API_BASE_4),
    ]

    clients_cfg: List[Dict] = []
    # Свой провайдер видео (Vertex / RoyalTechno / VARAG) — ключи VeoNonStop не поднимаем
    # вовсе: иначе ниже на них ушла бы чистка «зомби-задач».
    from units.vertex_client import vertex_selected as _vx_sel
    from units.royaltechno_client import royaltechno_selected as _rt_sel
    from units.varag_client import varag_selected as _vg_sel
    _own_provider = _vx_sel('video') or _rt_sel() or _vg_sel('video')
    for label, api_key, mp_default, base_url in ([] if _own_provider else ALL_KEY_CONFIGS):
        if not api_key:
            continue
        if slot_labels is not None and label not in slot_labels:
            continue
        client, mp, sem = _init_client(api_key, mp_default, label, no_cancel=args.no_cancel, base_url=base_url)
        clients_cfg.append({'client': client, 'mp': mp, 'sem': sem, 'label': label})

    from units.vertex_client import build_vertex_cfg, vertex_selected
    if vertex_selected('video'):
        clients_cfg = build_vertex_cfg('video')
    from units.royaltechno_client import build_royaltechno_cfg, royaltechno_selected
    if royaltechno_selected():
        clients_cfg = build_royaltechno_cfg()
    from units.varag_client import build_varag_cfg, varag_selected
    if varag_selected('video'):
        clients_cfg = build_varag_cfg('video')

    if not clients_cfg:
        print(f"[ERROR] No valid keys configured for slot={slot}")
        sys.exit(1)

    print(f"  Key slot: {', '.join(c['label'] for c in clients_cfg)}")

    total_parallel = sum(cfg['mp'] for cfg in clients_cfg)

    # Disable global semaphore — each cfg has its own. Also mirror the
    # per-key caps into veo_video.py; _process_single_clip() uses helpers from
    # that module for pre-submit usage gating. Without this watcher-specific
    # init the helpers see their default cap=0 and clamp every key to 1 slot.
    _vv._submit_semaphore = None
    _vv._max_parallel_global = total_parallel
    _vv._max_parallel_by_key = {cfg['label']: cfg['mp'] for cfg in clients_cfg}
    with _vv._inflight_by_key_lock:
        _vv._inflight_by_key = {cfg['label']: set() for cfg in clients_cfg}
    print("  Per-key caps: " + ", ".join(f"{cfg['label']}={cfg['mp']}" for cfg in clients_cfg))

    aspect = args.aspect or config.VEONONSTOP_ASPECT_RATIO

    # --- Worker ID tracking ---
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

    # --- Ctrl+C handler ---
    _original_sigint = signal.getsignal(signal.SIGINT)

    def _graceful_shutdown(signum, frame):
        if _shutdown_requested.is_set():
            print(f"\n\n  {_RED}[FORCE QUIT]{_RESET} Second Ctrl+C — exiting!")
            signal.signal(signal.SIGINT, _original_sigint)
            raise KeyboardInterrupt
        _shutdown_requested.set()
        print(f"\n\n  {_YELLOW}[SHUTDOWN]{_RESET} Ctrl+C — cancelling tasks...")
        for cfg in clients_cfg:
            try:
                n = cfg['client'].cancel_all_tasks()
                print(f"  {_YELLOW}[SHUTDOWN]{_RESET} [{cfg['label']}] cancelled {n} task(s)")
            except Exception as e:
                print(f"  {_YELLOW}[SHUTDOWN]{_RESET} [{cfg['label']}] cancel failed: {e}")

    signal.signal(signal.SIGINT, _graceful_shutdown)

    # --- Print summary ---
    print(f"\n{'='*60}")
    print(f"  PARALLEL I2V WATCHER")
    print(f"{'='*60}")
    print(f"  Project:  {project_path.name}")
    print(f"  Watch:    {generated_dir}")
    print(f"  Output:   {vid_img_dir}")
    print(f"  Clips:    {len(all_clip_ids)}")
    print(f"  Parallel: {total_parallel}" + (f" ({'+'.join(str(c['mp']) for c in clients_cfg)} keys)" if len(clients_cfg) > 1 else ""))
    print(f"  Aspect:   {aspect}")
    print(f"  Poll:     every {args.poll}s")
    print(f"  Log:      {log_path.name}")
    print(f"{'='*60}")
    print(f"\n  Waiting for images in generated/ ...\n  (Ctrl+C to stop)\n")

    submitted: Set[int] = set()
    active_futures: Dict = {}   # future -> cid
    _ok_durations: List[float] = []  # для расчёта среднего времени
    clip_retries: Dict[int, int] = {}     # cid -> plain retry count
    clip_remixed_img: Dict[int, str] = {} # cid -> remixed image path (replaces original on submit)
    clip_remixed: Set[int] = set()        # cids that already went through remix
    clip_static_done: Set[int] = set()    # cids that got static fallback (image copied to vid_img/)
    clip_remix_pending: Set[int] = set()  # cids queued for proactive remix (invalid_argument)
    MAX_CLIP_RETRIES = config.VEONONSTOP_MAX_RETRIES

    # Pre-mark already done
    for cid in all_clip_ids:
        if _has_video(vid_img_dir, cid):
            submitted.add(cid)
    if submitted:
        print(f"  Already done: {len(submitted)} clips (skipping)")

    def _run_claimed_i2v(claim, **kwargs):
        try:
            cid = kwargs.get('clip_id')
            if cid is not None and _has_video(vid_img_dir, int(cid)):
                return {
                    'id': int(cid), 'status': 'ok', 'file': None,
                    'duration_sec': 0, 'key_label': 'external',
                }
            return _process_single_clip(**kwargs)
        finally:
            claim.release()

    with ThreadPoolExecutor(max_workers=total_parallel) as executor:
        try:
            while not _shutdown_requested.is_set():

                # --- Collect completed futures ---
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
                        # Static fallback: status='ok' but no .mp4 — image was copied instead
                        if status == 'ok' and not _has_video(vid_img_dir, cid):
                            clip_static_done.add(cid)
                            logging.info(f"Clip #{cid}: static fallback detected — added to clip_static_done")
                        # Proactive remix: invalid_argument + retries exhausted → queue remix now
                        if (status != 'ok'
                                and r.get('errors', {}).get('invalid_argument', 0) > 0
                                and clip_retries.get(cid, 0) >= MAX_CLIP_RETRIES
                                and cid not in clip_remixed
                                and cid not in clip_remix_pending):
                            clip_remix_pending.add(cid)
                            logging.warning(f"Clip #{cid}: invalid_argument exhausted retries — queued for proactive remix")
                    except Exception as e:
                        print(f"  {_RED}[DONE]{_RESET} #{cid:03d} [X] error: {e}")
                        logging.error(f"Clip #{cid}: exception: {e}")

                # --- Proactive retry: re-queue failed clips if retries not exhausted ---
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
                        logging.warning(f"Watch i2v: re-queuing clip #{cid} attempt {clip_retries[cid]}/{MAX_CLIP_RETRIES}")

                # --- Proactive remix for invalid_argument clips ---
                # FAST_CENSOR=ultrafast → skip proactive remix too
                _fast_censor = getattr(config, 'VEONONSTOP_FAST_CENSOR', 'off')
                if _fast_censor == 'ultrafast':
                    # Move all pending to static fallback
                    for cid in list(clip_remix_pending):
                        clip_remix_pending.discard(cid)
                        clip_remixed.add(cid)
                        orig_img = _find_image(generated_dir, cid)
                        if orig_img:
                            claim = acquire_clip_claim(vid_img_dir, cid, owner='watch_i2v:ultrafast_static')
                            if not claim:
                                if _has_video(vid_img_dir, cid):
                                    clip_static_done.add(cid)
                                    submitted.add(cid)
                                continue
                            dest = vid_img_dir / f"{cid:03d}{Path(orig_img).suffix}"
                            try:
                                shutil.copy(orig_img, str(dest))
                                clip_static_done.add(cid)
                                print(f"  {_YELLOW}[ULTRAFAST STATIC]{_RESET} #{cid:03d}: skipping remix → static")
                            except Exception:
                                clip_static_done.add(cid)
                            finally:
                                claim.release()
                if clip_remix_pending:
                    _in_flight_cids_now = set(active_futures.values())
                    for cid in list(clip_remix_pending):
                        if cid in _in_flight_cids_now:
                            continue  # still running, wait
                        clip_remix_pending.discard(cid)
                        orig_img = _find_image(generated_dir, cid)
                        if not orig_img:
                            clip_static_done.add(cid)
                            continue
                        print(f"  {_YELLOW}[WATCH REMIX]{_RESET} #{cid:03d}: invalid_argument exhausted — remixing via the remixer...")
                        logging.warning(f"Watch i2v: proactive remix #{cid} (invalid_argument)")
                        _remixed_path = _image_remix(orig_img, cid, vid_img_dir)
                        if _remixed_path:
                            clip_remixed_img[cid] = _remixed_path
                            clip_remixed.add(cid)
                            clip_retries[cid] = 0
                            submitted.discard(cid)
                            logging.info(f"Clip #{cid}: proactive remix OK -> {_remixed_path}, retries reset")
                        else:
                            clip_remixed.add(cid)
                            ext = Path(orig_img).suffix
                            claim = acquire_clip_claim(vid_img_dir, cid, owner='watch_i2v:proactive_static')
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
                                logging.warning(f"Clip #{cid}: proactive remix failed, static fallback -> {dest.name}")
                            except Exception as _fe:
                                logging.error(f"Clip #{cid}: static fallback copy failed: {_fe}")
                                clip_static_done.add(cid)
                            finally:
                                claim.release()

                # --- Check for new images (portioned: keep at most total_parallel in-flight) ---
                for cid in all_clip_ids:
                    if cid in submitted or cid in clip_static_done or _shutdown_requested.is_set():
                        continue
                    if _has_video(vid_img_dir, cid):
                        submitted.add(cid)
                        continue
                    # Portioned submit: don't exceed total_parallel in-flight futures
                    if len(active_futures) >= total_parallel:
                        continue
                    # Use remixed image if available, otherwise look in generated/
                    _remixed = clip_remixed_img.get(cid)
                    if _remixed and Path(_remixed).exists():
                        img = _remixed
                    else:
                        img = _find_image(generated_dir, cid)
                    if img and _is_stable(img):
                        # Vislide: non-animated clips → static fallback immediately
                        if cid not in animated_ids:
                            claim = acquire_clip_claim(vid_img_dir, cid, owner='watch_i2v:vislide_static')
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
                                logging.info(f"Clip #{cid}: vislide static fallback -> {dest.name}")
                            except Exception as _fe:
                                logging.error(f"Clip #{cid}: vislide static copy failed: {_fe}")
                            finally:
                                claim.release()
                            continue
                        prompt = prompts_map.get(cid, '')
                        claim = acquire_clip_claim(vid_img_dir, cid, owner='watch_i2v:i2v')
                        if not claim:
                            if _has_video(vid_img_dir, cid):
                                submitted.add(cid)
                            continue
                        # Worker-assignment: клиент определяется ВНУТРИ воркера по worker_id
                        fut = executor.submit(
                            _run_claimed_i2v,
                            claim,
                            client=None,
                            clip_id=cid,
                            prompt=prompt,
                            mode='i2v',
                            image_path=img,
                            ref_dir=None,
                            output_dir=vid_img_dir,
                            aspect_ratio=aspect,
                            worker_id_fn=_get_worker_id,
                            single_prompt_mode=single_prompt_mode,
                            clients_cfg_list=clients_cfg,
                        )
                        active_futures[fut] = cid
                        submitted.add(cid)
                        sz_kb = Path(img).stat().st_size // 1024
                        print(f"  {_YELLOW}[NEW]{_RESET} #{cid:03d} ({sz_kb}KB) → i2v...")
                        logging.info(f"Clip #{cid}: submitted from {Path(img).name}")

                # --- Status line ---
                done_count = sum(1 for cid in all_clip_ids if _has_video(vid_img_dir, cid))
                in_flight  = len(active_futures)
                waiting    = len([cid for cid in all_clip_ids
                                  if cid not in submitted
                                  and cid not in clip_static_done
                                  and not _has_video(vid_img_dir, cid)])
                static_n   = len(clip_static_done)
                static_sfx = f'  static={static_n}' if static_n else ''
                print(f"  [STATUS] done={done_count}/{len(all_clip_ids)}{static_sfx}  in-flight={in_flight}  waiting-for-image={waiting}")

                # --- Exit conditions ---
                if done_count + len(clip_static_done) >= len(all_clip_ids) and in_flight == 0:
                    print(f"\n  {_GREEN}[WATCH]{_RESET} All {len(all_clip_ids)} clips complete!")
                    break

                if waiting == 0 and in_flight == 0 and len(submitted) + len(clip_static_done) >= len(all_clip_ids):
                    failed_ids = [cid for cid in all_clip_ids
                                  if not _has_video(vid_img_dir, cid) and cid not in clip_static_done]
                    if not failed_ids:
                        print(f"\n  {_GREEN}[WATCH]{_RESET} All submitted and completed!")
                        break

                    # --- Level 2: remix for clips not yet remixed ---
                    # FAST_CENSOR=ultrafast → skip remix, go straight to static fallback
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
                        print(f"\n  {_YELLOW}[WATCH REMIX]{_RESET} {len(remix_candidates)} clip(s) failed after {MAX_CLIP_RETRIES} retries — applying remix (vision check included)...")
                        logging.warning(f"Watch i2v: remix for: {remix_candidates}")
                        for cid in remix_candidates:
                            orig_img = _find_image(generated_dir, cid)
                            print(f"  {_YELLOW}[WATCH REMIX]{_RESET} #{cid:03d}: remixing via the remixer...")
                            _remixed_path = _image_remix(orig_img, cid, vid_img_dir)
                            if _remixed_path:
                                clip_remixed_img[cid] = _remixed_path
                                clip_remixed.add(cid)
                                clip_retries[cid] = 0
                                submitted.discard(cid)
                                logging.info(f"Clip #{cid}: remix OK -> {_remixed_path}, retries reset")
                            else:
                                # Remix itself failed — static fallback immediately
                                clip_remixed.add(cid)
                                ext = Path(orig_img).suffix
                                claim = acquire_clip_claim(vid_img_dir, cid, owner='watch_i2v:remix_static')
                                if not claim:
                                    if _has_video(vid_img_dir, cid):
                                        clip_static_done.add(cid)
                                        submitted.add(cid)
                                    continue
                                dest = vid_img_dir / f"{cid:03d}{ext}"
                                try:
                                    shutil.copy(orig_img, str(dest))
                                    clip_static_done.add(cid)
                                    print(f"  {_YELLOW}[STATIC]{_RESET} #{cid:03d}: remix failed — copied original image to vid_img/")
                                    logging.warning(f"Clip #{cid}: remix failed, static fallback -> {dest.name}")
                                except Exception as _fe:
                                    logging.error(f"Clip #{cid}: static fallback copy failed: {_fe}")
                                    clip_static_done.add(cid)
                                finally:
                                    claim.release()
                        # don't break — loop re-submits remixed ones
                        continue

                    # --- Level 3: static fallback (remix exhausted, still failing) ---
                    still_failed = [cid for cid in failed_ids if cid not in clip_static_done]
                    if still_failed:
                        print(f"\n  {_YELLOW}[WATCH STATIC]{_RESET} {len(still_failed)} clip(s) exhausted all recovery — static image fallback...")
                        logging.warning(f"Watch i2v: static fallback for: {still_failed}")
                        for cid in still_failed:
                            _src = clip_remixed_img.get(cid) or _find_image(generated_dir, cid)
                            if _src and Path(_src).exists():
                                ext = Path(_src).suffix
                                claim = acquire_clip_claim(vid_img_dir, cid, owner='watch_i2v:final_static')
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
                                    logging.warning(f"Clip #{cid}: static fallback -> {dest.name}")
                                except Exception as _fe:
                                    logging.error(f"Clip #{cid}: static fallback copy failed: {_fe}")
                                finally:
                                    claim.release()
                            clip_static_done.add(cid)
                    break

                time.sleep(args.poll)

        except KeyboardInterrupt:
            print("\n  [WATCH] Interrupted.")

    done_count = sum(1 for cid in all_clip_ids if _has_video(vid_img_dir, cid))
    failed_ids = [cid for cid in all_clip_ids
                  if not _has_video(vid_img_dir, cid) and cid not in clip_static_done]
    print(f"\n{'='*60}")
    print(f"  WATCH I2V DONE")
    print(f"  Animated: {done_count}/{len(all_clip_ids)}")
    if clip_static_done:
        print(f"  Static fallback: {sorted(clip_static_done)}")
    if failed_ids:
        print(f"  FAILED clips: {failed_ids}")
    print(f"{'='*60}")
    logging.info(f"Watch i2v finished: {done_count}/{len(all_clip_ids)} done, static={sorted(clip_static_done)}, failed: {failed_ids}")
    signal.signal(signal.SIGINT, _original_sigint)


if __name__ == '__main__':
    main()
