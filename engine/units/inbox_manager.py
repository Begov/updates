"""
Модуль очереди проектов - обработка аудиофайлов из pipeline_queue
"""
from __future__ import annotations

import os
import stat
import shutil
from pathlib import Path
from typing import List, Dict, Any
from datetime import datetime
import json

import params as config
from units.text_io_utils import read_user_text
from tqdm import tqdm


SUPPORTED_INPUT_EXTENSIONS = [
    '.wav', '.mp3', '.flac', '.m4a', '.aac',
    '.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v',
]


# ============================================================================
# ЛОГИРОВАНИЕ ПРОГОНА ПРОЕКТА
# ============================================================================

def append_run_log(project_dir: Path, project_name: str, stage: str, params: dict) -> None:
    """Дописывает запись о выполненном этапе в {project_name}_run_log.txt."""
    log_path = project_dir / f'{project_name}_run_log.txt'
    lines = [
        '',
        '=' * 72,
        f'[{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}] {stage}',
        '=' * 72,
    ]
    for k, v in params.items():
        lines.append(f'  {k:<22}{v}')
    lines.append('')
    with open(log_path, 'a', encoding='utf-8', errors='replace') as f:
        f.write('\n'.join(lines) + '\n')


# ============================================================================
# ВАЛИДАЦИЯ ВХОДНЫХ ДАННЫХ
# ============================================================================

def validate_audio_file(audio_file: Path) -> tuple[bool, str]:
    """
    Проверка аудиофайла перед обработкой
    
    Args:
        audio_file: Путь к аудиофайлу
    
    Returns:
        (успех, сообщение)
    """
    if not audio_file.exists():
        return False, f"Аудиофайл не найден: {audio_file}"
    
    if not audio_file.is_file():
        return False, f"Не является файлом: {audio_file}"
    
    if audio_file.suffix.lower() not in SUPPORTED_INPUT_EXTENSIONS:
        return False, f"Неподдерживаемый формат: {audio_file.suffix}. Поддерживаются: {', '.join(SUPPORTED_INPUT_EXTENSIONS)}"
    
    return True, f"✓ Аудиофайл валиден: {audio_file}"


def validate_ready_dir(ready_dir: Path) -> tuple[bool, str]:
    """
    Проверка папки pipeline_queue
    
    Args:
        ready_dir: Путь к папке
    
    Returns:
        (успех, сообщение)
    """
    if not ready_dir.exists():
        return False, f"Папка не найдена: {ready_dir}"
    
    if not ready_dir.is_dir():
        return False, f"Не является папкой: {ready_dir}"
    
    return True, f"✓ Папка валидна: {ready_dir}"


def validate_projects_dir(projects_dir: Path) -> tuple[bool, str]:
    """
    Проверка папки projects
    
    Args:
        projects_dir: Путь к папке
    
    Returns:
        (успех, сообщение)
    """
    if not projects_dir.exists():
        # Пытаемся создать
        try:
            projects_dir.mkdir(parents=True, exist_ok=True)
            return True, f"✓ Папка создана: {projects_dir}"
        except Exception as e:
            return False, f"Не удалось создать папку: {projects_dir}. Ошибка: {e}"
    
    if not projects_dir.is_dir():
        return False, f"Не является папкой: {projects_dir}"
    
    return True, f"✓ Папка валидна: {projects_dir}"


def validate_style_guide(style_guide: Path) -> tuple[bool, str]:
    """
    Проверка style guide
    
    Args:
        style_guide: Путь к файлу
    
    Returns:
        (успех, сообщение)
    """
    if style_guide is None:
        return False, "Style guide не найден"
    
    if not style_guide.exists():
        return False, f"Style guide не найден: {style_guide}"
    
    if not style_guide.is_file():
        return False, f"Не является файлом: {style_guide}"
    
    return True, f"✓ Style guide валиден: {style_guide}"


# ============================================================================
def get_ready_files(ready_dir: Path) -> List[Path]:
    """
    Получить список аудиофайлов из pipeline_queue
    
    Args:
        ready_dir: Путь к папке pipeline_queue
    
    Returns:
        Список путей к аудиофайлам
    """
    ready_files = []

    for ext in SUPPORTED_INPUT_EXTENSIONS:
        ready_files.extend(ready_dir.glob(f'*{ext}'))
        ready_files.extend(ready_dir.glob(f'*{ext.upper()}'))
    
    # Сортировка по имени
    ready_files = sorted(set(ready_files), key=lambda x: x.name.lower())
    
    return ready_files


def get_style_guide_for_project(audio_file: Path, ready_dir: Path) -> Path:
    """
    Получить style guide для проекта

    Логика:
    1. Если есть файл начинающийся с SG_ALL (*.txt, *.md) — использовать его для всех
    2. Иначе искать файл который начинается с имени аудиофайла и содержит _style

    Args:
        audio_file: Путь к аудиофайлу
        ready_dir: Путь к папке pipeline_queue

    Returns:
        Путь к style guide или None
    """
    # 1. Проверка SG_ALL* (общий для всех)
    # Ищем файлы которые начинаются с SG_ALL (любые .txt, .md)
    sg_all_variants = list(ready_dir.glob('SG_ALL*.txt')) + list(ready_dir.glob('SG_ALL*.md'))

    if sg_all_variants:
        # Берём первый найденный
        return sg_all_variants[0]

    # 2. Поиск индивидуального style guide
    # Ищем файлы которые НАЧИНАЮТСЯ с имени аудиофайла и содержат _style или имеют формат {аудио}_{пометка}.txt
    audio_name = audio_file.stem  # Имя без расширения

    # Собираем все .txt и .md файлы
    all_text_files = list(ready_dir.glob('*.txt')) + list(ready_dir.glob('*.md'))

    # Фильтруем: имя файла должно начинаться с audio_name и содержать хотя бы одно подчёркивание после audio_name
    for file in all_text_files:
        file_name_lower = file.name.lower()
        audio_name_lower = audio_name.lower()

        # Проверяем что имя файла начинается с названия аудио и имеет дополнительный суффикс
        if file_name_lower.startswith(audio_name_lower + '_'):
            # Исключаем файлы которые являются самим аудио/видео
            if file.suffix.lower() not in SUPPORTED_INPUT_EXTENSIONS:
                return file

    return None


def get_all_style_guides_for_audio(audio_file: Path, ready_dir: Path) -> List[Path]:
    """
    Получить ВСЕ style guide для аудиофайла (индивидуальные)

    Args:
        audio_file: Путь к аудиофайлу
        ready_dir: Путь к папке pipeline_queue

    Returns:
        Список путей к style guide
    """
    audio_name = audio_file.stem
    all_text_files = list(ready_dir.glob('*.txt')) + list(ready_dir.glob('*.md'))
    guides = []

    for file in all_text_files:
        file_name_lower = file.name.lower()
        audio_name_lower = audio_name.lower()

        # Проверяем что имя файла начинается с названия аудио
        # И это не сам аудиофайл (не .wav/.mp3/.mp4 и т.д.)
        if file_name_lower.startswith(audio_name_lower + '_') or file_name_lower.startswith(audio_name_lower + '-'):
            # Исключаем файлы которые являются самим аудио/видео (на всякий случай)
            if file.suffix.lower() not in SUPPORTED_INPUT_EXTENSIONS:
                guides.append(file)

    return sorted(guides, key=lambda x: x.name)


def get_sg_all_file(ready_dir: Path) -> Path:
    """
    Проверить наличие SG_ALL файла

    Args:
        ready_dir: Путь к папке pipeline_queue

    Returns:
        Путь к SG_ALL файлу или None
    """
    sg_all_variants = list(ready_dir.glob('SG_ALL*.txt')) + list(ready_dir.glob('SG_ALL*.md'))
    return sg_all_variants[0] if sg_all_variants else None


def get_script_files(ready_dir: Path) -> List[Path]:
    """
    Найти файлы сценариев TTS в pipeline_queue.

    Сценарий — это .txt файл, который НЕ является style guide:
      - НЕ начинается с SG_ALL
      - НЕ содержит _style или _sg_ в имени

    Голос выбирается в GUI/настройках; файл содержит только текст сценария.

    Args:
        ready_dir: Путь к папке pipeline_queue

    Returns:
        Список путей к файлам сценариев (отсортированных по имени)
    """
    scripts = []
    for txt in sorted(ready_dir.glob('*.txt'), key=lambda x: x.name.lower()):
        name_lower = txt.stem.lower()
        # Исключаем style guides
        if name_lower.startswith('sg_all'):
            continue
        if '_style' in name_lower or '_sg_' in name_lower:
            continue
        # Любой текстовый сценарий подходит: голос выбирается отдельно
        # в GUI/пресете и не должен съедать или фильтровать первую строку.
        scripts.append(txt)
    return scripts


# Сколько символов имени аудио берём в название проекта.
#
# Имя папки проекта раньше повторяло имя файла целиком, а в Windows полный путь
# ограничен 260 символами. У пользователя с заголовком ролика на 74 символа
# («EL TERREMOTO DE COLOMBIA DEJA 289 MUERTOS…») путь до временного файла Pass 2
# вырос до 272 и пайплайн упал на ровном месте: система в такой ситуации отвечает
# не «слишком длинно», а «файл не найден» (жалоба 29.08). Ограничение снимает
# корень: внутрь папки помещаются и промпты, и служебные файлы стадий.
#
# 60 символов + метка времени дают имя ~74, путь до папки проекта ~135 — на файлы
# внутри остаётся больше сотни символов запаса.
PROJECT_NAME_MAX_AUDIO = 60


def _short_audio_name(audio_name: str) -> str:
    """Имя аудио для названия проекта — обрезанное до разумной длины.

    Режем по границе слова, если она рядом: «…CASAS DESTRUI» выглядит хуже, чем
    «…9.000 CASAS». Точка отсечения ищется в последней четверти обрезка.
    """
    if len(audio_name) <= PROJECT_NAME_MAX_AUDIO:
        return audio_name
    cut = audio_name[:PROJECT_NAME_MAX_AUDIO]
    space = cut.rfind(' ')
    if space >= int(PROJECT_NAME_MAX_AUDIO * 0.75):
        cut = cut[:space]
    return cut.rstrip(' _-.')


def get_project_name(audio_file: Path, style_guide: Path) -> str:
    """
    Сформировать имя проекта: {audio}_{style}_{YYYYMMDD_HHmm}

    Args:
        audio_file: Путь к аудиофайлу
        style_guide: Путь к style guide

    Returns:
        Имя проекта
    """
    ts = datetime.now().strftime('%Y%m%d_%H%M')
    audio_name = _short_audio_name(audio_file.stem)
    sg_name = style_guide.stem

    if sg_name.upper().startswith('SG_ALL'):
        # SG_ALL_ужасы → ужасы, SG_ALL → (no mark)
        parts = sg_name.split('_', 2)
        mark = parts[2] if len(parts) > 2 else ''
        if mark:
            return f"{audio_name}_{mark}_{ts}"
        else:
            return f"{audio_name}_{ts}"

    # Individual style guide — just audio name + timestamp
    return f"{audio_name}_{ts}"


def create_project_folder(audio_file: Path, projects_dir: Path, style_guide: Path = None, copy_audio: bool = False) -> Path:
    """
    Создать папку проекта и скопировать/переместить аудиофайл

    Args:
        audio_file: Путь к аудиофайлу
        projects_dir: Путь к папке projects
        style_guide: Путь к style guide (опционально, для формирования имени проекта)
        copy_audio: Если True — копировать аудио, иначе перемещать

    Returns:
        Путь к папке проекта
    """
    # Название проекта
    if style_guide:
        project_name = get_project_name(audio_file, style_guide)
    else:
        # Та же обрезка, что и в get_project_name: без неё ветка «без стайлгайда»
        # снова собирала бы имя из полного заголовка ролика.
        project_name = (f"{_short_audio_name(audio_file.stem)}_"
                        f"{datetime.now().strftime('%Y%m%d_%H%M')}")

    # Папка проекта
    project_dir = projects_dir / project_name

    # Создание папки и подпапки для референсов обложек
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / 'ref_thumb').mkdir(exist_ok=True)
    (project_dir / 'ref').mkdir(exist_ok=True)
    (project_dir / 'vid_img').mkdir(exist_ok=True)
    (project_dir / 'generated').mkdir(exist_ok=True)

    # Пустой файл для single_prompt режима (заполняется вручную при необходимости)
    sp = project_dir / 'single_prompt.txt'
    if not sp.exists():
        sp.write_text('', encoding='utf-8')

    # Копирование аудиофайла (НЕ перемещение! чтобы не потерять файл)
    dest_audio = project_dir / audio_file.name
    shutil.copy2(str(audio_file), str(dest_audio))

    # Проверка что аудио скопировано
    if not dest_audio.exists():
        raise RuntimeError(f"Не удалось скопировать аудиофайл в {dest_audio}")

    # pipeline_config.json — слепок настроек первого запуска проекта.
    # Используется Finalize Stage при ресюме чтобы восстановить vislide_interval,
    # intro_block_duration и другие параметры, которые иначе теряются при закрытии GUI.
    # См. changelog_20260524_*.md и begoframe.py:_load_project_config.
    _cfg_json = os.environ.get('BEGO_PIPELINE_CONFIG_JSON', '')
    if _cfg_json:
        try:
            import json as _json
            _cfg = _json.loads(_cfg_json)
            _cfg_path = project_dir / 'pipeline_config.json'
            # Не перезаписываем если файл уже существует (защита от случайного override)
            if not _cfg_path.exists():
                _cfg['created_at'] = datetime.now().isoformat()
                _cfg_path.write_text(_json.dumps(_cfg, indent=2, ensure_ascii=False),
                                     encoding='utf-8')
        except Exception as _e:
            # Не валим пайплайн если конфиг битый — это вспомогательный файл
            print(f"  [pipeline_config] warning: failed to write — {_e}")

    return project_dir


def copy_concat_script(project_dir: Path, source_script: Path):
    """
    Скопировать скрипт склейки в папку проекта

    Args:
        project_dir: Папка проекта
        source_script: Путь к assemble.py
    """
    if source_script.exists():
        dest_script = project_dir / 'assemble.py'
        shutil.copy2(str(source_script), str(dest_script))


def copy_rename_script(project_dir: Path, source_script: Path):
    """
    Скопировать скрипт переименования в папку проекта

    Args:
        project_dir: Папка проекта
        source_script: Путь к rename_videos.py
    """
    if source_script.exists():
        dest_script = project_dir / 'rename_videos.py'
        shutil.copy2(str(source_script), str(dest_script))


def create_process_bat(project_dir: Path, project_name: str, mode: str, interactive: bool) -> Path:
    """
    Создать BAT-файл из шаблона

    Args:
        project_dir: Папка проекта
        project_name: Название проекта
        mode: 'fast' или '1080p'
        interactive: True если спрашивать об удалении temp_processed

    Returns:
        Путь к созданному BAT-файлу
    """
    suffix = '_interactive' if interactive else ''

    # --- Windows BAT ---
    template_path = Path(__file__).parent.parent / 'templates' / f'process_{mode}.txt'
    with open(template_path, 'r', encoding='utf-8') as f:
        template = f.read()
    content = template.format(project_name=project_name)
    bat_path = project_dir / f'process_{mode}{suffix}.bat'
    with open(bat_path, 'w', encoding='utf-8', errors='replace', newline='\r\n') as f:
        f.write(content)

    # --- macOS .command ---
    _write_command(project_dir, f'process_{mode}{suffix}.command', project_name)

    return bat_path


def create_director_bat(project_dir: Path, project_name: str) -> Path:
    """
    Создать run_director.bat — аварийный запуск режиссёра без транскрипции.
    Используется если этап 1 сломался после транскрипции.

    Args:
        project_dir: Папка проекта
        project_name: Название проекта

    Returns:
        Путь к созданному BAT-файлу
    """
    template_path = Path(__file__).parent.parent / 'templates' / 'run_director.txt'
    with open(template_path, 'r', encoding='utf-8') as f:
        template = f.read()

    content = template.format(project_name=project_name)
    bat_path = project_dir / 'run_director.bat'
    with open(bat_path, 'w', encoding='utf-8', errors='replace', newline='\r\n') as f:
        f.write(content)
    _write_command(project_dir, 'start_director.command', project_name)
    return bat_path


def create_generate_bat(project_dir: Path, project_name: str) -> Path:
    """
    Создать run_generate.bat — запуск генерации изображений через FastGen API.
    Интерактивный: спрашивает модель, аспект, параллельность, ID клипов, режим рефов.

    Args:
        project_dir: Папка проекта
        project_name: Название проекта

    Returns:
        Путь к созданному BAT-файлу
    """
    template_path = Path(__file__).parent.parent / 'templates' / 'run_generate.txt'
    with open(template_path, 'r', encoding='utf-8') as f:
        template = f.read()

    content = template.format(
        project_name=project_name,
        default_model=config.FASTGEN_MODEL,
    )
    bat_path = project_dir / 'run_generate.bat'
    with open(bat_path, 'w', encoding='utf-8', errors='replace', newline='\r\n') as f:
        f.write(content)
    _write_command(project_dir, 'start_generate.command', project_name)
    return bat_path


def create_img2video_bat(project_dir: Path, project_name: str) -> Path:
    """
    Создать run_veonon_gen.bat — запуск генерации видео через VeoNonStop API (i2v/t2v/components).

    Args:
        project_dir: Папка проекта
        project_name: Название проекта

    Returns:
        Путь к созданному BAT-файлу
    """
    template_path = Path(__file__).parent.parent / 'templates' / 'run_veonon_gen.txt'
    with open(template_path, 'r', encoding='utf-8') as f:
        template = f.read()

    content = template.format(project_name=project_name)
    bat_path = project_dir / 'run_veonon_gen.bat'
    with open(bat_path, 'w', encoding='utf-8', errors='replace', newline='\r\n') as f:
        f.write(content)
    _write_command(project_dir, 'start_veonon_gen.command', project_name)
    return bat_path


def create_veo_i2v_bat(project_dir: Path, project_name: str) -> Path:
    """Create run_veo_i2v.bat — standalone VeoNonStop image-to-video."""
    template_path = Path(__file__).parent.parent / 'templates' / 'run_veo_i2v.txt'
    with open(template_path, 'r', encoding='utf-8') as f:
        template = f.read()
    content = template.format(project_name=project_name)
    bat_path = project_dir / 'run_veo_i2v.bat'
    with open(bat_path, 'w', encoding='utf-8', errors='replace', newline='\r\n') as f:
        f.write(content)
    _write_command(project_dir, 'start_veo_i2v.command', project_name)
    return bat_path


def create_veo_t2v_bat(project_dir: Path, project_name: str) -> Path:
    """Create run_veo_t2v.bat — standalone VeoNonStop text-to-video."""
    template_path = Path(__file__).parent.parent / 'templates' / 'run_veo_t2v.txt'
    with open(template_path, 'r', encoding='utf-8') as f:
        template = f.read()
    content = template.format(project_name=project_name)
    bat_path = project_dir / 'run_veo_t2v.bat'
    with open(bat_path, 'w', encoding='utf-8', errors='replace', newline='\r\n') as f:
        f.write(content)
    _write_command(project_dir, 'start_veo_t2v.command', project_name)
    return bat_path


def create_veo_components_bat(project_dir: Path, project_name: str) -> Path:
    """Create run_veo_components.bat — standalone VeoNonStop multi-image-to-video."""
    template_path = Path(__file__).parent.parent / 'templates' / 'run_veo_components.txt'
    with open(template_path, 'r', encoding='utf-8') as f:
        template = f.read()
    content = template.format(project_name=project_name)
    bat_path = project_dir / 'run_veo_components.bat'
    with open(bat_path, 'w', encoding='utf-8', errors='replace', newline='\r\n') as f:
        f.write(content)
    _write_command(project_dir, 'start_veo_components.command', project_name)
    return bat_path


def create_veo_imggen_bat(project_dir: Path, project_name: str) -> Path:
    """Create run_veo_imggen.bat — VeoNonStop image generation (Imagen/Banana)."""
    template_path = Path(__file__).parent.parent / 'templates' / 'run_veo_imggen.txt'
    with open(template_path, 'r', encoding='utf-8') as f:
        template = f.read()
    content = template.format(project_name=project_name)
    bat_path = project_dir / 'run_veo_imggen.bat'
    with open(bat_path, 'w', encoding='utf-8', errors='replace', newline='\r\n') as f:
        f.write(content)
    _write_command(project_dir, 'start_veo_imggen.command', project_name)
    return bat_path


def create_fastgen_veo_i2v_bat(project_dir: Path, project_name: str) -> Path:
    """Create run_fastgen_veo_i2v.bat — FastGen images + VeoNonStop animate pipeline."""
    template_path = Path(__file__).parent.parent / 'templates' / 'run_fastgen_veo_i2v.txt'
    with open(template_path, 'r', encoding='utf-8') as f:
        template = f.read()
    content = template.format(
        project_name=project_name,
        default_model=config.FASTGEN_MODEL,
    )
    bat_path = project_dir / 'run_fastgen_veo_i2v.bat'
    with open(bat_path, 'w', encoding='utf-8', errors='replace', newline='\r\n') as f:
        f.write(content)
    _write_command(project_dir, 'start_fastgen_veo_i2v.command', project_name)
    return bat_path


def create_dogenerator_bat(project_dir: Path, project_name: str) -> Path:
    """
    Создать run_dogenerator.bat — догенерация недостающих клипов.
    Сканирует generated/, находит пропущенные ID и предлагает сгенерировать их.

    Args:
        project_dir: Папка проекта
        project_name: Название проекта

    Returns:
        Путь к созданному BAT-файлу
    """
    template_path = Path(__file__).parent.parent / 'templates' / 'run_dogenerator.txt'
    with open(template_path, 'r', encoding='utf-8') as f:
        template = f.read()

    content = template.format(project_name=project_name)
    bat_path = project_dir / 'run_dogenerator.bat'
    with open(bat_path, 'w', encoding='utf-8', errors='replace', newline='\r\n') as f:
        f.write(content)
    _write_command(project_dir, 'start_dogenerator.command', project_name)
    return bat_path


def create_photo_bat(project_dir: Path, project_name: str) -> Path:
    """
    Создать run_photo.bat — фото-слайдшоу с Ken Burns + xfade переходами.
    Использует photo_concat.py (должен быть скопирован в папку проекта).

    Args:
        project_dir: Папка проекта
        project_name: Название проекта

    Returns:
        Путь к созданному BAT-файлу или None если шаблон не найден
    """
    template_path = Path(__file__).parent.parent / 'templates' / 'run_photo.txt'
    if not template_path.exists():
        return None

    with open(template_path, 'r', encoding='utf-8') as f:
        template = f.read()

    content = template.format(project_name=project_name)
    bat_path = project_dir / 'run_photo.bat'
    with open(bat_path, 'w', encoding='utf-8', errors='replace', newline='\r\n') as f:
        f.write(content)
    _write_command(project_dir, 'start_photo.command', project_name)
    return bat_path


def create_yt_pack_bat(project_dir: Path, project_name: str) -> Path:
    """
    Создать run_yt_pack.bat — YouTube-упаковщик (субтитры, описание, обложки).

    Args:
        project_dir: Папка проекта
        project_name: Название проекта

    Returns:
        Путь к созданному BAT-файлу или None если шаблон не найден
    """
    template_path = Path(__file__).parent.parent / 'templates' / 'run_yt_pack.txt'
    if not template_path.exists():
        return None

    with open(template_path, 'r', encoding='utf-8') as f:
        template = f.read()

    content = template.format(project_name=project_name)
    bat_path = project_dir / 'run_yt_pack.bat'
    with open(bat_path, 'w', encoding='utf-8', errors='replace', newline='\r\n') as f:
        f.write(content)
    _write_command(project_dir, 'start_yt_pack.command', project_name)
    return bat_path


def create_inflight_i2v_bat(project_dir: Path, project_name: str) -> Path:
    """Create run_inflight_i2v.bat — animate images as FastGen generates them."""
    template_path = Path(__file__).parent.parent / 'templates' / 'run_inflight_i2v.txt'
    with open(template_path, 'r', encoding='utf-8') as f:
        template = f.read()
    content = template.format(project_name=project_name)
    bat_path = project_dir / 'run_inflight_i2v.bat'
    with open(bat_path, 'w', encoding='utf-8', newline='\r\n') as f:
        f.write(content)
    return bat_path


def create_parallel_i2v_bat(project_dir: Path, project_name: str) -> Path:
    """Create run_parallel_i2v.bat — parallel i2v watcher."""
    template_path = Path(__file__).parent.parent / 'templates' / 'run_parallel_i2v.txt'
    with open(template_path, 'r', encoding='utf-8') as f:
        template = f.read()
    content = template.format(project_name=project_name)
    bat_path = project_dir / 'run_parallel_i2v.bat'
    with open(bat_path, 'w', encoding='utf-8', newline='\r\n') as f:
        f.write(content)
    return bat_path


def create_variableconcat_bat(project_dir: Path, project_name: str) -> Path:
    """
    Создать run_variableconcat.bat — смешанный монтаж видео + фото из vid_img/.
    Использует variable_concat.py (должен быть скопирован в папку проекта).
    """
    template_path = Path(__file__).parent.parent / 'templates' / 'run_variableconcat.txt'
    if not template_path.exists():
        return None

    with open(template_path, 'r', encoding='utf-8') as f:
        template = f.read()

    content = template.format(project_name=project_name)
    bat_path = project_dir / 'run_variableconcat.bat'
    with open(bat_path, 'w', encoding='utf-8', errors='replace', newline='\r\n') as f:
        f.write(content)
    _write_command(project_dir, 'start_variableconcat.command', project_name)
    return bat_path



def _write_command(project_dir: Path, template_name: str, project_name: str) -> None:
    """Write macOS .command file from template if template exists (LF line endings)."""
    template_path = Path(__file__).parent.parent / 'templates' / template_name
    if not template_path.exists():
        return
    content = template_path.read_text(encoding='utf-8').replace('{project_name}', project_name)
    cmd_path = project_dir / template_name
    cmd_path.write_text(content, encoding='utf-8')


def create_yt_pack_interactive_bat(project_dir: Path, project_name: str) -> Path:
    """Create run_yt_pack_interactive.bat — interactive YT Packer."""
    content = (
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        "setlocal enabledelayedexpansion\r\n"
        "\r\n"
        "set \"PROJECT_NAME={project_name}\"\r\n"
        "set \"PROJECT_DIR=%~dp0\"\r\n"
        "\r\n"
        "echo.\r\n"
        "echo ================================================================================\r\n"
        "echo YT PACKER INTERACTIVE: {project_name}\r\n"
        "echo ================================================================================\r\n"
        "echo.\r\n"
        "\r\n"
        "if not exist \"%PROJECT_DIR%transcription.json\" (\r\n"
        "    echo [ERROR] transcription.json not found!\r\n"
        "    pause\r\n"
        "    exit /b 1\r\n"
        ")\r\n"
        "\r\n"
        "cd /d \"%PROJECT_DIR%..\\..\\agent\"\r\n"
        "\r\n"
        "python cli.py yt-pack --project \"%PROJECT_NAME%\" --interactive\r\n"
        "\r\n"
        "if errorlevel 1 (\r\n"
        "    echo.\r\n"
        "    echo [ERROR] YT Packer failed!\r\n"
        "    pause\r\n"
        "    exit /b 1\r\n"
        ")\r\n"
        "\r\n"
        "echo.\r\n"
        "echo [OK] YT Packer done! Check yt_pack/ folder.\r\n"
        "pause\r\n"
    ).format(project_name=project_name)
    bat_path = project_dir / 'run_yt_pack_interactive.bat'
    bat_path.write_bytes(content.encode('utf-8'))
    _write_command(project_dir, 'start_yt_pack_interactive.command', project_name)
    return bat_path


def create_translate_bat(project_dir: Path, project_name: str) -> Path:
    """Create run_translate.bat — SRT translation."""
    template_path = Path(__file__).parent.parent / 'templates' / 'run_translate.txt'
    if not template_path.exists():
        return None
    with open(template_path, 'r', encoding='utf-8') as f:
        template = f.read()
    content = template.format(project_name=project_name)
    bat_path = project_dir / 'run_translate.bat'
    with open(bat_path, 'w', encoding='utf-8', errors='replace', newline='\r\n') as f:
        f.write(content)
    _write_command(project_dir, 'start_translate.command', project_name)
    return bat_path


def create_process_batch_files(project_dir: Path, project_name: str):
    """
    Создать BAT-файлы для пост-обработки

    Args:
        project_dir: Папка проекта
        project_name: Название проекта
    """
    # Генерируем только интерактивные батники (non-interactive не используются)
    create_process_bat(project_dir, project_name, 'fast', interactive=True)
    create_process_bat(project_dir, project_name, '1080p', interactive=True)
    create_process_bat(project_dir, project_name, '4k', interactive=True)
    # Аварийный батник для повторного запуска режиссёра
    create_director_bat(project_dir, project_name)
    # Батник генерации изображений (FastGen)
    create_generate_bat(project_dir, project_name)
    # Батник генерации видео (VeoNonStop img2video — universal)
    create_img2video_bat(project_dir, project_name)
    # Standalone VeoNonStop bat files
    create_veo_i2v_bat(project_dir, project_name)
    create_veo_t2v_bat(project_dir, project_name)
    create_veo_components_bat(project_dir, project_name)
    create_veo_imggen_bat(project_dir, project_name)
    create_fastgen_veo_i2v_bat(project_dir, project_name)
    # Батник догенерации недостающих клипов
    create_dogenerator_bat(project_dir, project_name)
    # Батник фото-слайдшоу (Ken Burns + xfade)
    create_photo_bat(project_dir, project_name)
    # Батник YT Packer (субтитры, описание, обложки)
    create_yt_pack_bat(project_dir, project_name)
    create_yt_pack_interactive_bat(project_dir, project_name)
    # Батник перевода SRT
    create_translate_bat(project_dir, project_name)
    # Батник смешанного монтажа видео + фото
    create_variableconcat_bat(project_dir, project_name)
    # Батники параллельной анимации (inflight i2v)
    create_inflight_i2v_bat(project_dir, project_name)
    create_parallel_i2v_bat(project_dir, project_name)

    print(f"   [OK] process_fast_interactive.bat + .command")
    print(f"   [OK] process_1080p_interactive.bat + .command")
    print(f"   [OK] process_4k_interactive.bat + .command")
    print(f"   [OK] run_director.bat + .command")
    print(f"   [OK] run_generate.bat + .command")
    print(f"   [OK] run_veonon_gen.bat + .command")
    print(f"   [OK] run_veo_i2v.bat + .command")
    print(f"   [OK] run_veo_t2v.bat + .command")
    print(f"   [OK] run_veo_components.bat + .command")
    print(f"   [OK] run_veo_imggen.bat + .command")
    print(f"   [OK] run_fastgen_veo_i2v.bat + .command")
    print(f"   [OK] run_dogenerator.bat + .command")
    print(f"   [OK] run_photo.bat + .command")
    print(f"   [OK] run_yt_pack.bat + .command")
    print(f"   [OK] run_yt_pack_interactive.bat + .command")
    print(f"   [OK] run_translate.bat + .command")
    print(f"   [OK] run_variableconcat.bat + .command")
    print(f"   [OK] run_inflight_i2v.bat")
    print(f"   [OK] run_parallel_i2v.bat")


def create_fragment_checker(project_dir: Path):
    """
    Создать скрипт проверки наличия фрагментов

    Args:
        project_dir: Папка проекта
    """
    helper_py_content = """\
import json, os, sys

plan_path = 'montage_plan.json'
if not os.path.exists(plan_path):
    print('[ERROR] montage_plan.json ne nayden!')
    sys.exit(2)

data = json.load(open(plan_path, encoding='utf-8'))
clips = data.get('clips', [])
missing = []
VIDEO_EXTS = ('mp4', 'mov', 'avi', 'mkv', 'png', 'jpg', 'jpeg', 'webp')
for clip in clips:
    clip_id = clip['id']
    f = clip.get('file', '')
    found = False
    if f and os.path.exists(f):
        found = True
    if not found:
        for fmt in [f'{clip_id:03d}', str(clip_id)]:
            for ext in VIDEO_EXTS:
                if os.path.exists(os.path.join('vid_img', f'{fmt}.{ext}')):
                    found = True
                    break
            if found:
                break
    if not found:
        missing.append(str(clip_id))

print(f'Proverka {len(clips)} fragmentov...')

with open('missing_fragments.txt', 'w', encoding='utf-8') as mf:
    mf.write(' '.join(missing))

if missing:
    print(f'[ERROR] OTSUTSTVUYUT {len(missing)} fragmentov:')
    print(' '.join(missing))
    sys.exit(1)
else:
    print('[OK] Vse fragmenty na meste!')
    sys.exit(0)
"""

    checker_content = """@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

REM Proverka nalichiya vsekh fragmentov po montage_plan.json

if not exist "montage_plan.json" (
    echo [ERROR] montage_plan.json ne nayden!
    exit /b 1
)

python check_fragments.py
set PYEXIT=%ERRORLEVEL%

if "!PYEXIT!"=="2" exit /b 1

if "!PYEXIT!"=="1" (
    set /p MISSING_IDS=<missing_fragments.txt
    echo.
    echo    Sgeneriruyte nedostayushchie fragmenty v Veo3 i skachayte v papku vid_img\\.
    echo.
    exit /b 1
)

exit /b 0
"""

    with open(project_dir / 'check_fragments.py', 'w', encoding='utf-8') as f:
        f.write(helper_py_content)

    with open(project_dir / 'check_fragments.bat', 'w', encoding='utf-8', errors='replace', newline='\r\n') as f:
        f.write(checker_content)

    checker_command = """\
#!/bin/bash
# CHECK_FRAGMENTS.COMMAND - Check missing fragments (macOS)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -f "montage_plan.json" ]; then
    echo "[ERROR] montage_plan.json not found!"
    exit 1
fi

python3 check_fragments.py
PYEXIT=$?

if [ $PYEXIT -eq 2 ]; then
    exit 1
fi

if [ $PYEXIT -eq 1 ]; then
    MISSING_IDS=$(cat missing_fragments.txt 2>/dev/null)
    echo ""
    echo "   Generate missing fragments in Veo3 and download to vid_img/."
    echo ""
    exit 1
fi

exit 0
"""
    with open(project_dir / 'check_fragments.command', 'w', newline='', encoding='utf-8') as f:
        f.write(checker_command)

    print(f"   [OK] check_fragments.py")
    print(f"   [OK] check_fragments.bat")
    print(f"   [OK] check_fragments.command")



def copy_single_prompt(project_dir, source) -> bool:
    """Положить файл общего промпта видео в проект как single_prompt.txt.

    Его читают все пути видео (конвейер, «Видео» и «Довести проект» в Панели управления).
    `source` пустой или файла нет — ничего не делаем (остаётся прежняя копия проекта).
    """
    if not source or not Path(source).is_file():
        return False
    target = Path(project_dir) / 'single_prompt.txt'
    if Path(source).resolve() != target.resolve():
        shutil.copy2(str(source), str(target))
    print(f"   [OK] single_prompt.txt ({Path(source).name})")
    return True


def setup_project(audio_file: Path, ready_dir: Path, projects_dir: Path, concat_script: Path, style_guide: Path = None, copy_audio: bool = False) -> Dict[str, Any]:
    """
    Полная настройка проекта с валидацией

    Args:
        audio_file: Путь к аудиофайлу
        ready_dir: Папка pipeline_queue
        projects_dir: Папка projects
        concat_script: Путь к assemble.py
        style_guide: Путь к style guide (опционально, если уже выбран)
        copy_audio: Если True — копировать аудио, иначе перемещать

    Returns:
        Информация о проекте

    Raises:
        FileNotFoundError: Аудиофайл не найден
        ValueError: Неподдерживаемый формат
        NotADirectoryError: Папка не найдена
    """
    # ВАЛИДАЦИЯ ВХОДНЫХ ДАННЫХ
    # 1. Проверка аудиофайла
    is_valid, msg = validate_audio_file(audio_file)
    if not is_valid:
        raise FileNotFoundError(msg)

    # 2. Проверка папок
    is_valid, msg = validate_ready_dir(ready_dir)
    if not is_valid:
        raise NotADirectoryError(msg)

    is_valid, msg = validate_projects_dir(projects_dir)
    if not is_valid:
        raise NotADirectoryError(msg)

    # 3. Проверка concat_script
    if not concat_script.exists():
        raise FileNotFoundError(f"Скрипт склейки не найден: {concat_script}")

    # Если style guide не передан — ищем его
    if style_guide is None:
        style_guide = get_style_guide_for_project(audio_file, ready_dir)

    # Формируем имя проекта
    if style_guide:
        project_name = get_project_name(audio_file, style_guide)
    else:
        # Та же обрезка, что и в get_project_name: без неё ветка «без стайлгайда»
        # снова собирала бы имя из полного заголовка ролика.
        project_name = (f"{_short_audio_name(audio_file.stem)}_"
                        f"{datetime.now().strftime('%Y%m%d_%H%M')}")

    print(f"\nСоздание проекта: {project_name}")
    print("="*60)

    # 4. Проверка style guide
    is_valid, msg = validate_style_guide(style_guide)
    if not is_valid:
        print(f"   ⚠️  {msg}")
        print(f"      Создайте {audio_file.stem}_style.txt в inbox/ или SG_ALL.txt")
    else:
        print(f"   [OK] Style guide: {style_guide.name}")

    # Создание папки и перемещение аудио
    project_dir = create_project_folder(audio_file, projects_dir, style_guide, copy_audio)
    print(f"   [OK] Папка: {project_dir}")
    print(f"   [OK] Аудиофайл {'скопирован' if copy_audio else 'перемещён'}: {audio_file.name}")

    # Копирование style guide в папку проекта
    if style_guide and style_guide.exists():
        # Имя файла: {project}_style_{дата}.txt
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        style_filename = f'{project_name}_style_{timestamp}.txt'
        dest_style = project_dir / style_filename
        shutil.copy2(str(style_guide), str(dest_style))
        print(f"   [OK] {style_filename}")

    # Копирование ref/ (style-reference images для banana_ref / FastGen)
    # Приоритет: BEGO_REF_MAP[audio_name] → BEGO_REF_STAGING → inbox/ref/
    # BEGO_REFS_DISABLED=1 (BegoFrame) — референсов в программе нет: не копируем ничего,
    # иначе забытые файлы в inbox/ref/ молча попали бы в генерацию.
    _ref_staging = os.environ.get('BEGO_REF_STAGING', '')
    _ref_map_raw = os.environ.get('BEGO_REF_MAP', '')
    if os.environ.get('BEGO_REFS_DISABLED', '') == '1':
        ref_src = None
    elif _ref_map_raw:
        try:
            _ref_map_dict = json.loads(_ref_map_raw)
            _per_file = _ref_map_dict.get(audio_file.name, '')
            ref_src = Path(_per_file) if _per_file and Path(_per_file).exists() \
                      else (Path(_ref_staging) if _ref_staging and Path(_ref_staging).exists() else ready_dir / 'ref')
        except Exception:
            ref_src = Path(_ref_staging) if _ref_staging and Path(_ref_staging).exists() else ready_dir / 'ref'
    else:
        ref_src = Path(_ref_staging) if _ref_staging and Path(_ref_staging).exists() else ready_dir / 'ref'
    if ref_src is not None and ref_src.exists() and ref_src.is_dir():
        ref_dst = project_dir / 'ref'
        ref_dst.mkdir(exist_ok=True)
        n_refs = 0
        for f in ref_src.iterdir():
            if f.is_file():
                dst_f = ref_dst / f.name
                if not dst_f.exists():
                    shutil.copy2(str(f), str(dst_f))
                n_refs += 1
        if n_refs:
            print(f"   [OK] ref/ ({n_refs} files)")
        # Staging НЕ удаляем здесь — он нужен для следующих проектов в очереди.
        # Очистка происходит в начале следующего pipeline-запуска (begoframe.py).

    # Копирование ref_thumb/ (cover reference images для YT Packer)
    _cover_staging = os.environ.get('BEGO_COVER_REF_STAGING', '')
    cover_src = Path(_cover_staging) if _cover_staging and Path(_cover_staging).exists() else None
    if cover_src and cover_src.is_dir():
        cover_dst = project_dir / 'ref_thumb'
        cover_dst.mkdir(exist_ok=True)
        n_cover = 0
        for f in cover_src.iterdir():
            if f.is_file():
                dst_f = cover_dst / f.name
                if not dst_f.exists():
                    shutil.copy2(str(f), str(dst_f))
                n_cover += 1
        if n_cover:
            print(f"   [OK] ref_thumb/ ({n_cover} files)")
        # Staging НЕ удаляем здесь — нужен для следующих проектов в очереди.

    # Копирование logo.png в проект (только если WATERMARK_MODE=logo)
    # assemble.py автоматически подхватывает logo.png из папки проекта при старте
    if os.environ.get('WATERMARK_MODE', '') == 'logo':
        _logo_map_raw = os.environ.get('BEGO_LOGO_MAP', '')
        if _logo_map_raw:
            try:
                _logo_map_dict = json.loads(_logo_map_raw)
                _logo_src = _logo_map_dict.get(audio_file.name, '')
                if _logo_src and Path(_logo_src).exists():
                    shutil.copy2(_logo_src, str(project_dir / 'logo.png'))
                    print(f"   [OK] logo.png ({Path(_logo_src).name})")
            except Exception as _le:
                print(f"   [WARN] logo_map: {_le}")
        else:
            _logo_path_env = os.environ.get('LOGO_PATH', '')
            if _logo_path_env and Path(_logo_path_env).exists():
                shutil.copy2(_logo_path_env, str(project_dir / 'logo.png'))
                print(f"   [OK] logo.png ({Path(_logo_path_env).name})")

    # Обложка ролика: первый кадр начнётся с неё же, только ожившей.
    # Карта «имя аудиофайла → путь к обложке»: обложка ОДНА на файл (печём её
    # первым кадром, испечь можно только одну), поэтому карта, а не папка.
    _cover_map_raw = os.environ.get('BEGO_COVER_MAP', '')
    if _cover_map_raw:
        try:
            _cover_src = (json.loads(_cover_map_raw) or {}).get(audio_file.name, '')
            if _cover_src and Path(_cover_src).exists():
                # Расширение сохраняем: обложка едет в i2v как есть, а движок
                # разборчив к формату.
                _cover_ext = Path(_cover_src).suffix.lower() or '.png'
                if _cover_ext not in ('.png', '.jpg', '.jpeg', '.webp'):
                    _cover_ext = '.png'
                shutil.copy2(_cover_src, str(project_dir / f'cover{_cover_ext}'))
                print(f"   [OK] cover{_cover_ext} ({Path(_cover_src).name})")
        except Exception as _ce:
            print(f"   [WARN] cover_map: {_ce}")

    # Копирование single_prompt.txt (для режима видеогенерации single в GUI)
    copy_single_prompt(project_dir, os.environ.get('SINGLE_PROMPT_FILE', ''))

    # Вспомогательные .bat/.py в папку проекта не копируются: всем управляет BegoFrame.

    # Сохранение информации о проекте
    project_info = {
        'name': project_name,
        'audio_file': str(project_dir / audio_file.name),
        'style_guide': str(dest_style) if style_guide and style_guide.exists() else None,
        'project_dir': str(project_dir),
        'transcription': str(project_dir / 'transcription.json'),
        'montage_plan': str(project_dir / 'montage_plan.json'),
        # prompts файл будет создан позже (имя зависит от даты)
        'prompts': None,  # Будет обновлено после director
    }

    return project_info


if __name__ == '__main__':
    # Тест
    ready_dir = Path('../pipeline_queue')
    projects_dir = Path('../projects')
    concat_script = Path('../assemble.py')
    
    files = get_ready_files(ready_dir)
    print(f"Найдено файлов в pipeline_queue: {len(files)}")
    for f in files:
        print(f"  - {f.name}")
