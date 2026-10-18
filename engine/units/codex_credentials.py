"""Shared Codex credential location and loading helpers."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Dict, Optional


def codex_home() -> Path:
    """Return the stable user-level Codex home used by every BegoFrame process."""
    configured = (os.environ.get('CODEX_HOME') or '').strip()
    return Path(configured).expanduser() if configured else Path.home() / '.codex'


def codex_env(base_env: Optional[dict] = None) -> dict:
    """Return a process environment pinned to the same Codex home."""
    env = dict(base_env if base_env is not None else os.environ)
    env['CODEX_HOME'] = str(codex_home())
    return env


def auth_file() -> Path:
    return codex_home() / 'auth.json'


def sessions_dir() -> Path:
    return codex_home() / 'sessions'


def _load_json_file(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _migrate_legacy_auth(home: Path) -> Optional[Dict[str, Any]]:
    """Recover credentials from early portable builds that used a local home."""
    install_root = Path(__file__).resolve().parents[2]
    destination = home / 'auth.json'
    for legacy in (
        install_root / '.codex' / 'auth.json',
        install_root / 'Codex' / '.codex' / 'auth.json',
    ):
        data = _load_json_file(legacy)
        if not data:
            continue
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            temp = destination.with_suffix('.json.tmp')
            shutil.copy2(legacy, temp)
            os.chmod(temp, 0o600)
            temp.replace(destination)
        except OSError:
            pass
        return data
    return None


def load_auth() -> Optional[Dict[str, Any]]:
    """Load Codex credentials from auth.json."""
    home = codex_home()
    return (
        _load_json_file(home / 'auth.json')
        or _migrate_legacy_auth(home)
    )
