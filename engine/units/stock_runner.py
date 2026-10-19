"""
stock_stage.py — стадия подстановки сток-материала в клипы монтажного плана.

Сидит между Director (montage_plan.json) и генераторами изображений.
Клипы, помеченные режиссёром как `source: stock_pending`, получают реальный
материал вместо нейрогенерации:
  видео → vid_img/{id:03d}.mp4 (уже обрезан под клип) → source: stock_video
  фото  → generated/{id:03d}.jpg (concat сам сделает Ken Burns) → source: stock_photo

Генераторы (ImgGen/Veo) пропускают такие клипы —
см. units/source_ledger.py (EXTERNAL_SOURCES).

Поиск: спрашиваем ВСЕ включённые стоки сразу и сводим кандидатов на ОДИН лист
для вижна. Выбор идёт по качеству кадра,
а не по тому, кто ответил первым. Индексы у стоков разные: чего нет в Pexels,
часто есть в Pixabay, и наоборот — «нашлось» ещё не значит «нашлось лучшее».

Порядок при нулевой выдаче (решение юзера):
    все стоки по видео → все стоки по ФОТО (тот же запрос) →
    альтернативный запрос (LLM) → обычная нейрогенерация

Почему фото раньше альт-запроса: смена носителя — бесплатный HTTP-вызов, а
альт-запрос это вызов LLM (деньги/время/квота). Видеобиблиотеки держат широкие
темы, на узкий сюжет ролика футажа может не быть вовсе, тогда как снимок есть:
замер 26.08 — все четыре видео-запроса вернули пусто, все три фото-запроса
закрылись, при одинаково коротких и точных запросах. Перепромпчивать имеет
смысл, только когда не нашлось НИ ВИДЕО, НИ ФОТО — тогда дело в самом запросе.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

import params as config
from units.source_ledger import STOCK_SOURCES
from units.footage_trimmer import cut_footage
from units.stock_lookup import (StockResult, rank_stock, search_source, _words,
                                  DURATION_MARGIN, VIDEO_EXTS, PHOTO_EXTS, pexels_configured)

logger = logging.getLogger(__name__)

_PHOTO_EXT_RE = re.compile(r'\.(jpg|jpeg|png|webp)(\?|$)', re.I)
def _scene_brief(clip: Dict[str, Any]) -> str:
    """Описание сцены для вижна + контекст главы (эпоха и место).

    Без периода и страны вижн судит кадр в вакууме и берёт любой подходящий по
    составу футаж; тема ГЛАВЫ (а не всего ролика) — потому что глава про
    сегодняшний день внутри фильма про 1980-е должна и выглядеть современно."""
    description = str(clip.get('description') or clip.get('visual_intent') or '').strip()
    chapter_theme = str((clip.get('visual_story') or {}).get('chapter_theme') or '').strip()
    if chapter_theme:
        return f"Chapter context (period and place): {chapter_theme} {description}".strip()
    return description

# Человеческие причины фолбека — юзер должен понимать, почему сток «не сработал»,
# а не гадать. Молчаливая деградация недопустима.
_REASON_TEXT = {
    'no_query': 'промптер не дал запрос (LLM недоступна и озвучки нет)',
    'no_match': 'ничего подходящего не нашлось ни в библиотеке, ни на стоках',
    'download_failed': 'кандидаты найдены, но файл не скачался/не обрезался',
    'interrupted': 'остановлено пользователем',
}


def _match_local_by_words(queries: List[Any], items: List[StockResult], root: Path) -> Dict[int, str]:
    """Фолбек подбора библиотеки без LLM: слова запроса против имени файла и папки.

    Работает только когда промптер лёг. Тупее LLM, поэтому с двумя предохранителями:
      • match_count — учитывает словоформы (storm ⊂ thunderstorms), иначе половина
        файлов не найдётся из-за одной буквы;
      • порог 50% слов запроса — иначе одно случайное общее слово («night») подсунет
        грозовой футаж в клип про городской трафик. Пустой ответ лучше мусорного:
        клип уйдёт на сток.
    """
    from units.stock_lookup import match_count, _words

    out: Dict[int, str] = {}
    used: set = set()
    for q in queries:
        qwords = _words(getattr(q, 'query', '') or '')
        if not qwords:
            continue
        want_video = getattr(q, 'media', 'video') == 'video'
        best = None
        best_score = 0.0
        for it in items:
            if (it.media_type == 'video') != want_video:
                continue
            if it.local_path in used:
                continue
            hits = match_count(qwords, it.keywords)
            score = hits / len(qwords)
            if score >= 0.5 and score > best_score:      # порог: половина слов запроса
                best, best_score = it, score
        if best is not None:
            used.add(best.local_path)
            out[q.clip_id] = best.local_path
    return out


def _release_pending(clips, *, why: str = "") -> str:
    """Снять маркер `stock_pending`: клип берёт обычная генерация (или Pre-concat fallback).

    Возвращает, куда ушли клипы, — для честного текста в отчёте.
    """
    for clip in clips:
        if clip.get('source') == 'stock_pending':
            clip.pop('source', None)
    where = _fallback_word().upper()
    if why:
        logger.warning("[stock] %s — %d клип(ов) в %s", why, len(clips), where)
    return where


def _fallback_word() -> str:
    """Куда уходит сцена, которую сток не закрыл: нейрогенерация или (если она
    запрещена «Составом видео» / «Пропустить») запасной путь без неё."""
    try:
        from units.source_quota import ai_allowed
        return 'нейрогенерацию' if ai_allowed() else 'запасной путь без нейрогенерации'
    except Exception:                                      # noqa: BLE001
        return 'нейрогенерацию'


def _write_stock_report(project_dir: Path, ok_count: int, fallback_ids: List[int],
                        reasons: Dict[int, str], sources: List[str]) -> None:
    """Отчёт стадии в папку проекта — чтобы причины фолбека остались в проекте,
    а не только в консоли, которую юзер мог не смотреть."""
    try:
        from units.stock_lookup import down_sources
        payload = {
            'sources': sources,
            # Источники, которые сток отверг по ключу: без этого поля разбор
            # постфактум упирался в «ничего не нашлось» при мёртвом ключе.
            'disabled_sources': down_sources(),
            'taken_from_stock': ok_count,
            'fell_back_to_generation': len(fallback_ids),
            'fallback_clips': [
                {'clip_id': cid, 'reason': reasons.get(cid, 'неизвестно')}
                for cid in sorted(fallback_ids)
            ],
        }
        (project_dir / 'stock_report.json').write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception as exc:
        logger.warning("[stock] не смог записать stock_report.json: %s", exc)


def _enabled_sources() -> List[str]:
    """Включённые источники в порядке из настроек. local — только если задана папка."""
    raw = getattr(config, 'STOCK_SOURCES', ['local', 'pexels', 'pixabay'])
    if isinstance(raw, str):
        raw = [s.strip().lower() for s in raw.split(',') if s.strip()]
    out = []
    for s in raw:
        if s == 'local' and not (getattr(config, 'STOCK_LIBRARY_DIR', '') or '').strip():
            continue                     # папка не задана — источника нет
        if s == 'pexels' and not pexels_configured():
            continue                     # нет ни ключа, ни временного ключа сайта
        if s == 'pixabay' and not (getattr(config, 'PIXABAY_API_KEY', '') or '').strip():
            continue
        out.append(s)
    return out


def _http_download(url: str, dst: Path, *, stop_event: Optional[threading.Event] = None,
                   timeout: int = 120, max_mb: int = 400) -> bool:
    """Скачать файл. Прерываемо (стоп-кнопка) и с потолком размера."""
    proxy = (getattr(config, 'STOCK_PROXY_URL', '') or '').strip()
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({'http': proxy, 'https': proxy}) if proxy
        else urllib.request.ProxyHandler({})
    )
    req = urllib.request.Request(url, headers={'User-Agent': 'begoframe-stock/1.0'})
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + '.part')
    limit = max_mb * 1024 * 1024
    try:
        with opener.open(req, timeout=timeout) as r, open(tmp, 'wb') as f:
            got = 0
            while True:
                if stop_event is not None and stop_event.is_set():
                    raise InterruptedError('stopped')
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                got += len(chunk)
                if got > limit:
                    raise IOError(f'файл больше {max_mb} МБ — пропускаю')
                f.write(chunk)
        if tmp.stat().st_size < 1024:
            raise IOError('слишком маленький файл')
        if dst.exists():
            dst.unlink()
        tmp.replace(dst)
        return True
    except Exception as exc:
        if not isinstance(exc, InterruptedError):
            logger.info("  скачивание не удалось (%s): %s", type(exc).__name__, exc)
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        return False


def _search_cascade(
    query: str,
    media_type: str,
    *,
    primary: str,
    sources: List[str],
    clip_duration: float,
    target_w: int,
    target_h: int,
    used_urls: set,
    limit: int = 0,
    subject: str = '',
) -> List[StockResult]:
    """Назначенный источник, затем соседние — ТОТ ЖЕ запрос. Возвращает ранжированное.

    `limit` — сколько элементов просить у стока В ОДНОМ запросе (`per_page`), а не
    сколько запросов делать: лимиты стоков считаются по запросам, поэтому 80 стоит
    ровно столько же, сколько 20. Раньше стояло 20, и после отсева по доле слов
    оставалось 1-5 кандидатов; на живой проверке 26.08 сотня элементов давала вдвое
    -впятеро больше выживших («Man staring window» 1 → 5, «Soviet concert hall
    audience» 5 → 11). Провайдеры сами клампят: Pexels до 80, Pixabay до 200.
    """
    if limit <= 0:
        limit = int(getattr(config, 'STOCK_SEARCH_LIMIT', 80) or 80)
    order = [primary] + [s for s in sources if s != primary]

    # Спрашиваем ВСЕ стоки и сводим кандидатов на один лист. Раньше здесь стоял
    # `return` на первом источнике,
    # который что-то нашёл: до второго дело не доходило, и вижн выбирал лучший
    # кадр только среди того, что принёс сработавший сток, даже когда в соседнем
    # лежал кадр точнее (улов 26.08). Индексы у стоков разные, и «нашлось» ещё не
    # значит «нашлось лучшее».
    by_source: List[List[StockResult]] = []
    for src in order:
        raw = search_source(src, query, media_type, limit, target_w, target_h)
        ranked = rank_stock(raw, query, clip_duration=clip_duration,
                            target_w=target_w, target_h=target_h, used_urls=used_urls,
                            subject=subject)
        if ranked:
            by_source.append(ranked)
            logger.info("    '%s': %s дал %s кандидат(ов)", query[:40], src, len(ranked))
    if not by_source:
        return []
    if len(by_source) == 1:
        return by_source[0]

    # Чередуем по источникам, сохраняя порядок ранжирования внутри каждого: лист
    # вижна не должен быть занят одним стоком только потому, что он ответил первым.
    #
    # Квота на источник (`STOCK_VISION_PER_SOURCE`): сначала раздаём каждому по
    # N мест, и только потом добираем остальными. Без неё чередование не спасало
    # от перекоса — дальше стоит срез `ranked[:STOCK_VISION_CANDIDATES]`, и сток,
    # вернувший много кандидатов, вытеснял из листа того, кто вернул мало.
    # НЕДОБОР не пропадает: место, которое источник не занял, достаётся соседям.
    merged: List[StockResult] = []
    seen: set = set()

    def _take(item: StockResult) -> None:
        key = getattr(item, 'url', None) or id(item)
        if key in seen:
            return
        seen.add(key)
        merged.append(item)

    per_source = int(getattr(config, 'STOCK_VISION_PER_SOURCE', 0) or 0)
    if per_source > 0 and len(by_source) > 1:
        quota_rows = [items[:per_source] for items in by_source]
        tail_rows = [items[per_source:] for items in by_source]
    else:
        quota_rows, tail_rows = by_source, []

    for rows in (quota_rows, tail_rows):
        if not rows:
            continue
        for row in range(max((len(items) for items in rows), default=0)):
            for items in rows:
                if row < len(items):
                    _take(items[row])
    if per_source > 0 and len(by_source) > 1:
        logger.info("    '%s': лист из %s источников — по %s от каждого, "
                    "всего %s кандидат(ов)",
                    query[:40], len(by_source), per_source, len(merged))
    else:
        logger.info("    '%s': общий лист из %s источников — %s кандидат(ов)",
                    query[:40], len(by_source), len(merged))
    return merged


class StockProcessor:
    def __init__(
        self,
        project_dir: str | Path,
        *,
        target_w: int = 1920,
        target_h: int = 1080,
        fps: Optional[int] = None,
        alt_query_fn=None,
    ):
        self.project_dir = Path(project_dir).resolve()
        self.montage_path = self.project_dir / 'montage_plan.json'
        self.generated_dir = self.project_dir / 'generated'
        self.vid_img_dir = self.project_dir / 'vid_img'
        self.cache_dir = self.project_dir / '.stock_cache'
        self.target_w = target_w
        self.target_h = target_h
        self.fps = int(fps if fps is not None else getattr(config, 'VIDEO_FPS', 60))
        self.used_urls: set = set()
        # Клипы обрабатываются параллельно (28.08), поэтому «этот футаж уже
        # занят» и список авторов — общие структуры под замком. Без резерва
        # два потока успевали выбрать один и тот же ролик, и он вставал в две
        # соседние сцены: проверка used_urls шла ДО скачивания, а запись —
        # после, между ними помещался целый клип.
        self._urls_lock = threading.Lock()
        self._credits_lock = threading.Lock()
        self.credits: List[Dict[str, Any]] = []
        # {clip_id: StockQuery} — заполняет build_queries (промптер стока)
        self.queries: Dict[int, Any] = {}
        # Функция альт-запросов (LLM). Не задана — каскад просто закончится фолбеком.
        self.alt_query_fn = alt_query_fn

    # ── план ────────────────────────────────────────────────────────────────
    def _load_plan(self) -> Dict[str, Any]:
        raw = json.loads(self.montage_path.read_text(encoding='utf-8-sig'))
        return raw.get('montage_plan', raw) if isinstance(raw, dict) else {}

    def _commit_pending_delta(self, pending: List[Dict[str, Any]]) -> None:
        """Записать ТОЛЬКО свои клипы (по их итоговому source + материализованные поля)
        в montage_plan под locked-ретраем. Real_photo идёт параллельно и правит свои клипы —
        общий файл пишется атомарно по дельте, без гонки (см. apply_clip_source_delta).

        Материализованный клип несёт не только source, но и file/stock_source/stock_page
        (см. _materialize) — их тоже кладём в дельту, иначе они потеряются при записи."""
        from units.source_ledger import apply_clip_source_delta
        delta = {}
        for clip in pending:
            cid = clip.get('id')
            try:
                cid = int(cid)
            except (TypeError, ValueError):
                logging.warning('  [сток] клип с нечисловым id (%r) пропущен при записи дельты', cid)
                continue
            # source после обработки: 'stock_video'/'stock_photo' (ok) или отсутствует (снят → None)
            patch = {'source': clip.get('source')}
            # `parts` — сцена, собранная из двух футажей: склейка читает именно
            # это поле (`_resolve_clip_parts`). Без него в план попал бы только
            # первый кусок, и сцена вышла бы короче задуманного.
            for k in ('file', 'parts', 'stock_source', 'stock_page'):
                if k in clip:
                    patch[k] = clip[k]
            delta[cid] = patch
        apply_clip_source_delta(self.montage_path, delta,
                                log=lambda m: logger.info("[stock] " + m))

    def pending_clips(self, plan: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Клипы, ждущие сток. Готовые пропускаем — их файл уже на диске.

        Стадия пишет результат в план ОДНОЙ дельтой в самом конце, поэтому при
        обрыве (таймаут ожидания, закрытие процесса) скачанные клипы остаются
        помеченными `stock_pending`. Без этой проверки следующий запуск заново
        платил за промптер, вижн и скачивание того, что уже лежит на диске
        (аудит 25.08; тот же приём — у ютуба и EPF).
        """
        result = []
        for clip in (plan.get('clips') or []):
            if clip.get('source') != 'stock_pending':
                continue
            if self._clip_asset_ready(clip):
                logger.info("  [сток] клип %s уже materialized — пропускаю", clip.get('id'))
                continue
            result.append(clip)
        return result

    def _clip_asset_ready(self, clip: Dict[str, Any]) -> bool:
        """Есть ли у клипа готовый файл на диске."""
        try:
            cid = int(clip.get('id'))
        except (TypeError, ValueError):
            return False
        declared = str(clip.get('file') or '').strip()
        if declared and (self.project_dir / declared).exists():
            return True
        if (self.project_dir / 'vid_img' / f'{cid:03d}.mp4').exists():
            return True
        return any((self.project_dir / 'generated').glob(f'{cid:03d}.*'))

    def _transcript_fragments(self) -> List[Dict[str, Any]]:
        """Фрагменты транскрипции с таймингами (кэшируем: файл один на проект)."""
        cached = getattr(self, '_trans_cache', None)
        if cached is not None:
            return cached
        fragments: List[Dict[str, Any]] = []
        path = self.project_dir / 'transcription.json'
        try:
            data = json.loads(path.read_text(encoding='utf-8-sig'))
            raw = data.get('transcription') or data.get('segments') or []
            fragments = [f for f in raw if isinstance(f, dict)]
        except (OSError, ValueError) as exc:
            logger.debug("[stock] транскрипция недоступна (%s)", exc)
        self._trans_cache = fragments
        return fragments

    def _voiceover_from_transcript(self, clip: Dict[str, Any]) -> str:
        """Склеить реплики, попадающие в интервал клипа.

        Директор кладёт в montage_plan только id/тайминги/source — текста там
        НЕТ. Без него промптер стока сочинял запросы вслепую («factory
        workers», «mountain landscape» в ролике про Леонардо), и в ролик
        попадал случайный футаж.
        """
        fragments = self._transcript_fragments()
        if not fragments:
            return ''
        start = float(clip.get('startTime', 0) or 0)
        end = float(clip.get('endTime', 0) or 0)
        parts = [
            str(f.get('text', ''))
            for f in fragments
            if float(f.get('start', 0) or 0) < end and float(f.get('end', 0) or 0) > start
        ]
        return ' '.join(p for p in parts if p).strip()

    def _clip_voiceover(self, clip: Dict[str, Any]) -> str:
        """Текст озвучки клипа — контекст для промптера.

        Директор кладёт реплики по-разному в зависимости от бэкенда, поэтому
        собираем из всех известных полей; если их нет — берём из транскрипции
        по таймингам клипа.
        """
        for key in ('voiceover', 'text', 'narration', 'transcript'):
            v = clip.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        from_transcript = self._voiceover_from_transcript(clip)
        if from_transcript:
            return from_transcript[:600]
        segs = clip.get('segments') or clip.get('lines')
        if isinstance(segs, list):
            parts = [s.get('text', '') if isinstance(s, dict) else str(s) for s in segs]
            joined = ' '.join(p for p in parts if p).strip()
            if joined:
                return joined
        # последний шанс — визуальный промпт режиссёра (художественный, но лучше пустоты)
        return (clip.get('prompt') or '').strip()

    def build_queries(self, clips: List[Dict[str, Any]]) -> Dict[int, Any]:
        """Промптер стока: запросы + тип медиа + сопоставление своей библиотеки.

        Вызов 1 — озвучка → query + media (с пропорцией video/photo).
        Вызов 2 — те же query + список файлов → local_file | null (если папка включена).
        Промптер свой, от режиссёра не зависит: Pass 1 только ставит маркер.
        """
        from units import stock_queries as stock_prompter

        # Тема ролика (якорь) — Pass 1 кладёт в montage_plan.theme. Опционально.
        try:
            _raw = json.loads(self.montage_path.read_text(encoding='utf-8-sig'))
            _theme = (_raw.get('theme') or _raw.get('montage_plan', {}).get('theme', '') or '').strip()
        except Exception as exc:                          # noqa: BLE001
            logging.warning('  [сток] тема ролика не прочитана (%s) — запросы пойдут без якоря', exc)
            _theme = ''
        from units.scene_context import build_context_cards
        try:
            plan_for_context = self._load_plan()
        except Exception as exc:                          # noqa: BLE001
            logging.warning('  [сток] план не прочитан (%s) — карточки клипов без контекста', exc)
            plan_for_context = {}
        payload = [
            {
                'id': int(c['id']),
                'voiceover': self._clip_voiceover(c),
                **{
                    key: c[key]
                    for key in (
                        'entities', 'resolved_entities', 'resolved_facts',
                        'hidden_entities', 'revealed_entities', 'scene_role',
                        'chain_id', 'reveal_state', 'visual_intent',
                        'visual_description', 'visual_prompt', 'prompt',
                        'description',
                    )
                    if key in c
                },
            }
            for c in clips
        ]
        payload = build_context_cards(
            payload, self._transcript_fragments(), plan_for_context)
        queries = stock_prompter.generate_queries(payload, theme=_theme)
        if not queries:
            return {}

        # Локальная библиотека — вторым вызовом, только если она реально включена
        lib_root = (getattr(config, 'STOCK_LIBRARY_DIR', '') or '').strip().strip('"')
        sources = _enabled_sources()
        if lib_root and 'local' in sources:
            from units.stock_lookup import _scan_local_library
            root = Path(lib_root).expanduser()
            items = _scan_local_library(root)
            rel = []
            by_rel = {}
            for it in items:
                try:
                    r = str(Path(it.local_path).relative_to(root)).replace('\\', '/')
                except ValueError:
                    # файл вне корня библиотеки — показываем его по имени
                    logging.debug('  [сток] %s вне корня библиотеки — беру имя файла',
                                  it.local_path)
                    r = Path(it.local_path).name
                rel.append(r)
                by_rel[r] = it.local_path
            if rel:
                matched = stock_prompter.match_local(queries, rel)
                if matched is None:
                    # LLM легла → подбор по словам, иначе библиотека умрёт вместе с ней.
                    # (Пустой {} от LLM — это НЕ сюда: она честно сказала «не подходит».)
                    matched = _match_local_by_words(queries, items, root)
                    if matched:
                        print(f"   [stock] промптер недоступен → подбор по словам: "
                              f"{len(matched)} клип(ов) из своей библиотеки")
                for q in queries:
                    f = matched.get(q.clip_id)
                    if f:
                        q.local_file = by_rel.get(f, f)
                if matched:
                    print(f"   [stock] своя библиотека закрыла {len(matched)} из {len(queries)} клип(ов)")
        return {q.clip_id: q for q in queries}

    # ── обработка клипа ─────────────────────────────────────────────────────
    def process_clip(
        self,
        clip: Dict[str, Any],
        primary_source: str,
        sources: List[str],
        *,
        stop_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        cid = int(clip.get('id') or 0)
        sq = (self.queries or {}).get(cid)
        if sq is None or not (sq.query or '').strip():
            return {'id': cid, 'status': 'no_query'}
        query = sq.query
        media_type = sq.media
        duration = float(clip.get('endTime', 0)) - float(clip.get('startTime', 0))

        # Промптер выбрал файл из своей библиотеки → он и идёт в дело, сток не трогаем.
        # Тип берём по расширению файла, а не по ответу модели: .mp4 — это видео,
        # что бы она ни написала.
        if sq.local_file:
            p = Path(sq.local_file)
            is_video = p.suffix.lower() in VIDEO_EXTS
            cand = StockResult(
                source='local', media_type='video' if is_video else 'photo',
                url=str(p), local_path=str(p), title=p.stem,
                license_name='own material',
            )
            if self._reserve_url(cand.url):
                res = self._materialize(clip, cand, duration, stop_event=stop_event)
                if res.get('ok'):
                    self._add_credit(cid, cand)
                    return {'id': cid, 'status': 'ok', 'source': 'local',
                            'media': cand.media_type, 'query': query,
                            'url': cand.url, 'page_url': cand.page_url}
                self._release_url(cand.url)
            # Свой файл битый/не режется → не теряем клип, идём на сток с тем же запросом
            logger.info("    клип %s: свой файл не подошёл (%s) → сток", cid, p.name)

        # Субъект запроса: кадр без него бесполезен, что бы ни совпало из
        # остальных слов. Промптер не назвал — догадываемся по самому запросу.
        subject = str(getattr(sq, 'subject', '') or '').strip()
        if not subject:
            from units.stock_queries import guess_subject
            subject = guess_subject(query)
        relaxed = bool(getattr(self, 'relaxed', False))
        if relaxed:
            subject = ''                  # повтор без нейрогенерации: лучше близкий кадр, чем пустой

        tolerance = float(getattr(config, 'STOCK_STRETCH_TOLERANCE_SEC', 2) or 0)
        n_vision = 0 if relaxed else int(getattr(config, 'STOCK_VISION_CANDIDATES', 0) or 0)

        def _collect(media: str) -> List[StockResult]:
            """Лист кандидатов по ЛЕСТНИЦЕ запросов: богатый → бедный.

            Стоковый поиск цепляется за одно доминирующее существительное и
            остальные выбрасывает: по «sick sailor hammock» пришли 30 карточек
            с гамаками и ни одного матроса, по «sick sailor» — 2, по «sailor» —
            6 (замер 01.09). Кадр без субъекта бесполезен, поэтому вместо того
            чтобы соглашаться на гамак без матроса, спускаемся на ступень ниже
            и ДОБИРАЕМ лист — порядок сохраняется, точные кандидаты остаются
            первыми.

            Ступени ниже первой стоят по запросу к выдаче каждая, поэтому идём
            вниз только пока листа не хватает вижну.
            """
            collected: List[StockResult] = []
            seen: set = set()
            for step_no, step in enumerate(ladder, 1):
                found = _search_cascade(
                    step, media, primary=primary_source, sources=sources,
                    clip_duration=duration if media == 'video' else 0.0,
                    target_w=self.target_w, target_h=self.target_h,
                    used_urls=self.used_urls, subject=subject,
                )
                fresh = [c for c in found if c.url not in seen]
                seen.update(c.url for c in fresh)
                collected.extend(fresh)
                if step_no > 1 and fresh:
                    logger.info("    клип %s: ступень «%s» добрала %s кандидат(ов) "
                                "→ в листе %s", cid, step[:40], len(fresh), len(collected))
                if not n_vision or len(collected) >= n_vision:
                    break
            return collected

        from units.stock_queries import query_ladder
        ladder = query_ladder(sq)
        ranked = _collect(media_type)
        # Видео не нашлось НИ В ОДНОМ стоке → пробуем ФОТО тем же запросом, и
        # только потом переписываем сам запрос. Видеобиблиотеки держат широкие
        # темы, на узкий сюжет ролика может не быть вовсе, а снимок есть. Замер
        # 26.08: из семи клипов все четыре видео-запроса вернули пусто, все три
        # фото-запроса закрылись — при одинаково коротких и точных запросах.
        # `_search_cascade` уже обошёл все источники, так что здесь речь именно о
        # смене носителя, а не о смене стока.
        if not ranked and media_type == 'video':
            logger.info("    клип %s: видео не нашлось ни в одном стоке по «%s» → пробую фото",
                        cid, query[:60])
            ranked = _collect('photo')
            if ranked:
                media_type = 'photo'
                logger.info("    клип %s: фото нашлось — беру снимок вместо футажа", cid)

        # Нигде не нашлось → альтернативный запрос (LLM), затем снова по всем источникам
        if not ranked and self.alt_query_fn:
            for alt in (self.alt_query_fn(clip, query) or [])[:2]:
                if not alt or alt.strip().lower() == query.strip().lower():
                    continue
                logger.info("    альт-запрос: '%s'", alt[:50])
                ranked = _search_cascade(
                    alt, media_type, primary=primary_source, sources=sources,
                    clip_duration=duration if media_type == 'video' else 0.0,
                    target_w=self.target_w, target_h=self.target_h, used_urls=self.used_urls,
                    # Альт-запрос переписывает сцену целиком, поэтому и субъект
                    # берём из него, а не тянем прежний.
                    subject='',
                )
                if ranked:
                    break
        if not ranked:
            return {'id': cid, 'status': 'no_match', 'query': query}

        # Вижн-отбор (25.08): N лучших кандидатов → раскадровка → выбор по
        # описанию сцены и запретам. Все забракованы → сцена уходит в другой
        # источник (AVS: EPF), а не берёт первый попавшийся футаж.
        rejected_urls: set = set()
        if n_vision > 0:
            self._vision_reason = ''
            shown = ranked[:n_vision]
            # Что вижн уже видел — чтобы повторный заход не показал ему то же
            # самое (выдача без фильтра длины почти совпадает с выдачей под ним).
            seen_urls = {c.url for c in shown}
            picked, rejected_urls, vision_order = self._vision_pick(
                clip, shown, media_type, stop_event=stop_event)

            # ЛЕСТНИЦА ФОЛБЭКОВ (решение пользователя 01.09). Сцена не уходит в
            # нейрогенерацию, пока не пройдены все ступени:
            #   1) ещё одна порция кандидатов из ТОЙ ЖЕ выдачи;
            #   2) новый запрос — средняя ступень лестницы (`query_mid`);
            #   3) новый запрос — самая бедная ступень (`query_wide`);
            #   4) голый субъект.
            # Улов, из-за которого это понадобилось: клип 2 прогона cheese5 —
            # вижн признал годным одного из пяти, тот не скачался, и сцена
            # ушла в нейрогенерацию, хотя в листе лежали десятки кандидатов, а
            # в лестнице — ещё три неиспробованных запроса.
            if picked is None and getattr(self, '_vision_reason', '') != 'interrupted':
                for step_no, step_query in enumerate(ladder, 1):
                    if stop_event is not None and stop_event.is_set():
                        return {'id': cid, 'status': 'interrupted', 'query': query}
                    if step_no == 1:
                        step_pool = list(ranked)      # выдача уже на руках
                    else:
                        logger.info("    клип %s: вижн никого не выбрал → запрос "
                                    "ступени %s «%s»", cid, step_no, step_query[:40])
                        step_pool = _search_cascade(
                            step_query, media_type, primary=primary_source,
                            sources=sources,
                            clip_duration=duration if media_type == 'video' else 0.0,
                            target_w=self.target_w, target_h=self.target_h,
                            used_urls=self.used_urls, subject=subject)
                    portion = [c for c in step_pool if c.url not in seen_urls][:n_vision]
                    if not portion:
                        logger.info("    клип %s: ступень %s «%s» — новых кандидатов "
                                    "нет (всего в выдаче %s, все уже показаны)",
                                    cid, step_no, step_query[:40], len(step_pool))
                        continue
                    seen_urls |= {c.url for c in portion}
                    logger.info("    клип %s: показываю вижну ещё %s кандидат(ов) "
                                "(ступень %s)", cid, len(portion), step_no)
                    self._vision_reason = ''
                    picked, more_rejected, vision_order = self._vision_pick(
                        clip, portion, media_type, stop_event=stop_event)
                    rejected_urls |= more_rejected
                    if getattr(self, '_vision_reason', '') == 'interrupted':
                        return {'id': cid, 'status': 'interrupted', 'query': query}
                    if picked is not None:
                        ranked = step_pool
                        break

            if picked is None:
                reason = getattr(self, '_vision_reason', '') or 'all_rejected'
                if reason == 'interrupted':
                    return {'id': cid, 'status': 'interrupted', 'query': query}
                # Вижн забраковал ВСЁ видео — это не приговор сцене:
                # снимок на ту же тему часто есть там, где годного футажа нет.
                # Раньше смена носителя срабатывала только при ПУСТОЙ выдаче, а
                # «нашли, но всё отвергли» вело прямиком в нейрогенерацию
                # (каскад 2.7, ступень 2).
                if picked is None and reason == 'all_rejected' and media_type == 'video':
                    logger.info("    клип %s: вижн забраковал всё видео по «%s» → пробую фото",
                                cid, query[:60])
                    photo_ranked = _search_cascade(
                        query, 'photo', primary=primary_source, sources=sources,
                        clip_duration=0.0,
                        target_w=self.target_w, target_h=self.target_h,
                        used_urls=self.used_urls, subject=subject,
                    )
                    if photo_ranked:
                        self._vision_reason = ''
                        picked, rejected_urls, vision_order = self._vision_pick(
                            clip, photo_ranked[:n_vision], 'photo', stop_event=stop_event)
                        if picked is not None:
                            media_type = 'photo'
                            ranked = photo_ranked
                if picked is None:
                    reason = getattr(self, '_vision_reason', '') or reason
                    status = 'download_failed' if reason == 'download_failed' else 'no_match'
                    return {'id': cid, 'status': status, 'query': query, 'vision': reason}
            # Победитель идёт первым, остальные остаются ЗАПАСОМ: раньше список
            # схлопывался в один элемент, и сбой нарезки на победителе завершал
            # клип отказом, хотя оставались проверенные кандидаты (аудит 25.08).
            # Порядок задаёт ВИЖН: первым победитель, за ним второе и третье
            # место по его же оценке. Дальше — остаток листа как последний
            # резерв. Раньше запас шёл в порядке `rank_stock`, то есть по
            # совпадению слов, и «второй по качеству кадр» мог оказаться в
            # хвосте (или, как 01.09, вовсе в браке).
            ranked = vision_order + [c for c in ranked if c not in vision_order]

        for cand in ranked[:4]:
            if stop_event is not None and stop_event.is_set():
                return {'id': cid, 'status': 'interrupted'}
            # Забракованного вижном в запас НЕ берём: он прямо назвал кадр
            # непригодным, и подставлять его после сбоя на победителе — значит
            # тихо вернуть в ролик то, что уже отвергли (каскад 2.7, ступень 1).
            if cand.url in rejected_urls:
                logger.info("    клип %s: кандидат %s пропущен — забракован вижном",
                            cid, cand.url[-40:])
                continue
            # Повтор без нейрогенерации: то, что уже стоит в ролике, не дублируем.
            if cand.page_url and cand.page_url in getattr(self, 'taken_pages', ()):
                continue
            # Резерв до скачивания: соседний поток мог выбрать этот же ролик,
            # пока мы смотрели кандидатов.
            if not self._reserve_url(cand.url):
                continue

            # Футаж короче сцены. До порога разницу тянет склейка замедлением
            # (`speed_factor` в `video_concat.process_video`), дальше — сцена
            # собирается из двух кусков. Раньше короткий кандидат просто давал
            # фрагмент не в длину сцены, и склейка растягивала его как угодно
            # сильно: пять секунд в восьмисекундной сцене — это 0.62x, видно
            # невооружённым глазом.
            partner = second_len = None
            first_len = 0.0
            if cand.media_type == 'video' and duration > 0:
                first_len = self._known_duration(cand, cid)
                if first_len and (duration - first_len) > tolerance:
                    partner, second_len = self._find_partner(
                        ranked, cand, rejected_urls, clip_id=cid)
            if partner is not None:
                res = self._materialize_parts(
                    clip, cand, partner, duration, first_len, second_len,
                    stop_event=stop_event)
                if res.get('ok'):
                    self._add_credit(cid, cand)
                    self._add_credit(cid, partner)
                    return {'id': cid, 'status': 'ok', 'source': cand.source,
                            'media': cand.media_type, 'query': query,
                            'url': cand.url, 'page_url': cand.page_url,
                            'parts': res.get('parts')}
                # Парой не вышло — второй кандидат возвращается в оборот, а этот
                # ещё может закрыть сцену один, со снисхождением склейки.
                self._release_url(partner.url)

            logger.debug("    клип %s: беру кандидата %s «%s» (%s)", cid,
                         cand.url[-30:], (cand.title or '')[:60],
                         cand.source)
            res = self._materialize(clip, cand, duration, stop_event=stop_event)
            if not res.get('ok'):
                logger.info("    клип %s: кандидат %s не закрыл сцену — беру следующего",
                            cid, cand.url[-40:])
            if res.get('ok'):
                self._add_credit(cid, cand)
                return {'id': cid, 'status': 'ok', 'source': cand.source,
                        'media': cand.media_type, 'query': query,
                        'url': cand.url, 'page_url': cand.page_url}
            self._release_url(cand.url)
        # Итог по клипу пишем ВСЕГДА и с числами: сколько кандидатов вижн
        # посмотрел, сколько забраковал, сколько попыток скачать провалилось.
        # Иначе в логе оставалось глухое «файл не скачался», и разбираться было
        # не по чему (замечание пользователя 01.09).
        logger.warning("    клип %s: сцену закрыть не удалось. Запрос «%s», "
                       "кандидатов в листе %s, забраковано вижном %s",
                       cid, query[:50], len(ranked), len(rejected_urls))
        return {'id': cid, 'status': 'download_failed', 'query': query}

    def _find_partner(self, ranked: List[StockResult], winner: StockResult,
                      rejected_urls: set, *, clip_id: int):
        """Второй футаж для сцены: следующий годный кандидат ТОГО ЖЕ листа.

        Нового поиска и нового вижн-вызова нет: берём следующего по порядку
        листа, кого вижн не забраковал. Предпочтения источникам тут нет
        намеренно — порядок задан отбором (решение пользователя 01.09).
        """
        minimum = float(getattr(config, 'STOCK_MIN_PART_SEC', 1.5) or 1.5)
        for cand in ranked:
            if cand is winner or cand.media_type != 'video':
                continue
            if cand.url in rejected_urls or cand.url == winner.url:
                continue
            if not self._reserve_url(cand.url):
                continue
            length = self._known_duration(cand, clip_id)
            if length and length >= minimum:
                return cand, length
            # Не годится — сразу отпускаем, иначе кандидат «занят» впустую.
            self._release_url(cand.url)
        logger.info("    клип %s: второго футажа для добора нет — "
                    "сцену вытянет замедление", clip_id)
        return None, None

    # ── вижн-отбор (25.08) ──────────────────────────────────────────────────
    def _vision_pick(
        self,
        clip: Dict[str, Any],
        candidates: List[StockResult],
        media_type: str,
        *,
        stop_event: Optional[threading.Event] = None,
    ) -> tuple:
        """Скачать N кандидатов и отдать общему вижн-отбору (`units.vision_picker`):
        видео — 3 кадра с футажа, фото — кадр; лист + описание сцены + запреты.

        Возвращает ПАРУ (победитель, множество забракованных url). Победитель —
        с `_prefetched` (без повторной закачки) либо None: тогда сцену закроет
        другой источник. Забракованные нужны вызывающему: подставлять их в
        запас после сбоя на победителе нельзя — вижн уже назвал их непригодными.
        Сбой самого вижна → первый кандидат, как раньше, с записью в журнал.

        Артефакты: <project>/stock/vision/NNN/ — contact_sheet.jpg, кадры,
        vision_prompt.json, vision_response.txt, selection.json.
        """
        from units.vision_picker import Candidate, pick_with_vision, resolve_vision_provider

        cid = int(clip.get('id') or 0)
        provider, model = resolve_vision_provider('stock')
        if not provider or not model:
            logger.info("    клип %s: вижн-провайдер не настроен — беру первый результат", cid)
            return (candidates[0] if candidates else None), set(), list(candidates)

        work = self.project_dir / 'stock' / 'vision' / f'{cid:03d}'
        work.mkdir(parents=True, exist_ok=True)
        fetched: List[tuple] = []  # (Candidate, StockResult)
        for index, cand in enumerate(candidates, 1):
            if stop_event is not None and stop_event.is_set():
                self._vision_reason = 'interrupted'
                return None, set(), []
            candidate_id = f'C{index}'
            if cand.media_type == 'video':
                target = work / f'{candidate_id}.mp4'
            else:
                ext = 'jpg'
                m = _PHOTO_EXT_RE.search(cand.url or '')
                if m:
                    ext = 'jpg' if m.group(1).lower() == 'jpeg' else m.group(1).lower()
                target = work / f'{candidate_id}.{ext}'
            ok = False
            try:
                if cand.source == 'local' and cand.local_path:
                    shutil.copy2(cand.local_path, target)
                    ok = True
                else:
                    ok = _http_download(cand.url, target, stop_event=stop_event, timeout=120, max_mb=200)
            except Exception as exc:  # noqa: BLE001
                logger.info("    клип %s: кандидат %s не скачался: %s", cid, candidate_id, exc)
            if ok and target.exists() and target.stat().st_size > 1024:
                fetched.append((Candidate(
                    candidate_id=candidate_id, path=target, kind=cand.media_type,
                    duration=float(cand.duration or 0), title=cand.title or '',
                    meta={'url': cand.url, 'page_url': cand.page_url, 'source': cand.source},
                ), cand))
        if not fetched:
            # Кандидаты были, но ни один не скачался — это НЕ «ничего не нашлось».
            # Раньше все четыре исхода `_vision_pick` сводились к одному коду, и
            # отчёт врал пользователю (аудит 25.08).
            logger.warning("    клип %s: ни один кандидат не скачался (%s шт.)",
                           cid, len(candidates))
            self._vision_reason = 'download_failed'
            return None, set(), []

        story = clip.get('visual_story') or {}
        continuity = story.get('continuity') or {}
        verdict = pick_with_vision(
            [c for c, _ in fetched],
            work_dir=work, provider=provider, model=model,
            scene_brief=_scene_brief(clip),
            forbidden=list(continuity.get('must_not_reveal') or clip.get('avoid') or []),
            search_query=str(getattr(self.queries.get(cid), 'query', '') or ''),
            media_kind=media_type, stage='stock',
            # Ранжирование, а не один победитель: клипы идут параллельно, и
            # победителя может занять соседняя сцена за секунды до нас. Тогда
            # запасом становится ВТОРОЕ МЕСТО по оценке вижна, а не случайный
            # уцелевший кандидат (улов 01.09, клип 6 — см. `_RANK_RULE`).
            rank=True,
        )
        if verdict.error:
            logger.warning("    клип %s: вижн стока не отработал (%s) — беру первый результат", cid, verdict.error)
            return fetched[0][1], set(), [cand for _c, cand in fetched]
        if verdict.pick is None:
            logger.info("    клип %s: вижн стока забраковал всех → сцена уйдёт в другой источник", cid)
            self._vision_reason = 'all_rejected'
            return None, {cand.url for _c, cand in fetched}, []
        rejected_urls = {cand.url for c, cand in fetched
                         if c.candidate_id in set(verdict.rejected_ids or [])}
        by_id = {c.candidate_id: (c, cand) for c, cand in fetched}
        # Годные в порядке вижна: первый — победитель, дальше запас по убыванию.
        ordered: List[StockResult] = []
        for candidate_id in (verdict.ranked_ids or [verdict.pick]):
            pair = by_id.get(candidate_id)
            if pair is None:
                continue
            c, cand = pair
            cand._prefetched = str(c.path)   # превью уже на диске, второй раз не качаем
            ordered.append(cand)
        if not ordered:
            logger.warning("    клип %s: вижн назвал кандидата %r, которого нет в списке",
                           cid, verdict.pick)
            self._vision_reason = 'vision_bad_id'
            return None, rejected_urls, []
        if len(ordered) > 1:
            logger.info("    клип %s: вижн отранжировал %s годных — запас есть",
                        cid, len(ordered))
        self._vision_reason = ''
        return ordered[0], rejected_urls, ordered

    def _known_duration(self, cand: StockResult, clip_id: int) -> float:
        """Длительность кандидата из карточки стока (0 — неизвестна)."""
        known = float(getattr(cand, 'duration', 0) or 0)
        if known > 0:
            return known
        return 0.0

    def _plan_two_parts(self, scene: float, first_len: float, second_len: float) -> tuple:
        """Как поделить сцену между двумя футажами. Возвращает (первый, второй).

        Правило пользователя (01.09): долю задаёт ВТОРОЙ футаж, а первый
        добирает остаток и не опускается ниже половины сцены. Сцена 10:
        второй 2 → 8+2, 3 → 7+3, 4 → 6+4, 5 и длиннее → 5+5.

        Так куски получаются соразмерными. Обратный порядок («первый берёт
        сколько может, второй добирает хвост») давал 8+2 всегда: восемь секунд
        плавного футажа и вдруг two-секундный обрубок — провал ритма.

        Если первому не хватает на свою долю, отдаём ему сколько есть, а разницу
        добирает второй; остаток сверх этого вытянет замедление в склейке.
        """
        half = scene / 2.0
        second_take = min(max(0.0, second_len), half)
        first_take = scene - second_take
        if first_len and first_len < first_take:
            first_take = first_len
            second_take = min(max(0.0, second_len), scene - first_take)
        return round(first_take, 3), round(second_take, 3)

    def _materialize_parts(self, clip: Dict[str, Any], first: StockResult,
                           second: StockResult, scene: float, first_len: float,
                           second_len: float, *,
                           stop_event: Optional[threading.Event] = None) -> Dict[str, Any]:
        """Собрать сцену из ДВУХ футажей: `vid_img/NNN_1.mp4` и `NNN_2.mp4`.

        Куски кладутся как есть, форматом занимается склейка — ровно так же
        работает ютуб-стадия (`_process_clip_parts` в assemble.py читает
        поле `parts`). Своей склейки здесь нет и не нужно.
        """
        cid = int(clip.get('id') or 0)
        first_take, second_take = self._plan_two_parts(scene, first_len, second_len)
        if first_take <= 0 or second_take <= 0:
            return {'ok': False}
        logger.info("    клип %s: сцена %.1fс из двух футажей — %.1fс (%s) + %.1fс (%s)",
                    cid, scene, first_take, first.source, second_take, second.source)

        produced: List[Path] = []
        sources: List[StockResult] = []
        for index, (cand, take, length) in enumerate(
                ((first, first_take, first_len), (second, second_take, second_len)), 1):
            src = (Path(cand.local_path) if cand.source == 'local'
                   else self.cache_dir / f'{cid:03d}_{index}_src.mp4')
            if not self._obtain_source(cand, src, take, clip_id=cid, stop_event=stop_event):
                for done in produced:
                    done.unlink(missing_ok=True)
                return {'ok': False}
            dst = self.vid_img_dir / f'{cid:03d}_{index}.mp4'
            cut = cut_footage(
                src, dst, take,
                target_w=self.target_w, target_h=self.target_h, fps=self.fps,
                start_sec=float(getattr(config, 'STOCK_CLIP_START_SEC', 0) or 0),
                jitter=bool(getattr(config, 'STOCK_CLIP_START_JITTER', False)),
                jitter_sec=float(getattr(config, 'STOCK_CLIP_START_JITTER_SEC', 2) or 2),
                mute=bool(getattr(config, 'STOCK_MUTE_AUDIO', True)),
                stop_event=stop_event, clip_id=cid,
            )
            if cand.source != 'local':
                src.unlink(missing_ok=True)
            if not cut.get('ok'):
                for done in produced:
                    done.unlink(missing_ok=True)
                return {'ok': False}
            produced.append(dst)
            sources.append(cand)

        # Старый одиночный файл сцены убираем: склейка ищет и его, и куски, и
        # два источника на одну сцену — это спор о том, что показывать.
        (self.vid_img_dir / f'{cid:03d}.mp4').unlink(missing_ok=True)
        clip['source'] = 'stock_video'
        clip['file'] = f'vid_img/{produced[0].name}'
        clip['parts'] = [f'vid_img/{path.name}' for path in produced]
        clip['stock_source'] = ', '.join(dict.fromkeys(c.source for c in sources))
        clip['stock_page'] = first.page_url
        return {'ok': True, 'parts': list(clip['parts']), 'second': second}

    def _obtain_source(self, cand: StockResult, src: Path, clip_duration: float,
                       *, clip_id: int, stop_event: Optional[threading.Event] = None) -> bool:
        """Положить ИСХОДНИК кандидата в `src`: покупка, скачивание или свой файл.

        Отдельным методом, потому что зовётся из двух мест: обычной
        материализации и сборки сцены из двух кусков.
        """
        if cand.source == 'local':
            return True                       # свой файл уже лежит на диске
        prefetched = getattr(cand, '_prefetched', None)
        if prefetched and Path(prefetched).exists():
            # Победитель вижн-отбора уже скачан — без повторной закачки.
            src.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(prefetched, src)
            return True
        return bool(_http_download(cand.url, src, stop_event=stop_event))

    def _materialize(self, clip: Dict[str, Any], cand: StockResult, duration: float,
                     *, stop_event: Optional[threading.Event] = None) -> Dict[str, Any]:
        cid = int(clip.get('id') or 0)
        if cand.media_type == 'video':
            src = Path(cand.local_path) if cand.source == 'local' else self.cache_dir / f'{cid:03d}_src.mp4'
            if not self._obtain_source(cand, src, duration, clip_id=cid, stop_event=stop_event):
                return {'ok': False}
            dst = self.vid_img_dir / f'{cid:03d}.mp4'
            cut = cut_footage(
                src, dst, duration,
                target_w=self.target_w, target_h=self.target_h, fps=self.fps,
                start_sec=float(getattr(config, 'STOCK_CLIP_START_SEC', 0) or 0),
                jitter=bool(getattr(config, 'STOCK_CLIP_START_JITTER', False)),
                jitter_sec=float(getattr(config, 'STOCK_CLIP_START_JITTER_SEC', 2) or 2),
                mute=bool(getattr(config, 'STOCK_MUTE_AUDIO', True)),
                stop_event=stop_event, clip_id=cid,
            )
            if not cut.get('ok'):
                # Причину обрезки НЕЛЬЗЯ терять: `cut_footage` её называет
                # («not a valid video», текст ffmpeg, таймаут), а раньше здесь
                # стоял голый `return` — и в логе оставалось только «файл не
                # скачался/не обрезался».
                logger.warning("    клип %s: обрезка не удалась (%s): %s",
                               cid, cand.source,
                               cut.get('error') or 'без причины')
                if cand.source != 'local':
                    src.unlink(missing_ok=True)
                return {'ok': False}
            if cand.source != 'local':
                src.unlink(missing_ok=True)          # исходник больше не нужен
            clip['source'] = 'stock_video'
            # Путь с папкой, как у всех остальных производителей
            # («generated/003.jpg», «vid_img/003.mp4»). Голое имя «003.mp4»
            # никто не разрешал: склейка проверяет путь относительно проекта, и
            # такое значение не находилось никогда (аудит 25.08).
            clip['file'] = f'vid_img/{dst.name}'
            clip['stock_source'] = cand.source
            clip['stock_page'] = cand.page_url
            return {'ok': True}

        # фото → generated/, concat сам сделает Ken Burns
        ext = 'jpg'
        m = _PHOTO_EXT_RE.search(cand.url or '')
        if m:
            ext = m.group(1).lower()
            ext = 'jpg' if ext == 'jpeg' else ext
        dst = self.generated_dir / f'{cid:03d}.{ext}'
        prefetched = getattr(cand, '_prefetched', None)
        if cand.source == 'local':
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cand.local_path, dst)
        elif prefetched and Path(prefetched).exists():
            # Победитель вижн-отбора уже скачан — без повторной закачки.
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(prefetched, dst)
        elif not _http_download(cand.url, dst, stop_event=stop_event):
            return {'ok': False}
        clip['source'] = 'stock_photo'
        # Фото тоже обязано объявить свой файл: расширение бывает .png/.webp, а
        # в плане у клипа мог остаться заранее прописанный «generated/003.jpg» —
        # склейка читает `file` раньше, чем сканирует папку, и брала чужой файл
        # от прошлой генерации (аудит 25.08).
        clip['file'] = f'generated/{dst.name}'
        clip['stock_source'] = cand.source
        clip['stock_page'] = cand.page_url
        return {'ok': True}

    # ── общие структуры при параллельной обработке ──────────────────────────
    def _reserve_url(self, url: str) -> bool:
        """Занять кандидата за этим клипом. False — его уже забрал сосед."""
        key = str(url or '')
        if not key:
            return True
        with self._urls_lock:
            if key in self.used_urls:
                return False
            self.used_urls.add(key)
            return True

    def _release_url(self, url: str) -> None:
        """Вернуть кандидата в общий котёл: файл не скачался/не нарезался."""
        key = str(url or '')
        if not key:
            return
        with self._urls_lock:
            self.used_urls.discard(key)

    def _add_credit(self, clip_id: int, cand: StockResult) -> None:
        with self._credits_lock:
            self.credits.append({
                'clip_id': clip_id,
                'source': cand.source,
                'author': cand.author,
                'page_url': cand.page_url,
                'license': cand.license_name,
                'title': cand.title,
            })

    def write_credits(self) -> None:
        if not self.credits:
            return
        path = self.project_dir / 'stock_credits.json'
        path.write_text(json.dumps({'items': self.credits}, ensure_ascii=False, indent=2),
                        encoding='utf-8')

        # Человекочитаемая версия для описания YouTube. Дедуп по page_url.
        # 'local' (свой материал) в атрибуцию не идёт — незачем.
        _NO_ATTRIBUTION = {'local'}
        try:
            _site = {'pexels': 'Pexels', 'pixabay': 'Pixabay'}
            lines, seen = [], set()
            for it in sorted(self.credits, key=lambda x: x.get('clip_id', 0)):
                if it.get('source') in _NO_ATTRIBUTION:
                    continue
                key = it.get('page_url') or (it.get('source'), it.get('title'))
                if key in seen:
                    continue
                seen.add(key)
                site = _site.get(it.get('source'), (it.get('source') or 'Stock').title())
                parts = [f"**{site}**"]
                if (it.get('title') or '').strip():
                    parts.append(f"«{it['title'].strip()}»")
                if (it.get('author') or '').strip():
                    parts.append(it['author'].strip())
                if (it.get('license') or '').strip():
                    parts.append(it['license'].strip())
                row = ' — '.join(parts)
                if (it.get('page_url') or '').strip():
                    row += f" — {it['page_url'].strip()}"
                lines.append(f"- {row}")
            md = (
                "# Источники футажа (сток)\n\n"
                "Стоковые материалы, использованные в видео. Можно вставить в описание ролика.\n\n"
                + ("\n".join(lines) if lines else "_только собственный материал — атрибуция не нужна_")
                + "\n"
            )
            (self.project_dir / 'stock_credits.md').write_text(md, encoding='utf-8')
        except Exception as exc:
            logger.warning("[stock] credits .md write failed (non-fatal): %s", exc)






def run_stock_stage(
    project_dir: str | Path,
    *,
    target_w: int = 1920,
    target_h: int = 1080,
    fps: Optional[int] = None,
    stop_event: Optional[threading.Event] = None,
    deadline_ts: Optional[float] = None,
    alt_query_fn=None,
    relaxed: bool = False,
) -> Dict[str, Any]:
    """Обработать клипы `stock_pending`. Возвращает сводку.

    `relaxed` — повтор для сцен, которые иначе остались бы пустыми (нейрогенерация
    запрещена): без вижн-отбора и без обязательного субъекта, берётся лучший по
    словам кандидат.

    Клипы, которым материал не нашёлся, теряют маркер → уходят в обычную
    нейрогенерацию (STOCK_PROMPT_FALLBACK). Пайплайн не встаёт.
    """
    proc = StockProcessor(project_dir, target_w=target_w, target_h=target_h,
                          fps=fps, alt_query_fn=alt_query_fn)
    proc.relaxed = bool(relaxed)
    proc.taken_pages = set()
    if relaxed:
        # Повтор идёт вторым проходом по тому же проекту: атрибуцию первого не
        # затираем (write_credits пишет файл целиком) и уже стоящий в ролике
        # футаж второй раз не берём.
        try:
            _prev = json.loads((proc.project_dir / 'stock_credits.json').read_text(encoding='utf-8'))
            proc.credits = list(_prev.get('items') or [])
            proc.taken_pages = {str(it.get('page_url')) for it in proc.credits if it.get('page_url')}
        except (OSError, ValueError):
            pass
    if not proc.montage_path.exists():
        return {'ok': 0, 'failed': 0, 'total': 0, 'error': 'montage_plan.json не найден'}

    # СВОЙ лог стадии, как у генераторов (imggen/veo). Общий лог прогона
    # длинный, и стоковые строки в нём тонут между сотней чужих; разбирать «почему
    # клип ушёл в нейрогенерацию» приходилось грепом по всему файлу. Здесь — только
    # сток, с уровнем DEBUG: поиск, отбор, покупка, нарезка, каждая причина отказа.
    from units.stage_journal import attach as _attach_log, detach as _detach_log
    stage_handler, stage_log = _attach_log(proc.project_dir, 'stock')
    if stage_log:
        logger.info("[stock] лог стадии: %s", stage_log)

    # Интро-гейт: сток не ставим раньше STOCK_START_SEC — вступление ролика
    # должно быть своей динамикой, а не стоковой нарезкой. Своя граница у
    # каждой стадии (у инфографики — собственная).
    _gate_sec = float(getattr(config, 'STOCK_START_SEC', 0) or 0)
    if _gate_sec > 0:
        from units.source_ledger import STOCK_SOURCES, apply_intro_gate
        apply_intro_gate(proc.montage_path, STOCK_SOURCES, _gate_sec,
                         label='stock', printer=print)

    plan = proc._load_plan()
    pending = proc.pending_clips(plan)
    if not pending:
        return {'ok': 0, 'failed': 0, 'total': 0}

    sources = _enabled_sources()
    if not sources:
        why = ('нет доступных источников: не задана папка библиотеки и/или '
               'не заполнены ключи PEXELS_API_KEY (или временный ключ Pexels) / PIXABAY_API_KEY')
        print(f"\n[stock] ⚠️  {why}")
        ids = [int(c.get('id') or 0) for c in pending]
        where = _release_pending(pending, why=why)
        print(f"[stock] ⚠️  все {len(pending)} помеченных клип(ов) уходят в {where}")
        proc._commit_pending_delta(pending)
        _write_stock_report(proc.project_dir, 0, ids, {i: why for i in ids}, sources)
        return {'ok': 0, 'failed': len(pending), 'total': len(pending),
                'fallback': len(pending), 'error': 'no sources'}

    print(f"\n[stock] {len(pending)} клип(ов), источники: {', '.join(sources)}")

    # Промптер: запросы + тип медиа (вызов 1) и своя библиотека (вызов 2, если включена).
    # Нет запросов вообще (LLM легла и озвучки нет) → клипы уходят в нейрогенерацию.
    proc.queries = proc.build_queries(pending)
    if not proc.queries:
        why = ('промптер стока не дал ни одного запроса '
               f'(оператор: {getattr(config, "STOCK_PROMPT_PROVIDER", "auto")}) — '
               'проверь доступность LLM')
        print(f"\n[stock] ⚠️  {why}")
        ids = [int(c.get('id') or 0) for c in pending]
        where = _release_pending(pending, why=why)
        print(f"[stock] ⚠️  все {len(pending)} помеченных клип(ов) уходят в {where}")
        proc._commit_pending_delta(pending)
        _write_stock_report(proc.project_dir, 0, ids, {i: why for i in ids}, sources)
        return {'ok': 0, 'failed': len(pending), 'total': len(pending),
                'fallback': len(pending), 'error': 'no queries'}

    # Сетевые стоки — их раздаёт карусель. `local` идёт мимо неё: файл назначает промптер.
    net_sources = [s for s in sources if s != 'local']
    ok = failed = 0
    fallback_ids: List[int] = []

    reasons: Dict[int, str] = {}          # clip_id → почему ушёл в нейрогенерацию
    results_by_id: Dict[int, Dict[str, Any]] = {}   # clip_id → res (для stock_query_log.json)

    # Клипы идут параллельно (28.08). Раньше стадия шла строго по одному, а
    # вижн на клип занимает ~40 с: десять клипов — восемь минут, и хвост
    # очереди просто не доживал до дедлайна («не дошла очередь» у клипов 34 и
    # 36 в прогоне 28.08). Ютуб распараллелен давно, сток оставался последним
    # последовательным источником.
    from concurrent.futures import ThreadPoolExecutor

    max_parallel = int(getattr(config, 'STOCK_PARALLEL', 4) or 4)
    max_parallel = max(1, min(max_parallel, len(pending)))
    counter_lock = threading.Lock()
    done = 0
    if max_parallel > 1:
        print(f"[stock] параллель поиска: {max_parallel}")

    def _handle_one(index: int, clip: Dict[str, Any]) -> None:
        nonlocal ok, failed, done
        cid = int(clip.get('id') or 0)
        if stop_event is not None and stop_event.is_set():
            return                        # маркер останется → «не дошла очередь»
        if deadline_ts and time.time() > deadline_ts:
            return
        # round-robin ПО ПОРЯДКУ КЛИПОВ: иначе соседние клипы ролика окажутся
        # из одного стока и будут визуально слипаться по стилю.
        # `local` в карусели НЕ участвует: свой файл назначает промптер (он один
        # видит и клипы, и папку), а карусель раздаёт только сетевые стоки.
        primary = net_sources[index % len(net_sources)] if net_sources else 'local'
        try:
            res = proc.process_clip(clip, primary, net_sources, stop_event=stop_event)
        except Exception as exc:          # noqa: BLE001
            # Падение одного клипа не должно ронять стадию: остальные потоки
            # продолжают, а этот уйдёт в нейрогенерацию с честной причиной.
            logger.exception("[stock] клип %s упал: %s", cid, exc)
            res = {'id': cid, 'status': 'error', 'query': ''}
        with counter_lock:
            done += 1
            position = done
            results_by_id[cid] = res
            status = res.get('status') or ''
            if status == 'ok':
                ok += 1
            elif status == 'interrupted':
                pass                      # маркер останется → «не дошла очередь»
            else:
                failed += 1
                fallback_ids.append(cid)
                reasons[cid] = _REASON_TEXT.get(status, status or 'неизвестно')
        if status == 'ok':
            print(f"   [{position}/{len(pending)}] клип {res['id']}: "
                  f"{res['media']} из {res['source']}")
        elif status != 'interrupted':
            q = (res.get('query') or '')[:40]
            print(f"   [{position}/{len(pending)}] клип {cid}: {reasons[cid]}"
                  + (f" (запрос: '{q}')" if q else "") + f" → {_fallback_word()}")

    if max_parallel == 1:
        for i, clip in enumerate(pending):
            _handle_one(i, clip)
    else:
        with ThreadPoolExecutor(max_workers=max_parallel,
                                thread_name_prefix='stock') as pool:
            list(pool.map(lambda pair: _handle_one(*pair), list(enumerate(pending))))

    # Не нашлось / прервано / дедлайн: снимаем маркер, и клип подхватит
    # генератор (или Pre-concat fallback).
    _left = [c for c in pending if c.get('source') == 'stock_pending']
    if _left:
        _release_pending(_left, why='клипы не закрыты стоком')
        for clip in _left:
            cid = int(clip.get('id') or 0)
            if cid not in fallback_ids:
                fallback_ids.append(cid)
                reasons.setdefault(cid, 'не дошла очередь (стоп/дедлайн)')

    # ТОЛЬКО свои клипы по дельте (не весь план) — другие стадии пишут параллельно,
    # полный снимок затёр бы их правки (см. apply_clip_source_delta).
    proc._commit_pending_delta(pending)
    proc.write_credits()
    _write_stock_report(proc.project_dir, ok, fallback_ids, reasons, sources)

    # Query-log (отладка качества запросов): вход(id+voiceover), запрос+кем(провайдер/path/
    # fallback-причина), медиа, ссылка на файл + страницу. Логирование не роняет стадию.
    try:
        log_items = []
        for clip in pending:
            cid = int(clip.get('id') or 0)
            sq = (proc.queries or {}).get(cid)
            res = results_by_id.get(cid)
            status = res.get('status') if res else 'skipped'
            log_items.append({
                'clip_id': cid,
                'voiceover': proc._clip_voiceover(clip)[:200],
                'query': getattr(sq, 'query', '') if sq else '',
                'media': getattr(sq, 'media', '') if sq else '',
                'query_by': getattr(sq, 'query_by', '') if sq else '',
                'query_path': getattr(sq, 'query_path', '') if sq else '',
                'fallback_reason': getattr(sq, 'fallback_reason', '') if sq else '',
                'local_file': getattr(sq, 'local_file', '') if sq else '',
                'status': status,
                'status_reason': reasons.get(cid, ''),
                'source': (res.get('source') if res else '') or '',
                'image_url': (res.get('url') if res else '') or '',
                'page_url': (res.get('page_url') if res else '') or '',
            })
        (proc.project_dir / 'stock_query_log.json').write_text(
            json.dumps({'project': proc.project_dir.name, 'items': log_items},
                       ensure_ascii=False, indent=2), encoding='utf-8')
        logger.info("[stock] query-log → stock_query_log.json (%d clips)", len(log_items))
    except Exception as exc:
        logger.warning("[stock] query-log write failed (non-fatal): %s", exc)

    if proc.cache_dir.exists():
        shutil.rmtree(proc.cache_dir, ignore_errors=True)

    # Явный отчёт юзеру: фолбек НЕ должен быть тихим — иначе непонятно,
    # почему сток «не сработал».
    print(f"\n[stock] итог: {ok} из {len(pending)} клип(ов) взяты из стока/библиотеки")
    # Сначала — ГЛАВНАЯ причина, если она системная. Прогон 30.08: оба ключа
    # недействительны, стадия не могла найти ничего в принципе, а в итоге стояло
    # только «0 из 24» — и это читалось как «на стоках нет подходящего».
    try:
        from units.stock_lookup import down_sources
        _down = down_sources()
    except Exception:                                      # noqa: BLE001
        _down = {}
    if _down:
        for _src, _why in sorted(_down.items()):
            print(f"[stock] ⛔ {_src}: ключ недействителен ({_why}) — "
                  f"источник не работал весь прогон")
        if ok == 0:
            print("[stock]    поэтому со стока не взято НИ ОДНОГО клипа: "
                  "дело не в запросах, а в ключах")
    if fallback_ids:
        by_reason: Dict[str, List[int]] = {}
        for cid in fallback_ids:
            by_reason.setdefault(reasons.get(cid, 'неизвестно'), []).append(cid)
        print(f"[stock] ⚠️  {len(fallback_ids)} клип(ов) ушли в {_fallback_word().upper()}:")
        for why, ids in sorted(by_reason.items(), key=lambda x: -len(x[1])):
            shown = ', '.join(str(i) for i in sorted(ids)[:12])
            more = f" … и ещё {len(ids) - 12}" if len(ids) > 12 else ""
            print(f"[stock]    • {why}: {len(ids)} шт (клипы {shown}{more})")
            logger.warning("[stock] %s: клипы %s", why, sorted(ids))
        print(f"[stock]    подробности: {(proc.project_dir / 'stock_report.json')}")
    if stage_log:
        print(f"[stock]    полный лог стадии: {stage_log}")
    _detach_log(stage_handler)
    return {'ok': ok, 'failed': failed, 'total': len(pending),
            'fallback': len(fallback_ids), 'sources': sources, 'reasons': reasons}
