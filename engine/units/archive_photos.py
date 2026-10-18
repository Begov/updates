"""
real_photo.py — Real archival photo/video insertion stage.

Sits between Director (montage_plan.json + prompts) and ImgGen.
For selected clips, replaces AI-generated imagery with real archival material:
  - Searches Wikimedia Commons / NASA / Library of Congress / Internet Archive
  - Filters by license (PD / CC0 / CC-BY only)
  - Downloads top match
  - Photos: pre-renders Ken Burns video clip into vid_img/{id:03d}.mp4
  - Videos: extracts keyframes, asks Codex to pick best segment, cuts to clip duration
  - Marks clips in montage_plan as "source": "real_photo" or "real_video"
  - Writes credits.json for YT_Pack to use in description

After this stage:
  - ImgGen skips marked clips (no image needed — already have vid_img file)
  - Veo i2v skips clips where vid_img/{id}.mp4 already exists (existing behavior)
  - Final concat picks them up like any other clip

Codex CLI is the LLM brain (mirrors gpt_director.py pattern).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import http.client as http_client
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime
from itertools import zip_longest
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

import params as config
from units import codex_netproxy as codex_proxy
from units.scene_context import build_context_cards, compact_context


_CODEX_PROXY_ENV_KEYS = (
    'CODEX_PROXY',
    'HTTPS_PROXY', 'https_proxy',
    'HTTP_PROXY', 'http_proxy',
    'ALL_PROXY', 'all_proxy',
)


def _codex_env_without_proxy(env: dict) -> dict:
    direct = dict(env)
    for key in _CODEX_PROXY_ENV_KEYS:
        direct.pop(key, None)
    return direct


def _codex_transport_failed(output: str) -> bool:
    low = (output or '').lower()
    return any(marker in low for marker in (
        'unable to connect to proxy',
        'proxyerror',
        'connection refused',
        'os error 10061',
        'failed to connect to websocket',
        'error sending request for url',
        'stream disconnected before completion',
        'failed to refresh available models',
        'timeout waiting for child process to exit',
    ))


def _prepare_codex_env() -> tuple[dict, str, bool]:
    """Окружение Codex с единым на процесс вердиктом о прокси (codex_proxy)."""
    return codex_proxy.prepare()


# Suppress subprocess windows on Windows
if platform.system() == 'Windows':
    SUBPROCESS_FLAGS = {'creationflags': 0x08000000}
else:
    SUBPROCESS_FLAGS = {}

logger = logging.getLogger('RealPhoto')


# ============================================================================
# CONSTANTS
# ============================================================================

# License filter — only these licenses pass
ALLOWED_LICENSE_KEYWORDS = (
    'public domain', 'publicdomain', 'pd-',
    'cc0', 'cc-zero',
    'cc-by-4', 'cc-by-3', 'cc-by-2', 'cc-by-1', 'cc-by ',
    'cc by', 'creative commons attribution',
)

# Ken Burns parameters — gentle, documentary-style.
# Total zoom range across a clip is ~10-12% (1.00 → 1.12). Anything more than
# that produces visible jitter/shake from zoompan's per-frame rounding and looks
# unprofessional. Real documentaries use even smaller motion (5-8%).
KB_MIN_ZOOM = 1.0
KB_MAX_ZOOM = 1.12

# Background blur parameters for letterbox replacement
BG_BLUR_SIGMA = 30        # Gaussian blur sigma (higher = blurrier)
BG_BRIGHTNESS = -0.18     # Darken bg slightly so foreground photo pops

# HTTP defaults
HTTP_TIMEOUT = 30
HTTP_USER_AGENT = 'BegoFrame-RealPhoto/1.0 (https://github.com/begoframe; begoframe@local)'

# Codex defaults
CODEX_TIMEOUT = 600  # seconds for selector
CODEX_KF_TIMEOUT = 240  # seconds per keyframe choice

# Cache lives inside each project at <project>/real_photo_cache/.
# (No global cache — keeps projects isolated and inspectable.)
CACHE_DIR_NAME = 'real_photo_cache'


# ============================================================================
# DATACLASSES
# ============================================================================

@dataclass
class ArchiveResult:
    """One search hit from any archive source."""
    source: str           # 'wikimedia' | 'nasa' | 'loc' | 'archive'
    title: str
    url: str              # direct media URL
    page_url: str         # human page URL (for credits)
    license: str          # license string
    license_ok: bool      # passed filter
    media_type: str       # 'photo' | 'video'
    duration_sec: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    author: str = ''
    description: str = ''
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ClipSelection:
    clip_id: int
    search_query: str
    preferred_type: str        # 'video' | 'photo' | 'either'
    preferred_sources: List[str]
    reasoning: str
    confidence: str            # 'high' | 'medium' | 'low'
    search_query_local: str = ''   # optional native-language query (e.g. Russian for RU audio)
    # Трассировка для real_photo_query_log.json (отладка качества запросов):
    query_provider: str = ''       # 'codex/gpt-5.5' | 'deepseek/model' | 'voiceover-fallback'
    query_path: str = 'primary'    # 'primary' | 'legacy' | 'fallback'
    fallback_reason: str = ''      # ошибка, на которой упал выбранный провайдер (если fallback)
    # Описание сцены + запреты для вижн-отбора EPF (Auto Visual Story, 25.08).
    # Мост переносит его в промпт селектора; пусто — селектор работает как раньше.
    context_text: str = ''


@dataclass
class LocalCandidate:
    """Скачанный кандидат ЛЮБОГО источника Real Photo — то, что видит вижн.

    Общая валюта каскада и микса (25.08): архивы кладут сюда файл из кэша плюс
    исходный `ArchiveResult` (нужен титрам и дедупу), EPF — файл из своей папки
    закачек с `render_contained=True` (его картинки всегда вписываются в 16:9
    с размытым фоном)."""
    path: Path
    source: str                  # 'epf' | 'wikimedia' | 'openverse' | ...
    title: str = ''
    page_url: str = ''
    license: str = ''
    archive: Optional['ArchiveResult'] = None
    render_contained: bool = False


def _mix_max_candidates() -> int:
    """Потолок кандидатов на общем листе в режиме микса."""
    return max(2, int(getattr(config, 'REAL_PHOTO_MIX_MAX_CANDIDATES', 16) or 16))


def _interleave_candidates(groups: List[List['LocalCandidate']], limit: int) -> List['LocalCandidate']:
    """Слить кандидатов источников по кругу: по первому от каждого, потом по
    второму и т.д. Так потолок листа режет ХВОСТЫ источников, а не выбрасывает
    целиком тех, кто оказался последним в списке."""
    merged: List['LocalCandidate'] = []
    for row in zip_longest(*groups):
        for cand in row:
            if cand is None:
                continue
            merged.append(cand)
            if len(merged) >= limit:
                return merged
    return merged


@dataclass
class ClipResult:
    """Final result for one clip after processing."""
    clip_id: int
    status: str               # 'rendered' | 'no_match' | 'to_infographic' | 'download_failed' | 'render_failed' | 'license_blocked'
    media_type: str = ''      # 'photo' | 'video'
    output_file: str = ''     # vid_img/{id:03d}.mp4
    archive_result: Optional[ArchiveResult] = None
    segment_start: float = 0.0
    segment_end: float = 0.0
    error: str = ''
    size_mb: float = 0.0      # downloaded media size (for progress logging)
    elapsed_sec: float = 0.0  # per-clip processing time


# ============================================================================
# UTILITIES
# ============================================================================

def _http_get_json(url: str, timeout: int = HTTP_TIMEOUT, retries: int = 3) -> Dict[str, Any]:
    """GET JSON from URL with proper UA. Retries on IncompleteRead / URLError."""
    last_exc = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': HTTP_USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except (urllib.error.URLError, http_client.IncompleteRead, TimeoutError, OSError) as e:
            last_exc = e
            if attempt < retries - 1:
                delay = 1.0 * (attempt + 1) + 0.5  # 1.5s, 2.5s
                logger.info(f"  HTTP retry {attempt+1}/{retries-1} in {delay:.1f}s: {type(e).__name__}: {e}")
                time.sleep(delay)
    raise last_exc


# Сигнатуры настоящих картинок. Проверяем ПЕРВЫМИ БАЙТАМИ, а не расширением:
# ссылка с .jpg на конце сплошь и рядом отдаёт HTML-страницу (защита от
# хотлинка, редирект на просмотрщик, «страница не найдена»). Такой файл весит
# сотни килобайт и по размеру неотличим от фото.
_IMAGE_MAGIC = (
    b'\xff\xd8\xff',            # JPEG
    b'\x89PNG\r\n\x1a\n',       # PNG
    b'GIF87a', b'GIF89a',       # GIF
    b'BM',                      # BMP
    b'II*\x00', b'MM\x00*',     # TIFF
)


def _looks_like_image(path: Path) -> bool:
    """Правда ли в файле картинка (по сигнатуре, без чтения целиком).

    Зачем: прогон 26.08, клип 15 — Real Photo сохранил страницу
    `instagram.com/reel/...` как `generated/015.jpg` (639 КБ, начинается с
    `<!DOCTYPE html>`). Файл существовал и был непустым, поэтому пайплайн счёл
    сцену закрытой, а упало это только в склейке: ffmpeg не нашёл JPEG-данных,
    кадр заменили соседним — в ролике зумящийся кадр вдруг застывал на 7 секунд.
    Ловить нужно здесь, на скачивании, пока есть кем заменить кандидата.
    """
    try:
        with open(path, 'rb') as fh:
            head = fh.read(16)
    except OSError:
        return False
    if head.startswith(_IMAGE_MAGIC):
        return True
    # WebP: 'RIFF' .... 'WEBP'
    return head[:4] == b'RIFF' and head[8:12] == b'WEBP'


def _http_download(url: str, dest: Path, timeout: int = 120, retries: int = 3,
                   expect_image: bool = True) -> Tuple[bool, int]:
    """Download URL to dest. Retries on transient errors. Returns (success, bytes_downloaded).

    `expect_image=True` (по умолчанию — все вызовы здесь качают фото): файл, в
    котором лежит не картинка, считается НЕудачей и удаляется, чтобы кандидат
    не дошёл до вижна и до кадра.
    """
    last_exc = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': HTTP_USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with open(dest, 'wb') as f:
                    shutil.copyfileobj(resp, f)
            if dest.exists() and dest.stat().st_size > 0:
                if expect_image and not _looks_like_image(dest):
                    head = b''
                    try:
                        with open(dest, 'rb') as fh:
                            head = fh.read(12)
                    except OSError:
                        pass
                    logger.warning(
                        "Скачано не изображение (%s, %d КБ, первые байты %r) — "
                        "кандидат отброшен: %s",
                        dest.name, dest.stat().st_size // 1024, head, url[:120])
                    dest.unlink(missing_ok=True)
                    return False, 0
                return True, dest.stat().st_size
            else:
                return False, 0
        except (urllib.error.URLError, http_client.IncompleteRead, TimeoutError, OSError) as e:
            last_exc = e
            if attempt < retries - 1:
                delay = 1.5 * (attempt + 1)
                logger.info(f"  Download retry {attempt+1}/{retries-1} in {delay:.1f}s: {type(e).__name__}")
                time.sleep(delay)
    logger.warning(f"Download failed after {retries} attempts {url}: {last_exc}")
    return False, 0


def _first_usable(candidates: List['LocalCandidate']) -> Optional['LocalCandidate']:
    """Первый кандидат, чей файл существует и реально является изображением.

    Страховка для веток «вижн не отработал»: слепой `candidates[0]` однажды
    оказался HTML-страницей под именем .jpg, сцена считалась закрытой, и падало
    это только в склейке (прогон 26.08, клип 15). Возвращает None, если ни один
    файл не пригоден — вызывающему честнее отдать сцену следующему источнику.
    """
    for cand in candidates or []:
        path = getattr(cand, 'path', None)
        if not path:
            continue
        path = Path(path)
        if path.exists() and path.stat().st_size > 0 and _looks_like_image(path):
            return cand
    return None


def _safe_filename(s: str, max_len: int = 80) -> str:
    """Make a string safe for use as a filename."""
    s = re.sub(r'[^\w\-. ]+', '_', s)
    s = re.sub(r'\s+', '_', s)
    return s[:max_len].strip('_.')


def _cache_path(cache_root: Path, source: str, identifier: str, ext: str) -> Path:
    """Stable cache path for an archive item under the given cache root."""
    h = hashlib.sha1(identifier.encode('utf-8')).hexdigest()[:16]
    safe = _safe_filename(identifier, 60)
    return cache_root / source / f'{safe}_{h}.{ext.lstrip(".")}'


_CACHE_FILE_LOCKS: Dict[str, threading.Lock] = {}
_CACHE_LOCKS_GUARD = threading.Lock()


def _cache_file_lock(path: Path) -> threading.Lock:
    """Замок на КОНКРЕТНЫЙ файл кэша.

    Клипы идут пулом, а имя в кэше считается от адреса кадра: два клипа, нашедшие
    один и тот же кадр, метят в один файл. Без замка оба качают его одновременно
    и получают обрезанную картинку. Замков ровно столько, сколько разных файлов
    трогали за прогон, — это десятки-сотни маленьких объектов, не проблема.
    """
    key = str(path).lower()
    with _CACHE_LOCKS_GUARD:
        lock = _CACHE_FILE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _CACHE_FILE_LOCKS[key] = lock
        return lock


def _license_ok(license_text: str) -> bool:
    """Check if license string passes the allowed-license filter."""
    if not license_text:
        return False
    s = license_text.lower().replace('_', ' ').replace('/', ' ')
    return any(k in s for k in ALLOWED_LICENSE_KEYWORDS)



# ============================================================================
# ARCHIVE SEARCH — WIKIMEDIA COMMONS
# ============================================================================

def search_wikimedia(query: str, media_type: str = 'either', limit: int = 8) -> List[ArchiveResult]:
    """Wikipedia ARTICLE search → ТОЛЬКО главное изображение статьи (pageimage).

    Находим до `limit` СТАТЕЙ по запросу (по релевантности) и берём кураторское главное
    изображение КАЖДОЙ. Возвращаем по одному фото на статью — несколько вики-страниц,
    отсортированных по релевантности. Разнообразие между клипами даёт дедуп по СТРАНИЦЕ
    (used_pages) в process_clip: занята первая статья → берём pageimage следующей.

    Галерею статьи (prop=images) НЕ тянем: она отдаёт картинки страницы ПОДРЯД без
    привязки к субъекту клипа, и для широких статей (страна) это флаги/карты/политика —
    на клип про природу прилетала карта вторжения. pageimage же кураторский и релевантный.

    Только фото (pageimage = изображение, не видео).

    Commons-проход (шаг 9c, 25.08): вторым каналом — поиск ФАЙЛОВ на Commons по
    запросу (`_search_commons_files`): у файлов есть описание и лицензия из
    метаданных, выдача предметная (не «одна статья — одно фото»). Раньше такой
    проход собирал мусор — теперь всё, что отдаёт источник, проходит вижн-отбор
    (`RealPhotoProcessor._vision_order`), поэтому мусор режется по картинке, а не
    по регуляркам. Итог — чередование: pageimage статьи, файл Commons, статья,
    файл… — чтобы в первые N кандидатов вижна попали оба канала."""
    if media_type == 'video':
        return []
    articles = _search_wikipedia_pageimages(query, limit)
    commons = _search_commons_files(query, limit)
    results: List[ArchiveResult] = []
    seen_urls: set = set()
    for pair in zip_longest(articles, commons):
        for h in pair:
            if h is None or h.url in seen_urls:
                continue
            seen_urls.add(h.url)
            results.append(h)
    return results


# Минимальная ширина файла Commons: мельче на 1080p с Ken Burns — каша.
_COMMONS_MIN_WIDTH = 640


def archive_query_from_scene(
    scene_text: str,
    *,
    voiceover: str = "",
    clip_id: int | None = None,
    work_dir=None,
) -> str:
    """Короткий поисковый запрос по описанию сцены — тем же промптером, что пишет
    запросы на старте стадии (`REAL_PHOTO_PROMPT_PROVIDER`).

    Зачем: клип может прийти к архивам не своим ходом, а из чужого фолбэка —
    например, когда ютуб не успел его закрыть. Готового запроса у такого клипа
    нет, и раньше его добывали механически: выбрасывали служебные слова из
    описания сцены и брали первые семь оставшихся (`compact_query`).

    Так запрос всегда получался из НАЧАЛА описания, а Pass 2 начинает с места
    действия, а не с предмета. Живой пример 26.08: бриф «Timeless dispatch area
    beside a small delivery vehicle… hands lower a paper-wrapped cheese parcel
    into a crate» дал запрос «Timeless dispatch area beside small delivery
    vehicle». Поиск честно принёс электрофургоны и детский постер с гусём, вижн
    так же честно отверг всё до единого, и клип ушёл в фолбэк. Слово «cheese»
    стояло в третьем предложении и попасть в запрос не могло в принципе.

    Промптер же читает сцену целиком и называет её предмет. Возвращает пустую
    строку, если оператор не настроен или не ответил — вызывающий сам решит,
    чем это заменить.
    """
    scene_text = (scene_text or "").strip()
    if not scene_text:
        return ""
    provider = (getattr(config, "REAL_PHOTO_PROMPT_PROVIDER", "") or "codex").strip().lower()
    model = (getattr(config, "REAL_PHOTO_PROMPT_MODEL", "") or "").strip()
    if not model and provider == "codex":
        model = (getattr(config, "REAL_PHOTO_CODEX_MODEL", "") or "").strip()
    try:
        from units.operator_hub import ask_json

        parts = [
            "Write ONE image-search query for a photo archive.",
            "",
            "SCENE:",
            scene_text[:1200],
        ]
        if voiceover and voiceover.strip():
            parts += ["", "VOICEOVER:", voiceover.strip()[:600]]
        parts += [
            "",
            "The query must name the MAIN SUBJECT of the scene — whoever or "
            "whatever the viewer is meant to look at: a person, an animal, a "
            "place, a building, a vehicle, an object, a process. Not the setting "
            "it happens in, and not the camera work. Use 3-6 English words, no "
            "punctuation, no quotes.",
            "KEEP every proper name the scene depends on: a person, a place, a "
            "landmark, an organisation, a date or period. Those ARE the subject, "
            "and dropping them turns the query into something else entirely — "
            "'man' instead of the person, 'museum in France' instead of the "
            "specific one. Never generalise a name away.",
            "DROP what merely surrounds the subject: mood, lighting, weather, "
            "camera work and shot size, background props, and adjectives that "
            "describe the atmosphere rather than the thing itself.",
            'Answer as JSON: {"query": "..."}',
        ]
        # Схему ответа передаём ЯВНО. Без неё `ask_text` подставляет контрольную
        # `visual_story_file_control.json`, которая описывает ОТЧЁТ о работе, а не
        # результат: Codex отвечает статусом, поля `query` в ответе нет, и запрос
        # выходит пустым. Так вечером 26.08 четыре клипа получили пустой запрос и
        # ушли на аварийное сжатие описания.
        data = ask_json(
            provider, model, "\n".join(parts),
            work_dir=work_dir,
            phase=("archive-query/%s" % clip_id) if clip_id is not None else "archive-query",
            schema_file="archive_query.json",
            max_tokens=200,
            timeout=120,
        )
        query = str((data or {}).get("query") or "").strip().strip('"').strip()
        if query:
            logger.info("  [архивы] клип %s: запрос от промптера — «%s»", clip_id, query[:70])
            return query
        logger.warning("  [архивы] клип %s: промптер вернул пустой запрос", clip_id)
    except Exception as exc:                           # noqa: BLE001
        logger.warning("  [архивы] клип %s: промптер запроса не ответил (%s)", clip_id, exc)
    return ""


def _too_small_for_frame(width: int, height: int) -> bool:
    """Кадр заведомо не пройдёт отбор — не тратим на него скачивание.

    Считаем по КОРОТКОЙ стороне и тем же порогом, что у вижна
    (`vision_pick.MIN_FRAME_SIDE`). Раньше источники смотрели только ШИРИНУ:
    панорама 1200x400 проходила отбор, качалась, а `drop_tiny_images`
    выбрасывала её уже перед вижном — слот кандидата тратился впустую
    (замечание пользователя 26.08). Ноль означает «размер неизвестен» —
    такие пропускаем, решит фильтр после скачивания.
    """
    try:
        from units.vision_picker import MIN_FRAME_SIDE as _min_side
    except Exception:                                  # noqa: BLE001
        _min_side = _COMMONS_MIN_WIDTH
    short = min(int(width or 0), int(height or 0)) or max(int(width or 0), int(height or 0))
    return bool(short and short < _min_side)


def _commons_search_pages(query: str, limit: int) -> Dict[str, Any]:
    """Один вызов Commons: generator=search (File:) + imageinfo. Возвращает `pages`."""
    url = (
        'https://commons.wikimedia.org/w/api.php'
        '?action=query&format=json&generator=search&gsrnamespace=6'
        f'&gsrlimit={max(1, min(int(limit) * 2, 20))}'
        f'&gsrsearch={urllib.parse.quote(query + " filetype:bitmap")}'
        '&prop=imageinfo&iiprop=url|mime|size|extmetadata&iiurlwidth=1500'
        '&iiextmetadatafilter=LicenseShortName|Artist|ImageDescription&utf8=1'
    )
    data = _http_get_json(url)
    return data.get('query', {}).get('pages', {}) or {}


def _search_commons_files(query: str, limit: int = 8) -> List[ArchiveResult]:
    """Поиск файлов на Wikimedia Commons: generator=search в пространстве File:
    (namespace 6) + imageinfo одним вызовом (проверено живым запросом 25.08).

    `filetype:bitmap` в строке поиска — только растровые изображения (без svg,
    pdf, аудио, видео). Лицензия — `LicenseShortName` из extmetadata
    (проходит `_license_ok`, как у прочих источников), описание — `ImageDescription`
    без html-тегов. Имена-иконки/логотипы/карты режем `_WIKI_JUNK_RE`, мелочь —
    `_COMMONS_MIN_WIDTH`. Прямая ссылка — `thumburl` (до 1500 px), page_url —
    страница файла (для титров).

    Поиск Commons — строгое «И» по всем словам: «Polish village winter 1950s» даёт
    0 файлов, «Polish village winter» — 46 (проверено 25.08). Поэтому при пустой
    выдаче один повтор без годов/десятилетий в запросе (только они — смысл
    запроса не трогаем; десятилетие потом проверит вижн по картинке)."""
    results: List[ArchiveResult] = []
    try:
        pages = _commons_search_pages(query, limit)
        if not pages:
            relaxed = re.sub(r"\b(1[6-9]\d\d|20\d\d)(s|'s)?\b", ' ', query)
            relaxed = re.sub(r'\s+', ' ', relaxed).strip()
            if relaxed and relaxed.lower() != query.lower():
                logger.info(f"  Commons: пусто по '{query}' → без годов: '{relaxed}'")
                pages = _commons_search_pages(relaxed, limit)
        ordered = sorted([p for p in pages.values() if isinstance(p, dict)],
                         key=lambda p: p.get('index', 999))
        for page in ordered:
            title = str(page.get('title') or '')
            if not title.lower().startswith('file:') or _WIKI_JUNK_RE.search(title):
                continue
            info = (page.get('imageinfo') or [{}])[0]
            mime = str(info.get('mime') or '')
            if not mime.startswith('image/') or mime.endswith('svg+xml') or mime.endswith('gif'):
                continue
            width = int(info.get('width') or 0)
            height = int(info.get('height') or 0)
            if _too_small_for_frame(width, height):
                continue
            direct = info.get('thumburl') or info.get('url') or ''
            if not direct:
                continue
            meta = info.get('extmetadata') or {}
            license_text = str((meta.get('LicenseShortName') or {}).get('value') or '')
            author = re.sub(r'<[^>]+>', '', str((meta.get('Artist') or {}).get('value') or '')).strip()
            description = re.sub(r'<[^>]+>', '', str((meta.get('ImageDescription') or {}).get('value') or ''))
            description = re.sub(r'\s+', ' ', description).strip()[:300]
            results.append(ArchiveResult(
                source='wikimedia', title=title[5:].rsplit('.', 1)[0].replace('_', ' '),
                url=direct, page_url=info.get('descriptionurl') or
                f'https://commons.wikimedia.org/wiki/{urllib.parse.quote(title.replace(" ", "_"))}',
                license=license_text or 'unknown', license_ok=_license_ok(license_text),
                media_type='photo', width=width or None, height=height or None,
                author=author, description=description,
            ))
            if len(results) >= limit:
                break
    except Exception as e:
        logger.warning(f"Commons file search failed for '{query}': {e}")
    return results


def _search_wikipedia_pageimages(query: str, limit: int = 8) -> List[ArchiveResult]:
    """Канал 1 источника wikimedia: статьи Wikipedia → кураторское главное фото каждой."""
    results: List[ArchiveResult] = []
    try:
        # Step 1: поиск СТАТЕЙ (не файлов) — по релевантности MediaWiki
        search_url = (
            'https://en.wikipedia.org/w/api.php'
            '?action=query&format=json&list=search'
            f'&srsearch={urllib.parse.quote(query)}'
            f'&srlimit={limit}&utf8=1'
        )
        data = _http_get_json(search_url)
        titles = [item['title'] for item in data.get('query', {}).get('search', [])]
        if not titles:
            return []

        # Step 2: главное изображение каждой статьи (pageimages, до 1500px)
        titles_param = '|'.join(titles)
        info_url = (
            'https://en.wikipedia.org/w/api.php'
            '?action=query&format=json'
            f'&titles={urllib.parse.quote(titles_param)}'
            '&prop=pageimages|info|pageprops'
            '&pithumbsize=1500&inprop=url&utf8=1'
        )
        info = _http_get_json(info_url)
        pages = info.get('query', {}).get('pages', {})
        # pages — dict, порядок не гарантирован → восстанавливаем порядок релевантности из Step 1
        title_order = {t: i for i, t in enumerate(titles)}
        page_list = sorted(
            [p for p in pages.values() if isinstance(p, dict)],
            key=lambda p: title_order.get(p.get('title', ''), 999),
        )
        seen = set()
        for page in page_list:
            if 'missing' in page:
                continue
            # пропускаем страницы-неоднозначности (disambiguation)
            if isinstance(page.get('pageprops'), dict) and 'disambiguation' in page['pageprops']:
                continue
            title = page.get('title', '')
            page_url = page.get('fullurl') or (
                f'https://en.wikipedia.org/wiki/{urllib.parse.quote(title.replace(" ", "_"))}')
            # Только кураторское главное изображение статьи (pageimage) — по одному на статью.
            thumb = page.get('thumbnail') or {}
            main_url = thumb.get('source') or ''
            if main_url and main_url not in seen:
                seen.add(main_url)
                results.append(ArchiveResult(
                    source='wikimedia', title=title, url=main_url, page_url=page_url,
                    license='Wikipedia article image (лицензия — на странице файла)',
                    license_ok=True, media_type='photo',
                    width=thumb.get('width'), height=thumb.get('height'), description='',
                ))
    except Exception as e:
        logger.warning(f"Wikipedia search failed for '{query}': {e}")
    return results


# Мусорные файлы-иконки Wikipedia (не контентные фото) — отсекаем из галереи статьи
_WIKI_JUNK_RE = re.compile(
    r'(commons-logo|wiki\w*\.(svg|png)|edit-icon|ambox|question_book|'
    r'\bflag\b|\bicon\b|\blogo\b|\.svg$|padlock|symbol|disambig|nuvola|crystal|'
    r'red_pog|pog\.svg|location_map|blank\.|transparent)', re.I)


def _wikipedia_article_gallery(title: str, limit: int = 4) -> List[str]:
    """Изображения-файлы со страницы статьи (prop=images) → прямые URL, кроме иконок/логотипов/карт-меток.
    Даёт разные фото на одну тему, чтобы похожие запросы не приводили к одинаковым кадрам."""
    urls: List[str] = []
    try:
        img_url = (
            'https://en.wikipedia.org/w/api.php'
            '?action=query&format=json&prop=images&imlimit=20'
            f'&titles={urllib.parse.quote(title)}&utf8=1'
        )
        data = _http_get_json(img_url)
        pages = data.get('query', {}).get('pages', {})
        files = []
        for p in pages.values():
            for im in (p.get('images') or []):
                fn = im.get('title', '')
                if fn.lower().startswith('file:') and not _WIKI_JUNK_RE.search(fn):
                    files.append(fn)
        if not files:
            return []
        # imageinfo батчем → прямые URL
        info_url = (
            'https://en.wikipedia.org/w/api.php'
            '?action=query&format=json&prop=imageinfo&iiprop=url|mime'
            f'&titles={urllib.parse.quote("|".join(files[:12]))}&utf8=1'
        )
        info = _http_get_json(info_url)
        for p in info.get('query', {}).get('pages', {}).values():
            ii = p.get('imageinfo', [])
            if not ii:
                continue
            mime = ii[0].get('mime', '')
            u = ii[0].get('url', '')
            if u and mime.startswith('image/') and not mime.endswith('svg+xml'):
                urls.append(u)
            if len(urls) >= limit:
                break
    except Exception:
        pass
    return urls


# Минус-флаги для чистки выдачи Yandex (флаги эффективнее словесной инструкции модели:
# Yandex понимает `-слово` как активное исключение — проверено). Общий/generic-запрос
# («деньги», «солдаты») тянет из веба много мусора — режем классы, которые архивным
# материалом НЕ бывают НИКОГДА:
#   • AI/3D-генерации и рендеры;
#   • мемы / демотиваторы / коллажи;
#   • обои / иконки / инфографика / шаблоны;
#   • товарные листинги маркетплейсов (купить/цена/распродажа);
#   • рисунок/арт/вектор/гравюра/картина/иллюстрация — у Yandex это ЧИСТИМ:
#     живопись/гравюры эпохи подтягиваются из Wikipedia/архивов (их источник —
#     wiki), а Yandex оставляем только под РЕАЛЬНЫЕ ФОТО, иначе он засоряет выдачу.
# Источник правды — редактируемый файл prompts/real_photo_yandex_exclude.txt
# (виден и правится во вкладке Prompts GUI). Инлайн-дефолт ниже — фолбэк, если
# файла нет. Приоритет: env REAL_PHOTO_YANDEX_EXCLUDE > файл > этот инлайн-дефолт.
_YANDEX_EXCLUDE_DEFAULT = (
    '-нейросеть -нейро -ии -ai -midjourney -render -3d -рендер '
    '-мем -демотиватор -коллаж -обои -иконка -инфографика -шаблон '
    '-купить -цена -распродажа '
    '-рисунок -арт -вектор -гравюра -картина -иллюстрация'
)


def _load_yandex_exclude() -> str:
    """Минус-флаги Яндекса: env (кастомный) > файл prompts/real_photo_yandex_exclude.txt > инлайн.
    В файле строки с # и пустые игнорируются, остальное склеивается через пробел.

    ВАЖНО про env: config всегда подставляет старый 8-флаговый дефолт, и он же лежит
    в .env у существующих юзеров (updater его туда клал). Если считать ЛЮБОЙ непустой
    env за override — файл во вкладке Prompts никогда не подействует. Поэтому env
    считаем override ТОЛЬКО если он ОТЛИЧАЕТСЯ от старого авто-дефолта (значит юзер
    задал своё). Иначе — читаем файл (его и правят в GUI)."""
    _OLD_ENV_DEFAULT = '-нейросеть -нейро -ии -ai -midjourney -render -3d -рендер'
    _env = (getattr(config, 'REAL_PHOTO_YANDEX_EXCLUDE', '') or '').strip()
    if _env and _env != _OLD_ENV_DEFAULT:
        return _env
    try:
        _f = Path(__file__).parent.parent / 'instructions' / 'real_photo_yandex_exclude.txt'
        if _f.exists():
            _flags = []
            for _line in _f.read_text(encoding='utf-8').splitlines():
                _s = _line.strip()
                if _s and not _s.startswith('#'):
                    _flags.append(_s)
            _joined = ' '.join(_flags).strip()
            if _joined:
                return _joined
    except Exception:
        pass
    return _YANDEX_EXCLUDE_DEFAULT


def search_yandex(query: str, media_type: str = 'either', limit: int = 8) -> List[ArchiveResult]:
    """Яндекс Картинки (web scraping, БЕЗ API/ключа) — реальные фото по всему вебу.
    Ранжирует по релевантности КАРТИНКИ, а не по совпадению слов в имени файла (в отличие от архивов).
    К запросу добавляются минус-флаги исключения AI/арта (веб полон генераций — флаги чистят выдачу).
    ВНИМАНИЕ: веб-картинки БЕЗ лицензионных метаданных — license_ok форсим True (на риск пользователя,
    для документалок обычно ок). Только фото."""
    if media_type == 'video':
        return []
    results: List[ArchiveResult] = []
    try:
        from helpers.image_finder import search_yandex_images
        _excl = _load_yandex_exclude()
        q_full = f"{query} {_excl}".strip()
        urls = search_yandex_images(q_full, count=limit)
        for u in urls:
            if not u:
                continue
            results.append(ArchiveResult(
                source='yandex',
                title=query,
                url=u,
                page_url='https://yandex.ru/images/',
                license='web (без лицензионных метаданных)',
                license_ok=True,
                media_type='photo',
                description='',
            ))
    except Exception as e:
        logger.warning(f"Yandex image search failed for '{query}': {e}")
    return results


# ============================================================================
# ARCHIVE SEARCH — NASA
# ============================================================================

def search_nasa(query: str, media_type: str = 'either', limit: int = 8) -> List[ArchiveResult]:
    """
    Search NASA Image and Video Library.
    All NASA content is Public Domain (federal government work).
    """
    results: List[ArchiveResult] = []
    try:
        # Map media type
        if media_type == 'video':
            mt_param = 'video'
        elif media_type == 'photo':
            mt_param = 'image'
        else:
            mt_param = 'image,video'

        url = (
            'https://images-api.nasa.gov/search'
            f'?q={urllib.parse.quote(query)}&media_type={mt_param}'
        )
        data = _http_get_json(url)
        items = data.get('collection', {}).get('items', [])[:limit]

        for item in items:
            data_arr = item.get('data', [{}])
            if not data_arr:
                continue
            meta = data_arr[0]
            mtype_nasa = meta.get('media_type', 'image')
            mtype = 'video' if mtype_nasa == 'video' else 'photo'

            title = meta.get('title', '')
            description = meta.get('description', '')[:300]
            author = meta.get('photographer') or meta.get('secondary_creator') or 'NASA'
            page_url = meta.get('nasa_id', '')

            # Find direct asset URL via the asset endpoint
            href = item.get('href', '')
            asset_url = ''
            if href:
                try:
                    asset_data = _http_get_json(href)
                    candidates = asset_data if isinstance(asset_data, list) else asset_data.get('items', [])
                    # Pick best candidate by extension
                    if mtype == 'video':
                        prefer = ['~orig.mp4', 'orig.mp4', '.mp4', '.mov']
                    else:
                        prefer = ['~orig.jpg', 'orig.jpg', '.jpg', '.png']
                    for ext_pref in prefer:
                        for c in candidates:
                            cu = c if isinstance(c, str) else c.get('href', '')
                            if cu.lower().endswith(ext_pref) or ext_pref in cu.lower():
                                asset_url = cu
                                break
                        if asset_url:
                            break
                    if not asset_url and candidates:
                        first = candidates[0]
                        asset_url = first if isinstance(first, str) else first.get('href', '')
                except Exception:
                    pass

            if not asset_url:
                continue

            human_url = f'https://images.nasa.gov/details/{page_url}' if page_url else asset_url

            results.append(ArchiveResult(
                source='nasa',
                title=title,
                url=asset_url,
                page_url=human_url,
                license='Public Domain (NASA)',
                license_ok=True,
                media_type=mtype,
                author=author,
                description=description,
                raw={'nasa_id': page_url},
            ))
    except Exception as e:
        logger.warning(f"NASA search failed for '{query}': {e}")
    return results


# ============================================================================
# ARCHIVE SEARCH — LIBRARY OF CONGRESS
# ============================================================================

def search_loc(query: str, media_type: str = 'either', limit: int = 8) -> List[ArchiveResult]:
    """
    Search Library of Congress (loc.gov) via JSON API.
    Filter by 'no known restrictions' / 'public domain' rights.
    """
    results: List[ArchiveResult] = []
    try:
        # LoC media type filter
        fa = []
        if media_type == 'video':
            fa.append('original-format:film, video')
        elif media_type == 'photo':
            fa.append('original-format:photo, print, drawing')
        # else: leave empty

        params = {
            'q': query,
            'fo': 'json',
            'c': str(limit),
            'sp': '1',
        }
        url = 'https://www.loc.gov/search/?' + urllib.parse.urlencode(params)
        if fa:
            url += '&' + '&'.join(f'fa={urllib.parse.quote(f)}' for f in fa)

        data = _http_get_json(url, timeout=45)
        for item in data.get('results', [])[:limit]:
            rights = (item.get('rights', '') or item.get('access_restricted', ''))
            if isinstance(rights, list):
                rights = ' '.join(rights)
            rights = str(rights).lower()
            license_ok = ('no known restrictions' in rights) or ('public domain' in rights) or _license_ok(rights)

            # Find media URLs
            image_url = ''
            for img in item.get('image_url', [])[::-1]:  # last is usually largest
                if img:
                    image_url = img
                    break

            # Determine media type from original_format
            of = item.get('original_format', [])
            if isinstance(of, str):
                of = [of]
            of_join = ' '.join(of).lower()
            mtype = 'video' if ('film' in of_join or 'video' in of_join) else 'photo'

            if media_type == 'video' and mtype != 'video':
                continue
            if media_type == 'photo' and mtype != 'photo':
                continue

            # Try to find direct media URL
            asset_url = image_url
            for r in item.get('resources', []):
                if r.get('url'):
                    if mtype == 'video' and r.get('url', '').lower().endswith(('.mp4', '.mov', '.mpeg', '.mpg')):
                        asset_url = r['url']
                        break
                    if mtype == 'photo' and r.get('url', '').lower().endswith(('.jpg', '.jpeg', '.png', '.tif', '.tiff')):
                        asset_url = r['url']
                        break

            if not asset_url:
                continue

            page_url = item.get('id', '') or item.get('url', '')
            results.append(ArchiveResult(
                source='loc',
                title=item.get('title', ''),
                url=asset_url,
                page_url=page_url,
                license=str(item.get('rights', 'No known restrictions')),
                license_ok=license_ok,
                media_type=mtype,
                author=', '.join(item.get('contributor', [])) if isinstance(item.get('contributor'), list) else str(item.get('contributor', '')),
                description=str(item.get('description', ''))[:300] if item.get('description') else '',
            ))
    except Exception as e:
        logger.warning(f"LoC search failed for '{query}': {e}")
    return results


# ============================================================================
# ARCHIVE SEARCH — INTERNET ARCHIVE
# ============================================================================

def search_archive(query: str, media_type: str = 'either', limit: int = 8) -> List[ArchiveResult]:
    """
    Search Internet Archive (archive.org).
    Filter by licenseurl (CC) or by collection (publicdomain).
    """
    results: List[ArchiveResult] = []
    try:
        if media_type == 'video':
            mediatype_filter = 'mediatype:movies'
        elif media_type == 'photo':
            mediatype_filter = 'mediatype:image'
        else:
            mediatype_filter = '(mediatype:movies OR mediatype:image)'

        # Bias toward PD/CC items
        q = f'({query}) AND {mediatype_filter} AND (licenseurl:* OR collection:opensource_movies OR collection:prelinger OR collection:publicdomain)'
        params = {
            'q': q,
            'fl[]': 'identifier,title,description,creator,licenseurl,mediatype,collection,subject',
            'rows': str(limit),
            'page': '1',
            'output': 'json',
        }
        url = 'https://archive.org/advancedsearch.php?' + urllib.parse.urlencode(params, doseq=True)
        data = _http_get_json(url, timeout=45)
        docs = data.get('response', {}).get('docs', [])

        for doc in docs:
            ident = doc.get('identifier', '')
            if not ident:
                continue
            mt_doc = doc.get('mediatype', '')
            mtype = 'video' if mt_doc == 'movies' else 'photo'
            if media_type == 'video' and mtype != 'video':
                continue
            if media_type == 'photo' and mtype != 'photo':
                continue

            license_url = doc.get('licenseurl', '')
            if isinstance(license_url, list):
                license_url = license_url[0] if license_url else ''
            collections = doc.get('collection', [])
            if isinstance(collections, str):
                collections = [collections]
            collections_str = ' '.join(collections).lower()

            license_ok = (
                _license_ok(license_url) or
                'publicdomain' in collections_str or
                'prelinger' in collections_str
            )

            # Need to fetch the metadata file to get actual asset URLs
            try:
                meta = _http_get_json(f'https://archive.org/metadata/{ident}', timeout=20)
                files = meta.get('files', [])
                # Pick best file
                asset_url = ''
                if mtype == 'video':
                    # Prefer mp4, then ogv, then webm
                    for ext in ('.mp4', '.ogv', '.webm', '.mpeg', '.mpg'):
                        for f in files:
                            name = f.get('name', '')
                            if name.lower().endswith(ext):
                                asset_url = f'https://archive.org/download/{ident}/{urllib.parse.quote(name)}'
                                break
                        if asset_url:
                            break
                else:
                    for ext in ('.jpg', '.jpeg', '.png', '.tif', '.tiff'):
                        for f in files:
                            name = f.get('name', '')
                            if name.lower().endswith(ext) and 'thumb' not in name.lower():
                                asset_url = f'https://archive.org/download/{ident}/{urllib.parse.quote(name)}'
                                break
                        if asset_url:
                            break
                if not asset_url:
                    continue
            except Exception:
                continue

            page_url = f'https://archive.org/details/{ident}'
            creator = doc.get('creator', '')
            if isinstance(creator, list):
                creator = ', '.join(creator)

            results.append(ArchiveResult(
                source='archive',
                title=doc.get('title', ident),
                url=asset_url,
                page_url=page_url,
                license=license_url or ('Public Domain (Prelinger/PD collection)' if license_ok else 'unknown'),
                license_ok=license_ok,
                media_type=mtype,
                author=str(creator)[:100],
                description=str(doc.get('description', ''))[:300] if doc.get('description') else '',
                raw={'identifier': ident, 'collections': collections},
            ))
    except Exception as e:
        logger.warning(f"Internet Archive search failed for '{query}': {e}")
    return results


# ============================================================================
# ARCHIVE SEARCH — METROPOLITAN MUSEUM OF ART (no auth)
# ============================================================================

def search_metmuseum(query: str, media_type: str = 'either', limit: int = 8) -> List[ArchiveResult]:
    """
    Search Met Museum Open Access (https://metmuseum.github.io/).
    All results are CC0 (Public Domain). No API key needed.
    Met has only photos (artworks), no video.
    """
    if media_type == 'video':
        return []
    results: List[ArchiveResult] = []
    try:
        # Search returns object IDs; have to fetch each for details
        search_url = (
            'https://collectionapi.metmuseum.org/public/collection/v1/search'
            f'?q={urllib.parse.quote(query)}&hasImages=true'
        )
        data = _http_get_json(search_url, timeout=20)
        ids = (data.get('objectIDs') or [])[:limit]
        if not ids:
            return []

        # Fetch details in parallel — use small thread pool
        def _fetch_one(obj_id: int):
            try:
                detail_url = f'https://collectionapi.metmuseum.org/public/collection/v1/objects/{obj_id}'
                d = _http_get_json(detail_url, timeout=15)
                if not d.get('isPublicDomain'):
                    return None
                primary = d.get('primaryImage') or d.get('primaryImageSmall', '')
                if not primary:
                    return None
                return ArchiveResult(
                    source='met',
                    title=d.get('title', ''),
                    url=primary,
                    page_url=d.get('objectURL', ''),
                    license='Public Domain (CC0)',
                    license_ok=True,
                    media_type='photo',
                    author=d.get('artistDisplayName', '') or d.get('culture', ''),
                    description=(d.get('objectDate', '') + ' — ' + d.get('medium', ''))[:300],
                    raw={'object_id': obj_id, 'department': d.get('department', '')},
                )
            except Exception:
                return None

        with ThreadPoolExecutor(max_workers=4) as ex:
            for fut in as_completed([ex.submit(_fetch_one, oid) for oid in ids]):
                r = fut.result()
                if r:
                    results.append(r)
    except Exception as e:
        logger.warning(f"Met Museum search failed for '{query}': {e}")
    return results


# ============================================================================
# ARCHIVE SEARCH — EUROPEANA (optional API key)
# ============================================================================

def search_europeana(query: str, media_type: str = 'either', limit: int = 8) -> List[ArchiveResult]:
    """
    Search Europeana (50M+ EU cultural heritage items).
    Free API key required: https://pro.europeana.eu/page/get-api
    Set EUROPEANA_API_KEY in .env. If missing, returns [].
    """
    api_key = (getattr(config, 'EUROPEANA_API_KEY', '') or '').strip()
    if not api_key:
        return []
    results: List[ArchiveResult] = []
    try:
        # Build type filter
        type_filter = ''
        if media_type == 'video':
            type_filter = '&qf=TYPE:VIDEO'
        elif media_type == 'photo':
            type_filter = '&qf=TYPE:IMAGE'
        # Filter by reusable rights (CC + PD)
        rights_filter = '&reusability=open'

        url = (
            'https://api.europeana.eu/record/v2/search.json'
            f'?wskey={api_key}'
            f'&query={urllib.parse.quote(query)}'
            f'&rows={limit}'
            f'{type_filter}{rights_filter}'
            '&profile=rich'
        )
        data = _http_get_json(url, timeout=30)
        for item in data.get('items', []):
            rights = item.get('rights', [])
            if isinstance(rights, list):
                rights_str = ' '.join(rights)
            else:
                rights_str = str(rights)
            license_ok = _license_ok(rights_str)
            if not license_ok:
                continue

            ed_type = (item.get('type') or '').upper()
            if ed_type == 'VIDEO':
                mtype = 'video'
            elif ed_type == 'IMAGE':
                mtype = 'photo'
            else:
                continue
            if media_type == 'video' and mtype != 'video':
                continue
            if media_type == 'photo' and mtype != 'photo':
                continue

            # Find direct asset URL
            edm_isShownBy = item.get('edmIsShownBy', [])
            if isinstance(edm_isShownBy, list):
                asset_url = edm_isShownBy[0] if edm_isShownBy else ''
            else:
                asset_url = str(edm_isShownBy)
            if not asset_url:
                # Fallback to preview
                preview = item.get('edmPreview', [])
                asset_url = preview[0] if isinstance(preview, list) and preview else ''
            if not asset_url:
                continue

            title_arr = item.get('title', [])
            title = title_arr[0] if isinstance(title_arr, list) and title_arr else str(title_arr or '')
            creator_arr = item.get('dcCreator', [])
            creator = creator_arr[0] if isinstance(creator_arr, list) and creator_arr else str(creator_arr or '')
            page_url = item.get('guid', '') or item.get('id', '')

            results.append(ArchiveResult(
                source='europeana',
                title=title[:120],
                url=asset_url,
                page_url=page_url,
                license=rights_str[:120],
                license_ok=True,
                media_type=mtype,
                author=str(creator)[:100],
                description=str(item.get('dcDescription', [''])[0] if item.get('dcDescription') else '')[:300],
                raw={'europeana_id': item.get('id', '')},
            ))
    except Exception as e:
        logger.warning(f"Europeana search failed for '{query}': {e}")
    return results


# ============================================================================
# ARCHIVE SEARCH — SMITHSONIAN (optional API key)
# ============================================================================

def search_smithsonian(query: str, media_type: str = 'either', limit: int = 8) -> List[ArchiveResult]:
    """
    Search Smithsonian Open Access (4.5M+ items, mostly CC0).
    Free API key from api.data.gov: https://api.data.gov/signup/
    Set SMITHSONIAN_API_KEY in .env. If missing, returns [].
    """
    api_key = (getattr(config, 'SMITHSONIAN_API_KEY', '') or '').strip()
    if not api_key:
        return []
    results: List[ArchiveResult] = []
    try:
        # Smithsonian Open Access API — only items with CC0 are returned by default
        url = (
            'https://api.si.edu/openaccess/api/v1.0/search'
            f'?api_key={api_key}'
            f'&q={urllib.parse.quote(query)}'
            f'&rows={limit}'
        )
        data = _http_get_json(url, timeout=30)
        rows = data.get('response', {}).get('rows', [])
        for item in rows:
            content = item.get('content', {})
            descriptive = content.get('descriptiveNonRepeating', {})
            online_media = descriptive.get('online_media', {})
            media_arr = online_media.get('media', [])
            if not media_arr:
                continue

            # Filter by license
            usage = (media_arr[0].get('usage', {}) or {}).get('access', '').upper()
            license_ok = (usage == 'CC0') or _license_ok(usage)
            if not license_ok:
                continue

            # Determine type from media
            media_type_si = (media_arr[0].get('type', '') or '').lower()
            if 'video' in media_type_si:
                mtype = 'video'
            else:
                mtype = 'photo'
            if media_type == 'video' and mtype != 'video':
                continue
            if media_type == 'photo' and mtype != 'photo':
                continue

            asset_url = media_arr[0].get('content', '') or media_arr[0].get('thumbnail', '')
            if not asset_url:
                continue

            title = descriptive.get('title', {}).get('content', '') if isinstance(descriptive.get('title'), dict) else str(descriptive.get('title', ''))
            page_url = descriptive.get('record_link', '') or content.get('descriptiveNonRepeating', {}).get('guid', '')

            # Author / unit
            data_source = descriptive.get('data_source', '')

            results.append(ArchiveResult(
                source='smithsonian',
                title=str(title)[:120],
                url=asset_url,
                page_url=page_url,
                license=usage or 'CC0',
                license_ok=True,
                media_type=mtype,
                author=str(data_source)[:100],
                description='',
                raw={'unit': data_source},
            ))
    except Exception as e:
        logger.warning(f"Smithsonian search failed for '{query}': {e}")
    return results


# ============================================================================
# ARCHIVE SEARCH — RIJKSMUSEUM (Amsterdam, ~700K objects, all CC0)
# ============================================================================

def search_rijksmuseum(query: str, media_type: str = 'either', limit: int = 8) -> List[ArchiveResult]:
    """
    Search Rijksmuseum (Amsterdam) Open Data API.
    Free API key required: https://www.rijksmuseum.nl/en/research/conduct-research/data/api
    Set RIJKSMUSEUM_API_KEY in .env. If missing, returns [].
    All results in this collection are CC0 (Public Domain).
    Photos only — Rijksmuseum doesn't host video.
    """
    if media_type == 'video':
        return []
    api_key = (getattr(config, 'RIJKSMUSEUM_API_KEY', '') or '').strip()
    if not api_key:
        return []
    results: List[ArchiveResult] = []
    try:
        url = (
            'https://www.rijksmuseum.nl/api/en/collection'
            f'?key={api_key}'
            f'&q={urllib.parse.quote(query)}'
            f'&imgonly=true'
            f'&ps={limit}'
            f'&format=json'
        )
        data = _http_get_json(url, timeout=20)
        for item in data.get('artObjects', []):
            web_image = item.get('webImage') or {}
            asset_url = web_image.get('url', '')
            if not asset_url:
                continue
            # Bump to highest available res by stripping size constraints if present
            # Rijksmuseum URLs end like ".../=s0" — replace with no constraint
            if '=s' in asset_url:
                asset_url = re.sub(r'=s\d+$', '=s0', asset_url)

            obj_id = item.get('objectNumber', '') or item.get('id', '')
            title = item.get('title', '')
            long_title = item.get('longTitle', '')
            maker = item.get('principalOrFirstMaker', '') or ''
            page_url = item.get('links', {}).get('web', '') or f'https://www.rijksmuseum.nl/en/collection/{obj_id}'

            results.append(ArchiveResult(
                source='rijksmuseum',
                title=title[:120],
                url=asset_url,
                page_url=page_url,
                license='Public Domain (CC0)',
                license_ok=True,
                media_type='photo',
                author=maker[:100],
                description=long_title[:300],
                raw={'object_number': obj_id},
            ))
    except Exception as e:
        logger.warning(f"Rijksmuseum search failed for '{query}': {e}")
    return results


# ============================================================================
# ARCHIVE SEARCH — GETTY (Open Content Program)
# ============================================================================

def search_getty(query: str, media_type: str = 'either', limit: int = 8) -> List[ArchiveResult]:
    """
    Search Getty Museum Open Content via their public search gateway.

    Note: Getty does not expose a documented keyword JSON API for their
    Open Content collection. This implementation uses their public search
    endpoint best-effort. If Getty's API surface changes or returns HTML,
    this function gracefully returns []. Coverage may be limited.

    All Open Content items at Getty are Public Domain.
    """
    if media_type == 'video':
        return []
    results: List[ArchiveResult] = []
    try:
        # Getty's Search Gateway is the most stable public endpoint we have
        url = (
            'https://search.getty.edu/gateway/v1/search.json'
            f'?q={urllib.parse.quote(query)}'
            f'&pgs={limit}'
            f'&res=museum'
        )
        data = _http_get_json(url, timeout=20)
        items = (data.get('results') or {}).get('items', []) if isinstance(data, dict) else []

        for item in items[:limit]:
            # Try several common metadata shapes — Getty is inconsistent
            asset_url = ''
            for k in ('thumbnail_url', 'image_url', 'iiif_url'):
                v = item.get(k)
                if v:
                    asset_url = v if isinstance(v, str) else (v[0] if isinstance(v, list) and v else '')
                    if asset_url:
                        break
            if not asset_url:
                continue

            title = item.get('title', '')
            if isinstance(title, list):
                title = title[0] if title else ''
            page_url = item.get('url', '') or item.get('id', '')
            creator = item.get('creator', '') or item.get('artist', '')
            if isinstance(creator, list):
                creator = ', '.join(str(c) for c in creator if c)

            results.append(ArchiveResult(
                source='getty',
                title=str(title)[:120],
                url=str(asset_url),
                page_url=str(page_url),
                license='Public Domain (Getty Open Content)',
                license_ok=True,
                media_type='photo',
                author=str(creator)[:100],
                description='',
                raw={},
            ))
    except Exception as e:
        # Getty search is unstable — log debug, not warning
        logger.debug(f"Getty search returned no results for '{query}': {e}")
    return results


# ============================================================================
# ARCHIVE SEARCH — OPENVERSE (CC-фото: Flickr, Commons, музеи; ключ не нужен)
# ============================================================================

_OPENVERSE_TOKEN: Dict[str, Any] = {'value': '', 'expires': 0.0}


def _openverse_auth_header() -> Dict[str, str]:
    """Bearer-токен Openverse по client_id/secret (POST /v1/auth_tokens/token/,
    grant_type=client_credentials — по доке api.openverse.org). Пусто в config →
    анонимно (лимит 20/мин, 200/день — из заголовков ответа, пробник 25.08)."""
    cid = (getattr(config, 'OPENVERSE_CLIENT_ID', '') or '').strip()
    secret = (getattr(config, 'OPENVERSE_CLIENT_SECRET', '') or '').strip()
    if not cid or not secret:
        return {}
    if _OPENVERSE_TOKEN['value'] and time.time() < _OPENVERSE_TOKEN['expires'] - 60:
        return {'Authorization': f"Bearer {_OPENVERSE_TOKEN['value']}"}
    try:
        body = urllib.parse.urlencode({'client_id': cid, 'client_secret': secret,
                                       'grant_type': 'client_credentials'}).encode('utf-8')
        req = urllib.request.Request('https://api.openverse.org/v1/auth_tokens/token/', data=body,
                                     headers={'User-Agent': HTTP_USER_AGENT,
                                              'Content-Type': 'application/x-www-form-urlencoded'})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        token = str(data.get('access_token') or '')
        if token:
            _OPENVERSE_TOKEN['value'] = token
            _OPENVERSE_TOKEN['expires'] = time.time() + float(data.get('expires_in') or 3600)
            return {'Authorization': f'Bearer {token}'}
    except Exception as e:
        logger.warning(f"Openverse token failed (going anonymous): {e}")
    return {}


def search_openverse(query: str, media_type: str = 'either', limit: int = 8) -> List[ArchiveResult]:
    """Openverse /v1/images/ (параметры и поля — по доке и живому пробнику 25.08).

    `license_type=commercial` — только лицензии без NC/ND (by, by-sa, cc0, pdm):
    у Openverse это честный фильтр, в отличие от `_license_ok` по строке.
    `category=photograph` НЕ ставим — режет выдачу до единиц (56 → 2).
    Строгое «И» всех слов: пусто → один повтор без годов (как у Commons).
    Только фото. Поля результата: url (прямая), foreign_landing_url (страница
    для титров), creator, license + license_version, width/height."""
    if media_type == 'video':
        return []
    results: List[ArchiveResult] = []

    def _fetch(q: str) -> List[Dict[str, Any]]:
        params = {'q': q, 'page_size': max(1, min(int(limit) * 2, 20)),
                  'license_type': 'commercial', 'mature': 'false'}
        req = urllib.request.Request(
            'https://api.openverse.org/v1/images/?' + urllib.parse.urlencode(params),
            headers={'User-Agent': HTTP_USER_AGENT, 'Accept': 'application/json',
                     **_openverse_auth_header()})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        return list(data.get('results') or [])

    try:
        items = _fetch(query)
        if not items:
            relaxed = re.sub(r"\b(1[6-9]\d\d|20\d\d)(s|'s)?\b", ' ', query)
            relaxed = re.sub(r'\s+', ' ', relaxed).strip()
            if relaxed and relaxed.lower() != query.lower():
                logger.info(f"  Openverse: пусто по '{query}' → без годов: '{relaxed}'")
                items = _fetch(relaxed)
        for item in items:
            direct = str(item.get('url') or '')
            if not direct:
                continue
            width = int(item.get('width') or 0)
            height = int(item.get('height') or 0)
            if _too_small_for_frame(width, height):
                continue
            lic = str(item.get('license') or '').lower()
            ver = str(item.get('license_version') or '')
            license_text = {'pdm': 'Public domain mark', 'cc0': 'CC0'}.get(lic, f'CC {lic.upper()} {ver}'.strip())
            results.append(ArchiveResult(
                source='openverse', title=str(item.get('title') or '')[:120], url=direct,
                page_url=str(item.get('foreign_landing_url') or item.get('detail_url') or ''),
                license=license_text, license_ok=True, media_type='photo',
                width=width or None, height=height or None,
                author=str(item.get('creator') or '')[:100],
                description=', '.join(str(t.get('name') or '') for t in (item.get('tags') or [])[:8]),
                raw={'provider': item.get('provider'), 'source': item.get('source'),
                     'license_url': item.get('license_url')},
            ))
            if len(results) >= limit:
                break
    except urllib.error.HTTPError as e:
        logger.warning(f"Openverse search failed for '{query}': HTTP {e.code}"
                       + (' (лимит анонимных запросов — задайте OPENVERSE_CLIENT_ID/SECRET)' if e.code == 429 else ''))
    except Exception as e:
        logger.warning(f"Openverse search failed for '{query}': {e}")
    return results


# ============================================================================
# ARCHIVE SEARCH — GOOGLE IMAGES via SERPER.DEV (ключ SERPER_API_KEY)
# ============================================================================

def _no_stock_photos() -> bool:
    """Галка «Без стоковых фото» (GUI Real Photo). Касается только веб-поиска —
    EPF и Serper; у архивных источников фотостоков не бывает."""
    value = getattr(config, 'REAL_PHOTO_NO_STOCK_PHOTOS', True)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


# Минус-слова для веб-поиска: те же, что уходят в поисковую фразу EPF.
_STOCK_MINUS_WORDS = (
    'alamy', 'shutterstock', 'dreamstime', 'istockphoto', 'gettyimages',
    'depositphotos', 'freepik', 'vecteezy', '123rf',
)
# Фотостоки с водяным знаком на превью — всегда мусор для кадра, слоты вижна не тратим
_SERPER_SKIP_DOMAINS = (
    'alamy.', 'shutterstock.', 'gettyimages.', 'istockphoto.', 'superstock.', 'dreamstime.',
    '123rf.', 'depositphotos.', 'stock.adobe.', 'agefotostock.', 'bridgemanimages.',
    'i.ytimg.com', 'ytimg.com',
    # Держим в паре со списком моста (`extreme_picture_finder_bridge._STOCK_DOMAINS`):
    # списки разъехались, и часть стоков отсекалась только на одном из путей.
    'vecteezy.com', 'freepik.com', 'ftcdn.net', 'canstockphoto.',
    'bigstockphoto.com', 'stockvault.net', 'dissolve.com', 'pond5.com',
)

# Маркетплейсы — отдельная галка «Без товарных карточек». Держим в паре со
# списком моста (`extreme_picture_finder_bridge._MARKETPLACE_DOMAINS`): пути
# два — EPF и Serper, — и отсев обязан работать на обоих.
_MARKETPLACE_SKIP_DOMAINS = (
    'media-amazon.com', 'ssl-images-amazon.com', 'images-amazon.com', 'amazon.',
    'ebayimg.com', 'ebay.', 'alicdn.com', 'aliexpress.', 'alibaba.com', 'taobao.',
    'wbbasket.ru', 'wildberries.ru', 'wbstatic.net', 'ozone.ru', 'ozon.ru',
    'etsystatic.com', 'etsy.com', 'walmartimages.com', 'walmart.com',
    'target.scene7.com', 'shopify.com', 'avito.st', 'avito.ru',
    'joom.com', 'temu.com', 'shein.com',
)
# Минус-слова маркетплейсов для поисковой фразы — тот же приём, что со стоками.
_MARKETPLACE_MINUS_WORDS = ('amazon', 'ebay', 'aliexpress', 'wildberries', 'ozon', 'etsy')


def _no_marketplaces() -> bool:
    """Галка «Без товарных карточек» (GUI Real Photo).

    У стоков барьер — водяной знак, и вижн его видит. У товарной карточки знака
    нет: это фото товара с вшитой рекламной подписью, и на превью она часто
    нечитаема. Улов 01.09: в ролик про сыроварню попал кадр с Amazon с надписью
    «Professional cheese maker choose our set» поперёк нижней трети.
    """
    value = getattr(config, 'REAL_PHOTO_NO_MARKETPLACES', True)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


_PIN_PREVIEW_RE = re.compile(r'(https?://i\.pinimg\.com)/\d+x\d*/', re.I)


def _pinterest_original(url: str) -> str:
    """Превью Pinterest → путь к оригиналу (`/236x/` → `/originals/`).

    Google индексирует у Pinterest уменьшенные копии с потолком 736 px по
    ширине — по нашему порогу кадра (640 по короткой стороне) такие почти
    всегда отпадают. Оригинал лежит по тому же пути с сегментом `originals`.
    Если его нет (бывает 403), скачивание просто не удастся и кандидат
    отвалится штатным путём — терять тут нечего.
    """
    if not url or 'pinimg.com' not in url.lower():
        return url
    return _PIN_PREVIEW_RE.sub(r'\1/originals/', url)


def search_serper(query: str, media_type: str = 'either', limit: int = 8) -> List[ArchiveResult]:
    """Google Images через serper.dev: POST https://google.serper.dev/images,
    заголовок X-API-KEY, тело {q, num}; ответ images[] с imageUrl/imageWidth/
    imageHeight/title/link/domain (по доке serper.dev и живому пробнику 25.08;
    1 кредит на вызов независимо от num).

    Веб-картинки БЕЗ лицензионных метаданных — как yandex: license_ok=True на
    риск пользователя. Стоки с водяными знаками и превью YouTube/Pinterest
    отсекаем по домену (`_SERPER_SKIP_DOMAINS`), остальное судит вижн."""
    if media_type == 'video':
        return []
    api_key = (getattr(config, 'SERPER_API_KEY', '') or '').strip()
    if not api_key:
        return []
    results: List[ArchiveResult] = []
    try:
        # Serper — тот же поиск Google, что у EPF, значит и защита от фотостоков
        # та же: минус-слова в запрос (мусор не находится) плюс отсев по домену
        # ниже. Архивам (Wikipedia, NASA, LoC, Met, Europeana) это не нужно —
        # там фотостоков не бывает, и лишние слова только портят запрос.
        q = query
        if _no_stock_photos():
            q = q + ' ' + ' '.join(f'-{word}' for word in _STOCK_MINUS_WORDS)
        if _no_marketplaces():
            q = q + ' ' + ' '.join(f'-{word}' for word in _MARKETPLACE_MINUS_WORDS)
        body = json.dumps({'q': q, 'num': max(10, min(int(limit) * 3, 30))}).encode('utf-8')
        req = urllib.request.Request(
            'https://google.serper.dev/images', data=body, method='POST',
            headers={'X-API-KEY': api_key, 'Content-Type': 'application/json',
                     'User-Agent': HTTP_USER_AGENT})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        seen_pages: set = set()
        for item in data.get('images') or []:
            direct = str(item.get('imageUrl') or '')
            domain = str(item.get('domain') or '').lower()
            skip_stock = _no_stock_photos() and any(
                d in domain or d in direct.lower() for d in _SERPER_SKIP_DOMAINS)
            # Товарные карточки: у них нет водяного знака, поэтому вижн их
            # пропускает — отсекаем по домену ДО показа (улов 01.09).
            if not skip_stock and _no_marketplaces():
                skip_stock = any(d in domain or d in direct.lower()
                                 for d in _MARKETPLACE_SKIP_DOMAINS)
            # Обложки роликов YouTube бесполезны как кадр всегда, независимо от
            # галки: это монтаж с текстом и стрелками, а не фотография.
            junk = any(d in domain or d in direct.lower()
                       for d in ('i.ytimg.com', 'ytimg.com'))
            # Pinterest НЕ запрещаем: Google индексирует его превью
            # (`/236x/`, `/474x/`, `/736x/`), но по тому же пути лежит оригинал.
            # Проверено 26.08: подмена размера на `/originals/` отдаёт картинку
            # в 3 случаях из 4 и однажды дала прирост 236x353 → 600x898.
            # Мелочь после подмены отсеется порогом размера ниже и в вижне
            # (MIN_FRAME_SIDE), а годные пины мы больше не теряем.
            direct = _pinterest_original(direct)
            if not direct or skip_stock or junk:
                continue
            page = str(item.get('link') or '')
            if page and page in seen_pages:
                continue          # одна статья даёт 3-4 кадра подряд — берём по одному со страницы
            seen_pages.add(page)
            width = int(item.get('imageWidth') or 0)
            height = int(item.get('imageHeight') or 0)
            if _too_small_for_frame(width, height):
                continue
            results.append(ArchiveResult(
                source='serper', title=str(item.get('title') or query)[:120], url=direct,
                page_url=str(item.get('link') or ''),
                license='web (без лицензионных метаданных)', license_ok=True, media_type='photo',
                width=width or None, height=height or None,
                description=domain,
                raw={'domain': domain, 'position': item.get('position')},
            ))
            if len(results) >= limit:
                break
    except urllib.error.HTTPError as e:
        logger.warning(f"Serper search failed for '{query}': HTTP {e.code}"
                       + (' (ключ/кредиты serper.dev)' if e.code in (401, 403, 402, 429) else ''))
    except Exception as e:
        logger.warning(f"Serper search failed for '{query}': {e}")
    return results


# ============================================================================
# UNIFIED SEARCH
# ============================================================================

_BRIGHTDATA_ZONE_CACHE: Dict[str, str] = {}     # токен -> имя зоны
_BRIGHTDATA_ZONE_FAILED: Dict[str, float] = {}  # токен -> когда не вышло узнать


def _brightdata_zone(token: str) -> str:
    """Имя зоны Bright Data: из настроек, иначе спрашиваем у самой Bright Data.

    Зону человек называет сам при её создании, и у каждого она своя. Раньше
    здесь стояло запасное имя `brightdata_serp_api1` — это имя зоны
    РАЗРАБОТЧИКА: у любого другого человека запрос уходил бы в несуществующую
    зону, и источник молча не отдавал бы ничего. Теперь достаточно вписать
    токен: список зон отдаёт `/zone/get_active_zones`, из него берём зону
    типа `serp` (проверено живьём 02.09.2026 — ответ за 1,1 с).

    Настройка `BRIGHTDATA_ZONE` остаётся: если она заполнена, верим ей —
    у человека может быть несколько зон, и он выбирает нужную сам.
    """
    configured = (getattr(config, 'BRIGHTDATA_ZONE', '') or '').strip()
    if configured:
        return configured
    cached = _BRIGHTDATA_ZONE_CACHE.get(token)
    if cached:
        return cached
    # Не вышло минуту назад — не дёргаем службу на каждом клипе заново.
    if time.time() - _BRIGHTDATA_ZONE_FAILED.get(token, 0.0) < 60:
        return ''
    try:
        req = urllib.request.Request(
            'https://api.brightdata.com/zone/get_active_zones',
            headers={'Authorization': f'Bearer {token}',
                     'User-Agent': HTTP_USER_AGENT})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            zones = json.loads(resp.read().decode('utf-8'))
        serp = [z for z in zones if str(z.get('type') or '').lower() == 'serp']
        if serp:
            zone = str(serp[0].get('name') or '')
            if zone:
                _BRIGHTDATA_ZONE_CACHE[token] = zone
                logger.info(f"  Bright Data: зона определена сама — {zone}")
                return zone
        names = ', '.join(str(z.get('name') or '?') for z in zones) or 'ни одной'
        logger.warning(
            'Bright Data: среди зон нет ни одной типа serp '
            f'(есть: {names}). Создайте зону SERP API на brightdata.com '
            'или впишите её имя в настройку BRIGHTDATA_ZONE.')
    except Exception as e:
        logger.warning(f"Bright Data: список зон не получен ({e}); "
                       "впишите имя зоны в настройку BRIGHTDATA_ZONE")
    _BRIGHTDATA_ZONE_FAILED[token] = time.time()
    return ''


def search_brightdata(query: str, media_type: str = 'either', limit: int = 8) -> List[ArchiveResult]:
    """Google Images через шлюз Bright Data: POST /request с телом
    {zone, url, format:'json'}, где url — обычный поиск картинок Google.

    Зачем второй вход в ту же выдачу Google, если есть serper: модуль EPF
    работает только на Windows (это .exe), и на маке архивная ветка оставалась
    без Google вовсе. Здесь — обычный HTTP, работает везде.

    ВАЖНО про параметр поиска: `tbm=isch` Google объявил устаревшим (март
    2026), рабочий — `udm=2`. На пробе 02.09.2026 старый параметр то отдавал
    выдачу, то не отдавал вовсе, новый — стабильно 100 картинок за 11-16 с.

    Отличие от serper: РАЗМЕРОВ В ОТВЕТЕ НЕТ. Ширину и высоту отдаём как None
    («неизвестно»), и мелочь отсекается уже после скачивания, общим фильтром.
    Замер на трёх темах: из 12 скачанных годными (короткая сторона >= 640)
    выходят 5-7, зато кандидатов после фильтров остаётся под сотню — на запрос
    набирается около полусотни пригодных кадров.

    Лицензий у веб-картинок нет — как у serper и yandex, license_ok=True на
    риск пользователя. Фотостоки, маркетплейсы и обложки роликов режем теми же
    списками, что и serper: выдача-то одна и та же, гугловская."""
    if media_type == 'video':
        return []
    token = (getattr(config, 'BRIGHTDATA_API_TOKEN', '') or '').strip()
    if not token:
        return []
    zone = _brightdata_zone(token)
    if not zone:
        return []
    results: List[ArchiveResult] = []
    try:
        q = query
        if _no_stock_photos():
            q = q + ' ' + ' '.join(f'-{word}' for word in _STOCK_MINUS_WORDS)
        if _no_marketplaces():
            q = q + ' ' + ' '.join(f'-{word}' for word in _MARKETPLACE_MINUS_WORDS)
        search_url = ('https://www.google.com/search?q=' + urllib.parse.quote(q)
                      + '&udm=2&hl=en&gl=us')
        body = json.dumps({'zone': zone, 'url': search_url,
                           'format': 'json'}).encode('utf-8')
        req = urllib.request.Request(
            'https://api.brightdata.com/request', data=body, method='POST',
            headers={'Authorization': f'Bearer {token}',
                     'Content-Type': 'application/json',
                     'User-Agent': HTTP_USER_AGENT})
        # Шлюз ходит в Google живьём: 11-16 с на запрос против секунды у
        # serper. Свой таймаут, иначе общий (короткий) обрубит на полпути.
        with urllib.request.urlopen(req, timeout=max(HTTP_TIMEOUT, 90)) as resp:
            payload = json.loads(resp.read().decode('utf-8', 'replace'))
        # Ответ бывает обёрнут: {'body': '<json строкой>'}.
        if isinstance(payload, dict) and isinstance(payload.get('body'), str):
            payload = json.loads(payload['body'])
        seen_pages: set = set()
        for item in (payload.get('images') if isinstance(payload, dict) else None) or []:
            if not isinstance(item, dict):
                continue
            direct = str(item.get('original_image') or '')
            if not direct.startswith('http'):
                continue
            direct = _pinterest_original(direct)
            domain = urllib.parse.urlparse(direct).netloc.lower().replace('www.', '')
            haystack = direct.lower() + ' ' + str(item.get('source') or '').lower()
            skip_stock = _no_stock_photos() and any(
                d in haystack for d in _SERPER_SKIP_DOMAINS)
            if not skip_stock and _no_marketplaces():
                skip_stock = any(d in haystack for d in _MARKETPLACE_SKIP_DOMAINS)
            junk = any(d in haystack for d in ('i.ytimg.com', 'ytimg.com'))
            if skip_stock or junk:
                continue
            page = str(item.get('link') or '')
            if page and page in seen_pages:
                continue          # одна страница даёт несколько кадров — берём один
            seen_pages.add(page)
            # `source` — заголовок страницы вместе с именем сайта, `title` —
            # само имя сайта. Человеку в отчёте полезнее первое.
            caption = str(item.get('source') or item.get('title') or query)
            results.append(ArchiveResult(
                source='brightdata', title=caption[:120], url=direct,
                page_url=page,
                license='web (без лицензионных метаданных)', license_ok=True,
                media_type='photo',
                width=None, height=None,   # выдача размеров не даёт
                description=domain,
                raw={'domain': domain, 'rank': item.get('rank')},
            ))
            if len(results) >= limit:
                break
    except urllib.error.HTTPError as e:
        logger.warning(f"Bright Data search failed for '{query}': HTTP {e.code}"
                       + (' (токен/зона/баланс Bright Data)'
                          if e.code in (401, 402, 403, 429) else ''))
    except Exception as e:
        logger.warning(f"Bright Data search failed for '{query}': {e}")
    return results


SEARCH_FUNCS = {
    'wikimedia':   search_wikimedia,
    'yandex':      search_yandex,
    'openverse':   search_openverse,
    'serper':      search_serper,
    'brightdata':  search_brightdata,
    'nasa':        search_nasa,
    'loc':         search_loc,
    'archive':     search_archive,
    'met':         search_metmuseum,
    'europeana':   search_europeana,
    'smithsonian': search_smithsonian,
    'rijksmuseum': search_rijksmuseum,
    'getty':       search_getty,
}


def search_all_sources(
    query: str,
    media_type: str,
    sources: List[str],
    limit_per_source: Optional[int] = None,
) -> List[ArchiveResult]:
    """Search across all enabled sources in priority order. Returns merged license-passing results.

    limit_per_source — глубина выдачи каждого источника (сколько записей копнуть). None →
    REAL_PHOTO_SEARCH_DEPTH из config (настраивается в GUI). Yandex — веб-хранилище, ему
    глубина «статей» ни к чему, но limit как размер пачки не мешает."""
    if limit_per_source is None:
        limit_per_source = int(getattr(config, 'REAL_PHOTO_SEARCH_DEPTH', 6) or 6)
    out: List[ArchiveResult] = []
    for src in sources:
        fn = SEARCH_FUNCS.get(src)
        if not fn:
            continue
        try:
            hits = fn(query, media_type=media_type, limit=limit_per_source)
            out.extend(h for h in hits if h.license_ok)
        except Exception as e:
            logger.warning(f"Source {src} failed for '{query}': {e}")
    return out


def rank_results(results: List[ArchiveResult], preferred_type: str) -> List[ArchiveResult]:
    """Rank results: preferred type first, then by source priority, then by title length (proxy for specificity)."""
    type_score = {'video': 0, 'photo': 1, 'either': 0.5}
    src_priority = {'wikimedia': 0, 'nasa': 1, 'loc': 2, 'archive': 3}

    def key(r: ArchiveResult):
        type_match = 0 if (preferred_type == 'either' or r.media_type == preferred_type) else 1
        return (type_match, src_priority.get(r.source, 99), -len(r.title))

    return sorted(results, key=key)


# ============================================================================
# CODEX RUNNER (mirrors gpt_director.py pattern)
# ============================================================================

def _clip_parallel_limit() -> int:
    """Сколько клипов Real Photo обрабатывать одновременно.

    Ровно столько же, сколько экземпляров EPF (`REAL_PHOTO_EPF_LANES`): дорожка
    и есть клип в поиске, отдельная настройка была бы дублем (решение
    пользователя 31.08). Когда EPF выключен, число работает как общий потолок
    стадии — сетевые источники параллель держат: замер 31.08 дал wikimedia ×1.9
    и serper ×2.4 на четырёх потоках, с той же выдачей и без отказов.
    """
    try:
        return max(1, min(8, int(getattr(config, 'REAL_PHOTO_EPF_LANES', 4) or 4)))
    except (TypeError, ValueError):
        return 4


def _codex_parallel_limit() -> int:
    """Сколько вызовов Codex CLI разрешено одновременно."""
    try:
        return max(1, min(16, int(getattr(config, 'CODEX_MAX_PARALLEL', 4) or 4)))
    except (TypeError, ValueError):
        return 4


# Один семафор на ВСЕ вызовы `codex exec` в процессе. Дорожки EPF, промптеры
# стадий (сток, ютуб, Real Photo), вижн и адаптеры дёргают Codex независимо
# друг от друга — без ограничения их набегает больше десятка, и вызовы начинают
# упираться в таймаут (прогон 26.08: промптер стока получил «Codex timed out
# after 180s», а в одиночку тот же запрос отвечает за 21с).
# Лимит настраивается `CODEX_MAX_PARALLEL`; очередь честная — кто пришёл раньше,
# тот раньше и запустится.
_CODEX_SLOTS = threading.Semaphore(_codex_parallel_limit())


class CodexRunner:
    """Wraps codex CLI invocation with output schema."""

    def __init__(self, model_name: Optional[str] = None, timeout: int = CODEX_TIMEOUT):
        self.model_name = model_name or getattr(config, 'GPT_MODEL', 'gpt-5.4')
        self.timeout = timeout
        # Resolve codex executable path (matches gpt_director.py logic)
        self.codex_exe = self._find_codex_exe()
        self.schemas_dir = Path(__file__).parent.parent / 'schemas'

    def _find_codex_exe(self) -> Path:
        """Find codex CLI executable.

        Единый источник правды — config.CODEX_EXE_PATH (он уже резолвит платформу:
        win32 → Codex/codex-x86_64-pc-windows-msvc.exe, macOS/Linux → which('codex'),
        плюс явный override через CODEX_EXE_PATH в .env).

        Раньше здесь был свой список кандидатов, который ИГНОРИРОВАЛ config: первым
        всегда шёл ВИНДОВЫЙ .exe и проверялся только на .exists(). В папке Codex/
        лежит именно виндовый бинарь (маководы ставят codex через brew — см.
        Codex/README.txt), поэтому на macOS находился .exe и запуск падал с
        [Errno 13] Permission denied — при том что Pass 1/Pass 2 на той же машине
        работали, т.к. gpt_director берёт путь из config.
        Бьёт по всем потребителям CodexRunner: Style Extract + Real Photo.
        """
        # 1) Общий резолвер конфига — то же, что использует gpt_director/sg_adapter/etc.
        cfg_path = getattr(config, 'CODEX_EXE_PATH', '') or ''
        if cfg_path and Path(cfg_path).is_file():
            return Path(cfg_path)

        # 2) Фолбэк: кандидаты СТРОГО под текущую платформу (не берём чужой бинарь)
        root = Path(__file__).parent.parent.parent
        if sys.platform == 'win32':
            candidates = [root / 'runtime' / 'codex' / 'codex-x86_64-pc-windows-msvc.exe']
        else:
            candidates = [root / 'runtime' / 'codex' / 'codex']
        candidates.append(Path(shutil.which('codex') or ''))

        for c in candidates:
            if not c or not c.is_file():
                continue
            # На POSIX бинарь без флага исполнения запускать нельзя — пропускаем,
            # иначе получаем Permission denied вместо понятной ошибки.
            if os.name != 'nt' and not os.access(str(c), os.X_OK):
                continue
            return c

        raise FileNotFoundError(
            f"Codex executable not found (platform={sys.platform}). "
            f"config.CODEX_EXE_PATH='{cfg_path}'. Looked in: "
            + ', '.join(str(c) for c in candidates if c)
        )

    def run(
        self,
        task_prompt: str,
        schema_file: str,
        result_file: Path,
        working_dir: Path,
        images: Optional[List[Path]] = None,
    ) -> Dict[str, Any]:
        """Run codex exec with schema. Returns parsed control dict.

        `images` прикладывает файлы к промпту через `codex exec -i` — так вижн
        работает на GPT-подписке, а не только через HTTP chat-провайдера
        (решение пользователя 25.08: «вижн можешь юзать на гпт подписке»).
        """
        schema_path = self.schemas_dir / schema_file
        if not schema_path.exists():
            raise FileNotFoundError(f"Schema not found: {schema_path}")

        if result_file.exists():
            result_file.unlink()

        # Write task prompt to file (avoid Windows MAX_PATH limits)
        task_file = working_dir / f'_codex_{schema_file.replace(".json", "")}_task.md'
        task_file.write_text(task_prompt, encoding='utf-8')
        short_prompt = f"Read and follow ALL instructions from this file: {task_file.as_posix()}"

        image_args: List[str] = []
        for image in (images or []):
            image = Path(image)
            if image.is_file():
                image_args += ['-i', str(image)]

        cmd = [
            str(self.codex_exe), 'exec',
            short_prompt,
            *image_args,
            '--output-schema', str(schema_path),
            '--output-last-message', str(result_file),
            '--dangerously-bypass-approvals-and-sandbox',
            '--skip-git-repo-check',
            *config.codex_cli_args_for_provider(),
            '-m', self.model_name,
            '-C', str(working_dir),
        ]

        logger.info(f"Codex exec: model={self.model_name} schema={schema_file} cwd={working_dir.name}"
                    + (f" images={len(image_args) // 2}" if image_args else ""))
        run_env, proxy_fallback_reason, _ = _prepare_codex_env()
        if proxy_fallback_reason:
            logger.warning(
                "Codex proxy unavailable; Real Photo selector runs directly (%s)",
                proxy_fallback_reason,
            )

        # Одиночный запуск без повтора стоил прогону целой сцены. Улов 27.08:
        # четыре клипа стартовали в одну миллисекунду, три Codex отработали за
        # 24 секунды, а четвёртый упал за 6.7с с rc=1 — и стадия молча ушла на
        # механический фолбэк, который выдал запрос из технических слов промпта.
        # Отказ разовый и лечится простым повтором.
        attempts = max(1, int(getattr(config, 'CODEX_RETRIES', 2) or 1))
        problem = ''
        for attempt in range(1, attempts + 1):
            result_file.unlink(missing_ok=True)
            t0 = time.time()
            _waited = time.time()
            # Ждём свободный слот: одновременных `codex exec` не больше
            # CODEX_MAX_PARALLEL, иначе вызовы душат друг друга таймаутами.
            _CODEX_SLOTS.acquire()
            _queued = time.time() - _waited
            if _queued > 1.0:
                logger.info("Codex: ждал очереди %.1fс (лимит %d параллельных)",
                            _queued, _codex_parallel_limit())
            try:
                p = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                    timeout=self.timeout,
                    env=run_env,
                    **SUBPROCESS_FLAGS,
                )
            except subprocess.TimeoutExpired:
                raise TimeoutError(f"Codex timed out after {self.timeout}s")
            finally:
                # Слот отдаём в ЛЮБОМ случае, включая таймаут и падение — иначе
                # одна зависшая задача навсегда съест место в очереди.
                _CODEX_SLOTS.release()

            elapsed = time.time() - t0
            logger.info(f"Codex finished in {elapsed:.1f}s, rc={p.returncode}")

            # Понятный хинт: выбранная модель новее установленного Codex CLI
            # (OpenAI отвечает 400 "requires a newer version of Codex").
            _combined = ((p.stderr or '') + '\n' + (p.stdout or '')).lower()
            if 'requires a newer version of codex' in _combined or 'upgrade to the latest' in _combined:
                _msg = (
                    f"⛔ Модель '{self.model_name}' требует более новую версию Codex CLI.\n"
                    f"   РЕШЕНИЕ: вкладка Settings → кнопка «Update Codex CLI», "
                    f"либо выберите gpt-5.4 / gpt-5.5."
                )
                print(f"\n{_msg}\n", flush=True)
                logger.error(f"Codex model too new for CLI: {self.model_name}")
                raise RuntimeError(_msg)

            if result_file.exists():
                break

            # НАЧАЛО stderr, а не только хвост: у Codex в хвосте стоит баннер
            # («provider: openai, approval: never, sandbox: …»), и прежний
            # `stderr[-500:]` показывал в логе именно его — сообщение об ошибке
            # оставалось невидимым, и разбираться было не с чем (улов 27.08).
            _err = (p.stderr or '').strip()
            _out = (p.stdout or '').strip()
            problem = (
                f"rc={p.returncode}\n"
                f"stderr начало: {_err[:400]}\n"
                f"stderr конец: {_err[-300:]}\n"
                f"stdout конец: {_out[-200:]}"
            )
            if attempt < attempts:
                logger.warning(
                    "Codex не создал %s (попытка %s из %s, %.1fс) — повторяю. %s",
                    result_file.name, attempt, attempts, elapsed, problem)
                time.sleep(3.0)

        if not result_file.exists():
            raise RuntimeError(
                f"Codex did not produce result file {result_file.name} "
                f"за {attempts} попыт(ку/ки).\n{problem}"
            )

        with open(result_file, 'r', encoding='utf-8') as f:
            return json.load(f)



# ============================================================================
# MAIN PROCESSOR
# ============================================================================

def offline_archive_query(clip: Dict[str, Any]) -> str:
    """Запрос к архивам БЕЗ LLM — для сцен, вернувшихся из инфографики.

    Звать промптер здесь нельзя: инфографика уже упала на всех генераторах, и
    лишний вызов модели — ещё одна точка отказа. Берём визуальное описание
    сцены и оставляем в нём то, что вообще ищется: место, субъект, эпоху.
    ЦИФРЫ И СТАТИСТИКУ выбрасываем — инфографические сцены полны процентов и
    сумм, а по «47%» архивный поиск отдаёт мусор, и вижн честно бракует всё
    подряд (замечание пользователя 26.08). Десятилетия («1980s», «1985») —
    единственные числа, которые оставляем: они и есть эпоха.
    """
    import re as _re

    text = str(
        clip.get('description') or clip.get('visual_intent')
        or clip.get('voiceover') or clip.get('text') or ''
    ).strip()
    if not text:
        return ''
    words = _re.findall(r"[\w'-]+", text)
    era: List[str] = []
    kept: List[str] = []
    stop = {
        'a', 'an', 'the', 'of', 'in', 'on', 'at', 'to', 'for', 'with', 'and',
        'or', 'as', 'by', 'from', 'into', 'over', 'that', 'this', 'is', 'are',
        'was', 'were', 'be', 'chart', 'graph', 'diagram', 'percent', 'percentage',
        'number', 'numbers', 'share', 'total', 'growth', 'rate', 'data',
        'statistics', 'infographic', 'showing', 'shows', 'displaying', 'depicting',
    }
    for word in words:
        low = word.lower()
        if _re.fullmatch(r'(18|19|20)\d\d s?', low.replace('s', ' s')) or            _re.fullmatch(r'(18|19|20)\d0s', low) or _re.fullmatch(r'(18|19|20)\d\d', low):
            if low not in era:
                era.append(word)
            continue
        if any(ch.isdigit() for ch in low):       # проценты, суммы, счётчики — мимо
            continue
        if low in stop or len(low) < 3:
            continue
        if low not in [k.lower() for k in kept]:
            kept.append(word)
    query = ' '.join(kept[:6] + era[:1])
    return query.strip()


class RealPhotoProcessor:
    """Main entry point for the real-photo stage."""

    def __init__(
        self,
        project_dir: Path,
        target_ratio: float = 0.40,
        min_ratio: float = 0.30,
        max_ratio: float = 0.55,
        sources: Optional[List[str]] = None,
        codex_model: Optional[str] = None,
        target_w: int = 1920,
        target_h: int = 1080,
        fps: int = 60,
    ):
        self.project_dir = Path(project_dir)
        if not self.project_dir.exists():
            raise FileNotFoundError(f"Project directory not found: {self.project_dir}")

        self.target_ratio = target_ratio
        self.min_ratio = min_ratio
        self.max_ratio = max_ratio
        # None means "use defaults"; [] is an intentional EPF-only/no-archive mode.
        self.sources = (
            list(sources)
            if sources is not None
            else ['wikimedia', 'nasa', 'loc', 'archive']
        )
        # Дедуп между клипами. Для wiki — по СТРАНИЦЕ (used_pages): занята статья → берём
        # pageimage следующей по релевантности («иная страница»), а не вторую картинку той же.
        # Для прочих источников (yandex/nasa/...) — по URL картинки (used_urls).
        self.used_urls: set = set()
        self.used_pages: set = set()
        # Резерв под замком. Клипы идут пулом (по числу дорожек EPF), а между
        # «кандидат свободен» (_source_candidates) и «занял» (_pick_and_save)
        # проходят десятки секунд: скачивание и вижн. Без резерва два потока с
        # похожими запросами выбирают ОДИН кадр, и в ролике встают два
        # одинаковых подряд — у стоковой стадии это ловили 28.08 на used_urls.
        self._used_lock = threading.RLock()
        self._reserved: set = set()     # взято в работу, но ещё не подтверждено
        self.target_w = target_w
        self.target_h = target_h
        self.fps = fps

        self.codex = CodexRunner(model_name=codex_model)

        # Project paths
        self.montage_path = self.project_dir / 'montage_plan.json'
        self.transcription_path = self.project_dir / 'transcription.json'
        self.vid_img_dir = self.project_dir / 'vid_img'
        self.generated_dir = self.project_dir / 'generated'
        self.work_dir = self.project_dir / '_real_photo_work'
        self.work_dir.mkdir(exist_ok=True)
        self.credits_path = self.project_dir / 'real_photos_credits.json'
        # Per-project cache: keeps downloads scoped to the project, easy to inspect/delete
        self.cache_dir = self.project_dir / CACHE_DIR_NAME
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # --- Дедуп кадров между клипами (потокобезопасный) -----------------------

    @staticmethod
    def _candidate_key(cand: 'LocalCandidate') -> str:
        """Ключ дедупа кандидата — тот же, что считает `_source_candidates`."""
        archive = getattr(cand, 'archive', None)
        if archive is None:
            return ''                      # EPF-файл: своя папка, дедуп не нужен
        page = getattr(archive, 'page_url', '') or ''
        url = getattr(archive, 'url', '') or ''
        return ((page if cand.source == 'wikimedia' else url) or url)

    def _reserve_candidate(self, key: str) -> bool:
        """Занять кадр на время скачивания и вижна. False — уже у другого клипа."""
        if not key:
            return True                    # без ключа дедупить нечего
        with self._used_lock:
            if (key in self.used_urls or key in self.used_pages
                    or key in self._reserved):
                return False
            self._reserved.add(key)
            return True

    def _release_candidate(self, key: str) -> None:
        """Кадр не пригодился (не скачался, вижн забраковал) — вернуть в оборот."""
        if not key:
            return
        with self._used_lock:
            self._reserved.discard(key)

    def _commit_candidate(self, cand: 'LocalCandidate') -> None:
        """Кадр лёг в клип — закрепить за ним навсегда."""
        archive = getattr(cand, 'archive', None)
        if archive is None:
            return
        page = getattr(archive, 'page_url', '') or ''
        url = getattr(archive, 'url', '') or ''
        with self._used_lock:
            if cand.source == 'wikimedia' and page:
                self.used_pages.add(page)
            if url:
                self.used_urls.add(url)
            self._reserved.discard(self._candidate_key(cand))

    # --- Phase 1: Codex selection -------------------------------------------

    def _build_clip_cards(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Build per-clip cards with voiceover and visual prompt."""
        with open(self.montage_path, 'r', encoding='utf-8') as f:
            montage = json.load(f)
        with open(self.transcription_path, 'r', encoding='utf-8') as f:
            trans = json.load(f)

        # visual_prompt (промпт Pass 2) промптеру запросов НЕ даём: он художественный
        # («Wide shot… photorealistic premium natural-history documentary…») и уводит
        # архивный запрос от реального субъекта в сторону стиля/кинематографии. Источник
        # смысла для запроса — только войсовер (реальные имена/места/факты).
        # `transcription` — список фрагментов со временем. Но у части источников
        # это просто текст целиком: тогда фрагментов нет, и войсовер клипа
        # собирается из слов (`words`), которые в транскрипте есть всегда.
        fragments = trans.get('transcription', [])
        if not isinstance(fragments, list) or (fragments and not isinstance(fragments[0], dict)):
            fragments = [
                w for w in (trans.get('words') or [])
                if isinstance(w, dict) and w.get('start') is not None
            ]
        clips = montage.get('clips', [])

        cards = []
        for clip in clips:
            cs = clip.get('startTime', 0)
            ce = clip.get('endTime', 0)
            voiceover = ' '.join(
                str(f.get('text') or '') for f in fragments
                if isinstance(f, dict)
                and float(f.get('start') or 0) < ce and float(f.get('end') or 0) > cs
            ).strip() or '(pause)'
            cards.append({
                'id': clip['id'],
                'voiceover': voiceover[:600],
                'startTime': cs,
                'endTime': ce,
                **{
                    key: clip[key]
                    for key in (
                        'entities', 'resolved_entities', 'resolved_facts',
                        'hidden_entities', 'revealed_entities', 'scene_role',
                        'chain_id', 'reveal_state', 'visual_intent',
                        'visual_description', 'visual_prompt', 'prompt',
                        'description',
                    )
                    if key in clip
                },
            })

        cards = build_context_cards(cards, fragments, montage)
        # Detect language from transcription / config
        language = trans.get('language') or trans.get('language_code') or getattr(config, 'ASSEMBLYAI_LANGUAGE_CODE', 'en')
        # Тема ролика (якорь запросов) — Pass 1 кладёт в montage_plan.theme. Опционально:
        # нет темы → промптер работает без якоря (graceful).
        theme = (montage.get('theme') or montage.get('montage_plan', {}).get('theme', '') or '').strip()
        meta = {
            'project': self.project_dir.name,
            'audio_duration_sec': montage.get('audioDuration', 0),
            'total_clips': len(clips),
            'target_ratio_pct': int(self.target_ratio * 100),
            'min_ratio_pct': int(self.min_ratio * 100),
            'max_ratio_pct': int(self.max_ratio * 100),
            'language': language,
            'theme': theme,
        }
        return cards, meta

    @staticmethod
    def _parse_prompts_file(path: Path) -> Dict[int, str]:
        """Parse prompts file: each clip is delimited by '=== CLIP N ===' or 'CLIP N:'."""
        text = path.read_text(encoding='utf-8', errors='replace')
        result: Dict[int, str] = {}
        # Try multiple delimiter patterns
        patterns = [
            r'(?:^|\n)#?\s*CLIP\s+(\d+)\s*[:=].*?\n(.*?)(?=\n#?\s*CLIP\s+\d+|\Z)',
            r'(?:^|\n)===\s*CLIP\s+(\d+).*?\s*===\s*\n(.*?)(?=\n===\s*CLIP|\Z)',
            r'(?:^|\n)\[CLIP\s+(\d+)\]\s*\n(.*?)(?=\n\[CLIP|\Z)',
        ]
        for pat in patterns:
            for m in re.finditer(pat, text, flags=re.DOTALL | re.IGNORECASE):
                cid = int(m.group(1))
                if cid not in result:
                    result[cid] = m.group(2).strip()[:1000]
            if result:
                break
        return result

    def select_clips(self, cards: List[Dict[str, Any]], meta: Dict[str, Any]) -> List[ClipSelection]:
        """Полный селектор (Path B): САМ решает клипы + пишет запросы. Страховка на случай,
        когда режиссёр не разметил (напр. standalone-запуск на старом проекте или сбой Pass 1).

        Роутится по REAL_PHOTO_PROMPT_PROVIDER — тем же оператором, что и slim-селектор
        (codex/claude_code — CLI-агенты по файлам; deepseek/openrouter/claude_api —
        прямой inline chat). Раньше был жёстко прибит к Codex и падал, если выбран другой
        провайдер, а Codex не авторизован."""
        _provider = (getattr(config, 'REAL_PHOTO_PROMPT_PROVIDER', 'codex') or 'codex').lower()
        if _provider == 'claude_code':
            return self._claude_code_select_clips(cards, meta)
        if _provider in (
            'deepseek', 'openrouter', 'meta_muse', 'claude_api',
            'codex_reseller', 'gpt_nexus',
        ):
            return self._chat_select_clips(cards, meta, _provider)
        return self._codex_select_clips(cards, meta)

    def _finalize_legacy_selections(self, selected: Any, meta: Dict[str, Any],
                                    query_provider: str) -> List[ClipSelection]:
        """selected[] → List[ClipSelection] + потолок max_ratio. Общий хвост всех веток."""
        selections = [
            ClipSelection(
                clip_id=int(s['clip_id']),
                search_query=str(s.get('search_query') or '').strip(),
                search_query_local=(s.get('search_query_local') or '').strip(),
                preferred_type=s.get('preferred_type', 'photo'),
                preferred_sources=s.get('preferred_sources', self.sources),
                reasoning=s.get('reasoning', ''),
                confidence=s.get('confidence', 'medium'),
                query_provider=query_provider,
                query_path='legacy',
            )
            for s in (selected or []) if str(s.get('search_query') or '').strip()
        ]
        max_count = int(meta['total_clips'] * self.max_ratio)
        if len(selections) > max_count:
            confidence_order = {'high': 0, 'medium': 1, 'low': 2}
            selections.sort(key=lambda s: confidence_order.get(s.confidence, 3))
            selections = selections[:max_count]
        return selections

    def _selector_rules_text(self, meta: Dict[str, Any]) -> str:
        """Правила отбора из real_photo_selector.txt БЕЗ файловых секций — для chat-ветки
        (модель не имеет доступа к ФС, данные передаём inline)."""
        try:
            t = (Path(__file__).parent.parent / 'instructions' / 'real_photo_selector.txt').read_text(encoding='utf-8')
            t = t.split('# WORKING DIRECTORY')[0].strip()
            return (t.replace('{{TARGET_RATIO_PCT}}', str(meta['target_ratio_pct']))
                     .replace('{{MIN_RATIO_PCT}}', str(meta['min_ratio_pct']))
                     .replace('{{MAX_RATIO_PCT}}', str(meta['max_ratio_pct'])))
        except Exception:
            return ("Pick clips with a concrete real-photographable subject (named people/places "
                    "first, then historical, then generic). Skip un-photographable ones. Write one "
                    "English search query per picked clip.")

    def _chat_select_clips(self, cards: List[Dict[str, Any]], meta: Dict[str, Any],
                           provider: str) -> List[ClipSelection]:
        """Легаси-селектор через chat-провайдера (inline, без файлов). Провал → [] (real_photo
        просто ничего не делает, пайплайн живёт)."""
        from units import llm_gateway as chat_client
        model = self._prompt_model_for(provider)
        _by = f'{provider}/{model}'
        rules = self._selector_rules_text(meta)
        _theme = (meta.get('theme') or '').strip()
        theme_block = f"Video theme (background context): {_theme}\n\n" if _theme else ""
        clips_payload = []
        for card in cards:
            item = {'id': card['id'], 'voiceover': (card.get('voiceover') or '')[:400]}
            context = compact_context(card)
            if context:
                item['context'] = context
            clips_payload.append(item)
        task = (
            f"{rules}\n\n{theme_block}"
            f"## CLIPS (inline JSON)\n{json.dumps({'clips': clips_payload}, ensure_ascii=False)}\n\n"
            f"## LANGUAGE HINT\n{meta.get('language', 'en')}\n\n"
            "## OUTPUT\nReturn STRICT JSON only (no markdown, no commentary):\n"
            '{"selected":[{"clip_id":<int>,"search_query":"<specific English query>",'
            '"search_query_local":"<same in voiceover language or empty>",'
            '"preferred_type":"photo","confidence":"high|medium|low"}]}\n'
            "Pick by priority (named people/places first, then historical, then generic). "
            "Skip only genuinely un-photographable clips. Empty selected array is allowed."
        )
        try:
            raw = chat_client.chat_completion(
                provider, model, [{"role": "user", "content": task}],
                json_mode=True, timeout=getattr(config, 'GPT_DIRECTOR_TIMEOUT', 600))
        except Exception as e:
            logger.warning(f"  Chat legacy selector ({_by}) failed: {e} — real_photo skipped")
            return []
        data = self._parse_json_loose(raw)
        selected = data.get('selected') if isinstance(data, dict) else None
        sels = self._finalize_legacy_selections(selected, meta, _by)
        logger.info(f"  Legacy selector via {_by}: picked {len(sels)}/{len(cards)}")
        return sels

    def _claude_code_select_clips(self, cards: List[Dict[str, Any]],
                                  meta: Dict[str, Any]) -> List[ClipSelection]:
        """Легаси-селектор через Claude Code CLI (агент, файлы). Провал → []."""
        try:
            from units.claudecli_runner import ClaudeCodeRunner, load_json_file
        except Exception as e:
            logger.warning(f"  Claude Code runner import failed: {e} — real_photo skipped")
            return []
        (self.work_dir / 'selector_input.json').write_text(
            json.dumps({'clips': cards}, ensure_ascii=False, indent=2), encoding='utf-8')
        (self.work_dir / 'selector_meta.json').write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
        output_file = self.work_dir / 'real_photo_selections.json'
        if output_file.exists():
            output_file.unlink()
        prompt_template = (Path(__file__).parent.parent / 'instructions' / 'real_photo_selector.txt').read_text(encoding='utf-8')
        prompt = (prompt_template
                  .replace('{{TARGET_RATIO_PCT}}', str(meta['target_ratio_pct']))
                  .replace('{{MIN_RATIO_PCT}}', str(meta['min_ratio_pct']))
                  .replace('{{MAX_RATIO_PCT}}', str(meta['max_ratio_pct']))
                  .replace('{{WORKING_DIR}}', self.work_dir.as_posix())
                  .replace('{{OUTPUT_PATH}}', output_file.as_posix()))
        task_file = self.work_dir / '_rp_selector_task.md'
        task_file.write_text(prompt, encoding='utf-8')
        _model = self._prompt_model_for('claude_code')
        _by = f'claude_code/{_model}'
        try:
            runner = ClaudeCodeRunner(model_name=_model)
            runner.run_task_file(task_file, self.work_dir, label='rp_selector')
        except Exception as e:
            logger.warning(f"  Claude Code legacy selector failed: {e} — real_photo skipped")
            return []
        if not output_file.exists():
            logger.warning("  Claude Code: selections file missing — real_photo skipped")
            return []
        try:
            data = load_json_file(output_file)
        except Exception:
            data = self._parse_json_loose(output_file.read_text(encoding='utf-8', errors='replace'))
        return self._finalize_legacy_selections((data or {}).get('selected'), meta, _by)

    def _codex_select_clips(self, cards: List[Dict[str, Any]], meta: Dict[str, Any]) -> List[ClipSelection]:
        """Codex-путь легаси-селектора (агент читает файлы из work_dir, пишет selections).
        Провал → raise (ловит cli.py → «Selector failed, continuing pipeline»)."""
        sel_input = self.work_dir / 'selector_input.json'
        sel_meta = self.work_dir / 'selector_meta.json'
        sel_input.write_text(json.dumps({'clips': cards}, ensure_ascii=False, indent=2), encoding='utf-8')
        sel_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')

        output_file = self.work_dir / 'real_photo_selections.json'
        if output_file.exists():
            output_file.unlink()

        prompt_template = (Path(__file__).parent.parent / 'instructions' / 'real_photo_selector.txt').read_text(encoding='utf-8')
        prompt = (prompt_template
                  .replace('{{TARGET_RATIO_PCT}}', str(meta['target_ratio_pct']))
                  .replace('{{MIN_RATIO_PCT}}', str(meta['min_ratio_pct']))
                  .replace('{{MAX_RATIO_PCT}}', str(meta['max_ratio_pct']))
                  .replace('{{WORKING_DIR}}', self.work_dir.as_posix())
                  .replace('{{OUTPUT_PATH}}', output_file.as_posix()))

        control_file = self.work_dir / '_codex_real_photo_control.json'
        ctrl = self.codex.run(
            task_prompt=prompt,
            schema_file='real_photo_control.json',
            result_file=control_file,
            working_dir=self.work_dir,
        )

        if ctrl.get('status') != 'ok':
            raise RuntimeError(f"Codex selector returned error: {ctrl.get('error_message')}")

        sel_path = Path(ctrl['selections_path'])
        if not sel_path.is_absolute():
            sel_path = self.work_dir / sel_path
        if not sel_path.exists():
            sel_path = output_file  # fallback
        if not sel_path.exists():
            raise RuntimeError(f"Selections file not found: {sel_path}")

        with open(sel_path, 'r', encoding='utf-8') as f:
            sel_data = json.load(f)

        return self._finalize_legacy_selections(
            sel_data.get('selected'), meta, f'codex/{self.codex.model_name}')

    # --- Phase 2: Per-clip processing ---------------------------------------

    def process_clip(self, sel: ClipSelection, clip_meta: Dict[str, Any]) -> ClipResult:
        """Search → download photo → save to generated/{id:03d}.{ext}.

        Два режима (25.08, по просьбе пользователя):

        • КАСКАД (по умолчанию). Источник за источником в порядке списка из GUI —
          и EPF теперь в этом же списке, на позиции, которую задал пользователь
          (раньше он был безусловно первым). У каждого источника берётся до
          REAL_PHOTO_VISION_CANDIDATES кандидатов, вижн судит их своим листом:
          выбрал → клип закрыт, каскад остановлен; забраковал всех → следующий
          источник. Дедуп wiki — по СТРАНИЦЕ, прочие — по URL картинки.
          Если Yandex НЕ включён и источники исчерпаны → переформулировка запроса.

        • МИКС (REAL_PHOTO_SOURCE_MIX, галка «Микс источников»). Кандидаты
          собираются со ВСЕХ включённых источников сразу (включая EPF) и идут
          вижну ОДНИМ контактным листом: он сравнивает их между собой и берёт
          лучшее, а не первое подошедшее. Забраковал всех → клип уходит на
          AI-генерацию. Дороже по скачиваниям, зато один вижн-вызов на клип."""
        clip_id = sel.clip_id

        sources = [s for s in (sel.preferred_sources or self.sources) if s in self.sources] or self.sources

        queries = [sel.search_query]
        if sel.search_query_local and sel.search_query_local.strip():
            queries.append(sel.search_query_local.strip())

        attempt = {'n': 0}      # номер захода: артефакты вижна не должны затираться

        def _cascade(query_list: List[str]) -> Optional[ClipResult]:
            """Источник за источником ПО ПРИОРИТЕТУ; СТОП на первом, что дал фото."""
            attempt['n'] += 1
            for src in sources:
                cands = self._source_candidates(src, sel, clip_meta, query_list)
                if not cands:
                    continue
                r = self._pick_and_save(cands, sel, clip_meta, stage=src, attempt=attempt['n'])
                if r is not None:
                    return r
                logger.info(f"Clip {clip_id}: {src} — вижн забраковал всех, следующий источник")
            return None

        def _mixed(query_list: List[str]) -> Optional[ClipResult]:
            """Все источники → общий лист → один вердикт."""
            attempt['n'] += 1
            collected: List[List[LocalCandidate]] = []
            for src in sources:
                cands = self._source_candidates(src, sel, clip_meta, query_list)
                if cands:
                    collected.append(cands)
            if not collected:
                return None
            merged = _interleave_candidates(collected, _mix_max_candidates())
            # Лист обрезан по потолку, и хвосты источников в него не попали.
            # Их резерв надо снять здесь: `_pick_and_save` увидит только merged
            # и об остальных не узнает, а зарезервированный кадр остался бы
            # «занятым» до конца прогона и пропал для соседних клипов.
            _kept = {id(c) for c in merged}
            for _bucket in collected:
                for _cand in _bucket:
                    if id(_cand) not in _kept:
                        self._release_candidate(self._candidate_key(_cand))
            logger.info(
                f"Clip {clip_id}: микс — {len(merged)} кандидатов из "
                f"{len({c.source for c in merged})} источников на общем листе")
            return self._pick_and_save(merged, sel, clip_meta, stage='mix', attempt=attempt['n'])

        run = _mixed if bool(getattr(config, 'REAL_PHOTO_SOURCE_MIX', False)) else _cascade
        result = run(queries)

        # `result is None` — КАДР НЕ ВЫБРАН. Это НЕ значит «поиск ничего не нашёл»:
        # чаще источники приносят кандидатов, а вижн бракует их всех, и результата
        # всё равно нет. Оба пути ведут сюда, поэтому по логу этой строки нельзя
        # судить, была ли выдача пустой — смотри `vision/NNN_*/selection.json`
        # (26.08: клип 1 — EPF скачал три молоковоза, вижн отверг все три).
        # Yandex в списке источников делает переформулировку лишней: он на своей
        # позиции почти всегда что-то отдаёт.
        # Повтор идёт ТЕМ ЖЕ режимом, что и первый заход (микс остаётся миксом —
        # улов живого теста 25.08: раньше здесь был жёсткий вызов каскада).
        if result is None and 'yandex' not in sources:
            for alt in self._codex_fallback_queries(sel, max_alternatives=2):
                if alt in queries:
                    continue
                logger.info(f"      переформулировка (Yandex выкл): '{alt}'")
                result = run([alt])
                if result is not None:
                    break

        if result is None:
            # Последний рубеж — Wikimedia, ДАЖЕ если её не включали в прогон.
            # Свободная энциклопедия почти всегда даёт хоть что-то по месту,
            # эпохе или предмету, а в режиме «только не-AI» никакой генерации
            # за спиной нет: без этого шага сцена доживает до склейки пустой
            # (требование пользователя 26.08).
            if 'wikimedia' not in sources:
                logger.info(f"      клип {clip_id}: источники исчерпаны — "
                            f"последняя попытка по Wikimedia (вне списка прогона)")
                for query in queries:
                    cands = self._source_candidates('wikimedia', sel, clip_meta, [query])
                    if not cands:
                        continue
                    attempt['n'] += 1
                    result = self._pick_and_save(cands, sel, clip_meta,
                                                 stage='wikimedia_last',
                                                 attempt=attempt['n'])
                    if result is not None:
                        logger.info(f"      клип {clip_id}: закрыт Wikimedia "
                                    f"на последнем рубеже")
                        return result

        if result is None:
            # Ни один источник, ни Wikimedia не дали кадра. В режиме «только
            # не-AI» обычной генерации за спиной нет, поэтому сцену забирает
            # ИНФОГРАФИКА — единственная оставшаяся статическая ветка
            # (требование пользователя 26.08). В обычном режиме поведение
            # прежнее: клип освободится под обычную генерацию.
            from units.real_source_policy import enabled as _non_ai_enabled

            # Сцена, уже побывавшую в инфографике и вернувшуюся оттуда (все
            # генераторы легли), обратно НЕ отправляем: иначе петля
            # EPF → Wikimedia → инфографика → EPF. Она проходит архивы и
            # Wikimedia ещё раз — вдруг источник ответил, — и на этом всё.
            _been_in_infographic = bool((clip_meta or {}).get('infographic_failed'))
            if _non_ai_enabled() and not _been_in_infographic:
                logger.info(f"      клип {clip_id}: архивы и Wikimedia пусты — "
                            f"сцена уходит инфографике")
                return ClipResult(clip_id=clip_id, status='to_infographic',
                                  error=f"No matches for '{sel.search_query}'")
            if _been_in_infographic:
                logger.warning(f"      клип {clip_id}: инфографика уже не смогла, "
                               f"архивы и Wikimedia тоже — сцена остаётся без кадра")
            return ClipResult(clip_id=clip_id, status='no_match',
                              error=f"No matches for '{sel.search_query}'")
        return result

    # ── кандидаты источников и общий выбор (25.08) ──────────────────────────
    def _source_candidates(self, src: str, sel: ClipSelection, clip_meta: Dict[str, Any],
                           query_list: List[str]) -> List[LocalCandidate]:
        """Опросить ОДИН источник и СКАЧАТЬ его кандидатов (без выбора).

        Возвращает локальные файлы: их судит `_pick_and_save` — либо листом
        этого источника (каскад), либо общим листом (микс). EPF здесь такой же
        источник, как архивы: его позицию в очереди задаёт пользователь."""
        limit = max(1, int(getattr(config, 'REAL_PHOTO_VISION_CANDIDATES', 4) or 1))
        clip_id = sel.clip_id
        if src == 'epf':
            return self._epf_candidates(sel, limit)

        fn = SEARCH_FUNCS.get(src)
        if not fn:
            return []
        is_wiki = (src == 'wikimedia')
        depth = int(getattr(config, 'REAL_PHOTO_SEARCH_DEPTH', 6) or 6)
        out: List[LocalCandidate] = []
        seen: set = set()
        # Слоты делятся между запросами ПОПОЛАМ (решение пользователя 25.08).
        # Раньше цикл просто набирал лимит первым запросом — а поиск всегда что-то
        # отдаёт, поэтому запрос на языке озвучки не использовался НИКОГДА: на
        # польской теме английский приносил Барбру Стрейзанд, а польский —
        # Maryla Rodowicz, и до него очередь не доходила. Остаток от нечётного
        # деления достаётся первому (английскому) запросу.
        queries = [q for q in query_list if str(q or '').strip()]
        share = max(1, limit // max(1, len(queries)))
        quotas = [share] * len(queries)
        for i in range(limit - share * len(queries)):
            quotas[i % len(quotas)] += 1
        for q, quota in zip(queries, quotas):
            target = min(limit, len(out) + quota)
            if len(out) >= limit:
                break
            try:
                hits = [h for h in fn(q, media_type='photo', limit=depth) if h.license_ok]
            except Exception as e:
                logger.warning(f"Source {src} failed for '{q}': {e}")
                continue
            for h in hits:
                if len(out) >= target:
                    break
                key = (h.page_url if is_wiki else h.url) or h.url
                if not key or key in seen:
                    continue
                # Резерв вместо простой проверки «занято»: для wiki ключ — СТРАНИЦА
                # (занята статья → берём следующую по релевантности), для прочих —
                # адрес картинки. Держится до конца отбора, потом кадр либо
                # закрепляется за клипом, либо возвращается в оборот.
                if not self._reserve_candidate(key):
                    continue          # кадр уже взят другим клипом
                seen.add(key)
                url_path = urllib.parse.urlparse(h.url).path.lower()
                ext = Path(url_path).suffix.lstrip('.').lower()
                if ext not in ('jpg', 'jpeg', 'png', 'webp'):
                    ext = 'jpg'
                cache_file = _cache_path(self.cache_dir, h.source, h.title or h.url, ext)
                if not cache_file.exists():
                    # Имя в кэше считается от адреса, поэтому два клипа, нашедшие
                    # один кадр, метили бы в один файл. Замок на имя: второй ждёт
                    # и забирает уже скачанное, а не пишет поверх наполовину.
                    with _cache_file_lock(cache_file):
                        if not cache_file.exists():
                            try:
                                ok, _ = _http_download(h.url, cache_file)
                            except Exception as exc:  # noqa: BLE001
                                ok = False
                                logger.info(f"Clip {clip_id}: кандидат не скачался ({src}): {exc}")
                            if (not ok or not cache_file.exists()
                                    or cache_file.stat().st_size < 1024):
                                self._release_candidate(key)
                                continue
                out.append(LocalCandidate(
                    path=cache_file, source=src, title=(h.title or '')[:90],
                    page_url=h.page_url, license=h.license, archive=h))
        return out

    def _epf_candidates(self, sel: ClipSelection, limit: int) -> List[LocalCandidate]:
        """EPF как источник: запустить поиск на клип и забрать скачанные файлы.

        Ищет ОБЕИМИ фразами — английской и на языке озвучки, слоты пополам
        (решение пользователя 25.08). Раньше локальная фраза только сохранялась
        рядом в `search_phrase_local.txt` и в поиск не шла. Цена — второй запуск
        EPF на клип (~30-40 с). Выбор делает общий вижн, не мост."""
        if not bool(getattr(config, 'REAL_PHOTO_EPF_BRIDGE', False)):
            return []
        try:
            from helpers.epf_link import download_candidates
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"  [EPF] мост недоступен: {exc}")
            return []

        phrases = [str(sel.search_query or '').strip()]
        local = str(getattr(sel, 'search_query_local', '') or '').strip()
        if local and local.lower() != phrases[0].lower():
            phrases.append(local)
        phrases = [p for p in phrases if p]
        share = max(1, limit // len(phrases)) if phrases else limit
        out: List[LocalCandidate] = []
        seen_names: set = set()
        for index, phrase in enumerate(phrases):
            if len(out) >= limit:
                break
            quota = share if index < len(phrases) - 1 else max(1, limit - len(out))
            variant = ClipSelection(
                clip_id=sel.clip_id, search_query=phrase,
                preferred_type=sel.preferred_type, preferred_sources=sel.preferred_sources,
                reasoning=sel.reasoning, confidence=sel.confidence,
                context_text=getattr(sel, 'context_text', ''),
            )
            try:
                paths, _clip_dir = download_candidates(
                    self.project_dir, variant,
                    max_files=int(getattr(config, 'REAL_PHOTO_EPF_CANDIDATES', 12) or 12),
                    timeout_sec=int(getattr(config, 'REAL_PHOTO_EPF_SEARCH_TIMEOUT', 180) or 180),
                    stop_event=getattr(self, '_stop_event', None),
                    deadline_ts=getattr(self, '_deadline_ts', None),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"  [EPF] поиск клипа {sel.clip_id} не удался ('{phrase[:40]}'): {exc}")
                continue
            taken = 0
            for path in paths:
                if taken >= quota or len(out) >= limit:
                    break
                if path.name in seen_names:
                    continue          # одна и та же картинка нашлась обеими фразами
                seen_names.add(path.name)
                out.append(LocalCandidate(path=path, source='epf', title=path.name[:90],
                                          license='unknown', render_contained=True))
                taken += 1
        return out

    def _pick_and_save(self, candidates: List[LocalCandidate], sel: ClipSelection,
                       clip_meta: Dict[str, Any], *, stage: str,
                       attempt: int = 1) -> Optional[ClipResult]:
        """Общий вижн-отбор (`units.vision_picker`) + сохранение победителя.

        None — вижн забраковал ВСЕХ (каскад идёт к следующему источнику; в миксе
        клип уходит на AI-генерацию). Вижн выключен (0 кандидатов), провайдер не
        настроен или упал → берём первого кандидата, как было до вижна.
        Артефакты: <project>/_real_photo_work/vision/NNN_<stage>/."""
        if not candidates:
            return None
        clip_id = sel.clip_id
        # Мелкие кадры выбывают ДО отбора: на панели контактного листа они
        # выглядят нормально, а на 1080p с Ken Burns это мыло во весь экран
        # (улов прогона 25.08 — сцена получила кадр 239x317). Если после отсева
        # не осталось никого, работаем с исходным списком: пустая сцена хуже
        # мелкого кадра, и её судьбу решит вижн.
        from units.vision_picker import drop_tiny_images

        big_paths, tiny = drop_tiny_images([c.path for c in candidates])
        if tiny:
            logger.info("      клип %s: отброшено мелких кадров %s (%s)",
                        clip_id, len(tiny), ", ".join(size for _, size in tiny[:4]))
            big_set = {str(path) for path in big_paths}
            filtered = [c for c in candidates if str(c.path) in big_set]
            if filtered:
                candidates = filtered
        n_vision = int(getattr(config, 'REAL_PHOTO_VISION_CANDIDATES', 0) or 0)
        chosen: Optional[LocalCandidate] = None

        if n_vision <= 0 or len(candidates) == 1:
            chosen = candidates[0]
        else:
            from units.vision_picker import Candidate, pick_with_vision, resolve_vision_provider
            provider, model = resolve_vision_provider('real_photo')
            if not provider or not model:
                logger.warning(
                    f"Clip {clip_id}: вижн не настроен (REAL_PHOTO_VISION_PROVIDER/MODEL) "
                    f"— беру первого кандидата из {len(candidates)}")
                chosen = candidates[0]
            else:
                # Заход в имени папки: переформулировка запроса не должна затирать
                # вердикт первого захода (улов разбора 25.08).
                suffix = '' if attempt <= 1 else f'_{attempt}'
                work = self.project_dir / '_real_photo_work' / 'vision' / f'{clip_id:03d}_{stage}{suffix}'
                work.mkdir(parents=True, exist_ok=True)
                # Вижну — только НАЗВАНИЕ кандидата. Имя источника (wikimedia,
                # epf…) в подписи было бы подсказкой «этому можно доверять»,
                # а не фактом о кадре; для разбора оно остаётся в selection.json
                # через meta (замечание пользователя 25.08).
                numbered = [
                    (Candidate(candidate_id=f'C{index}', path=cand.path, kind='photo',
                               title=str(cand.title or '')[:90],
                               meta={'source': cand.source, 'page_url': cand.page_url,
                                     'license': cand.license}), cand)
                    for index, cand in enumerate(candidates, 1)
                ]
                brief = (getattr(sel, 'context_text', '') or '').strip()
                forbidden: List[str] = []
                if not brief:
                    brief = str(clip_meta.get('visual_intent') or clip_meta.get('description')
                                or clip_meta.get('voiceover') or sel.search_query or '').strip()
                    forbidden = [str(x) for x in (clip_meta.get('hidden_entities') or [])]
                verdict = pick_with_vision(
                    [c for c, _ in numbered], work_dir=work, provider=provider, model=model,
                    scene_brief=brief, forbidden=forbidden, search_query=sel.search_query,
                    media_kind='photo', stage=f'real_photo/{stage}',
                    # Ранжирование, как у стока (01.09): лист здесь СМЕШАННЫЙ —
                    # EPF, Wikimedia, архивы, — и при одном победителе всё
                    # остальное уходило в брак скопом, включая годные кадры.
                    # Теперь второй по оценке подстрахует, если файл победителя
                    # окажется битым или его заберёт соседняя сцена.
                    rank=True,
                )
                if verdict.error:
                    # Вижн не отработал — берём первый ПРИГОДНЫЙ кадр, а не
                    # просто первый по списку. Прогон 26.08, клип 15: лист не
                    # собрался, слепой `candidates[0]` оказался HTML-страницей
                    # под именем .jpg — сцена считалась закрытой, а падало это
                    # уже в склейке, где кадр заменили соседним и он застыл на
                    # 7 секунд. Нет ни одного пригодного — отдаём сцену дальше
                    # по каскаду, это честнее битого файла.
                    chosen = _first_usable(candidates)
                    if chosen is None:
                        logger.warning(
                            f"Clip {clip_id}: вижн не отработал ({verdict.error}) "
                            f"и ни один из {len(candidates)} кандидатов не читается "
                            f"как изображение — сцена уходит следующему источнику")
                        return None
                    logger.warning(
                        f"Clip {clip_id}: вижн не отработал ({verdict.error}) — "
                        f"первый пригодный кандидат ({chosen.source})")
                elif verdict.pick is None:
                    return None
                else:
                    # Идём по РАНГУ вижна: победитель, за ним второе место и так
                    # далее. Битый файл (HTML под именем .jpg и подобное) больше
                    # не роняет сцену к «первому по списку» — подстраховывает
                    # следующий ГОДНЫЙ кадр по оценке вижна.
                    by_id = {c.candidate_id: cand for c, cand in numbered}
                    order = list(verdict.ranked_ids or [verdict.pick])
                    for position, candidate_id in enumerate(order):
                        cand = by_id.get(candidate_id)
                        if cand is None:
                            continue
                        if _first_usable([cand]) is None:
                            logger.warning(
                                f"Clip {clip_id}: кандидат {candidate_id} не читается "
                                f"как изображение — беру следующего по рангу")
                            continue
                        chosen = cand
                        if position:
                            logger.info(
                                f"Clip {clip_id}: победитель не подошёл, взял "
                                f"{position + 1}-е место по оценке вижна")
                        break
                    if chosen is None:
                        # Вижн назвал id, которого нет в листе (см. `vision_bad_id`).
                        chosen = _first_usable(candidates)
                        if chosen is None:
                            logger.warning(
                                f"Clip {clip_id}: вижн назвал неизвестный id "
                                f"{verdict.pick!r}, пригодных кандидатов нет — "
                                f"сцена уходит следующему источнику")
                            return None

        # Победитель → generated/. EPF-файлы вписываем в 16:9 с размытым фоном
        # (так их всегда импортировал мост), архивные — копией, как раньше.
        order = [chosen] + [c for c in candidates if c is not chosen]
        winner: Optional[LocalCandidate] = None
        try:
            for cand in order:
                try:
                    result = self._save_local(cand, clip_id, sel)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"Clip {clip_id}: сохранение не удалось ({cand.source}): {exc}")
                    continue
                if result is not None:
                    self._commit_candidate(cand)
                    winner = cand
                    return result
            return None
        finally:
            # Кадры, не попавшие в клип, возвращаются в оборот. Иначе резерв,
            # взятый при отборе, держал бы их «занятыми» до конца прогона, и
            # соседние клипы не увидели бы годный материал.
            for cand in candidates:
                if cand is winner:
                    continue
                self._release_candidate(self._candidate_key(cand))

    def _save_local(self, cand: LocalCandidate, clip_id: int,
                    sel: ClipSelection) -> Optional[ClipResult]:
        """Скачанный файл → generated/{id:03d}.*; ClipResult или None при сбое."""
        self.generated_dir.mkdir(parents=True, exist_ok=True)
        if cand.render_contained:
            from helpers.epf_link import render_contained_landscape
            target = self.generated_dir / f'{clip_id:03d}.jpg'
            for old in self.generated_dir.glob(f'{clip_id:03d}.*'):
                if old != target:
                    old.unlink(missing_ok=True)
            render_contained_landscape(cand.path, target)
        else:
            ext = cand.path.suffix.lstrip('.').lower() or 'jpg'
            if ext not in ('jpg', 'jpeg', 'png', 'webp'):
                ext = 'jpg'
            target = self.generated_dir / f'{clip_id:03d}.{ext}'
            shutil.copy2(cand.path, target)
        if not target.exists() or target.stat().st_size < 1024:
            return None
        archive = cand.archive or ArchiveResult(
            # В титрах и статистике EPF всегда назывался 'extreme_picture_finder' —
            # ключ источника в списке ('epf') наружу не выносим.
            source='extreme_picture_finder' if cand.source == 'epf' else cand.source,
            title=sel.search_query, url='', page_url=cand.page_url,
            license=cand.license or 'unknown', license_ok=False, media_type='photo',
            description=('Automatic Extreme Picture Finder candidate'
                         if cand.source == 'epf' else ''),
        )
        return ClipResult(
            clip_id=clip_id, status='rendered', media_type='photo',
            output_file=str(target.relative_to(self.project_dir)),
            archive_result=archive,
            size_mb=round(target.stat().st_size / (1024 * 1024), 3),
        )

    def _try_save(self, cand: ArchiveResult, clip_id: int) -> ClipResult:
        """Download photo and save to generated/{id:03d}.{ext}. No ffmpeg.

        LEGACY с 25.08: каскад скачивает кандидатов заранее (`_source_candidates`)
        и сохраняет победителя через `_save_local`. Функция оставлена для внешних
        вызовов и как запасной путь одиночного скачивания."""
        url_path = urllib.parse.urlparse(cand.url).path.lower()
        ext = Path(url_path).suffix.lstrip('.').lower()
        # Normalize: video_concat slideshow fallback accepts png/jpg/jpeg/webp
        if ext not in ('jpg', 'jpeg', 'png', 'webp'):
            ext = 'jpg'

        cache_file = _cache_path(self.cache_dir, cand.source, cand.title or cand.url, ext)
        size_mb = 0.0
        if not cache_file.exists():
            logger.info(f"      ↓ downloading from {cand.source}... ({cand.title[:50] if cand.title else cand.url[-40:]})")
            ok, size_bytes = _http_download(cand.url, cache_file)
            if not ok or cache_file.stat().st_size < 1024:
                return ClipResult(clip_id=clip_id, status='download_failed',
                                  archive_result=cand, error=f"Download failed: {cand.url}")
            size_mb = size_bytes / (1024 * 1024)
            logger.info(f"      ✓ downloaded {size_mb:.1f} MB from {cand.source}")
        else:
            size_mb = cache_file.stat().st_size / (1024 * 1024)
            logger.info(f"      ✓ cache hit from {cand.source} ({size_mb:.1f} MB)")

        # Drop into generated/{id:03d}.{ext} — slideshow fallback in video_concat picks it up
        self.generated_dir.mkdir(parents=True, exist_ok=True)
        target = self.generated_dir / f'{clip_id:03d}.{ext}'
        try:
            shutil.copy2(cache_file, target)
        except Exception as e:
            return ClipResult(clip_id=clip_id, status='render_failed',
                              archive_result=cand, error=f"Copy failed: {e}")

        return ClipResult(clip_id=clip_id, status='rendered', media_type='photo',
                          output_file=str(target.relative_to(self.project_dir)),
                          archive_result=cand, size_mb=size_mb)

    def _codex_fallback_queries(self, sel: ClipSelection, max_alternatives: int = 3) -> List[str]:
        """Переформулировать запрос, когда кадр так и не выбран.

        Зовётся НЕ только при пустой выдаче: чаще источники находят кандидатов, а
        вижн бракует их всех — для стадии это одинаковый исход «кадра нет».
        Пишет запрос ВЫБРАННЫМ промптером (`REAL_PHOTO_PROMPT_PROVIDER`: codex →
        Codex CLI, иначе → chat API). Возвращает список альтернатив, возможно пустой.
        """
        _provider = (getattr(config, 'REAL_PHOTO_PROMPT_PROVIDER', 'codex') or 'codex').lower()
        if _provider in (
            'deepseek', 'openrouter', 'meta_muse', 'claude_api',
            'codex_reseller', 'gpt_nexus',
        ):
            return self._chat_alt_queries(sel, max_alternatives, _provider)
        if _provider == 'claude_code':
            return self._claude_code_alt_queries(sel, max_alternatives)
        try:
            logger.warning(
                "  [архивы] клип %s: кадр не выбран по «%s» (источники: %s) — "
                "переформулирую запрос через %s",
                sel.clip_id, (sel.search_query or '')[:70],
                ','.join(self.sources), _provider)
            fb_dir = self.work_dir / f'fallback_{sel.clip_id:03d}'
            fb_dir.mkdir(exist_ok=True)
            (fb_dir / 'failed_query.json').write_text(
                json.dumps({
                    'primary_query': sel.search_query,
                    'voiceover_excerpt': sel.reasoning,
                    'visual_prompt': '',
                    'language': getattr(self, '_lang_hint', 'en'),
                    'tried_sources': self.sources,
                }, ensure_ascii=False, indent=2),
                encoding='utf-8',
            )

            output_file = fb_dir / 'fallback_queries.json'
            prompt_template = (Path(__file__).parent.parent / 'instructions' / 'real_photo_fallback.txt').read_text(encoding='utf-8')
            prompt = (prompt_template
                      .replace('{{WORKING_DIR}}', fb_dir.as_posix())
                      .replace('{{OUTPUT_PATH}}', output_file.as_posix()))

            control_file = fb_dir / '_codex_fallback_control.json'
            # Use shorter timeout — fallback should be quick
            short_codex = CodexRunner(
                model_name=self.codex.model_name,
                timeout=120,
            )
            ctrl = short_codex.run(
                task_prompt=prompt,
                schema_file='real_photo_fallback_control.json',
                result_file=control_file,
                working_dir=fb_dir,
            )

            if ctrl.get('status') != 'ok':
                logger.warning("  [архивы] клип %s: промптер не переформулировал запрос "
                               "(статус %s) — альтернатив нет",
                               sel.clip_id, ctrl.get('status'))
                return []

            with open(output_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            alternatives = [q.strip() for q in data.get('alternatives', []) if q and q.strip()]
            if alternatives:
                logger.info("  [архивы] клип %s: альтернативные запросы — %s",
                            sel.clip_id, '; '.join(alternatives[:max_alternatives]))
            else:
                logger.warning("  [архивы] клип %s: промптер вернул пустой список альтернатив",
                               sel.clip_id)
            return alternatives[:max_alternatives]
        except Exception as e:
            # Было logger.debug — то есть молча. Стадия уходила в генерацию, а
            # причина не попадала ни в лог прогона, ни куда-либо ещё (улов 26.08).
            logger.warning("  [архивы] клип %s: переформулировка не удалась (%s)",
                           sel.clip_id, e)
            return []

    def update_montage_and_credits(self, results: List[ClipResult]):
        """Resolve `real_photo_pending` markers to final state in montage_plan,
        save credits. Successful clips → `source: real_photo` + real_meta.
        Failed clips → revert marker (so downstream re-runs handle them as AI).

        Пишем ТОЛЬКО свою дельту под locked-ретраем (montage читается свежим внутри
        apply_clip_source_delta), поэтому montage целиком здесь не грузим."""
        rendered_ids = {r.clip_id: r for r in results if r.status == 'rendered'}
        all_attempted = {r.clip_id for r in results}

        # credit_text для плашки video_concat — ТОЛЬКО имя сайта (английское).
        # Лицензия/автор/page_url остаются в real_meta и real_photos_credits.json; в кадр их
        # не выносим: длинная строка «Source / License (лицен…» вылезала за край кадра и
        # обрезалась мусором (drawtext рисует как есть, авто-ширина, без переноса).
        def _credit_text(ar) -> str:
            return {
                'wikimedia': 'Wikimedia', 'nasa': 'NASA',
                'loc': 'Library of Congress', 'archive': 'Internet Archive',
                'met': 'Met Museum', 'europeana': 'Europeana',
                'smithsonian': 'Smithsonian', 'rijksmuseum': 'Rijksmuseum',
                'getty': 'Getty', 'yandex': 'Yandex',
                'openverse': 'Openverse', 'serper': 'Google Images (Serper)', 'brightdata': 'Google Images (Bright Data)',
                'extreme_picture_finder': 'Extreme Picture Finder',
            }.get(ar.source, (ar.source or 'Web').title())

        # Дельта ТОЛЬКО по своим клипам (rendered / attempted-failed). Стоковые клипы
        # не трогаем — их правит сток-стадия параллельно, и общий montage пишется
        # атомарно под locked-ретраем (apply_clip_source_delta), без гонки.
        delta: Dict[int, Dict[str, Any]] = {}
        for cid, r in rendered_ids.items():
            patch: Dict[str, Any] = {'source': 'real_photo'}
            if r.archive_result:
                patch['real_meta'] = {
                    'archive': r.archive_result.source,
                    'title': r.archive_result.title,
                    'license': r.archive_result.license,
                    'author': r.archive_result.author,
                    'page_url': r.archive_result.page_url,
                    'credit_text': _credit_text(r.archive_result),
                }
            delta[int(cid)] = patch
        to_infographic = {r.clip_id for r in results if r.status == 'to_infographic'}
        for cid in all_attempted:
            if cid in rendered_ids:
                continue
            if cid in to_infographic:
                # Архивы и Wikimedia пусты, а нейрогенерации в этом режиме нет:
                # сцену забирает инфографика (требование пользователя 26.08).
                delta[int(cid)] = {'source': 'infographic'}
                logger.info("  [Real Photo] клип %s передан инфографике", cid)
            else:
                # Attempted but failed: снять маркер → downstream догенерит как AI
                delta[int(cid)] = {'source': None}

        # Backup original montage_plan once (снимок до наших правок)
        backup = self.montage_path.with_suffix('.json.before_real_photos')
        if not backup.exists():
            shutil.copy2(self.montage_path, backup)

        from units.source_ledger import apply_clip_source_delta
        apply_clip_source_delta(self.montage_path, delta, log=lambda m: logger.info("  [Real Photo] " + m))

        # Build credits file
        credits = {
            'project': self.project_dir.name,
            'generated_at': datetime.now().isoformat(timespec='seconds'),
            'items': [],
        }
        for r in rendered_ids.values():
            ar = r.archive_result
            if not ar:
                continue
            credits['items'].append({
                'clip_id': r.clip_id,
                'media_type': r.media_type,
                'source': ar.source,
                'title': ar.title,
                'author': ar.author,
                'license': ar.license,
                'page_url': ar.page_url,
            })
        self.credits_path.write_text(
            json.dumps(credits, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )

        # Человекочитаемая версия для описания YouTube — юзеру не копировать из JSON.
        # Дедуп по page_url (одно фото могло уйти в несколько клипов).
        try:
            _site = {
                'wikimedia': 'Wikimedia', 'nasa': 'NASA',
                'loc': 'Library of Congress', 'archive': 'Internet Archive',
                'met': 'Met Museum', 'europeana': 'Europeana',
                'smithsonian': 'Smithsonian', 'rijksmuseum': 'Rijksmuseum',
                'getty': 'Getty', 'yandex': 'Yandex',
                'openverse': 'Openverse', 'serper': 'Google Images (Serper)', 'brightdata': 'Google Images (Bright Data)',
                'extreme_picture_finder': 'Extreme Picture Finder',
            }
            lines, seen = [], set()
            for it in sorted(credits['items'], key=lambda x: x.get('clip_id', 0)):
                key = it.get('page_url') or (it.get('source'), it.get('title'))
                if key in seen:
                    continue
                seen.add(key)
                site = _site.get(it.get('source'), (it.get('source') or 'Web').title())
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
                "# Источники изображений\n\n"
                "Архивные материалы, использованные в видео. Можно вставить в описание ролика.\n\n"
                + ("\n".join(lines) if lines else "_нет архивных изображений_")
                + "\n"
            )
            self.credits_path.with_suffix('.md').write_text(md, encoding='utf-8')
        except Exception as e:
            logger.warning(f"  credits .md write failed (non-fatal): {e}")

    # --- Phase 4: Phase-split orchestration (parallelism-friendly) -----------

    def _read_pre_marked_clip_ids(self) -> List[int]:
        """Read montage_plan.json, return clip_ids that the Director already
        marked as `source: real_photo_pending`. Returns empty list if Pass 1
        did not run with REAL_PHOTO_ENABLED (legacy/manual run path)."""
        try:
            mp = json.loads(self.montage_path.read_text(encoding='utf-8-sig'))
            clips = mp.get('clips') or mp.get('montage_plan', {}).get('clips', [])
            return [c['id'] for c in clips if c.get('source') == 'real_photo_pending']
        except Exception as e:
            logger.warning(f"  Could not read pre-marks from montage_plan: {e}")
            return []

    def _apply_intro_gate(self, pre_marked: List[int]) -> List[int]:
        """Снять маркер `real_photo_pending` с клипов, начинающихся раньше
        REAL_PHOTO_START_SEC (вступительный хук — не для архивного фото).

        Отсеянные клипы теряют source → обычные генераторы их подхватят (дырки нет).
        Возвращает список clip_id, что ОСТАЛИСЬ помечены (startTime >= порога).
        0 или отрицательный порог = гейт выключен, возвращаем pre_marked как есть.
        """
        try:
            from units.real_source_policy import enabled as non_ai_enabled
            if non_ai_enabled():
                return pre_marked
        except Exception:
            pass
        start_sec = float(getattr(config, 'REAL_PHOTO_START_SEC', 0) or 0)
        if start_sec <= 0 or not pre_marked:
            return pre_marked
        try:
            mp = json.loads(self.montage_path.read_text(encoding='utf-8-sig'))
        except Exception as e:
            logger.warning(f"  Intro-gate: не смог прочитать montage_plan ({e}) — пропускаю гейт")
            return pre_marked
        clips = mp.get('clips') or mp.get('montage_plan', {}).get('clips', [])
        marked = set(pre_marked)
        kept: List[int] = []
        gated: List[int] = []
        for c in clips:
            cid = c.get('id')
            if cid in marked and c.get('source') == 'real_photo_pending':
                if float(c.get('startTime', 0) or 0) < start_sec:
                    c.pop('source', None)          # интро → обычная генерация
                    gated.append(cid)
                else:
                    kept.append(cid)
        if gated:
            self.montage_path.write_text(
                json.dumps(mp, ensure_ascii=False, indent=2), encoding='utf-8')
            logger.info(f"  Интро-гейт (<{start_sec:g}с): снял архив-маркер с {len(gated)} клип(ов) "
                        f"{sorted(gated)[:12]} → обычная генерация; осталось {len(kept)} под архив")
        return kept

    def select_phase(self) -> List[ClipSelection]:
        """Determine selections + mark montage_plan as `source: real_photo_pending`.

        Path A (preferred — when Pass 1 Director already marked clips):
          Read montage_plan, take pre-marked clip_ids, call slim Codex
          query-only selector. Director's decision is final, queries are
          generated downstream.

        Path B (fallback — legacy run on project where Pass 1 didn't mark,
        e.g. `run_real_photos.bat` on old project):
          Call the legacy full selector that BOTH decides + generates queries.

        Other stages (FastGen, Veo i2v) check `real_photo_pending` marker and
        skip the clip — they run in parallel with our download phase.
        """
        logger.info(f"Real photo stage starting on {self.project_dir.name}")
        cards, meta = self._build_clip_cards()
        clip_meta_by_id = {c['id']: c for c in cards}
        self._lang_hint = meta.get('language', 'en')
        self._meta = meta
        self._clip_meta_by_id = clip_meta_by_id

        logger.info(f"  Total clips: {meta['total_clips']}, target ratio: {meta['target_ratio_pct']}%")

        pre_marked = self._read_pre_marked_clip_ids()
        # Интро-гейт: клипы раньше REAL_PHOTO_START_SEC не идут в архив (вступительный хук —
        # динамика, не статичное фото с плашкой). Снимаем маркер → обычная генерация.
        if pre_marked:
            pre_marked = self._apply_intro_gate(pre_marked)
            # Path A — Director already decided. Slim selector for queries only.
            _provider = (
                getattr(config, 'REAL_PHOTO_PROMPT_PROVIDER', 'codex') or 'codex'
            ).lower()
            _query_started = time.perf_counter()
            logger.info(
                "  [1/2] Director marked %s clips. Building search queries via %s...",
                len(pre_marked), _provider,
            )
            selections = self.select_queries_for_marked(pre_marked, cards, meta)
            logger.info(
                "  Queries ready for %s/%s clips in %.1fs",
                len(selections), len(pre_marked),
                time.perf_counter() - _query_started,
            )
            # Marker уже стоит у клипов (от Director Pass 1) — _mark_pending не нужен
        else:
            # Path B — legacy fallback: full selector decides + writes queries
            logger.info("  [1/2] No pre-marks from Director — running LEGACY full selector...")
            selections = self.select_clips(cards, meta)
            logger.info(f"  Legacy selector picked {len(selections)} clips "
                        f"({100*len(selections)/max(1, meta['total_clips']):.0f}%)")
            if selections:
                self._mark_pending(selections)

        return selections

    def select_queries_for_marked(
        self,
        pre_marked_ids: List[int],
        cards: List[Dict[str, Any]],
        meta: Dict[str, Any],
    ) -> List[ClipSelection]:
        """Генерация search_query для клипов, помеченных режиссёром (его решение финально).

        Оператор промптажа выбирается ОТДЕЛЬНО от режиссёра (REAL_PHOTO_PROMPT_PROVIDER):
        codex → Codex CLI (агент, файлы); deepseek/openrouter → chat API (inline, надёжнее).
        При провале любого — voiceover-as-query фолбек (graceful degradation).
        """
        # Filter cards to only pre-marked clips
        marked_set = set(pre_marked_ids)
        marked_cards = [c for c in cards if c['id'] in marked_set]
        if not marked_cards:
            return []

        # Сцены, вернувшиеся из инфографики (там легли все генераторы), запрос
        # получают БЕЗ LLM: лишний вызов модели — ещё одна точка отказа, а из
        # инфографического описания нужна визуальная часть без цифр
        # (требование пользователя 26.08).
        offline_ready: List[ClipSelection] = []
        _from_infographic = [c for c in marked_cards if c.get('infographic_failed')]
        if _from_infographic:
            for card in _from_infographic:
                query = offline_archive_query(card)
                logger.info("  [Real Photo] клип %s вернулся из инфографики — "
                            "запрос без LLM: «%s»", card.get('id'), query or '—')
                offline_ready.append(ClipSelection(
                    clip_id=int(card['id']),
                    search_query=query,
                    search_query_local='',
                    preferred_type='photo',
                    preferred_sources=list(self.sources),
                    reasoning='инфографика не смогла; запрос собран без LLM',
                    confidence='medium',
                    query_provider='offline_after_infographic',
                    query_path='offline',
                ))
            marked_cards = [c for c in marked_cards if not c.get('infographic_failed')]
            if not marked_cards:
                return offline_ready

        # Роутинг оператора промптажа (не зависит от выбранного режиссёра):
        #   codex / claude_code → CLI-агенты (файлы); остальные → прямой chat API (inline, надёжнее)
        _provider = (getattr(config, 'REAL_PHOTO_PROMPT_PROVIDER', 'codex') or 'codex').lower()
        if _provider == 'claude_code':
            return offline_ready + self._claude_code_queries_for_marked(marked_cards, meta)
        if _provider in (
            'deepseek', 'openrouter', 'meta_muse', 'claude_api',
            'codex_reseller', 'gpt_nexus',
        ):
            return offline_ready + self._chat_queries_for_marked(marked_cards, meta, _provider)

        # ── Codex-путь: агент читает файлы из work_dir и пишет output-файл ──
        # Write slim input — same format as legacy, but only marked clips
        sel_input = self.work_dir / 'selector_input.json'
        sel_meta = self.work_dir / 'selector_meta.json'
        sel_input.write_text(json.dumps({'clips': marked_cards}, ensure_ascii=False, indent=2), encoding='utf-8')
        sel_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')

        output_file = self.work_dir / 'real_photo_queries.json'
        if output_file.exists():
            output_file.unlink()

        prompt_template = (Path(__file__).parent.parent / 'instructions' / 'real_photo_query_only.txt').read_text(encoding='utf-8')
        prompt = (prompt_template
                  .replace('{{WORKING_DIR}}', self.work_dir.as_posix())
                  .replace('{{OUTPUT_PATH}}', output_file.as_posix()))

        control_file = self.work_dir / '_codex_real_photo_query_control.json'
        try:
            ctrl = self.codex.run(
                task_prompt=prompt,
                schema_file='real_photo_query_control.json',
                result_file=control_file,
                working_dir=self.work_dir,
            )
        except Exception as e:
            logger.warning(f"  Slim Codex query selector failed: {e} — falling back to voiceover-as-query")
            return self._fallback_voiceover_queries(marked_cards, reason=f'codex: {e}')

        if ctrl.get('status') != 'ok':
            logger.warning(f"  Slim selector returned error: {ctrl.get('error_message')} — voiceover fallback")
            return self._fallback_voiceover_queries(
                marked_cards, reason=f"codex: {ctrl.get('error_message')}")

        sel_path = Path(ctrl['selections_path'])
        if not sel_path.is_absolute():
            sel_path = self.work_dir / sel_path
        if not sel_path.exists():
            sel_path = output_file
        if not sel_path.exists():
            logger.warning("  Queries file missing — voiceover fallback")
            return self._fallback_voiceover_queries(marked_cards, reason='codex: queries file missing')

        with open(sel_path, 'r', encoding='utf-8') as f:
            q_data = json.load(f)

        _by = f'codex/{self.codex.model_name}'
        selections = [
            ClipSelection(
                clip_id=s['clip_id'],
                search_query=s['search_query'],
                search_query_local=(s.get('search_query_local') or '').strip(),
                preferred_type='photo',
                preferred_sources=list(self.sources),
                reasoning='Director marked in Pass 1',
                confidence='high',
                query_provider=_by, query_path='primary',
            )
            for s in q_data.get('selected', [])
        ]
        # Edge case: Codex returned fewer queries than requested
        returned_ids = {s.clip_id for s in selections}
        missing = [c for c in marked_cards if c['id'] not in returned_ids]
        if missing:
            logger.warning(f"  Slim selector missed {len(missing)} clips — voiceover fallback for them")
            selections.extend(self._fallback_voiceover_queries(
                missing, reason=f'{_by}: returned fewer clips than requested'))
        return offline_ready + selections

    def _fallback_voiceover_queries(self, cards: List[Dict[str, Any]],
                                    reason: str = '') -> List[ClipSelection]:
        """Last-resort: use raw voiceover as query (most archives accept natural
        language). Quality lower than prompter-generated queries but keeps pipeline
        alive on prompter failure. `reason` — ошибка провайдера (для query-log)."""
        results: List[ClipSelection] = []
        for c in cards:
            vo = (c.get('voiceover') or '').strip()
            # Take first 80 chars as query — most archives tokenize anyway
            query = vo[:80] if vo else f"clip {c['id']}"
            results.append(ClipSelection(
                clip_id=c['id'],
                search_query=query,
                search_query_local='',
                preferred_type='photo',
                preferred_sources=list(self.sources),
                reasoning='voiceover-as-query fallback (prompter failed)',
                confidence='low',
                query_provider='voiceover-fallback', query_path='fallback',
                fallback_reason=reason,
            ))
        return results

    # ── Промптаж через chat-провайдера (inline, без Codex-агента/файлов) ──────

    @staticmethod
    def _prompt_model_for(provider: str) -> str:
        """Модель промптера: явная REAL_PHOTO_PROMPT_MODEL или дефолт выбранного провайдера."""
        explicit = (getattr(config, 'REAL_PHOTO_PROMPT_MODEL', '') or '').strip()
        if explicit:
            return explicit
        if provider == 'deepseek':
            return getattr(config, 'DEEPSEEK_MODEL', '') or 'deepseek-v4-flash'
        if provider == 'openrouter':
            return getattr(config, 'OPENROUTER_MODEL', '') or 'openrouter/free'
        if provider == 'claude_api':
            return getattr(config, 'CLAUDE_API_MODEL', '') or 'kr/claude-sonnet-4.6'
        if provider == 'codex_reseller':
            return getattr(config, 'CODEX_RESELLER_MODEL', '') or 'gpt-5.6-terra'
        if provider == 'gpt_nexus':
            return getattr(config, 'GPT_NEXUS_MODEL', '') or 'gpt-5.5'
        if provider == 'meta_muse':
            return (
                getattr(config, 'META_MUSE_MODEL', '')
                or 'muse-spark-1.2-contributor'
            )
        if provider == 'claude_code':
            return getattr(config, 'CLAUDE_MODEL', '') or 'sonnet'
        return getattr(config, 'REAL_PHOTO_CODEX_MODEL', 'gpt-5.5')

    def _claude_code_queries_for_marked(self, marked_cards: List[Dict[str, Any]],
                                        meta: Dict[str, Any]) -> List[ClipSelection]:
        """Промптаж через Claude Code CLI (агент, как Codex): task-файл → output-файл.
        Требует установленный/авторизованный `claude` CLI. Провал → voiceover-фолбек."""
        _cc_model = self._prompt_model_for('claude_code')
        _by = f'claude_code/{_cc_model}'
        try:
            from units.claudecli_runner import ClaudeCodeRunner, load_json_file
        except Exception as e:
            logger.warning(f"  Claude Code runner import failed: {e} — voiceover fallback")
            return self._fallback_voiceover_queries(marked_cards, reason=f'{_by}: import failed: {e}')

        (self.work_dir / 'selector_input.json').write_text(
            json.dumps({'clips': marked_cards}, ensure_ascii=False, indent=2), encoding='utf-8')
        (self.work_dir / 'selector_meta.json').write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
        output_file = self.work_dir / 'real_photo_queries.json'
        if output_file.exists():
            output_file.unlink()

        prompt_template = (Path(__file__).parent.parent / 'instructions' / 'real_photo_query_only.txt').read_text(encoding='utf-8')
        prompt = (prompt_template
                  .replace('{{WORKING_DIR}}', self.work_dir.as_posix())
                  .replace('{{OUTPUT_PATH}}', output_file.as_posix()))
        task_file = self.work_dir / '_rp_query_task.md'
        task_file.write_text(prompt, encoding='utf-8')

        try:
            runner = ClaudeCodeRunner(model_name=_cc_model)
            runner.run_task_file(task_file, self.work_dir, label='rp_queries')
        except Exception as e:
            logger.warning(f"  Claude Code query selector failed: {e} — voiceover fallback")
            return self._fallback_voiceover_queries(marked_cards, reason=f'{_by}: {e}')

        if not output_file.exists():
            logger.warning("  Claude Code: queries file missing — voiceover fallback")
            return self._fallback_voiceover_queries(marked_cards, reason=f'{_by}: queries file missing')
        try:
            q_data = load_json_file(output_file)
        except Exception:
            q_data = self._parse_json_loose(output_file.read_text(encoding='utf-8', errors='replace'))

        selections = [
            ClipSelection(
                clip_id=int(s['clip_id']),
                search_query=str(s.get('search_query') or '').strip(),
                search_query_local=str(s.get('search_query_local') or '').strip(),
                preferred_type='photo',
                preferred_sources=list(self.sources),
                reasoning='Director marked; query by claude_code',
                confidence='high',
                query_provider=_by, query_path='primary',
            )
            for s in (q_data.get('selected') or []) if str(s.get('search_query') or '').strip()
        ]
        returned = {s.clip_id for s in selections}
        missing = [c for c in marked_cards if c['id'] not in returned]
        if missing:
            logger.warning(f"  Claude Code missed {len(missing)} clips — voiceover fallback for them")
            selections.extend(self._fallback_voiceover_queries(
                missing, reason=f'{_by}: returned fewer clips than requested'))
        return selections

    @staticmethod
    def _query_rules_text() -> str:
        """Правила генерации запросов из real_photo_query_only.txt БЕЗ файловых секций —
        chat-модель не имеет доступа к ФС, данные передаём inline."""
        try:
            t = (Path(__file__).parent.parent / 'instructions' / 'real_photo_query_only.txt').read_text(encoding='utf-8')
            return t.split('# WORKING DIRECTORY')[0].strip()
        except Exception:
            return "Generate one concise English archival search query (2-5 keywords) per clip."

    @staticmethod
    def _parse_json_loose(raw: str) -> Dict[str, Any]:
        """JSON из ответа модели (может быть обёрнут текстом/markdown)."""
        raw = (raw or '').strip()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except Exception:
            pass
        s, e = raw.find('{'), raw.rfind('}')
        if s >= 0 and e > s:
            try:
                return json.loads(raw[s:e + 1])
            except Exception:
                pass
        return {}

    def _chat_queries_for_marked(self, marked_cards: List[Dict[str, Any]],
                                 meta: Dict[str, Any], provider: str) -> List[ClipSelection]:
        """search_query через chat-провайдера (deepseek/openrouter): inline-промпт + строгий
        JSON. Надёжнее Codex-агента (нет CLI, файлов, agentic-поведения). Провал → voiceover-фолбек."""
        from units import llm_gateway as chat_client
        model = self._prompt_model_for(provider)
        clips_payload = []
        for card in marked_cards:
            item = {'id': card['id'], 'voiceover': (card.get('voiceover') or '')[:400]}
            context = compact_context(card)
            if context:
                item['context'] = context
            clips_payload.append(item)
        _theme = (meta.get('theme') or '').strip()
        theme_block = (
            f"Video theme (background context): {_theme}\n"
            "Ground each query in this theme AND the clip's voiceover — the specific subject plus "
            "its own time/setting cues. The clip's own cues WIN over the theme (a modern clip in a "
            "history video → modern query). Make it specific (subject + what pins it down: "
            "setting/period/kind), not one broad word. Add a period only when the context calls "
            "for it — do not assume the subject is old/archival.\n\n"
        ) if _theme else ""
        task = (
            f"{self._query_rules_text()}\n\n"
            f"{theme_block}"
            f"## CLIPS (inline JSON)\n{json.dumps({'clips': clips_payload}, ensure_ascii=False)}\n\n"
            f"## LANGUAGE HINT\n{meta.get('language', 'en')}\n\n"
            "## OUTPUT\nReturn STRICT JSON only (no markdown, no commentary):\n"
            '{"selected":[{"clip_id":<int>,"search_query":"<2-5 English keywords>",'
            '"search_query_local":"<same query in voiceover language, or empty string>"}]}\n'
            "One entry per clip received — do NOT skip clips."
        )
        _by = f'{provider}/{model}'
        try:
            raw = chat_client.chat_completion(
                provider, model, [{"role": "user", "content": task}],
                json_mode=True, timeout=getattr(config, 'GPT_DIRECTOR_TIMEOUT', 600))
        except Exception as e:
            logger.warning(f"  Chat query selector ({_by}) failed: {e} — voiceover fallback")
            return self._fallback_voiceover_queries(marked_cards, reason=f'{_by}: {e}')

        data = self._parse_json_loose(raw)
        selections: List[ClipSelection] = []
        for s in (data.get('selected') or []):
            try:
                q = str(s.get('search_query') or '').strip()
                if not q:
                    continue
                selections.append(ClipSelection(
                    clip_id=int(s['clip_id']),
                    search_query=q,
                    search_query_local=str(s.get('search_query_local') or '').strip(),
                    preferred_type='photo',
                    preferred_sources=list(self.sources),
                    reasoning=f'Director marked; query by {provider}',
                    confidence='high',
                    query_provider=_by, query_path='primary',
                ))
            except Exception:
                continue
        logger.info(f"  Queries via {_by}: {len(selections)}/{len(marked_cards)}")
        returned = {s.clip_id for s in selections}
        missing = [c for c in marked_cards if c['id'] not in returned]
        if missing:
            logger.warning(f"  {provider} missed {len(missing)} clips — voiceover fallback for them")
            selections.extend(self._fallback_voiceover_queries(
                missing, reason=f'{_by}: returned fewer clips than requested'))
        return selections

    def _claude_code_alt_queries(self, sel: ClipSelection, max_alternatives: int = 3) -> List[str]:
        """Альтернативные запросы при 0 hits через Claude Code CLI (агент, тот же промпт что у Codex)."""
        try:
            from units.claudecli_runner import ClaudeCodeRunner, load_json_file
            fb_dir = self.work_dir / f'fallback_{sel.clip_id:03d}'
            fb_dir.mkdir(exist_ok=True)
            (fb_dir / 'failed_query.json').write_text(
                json.dumps({
                    'primary_query': sel.search_query,
                    'voiceover_excerpt': sel.reasoning,
                    'visual_prompt': '',
                    'language': getattr(self, '_lang_hint', 'en'),
                    'tried_sources': self.sources,
                }, ensure_ascii=False, indent=2), encoding='utf-8')
            output_file = fb_dir / 'fallback_queries.json'
            if output_file.exists():
                output_file.unlink()
            prompt_template = (Path(__file__).parent.parent / 'instructions' / 'real_photo_fallback.txt').read_text(encoding='utf-8')
            prompt = (prompt_template
                      .replace('{{WORKING_DIR}}', fb_dir.as_posix())
                      .replace('{{OUTPUT_PATH}}', output_file.as_posix()))
            task_file = fb_dir / '_rp_fallback_task.md'
            task_file.write_text(prompt, encoding='utf-8')
            ClaudeCodeRunner(model_name=self._prompt_model_for('claude_code')).run_task_file(
                task_file, fb_dir, label=f'rp_fb_{sel.clip_id}')
            if not output_file.exists():
                return []
            data = load_json_file(output_file)
            qs = data.get('queries') or data.get('alternatives') or []
            return [str(q).strip() for q in qs if str(q).strip()][:max_alternatives]
        except Exception as e:
            logger.warning(f"  Claude Code alt-queries failed: {e}")
            return []

    def _chat_alt_queries(self, sel: ClipSelection, max_alternatives: int, provider: str) -> List[str]:
        """Альтернативные запросы при 0 hits — через ВЫБРАННОГО промптера (не жёстко Codex)."""
        from units import llm_gateway as chat_client
        model = self._prompt_model_for(provider)
        task = (
            "The archival image search returned NOTHING for this query. Propose alternative English "
            f"search queries (2-5 keywords each, max {max_alternatives}) that would find REAL archival "
            "material on the same subject: broaden or rephrase — drop over-specific tokens, use the main "
            "named entity, or a well-known related event/place/person.\n"
            "Avoid AI/3D-render wording. Period engravings, paintings, maps and schematics are acceptable "
            "for pre-photography subjects.\n\n"
            f"## FAILED QUERY\n{sel.search_query}\n\n"
            '## OUTPUT\nSTRICT JSON only: {"queries":["q1","q2"]}'
        )
        try:
            raw = chat_client.chat_completion(
                provider, model, [{"role": "user", "content": task}],
                json_mode=True, timeout=120)
        except Exception as e:
            logger.warning(f"  Chat alt-queries ({provider}/{model}) failed: {e}")
            return []
        data = self._parse_json_loose(raw)
        return [str(q).strip() for q in (data.get('queries') or []) if str(q).strip()][:max_alternatives]

    def _run_epf_automatic(
        self,
        selections: List[ClipSelection],
        *,
        stop_event=None,
        deadline_ts=None,
    ) -> tuple[List[ClipResult], set[int]]:
        """Run EPF from Real Photo queries; leave misses for the archive cascade.

        LEGACY с 25.08: пакетный проход «EPF впереди всех» заменён участием EPF
        в общем каскаде/миксе (`process_clip`), где его приоритет задаёт список
        источников. Функция оставлена для ручных/внешних запусков моста."""
        from helpers.epf_link import run_automatic_job

        provider = getattr(config, 'REAL_PHOTO_VISION_PROVIDER', 'codex')
        model = getattr(config, 'REAL_PHOTO_VISION_MODEL', '')
        provider_label = 'Claude Nexus' if provider == 'claude_api' else provider
        max_files = getattr(config, 'REAL_PHOTO_EPF_CANDIDATES', 12)
        timeout_sec = getattr(config, 'REAL_PHOTO_EPF_SEARCH_TIMEOUT', 180)
        logger.info(
            "  [EPF bridge] Automatic search for %s Real Photo queries "
            "(%s/%s, %s candidates, %ss each)",
            len(selections), provider_label, model, max_files, timeout_sec,
        )
        summary = run_automatic_job(
            self.project_dir,
            selections,
            provider=provider,
            model=model,
            max_files_per_clip=max_files,
            timeout_sec=timeout_sec,
            stop_event=stop_event,
            deadline_ts=deadline_ts,
        )
        selections_by_id = {selection.clip_id: selection for selection in selections}
        rendered: List[ClipResult] = []
        rendered_ids: set[int] = set()
        for item in summary.get('ingest', {}).get('results', []):
            if item.get('status') != 'ingested':
                continue
            clip_id = int(item['clip_id'])
            selection = selections_by_id.get(clip_id)
            target = Path(item.get('file') or '')
            if selection is None or not target.is_file():
                continue
            rendered_ids.add(clip_id)
            rendered.append(ClipResult(
                clip_id=clip_id,
                status='rendered',
                media_type='photo',
                output_file=str(target),
                archive_result=ArchiveResult(
                    source='extreme_picture_finder',
                    title=selection.search_query,
                    url='',
                    page_url='',
                    license='unknown',
                    license_ok=False,
                    media_type='photo',
                    description=(
                        'Automatic Extreme Picture Finder candidate selected by '
                        f'{provider_label}/{model}'
                    ),
                    raw={
                        'bridge_job': summary.get('job_dir', ''),
                        'source_file': item.get('source_file', ''),
                    },
                ),
                size_mb=round(target.stat().st_size / (1024 * 1024), 3),
            ))
        logger.info(
            "  [EPF bridge] Imported %s/%s; normal Real Photo archives will "
            "handle %s remaining clips.",
            len(rendered_ids), len(selections), len(selections) - len(rendered_ids),
        )
        return rendered, rendered_ids

    def process_phase(self, selections: List[ClipSelection],
                      stop_event=None, deadline_ts=None) -> Dict[str, Any]:
        """Background-friendly phase: download photos and save to generated/.
        Updates montage_plan markers + credits when done.

        stop_event  — threading.Event; если выставлен (видео-генерация закончилась) —
                      прекращаем загрузку, недокачанные клипы догенерятся нейросетью.
        deadline_ts — абсолютный дедлайн (time.time()); по достижении (таймаут 30 мин) —
                      тоже прекращаем. Дедлайн real_photo = МИН(конец генерации, таймаут)."""
        meta = getattr(self, '_meta', None)
        clip_meta_by_id = getattr(self, '_clip_meta_by_id', None)
        if meta is None or clip_meta_by_id is None:
            cards, meta = self._build_clip_cards()
            clip_meta_by_id = {c['id']: c for c in cards}
            self._lang_hint = meta.get('language', 'en')

        if not selections:
            logger.warning("  No clips selected — nothing to do")
            return {'status': 'ok', 'rendered': 0, 'selected': 0, 'total': meta['total_clips']}

        all_selections = sorted(selections, key=lambda s: s.clip_id)
        selections = all_selections
        results: List[ClipResult] = []
        # Стоп/дедлайн видны источникам (EPF запускает свой процесс на клип).
        self._stop_event = stop_event
        self._deadline_ts = deadline_ts

        # EPF больше НЕ отдельный проход впереди всех (25.08): он такой же источник
        # каскада на позиции из списка GUI, а в режиме микса — просто один из
        # поставщиков кандидатов на общий лист. См. process_clip.

        if selections and not self.sources:
            # EPF-only is a valid configuration. Every candidate that EPF did
            # not import must be recorded as attempted-failed so
            # update_montage_and_credits() removes real_photo_pending and the
            # normal AI generation stage can pick the clip up.
            results.extend(
                ClipResult(
                    clip_id=selection.clip_id,
                    status='no_match',
                    error='EPF did not import an image and no archive sources are enabled',
                )
                for selection in selections
            )
            logger.info(
                "  [EPF bridge] EPF-only: %s unresolved clips returned to AI generation",
                len(selections),
            )
            selections = []

        logger.info(f"  [2/2] Downloading {len(selections)} fallback photos -> generated/")
        stage_start = time.perf_counter()
        cumulative_mb = sum(result.size_mb for result in results)
        N = len(selections)
        _stopped_early = False
        # Клипы идут пулом, и потоков ровно столько, сколько экземпляров EPF
        # (`REAL_PHOTO_EPF_LANES`): дорожка = клип в поиске, второй настройки
        # быть не должно. До 31.08 стадия шла строго по одному, и дорожки
        # простаивали — в логе пользователя 30.08 они честно чередовались
        # 1→2→3→4, но по времени не пересекались НИ РАЗУ: 13 клипов из 163 за
        # отведённые 12 минут. Сетевые источники параллель держат (замер 31.08:
        # wikimedia ×1.9, serper ×2.4, выдача та же, отказов нет), а вижн через
        # Codex сам придержит очередь семафором `CODEX_MAX_PARALLEL` — картинки
        # к тому моменту уже скачаны, ждёт только выбор.
        lanes = _clip_parallel_limit()
        counter_lock = threading.Lock()
        started = 0
        _fmt = lambda s: f"{int(s//60)}m{int(s%60):02d}s" if s >= 60 else f"{int(s)}s"

        def _handle_one(sel) -> None:
            nonlocal cumulative_mb, started, _stopped_early
            # Дедлайн real_photo = МИН(конец генерации видео [stop_event], таймаут
            # [deadline_ts]). Проверяем В ЗАДАЧЕ, а не только перед раздачей:
            # иначе после стопа доработали бы все уже розданные клипы.
            if stop_event is not None and stop_event.is_set():
                with counter_lock:
                    if not _stopped_early:
                        logger.info(f"  [Real Photo] stop signal (видео готово) — "
                                    f"прекращаю на {started}/{N}, остальные → догенерация")
                    _stopped_early = True
                return
            if deadline_ts is not None and time.time() > deadline_ts:
                with counter_lock:
                    if not _stopped_early:
                        logger.info(f"  [Real Photo] таймаут — прекращаю на "
                                    f"{started}/{N}, остальные → догенерация")
                    _stopped_early = True
                return
            clip_meta = clip_meta_by_id.get(sel.clip_id)
            if not clip_meta:
                return
            with counter_lock:
                started += 1
                position = started
                elapsed_total = time.perf_counter() - stage_start
                avg_per_clip = (elapsed_total / max(position - 1, 1)
                                if position > 1 else 0)
                remaining = N - position + 1
                # Клипы идут параллельно, поэтому остаток делим на число потоков:
                # иначе ETA завышался бы ровно во столько же раз.
                eta_sec = (avg_per_clip * remaining / max(1, lanes)
                           if avg_per_clip > 0 else 0)
                mb_now = cumulative_mb
            logger.info(f"  [{position}/{N}] clip {sel.clip_id}  |  elapsed {_fmt(elapsed_total)}  |  ETA ~{_fmt(eta_sec)}  |  downloaded {mb_now:.1f} MB total")
            logger.info(f"    query: '{sel.search_query}'  (sources: {','.join(sel.preferred_sources or self.sources)})")
            clip_start = time.perf_counter()
            try:
                result = self.process_clip(sel, clip_meta)
                result.elapsed_sec = time.perf_counter() - clip_start
                with counter_lock:
                    cumulative_mb += result.size_mb
                    results.append(result)
                if result.status == 'rendered':
                    src = result.archive_result.source if result.archive_result else '?'
                    logger.info(f"    ✓ rendered in {result.elapsed_sec:.1f}s from {src} ({result.size_mb:.1f} MB)")
                else:
                    logger.info(f"    ✗ {result.status}: {result.error[:80]}")
            except Exception as e:
                logger.error(f"    ✗ exception: {e}")
                logger.debug(traceback.format_exc())
                with counter_lock:
                    results.append(ClipResult(clip_id=sel.clip_id, status='render_failed',
                                              error=str(e), elapsed_sec=time.perf_counter()-clip_start))

        if lanes <= 1 or N <= 1:
            for sel in selections:
                _handle_one(sel)
        else:
            logger.info(f"  [Real Photo] параллель потоков: {lanes} "
                        f"(по числу экземпляров EPF, настройка REAL_PHOTO_EPF_LANES)")
            with ThreadPoolExecutor(max_workers=lanes,
                                    thread_name_prefix='realphoto') as pool:
                list(pool.map(_handle_one, selections))

        rendered = [r for r in results if r.status == 'rendered']
        logger.info(f"  Finalizing montage_plan + credits...")
        self.update_montage_and_credits(results)

        # Query-log (отладка качества запросов): вход(id+voiceover), запрос+кем(провайдер/path/
        # fallback-причина), ссылка на изображение + страницу. Логирование не должно ронять стадию.
        try:
            results_by_id = {r.clip_id: r for r in results}
            log_items = []
            for sel in all_selections:
                r = results_by_id.get(sel.clip_id)
                ar = r.archive_result if r else None
                log_items.append({
                    'clip_id': sel.clip_id,
                    'voiceover': ((clip_meta_by_id.get(sel.clip_id) or {}).get('voiceover') or '')[:200],
                    'query': sel.search_query,
                    'query_local': sel.search_query_local,
                    'query_by': sel.query_provider,
                    'query_path': sel.query_path,
                    'fallback_reason': sel.fallback_reason,
                    'status': r.status if r else 'skipped',
                    'source': ar.source if ar else '',
                    'image_url': ar.url if ar else '',
                    'page_url': ar.page_url if ar else '',
                })
            (self.project_dir / 'real_photo_query_log.json').write_text(
                json.dumps({'project': self.project_dir.name, 'items': log_items},
                           ensure_ascii=False, indent=2), encoding='utf-8')
            logger.info(f"  Query-log → real_photo_query_log.json ({len(log_items)} clips)")
        except Exception as e:
            logger.warning(f"  Query-log write failed (non-fatal): {e}")

        # By-source breakdown
        by_source: Dict[str, int] = {}
        for r in rendered:
            if r.archive_result:
                by_source[r.archive_result.source] = by_source.get(r.archive_result.source, 0) + 1
        total_elapsed = time.perf_counter() - stage_start
        _fmt2 = lambda s: f"{int(s//60)}m{int(s%60):02d}s" if s >= 60 else f"{int(s)}s"

        summary = {
            'status': 'ok',
            'total': meta['total_clips'],
            'selected': len(all_selections),
            'rendered': len(rendered),
            'failed': len(results) - len(rendered),
            'by_type': {
                'photo': sum(1 for r in rendered if r.media_type == 'photo'),
                'video': 0,
            },
            'by_source': by_source,
            'downloaded_mb': round(cumulative_mb, 1),
            'elapsed_sec': round(total_elapsed, 1),
            'credits_path': str(self.credits_path),
        }
        logger.info("  " + "=" * 50)
        logger.info(f"  Done: {len(rendered)}/{len(all_selections)} rendered in {_fmt2(total_elapsed)}")
        logger.info(f"  Photos: {summary['by_type']['photo']}  |  Downloaded: {cumulative_mb:.1f} MB total")
        if by_source:
            src_str = "  ".join(f"{k} {v}" for k, v in sorted(by_source.items(), key=lambda kv: -kv[1]))
            logger.info(f"  By source: {src_str}")
        logger.info("  " + "=" * 50)
        return summary

    def run(self) -> Dict[str, Any]:
        """End-to-end synchronous run (select then process). For standalone CLI use."""
        selections = self.select_phase()
        return self.process_phase(selections)

    # --- Marker management ---------------------------------------------------

    def _mark_pending(self, selections: List[ClipSelection]):
        """Mark clips as `source: real_photo_pending` so FastGen / Veo i2v skip them.
        Called right after select_phase, before any download. Other stages read
        the same montage_plan.json and respect the marker.
        """
        try:
            with open(self.montage_path, 'r', encoding='utf-8') as f:
                montage = json.load(f)
            backup = self.montage_path.with_suffix('.json.before_real_photos')
            if not backup.exists():
                shutil.copy2(self.montage_path, backup)
            sel_ids = {s.clip_id for s in selections}
            for clip in montage.get('clips', []):
                if clip['id'] in sel_ids:
                    clip['source'] = 'real_photo_pending'
            self.montage_path.write_text(
                json.dumps(montage, ensure_ascii=False, indent=2), encoding='utf-8'
            )
            logger.info(f"  Marked {len(sel_ids)} clips as 'real_photo_pending' in montage_plan")
        except Exception as e:
            logger.error(f"Failed to mark pending: {e}")


# ============================================================================
# PUBLIC ENTRY POINT
# ============================================================================

def run_real_photo_stage(
    project_dir: str,
    target_ratio: Optional[float] = None,
    min_ratio: Optional[float] = None,
    max_ratio: Optional[float] = None,
    sources: Optional[List[str]] = None,
    codex_model: Optional[str] = None,
) -> Dict[str, Any]:
    """High-level entry point. Reads defaults from config if not provided."""
    # Свой лог стадии. Отдельный файл заводился ТОЛЬКО для CLI-команды
    # `real-photos`; внутри пайплайна стадия писала лишь в общий лог, из-за чего
    # разбор EPF 01.09 пришлось вести по `bridge_manifest.json`, а не по логу.
    from units.stage_journal import attach as _attach_log, detach as _detach_log
    _log_handler, _log_path = _attach_log(project_dir, 'real_photo')
    if _log_path:
        print(f"  [Real Photo] лог стадии: {_log_path}")
    target_ratio = target_ratio if target_ratio is not None else getattr(config, 'REAL_PHOTO_RATIO_TARGET', 0.40)
    min_ratio = min_ratio if min_ratio is not None else getattr(config, 'REAL_PHOTO_RATIO_MIN', 0.30)
    max_ratio = max_ratio if max_ratio is not None else getattr(config, 'REAL_PHOTO_RATIO_MAX', 0.55)
    sources = sources if sources is not None else getattr(config, 'REAL_PHOTO_SOURCES',
                                                          ['wikimedia', 'nasa', 'loc', 'archive'])

    fps = getattr(config, 'VIDEO_FPS', 60)
    # Match ratio to fastgen aspect (landscape default)
    target_w, target_h = 1920, 1080

    proc = RealPhotoProcessor(
        project_dir=Path(project_dir),
        target_ratio=target_ratio,
        min_ratio=min_ratio,
        max_ratio=max_ratio,
        sources=sources,
        codex_model=codex_model,
        target_w=target_w,
        target_h=target_h,
        fps=fps,
    )
    try:
        return proc.run()
    finally:
        # Хендлер снимаем ВСЕГДА, даже когда стадия упала: GUI живёт часами, и
        # оставленный обработчик продолжил бы писать в лог прошлого проекта.
        if _log_path:
            print(f"  [Real Photo] полный лог стадии: {_log_path}")
        _detach_log(_log_handler)


if __name__ == '__main__':
    # Standalone test mode: python -m units.archive_photos <project_path>
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s | %(levelname)-7s | %(message)s',
                        datefmt='%H:%M:%S')
    if len(sys.argv) < 2:
        print("Usage: python -m units.archive_photos <project_dir>")
        sys.exit(1)
    summary = run_real_photo_stage(sys.argv[1])
    print(json.dumps(summary, ensure_ascii=False, indent=2))
