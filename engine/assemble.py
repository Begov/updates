#!/usr/bin/env python3
"""
ГРУППОВАЯ СКЛЕЙКА - v6 OVERLAP GUARD
• Валидация монтажного плана
• Сортировка клипов по startTime (защита от неверного порядка клипов в JSON режиссёра)
• Анализ и заполнение всех гэпов
• Обнаружение и коррекция перекрытий (внахлёст)
• Проверка существующих файлов
• Групповая склейка без перекодирования
• Автоисправление финальной синхронизации (быстрое обрезание)
• GPU ускорение
• Логгирование всех операций
• Запрос перед удалением temp папки
"""

import subprocess
import json
import sys
import os
import glob as _glob
import argparse
import random
from pathlib import Path
from tqdm import tqdm
import platform
import time
import shutil
import hashlib
import logging
from datetime import datetime, timedelta
import traceback

IS_PIPE = not sys.stdout.isatty()  # True когда запущен как subprocess (pipeline mode)

if platform.system() == 'Windows':
    SUBPROCESS_FLAGS = {'creationflags': 0x08000000}
    # Принудительно UTF-8 для stdout/stderr — иначе на английских Windows (cp1252)
    # Кириллица в print() вызывает UnicodeEncodeError
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
else:
    SUBPROCESS_FLAGS = {}

# ============================================================================
# FFMPEG RESOLVER — ищет ffmpeg/ffprobe в PATH и типичных местах установки
# (на случай если PATH не обновился после winget/choco install)
# ============================================================================
def _resolve_ff(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    if platform.system() == 'Windows':
        exe = name + '.exe'
        # winget (Gyan.FFmpeg) — самый частый способ установки
        wg = os.path.expandvars('%LOCALAPPDATA%\\Microsoft\\WinGet\\Packages')
        if os.path.isdir(wg):
            import glob as _g2
            for candidate in _g2.glob(
                    os.path.join(wg, 'Gyan.FFmpeg_*', '**', exe), recursive=True):
                if os.path.isfile(candidate):
                    return candidate
        # Chocolatey
        choco = os.path.join(os.environ.get('ProgramData', 'C:\\ProgramData'),
                             'chocolatey', 'lib', 'ffmpeg', 'tools', 'ffmpeg', 'bin', exe)
        if os.path.isfile(choco):
            return choco
        # Ручная установка
        for d in [r'C:\ffmpeg\bin', r'C:\Program Files\ffmpeg\bin',
                  r'C:\Program Files (x86)\ffmpeg\bin']:
            p = os.path.join(d, exe)
            if os.path.isfile(p):
                return p
    return name  # надеемся что PATH уже содержит бинарник

FFMPEG  = _resolve_ff('ffmpeg')
FFPROBE = _resolve_ff('ffprobe')

# Клипы с target_duration <= этого порога обрезаются (trim) вместо ускорения через setpts.
# Ускорение коротких клипов до 2x и выше даёт неестественное видео; обрезка выглядит лучше.
TRIM_MAX_DURATION = 5.9
# Ускорять клип сильнее нельзя: ИИ-видео на 1.5x выглядит суетливым и дёрганым.
# Бесплатные модели CinAI в i2v всегда отдают 10 с, а сцена бывает 6 с — раньше такой
# клип ускорялся в 1.7 раза. Теперь ускорение не больше этого, остаток хвоста обрезается.
MAX_SPEEDUP = 1.25

# ============================================================================
# ============================================================================
# SUBTITLE HELPERS
# ============================================================================

def _srt_to_ass(srt_path: str, template_path: str, output_path: str):
    """Конвертирует SRT в ASS, применяя стили из ASS-шаблона."""
    import re as _re

    # Читаем шаблон — берём всё до [Events]
    tpl = open(template_path, encoding='utf-8-sig').read()
    if '[Events]' in tpl:
        header = tpl[:tpl.index('[Events]')]
    else:
        header = tpl.rstrip() + '\n'

    # Парсим SRT
    srt_text = open(srt_path, encoding='utf-8-sig').read()
    blocks = [b.strip() for b in _re.split(r'\n{2,}', srt_text) if b.strip()]

    def _tc(s):
        # 00:00:01,500 → 0:00:01.50
        h, m, rest = s.split(':')
        sec, ms = _re.split(r'[,.]', rest)
        return f"{int(h)}:{m}:{sec}.{ms[:2]}"

    events = ['[Events]',
              'Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text']
    for b in blocks:
        lines = b.splitlines()
        tc_line = next((l for l in lines if '-->' in l), None)
        if not tc_line:
            continue
        ti = lines.index(tc_line)
        t0, t1 = [s.strip() for s in tc_line.split('-->')]
        text = ' '.join(lines[ti + 1:]).strip()
        if not text:
            continue
        events.append(f"Dialogue: 0,{_tc(t0)},{_tc(t1)},Default,,0,0,0,,{text}")

    open(output_path, 'w', encoding='utf-8-sig').write(
        header + '\n'.join(events) + '\n')


# ============================================================================
# НАСТРОЙКА ЛОГГИРОВАНИЯ
# ============================================================================

def setup_logging():
    """Настраивает логгирование в файл и консоль"""
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_file = f'video_concat_{timestamp}.log'

    logger = logging.getLogger('VideoConcat')
    logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        '%(asctime)s | %(levelname)-8s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    logger.info("="*80)
    logger.info("НАЧАЛО РАБОТЫ СКРИПТА")
    logger.info("="*80)

    return logger, log_file

# Глобальный логгер
logger = None

# ============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================================

def get_audio_duration(audio_path):
    """Получает длительность аудиофайла в секундах"""
    try:
        logger.debug(f"Получение длительности аудио: {audio_path}")
        result = subprocess.run(
            [FFPROBE, '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', audio_path],
            capture_output=True, text=True, encoding='utf-8', errors='replace', check=True, **SUBPROCESS_FLAGS
        )
        duration = float(result.stdout.strip())
        logger.info(f"Длительность аудио: {duration:.2f} сек")
        return duration
    except Exception as e:
        logger.error(f"Ошибка при получении длительности аудио: {e}")
        logger.error(traceback.format_exc())
        print(f"❌ Ошибка при получении длительности аудио: {e}")
        sys.exit(1)

def get_video_duration(video_path):
    """Получает длительность видеофайла в секундах"""
    try:
        result = subprocess.run(
            [FFPROBE, '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', str(video_path)],
            capture_output=True, text=True, encoding='utf-8', errors='replace', check=True, **SUBPROCESS_FLAGS
        )
        duration = float(result.stdout.strip())
        logger.debug(f"Длительность видео {video_path}: {duration:.2f} сек")
        return duration
    except Exception as e:
        logger.warning(f"Не удалось получить длительность {video_path}: {e}")
        return 0

def get_file_size_mb(file_path):
    """Получает размер файла в МБ"""
    try:
        size = os.path.getsize(file_path) / (1024 * 1024)
        return size
    except:
        return 0

def check_processed_file(file_path, min_size_kb=100):
    """Проверяет что обработанный файл существует и не битый"""
    if not os.path.exists(file_path):
        return False
    if os.path.getsize(file_path) < min_size_kb * 1024:
        logger.debug(f"Файл {file_path} слишком мал")
        return False
    try:
        duration = get_video_duration(file_path)
        return duration > 0
    except:
        return False

def get_video_resolution(file_path):
    """Возвращает (width, height) видео или (0, 0) при ошибке."""
    try:
        result = subprocess.run(
            [FFPROBE, '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=width,height',
             '-of', 'csv=p=0', str(file_path)],
            capture_output=True, text=True, encoding='utf-8', errors='replace', **SUBPROCESS_FLAGS
        )
        parts = result.stdout.strip().split(',')
        if len(parts) >= 2:
            return int(parts[0]), int(parts[1])
    except Exception:
        pass
    return 0, 0

def has_audio_stream(file_path):
    """Returns True if the media has at least one audio stream."""
    try:
        result = subprocess.run(
            [FFPROBE, '-v', 'error', '-select_streams', 'a:0',
             '-show_entries', 'stream=index',
             '-of', 'csv=p=0', str(file_path)],
            capture_output=True, text=True, encoding='utf-8', errors='replace', **SUBPROCESS_FLAGS
        )
        return bool(result.stdout.strip())
    except Exception:
        return False

def validate_montage_plan(clips, audio_path):
    """Валидация монтажного плана"""
    logger.info("ЭТАП 1: ВАЛИДАЦИЯ МОНТАЖНОГО ПЛАНА")

    print("\n" + "="*80)
    print("📋 ВАЛИДАЦИЯ МОНТАЖНОГО ПЛАНА")
    print("="*80)

    if not clips:
        logger.error("Ошибка: нет clips в плане!")
        print("❌ Ошибка: нет clips в плане!")
        sys.exit(1)

    if audio_path is None:
        # VEO audio inline mode without narration — derive timeline from clips
        actual_audio_duration = clips[-1]['endTime']
        logger.info(f"veo-audio-inline без нарации: длительность из клипов {actual_audio_duration:.2f}с")
        print(f"\n[INFO] Нарация не задана (veo-audio-inline режим). Длительность из монтажного плана: {actual_audio_duration:.2f}с")
    else:
        actual_audio_duration = get_audio_duration(audio_path)
        if actual_audio_duration is None:
            logger.error(f"Не удалось получить длительность аудио: {audio_path}")
            print(f"❌ Не удалось получить длительность аудио: {audio_path}")
            sys.exit(1)
    logger.info(f"Количество clips в плане: {len(clips)}")

    print(f"\n📊 Длительность аудио: {actual_audio_duration:.2f} сек")
    print(f"📊 Количество clips: {len(clips)}")

    first_start = clips[0]['startTime']
    last_end = clips[-1]['endTime']

    logger.info(f"Первый clip начинается: {first_start:.2f} сек")
    logger.info(f"Последний clip заканчивается: {last_end:.2f} сек")

    print(f"\n📊 Покрытие плана:")
    print(f"   • Первый clip: {first_start:.2f} сек")
    print(f"   • Последний clip: {last_end:.2f} сек")
    print(f"   • Аудио: 0.00 - {actual_audio_duration:.2f} сек")

    if first_start > 0.1:
        logger.warning(f"Гэп в начале: {first_start:.2f} сек")
        print(f"\n⚠️  ВНИМАНИЕ: Первый clip начинается с {first_start:.2f}с (будет исправлено)")

    if abs(last_end - actual_audio_duration) > 0.1:
        if last_end < actual_audio_duration:
            logger.warning(f"Гэп в конце: {actual_audio_duration - last_end:.2f} сек")
            print(f"⚠️  ВНИМАНИЕ: Последний clip {last_end:.2f}с < аудио {actual_audio_duration:.2f}с (будет исправлено)")
        else:
            logger.warning(f"Последний clip длиннее аудио на {last_end - actual_audio_duration:.2f} сек")
            print(f"⚠️  ВНИМАНИЕ: Последний clip {last_end:.2f}с > аудио {actual_audio_duration:.2f}с")

    print(f"\n✅ Валидация завершена")
    print("="*80)
    logger.info("Валидация плана завершена успешно")

    return actual_audio_duration

def analyze_gaps(clips, audio_duration):
    """Анализирует все гэпы и перекрытия: в начале, внутри, в конце"""
    logger.info("ЭТАП 2: АНАЛИЗ ГЭПОВ И ПЕРЕКРЫТИЙ")

    print("\n" + "="*80)
    print("🔍 АНАЛИЗ ГЭПОВ И ПЕРЕКРЫТИЙ")
    print("="*80)

    gaps_info = []
    corrections = []

    # 1. Проверка начального гэпа
    first_start = clips[0]['startTime']
    if first_start > 0.01:
        gap = first_start
        logger.info(f"ГЭП В НАЧАЛЕ: {gap:.2f} сек")
        gaps_info.append(f"⚠️  ГЭП В НАЧАЛЕ: {gap:.2f} сек (до первого clip)")
        corrections.append({
            'type': 'start_gap',
            'clip_index': 0,
            'original_start': first_start,
            'new_start': 0.0,
            'gap_size': gap
        })

    # 2. Проверка внутренних гэпов и перекрытий
    for i in range(len(clips) - 1):
        end_current = clips[i]['endTime']
        start_next = clips[i+1]['startTime']
        diff = start_next - end_current

        if diff > 0.01:
            # Гэп — растягиваем предыдущий клип
            gap = diff
            logger.info(f"ВНУТРЕННИЙ ГЭП #{i+1}: {gap:.2f} сек между clip {clips[i]['id']} и {clips[i+1]['id']}")
            gaps_info.append(f"⚠️  ГЭП #{i+1}: {gap:.2f} сек между clip {clips[i]['id']} и {clips[i+1]['id']}")
            logger.info(f"  → Решение: РЕТАЙМ clip {clips[i]['id']} (+{gap:.2f} сек)")
            corrections.append({
                'type': 'retime_extend',
                'clip_index': i,
                'gap_size': gap,
                'original_duration': end_current - clips[i]['startTime'],
                'new_duration': (end_current - clips[i]['startTime']) + gap
            })

        elif diff < -0.01:
            # Перекрытие — обрезаем предыдущий клип
            overlap = -diff
            logger.warning(f"ПЕРЕКРЫТИЕ #{i+1}: {overlap:.2f} сек между clip {clips[i]['id']} и {clips[i+1]['id']}")
            gaps_info.append(f"🔴 ПЕРЕКРЫТИЕ #{i+1}: {overlap:.2f} сек между clip {clips[i]['id']} и {clips[i+1]['id']}")
            logger.info(f"  → Решение: ОБРЕЗКА clip {clips[i]['id']} endTime {end_current:.2f} → {start_next:.2f}")
            corrections.append({
                'type': 'trim_overlap',
                'clip_index': i,
                'overlap_size': overlap,
                'original_end': end_current,
                'new_end': start_next
            })

    # 3. Проверка конечного гэпа
    last_end = clips[-1]['endTime']
    if last_end < audio_duration - 0.01:
        gap = audio_duration - last_end
        logger.info(f"ГЭП В КОНЦЕ: {gap:.2f} сек")
        gaps_info.append(f"⚠️  ГЭП В КОНЦЕ: {gap:.2f} сек (после последнего clip)")
        corrections.append({
            'type': 'end_gap',
            'clip_index': len(clips) - 1,
            'original_end': last_end,
            'new_end': audio_duration,
            'gap_size': gap
        })

    # Вывод результатов
    if gaps_info:
        overlaps_count = sum(1 for c in corrections if c['type'] == 'trim_overlap')
        gaps_count = len(gaps_info) - overlaps_count
        logger.info(f"Найдено: гэпов={gaps_count}, перекрытий={overlaps_count}")
        print(f"\n📊 Найдено: гэпов={gaps_count}, перекрытий={overlaps_count}")
        for info in gaps_info:
            print(f"   {info}")

        print(f"\n🔧 План корректировок:")
        for corr in corrections:
            if corr['type'] == 'start_gap':
                print(f"   • Clip {clips[corr['clip_index']]['id']}: РЕТАЙМ начала с {corr['original_start']:.2f}→0.00 (+{corr['gap_size']:.2f}с)")
            elif corr['type'] == 'end_gap':
                print(f"   • Clip {clips[corr['clip_index']]['id']}: РЕТАЙМ конца с {corr['original_end']:.2f}→{corr['new_end']:.2f} (+{corr['gap_size']:.2f}с)")
            elif corr['type'] == 'retime_extend':
                print(f"   • Clip {clips[corr['clip_index']]['id']}: РЕТАЙМ длительности {corr['original_duration']:.2f}→{corr['new_duration']:.2f}с")
            elif corr['type'] == 'trim_overlap':
                print(f"   • Clip {clips[corr['clip_index']]['id']}: ОБРЕЗКА {corr['original_end']:.2f}→{corr['new_end']:.2f} (-{corr['overlap_size']:.2f}с)")
    else:
        logger.info("Гэпов и перекрытий не обнаружено")
        print(f"\n✅ Гэпов и перекрытий не обнаружено! План идеален!")

    print("="*80)
    logger.info(f"Анализ завершен. Корректировок: {len(corrections)}")

    return corrections

def fix_video_audio_sync(video_path, audio_path, temp_dir, encoder_type, codec, bitrate):
    """АВТОМАТИЧЕСКОЕ ИСПРАВЛЕНИЕ рассинхронизации видео и аудио"""
    logger.info("ЭТАП 6: ПРОВЕРКА ФИНАЛЬНОЙ СИНХРОНИЗАЦИИ")
    print("\n" + "="*80)
    print("🔧 ПРОВЕРКА ФИНАЛЬНОЙ СИНХРОНИЗАЦИИ")
    print("="*80)

    video_duration = get_video_duration(video_path)
    audio_duration = get_audio_duration(audio_path)

    logger.info(f"Длительность склеенного видео: {video_duration:.2f} сек")
    logger.info(f"Длительность аудио озвучки: {audio_duration:.2f} сек")

    print(f"\n📊 Длительность склеенного видео: {video_duration:.2f} сек")
    print(f"📊 Длительность аудио озвучки: {audio_duration:.2f} сек")

    diff = video_duration - audio_duration
    print(f"📊 Разница: {diff:.2f} сек")
    logger.info(f"Разница: {diff:.2f} сек")

    # Идеальная синхронизация
    if abs(diff) <= 0.1:
        logger.info("Синхронизация идеальна")
        print(f"\n✅ Отлично! Синхронизация идеальна (разница {abs(diff):.2f} сек)")
        print("="*80)
        return str(video_path)

    # Допустимая рассинхронизация
    if abs(diff) <= 0.5:
        logger.info("Небольшая рассинхронизация, допустима")
        print(f"\n✅ Хорошо! Небольшая рассинхронизация ({abs(diff):.2f} сек)")
        print("="*80)
        return str(video_path)

    # Быстрая обрезка/дополнение хвоста (0.5-2.5 сек) БЕЗ перекодирования
    if abs(diff) <= 2.5:
        logger.info(f"Быстрая коррекция хвоста: {abs(diff):.2f} сек (trim без перекодирования)")
        print(f"\n🔧 Обнаружена рассинхронизация: {abs(diff):.2f} сек")
        print(f"   Применяю быструю обрезку/дополнение (БЕЗ перекодирования)...")

        fixed_video = temp_dir / 'concat_video_fixed.mp4'

        if diff > 0:
            # Видео длиннее - обрезаем хвост
            logger.info(f"Обрезаем {diff:.2f} сек с хвоста (trim)")
            print(f"   Видео длиннее аудио на {diff:.2f} сек")
            print(f"   Обрезаем хвост через trim...")
            cmd = [
                FFMPEG, '-y',
                '-i', str(video_path),
                '-t', str(audio_duration),
                '-c', 'copy',
                str(fixed_video)
            ]
        else:
            # Видео короче - обрезаем по аудио
            logger.info(f"Видео короче на {abs(diff):.2f} сек - обрезаем по аудио (копия)")
            print(f"   Видео короче аудио на {abs(diff):.2f} сек")
            print(f"   Обрезаем по длительности аудио (копия)...")
            cmd = [
                FFMPEG, '-y',
                '-i', str(video_path),
                '-t', str(audio_duration),
                '-c', 'copy',
                str(fixed_video)
            ]

        logger.debug(f"FFmpeg команда: {' '.join(cmd)}")

        trim_success = False
        try:
            run_ffmpeg_with_spinner(cmd, "Обрезка хвоста")
            logger.info("Быстрая коррекция хвоста выполнена успешно")
            trim_success = True
        except subprocess.CalledProcessError as e:
            logger.error(f"Ошибка коррекции хвоста: {e}")
            logger.error(traceback.format_exc())
            print(f"   ⚠️  Trim не сработал, перехожу на setpts...")

        if trim_success:
            fixed_duration = get_video_duration(fixed_video)
            final_diff = abs(fixed_duration - audio_duration)
            logger.info(f"Новая длительность видео: {fixed_duration:.2f} сек, разница: {final_diff:.2f} сек")

            print(f"\n✅ Исправление завершено!")
            print(f"   Новая длительность видео: {fixed_duration:.2f} сек")
            print(f"   Разница с аудио: {final_diff:.2f} сек")
            print("="*80)

            return str(fixed_video)
        # Если trim не удался - код продолжит выполнение и перейдёт к setpts ниже

    # Разница > 2 сек (или trim не сработал) - равномерная коррекция через setpts (перекодирование).
    # setpts равномерно сжимает/растягивает весь видеоряд - синхрон идеален в каждой точке.

    logger.warning(f"Рассинхронизация {abs(diff):.2f} сек - применяю setpts ускорение/замедление")
    print(f"\n🔧 Обнаружена рассинхронизация: {abs(diff):.2f} сек")
    print(f"   Применяю ускорение/замедление (требует перекодирования)...")

    fixed_video = temp_dir / 'concat_video_fixed.mp4'
    encoder_params = get_encoder_params(encoder_type, codec, bitrate)
    speed_factor = video_duration / audio_duration

    if diff < 0:
        logger.info(f"Замедляем видео: {speed_factor:.4f}x")
        print(f"   Видео короче аудио на {abs(diff):.2f} сек")
        print(f"   Замедляем видео: {speed_factor:.4f}x")
    else:
        logger.info(f"Ускоряем видео: {speed_factor:.4f}x")
        print(f"   Видео длиннее аудио на {diff:.2f} сек")
        print(f"   Ускоряем видео: {speed_factor:.4f}x")

    # Не используем -r 60: он форсирует N кадров/сек независимо от PTS,
    # что нейтрализует сжатие setpts и оставляет ту же длину видео.
    cmd = [
        FFMPEG, '-y',
        '-i', str(video_path),
        '-filter:v', f'setpts={1/speed_factor}*PTS',
    ] + encoder_params + [
        '-c:a', 'copy',
        str(fixed_video)
    ]

    logger.debug(f"FFmpeg команда: {' '.join(cmd)}")
    print(f"   Обработка (это займёт время)...")

    try:
        subprocess.run(cmd, check=True, capture_output=True, **SUBPROCESS_FLAGS)
        logger.info("Ускорение/замедление выполнено успешно")
    except subprocess.CalledProcessError as e:
        logger.error(f"Ошибка ускорения/замедления: {e}")
        logger.error(traceback.format_exc())
        raise

    fixed_duration = get_video_duration(fixed_video)
    final_diff = abs(fixed_duration - audio_duration)
    logger.info(f"Новая длительность видео: {fixed_duration:.2f} сек, разница: {final_diff:.2f} сек")

    print(f"\n✅ Исправление завершено!")
    print(f"   Новая длительность видео: {fixed_duration:.2f} сек")
    print(f"   Разница с аудио: {final_diff:.2f} сек")
    print("="*80)

    return str(fixed_video)


def _test_encoder(codec):
    """Проверяет, работает ли энкодер реально (не просто скомпилирован)"""
    try:
        test_cmd = [
            FFMPEG, '-hide_banner', '-loglevel', 'error',
            '-f', 'lavfi', '-i', 'nullsrc=s=256x256:d=0.1',
            '-pix_fmt', 'yuv420p',
            '-c:v', codec, '-frames:v', '1', '-f', 'null', '-'
        ]
        r = subprocess.run(test_cmd, capture_output=True, timeout=10, **SUBPROCESS_FLAGS)
        return r.returncode == 0
    except Exception:
        return False


def _nvidia_present():
    """True если в системе есть РЕАЛЬНАЯ дискретная NVIDIA (nvidia-smi отрабатывает).
    nvidia-smi ставится только вместе с драйвером NVIDIA — надёжнее ffmpeg-теста,
    который на гибридных APU (Ryzen + встроенная Radeon) ложно проходит для h264_amf/nvenc."""
    for _exe in ('nvidia-smi', r'C:\Windows\System32\nvidia-smi.exe'):
        try:
            r = subprocess.run([_exe, '-L'], capture_output=True, timeout=8, **SUBPROCESS_FLAGS)
            if r.returncode == 0 and b'GPU' in (r.stdout or b''):
                return True
        except Exception:
            continue
    return False


def detect_gpu_encoder(force_amd=False):
    """Определяет доступный GPU энкодер (с реальной проверкой работоспособности).

    Приоритет: дискретная NVIDIA (h264_nvenc) > встроенная AMD Radeon (h264_amf) > CPU.
    На гибридных APU (Ryzen со встроенной Radeon + дискретная NVIDIA) h264_amf доступен
    на слабой iGPU и падает на нагрузке — поэтому дискретную NVIDIA выбираем первой.
    force_amd=True — ручной оверрайд: принудительно AMD (h264_amf), если он доступен."""
    logger.debug("Определение доступного GPU энкодера")
    try:
        result = subprocess.run([FFMPEG, '-hide_banner', '-encoders'],
                              capture_output=True, text=True, encoding='utf-8', errors='replace', **SUBPROCESS_FLAGS)
        has_amf   = 'h264_amf'   in result.stdout
        has_nvenc = 'h264_nvenc' in result.stdout

        # Ручной оверрайд: юзер явно просит AMD (например, nvenc глючит на его системе)
        if force_amd and has_amf and _test_encoder('h264_amf'):
            logger.info("force_amd: принудительно AMD GPU (h264_amf)")
            return 'amd', 'h264_amf'

        # Дискретная NVIDIA приоритетнее встроенной Radeon. Гейт nvidia-smi отсекает
        # ложноположительный ffmpeg-тест на машинах без реальной NVIDIA-карты.
        if has_nvenc and _nvidia_present() and _test_encoder('h264_nvenc'):
            logger.info("Обнаружен дискретный NVIDIA GPU (h264_nvenc)")
            return 'nvidia', 'h264_nvenc'

        # NVIDIA нет (или nvidia-smi недоступен) — пробуем AMD (в т.ч. встроенную Radeon)
        if has_amf and _test_encoder('h264_amf'):
            logger.info("Обнаружен AMD GPU (h264_amf)")
            return 'amd', 'h264_amf'

        # Крайний случай: nvenc в списке, но nvidia-smi не отработал — пробуем nvenc напрямую
        if has_nvenc and _test_encoder('h264_nvenc'):
            logger.info("Обнаружен NVIDIA GPU (h264_nvenc, без nvidia-smi)")
            return 'nvidia', 'h264_nvenc'

        logger.info("GPU не обнаружен, используется CPU (libx264)")
        return 'cpu', 'libx264'
    except Exception as e:
        logger.warning(f"Ошибка определения GPU: {e}")
        return 'cpu', 'libx264'

def _bitrate_mbit(bitrate, default=10.0):
    """'10M' / '10000k' / '10000000' → мегабиты числом. Нужен, чтобы считать
    потолок и буфер от целевого битрейта и не ломаться на форме записи:
    старый код резал последний символ и падал бы на '10000k' и на голом числе."""
    try:
        raw = str(bitrate).strip().lower()
        if raw.endswith('m'):
            return float(raw[:-1])
        if raw.endswith('k'):
            return float(raw[:-1]) / 1000.0
        return float(raw) / 1_000_000.0
    except (TypeError, ValueError):
        return default


def _fmt_mbit(value):
    return f"{value:.6g}M"


_ENCODER_OPT_CACHE = {}


def _encoder_supports(codec, extra_opts):
    """Принимает ли сборка ffmpeg эти ключи у данного энкодера.

    Проверка боем — кодируем один кадр, как в `_test_encoder`. Разбор вывода
    `-h encoder=...` не годится: ключ бывает в справке, но отваливается на
    старом драйвере или урезанной сборке, а падает при этом вся склейка
    целиком. Результат кэшируется: проверка идёт один раз за запуск."""
    key = (codec, tuple(extra_opts))
    if key in _ENCODER_OPT_CACHE:
        return _ENCODER_OPT_CACHE[key]
    ok = False
    try:
        test_cmd = [
            FFMPEG, '-hide_banner', '-loglevel', 'error',
            '-f', 'lavfi', '-i', 'nullsrc=s=256x256:d=0.5',
            '-pix_fmt', 'yuv420p',
            '-c:v', codec, *extra_opts,
            '-frames:v', '1', '-f', 'null', '-'
        ]
        r = subprocess.run(test_cmd, capture_output=True, timeout=25, **SUBPROCESS_FLAGS)
        ok = r.returncode == 0
    except Exception:
        ok = False
    _ENCODER_OPT_CACHE[key] = ok
    return ok


# Наборы ключей NVENC против блочности — от полного к урезанному. Берётся
# первый, который сборка у пользователя реально приняла.
#
# Зачем вообще: медленный зум по картинке — для кодека не «статичный кадр», а
# полностью новый кадр каждую 1/60 секунды. При наезде каждый блок уезжает на
# доли пикселя, ни один не совпадает с предыдущим кадром, и дешёвых P-кадров
# не остаётся. Голый NVENC (`p4 -tune hq` и больше ничего) квантует такой кадр
# равномерно, поэтому первыми сыпятся гладкие места — небо, стена, размытый
# задник. Сыпятся именно квадратами 16×16, что пользователи и видят.
#   spatial-aq   — отдаёт гладким участкам больше бит, это и лечит квадраты;
#   temporal-aq  — перераспределяет биты между кадрами;
#   rc-lookahead — кодек видит наезд заранее и готовит под него бюджет;
#   bf           — B-кадры (у h264_nvenc их по умолчанию нет вовсе);
#   multipass    — второй проход по полному разрешению, точнее раскладывает биты.
_NVENC_QUALITY_SETS = (
    ('-rc', 'vbr', '-spatial-aq', '1', '-aq-strength', '8', '-temporal-aq', '1',
     '-rc-lookahead', '20', '-bf', '3', '-multipass', 'fullres'),
    ('-rc', 'vbr', '-spatial-aq', '1', '-aq-strength', '8', '-temporal-aq', '1',
     '-rc-lookahead', '20', '-bf', '3'),
    ('-rc', 'vbr', '-spatial-aq', '1', '-aq-strength', '8'),
)


def _log(level, message):
    """Лог, который не валит процесс. `logger` в модуле до setup_logging() равен
    None, а get_encoder_params зовут и из тестов, и из внешнего кода — там
    обычный logger.info падал бы с AttributeError (поймано проверкой 01.09)."""
    if logger is not None:
        getattr(logger, level, logger.info)(message)


def _nvenc_tuning_enabled():
    """CONCAT_NVENC_TUNING=off возвращает ровно прежнее поведение NVENC."""
    raw = (os.getenv('CONCAT_NVENC_TUNING', 'on') or 'on').strip().lower()
    return raw not in ('0', 'off', 'false', 'no')


def _nvenc_quality_opts(codec):
    """Первый набор ключей NVENC, который переварила сборка ffmpeg.
    Пусто — значит сборка старая: кодируем без них, склейка не падает."""
    for idx, opts in enumerate(_NVENC_QUALITY_SETS, 1):
        if _encoder_supports(codec, list(opts)):
            _log('info', f"NVENC: набор качества #{idx} принят сборкой ffmpeg: {' '.join(opts)}")
            return list(opts)
    _log('warning', "NVENC: сборка ffmpeg не приняла ни один набор качества — "
                    "кодирую прежними ключами (обнови ffmpeg, если видны квадраты на зуме)")
    return []


def get_encoder_params(encoder_type, codec, bitrate='10M'):
    """Возвращает параметры для выбранного энкодера"""
    if encoder_type == 'nvidia':
        if not _nvenc_tuning_enabled():
            _log('info', "NVENC: тюнинг качества выключен (CONCAT_NVENC_TUNING=off)")
            return ['-c:v', codec, '-preset', 'p4', '-tune', 'hq', '-b:v', bitrate,
                    '-maxrate', bitrate, '-bufsize', f'{int(bitrate[:-1])*2}M',
                    '-profile:v', 'high', '-pix_fmt', 'yuv420p']
        # Потолок выше цели, буфер вдвое от потолка. При равных `-b:v` и
        # `-maxrate` кодек упирается в полку и не может занять биты на тяжёлый
        # кусок, даже когда соседние сцены дешёвые: замер финального ролика
        # показывал ровные 9.5–10.5 Мбит/с при `-b 10M` — это был упор, а не
        # достаток (разбор 01.09). Средний битрейт держит `-b:v`, размер файла
        # почти не растёт.
        mbit = _bitrate_mbit(bitrate)
        quality_opts = _nvenc_quality_opts(codec)
        # p5 вместо p4 — только вместе с рабочим набором качества: на пустом
        # наборе сборка старая, и лишний вес пресета ничем не окупится.
        preset = 'p5' if quality_opts else 'p4'
        return ['-c:v', codec, '-preset', preset, '-tune', 'hq', '-b:v', bitrate,
                '-maxrate', _fmt_mbit(mbit * 1.5), '-bufsize', _fmt_mbit(mbit * 3),
                *quality_opts,
                '-profile:v', 'high', '-pix_fmt', 'yuv420p']
    elif encoder_type == 'amd':
        return ['-c:v', codec, '-quality', 'speed', '-b:v', bitrate,
                '-maxrate', bitrate, '-bufsize', f'{int(bitrate[:-1])*2}M',
                '-profile:v', 'high', '-pix_fmt', 'yuv420p']
    else:
        return ['-c:v', codec, '-preset', 'medium', '-b:v', bitrate,
                '-maxrate', bitrate, '-bufsize', f'{int(bitrate[:-1])*2}M',
                '-profile:v', 'high', '-pix_fmt', 'yuv420p']

_IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tiff', '.tif'}
_STILL_EXPECTED_FORMATS = {
    '.png': {'PNG'},
    '.jpg': {'JPEG'},
    '.jpeg': {'JPEG'},
    '.webp': {'WEBP'},
    '.bmp': {'BMP'},
    '.tif': {'TIFF'},
    '.tiff': {'TIFF'},
}

# ── Формат ролика (--aspect / env VIDEO_TARGET_ASPECT) ───────────────────────
# Все целевые разрешения в коде заданы ЛАНДШАФТНОЙ базой (1920x1080 и т.п.) и
# прогоняются через oriented(): 9:16 → стороны меняются местами (1080x1920),
# 1:1 → квадрат по меньшей стороне. main() перекрывает из args.aspect.
TARGET_ASPECT = (os.environ.get('VIDEO_TARGET_ASPECT', '16:9') or '16:9').strip()


def oriented(w: int, h: int) -> tuple:
    """Повернуть базовое ландшафтное разрешение под TARGET_ASPECT."""
    if TARGET_ASPECT == '9:16':
        return h, w
    if TARGET_ASPECT == '1:1':
        return h, h
    return w, h


def _target_frame_size(resolution, prescan_height=0):
    """Целевой размер кадра на весь прогон — ОДНА точка решения.

    Раньше их было две, и они спорили: явный `-r` давал флаги upscale_1080 /
    upscale_4k, а префскан отдельно поднимал `_auto_target_1080`. Второй
    перебивал первый — выбор «720» игнорировался, и клипы всё равно уезжали в
    1080p. Опускания сверху вниз не было вовсе: масштаб умел только тянуть вверх
    (разбор с пользователем 27.08).

    Теперь: задан `-r` — цель из него, включая уменьшение того, что выше;
    не задан — цель по префскану, округлённая к ступени ВНИЗ (1440 → 1080),
    чтобы не растягивать исходники сверх их разрешения.
    """
    choice = str(resolution or '').strip().lower()
    if choice in ('4k', '2160'):
        return oriented(3840, 2160)
    if choice in ('1080', '1080p'):
        return oriented(1920, 1080)
    if choice in ('720', '720p'):
        return oriented(1280, 720)
    try:
        height = int(prescan_height or 0)
    except (TypeError, ValueError):     # префскан не дал числа — берём базовую ступень
        height = 0
    # Потолок авто-режима — 1080. Решение «собираем в 4K» слишком дорогое, чтобы
    # принимать его самостоятельно: на прогоне 27.08 один случайный 4K-отрезок
    # из ютуба утащил туда ВЕСЬ проект, где остальные клипы были от 568x320.
    # Четыре килопикселя — только по явному `-r 4k`.
    if height >= 1080:
        return oriented(1920, 1080)
    return oriented(1280, 720)


def _ffmpeg_font_path(p: Path):
    """Готовит путь к шрифту для ffmpeg drawtext. ffmpeg на Windows НЕ читает fontfile
    с non-ASCII (кириллическим) путём → 'could not parse fontconfig pat'. Поэтому если путь
    содержит не-ASCII символы — копируем шрифт в ASCII-temp и отдаём его. Возвращает
    ffmpeg-escaped путь (двоеточие экранировано)."""
    try:
        str(p).encode('ascii')
        ascii_path = p
    except UnicodeEncodeError:
        import tempfile, shutil
        dst = Path(tempfile.gettempdir()) / f'begoframe_font_{p.name}'
        try:
            if not dst.exists() or dst.stat().st_size != p.stat().st_size:
                shutil.copy2(p, dst)
            ascii_path = dst
        except Exception:
            ascii_path = p  # копия не удалась — отдаём как есть (лучше попытка чем None)
    return ascii_path.as_posix().replace(':', '\\:')


def _resolve_credit_font():
    """Находит ttf-шрифт для drawtext-плашки real_photo. Возвращает ffmpeg-escaped путь или None.

    drawtext БЕЗ fontfile падает на Windows (нет fontconfig); с КИРИЛЛИЧЕСКИМ путём — тоже
    (could not parse fontconfig pat). Поэтому: ищем шрифт надёжно (бандл на неск. уровнях +
    системный по ОС), а путь прогоняем через _ffmpeg_font_path (ASCII-temp если кириллица).
    None → вызывающий код пропустит плашку, а не уронит concat.
    """
    _base = Path(__file__).resolve().parent
    for _b in (_base, _base.parent, _base.parent.parent, Path.cwd(),
               Path.cwd().parent, Path.cwd().parent.parent):
        for _name in ('BookAntiqua.ttf', 'BookAntiqua_Bold.ttf'):
            _c = _b / 'fonts' / _name
            if _c.exists():
                return _ffmpeg_font_path(_c)
    # системный fallback по ОС
    if sys.platform == 'win32':
        _sys_fonts = [r'C:\Windows\Fonts\arial.ttf', r'C:\Windows\Fonts\segoeui.ttf']
    else:
        _sys_fonts = ['/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
                      '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf']
    for _sf in _sys_fonts:
        if Path(_sf).exists():
            return _ffmpeg_font_path(Path(_sf))
    return None


def attribution_enabled() -> bool:
    """Рисовать ли плашку источника на кадре («Wikimedia», «Google Images» и т.п.).

    ПО УМОЛЧАНИЮ ВЫКЛЮЧЕНО (решение пользователя 26.08): надпись в углу кадра
    портит продакшн-качество ролика. Атрибуция при этом НЕ теряется — она
    остаётся в `real_photos_credits.json` и в `real_meta` монтажного плана,
    откуда её берут для описания ролика.

    Включается галкой «Атрибуция» в интерфейсе или `ATTRIBUTION_OVERLAY=1`
    в `.env`; отдельные лицензии (часть Wikimedia) требуют указания источника —
    для таких проектов её включают осознанно.
    """
    raw = str(os.environ.get('ATTRIBUTION_OVERLAY', '') or '').strip().lower()
    return raw in ('1', 'true', 'yes', 'on')


def _drawtext_real_photo(text):
    """Build drawtext filter for real_photo credit overlay (top-right corner)."""
    if not text:
        return ''
    if not attribution_enabled():
        return ''
    font_path = _resolve_credit_font()
    if not font_path:
        # Шрифта нет нигде — пропускаем плашку, иначе drawtext без fontfile уронит concat на Windows.
        logger.warning("real_photo credit: шрифт не найден (agent/fonts/ + системные) — плашка пропущена")
        return ''
    s = text.replace('\\', '\\\\').replace(':', '\\:').replace("'", "\\'")
    s = s.replace(',', '\\,').replace('[', '\\[').replace(']', '\\]').replace(';', '\\;')
    parts = [
        f"fontfile='{font_path}'",
        f"text='{s}'",
        'fontsize=22',
        'fontcolor=white@0.92',
        'bordercolor=black@0.75',
        'borderw=2',
        'x=w-tw-28',
        'y=28',
    ]
    return 'drawtext=' + ':'.join(parts)


# ── Dip-to-X переходы между клипами ──────────────────────────────────────────
# Переход применяется НА ГРАНИЦЕ клипа (fade_in/fade_out), но не съедает время и не зависит
# от соседа: клип сохраняет полную длительность (транзишн «съедает» ровно свою длину внутри
# самого клипа против синтетического цветного фрагмента), поэтому ретайминг клипа не требуется.
# group_concat остаётся простой copy-склейкой.
_FADE_NATIVE_COLORS = {'fade': 'black', 'fadeblack': 'black', 'fadewhite': 'white'}
_XFADE_STYLES = {
    'wipeleft', 'wiperight', 'wipeup', 'wipedown',
    'slideleft', 'slideright', 'slideup', 'slidedown',
    'smoothleft', 'smoothright',
    'dissolve', 'pixelize', 'circlecrop', 'radial', 'zoomin', 'hblur', 'distance',
}
TRANSITION_STYLES = sorted(_FADE_NATIVE_COLORS) + sorted(_XFADE_STYLES)


class _TransitionGraphBuilder:
    """Собирает filter_complex-граф для видео одного клипа: применяет fade_in/fade_out
    переходы (native fade или xfade-против-синтетического-цвета) поверх уже готовой цепочки
    (после setpts/blur/scale/logo). Копит доп. входы (`-f lavfi -i color=...`) и filter-строки;
    вызывающий код добавляет extra_inputs к cmd и filter_parts — к остальному filter_complex."""

    def __init__(self, next_input_index, fps):
        self.extra_inputs = []
        self.filter_parts = []
        self._next_idx = next_input_index
        self.fps = fps

    def apply(self, label, style, side, target_duration, transition_duration):
        """side: 'in' | 'out'. Возвращает новый label потока после применения перехода."""
        if not style:
            return label
        fdur = min(transition_duration, max(0.05, target_duration / 2))
        if style in _FADE_NATIVE_COLORS:
            color = _FADE_NATIVE_COLORS[style]
            out_label = f"{label}{side[0]}f"
            if side == 'in':
                self.filter_parts.append(f"[{label}]fade=t=in:st=0:d={fdur:.3f}:color={color}[{out_label}]")
            else:
                st = max(0.0, target_duration - fdur)
                self.filter_parts.append(f"[{label}]fade=t=out:st={st:.3f}:d={fdur:.3f}:color={color}[{out_label}]")
            return out_label

        # Xfade-стиль: синтетический цветной клип длиной ровно fdur + scale2ref
        # (чтобы совпадали размеры с основным видео, не завися от upscale-флагов).
        # xfade требует ОДИНАКОВЫЙ timebase у обоих входов — исходный клип (mp4/libx264)
        # обычно несёт очень мелкий timebase (напр. 1/12800), а lavfi color-источник — 1/fps;
        # без нормализации (fps=+settb=AVTB) xfade падает с "timebase do not match".
        idx = self._next_idx
        self._next_idx += 1
        self.extra_inputs.extend(['-f', 'lavfi', '-i', f'color=c=black:d={fdur:.3f}:r={self.fps}'])
        color_scaled = f"c{idx}s"
        main_scaled = f"{label}{side[0]}ms"
        out_label = f"{label}{side[0]}x"
        self.filter_parts.append(f"[{idx}:v][{label}]scale2ref=flags=neighbor[{color_scaled}][{main_scaled}]")
        self.filter_parts.append(
            f"[{color_scaled}]format=yuv420p,fps={self.fps},settb=AVTB,setsar=1[{color_scaled}p]")
        self.filter_parts.append(f"[{main_scaled}]fps={self.fps},settb=AVTB[{main_scaled}n]")
        if side == 'in':
            self.filter_parts.append(
                f"[{color_scaled}p][{main_scaled}n]xfade=transition={style}:duration={fdur:.3f}:offset=0[{out_label}]")
        else:
            st = max(0.0, target_duration - fdur)
            self.filter_parts.append(
                f"[{main_scaled}n][{color_scaled}p]xfade=transition={style}:duration={fdur:.3f}:offset={st:.3f}[{out_label}]")
        return out_label


# Ken Burns zoom-стили: направление (in — от нормального к увеличенному, out — наоборот)
# × точка, к которой смещается кадрирование по мере роста масштаба (center = как раньше,
# без панорамирования). При pan='center' формула идентична старому поведению (крадёт строго
# из центра); margin для панорамирования появляется естественно по мере роста/убывания scale,
# отдельно интерполировать позицию по времени не нужно.
_PAN_FRACTIONS = {
    'center':      (0.5, 0.5),
    'left':        (0.0, 0.5),
    'right':       (1.0, 0.5),
    'up':          (0.5, 0.0),
    'down':        (0.5, 1.0),
    'topleft':     (0.0, 0.0),
    'topright':    (1.0, 0.0),
    'bottomleft':  (0.0, 1.0),
    'bottomright': (1.0, 1.0),
}
ZOOM_STYLES = [f"{d}-{p}" for d in ('in', 'out') for p in _PAN_FRACTIONS]


def _parse_zoom_style(style):
    style = (style or 'in-center').strip().lower()
    direction, _, pan = style.partition('-')
    if direction not in ('in', 'out'):
        direction = 'in'
    if pan not in _PAN_FRACTIONS:
        pan = 'center'
    return direction, pan


def _build_smooth_zoom_chain(style, target_w, target_h, target_duration, zoom_amount,
                             zoom_curve, zoom_flags, supersample=1):
    """Собирает scale+crop цепочку Ken Burns эффекта для одного стиля (direction-pan).
    pan='center' → идентично старому поведению (кроп строго из центра).

    `supersample` (1/2/3) — во сколько раз считать движение крупнее финального
    кадра. Зачем: `scale` выдаёт ЦЕЛОЕ число пикселей, а при зуме 6% за 6 секунд
    картинка должна ползти примерно на 0.45 px за кадр. Целое приращение
    означает, что каждый второй кадр она стоит, а потом прыгает на пиксель —
    это и читается как дрожание, особенно на тонких прямых линиях.

    Считая в S раз крупнее и ужимая в самом конце, мы превращаем шаг в один
    пиксель промежуточного кадра в долю пикселя финального: ресемплер
    размазывает его между соседними точками, и движение становится
    субпиксельным. Замер на тестовой картинке (зум 6% за 6 с, 30 fps):
    как сейчас — дрожание 102%, 63 стоящих кадра из 176; ×2 — 38% и ни одного
    стоящего; ×3 — 21% и один. Резкость при этом не падает, а растёт: финальный
    bicubic-даунскейл чётче bilinear-апскейла в мелком масштабе.

    Цена — время склейки: ×2 примерно +20%, ×3 примерно +65% (замер на живом
    материале). Умолчание — 2: дрожание уходит совсем, а плата невелика.
    """
    direction, pan = _parse_zoom_style(style)
    if zoom_curve == 'constant_speed':
        base = f"({zoom_amount * 0.125} * t)"
    elif zoom_curve == 'ease_out':
        base = f"({zoom_amount} * (1 - pow(1 - t/{target_duration:.6f}, 2)))"
    else:  # linear (default)
        base = f"({zoom_amount} * t/{target_duration:.6f})"
    # 'out' — зеркалим прогресс: старт на максимуме масштаба, к концу — нормальный размер.
    progress = f"({zoom_amount} - {base})" if direction == 'out' else base
    fx, fy = _PAN_FRACTIONS[pan]
    try:
        scale_factor = int(supersample)
    except (TypeError, ValueError):
        scale_factor = 1
    scale_factor = min(3, max(1, scale_factor))
    work_w = target_w * scale_factor
    work_h = target_h * scale_factor
    # max(1.0, ...) — страховка от кадра, который ffmpeg отрисовывает чуть за
    # пределом длительности: там прогресс у стиля «out» уходит в минус, масштаб
    # становится МЕНЬШЕ кропа, и ffmpeg падает с access violation. Ловилось на
    # `out-left` при супер-сэмплинге (проверка 30.08); при множителе 1 те же
    # кадры проходили молча, потому что финального уменьшения не было.
    growth = f"max(1.0, 1.0+{progress})"
    chain = (
        f"scale=w='{work_w}*{growth}':"
        f"h='{work_h}*{growth}':eval=frame:flags={zoom_flags},"
        f"crop={work_w}:{work_h}:'(in_w-{work_w})*{fx}':'(in_h-{work_h})*{fy}'"
    )
    if scale_factor > 1:
        # Уменьшение в самом конце — здесь субпиксельный ход и появляется.
        # bicubic, а не bilinear: на даунскейле он держит мелкие детали.
        chain += f",scale={target_w}:{target_h}:flags=bicubic"
    return chain


def _remove_file_safely(path):
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def _normalize_still_image(input_file, work_dir, job_key=''):
    """Return a decoder-safe PNG for mislabeled or special still-image inputs."""
    source = Path(input_file)
    try:
        from PIL import Image, ImageOps

        with Image.open(source) as image:
            image.load()
            actual_format = (image.format or '').upper()
            expected_formats = _STILL_EXPECTED_FORMATS.get(source.suffix.lower(), set())
            has_alpha = 'A' in image.getbands() or 'transparency' in image.info
            is_animated = bool(getattr(image, 'is_animated', False))
            is_tiny = min(image.size) < 2
            needs_normalize = (
                actual_format not in expected_formats
                or has_alpha
                or is_animated
                or is_tiny
            )
            if not needs_normalize:
                return str(source), None

            source_stat = source.stat()
            fingerprint = hashlib.sha1(
                f"{source.resolve()}:{source_stat.st_size}:{source_stat.st_mtime_ns}:{job_key}:{os.getpid()}".encode(
                    'utf-8', 'surrogatepass')
            ).hexdigest()[:16]
            normalized_dir = Path(work_dir) / '_concat_stills'
            normalized_path = normalized_dir / f"{source.stem}_{fingerprint}.png"

            normalized_dir.mkdir(parents=True, exist_ok=True)
            image.seek(0)
            frame = ImageOps.exif_transpose(image).copy()
            if 'A' in frame.getbands() or 'transparency' in frame.info:
                rgba = frame.convert('RGBA')
                background = Image.new('RGBA', rgba.size, (0, 0, 0, 255))
                background.alpha_composite(rgba)
                frame = background.convert('RGB')
            else:
                frame = frame.convert('RGB')
            frame.save(normalized_path, format='PNG')

        logger.warning(
            f"Normalized still image {source} ({actual_format or 'unknown'}, "
            f"{source.suffix or 'no extension'}) to decoder-safe PNG"
        )
        return str(normalized_path), normalized_path
    except Exception as exc:
        logger.warning(f"Could not inspect still image {source}: {exc}; using original file")
        return str(source), None


def _image_to_zoom_video(input_file, output_file, target_duration, encoder_type, codec, bitrate,
                         logo_path=None, fps=60, keep_audio=False,
                         credit_text=None, blur_fill=False, upscale_1080=False, upscale_4k=False,
                         zoom_style='in-center', fade_in=False, fade_out=False, transition_duration=0.5,
                         static_no_motion=False, target_size=None):
    """
    Конвертирует изображение в видео с медленным зумом (Ken Burns effect).
    Зум: 100% → 110% за target_duration секунд. zoom_style — направление+панорама (см. ZOOM_STYLES).
    fade_in/fade_out — dip-to-X переход на границе клипа (native fade или xfade-стиль, см. TRANSITION_STYLES).

    Real Photo extras (when used by archival stage):
      blur_fill=True   — фон = blurred copy того же фото (без чёрных рамок)
      credit_text="…"  — credit overlay в верхнем правом углу
    """
    logger.info(f"Изображение → зум-видео: {input_file} ({target_duration:.2f}с)")
    is_real_photo = bool(credit_text or blur_fill)
    if is_real_photo:
        print(f"   📷 [Real Photo] Ken Burns + blur + credit ({target_duration:.2f}с)")
    else:
        print(f"   🖼️  Изображение → зум-видео ({target_duration:.2f}с)")

    output_path = Path(output_file)
    render_input, normalized_path = _normalize_still_image(
        input_file, output_path.parent, output_path.stem
    )
    encoder_params = get_encoder_params(encoder_type, codec, bitrate)
    total_frames = int(target_duration * fps)
    if target_size:
        target_w, target_h = int(target_size[0]), int(target_size[1])
    elif upscale_4k:
        target_w, target_h = oriented(3840, 2160)
    elif upscale_1080:
        target_w, target_h = oriented(1920, 1080)
    else:
        target_w, target_h = oriented(1280, 720)

    # --- Параметры зума (читаются из env) ---
    # ZOOM_AMOUNT — амплитуда (0.05/0.10/0.15/0.20)
    # ZOOM_CURVE  — кривая по времени: linear | constant_speed | ease_out
    # ZOOM_FLAGS  — resampler scale-фильтра: bilinear (быстро, дефолт) | bicubic | lanczos (медленно, качественно)
    try:
        zoom_amount = float(os.getenv('ZOOM_AMOUNT', '0.10'))
    except (TypeError, ValueError):
        zoom_amount = 0.10
    zoom_curve = (os.getenv('ZOOM_CURVE', 'linear') or 'linear').strip().lower()
    zoom_flags_raw = (os.getenv('ZOOM_FLAGS', 'bilinear') or 'bilinear').strip().lower()
    # Whitelist: пускаем только проверенные resampler'ы, иначе fallback на bilinear.
    # На каждом кадре делается scale с eval=frame — lanczos тут в 3-5× медленнее bilinear,
    # bicubic — компромисс (~1.5× медленнее bilinear, заметно острее на статичных деталях).
    _ALLOWED_ZOOM_FLAGS = {'bilinear', 'bicubic', 'lanczos', 'fast_bilinear'}
    zoom_flags = zoom_flags_raw if zoom_flags_raw in _ALLOWED_ZOOM_FLAGS else 'bilinear'
    # ZOOM_SUPERSAMPLE — 1 (как раньше) / 2 (умолчание) / 3. Считать движение крупнее и
    # ужимать в конце: так шаг зума становится субпиксельным и дрожание уходит
    # (подробности в `_build_smooth_zoom_chain`). Плата — время рендера.
    try:
        zoom_supersample = int(os.getenv('ZOOM_SUPERSAMPLE', '2') or '2')
    except (TypeError, ValueError):
        zoom_supersample = 2
    zoom_supersample = min(3, max(1, zoom_supersample))
    if static_no_motion:
        zoom_amount = 0.0

    # Плавный зум через scale+crop с eval=frame (вместо zoompan который дрожит на низком fps).
    # zoom_style = "{in|out}-{center|left|right|up|down|topleft|topright|bottomleft|bottomright}"
    # pan=center → идентично старому поведению (кроп из центра); прочие — панорамирование.
    smooth_zoom_chain = _build_smooth_zoom_chain(
        zoom_style, target_w, target_h, target_duration, zoom_amount, zoom_curve,
        zoom_flags, supersample=zoom_supersample)

    # [DEPRECATED ниже] оставлен на случай отката — старая zoompan-формула (дрожит на низком fps).
    # zoom_expr = f"'min(1.1, 1.0 + (in_w*0.1/in_w)*on/{total_frames})'"

    audio_input_args = []
    audio_output_args = ['-an']
    if keep_audio:
        # Keep stream layout compatible with VEO-audio-inline clips.
        audio_input_args = ['-f', 'lavfi', '-t', f'{target_duration:.6f}',
                            '-i', 'anullsrc=channel_layout=stereo:sample_rate=48000']
        audio_output_args = ['-c:a', 'aac', '-b:a', '192k', '-shortest']

    if blur_fill:
        # Real Photo path: blurred BG + foreground smooth zoom + optional credit
        bg_chain = (
            f'scale={target_w}:{target_h}:force_original_aspect_ratio=increase,'
            f'crop={target_w}:{target_h},'
            f'gblur=sigma=30,'
            f'eq=brightness=-0.18,'
            f'fps={fps},setsar=1'
        )
        # Снимок виден ЦЕЛИКОМ поверх размытого фона, поэтому он не масштабируется
        # отдельно: сперва собирается НЕПОДВИЖНАЯ композиция (фон + фото), а зум
        # применяется к готовому кадру целиком — тем же `_build_smooth_zoom_chain`,
        # что и в основной ветке.
        #
        # Почему именно так (разбор 26–27.08, четыре неудачных подхода):
        #   • зума не было вовсе — комментарий обещал «foreground smooth zoom», но
        #     в цепочке стоял обычный `scale`. 12 архивных кадров и 3 карточки
        #     инфографики стояли в ролике неподвижно;
        #   • рост снимка внутри кадра — фото дрожало: `overlay` центрирует по
        #     ширине потока, а та менялась покадрово;
        #   • рост внутри фиксированной рамки — фото не закрывало рамку, сверху и
        #     снизу оставались полосы фона;
        #   • масштабирование рамки — стороны 608 и 1080 квантуются каждая по
        #     своему, пропорции гуляли, движение читалось как рывки.
        # Композиция снимает все четыре: масштабируется одно изображение размером
        # в полный кадр, ровно как у стоковых, где к зуму претензий нет.
        fg_chain = (
            f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,setsar=1"
        )
        bg_chain = bg_chain.replace(f'fps={fps},', '')   # fps ставится после зума
        overlay_filter = 'overlay=(W-w)/2:(H-h)/2:format=auto,setsar=1'
        # Клеим по РЕЗУЛЬТАТУ, а не по наличию credit_text: плашка может быть
        # выключена настройкой «Атрибуция» (или пропущена из-за отсутствия
        # шрифта), и тогда `_drawtext_real_photo` отдаёт пустую строку. Старое
        # условие добавляло запятую до проверки — в граф уходил пустой фильтр,
        # ffmpeg падал с «No such filter: ''», и КАЖДЫЙ архивный кадр терялся
        # (прогон 26.08: клипы 8 и 15 заменены соседним кадром).
        _credit_filter = _drawtext_real_photo(credit_text) if credit_text else ''
        if _credit_filter:
            overlay_filter = overlay_filter + ',' + _credit_filter
        # Готовый кадр зумим целиком. Амплитуда своя: архивный снимок уже
        # вписан в кадр, и наезд в 10% съедал бы заметную часть композиции —
        # на живой проверке 27.08 подошли 5%.
        _archive_amount = max(0.0, float(os.getenv('ZOOM_AMOUNT_ARCHIVE', '0.05') or 0.05))
        if static_no_motion:
            _archive_amount = 0.0
        _archive_zoom = _build_smooth_zoom_chain(
            zoom_style, target_w, target_h, target_duration,
            _archive_amount, zoom_curve, zoom_flags, supersample=zoom_supersample)
        _fparts = [
            f'[0:v]split=2[bg_in][fg_in]',
            f'[bg_in]{bg_chain}[bg]',
            f'[fg_in]{fg_chain}[fg]',
            f'[bg][fg]{overlay_filter}[composed]',
            f'[composed]{_archive_zoom},fps={fps}[zoomed]',
        ]
        input_args = ['-loop', '1', '-i', render_input] + audio_input_args
        _audio_idx = 1
        _next_idx = 2 if keep_audio else 1
    else:
        zoom_filter = (
            f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
            f"crop={target_w}:{target_h},"
            f"{smooth_zoom_chain},"
            f"fps={fps}"
        )
        # См. выше: клеим только непустой фильтр, иначе граф ломается запятой.
        _credit_filter = _drawtext_real_photo(credit_text) if credit_text else ''
        if _credit_filter:
            zoom_filter = zoom_filter + ',' + _credit_filter

        if logo_path:
            # Лого накладывается AS-IS (в исходных размерах PNG). Юзер сам решает
            # какого размера ему нужно — 150×150 / 350×500 — ffmpeg не трогает.
            _fparts = [f"[0:v]{zoom_filter}[v]", f"[v][1:v]overlay=W-w-4:H-h-4[zoomed]"]
            input_args = ['-loop', '1', '-i', render_input, '-i', str(logo_path)] + audio_input_args
            _audio_idx = 2
            _next_idx = 3 if keep_audio else 2
        else:
            _fparts = [f"[0:v]{zoom_filter}[zoomed]"]
            input_args = ['-loop', '1', '-i', render_input] + audio_input_args
            _audio_idx = 1
            _next_idx = 2 if keep_audio else 1

    # Dip-to-X переходы: клип сохраняет полную target_duration — native fade или xfade против
    # синтетического цветного клипа (wipe/slide/dissolve/...), время НЕ съедается. Без fade — no-op.
    _cur_label = 'zoomed'

    _tb = _TransitionGraphBuilder(_next_idx, fps)
    _cur_label = _tb.apply(_cur_label, fade_in, 'in', target_duration, transition_duration)
    _cur_label = _tb.apply(_cur_label, fade_out, 'out', target_duration, transition_duration)
    _fparts.extend(_tb.filter_parts)
    video_filter = ';'.join(_fparts)

    maps = ['-map', f'[{_cur_label}]']
    if keep_audio:
        maps += ['-map', f'{_audio_idx}:a']

    cmd = [FFMPEG, '-y', '-nostdin'] + input_args + _tb.extra_inputs + [
           '-filter_complex', video_filter] + maps + [
           '-t', f'{target_duration:.6f}',
           '-r', str(fps)] + encoder_params + audio_output_args + [str(output_path)]

    try:
        try:
            subprocess.run(cmd, check=True, capture_output=True, **SUBPROCESS_FLAGS)
        except subprocess.CalledProcessError as e:
            # GPU-энкодер (h264_amf/nvenc) может крашиться на отдельных кадрах (особенно AMF
            # на слабой встроенной Radeon). Фолбэк на libx264 (CPU), чтобы ОДИН real-photo кадр
            # не ронял весь concat. У видео-пути (process_video) такой фолбэк уже есть.
            if encoder_type == 'cpu':
                raise
            _stderr = e.stderr[-400:].decode('utf-8', 'replace') if isinstance(e.stderr, (bytes, bytearray)) else str(e.stderr or '')
            logger.warning(f"{codec} ({encoder_type}) упал на {input_file} — фолбэк на libx264 (CPU). stderr: {_stderr}")
            print(f"   ⚠️  {codec} упал на кадре → CPU-фолбэк (libx264)")
            _cpu_params = get_encoder_params('cpu', 'libx264', bitrate)
            _n = len(encoder_params)
            _cmd_cpu = None
            for _i in range(len(cmd) - _n + 1):
                if cmd[_i:_i + _n] == encoder_params:
                    _cmd_cpu = cmd[:_i] + list(_cpu_params) + cmd[_i + _n:]
                    break
            if _cmd_cpu is None:
                raise
            subprocess.run(_cmd_cpu, check=True, capture_output=True, **SUBPROCESS_FLAGS)
            logger.info(f"Зум-видео готово (CPU-фолбэк): {output_file}")
        else:
            logger.info(f"Зум-видео готово: {output_file}")
    finally:
        if normalized_path is not None:
            _remove_file_safely(normalized_path)


def _build_atempo(speed_factor: float) -> str:
    """Строит цепочку atempo фильтров для любого speed_factor (диапазон каждого [0.5, 2.0])."""
    filters = []
    sf = speed_factor
    while sf > 2.0:
        filters.append("atempo=2.0")
        sf /= 2.0
    while sf < 0.5:
        filters.append("atempo=0.5")
        sf /= 0.5
    filters.append(f"atempo={sf:.6f}")
    return ",".join(filters)


def _afade_filter_suffix(fade_in: bool, fade_out: bool, target_duration: float,
                         transition_duration: float) -> str:
    """Аудио-суффикс перехода на границе клипа (keep_audio-путь): afade in/out той же
    длительности, что и видео-fade. У afade нет «формы» перехода — просто плавное
    нарастание/приглушение громкости. Возвращает '' либо ',afade=...' — суффикс
    конкатенируется к результату _build_atempo().

    ФИКС 2026-07-19: вызов этой функции добавили вместе с переходами (transitions),
    а само определение потеряли — у юзеров с включённым звуком VEO концах падал
    NameError на каждом клипе."""
    if not (fade_in or fade_out):
        return ''
    try:
        _dur = float(target_duration)
        d = max(0.05, min(float(transition_duration), _dur / 2.0))
    except (TypeError, ValueError):
        return ''
    parts = []
    if fade_in:
        parts.append(f"afade=t=in:st=0:d={d:.3f}")
    if fade_out:
        st = max(0.0, _dur - d)
        parts.append(f"afade=t=out:st={st:.3f}:d={d:.3f}")
    return ',' + ','.join(parts)


def process_video(input_file, output_file, target_duration, encoder_type, codec, bitrate,
                   logo_path=None, blur_borders=False, keep_audio=False, upscale_1080=False, fps=60,
                   is_chain_extend=False, real_photo_meta=None, upscale_4k=False,
                   zoom_style='in-center', fade_in=False, fade_out=False, transition_duration=0.5,
                   static_no_motion=False, target_size=None):
    """Обрабатывает видео с ретаймингом до нужной длительности + точная обрезка.
    Если input_file — изображение, создаёт зум-видео нужной длины (zoom_style — см. ZOOM_STYLES).
    keep_audio=True  — сохраняет VEO аудио в ретаймленном фрагменте (для script-mode без нарации).
    is_chain_extend=True — trim first N frames (duplicate from prev clip's last_frame) + mute audio (VEO artifacts).
    real_photo_meta — dict с {credit_text, zoom_direction} для архивных кадров (real_photo stage).
    target_size — целевой размер кадра (w, h) на весь прогон: клип приводится к нему в
    ЛЮБУЮ сторону, а при совпадении размера масштаб не применяется вовсе. Флаги
    upscale_1080/upscale_4k оставлены для старых вызовов и работают как раньше."""
    input_path = Path(input_file)

    # Chain extend: configurable trim offset (skip first N frames, fps-independent)
    _trim_frames = int(os.getenv('VIDEO_EXTEND_TRIM_FRAMES', '1'))

    # Still image to timeline-compatible video.
    if input_path.suffix.lower() in _IMAGE_EXTENSIONS:
        try:
            _credit = (real_photo_meta or {}).get('credit_text') if real_photo_meta else None
            _blur_fill = bool(real_photo_meta)  # blurred bg only for real_photo clips
            _image_to_zoom_video(input_file, output_file, target_duration,
                                 encoder_type, codec, bitrate, logo_path, fps=fps,
                                 keep_audio=keep_audio,
                                 credit_text=_credit, blur_fill=_blur_fill,
                                 upscale_1080=upscale_1080, upscale_4k=upscale_4k,
                                 zoom_style=zoom_style,
                                 fade_in=fade_in, fade_out=fade_out,
                                 transition_duration=transition_duration,
                                 static_no_motion=static_no_motion,
                                 target_size=target_size)
            rendered_duration = get_video_duration(output_file)
            if rendered_duration > 0:
                return rendered_duration
            raise RuntimeError("FFmpeg created no valid video duration")
        except Exception as exc:
            _remove_file_safely(output_file)
            logger.warning(f"Still-image render failed for {input_file}: {exc}")
            return None

    logger.debug(f"Ретайминг {input_file} → {output_file} (целевая длительность: {target_duration:.2f} сек)")

    output_path = Path(output_file)
    temp_output = output_path.with_suffix('.temp.mp4')

    try:
        result = subprocess.run(
            [FFPROBE, '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', input_file],
            capture_output=True, text=True, encoding='utf-8', errors='replace', **SUBPROCESS_FLAGS
        )
        _dur_str = result.stdout.strip()
        if not _dur_str:
            logger.warning(f"ffprobe returned empty duration for {input_file} — file may be corrupt, skipping")
            return None
        current_duration = float(_dur_str)

        # Trim mode: короткие целевые длительности обрезаем вместо ускорения.
        # Если source достаточно длинный — speed_factor=1.0 → setpts=PTS/1.0 (no-op) +
        # -t target_duration обрежет хвост без перекодирования темпа.
        # Если source короче target — нормальный ретайм (замедление).
        if target_duration <= TRIM_MAX_DURATION and current_duration >= target_duration:
            speed_factor = 1.0
            logger.info(f"Trim mode: target {target_duration:.2f}с ≤ {TRIM_MAX_DURATION}с, source {current_duration:.2f}с → обрезка без ускорения")
        else:
            speed_factor = current_duration / target_duration
            if speed_factor > MAX_SPEEDUP:
                # -t target_duration ниже обрежет то, что не влезло
                logger.info(f"Ускорение {speed_factor:.2f}x ограничено до {MAX_SPEEDUP}x, хвост обрезается")
                speed_factor = MAX_SPEEDUP

        logger.debug(f"Текущая длительность: {current_duration:.2f} сек, speed_factor: {speed_factor:.4f}")

        encoder_params = get_encoder_params(encoder_type, codec, bitrate)

        blur_suffix = ""

        # Приведение к целевому размеру. Lanczos — плавная интерполяция в обе стороны.
        # target_size задан → цель одна на весь прогон, и клип подгоняется к ней вверх
        # ИЛИ вниз; совпал — не трогаем, лишний scale только жрёт время и мылит кадр.
        if target_size:
            _sw, _sh = int(target_size[0]), int(target_size[1])
            _cw, _ch = get_video_resolution(input_file)
            if (_cw, _ch) == (_sw, _sh):
                scale_suffix = ""
            else:
                scale_suffix = (
                    f",scale={_sw}:{_sh}:force_original_aspect_ratio=decrease:flags=lanczos"
                    f",pad={_sw}:{_sh}:(ow-iw)/2:(oh-ih)/2,setsar=1"
                )
        elif upscale_4k:
            _sw, _sh = oriented(3840, 2160)
            scale_suffix = f",scale={_sw}:{_sh}:flags=lanczos"
        elif upscale_1080:
            _sw, _sh = oriented(1920, 1080)
            scale_suffix = f",scale={_sw}:{_sh}:flags=lanczos"
        else:
            scale_suffix = ""

        # ШАГ 1: Ретайминг с перекодированием (+ overlay лого если задан)
        # keep_audio=False (по умолчанию): аудио стрипается (-an), заменяется нарацией в Этапе 8.
        # keep_audio=True (script-mode): аудио ретаймится через atempo и сохраняется в итоговом видео.
        # is_chain_extend=True: force mute audio (VEO artifacts on extend clips)
        if is_chain_extend:
            audio_args = ['-an']
            keep_audio = False  # Override for chain extends
        elif keep_audio:
            # Audio просто плавно приглушается/нарастает на границе (у afade нет 'формы' перехода)
            _afade = _afade_filter_suffix(bool(fade_in), bool(fade_out), target_duration, transition_duration)
            audio_args = ['-filter:a', _build_atempo(speed_factor) + _afade]
        else:
            audio_args = ['-an']

        # Единая цепочка: setpts/blur/scale → [base], опц. overlay лого → [logoed], опц. dip-to-X переход.
        # Chain extend: select='gt(n,N-1)' skips first N frames (fps-independent, configurable)
        if is_chain_extend:
            _n_thresh = _trim_frames - 1
            _base_chain = f"select='gt(n,{_n_thresh})',setpts=(PTS-STARTPTS)/{speed_factor}{blur_suffix}{scale_suffix}"
        else:
            _base_chain = f'setpts=PTS/{speed_factor}{blur_suffix}{scale_suffix}'

        input_args = ['-i', input_file]
        _next_idx = 1
        _fparts = [f"[0:v]{_base_chain}[base]"]
        _cur_label = "base"

        if logo_path:
            # Лого накладывается AS-IS (в исходных размерах PNG). Юзер сам решает размер.
            input_args = input_args + ['-i', str(logo_path)]
            _next_idx = 2
            _fparts.append(f"[{_cur_label}][1:v]overlay=W-w-4:H-h-4[logoed]")
            _cur_label = "logoed"


        # Dip-to-X переход на границе клипа (native fade или xfade против синтетического клипа).
        # Без fade_in/fade_out — no-op (граф не добавляет ничего, поведение как раньше).
        _tb = _TransitionGraphBuilder(_next_idx, fps)
        _cur_label = _tb.apply(_cur_label, fade_in, 'in', target_duration, transition_duration)
        _cur_label = _tb.apply(_cur_label, fade_out, 'out', target_duration, transition_duration)
        _fparts.extend(_tb.filter_parts)

        video_filter = ';'.join(_fparts)
        map_args = ['-map', f'[{_cur_label}]']
        if keep_audio:
            map_args += ['-map', '0:a?']
        cmd = [FFMPEG, '-y', '-nostdin'] + input_args + _tb.extra_inputs + [
               '-filter_complex', video_filter,
               ] + map_args + audio_args + [
               '-t', f'{target_duration:.6f}',
               '-r', str(fps)] + encoder_params + [str(temp_output)]

        subprocess.run(cmd, check=True, capture_output=True, **SUBPROCESS_FLAGS)

        # Переименовываем temp → final
        if output_path.exists():
            output_path.unlink()
        temp_output.rename(output_path)

        result_duration = get_video_duration(output_path)
        diff = abs(result_duration - target_duration)
        logger.info(f"Clip {os.path.basename(input_file)}: {current_duration:.2f}с → {result_duration:.2f}с (цель {target_duration:.2f}с, разница {diff:.3f}с)")

        logger.debug(f"Ретайминг завершен: {output_file}")
        return result_duration
    except subprocess.CalledProcessError as e:
        # ffmpeg упал на перекодировке — файл частично битый (ffprobe duration
        # прочитал, но декодер не может обработать содержимое). Возвращаем None,
        # чтобы наружный fallback в main() подхватил картинку из generated/.
        logger.warning(f"ffmpeg failed on {input_file} (CalledProcessError) — triggering image fallback")
        try:
            if temp_output.exists():
                temp_output.unlink()
        except:
            pass
        return None
    except Exception as e:
        logger.error(f"Ошибка ретайминга {input_file}: {e}")
        logger.error(traceback.format_exc())
        # Пробуем очистить temp файл
        try:
            if temp_output.exists():
                temp_output.unlink()
        except:
            pass
        raise

def _resolve_clip_source(clip):
    """Find the preferred source for a montage clip in the current project."""
    clip_id = clip['id']
    for fmt in (f'{clip_id:03d}', str(clip_id)):
        for ext in ('mp4', 'mov', 'png', 'jpg', 'jpeg', 'webp', 'bmp', 'tiff', 'tif'):
            candidate = Path('vid_img') / f'{fmt}.{ext}'
            if candidate.exists():
                return str(candidate)

    clip_file = clip.get('file')
    if clip_file and Path(clip_file).exists():
        return clip_file

    for fmt in (f'{clip_id:03d}', str(clip_id)):
        for ext in ('png', 'jpg', 'jpeg', 'webp', 'bmp', 'tiff', 'tif'):
            candidate = Path('generated') / f'{fmt}.{ext}'
            if candidate.exists():
                return str(candidate)
    return None


def _resolve_clip_parts(clip):
    """Куски одной сцены, если она собрана из нескольких файлов.

    Куски лежат рядом с обычными клипами: `001_1.mp4`, `001_2.mp4`.

    Стадия YouTube нарезает сцену на отрезки по несколько секунд. Раньше она
    сама склеивала их в один файл: каждый отрезок перекодировался под целевой
    формат, потом склейка кодировала результат ВТОРОЙ раз, а здесь он же
    приводился к формату в ТРЕТИЙ. На пятисекундную сцену уходило под минуту
    (замер 27.08). Теперь стадия отдаёт отрезки как есть, а приведение к формату
    случается ровно один раз — здесь, где и так делаются ретайминг, масштаб,
    fps и кодек.

    Возвращает список путей либо пустой список, когда сцена из одного файла.
    """
    parts = clip.get('parts')
    resolved = []
    if isinstance(parts, (list, tuple)) and parts:
        for item in parts:
            path = Path(str(item))
            if not path.is_file():
                logger.warning(f"Clip {clip.get('id')}: кусок не найден ({item}) — "
                               "ищу куски на диске")
                resolved = []
                break
            resolved.append(str(path))
    if not resolved:
        # Плана может не быть под рукой (ручной перезапуск склейки, старый план),
        # поэтому куски ищутся и просто по папке.
        parts_dir = Path('vid_img')
        if parts_dir.is_dir():
            stem = f"{int(clip['id']):03d}"
            found = sorted(
                (f for ext in ('.mp4', '.mov', '.mkv', '.webm')
                 for f in parts_dir.glob(f"{stem}_*{ext}")),
                key=lambda f: int(f.stem.rsplit('_', 1)[-1])
                if f.stem.rsplit('_', 1)[-1].isdigit() else 0,
            )
            resolved = [str(f) for f in found if f.stat().st_size > 1024]
    return resolved if len(resolved) >= 2 else []


def _process_clip_parts(parts, output_file, target_duration, temp_dir,
                        encoder_type, codec, bitrate, **kwargs):
    """Собрать сцену из нескольких кусков: каждый своей длиной, затем склейка.

    Доля куска считается по его ФАКТИЧЕСКОЙ длине: отрезки бывают неравными, и
    деление поровну растянуло бы один и сжало другой.

    Переход на границе сцены достаётся крайним кускам: fade_in — первому,
    fade_out — последнему; внутри сцены переходов нет.

    Зум трогает только картиночные куски — так устроен `process_video`, и это
    ровно нужное поведение: в сцене «ютуб + архивное фото» наезд получит одно
    фото (решение пользователя 27.08).
    """
    durations = [get_video_duration(part) or 0.0 for part in parts]
    total = sum(durations)
    if total <= 0:
        durations = [1.0] * len(parts)
        total = float(len(parts))

    fade_in = kwargs.pop('fade_in', None)
    fade_out = kwargs.pop('fade_out', None)
    work_dir = Path(temp_dir) / f'_parts_{Path(output_file).stem}'
    work_dir.mkdir(parents=True, exist_ok=True)
    rendered = []
    produced = 0.0
    try:
        for index, part in enumerate(parts):
            # Последнему куску отдаём весь остаток: доли округляются, и без
            # этого сцена не добрала бы нескольких сотых секунды.
            if index == len(parts) - 1:
                piece_target = max(0.05, target_duration - produced)
            else:
                piece_target = max(0.05, target_duration * (durations[index] / total))
            piece_out = work_dir / f'{index + 1:02d}.mp4'
            actual = process_video(
                part, str(piece_out), piece_target, encoder_type, codec, bitrate,
                fade_in=fade_in if index == 0 else None,
                fade_out=fade_out if index == len(parts) - 1 else None,
                **kwargs,
            )
            if actual is None:
                logger.warning(f"Кусок {part} не обработан — сцена соберётся из остальных")
                continue
            rendered.append(str(piece_out))
            produced += actual
        if not rendered:
            return None
        if len(rendered) == 1:
            Path(rendered[0]).replace(output_file)
            return get_video_duration(output_file)

        # После process_video куски одного формата — склейка копированием.
        list_path = work_dir / 'parts.txt'
        with open(list_path, 'w', encoding='utf-8') as handle:
            for item in rendered:
                safe = str(Path(item).absolute()).replace("'", r"'\''")
                handle.write(f"file '{safe}'\n")
        cmd = [FFMPEG, '-y', '-nostdin', '-f', 'concat', '-safe', '0',
               '-i', str(list_path), '-c', 'copy', '-reset_timestamps', '1',
               str(output_file)]
        subprocess.run(cmd, check=True, capture_output=True, **SUBPROCESS_FLAGS)
        result = get_video_duration(output_file)
        logger.info(f"Сцена собрана из {len(rendered)} кус.: {result:.2f}с "
                    f"(цель {target_duration:.2f}с)")
        return result
    except subprocess.CalledProcessError as exc:
        logger.warning(f"Склейка кусков сцены не удалась: {exc}")
        return None
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _same_file(left, right):
    try:
        return Path(left).resolve() == Path(right).resolve()
    except OSError:
        return str(left) == str(right)


def _iter_clip_still_fallbacks(clip_id, source_file):
    """Yield alternate still images for this clip without repeating source_file."""
    source_path = Path(source_file)
    directories = [
        Path('generated'),
        source_path.parent.parent / 'generated',
        Path('vid_img'),
    ]
    seen = set()
    for directory in directories:
        for fmt in (f'{clip_id:03d}', str(clip_id)):
            for ext in ('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tiff', '.tif'):
                candidate = directory / f'{fmt}{ext}'
                key = str(candidate.resolve()) if candidate.exists() else str(candidate)
                if (
                    key in seen
                    or not candidate.is_file()
                    or candidate.stat().st_size == 0
                    or _same_file(candidate, source_file)
                ):
                    continue
                seen.add(key)
                yield str(candidate)


def _render_still_fallback(
    input_file, output_file, target_duration, encoder_type, codec, bitrate, *,
    logo_path=None, fps=60, keep_audio=False, credit_text=None, blur_fill=False,
    upscale_1080=False, upscale_4k=False, fade_in=False, fade_out=False,
    transition_duration=0.5, zoom_style='in-center',
    static_no_motion=False, target_size=None,
):
    try:
        _remove_file_safely(output_file)
        _image_to_zoom_video(
            input_file, output_file, target_duration, encoder_type, codec, bitrate,
            logo_path=logo_path, fps=fps, keep_audio=keep_audio,
            credit_text=credit_text, blur_fill=blur_fill,
            upscale_1080=upscale_1080, upscale_4k=upscale_4k,
            fade_in=fade_in, fade_out=fade_out,
            transition_duration=transition_duration, zoom_style=zoom_style,
            static_no_motion=static_no_motion,
            target_size=target_size,
        )
        rendered_duration = get_video_duration(output_file)
        if rendered_duration > 0:
            return rendered_duration
    except Exception as exc:
        logger.warning(f"Still fallback failed for {input_file}: {exc}")
    _remove_file_safely(output_file)
    return None


def _iter_neighbor_sources(clips, clip_idx, existing_files):
    """Yield nearby media, preferring the previous clip on equal distance."""
    for distance in range(1, len(clips)):
        previous_idx = clip_idx - distance
        if previous_idx >= 0:
            previous_id = clips[previous_idx]['id']
            previous_output = existing_files.get(previous_id)
            if previous_output and Path(previous_output).is_file():
                yield previous_output, True

        next_idx = clip_idx + distance
        if next_idx < len(clips):
            next_id = clips[next_idx]['id']
            next_output = existing_files.get(next_id)
            if next_output and Path(next_output).is_file():
                yield next_output, False
            else:
                next_source = _resolve_clip_source(clips[next_idx])
                if next_source:
                    yield next_source, False


def _render_neighbor_hold(
    source_file, output_file, target_duration, encoder_type, codec, bitrate, *,
    use_last_frame, logo_path=None, fps=60, keep_audio=False,
    upscale_1080=False, upscale_4k=False, fade_in=False, fade_out=False,
    transition_duration=0.5, target_size=None,
):
    """Render one nearby frame for a failed clip without changing its duration."""
    source_path = Path(source_file)
    if source_path.suffix.lower() in _IMAGE_EXTENSIONS:
        return _render_still_fallback(
            source_file, output_file, target_duration, encoder_type, codec, bitrate,
            logo_path=logo_path, fps=fps, keep_audio=keep_audio,
            upscale_1080=upscale_1080, upscale_4k=upscale_4k,
            fade_in=fade_in, fade_out=fade_out,
            transition_duration=transition_duration,
            static_no_motion=True,
            target_size=target_size,
        )

    recovery_dir = Path(output_file).parent / '_concat_stills'
    fingerprint = hashlib.sha1(
        f"{source_path.resolve()}:{source_path.stat().st_size}:{source_path.stat().st_mtime_ns}".encode(
            'utf-8', 'surrogatepass')
    ).hexdigest()[:16]
    frame_path = recovery_dir / f"{Path(output_file).stem}_hold_{fingerprint}.png"
    try:
        recovery_dir.mkdir(parents=True, exist_ok=True)
        command = [FFMPEG, '-y', '-nostdin']
        if use_last_frame:
            command += ['-sseof', '-0.25']
        command += ['-i', str(source_path), '-frames:v', '1', str(frame_path)]
        subprocess.run(command, check=True, capture_output=True, **SUBPROCESS_FLAGS)
        return _render_still_fallback(
            str(frame_path), output_file, target_duration, encoder_type, codec, bitrate,
            logo_path=logo_path, fps=fps, keep_audio=keep_audio,
            upscale_1080=upscale_1080, upscale_4k=upscale_4k,
            fade_in=fade_in, fade_out=fade_out,
            transition_duration=transition_duration,
            static_no_motion=True,
            target_size=target_size,
        )
    except Exception as exc:
        logger.warning(f"Neighbor-frame recovery failed for {source_file}: {exc}")
        return None
    finally:
        _remove_file_safely(frame_path)


def group_concat(file_list, output_file, temp_dir, group_size=9999, sub_path=None, encoder_type='cpu', bitrate='10M', total_duration=None):
    """ГРУППОВАЯ СКЛЕЙКА"""
    logger.info("ЭТАП 5: ГРУППОВАЯ СКЛЕЙКА")
    logger.info(f"Всего файлов: {len(file_list)}, размер группы: {group_size}")

    print(f"\n🔗 ГРУППОВАЯ СКЛЕЙКА {len(file_list)} файлов...")
    print(f"   💡 Размер группы: {group_size} файлов")

    num_groups = (len(file_list) + group_size - 1) // group_size
    print(f"   📊 Групп: {num_groups}")
    print()

    chunk_files = []
    start_time = time.time()

    print(f"📦 Этап 1: Склейка групп")
    for group_idx in range(num_groups):
        start_idx = group_idx * group_size
        end_idx = min(start_idx + group_size, len(file_list))
        group_files = file_list[start_idx:end_idx]

        chunk_file = temp_dir / f'chunk_{group_idx+1:02d}.mp4'

        logger.info(f"Склейка группы {group_idx+1}/{num_groups}: файлы {start_idx+1}-{end_idx}")

        concat_list = temp_dir / f'group_{group_idx+1}.txt'
        with open(concat_list, 'w', encoding='utf-8') as f:
            for fpath in group_files:
                # Escape ' in path for ffmpeg concat demuxer (breaks on apostrophes in project names)
                _p = str(Path(fpath).absolute()).replace("'", r"'\''")
                f.write(f"file '{_p}'\n")

        print(f"   Группа {group_idx+1}/{num_groups}: файлы {start_idx+1}-{end_idx} → chunk_{group_idx+1:02d}.mp4")

        cmd = [FFMPEG, '-y', '-nostdin', '-f', 'concat', '-safe', '0',
               '-i', str(concat_list), '-c', 'copy', '-reset_timestamps', '1', str(chunk_file)]

        group_start = time.time()
        try:
            # TTY (BAT) → реальный tqdm прогресс-бар; IS_PIPE (GUI) → простой текст
            run_ffmpeg_with_progress(cmd, None, f"Склейка {group_idx+1}/{num_groups}")

            elapsed = time.time() - group_start
            print(f"   ✅ Склейка группы {group_idx+1}/{num_groups} ({elapsed:.1f}с)")
            chunk_files.append(str(chunk_file))
            concat_list.unlink()
            logger.info(f"Группа {group_idx+1} склеена за {elapsed:.1f}с")
        except subprocess.CalledProcessError as e:
            print(f"\n   ❌")
            logger.error(f"Ошибка склейки группы {group_idx+1}: {e}")
            logger.error(traceback.format_exc())
            raise

    print()

    # Optimisation: if only 1 chunk and no subtitles — just rename, skip redundant ffmpeg pass
    if len(chunk_files) == 1 and not sub_path:
        import shutil as _shutil
        _shutil.move(str(chunk_files[0]), str(output_file))
        print(f"   ✅ Единственный чанк → финальный файл (без пересклейки)")
        logger.info("Единственный чанк — перемещён напрямую, ffmpeg не нужен")
    else:
        print(f"🔗 Этап 2: Склейка {len(chunk_files)} чанков...")
        logger.info(f"Склейка {len(chunk_files)} чанков в финальный файл")

        final_concat_list = temp_dir / 'final_chunks.txt'
        with open(final_concat_list, 'w', encoding='utf-8') as f:
            for chunk in chunk_files:
                _p = str(Path(chunk).absolute()).replace("'", r"'\''")
                f.write(f"file '{_p}'\n")

        if sub_path:
            # FFmpeg ass= filter chokes on non-ASCII paths — copy to system temp (guaranteed ASCII)
            # Also set fontsdir= so libass finds Book Antiqua from engine/fonts/
            import shutil as _shutil, tempfile as _tempfile
            _ass_src = Path(sub_path).absolute()
            _ass_safe = Path(_tempfile.gettempdir()) / '_begoframe_subs.ass'
            _shutil.copy2(str(_ass_src), str(_ass_safe))
            _ass_escaped = _escape_ffmpeg_filter_path(_ass_safe)
            # Шрифты лежат в engine/fonts рядом с assemble.py
            _fonts_dir = None
            for _cand in [
                Path(__file__).parent / 'fonts',
                Path(__file__).parent.parent / 'engine' / 'fonts',
            ]:
                if _cand.exists():
                    _fonts_dir = _cand
                    break
            if _fonts_dir:
                _fd_escaped = _escape_ffmpeg_filter_path(_fonts_dir)
                _vf = _build_ass_filter(_ass_escaped, _fd_escaped)
                logger.info(f"Subtitle fontsdir: {_fonts_dir}")
            else:
                _vf = _build_ass_filter(_ass_escaped)
            if encoder_type == 'amd':
                _vcodec = ['-c:v', 'h264_amf', '-quality', 'quality', '-b:v', bitrate]
            elif encoder_type == 'nvidia':
                _vcodec = ['-c:v', 'h264_nvenc', '-preset', 'p4', '-b:v', bitrate]
            else:
                _vcodec = ['-c:v', 'libx264', '-preset', 'slow', '-crf', '18']
            print(f"   [SUBTITLES] Burning {Path(sub_path).name} into video (re-encoding)...")
            logger.info(f"Subtitle burn-in: encoder={encoder_type} file={sub_path}")
            cmd = [FFMPEG, '-y', '-nostdin', '-f', 'concat', '-safe', '0',
                   '-i', str(final_concat_list), '-vf', _vf] + _vcodec + [
                   '-c:a', 'copy', '-reset_timestamps', '1', str(output_file)]
            # Re-encode with progress bar (reads stdout line-by-line, no RAM buildup)
            try:
                run_ffmpeg_with_progress(cmd, total_duration, description="Subtitle burn-in")
            except subprocess.CalledProcessError:
                if not _fonts_dir:
                    raise
                # Some FFmpeg/libass builds reject the optional
                # fontsdir argument even though the ass filter is available.
                logger.warning("Subtitle burn-in failed with fontsdir; retrying without fontsdir")
                print("   [SUBTITLES] fontsdir unsupported; retrying with system font fallback...")
                _remove_file_safely(output_file)
                fallback_cmd = list(cmd)
                fallback_cmd[fallback_cmd.index('-vf') + 1] = _build_ass_filter(_ass_escaped)
                run_ffmpeg_with_progress(
                    fallback_cmd, total_duration,
                    description="Subtitle burn-in (font fallback)")
            final_concat_list.unlink()
        else:
            cmd = [FFMPEG, '-y', '-nostdin', '-f', 'concat', '-safe', '0',
                   '-i', str(final_concat_list), '-c', 'copy', '-reset_timestamps', '1', str(output_file)]
            try:
                # -c copy is fast — stderr is small, but still write to file not RAM
                _ff_log = Path(output_file).parent / 'ffmpeg_concat.log'
                with open(_ff_log, 'w') as _fl:
                    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=_fl, **SUBPROCESS_FLAGS)
                final_concat_list.unlink()
            except subprocess.CalledProcessError as e:
                logger.error(f"Ошибка финальной склейки: {e}")
                if _ff_log.exists():
                    _tail = _ff_log.read_text(errors='replace')[-2000:]
                    logger.error(f"FFmpeg stderr (tail): {_tail}")
                logger.error(traceback.format_exc())
                raise

        print(f"   ✅ Готово!")
        logger.info("Финальная склейка чанков завершена")

        for chunk in chunk_files:
            Path(chunk).unlink()

    elapsed = time.time() - start_time
    final_size = get_file_size_mb(output_file)

    logger.info(f"Групповая склейка завершена: размер {final_size:.0f} MB, время {elapsed/60:.1f} мин")

    print(f"\n   ✅ Склейка завершена!")
    print(f"      💾 Размер: {final_size:.0f} MB")
    print(f"      ⏱️  Время: {elapsed/60:.1f} мин")
    print()

def run_ffmpeg_with_spinner(cmd, description="Обработка"):
    """
    Запускает FFmpeg со спиннером (анимация ожидания).
    Stdout дренируется в фоновом потоке чтобы не было pipe deadlock.
    """
    logger.debug(f"Запуск FFmpeg со спиннером: {description}")

    process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL,
                               **SUBPROCESS_FLAGS)

    spinners = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
    start_time = time.time()
    spinner_idx = 0
    last_refresh = start_time

    try:
        while process.poll() is None:
            now = time.time()
            if now - last_refresh >= 0.1:
                elapsed = now - start_time
                sys.stdout.write(f'\r   {spinners[spinner_idx]} {description}: {elapsed:.1f}с')
                sys.stdout.flush()
                spinner_idx = (spinner_idx + 1) % len(spinners)
                last_refresh = now
            time.sleep(0.05)

        elapsed = time.time() - start_time
        sys.stdout.write(f'\r   ✅ {description} завершено за {elapsed:.1f}с   \n')
        sys.stdout.flush()
        logger.info(f"{description} завершено за {elapsed:.1f}с")

        if process.returncode != 0:
            logger.error(f"FFmpeg завершился с ошибкой: returncode={process.returncode}")
            raise subprocess.CalledProcessError(process.returncode, cmd)

    except KeyboardInterrupt:
        process.kill()
        sys.stdout.write(f'\r   ❌ {description} прервано   \n')
        sys.stdout.flush()
        raise


def _escape_ffmpeg_filter_path(path) -> str:
    """Escape a path for a quoted FFmpeg filter option."""
    return (str(path).replace('\\', '/')
            .replace(':', '\\:')
            .replace("'", "\\'"))


def _build_ass_filter(ass_path: str, fonts_dir: str = None) -> str:
    result = f"ass=filename='{ass_path}'"
    if fonts_dir:
        result += f":fontsdir='{fonts_dir}'"
    return result


def run_ffmpeg_with_progress(cmd, total_duration, description="Processing"):
    """Запускает FFmpeg с прогресс-баром"""
    logger.debug(f"Запуск FFmpeg с прогресс-баром: {description}")

    import tempfile

    cmd_with_progress = cmd + ['-progress', 'pipe:1']
    stderr_file = tempfile.TemporaryFile(
        mode='w+', encoding='utf-8', errors='replace')
    process = subprocess.Popen(
        cmd_with_progress, stdout=subprocess.PIPE, stderr=stderr_file,
        universal_newlines=True, encoding='utf-8', errors='replace',
        **SUBPROCESS_FLAGS)

    start_time = time.time()
    got_any_progress = False
    last_refresh = start_time

    if total_duration is None or total_duration <= 0:
        bar_fmt = None
        tqdm_total = None
    else:
        tqdm_total = round(total_duration, 1)
        bar_fmt = None

    if IS_PIPE:
        # Pipeline mode: drain progress lines, печатаем простой текст каждые 5с
        last_time = 0
        last_pipe_print = start_time
        for line in process.stdout:
            line = line.strip()
            if line.startswith('out_time_us=') or line.startswith('out_time_ms='):
                try:
                    val = line.split('=')[1]
                    if val and val != 'N/A':
                        time_s = int(val) / 1000000.0
                        if time_s > last_time:
                            last_time = time_s
                            got_any_progress = True
                            if tqdm_total and time.time() - last_pipe_print >= 5.0:
                                pct = min(100, int(time_s / tqdm_total * 100))
                                print(f"   {description}: {pct}% ({time_s:.0f}/{tqdm_total:.0f}s)")
                                last_pipe_print = time.time()
                except (ValueError, IndexError):
                    pass
    else:
        # TTY mode: tqdm прогресс-бар
        tqdm_kwargs = dict(total=tqdm_total, desc=description, unit="s")
        if bar_fmt:
            tqdm_kwargs['bar_format'] = bar_fmt
        with tqdm(**tqdm_kwargs) as pbar:
            last_time = 0
            for line in process.stdout:
                line = line.strip()
                if line.startswith('out_time_us=') or line.startswith('out_time_ms='):
                    try:
                        val = line.split('=')[1]
                        if val and val != 'N/A':
                            time_s = int(val) / 1000000.0
                            delta = time_s - last_time
                            if delta > 0:
                                pbar.update(delta)
                                last_time = time_s
                                got_any_progress = True
                    except (ValueError, IndexError):
                        pass
                elif line.startswith('out_time=') and not got_any_progress:
                    try:
                        val = line.split('=')[1]
                        if val and val != 'N/A':
                            parts = val.split(':')
                            time_s = float(parts[0])*3600 + float(parts[1])*60 + float(parts[2])
                            delta = time_s - last_time
                            if delta > 0:
                                pbar.update(delta)
                                last_time = time_s
                    except (ValueError, IndexError):
                        pass
                now = time.time()
                if now - last_refresh >= 1.0:
                    pbar.refresh()
                    last_refresh = now

    process.wait()
    stderr_file.seek(0)
    stderr_text = stderr_file.read()
    stderr_file.close()

    elapsed = time.time() - start_time
    logger.info(f"{description} завершено за {elapsed:.1f}с (прогресс отслеживался: {got_any_progress})")

    if process.returncode != 0:
        logger.error(f"FFmpeg завершился с ошибкой: returncode={process.returncode}")
        if stderr_text:
            stderr_tail = stderr_text[-8000:]
            logger.error(f"FFmpeg stderr (tail):\n{stderr_tail}")
            last_line = next(
                (line.strip() for line in reversed(stderr_tail.splitlines())
                 if line.strip()), '')
            if last_line:
                print(f"   [FFMPEG] {last_line}")
        raise subprocess.CalledProcessError(process.returncode, cmd)

def ask_delete_temp_folder(temp_dir, no_cleanup=False):
    """Спрашивает пользователя об удалении временной папки"""
    
    # Если --no-cleanup, просто оставляем папку без вопросов
    if no_cleanup:
        logger.info("--no-cleanup: temp папка сохранена")
        print(f"\n🧹 Временная папка: {temp_dir}")
        print(f"   💾 Размер: {sum(f.stat().st_size for f in temp_dir.rglob('*') if f.is_file()) / (1024*1024):.0f} MB")
        print(f"   ✅ Папка сохранена (флаг --no-cleanup)")
        return
    
    print(f"\n🧹 Временная папка: {temp_dir}")
    print(f"   💾 Размер: {sum(f.stat().st_size for f in temp_dir.rglob('*') if f.is_file()) / (1024*1024):.0f} MB")

    # Если stdin не интерактивный (IS_PIPE, subprocess, redirected) — не спрашиваем, удаляем автоматически
    if not sys.stdin.isatty():
        logger.info("stdin не TTY — удаляем temp папку автоматически")
        try:
            shutil.rmtree(temp_dir)
            print(f"   ✅ Папка удалена (auto)")
            logger.info("Временная папка удалена (auto, non-TTY)")
        except Exception as e:
            print(f"   ⚠️  Ошибка удаления: {e}")
        return

    print()
    while True:
        try:
            response = input("Удалить временную папку? (y/n): ").strip().lower()
        except EOFError:
            logger.info("stdin EOF — удаляем temp папку автоматически")
            try:
                shutil.rmtree(temp_dir)
                print(f"\n   ✅ Папка удалена (auto, EOF)")
                logger.info("Временная папка удалена (auto, EOF)")
            except Exception as e:
                print(f"\n   ⚠️  Ошибка удаления: {e}")
            return
        if response in ['y', 'yes', 'д', 'да']:
            logger.info("Пользователь выбрал удалить temp папку")
            try:
                shutil.rmtree(temp_dir)
                print("   ✅ Папка удалена")
                logger.info("Временная папка удалена")
            except Exception as e:
                print(f"   ⚠️  Ошибка удаления: {e}")
                logger.error(f"Ошибка удаления temp папки: {e}")
            break
        elif response in ['n', 'no', 'н', 'нет']:
            logger.info("Пользователь выбрал оставить temp папку")
            print(f"   ✅ Папка сохранена: {temp_dir.absolute()}")
            break
        elif response == '':
            # Enter без ввода — не цикличим, считаем как 'n'
            logger.info("Пустой ввод — temp папка сохранена")
            print(f"   ✅ Папка сохранена")
            break
        else:
            print("   ⚠️  Введите 'y' (удалить) или 'n' (оставить)")

# ============================================================================
# MAIN
# ============================================================================

def main():
    global logger

    # Форсируем line-buffered вывод — без этого BAT накапливает весь вывод и выдаёт разом
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass  # Python < 3.7

    parser = argparse.ArgumentParser(description='Групповая склейка - v6 OVERLAP GUARD')
    parser.add_argument('plan', help='Путь к montage_plan.json')
    parser.add_argument('-o', '--output', default='result.mp4', help='Выходной файл')
    parser.add_argument('-a', '--audio', default=None, help='Аудио озвучка (по умолчанию ищется любой .wav/.mp3 файл)')
    parser.add_argument('-b', '--bitrate', default='10M', help='Битрейт видео')
    parser.add_argument('--attribution', dest='attribution', action='store_true', default=None,
                        help='Плашка источника на кадре («Wikimedia», «Google Images»). '
                             'По умолчанию ВЫКЛЮЧЕНА — надпись портит продакшн-качество; '
                             'сама атрибуция остаётся в real_photos_credits.json')
    parser.add_argument('--no-attribution', dest='attribution', action='store_false',
                        help='Не рисовать плашку источника (поведение по умолчанию)')
    parser.add_argument('-g', '--group-size', type=int, default=9999, help='Размер группы для склейки (по умолчанию 9999 = один проход)')
    parser.add_argument('-r', '--resolution', default=None, help='Разрешение (1080, 720) — опционально, для совместимости')
    parser.add_argument('--aspect', default=None, choices=['16:9', '9:16', '1:1'],
                        help='Формат ролика: 16:9 (default), 9:16 (вертикаль/shorts), 1:1. '
                             'Влияет на целевые разрешения (1080p: 1920x1080 / 1080x1920 / 1080x1080)')
    parser.add_argument('--no-gpu', action='store_true', help='Использовать CPU вместо GPU')
    parser.add_argument('--force-amd', action='store_true', help='Принудительно использовать AMD GPU')
    parser.add_argument('--force-reprocess', action='store_true', help='Переобработать все файлы')
    parser.add_argument('--logo', default=None, help='PNG лого для наложения (правый нижний угол). Если не указан — ищется logo.png в папке проекта')
    parser.add_argument('--no-logo', action='store_true',
                        help='Без логотипа, даже если в папке проекта лежит logo.png (в форме выбрано «без лого»)')
    parser.add_argument('--no-cleanup', action='store_true', help='Не удалять temp_processed после завершения')
    parser.add_argument('--veo-audio-inline', action='store_true', help='Оставить VEO аудио прямо на видеоряде (для script-mode без нарации — итоговый concat будет со звуком VEO)')
    parser.add_argument('--keep-veo-sound', action='store_true',
                        help='Сохранить склеенную VEO-аудиодорожку (звук веогенераций всех клипов, '
                             'ретаймленный, длиной в видео) в папку retimed_sound/ рядом с итоговым видео. '
                             'Видео при этом собирается КАК ОБЫЧНО (с нарацией) — дорожка кладётся отдельно '
                             'для ручного наложения в редакторе (CapCut и т.п.).')
    parser.add_argument('--subtitles', default=None, help='Путь к .ass файлу субтитров для выжигания. Используй "auto" для авто-поиска *.ass в папке проекта.')
    parser.add_argument('--fps', type=int, default=60, choices=[24, 25, 30, 50, 60],
                        help='FPS для зум-видео из изображений и freeze-frame (default=60)')
    parser.add_argument('--usatayai-bridge-manifest', default=None,
                        help='JSON manifest для финальной добивки Usataya Lentyayka. Если не задан, также читается USATAYAI_BRIDGE_MANIFEST.')
    parser.add_argument('--transitions', dest='transitions', action='store_true', default=None,
                        help='Переход между клипами (fade/wipe/dissolve/...): конец клипа гаснет, '
                             'следующий высветляется (каждый клип сохраняет полную длительность, '
                             'без потери времени на склейке)')
    parser.add_argument('--no-transitions', dest='transitions', action='store_false',
                        help='Отключить переходы')
    parser.add_argument('--transition-duration', type=float, default=0.5,
                        help='Длительность fade-in/fade-out перехода в секундах (default=0.5)')
    parser.add_argument('--transition-styles', default='fade',
                        help='Тип(ы) перехода через запятую. Один = фиксированный на всё видео; '
                             'несколько = случайный микс (один и тот же стиль на обеих сторонах стыка). '
                             f'Доступно: {",".join(TRANSITION_STYLES)}')
    parser.add_argument('--zoom-styles', default='in-center',
                        help='Ken Burns zoom-стили для картиночных клипов через запятую '
                        '(in-center, out-left, in-topright, ...). Один = фиксированный на всё '
                        f'видео, несколько = случайный на каждый клип. Доступно: {",".join(ZOOM_STYLES)}')
    parser.add_argument('--static-infographics', dest='static_infographics', action='store_true', default=None,
                        help='Keep clips with source=infographic fully static (no Ken Burns movement)')
    parser.add_argument('--animate-infographics', dest='static_infographics', action='store_false',
                        help='Allow Ken Burns movement for clips with source=infographic')
    args = parser.parse_args()
    if args.static_infographics is None:
        args.static_infographics = (
            os.getenv('INFOGRAPHIC_STATIC_CONCAT', 'true').strip().lower()
            in ('1', 'true', 'yes', 'on')
        )

    # Плашка источника на кадре: CLI перекрывает ATTRIBUTION_OVERLAY из окружения.
    # Кладём в окружение, а не в args: рисует её `_drawtext_real_photo` глубоко
    # в фильтрах, куда args не доходит, и точка решения должна быть одна.
    if args.attribution is not None:
        os.environ['ATTRIBUTION_OVERLAY'] = '1' if args.attribution else '0'

    # Формат ролика: CLI --aspect перекрывает env VIDEO_TARGET_ASPECT (дефолт 16:9)
    global TARGET_ASPECT
    if args.aspect:
        TARGET_ASPECT = args.aspect

    # НАСТРОЙКА ЛОГГИРОВАНИЯ
    logger, log_file = setup_logging()

    logger.info(f"Параметры запуска:")
    logger.info(f"  aspect: {TARGET_ASPECT}")
    logger.info(f"  plan: {args.plan}")
    logger.info(f"  output: {args.output}")
    logger.info(f"  audio: {args.audio}")
    logger.info(f"  bitrate: {args.bitrate}")
    logger.info(f"  group_size: {args.group_size}")
    logger.info(f"  resolution: {args.resolution}")
    logger.info(f"  no_gpu: {args.no_gpu}")
    logger.info(f"  force_amd: {args.force_amd}")
    logger.info(f"  force_reprocess: {args.force_reprocess}")
    logger.info(f"  logo: {args.logo}")
    logger.info(f"  no_cleanup: {args.no_cleanup}")
    logger.info(f"  veo_audio_inline: {args.veo_audio_inline}")
    logger.info(f"  subtitles: {args.subtitles}")
    logger.info(f"  usatayai_bridge_manifest: {args.usatayai_bridge_manifest or os.environ.get('USATAYAI_BRIDGE_MANIFEST') or ''}")

    print("="*80)
    print("[VIDEO] ГРУППОВАЯ СКЛЕЙКА - v6 OVERLAP GUARD")
    print("="*80)
    print(f"\n[LOG] Лог-файл: {log_file}")

    # Авто-rename vid_img/ и generated/ в 3-значный формат перед склейкой.
    # Источники генераций часто пишут файлы с длинными именами
    # (banana_0001_Late Renaissance_...png, veo3_generation_abc123.mp4 и т.п.),
    # склейка ожидает 001.mp4 / 001.png. rename_videos.py идемпотентен —
    # повторный вызов на уже переименованных файлах ничего не делает.
    try:
        _proj_dir = Path(args.plan).resolve().parent
        for _sub in ('vid_img', 'generated'):
            _sd = _proj_dir / _sub
            if not _sd.is_dir():
                continue
            print(f"\n[AUTO-RENAME] {_sub}/ → 3-digit format...")
            try:
                import sys as _sys
                _agent_dir = (Path(__file__).resolve().parent / 'agent')
                if str(_agent_dir) not in _sys.path:
                    _sys.path.insert(0, str(_agent_dir))
                from rename_videos import rename_videos as _rv
                _ok, _err, _w = _rv(directory=_sd, dry_run=False, verbose=False, zero_pad=3)
                print(f"  renamed OK={_ok}, errors={_err}, warnings={len(_w)}")
            except Exception as _re:
                print(f"  [WARN] auto-rename failed ({_re}) — продолжаем с тем что есть")
    except Exception:
        pass

    # Определение энкодера
    encoder_type, codec = ('cpu', 'libx264') if args.no_gpu else detect_gpu_encoder(args.force_amd)

    if encoder_type == 'nvidia':
        print("\n[OK] NVIDIA GPU - h264_nvenc")
    elif encoder_type == 'amd':
        print("\n[OK] AMD GPU - h264_amf")
    else:
        print("\n[INFO] CPU - libx264")

    print(f"[INFO] Битрейт: {args.bitrate}")
    print(f"[INFO] План: {args.plan}")

    # Определение пути к .ass субтитрам (если переданы)
    _sub_path = None
    if args.subtitles:
        if args.subtitles.lower() == 'auto':
            _ass_files = _glob.glob('*.ass')
            if _ass_files:
                _sub_path = str(Path(_ass_files[0]).absolute())
                print(f"[SUBTITLES] Auto-detected: {Path(_sub_path).name}")
                logger.info(f"Subtitles auto-detected: {_sub_path}")
            else:
                logger.info("Subtitles: auto mode, no .ass file found in project folder")
        elif os.path.exists(args.subtitles):
            _template_path = str(Path(args.subtitles).absolute())
            # Если передан .ass шаблон — ищем SRT в папке проекта и мержим
            _srt_candidates = [
                Path(args.plan).parent / 'SRT' / 'subtitles.srt',
                Path(args.plan).parent / 'SRT' / 'subtitles_split.srt',
            ]
            _srt_found = next((p for p in _srt_candidates if p.exists()), None)
            if _srt_found:
                _merged_ass = Path(args.plan).parent / 'SRT' / '_subtitles_merged.ass'
                _srt_to_ass(str(_srt_found), _template_path, str(_merged_ass))
                _sub_path = str(_merged_ass.absolute())
                print(f"[SUBTITLES] Template: {Path(_template_path).name} + SRT: {_srt_found.name} → merged")
                logger.info(f"Subtitles merged: {_sub_path}")
            else:
                # Нет SRT — выжигаем шаблон как есть (если в нём уже есть события)
                _sub_path = _template_path
                print(f"[SUBTITLES] Template only (no SRT found): {Path(_sub_path).name}")
                logger.info(f"Subtitles template only: {_sub_path}")
        else:
            print(f"[SUBTITLES] WARNING: file not found: {args.subtitles}")
            logger.warning(f"Subtitles file not found: {args.subtitles}")

    # Загрузка плана
    try:
        logger.info(f"Загрузка плана: {args.plan}")
        with open(args.plan, 'r', encoding='utf-8-sig') as f:
            plan = json.load(f)
        logger.info("План загружен успешно")
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.error(f"Ошибка загрузки плана: {e}")
        logger.error(traceback.format_exc())
        print(f"❌ Ошибка загрузки плана: {e}")
        sys.exit(1)

    clips = plan.get('clips', [])
    if not clips:
        logger.error("Нет clips в плане")
        print("❌ Нет clips в плане!")
        sys.exit(1)

    # -----------------------------------------------------------------------
    # СОРТИРОВКА ПО startTime
    # Режиссёр иногда вставляет клипы не в том порядке в JSON-массив.
    # Сортируем по startTime, чтобы гарантировать правильный порядок склейки.
    # -----------------------------------------------------------------------
    ids_before = [c['id'] for c in clips]
    clips = sorted(clips, key=lambda c: c['startTime'])
    ids_after = [c['id'] for c in clips]

    misplaced = [ids_before[i] for i in range(len(ids_before)) if ids_before[i] != ids_after[i]]
    if misplaced:
        logger.warning(f"Клипы переупорядочены по startTime! Было не на месте: {misplaced}")
        print(f"\n⚠️  ПРЕДУПРЕЖДЕНИЕ: {len(misplaced)} клипов были не в хронологическом порядке в JSON")
        print(f"   Переставлены: {misplaced}")
        print(f"   (режиссёр иногда вставляет клипы не на своё место — теперь исправлено)")
    else:
        logger.info("Порядок клипов по startTime: OK")

    # Поиск аудиофайла если не указан
    audio_file = args.audio
    if audio_file is None:
        logger.debug(f"Audio auto-search in cwd: {Path('.').resolve()}")
        all_audio = []
        for ext in ['*.wav', '*.WAV', '*.mp3', '*.MP3', '*.Mp3',
                    '*.flac', '*.FLAC', '*.m4a', '*.M4A', '*.aac', '*.AAC']:
            found = [f for f in Path('.').glob(ext) if '_original_audio' not in f.name]
            all_audio.extend(found)
            if found and audio_file is None:
                audio_file = str(found[0])
        if all_audio:
            logger.debug(f"Audio files found: {[f.name for f in all_audio]}")
        else:
            logger.debug(f"No audio files found. Files in cwd: {[f.name for f in Path('.').iterdir() if f.is_file()][:20]}")

    # Вход-видео как источник нарации: если аудиофайла нет, но в папке проекта лежит
    # входной .mp4/.mov/... (юзер подал видео, не аудио) — берём его аудиодорожку как
    # нарацию. Раньше concat искал только аудио-расширения и падал на видео-входе.
    # Клипы генерации лежат в vid_img/ (не в корне), поиск в cwd нерекурсивный — их не
    # заденет; итоговый рендер и служебные файлы исключаем по имени.
    if audio_file is None or not os.path.exists(str(audio_file)):
        _out_name = os.path.basename(str(args.output))
        _vid_exts = ['*.mp4', '*.MP4', '*.mov', '*.MOV', '*.mkv', '*.MKV',
                     '*.webm', '*.WEBM', '*.avi', '*.AVI', '*.m4v', '*.M4V']
        _vid_cands = []
        for _vext in _vid_exts:
            for _vf in Path('.').glob(_vext):
                _n = _vf.name
                if _n == _out_name:
                    continue                       # не брать итоговый рендер (re-concat)
                if _n.startswith('temp_') or '_original' in _n or '_narration_from_video' in _n:
                    continue
                _vid_cands.append(_vf)
        if _vid_cands:
            # старейший по mtime = скорее всего входной файл (копируется при создании
            # проекта раньше, чем рендерится выход)
            _src_vid = sorted(_vid_cands, key=lambda f: f.stat().st_mtime)[0]
            _extracted = (Path('.').resolve() / '_narration_from_video.wav')
            try:
                subprocess.run([FFMPEG, '-y', '-nostdin', '-i', str(_src_vid), '-vn',
                                '-acodec', 'pcm_s16le', str(_extracted)],
                               check=True, capture_output=True, **SUBPROCESS_FLAGS)
                if _extracted.exists() and _extracted.stat().st_size > 1000:
                    audio_file = str(_extracted)
                    logger.info(f"Нарация извлечена из входного видео {_src_vid.name} → {_extracted.name}")
                    print(f"\n🔊 Нарация взята из входного видео: {_src_vid.name}")
                else:
                    logger.warning(f"Во входном видео {_src_vid.name} нет аудиодорожки — нарации нет")
            except Exception as _ve:
                logger.warning(f"Не удалось извлечь аудио из {_src_vid.name}: {_ve}")

    if audio_file is None or not os.path.exists(str(audio_file)):
        if getattr(args, 'veo_audio_inline', False):
            logger.warning("Нарация не найдена — veo-audio-inline режим: видео будет БЕЗ нарации, только VEO-звук")
            print(f"\n⚠️  ВНИМАНИЕ: Аудиофайл нарации не найден в папке проекта!")
            print(f"   cwd: {Path('.').resolve()}")
            print(f"   Видео будет собрано БЕЗ нарации (только VEO-звук клипов)")
            print(f"   Чтобы добавить нарацию — положите .mp3/.wav в папку проекта и перезапустите concat\n")
            audio_file = None
        else:
            logger.error(f"Аудио файл не найден")
            print(f"❌ Аудио файл не найден!")
            print(f"   Положите .wav, .mp3, .flac, .m4a или .aac в папку проекта")
            print(f"   Или укажите явно: -a имя_файла.wav")
            sys.exit(1)

    logger.info(f"Используемый аудиофайл: {audio_file}")

    # Поиск лого для наложения (logo.png в текущей папке или --logo)
    logo_path = None
    if args.logo:
        if os.path.exists(args.logo):
            logo_path = Path(args.logo)
            print(f"[INFO] Лого: {logo_path}")
        else:
            print(f"⚠️  Лого не найдено: {args.logo} — продолжаем без лого")
    elif not args.no_logo:
        auto_logo = Path('logo.png')
        if auto_logo.exists():
            logo_path = auto_logo
            print(f"[INFO] Лого найдено: logo.png")
    if logo_path:
        logger.info(f"Лого overlay: {logo_path}")
        # Runtime auto-trim: если в PNG есть прозрачные колонки/строки по краям,
        # они визуально ничего не значат, но сбивают overlay=W-w-4:H-h-4 —
        # ffmpeg кладёт в угол ВЕСЬ файл с padding'ом, а видимый контент висит
        # в отступе = (прозрачная рамка + 4). Обрезаем alpha-bbox → сохраняем
        # в tmp → подменяем logo_path. Для корректно свёрстанных логотипов
        # (без мёртвых полей) bbox == размер файла → никаких изменений.
        try:
            from PIL import Image as _PILImage
            import tempfile as _tempfile
            _im = _PILImage.open(str(logo_path))
            if _im.mode in ('RGBA', 'LA'):
                _bbox = _im.getchannel('A').getbbox()
                if _bbox and _bbox != (0, 0, _im.size[0], _im.size[1]):
                    _trimmed = _im.crop(_bbox)
                    _tmp = Path(_tempfile.gettempdir()) / f'_logo_trimmed_{os.getpid()}.png'
                    _trimmed.save(str(_tmp))
                    _pad_l, _pad_t = _bbox[0], _bbox[1]
                    _pad_r = _im.size[0] - _bbox[2]
                    _pad_b = _im.size[1] - _bbox[3]
                    print(f"[LOGO] Обрезана прозрачная рамка: L={_pad_l} T={_pad_t} R={_pad_r} B={_pad_b} → {_bbox[2]-_bbox[0]}x{_bbox[3]-_bbox[1]}")
                    logger.info(f"Logo trim: bbox={_bbox}, padding L/T/R/B={_pad_l}/{_pad_t}/{_pad_r}/{_pad_b}, saved to {_tmp}")
                    logo_path = _tmp
        except Exception as _e:
            logger.warning(f"Auto-trim лого пропущен: {_e}")
    else:
        logger.info("Лого overlay: не используется")

    try:
        # ЭТАП 1: ВАЛИДАЦИЯ ПЛАНА
        audio_duration = validate_montage_plan(clips, audio_file)

        # ЭТАП 2: АНАЛИЗ ГЭПОВ И ПЕРЕКРЫТИЙ
        corrections = analyze_gaps(clips, audio_duration)

        temp_dir = Path('temp_processed')
        temp_dir.mkdir(exist_ok=True)
        logger.info(f"Создана временная папка: {temp_dir.absolute()}")

        # Применяем корректировки к clips
        corrected_clips = clips.copy()
        for corr in corrections:
            idx = corr['clip_index']
            if corr['type'] == 'start_gap':
                corrected_clips[idx] = {
                    **corrected_clips[idx],
                    'startTime': 0.0,
                    'original_startTime': corrected_clips[idx]['startTime']
                }
            elif corr['type'] == 'end_gap':
                corrected_clips[idx] = {
                    **corrected_clips[idx],
                    'endTime': audio_duration,
                    'original_endTime': corrected_clips[idx]['endTime']
                }
            elif corr['type'] == 'retime_extend':
                corrected_clips[idx] = {
                    **corrected_clips[idx],
                    'endTime': corrected_clips[idx]['endTime'] + corr['gap_size'],
                    'original_endTime': corrected_clips[idx]['endTime']
                }
            elif corr['type'] == 'trim_overlap':
                corrected_clips[idx] = {
                    **corrected_clips[idx],
                    'endTime': corr['new_end'],
                    'original_endTime': corrected_clips[idx]['endTime']
                }

        # Переходы/зум — настраиваем ДО этапа 3 и на общем уровне (не под `if files_to_process`):
        # _transitions_active нужен уже на этапе 3 (кэш готовых файлов), а _boundary_styles/
        # _zoom_style_pool — в цикле обработки клипов, который выполняется даже когда ретаймить
        # нечего. corrected_clips к этому моменту готов, от ретайминга блок не зависит.
        # Переходы (dip-to-X): активны только на 1080/4k (на 720 xfade-стили грубоваты).
        # Стиль — по ГРАНИЦАМ клипов, один и тот же на обе стороны стыка. Один стиль =
        # фиксированный; несколько через запятую = микс.
        _transitions_active = bool(getattr(args, 'transitions', False)) and args.resolution in ('1080', '4k')
        _transition_duration = getattr(args, 'transition_duration', 0.5) or 0.5
        _style_pool = [s.strip() for s in str(getattr(args, 'transition_styles', 'fade') or 'fade').split(',') if s.strip()]
        _style_pool = [s for s in _style_pool if s in TRANSITION_STYLES] or ['fade']
        _n_boundaries = max(0, len(corrected_clips) - 1)
        if len(_style_pool) == 1:
            _boundary_styles = [_style_pool[0]] * _n_boundaries
        else:
            _boundary_styles = [random.choice(_style_pool) for _ in range(_n_boundaries)]
        if _transitions_active:
            logger.info(f"transitions: стиль(и)={_style_pool}, границ={_n_boundaries}")
        # Ken Burns zoom для картиночных клипов: один = фиксированный на всё видео,
        # несколько через запятую = случайный на каждый клип (согласование с соседями не нужно).
        _zoom_style_pool = [s.strip() for s in str(getattr(args, 'zoom_styles', 'in-center') or 'in-center').split(',') if s.strip()]
        _zoom_style_pool = [s for s in _zoom_style_pool if s in ZOOM_STYLES] or ['in-center']
        if _zoom_style_pool != ['in-center']:
            logger.info(f"zoom: стиль(и)={_zoom_style_pool}")

        # Определяем общий размер кадра до проверки processed-кэша.
        _max_prescan_h = 0
        _vid_img_prescan = Path('vid_img')
        if _vid_img_prescan.exists():
            # Куски сцен (`001_1.mp4`) лежат тут же и тоже попадают в префскан.
            for _pf in _vid_img_prescan.glob('*.mp4'):
                _, _ph = get_video_resolution(str(_pf))
                if _ph > _max_prescan_h:
                    _max_prescan_h = _ph
        # Цель одна на весь прогон: явный -r или ступень по префскану. Раньше
        # выбор пользователя мог быть перебит авто-подъёмом до 1080 (выбрал 720 —
        # всё равно уезжало вверх), а привести кадр ВНИЗ было нечем.
        _target_w, _target_h = _target_frame_size(args.resolution, _max_prescan_h)
        _target_size = (_target_w, _target_h)
        _auto_target_1080 = _target_h >= 1080
        if args.resolution:
            logger.info(f"Целевой кадр: {_target_w}x{_target_h} (выбор пользователя: {args.resolution})")
        else:
            logger.info(f"Целевой кадр: {_target_w}x{_target_h} "
                        f"(по исходникам, максимум в проекте {_max_prescan_h}px)")
        print(f"\n[FORMAT] Целевой кадр: {_target_w}x{_target_h}"
              + (f" (выбрано: {args.resolution})" if args.resolution
                 else f" (по исходникам: {_max_prescan_h}px)"))

        # ЭТАП 3: ПРОВЕРКА СУЩЕСТВУЮЩИХ ФАЙЛОВ
        logger.info("ЭТАП 3: ПРОВЕРКА СУЩЕСТВУЮЩИХ ФАЙЛОВ")
        existing_files = {}
        if _transitions_active:
            # fade запечён в файл, но не меняет длительность — кэш по длине не отличит клип с
            # переходом от клипа без. При включённых переходах безопасно только полное переисполнение.
            logger.info("transitions включены — пропускаем кэш готовых файлов (переисполняем всё)")
            print(f"\n🎬 [TRANSITIONS] Переходы включены — переобрабатываем все клипы (кэш не применим)")
        elif not args.force_reprocess:
            print(f"\n🔍 Проверка существующих файлов...")
            _total_clips = len(corrected_clips)
            if IS_PIPE:
                print(f"PROGRESS:0/{_total_clips}", flush=True)
            _check_iter = corrected_clips if IS_PIPE else tqdm(
                corrected_clips, desc="   Checking", unit="clip",
                bar_format="{desc}: {bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]"
            )
            # Параллельный health-check: каждый файл требует 1-3 ffprobe вызовов
            # (duration + optionally resolution + audio_stream). Sequential = ~1с/файл
            # × 596 = ~10 минут. ThreadPool(16) сжимает до ~30-60с.
            import concurrent.futures as _futures

            # Кэш годен, только если готовый клип уже целевого размера.
            _need_check_1080p = True
            _need_check_audio = (getattr(args, 'veo_audio_inline', False)
                                 or getattr(args, 'keep_veo_sound', False))

            def _probe_one(clip):
                clip_id = clip['id']
                target_duration = clip['endTime'] - clip['startTime']
                output_file = temp_dir / f"processed_{clip_id}.mp4"
                if not check_processed_file(output_file):
                    return (clip_id, None, None)
                actual_dur = get_video_duration(output_file)
                if abs(actual_dur - target_duration) >= 0.1:
                    return (clip_id, None, None)
                if _need_check_1080p:
                    w, h = get_video_resolution(output_file)
                    if w != _target_w or h != _target_h:
                        return (clip_id, None,
                                f"not target res ({w}x{h}, want {_target_w}x{_target_h})")
                if _need_check_audio and not has_audio_stream(output_file):
                    return (clip_id, None, "no audio stream in veo-audio-inline mode")
                return (clip_id, str(output_file), None)

            _ci = 0
            with _futures.ThreadPoolExecutor(max_workers=16) as _pool:
                _futs = {_pool.submit(_probe_one, clip): clip for clip in _check_iter}
                for _fut in _futures.as_completed(_futs):
                    _ci += 1
                    clip_id, path, skip_reason = _fut.result()
                    if path is not None:
                        existing_files[clip_id] = path
                        logger.debug(f"Файл {clip_id} уже готов")
                    elif skip_reason:
                        logger.debug(f"Файл {clip_id} {skip_reason}, пересоздаём")
                    if IS_PIPE:
                        print(f"PROGRESS:{_ci}/{_total_clips}", flush=True)

            logger.info(f"Найдено готовых файлов: {len(existing_files)}")
            logger.info(f"Требуют обработки: {len(corrected_clips) - len(existing_files)}")

            print(f"   ✅ Найдено готовых: {len(existing_files)}")
            print(f"   🔄 Требуют обработки: {len(corrected_clips) - len(existing_files)}")
        else:
            logger.info("Режим force_reprocess - все файлы будут переобработаны")
            print(f"\n⚠️  Режим --force-reprocess: переобработка всех файлов")

        # ЭТАП 4: ОБРАБОТКА КЛИПОВ
        logger.info("ЭТАП 4: РЕТАЙМИНГ ФРАГМЕНТОВ")
        files_to_process = []
        for clip in corrected_clips:
            if clip['id'] not in existing_files:
                files_to_process.append(clip)

        # Папка для ретаймленных фрагментов с оригинальным VEO аудио

        # Кумулятивный трекер дрифта: разница Σ(actual) - Σ(planned).
        # При ретайминге каждого следующего клипа вычитаем накопленный дрифт из target_duration,
        # чтобы итоговая сумма клипов точно совпала с длиной аудио (без re-encode в финале).
        cumulative_planned = 0.0   # сумма target_duration из плана
        cumulative_actual  = 0.0   # сумма фактических длин выходных клипов

        if files_to_process:
            logger.info(f"Требуют ретайминга: {len(files_to_process)} файлов")
            print(f"\n🔄 Ретайминг {len(files_to_process)} фрагментов (с коррекцией дрифта)...", flush=True)
            files_to_process_ids = {clip['id'] for clip in files_to_process}
            _retime_total = len(files_to_process)
            _retime_done = 0
            if IS_PIPE:
                print(f"PROGRESS:0/{_retime_total}", flush=True)
                print(f"   Всего к ретаймингу: {_retime_total}", flush=True)
            _retime_pbar = None if IS_PIPE else tqdm(
                total=_retime_total, desc="   Ретайминг", unit="фр",
                bar_format="{desc}: {bar}| {n}/{total} фрагментов [{elapsed}<{remaining}]"
            )
            if _auto_target_1080:
                print(
                    f"   Авто-апскейл: найдены 1080p+ клипы — "
                    "720p клипы будут отмасштабированы до 1080p",
                    flush=True,
                )
            # Итерируем ВСЕ клипы в порядке монтажного плана для корректного трекинга дрифта.
            # Для уже готовых клипов — только читаем фактическую длину и обновляем счётчики.
            for clip_idx, clip in enumerate(corrected_clips):
                    clip_id = clip['id']
                    _clip_zoom_style = random.choice(_zoom_style_pool)
                    # Переход на границе: слева (от clip_idx-1) и справа (к clip_idx+1). None → нет перехода.
                    _fade_in = _boundary_styles[clip_idx - 1] if (_transitions_active and clip_idx > 0) else None
                    _fade_out = _boundary_styles[clip_idx] if (_transitions_active and clip_idx < len(corrected_clips) - 1) else None

                    # Chain extend detection: clip N is extend if chain_id == chain_id of clip N-1
                    is_chain_extend = False
                    if clip_idx > 0 and clip.get('chain_id'):
                        prev_clip = corrected_clips[clip_idx - 1]
                        if prev_clip.get('chain_id') == clip['chain_id']:
                            is_chain_extend = True
                            logger.info(f"Clip {clip_id}: chain extend (chain_id={clip['chain_id']}) — will trim first frame + mute audio")
                    target_duration = clip['endTime'] - clip['startTime']
                    output_file = temp_dir / f"processed_{clip_id}.mp4"

                    if clip_id not in files_to_process_ids:
                        # Файл уже готов — обновляем трекеры без перекодирования
                        actual_dur = get_video_duration(output_file)
                        cumulative_planned += target_duration
                        cumulative_actual  += actual_dur
                        continue

                    # Кумулятивный дрифт: если положительный — мы уже "убежали" вперёд,
                    # нужно укоротить текущий клип; если отрицательный — мы отстаём.
                    drift = cumulative_actual - cumulative_planned
                    adjusted_target = max(0.1, target_duration - drift)
                    if abs(drift) > 0.001:
                        logger.debug(f"Clip {clip_id}: drift={drift:+.4f}с → target {target_duration:.4f}с → adjusted {adjusted_target:.4f}с")

                    # Сцена может состоять из нескольких скачанных отрезков —
                    # тогда каждый приводится к целевому формату отдельно, и они
                    # склеиваются копированием (см. `_process_clip_parts`).
                    clip_parts = _resolve_clip_parts(clip)
                    source_file = clip_parts[0] if clip_parts else _resolve_clip_source(clip)
                    if not source_file:
                        logger.error(f"Clip {clip_id}: файл не найден (clip['file']={clip.get('file')}, vid_img/ пусто)")
                        print(f"\n❌ Clip {clip_id}: файл не найден! Проверьте vid_img/")
                        sys.exit(1)

                    try:
                        # Per-clip upscale: явный -r 1080/4k, либо авто-1080 если в проекте смешанные разрешения
                        # Размер задаётся целью прогона; масштаб применяется только
                        # при несовпадении, в любую сторону (см. process_video).
                        _per_clip_upscale_4k = False
                        _per_clip_upscale = False
                        # Real Photo: extract credit/zoom meta from clip → image branch will apply blur_fill + drawtext
                        _rp_meta = clip.get('real_meta') if clip.get('source') == 'real_photo' else None
                        _static_infographic = bool(
                            args.static_infographics and clip.get('source') == 'infographic'
                        )
                        _clip_kwargs = dict(
                            logo_path=logo_path,
                            blur_borders=False,
                            keep_audio=(getattr(args, 'veo_audio_inline', False)
                                        or getattr(args, 'keep_veo_sound', False)),
                            upscale_1080=_per_clip_upscale,
                            upscale_4k=_per_clip_upscale_4k,
                            target_size=_target_size,
                            fps=getattr(args, 'fps', 60),
                            is_chain_extend=is_chain_extend,
                            real_photo_meta=_rp_meta,
                            fade_in=_fade_in, fade_out=_fade_out,
                            transition_duration=_transition_duration,
                            zoom_style=_clip_zoom_style,
                            static_no_motion=_static_infographic,
                        )
                        if clip_parts:
                            print(f"   [PARTS] Clip {clip_id}: сцена из {len(clip_parts)} кус.")
                            actual_dur = _process_clip_parts(
                                clip_parts, str(output_file), adjusted_target, temp_dir,
                                encoder_type, codec, args.bitrate, **_clip_kwargs)
                        else:
                            actual_dur = process_video(
                                source_file, str(output_file), adjusted_target,
                                encoder_type, codec, args.bitrate, **_clip_kwargs)
                        if actual_dur is None:
                            _rp_meta_fb = clip.get('real_meta') if clip.get('source') == 'real_photo' else None
                            for _fallback_img in _iter_clip_still_fallbacks(clip_id, source_file):
                                logger.warning(
                                    f"Clip {clip_id}: source {source_file} failed; "
                                    f"trying still fallback {_fallback_img}"
                                )
                                actual_dur = _render_still_fallback(
                                    _fallback_img, str(output_file), adjusted_target,
                                    encoder_type, codec, args.bitrate, logo_path=logo_path,
                                    fps=getattr(args, 'fps', 60),
                                    keep_audio=(getattr(args, 'veo_audio_inline', False)
                                                or getattr(args, 'keep_veo_sound', False)),
                                    credit_text=(_rp_meta_fb or {}).get('credit_text') if _rp_meta_fb else None,
                                    blur_fill=bool(_rp_meta_fb),
                                    upscale_1080=_per_clip_upscale,
                                    upscale_4k=_per_clip_upscale_4k,
                                    target_size=_target_size,
                                    fade_in=_fade_in, fade_out=_fade_out,
                                    transition_duration=_transition_duration,
                                    zoom_style=_clip_zoom_style,
                                    static_no_motion=_static_infographic,
                                )
                                if actual_dur is not None:
                                    break

                        if actual_dur is None:
                            for _neighbor_source, _use_last_frame in _iter_neighbor_sources(
                                corrected_clips, clip_idx, existing_files
                            ):
                                logger.warning(
                                    f"Clip {clip_id}: using nearby frame from {_neighbor_source} "
                                    f"to preserve timeline duration"
                                )
                                actual_dur = _render_neighbor_hold(
                                    _neighbor_source, str(output_file), adjusted_target,
                                    encoder_type, codec, args.bitrate,
                                    use_last_frame=_use_last_frame, logo_path=logo_path,
                                    fps=getattr(args, 'fps', 60),
                                    keep_audio=(getattr(args, 'veo_audio_inline', False)
                                                or getattr(args, 'keep_veo_sound', False)),
                                    upscale_1080=_per_clip_upscale,
                                    upscale_4k=_per_clip_upscale_4k,
                                    target_size=_target_size,
                                    fade_in=_fade_in, fade_out=_fade_out,
                                    transition_duration=_transition_duration,
                                )
                                if actual_dur is not None:
                                    break

                        if actual_dur is None:
                            raise RuntimeError(
                                f"Clip {clip_id}: no usable source or neighboring frame for recovery"
                            )
                        existing_files[clip_id] = str(output_file)
                        cumulative_planned += target_duration
                        cumulative_actual  += actual_dur

                        _retime_done += 1
                        if IS_PIPE:
                            print(f"   Ретайминг: {_retime_done}/{_retime_total} (clip {clip_id})", flush=True)
                        elif _retime_pbar is not None:
                            _retime_pbar.update(1)
                    except Exception as e:
                        logger.error(f"Критическая ошибка обработки clip {clip_id}: {e}")
                        print(f"\n❌ Ошибка обработки: {e}")
                        sys.exit(1)

            if _retime_pbar is not None:
                _retime_pbar.close()
            total_drift = cumulative_actual - cumulative_planned
            logger.info(f"Ретайминг завершён. Суммарный дрифт после коррекции: {total_drift:+.3f}с")
            print(f"   📊 Суммарный дрифт после коррекции: {total_drift:+.3f}с")
        else:
            logger.info("Все файлы уже обработаны")
            print(f"\n✅ Все файлы уже обработаны")

        # Формируем упорядоченный список обработанных файлов
        print(f"\n📦 Формирование финального списка файлов...")
        logger.info("Формирование финального списка файлов")
        processed_files_ordered = []

        for clip in corrected_clips:
            clip_id = clip['id']
            processed_files_ordered.append(existing_files[clip_id])

        logger.info(f"Всего файлов для склейки: {len(processed_files_ordered)}")
        print(f"   ✅ Всего файлов для склейки: {len(processed_files_ordered)}")

        # ЭТАП 5: ГРУППОВАЯ СКЛЕЙКА
        temp_video = temp_dir / 'concat_video.mp4'
        group_concat(processed_files_ordered, temp_video, temp_dir, args.group_size, sub_path=_sub_path, encoder_type=encoder_type, bitrate=args.bitrate, total_duration=audio_duration)

        # ЭТАП 5.5: KEEP VEO SOUND — склеенная VEO-аудиодорожка в retimed_sound/
        # temp_video сейчас содержит ретаймленный VEO-звук всех клипов (клипы
        # ретаймились с keep_audio=True), длиной ровно в видео. Извлекаем его ДО
        # ЭТАП 6/8 (там аудио заменится на нарацию). Итог: обычное видео с нарацией
        # + отдельная дорожка звука веогенераций для ручного наложения (CapCut).
        # Не фатально: сбой извлечения не валит основной конкат.
        if getattr(args, 'keep_veo_sound', False):
            try:
                _veo_snd_dir = Path(args.output).resolve().parent / 'retimed_sound'
                _veo_snd_dir.mkdir(parents=True, exist_ok=True)
                _veo_snd_file = _veo_snd_dir / f"{Path(args.output).stem}_veo_audio.m4a"
                print(f"\n🎧 Keep VEO sound: извлечение склеенной VEO-дорожки → {_veo_snd_file.name}")
                logger.info(f"Keep VEO sound: extracting concatenated VEO audio track to {_veo_snd_file}")
                _cmd_veo = [FFMPEG, '-y', '-nostdin', '-i', str(temp_video),
                            '-vn', '-c:a', 'aac', '-b:a', '192k', str(_veo_snd_file)]
                subprocess.run(_cmd_veo, check=True, capture_output=True, **SUBPROCESS_FLAGS)
                _snd_dur = get_video_duration(str(_veo_snd_file)) or 0
                print(f"   ✅ VEO-дорожка сохранена ({_snd_dur:.1f}с): retimed_sound/{_veo_snd_file.name}")
                logger.info(f"Keep VEO sound: track saved ({_snd_dur:.1f}s): {_veo_snd_file}")
            except Exception as _veo_snd_err:
                print(f"   ⚠️  Не удалось сохранить VEO-дорожку: {_veo_snd_err}")
                logger.warning(f"Keep VEO sound: track extraction failed: {_veo_snd_err}")

        # ЭТАП 6: АВТОИСПРАВЛЕНИЕ ФИНАЛЬНОЙ СИНХРОНИЗАЦИИ
        if audio_file is not None:
            temp_video = fix_video_audio_sync(temp_video, audio_file, temp_dir,
                                              encoder_type, codec, args.bitrate)
        else:
            logger.info("ЭТАП 6: пропуск (нет нарации, veo-audio-inline режим)")

        # ЭТАП 7: ИЗВЛЕЧЕНИЕ ОРИГИНАЛЬНОГО АУДИО
        logger.info("ЭТАП 7: ИЗВЛЕЧЕНИЕ ОРИГИНАЛЬНОГО АУДИО")
        print(f"\n🔊 Извлечение оригинального аудио...")
        original_audio = args.output.replace('.mp4', '_original_audio.wav')
        try:
            subprocess.run([FFMPEG, '-y', '-i', str(temp_video),
                           '-vn', '-acodec', 'pcm_s16le', original_audio],
                          check=True, capture_output=True, **SUBPROCESS_FLAGS)
            logger.info(f"Оригинальное аудио извлечено: {original_audio}")
            print(f"   ✅ {original_audio}")
        except Exception as e:
            logger.warning(f"Не удалось извлечь оригинальное аудио: {e}")
            print(f"   ⚠️  Не удалось")

        # ЭТАП 8: ФИНАЛЬНЫЙ MUX / USATAYAI BRIDGE / ЗАМЕНА АУДИО
        bridge_manifest = args.usatayai_bridge_manifest or os.environ.get('USATAYAI_BRIDGE_MANIFEST')
        if bridge_manifest:
            logger.info("ЭТАП 8: USATAYAI BRIDGE FINAL RENDER")
            print(f"\n🧩 USATAYAI BRIDGE: финальная добивка через manifest...")
            try:
                from usatayai_bridge import run_bridge_if_requested
                run_bridge_if_requested(
                    bridge_manifest,
                    str(temp_video),
                    args.output,
                    audio_file,
                    bitrate=args.bitrate,
                    fps=getattr(args, 'fps', 60),
                    encoder_type=encoder_type,
                    codec=codec,
                )
                logger.info(f"Финальный файл создан через USATAYAI bridge: {args.output}")
            except Exception as e:
                logger.error(f"USATAYAI bridge failed: {e}")
                logger.error(traceback.format_exc())
                raise
        elif audio_file is not None:
            logger.info("ЭТАП 8: ЗАМЕНА АУДИО НА ОЗВУЧКУ")

            print(f"\n🎵 Замена аудио на озвучку...")
            pre_encoded_audio = temp_dir / 'narration_aac.m4a'
            if IS_PIPE:
                print(f"PROGRESS:0/2", flush=True)
            print(f"   Шаг 1/2: Кодирование аудио WAV → AAC...")
            cmd_aac = [FFMPEG, '-y', '-i', audio_file,
                       '-c:a', 'aac', '-b:a', '192k', str(pre_encoded_audio)]
            aac_duration = audio_duration if audio_duration else None
            try:
                run_ffmpeg_with_progress(cmd_aac, aac_duration, "AAC кодирование")
                logger.info(f"Аудио предварительно закодировано")
            except subprocess.CalledProcessError as e:
                logger.error(f"Ошибка кодирования аудио: {e}")
                raise

            print(f"   Шаг 2/2: Сборка видео + аудио (без перекодирования)...")
            cmd_mux = [FFMPEG, '-y', '-i', str(temp_video), '-i', str(pre_encoded_audio),
                       '-map', '0:v', '-map', '1:a', '-c:v', 'copy',
                       '-c:a', 'copy', '-shortest', args.output]

            run_ffmpeg_with_progress(cmd_mux, aac_duration, "Сборка")
            logger.info(f"Финальный файл создан: {args.output}")

            try:
                pre_encoded_audio.unlink()
            except:
                pass
        else:
            # veo-audio-inline без нарации: temp_video уже содержит ретаймленный VEO звук
            logger.info("ЭТАП 8: пропуск замены аудио — копируем temp_video с VEO звуком в выходной файл")
            print(f"\n[OK] VEO audio inline: сохраняем VEO звук в финальном видео...")
            shutil.copy2(str(temp_video), args.output)
            logger.info(f"Финальный файл создан (VEO audio): {args.output}")

        # ЭТАП 9: ОЧИСТКА
        logger.info("ЭТАП 9: ОЧИСТКА")
        ask_delete_temp_folder(temp_dir, args.no_cleanup)

        # ФИНАЛ
        print("\n" + "="*80)
        print("✅ ГОТОВО!")
        print("="*80)
        print(f"\n📹 Видео: {args.output}")
        print(f"   • FPS: {getattr(args, 'fps', 60)}")
        print(f"   • Битрейт: {args.bitrate}")
        print(f"   • Кодек: {codec} ({encoder_type.upper()})")
        print(f"\n🔊 Оригинальное аудио: {original_audio}")
        print(f"🎵 Озвучка: {audio_file if audio_file else '(VEO audio inline — нарация не использовалась)'}")
        print(f"\n📝 Лог-файл: {log_file}")
        print("="*80)

        logger.info("="*80)
        logger.info("СКРИПТ ЗАВЕРШЁН УСПЕШНО")
        logger.info("="*80)

    except Exception as e:
        logger.critical("КРИТИЧЕСКАЯ ОШИБКА!")
        logger.critical(f"Ошибка: {e}")
        logger.critical(traceback.format_exc())
        print(f"\n❌ КРИТИЧЕСКАЯ ОШИБКА: {e}")
        print(f"\n📝 Подробности в лог-файле: {log_file}")
        sys.exit(1)

if __name__ == '__main__':
    main()
