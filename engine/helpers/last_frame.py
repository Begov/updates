"""
Extract the chain seam frame for Story/Chain mode.

For normal fallback usage this can still take a frame near the raw mp4 end via
ffmpeg -sseof. In chain mode, however, get_last_frame_path() reads
montage_plan.json and extracts the frame at the montage cut point. That keeps
the next extend clip aligned with the exact point assemble.py will later
trim to, without doing any extra video trim during generation.

Usage:
    from helpers.last_frame import get_last_frame_path
    path = get_last_frame_path(project_dir, clip_id)
"""

import json
import subprocess
from pathlib import Path
from typing import Optional, Tuple


def _run_ffprobe(args: list[str]) -> Optional[str]:
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error'] + args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            errors='replace',
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()
    except Exception:
        return None


def _probe_duration(input_mp4: Path) -> Optional[float]:
    out = _run_ffprobe([
        '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1',
        str(input_mp4),
    ])
    if not out:
        return None
    try:
        return float(out)
    except ValueError:
        return None


def _probe_fps(input_mp4: Path) -> Optional[float]:
    out = _run_ffprobe([
        '-select_streams', 'v:0',
        '-show_entries', 'stream=avg_frame_rate,r_frame_rate',
        '-of', 'json',
        str(input_mp4),
    ])
    if not out:
        return None

    try:
        data = json.loads(out)
        streams = data.get('streams') or []
        if not streams:
            return None
        stream = streams[0]

        for key in ('avg_frame_rate', 'r_frame_rate'):
            raw = stream.get(key)
            if not raw or raw == '0/0':
                continue
            if '/' in raw:
                num, den = raw.split('/', 1)
                den_f = float(den)
                if den_f == 0:
                    continue
                fps = float(num) / den_f
            else:
                fps = float(raw)
            if fps > 0:
                return fps
    except Exception:
        return None

    return None


def _read_montage_clip_duration(project_dir: Path, clip_id: int) -> Optional[float]:
    plan_path = project_dir / 'montage_plan.json'
    if not plan_path.exists():
        return None

    try:
        data = json.loads(plan_path.read_text(encoding='utf-8-sig'))
        for clip in data.get('clips', []):
            if int(clip.get('id', -1)) == int(clip_id):
                start = float(clip.get('startTime', 0))
                end = float(clip.get('endTime', 0))
                duration = end - start
                return duration if duration > 0 else None
    except Exception as e:
        print(f"[extract_last_frame] WARNING: montage_plan read failed: {e}")

    return None


def _safe_cut_frame_time(input_mp4: Path, montage_duration: float) -> Tuple[float, float]:
    """Return a source timestamp matching the last frame kept by ffmpeg -t."""
    source_duration = _probe_duration(input_mp4)
    fps = _probe_fps(input_mp4) or 24.0
    frame_step = 1.0 / fps

    cut_time = montage_duration
    if source_duration and source_duration > 0:
        cut_time = min(cut_time, source_duration)

    # video_concat trims at clip duration. The last visible frame is just before
    # that cut point, so seek one source frame earlier.
    seek_time = max(0.0, cut_time - frame_step)
    return seek_time, frame_step


def _metadata_matches(meta_path: Path, expected: dict) -> bool:
    if not meta_path.exists():
        return False
    try:
        return json.loads(meta_path.read_text(encoding='utf-8')) == expected
    except Exception:
        return False


def extract_last_frame(
    input_mp4: Path,
    output_png: Path,
    offset: float = 0.5,
    seek_time: Optional[float] = None,
    force: bool = False,
) -> bool:
    """
    Extract one PNG frame from mp4.

    If seek_time is provided, it is an absolute timestamp in the source mp4.
    Otherwise offset is interpreted as seconds before the raw mp4 end.
    """
    input_mp4 = Path(input_mp4)
    output_png = Path(output_png)

    if not input_mp4.exists() or input_mp4.stat().st_size < 1000:
        print(f"[extract_last_frame] ERROR: source mp4 missing or too small: {input_mp4}")
        return False

    if output_png.exists() and not force:
        return True

    output_png.parent.mkdir(parents=True, exist_ok=True)

    if seek_time is not None:
        cmd = [
            'ffmpeg', '-y',
            '-ss', f'{max(0.0, seek_time):.6f}',
            '-i', str(input_mp4),
            '-vframes', '1',
            '-q:v', '2',
            str(output_png),
        ]
    else:
        cmd = [
            'ffmpeg', '-y',
            '-sseof', f'-{offset}',
            '-i', str(input_mp4),
            '-vframes', '1',
            '-q:v', '2',
            str(output_png),
        ]

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            errors='replace',
        )
        if result.returncode != 0:
            print(f"[extract_last_frame] ffmpeg FAILED (exit {result.returncode}): {input_mp4.name}")
            print(result.stderr[-500:])
            return False
        if not output_png.exists() or output_png.stat().st_size < 1000:
            print(f"[extract_last_frame] ERROR: output png missing or too small: {output_png}")
            return False
        return True
    except Exception as e:
        print(f"[extract_last_frame] Exception: {e}")
        return False


def get_last_frame_path(
    project_dir: Path,
    clip_id: int,
    offset: Optional[float] = None,
) -> Optional[Path]:
    """
    Return the cached seam frame PNG for a clip.

    If montage_plan.json is available, the frame comes from the planned clip
    duration, not from the raw 8s VEO end. This keeps generation continuity and
    concat trimming aligned.
    """
    project_dir = Path(project_dir)

    if offset is None:
        try:
            import params as config
            offset = getattr(config, 'VIDEO_EXTEND_FRAME_OFFSET', 0.5)
        except Exception:
            offset = 0.5

    mp4 = project_dir / 'vid_img' / f"{clip_id:03d}.mp4"
    if not mp4.exists() or mp4.stat().st_size < 1000:
        return None

    lf_dir = project_dir / 'vid_img_lastframes'
    lf_dir.mkdir(exist_ok=True)
    lf = lf_dir / f"{clip_id:03d}.png"
    meta_path = lf.with_suffix('.json')

    montage_duration = _read_montage_clip_duration(project_dir, clip_id)
    seek_time = None
    frame_step = None
    mode = 'raw_end'
    if montage_duration is not None:
        seek_time, frame_step = _safe_cut_frame_time(mp4, montage_duration)
        mode = 'montage_cut'

    expected_meta = {
        'mode': mode,
        'source': str(mp4),
        'source_mtime': mp4.stat().st_mtime,
        'clip_id': int(clip_id),
        'fallback_offset': float(offset) if seek_time is None else None,
        'montage_duration': montage_duration,
        'seek_time': seek_time,
        'frame_step': frame_step,
    }

    if lf.exists():
        if _metadata_matches(meta_path, expected_meta):
            return lf
        try:
            lf.unlink()
        except Exception:
            pass

    ok = extract_last_frame(mp4, lf, offset=float(offset), seek_time=seek_time, force=True)
    if ok and lf.exists():
        try:
            meta_path.write_text(
                json.dumps(expected_meta, ensure_ascii=False, indent=2),
                encoding='utf-8',
            )
        except Exception as e:
            print(f"[extract_last_frame] WARNING: metadata write failed: {e}")
        return lf

    return None


