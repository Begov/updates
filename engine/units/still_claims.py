"""Atomic cross-provider claims for image generation."""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

IMAGE_OUTPUT_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")
DEFAULT_MIN_BYTES = 1
DEFAULT_CLAIM_TTL_SEC = 12 * 60 * 60


def _claim_ttl_sec() -> float:
    try:
        return float(
            os.getenv(
                "BEGO_IMAGE_CLAIM_TTL_SEC",
                str(DEFAULT_CLAIM_TTL_SEC),
            )
            or DEFAULT_CLAIM_TTL_SEC
        )
    except Exception:
        return float(DEFAULT_CLAIM_TTL_SEC)


def image_media_path(
    output_dir: Path,
    clip_id: int,
    min_bytes: int = DEFAULT_MIN_BYTES,
) -> Optional[Path]:
    stem = f"{int(clip_id):03d}"
    for ext in IMAGE_OUTPUT_EXTENSIONS:
        path = Path(output_dir) / f"{stem}{ext}"
        try:
            if path.is_file() and path.stat().st_size >= min_bytes:
                return path
        except OSError:
            continue
    return None


def image_media_exists(
    output_dir: Path,
    clip_id: int,
    min_bytes: int = DEFAULT_MIN_BYTES,
) -> bool:
    return image_media_path(output_dir, clip_id, min_bytes=min_bytes) is not None


def claim_path(output_dir: Path, clip_id: int) -> Path:
    return Path(output_dir) / f".{int(clip_id):03d}.imgclaim"


@dataclass
class ImageClipClaim:
    path: Path
    clip_id: int
    owner: str

    def release(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except Exception:
            pass


def acquire_image_claim(
    output_dir: Path,
    clip_id: int,
    owner: str = "",
) -> Optional[ImageClipClaim]:
    """Claim one image clip unless output exists or another worker owns it."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    if image_media_exists(out, clip_id):
        return None

    path = claim_path(out, clip_id)
    now = time.time()
    try:
        if path.exists() and now - path.stat().st_mtime > _claim_ttl_sec():
            path.unlink(missing_ok=True)
    except OSError:
        pass

    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except (FileExistsError, OSError):
        return None

    payload = {
        "clip_id": int(clip_id),
        "owner": owner or "unknown",
        "pid": os.getpid(),
        "created_at": now,
    }
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
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

    return ImageClipClaim(
        path=path,
        clip_id=int(clip_id),
        owner=owner or "unknown",
    )
