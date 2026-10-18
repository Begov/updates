"""
footage_cut.py — подготовка скачанного видео-футажа под клип монтажного плана.

Используется стоковой стадией (Pexels/Pixabay/Coverr) и пригодится, когда вернём
видео-путь Real Photo — задача одна: из длинного чужого ролика получить фрагмент,
который video_concat примет как родной.

Что делает:
  сегмент [start, start+clip_duration] → scale к целевому разрешению → fps → (-an) → vid_img/{id:03d}.mp4

Почему длительность ОБЯЗАНА совпадать с клипом
----------------------------------------------
video_concat.process_video ретаймит фрагмент под `target_duration` из montage_plan:
  - target <= TRIM_MAX_DURATION (5.9с) и source длиннее → trim mode, обрежет хвост (ОК);
  - иначе → speed_factor = current/target и **ускорит видео**.
Клипы бывают до VIDEO_CLIP_MAX_DURATION=8с, т.е. 20-секундный футаж на 8-секундном клипе
разогнался бы в 2.5 раза. Поэтому режем ровно под клип: current == target → speed_factor 1.0.

Почему start/jitter здесь, а не в concat
----------------------------------------
concat умеет только «обрезать с начала». Первые секунды стоковых роликов — самые
постановочные, и у похожих футажей они одинаковые. Старт + джиттер задаются здесь.
"""
from __future__ import annotations

import json
import logging
import random
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Windows: не показывать окно консоли на каждый вызов ffmpeg
_SUBPROCESS_FLAGS: Dict[str, Any] = {}
try:
    import os
    if os.name == 'nt':
        _SUBPROCESS_FLAGS['creationflags'] = subprocess.CREATE_NO_WINDOW
except Exception:
    pass


def _ffmpeg() -> str:
    return shutil.which('ffmpeg') or 'ffmpeg'


def _ffprobe() -> str:
    return shutil.which('ffprobe') or 'ffprobe'


def probe_video(path: str | Path) -> Optional[Dict[str, Any]]:
    """Длительность/размер/fps/наличие звука. None — если файл битый или не видео."""
    path = Path(path)
    if not path.exists() or path.stat().st_size < 1024:
        return None
    cmd = [
        _ffprobe(), '-v', 'error',
        '-show_entries', 'format=duration',
        '-show_entries', 'stream=width,height,codec_type,avg_frame_rate',
        '-of', 'json', str(path),
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=60, **_SUBPROCESS_FLAGS)
        if p.returncode != 0:
            return None
        data = json.loads(p.stdout or '{}')
    except Exception as exc:
        logger.warning("ffprobe failed for %s: %s", path, exc)
        return None

    duration = 0.0
    try:
        duration = float((data.get('format') or {}).get('duration') or 0)
    except (TypeError, ValueError):
        duration = 0.0

    width = height = 0
    fps = 0.0
    has_audio = False
    for st in data.get('streams') or []:
        if st.get('codec_type') == 'audio':
            has_audio = True
        elif st.get('codec_type') == 'video' and not width:
            width = int(st.get('width') or 0)
            height = int(st.get('height') or 0)
            rate = str(st.get('avg_frame_rate') or '0/0')
            if '/' in rate:
                num, _, den = rate.partition('/')
                try:
                    fps = float(num) / float(den) if float(den) else 0.0
                except (TypeError, ValueError, ZeroDivisionError):
                    fps = 0.0
    if duration <= 0 or width <= 0 or height <= 0:
        return None
    return {'duration': duration, 'width': width, 'height': height,
            'fps': fps, 'has_audio': has_audio}


def pick_start(
    footage_duration: float,
    clip_duration: float,
    *,
    start_sec: float = 0.0,
    jitter: bool = False,
    jitter_sec: float = 0.0,
    rnd: Optional[random.Random] = None,
    clip_id: Optional[int] = None,
) -> float:
    """С какой секунды резать футаж.

    Считаем ОКНО [lo, hi] и берём точку внутри него. Наивный clamp(start + jitter)
    прижимает значения к краям: при запасе 1с и джиттере 3с ~84% стартов залипают
    ровно на 0 или на конце — джиттер делает обратное тому, зачем нужен.
    """
    rnd = rnd or random
    usable = footage_duration - clip_duration
    if usable <= 0:
        # Штатная ситуация: футаж ровно под клип (или короче — такие отбраковываются
        # при ранжировании). Берём целиком с начала. info, не warning — ничего не сломано.
        if start_sec or jitter:
            logger.info(
                "клип %s: футаж %.1fс под клип %.1fс — взят целиком с 0.0с "
                "(для старта/джиттера нет запаса)",
                clip_id if clip_id is not None else '?', footage_duration, clip_duration,
            )
        return 0.0

    start = min(max(0.0, start_sec), usable)
    if jitter and jitter_sec > 0:
        lo = max(0.0, start - jitter_sec)
        hi = min(usable, start + jitter_sec)
        start = rnd.uniform(lo, hi) if hi > lo else lo
    return start


def _scale_filter(target_w: int, target_h: int, fps: int) -> str:
    """Заполнить целевой кадр: увеличить по большей стороне и обрезать лишнее.

    increase+crop, а не decrease+pad — чёрные поля в середине ролика выглядят как брак.
    Сильно вертикальный футаж потеряет края, поэтому при поиске просим landscape.
    """
    return (
        f'scale={target_w}:{target_h}:force_original_aspect_ratio=increase:flags=lanczos,'
        f'crop={target_w}:{target_h},'
        f'fps={fps},setsar=1,format=yuv420p'
    )


def cut_footage(
    src: str | Path,
    dst: str | Path,
    clip_duration: float,
    *,
    target_w: int = 1920,
    target_h: int = 1080,
    fps: int = 60,
    start_sec: float = 0.0,
    jitter: bool = False,
    jitter_sec: float = 0.0,
    mute: bool = True,
    crf: int = 18,
    preset: str = 'fast',
    timeout: int = 600,
    stop_event: Optional[threading.Event] = None,
    rnd: Optional[random.Random] = None,
    clip_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Вырезать фрагмент под клип. Возвращает {'ok', 'start', 'duration', 'error', ...}.

    mute=True (дефолт) — вырезать звуковую дорожку. Звук футажа это:
      1) музыка/шум поверх голоса нарации;
      2) главный канал Content ID-клеймов (видеоряд стока почти не регистрируют,
         а трек внутри ролика — запросто).
    Примечание: video_concat по умолчанию тоже стрипает аудио (keep_audio=False),
    так что mute=False оставит звук только в файле vid_img/, а в финал он попадёт
    лишь в script-mode. Здесь режем на уровне файла — двойная защита.

    stop_event — прерывание (стоп-кнопка пайплайна): процесс ffmpeg убивается.
    """
    src, dst = Path(src), Path(dst)
    info = probe_video(src)
    if not info:
        return {'ok': False, 'error': f'not a valid video: {src.name}'}

    if clip_duration <= 0:
        return {'ok': False, 'error': f'bad clip_duration: {clip_duration}'}

    start = pick_start(
        info['duration'], clip_duration,
        start_sec=start_sec, jitter=jitter, jitter_sec=jitter_sec,
        rnd=rnd, clip_id=clip_id,
    )
    # Не просить у ffmpeg больше, чем есть: иначе на выходе фрагмент короче target,
    # и concat начнёт его замедлять.
    take = min(clip_duration, max(0.0, info['duration'] - start))
    if take <= 0:
        return {'ok': False, 'error': 'nothing to cut (start beyond duration)'}

    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f'{dst.stem}__cut.tmp.mp4')

    cmd = [
        _ffmpeg(), '-y', '-hide_banner', '-loglevel', 'error',
        # -ss ДО -i: быстрый seek; при перекодировании ffmpeg делает его точным
        '-ss', f'{start:.3f}',
        '-i', str(src),
        '-t', f'{take:.3f}',
        '-vf', _scale_filter(target_w, target_h, fps),
        '-c:v', 'libx264', '-crf', str(crf), '-preset', preset,
        '-pix_fmt', 'yuv420p',
        '-movflags', '+faststart',
    ]
    cmd += ['-an'] if mute else ['-c:a', 'aac', '-b:a', '128k']
    cmd += [str(tmp)]

    proc = None
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding='utf-8', errors='replace',
                                **_SUBPROCESS_FLAGS)
        waited = 0.0
        step = 0.5
        while True:
            try:
                _out, err = proc.communicate(timeout=step)
                break
            except subprocess.TimeoutExpired:
                waited += step
                if stop_event is not None and stop_event.is_set():
                    proc.kill()
                    proc.communicate()
                    if tmp.exists():
                        tmp.unlink(missing_ok=True)
                    return {'ok': False, 'error': 'interrupted', 'interrupted': True}
                if waited >= timeout:
                    proc.kill()
                    proc.communicate()
                    if tmp.exists():
                        tmp.unlink(missing_ok=True)
                    return {'ok': False, 'error': f'ffmpeg timeout after {timeout}s'}
        if proc.returncode != 0:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            return {'ok': False, 'error': (err or '').strip()[-400:] or 'ffmpeg failed'}
    except Exception as exc:
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        return {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}

    out_info = probe_video(tmp)
    if not out_info:
        tmp.unlink(missing_ok=True)
        return {'ok': False, 'error': 'output is not a valid video'}

    if dst.exists():
        dst.unlink()
    tmp.replace(dst)

    return {
        'ok': True,
        'file': str(dst),
        'start': round(start, 3),
        'duration': round(out_info['duration'], 3),
        'requested': round(take, 3),
        'width': out_info['width'],
        'height': out_info['height'],
        'fps': round(out_info['fps'], 2),
        'has_audio': out_info['has_audio'],
        'source_duration': round(info['duration'], 3),
        'source_size': f"{info['width']}x{info['height']}",
    }
