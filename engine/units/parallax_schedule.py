"""parallax_plan.py — единая точка правды «кому ставить параллакс».

Зачем модуль
------------
Список «каждый N-й» считался в ДВУХ местах и по РАЗНЫМ правилам:

  • наблюдатель (`auto_visual_parallax`) — каждый N-й ПО ПОЗИЦИИ в списке
    подходящих клипов;
  • нейрокадры внутри стадии i2v (`veo_video`, через
    `parallax.use_parallax_for_clip`) — по НОМЕРУ клипа, `id % N == 0`.

На смешанном ролике это ломало долю. Если режиссёр отдал чётные номера стоку и
архивам, а нейрогенерации достались нечётные, то при N=2 нейрокадры не получали
параллакс ВООБЩЕ: среди 1,3,5,7,9 нет ни одного номера, кратного двум. Настройка
стоит, галка нажата, а половина ролика без параллакса — и в логе это выглядит
нормой.

Теперь список считается ОДИН раз, по позиции в общем ряду, и кладётся в проект
файлом. Дальше и наблюдатель, и стадия i2v только сверяются с ним: номер в
списке — параллакс-промпт, нет — промпт сцены (для нейрокадров) или зум в
склейке (для стока и архивов).

Правила отбора
--------------
Берём клипы, отмеченные галками параллакса, в порядке монтажного плана:
нейрокадры (галка AI), сток (галка «сток»), архивы (галка «Real Photo»).

  • нейрокадрам выбрано «вперемешку с промптами сцен» (`MIX_PERCLIP` включён)
    ИЛИ видеогенерация выключена → берём каждый N-й ПО ПОЗИЦИИ из общего ряда;
  • нейрокадрам выбрано «всем» (`MIX_PERCLIP` выключен) И видеогенерация
    включена → нейрокадры идут параллаксом ВСЕ, а «каждый N-й» остаётся
    работать только на стоке и архивах.

Второе правило — сегодняшнее поведение, оно сохранено намеренно: «всем» для
нейрокадров задаётся именно этим выбором, а не значением N. При выключенной
видеогенерации выбор не действует — нейрокадры там оживляет наблюдатель, а он
всегда берёт каждый N-й.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import params as config
logger = logging.getLogger(__name__)

PLAN_FILENAME = 'parallax_plan.json'

# Группы клипов в общем ряду.
GROUP_AI = 'ai'              # нейрокадр: картинку рисует генератор
GROUP_EXTERNAL = 'external'  # сток или архивное фото: картинку приносит стадия


def every_n() -> int:
    """Сколько клипов приходится на один параллакс (N из настроек)."""
    try:
        return max(1, int(getattr(config, 'VEONONSTOP_I2V_PARALLAX_EVERY_N', 2) or 2))
    except (TypeError, ValueError):
        return 2


def ai_mode() -> str:
    """Что делать с нейрокадрами: 'mix' — каждый N-й, 'all' — все.

    Соответствует выпадающему списку в строке «Нейрокадры»: «вперемешку с
    промптами сцен» = mix, «всем» = all. Хранится в прежней переменной
    `VEONONSTOP_I2V_PARALLAX_MIX_PERCLIP`, чтобы не ломать пресеты.
    """
    return 'mix' if bool(
        getattr(config, 'VEONONSTOP_I2V_PARALLAX_MIX_PERCLIP', False)) else 'all'


def ai_enabled() -> bool:
    """Отмечен ли чекбокс «Нейрокадры»."""
    return bool(getattr(config, 'VEONONSTOP_I2V_PARALLAX_AI', True))


def photo_sources() -> frozenset:
    """Какие ГОТОВЫЕ фото разрешено оживлять (чекбоксы сток / Real Photo)."""
    from units.source_ledger import parallax_photo_sources

    return parallax_photo_sources()


def video_on() -> bool:
    """Включена ли видеогенерация в этом прогоне.

    Смотрим окружение первым: GUI выставляет `PIPELINE_VIDEO_MODE` на запуск, а
    в `.env` может лежать другое значение с прошлого раза.
    """
    mode = (os.environ.get('PIPELINE_VIDEO_MODE')
            or getattr(config, 'PIPELINE_VIDEO_MODE', '')
            or '').strip().lower()
    return mode != 'skip'


def _read_clips(project_dir: str | Path) -> List[Dict[str, Any]]:
    """Клипы монтажного плана в порядке файла."""
    plan_path = Path(project_dir) / 'montage_plan.json'
    raw = json.loads(plan_path.read_text(encoding='utf-8-sig'))
    plan = raw.get('montage_plan', raw)
    return plan.get('clips', []) or []


def eligible_clips(
    project_dir: str | Path,
    *,
    sources=None,
    include_ai: Optional[bool] = None,
) -> List[Tuple[int, str]]:
    """Отмеченные галками клипы в порядке плана: [(clip_id, группа), ...].

    Учитываем НАМЕРЕНИЕ режиссёра, а не только закрытые клипы: пока стадия
    качает, у клипа промежуточный маркер (`stock_pending` / `real_photo_pending`),
    а если материал не нашёлся — маркер снимут вовсе. Считая только по готовым,
    мы получали бы каждый круг новый список, и «каждый N-й» ездил бы по ролику.
    """
    from units.source_ledger import VIDEO_SKIP_SOURCES

    allowed = set(sources) if sources is not None else set(photo_sources())
    if 'stock_photo' in allowed:
        allowed.add('stock_pending')
    if 'real_photo' in allowed:
        allowed.add('real_photo_pending')
    take_ai = ai_enabled() if include_ai is None else bool(include_ai)

    out: List[Tuple[int, str]] = []
    for clip in _read_clips(project_dir):
        source = clip.get('source')
        try:
            clip_id = int(clip['id'])
        except (KeyError, TypeError, ValueError):
            continue
        if source in allowed:
            out.append((clip_id, GROUP_EXTERNAL))
        elif take_ai and source not in VIDEO_SKIP_SOURCES:
            # Нейрокадр: внешнего маркера нет, картинку рисует генератор.
            out.append((clip_id, GROUP_AI))
    return out


def _pick_every_nth(ids: List[int], step: int) -> List[int]:
    """Каждый N-й ПО ПОЗИЦИИ в ряду, а не по номеру клипа."""
    step = max(1, int(step or 1))
    return [clip_id for position, clip_id in enumerate(ids, start=1)
            if position % step == 0]


def build(
    project_dir: str | Path,
    *,
    sources=None,
    include_ai: Optional[bool] = None,
    video_enabled: Optional[bool] = None,
    save: bool = True,
) -> Dict[str, Any]:
    """Посчитать список параллакса и (по умолчанию) записать его в проект."""
    project = Path(project_dir)
    step = every_n()
    mode = ai_mode()
    with_video = video_on() if video_enabled is None else bool(video_enabled)
    pairs = eligible_clips(project, sources=sources, include_ai=include_ai)

    ai_ids = [clip_id for clip_id, group in pairs if group == GROUP_AI]
    external_ids = [clip_id for clip_id, group in pairs if group == GROUP_EXTERNAL]

    if mode == 'all' and with_video:
        # «Всем» действует только когда нейрокадры оживляет стадия i2v. При
        # выключенном видео их берёт наблюдатель, а он всегда идёт каждым N-м.
        chosen = sorted(set(ai_ids) | set(_pick_every_nth(external_ids, step)))
        rule = 'ai_all + external_every_n'
    else:
        chosen = sorted(_pick_every_nth([clip_id for clip_id, _ in pairs], step))
        rule = 'every_n_over_all'

    plan: Dict[str, Any] = {
        'every_n': step,
        'ai_mode': mode,
        'video_enabled': with_video,
        'rule': rule,
        'sources': sorted(set(sources) if sources is not None else photo_sources()),
        'include_ai': ai_enabled() if include_ai is None else bool(include_ai),
        'eligible': [clip_id for clip_id, _ in pairs],
        'ai_eligible': ai_ids,
        'external_eligible': external_ids,
        'ids': chosen,
    }
    if save:
        try:
            (project / PLAN_FILENAME).write_text(
                json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
        except OSError as exc:
            # Не роняем прогон: список посчитан, дальше поедет и без файла.
            logger.warning("parallax_plan: не смог записать %s: %s",
                           PLAN_FILENAME, exc)
    logger.info(
        "Параллакс: список из %d клип(ов) — правило %s, N=%d, нейрокадров %d, "
        "внешних %d", len(chosen), rule, step, len(ai_ids), len(external_ids))
    return plan


def load(project_dir: str | Path) -> Optional[Dict[str, Any]]:
    """Готовый список из проекта или None, если его ещё не считали."""
    path = Path(project_dir) / PLAN_FILENAME
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as exc:
        logger.warning("parallax_plan: %s не читается (%s) — пересчитаю",
                       PLAN_FILENAME, exc)
        return None


def parallax_ids(project_dir: str | Path) -> set:
    """Кому ставить параллакс. Файла нет — считаем на лету и не пишем.

    Не пишем намеренно: стадию можно запустить отдельным BAT, мимо пайплайна, и
    тогда файла ещё нет. Записать его здесь значило бы зафиксировать список без
    оглядки на то, что стадии ещё не разметили клипы.
    """
    plan = load(project_dir)
    if plan is None:
        try:
            plan = build(project_dir, save=False)
        except (OSError, ValueError, KeyError) as exc:
            logger.warning("parallax_plan: не смог посчитать список (%s) — "
                           "параллакс пропускается", exc)
            return set()
    try:
        return {int(clip_id) for clip_id in (plan.get('ids') or [])}
    except (TypeError, ValueError):
        return set()
