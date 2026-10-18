#!/usr/bin/env python3
"""
Извлечение промпта для конкретного фрагмента из {project}_prompts_{date}.txt

Формат:
[промпт 1]

[промпт 2]

[промпт 3]
...

Важно:
- 4list_prompts.txt ЗАМОРОЖЕН (исключён из пайплайна)
- Используем только prompts.txt который создаётся в первом этапе
"""
import re
import os
from pathlib import Path


def find_prompts_file(directory: str = ".") -> str:
    """
    Найти файл промптов в директории по маске *_prompts_*.txt

    Args:
        directory: Директория для поиска

    Returns:
        Путь к файлу промптов или None
    """
    dir_path = Path(directory)
    prompts_files = list(dir_path.glob('*_prompts_*.txt'))
    
    if prompts_files:
        # Возвращаем самый новый по времени модификации
        prompts_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        return str(prompts_files[0])
    
    # Если не найдено - пробуем prompts.txt (для обратной совместимости)
    default_prompts = dir_path / 'prompts.txt'
    if default_prompts.exists():
        return str(default_prompts)
    
    return None


def extract_all_prompts(file_path: str = None) -> dict:
    """
    Извлекает все промпты из файла

    Args:
        file_path: Путь к файлу промптов (если None - ищется автоматически)

    Returns:
        Словарь {fragment_id: prompt_text}
    """
    try:
        # Если путь не указан - ищем файл
        if file_path is None:
            file_path = find_prompts_file()
            if file_path is None:
                return {}
        
        # Пробуем разные кодировки
        content = None
        for encoding in ['utf-8', 'cp1251', 'latin-1']:
            try:
                with open(file_path, 'r', encoding=encoding) as f:
                    content = f.read()
                break
            except UnicodeDecodeError:
                continue
        
        if content is None:
            return {}

        # Режем по МАРКЕРУ клипа, если он есть: промпт со стиль-блоком внутри
        # содержит пустые строки, и деление по ним отрывало у него хвост — тот
        # становился блоком без префикса и молча выбрасывался ниже, причём
        # проверка «префиксов не меньше половины» оставалась истинной, так что
        # потеря не была видна ни в логе, ни по числу промптов (аудит 25.08).
        marker_re = re.compile(r'^\s*\[(?:CLIP\s+)?\d+\]', re.IGNORECASE | re.MULTILINE)
        if marker_re.search(content):
            parts = re.split(r'\n(?=\s*\[(?:CLIP\s+)?\d+\])', content.strip(),
                             flags=re.IGNORECASE)
            prompts = [p.strip() for p in parts if p.strip()]
        else:
            prompts = [p.strip() for p in re.split(r'\n\s*\n', content.strip()) if p.strip()]

        prefixed = {}
        id_re = re.compile(r'^\s*\[(?:CLIP\s+)?(\d+)\]\s*(.*)$', re.IGNORECASE | re.DOTALL)
        for prompt in prompts:
            m = id_re.match(prompt)
            if not m:
                continue
            cid = int(m.group(1))
            body = m.group(2).strip()
            if body:
                prefixed[cid] = body
        if prefixed and len(prefixed) >= max(1, len(prompts) // 2):
            return prefixed

        result = {}
        for i, prompt in enumerate(prompts, start=1):
            result[i] = prompt.strip()

        return result

    except Exception as e:
        return {}


def remap_dense_prompts_for_montage(prompts: dict, clips: list) -> tuple:
    """
    Compatibility shim for projects created while Real Photo clips were removed
    before Pass 2. Those prompt files are dense 1..N, where N equals the number
    of non-real-photo clips, but the keys are not actual montage clip IDs.

    Returns: (prompts_map, remapped_bool)
    """
    if not prompts or not clips:
        return prompts, False

    # Точка правды — units/source_ledger.py. Но этот файл queue.py копирует в папку
    # проекта, и process_*.bat запускает его ОТТУДА (`python prompt_extract.py`), где
    # modules/ рядом нет — поэтому импорт с fallback, иначе копии в проектах падают.
    try:
        from units.source_ledger import EXTERNAL_SOURCES as real_sources
    except ImportError:
        real_sources = frozenset({
            'real_photo', 'real_photo_pending', 'real_video',
            'stock_pending', 'stock_photo', 'stock_video',
        })
    non_real_clips = [c for c in clips if c.get('source') not in real_sources]
    real_count = len(clips) - len(non_real_clips)
    if real_count <= 0:
        return prompts, False

    keys = set(prompts.keys())
    dense_keys = set(range(1, len(prompts) + 1))
    has_id_gaps = any(c.get('id') != i + 1 for i, c in enumerate(non_real_clips))
    if len(prompts) == len(non_real_clips) and keys == dense_keys and has_id_gaps:
        remapped = {
            int(clip['id']): prompts[i + 1]
            for i, clip in enumerate(non_real_clips)
            if (i + 1) in prompts and 'id' in clip
        }
        return remapped, True

    return prompts, False


