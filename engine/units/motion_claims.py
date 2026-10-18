"""
video_claims.py - cross-process clip output guards for VideoGen.

The guard prevents two independently launched video engines from submitting the
same clip at the same time. A finished media file wins; otherwise a process
must atomically create vid_img/.NNN.claim before submitting clip NNN.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

VIDEO_OUTPUT_EXTENSIONS = ('.mp4', '.mov', '.mkv', '.webm', '.png', '.jpg', '.jpeg', '.webp')
DEFAULT_MIN_BYTES = 1000
DEFAULT_CLAIM_TTL_SEC = 12 * 60 * 60


def _claim_ttl_sec() -> float:
    try:
        return float(os.getenv('BEGO_VIDEO_CLAIM_TTL_SEC', str(DEFAULT_CLAIM_TTL_SEC)) or DEFAULT_CLAIM_TTL_SEC)
    except Exception:
        return float(DEFAULT_CLAIM_TTL_SEC)


def clip_media_path(output_dir: Path, clip_id: int, min_bytes: int = DEFAULT_MIN_BYTES) -> Optional[Path]:
    """Return an existing final media file for clip_id in vid_img, video or static image.

    Сцена может лежать не одним файлом, а НЕСКОЛЬКИМИ кусками — `001_1.mp4`,
    `001_2.mp4` рядом в `vid_img/`
    (стадия YouTube отдаёт скачанные отрезки как есть, а склейка приводит их к
    формату один раз — см. `_process_clip_parts` в assemble.py). Такая сцена
    тоже «готова»: без этой ветки генерация видела пустое место и рисовала клип
    поверх уже скачанного материала, а склейка уводила его в фолбэк.

    Возвращается ПЕРВЫЙ кусок — вызывающему коду нужен факт наличия материала и
    его формат; полный список кусков лежит в монтажном плане (`parts`).
    """
    stem = f"{int(clip_id):03d}"
    for ext in VIDEO_OUTPUT_EXTENSIONS:
        p = Path(output_dir) / f"{stem}{ext}"
        try:
            if p.exists() and p.is_file() and p.stat().st_size > min_bytes:
                return p
        except OSError:
            continue
    parts = clip_parts_paths(output_dir, clip_id, min_bytes=min_bytes)
    return parts[0] if parts else None


def clip_parts_paths(output_dir: Path, clip_id: int,
                     min_bytes: int = DEFAULT_MIN_BYTES) -> list:
    """Куски одной сцены (`001_1.mp4`, `001_2.mp4`), по порядку. Пусто — сцена цельная."""
    parts_dir = Path(output_dir)
    if not parts_dir.is_dir():
        return []
    stem = f"{int(clip_id):03d}"
    found = []
    for ext in VIDEO_OUTPUT_EXTENSIONS:
        found.extend(parts_dir.glob(f"{stem}_*{ext}"))

    def _order(path: Path) -> int:
        tail = path.stem.rsplit("_", 1)[-1]
        return int(tail) if tail.isdigit() else 0

    usable = []
    for item in sorted(found, key=_order):
        try:
            if item.is_file() and item.stat().st_size > min_bytes:
                usable.append(item)
        except OSError:      # файл исчез между glob и stat — просто пропускаем
            continue
    return usable


def clip_media_exists(output_dir: Path, clip_id: int, min_bytes: int = DEFAULT_MIN_BYTES) -> bool:
    return clip_media_path(output_dir, clip_id, min_bytes=min_bytes) is not None


def claim_path(output_dir: Path, clip_id: int) -> Path:
    return Path(output_dir) / f".{int(clip_id):03d}.claim"


@dataclass
class VideoClipClaim:
    path: Path
    clip_id: int
    owner: str

    def release(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except Exception:
            pass


def acquire_clip_claim(output_dir: Path, clip_id: int, owner: str = '') -> Optional[VideoClipClaim]:
    """
    Atomically claim clip_id for generation.

    Returns None when final media already exists or another live process owns the
    claim. Stale claims are removed after BEGO_VIDEO_CLAIM_TTL_SEC.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    if clip_media_exists(out, clip_id):
        return None

    path = claim_path(out, clip_id)
    now = time.time()
    try:
        if path.exists() and now - path.stat().st_mtime > _claim_ttl_sec():
            path.unlink(missing_ok=True)
    except OSError:
        pass

    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(str(path), flags)
    except FileExistsError:
        return None
    except OSError:
        return None

    payload = {
        'clip_id': int(clip_id),
        'owner': owner or 'unknown',
        'pid': os.getpid(),
        'created_at': now,
    }
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False)
    except Exception:
        try:
            os.close(fd)
        except Exception:
            pass
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
        return None

    return VideoClipClaim(path=path, clip_id=int(clip_id), owner=owner or 'unknown')

