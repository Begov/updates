"""cover_hook.py — оживление обложки первым кадром ролика.

Зачем
-----
Люди рисуют обложку, а под неё строят первые фразы: зритель кликнул — и сразу
слышит то, что видел на картинке. Идея пользователя: усилить это — начать ролик
С ТОЙ ЖЕ обложки, только ожившей. Кадр, который зритель выбрал глазами,
продолжается в движении.

Как устроено
------------
Обложку прикладывает человек, режиссёр ставит её ПЕРВЫМ клипом плана (0.0 → N
секунд, маркер `cover`). Дальше всё едет обычным путём: генератор картинок такой
клип пропускает (файл уже есть), а стадия видео берёт его как готовую картинку и
подставляет СВОЙ промпт вместо промпта сцены — тем же приёмом, что параллакс.

Длину задаёт ЧЕЛОВЕК, а не режиссёр: отдай решение режиссёру — растянет до
четырёх секунд, и зритель столько времени смотрит в неподвижную картинку.

Здесь только: где лежит файл, какой промпт выбран и разметка клипа. Работу
ведут `queue.py` (кладёт файл в проект) и `veo_video.py` (оживляет).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

import params as config
logger = logging.getLogger(__name__)

SOURCE = 'cover'
PENDING_SOURCE = 'cover_pending'
PROMPTS_FILENAME = 'cover_hook_prompts.txt'
COVER_FILENAME = 'cover'                 # расширение подставляется по факту
IMAGE_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.webp')

# Порядок = номера ПРОМПТ N в файле шаблонов.
MODES = ('zoom_in', 'zoom_out', 'drift', 'fly_in', 'awaken')
MODE_NAMES = {
    'zoom_in': 'наезд',
    'zoom_out': 'отъезд',
    'drift': 'дрейф',
    'fly_in': 'залёт в сцену',
    'awaken': 'оживание',
}


def hook_seconds() -> float:
    """Сколько длится обложка. 0 — оживление выключено."""
    try:
        return max(0.0, float(getattr(config, 'COVER_HOOK_SEC', 2) or 0))
    except (TypeError, ValueError):
        return 2.0


def hook_mode() -> str:
    mode = str(getattr(config, 'COVER_HOOK_MODE', '') or '').strip().lower()
    return mode if mode in MODES else MODES[0]


def mode_name(mode: str = '') -> str:
    return MODE_NAMES.get(mode or hook_mode(), '?')


def prompts_path() -> Path:
    """Одна точка правды — как у параллакса, файл едет с обновлением."""
    return Path(config.BASE_DIR) / 'instructions' / PROMPTS_FILENAME


def cover_path(project_dir: str | Path) -> Optional[Path]:
    """Приложенная обложка внутри проекта. None — не приложена."""
    project = Path(project_dir)
    for ext in IMAGE_EXTENSIONS:
        candidate = project / f'{COVER_FILENAME}{ext}'
        if candidate.is_file():
            return candidate
    return None


def enabled(project_dir: str | Path) -> bool:
    """Оживлять ли обложку в этом проекте.

    Отдельной галки нет намеренно: включает сам факт приложенного файла плюс
    ненулевая длительность. Лишний переключатель здесь только запутывал бы —
    приложил обложку, значит хочешь её увидеть.
    """
    return hook_seconds() > 0 and cover_path(project_dir) is not None


def load_templates(path: Path | None = None) -> dict[int, str]:
    """Шаблоны из файла: {номер: текст}.

    Файла нет → пустой словарь, а НЕ исключение: обложка это украшение начала,
    и её отсутствие не должно ронять весь прогон (тот же урок, что с
    параллаксом 29.08 — пропавший файл шаблонов убил стадию целиком).
    """
    prompt_path = path or prompts_path()
    try:
        text = prompt_path.read_text(encoding='utf-8-sig')
    except OSError:
        logger.warning('cover_hook: шаблоны не найдены (%s) — обложка останется '
                       'статичным кадром', prompt_path)
        return {}
    parts = re.split(r'\n-{20,}\s*\n', text)
    templates: dict[int, str] = {}
    for index, part in enumerate(parts[:-1]):
        match = re.search(r'ПРОМПТ\s+([1-9])', part, flags=re.IGNORECASE)
        if not match:
            continue
        body = parts[index + 1].strip()
        if body:
            templates[int(match.group(1))] = body
    return templates


def prompt_for(mode: str = '', path: Path | None = None) -> Optional[str]:
    """Текст промпта выбранного режима. None — шаблонов нет.

    Возвращаем None вместо исключения: вызывающий сам решает, оставить кадр
    статикой или пропустить оживление.
    """
    mode = mode or hook_mode()
    templates = load_templates(path)
    if not templates:
        return None
    try:
        number = MODES.index(mode) + 1
    except ValueError:
        number = 1
    return templates.get(number) or templates.get(1) or None


def cover_clip_id(project_dir: str | Path) -> Optional[int]:
    """Номер клипа-обложки в монтажном плане. None — режиссёр её не разметил."""
    import json

    plan_path = Path(project_dir) / 'montage_plan.json'
    try:
        raw = json.loads(plan_path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return None
    plan = raw.get('montage_plan', raw)
    for clip in (plan.get('clips') or []):
        if str(clip.get('source') or '') in (SOURCE, PENDING_SOURCE):
            try:
                return int(clip['id'])
            except (KeyError, TypeError, ValueError):
                return None
    return None


def stage_cover_image(project_dir: str | Path, printer=print) -> Optional[int]:
    """Положить обложку в `generated/` под номером её клипа.

    Стадия видео ищет картинку клипа именно там (`generated/{id:03d}.{ext}`) —
    это общая конвенция для всех готовых материалов, стока и архивов в том
    числе. Возвращает номер клипа или None.

    Заодно переводит маркер `cover_pending` → `cover`: пока файла не было,
    клип считался незакрытым.
    """
    import json

    project = Path(project_dir)
    source_file = cover_path(project)
    if source_file is None:
        return None
    clip_id = cover_clip_id(project)
    if clip_id is None:
        printer("  [Обложка] приложена, но режиссёр не разметил её первым "
                "клипом — оживлять нечего")
        return None

    generated = project / 'generated'
    generated.mkdir(exist_ok=True)
    target = generated / f'{clip_id:03d}{source_file.suffix.lower()}'
    if not target.exists():
        import shutil

        try:
            shutil.copy2(source_file, target)
        except OSError as exc:
            printer(f"  [Обложка] не скопировалась в generated/: {exc}")
            return None

    # Маркер на финальный: файл на месте.
    plan_path = project / 'montage_plan.json'
    try:
        raw = json.loads(plan_path.read_text(encoding='utf-8-sig'))
        plan = raw.get('montage_plan', raw)
        changed = False
        for clip in (plan.get('clips') or []):
            if int(clip.get('id', -1)) == clip_id and clip.get('source') != SOURCE:
                clip['source'] = SOURCE
                changed = True
        if changed:
            plan_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2),
                                 encoding='utf-8')
    except (OSError, ValueError, TypeError) as exc:
        printer(f"  [Обложка] маркер в плане не обновлён: {exc}")

    printer(f"  [Обложка] {source_file.name} → generated/{target.name} "
            f"(клип {clip_id:03d}, {hook_seconds():.1f}с, "
            f"режим «{mode_name()}»)")
    return clip_id


def animate_before_stage(project_dir: str | Path, generate_videos,
                         *, active_keys=None, aspect_ratio=None,
                         printer=print) -> Optional[int]:
    """Оживить обложку ДО основной стадии и вернуть её номер клипа.

    Нужно режимам, которые картинками не занимаются вовсе (прямое видео из
    текста, цепочки, компоненты): обложка там — единственный клип с готовой
    картинкой, и общей дороги у них нет.

    СТРОГО ПОСЛЕДОВАТЕЛЬНО, а не рядом с основной стадией: слоты у ключа одни
    на всех, и два вызова разом дрались бы за них (замечание пользователя
    02.09.2026). Сначала обложка, дождались — потом остальное.

    Вернувшийся номер вызывающий обязан ИСКЛЮЧИТЬ из списка для своей стадии:
    иначе она попробует сделать клип 1 заново и затрёт готовый файл.
    """
    project = Path(project_dir)
    clip_id = stage_cover_image(project, printer=printer)
    if clip_id is None:
        return None
    if (project / 'vid_img' / f'{clip_id:03d}.mp4').is_file():
        printer(f"  [Обложка] клип {clip_id:03d} уже оживлён — пропускаю")
        return clip_id
    printer(f"  [Обложка] оживляю до основной стадии "
            f"(режим «{mode_name()}», {hook_seconds():.1f}с)")
    try:
        generate_videos(
            project_dir=str(project),
            mode='i2v',
            clip_ids=[clip_id],
            silent=True,
            no_cancel=True,
            allow_external_image_clip_ids=[clip_id],
            active_keys=active_keys,
            aspect_ratio=aspect_ratio,
        )
    except Exception as exc:                                # noqa: BLE001
        # Обложка — украшение начала: кадр останется статикой и получит зум в
        # склейке. Ронять из-за неё весь прогон нельзя.
        printer(f"  [Обложка] не оживилась ({str(exc)[:160]}) — останется "
                f"статичным кадром")
    return clip_id


def director_note() -> str:
    """Указание режиссёру. Пустая строка — обложки нет, контекст не засоряем.

    Второе требование важнее первого: у клипа после обложки сдвигается только
    КАРТИНКА, но не речь — озвучка шла с нуля. Без этой оговорки режиссёр
    опишет сцену по обрывку фразы и потеряет начало мысли (замечание
    пользователя 02.09.2026).
    """
    seconds = hook_seconds()
    if seconds <= 0:
        return ''
    return (
        f"ОБЛОЖКА. Пользователь приложил обложку ролика. Первым клипом плана "
        f"поставь её: id 1, startTime 0.0, endTime {seconds:.1f}, "
        f"\"source\": \"{SOURCE}\". Описание и промпт для этого клипа не нужны — "
        f"картинка уже готова.\n"
        f"Следующий клип начинается с {seconds:.1f} с. ВАЖНО: первые "
        f"{seconds:.1f} с озвучены, просто показаны обложкой — поэтому, описывая "
        f"визуальное содержание следующего клипа, опирайся на речь С САМОГО "
        f"НАЧАЛА записи, а не только с {seconds:.1f} с. Иначе потеряешь начало "
        f"мысли."
    )
