"""
stock_search.py — поиск сток-футажа и фото (Pexels / Pixabay / локальная папка).

Все правила ниже выстраданы живыми проверками API (июль 2026), а не докой —
дока в ключевых местах умалчивает или врёт. Не «упрощать», не проверив заново:

Pexels:
  • orientation ОБЯЗАТЕЛЕН (без него выдача — микс горизонтали и вертикали);
    значение — от целевого формата ролика: landscape / portrait / square;
  • locale=ru-RU ЗАПРЕЩЁН — с ним url теряет slug (остаётся голый id), а slug у
    Pexels единственный источник ключевых слов (tags пусты в 280/280 проверенных);
  • русский запрос работает и без locale;
  • quality бывает null → размер файла выбираем по фактическим width/height.

Pexels без ключа («Временный API-ключ», живая проверка 10.10.2026) — json сайта
  https://www.pexels.com/_next/data/<id сборки>/<locale>/search[/videos].json?q=<запрос>:
  • вид …/search/<запрос>.json?query=… ищет СЛОВО ИЗ ПУТИ (rock.json?query=sun
    отдаёт камни) и работает только для «известных» запросов — остальные сайт
    перенаправляет (__N_REDIRECT) на …/search.json?q=…, его и используем сразу;
  • <id сборки> меняется с каждым выкатом сайта; старый id → HTTP 404. Пустой или
    устаревший id берётся с главной pexels.com ("buildId"), один раз за прогон;
  • locale по языку запроса (латиница → en-US, кириллица → ru-RU): на нём теги и
    заголовки, по которым считается релевантность;
  • 24 элемента на страницу, листается &page=N; &orientation= работает как в API;
  • Cloudflare без cookies и браузерных заголовков через раз отвечает 403;
  • теги есть (в API их нет) — релевантность считается по ним, а не по slug;
  • фото: image.download_link — оригинал (до 6000px, 5+ МБ), &w=N отдаёт нужный
    размер; видео: video.video_files[].link — прямые mp4, как в API;
  • без User-Agent — 403.

Pixabay:
  • per_page ∈ [3, 200] — 1 и 2 дают HTTP 400;
  • параметров фильтрации ИИ НЕ существует (ai=false и т.п. принимаются, но
    ничего не меняют) → отсекаем isAiGenerated на своей стороне;
  • lang=ru даёт русские теги;
  • totalHits=500 — потолок ответа, а не реальное число совпадений;
  • 'large' ≠ 4K: у разных роликов размеры под одним именем разные.
"""
from __future__ import annotations

import http.cookiejar
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import params as config
logger = logging.getLogger(__name__)

VIDEO_EXTS = {'.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v'}
PHOTO_EXTS = {'.jpg', '.jpeg', '.png', '.webp'}

# Мусорные слова: дают ложные совпадения при подсчёте релевантности.
_STOPWORDS = {
    'a', 'an', 'the', 'is', 'are', 'was', 'were', 'be', 'in', 'on', 'at', 'of', 'to',
    'and', 'or', 'for', 'with', 'from', 'by', 'as', 'it', 'its', 'this', 'that',
    'video', 'footage', 'clip', 'stock', 'free',
    'и', 'в', 'на', 'с', 'по', 'из', 'за', 'от', 'до', 'у', 'о', 'об', 'для',
    'это', 'как', 'что', 'над', 'под', 'при',
}


@dataclass
class StockResult:
    source: str                 # pexels | pixabay | local
    media_type: str             # video | photo
    url: str                    # прямая ссылка на файл (или путь для local)
    page_url: str = ''          # страница источника (для credits)
    author: str = ''            # автор (для credits)
    title: str = ''             # человекочитаемое описание
    keywords: set = field(default_factory=set)
    duration: float = 0.0       # 0 для фото
    width: int = 0
    height: int = 0
    fps: float = 0.0
    is_ai: bool = False         # Pixabay умеет это сказать; Pexels — нет
    low_quality: bool = False
    license_name: str = ''
    local_path: str = ''        # для source=local
    # ── платные источники (filesta_envato) ──────────────────────────────────
    # У бесплатных стоков `url` — это и есть конечный файл: скачал и вставил.
    # У Envato за файлом стоит платная загрузка (300 в сутки), а до неё доступны
    # только превью. Поля с безопасными дефолтами: остальные провайдеры их не
    # заполняют, и ничего для них не меняется.
    preview_url: str = ''       # публичный mp4-превью (у видео), с водяным знаком
    poster_url: str = ''        # кадр-обложка карточки
    preview_only: bool = False  # `url` — НЕ конечный файл: за ним платная загрузка
    asset_id: str = ''          # код ассета Envato (7 символов)


def _words(text: str) -> set:
    """Значимые слова (>=3 символов), без стоп-слов и без чисто числовых токенов.

    Числа отбрасываем: в тегах Pixabay попадаются id вида '214505244', они дают
    ложные совпадения при подсчёте релевантности.
    """
    return {w for w in re.findall(r'[a-zA-Zа-яА-ЯёЁ0-9]{3,}', (text or '').lower())
            if w not in _STOPWORDS and not w.isdigit()}


def match_count(qwords: set, kwords: set) -> int:
    """Сколько слов запроса нашлось у кандидата, с учётом словоформ.

    Точное сравнение множеств здесь не годится и создаёт системный перекос:
      • «thunderstorm» vs slug «thunderstorms-during-night-time» → 0 совпадений
        из-за одной буквы (мн. число), хотя ролик ровно про это;
      • у Pixabay 8-10 тегов, у Pexels 4-6 слов из slug — Pexels проигрывал бы
        всегда, и его годные ролики отсеивались бы как «не по теме».
    Поэтому для слов от 4 символов засчитываем вхождение подстрокой в обе стороны
    (storm ⊂ thunderstorms, гроза ⊂ грозами). Для коротких (3 символа) — только
    точное совпадение, иначе «art» найдётся в «earth».
    """
    n = 0
    for q in qwords:
        if q in kwords:
            n += 1
        elif len(q) >= 4 and any(q in k or (len(k) >= 4 and k in q) for k in kwords):
            n += 1
    return n


def _slug_words(url: str) -> tuple:
    """Ключевые слова из slug Pexels: .../video/lightning-strikes-on-a-dark-night-8223945/

    Единственный источник релевантности для видео Pexels — tags там всегда пусты.
    """
    tail = (url or '').rstrip('/').rsplit('/', 1)[-1]
    tail = re.sub(r'-\d+$', '', tail)               # хвостовой id
    text = tail.replace('-', ' ')
    return text, _words(text)


# ── Ротация ключей ──────────────────────────────────────────────────────────
_key_lock = threading.Lock()
_key_state: Dict[str, int] = {}      # provider -> индекс текущего ключа
_key_disabled: Dict[str, set] = {}   # provider -> индексы, упавшие в 429
# Ключи, которые сток ОТВЕРГ (401/403 или 400 «invalid api key»). В отличие от
# 429 это не временно: пробовать их снова весь прогон бессмысленно. Держим
# отдельно, иначе сброс лимитных ключей в `_next_key` воскресил бы и эти.
_key_invalid: Dict[str, set] = {}    # provider -> индексы отвергнутых ключей
# Источник, у которого не осталось ни одного рабочего ключа: причина словами.
# Прогон 30.08 (OLEJE): у пользователя недействительны оба ключа, и стадия
# высыпала в лог десятки одинаковых «HTTP 401 Invalid API key», а на выходе
# сказала лишь «итог: 0 из 24» — причина утонула, отказ выглядел как «ничего не
# нашлось». Теперь источник выключается на прогон с одной внятной строкой.
_source_down: Dict[str, str] = {}    # provider -> причина
_down_announced: set = set()


def down_sources() -> Dict[str, str]:
    """Источники, выключенные из-за ключей: {провайдер: причина}."""
    with _key_lock:
        return dict(_source_down)


def _all_keys(provider: str) -> List[str]:
    """PEXELS_API_KEY, PEXELS_API_KEY_2, _3, … _8 — пул ключей провайдера.

    Читаем и из config, и из окружения: config.py объявляет только _2 и _3, а .env
    юзера может содержать больше. Без os.getenv ключи _4.._8 молча игнорировались бы
    (getattr вернул бы дефолт '') — юзер добавил бы 5 ключей, а работали бы 3.
    Дубли отсекаем: один и тот же ключ в двух слотах не должен «удваивать» лимит.
    """
    base = 'PEXELS_API_KEY' if provider == 'pexels' else 'PIXABAY_API_KEY'
    keys: List[str] = []
    seen: set = set()
    for name in [base] + [f'{base}_{i}' for i in range(2, 9)]:
        k = (getattr(config, name, '') or os.getenv(name, '') or '').strip()
        if k and k not in seen:
            seen.add(k)
            keys.append(k)
    return keys


def _next_key(provider: str) -> Optional[str]:
    keys = _all_keys(provider)
    if not keys:
        return None
    with _key_lock:
        if provider in _source_down:        # источник выключен на прогон
            return None
        bad = _key_invalid.setdefault(provider, set())
        usable = [i for i in range(len(keys)) if i not in bad]
        if not usable:                      # все ключи отвергнуты стоком
            return None
        dead = _key_disabled.setdefault(provider, set())
        alive = [i for i in usable if i not in dead]
        if not alive:                       # все в 429 — сбрасываем, вдруг лимит уже отпустило
            dead.clear()
            alive = list(usable)
        idx = _key_state.get(provider, 0)
        if idx not in alive:
            idx = alive[0]
        _key_state[provider] = idx
        return keys[idx]


def _mark_key_exhausted(provider: str) -> None:
    """429 → пометить текущий ключ и перейти к следующему."""
    keys = _all_keys(provider)
    if not keys:
        return
    with _key_lock:
        idx = _key_state.get(provider, 0)
        _key_disabled.setdefault(provider, set()).add(idx)
        alive = [i for i in range(len(keys)) if i not in _key_disabled[provider]]
        if alive:
            _key_state[provider] = alive[0]
            logger.info("%s: ключ #%d исчерпан → переключаюсь на #%d", provider, idx + 1, alive[0] + 1)


_SOURCE_TITLE = {'pexels': 'Pexels', 'pixabay': 'Pixabay'}
_SOURCE_ENV = {'pexels': 'PEXELS_API_KEY', 'pixabay': 'PIXABAY_API_KEY'}


def _mark_key_invalid(provider: str, detail: str) -> None:
    """Сток отверг ключ (401/403/«invalid api key») → снять его с прогона.

    Отличается от `_mark_key_exhausted` тем, что ключ не воскресает: лимит
    отпускает через время, а недействительный ключ недействителен всегда.
    Когда рабочих ключей не осталось — источник выключается, и об этом
    печатается ОДНА заметная строка вместо потока одинаковых предупреждений.
    """
    keys = _all_keys(provider)
    if not keys:
        return
    title = _SOURCE_TITLE.get(provider, provider or 'сток')
    env_name = _SOURCE_ENV.get(provider, '')
    announce = ''
    with _key_lock:
        idx = _key_state.get(provider, 0)
        _key_invalid.setdefault(provider, set()).add(idx)
        alive = [i for i in range(len(keys)) if i not in _key_invalid[provider]]
        if alive:
            _key_state[provider] = alive[0]
            logger.warning("%s: ключ #%d отвергнут (%s) → перехожу на #%d",
                           provider, idx + 1, detail, alive[0] + 1)
        elif provider not in _source_down:
            _source_down[provider] = detail
            if provider not in _down_announced:
                _down_announced.add(provider)
                announce = (
                    f"\n[stock] ⛔ {title}: ключ недействителен ({detail}). "
                    f"Источник выключен до конца прогона — клипы уйдут в "
                    f"нейрогенерацию."
                    + (f"\n[stock]    Проверь {env_name} в настройках "
                       f"(ключ берётся на сайте сервиса, бесплатно)."
                       if env_name else "")
                )
    if announce:
        # print, а не только logger: строка должна быть видна пользователю в
        # Control Panel и в консоли, а не лежать одной из сотен в файле лога.
        print(announce)
        logger.error("[stock] %s выключен: ключ недействителен (%s)", title, detail)


def _looks_like_bad_key(code: int, body: str) -> bool:
    """Ответ стока означает «ключ не годится»?

    401/403 — однозначно. Pixabay на пустой или чужой ключ отвечает 400 с
    текстом «Invalid or missing API key», а другие его 400 (например per_page
    вне диапазона) к ключу отношения не имеют — поэтому смотрим текст.
    """
    if code in (401, 403):
        return True
    return code == 400 and 'api key' in (body or '').lower()


def _http_json_once(url: str, headers: Optional[Dict[str, str]] = None,
                    provider: str = '', timeout: int = 30,
                    cookies: Optional[http.cookiejar.CookieJar] = None) -> tuple:
    """Один запрос. Возвращает (data, retriable, retry_after_sec).

    `retriable` отделяет отказ стока (лимит, авария на их стороне, обрыв связи)
    от честного «такого запроса нет»: по первому надо подождать и повторить, по
    второму — идти дальше. `cookies` — общая банка cookies между запросами (сайту
    Pexels без них Cloudflare через раз отвечает 403 «Just a moment…»).
    """
    hdrs = {'User-Agent': 'begoframe-stock/1.0'}
    hdrs.update(headers or {})
    proxy = (getattr(config, 'STOCK_PROXY_URL', '') or '').strip()
    try:
        handlers = [urllib.request.ProxyHandler({'http': proxy, 'https': proxy}) if proxy
                    else urllib.request.ProxyHandler({})]
        if cookies is not None:
            handlers.append(urllib.request.HTTPCookieProcessor(cookies))
        opener = urllib.request.build_opener(*handlers)
        req = urllib.request.Request(url, headers=hdrs)
        with opener.open(req, timeout=timeout) as r:
            data = json.loads(r.read())
            rem = r.headers.get('X-Ratelimit-Remaining') or r.headers.get('X-RateLimit-Remaining')
            if (provider in ('pexels', 'pixabay') and rem is not None
                    and str(rem).isdigit() and int(rem) <= 2):
                logger.info("%s: остаток лимита ключа = %s", provider or 'stock', rem)
                _mark_key_exhausted(provider)
            return data, False, 0.0
    except urllib.error.HTTPError as e:
        retry_after = 0.0
        raw_after = ''
        try:
            raw_after = (e.headers.get('Retry-After') or '') if e.headers else ''
        except Exception:                                  # noqa: BLE001
            raw_after = ''
        if str(raw_after).strip().isdigit():
            retry_after = float(str(raw_after).strip())
        if e.code == 429:
            logger.info("%s: 429 rate-limit", provider or 'stock')
            if provider in ('pexels', 'pixabay'):
                _mark_key_exhausted(provider)
            return None, True, retry_after
        if provider == 'pexels_web' and e.code == 404:
            # 404 на /_next/data/<id>/… — сайт выкатил новую сборку, id устарел.
            # Источник не выключаем: `_pexels_web_page` возьмёт свежий id с сайта.
            return _PEXELS_STALE, False, 0.0
        if provider == 'pexels_web' and e.code == 403:
            # проверка Cloudflare «Just a moment…»: со свежими cookies повтор обычно проходит
            logger.info("pexels: Cloudflare ответил 403 — повторю")
            return None, True, 0.0
        if e.code >= 500:
            logger.warning("%s: HTTP %s на стороне стока", provider or 'stock', e.code)
            return None, True, retry_after
        body = ''
        try:
            body = (e.read()[:300] if e.fp else b'').decode('utf-8', 'replace')
        except Exception:                                  # noqa: BLE001
            body = ''
        if provider in ('pexels', 'pixabay') and _looks_like_bad_key(e.code, body):
            _mark_key_invalid(provider, f'HTTP {e.code}')
            return None, False, 0.0
        logger.warning("%s: HTTP %s — %s", provider or 'stock', e.code, body[:150])
        return None, False, 0.0
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # Обрыв связи, таймаут, DNS — сток мог и не отказать; повторяем.
        logger.warning("%s: связь оборвалась: %s: %s", provider or 'stock',
                       type(exc).__name__, exc)
        return None, True, 0.0
    except Exception as exc:                               # noqa: BLE001
        logger.warning("%s: запрос упал: %s: %s", provider or 'stock', type(exc).__name__, exc)
        return None, False, 0.0


def _request_json(url_fn, provider: str = '', *, headers_fn=None,
                  timeout: int = 30) -> Optional[Dict[str, Any]]:
    """Запрос к стоку с повтором и кулдауном (задание пользователя 28.08).

    Раньше единственный отказ означал для клипа «ничего не нашлось», и он уходил
    прямиком в нейрогенерацию — при 12 одновременных клипах цена такой ошибки
    выросла бы. Теперь: 429 → ключ уже переключён `_mark_key_exhausted`, ждём и
    пробуем снова УЖЕ ДРУГИМ ключом (у Pixabay он внутри URL, поэтому адрес
    собирается заново на каждой попытке); 5xx и обрывы связи — та же пауза.
    Заголовок `Retry-After` уважаем, если сток его прислал.
    """
    attempts = max(1, 1 + int(getattr(config, 'STOCK_HTTP_RETRIES', 3) or 0))
    cooldown = max(0.5, float(getattr(config, 'STOCK_HTTP_COOLDOWN', 5) or 5))
    delay = cooldown
    for attempt in range(1, attempts + 1):
        key = _next_key(provider) if provider in ('pexels', 'pixabay') else ''
        if provider in ('pexels', 'pixabay') and not key:
            return None
        data, retriable, retry_after = _http_json_once(
            url_fn(key), headers_fn(key) if headers_fn else None,
            provider=provider, timeout=timeout)
        if data is not None or not retriable or attempt >= attempts:
            if data is None and retriable:
                logger.warning("%s: отказ не ушёл за %d попыт(ок) — источник пропускаю",
                               provider or 'stock', attempts)
            return data
        wait = retry_after if retry_after > 0 else delay
        logger.info("%s: отказ (попытка %d из %d) — жду %.0fс и повторяю",
                    provider or 'stock', attempt, attempts, wait)
        time.sleep(wait)
        delay *= 2


def _pick_file_by_size(files: List[Dict[str, Any]], target_w: int, target_h: int) -> Optional[Dict[str, Any]]:
    """Файл с фактическим размером, ближайшим к целевому (не по имени 'large'/'hd').

    Штраф считаем по ОБЕИМ сторонам. Только по ширине нельзя: Pixabay отдаёт ролики
    вида 2048x631 (аспект 3.25:1) — по ширине это «почти 1920», а по факту, чтобы
    заполнить кадр 1080p, высоту пришлось бы растянуть в 1.7 раза → мыло во весь экран.
    Нехватку штрафуем вдвое: апскейл заметно хуже даунскейла.
    """
    def side_penalty(actual: int, target: int) -> float:
        return (target - actual) * 2.0 if actual < target else (actual - target) * 1.0

    scored = []
    for f in files:
        w, h = int(f.get('width') or 0), int(f.get('height') or 0)
        if w <= 0 or h <= 0:
            continue
        penalty = side_penalty(w, target_w) + side_penalty(h, target_h)
        scored.append((penalty, f))
    if not scored:
        return None
    scored.sort(key=lambda x: x[0])
    return scored[0][1]


# ── Pexels ──────────────────────────────────────────────────────────────────

def pexels_temp_key() -> str:
    """id сборки сайта из «Временного API-ключа». Принимает и голый id, и всю ссылку."""
    raw = (getattr(config, 'PEXELS_TEMP_KEY', '') or os.getenv('PEXELS_TEMP_KEY', '') or '').strip()
    m = re.search(r'/_next/data/([^/?#\s]+)/', raw)
    key = (m.group(1) if m else raw).strip().strip('/')
    return key if re.fullmatch(r'[A-Za-z0-9_-]+', key or '') else ''


def pexels_configured() -> bool:
    """Pexels доступен всегда: обычный ключ → API, иначе json сайта. id сборки для
    него берётся из «Временного API-ключа», а если тот пуст или устарел — с самого
    сайта (`_pexels_refresh_key`)."""
    return True


_PEXELS_STALE = {'__pexels_stale_build__': True}     # ответ «id сборки устарел» (HTTP 404)
_pexels_live = {'key': '', 'tried': False}           # id, найденный на сайте в этом прогоне
_pexels_refresh_lock = threading.Lock()


def _pexels_web_key() -> str:
    """id сборки для запросов: найденный на сайте в этом прогоне или из настроек."""
    return _pexels_live['key'] or pexels_temp_key()


def _pexels_fresh_build_id() -> str:
    """Текущий id сборки с главной pexels.com (`"buildId"` в __NEXT_DATA__); '' — не вышло.

    id меняется с каждым выкатом сайта, и ключ из настроек протухает за дни. Главная
    отдаётся тем же браузерным заголовкам и банке cookies, что и данные поиска
    (живая проверка 13.10.2026: 15 из 15)."""
    hdrs = dict(_PEXELS_WEB_HEADERS)
    hdrs.pop('x-nextjs-data', None)
    hdrs.update({'Accept': 'text/html,application/xhtml+xml', 'Sec-Fetch-Dest': 'document',
                 'Sec-Fetch-Mode': 'navigate'})
    proxy = (getattr(config, 'STOCK_PROXY_URL', '') or '').strip()
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({'http': proxy, 'https': proxy} if proxy else {}),
        urllib.request.HTTPCookieProcessor(_pexels_web_cookies))
    for _ in range(3):                                   # Cloudflare изредка просит повтор
        try:
            with opener.open(urllib.request.Request('https://www.pexels.com/', headers=hdrs),
                             timeout=30) as r:
                html = r.read().decode('utf-8', 'replace')
            m = re.search(r'"buildId"\s*:\s*"([A-Za-z0-9_-]+)"', html)
            if m:
                return m.group(1)
        except Exception as exc:                         # noqa: BLE001
            logger.info("pexels: главная сайта не открылась: %s: %s", type(exc).__name__, exc)
        time.sleep(1.0)
    return ''


def _pexels_refresh_key(stale: str) -> str:
    """Свежий id сборки вместо `stale` (один поход на сайт за прогон). '' — не вышло."""
    with _pexels_refresh_lock:
        if _pexels_live['key'] and _pexels_live['key'] != stale:
            return _pexels_live['key']                   # другой поток уже обновил
        if _pexels_live['tried']:
            return ''
        _pexels_live['tried'] = True
        fresh = _pexels_fresh_build_id()
        if not fresh or fresh == stale:
            return ''
        _pexels_live['key'] = fresh
    print(f"[stock] Pexels: id сборки сайта {'устарел' if stale else 'не задан'} — "
          f"взял свежий с pexels.com ({fresh})")
    logger.info("pexels: id сборки %s → %s", stale or '—', fresh)
    return fresh


def search_pexels(query: str, media_type: str = 'video', limit: int = 20,
                  target_w: int = 1920, target_h: int = 1080) -> List[StockResult]:
    """Обычный ключ прописан — поиск через API, иначе через json сайта (фото и видео)."""
    if _all_keys('pexels'):
        return _search_pexels_api(query, media_type, limit, target_w, target_h)
    return _search_pexels_web(query, media_type, limit, target_w, target_h)


_PEXELS_WEB_PAGE = 24          # столько элементов сайт отдаёт на страницу
_PEXELS_WEB_MAX_PAGES = 4      # 4 × 24 ≈ STOCK_SEARCH_LIMIT 80
# Заголовки как у запроса данных из браузера + общая банка cookies (__cf_bm, _cfuvid):
# голые запросы Cloudflare пропускал через раз (замер 10.10: 6 из 10), так — 10 из 10.
_PEXELS_WEB_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36'),
    'Accept': '*/*',
    'Accept-Language': 'en-US,en;q=0.9,ru;q=0.8',
    'Referer': 'https://www.pexels.com/',
    'x-nextjs-data': '1',
    'Sec-Fetch-Dest': 'empty',
    'Sec-Fetch-Mode': 'cors',
    'Sec-Fetch-Site': 'same-origin',
}
_pexels_web_cookies = http.cookiejar.CookieJar()     # CookieJar потокобезопасен


def _pexels_web_down(detail: str) -> None:
    """Временный ключ не принят сайтом → Pexels выключен до конца прогона (одна строка в лог)."""
    with _key_lock:
        if 'pexels' in _source_down:
            return
        _source_down['pexels'] = detail
        first = 'pexels' not in _down_announced
        _down_announced.add('pexels')
    if first:
        print(f"\n[stock] ⛔ Pexels: временный API-ключ не подходит ({detail}). "
              f"Источник выключен до конца прогона — клипы уйдут в нейрогенерацию."
              f"\n[stock]    Обнови «Временный API-ключ» в Настройках → Сток: возьми id из "
              f"свежей ссылки pexels.com/_next/data/<id>/… (или впиши обычный ключ Pexels).")
        logger.error("[stock] Pexels выключен: временный ключ не подходит (%s)", detail)


def _pexels_web_page(key: str, query: str, media_type: str, orient: str,
                     page: int) -> Optional[Dict[str, Any]]:
    """Одна страница выдачи; устаревший id сборки один раз меняется на свежий с сайта."""
    init = _pexels_web_page_once(key, query, media_type, orient, page)
    if init is not _PEXELS_STALE:
        return init
    fresh = _pexels_refresh_key(key)
    if fresh:
        init = _pexels_web_page_once(fresh, query, media_type, orient, page)
        if init is not _PEXELS_STALE:
            return init
    _pexels_web_down('id сборки устарел, а свежий с сайта взять не удалось')
    return None


def _pexels_web_page_once(key: str, query: str, media_type: str, orient: str,
                          page: int) -> Optional[Dict[str, Any]]:
    """Одна страница выдачи сайта. None — ничего не пришло (причина уже в логе).

    Адрес — общий поиск сайта `…/search.json?q=` (видео: `…/search/videos.json?q=`).
    Вид `…/search/<запрос>.json?query=` сайт отдаёт только для «известных» запросов,
    остальные (lightning storm night, old soviet factory…) перенаправляет
    (__N_REDIRECT) ровно сюда — поэтому идём сразу сюда, без лишнего запроса.
    """
    q = urllib.parse.quote(query.strip(), safe='')
    locale = 'ru-RU' if _is_cyrillic(query) else 'en-US'   # язык тегов и заголовков = язык запроса
    kind = 'search/videos' if media_type == 'video' else 'search'
    url = (f'https://www.pexels.com/_next/data/{key}/{locale}/{kind}.json'
           f'?q={q}&orientation={orient}' + (f'&page={page}' if page > 1 else ''))
    data, retriable, _ = _http_json_once(url, _PEXELS_WEB_HEADERS, provider='pexels_web',
                                         cookies=_pexels_web_cookies)
    if data is None and retriable:                     # обрыв / 5xx / 429 / Cloudflare — пауза и повтор
        time.sleep(max(0.5, float(getattr(config, 'STOCK_HTTP_COOLDOWN', 5) or 5)))
        data, _, _ = _http_json_once(url, _PEXELS_WEB_HEADERS, provider='pexels_web',
                                     cookies=_pexels_web_cookies)
    if data is _PEXELS_STALE:
        return _PEXELS_STALE
    if not isinstance(data, dict):
        return None
    props = data.get('pageProps') or {}
    if props.get('__N_REDIRECT'):
        logger.info("pexels: «%s» — сайт перенаправил запрос (%s), пропускаю",
                    query[:60], props.get('__N_REDIRECT'))
        return None
    init = props.get('initialData')
    if not isinstance(init, dict):
        # так сайт отвечает и на устаревший id (вместе с 404, см. _http_json_once)
        return _PEXELS_STALE
    return init


def _pexels_web_keywords(attrs: Dict[str, Any]) -> set:
    tags = ' '.join(f"{t.get('name') or ''} {t.get('search_term') or ''}"
                    for t in (attrs.get('tags') or []) if isinstance(t, dict))
    return (_words(tags) | _words(attrs.get('title') or '') | _words(attrs.get('description') or '')
            | _words(attrs.get('alt') or '') | _words((attrs.get('slug') or '').replace('-', ' ')))


def _pexels_web_author(attrs: Dict[str, Any]) -> str:
    user = attrs.get('user') or {}
    return ' '.join(p for p in (user.get('first_name'), user.get('last_name')) if p) or user.get('username') or ''


def _pexels_photo_link(image: Dict[str, Any], w: int, h: int,
                       target_w: int, target_h: int) -> tuple:
    """Ссылка на фото под кадр: оригинал, ужатый параметром w до «закрыть кадр», и его размер."""
    link = image.get('download_link') or ''
    if not link:
        return '', w, h
    if w > 0 and h > 0:
        scale = max(target_w / w, target_h / h)        # во сколько раз ужать, чтобы ещё закрывать кадр
        if scale < 1:
            new_w = max(1, int(round(w * scale)))
            sep = '&' if '?' in link else '?'
            return f'{link}{sep}w={new_w}', new_w, int(round(h * new_w / w))
    return link, w, h


def _search_pexels_web(query: str, media_type: str, limit: int,
                       target_w: int, target_h: int) -> List[StockResult]:
    """Поиск без ключа — json страницы поиска pexels.com (см. шапку модуля)."""
    with _key_lock:
        if 'pexels' in _source_down:
            return []
    key = _pexels_web_key() or _pexels_refresh_key('')
    if not key:
        _pexels_web_down('id сборки не задан, а с сайта взять не удалось')
        return []
    orient = ('portrait' if target_h > target_w
              else ('square' if target_h == target_w else 'landscape'))
    pages = max(1, min(_PEXELS_WEB_MAX_PAGES, -(-int(limit or 1) // _PEXELS_WEB_PAGE)))
    out: List[StockResult] = []
    seen: set = set()
    for page in range(1, pages + 1):
        init = _pexels_web_page(key, query, media_type, orient, page)
        if not init:
            break
        items = init.get('data') or []
        for item in items:
            attrs = (item or {}).get('attributes') or {}
            item_id = attrs.get('id') or item.get('id')
            if not item_id or item_id in seen:
                continue
            seen.add(item_id)
            slug = attrs.get('slug') or ''
            title = (attrs.get('title') or attrs.get('alt') or attrs.get('description') or '').strip()
            if media_type == 'video':
                if item.get('type') != 'video':
                    continue
                video = attrs.get('video') or {}
                best = _pick_file_by_size(video.get('video_files') or [], target_w, target_h)
                if not best or not best.get('link'):
                    continue
                out.append(StockResult(
                    source='pexels', media_type='video',
                    url=best['link'],
                    page_url=f"https://www.pexels.com/video/{slug + '-' if slug else ''}{item_id}/",
                    author=_pexels_web_author(attrs),
                    title=title or slug.replace('-', ' '), keywords=_pexels_web_keywords(attrs),
                    duration=float(attrs.get('duration') or 0),
                    width=int(best.get('width') or 0), height=int(best.get('height') or 0),
                    fps=float(best.get('fps') or attrs.get('fps') or 0),
                    license_name='Pexels License',
                ))
            else:
                if item.get('type') != 'photo':
                    continue
                link, w, h = _pexels_photo_link(attrs.get('image') or {},
                                                int(attrs.get('width') or 0), int(attrs.get('height') or 0),
                                                target_w, target_h)
                if not link:
                    continue
                out.append(StockResult(
                    source='pexels', media_type='photo',
                    url=link,
                    page_url=f"https://www.pexels.com/photo/{slug + '-' if slug else ''}{item_id}/",
                    author=_pexels_web_author(attrs),
                    title=title, keywords=_pexels_web_keywords(attrs),
                    width=w, height=h,
                    license_name='Pexels License',
                ))
            if len(out) >= limit:
                return out
        pag = init.get('pagination') or {}
        if len(items) < _PEXELS_WEB_PAGE or page >= int(pag.get('total_pages') or page):
            break
    return out


def _search_pexels_api(query: str, media_type: str = 'video', limit: int = 20,
                       target_w: int = 1920, target_h: int = 1080) -> List[StockResult]:
    key = _next_key('pexels')
    if not key:
        return []
    q = urllib.parse.quote(query)
    per_page = max(1, min(int(limit), 80))
    hdrs = {'Authorization': key}
    out: List[StockResult] = []

    # Ориентация — от целевого формата ролика (9:16 → portrait), не хардкод landscape
    _orient = ('portrait' if target_h > target_w
               else ('square' if target_h == target_w else 'landscape'))

    if media_type == 'video':
        # size=medium → FHD. orientation ОБЯЗАТЕЛЕН. locale НЕ передаём (убивает slug).
        url = (f'https://api.pexels.com/v1/videos/search?query={q}'
               f'&orientation={_orient}&size=medium&per_page={per_page}')
        data = _request_json(lambda _k: url, provider='pexels',
                             headers_fn=lambda k: {'Authorization': k or key})
        for v in (data or {}).get('videos') or []:
            title, kw = _slug_words(v.get('url') or '')
            best = _pick_file_by_size(v.get('video_files') or [], target_w, target_h)
            if not best or not best.get('link'):
                continue
            out.append(StockResult(
                source='pexels', media_type='video',
                url=best['link'], page_url=v.get('url') or '',
                author=((v.get('user') or {}).get('name') or ''),
                title=title, keywords=kw,
                duration=float(v.get('duration') or 0),
                width=int(best.get('width') or 0), height=int(best.get('height') or 0),
                fps=float(best.get('fps') or 0),
                license_name='Pexels License',
            ))
    else:
        url = (f'https://api.pexels.com/v1/search?query={q}'
               f'&orientation={_orient}&per_page={per_page}')
        data = _request_json(lambda _k: url, provider='pexels',
                             headers_fn=lambda k: {'Authorization': k or key})
        for p in (data or {}).get('photos') or []:
            alt = (p.get('alt') or '').strip()
            slug_title, slug_kw = _slug_words(p.get('url') or '')
            src = p.get('src') or {}
            link = src.get('large2x') or src.get('large') or src.get('original')
            if not link:
                continue
            out.append(StockResult(
                source='pexels', media_type='photo',
                url=link, page_url=p.get('url') or '',
                author=(p.get('photographer') or ''),
                title=alt or slug_title,
                keywords=_words(alt) | slug_kw,
                width=int(p.get('width') or 0), height=int(p.get('height') or 0),
                license_name='Pexels License',
            ))
    return out


# ── Pixabay ─────────────────────────────────────────────────────────────────

def _is_cyrillic(text: str) -> bool:
    return bool(re.search(r'[а-яА-ЯёЁ]', text or ''))


def search_pixabay(query: str, media_type: str = 'video', limit: int = 20,
                   target_w: int = 1920, target_h: int = 1080) -> List[StockResult]:
    key = _next_key('pixabay')
    if not key:
        return []
    q = urllib.parse.quote_plus(query)
    per_page = max(3, min(int(limit), 200))          # <3 → HTTP 400
    lang = '&lang=ru' if _is_cyrillic(query) else ''
    out: List[StockResult] = []

    # Порог запроса — 720p-класс (MIN_WIDTH), а НЕ целевой размер кадра.
    # Раньше сюда шёл target_w/target_h, то есть 1920: при рендере в 720p это
    # отсекало годный материал, а при 4k всё равно не спасало — целевой размер
    # и минимально допустимый это разные вещи (замечание пользователя 26.08).
    # Ориентация сохранена: для портретной цели фильтруем по высоте, иначе
    # вертикальные ролики (width ~1080) отсеклись бы все.
    _dim_filter = (f'&min_height={MIN_WIDTH}' if target_h > target_w
                   else f'&min_width={MIN_WIDTH}')

    if media_type == 'video':
        # Ключ у Pixabay сидит в адресе, поэтому URL собирается на каждой
        # попытке заново — иначе повтор ушёл бы с тем же исчерпанным ключом.
        data = _request_json(
            lambda k: (f'https://pixabay.com/api/videos/?key={k or key}&q={q}'
                       f'&per_page={per_page}{_dim_filter}{lang}'),
            provider='pixabay')
        for h in (data or {}).get('hits') or []:
            vids = h.get('videos') or {}
            files = [{'width': v.get('width'), 'height': v.get('height'), 'link': v.get('url')}
                     for v in vids.values() if v.get('url')]
            best = _pick_file_by_size(files, target_w, target_h)
            if not best:
                continue
            tags = h.get('tags') or ''
            out.append(StockResult(
                source='pixabay', media_type='video',
                url=best['link'], page_url=h.get('pageURL') or '',
                author=(h.get('user') or ''),
                title=(h.get('name') or tags),
                keywords=_words(tags) | _words(h.get('name') or ''),
                duration=float(h.get('duration') or 0),
                width=int(best.get('width') or 0), height=int(best.get('height') or 0),
                is_ai=bool(h.get('isAiGenerated')),
                low_quality=bool(h.get('isLowQuality')),
                license_name='Pixabay Content License',
            ))
    else:
        data = _request_json(
            lambda k: (f'https://pixabay.com/api/?key={k or key}&q={q}'
                       f'&per_page={per_page}&image_type=photo{_dim_filter}{lang}'),
            provider='pixabay')
        for h in (data or {}).get('hits') or []:
            link = h.get('largeImageURL') or h.get('webformatURL')
            if not link:
                continue
            tags = h.get('tags') or ''
            out.append(StockResult(
                source='pixabay', media_type='photo',
                url=link, page_url=h.get('pageURL') or '',
                author=(h.get('user') or ''),
                title=tags, keywords=_words(tags),
                width=int(h.get('imageWidth') or 0), height=int(h.get('imageHeight') or 0),
                is_ai=bool(h.get('isAiGenerated')),
                low_quality=bool(h.get('isLowQuality')),
                license_name='Pixabay Content License',
            ))
    return out


# ── Local Stock Library (свои футажи из папки) ──────────────────────────────

_local_cache: Dict[str, List[StockResult]] = {}
_local_lock = threading.Lock()


def _scan_local_library(root: Path) -> List[StockResult]:
    """Все видео/фото из папки. Ключевые слова — имя файла + имя подпапки.

    Логика та же, что в music_safe_stock: юзер сам отобрал материал, поэтому
    никаких паспортов/лицензий не требуем — он его снял.
    """
    results: List[StockResult] = []
    try:
        if not root.exists():
            return results
        files = sorted(p for p in root.rglob('*')
                       if p.suffix.lower() in (VIDEO_EXTS | PHOTO_EXTS))
    except OSError as exc:
        logger.warning("Local Stock Library: папка недоступна (%s): %s", root, exc)
        return results

    for p in files:
        is_video = p.suffix.lower() in VIDEO_EXTS
        kw = _words(p.stem) | _words(p.parent.name if p.parent != root else '')
        results.append(StockResult(
            source='local', media_type='video' if is_video else 'photo',
            url=str(p), local_path=str(p),
            title=p.stem, keywords=kw,
            license_name='own material',
        ))
    return results


def search_local(query: str, media_type: str = 'video', limit: int = 20,
                 target_w: int = 1920, target_h: int = 1080) -> List[StockResult]:
    root_raw = (getattr(config, 'STOCK_LIBRARY_DIR', '') or '').strip().strip('"')
    if not root_raw:
        return []
    root = Path(root_raw).expanduser()
    ck = str(root).lower()
    with _local_lock:
        if ck not in _local_cache:
            _local_cache[ck] = _scan_local_library(root)
            logger.info("Local Stock Library: %d файлов в %s", len(_local_cache[ck]), root)
        items = _local_cache[ck]

    qwords = _words(query)
    pool = [r for r in items if r.media_type == media_type]
    # Совпадений нет вообще — вернуть пусто, пусть каскад идёт дальше. Иначе
    # отдали бы случайный файл «лишь бы что-то», а это и есть «не по теме».
    scored = [(len(qwords & r.keywords), r) for r in pool]
    hits = [r for s, r in sorted(scored, key=lambda x: -x[0]) if s > 0]
    return hits[:limit]


# ── Ранжирование ────────────────────────────────────────────────────────────

# Насколько аспект кандидата может отличаться от ЦЕЛЕВОГО (доля). Целевой аспект
# динамический (target_w/target_h): при цели 16:9 (1.78) 0.35 пропускает 4:3 и 21:9,
# отсекает «киношные» полосы и вертикалки; при цели 9:16 (0.56) — наоборот,
# пропускает вертикаль и отсекает горизонталь.
ASPECT_TOLERANCE = 0.35
# Порог качества по ДЛИННОЙ стороне кандидата (см. rank_stock): ниже 720p-класса
# брать нет смысла — апскейл даёт мыло. Работает и для портретных исходников.
MIN_WIDTH = 1280
# Запас длительности сверх клипа: обрезка -t 5.0 из ровно 5.0с даёт 4.98 (keyframes/fps),
# а concat потом растягивает фрагмент → фриз последнего кадра.
DURATION_MARGIN = 0.3
# Какая ДОЛЯ слов запроса должна совпасть с тегами кандидата, чтобы он вообще
# дошёл до вижна. 0.5 — то же значение и то же обоснование, что у локальной
# библиотеки (`stock_stage._match_local_by_words`): стоки ищут по любому слову,
# и одного случайного совпадения хватало, чтобы в тройку кандидатов попал волк
# с авторским тегом `staring` по запросу «Man staring window» (прогон 26.08).
# Настраивается на случай узких ниш, где выдача бедна и половина слов недостижима.
MATCH_MIN_SHARE = max(0.0, min(1.0, float(
    getattr(config, 'STOCK_MATCH_MIN_SHARE', 0.5) or 0.5)))


def _has_subject(subject_words: set, keywords: set) -> bool:
    """Виден ли субъект у кандидата — с учётом словоформ (penguin ⊂ penguins)."""
    if not subject_words:
        return True
    for word in subject_words:
        if word in keywords:
            return True
        if len(word) >= 4 and any(word in k or (len(k) >= 4 and k in word)
                                  for k in keywords):
            return True
    return False


def rank_stock(
    results: List[StockResult],
    query: str,
    *,
    clip_duration: float = 0.0,
    target_w: int = 1920,
    target_h: int = 1080,
    used_urls: Optional[set] = None,
    exclude_ai: Optional[bool] = None,
    subject: str = '',
) -> List[StockResult]:
    """Отфильтровать и отсортировать кандидатов. Релевантность — ПЕРВЫЙ ключ.

    Главный принцип: **лучше пусто, чем не по теме**. Кандидат без единого
    совпадения слов выбрасывается — пусть каскад идёт в соседний сток или
    перепромптит. Именно ранжирование «лишь бы что-то отдать» и породило
    жалобу на Real Photo («качает хуйню не по теме»).
    """
    if exclude_ai is None:
        exclude_ai = bool(getattr(config, 'STOCK_EXCLUDE_AI', True))
    used_urls = used_urls or set()
    qwords = _words(query)
    target_ar = (target_w / target_h) if target_h else 1.778
    # Субъект — то, что ОБЯЗАНО быть в кадре; остальные слова запроса лишь
    # поднимают кандидата в ранге. Раньше порог считал слова равноценными:
    # по «penguin sliding snow» проходил ролик «man sliding in the snow»
    # (2 слова из 3, пингвина нет), а десятки роликов с тегом «penguin»
    # вылетали как одно совпадение из трёх, и вижну доставался мусор или
    # пустота (замер 28.08: из 80 кандидатов Pixabay доживало 3, а по
    # «penguin belly sliding» — ни одного из 160).
    subject_words = _words(subject)
    dropped_no_subject = 0

    scored = []
    for r in results:
        if r.url in used_urls:
            continue                                   # уже вставлен в другой клип
        if exclude_ai and r.is_ai:
            continue                                   # ИИ-ролик в документалке = кринж
        if r.low_quality:
            continue
        # Порог качества по ДЛИННОЙ стороне (портрет 1080x1920: width=1080 — норм,
        # длинная сторона 1920 ≥ 1280; старый чек по width резал всю вертикаль)
        _long_side = max(r.width or 0, r.height or 0)
        if _long_side and _long_side < MIN_WIDTH:
            continue                                   # ниже 720p → мыло после апскейла
        if r.width and r.height:
            ar = r.width / r.height
            if abs(ar - target_ar) / target_ar > ASPECT_TOLERANCE:
                continue                               # вертикалки и «киношные полосы»
        if r.media_type == 'video' and clip_duration > 0 and r.duration:
            if r.duration < clip_duration + DURATION_MARGIN:
                continue                               # короче клипа → зацикливание/фриз

        # Порог — ДОЛЯ слов запроса, а не «хотя бы одно». Стоки ищут по любому
        # слову: «Man staring window» дал 500 хитов на Pixabay, среди них волки —
        # у них законный авторский тег `staring`. Одного совпавшего слова хватало,
        # чтобы волк дошёл до вижна и занял место релевантного кандидата
        # (прогон 26.08, клип 16: из трёх кандидатов два — волк и оконная штора).
        # Ровно этот порог уже стоит у локальной библиотеки
        # (`stock_stage._match_local_by_words`) с тем же обоснованием: одно
        # случайное общее слово подсовывает материал не по теме. Пустой ответ
        # лучше мусорного — дальше каскад пойдёт в соседний сток или перепромптит.
        matched = match_count(qwords, r.keywords)      # с учётом словоформ (см. match_count)
        if subject_words:
            # Есть субъект — судим по нему одному: кадр без него бесполезен,
            # кадр с ним годен, даже если обстоятельства не совпали.
            if not _has_subject(subject_words, r.keywords):
                dropped_no_subject += 1
                continue
        elif qwords and (matched / len(qwords)) < MATCH_MIN_SHARE:
            # Субъект неизвестен (промптер лёг) — работает прежний порог доли.
            continue

        # Свой материал уникален (у других каналов его нет) → при равном совпадении вперёд.
        local_first = 1 if r.source == 'local' else 0
        # Футаж, лишь немного длиннее клипа, лучше 60-секундного на 5-секундный слот.
        if r.media_type == 'video' and clip_duration > 0 and r.duration:
            dur_fit = -abs(r.duration - (clip_duration + 2.0))
        else:
            dur_fit = 0.0
        res_fit = -abs((r.width or target_w) - target_w)

        scored.append((matched, local_first, dur_fit, res_fit, r))

    scored.sort(key=lambda x: (x[0], x[1], x[2], x[3]), reverse=True)
    if not scored and subject_words and dropped_no_subject:
        # Субъекта нет ни у кого: либо сток называет его иначе (у Pexels теги
        # пусты, релевантность собирается из slug), либо в индексе его правда
        # нет. Пустой лист гарантированно отправит сцену в нейрогенерацию,
        # поэтому пробуем прежним порогом доли слов — но говорим об этом вслух.
        logger.info("отбор: субъект «%s» не найден ни у одного из %d кандидатов "
                    "— смягчаю до доли слов", subject, len(results))
        return rank_stock(results, query, clip_duration=clip_duration,
                          target_w=target_w, target_h=target_h,
                          used_urls=used_urls, exclude_ai=exclude_ai, subject='')
    if dropped_no_subject:
        logger.info("отбор: %d кандидат(ов) без субъекта «%s» отброшено, осталось %d",
                    dropped_no_subject, subject, len(scored))
    return [x[4] for x in scored]


def search_envato(query: str, media_type: str = 'video', limit: int = 20,
                  target_w: int = 1920, target_h: int = 1080) -> List[StockResult]:
    from units.envato_source import search_envato as _search
    return _search(query, media_type, limit, target_w, target_h)


def search_filesta_envato(query: str, media_type: str = 'video', limit: int = 20,
                          target_w: int = 1920, target_h: int = 1080,
                          min_len: float = 0.0) -> List[StockResult]:
    """Envato Elements: поиск по публичной выдаче, файл — платной загрузкой
    через Filesta (см. units/filesta_source.py)."""
    from units.filesta_source import search_filesta_envato as _search
    return _search(query, media_type, limit, target_w, target_h, min_len)


SEARCH_FUNCS = {
    'local': search_local,
    'pexels': search_pexels,
    'pixabay': search_pixabay,
    # Старый браузерный Envato по личным кукам заморожен (28.08), но оставлен:
    # он у кого-то настроен и служит запасным путём.
    'envato': search_envato,
    'filesta_envato': search_filesta_envato,
}


# Источники, умеющие ОТБИРАТЬ по длительности на своей стороне. Остальным
# `min_len` не передаём вовсе: у них общая сигнатура из пяти аргументов, и
# лишний параметр их сломает.
LENGTH_FILTER_SOURCES = {'filesta_envato'}


def search_source(source: str, query: str, media_type: str = 'video',
                  limit: int = 20, target_w: int = 1920, target_h: int = 1080,
                  min_len: float = 0.0) -> List[StockResult]:
    fn = SEARCH_FUNCS.get(source)
    if not fn:
        return []
    extra = {}
    if min_len and media_type == 'video' and source in LENGTH_FILTER_SOURCES:
        extra['min_len'] = float(min_len)
    try:
        return fn(query, media_type, limit, target_w, target_h, **extra)
    except Exception as exc:
        logger.warning("%s: поиск упал: %s: %s", source, type(exc).__name__, exc)
        return []
