"""Проверка готовности проекта к склейке и восстановление пропусков (общая для GUI и движка).

Перед запуском склейки все клипы монтажного плана должны иметь медиа:
видео в vid_img/, картинку в generated/ или файл, указанный в плане. Фоновые
стадии (Real Photo, сток) могут дописывать план — поэтому склейка ждёт их.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Callable, Optional

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")
MEDIA_EXTS = (".mp4", ".mov", ".mkv", ".avi", ".webm") + IMAGE_EXTS


def load_clips(project: Path) -> list:
    plan_path = Path(project) / "montage_plan.json"
    if not plan_path.exists():
        raise FileNotFoundError(f"montage_plan.json not found: {plan_path}")
    data = json.loads(plan_path.read_text(encoding="utf-8-sig"))
    plan = data.get("montage_plan", data)
    return plan.get("clips", [])


def _real_photo_image(project: Path, clip_id: int) -> Optional[Path]:
    gen = project / "generated"
    for fmt in (f"{clip_id:03d}", str(clip_id)):
        for ext in IMAGE_EXTS:
            candidate = gen / f"{fmt}{ext}"
            if candidate.exists() and candidate.stat().st_size > 1000:
                return candidate
    return None


def clip_media_exists(project: Path, clip: dict) -> bool:
    clip_id = clip.get("id")
    if clip_id is None:
        return False
    vid = project / "vid_img"
    for fmt in (f"{int(clip_id):03d}", str(clip_id)):
        for ext in MEDIA_EXTS:
            candidate = vid / f"{fmt}{ext}"
            if candidate.exists() and candidate.stat().st_size > 1000:
                return True
    cf = clip.get("file")
    if cf:
        cfp = Path(cf)
        for candidate in ([cfp] if cfp.is_absolute() else [project / cfp, cfp]):
            if candidate.exists() and candidate.stat().st_size > 1000:
                return True
    return _real_photo_image(project, int(clip_id)) is not None


def readiness(project: Path) -> tuple[bool, str]:
    """(готов ли, пояснение)."""
    project = Path(project)
    clips = load_clips(project)
    if not clips:
        return False, "montage_plan has no clips"
    pending = [int(c["id"]) for c in clips if c.get("source") == "real_photo_pending" and "id" in c]
    if pending:
        return False, f"RealPhoto still pending: {pending[:12]}"
    missing_img, missing_meta, missing = [], [], []
    for clip in clips:
        cid = clip.get("id")
        if cid is None:
            continue
        cid = int(cid)
        if clip.get("source") == "real_photo":
            if _real_photo_image(project, cid) is None:
                missing_img.append(cid)
            if not clip.get("real_meta"):
                missing_meta.append(cid)
        if not clip_media_exists(project, clip):
            missing.append(cid)
    if missing_img:
        return False, f"RealPhoto images missing: {missing_img[:12]}"
    if missing_meta:
        return False, f"RealPhoto metadata missing: {missing_meta[:12]}"
    if missing:
        return False, f"Media missing for clips: {missing[:12]}"
    return True, f"ready ({len(clips)} clips)"


def static_backfill(project: Path, log: Callable[[str, str], None]) -> list[int]:
    """Закрыть оставшиеся пропуски ближайшей готовой картинкой и снять
    зависшие маркеры *_pending (конвейер завершён — их уже никто не снимет)."""
    project = Path(project)
    clips = load_clips(project)
    missing = [int(c["id"]) for c in clips if c.get("id") is not None and not clip_media_exists(project, c)]
    images = []
    for folder_name in ("generated", "vid_img"):
        folder = project / folder_name
        if not folder.is_dir():
            continue
        for candidate in folder.iterdir():
            if candidate.is_file() and candidate.suffix.lower() in IMAGE_EXTS and candidate.stem.isdigit():
                try:
                    if candidate.stat().st_size > 1000:
                        images.append((int(candidate.stem), candidate))
                except OSError:
                    continue
    recovered = []
    if missing and images:
        out = project / "generated"
        out.mkdir(parents=True, exist_ok=True)
        for clip_id in missing:
            src_id, src = min(images, key=lambda it: (abs(it[0] - clip_id), it[0] > clip_id, it[0]))
            target = out / f"{clip_id:03d}{src.suffix.lower()}"
            try:
                shutil.copy2(src, target)
            except OSError as exc:
                log(f"[ConcatFallback] #{clip_id}: {exc}", "ERROR")
                continue
            recovered.append(clip_id)
            log(f"[ConcatFallback] #{clip_id}: ближайший кадр #{src_id} / nearest still #{src_id}", "WARNING")
    plan_path = project / "montage_plan.json"
    try:
        data = json.loads(plan_path.read_text(encoding="utf-8-sig"))
        plan = data.get("montage_plan", data)
        cleared = []
        for clip in plan.get("clips", []):
            cid, src = clip.get("id"), clip.get("source")
            if cid is None or not src or src == "infographic":
                continue
            cid = int(cid)
            if (cid in recovered or (src.endswith("_pending") and clip_media_exists(project, clip))
                    or (src == "real_photo" and not clip.get("real_meta") and clip_media_exists(project, clip))):
                clip.pop("source", None)
                cleared.append(cid)
        if cleared:
            plan_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            log(f"[ConcatFallback] cleared stale markers: {sorted(cleared)[:12]}", "WARNING")
            recovered = sorted(set(recovered) | set(cleared))
    except Exception as exc:  # noqa: BLE001
        log(f"[ConcatFallback] montage_plan.json: {exc}", "ERROR")
    return recovered


def wait_ready(project: Path, log: Callable[[str, str], None],
               cancel: Optional[Callable[[], bool]] = None, terminal: bool = True,
               timeout_s: Optional[float] = None) -> str:
    """Ждать готовности проекта; в конце конвейера — один раз подставить кадры."""
    project = Path(project)
    timeout_s = timeout_s or float(os.environ.get("BEGO_CONCAT_READY_TIMEOUT", "86400"))
    start = time.time()
    last_msg, last_log = None, 0.0
    tried_backfill = False
    while True:
        if cancel and cancel():
            raise RuntimeError("cancelled while waiting for project readiness")
        ready, msg = readiness(project)
        if ready:
            if last_msg is not None:
                log(f"[ConcatGuard] {msg}", "INFO")
            return msg
        now = time.time()
        if terminal and not tried_backfill:
            tried_backfill = True
            if static_backfill(project, log):
                ready, msg = readiness(project)
                if ready:
                    log(f"[ConcatGuard] ready after fallback: {msg}", "WARNING")
                    return msg
            raise RuntimeError(f"Concat cannot start: {msg}")
        if msg != last_msg or now - last_log >= 30:
            log(f"[ConcatGuard] waiting: {msg}", "INFO")
            last_msg, last_log = msg, now
        if now - start > timeout_s:
            raise RuntimeError(f"Concat readiness timeout: {msg}")
        time.sleep(3)


def load_project_config(project: Path) -> dict:
    """Слепок настроек первого запуска проекта (pipeline_config.json)."""
    path = Path(project) / "pipeline_config.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def upsert_concat(project: Path, section: dict) -> None:
    """Записать секцию concat в pipeline_config.json проекта."""
    path = Path(project) / "pipeline_config.json"
    cfg = load_project_config(project)
    cfg["concat"] = section
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
