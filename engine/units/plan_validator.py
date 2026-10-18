"""
Модуль валидации данных
"""
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple


def _profile_duration_bounds() -> Tuple[float, float]:
    """Границы длительности сцены из настроек пользователя.

    Раньше валидатор судил по своим 3.0/12.0 и ругался на законные сцены:
    юзер выставил диапазон 2-4, а план с 2.4-секундной сценой получал
    «слишком короткая сцена» (прогон 28.08). Заодно сцена в 10с при потолке
    4с проходила молча — потолок тоже был чужой.

    Интро-диапазон учитываем, только если он реально задан: в config его
    умолчание 0 означает «отдельного диапазона у интро нет», и слепой
    min() с нулём отключил бы проверку совсем.
    """
    try:
        import params as config
    except Exception:                                   # noqa: BLE001
        return 3.0, 12.0
    try:
        low = float(getattr(config, 'VIDEO_CLIP_MIN_DURATION', 3) or 3)
        high = float(getattr(config, 'VIDEO_CLIP_MAX_DURATION', 12) or 12)
    except (TypeError, ValueError):
        return 3.0, 12.0
    try:
        intro_low = float(getattr(config, 'INTRO_CLIP_MIN_DURATION', 0) or 0)
        intro_high = float(getattr(config, 'INTRO_CLIP_MAX_DURATION', 0) or 0)
    except (TypeError, ValueError):
        intro_low = intro_high = 0.0
    if intro_low > 0:
        low = min(low, intro_low)
    if intro_high > 0:
        high = max(high, intro_high)
    if high < low:
        low, high = high, low
    return low, high


def validate_transcription_json(data: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """
    Валидация transcription.json
    
    Args:
        data: Словарь с транскрипцией
        
    Returns:
        (is_valid, список ошибок)
    """
    errors = []
    
    # Проверка поля audioDuration
    if 'audioDuration' not in data:
        errors.append("Отсутствует поле 'audioDuration'")
    elif not isinstance(data['audioDuration'], (int, float)):
        errors.append("Поле 'audioDuration' должно быть числом")
    elif data['audioDuration'] <= 0:
        errors.append("Поле 'audioDuration' должно быть положительным")
    
    # Проверка поля transcription
    if 'transcription' not in data:
        errors.append("Отсутствует поле 'transcription'")
    elif not isinstance(data['transcription'], list):
        errors.append("Поле 'transcription' должно быть массивом")
    elif len(data['transcription']) == 0:
        errors.append("Массив 'transcription' пуст")
    else:
        # Проверка каждого фрагмента
        prev_end = 0
        for i, fragment in enumerate(data['transcription']):
            # Обязательные поля
            for field in ['id', 'text', 'start', 'end']:
                if field not in fragment:
                    errors.append(f"Фрагмент {i+1}: отсутствует поле '{field}'")
            
            # Проверка типов
            if 'id' in fragment and not isinstance(fragment['id'], int):
                errors.append(f"Фрагмент {i+1}: 'id' должно быть целым числом")
            
            if 'text' in fragment and not isinstance(fragment['text'], str):
                errors.append(f"Фрагмент {i+1}: 'text' должно быть строкой")
            
            if 'start' in fragment:
                if not isinstance(fragment['start'], (int, float)):
                    errors.append(f"Фрагмент {i+1}: 'start' должно быть числом")
                elif fragment['start'] < 0:
                    errors.append(f"Фрагмент {i+1}: 'start' не может быть отрицательным")
            
            if 'end' in fragment:
                if not isinstance(fragment['end'], (int, float)):
                    errors.append(f"Фрагмент {i+1}: 'end' должно быть числом")
                elif fragment['end'] <= fragment.get('start', 0):
                    errors.append(f"Фрагмент {i+1}: 'end' должно быть больше 'start'")
            
            # Проверка последовательности
            if i > 0 and 'start' in fragment:
                if fragment['start'] < prev_end - 0.1:  # Допуск 0.1 сек
                    errors.append(f"Фрагмент {i+1}: 'start' ({fragment['start']}) < предыдущий 'end' ({prev_end})")
            
            if 'end' in fragment:
                prev_end = fragment['end']
    
    is_valid = len(errors) == 0
    return is_valid, errors


def validate_montage_plan_json(
    data: Dict[str, Any],
    audio_duration: float,
    *,
    min_duration: Optional[float] = None,
    max_duration: Optional[float] = None,
) -> Tuple[bool, List[str]]:
    """
    Валидация montage_plan.json

    Args:
        data: Словарь с монтажным планом
        audio_duration: Длительность аудио из transcription.json
        min_duration: нижняя граница сцены; None — берём диапазон пользователя
        max_duration: верхняя граница сцены; None — берём диапазон пользователя

    Returns:
        (is_valid, список ошибок)
    """
    if min_duration is None or max_duration is None:
        profile_min, profile_max = _profile_duration_bounds()
        if min_duration is None:
            min_duration = profile_min
        if max_duration is None:
            max_duration = profile_max

    errors = []
    
    # Проверка поля audioDuration
    if 'audioDuration' not in data:
        errors.append("Отсутствует поле 'audioDuration'")
    elif not isinstance(data['audioDuration'], (int, float)):
        errors.append("Поле 'audioDuration' должно быть числом")
    elif abs(data['audioDuration'] - audio_duration) > 0.1:
        errors.append(f"audioDuration ({data['audioDuration']}) не совпадает с аудио ({audio_duration})")
    
    # Проверка поля clips
    if 'clips' not in data:
        errors.append("Отсутствует поле 'clips'")
    elif not isinstance(data['clips'], list):
        errors.append("Поле 'clips' должно быть массивом")
    elif len(data['clips']) == 0:
        errors.append("Массив 'clips' пуст")
    else:
        # Проверка каждого клипа
        prev_clip = None
        for i, clip in enumerate(data['clips']):
            # Обязательные поля
            for field in ['id', 'file', 'startTime', 'endTime']:
                if field not in clip:
                    errors.append(f"Clip {i+1}: отсутствует поле '{field}'")
            
            # Проверка типов
            if 'id' in clip and not isinstance(clip['id'], int):
                errors.append(f"Clip {i+1}: 'id' должно быть целым числом")
            
            if 'file' in clip and not isinstance(clip['file'], str):
                errors.append(f"Clip {i+1}: 'file' должно быть строкой")
            
            if 'startTime' in clip:
                if not isinstance(clip['startTime'], (int, float)):
                    errors.append(f"Clip {i+1}: 'startTime' должно быть числом")
                elif clip['startTime'] < 0:
                    errors.append(f"Clip {i+1}: 'startTime' не может быть отрицательным")
            
            if 'endTime' in clip:
                if not isinstance(clip['endTime'], (int, float)):
                    errors.append(f"Clip {i+1}: 'endTime' должно быть числом")
                elif clip['endTime'] <= clip.get('startTime', 0):
                    errors.append(f"Clip {i+1}: 'endTime' должно быть больше 'startTime'")
            
            # Проверка длительности сцены по активному профилю.
            if 'startTime' in clip and 'endTime' in clip:
                duration = clip['endTime'] - clip['startTime']
                if duration < min_duration:
                    errors.append(
                        f"Clip {i+1}: Длительность {duration:.1f}с < {min_duration:.1f}с — слишком короткая сцена!\n"
                        f"   → Минимум: {min_duration:.1f} секунды"
                    )
                elif duration > max_duration:
                    errors.append(
                        f"Clip {i+1}: Длительность {duration:.1f}с > {max_duration:.1f}с — слишком длинная сцена!\n"
                        f"   → Максимум: {max_duration:.1f} секунд\n"
                        f"   → Если пауза > 3 сек, создай бонусный клип"
                    )

            # Проверка последовательности
            if prev_clip is not None and 'startTime' in clip:
                gap = clip['startTime'] - prev_clip.get('endTime', 0)
                
                # ⚠️  ПРОВЕРКА НА ПАУЗЫ:
                # Если gap > 0.1 сек — это означает что предыдущая сцена
                # НЕ включила паузу в свой endTime. Это ошибка режиссера!
                if gap > 0.1:
                    # Проверяем: это бонусный клип для длинной паузы?
                    # Бонусный клип должен быть < 3.5 сек
                    bonus_duration = gap
                    
                    if bonus_duration > 3.5:
                        # Слишком длинная пауза без бонусного клипа!
                        errors.append(
                            f"Clip {i+1}: ОБНАРУЖЕН ГЭП {gap:.2f} сек между clip {prev_clip.get('id')} "
                            f"(end: {prev_clip.get('endTime'):.2f}) и clip {clip.get('id')} "
                            f"(start: {clip.get('startTime'):.2f}).\n"
                            f"   → Пауза {gap:.2f}с > 3.0с! Создайте бонусный clip между ними."
                        )
                
                # Отрицательный gap (перекрытие) — тоже ошибка
                if gap < -0.1:
                    errors.append(
                        f"Clip {i+1}: ПЕРЕКРЫТИЕ {abs(gap):.2f} сек между clip {prev_clip.get('id')} "
                        f"и clip {clip.get('id')}"
                    )

            prev_clip = clip
        
        # Проверка покрытия
        if data['clips']:
            first_clip = data['clips'][0]
            last_clip = data['clips'][-1]
            
            if first_clip.get('startTime', 0) > 0.1:
                errors.append(f"Первый clip начинается с {first_clip.get('startTime')}, а не с 0.00")
            
            if last_clip.get('endTime', 0) < audio_duration - 0.1:
                errors.append(f"Последний clip заканчивается на {last_clip.get('endTime')}, а аудио на {audio_duration}")

        # Валидация chain_ids (если присутствуют)
        chain_errors = _validate_chain_ids(data['clips'])
        errors.extend(chain_errors)

    is_valid = len(errors) == 0
    return is_valid, errors


def check_clip_id_sequence(clips: list) -> List[dict]:
    """
    Проверяет, что ID клипов идут строго последовательно: 1, 2, 3, ..., N.

    Возвращает список проблем вида:
        [{'position': 65, 'expected': 65, 'actual': 5}, ...]
    Пустой список = всё OK.
    """
    problems = []
    for i, clip in enumerate(clips):
        expected = i + 1
        actual = clip.get('id')
        if actual != expected:
            problems.append({
                'position': i + 1,   # порядковый номер в массиве (1-based)
                'expected': expected,
                'actual': actual,
            })
    return problems


def validate_file_exists(path: str, file_type: str = "Файл") -> Tuple[bool, str]:
    """
    Проверка существования файла
    
    Args:
        path: Путь к файлу
        file_type: Описание типа файла для сообщения
        
    Returns:
        (exists, сообщение)
    """
    path = Path(path)
    
    if not path.exists():
        return False, f"{file_type} не найден: {path}"
    
    if not path.is_file():
        return False, f"{file_type} не является файлом: {path}"
    
    return True, f"✓ {file_type} найден: {path}"


def validate_transcription_quality(data: Dict[str, Any], language_code: str = 'ru') -> Tuple[List[str], bool]:
    """
    Проверка КАЧЕСТВА транскрипции (не формата, а содержания).
    Ловит: транслит вместо кириллицы, потерю речи, аномально низкую плотность фраз.

    Args:
        data: Словарь с транскрипцией
        language_code: Ожидаемый язык ('ru', 'en', 'es', ...)

    Returns:
        (warnings: list[str], is_critical: bool)
    """
    warnings = []
    is_critical = False

    audio_duration = data.get('audioDuration', 0)
    fragments = data.get('transcription', [])

    if not fragments or audio_duration <= 0:
        return ['Пустая транскрипция или нулевая длительность'], True

    # 1. ПОКРЫТИЕ: сколько % аудио покрыто речью
    speech_duration = sum(f['end'] - f['start'] for f in fragments)
    coverage = speech_duration / audio_duration if audio_duration > 0 else 0

    if coverage < 0.10:
        warnings.append(
            f'ПОКРЫТИЕ КРИТИЧЕСКИ НИЗКОЕ: {coverage:.0%} '
            f'(речь {speech_duration:.0f}с из {audio_duration:.0f}с). '
            f'Возможно AssemblyAI не распознал речь.'
        )
        is_critical = True
    elif coverage < 0.30:
        warnings.append(
            f'Покрытие низкое: {coverage:.0%} '
            f'(речь {speech_duration:.0f}с из {audio_duration:.0f}с)'
        )

    # 2. ПЛОТНОСТЬ: фраз на минуту
    minutes = audio_duration / 60
    density = len(fragments) / minutes if minutes > 0 else 0

    if density < 3:
        warnings.append(
            f'ПЛОТНОСТЬ КРИТИЧЕСКИ НИЗКАЯ: {density:.1f} фраз/мин '
            f'({len(fragments)} фраз на {minutes:.1f} мин). '
            f'Ожидается 8-20 фраз/мин для нормальной речи.'
        )
        is_critical = True
    elif density < 5:
        warnings.append(
            f'Плотность низкая: {density:.1f} фраз/мин '
            f'({len(fragments)} фраз на {minutes:.1f} мин)'
        )

    # 3. ЯЗЫК: проверка что текст содержит символы ожидаемого алфавита
    if language_code == 'ru':
        all_text = ' '.join(f['text'] for f in fragments)
        cyrillic_chars = sum(1 for c in all_text if '\u0400' <= c <= '\u04ff')
        latin_chars = sum(1 for c in all_text if 'a' <= c.lower() <= 'z')
        total_alpha = cyrillic_chars + latin_chars

        if total_alpha > 0:
            cyrillic_ratio = cyrillic_chars / total_alpha
            if cyrillic_ratio < 0.3:
                warnings.append(
                    f'ЯЗЫК: ожидался русский (кириллица), но текст на '
                    f'{cyrillic_ratio:.0%} кириллица / {1-cyrillic_ratio:.0%} латиница. '
                    f'Возможно AssemblyAI вернул транслит.'
                )
                is_critical = True

    # 4. МАКС ГЭПЫ: проверяем не потерялась ли середина аудио
    if len(fragments) >= 2:
        max_gap = 0
        max_gap_pos = ''
        for i in range(len(fragments) - 1):
            gap = fragments[i+1]['start'] - fragments[i]['end']
            if gap > max_gap:
                max_gap = gap
                max_gap_pos = f'{fragments[i]["end"]:.0f}с-{fragments[i+1]["start"]:.0f}с'

        if max_gap > 120:
            warnings.append(
                f'Гэп {max_gap:.0f}с в зоне {max_gap_pos} — '
                f'возможно потерян большой фрагмент аудио'
            )

    return warnings, is_critical


def _validate_chain_ids(clips: list) -> List[str]:
    """
    Валидация chain_id полей в clips (если присутствуют).

    Проверяет:
    - chain_id монотонно не убывает
    - Клипы внутри одного chain подряд по id (без разрывов)

    Returns:
        List[str]: список ошибок (пустой если всё ОК)
    """
    errors = []
    if not clips:
        return errors

    prev_chain = 0
    current_chain_clips = []

    for clip in sorted(clips, key=lambda c: c.get('id', 0)):
        cid = clip.get('chain_id')
        if cid is None:
            # No chain_id = skip validation (back-compat)
            continue

        # chain_id должен быть int
        if not isinstance(cid, int):
            errors.append(f"Clip {clip.get('id')}: chain_id должен быть int, получен {type(cid).__name__}")
            continue

        # chain_id не может быть меньше предыдущего (монотонность)
        if cid < prev_chain:
            errors.append(
                f"Clip {clip.get('id')}: chain_id={cid} меньше предыдущего chain_id={prev_chain} "
                "(chain_id должен монотонно не убывать)"
            )

        # Новый chain started
        if cid != prev_chain:
            # Проверяем что предыдущий chain был consecutive
            if current_chain_clips:
                ids = [c.get('id', 0) for c in current_chain_clips]
                expected = list(range(ids[0], ids[0] + len(ids)))
                if ids != expected:
                    errors.append(
                        f"chain_id={prev_chain}: клипы не подряд по id (ожидалось {expected}, получено {ids})"
                    )
            current_chain_clips = [clip]
            prev_chain = cid
        else:
            current_chain_clips.append(clip)

    # Проверка последнего chain
    if current_chain_clips:
        ids = [c.get('id', 0) for c in current_chain_clips]
        expected = list(range(ids[0], ids[0] + len(ids)))
        if ids != expected:
            errors.append(
                f"chain_id={prev_chain}: клипы не подряд по id (ожидалось {expected}, получено {ids})"
            )

    return errors


