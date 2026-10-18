"""
codex_usage.py — Codex / ChatGPT quota checker

Источники данных:
1. Live API: GET https://chatgpt.com/backend-api/wham/usage
   — самый точный (учитывает web + CLI + VSCode + прочие каналы)
   — не расходует квоту генерации
2. Session JSONL tail: парсинг самого свежего rollout-*.jsonl в ~/.codex/sessions/
   — бесплатно, без сети, обновляется во время активной генерации
   — устаревает если юзер работал с ChatGPT вне CLI

Публичные функции:
    get_quota(force_api: bool = False) -> dict
    fetch_via_api() -> dict
    fetch_via_session() -> Optional[dict]
"""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from units import codex_credentials as codex_auth

_API_URL = 'https://chatgpt.com/backend-api/wham/usage'
_API_TIMEOUT = 10

# Internal cache (API results)
_cache_lock = threading.Lock()
_cache: Dict[str, Any] = {
    'data': None,        # last successful result dict
    'fetched_at': 0.0,   # Unix ts
    'source': None,      # 'api' | 'session' | None
    'error': None,       # last error text
}
_CACHE_TTL_SEC = 30  # return cached result if fresher than this


def _load_auth() -> Optional[Dict[str, Any]]:
    """Load Codex credentials from its configured file or OS credential store."""
    return codex_auth.load_auth()


def fetch_via_api() -> Dict[str, Any]:
    """
    Live API call to /backend-api/wham/usage.

    Returns a dict with keys:
        ok: bool
        source: 'api'
        plan_type: str (e.g. 'team', 'plus', 'pro')
        primary_pct: int (used percent in 5h window)
        primary_resets_at: int (Unix ts)
        primary_window_sec: int
        secondary_pct: int
        secondary_resets_at: int
        secondary_window_sec: int
        credits_has: bool
        credits_unlimited: bool
        error: str (if ok=False)
    """
    auth = _load_auth()
    if not auth:
        return {'ok': False, 'source': 'api',
                'error': 'Codex credentials not found (auth.json / Keychain)'}

    tokens = auth.get('tokens') or {}
    access_token = tokens.get('access_token')
    account_id = tokens.get('account_id')
    if not access_token or not account_id:
        return {'ok': False, 'source': 'api', 'error': 'access_token / account_id missing'}

    _headers = {
        'Authorization': f'Bearer {access_token}',
        'Accept': 'application/json',
        'ChatGPT-Account-Id': account_id,
        'Origin': 'https://chatgpt.com',
        'Referer': 'https://chatgpt.com/',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    }
    try:
        import params as _cfg
        _px = getattr(_cfg, 'CODEX_PROXY', '') or ''
    except Exception:
        _px = ''

    def _fail(code):
        if code == 401:
            return {'ok': False, 'source': 'api', 'error': 'token_expired (401) — relogin codex'}
        if code == 403:
            return {'ok': False, 'source': 'api', 'error':
                    '403 — гео-блок chatgpt.com (IP/регион). Укажи прокси Codex в Settings. '
                    'На работу самого Codex не влияет.'}
        return {'ok': False, 'source': 'api', 'error': f'HTTP {code}'}

    if _px:
        # urllib НЕ умеет socks5 (BadStatusLine). Прокси-запрос гоним через curl (socks5h/http).
        import shutil as _sh, subprocess as _sp
        curl = _sh.which('curl') or 'curl'
        parg = _px.replace('socks5://', 'socks5h://', 1) if _px.startswith('socks5://') else _px
        _cmd = [curl, '-s', '--max-time', str(_API_TIMEOUT), '--proxy', parg,
                '-w', '\n%{http_code}']
        for _k, _v in _headers.items():
            _cmd += ['-H', f'{_k}: {_v}']
        _cmd.append(_API_URL)
        try:
            _r = _sp.run(_cmd, capture_output=True, text=True, encoding='utf-8', errors='replace',
                         creationflags=0x08000000 if sys.platform == 'win32' else 0)
            _out = (_r.stdout or '').rstrip('\n')
            _nl = _out.rfind('\n')
            _code_str = _out[_nl + 1:].strip() if _nl >= 0 else _out.strip()
            _body_txt = _out[:_nl] if _nl >= 0 else ''
            _code = int(_code_str) if _code_str.isdigit() else 0
            if _code and _code != 200:
                return _fail(_code)
            if not _body_txt:
                return {'ok': False, 'source': 'api', 'error': f'curl: пустой ответ (proxy?) {(_r.stderr or "")[:120]}'}
            body = json.loads(_body_txt)
        except Exception as e:
            return {'ok': False, 'source': 'api', 'error': f'{type(e).__name__}: {e}'}
    else:
        # Без прокси — прямой urllib (VPN / не РФ)
        req = urllib.request.Request(_API_URL, headers=_headers)
        try:
            resp = urllib.request.urlopen(req, timeout=_API_TIMEOUT)
            body = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return _fail(e.code)
        except Exception as e:
            return {'ok': False, 'source': 'api', 'error': f'{type(e).__name__}: {e}'}

    rl = body.get('rate_limit') or {}
    prim = rl.get('primary_window') or {}
    sec = rl.get('secondary_window') or {}
    creds = body.get('credits') or {}

    return {
        'ok': True,
        'source': 'api',
        'plan_type': body.get('plan_type', '?'),
        'primary_pct': int(prim.get('used_percent', 0)),
        'primary_resets_at': int(prim.get('reset_at', 0)),
        'primary_window_sec': int(prim.get('limit_window_seconds', 0)),
        'secondary_pct': int(sec.get('used_percent', 0)),
        'secondary_resets_at': int(sec.get('reset_at', 0)),
        'secondary_window_sec': int(sec.get('limit_window_seconds', 0)),
        'credits_has': bool(creds.get('has_credits', False)),
        'credits_unlimited': bool(creds.get('unlimited', False)),
        'error': None,
    }


def _find_latest_session_file() -> Optional['Path']:
    """Return path to the most recently modified rollout-*.jsonl."""
    sessions_dir = codex_auth.sessions_dir()
    if not sessions_dir.exists():
        return None
    latest = None
    latest_mtime = 0.0
    try:
        for f in sessions_dir.rglob('rollout-*.jsonl'):
            try:
                mt = f.stat().st_mtime
                if mt > latest_mtime:
                    latest_mtime = mt
                    latest = f
            except Exception:
                continue
    except Exception:
        pass
    return latest


def fetch_via_session() -> Optional[Dict[str, Any]]:
    """
    Parse the latest session JSONL for the last token_count rate_limits.

    Returns None if no session file exists or no rate_limits found.
    Otherwise returns dict with same keys as fetch_via_api() but source='session'.
    Additionally includes:
        session_mtime: float (file modification time)
        session_active: bool (True if mtime < 60s ago)
    """
    f = _find_latest_session_file()
    if not f:
        return None

    try:
        lines = f.read_text(encoding='utf-8', errors='replace').splitlines()
    except Exception as e:
        logging.debug(f"codex_usage: session read failed: {e}")
        return None

    # Find the last event_msg/token_count event with rate_limits
    rate_limits = None
    for line in reversed(lines):
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get('type') != 'event_msg':
            continue
        payload = ev.get('payload') or {}
        if payload.get('type') != 'token_count':
            continue
        rl = payload.get('rate_limits')
        if rl:
            rate_limits = rl
            break

    if not rate_limits:
        return None

    prim = rate_limits.get('primary') or {}
    sec = rate_limits.get('secondary') or {}

    try:
        mtime = f.stat().st_mtime
    except Exception:
        mtime = 0.0

    return {
        'ok': True,
        'source': 'session',
        'plan_type': rate_limits.get('plan_type', '?'),
        'primary_pct': int(prim.get('used_percent', 0)),
        'primary_resets_at': int(prim.get('resets_at', 0)),
        'primary_window_sec': int((prim.get('window_minutes', 0) or 0) * 60),
        'secondary_pct': int(sec.get('used_percent', 0)),
        'secondary_resets_at': int(sec.get('resets_at', 0)),
        'secondary_window_sec': int((sec.get('window_minutes', 0) or 0) * 60),
        'credits_has': False,
        'credits_unlimited': False,
        'session_mtime': mtime,
        'session_active': (time.time() - mtime) < 60,
        'error': None,
    }


def get_quota(force_api: bool = False) -> Dict[str, Any]:
    """
    Combined getter with internal cache.

    Logic:
    1. If there is an active session JSONL (mtime < 60s) — always use session
       (more fresh than any cached API result, free, no network).
    2. Otherwise if cache is fresh (< 30s) — return cached API result.
    3. Otherwise fetch via API, cache, return.
    4. If API fails — fall back to session (stale data with warning), then cached, then error.

    force_api=True bypasses cache and session, always hits API.
    """
    now = time.time()

    if not force_api:
        # Session has priority ONLY if currently active (mtime < 60s)
        sess = fetch_via_session()
        if sess and sess.get('session_active'):
            with _cache_lock:
                _cache['data'] = sess
                _cache['fetched_at'] = now
                _cache['source'] = 'session'
                _cache['error'] = None
            return sess

        # Cache hit (API result)
        with _cache_lock:
            if _cache['data'] and _cache['source'] == 'api' and (now - _cache['fetched_at'] < _CACHE_TTL_SEC):
                return _cache['data']

    # Fresh API call
    api_result = fetch_via_api()
    if api_result.get('ok'):
        with _cache_lock:
            _cache['data'] = api_result
            _cache['fetched_at'] = now
            _cache['source'] = 'api'
            _cache['error'] = None
        return api_result

    # API failed — try session fallback (even if stale)
    sess_any = fetch_via_session()
    if sess_any:
        sess_any['stale_api_error'] = api_result.get('error')
        with _cache_lock:
            _cache['data'] = sess_any
            _cache['fetched_at'] = now
            _cache['source'] = 'session_fallback'
            _cache['error'] = api_result.get('error')
        return sess_any

    # Nothing worked — return cached if available
    with _cache_lock:
        if _cache['data']:
            return _cache['data']
        _cache['error'] = api_result.get('error')

    return api_result


def format_time_delta(seconds_until: int) -> str:
    """Format seconds into human-readable 'Xh Ym' or 'Xd Yh'."""
    if seconds_until <= 0:
        return 'now'
    h = seconds_until // 3600
    m = (seconds_until % 3600) // 60
    if h >= 24:
        d = h // 24
        h = h % 24
        return f'{d}d {h}h'
    return f'{h}h {m:02d}m'


