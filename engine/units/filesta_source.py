"""
filesta_envato.py — сток Envato Elements: ПОИСК по публичной выдаче,
ПЛАТНАЯ загрузка мастер-файла через панель Filesta.

Почему два разных канала (метод разведан 28.08, см. docs/ENVATO_FILESTA_METHOD.md):

  • выдача Envato публична — обычный GET отдаёт готовый HTML со всеми карточками,
    браузер и логин для поиска НЕ нужны;
  • сам файл отдаёт только Filesta, где куплена лицензия (300 загрузок в сутки).
    Её защита (proof-of-work, Cloudflare Turnstile, подпись запроса от отпечатка
    устройства) проходится ТОЛЬКО из настоящего Chrome, поэтому загрузка идёт
    через CDP-подключение к пользовательскому браузеру.

Отбор целиком бесплатный: вижн смотрит превью (постер карточки или превью-mp4),
и лишь ПОБЕДИТЕЛЬ стоит одной загрузки из суточной квоты.

Разметка карточки — проверена на сохранённых страницах 31.08, у видео и фото она
РАЗНАЯ (см. `parse_cards`).
"""
from __future__ import annotations

import base64 as _b64
import hashlib
import hmac
import html as _html
import json
import logging
import os
import queue
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

import params as config
from units.stock_lookup import StockResult, _words

logger = logging.getLogger(__name__)

SOURCE = 'filesta_envato'
_BASE = 'https://elements.envato.com'

# Ниже этой границы фильтр длины не ставим: роликов короче в выдаче почти нет
# (замер 01.09 на четырёх запросах: `max-length-4` дал 0-1 карточку), а лишний
# сегмент только сужает выдачу без всякой пользы.
_MIN_LENGTH_FLOOR = 2

# Ссылка на ассет: /<slug строчными>-<КОД>, КОД — ровно 7 символов [A-Z0-9] в
# ВЕРХНЕМ регистре. Цифра в коде НЕ обязательна (проверено: 11 карточек из 70
# без единой цифры — REJYWRH, BVRZLYU, RXKAVSM). Регистр отсекает мусор лучше:
# /sound-effects не проходит, потому что effects строчными.
_ASSET_PATH_RE = re.compile(r'^/[a-z0-9][a-z0-9-]*-([A-Z0-9]{7})/?$')
# Якорь карточки. Он один и тот же на обеих страницах: 70/70 у видео, 60/60 у
# фото. Искать карточки по классам нельзя — они генерируются сборкой.
_ITEM_LINK_RE = re.compile(r'<a\b[^>]*data-testid="item-link"[^>]*>')
_ATTR_RE = {
    'href': re.compile(r'\bhref="([^"]*)"'),
    'title': re.compile(r'\btitle="([^"]*)"'),
    'aria': re.compile(r'\baria-label="([^"]*)"'),
}
_VIDEO_TAG_RE = re.compile(r'<video\b[^>]*>')
_IMG_TAG_RE = re.compile(r'<img\b[^>]*>')
_IAR_RE = re.compile(r'--iar:\s*([0-9.]+)')
_SRCSET_ITEM_RE = re.compile(r'(https?://[^\s,"]+)\s+(\d+)w')

# Какой ширины постер брать из srcSet. В нём лежат УЖЕ ПОДПИСАННЫЕ адреса на
# 200-800 px, поэтому крупный кадр достаётся бесплатно и без подделки подписи
# (`w` входит в подпись `s=`, вручную её не сменить — отсюда прежний потолок 500
# в заметке разведки). Панель контактного листа вижна — до 900 px, так что 800
# закрывает её целиком.
_POSTER_WIDTH = 800

_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
       '(KHTML, like Gecko) Chrome/122.0 Safari/537.36')


# ── настройки ───────────────────────────────────────────────────────────────

def _home() -> Path:
    """Папка провайдера: отпечаток и профиль Chrome лежат рядом с агентом."""
    raw = str(getattr(config, 'FILESTA_HOME', '') or '').strip()
    if raw:
        return Path(raw).expanduser()
    return Path(__file__).resolve().parents[1] / '.filesta'


def fingerprint_path() -> Path:
    raw = str(getattr(config, 'FILESTA_FINGERPRINT', '') or '').strip()
    return Path(raw).expanduser() if raw else _home() / 'fp_real.json'


def profile_path() -> Path:
    raw = str(getattr(config, 'FILESTA_PROFILE', '') or '').strip()
    return Path(raw).expanduser() if raw else _home() / 'chrome_profile'


def _cdp_url() -> str:
    return str(getattr(config, 'FILESTA_CDP_URL', '') or 'http://127.0.0.1:9222').strip()


def _max_master_mb() -> int:
    try:
        return max(1, int(getattr(config, 'FILESTA_MAX_MASTER_MB', 300) or 300))
    except (TypeError, ValueError):
        return 300


def configured() -> bool:
    """Источник настроен? Без отпечатка загрузка невозможна в принципе."""
    return fingerprint_path().is_file()


# ── поиск: адрес выдачи ─────────────────────────────────────────────────────

def search_url(query: str, media_type: str = 'video',
               target_w: int = 1920, target_h: int = 1080,
               min_len: float = 0.0) -> str:
    """Адрес публичной выдачи Envato.

    Фильтры задаются ПУТЁМ, не параметрами: `?orientation=landscape` сервер сам
    перекидывает на путь, а `?orientation=horizontal` игнорирует — полагаться на
    параметры нельзя.

    Слова ориентации у фото и видео РАЗНЫЕ: landscape/portrait/square против
    horizontal/vertical/square. Разрешение видео — только 720p и 1080p: мастер
    приходит в исходном качестве, и у 4K-ассета он бывает на гигабайты
    (замер: 4,3 ГБ против 86 МБ у 1080p).

    `min_len` — нижняя граница длительности в секундах, сегментом `min-length-N`
    (разведка 01.09). В UI слайдер ходит шагом 5, но путь принимает ЛЮБОЕ целое:
    `min-length-8` и `min-length-12` отработали. Секунды округляем ВНИЗ — выдача
    от этого шире, а недобор в полсекунды всё равно съедает замедление.
    Верхнюю границу не ставим никогда: ролик длиннее сцены просто режется.
    """
    slug = urllib.parse.quote(' '.join(str(query or '').split()), safe='')
    if target_h > target_w:
        photo_orient, video_orient = 'portrait', 'vertical'
    elif target_h == target_w:
        photo_orient, video_orient = 'square', 'square'
    else:
        photo_orient, video_orient = 'landscape', 'horizontal'

    if media_type == 'photo':
        # У фото длительности нет — фильтр не ставим даже если попросили.
        return f'{_BASE}/photos/{slug}/orientation-{photo_orient}'
    # `stock-footage` отсекает моушн-графику и оставляет живые съёмки.
    url = (f'{_BASE}/stock-video/stock-footage/{slug}'
           f'/orientation-{video_orient}'
           f'/resolution-720p-(hd)+1080p-(full-hd)')
    seconds = int(min_len or 0)
    if seconds >= _MIN_LENGTH_FLOOR:
        url += f'/min-length-{seconds}'
    return url


# ── поиск: разбор карточек ──────────────────────────────────────────────────

def _unescape(url: str) -> str:
    """`&amp;` → `&`. Без этого подпись `s=` не сходится и CDN отвечает 403 —
    это не защита, а наша же ошибка разбора."""
    return _html.unescape(str(url or '')).strip()


def _pick_srcset(tag: str, want_width: int = _POSTER_WIDTH) -> str:
    """Самый подходящий адрес из srcSet: ближайший снизу к нужной ширине.

    В srcSet лежат подписанные варианты 200..800 px — берём готовый, а не
    правим `w` в адресе (ширина входит в подпись, подмена даёт 403).
    """
    srcset = re.search(r'\bsrcSet="([^"]*)"', tag, re.I)
    if not srcset:
        return ''
    best_url, best_width = '', -1
    for url, width in _SRCSET_ITEM_RE.findall(_unescape(srcset.group(1))):
        width = int(width)
        if width <= want_width and width > best_width:
            best_url, best_width = url, width
    if best_url:
        return best_url
    # Все варианты крупнее нужного — берём самый мелкий из них.
    items = [(int(w), u) for u, w in _SRCSET_ITEM_RE.findall(_unescape(srcset.group(1)))]
    return min(items)[1] if items else ''


def _attr(tag: str, name: str) -> str:
    match = re.search(rf'\b{name}="([^"]*)"', tag, re.I)
    return _unescape(match.group(1)) if match else ''


def parse_cards(html_text: str, media_type: str = 'video') -> List[Dict[str, Any]]:
    """Карточки выдачи → список словарей. Чистая функция: сети здесь нет,
    поэтому её гоняет офлайн-тест на сохранённых страницах.

    Разметка у видео и фото РАЗНАЯ (проверено на снимках 31.08):

      видео: <video src=ПРЕВЬЮ.mp4 poster=ПОСТЕР> … <img alt> … затем
             <a data-testid="item-link" aria-label="ЗАГОЛОВОК" href="/slug-КОД">
             — медиа стоит ПЕРЕД ссылкой, заголовок в `aria-label`;

      фото:  <a title="ЗАГОЛОВОК" data-testid="item-link" href="/slug-КОД">
                 <img srcSet=… src=…>
             </a>
             — медиа ВНУТРИ ссылки, заголовок в `title`.

    Заголовок берём у самой ссылки, а НЕ из `img[alt]`: первые `alt` на странице
    принадлежат шапке сайта («Envato», «PremiumBeat Content Feature»), и разбор
    по ним даёт мусор вместо названия ассета.
    """
    cards: List[Dict[str, Any]] = []
    seen: set = set()
    for match in _ITEM_LINK_RE.finditer(html_text):
        tag = match.group(0)
        href = _attr(tag, 'href')
        path = urllib.parse.urlparse(href).path or href
        code_match = _ASSET_PATH_RE.match(path)
        if not code_match:
            continue
        code = code_match.group(1)
        if code in seen:
            continue
        seen.add(code)

        title = _attr(tag, 'aria-label') or _attr(tag, 'title')
        # У фото заголовок иногда идёт с приставкой «Preview: ».
        title = re.sub(r'^preview:\s*', '', title, flags=re.I).strip()

        preview_url = poster_url = ''
        aspect = 0.0
        if media_type == 'video':
            # ПОСЛЕДНИЙ <video> перед ссылкой — он и принадлежит этой карточке.
            # «Первый <video> после ссылки» даёт превью СОСЕДНЕГО ролика: вижн
            # смотрел бы одно, а покупали бы другое (грабли разведки 28.08).
            head = html_text[:match.start()]
            video_tags = _VIDEO_TAG_RE.findall(head[-6000:])
            if video_tags:
                video_tag = video_tags[-1]
                preview_url = _unescape(_attr(video_tag, 'src'))
                poster_url = _unescape(_attr(video_tag, 'poster'))
            img_tags = _IMG_TAG_RE.findall(head[-6000:])
            if img_tags and not poster_url:
                poster_url = _pick_srcset(img_tags[-1]) or _attr(img_tags[-1], 'src')
            elif img_tags:
                # Из srcSet берём кадр покрупнее постера плеера (тот всегда w=500).
                bigger = _pick_srcset(img_tags[-1])
                if bigger:
                    poster_url = bigger
            iar = _IAR_RE.findall(head[-6000:])
            if iar:
                try:
                    aspect = float(iar[-1])
                except ValueError:
                    aspect = 0.0
        else:
            # У фото картинка лежит ВНУТРИ ссылки — смотрим вперёд, до её конца.
            tail = html_text[match.end():match.end() + 8000]
            close = tail.find('</a>')
            if close > 0:
                tail = tail[:close]
            img_match = _IMG_TAG_RE.search(tail)
            if img_match:
                img_tag = img_match.group(0)
                poster_url = _pick_srcset(img_tag) or _unescape(_attr(img_tag, 'src'))

        cards.append({
            'code': code,
            'page_url': _BASE + path.rstrip('/'),
            'title': title,
            'preview_url': preview_url,
            'poster_url': poster_url,
            'aspect': aspect,
        })
    return cards


def _http_get(url: str, timeout: int = 30) -> str:
    proxy = (getattr(config, 'STOCK_PROXY_URL', '') or '').strip()
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({'http': proxy, 'https': proxy}) if proxy
        else urllib.request.ProxyHandler({})
    )
    request = urllib.request.Request(url, headers={
        'User-Agent': _UA,
        'Accept': 'text/html,application/xhtml+xml',
        'Accept-Language': 'en-US,en;q=0.9',
    })
    with opener.open(request, timeout=timeout) as response:
        return response.read().decode('utf-8', 'replace')


def search_filesta_envato(query: str, media_type: str = 'video', limit: int = 20,
                          target_w: int = 1920, target_h: int = 1080,
                          min_len: float = 0.0) -> List[StockResult]:
    """Та же сигнатура, что у остальных источников стока, плюс `min_len`.

    `limit` — сколько карточек взять СО СТРАНИЦЫ: выдача отдаёт 60-70 разом,
    пагинацию не трогаем, этого хватает с запасом.

    `min_len` — нижняя граница длительности (см. `search_url`). Стоит бесплатно:
    замер 01.09 показал, что страница под фильтром остаётся ПОЛНОЙ — Envato
    добирает её из глубины выдачи, топ-10 не меняется, а уходят как раз короткие
    (3-5 карточек из ~65), каждая из которых стоила бы второй платной загрузки.
    """
    if not str(query or '').strip():
        return []
    url = search_url(query, media_type, target_w, target_h, min_len)
    try:
        html_text = _http_get(url)
    except Exception as exc:                                # noqa: BLE001
        logger.warning("%s: выдача не открылась (%s): %s", SOURCE, type(exc).__name__, exc)
        return []

    cards = parse_cards(html_text, media_type)
    if not cards:
        logger.info("%s: по «%s» карточек не нашлось", SOURCE, str(query)[:50])
        return []

    results: List[StockResult] = []
    for card in cards[:max(1, int(limit))]:
        title = card['title'] or card['code']
        results.append(StockResult(
            source=SOURCE,
            media_type=media_type,
            # `url` — ключ дедупа `used_urls` и адрес для платной загрузки.
            # Конечного файла здесь НЕТ: он появится только после покупки, о чём
            # и говорит `preview_only`.
            url=card['page_url'],
            page_url=card['page_url'],
            title=title,
            keywords=_words(title),
            license_name='Envato Elements subscription',
            preview_url=card['preview_url'],
            poster_url=card['poster_url'],
            preview_only=True,
            asset_id=card['code'],
        ))
    logger.info("%s: «%s» → %d карточек (%s)", SOURCE, str(query)[:40],
                len(results), media_type)
    return results


# ── платная загрузка: один браузер, очередь, пачки ──────────────────────────

# Подпись запроса и решение time-lock живут В СТРАНИЦЕ: нужен доступ к
# localStorage (sessionId, accessToken, csrf) и crypto.subtle. Скрипт — перенос
# отлаженного docs/envato_filesta_download.py, с одной правкой: он принимает
# СПИСОК ссылок и отдаёт список ответов, чтобы пачка уходила одним заходом.
_REQUEST_JS = """
async (args) => {
  const {items, fp} = args;
  const clean = (v) => (v || '').replace(/^"|"$/g, '');
  const hex = (b) => Array.from(new Uint8Array(b))
      .map(x => x.toString(16).padStart(2, '0')).join('');
  const sha256 = async (s) => hex(await crypto.subtle.digest(
      'SHA-256', new TextEncoder().encode(s)));
  const hmac = async (msg, key) => {
    const k = await crypto.subtle.importKey('raw', new TextEncoder().encode(key),
        {name: 'HMAC', hash: 'SHA-256'}, false, ['sign']);
    return hex(await crypto.subtle.sign('HMAC', k, new TextEncoder().encode(msg)));
  };
  const nonce = () => {
    const a = new Uint8Array(16); crypto.getRandomValues(a);
    return Array.from(a).map(b => b.toString(16).padStart(2, '0')).join('');
  };
  // time-lock: k возведений в квадрат по модулю, ускорить нельзя (в этом смысл)
  const solve = (x, k, n) => {
    const a = BigInt('0x' + n); let v = BigInt('0x' + x) % a;
    for (let i = 0; i < k; i++) v = (v * v) % a;
    return v.toString(16);
  };

  const sessionId = clean(localStorage.getItem('sessionId'));
  const secret = (await sha256('filesta:download:' + sessionId)).substring(0, 32);
  const parts = [`sr:${fp.screenResolution}`, `cd:${fp.colorDepth}`,
                 `tz:${fp.timezone}`, `lang:${fp.language}`, `plat:${fp.platform}`];
  if (fp.canvas) parts.push(`cv:${fp.canvas}`);
  if (fp.cpuCores) parts.push(`cpu:${fp.cpuCores}`);
  if (fp.deviceMemory) parts.push(`mem:${fp.deviceMemory}`);
  if (fp.touchSupport !== undefined) parts.push(`touch:${fp.touchSupport}`);
  if (fp.webgl) parts.push(`wgl:${fp.webgl}`);
  if (fp.audio) parts.push(`aud:${fp.audio}`);
  if (fp.fonts) parts.push(`fonts:${fp.fonts}`);
  const fpHash = await sha256(parts.sort().join('|'));

  const send = async (itemUrl, extra) => {
    fp.collectedAt = Date.now();       // ОБЯЗАТЕЛЬНО: сервер проверяет свежесть
    const ts = Date.now().toString(), nc = nonce();
    const sig = await hmac(`${ts}:${nc}:${fpHash}:POST:/downloads`,
                           `${secret}:${sessionId}`);
    const csrf = clean(localStorage.getItem('x-csrf-token'));
    const gesture = window.__lastGesture ? Date.now() - window.__lastGesture : 900;
    const len = itemUrl.length;
    const r = await fetch('/api/downloads', {
      method: 'POST', credentials: 'include',
      headers: Object.assign({
        'Accept': 'application/json, text/plain, */*',
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + clean(localStorage.getItem('accessToken')),
        'X-Device-Fingerprint': btoa(JSON.stringify(fp)),
        'X-Request-Signature': sig,
        'X-Request-Timestamp': ts,
        'X-Request-Nonce': nc,
        'X-Session-Id': sessionId,
        'X-UI-Gesture': String(gesture),
        'X-UI-Path': 'm=1;k=mouse;t=-1;d=-1;v=-1',
        'X-UI-Trap': '0',
        'X-UI-Field': `e=${len};u=0;o=0;p=0;d=0;b=1;c=${len};f=5200;s=${gesture};n=2;t=0`,
        'X-CSRF-Token': csrf,
        'X-XSRF-TOKEN': csrf,
        'X-Turnstile-Token': window.__tsToken || '',
      }, extra || {}),
      body: JSON.stringify(Object.assign(
        {platform: 'envato', itemUrl, itemType: 'standard'},
        // Токен кладём во все три места разом: панель ставит его сама, и какое
        // именно поле читает сервер, из ответа не видно. Лишние он игнорирует —
        // проверено живьём 01.09 (статус 202 на ассете, который до этого
        // отвечал 428 TURNSTILE_REQUIRED).
        window.__tsToken ? {turnstileToken: window.__tsToken,
                            cfTurnstileResponse: window.__tsToken} : {})),
    });
    return {status: r.status, body: (await r.text()).slice(0, 4000)};
  };

  const out = [];
  for (const itemUrl of items) {
    let res = await send(itemUrl);
    for (let i = 0; i < 4; i++) {
      let p = {};
      try { p = JSON.parse(res.body); } catch (e) { break; }
      const fresh = p?.error?.newToken || p?.newToken;
      if (fresh) {                                  // CSRF ротируется
        localStorage.setItem('x-csrf-token', fresh);
        res = await send(itemUrl);
        continue;
      }
      if (p?.code === 'TIMELOCK_REQUIRED' && p.puzzle) {
        const answer = solve(p.puzzle.x, p.puzzle.k, p.puzzle.n);
        res = await send(itemUrl, {'X-Timelock-Id': p.puzzle.id,
                                   'X-Timelock-Answer': answer});
        continue;
      }
      // Cloudflare Turnstile. Панель включает его после серии загрузок (замер
      // 01.09: 8 покупок за 3 мин 22 с — и стена 428). Виджет НЕ требует
      // человека: скрипт challenges.cloudflare уже загружен самой панелью, а
      // sitekey сервер присылает в этом же ответе. Рисуем виджет невидимо,
      // ждём токен и повторяем запрос — проверено живьём, 428 → 202.
      if (p?.requireTurnstile || p?.code === 'TURNSTILE_REQUIRED') {
        const key = p.turnstileSiteKey || window.__tsSiteKey;
        if (!key || typeof window.turnstile === 'undefined') break;
        window.__tsSiteKey = key;
        const token = await new Promise((done) => {
          let box = document.getElementById('__ts_box');
          if (!box) {
            box = document.createElement('div');
            box.id = '__ts_box';
            // Виджет невидимый, но НЕ display:none: скрытый так Cloudflare
            // считает невалидным и токен не отдаёт.
            box.style.cssText = 'position:fixed;bottom:0;right:0;opacity:0.01;'
                              + 'width:300px;height:65px;z-index:0';
            document.body.appendChild(box);
          }
          box.innerHTML = '';
          const stop = setTimeout(() => done(''), 25000);
          try {
            window.turnstile.render('#__ts_box', {
              sitekey: key,
              callback: (t) => { clearTimeout(stop); done(t); },
              'error-callback': () => { clearTimeout(stop); done(''); },
              'timeout-callback': () => { clearTimeout(stop); done(''); },
            });
          } catch (e) { clearTimeout(stop); done(''); }
        });
        if (!token) break;
        window.__tsToken = token;     // держим для следующих загрузок пачки
        res = await send(itemUrl);
        continue;
      }
      break;
    }
    out.push({itemUrl, status: res.status, body: res.body});
  }
  return out;
}
"""

_GESTURE_JS = """
() => {
  window.__lastGesture = window.__lastGesture || 0;
  if (window.__gestureHooked) return;
  window.__gestureHooked = true;
  for (const e of ['pointerdown', 'mousedown', 'keydown'])
    document.addEventListener(e, ev => {
      if (ev.isTrusted) window.__lastGesture = Date.now();
    }, {capture: true, passive: true});
}
"""

# Рендер виджета Turnstile и опрос его состояния. Кликать по чекбоксу отсюда
# НЕЛЬЗЯ: виджет живёт в cross-origin iframe, JS страницы внутрь не достаёт —
# managed-вариант дожимается настоящей мышью через CDP (см. `_solve_turnstile`).
_TS_RENDER_JS = """
(sitekey) => {
  window.__ts = {token: null, state: 'start', box: null};
  try {
    let box = document.getElementById('__ts_box');
    if (!box) {
      box = document.createElement('div');
      box.id = '__ts_box';
      // Невидимость через opacity, а НЕ display:none: скрытый вторым способом
      // виджет Cloudflare считает невалидным и токена не отдаёт (проверено).
      box.style.cssText = 'position:fixed;bottom:0;right:0;opacity:0.01;'
                        + 'width:300px;height:65px;z-index:0';
      document.body.appendChild(box);
    }
    box.innerHTML = '';
    window.turnstile.render('#__ts_box', {
      sitekey: sitekey,
      callback: (t) => { window.__ts.token = t; window.__ts.state = 'solved'; },
      'error-callback': (e) => { window.__ts.state = 'error'; },
      'timeout-callback': () => { window.__ts.state = 'timeout'; },
    });
    window.__ts.state = 'rendered';
  } catch (e) { window.__ts.state = 'throw'; }
  return window.__ts.state;
}
"""

# Состояние виджета: токен уже есть или перед нами ИНТЕРАКТИВНЫЙ чекбокс.
_TS_STATE_JS = """
() => {
  const st = window.__ts || {};
  const box = document.getElementById('__ts_box');
  const frame = box ? box.querySelector('iframe') : null;
  const rect = frame ? frame.getBoundingClientRect() : null;
  return {
    token: st.token || null,
    state: st.state || 'none',
    // Managed-виджет — это видимый блок с чекбоксом (примерно 300x65).
    // У неинтерактивного высота нулевая, кликать там нечего.
    interactive: !!(rect && rect.width > 40 && rect.height > 20),
    x: rect ? rect.x + 30 : 0,
    y: rect ? rect.y + rect.height / 2 : 0,
  };
}
"""

# Показать виджет человеку: крупно, по центру, с подписью. До этого момента он
# был почти прозрачным (`opacity:0.01`) — сам себя пользователь там не нашёл бы.
_TS_SHOW_JS = """
() => {
  const box = document.getElementById('__ts_box');
  if (!box) return false;
  box.style.cssText = 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);'
                    + 'opacity:1;width:320px;height:90px;z-index:2147483647;'
                    + 'background:#fff;padding:14px;border:3px solid #e74c3c;'
                    + 'border-radius:8px;box-shadow:0 0 0 9999px rgba(0,0,0,.55)';
  if (!document.getElementById('__ts_note')) {
    const note = document.createElement('div');
    note.id = '__ts_note';
    note.textContent = 'BegoFrame: нажмите проверку — загрузка продолжится сама';
    note.style.cssText = 'position:fixed;top:calc(50% - 80px);left:50%;'
                       + 'transform:translateX(-50%);z-index:2147483647;color:#fff;'
                       + 'font:600 15px system-ui;text-align:center';
    document.body.appendChild(note);
  }
  return true;
}
"""

# Панель требует `x-session-id` (замер 01.09): без него /api/users/licenses
# отвечает 400 «x-session-id header is required», квота не приходит, и источник
# запирался с текстом «похоже, сессия закончилась» — при живом входе и снятом
# отпечатке. Идентификатор лежит в localStorage рядом с токеном.
_LICENSES_JS = """
async () => {
  const clean = (v) => (v || '').replace(/^"|"$/g, '');
  const headers = {
    'Authorization': 'Bearer ' + clean(localStorage.getItem('accessToken')),
    'Accept': 'application/json',
  };
  const sid = clean(localStorage.getItem('sessionId'));
  if (sid) headers['x-session-id'] = sid;
  const r = await fetch('/api/users/licenses', {
    credentials: 'include',
    headers: headers,
  });
  return {status: r.status, body: (await r.text()).slice(0, 8000)};
}
"""

_XHR_SPY_JS = """
() => {
  if (window.__fpSpy !== undefined) return;
  window.__fpSpy = null;
  const open = XMLHttpRequest.prototype.open;
  const set = XMLHttpRequest.prototype.setRequestHeader;
  XMLHttpRequest.prototype.open = function (m, u, ...rest) {
    this.__u = u; return open.call(this, m, u, ...rest);
  };
  XMLHttpRequest.prototype.setRequestHeader = function (k, v) {
    if (this.__u && this.__u.includes('/downloads') &&
        k.toLowerCase() === 'x-device-fingerprint') window.__fpSpy = v;
    return set.call(this, k, v);
  };
}
"""


class _Job:
    """Одно задание загрузчику: ссылка → файл. Клип ждёт свой `done`."""

    __slots__ = ('kind', 'asset_url', 'destination', 'done', 'ok', 'error', 'payload')

    def __init__(self, kind: str, asset_url: str = '', destination: Optional[Path] = None):
        self.kind = kind                     # 'download' | 'quota'
        self.asset_url = asset_url
        self.destination = destination
        self.done = threading.Event()
        self.ok = False
        self.error = ''
        self.payload: Any = None


class _Downloader:
    """Один Chrome, одна вкладка, ОДИН рабочий поток — и очередь заданий.

    Почему так: Playwright sync-API привязан к своему потоку, а Chrome по CDP у
    нас один. При этом стадия идёт в 12 потоков — значит нужен не замок вокруг
    чужого вызова, а исполнитель-потребитель.

    Ожидания «пока наберётся пачка» здесь НЕТ: первое задание берётся
    блокирующе и уходит в работу сразу. Пачка складывается сама — из того, что
    успело накопиться, пока качалось предыдущее (`get_nowait` до опустошения).
    """

    _MAX_BATCH = 8

    def __init__(self) -> None:
        self._queue: "queue.Queue[_Job]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._playwright = None
        self._browser = None
        self._page = None
        self._fingerprint: Optional[dict] = None
        self._fatal = ''                     # источник выключен на прогон

    # — публичное —

    def submit(self, job: _Job, timeout: float = 900.0) -> _Job:
        if self._fatal:
            job.error = self._fatal
            job.done.set()
            return job
        self._ensure_thread()
        self._queue.put(job)
        if not job.done.wait(timeout=timeout):
            job.error = f'загрузчик не ответил за {int(timeout)}с'
        return job

    def fatal(self) -> str:
        return self._fatal

    # — поток-исполнитель —

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name='filesta-downloader')
            self._thread.start()

    def _loop(self) -> None:
        while True:
            job = self._queue.get()
            batch = [job]
            # Всё, что уже лежит в очереди, забираем этим же заходом.
            while len(batch) < self._MAX_BATCH:
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            try:
                self._handle_batch(batch)
            except Exception as exc:                        # noqa: BLE001
                for item in batch:
                    if not item.done.is_set():
                        item.error = f'{type(exc).__name__}: {exc}'
                        item.done.set()
                logger.warning("%s: загрузчик упал на пачке из %d: %s",
                               SOURCE, len(batch), exc)
            finally:
                for item in batch:
                    item.done.set()

    def _handle_batch(self, batch: List[_Job]) -> None:
        quota_jobs = [item for item in batch if item.kind == 'quota']
        download_jobs = [item for item in batch if item.kind == 'download']
        page = self._page_or_start()

        for item in quota_jobs:
            try:
                item.payload = self._read_quota(page)
                item.ok = item.payload is not None
            except Exception as exc:                        # noqa: BLE001
                item.error = str(exc)
            item.done.set()

        if not download_jobs:
            return

        if len(download_jobs) > 1:
            logger.info("%s: пачка загрузок — %d шт.", SOURCE, len(download_jobs))
        # Доверенный жест: его требует заголовок X-UI-Gesture.
        page.evaluate(_GESTURE_JS)
        page.mouse.move(620, 390)
        page.mouse.click(620, 390)
        page.wait_for_timeout(250)

        answers = page.evaluate(_REQUEST_JS, {
            'items': [item.asset_url for item in download_jobs],
            'fp': self._load_fingerprint(),
        })
        # Turnstile мог включиться посреди пачки. JS-ветка проходит его сама,
        # но только в невидимом варианте; managed-виджет требует НАСТОЯЩЕГО
        # клика мышью, а из страницы кликнуть нельзя — он в чужом iframe.
        # Здесь дожимаем такие случаи и повторяем неудавшиеся ссылки.
        stuck = [answer for answer in (answers or [])
                 if 'TURNSTILE' in str(answer.get('body') or '')]
        if stuck:
            site_key = _site_key_from(stuck[0].get('body'))
            if self._solve_turnstile(page, site_key):
                retry = [str(a.get('itemUrl') or '') for a in stuck]
                logger.info("%s: капча пройдена — повторяю %d загрузку(и)",
                            SOURCE, len(retry))
                again = page.evaluate(_REQUEST_JS, {
                    'items': retry, 'fp': self._load_fingerprint()})
                fixed = {str(a.get('itemUrl') or ''): a for a in (again or [])}
                answers = [fixed.get(str(a.get('itemUrl') or ''), a)
                           for a in (answers or [])]
        by_url = {str(answer.get('itemUrl') or ''): answer for answer in (answers or [])}

        for item in download_jobs:
            answer = by_url.get(item.asset_url) or {}
            status = int(answer.get('status') or 0)
            body = str(answer.get('body') or '')
            if status != 202:
                item.error = f'Filesta отказала ({status}): {body[:200]}'
                # Смена схемы подписи — единственная причина выключить источник
                # целиком: дальше все запросы будут падать так же.
                if 'SECURITY_CHECK_FAILED' in body:
                    self._note_security_failure()
                item.done.set()
                continue
            try:
                result = json.loads(body)['data']['result']
            except Exception as exc:                        # noqa: BLE001
                item.error = f'ответ не разобран: {exc}'
                item.done.set()
                continue
            try:
                link = str(result.get('downloadUrl') or '')
                if not link:
                    item.error = ('Filesta ответила 202, но ссылки на файл в ответе нет: '
                                  f'{str(result)[:200]}')
                    item.done.set()
                    continue
                item.ok = self._fetch_file(link, item.destination)
                if not item.ok and not item.error:
                    # Причину назвал сам `_fetch_file` строкой в логе; сюда
                    # кладём хотя бы адрес, чтобы отказ можно было повторить.
                    item.error = f'файл не скачался: {link[:120]}'
            except Exception as exc:                        # noqa: BLE001
                item.error = f'{type(exc).__name__}: {exc}'
            item.done.set()

    # — браузер —

    def _page_or_start(self):
        if self._page is not None:
            try:
                if not self._page.is_closed():
                    return self._page
            except Exception:                               # noqa: BLE001
                pass
        from playwright.sync_api import sync_playwright

        if self._playwright is None:
            self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.connect_over_cdp(_cdp_url())
        except Exception:                                   # noqa: BLE001
            _launch_chrome()
            self._browser = self._playwright.chromium.connect_over_cdp(_cdp_url())
        context = self._browser.contexts[0] if self._browser.contexts else self._browser.new_context()
        page = next((p for p in context.pages if 'filesta.com' in (p.url or '')), None)
        if page is None:
            page = context.new_page()
            page.goto('https://filesta.com/services/envato/content',
                      wait_until='domcontentloaded', timeout=60_000)
            page.wait_for_timeout(6000)
        self._page = page
        return page

    def _load_fingerprint(self) -> dict:
        if self._fingerprint is None:
            path = fingerprint_path()
            if not path.is_file():
                raise RuntimeError(
                    f'отпечаток не снят: {path}. Settings → Filesta → «Снять отпечаток»')
            self._fingerprint = json.loads(path.read_text(encoding='utf-8'))
        return dict(self._fingerprint)

    def _note_security_failure(self) -> None:
        """Два SECURITY_CHECK_FAILED подряд = схема подписи сменилась."""
        count = getattr(self, '_security_fails', 0) + 1
        setattr(self, '_security_fails', count)
        if count >= 2:
            self._fatal = ('Filesta сменила схему подписи (SECURITY_CHECK_FAILED). '
                           'Источник выключен на прогон; снимите отпечаток заново.')
            logger.error("[stock] ⛔ %s", self._fatal)

    def _solve_turnstile(self, page, site_key: str) -> bool:
        """Пройти Cloudflare Turnstile. Три ступени, каждая — по факту, не вслепую.

        1. Невидимый рендер. Скрипт Cloudflare уже загружен самой панелью, ключ
           сервер присылает в отказе 428. В большинстве случаев токен приходит
           за пару секунд без единого клика: браузер настоящий, профиль с
           историей (проверено живьём 01.09 — `solved`, 428 → 202).
        2. Managed-виджет. Если вместо токена отрисовался ВИДИМЫЙ чекбокс,
           JS помочь не может: виджет в cross-origin iframe. Кликаем настоящей
           мышью через CDP — тот же браузер, тот же адрес, и такой клик
           Cloudflare принимает.
        3. Сервис-решатель. Последний рубеж и НЕобязательный: ключа нет —
           просто пропускаем ступень со строкой в лог, ничего не роняя.
        """
        if not site_key:
            logger.warning("%s: капча требуется, но сервер не прислал ключ виджета", SOURCE)
            return False
        try:
            page.evaluate(_TS_RENDER_JS, site_key)
        except Exception as exc:                            # noqa: BLE001
            logger.warning("%s: виджет капчи не отрисовался (%s)", SOURCE, exc)
            return False

        state = {}
        for _ in range(6):                                  # ~3 c на тихий вариант
            page.wait_for_timeout(500)
            state = page.evaluate(_TS_STATE_JS) or {}
            if state.get('token'):
                logger.info("%s: капча пройдена сама (невидимый вариант)", SOURCE)
                return self._keep_token(page, state['token'])
        if state.get('interactive'):
            logger.info("%s: капча требует нажатия — кликаю чекбокс", SOURCE)
            try:
                page.mouse.click(float(state.get('x') or 0), float(state.get('y') or 0))
            except Exception as exc:                        # noqa: BLE001
                logger.warning("%s: клик по чекбоксу не удался (%s)", SOURCE, exc)
            for _ in range(20):                             # ~10 c на проверку
                page.wait_for_timeout(500)
                state = page.evaluate(_TS_STATE_JS) or {}
                if state.get('token'):
                    logger.info("%s: капча пройдена нажатием", SOURCE)
                    return self._keep_token(page, state['token'])

        token = _solve_via_service(site_key, page.url)
        if token:
            return self._keep_token(page, token)

        # Ступень 4: зовём человека. Виджет до сих пор был почти прозрачным —
        # показываем его крупно и по центру, чтобы было видно, куда жать, и
        # ЖДЁМ. Прогон на это время стоит: закрыть сцены без загрузок всё равно
        # нечем, а бросить пачку — значит потерять кандидатов зря.
        try:
            page.evaluate(_TS_SHOW_JS)
            page.bring_to_front()
        except Exception:                                   # noqa: BLE001
            pass
        wait_sec = _human_captcha_wait()
        logger.warning("%s: ⚠️  КАПЧА ЖДЁТ ВАС. Откройте окно Chrome с панелью "
                       "Filesta и нажмите чекбокс в рамке справа снизу — прогон "
                       "продолжится сам. Жду до %d мин.", SOURCE, max(1, wait_sec // 60))
        for _ in range(max(1, wait_sec * 2)):
            page.wait_for_timeout(500)
            state = page.evaluate(_TS_STATE_JS) or {}
            if state.get('token'):
                logger.info("%s: капча пройдена вручную — продолжаю", SOURCE)
                return self._keep_token(page, state['token'])
        logger.warning("%s: капчу так и не прошли — загрузки этой пачки отложены", SOURCE)
        return False

    @staticmethod
    def _keep_token(page, token: str) -> bool:
        """Токен живёт минуты — держим его в странице для следующих загрузок."""
        page.evaluate("(t) => { window.__tsToken = t; }", token)
        return True

    def _read_quota(self, page) -> Optional[int]:
        """Остаток суточных загрузок: сумма по валидным лицензиям Envato.

        Ключ лицензии (`key`) — секрет, наружу он не идёт: берём только числа.

        Причина отказа запоминается в `last_quota_error`: пустой ответ раньше
        объявлялся протухшей сессией, и человек переснимал отпечаток и заново
        входил в панель, хотя панель просто требовала новый заголовок
        (01.09 — HTTP 400 «x-session-id header is required»).
        """
        answer = page.evaluate(_LICENSES_JS)
        status = int(answer.get('status') or 0)
        if status != 200:
            body = str(answer.get('body') or '').strip()
            self.last_quota_error = f'панель ответила {status}: {body[:160]}'
            logger.warning('[filesta] лицензии: %s', self.last_quota_error)
            return None
        payload = json.loads(answer.get('body') or '{}')
        items = payload.get('data') if isinstance(payload, dict) else payload
        if isinstance(items, dict):
            items = items.get('licenses') or items.get('items') or []
        total = 0
        found = False
        for license_item in items or []:
            if not isinstance(license_item, dict):
                continue
            if str(license_item.get('service') or '').lower() != 'envato':
                continue
            if license_item.get('isValid') is False:
                continue
            remaining = license_item.get('remainingDailyLimit')
            if remaining is None:
                continue
            try:
                total += int(remaining)
                found = True
            except (TypeError, ValueError):
                continue
        if not found:
            self.last_quota_error = ('в ответе панели нет действующей лицензии '
                                     'Envato с остатком загрузок')
            logger.warning('[filesta] лицензии: %s', self.last_quota_error)
            return None
        self.last_quota_error = ''
        return total

    def _fetch_file(self, download_url: str, destination: Path) -> bool:
        """Забрать мастер ПОТОКОМ. Читать целиком в память нельзя: замер
        разведки — 4,3 ГБ у 4K-ассета, машина вставала."""
        if not download_url or destination is None:
            return False
        destination.parent.mkdir(parents=True, exist_ok=True)
        limit = _max_master_mb() * 1024 * 1024
        request = urllib.request.Request(download_url, headers={'User-Agent': _UA})
        temporary = destination.with_suffix(destination.suffix + '.part')
        with urllib.request.urlopen(request, timeout=300) as response:
            declared = int(response.headers.get('Content-Length') or 0)
            if declared and declared > limit:
                # Квота УЖЕ списана (она уходит в момент запроса), поэтому такой
                # случай пишем отдельной строкой: если порог начнёт срабатывать
                # часто, это видно сразу.
                logger.warning("[stock] %s: мастер %.0f МБ больше потолка %d МБ — "
                               "не качаю (загрузка уже списана)",
                               SOURCE, declared / 1024 / 1024, _max_master_mb())
                return False
            got = 0
            with temporary.open('wb') as handle:
                while True:
                    chunk = response.read(512 * 1024)
                    if not chunk:
                        break
                    got += len(chunk)
                    if got > limit:
                        handle.close()
                        temporary.unlink(missing_ok=True)
                        logger.warning("[stock] %s: мастер перевалил потолок %d МБ — обрываю",
                                       SOURCE, _max_master_mb())
                        return False
                    handle.write(chunk)
        got_mb = temporary.stat().st_size / 1024 / 1024
        if temporary.stat().st_size < 1024:
            # Пустой ответ CDN. Загрузка при этом СПИСАНА — молчать нельзя.
            logger.warning("[stock] %s: пришло %d байт вместо файла — "
                           "пустой ответ CDN (загрузка уже списана): %s",
                           SOURCE, temporary.stat().st_size, download_url[:120])
            temporary.unlink(missing_ok=True)
            return False
        if declared and got_mb * 1024 * 1024 < declared * 0.9:
            logger.warning("[stock] %s: скачано %.1f из %.1f МБ — обрыв связи "
                           "(загрузка уже списана)", SOURCE, got_mb, declared / 1024 / 1024)
            temporary.unlink(missing_ok=True)
            return False
        logger.info("[stock] %s: мастер получен, %.1f МБ", SOURCE, got_mb)
        destination.unlink(missing_ok=True)
        temporary.replace(destination)
        return _unpack_if_archive(destination)


_downloader = _Downloader()


def _launch_chrome(account=None) -> None:
    """Поднять Chrome с отладочным портом и нашим профилем.

    Профиль — отдельный (`agent/.filesta/chrome_profile`), чтобы не трогать
    рабочий браузер пользователя; вход в Filesta он делает один раз в нём же.

    `account` — аккаунт из `filesta_accounts`: у каждого свой профиль и свой
    порт, потому что в одном браузере нельзя быть залогиненным в два разных
    аккаунта Filesta, а два Chrome на одном порту не живут. Без аргумента —
    прежнее поведение, одиночный профиль из настроек.
    """
    executable = str(getattr(config, 'FILESTA_CHROME_EXE', '') or '').strip()
    if not executable:
        for candidate in (
            r'C:\Program Files\Google\Chrome\Application\chrome.exe',
            r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
            '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
        ):
            if Path(candidate).is_file():
                executable = candidate
                break
    if not executable:
        raise RuntimeError('Chrome не найден — задайте FILESTA_CHROME_EXE')
    if account is not None:
        port = account.cdp_port
        profile = account.profile_dir
        account.ensure_dirs()
    else:
        port = urllib.parse.urlparse(_cdp_url()).port or 9222
        profile = profile_path()
        profile.mkdir(parents=True, exist_ok=True)
    flags = {}
    if os.name == 'nt':
        flags['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    subprocess.Popen([
        executable,
        f'--remote-debugging-port={port}',
        f'--user-data-dir={profile}',
        # Панель отдаёт файл через всплывающее окно, а Chrome их блокирует по
        # умолчанию: загрузка упиралась в блокировку, и человек сам разбирался,
        # почему ничего не скачалось (замечание пользователя 02.09.2026).
        '--disable-popup-blocking',
        'https://filesta.com/services/envato/content',
    ], **flags)
    logger.info("%s: поднял Chrome (порт %s, профиль %s)", SOURCE, port, profile)
    time.sleep(6)


# Снять сессию из живой вкладки. Токены нужны, чтобы дальше ходить за
# подписанной ссылкой БЕЗ браузера: `sessionId` в профиле Chrome не хранится
# (живёт только в открытой вкладке), а куки там зашифрованы.
_SESSION_JS = """
() => {
  const clean = (v) => (v || '').replace(/^"|"$/g, '');
  return {
    sessionId: clean(localStorage.getItem('sessionId')),
    accessToken: clean(localStorage.getItem('accessToken')),
    csrf: clean(localStorage.getItem('x-csrf-token')),
    siteKey: window.__tsSiteKey || '',
    ua: navigator.userAgent,
  };
}
"""


def capture_session(account=None, timeout_sec: int = 60) -> dict:
    """Снять сессию аккаунта из живой вкладки и записать в его файл.

    Браузер нужен ТОЛЬКО здесь и лишь на секунды: дальше загрузка идёт из
    Python (подпись, замок по времени и заголовки собираются кодом — проверено
    живьём 02.09.2026, 15 файлов без единого браузера).

    ВАЖНО: сессия и отпечаток должны быть из ОДНОГО браузера — сервер сверяет
    их между собой и на несовпадение отвечает `403 SECURITY_CHECK_FAILED`,
    причём первый запрос обычно проходит, и причина по ответу не видна.
    """
    from playwright.sync_api import sync_playwright

    cdp = account.cdp_url if account is not None else _cdp_url()
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.connect_over_cdp(cdp)
        except Exception:                                   # noqa: BLE001
            _launch_chrome(account)
            browser = pw.chromium.connect_over_cdp(cdp)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = next((p for p in context.pages if 'filesta.com' in (p.url or '')), None)
        if page is None:
            page = context.new_page()
            page.goto(PANEL_URL, wait_until='domcontentloaded', timeout=60_000)
            page.wait_for_timeout(5000)
        deadline = time.time() + timeout_sec
        data = {}
        while time.time() < deadline:
            data = page.evaluate(_SESSION_JS) or {}
            if data.get('sessionId') and data.get('accessToken'):
                break
            page.wait_for_timeout(2000)
        data['cookies'] = [
            {k: c[k] for k in ('name', 'value', 'domain', 'path') if k in c}
            for c in context.cookies() if 'filesta' in (c.get('domain') or '')
        ]
        # Ключ защиты берём из куки, если в хранилище страницы его ещё нет: он
        # появляется там только после первого запроса панели, а сессию мы
        # снимаем сразу за отпечатком. Без него сервис отвечает
        # `403 CSRF_HEADER_MISSING` на каждую покупку — так молча отказывал
        # второй аккаунт (поймано живым прогоном 02.09.2026).
        if not data.get('csrf'):
            for cookie in data['cookies']:
                if str(cookie.get('name') or '').upper() == 'XSRF-TOKEN':
                    data['csrf'] = urllib.parse.unquote(str(cookie.get('value') or ''))
                    logger.info('%s: ключ защиты взят из куки XSRF-TOKEN', SOURCE)
                    break
        data['captured_at'] = int(time.time())
        browser.close()

    if not data.get('sessionId') or not data.get('accessToken'):
        raise RuntimeError('сессия не снята: войдите в filesta.com в открытом окне')

    target = (account.session_file if account is not None
              else _home() / 'session.json')
    if account is not None:
        account.ensure_dirs()
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    logger.info("%s: сессия снята → %s", SOURCE, target)
    return data


class HeadlessClient:
    """Покупка мастера БЕЗ браузера — тем же протоколом, что и панель.

    Браузер сегодня держится ради ОДНОГО запроса: за подписанной ссылкой код
    ходит внутри страницы (`page.evaluate(_REQUEST_JS)`), а сам файл качается
    уже из Python. Здесь этот единственный запрос собирается на месте.

    Проверено живьём 02.09.2026: 15 файлов (JPEG 4–42 МБ) скачаны без единого
    браузера; замок по времени при `k=100000` считается 1,6–1,9 с; ротация
    ключа CSRF отрабатывает штатно.

    Зачем это нужно: несколько аккаунтов иначе означали бы несколько постоянно
    открытых Chrome. Плюс запросы можно пустить через прокси, чего с браузером
    не получалось.
    """

    _MAX_ATTEMPTS = 6

    def __init__(self, account=None):
        import requests

        self.account = account
        session = load_session(account)
        self.sid = str(session.get('sessionId') or '')
        self.token = str(session.get('accessToken') or '')
        self.csrf = str(session.get('csrf') or '')
        if not self.csrf:
            # Ключ защиты живёт и в куке. В хранилище страницы он появляется
            # только после первого запроса панели, поэтому у свежеснятой сессии
            # бывает пуст — а без него каждая покупка отбивается
            # `403 CSRF_HEADER_MISSING`.
            for cookie in (session.get('cookies') or []):
                if str(cookie.get('name') or '').upper() == 'XSRF-TOKEN':
                    self.csrf = urllib.parse.unquote(str(cookie.get('value') or ''))
                    break
        self.site_key = str(session.get('siteKey') or '')
        self.ts_token = ''
        self.last_error = ''
        fp_file = (account.fingerprint_file if account is not None
                   else fingerprint_path())
        try:
            self.fp = json.loads(Path(fp_file).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            self.fp = {}
        self.secret = hashlib.sha256(
            f'filesta:download:{self.sid}'.encode()).hexdigest()[:32]
        self.fp_hash = _fp_hash(self.fp)
        self.http = requests.Session()
        self.http.headers['User-Agent'] = str(session.get('ua') or _UA)
        for cookie in (session.get('cookies') or []):
            try:
                self.http.cookies.set(cookie['name'], cookie['value'],
                                      domain=cookie.get('domain'),
                                      path=cookie.get('path', '/'))
            except Exception:                               # noqa: BLE001
                continue
        proxy = str(getattr(config, 'STOCK_PROXY_URL', '') or '').strip()
        if proxy:
            self.http.proxies = {'http': proxy, 'https': proxy}

    def ready(self) -> bool:
        return bool(self.sid and self.token and self.fp)

    # ── внутреннее ──────────────────────────────────────────────────────

    def _headers(self, item_url: str, extra=None) -> dict:
        # `collectedAt` обновляем перед КАЖДОЙ отправкой: сервер проверяет
        # свежесть отпечатка и со старой меткой отбивает 403
        # SECURITY_CHECK_FAILED, не показывая причины. На подпись поле не
        # влияет — в хеш оно не входит.
        self.fp['collectedAt'] = int(time.time() * 1000)
        stamp = str(int(time.time() * 1000))
        nonce = secrets.token_hex(16)
        signature = hmac.new(
            f'{self.secret}:{self.sid}'.encode(),
            f'{stamp}:{nonce}:{self.fp_hash}:POST:/downloads'.encode(),
            hashlib.sha256).hexdigest()
        length = len(item_url)
        head = {
            'Accept': 'application/json, text/plain, */*',
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self.token}',
            'X-Device-Fingerprint': _b64.b64encode(
                json.dumps(self.fp, separators=(',', ':')).encode()).decode(),
            'X-Request-Signature': signature,
            'X-Request-Timestamp': stamp,
            'X-Request-Nonce': nonce,
            'X-Session-Id': self.sid,
            'X-UI-Gesture': '900',
            'X-UI-Path': 'm=1;k=mouse;t=-1;d=-1;v=-1',
            'X-UI-Trap': '0',
            'X-UI-Field': (f'e={length};u=0;o=0;p=0;d=0;b=1;c={length};'
                           f'f=5200;s=900;n=2;t=0'),
            'X-CSRF-Token': self.csrf,
            'X-XSRF-TOKEN': self.csrf,
            'X-Turnstile-Token': self.ts_token,
            'Origin': PANEL_ORIGIN,
            'Referer': PANEL_URL,
        }
        head.update(extra or {})
        return head

    def _post(self, item_url: str, extra=None):
        body = {'platform': 'envato', 'itemUrl': item_url, 'itemType': 'standard'}
        if self.ts_token:
            body['turnstileToken'] = self.ts_token
            body['cfTurnstileResponse'] = self.ts_token
        response = self.http.post(f'{PANEL_ORIGIN}/api/downloads',
                                  headers=self._headers(item_url, extra),
                                  json=body, timeout=180)
        # Ответ пишем ЦЕЛИКОМ: без этого разбор падений превращается в
        # гадание — обрезанного текста ошибки не хватает (поймано 02.09.2026).
        logger.info('[filesta] /api/downloads %s → HTTP %s: %s',
                    item_url.rsplit('/', 1)[-1], response.status_code,
                    response.text[:1200])
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, response.text

    def _solve_captcha(self, payload) -> bool:
        """Капчу без браузера проходит только платный решатель."""
        key = ''
        if isinstance(payload, dict):
            key = str(payload.get('turnstileSiteKey') or '')
        key = key or self.site_key
        if not key:
            self.last_error = 'капча без ключа сайта'
            return False
        self.site_key = key
        try:
            from units.captcha_turnstile import solve as _solve
            token = _solve(key, PANEL_URL)
        except Exception as exc:                            # noqa: BLE001
            self.last_error = f'решатель капчи не сработал: {exc}'
            return False
        if not token:
            self.last_error = 'решатель капчи не вернул токен'
            return False
        self.ts_token = token
        return True

    # ── публичное ───────────────────────────────────────────────────────

    def request_link(self, item_url: str) -> str:
        """Подписанная ссылка на мастер. Пустая строка — не вышло."""
        if not self.ready():
            self.last_error = 'нет сессии или отпечатка'
            return ''
        self.last_error = ''
        for _ in range(self._MAX_ATTEMPTS):
            status, payload = self._post(item_url)
            if not isinstance(payload, dict):
                self.last_error = f'HTTP {status}: {str(payload)[:200]}'
                return ''
            if status in (200, 202):
                data = payload.get('data') or {}
                result = data.get('result') or {}
                link = str(result.get('downloadUrl') or data.get('downloadUrl') or '')
                if link:
                    return link
                self.last_error = 'в ответе нет ссылки на файл'
                return ''
            error = payload.get('error')
            fresh = ((error.get('newToken') if isinstance(error, dict) else None)
                     or payload.get('newToken'))
            if fresh:
                self.csrf = str(fresh)      # ключ ротируется — берём новый
                continue
            if payload.get('code') == 'TIMELOCK_REQUIRED' and payload.get('puzzle'):
                puzzle = payload['puzzle']
                answer = _solve_timelock(puzzle.get('x'), puzzle.get('k'),
                                         puzzle.get('n'))
                if not answer:
                    self.last_error = 'замок по времени не решён'
                    return ''
                status, payload = self._post(item_url, {
                    'X-Timelock-Id': str(puzzle.get('id') or ''),
                    'X-Timelock-Answer': answer})
                if status in (200, 202) and isinstance(payload, dict):
                    data = payload.get('data') or {}
                    result = data.get('result') or {}
                    link = str(result.get('downloadUrl') or data.get('downloadUrl') or '')
                    if link:
                        return link
                if isinstance(payload, dict) and (
                        payload.get('requireTurnstile')
                        or payload.get('code') == 'TURNSTILE_REQUIRED'):
                    if self._solve_captcha(payload):
                        continue
                    return ''
                self.last_error = f'после замка HTTP {status}: {str(payload)[:200]}'
                return ''
            if (payload.get('requireTurnstile')
                    or payload.get('code') == 'TURNSTILE_REQUIRED'):
                if self._solve_captcha(payload):
                    continue
                return ''
            code = str(payload.get('code') or (
                error.get('code') if isinstance(error, dict) else '') or '')
            self.last_error = f'HTTP {status} {code}: {str(payload)[:200]}'
            return ''
        self.last_error = 'исчерпаны попытки'
        return ''


def _fp_hash(fp: dict) -> str:
    """Хеш отпечатка — как считает панель: отсортированные поля через `|`."""
    parts = [f'sr:{fp.get("screenResolution")}', f'cd:{fp.get("colorDepth")}',
             f'tz:{fp.get("timezone")}', f'lang:{fp.get("language")}',
             f'plat:{fp.get("platform")}']
    for key, prefix in (('canvas', 'cv'), ('cpuCores', 'cpu'),
                        ('deviceMemory', 'mem'), ('touchSupport', 'touch'),
                        ('webgl', 'wgl'), ('audio', 'aud'), ('fonts', 'fonts')):
        value = fp.get(key)
        if value is None or value == '':
            continue
        parts.append(f'{prefix}:{value}')
    return hashlib.sha256('|'.join(sorted(parts)).encode()).hexdigest()


def _solve_timelock(x, k, n) -> str:
    """`k` возведений в квадрат по модулю. Ускорить нельзя — в этом смысл."""
    try:
        modulus = int(str(n), 16)
        value = int(str(x), 16) % modulus
        rounds = int(k)
    except (TypeError, ValueError):
        return ''
    for _ in range(rounds):
        value = (value * value) % modulus
    return format(value, 'x')


def load_session(account=None) -> dict:
    """Сохранённая сессия аккаунта. Нет файла — пустой словарь."""
    path = (account.session_file if account is not None
            else _home() / 'session.json')
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


# ── публичные функции провайдера ────────────────────────────────────────────

def download_master(asset_url: str, destination: str | Path) -> bool:
    """Купить и скачать мастер-файл. Одна загрузка = одна единица квоты."""
    job = _downloader.submit(_Job('download', str(asset_url), Path(destination)))
    if not job.ok and job.error:
        logger.warning("[stock] %s: %s — %s", SOURCE, str(asset_url)[-40:], job.error)
    return bool(job.ok)


# Медиа внутри архива. Envato кладёт мастер в ZIP далеко не всегда, а когда
# кладёт — расширение внутри своё (.mov у улова 01.09).
_ARCHIVE_MAGIC = b'PK\x03\x04'
_MEDIA_SUFFIXES = ('.mov', '.mp4', '.m4v', '.webm', '.mkv', '.avi', '.mpg', '.mpeg',
                   '.jpg', '.jpeg', '.png', '.webp', '.tif', '.tiff')


def _unpack_if_archive(path: Path) -> bool:
    """Мастер приехал ZIP-архивом — достать из него сам файл.

    Улов живого прогона 01.09: за ассет `F5LB763` загрузка списалась, файл
    скачался (52,8 МБ) — и оказался ZIP, внутри которого лежал
    `270_Y2ggKDEp.mov`. Мы сохраняли архив под именем `.mp4`, ffprobe его не
    читал («not a valid video»), обрезка падала, клип уходил в нейрогенерацию,
    а квота была потрачена. Два соседних ассета в том же прогоне пришли голым
    mp4 и отработали — потому баг и не был виден раньше.

    Не архив — оставляем как есть.
    """
    import zipfile

    try:
        with path.open('rb') as handle:
            if handle.read(4) != _ARCHIVE_MAGIC:
                return True
    except OSError:
        return False
    try:
        with zipfile.ZipFile(path) as archive:
            members = [m for m in archive.infolist()
                       if not m.is_dir()
                       and Path(m.filename).suffix.lower() in _MEDIA_SUFFIXES]
            if not members:
                logger.warning("%s: в архиве %s нет медиафайла — оставляю как есть",
                               SOURCE, path.name)
                return False
            # Самый крупный: рядом с роликом лежат превью и картинки обложки.
            member = max(members, key=lambda m: m.file_size)
            staging = path.with_name(path.name + '.unpacked')
            with archive.open(member) as source, staging.open('wb') as target:
                shutil.copyfileobj(source, target, 1024 * 1024)
    except Exception as exc:                                # noqa: BLE001
        logger.warning("%s: архив %s не распаковался (%s)", SOURCE, path.name, exc)
        return False
    path.unlink(missing_ok=True)
    staging.replace(path)
    logger.info("%s: мастер приехал архивом — распаковал %s (%.1f МБ)",
                SOURCE, member.filename, member.file_size / 1024 / 1024)
    return True


# Поддержанные решатели: (адрес API, имя задачи для Turnstile). У обоих один и
# тот же порядок работы — createTask, затем опрос getTaskResult, — различаются
# только хост и название типа задачи.
_SOLVERS = {
    'capsolver': ('https://api.capsolver.com', 'AntiTurnstileTaskProxyLess'),
    'capmonster': ('https://api.capmonster.cloud', 'TurnstileTaskProxyless'),
}


def _site_key_from(body: Any) -> str:
    """Ключ виджета из отказа 428: `turnstileSiteKey` в теле ответа."""
    try:
        payload = json.loads(str(body or '') or '{}')
    except (TypeError, ValueError):
        return ''
    return str(payload.get('turnstileSiteKey') or '').strip()


def _human_captcha_wait() -> int:
    """Сколько ждать человека у капчи, секунд (0 — не ждать вовсе)."""
    try:
        return max(0, int(getattr(config, 'FILESTA_CAPTCHA_WAIT_SEC', 300) or 0))
    except (TypeError, ValueError):
        return 300


def _solve_via_service(site_key: str, page_url: str) -> str:
    """Решить Turnstile сторонним сервисом. Ключа нет — тихо возвращаем пусто.

    Ступень НЕобязательная: свой рендер и клик мышью закрывают почти всё, а
    сервис нужен на редкий случай упрямого челленджа. Поэтому отсутствие ключа
    — это не ошибка, а «ступени нет»: пишем строку в лог и идём дальше, к
    ожиданию человека.

    Оговорка: сервис решает капчу на СВОЁМ адресе и своём браузере, и
    Cloudflare такой токен может не принять для нашей сессии. Тогда мы увидим
    очередной 428 — потерь никаких, ступень просто не сработает.
    """
    api_key = str(getattr(config, 'CAPTCHA_SOLVER_KEY', '') or '').strip()
    if not api_key:
        logger.info("%s: сервис-решатель капчи не подключён (CAPTCHA_SOLVER_KEY пуст) "
                    "— пропускаю эту ступень", SOURCE)
        return ''
    provider = str(getattr(config, 'CAPTCHA_SOLVER_PROVIDER', 'capsolver')
                   or 'capsolver').strip().lower()
    setup = _SOLVERS.get(provider)
    if setup is None:
        logger.warning("%s: решатель «%s» не поддержан (есть: %s) — пропускаю ступень",
                       SOURCE, provider, ', '.join(sorted(_SOLVERS)))
        return ''
    base, task_type = setup

    def _post(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        request = urllib.request.Request(
            f'{base}/{path}', data=json.dumps(body).encode('utf-8'),
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode('utf-8'))

    try:
        created = _post('createTask', {
            'clientKey': api_key,
            'task': {'type': task_type, 'websiteURL': page_url, 'websiteKey': site_key},
        })
        task_id = created.get('taskId')
        if not task_id:
            logger.warning("%s: решатель не принял задачу: %s", SOURCE,
                           str(created.get('errorDescription') or created)[:160])
            return ''
        for _ in range(30):                                  # до ~60 секунд
            time.sleep(2)
            result = _post('getTaskResult', {'clientKey': api_key, 'taskId': task_id})
            if result.get('errorId'):
                logger.warning("%s: решатель вернул ошибку: %s", SOURCE,
                               str(result.get('errorDescription'))[:160])
                return ''
            if result.get('status') == 'ready':
                solution = result.get('solution') or {}
                # CapSolver отдаёт `token`, CapMonster — `token` либо
                # `gRecaptchaResponse` (наследие их общего формата).
                token = str(solution.get('token')
                            or solution.get('gRecaptchaResponse') or '')
                if token:
                    logger.info("%s: капча решена сервисом (%s)", SOURCE, provider)
                return token
    except Exception as exc:                                # noqa: BLE001
        logger.warning("%s: решатель недоступен (%s) — иду дальше", SOURCE, exc)
    return ''


def quota_left() -> Optional[int]:
    """Остаток суточных загрузок (сумма по лицензиям Envato) или None."""
    job = _downloader.submit(_Job('quota'), timeout=120)
    return job.payload if job.ok else None


def quota_error() -> str:
    """Почему квота не пришла — словами. Пусто, если всё в порядке.

    Нужно интерфейсу: он показывал единственную догадку про истёкшую сессию на
    любой сбой, и человек чинил не то.
    """
    return str(getattr(_downloader, 'last_quota_error', '') or '')


def source_down() -> str:
    """Непустая строка — источник выключен на прогон (и почему)."""
    return _downloader.fatal()


# ── работа по аккаунтам (без браузера) ──────────────────────────────────────
# Ниже — тот же провайдер, но с явным аккаунтом и БЕЗ поднятого Chrome.
# Старые функции выше не тронуты: они остаются запасным путём на случай, если
# сервис сменит схему подписи.

_headless_clients: Dict[str, 'HeadlessClient'] = {}
_headless_lock = threading.Lock()
_account_quota: Dict[str, Optional[int]] = {}


def headless_enabled() -> bool:
    """Качать без браузера? Умолчание — да (проверено живьём 02.09.2026)."""
    raw = str(getattr(config, 'FILESTA_HEADLESS', '') or '').strip().lower()
    return raw not in ('0', 'off', 'false', 'no')


def client_for(account) -> 'HeadlessClient':
    """Клиент аккаунта. Один на аккаунт: держит сессию и токен капчи."""
    key = account.id if account is not None else '_single'
    with _headless_lock:
        client = _headless_clients.get(key)
        if client is None:
            client = HeadlessClient(account)
            _headless_clients[key] = client
        return client


def reset_clients() -> None:
    """Забыть клиентов (сессию переснимали — старая уже не годится)."""
    with _headless_lock:
        _headless_clients.clear()
        _account_quota.clear()


def account_quota_left(account, refresh: bool = False) -> Optional[int]:
    """Остаток суточных загрузок аккаунта. None — спросить не удалось.

    Ответ запоминается: во время прогона квоту спрашивают перед каждой
    покупкой, а это лишний запрос на каждый клип. После успешной покупки
    значение уменьшается на единицу (`note_spent`).
    """
    key = account.id if account is not None else '_single'
    if not refresh and key in _account_quota:
        return _account_quota[key]
    client = client_for(account)
    if not client.ready():
        _account_quota[key] = None
        return None
    try:
        response = client.http.get(
            f'{PANEL_ORIGIN}/api/users/licenses',
            headers={'Accept': 'application/json',
                     'Authorization': f'Bearer {client.token}',
                     'x-session-id': client.sid,
                     'X-CSRF-Token': client.csrf,
                     'X-XSRF-TOKEN': client.csrf,
                     'Origin': PANEL_ORIGIN, 'Referer': PANEL_URL},
            timeout=60)
        payload = response.json()
    except Exception as exc:                                # noqa: BLE001
        logger.warning('[filesta] %s: квота не пришла — %s',
                       getattr(account, 'id', '?'), exc)
        _account_quota[key] = None
        return None
    items = payload.get('data') if isinstance(payload, dict) else payload
    if isinstance(items, dict):
        items = items.get('licenses') or items.get('items') or []
    total = 0
    found = False
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if str(item.get('service') or '').lower() != 'envato':
            continue
        if item.get('isValid') is False:
            continue
        remaining = item.get('remainingDailyLimit')
        if remaining is None:
            continue
        try:
            total += int(remaining)
            found = True
        except (TypeError, ValueError):
            continue
    value = total if found else None
    _account_quota[key] = value
    return value


def note_spent(account) -> None:
    """Учесть списанную загрузку, не спрашивая сервер заново."""
    key = account.id if account is not None else '_single'
    left = _account_quota.get(key)
    if isinstance(left, int) and left > 0:
        _account_quota[key] = left - 1


def download_master_headless(asset_url: str, destination: str | Path,
                             account=None) -> bool:
    """Купить и скачать мастер БЕЗ браузера, от имени аккаунта."""
    client = client_for(account)
    link = client.request_link(str(asset_url))
    if not link:
        logger.warning('[stock] %s: %s — %s', SOURCE, str(asset_url)[-40:],
                       client.last_error or 'ссылка не получена')
        return False
    note_spent(account)          # квота списана в момент запроса, даже если
                                 # файл не доедет
    return _downloader._fetch_file(link, Path(destination))


PANEL_ORIGIN = 'https://filesta.com'
PANEL_URL = f'{PANEL_ORIGIN}/services/envato/content'
# Запасной адрес ассета — на случай, если живой поиск не ответит. Годится любая
# страница Envato Elements: панели важен адрес, а не что именно за ним.
_FALLBACK_SAMPLE_URL = f'{_BASE}/photos/blue-sky-with-clouds-REJYWRH'
# Поле «Вставьте URL страницы Envato Elements…». Идентификаторы на странице
# генерируются заново при каждой сборке (`f6t1b1b50`), поэтому цепляемся за тип
# поля и текст подсказки — они устойчивы.
_URL_INPUT_SELECTOR = 'input[type="url"]'


def sample_asset_url() -> str:
    """Адрес ассета, который можно вставить в панель, чтобы снять отпечаток.

    Берём первый попавшийся из живой выдачи — так ссылка не протухнет, если
    конкретный ассет снимут с публикации. Не получилось — отдаём запасной.
    """
    try:
        found = search_filesta_envato('sky clouds', 'photo', limit=1)
        if found and found[0].page_url:
            return found[0].page_url
    except Exception as exc:                                # noqa: BLE001
        logger.debug("%s: образец ассета не нашёлся (%s)", SOURCE, exc)
    return _FALLBACK_SAMPLE_URL


def _prefill_download_field(page, url: str) -> bool:
    """Вписать адрес в поле загрузки, чтобы пользователю осталось одно нажатие.

    Поле реактивное: простая запись в `value` его не убеждает — значение
    возвращается назад при первом же перерисовывании. Поэтому пишем через
    нативный сеттер и сами шлём событие `input`, как это делает браузер.
    """
    try:
        return bool(page.evaluate(
            """(args) => {
              const fields = Array.from(document.querySelectorAll(args.sel))
                .filter(el => el.offsetParent && (el.placeholder || '').includes('Envato'));
              const field = fields[0];
              if (!field) return false;
              const setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value').set;
              setter.call(field, args.url);
              field.dispatchEvent(new Event('input', {bubbles: true}));
              field.dispatchEvent(new Event('change', {bubbles: true}));
              field.scrollIntoView({block: 'center'});
              return true;
            }""",
            {'sel': _URL_INPUT_SELECTOR, 'url': url},
        ))
    except Exception as exc:                                # noqa: BLE001
        logger.debug("%s: подставить ссылку не вышло (%s)", SOURCE, exc)
        return False


def _start_download(page) -> bool:
    """Нажать «Начать загрузку» за пользователя.

    Жмём именно эту кнопку по её тексту, а не первую попавшуюся: рядом живут
    «Скачать превью» и переключатели стоков, и промах списал бы не ту загрузку.
    Кнопка оживает не сразу — панель сперва проверяет вставленный адрес.
    """
    for _ in range(10):
        clicked = page.evaluate(
            """() => {
              const wanted = 'начать загрузку';
              const btn = Array.from(document.querySelectorAll('button'))
                .find(b => (b.innerText || '').trim().toLowerCase().startsWith(wanted)
                           && b.offsetParent && !b.disabled);
              if (!btn) return false;
              btn.click();
              return true;
            }"""
        )
        if clicked:
            return True
        page.wait_for_timeout(1000)
    return False


_HIGHLIGHT_JS = """
() => {
  const wanted = 'начать загрузку';
  const btn = Array.from(document.querySelectorAll('button'))
    .find(b => (b.innerText || '').trim().toLowerCase().startsWith(wanted));
  // Подсказка поверх страницы: без неё человек видит открытое окно и не
  // понимает, что от него хотят.
  let tip = document.getElementById('__va_tip');
  if (!tip) {
    tip = document.createElement('div');
    tip.id = '__va_tip';
    tip.style.cssText = 'position:fixed;left:50%;top:18px;transform:translateX(-50%);'
      + 'z-index:2147483647;background:#1f6feb;color:#fff;padding:12px 20px;'
      + 'border-radius:8px;font:600 15px/1.4 system-ui,sans-serif;'
      + 'box-shadow:0 6px 24px rgba(0,0,0,.35);text-align:center';
    document.body.appendChild(tip);
  }
  tip.textContent = btn
    ? 'BegoFrame: ссылка вставлена — нажмите «Начать загрузку» (обведена)'
    : 'BegoFrame: вставьте ссылку на любой ассет Envato и нажмите «Начать загрузку»';
  if (btn) {
    btn.scrollIntoView({block: 'center'});
    btn.style.outline = '3px solid #1f6feb';
    btn.style.outlineOffset = '3px';
    btn.style.boxShadow = '0 0 0 6px rgba(31,111,235,.35)';
  }
  return Boolean(btn);
}
"""


def _highlight_download_button(page) -> bool:
    """Обвести кнопку и написать на странице, что нажать. Молчание — хуже."""
    try:
        return bool(page.evaluate(_HIGHLIGHT_JS))
    except Exception:                                       # noqa: BLE001
        return False


def capture_fingerprint(timeout_sec: int = 300, prefill_url: str = '',
                        autostart: bool = True, account=None,
                        on_progress=None) -> dict:
    """Разово снять НАСТОЯЩИЙ отпечаток устройства.

    Собрать его руками нельзя: canvas/webgl/audio/fonts — вычисляемые хеши.
    Вешаем перехват на XHR (через window.fetch НЕ работает — панель ходит по
    XHR), пользователь один раз качает что-нибудь кнопкой, мы забираем заголовок.

    Панель открываем сами, сами вписываем адрес ассета и сами жмём «Начать
    загрузку»: искать, что бы такое скачать, и куда это вставлять, пользователь
    не должен — он уже согласился, нажав «Снять отпечаток». Загрузка при этом
    списывается одна, из суточных 300. `autostart=False` оставляет нажатие ему.
    """
    import base64
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        _cdp = account.cdp_url if account is not None else _cdp_url()
        try:
            browser = pw.chromium.connect_over_cdp(_cdp)
        except Exception:                                   # noqa: BLE001
            _launch_chrome(account)
            browser = pw.chromium.connect_over_cdp(_cdp)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = next((p for p in context.pages if 'filesta.com' in (p.url or '')), None)
        if page is None:
            page = context.new_page()
            page.goto(PANEL_URL, wait_until='domcontentloaded', timeout=60_000)
            page.wait_for_timeout(6000)
        elif 'envato/content' not in (page.url or ''):
            # Вкладка панели есть, но открыта не на загрузках — переводим сами.
            page.goto(PANEL_URL, wait_until='domcontentloaded', timeout=60_000)
            page.wait_for_timeout(4000)
        page.bring_to_front()
        _say = on_progress if callable(on_progress) else (lambda _t: None)
        _say('открываю панель загрузок…')
        filled = _prefill_download_field(page, prefill_url or sample_asset_url())
        # Перехват вешаем ДО нажатия: запрос, ушедший раньше, мы не увидим.
        page.evaluate(_XHR_SPY_JS)
        # Нажимает ЧЕЛОВЕК, а не код. Программный клик панель отбивает
        # («download error»), а тот же клик мышью проходит — защита смотрит на
        # доверенность события. Раньше агент жал сам, получал отказ, и человек
        # видел молчание и непонятную ошибку (замечание пользователя 02.09.2026).
        if filled:
            _highlight_download_button(page)
            _say('ссылка вставлена — нажмите в окне «Начать загрузку»')
            logger.info("%s: ссылка вставлена — ждём нажатия «Начать загрузку»",
                        SOURCE)
        else:
            _say('вставьте в окне ссылку на любой ассет Envato и нажмите '
                 '«Начать загрузку»')
            logger.info("%s: вставить ссылку не удалось — вставьте адрес любого "
                        "ассета Envato сами и нажмите «Начать загрузку»", SOURCE)
        deadline = time.time() + timeout_sec
        _last_note = 0.0
        while time.time() < deadline:
            page.wait_for_timeout(2000)
            # Раз в 10 секунд напоминаем, сколько осталось: иначе человек видит
            # неподвижную кнопку и думает, что всё зависло.
            if time.time() - _last_note >= 10:
                _last_note = time.time()
                left = int(deadline - time.time())
                _say(f'жду нажатия «Начать загрузку» в окне браузера '
                     f'({left // 60} мин {left % 60} с)')
                _highlight_download_button(page)
            raw = page.evaluate('() => window.__fpSpy')
            if raw:
                _say('отпечаток пойман, сохраняю…')
                data = json.loads(base64.b64decode(raw + '=' * (-len(raw) % 4)))
                target = (account.fingerprint_file if account is not None
                          else fingerprint_path())
                if account is not None:
                    account.ensure_dirs()
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
                logger.info("%s: отпечаток сохранён → %s", SOURCE, target)
                return data
    raise RuntimeError('отпечаток не пойман: скачайте что-нибудь кнопкой в панели Filesta')
