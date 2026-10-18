"""
clip_sources.py — единый реестр маркеров `source` в montage_plan.json.

Клип с любым из этих маркеров обслуживает ВНЕШНЯЯ стадия (архив или сток), а не
нейрогенерация. ImgGen / FastGen / Veo / FlowUltra обязаны такие клипы пропускать:
иначе картинка сгенерится поверх скачанной — конфликт файлов и сожжённая квота,
причём молча.

Зачем модуль: литерал ('real_photo', 'real_photo_pending', 'real_video') был
скопирован в 7 файлов. Добавление нового источника требовало не забыть ни один —
забыл хоть один, получил двойную генерацию без единой ошибки в логе. Теперь точка
правды одна.

Значения маркеров
-----------------
Real Photo stage (архивные материалы: Wikipedia/NASA/LoC/Archive/Yandex/...):
  real_photo_pending — режиссёр пометил клип, стадия ещё не отработала
  real_photo         — архивное ФОТО скачано (лежит в generated/{id:03d}.{ext})
  real_video         — архивное ВИДЕО готово (лежит в vid_img/{id:03d}.mp4)
                       (видео-путь сейчас отключён, маркер зарезервирован)

Stock stage (Pexels / Pixabay / Coverr — современный постановочный футаж):
  stock_pending      — режиссёр пометил клип, стадия ещё не отработала
  stock_photo        — сток-ФОТО скачано (generated/{id:03d}.{ext}; concat сам
                       сделает Ken Burns)
  stock_video        — сток-ВИДЕО готово (vid_img/{id:03d}.mp4; уже обрезано под
                       длину клипа, zoom не нужен)

Сток и архив намеренно РАЗНЫЕ маркеры: у них разные источники, лицензии и смысл
(архив — исторический материал, сток — современная съёмка), и разные стадии.
"""
from __future__ import annotations

# Архивная стадия (real_photo.py)
REAL_PHOTO_SOURCES = frozenset({
    'real_photo',
    'real_photo_pending',
    'real_video',
})

# Стоковая стадия (Pexels/Pixabay/Coverr)
STOCK_SOURCES = frozenset({
    'stock_pending',
    'stock_photo',
    'stock_video',
})

# YouTube Footage stage. ``youtube_pending`` is assigned by a director;
# ``youtube`` is a rendered video in ``vid_img/``.
YOUTUBE_SOURCES = frozenset({
    'youtube_pending',
    'youtube',
    'youtube_video',
})

# AI-generated information graphics. Unlike archival/stock sources, these
# clips MUST go through image generation but MUST NOT be sent to VideoGen.
INFOGRAPHIC_SOURCES = frozenset({
    'infographic',
})

# Моушн-графика (Remotion + Codex). Режиссёр ставит ``motion_graphics_pending``,
# стадия (mograph_runner.py) рендерит сцену в vid_img/{id:03d}.mp4 и меняет метку
# на ``motion_graphics``. Картинку для такого клипа не генерируем и в VideoGen не шлём.
MOTION_GRAPHICS_SOURCES = frozenset({
    'motion_graphics_pending',
    'motion_graphics',
})

# Обложка ролика, приложенная пользователем. Первый клип плана: зритель кликает
# на обложку — и ролик начинается с неё же, только ожившей. Картинку НЕ
# генерируем (она пришла файлом), но в оживление отправляем со своим промптом.
#
# Своя метка, а не стоковая: иначе обложка смешалась бы со стоком в подсчётах,
# отчётах и атрибуции, хотя источник у неё совсем другой.
COVER_SOURCES = frozenset({
    'cover_pending',                # режиссёр разметил, файл ещё не подложен
    'cover',                        # файл на месте в generated/
})

# Сцена, которую нечем было закрыть при запрещённой нейрогенерации (доля 0 или
# «Пропустить»): ей отдан кадр соседнего клипа (source_quota.fill_without_ai).
# ``filler_pending`` — «удержание»: режиссёр решил, что сцене не подходит ни один
# источник, кадр соседа подложит Pre-concat fallback. ``filler_photo`` — картинка
# уже лежит в generated/. В обоих случаях генераторам здесь делать нечего.
FILLER_SOURCES = frozenset({
    'filler_pending',
    'filler_photo',
})

# Всё, что НЕ должно попасть в нейрогенерацию.
# Именно этот набор проверяют ImgGen/FastGen/Veo/FlowUltra/watch_i2v.
# Обложка здесь же: её картинка пришла файлом от пользователя, рисовать нечего.
EXTERNAL_SOURCES = (REAL_PHOTO_SOURCES | STOCK_SOURCES | YOUTUBE_SOURCES
                    | COVER_SOURCES | MOTION_GRAPHICS_SOURCES | FILLER_SOURCES)

# Everything that must skip Veo / Flow Ultra / i2v watchers. Infographics are
# generated as still images by a dedicated image route, then concat decides
# whether to retain the still frame or apply the optional motion treatment.
VIDEO_SKIP_SOURCES = EXTERNAL_SOURCES | INFOGRAPHIC_SOURCES

# Клипы, чей материал кладётся в vid_img/ готовым видео (concat берёт как фрагмент,
# Ken Burns не применяется).
VIDEO_SOURCES = frozenset({
    'real_video',
    'stock_video',
    'youtube',
    'youtube_video',
    'motion_graphics',
})

# Клипы, чей материал кладётся картинкой в generated/ (concat делает Ken Burns).
PHOTO_SOURCES = frozenset({'real_photo', 'stock_photo', 'infographic',
                          'cover', 'filler_photo'})

# Auto Visual Story can selectively animate only these already-downloaded
# documentary/still sources with the VEO parallax route.
AUTO_VISUAL_PARALLAX_PHOTO_SOURCES = frozenset({'real_photo', 'stock_photo'})

# Готовые картинки, которые РАЗРЕШЕНО отправлять в оживление. Стадия видео по
# метке источника решает, кого пропустить как готовый материал, а кого пустить
# в i2v; всё остальное внешнее едет в склейку статикой.
ANIMATABLE_STILL_SOURCES = AUTO_VISUAL_PARALLAX_PHOTO_SOURCES | frozenset({'cover'})


def parallax_photo_sources() -> frozenset:
    """Какие ГОТОВЫЕ фото пользователь разрешил оживлять параллаксом.

    Галки стоят рядом с самим параллаксом в GUI. Нейрокадры сюда не входят:
    они и так проходят обычный i2v, и за них отвечает галка «AI» внутри
    генератора. Здесь только источники, которые генератор иначе пропустил бы
    как внешние (сток и архивные фото).
    """
    import params as config
    selected = set()
    if getattr(config, 'VEONONSTOP_I2V_PARALLAX_STOCK', False):
        selected.add('stock_photo')
    if getattr(config, 'VEONONSTOP_I2V_PARALLAX_REAL_PHOTO', False):
        selected.add('real_photo')
    return frozenset(selected)

# Still sources that must be rendered by concat without a Ken Burns movement
# when INFOGRAPHIC_STATIC_CONCAT is enabled.
STATIC_NO_MOTION_SOURCES = INFOGRAPHIC_SOURCES


def clip_media_ready(project_dir, clip) -> bool:
    """Есть ли у клипа готовый файл.

    Одна проверка на всех, кто решает судьбу маркера: интро-гейт (снимать
    маркер нельзя — сцену сгенерируют заново поверх готовой) и защита режима
    «только не-AI» (объявлять сцену нерешённой нельзя — она уже закрыта).
    Раньше жила вложенной в `apply_intro_gate`, и защита Non-AI такой проверки
    не имела вовсе: клип 10 прогона 26.08 EPF закрыл, а защита вернула ему
    `real_photo_pending` — склейка перестала считать кадр архивным и выпустила
    его без подписи источника.
    """
    from pathlib import Path

    project = Path(project_dir)
    clip_id = clip.get('id')
    try:
        clip_id = int(clip_id)
    except (TypeError, ValueError):
        return False
    declared = str(clip.get('file') or '').strip()
    if declared and (project / declared).exists():
        return True
    if (project / 'vid_img' / f'{clip_id:03d}.mp4').exists():
        return True
    if _clip_parts_on_disk(project, clip_id, clip):
        return True
    return any((project / 'generated').glob(f'{clip_id:03d}.*'))


def _clip_parts_on_disk(project, clip_id: int, clip=None) -> bool:
    """Сцена собрана из НЕСКОЛЬКИХ кусков — `001_1.mp4`, `001_2.mp4`.

    Стадия YouTube отдаёт скачанные отрезки как есть, а к формату их приводит
    склейка (см. `_process_clip_parts` в assemble.py). Проверки «сцена уже
    закрыта» искали ОДИН файл `vid_img/001.mp4`, такого при кусках нет — и
    архивы шли добирать кадр поверх готового ютуб-материала, переписывая `file`
    в плане на картинку (улов прогона AVS 27.08: так переписаны 8 сцен из 12).
    """
    from pathlib import Path

    declared_parts = (clip or {}).get('parts')
    if isinstance(declared_parts, (list, tuple)) and declared_parts:
        if all((Path(project) / str(item)).exists() for item in declared_parts):
            return True
    vid_dir = Path(project) / 'vid_img'
    if not vid_dir.is_dir():
        return False
    stem = f'{int(clip_id):03d}'
    for ext in ('.mp4', '.mov', '.mkv', '.webm'):
        if any(vid_dir.glob(f'{stem}_*{ext}')):
            return True
    return False


def final_marker_for(project_dir, clip, stage: str) -> str | None:
    """Финальный маркер для клипа с УЖЕ готовым файлом.

    `*_pending` означает «стадия ещё не отработала». Когда файл на диске, такой
    маркер врёт: склейка не узнаёт архивный кадр (нет подписи источника и
    blur-заливки, `PHOTO_SOURCES` его не содержит), а повторный запуск шлёт
    стадию искать заново поверх готового.

    Видео в `vid_img/` → видео-маркер стадии, картинка в `generated/` → фото.
    Стадия неизвестна или файла нет — None, вызывающий не трогает маркер.
    """
    from pathlib import Path

    project = Path(project_dir)
    clip_id = clip.get('id')
    try:
        clip_id = int(clip_id)
    except (TypeError, ValueError):
        return None
    if not clip_media_ready(project, clip):
        return None

    # Расширению в `file` верить нельзя: Pass 1 проставляет ВСЕМ клипам
    # заглушку вида `10.mp4` ещё до того, как хоть что-то скачано. Видео — это
    # только реально существующий mp4 (проверено на клипе 10 прогона 26.08:
    # архивное ФОТО получало маркер `real_video` из-за заглушки).
    declared = str(clip.get('file') or '').strip()
    declared_path = (project / declared) if declared else None
    is_video = bool(
        (declared_path is not None
         and declared_path.suffix.lower() == '.mp4'
         and declared_path.exists())
        or (project / 'vid_img' / f'{clip_id:03d}.mp4').exists()
        or _clip_parts_on_disk(project, clip_id, clip)
    )
    finals = {
        'real_photo': ('real_video', 'real_photo'),
        'stock': ('stock_video', 'stock_photo'),
        # У ютуба картинки не бывает: стадия кладёт только видеофрагмент.
        'youtube': ('youtube', 'youtube'),
        'infographic': ('infographic', 'infographic'),
        # Графика бывает только видео
        'motion_graphics': ('motion_graphics', 'motion_graphics'),
    }.get(stage)
    if not finals:
        return None
    return finals[0] if is_video else finals[1]


def is_external(source: str | None) -> bool:
    """True, если клип обслуживает внешняя стадия и генератор должен его пропустить."""
    return bool(source) and source in EXTERNAL_SOURCES


def is_video_skipped(source: str | None) -> bool:
    """True when a clip has a still/external source and must not reach VideoGen."""
    return bool(source) and source in VIDEO_SKIP_SOURCES


def materialize_static_images(project_dir, sources=INFOGRAPHIC_SOURCES, printer=print) -> list[int]:
    """Copy generated stills for static sources into ``vid_img/``.

    This gives the concat stage an explicit source file and makes infographic
    clips visible as completed media before any VideoGen work finishes.
    """
    from pathlib import Path
    import json
    import shutil

    project = Path(project_dir)
    plan_path = project / 'montage_plan.json'
    if not plan_path.exists():
        return []
    try:
        raw = json.loads(plan_path.read_text(encoding='utf-8-sig'))
        plan = raw.get('montage_plan', raw)
        clips = plan.get('clips', [])
    except Exception:
        return []

    generated = project / 'generated'
    vid_img = project / 'vid_img'
    vid_img.mkdir(exist_ok=True)
    copied: list[int] = []
    for clip in clips:
        if clip.get('source') not in sources:
            continue
        try:
            clip_id = int(clip.get('id'))
        except (TypeError, ValueError):
            continue
        target_exists = any((vid_img / f'{clip_id:03d}{ext}').exists()
                            for ext in ('.png', '.jpg', '.jpeg', '.webp', '.mp4'))
        if target_exists:
            continue
        source_file = next(
            (generated / f'{clip_id:03d}{ext}'
             for ext in ('.png', '.jpg', '.jpeg', '.webp')
             if (generated / f'{clip_id:03d}{ext}').exists()),
            None,
        )
        if source_file is None:
            continue
        shutil.copy2(source_file, vid_img / source_file.name)
        copied.append(clip_id)
    if copied:
        printer(f"  [Infographic] Static images copied to vid_img/: {len(copied)}")
    return copied


def apply_intro_gate(montage_path, sources, start_sec: float, *,
                     label: str = 'stage', printer=print) -> list:
    """Снять маркеры стадии с клипов, начинающихся раньше `start_sec`.

    Вступление ролика — это хук: там нужна динамика собственной генерации, а не
    статичное фото, стоковая нарезка или диаграмма. Режиссёр про это не знает,
    поэтому чистим уже размеченный план: клип теряет `source` и его подхватывает
    обычный ImageGen/VideoGen (дырки в таймлайне не образуется).

    Ровно тот же приём, что у Real Photo (`_apply_intro_gate`), но общий —
    чтобы Сток и Инфографика вели себя одинаково и не расходились.

    Возвращает список id, с которых маркер снят. `start_sec <= 0` = гейт выкл.
    """
    import json
    from pathlib import Path

    import logging

    try:
        from units.real_source_policy import enabled as non_ai_enabled
        if non_ai_enabled():
            return []
    except Exception as exc:                     # noqa: BLE001
        # Молчать нельзя: не узнав про режим «только не-AI», гейт снимет маркеры
        # и отдаст клипы обычной генерации — ровно то, что этот режим запрещает.
        logging.warning("  [%s] режим не-AI не проверен (%s) — гейт работает как обычно",
                        label, exc)

    if start_sec is None or float(start_sec) <= 0:
        return []
    # Гейт отдаёт вступление нейрогенерации. Если она запрещена (доля 0 или
    # «Пропустить»), снятая метка оставила бы сцену без хозяина.
    try:
        from units.source_quota import ai_allowed
        if not ai_allowed():
            return []
    except Exception as exc:                     # noqa: BLE001
        logging.warning("  [%s] доля нейрогенерации не проверена (%s)", label, exc)
    path = Path(montage_path)
    try:
        plan = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as exc:
        printer(f"  [{label}] интро-гейт пропущен: не читается montage_plan ({exc})")
        return []
    clips = plan.get('clips') or plan.get('montage_plan', {}).get('clips', []) or []
    wanted = set(sources)
    project = path.parent

    def _already_materialized(clip) -> bool:
        return clip_media_ready(project, clip)

    gated = []
    for clip in clips:
        if clip.get('source') not in wanted:
            continue
        if float(clip.get('startTime', 0) or 0) >= float(start_sec):
            continue
        # Готовый клип не трогаем. Вызывающие передают ВЕСЬ набор маркеров
        # стадии (и `*_pending`, и уже материализованные `stock_photo` /
        # `stock_video`), поэтому на повторном запуске гейт снимал маркер с
        # готовой сцены: её заново генерировали, а склейка всё равно брала
        # старый файл — и инфографика вдобавок уходила в видеоген, чего делать
        # нельзя (аудит 25.08).
        if _already_materialized(clip):
            continue
        clip.pop('source', None)
        gated.append(clip.get('id'))
    if gated:
        # Пишем ДЕЛЬТОЙ под замком: стадии работают одновременно, и снимок
        # целого файла затирал чужие правки — см. `apply_clip_source_delta`.
        apply_clip_source_delta(
            path,
            {int(clip_id): {'source': None} for clip_id in gated if clip_id is not None},
            log=lambda message: printer(f"  [{label}] {message}"),
        )
        printer(f"  [{label}] интро-гейт (<{float(start_sec):g}с): снял маркер с "
                f"{len(gated)} клип(ов) {sorted(i for i in gated if i is not None)[:12]}"
                f" → обычная генерация")
    return gated


def apply_stage_intro_gates(project_dir, printer=print) -> dict:
    """Прогнать интро-гейты ВСЕХ стадий один раз, ДО старта самих стадий.

    Зачем отдельный барьер. Гейт снимает маркер со вступления, а генераторы
    решают по этому же маркеру, брать клип или пропустить. Пока каждый гейт
    жил внутри своей стадии, стадии стартовали параллельно с ImageGen — и тот
    успевал прочитать план со ЕЩЁ НЕ снятым маркером: клип считался внешним и
    пропускался, а гейт снимал метку уже в пустоту. Кадра не появлялось ни у
    стадии, ни у генерации (прогон 26.08: клип 3, инфографика, `IDs: 3` в
    «images missing» → i2v его пропустил).

    Гейты идемпотентны: повторный вызов внутри самой стадии маркеров уже не
    найдёт и ничего не сделает, поэтому вызовы там оставлены — стадию можно
    запускать и отдельным BAT, мимо пайплайна.

    Real Photo сюда не входит: у него свой гейт по СПИСКУ отобранных клипов
    (`RealPhotoProcessor._apply_intro_gate`), а не по маркерам в плане, и его
    стадия отбора и так идёт синхронно до генерации.

    Возвращает {'stock': [ids], 'infographic': [ids], 'motion_graphics': [ids]} —
    с кого снят маркер.
    """
    from pathlib import Path

    import params as config
    plan_path = Path(project_dir) / 'montage_plan.json'
    result: dict = {'stock': [], 'infographic': [], 'motion_graphics': []}
    if not plan_path.exists():
        return result

    for key, enabled_attr, start_attr, sources, label in (
        ('stock', 'STOCK_ENABLED', 'STOCK_START_SEC', STOCK_SOURCES, 'stock'),
        ('infographic', 'INFOGRAPHIC_ENABLED', 'INFOGRAPHIC_START_SEC',
         INFOGRAPHIC_SOURCES, 'Infographic'),
        ('motion_graphics', 'MOTION_GRAPHICS_ENABLED', 'MOTION_GRAPHICS_START_SEC',
         MOTION_GRAPHICS_SOURCES, 'Motion graphics'),
    ):
        if not getattr(config, enabled_attr, False):
            continue
        try:
            start_sec = float(getattr(config, start_attr, 0) or 0)
        except (TypeError, ValueError) as exc:
            printer(f"  [{label}] интро-гейт пропущен: {start_attr} не число ({exc})")
            continue
        if start_sec <= 0:
            continue
        try:
            result[key] = apply_intro_gate(plan_path, sources, start_sec,
                                           label=label, printer=printer) or []
        except Exception as exc:                     # noqa: BLE001
            # Молчать нельзя: без гейта клип из вступления останется под
            # внешним маркером, стадия его может не закрыть — и он дойдёт до
            # склейки пустым.
            printer(f"  [{label}] интро-гейт упал: {exc}")
            import logging
            logging.exception(
                "Intro gate failed for %s (stage=%s, start_sec=%s): %s",
                plan_path, label, start_sec, exc)
    return result


def log_source_markup(clips, printer=print) -> None:
    """Сводка разметки Pass 1 в лог юзера: сколько клипов помечено под Real Photo /
    Сток. Печатается только для ВКЛЮЧЁННЫХ стадий — в т.ч. явный ноль («Сток: 0»),
    чтобы не гадать, почему сток-стадия не оставила ни одного файла (она молча
    выходит при нуле помеченных клипов)."""
    import params as config
    rp_on = bool(getattr(config, 'REAL_PHOTO_ENABLED', False))
    st_on = bool(getattr(config, 'STOCK_ENABLED', False))
    ig_on = bool(getattr(config, 'INFOGRAPHIC_ENABLED', False))
    # Ютуб теперь размечает РЕЖИССЁР, наравне с прочими (27.08). Пока его клипы
    # назначал пост-маршрутизатор, показывать в сводке Pass 1 было нечего, и
    # стадии здесь не было вовсе — из-за чего в логе прогона казалось, что ютуб
    # обошли стороной.
    yt_on = bool(getattr(config, 'YOUTUBE_FOOTAGE_ENABLED', False))
    mg_on = bool(getattr(config, 'MOTION_GRAPHICS_ENABLED', False))
    if not (rp_on or st_on or ig_on or yt_on or mg_on):
        return
    clips = clips or []
    n_rp = sum(1 for c in clips if c.get('source') in REAL_PHOTO_SOURCES)
    n_st = sum(1 for c in clips if c.get('source') in STOCK_SOURCES)
    n_ig = sum(1 for c in clips if c.get('source') in INFOGRAPHIC_SOURCES)
    n_yt = sum(1 for c in clips if c.get('source') in YOUTUBE_SOURCES)
    n_mg = sum(1 for c in clips if c.get('source') in MOTION_GRAPHICS_SOURCES)
    total = len(clips) or 1
    # (подпись, сколько размечено, целевая доля, ключ настройки)
    rows: List[tuple] = []
    if rp_on:
        rows.append(("Real Photo", n_rp, float(getattr(config, 'REAL_PHOTO_RATIO_TARGET', 0) or 0)))
    if st_on:
        rows.append(("Сток", n_st, float(getattr(config, 'STOCK_RATIO_TARGET', 0) or 0)))
    if yt_on:
        rows.append(("YouTube", n_yt, float(getattr(config, 'YOUTUBE_RATIO_TARGET', 0) or 0)))
    if ig_on:
        rows.append(("Infographic", n_ig, float(getattr(config, 'INFOGRAPHIC_RATIO_TARGET', 0) or 0)))
    if mg_on:
        rows.append(("Моушн-графика", n_mg,
                     float(getattr(config, 'MOTION_GRAPHICS_RATIO_TARGET', 0) or 0)))
    n_hold = sum(1 for c in clips if c.get('source') in FILLER_SOURCES)
    if n_hold:
        rows.append(("Удержание", n_hold, -1.0))
    marked = n_rp + n_st + n_yt + n_ig + n_mg + n_hold
    rows.append(("Нейрогенерация", max(0, len(clips) - marked), -1.0))

    _BAR = 22          # длина полосы
    # Ширина подобрана под самую длинную строку («Нейрогенерация» + полоса +
    # счётчик + цель): рамка не должна разъезжаться ни на одной из них.
    width = 68
    printer("")
    printer("   ┏" + "━" * width + "┓")
    printer("   ┃" + f"  РАЗМЕТКА РЕЖИССЁРА — {len(clips)} клип(ов)".ljust(width) + "┃")
    printer("   ┣" + "━" * width + "┫")
    alerts: List[str] = []
    for title, count, target in rows:
        share = count / total
        filled = int(round(share * _BAR))
        bar = "█" * filled + "░" * (_BAR - filled)
        line = f"  {title:<15}{bar} {count:>3}/{len(clips):<3} {share * 100:>5.0f}%"
        if target >= 0:
            line += f"   цель {target * 100:>3.0f}%"
        printer("   ┃" + line.ljust(width) + "┃")
        # Отклонение от заданной доли — то, ради чего сводка и печатается: увидеть
        # это НА СТАРТЕ, а не через час генерации (правило пользователя 01.09).
        if target > 0 and count == 0:
            alerts.append(f"{title}: включён, доля {target * 100:.0f}%, "
                          f"но режиссёр не пометил НИ ОДНОГО клипа — стадия будет пропущена")
        elif target > 0 and share < target / 2:
            alerts.append(f"{title}: размечено {share * 100:.0f}% при цели {target * 100:.0f}% "
                          f"— меньше половины заданного")
    printer("   ┗" + "━" * width + "┛")
    for text in alerts:
        printer(f"   ⚠️  {text}")
    if yt_on and n_yt == 0:
        printer("   ⚠️  YouTube: свою долю маршрутизатор добирает только подрезкой, "
                "собственных меток он не ставит")
    if alerts:
        printer("   ⚠️  Не то, чего ждали? Остановите прогон сейчас — дальше идут "
                "поиск и генерация, и переделка обойдётся в час.")
    printer("")


def apply_clip_source_delta(montage_path, delta, *, retries: int = 6, delay: float = 5.0,
                            acquire_timeout: float = 4.0, log=None) -> bool:
    """Атомарно применить дельту к СВОИМ клипам montage_plan.json.

    Зачем: real_photo и сток — две ФОНОВЫЕ стадии, финишируют независимо и обе правят
    montage_plan. Если писать весь файл своим снимком — параллельная стадия затрёт чужие
    правки (гонка read-modify-write). Здесь каждая стадия под filelock ПЕРЕЧИТЫВАЕТ свежий
    файл и меняет ТОЛЬКО клипы из своей дельты — чужие клипы физически не переписываются
    устаревшим снимком, поэтому затирания нет.

    Ждать друг друга стадиям нельзя (упавшая/зависшая задержит готовую до таймаута), поэтому
    пишем независимо, а конфликт разруливаем локом.

    delta: {clip_id(int): patch(dict)}. Ключ 'source' со значением None → снять маркер
    (clip.pop('source')); прочие пары → clip[k] = v. Клипы не из delta не трогаются.

    При занятом локе НЕ падаем: ждём `delay` сек и повторяем (до `retries` раз). После
    исчерпания — форс-запись своей дельты (лучше записать, чем потерять результат стадии).
    Возвращает True при успешной записи.
    """
    import json
    import time
    from pathlib import Path

    montage_path = Path(montage_path)
    if not delta:
        return True

    def _do_write():
        raw = json.loads(montage_path.read_text(encoding='utf-8-sig'))
        container = raw['montage_plan'] if (isinstance(raw, dict) and 'montage_plan' in raw) else raw
        clips = (container.get('clips') if isinstance(container, dict) else None) or []
        for clip in clips:
            cid = clip.get('id')
            try:
                cid = int(cid)
            except (TypeError, ValueError):
                continue
            patch = delta.get(cid)
            if not patch:
                continue
            for k, v in patch.items():
                if k == 'source' and v is None:
                    clip.pop('source', None)
                else:
                    clip[k] = v
        montage_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding='utf-8')

    try:
        from filelock import FileLock, Timeout
    except Exception:
        # filelock недоступен — пишем без лока (лучше запись со слабым риском гонки, чем потеря)
        try:
            _do_write()
            return True
        except Exception as e:
            if log:
                log(f"montage delta write failed (no filelock): {e}")
            return False

    lock = FileLock(str(montage_path) + '.lock')
    for attempt in range(1, retries + 1):
        try:
            with lock.acquire(timeout=acquire_timeout):
                _do_write()
            return True
        except Timeout:
            if log:
                log(f"montage_plan занят другой стадией — жду {delay:.0f}с "
                    f"(попытка {attempt}/{retries})")
            time.sleep(delay)
        except Exception as e:
            if log:
                log(f"montage delta write error: {e}")
            return False
    # Все ретраи исчерпаны — форсируем свою дельту (ломаем зависший lock)
    if log:
        log("montage_plan lock не отпущен за все ретраи — форс-запись своей дельты")
    try:
        _do_write()
        return True
    except Exception as e:
        if log:
            log(f"montage force-write failed: {e}")
        return False
