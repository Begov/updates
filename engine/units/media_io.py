"""
Запись и проверка медиафайлов проекта (картинки generated/, видео vid_img/).

Стадии считают клип готовым, если его файл есть на диске. Поэтому файл должен
появляться под своим именем только целиком: «Стоп» убивает процесс сразу, и прямая
запись оставляла обрезанный 001.png / 001.mp4, который следующий запуск принимал за
готовый. `atomic_open` / `write_bytes_atomic` пишут во временный `<имя>.part` рядом
и переименовывают его в итоговое имя только после успешной записи.

`quarantine_broken` находит уже лежащие битые файлы (обрезанные прошлыми запусками)
и откладывает их как `<имя>.broken` — тогда стадия сделает клип заново.
`archive_previous_run` перед повторной режиссурой уносит результат прошлого плана в
`old_<дата>/`, чтобы новые сцены не получили кадры старых. Ничего не удаляется.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, List, Optional

IMAGE_EXTS = ('.png', '.jpg', '.jpeg', '.webp')
VIDEO_EXTS = ('.mp4', '.mov', '.mkv', '.webm')


def _part_path(path: Path) -> Path:
    return path.with_name(path.name + '.part')


@contextmanager
def atomic_target(path) -> Iterator[Path]:
    """Временный путь для записи; при успехе он становится `path`, при ошибке удаляется."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = _part_path(target)
    try:
        yield tmp
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


@contextmanager
def atomic_open(path):
    """Как `open(path, 'wb')`, но файл появляется под своим именем только целиком."""
    with atomic_target(path) as tmp:
        with open(tmp, 'wb') as fh:
            yield fh


def write_bytes_atomic(path, data: bytes) -> None:
    with atomic_target(path) as tmp:
        tmp.write_bytes(data)


def image_ok(path) -> bool:
    """Картинка читается целиком (обрезанная падает на полной декодировке)."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            im.load()
        return True
    except Exception:                                              # noqa: BLE001
        return False


def video_ok(path) -> bool:
    try:
        from units.veo_client import _verify_video_integrity
    except ImportError:                                            # pragma: no cover
        from veo_client import _verify_video_integrity
    return bool(_verify_video_integrity(Path(path)))


MEDIA_DIRS = ('generated', 'vid_img', 'vid_img_cinai')


def has_media(project_dir) -> bool:
    """Есть ли у проекта картинки, видео или собранный ролик (результат прошлого плана)."""
    project = Path(project_dir)
    if (project / f"{project.name}.mp4").is_file():
        return True
    return any(f.is_file() for d in MEDIA_DIRS if (project / d).is_dir() for f in (project / d).iterdir())


def archive_previous_run(project_dir, printer: Optional[Callable[[str], None]] = print) -> Optional[Path]:
    """Перед новой режиссурой убрать результат прошлого плана в `old_<дата>/` проекта.

    Картинки и видео привязаны к номерам клипов плана: новый план с теми же номерами
    получил бы чужие кадры. Переносятся: generated/, vid_img/, vid_img_cinai/, mograph/,
    montage_plan.json, промпты, адаптированные промпты, список параллакса и собранный
    ролик. Аудио, расшифровка, стайлгайд, обложка, логотип и журналы остаются.
    Ничего не удаляется. None — переносить нечего.
    """
    import datetime as _dt

    project = Path(project_dir)
    if not has_media(project):
        return None
    archive = project / f"old_{_dt.datetime.now():%Y%m%d_%H%M%S}"
    archive.mkdir()
    items = [project / d for d in MEDIA_DIRS + ('mograph',)]
    items += [project / 'montage_plan.json', project / 'parallax_plan.json', project / f"{project.name}.mp4"]
    items += sorted(project.glob('*_prompts_*.txt')) + sorted(project.glob('*_veo_adapt_*.txt'))
    moved = []
    for item in items:
        if item.exists():
            os.replace(item, archive / item.name)
            moved.append(item.name)
    if printer:
        printer(f"  [Архив] результат прошлого плана перенесён в {archive.name}/: {', '.join(moved)}")
    return archive


def quarantine_broken(project_dir, printer: Optional[Callable[[str], None]] = print) -> List[str]:
    """Отложить битые картинки и видео проекта как `<имя>.broken`. Возвращает их имена.

    Проверяются generated/ и vid_img/ (и vid_img_cinai/). Пустые файлы — тоже битые.
    Недописанные `*.part` от убитых процессов убираются туда же.
    """
    project = Path(project_dir)
    moved: List[str] = []
    for folder in ('generated', 'vid_img', 'vid_img_cinai'):
        directory = project / folder
        if not directory.is_dir():
            continue
        for f in sorted(directory.iterdir()):
            if not f.is_file():
                continue
            suffix = f.suffix.lower()
            if suffix == '.part':
                broken = True
            elif suffix in IMAGE_EXTS:
                broken = f.stat().st_size == 0 or not image_ok(f)
            elif suffix in VIDEO_EXTS:
                broken = f.stat().st_size == 0 or not video_ok(f)
            else:
                continue
            if not broken:
                continue
            target = f.with_name(f.name + '.broken')
            n = 1
            while target.exists():
                target = f.with_name(f"{f.name}.broken{n}")
                n += 1
            try:
                os.replace(f, target)
                moved.append(f"{folder}/{f.name}")
            except OSError:
                continue
    if moved and printer:
        printer(f"  [Проверка файлов] битые файлы отложены как *.broken и будут сделаны заново: "
                f"{len(moved)} ({', '.join(moved[:10])}{'…' if len(moved) > 10 else ''})")
    return moved
