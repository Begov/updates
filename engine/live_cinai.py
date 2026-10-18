"""CinAI inflight i2v watcher.

The watcher starts before ImageGen, submits each stable generated frame as
soon as it appears, and drains a bounded window after the parent marks
ImageGen complete.  A non-zero exit means that finalized VideoGen should
handle the remaining clips.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Optional, Set

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))

import params as config
from units.cinai_motion import (
    CinAIVideoClient,
    _find_image,
    _load_project_clips,
    _load_video_prompts,
    snap_duration,
)
from units.source_ledger import VIDEO_SKIP_SOURCES
from units.motion_claims import acquire_clip_claim, clip_media_exists


IMAGE_STABLE_SECONDS = 3
POLL_SECONDS = 5


def _is_stable(path: Path) -> bool:
    try:
        return time.time() - path.stat().st_mtime >= IMAGE_STABLE_SECONDS
    except OSError:
        return False


def _parse_bool(value: str) -> Optional[bool]:
    text = str(value or "").strip().lower()
    if not text:
        return None
    return text in ("1", "true", "yes", "on")


def _resolve_project(value: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute():
        return candidate
    return Path(__file__).parent.parent / "projects" / candidate


def main() -> int:
    parser = argparse.ArgumentParser(description="Watch generated/ and animate frames through CinAI")
    parser.add_argument("--project", required=True)
    parser.add_argument("--parallel", type=int, default=None)
    parser.add_argument("--key-slot", default="1")
    parser.add_argument("--no-cancel", action="store_true")
    parser.add_argument("--aspect", default=None)
    parser.add_argument("--poll", type=int, default=POLL_SECONDS)
    parser.add_argument("--model", default=None)
    parser.add_argument("--mode", default="i2v")
    parser.add_argument("--duration", type=int, default=None)
    parser.add_argument("--resolution", default=None)
    parser.add_argument("--audio", default=None)
    parser.add_argument("--prompts-file", default=None)
    parser.add_argument("--single-prompt", action="store_true")
    parser.add_argument("--vislide-interval", type=int, default=0)
    parser.add_argument("--done-file", default=None)
    parser.add_argument("--drain-seconds", type=int, default=120)
    parser.add_argument("--output-dir", default="vid_img")
    args = parser.parse_args()

    project_path = _resolve_project(args.project)
    if not project_path.exists():
        print(f"[ERROR] Project not found: {project_path}")
        return 1
    if args.mode != "i2v":
        print(f"[ERROR] CinAI inflight supports i2v only, got {args.mode!r}")
        return 1

    generated_dir = project_path / "generated"
    output_dir = project_path / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    done_file = Path(args.done_file) if args.done_file else generated_dir / ".imagegen_done"
    log_path = project_path / f"watch_i2v_cinai_{time.strftime('%Y%m%d_%H%M%S')}.log"
    logging.basicConfig(
        level=logging.INFO,
        handlers=[logging.FileHandler(log_path, encoding="utf-8")],
        format="%(asctime)s | %(levelname)-8s | %(message)s",
    )

    clips = _load_project_clips(project_path)
    clip_map = {
        int(clip["id"]): clip
        for clip in clips
        if clip.get("id") is not None
    }
    all_ids = [
        clip_id
        for clip_id, clip in sorted(clip_map.items())
        if clip.get("source") not in VIDEO_SKIP_SOURCES
    ]
    if args.vislide_interval >= 2:
        from units.vislide import animated_ids as _vislide_animated
        animated_ids = set(_vislide_animated(
            clips, args.vislide_interval, getattr(config, "INTRO_BLOCK_DURATION", 0)))
    else:
        animated_ids = set(all_ids)

    single_prompt = None
    if args.single_prompt:
        single_path = project_path / "single_prompt.txt"
        if single_path.exists():
            single_prompt = single_path.read_text(encoding="utf-8-sig").strip() or None
        # нет файла — как у VeoNonStop: первый промпт на все клипы
        single_prompt = single_prompt or "default"
    prompts = _load_video_prompts(
        project_path,
        all_ids,
        single_prompt=single_prompt,
        prompts_file_path=args.prompts_file,
        motion_mode=args.mode,
    )

    client = CinAIVideoClient()
    workers = max(
        1,
        min(
            int(args.parallel or getattr(config, "CINAI_VIDEO_MAX_PARALLEL", 2)),
            6,
        ),
    )
    model = args.model or config.CINAI_VIDEO_MODEL
    aspect = args.aspect or getattr(config, "CINAI_VIDEO_ASPECT_RATIO", "16:9")
    duration = snap_duration(model, args.duration or getattr(config, "CINAI_VIDEO_DURATION", 5))
    resolution = (
        args.resolution
        if args.resolution is not None
        else getattr(config, "CINAI_VIDEO_RESOLUTION", "")
    )
    audio = _parse_bool(
        args.audio
        if args.audio is not None
        else getattr(config, "CINAI_VIDEO_AUDIO", "")
    )

    stop_event = threading.Event()
    original_sigint = signal.getsignal(signal.SIGINT)

    def stop(signum, frame):
        stop_event.set()
        print("\n[CinAI INFLIGHT] stopping after active jobs...")

    signal.signal(signal.SIGINT, stop)

    upload_cache: Dict[str, str] = {}
    upload_lock = threading.Lock()
    submitted: Set[int] = set()
    completed: Set[int] = set()
    failed: Dict[int, Dict[str, Any]] = {}
    active: Dict[Any, int] = {}
    state_path = project_path / "cinai_video_inflight.json"
    state_lock = threading.Lock()
    states: Dict[str, Dict[str, Any]] = {
        str(clip_id): {"id": clip_id, "status": "waiting"}
        for clip_id in all_ids
    }

    def persist_state() -> None:
        with state_lock:
            payload = {
                "provider": "cinai",
                "model": model,
                "mode": "i2v",
                "output_dir": str(output_dir),
                "updated_at": time.time(),
                "clips": list(states.values()),
            }
        temp_path = state_path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(state_path)

    def set_state(clip_id: int, status: str, **extra: Any) -> None:
        with state_lock:
            states[str(clip_id)] = {
                **states.get(str(clip_id), {"id": clip_id}),
                "id": clip_id,
                "status": status,
                **extra,
            }
        persist_state()

    def upload_once(image: Path) -> str:
        key = str(image.resolve())
        with upload_lock:
            cached = upload_cache.get(key)
        if cached:
            return cached
        url = client.upload_image(str(image))
        with upload_lock:
            upload_cache[key] = url
        return url

    def run_clip(clip_id: int, image: Path) -> Dict[str, Any]:
        claim = acquire_clip_claim(output_dir, clip_id, owner="watch_i2v_cinai")
        if not claim:
            if clip_media_exists(output_dir, clip_id):
                return {"id": clip_id, "status": "already_done"}
            raise RuntimeError(f"clip #{clip_id} is claimed by another worker")
        target = output_dir / f"{clip_id:03d}.mp4"
        try:
            # Без промпта не отправляем: раньше уходил prompt="" — провайдер
            # генерил непойми что. Батч-путь в этом случае честно пишет
            # MissingPromptError — здесь то же самое.
            prompt = (prompts.get(clip_id) or "").strip()
            if not prompt:
                raise RuntimeError(f"No prompt was found for clip #{clip_id}")
            source_url = upload_once(image)
            result = client.generate_video(
                prompt=prompt,
                model=model,
                mode="i2v",
                aspect_ratio=aspect,
                duration=duration,
                images=[source_url],
                resolution=resolution or None,
                audio=audio,
            )
            metadata = client.download_video(str(result["output_url"]), target)
            return {
                "id": clip_id,
                "status": "ok",
                "file": str(target),
                "attempts": result.get("attempts", 1),
                "metadata": metadata,
            }
        finally:
            claim.release()

    print(
        f"[CinAI INFLIGHT] project={project_path.name} model={model} "
        f"mode=i2v clips={len(all_ids)} parallel={workers} drain={args.drain_seconds}s"
    )
    print(f"[CinAI INFLIGHT] waiting for stable frames in {generated_dir}")
    logging.info("Started: model=%s clips=%s output=%s", model, all_ids, output_dir)

    for clip_id in all_ids:
        if clip_media_exists(output_dir, clip_id):
            completed.add(clip_id)
            set_state(clip_id, "downloaded", source="existing")
    persist_state()

    drain_deadline: Optional[float] = None
    active_grace_deadline: Optional[float] = None

    def consume_future(future: Any, clip_id: int) -> None:
        try:
            result = future.result()
            if result.get("status") in ("ok", "already_done"):
                completed.add(clip_id)
                set_state(
                    clip_id,
                    "downloaded",
                    attempts=result.get("attempts", 0),
                )
                print(f"[CinAI INFLIGHT] #{clip_id:03d}: done")
            else:
                failed[clip_id] = result
        except Exception as exc:
            failed[clip_id] = {
                "id": clip_id,
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
            }
            set_state(
                clip_id,
                "failed",
                error_type=type(exc).__name__,
                error=str(exc)[:500],
            )
            logging.warning("Clip #%s failed: %s", clip_id, exc)
            print(f"[CinAI INFLIGHT] #{clip_id:03d}: failed: {exc}")

    try:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="cinai-inflight") as executor:
            while not stop_event.is_set():
                for future in [item for item in list(active) if item.done()]:
                    clip_id = active.pop(future)
                    consume_future(future, clip_id)

                if drain_deadline is None or time.time() < drain_deadline:
                    for clip_id in all_ids:
                        if clip_id in submitted or clip_id in completed:
                            continue
                        if len(active) >= workers:
                            break
                        image = _find_image(generated_dir, clip_id)
                        if not image or not _is_stable(image):
                            continue
                        if clip_id not in animated_ids:
                            completed.add(clip_id)
                            # слайд кладём в vid_img/ сразу, как у вотчера VeoNonStop:
                            # иначе «Довести проект» примет его за недоделанное видео
                            if output_dir.name == "vid_img":
                                target = output_dir / f"{clip_id:03d}{image.suffix}"
                                if not target.exists():
                                    shutil.copy(str(image), str(target))
                            set_state(clip_id, "skipped", reason="vislide")
                            logging.info("Clip #%s skipped by vislide", clip_id)
                            continue
                        future = executor.submit(run_clip, clip_id, image)
                        active[future] = clip_id
                        submitted.add(clip_id)
                        set_state(clip_id, "submitted", image=image.name)
                        print(f"[CinAI INFLIGHT] #{clip_id:03d}: submitted")

                if done_file.exists() and drain_deadline is None:
                    drain_deadline = time.time() + max(0, int(args.drain_seconds))
                    print(
                        f"[CinAI INFLIGHT] ImageGen done; draining for "
                        f"{max(0, int(args.drain_seconds))}s"
                    )

                if len(completed) >= len(all_ids) and not active:
                    break
                if drain_deadline is not None and time.time() >= drain_deadline:
                    if not active:
                        break
                    if active_grace_deadline is None:
                        active_grace_deadline = time.time() + max(
                            15,
                            int(getattr(config, "CINAI_VIDEO_INFLIGHT_ACTIVE_GRACE_SECONDS", 120)),
                        )
                        print(
                            "[CinAI INFLIGHT] Drain window ended; waiting for "
                            "already-submitted jobs before fallback"
                        )
                    elif time.time() >= active_grace_deadline:
                        break
                time.sleep(max(1, int(args.poll)))
    finally:
        signal.signal(signal.SIGINT, original_sigint)

    # A completed download can race the final polling tick. Reconcile it
    # before deciding whether the caller must enter finalized fallback.
    for future in list(active):
        clip_id = active.pop(future)
        if future.done():
            consume_future(future, clip_id)
        elif clip_media_exists(output_dir, clip_id):
            completed.add(clip_id)
            set_state(clip_id, "downloaded", source="reconciled_output")

    unresolved = [
        clip_id
        for clip_id in all_ids
        if clip_id not in completed and not clip_media_exists(output_dir, clip_id)
    ]
    failure_path = project_path / "cinai_video_failures.json"
    if unresolved or failed:
        merged = list(failed.values())
        known = {int(item.get("id")) for item in merged if item.get("id") is not None}
        merged.extend(
            {
                "id": clip_id,
                "model": model,
                "mode": "i2v",
                "error_type": "inflight_drain_timeout",
                "error": "Image was not available or generation did not finish before the inflight drain deadline",
            }
            for clip_id in unresolved
            if clip_id not in known
        )
        failure_path.write_text(
            json.dumps(sorted(merged, key=lambda item: int(item.get("id", 0))), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        for clip_id in unresolved:
            set_state(
                clip_id,
                "failed",
                error_type="inflight_drain_timeout",
                error="Image was not available or generation did not finish before the inflight drain deadline",
            )

    print(
        f"[CinAI INFLIGHT] done={len(all_ids) - len(unresolved)}/{len(all_ids)} "
        f"active={len(active)} failed={len(unresolved)}"
    )
    return 0 if not unresolved and not active else 2


if __name__ == "__main__":
    raise SystemExit(main())
