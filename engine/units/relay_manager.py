"""
relay_manager.py — прокси для Codex: сборка URL прокси и сетевая проверка.

Используется вкладкой «Настройки → Codex → Прокси Codex»:
  • apply_proxy_scheme() — навесить выбранную схему (http/socks5) на введённый прокси;
  • test_proxy() — проверить прокси через curl (какой IP и страна видны снаружи).
"""
from __future__ import annotations

import shutil
import subprocess
import sys

_NOWIN = {"creationflags": 0x08000000} if sys.platform == "win32" else {}


def normalize_proxy(raw: str) -> str:
    """Приводит прокси к форме с кредами БЕЗ схемы: user:pass@host:port (или host:port).
    Принимает и URL (со схемой — схему сохраняет), и формат антика host:port:user:pass."""
    s = (raw or "").strip()
    if not s:
        return ""
    if "://" in s:
        return s  # уже URL со схемой
    parts = s.split(":")
    if len(parts) == 4:  # host:port:user:pass (формат антика/Proxifier)
        host, port, user, pw = parts
        return f"{user}:{pw}@{host}:{port}"
    if len(parts) == 2:  # host:port (без авторизации)
        return f"{parts[0]}:{parts[1]}"
    return s  # что-то нестандартное — отдаём как есть


def _curl_ip(proxy_arg: str, timeout: int) -> tuple[bool, str]:
    """Один прогон curl через заданный --proxy. (ok, текст об IP или ошибке)."""
    curl = shutil.which("curl") or "curl"
    try:
        r = subprocess.run(
            [curl, "-s", "--max-time", str(timeout), "--proxy", proxy_arg, "https://ifconfig.co/json"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", **_NOWIN,
        )
        out = (r.stdout or "").strip()
        if out and ("ip" in out.lower()):
            import json as _json
            try:
                j = _json.loads(out)
                return True, f"IP {j.get('ip','?')} | {j.get('country','?')} ({j.get('city','')})"
            except Exception:
                return True, out[:200]
        return False, (r.stderr or out or "нет данных")[:200]
    except Exception as e:
        return False, str(e)


def apply_proxy_scheme(raw: str, scheme: str) -> str:
    """ПРИНУДИТЕЛЬНО навесить выбранную юзером схему (http/socks5). Единая точка сборки:
    снимает существующую схему, ставит выбранную. Тип прокси из host:port:user:pass не определить
    — поэтому юзер выбирает сам, а мы просто собираем. Мгновенно, без сети."""
    s = (raw or "").strip()
    if not s:
        return ""
    scheme = (scheme or "http").strip() or "http"
    creds = s.split("://", 1)[1] if "://" in s else normalize_proxy(s)
    return f"{scheme}://{creds}" if creds else ""


def probe_proxy(raw: str, timeout: int = 25) -> tuple[bool, str, str]:
    """СЕТЕВОЙ пробник (для кнопки «Проверить»): (ok, текст об IP/ошибке, рабочий_URL_со_схемой).
    Принимает URL и формат антика host:port:user:pass; без схемы пробует socks5, затем http."""
    pu = (raw or "").strip()
    if not pu:
        return True, "Прокси не задан — прямое соединение.", ""
    if "://" in pu:
        arg = pu.replace("socks5://", "socks5h://", 1) if pu.startswith("socks5://") else pu
        ok, msg = _curl_ip(arg, timeout)
        return (ok, msg if ok else f"Прокси не ответил: {msg}", pu)
    creds = normalize_proxy(pu)
    for scheme, label in (("socks5h", "socks5"), ("http", "http")):
        ok, msg = _curl_ip(f"{scheme}://{creds}", timeout)
        if ok:
            return True, f"[{label}] {msg}", f"{label}://{creds}"
    return (False, "Прокси не ответил ни по socks5, ни по http (проверь host:port:user:pass)",
            f"socks5://{creds}")


def test_proxy(proxy_url: str, timeout: int = 25) -> tuple[bool, str]:
    """Обёртка (ok, msg) над probe_proxy — для существующих вызовов."""
    ok, msg, _ = probe_proxy(proxy_url, timeout)
    return ok, msg
