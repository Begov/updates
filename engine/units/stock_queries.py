"""
stock_prompter.py — оператор стоковой стадии: пишет поисковые запросы и
сопоставляет локальную библиотеку.

Своя LLM, НЕ режиссёр. Pass 1 только помечает клипы (`source: stock_pending`) —
его задача монтажный план, и всё. Промптаж отдельно, потому что:
  • ответ Pass 1 не должен раздуваться (риск обрезки на длинных роликах);
  • Pass 1 чанкуется (DIRECTOR_PASS1_CHUNK_THRESHOLD) — запросы разъехались бы;
  • режиссёр может быть дорогой моделью, а «lightning storm clouds» напишет дешёвая;
  • режиссёр не знает, что лежит в папке юзера.

Два вызова, а не один
---------------------
1) озвучка клипов          → query + media (video|photo) с учётом пропорции
2) готовые query + файлы   → local_file | null        (только если папка включена)

Почему не одним: в одном модель одновременно сочиняет запрос, классифицирует
media, держит пропорцию по всему ролику, сканирует сотни файлов и следит за
уникальностью — на этом обычно и сыпется (поедет доля или файл уйдёт в два клипа).
Почему во второй вызов идёт query, а не озвучка: query — уже выжимка смысла
(вчетверо короче), ради того и делался, и локал со стоком ищут по ОДНОМУ критерию
→ отбор консистентен.

`local_file: null` — НОРМАЛЬНЫЙ, ожидаемый результат: библиотека юзера маленькая,
и грозы в ней может просто не быть. Это ничего не говорит о качестве query.
Такой клип уходит на сток с тем же запросом — фолбек, а не ошибка.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import params as config
from units.scene_context import compact_context

logger = logging.getLogger(__name__)

# Провайдеры, работающие через прямой chat API (остальные — CLI-агенты)
_CHAT_PROVIDERS = (
    'deepseek', 'openrouter', 'meta_muse', 'claude_api',
    'codex_reseller', 'gpt_nexus',
)


@dataclass
class StockQuery:
    clip_id: int
    query: str
    media: str = 'video'          # video | photo
    # Что ОБЯЗАНО быть в кадре (1-2 слова из query). Остальные слова запроса —
    # обстоятельства: они поднимают кандидата в ранге, но не решают, годен ли он.
    subject: str = ''
    # Лестница запросов от богатого к бедному (01.09). `query` — её первая
    # ступень; поле хранит ВСЮ лестницу, включая её.
    queries: List[str] = field(default_factory=list)
    local_file: str = ''          # путь из библиотеки; пусто = идём на сток
    # Трассировка для stock_query_log.json (отладка качества запросов):
    query_by: str = ''            # 'codex/model' | 'deepseek/model' | 'voiceover-fallback'
    query_path: str = 'primary'   # 'primary' | 'fallback'
    fallback_reason: str = ''     # ошибка, на которой упал промптер (если fallback)


def _provider() -> str:
    """Кто пишет запросы к стоку. Имя возвращается В КАНОНЕ `text_operator`.

    Раньше отсюда уходило `gpt` — так называется бэкенд режиссёра, но у
    `text_operator` CLI-агент зовётся `codex`. Имя не попадало в список
    CLI-агентов, уезжало в ветку chat API и падало с «Неизвестный chat-провайдер:
    gpt». Прогон 01.09.2026: запросами к стоку стали куски озвучки, сток вернул
    пусто, 5 клипов из 6 ушли в нейрогенерацию.
    """
    from units.operator_hub import normalize_provider

    p = (getattr(config, 'STOCK_PROMPT_PROVIDER', '') or '').strip().lower()
    if p and p != 'auto':
        return normalize_provider(p)
    # auto → тем же, кем размечал режиссёр; иначе прямой chat
    d = (getattr(config, 'DIRECTOR_BACKEND', '') or '').strip().lower()
    if d in ('gpt', 'openai'):
        gp = (getattr(config, 'GPT_PROVIDER', '') or '').strip().lower()
        return gp if gp in _CHAT_PROVIDERS else 'codex'
    if d in _CHAT_PROVIDERS or d in ('codex', 'claude_code'):
        return normalize_provider(d)
    return 'codex'


def _model(provider: str) -> str:
    m = (getattr(config, 'STOCK_PROMPT_MODEL', '') or '').strip()
    if m:
        return m
    return {
        'deepseek': getattr(config, 'DEEPSEEK_MODEL', ''),
        'openrouter': getattr(config, 'OPENROUTER_MODEL', ''),
        'claude_api': getattr(config, 'CLAUDE_API_MODEL', ''),
        'codex_reseller': getattr(config, 'CODEX_RESELLER_MODEL', ''),
        'gpt_nexus': getattr(config, 'GPT_NEXUS_MODEL', ''),
        'meta_muse': getattr(config, 'META_MUSE_MODEL', ''),
        # CLI-агенты: модель берётся из настроек режиссёра/Claude Code —
        # раньше их тут не было, и `ask_text` получал пустую модель.
        'codex': getattr(config, 'GPT_MODEL', ''),
        'gpt': getattr(config, 'GPT_MODEL', ''),
        'claude_code': getattr(config, 'CLAUDE_CODE_MODEL', ''),
    }.get(provider, '')


def _parse_json_loose(raw: str) -> Any:
    """JSON из ответа модели: она любит обернуть в ```json или добавить болтовню."""
    if not raw:
        return None
    txt = raw.strip()
    m = re.search(r'```(?:json)?\s*(.+?)```', txt, re.S)
    if m:
        txt = m.group(1).strip()
    try:
        return json.loads(txt)
    except Exception:
        pass
    for opener, closer in (('[', ']'), ('{', '}')):
        i, j = txt.find(opener), txt.rfind(closer)
        if i >= 0 and j > i:
            try:
                return json.loads(txt[i:j + 1])
            except Exception:
                continue
    return None


def _chat(provider: str, task: str, timeout: int = 300, *, phase: str = "stock_queries",
          work_dir=None) -> str:
    """Спросить оператора — любым транспортом (chat API или CLI-агент).

    Раньше здесь был прямой `chat_completion`, и оператор-агент (Codex,
    Claude Code) для стока просто не существовал: настройка выбиралась, а
    вызова не происходило (улог 25.08)."""
    from units.operator_hub import ask_text

    model = _model(provider)
    logging.info("   [сток/промптер] %s/%s ← задача %s символов",
                 provider, model or "—", len(task))
    raw = ask_text(
        provider, model, task,
        work_dir=Path(work_dir) if work_dir else Path.cwd(),
        phase=phase, schema_file="stock_queries.json",
        max_tokens=None, timeout=timeout,
    ) or ''
    logging.info("   [сток/промптер] ответ %s символов", len(raw))
    return raw


# ── Вызов 1: запросы + тип медиа ────────────────────────────────────────────

def _queries_prompt(clips: List[Dict[str, Any]], video_share: float, theme: str = '') -> str:
    payload = []
    for card in clips:
        item = {'id': card['id'], 'voiceover': (card.get('voiceover') or '')[:400]}
        # Описание кадра от Pass 2 — та же опора, что у архивов и ютуба. Раньше
        # сток его не получал вовсе и писал запрос по одной озвучке, хотя
        # режиссёр уже решил, ЧТО показывать (улов 26.08).
        shot = str(card.get('visual_intent') or card.get('description') or '').strip()
        if shot:
            item['shot'] = shot[:600]
        context = compact_context(card)
        if context:
            item['context'] = context
        payload.append(item)
    video_pct = int(video_share * 100)
    theme_block = (
        f"Video theme (background context): {theme}\n"
        "Ground each query in this theme AND the clip's voiceover — what is SEEN in the shot "
        "plus its own time/setting cues. The clip's own cues WIN over the theme. Make it "
        "specific (subject + setting/kind), not one broad word. Use context only to "
        "resolve references or continuity; do not copy neighboring narration into the "
        "query.\n\n"
    ) if (theme or '').strip() else ""
    return (
        "You write stock-footage search queries for a documentary video.\n\n"
        f"{theme_block}"
        "Each clip may carry `shot` — the frame as the director imagined it, written for\n"
        "an image GENERATOR. It stages a scene: invented props, a person placed just so,\n"
        "lighting, lens and mood, sometimes demands no real footage would satisfy. Read it\n"
        "for ONE thing — WHAT the frame is about — and leave the staging behind. Take the\n"
        "subject and the kind of place; drop camera work, light, weather, mood words,\n"
        "passing props, exact postures and negative demands. Drop the phase of the action\n"
        "too: footage of the same subject a moment earlier or later is still the right\n"
        "footage. Copying a staged description word-for-word searches for a shot nobody\n"
        "has filmed.\n\n"
        "For EACH clip below write:\n"
        "  query — 2-4 ENGLISH words naming what is SEEN in the shot, not what is meant.\n"
        "    Name the concrete visible subject or scene. No proper names, no dates, no\n"
        "    abstractions/metaphors, no camera directions.\n"
        # Субъект отдельным полем (28.08): без него отбор считал все слова
        # запроса равноценными. По «penguin sliding snow» проходил ролик «man
        # sliding in the snow» (2 слова из 3, пингвина нет), а десятки роликов
        # с тегом «penguin» вылетали как одно совпадение из трёх.
        "  subject — 1-2 words TAKEN FROM the query: the thing that MUST be in the\n"
        "    frame. Everything else in the query is circumstance — where it happens,\n"
        "    what the weather is, what the subject is doing. For \"penguin sliding\n"
        "    snow\" the subject is \"penguin\": a snowy slope without a penguin is the\n"
        "    wrong frame, a penguin on bare rock is still the right one.\n"
        "  media — \"video\" if the scene has motion; \"photo\" if it is static.\n"
        f"    Keep roughly {video_pct}% video / {100 - video_pct}% photo across ALL clips.\n"
        # Лестница запросов (01.09). Стоковый поиск цепляется за одно
        # доминирующее существительное и остальные выбрасывает: по «sick sailor
        # hammock» пришли 30 карточек с гамаками и НИ ОДНОГО матроса, по «sick
        # sailor» — 2 матроса, по «sailor» — 6. Материал есть, до него не
        # доходит запрос. Ослабляет его тот, кто его и сочинил: модель знает,
        # какое слово можно снять без потери смысла.
        "  query_mid — the SAME query with the least important word dropped:\n"
        "    fewer words, wider results, subject still in. Never drop the subject.\n"
        "  query_wide — the poorest form still worth searching: subject plus at\n"
        "    most ONE word. Do NOT reduce it to the bare subject alone — that step\n"
        "    is added automatically.\n"
        "    Each step must be SHORTER than the one before it. A step that adds\n"
        "    words is useless: it narrows the search instead of widening it.\n\n"
        f"## CLIPS\n{json.dumps({'clips': payload}, ensure_ascii=False)}\n\n"
        "## OUTPUT\nSTRICT JSON only, no markdown, no commentary:\n"
        '{"items":[{"clip_id":<int>,"query":"<2-4 english words>",'
        '"query_mid":"<shorter>","query_wide":"<shortest>",'
        '"subject":"<1-2 words from the query>","media":"video|photo"}]}\n'
        "One entry per clip received — do NOT skip clips. Use only words grounded in the "
        "clip's voiceover and the video theme; do NOT copy any word from these instructions."
    )


# Слова, которые описывают ОБСТОЯТЕЛЬСТВА, а не предмет съёмки. Нужны только
# для догадки о субъекте, когда промптер его не назвал: субъектом становится
# первое слово запроса, которого нет в этом списке («lone penguin ice» →
# penguin, а не lone).
_MODIFIER_WORDS = {
    'lone', 'single', 'solo', 'group', 'crowd', 'many', 'few', 'big', 'large',
    'small', 'tiny', 'huge', 'giant', 'old', 'young', 'new', 'ancient',
    'modern', 'close', 'closeup', 'macro', 'wide', 'aerial', 'drone', 'top',
    'overhead', 'distant', 'far', 'near', 'dark', 'bright', 'night', 'day',
    'morning', 'evening', 'sunset', 'sunrise', 'winter', 'summer', 'autumn',
    'spring', 'cold', 'warm', 'frozen', 'snowy', 'icy', 'wet', 'dry', 'empty',
    'busy', 'slow', 'fast', 'beautiful', 'dramatic', 'detailed', 'real',
    'natural', 'wild', 'abstract', 'blurred', 'view', 'shot', 'scene', 'time',
}


def _ladder_from_item(item: Dict[str, Any], rich: str) -> List[str]:
    """Лестница запросов из ответа промптера: богатый → средний → бедный.

    Замер 01.09 по живой выдаче Envato («sick sailor hammock» — «больной матрос
    в гамаке»): полный запрос дал 0 карточек с матросом, «sick sailor» — 2,
    «sailor» — 6. Поиск цепляется за одно доминирующее существительное («гамак»)
    и остальные выбрасывает, поэтому материал есть, а составной запрос до него
    не доходит.

    Ступень принимается только если она КОРОЧЕ предыдущей: модель иногда вместо
    ослабления «уточняет» запрос, а такая ступень выдачу не расширит.
    """
    steps: List[str] = []
    seen: set = set()

    def _add(value: str) -> None:
        value = ' '.join(str(value or '').split())
        if not value:
            return
        key = value.lower()
        if key in seen:
            return
        if steps and len(value.split()) >= len(steps[-1].split()):
            return                    # не беднее предыдущей — это не ступень
        seen.add(key)
        steps.append(value)

    _add(rich)
    _add(item.get('query_mid'))
    _add(item.get('query_wide'))
    return steps


def query_ladder(sq: 'StockQuery') -> List[str]:
    """Ступени поиска для клипа: лестница промптера плюс голый субъект.

    Субъект стоит последним и добавляется НАМИ: просить его у промптера нечего
    — он и так назвал его отдельным полем. Это самая широкая ступень: кадр с
    субъектом без обстоятельств всё ещё годен, кадр с обстоятельствами без
    субъекта — нет (гамак без матроса не иллюстрирует ничего).
    """
    steps = [q for q in (list(sq.queries) or [sq.query]) if str(q or '').strip()]
    subject = ' '.join(str(sq.subject or '').split())
    if subject and not any(s.strip().lower() == subject.lower() for s in steps):
        steps.append(subject)
    return steps


def guess_subject(query: str) -> str:
    """Субъект запроса, когда промптер его не назвал.

    Берём первое значимое слово, не описывающее обстоятельства. Догадка грубая,
    поэтому она только фолбэк: основной путь — поле `subject` от промптера.
    """
    words = [w for w in re.findall(r'[A-Za-zА-Яа-яЁё]{3,}', str(query or '').lower())]
    for word in words:
        if word not in _MODIFIER_WORDS:
            return word
    return words[0] if words else ''


def _fallback_queries(clips: List[Dict[str, Any]], video_share: float,
                      reason: str = '') -> List[StockQuery]:
    """LLM недоступна → запросом становится озвучка (стоки её токенизируют).

    Качество ниже, но стадия жива. media раскидываем по пропорции — смысл сцены
    без модели оценить нечем. `reason` — ошибка промптера (для stock_query_log).
    """
    out: List[StockQuery] = []
    for i, c in enumerate(clips):
        vo = (c.get('voiceover') or '').strip()
        out.append(StockQuery(
            clip_id=int(c['id']),
            query=vo[:80] if vo else '',
            media='video' if (i % 10) < int(video_share * 10) else 'photo',
            query_by='voiceover-fallback', query_path='fallback',
            fallback_reason=reason,
        ))
    return [q for q in out if q.query]


def generate_queries(clips: List[Dict[str, Any]], *, video_share: Optional[float] = None,
                     theme: str = '') -> List[StockQuery]:
    """Вызов 1: озвучка → query + media. Провал → озвучка как запрос."""
    if not clips:
        return []
    if video_share is None:
        # 0 — законное значение (весь сток — фото), поэтому не «or 0.70»
        video_share = getattr(config, 'STOCK_VIDEO_SHARE', None)
        video_share = 0.70 if video_share is None else max(0.0, min(1.0, float(video_share)))
    provider = _provider()
    task = _queries_prompt(clips, video_share, theme)
    _by = f'{provider}/{_model(provider)}'

    try:
        if provider in _CHAT_PROVIDERS:
            raw = _chat(provider, task)
        else:
            raw = _agent_run(provider, task, 'stock_queries.json')
        data = _parse_json_loose(raw)
        items = (data or {}).get('items') if isinstance(data, dict) else data
        if not items:
            raise ValueError('пустой/непарсимый ответ')
        by_id = {int(c['id']): c for c in clips}
        out: List[StockQuery] = []
        for it in items:
            cid = int(it.get('clip_id') or 0)
            q = (it.get('query') or '').strip()
            if cid not in by_id or not q:
                continue
            media = (it.get('media') or 'video').strip().lower()
            # Субъект берём у промптера; не назвал или назвал не из запроса —
            # догадываемся сами, иначе отбор снова уравняет все слова.
            subject = str(it.get('subject') or '').strip().lower()
            if not subject or not any(
                    w in q.lower() for w in re.findall(r'[A-Za-zА-Яа-яЁё]{3,}', subject)):
                subject = guess_subject(q)
            out.append(StockQuery(cid, q, 'photo' if media == 'photo' else 'video',
                                  subject=subject,
                                  queries=_ladder_from_item(it, q),
                                  query_by=_by, query_path='primary'))
        if not out:
            raise ValueError('ни одного валидного запроса')
        missing = [c for c in clips if int(c['id']) not in {q.clip_id for q in out}]
        if missing:
            logger.info("[stock] промптер пропустил %d клип(ов) → озвучка как запрос", len(missing))
            out += _fallback_queries(missing, video_share,
                                     reason=f'{_by}: returned fewer clips than requested')
        return out
    except Exception as exc:
        # Падение промптера меняет качество ВСЕЙ стадии: вместо «penguin sliding
        # snow» в сток уходит кусок озвучки, и поиск не находит ничего. Раньше
        # это была одна строка WARNING среди сотен — прогон 30.08 (OLEJE) юзер
        # прочитал как «сток ничего не нашёл», хотя запросов просто не было.
        short = ' | '.join(
            line.strip() for line in str(exc).splitlines() if line.strip()
        )[:240]
        print(f"\n[stock] ⚠️  Промптер запросов ({provider}) не сработал: {short}")
        print("[stock]    Запросами станут куски озвучки — сток почти наверняка "
              "вернёт пусто, клипы уйдут в нейрогенерацию.")
        logger.error("[stock] промптер (%s) не сработал: %s — озвучка как запрос",
                     provider, short)
        return _fallback_queries(clips, video_share, reason=f'{_by}: {exc}')


# ── Вызов 2: сопоставление локальной библиотеки ─────────────────────────────

def _local_prompt(queries: List[StockQuery], files: List[str]) -> str:
    q_payload = [{'clip_id': q.clip_id, 'query': q.query, 'media': q.media} for q in queries]
    return (
        "You match a user's own footage library against search queries.\n\n"
        "## LIBRARY FILES (path relative to library root)\n"
        + "\n".join(f"  {f}" for f in files) + "\n\n"
        f"## QUERIES\n{json.dumps({'items': q_payload}, ensure_ascii=False)}\n\n"
        "Rules:\n"
        "  • Judge by file and folder names — they describe the footage.\n"
        "  • Match only if the file REALLY fits the query. A wrong shot is worse than none.\n"
        "  • media must match: video query needs a video file, photo query needs an image.\n"
        "  • Each file may be used for AT MOST ONE clip. Never repeat a file.\n"
        "  • null is a NORMAL answer — the clip will then be taken from stock sites.\n\n"
        "## OUTPUT\nSTRICT JSON only, no markdown, no commentary:\n"
        '{"items":[{"clip_id":<int>,"local_file":"<exact path from list>"|null}]}'
    )


def match_local(queries: List[StockQuery], files: List[str]) -> Optional[Dict[int, str]]:
    """Вызов 2: query + файлы → {clip_id: file}.

    Возвращает:
      {...} / {} — модель отработала; пустой словарь = честное «ничего не подходит»,
                   лезть python-подбором сюда НЕ надо (найдёт мусор по случайному слову);
      None       — модель НЕ отработала (упала/недоступна) → вызывающий может
                   подстраховаться python-подбором, иначе библиотека умрёт вместе с LLM.
    """
    if not queries or not files:
        return {}
    provider = _provider()
    task = _local_prompt(queries, files)
    try:
        if provider in _CHAT_PROVIDERS:
            raw = _chat(provider, task)
        else:
            raw = _agent_run(provider, task, 'stock_local_match.json')
        data = _parse_json_loose(raw)
        if data is None:
            raise ValueError('непарсимый ответ')
        items = data.get('items') if isinstance(data, dict) else data
        if items is None:
            raise ValueError('нет поля items')
        if not items:
            return {}                      # модель ответила «ничего не подходит» — это норма
        known = set(files)
        valid_ids = {q.clip_id for q in queries}
        out: Dict[int, str] = {}
        used: set = set()
        for it in items:
            cid = int(it.get('clip_id') or 0)
            f = it.get('local_file')
            if not f or cid not in valid_ids:
                continue
            f = str(f).strip().replace('\\', '/')
            if f not in known:                     # модель выдумала путь
                logger.info("[stock] промптер вернул несуществующий файл %r — игнорирую", f[:60])
                continue
            if f in used:                          # один файл в два клипа
                logger.info("[stock] файл %r назначен повторно — игнорирую", f[:60])
                continue
            used.add(f)
            out[cid] = f
        return out
    except Exception as exc:
        logger.warning("[stock] сопоставление библиотеки (%s) не сработало: %s — "
                       "подстрахуюсь подбором по словам", provider, exc)
        return None                        # None ≠ {}: вызывающий включит python-фолбек


# ── CLI-агенты (codex / claude_code) ────────────────────────────────────────

def _agent_run(provider: str, task: str, out_name: str) -> str:
    """Спросить CLI-агента (Codex / Claude Code) через общий транспорт.

    Раньше здесь стояли прямые импорты `units.codex_runner` и
    `run_claude_code_task` — таких модулей в проекте НЕТ, поэтому ветка падала
    всегда: при `STOCK_PROMPT_PROVIDER=codex` промптер стока валился с
    «No module named 'units.codex_runner'», сток искал по сырой озвучке и не
    находил ничего (прогон 26.08: 0 из 2 клипов, запрос — обрезанная фраза
    из транскрипта).

    Теперь идём через `text_operator.ask_text` — тем же путём, которым CLI-агента
    зовут ютуб и Real Photo: он сам различает транспорт и сам решает, забрать
    ответ последним сообщением или из файла.
    """
    from units.operator_hub import ask_text

    work = Path(getattr(config, 'PROJECTS_DIR', '.')) / '_stock_prompt'
    work.mkdir(parents=True, exist_ok=True)
    phase = str(out_name).replace('.json', '') or 'stock_prompt'
    # Схема ОТВЕТА под формат этого промптера: без неё уходила контрольная
    # (`visual_story_file_control`), и Codex возвращал отчёт о записи файла
    # вместо самих запросов — парсер видел «пустой/непарсимый ответ» и стадия
    # сваливалась на озвучку (прогон 26.08).
    schema = ('stock_queries.json' if 'queries' in str(out_name)
              else 'stock_local_match.json')
    if not (Path(__file__).parent.parent / 'schemas' / schema).exists():
        schema = ''
    return ask_text(provider, _model(provider), task,
                    work_dir=work, phase=phase, schema_file=schema,
                    max_tokens=1200, timeout=180)
