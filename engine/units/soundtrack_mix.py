"""
Music Mix — накладывает фоновую музыку на готовый MP4 согласно music_plan.json.

Делает ровно три вещи в FFmpeg:
1. Собирает один длинный музыкальный stream из всех треков с per-track volume,
   fade-in/out и crossfade между соседями.
2. Применяет sidechain ducking: голос (из исходного видео) автоматически
   приглушает музыку — speech compressor с music как target, voice как trigger.
3. Mix(voice, ducked_music) → новая аудиодорожка → mux обратно в MP4 без перекодирования видео.

Использование:
    from units.soundtrack_mix import apply_music_to_video
    apply_music_to_video(
        input_video='project.mp4',
        music_plan_path='music_plan.json',
        music_dir='music/',
        output_video='project_with_music.mp4',
    )

Можно из CLI:
    python -m units.soundtrack_mix INPUT.mp4 music_plan.json music/ OUTPUT.mp4
"""
from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import List, Dict, Any, Optional

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / '.env')
except Exception:
    pass

for _stream in (getattr(sys, 'stdout', None), getattr(sys, 'stderr', None)):
    if hasattr(_stream, 'reconfigure'):
        try:
            _stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass


# =============================================================================
# Defaults
# =============================================================================

def _coerce_db(value: Any, default: float) -> float:
    """Normalize dB values; positive UI input like 16 means -16 dB."""
    try:
        db = float(value)
    except (TypeError, ValueError):
        db = float(default)
    if db > 0:
        db = -db
    return max(-60.0, min(0.0, db))


DEFAULT_GLOBAL_VOLUME_DB = _coerce_db(os.getenv('MUSIC_DEFAULT_VOLUME_DB', '-21'), -21.0)
DEFAULT_MUSIC_CEILING_DB = _coerce_db(os.getenv('MUSIC_MIX_MAX_VOLUME_DB', '0'), 0.0)

# На сколько dB музыка душится под голосом (relative).
# Слабый ducking: музыка остается ровнее, без сильного прижатия под каждую фразу.
DEFAULT_DUCKING_DB = _coerce_db(os.getenv('MUSIC_DUCKING_DB', '-5'), -5.0)

# Параметры sidechain compressor:
#   threshold = -25 dB (меньше ложных срабатываний на тихие хвосты голоса)
#   ratio     = зависит от MUSIC_DUCKING_DB, либо MUSIC_SIDECHAIN_RATIO
#   attack    = 25 ms (мягче начало срабатывания)
#   release   = 800ms (ровный возврат без сильного удержания)
DEFAULT_THRESHOLD_DB = float(os.getenv('MUSIC_SIDECHAIN_THRESHOLD_DB', '-25'))
DEFAULT_RATIO = float(os.getenv('MUSIC_SIDECHAIN_RATIO', '0') or '0')
DEFAULT_ATTACK_MS = int(os.getenv('MUSIC_SIDECHAIN_ATTACK_MS', '25'))
DEFAULT_RELEASE_MS = int(os.getenv('MUSIC_SIDECHAIN_RELEASE_MS', '800'))

# Между двумя соседними треками — кроссфейд этой длительности
DEFAULT_CROSSFADE_MS = 2000


# =============================================================================
# Public API
# =============================================================================

def apply_music_to_video(
    input_video: str | Path,
    music_plan_path: str | Path,
    music_dir: str | Path,
    output_video: str | Path,
    ffmpeg_path: str = 'ffmpeg',
    encoder: str = 'libx264',  # passthrough video, this is for fallback
    audio_bitrate: str = '192k',
    overwrite: bool = True,
) -> Path:
    """
    Накладывает музыку на input_video по плану и пишет в output_video.

    Returns: Path(output_video).
    """
    input_video = Path(input_video)
    output_video = Path(output_video)
    music_dir = Path(music_dir)

    if not input_video.exists():
        raise FileNotFoundError(f"input_video не найден: {input_video}")

    plan = _load_plan(Path(music_plan_path))
    tracks = [t for t in plan.get('tracks', []) if t.get('genre') != 'silence']
    tracks.sort(key=lambda t: float(t.get('startTime', 0)))

    if not tracks:
        logging.info("Music Mix: треков нет (или все silence) — копируем video без изменений")
        if input_video.resolve() != output_video.resolve():
            import shutil
            shutil.copy2(input_video, output_video)
        return output_video

    requested_global_volume_db = _coerce_db(plan.get('global_volume_db', DEFAULT_GLOBAL_VOLUME_DB), DEFAULT_GLOBAL_VOLUME_DB)
    global_volume_db = min(requested_global_volume_db, DEFAULT_MUSIC_CEILING_DB)
    ducking_db = _coerce_db(plan.get('global_ducking_db', DEFAULT_DUCKING_DB), DEFAULT_DUCKING_DB)

    # Существование файлов треков
    valid_tracks = []
    for t in tracks:
        fname = t.get('file') or f"{t.get('id')}.mp3"
        track_path = music_dir / fname
        if not track_path.exists():
            logging.warning(f"Music Mix: трек {t.get('id')} {track_path} не найден — skip")
            continue
        t['_path'] = str(track_path)
        valid_tracks.append(t)

    if not valid_tracks:
        raise FileNotFoundError(f"Ни один трек не найден в {music_dir}")

    print(f"\n🎼 Music Mix: {len(valid_tracks)} треков → {output_video.name}")
    print(f"   Global volume: {global_volume_db} dB")
    if global_volume_db < requested_global_volume_db:
        print(f"   Music ceiling: capped from {requested_global_volume_db} dB")
    if abs(ducking_db) < 0.5:
        print(f"   Ducking:       ВЫКЛ (музыка ровная, без приглушения под голос)")
    else:
        print(f"   Ducking:       {ducking_db} dB (компрессор активируется голосом)")
        print(f"   Duck release:  {DEFAULT_RELEASE_MS} ms (smoother return)")
    for t in valid_tracks:
        planned_dur = float(t.get('endTime', 0)) - float(t.get('startTime', 0))
        if t.get('suno_clip_ids'):
            source_label = 'Suno'
        elif t.get('fallback'):
            source_label = f"fallback:{t.get('fallback')}"
        elif t.get('source'):
            source_label = str(t.get('source'))
        else:
            source_label = 'audio file'
        try:
            selected_variant = int(t.get('selected_variant', 1))
        except (TypeError, ValueError):
            selected_variant = 1
        durations = t.get('suno_durations') or []
        suno_dur = 0.0
        if len(durations) >= selected_variant:
            try:
                suno_dur = float(durations[selected_variant - 1])
            except (TypeError, ValueError):
                suno_dur = 0.0
        if suno_dur > 0:
            if suno_dur < planned_dur - 0.25:
                fit_mode = 'loop'
            elif suno_dur > planned_dur + 0.25:
                fit_mode = 'trim'
            else:
                fit_mode = 'as-is'
            print(
                f"   Track {t.get('id')}: {source_label} {suno_dur:.1f}s → "
                f"slot {planned_dur:.1f}s ({fit_mode})"
            )
        else:
            print(
                f"   Track {t.get('id')}: {source_label} → "
                f"slot {planned_dur:.1f}s (loop/trim as needed)"
            )

    # Build ffmpeg command
    cmd = _build_ffmpeg_cmd(
        ffmpeg_path=ffmpeg_path,
        input_video=input_video,
        tracks=valid_tracks,
        output_video=output_video,
        global_volume_db=global_volume_db,
        ducking_db=ducking_db,
        audio_bitrate=audio_bitrate,
        overwrite=overwrite,
    )

    logging.info(f"Music Mix ffmpeg: {' '.join(shlex.quote(c) for c in cmd[:6])} ... ({len(cmd)} args)")
    print(f"   ⏳ FFmpeg mix...")

    proc = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if proc.returncode != 0:
        # Show last 30 lines of stderr
        err_tail = '\n'.join(proc.stderr.strip().split('\n')[-30:])
        raise RuntimeError(f"FFmpeg failed (rc={proc.returncode}):\n{err_tail}")

    if not output_video.exists():
        raise RuntimeError(f"FFmpeg не создал {output_video}")

    print(f"   ✅ Готово: {output_video} ({output_video.stat().st_size / 1024 / 1024:.1f} MB)")
    return output_video


# =============================================================================
# FFmpeg command builder
# =============================================================================

def _build_ffmpeg_cmd(
    ffmpeg_path: str,
    input_video: Path,
    tracks: List[Dict[str, Any]],
    output_video: Path,
    global_volume_db: float,
    ducking_db: float,
    audio_bitrate: str,
    overwrite: bool,
) -> List[str]:
    """Строит ffmpeg команду с complex filter graph."""

    # ---- Inputs ----
    cmd = [ffmpeg_path, '-hide_banner', '-loglevel', 'warning']
    if overwrite:
        cmd.append('-y')

    cmd.extend(['-i', str(input_video)])  # [0:v] [0:a]
    for t in tracks:
        # Suno decides the real clip length. Loop every music input, then
        # trim it to the planned slot below: long clips are cut, short clips
        # keep playing instead of leaving silence at the end of the segment.
        cmd.extend(['-stream_loop', '-1', '-i', t['_path']])    # [1:a], [2:a], ...

    # ---- Filter graph ----
    filter_parts: List[str] = []

    # Per-track preprocessing: trim/pad to start/end, volume, fade
    track_labels = []
    for i, t in enumerate(tracks, start=1):
        st = float(t.get('startTime', 0))
        en = float(t.get('endTime', 0))
        dur = en - st
        if dur <= 0:
            continue

        vol_db = min(_coerce_db(t.get('volume_db', global_volume_db), global_volume_db), DEFAULT_MUSIC_CEILING_DB)
        fi_ms = float(t.get('fade_in_ms', 1500))
        fo_ms = float(t.get('fade_out_ms', 2500))

        # Шаги:
        # 1. atrim from a looped input: longer Suno clips are cut, shorter clips
        #    are extended by looping before fade/volume/placement.
        # 3. afade in / out
        # 4. volume
        # 5. adelay = startTime в мс (чтобы трек начинался в правильное время на таймлайне)
        steps = []
        silence_threshold = '-45dB' if t.get('intro_dynamic') else '-50dB'
        silence_start = 0.03 if t.get('intro_dynamic') else 0.05
        steps.append(
            'silenceremove='
            f'start_periods=1:start_duration={silence_start:.2f}:'
            f'start_threshold={silence_threshold}'
        )
        steps.append(f"atrim=duration={dur:.3f}")
        steps.append("asetpts=PTS-STARTPTS")
        if fi_ms > 0:
            steps.append(f"afade=t=in:st=0:d={fi_ms/1000:.3f}")
        if fo_ms > 0:
            steps.append(
                f"afade=t=out:st={max(0, dur - fo_ms/1000):.3f}:d={fo_ms/1000:.3f}"
            )
        steps.append(f"volume={_db_to_factor(vol_db):.4f}")
        # adelay требует значение в мс на каждый канал; используем =all=1
        delay_ms = int(st * 1000)
        if delay_ms > 0:
            steps.append(f"adelay=delays={delay_ms}|{delay_ms}:all=1")

        label = f"m{i}"
        filter_parts.append(f"[{i}:a]" + ",".join(steps) + f"[{label}]")
        track_labels.append(label)

    # ---- Mix all music tracks into single stream ----
    if len(track_labels) == 1:
        # Только один трек
        music_label = track_labels[0]
    else:
        # amix всех с одинаковым весом, longest=true → результат не обрезается
        amix_inputs = "".join(f"[{l}]" for l in track_labels)
        filter_parts.append(
            f"{amix_inputs}amix=inputs={len(track_labels)}:duration=longest:dropout_transition=0:normalize=0[music_raw]"
        )
        music_label = "music_raw"

    # ---- Sidechain compressor: voice triggers music ducking ----
    if abs(ducking_db) < 0.5:
        # Ducking ОТКЛЮЧЁН (MUSIC_DUCKING_DB=0): музыка играет ровно, простой mix с голосом,
        # без приглушения под нарацию.
        filter_parts.append(
            f"[0:a][{music_label}]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
        )
    else:
        # Используем asplit чтобы клонировать голос — один клон в sidechain, второй — в финальный mix
        filter_parts.append("[0:a]asplit=2[voice_a][voice_b]")
        # sidechaincompress: source=music, sidechain=voice. Slow release keeps the bed tucked.
        threshold_lin = _db_to_factor(DEFAULT_THRESHOLD_DB)
        duck_ratio = DEFAULT_RATIO if DEFAULT_RATIO > 0 else 1.0 + max(1.0, abs(ducking_db) * 0.5)
        duck_ratio = max(2.0, min(20.0, duck_ratio))
        filter_parts.append(
            f"[{music_label}][voice_a]sidechaincompress="
            f"threshold={threshold_lin:.4f}:"
            f"ratio={duck_ratio:.2f}:"
            f"attack={DEFAULT_ATTACK_MS}:"
            f"release={DEFAULT_RELEASE_MS}:"
            f"makeup=1[music_ducked]"
        )
        # Final mix: voice + ducked music.
        filter_parts.append("[voice_b][music_ducked]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]")

    filter_complex = ";".join(filter_parts)

    cmd.extend(['-filter_complex', filter_complex])
    cmd.extend(['-map', '0:v'])  # passthrough video
    cmd.extend(['-map', '[aout]'])
    cmd.extend(['-c:v', 'copy'])  # video не перекодируем
    cmd.extend(['-c:a', 'aac', '-b:a', audio_bitrate, '-ar', '48000'])
    cmd.extend(['-shortest'])
    cmd.append(str(output_video))

    return cmd


# =============================================================================
# Helpers
# =============================================================================

def _load_plan(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"music_plan.json не найден: {path}")
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    if 'music_plan' in data and 'tracks' in data['music_plan']:
        return data['music_plan']
    return data


def _db_to_factor(db: float) -> float:
    """dB → linear amplitude factor."""
    if db <= -60:
        return 0.0
    return 10 ** (db / 20)


# =============================================================================
# CLI
# =============================================================================

def main():
    import argparse
    ap = argparse.ArgumentParser(description="Накладывает music_plan на готовый MP4")
    ap.add_argument('input_video')
    ap.add_argument('music_plan')
    ap.add_argument('music_dir')
    ap.add_argument('output_video')
    ap.add_argument('--ffmpeg', default='ffmpeg')
    args = ap.parse_args()

    apply_music_to_video(
        input_video=args.input_video,
        music_plan_path=args.music_plan,
        music_dir=args.music_dir,
        output_video=args.output_video,
        ffmpeg_path=args.ffmpeg,
    )


if __name__ == '__main__':
    main()
