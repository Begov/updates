"""
source_quota.py — «Состав видео»: доли источников как предпочтение режиссёру.

Доли стока, инфографики и моушн-графики — ориентир, а не квота. Квоту пробовали
(10.10.2026): недобор режиссёра добирался вторым запросом «выбери ещё N» и затем
кодом равномерно по ролику. Число сходилось, но в ролик попадал сток, который
сцене не подходит, — хуже, чем его отсутствие. Решает режиссёр: он помечает только
то, что действительно подходит, а программа лишь сводит разметку с настройками.

Здесь три вещи:
  • `ai_share()` / `ai_allowed()` — сколько нейрогенерации заказано и можно ли её
    вообще делать (доля > 0 и картинки или видео не «Пропустить»);
  • `enforce_source_mix()` — после Pass 1 сводит разметку с долями:
      – нейрогенерация в составе есть: лишнее сверх доли снимается, а недобор
        остаётся нейрогенерации — непомеченные сцены и так уходят к ней;
      – нейрогенерации нет: непомеченные сцены режиссёр вторым запросом сам
        раскладывает по включённым источникам — по тому, что сцене подходит,
        доли лишь подсказывают выбор между равными. Если не подходит ничего —
        «удержание» (`filler_pending`): сцену закроет кадр соседнего клипа;
  • `fill_without_ai()` — последний рубеж перед склейкой, когда нейрогенерация
    запрещена: сцене без медиа отдаётся кадр соседнего клипа (дыр в ролике нет,
    нейросеть не трогаем).
"""
from __future__ import annotations

import json
import logging
import math
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import params as config
from units.source_ledger import (COVER_SOURCES, INFOGRAPHIC_SOURCES, MOTION_GRAPHICS_SOURCES,
                                 STOCK_SOURCES)

logger = logging.getLogger(__name__)

# Источник → (флаг включения, доля, маркер для новой разметки, все его маркеры)
_SOURCES = {
    'stock': ('STOCK_ENABLED', 'STOCK_RATIO_TARGET', 'stock_pending', STOCK_SOURCES),
    'infographic': ('INFOGRAPHIC_ENABLED', 'INFOGRAPHIC_RATIO_TARGET', 'infographic',
                    INFOGRAPHIC_SOURCES),
    'motion_graphics': ('MOTION_GRAPHICS_ENABLED', 'MOTION_GRAPHICS_RATIO_TARGET',
                        'motion_graphics_pending', MOTION_GRAPHICS_SOURCES),
}
_TITLES = {'stock': 'Сток', 'infographic': 'Инфографика', 'motion_graphics': 'Моушн-графика'}
# Сцена, которой не подошёл ни один источник при запрещённой нейрогенерации:
# её закроет кадр соседнего клипа (см. fill_without_ai и Pre-concat fallback).
HOLD_SOURCE = 'filler_pending'
_EPS = 0.005


def _flag(name: str) -> bool:
    return bool(getattr(config, name, False))


def _share(name: str) -> float:
    try:
        return max(0.0, min(1.0, float(getattr(config, name, 0) or 0)))
    except (TypeError, ValueError):
        return 0.0


def source_targets() -> Dict[str, float]:
    """Доли включённых источников (0..1). Выключенный или с долей 0 — не участвует."""
    out: Dict[str, float] = {}
    for name, (flag, ratio, _marker, _all) in _SOURCES.items():
        share = _share(ratio)
        if _flag(flag) and share > _EPS:
            out[name] = share
    return out


def ai_share() -> float:
    """Заказанная доля нейрогенерации: остаток после включённых источников."""
    return max(0.0, 1.0 - sum(source_targets().values()))


def generation_available() -> bool:
    """Включена ли хоть одна нейрогенерация: картинки или видео не «Пропустить»."""
    img = (os.environ.get('PIPELINE_IMG_GEN') or getattr(config, 'PIPELINE_IMG_GEN', '')
           or 'veononstop').strip().lower()
    vid = (os.environ.get('PIPELINE_VIDEO_MODE') or getattr(config, 'PIPELINE_VIDEO_MODE', '')
           or 'i2v').strip().lower()
    return img != 'skip' or vid != 'skip'


def ai_allowed() -> bool:
    """Можно ли отдавать сцены нейрогенерации (в т.ч. запасной догенерацией)."""
    return ai_share() > _EPS and generation_available()


def _spread(items: List[Any], k: int) -> List[Any]:
    """k элементов, равномерно разнесённых по списку (сохраняя порядок)."""
    if k <= 0 or not items:
        return []
    if k >= len(items):
        return list(items)
    step = len(items) / k
    return [items[int(i * step + step / 2)] for i in range(k)]


def _bucket_of(source: str) -> Optional[str]:
    for name, (_f, _r, _m, markers) in _SOURCES.items():
        if source in markers:
            return name
    return None


def _narration(transcription: Dict[str, Any], start: float, end: float) -> str:
    parts = [str(f.get('text', '')) for f in (transcription.get('transcription') or [])
             if isinstance(f, dict)
             and float(f.get('start', 0) or 0) < end and float(f.get('end', 0) or 0) > start]
    return ' '.join(p for p in parts if p).strip()[:400]


_SOURCE_HELP = {
    'stock': (
        "real modern stock footage or photo. Many clips can carry a neutral, timeless shot "
        "that illustrates the narration without contradicting it: nature, sky, water, "
        "materials and textures, hands at work, close-ups of objects, processes, places "
        "without people. NOT for named public figures, named landmarks, or a scene the "
        "narration dates to the past when the shot would reveal today's world."),
    'infographic': (
        "a STILL diagram, chart, map, timeline, comparison or labelled scheme - clips with "
        "numbers, dates, lists, processes, geography."),
    'motion_graphics': (
        "an ANIMATED graphic - counters, growing charts, a route drawn on a map, a timeline "
        "unrolling, or kinetic typography of the clip's key phrase (so it suits any clip with "
        "a clear statement). Give a one-sentence English brief with the real numbers, names "
        "and dates from the narration."),
}


def _ask_director(targets: Dict[str, float], have: Dict[str, int], total: int,
                  pool: List[Dict[str, Any]], transcription: Dict[str, Any],
                  work_dir: Path) -> List[Dict[str, Any]]:
    """Второй запрос режиссёру (нейрогенерации нет): источник для каждой непомеченной сцены."""
    from units.operator_hub import ask_json, resolve_text_operator

    provider, model = resolve_text_operator('')
    if not provider:
        raise RuntimeError('оператор режиссёра не настроен')
    lines = []
    for clip in pool:
        start, end = float(clip.get('startTime', 0) or 0), float(clip.get('endTime', 0) or 0)
        intent = str(clip.get('visual_intent') or '').strip()[:220]
        lines.append(f"- id {int(clip['id'])} [{start:.1f}-{end:.1f}s] "
                     f"narration: \"{_narration(transcription, start, end)}\""
                     + (f" | visual: {intent}" if intent else ""))
    prefs = '\n'.join(f"- {name}: about {share * 100:.0f}% (now {have[name]} of {total} clips)"
                      for name, share in targets.items())
    sources = '\n'.join(f"- {name}: {_SOURCE_HELP[name]}" for name in targets)
    prompt = f"""You are helping the director of a narrated video finish the source markup.

AI image and video generation is OFF for this video, so every clip below still needs
a source. The user's preferred composition of the whole video:
{prefs}
These shares are a PREFERENCE, not a quota. For each clip pick the source that fits
it best; let the preferred shares decide only between equally good options. A source
that does not suit the scene is worse than any alternative.

Sources you may use:
{sources}
- hold: nothing above fits - the neighbouring shot stays on screen for this clip.
  Use it rarely, never for two clips in a row.

Return exactly one entry per candidate clip. "brief" is required for motion_graphics
and an empty string for every other source.

Candidates:
{chr(10).join(lines)}
"""
    answer = ask_json(provider, model, prompt, work_dir=work_dir, phase='source_mix',
                      schema_file='source_mix_assign.json',
                      max_tokens=600 + 60 * len(pool), timeout=300)
    return [item for item in (answer.get('clips') or []) if isinstance(item, dict)]


def enforce_source_mix(montage_path: str | Path, transcription: Optional[Dict[str, Any]] = None,
                       *, printer: Callable[[str], None] = print, ask_director: bool = True) -> Dict[str, Any]:
    """Свести разметку Pass 1 с «Составом видео». Пишет план, возвращает сводку."""
    path = Path(montage_path)
    targets = source_targets()
    summary: Dict[str, Any] = {'changed': False}
    if not targets or not path.exists():
        return summary
    try:
        from units.real_source_policy import enabled as non_ai_enabled
        if non_ai_enabled():
            return summary                  # у режима «только не-AI» своё распределение
    except Exception:                       # noqa: BLE001
        pass

    raw = json.loads(path.read_text(encoding='utf-8-sig'))
    plan = raw.get('montage_plan', raw) if isinstance(raw, dict) else raw
    clips = [c for c in (plan.get('clips') or []) if str(c.get('id', '')).lstrip('-').isdigit()]
    work = [c for c in clips if c.get('source') not in COVER_SOURCES]
    if not work:
        return summary

    allowed_ai = ai_allowed()
    buckets: Dict[str, List[Dict[str, Any]]] = {n: [] for n in targets}
    for clip in work:
        source = str(clip.get('source') or '')
        name = _bucket_of(source)
        if name in buckets:
            buckets[name].append(clip)
        elif name is not None or (source == HOLD_SOURCE and allowed_ai):
            # источник выключен или «удержание» осталось от прогона без нейрогенерации
            clip.pop('source', None)
            clip.pop('mograph_brief', None)
            summary['changed'] = True
    before = {n: len(buckets[n]) for n in buckets}
    held = 0

    if allowed_ai:
        # Доля — потолок предпочтения: лишнее снимаем (оставляем равномерно разнесённые
        # по ролику), недобор не трогаем — эти сцены уйдут нейрогенерации.
        for name in buckets:
            cap = math.ceil(len(work) * targets[name] - 1e-9)
            if len(buckets[name]) > cap:
                keep = _spread(buckets[name], cap)
                for clip in buckets[name]:
                    if clip not in keep:
                        clip.pop('source', None)
                        clip.pop('mograph_brief', None)
                buckets[name] = keep
                summary['changed'] = True
    else:
        # Нейрогенерации не будет: каждой непомеченной сцене нужен источник.
        if ai_share() > _EPS:
            printer(f"   [Состав] нейрогенерация выключена («Пропустить»), а её доля "
                    f"{ai_share() * 100:.0f}% — эти сцены распределит режиссёр")
        free = [c for c in work if not c.get('source')]
        if free and ask_director:
            try:
                answer = _ask_director(targets, {n: len(buckets[n]) for n in buckets}, len(work),
                                       free, transcription or {}, path.parent / '_source_mix')
                by_id = {int(c['id']): c for c in free}
                taken = 0
                for item in answer:
                    try:
                        clip = by_id.pop(int(item.get('id')))
                    except (TypeError, ValueError, KeyError):
                        continue
                    name = str(item.get('source') or '')
                    if name == 'hold':
                        clip['source'] = HOLD_SOURCE
                        held += 1
                    elif name in buckets:
                        clip['source'] = _SOURCES[name][2]
                        brief = str(item.get('brief') or '').strip()
                        if name == 'motion_graphics' and brief:
                            clip['mograph_brief'] = brief
                        buckets[name].append(clip)
                        taken += 1
                if taken or held:
                    summary['changed'] = True
                    printer(f"   [Состав] режиссёр распределил {taken} сцен(ы)"
                            + (f", {held} — удержание соседнего кадра" if held else ""))
            except Exception as exc:        # noqa: BLE001
                printer(f"   [Состав] распределение через режиссёра не удалось ({exc}) — "
                        f"распределяю по долям сам")
                logger.warning("source mix assignment via director failed: %s", exc, exc_info=True)

        # Что режиссёр не распределил (сбой или пропуск) — по долям: каждая сцена
        # уходит источнику с наибольшим отставанием от своей доли.
        free = [c for c in work if not c.get('source')]
        total_share = sum(targets.values())
        for clip in free:
            name = max(targets, key=lambda n: (len(work) * targets[n] / total_share
                                               - len(buckets[n]), targets[n]))
            clip['source'] = _SOURCES[name][2]
            buckets[name].append(clip)
        if free:
            summary['changed'] = True
            printer(f"   [Состав] {len(free)} нераспределённых сцен(ы) отданы источникам по долям")

    after = {n: len(buckets[n]) for n in buckets}
    if summary['changed']:
        path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding='utf-8')
    ai_left = sum(1 for c in work if not c.get('source'))
    held = sum(1 for c in work if c.get('source') == HOLD_SOURCE)
    parts = [f"{_TITLES[n]} {before[n]}→{after[n]} (≈{targets[n] * 100:.0f}%)" for n in buckets]
    tail = (f", нейрогенерация {ai_left} из {len(work)}" if allowed_ai
            else f", удержание кадра {held} из {len(work)}")
    printer("   [Состав] доли — предпочтение: " + ', '.join(parts) + tail)
    summary.update(before=before, after=after, ai=ai_left, held=held)
    return summary


# ── Без нейрогенерации: кадр соседнего клипа ────────────────────────────────

_IMG_EXTS = ('.png', '.jpg', '.jpeg', '.webp')


def _media_of(project: Path, cid: int) -> Optional[Path]:
    for name in (f'{cid:03d}', str(cid)):
        for ext in _IMG_EXTS:
            p = project / 'generated' / f'{name}{ext}'
            if p.exists() and p.stat().st_size > 1000:
                return p
        for ext in ('.mp4', '.mov') + _IMG_EXTS:
            p = project / 'vid_img' / f'{name}{ext}'
            if p.exists() and p.stat().st_size > 1000:
                return p
    parts = sorted((project / 'vid_img').glob(f'{cid:03d}_*.mp4')) if (project / 'vid_img').is_dir() else []
    return parts[0] if parts else None


# Где брать кадр из видео соседа: при повторном использовании — другой момент,
# чтобы подряд идущие сцены-заглушки не были одним и тем же кадром.
_FRAME_POINTS = (0.5, 0.25, 0.75, 0.12, 0.88, 0.38, 0.62)


def _still_from(media: Path, dst: Path, use: int = 0) -> bool:
    """Картинка из медиа соседа: копия фото или кадр видео (момент зависит от `use`)."""
    if media.suffix.lower() in _IMG_EXTS:
        shutil.copy2(media, dst.with_suffix(media.suffix.lower()))
        return True
    ffmpeg = shutil.which('ffmpeg') or 'ffmpeg'
    try:
        probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                                '-of', 'default=nw=1:nk=1', str(media)],
                               capture_output=True, text=True, timeout=30)
        length = float((probe.stdout or '0').strip() or 0)
        mid = max(0.0, length * _FRAME_POINTS[use % len(_FRAME_POINTS)])
    except (OSError, ValueError, subprocess.SubprocessError):
        mid = 0.5
    try:
        subprocess.run([ffmpeg, '-y', '-v', 'error', '-ss', f'{mid:.2f}', '-i', str(media),
                        '-frames:v', '1', '-q:v', '2', str(dst.with_suffix('.jpg'))],
                       capture_output=True, timeout=60, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("кадр из %s не извлечён: %s", media.name, exc)
        return False
    return dst.with_suffix('.jpg').exists()


def fill_without_ai(project_dir: str | Path, clip_ids: List[int], *,
                    printer: Callable[[str], None] = print) -> List[int]:
    """Сценам без медиа — кадр ближайшего соседа (сначала предыдущего). Возвращает закрытые."""
    project = Path(project_dir)
    plan_path = project / 'montage_plan.json'
    try:
        raw = json.loads(plan_path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return []
    plan = raw.get('montage_plan', raw)
    clips = [c for c in plan.get('clips') or [] if str(c.get('id', '')).lstrip('-').isdigit()]
    order = [int(c['id']) for c in clips]
    by_id = {int(c['id']): c for c in clips}
    (project / 'generated').mkdir(exist_ok=True)
    closed: List[int] = []
    uses: Dict[int, int] = {}
    wanted = set(int(i) for i in clip_ids)
    for cid in sorted(wanted):
        if cid not in by_id:
            continue
        pos = order.index(cid)
        neighbours = [order[j] for d in range(1, len(order))
                      for j in (pos - d, pos + d) if 0 <= j < len(order)]
        # Сначала ближайший сосед со СВОИМ материалом; заглушка от заглушки — только
        # если своего материала в ролике нет вовсе (иначе цепочка одинаковых кадров).
        for allow_fillers in (False, True):
            done = False
            for other in neighbours:
                if other in wanted and (not allow_fillers or other not in closed):
                    continue
                media = _media_of(project, other)
                if media is None:
                    continue
                if _still_from(media, project / 'generated' / f'{cid:03d}', uses.get(other, 0)):
                    uses[other] = uses.get(other, 0) + 1
                    clip = by_id[cid]
                    clip['source'] = 'filler_photo'
                    clip['filler_from'] = other
                    closed.append(cid)
                    done = True
                    break
            if done:
                break
    if closed:
        plan_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding='utf-8')
        printer(f"  [Без нейрогенерации] {len(closed)} сцен(ы) закрыты кадром соседнего клипа: "
                f"{closed[:12]}{'...' if len(closed) > 12 else ''}")
    missing = sorted(set(clip_ids) - set(closed))
    if missing:
        printer(f"  [Без нейрогенерации] ⚠️  {len(missing)} сцен(ы) нечем закрыть "
                f"(нет ни одного готового кадра рядом): {missing[:12]}")
    return closed
