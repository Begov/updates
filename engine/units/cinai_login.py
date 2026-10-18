"""Забор сессии CinAI из браузера пользователя (без автоматизации браузера).

Зачем: вход в CinAI идёт только через Google, а Google отказывает
автоматизированным профилям («Возможно, этот браузер или приложение
небезопасны») — то есть Playwright-путь для логина мёртв. Рабочая схема та же,
что у Suno: пользователь входит в СВОЁМ обычном браузере, а мы читаем уже
выданную сессию.

Где лежит (снято 16.08.2026): localStorage origin `https://cinai.tv`,
  token          — access JWT (живёт ~24 ч)
  refresh_token  — 64 символа, по нему клиент сам продлевает пару
  base44_access_token — дубль access

Хранилища по браузерам:
  Firefox  — <profile>/storage/default/https+++cinai.tv/ls/data.sqlite (SQLite)
  Chromium — <profile>/Local Storage/leveldb/*.ldb|*.log (значение может
             пересекать границу блока, поэтому кандидатов валидируем)

Куки не трогаем: в Chrome 130+ они зашифрованы, а localStorage — нет.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

import params as config
from units.cinai_stills import CinAIAuthError, persist_cinai_credentials

CINAI_ORIGIN_FF = 'https+++cinai.tv'
_JWT_RE = re.compile(r'eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}')
_REFRESH_RE = re.compile(r'^[A-Za-z0-9_-]{32,128}$')


def open_cinai_in_firefox(url: str = 'https://cinai.tv/StudioChat?tab=image') -> None:
    """Open CinAI in the user's real Firefox installation, never the default browser."""
    candidates = []
    which = shutil.which('firefox')
    if which:
        candidates.append(which)
    for path in (
        Path(os.environ.get('PROGRAMFILES', '')) / 'Mozilla Firefox/firefox.exe',
        Path(os.environ.get('PROGRAMFILES(X86)', '')) / 'Mozilla Firefox/firefox.exe',
        Path(os.environ.get('LOCALAPPDATA', '')) / 'Mozilla Firefox/firefox.exe',
    ):
        if str(path) not in ('Mozilla Firefox\\firefox.exe',) and path.exists():
            candidates.append(str(path))
    executable = next(iter(candidates), None)
    if not executable:
        raise FileNotFoundError('Firefox executable was not found')
    subprocess.Popen([executable, '-new-tab', url],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _jwt_expires_in(token: str) -> Optional[int]:
    try:
        payload = json.loads(base64.urlsafe_b64decode(token.split('.')[1] + '=='))
        return int(float(payload.get('exp', 0)) - time.time())
    except Exception:
        return None


# ── Firefox ────────────────────────────────────────────────────────────────

def _firefox_stores() -> List[Path]:
    roots = []
    appdata = os.environ.get('APPDATA')
    if appdata:
        roots.append(Path(appdata) / 'Mozilla/Firefox/Profiles')
    home = Path.home()
    roots += [home / '.mozilla/firefox',
              home / 'Library/Application Support/Firefox/Profiles']
    stores = []
    for root in roots:
        if root.exists():
            stores += list(root.glob(f'*/storage/default/{CINAI_ORIGIN_FF}/ls/data.sqlite'))
    return stores


def _read_firefox(store: Path) -> Dict[str, str]:
    """Копируем базу во временную папку: живой Firefox держит её залоченной."""
    tmp_dir = Path(tempfile.mkdtemp(prefix='cinai_ls_'))
    tmp = tmp_dir / 'data.sqlite'
    try:
        shutil.copy2(store, tmp)
        for suffix in ('-wal', '-shm'):
            extra = store.with_name(store.name + suffix)
            if extra.exists():
                shutil.copy2(extra, tmp.with_name(tmp.name + suffix))
        con = sqlite3.connect(str(tmp))
        try:
            rows = con.execute('SELECT key, value FROM data').fetchall()
        finally:
            con.close()
    except Exception:
        return {}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    out = {}
    for key, value in rows:
        text = value.decode('utf-8', 'ignore') if isinstance(value, bytes) else str(value)
        out[str(key)] = text.strip()
    return out


# ── Chromium (Chrome / Opera / Edge / Brave / Vivaldi) ─────────────────────

def _chromium_leveldbs() -> List[Path]:
    local = os.environ.get('LOCALAPPDATA')
    appdata = os.environ.get('APPDATA')
    bases: List[Path] = []
    if local:
        bases += [Path(local) / 'Google/Chrome/User Data',
                  Path(local) / 'Microsoft/Edge/User Data',
                  Path(local) / 'BraveSoftware/Brave-Browser/User Data',
                  Path(local) / 'Vivaldi/User Data',
                  Path(local) / 'Yandex/YandexBrowser/User Data']
    if appdata:
        bases += [Path(appdata) / 'Opera Software/Opera Stable',
                  Path(appdata) / 'Opera Software/Opera GX Stable',
                  Path(appdata) / 'Opera']
    dirs: List[Path] = []
    for base in bases:
        if not base.exists():
            continue
        dirs += list(base.glob('Local Storage/leveldb'))
        dirs += list(base.glob('*/Local Storage/leveldb'))
    return dirs


def _read_chromium(leveldb: Path) -> Dict[str, str]:
    """Грубый разбор leveldb: ищем записи origin cinai.tv и достаём JWT/refresh.

    Полноценный парсер leveldb не нужен и тянул бы зависимость: нам хватает
    поиска маркера origin и валидации кандидатов (JWT разбирается, refresh
    проверяется на сервере)."""
    found: Dict[str, str] = {}
    marker = b'_https://cinai.tv'
    for path in sorted(leveldb.glob('*')):
        if path.suffix not in ('.log', '.ldb'):
            continue
        try:
            blob = path.read_bytes()
        except OSError:
            continue
        if b'cinai' not in blob.lower():
            continue
        index = blob.find(marker)
        while index >= 0:
            window = blob[index:index + 3000]
            for variant in (window, window.replace(b'\x00', b'')):
                text = re.sub(rb'[^A-Za-z0-9_.\-]', b'', variant).decode('ascii', 'ignore')
                for match in _JWT_RE.finditer(text):
                    token = match.group(0)
                    if _jwt_expires_in(token) is not None:
                        prev = found.get('token')
                        if not prev or (_jwt_expires_in(token) or 0) > (_jwt_expires_in(prev) or 0):
                            found['token'] = token
            index = blob.find(marker, index + 1)
    return found


# ── публичный API ──────────────────────────────────────────────────────────

def _browser_session_candidates() -> List[Dict[str, object]]:
    candidates: List[Dict[str, object]] = []
    seen = set()

    def add(access: str, refresh: str, source: str) -> None:
        access = (access or '').strip()
        if not access or access in seen:
            return
        left = _jwt_expires_in(access)
        if left is None:
            return
        seen.add(access)
        candidates.append({'access_token': access,
                           'refresh_token': (refresh or '').strip(),
                           'expires_in': left,
                           'source': source})

    for store in _firefox_stores():
        data = _read_firefox(store)
        refresh = data.get('refresh_token') or ''
        if refresh and not _REFRESH_RE.match(refresh):
            refresh = ''
        add(data.get('token') or data.get('base44_access_token') or '',
            refresh, 'Firefox')

    for leveldb in _chromium_leveldbs():
        data = _read_chromium(leveldb)
        browser = leveldb.parts[-4] if len(leveldb.parts) >= 4 else 'Chromium'
        add(data.get('token', ''), '', browser)

    candidates.sort(key=lambda item: int(item['expires_in']), reverse=True)
    return candidates


def read_browser_session() -> Optional[Dict[str, object]]:
    """Самая свежая пара токенов из браузеров. None — ничего не нашли.

    Firefox идёт первым: там читается и refresh_token, с которым сессия
    продлевается без повторного входа. Из leveldb Chromium (Chrome/Edge/Opera/
    Brave/Vivaldi/Yandex) достаётся только access — такая сессия живёт около
    суток и не продлевается, поэтому это запасной вариант, когда входа в
    Firefox нет."""
    candidates = _browser_session_candidates()
    if not candidates:
        return None
    firefox = [item for item in candidates
               if str(item.get('source', '')) == 'Firefox']
    if firefox:
        return firefox[0]
    return candidates[0]


def token_is_accepted(access_token: str, timeout: int = 20) -> bool:
    """Живой ли токен по мнению сервера (дешёвый вызов баланса)."""
    try:
        response = requests.post(
            'https://api.cinai.tv/api/functions/getBillingBalance',
            headers={'Authorization': f'Bearer {access_token}',
                     'X-App-Id': 'runway-workflows', 'Accept': 'application/json'},
            timeout=timeout)
    except requests.RequestException:
        return False
    return response.status_code == 200


def import_browser_session(min_seconds_left: int = 300) -> Dict[str, object]:
    """Найти сессию в браузере, проверить на сервере и записать в .env.

    Возвращает {'source', 'expires_in', 'has_refresh'}. Бросает CinAIAuthError,
    если свежей сессии нет — тогда пользователю нужно войти в браузере."""
    session = read_browser_session()
    if not session:
        raise CinAIAuthError(
            'Сессия CinAI в браузерах не найдена. Войдите на cinai.tv в своём '
            'браузере и повторите.')
    if int(session['expires_in']) < min_seconds_left:
        raise CinAIAuthError(
            f"Найден просроченный токен ({int(session['expires_in']) // 60} мин). "
            'Откройте cinai.tv в браузере — сайт обновит сессию.')
    access = str(session['access_token'])
    if not token_is_accepted(access):
        raise CinAIAuthError(
            'Сервер CinAI не принял токен из браузера. Откройте cinai.tv, '
            'дождитесь загрузки студии и повторите.')
    persist_cinai_credentials(access, str(session.get('refresh_token') or '') or None)
    return {'source': session['source'],
            'expires_in': int(session['expires_in']),
            'has_refresh': bool(session.get('refresh_token'))}


def ensure_fresh_token(min_hours: Optional[float] = None,
                       on_status=None) -> Optional[Dict[str, object]]:
    """Превентивное продление пары токенов (для фонового вызова из GUI).

    Refresh-токен CinAI протухает от простоя: если им долго не пользоваться,
    сервер отвечает `Invalid or expired refresh token`, и нужен ручной вход.
    Поэтому продлеваем заранее, пока access ещё жив.

    Возвращает None, если продление не требовалось."""
    from units.cinai_stills import refresh_cinai_session, token_expires_in

    report = on_status or (lambda _text: None)
    hours = float(min_hours if min_hours is not None
                  else getattr(config, 'CINAI_TOKEN_PREFRESH_HOURS', 12) or 12)
    access = str(getattr(config, 'CINAI_ACCESS_TOKEN', '') or '')
    if not access:
        return None
    left = token_expires_in(access)
    if left is not None and left > hours * 3600:
        return None
    try:
        refreshed = refresh_cinai_session()
        report('Токен CinAI продлён')
        return {'expires_in': token_expires_in(refreshed['access_token']),
                'via': 'refresh'}
    except Exception:
        # refresh мёртв — пробуем подобрать свежую сессию из браузера
        try:
            result = import_browser_session()
            report(f"Токен CinAI взят из браузера ({result['source']})")
            return {'expires_in': result['expires_in'], 'via': 'browser'}
        except CinAIAuthError:
            return None
