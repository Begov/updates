"""Фоновые мониторы: квота подписки GPT (Codex), ключи VeoNonStop, баланс CinAI.

Опросы идут в отдельных потоках; результаты приходят в интерфейс сигналами.
"""
from __future__ import annotations

import logging
import threading
import time

from PySide6.QtCore import QObject, Signal

from .. import engine_link


class Monitors(QObject):
    codex_quota = Signal(dict)
    veo_accounts = Signal(dict)
    veo_key = Signal(str, str, int, int)     # слот, подпись, лимит потоков, версия API
    cinai_status = Signal(dict)
    note = Signal(str, str)

    CODEX_API_SEC = 300
    CODEX_SESSION_SEC = 5
    VEO_SEC = 300
    CINAI_SEC = 300
    CINAI_PREFRESH_SEC = 6 * 3600

    def __init__(self):
        super().__init__()
        self._stop = threading.Event()
        self._veo_last: dict = {}
        self._veo_lock = threading.Lock()
        self._cinai_lock = threading.Lock()

    def start(self) -> None:
        threading.Thread(target=self._codex_loop, daemon=True, name="codex-quota").start()
        threading.Thread(target=self._veo_loop, daemon=True, name="veo-accounts").start()
        threading.Thread(target=self._cinai_loop, daemon=True, name="cinai-status").start()
        self.refresh_veo_keys()

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------ Codex / GPT
    def refresh_codex(self) -> None:
        def work():
            try:
                data = engine_link.unit("codex_quota").get_quota(force_api=True)
            except Exception as exc:  # noqa: BLE001
                data = {"ok": False, "error": str(exc)}
            self.codex_quota.emit(data)
        threading.Thread(target=work, daemon=True).start()

    def _codex_loop(self) -> None:
        last_api = 0.0
        while not self._stop.is_set():
            try:
                mod = engine_link.unit("codex_quota")
                if time.time() - last_api >= self.CODEX_API_SEC:
                    data = mod.fetch_via_api()
                    last_api = time.time()
                    self.codex_quota.emit(data)
                else:
                    session = mod.fetch_via_session()
                    if session and session.get("session_active"):
                        self.codex_quota.emit(session)
            except Exception as exc:  # noqa: BLE001
                self.codex_quota.emit({"ok": False, "error": str(exc)})
                last_api = time.time()
            self._stop.wait(self.CODEX_SESSION_SEC)

    # ------------------------------------------------------ VeoNonStop
    def refresh_veo(self) -> None:
        threading.Thread(target=self._veo_fetch, daemon=True).start()

    def _veo_fetch(self) -> None:
        if not self._veo_lock.acquire(blocking=False):
            return
        try:
            try:
                data = engine_link.unit("veo_accounts").fetch_accounts(engine_link.params())
            except Exception as exc:  # noqa: BLE001
                data = {"ok": False, "error": type(exc).__name__, "slots": {}}
            previous = self._veo_last.get("slots") or {}
            for slot, item in (data.get("slots") or {}).items():
                if not item.get("ok") and item.get("error_kind") == "unavailable" and (previous.get(slot) or {}).get("ok"):
                    stale = dict(previous[slot])
                    stale.update(stale=True, last_error=item.get("error"))
                    data["slots"][slot] = stale
            self._veo_last = data
            self.veo_accounts.emit(data)
        finally:
            self._veo_lock.release()

    def _veo_loop(self) -> None:
        while not self._stop.is_set():
            self._veo_fetch()
            if self._stop.wait(self.VEO_SEC):
                return

    def refresh_veo_keys(self) -> None:
        """Подписи ключей VeoNonStop (тариф, занятые слоты) и их лимиты потоков."""
        def work():
            try:
                client_mod = engine_link.unit("veo_client")
                usage_mod = engine_link.unit("veo_accounts")
                accounts = usage_mod.configured_accounts(engine_link.params())
            except Exception as exc:  # noqa: BLE001
                logging.debug("veo keys: %s", exc)
                return
            for acc in accounts:
                for attempt in range(3):
                    try:
                        client = client_mod.VeoNonStopClient(api_key=acc["api_key"], base_url=acc["base_url"])
                        info = client.get_account_info()
                        try:
                            usage = client.get_account_usage()
                        except Exception:  # noqa: BLE001
                            usage = {}
                        concurrent = info.get("concurrent_tasks", "?")
                        slots = usage_mod.slot_usage_text({
                            "active_tasks": usage.get("active_tasks"),
                            "queued_tasks": usage.get("queued_tasks"),
                            "max_concurrent_tasks": usage.get("max_concurrent_tasks"),
                            "concurrent_tasks": concurrent,
                        })
                        version = 2 if "/api/v2" in (acc["base_url"] or "") else 1
                        label = f"{acc['name']} · v{version} · {info.get('plan', '?')} ({slots})"
                        limit = int(concurrent) if str(concurrent).isdigit() else 0
                        self.veo_key.emit(acc["slot"], label, limit, version)
                        break
                    except Exception:  # noqa: BLE001
                        time.sleep(2)
        threading.Thread(target=work, daemon=True, name="veo-keys").start()

    # --------------------------------------------------------------- CinAI
    def refresh_cinai(self) -> None:
        threading.Thread(target=self._cinai_fetch, daemon=True).start()

    def _cinai_fetch(self) -> None:
        if not str(engine_link.cfg("CINAI_ACCESS_TOKEN", "") or "").strip():
            self.cinai_status.emit({"ok": False, "missing": True})
            return
        if not self._cinai_lock.acquire(blocking=False):
            return
        try:
            mod = engine_link.unit("cinai_stills")
            try:
                client = mod.CinAIClient()
                try:
                    balance = client.get_billing_balance()
                    perks = client.get_model_perks_status()
                except mod.CinAIAuthError:
                    engine_link.unit("cinai_login").import_browser_session()
                    client = mod.CinAIClient(access_token=engine_link.cfg("CINAI_ACCESS_TOKEN", ""))
                    balance = client.get_billing_balance()
                    perks = client.get_model_perks_status()
                self.cinai_status.emit({"ok": True, "balance": balance or {}, "perks": perks or {}})
            except Exception as exc:  # noqa: BLE001
                self.cinai_status.emit({"ok": False, "error": " ".join(str(exc).split())[:120]})
        finally:
            self._cinai_lock.release()

    def _cinai_loop(self) -> None:
        last_prefresh = 0.0
        self._stop.wait(8)
        while not self._stop.is_set():
            if time.time() - last_prefresh >= self.CINAI_PREFRESH_SEC and engine_link.cfg("CINAI_ACCESS_TOKEN", ""):
                last_prefresh = time.time()
                try:
                    result = engine_link.unit("cinai_login").ensure_fresh_token()
                    if result:
                        hours = int((result.get("expires_in") or 0) // 3600)
                        self.note.emit(f"CinAI: сессия продлена на {hours} ч / session extended for {hours} h", "INFO")
                except Exception as exc:  # noqa: BLE001
                    logging.debug("cinai prefresh: %s", exc)
            self._cinai_fetch()
            if self._stop.wait(self.CINAI_SEC):
                return


def quota_texts(data: dict) -> dict:
    """Готовые строки для отображения квоты GPT (5 ч и неделя)."""
    if not data or not data.get("ok"):
        return {"ok": False, "error": str((data or {}).get("error", "—"))}
    try:
        fmt = engine_link.unit("codex_quota").format_time_delta
    except Exception:  # noqa: BLE001
        def fmt(sec):
            return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"
    now = time.time()
    p_pct = int(data.get("primary_pct", 0) or 0)
    s_pct = int(data.get("secondary_pct", 0) or 0)
    return {
        "ok": True,
        "plan": str(data.get("plan_type", "?")),
        "source": str(data.get("source", "")),
        "p_pct": p_pct,
        "s_pct": s_pct,
        "p_reset": fmt(int(max(0, (data.get("primary_resets_at") or now) - now))),
        "s_reset": fmt(int(max(0, (data.get("secondary_resets_at") or now) - now))),
    }


def tone_for_pct(pct: int) -> str:
    return "ok" if pct < 50 else ("warn" if pct < 80 else "err")
