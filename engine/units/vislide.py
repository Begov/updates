"""
vislide.py — режим «Вислайд»: какие сцены оживляются, а какие остаются слайдами.

Оживляется каждый N-й клип и все клипы вступления (интро-блок), остальные идут
картинкой с Ken Burns. Счёт ведётся только по клипам, которым вообще нужно видео:
сток, реальные фото и инфографика (VIDEO_SKIP_SOURCES) приходят готовыми и в
нумерацию не входят — иначе «каждый третий» мог попасть на сток и оживлённых
сцен выходило меньше обещанного.

Раньше это правило было переписано в каждом месте отдельно (финальная генерация,
вотчеры VeoNonStop / CinAI, «Довести проект»), и пути CinAI его не
применяли вовсе — при видео через CinAI оживлялись все сцены.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

IMAGE_EXTS = ('.png', '.jpg', '.jpeg', '.webp')
VIDEO_EXTS = ('.mp4', '.mov')
# режимы видео, которые оживляют готовую картинку; t2v картинок не ждёт,
# и слайдам там взяться неоткуда
IMAGE_VIDEO_MODES = ('i2v', 'first_last', 'veo_i2v', 'keyframes')


def interval_from_env() -> int:
    """Интервал вислайда из окружения (BEGO_VISLIDE_INTERVAL); 0 — режим выключен."""
    try:
        value = int(os.environ.get('BEGO_VISLIDE_INTERVAL', '0') or 0)
    except ValueError:
        return 0
    return value if value >= 2 else 0


def load_clips(project_dir) -> list:
    """Клипы монтажного плана (пустой список, если плана нет или он битый)."""
    try:
        data = json.loads((Path(project_dir) / 'montage_plan.json').read_text(encoding='utf-8-sig'))
        clips = data.get('montage_plan', data).get('clips', [])
        return clips if isinstance(clips, list) else []
    except (OSError, ValueError, AttributeError):
        return []


def _skip_sources() -> frozenset:
    from units.source_ledger import VIDEO_SKIP_SOURCES
    return frozenset(VIDEO_SKIP_SOURCES)


def animated_ids(clips: Sequence[dict], interval: int, intro_dur: float = 0) -> List[int]:
    """Клипы, которые оживляются: каждый N-й (счёт с 1) + все клипы интро-блока."""
    skip = _skip_sources()
    video_clips = [c for c in clips if c.get('id') is not None and c.get('source') not in skip]
    if interval < 2:
        return sorted(int(c['id']) for c in video_clips)
    animated = {int(c['id']) for i, c in enumerate(video_clips, start=1) if i % interval == 0}
    if intro_dur and intro_dur > 0:
        animated |= {int(c['id']) for c in video_clips
                     if float(c.get('startTime', 0) or 0) < float(intro_dur)}
    return sorted(animated)


def intro_duration() -> float:
    try:
        import params as config
        return float(getattr(config, 'INTRO_BLOCK_DURATION', 0) or 0)
    except Exception:                                              # noqa: BLE001
        return 0.0


def project_animated_ids(project_dir, interval: Optional[int] = None) -> Optional[List[int]]:
    """Оживляемые клипы проекта; None — вислайд выключен (оживлять всё)."""
    interval = interval_from_env() if interval is None else int(interval or 0)
    if interval < 2:
        return None
    return animated_ids(load_clips(project_dir), interval, intro_duration())


def copy_static(project_dir, animated: Iterable[int], output_dir_name: str = 'vid_img') -> int:
    """Картинки неоживляемых клипов generated/ → vid_img/ (слайды для склейки).

    Склейка нашла бы картинку и в generated/, но «Довести проект» и проверки
    перед склейкой считают сцену готовой только по vid_img/ — без копии они
    приняли бы слайды за недоделанное видео и запустили генерацию снова."""
    project = Path(project_dir)
    keep = {int(cid) for cid in animated}
    skip = _skip_sources()
    gen_dir = project / 'generated'
    out_dir = project / output_dir_name
    copied = 0
    for clip in load_clips(project):
        if clip.get('id') is None or clip.get('source') in skip:
            continue
        cid = int(clip['id'])
        if cid in keep:
            continue
        stem = f"{cid:03d}"
        if any((out_dir / f"{stem}{ext}").exists() for ext in VIDEO_EXTS + IMAGE_EXTS):
            continue
        src = next((gen_dir / f"{stem}{ext}" for ext in IMAGE_EXTS if (gen_dir / f"{stem}{ext}").exists()), None)
        if src is None:
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(str(src), str(out_dir / src.name))
        copied += 1
    return copied
