"""Browser-backed Envato Elements stock provider.

Envato Elements does not expose a public search/download API for this workflow.
This provider uses a user-owned Playwright profile and the normal site UI:
search -> asset page -> Download. Session cookies are imported only into the
local Playwright profile and are never written to the project or .env.
"""
from __future__ import annotations

import atexit
import json
import logging
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlparse

import params as config
from units.stock_lookup import StockResult, _words

logger = logging.getLogger(__name__)

_CLIENT = None
_CLIENT_LOCK = threading.RLock()
_DOWNLOAD_TIMEOUT_MS = 90_000
_ASSET_RE = re.compile(r"^/[a-z0-9][a-z0-9-]*-([a-z0-9]{7})/?$", re.I)


def _is_asset_href(href: str) -> bool:
    parsed = urlparse(href)
    if parsed.netloc and parsed.netloc.lower() != "elements.envato.com":
        return False
    match = _ASSET_RE.match(parsed.path)
    return bool(match and any(ch.isdigit() for ch in match.group(1)))


def _cookie_items(path: str | Path) -> List[Dict[str, Any]]:
    """Read Playwright storage-state or common browser cookie JSON exports."""
    cookie_path = Path(path).expanduser()
    if not cookie_path.is_file():
        raise ValueError(f"Envato cookie file not found: {cookie_path}")
    raw = json.loads(cookie_path.read_text(encoding="utf-8-sig"))
    if isinstance(raw, dict) and isinstance(raw.get("cookies"), list):
        raw = raw["cookies"]
    if not isinstance(raw, list):
        raise ValueError("Envato cookie JSON must be a list or storage-state object")

    out: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name, value = item.get("name"), item.get("value")
        domain = item.get("domain") or ".envato.com"
        if not name or value is None:
            continue
        if "envato.com" not in str(domain).lower():
            continue
        cookie = {
            "name": str(name),
            "value": str(value),
            "domain": str(domain),
            "path": str(item.get("path") or "/"),
        }
        expires = item.get("expires", item.get("expirationDate"))
        if expires not in (None, "", 0, -1):
            try:
                cookie["expires"] = float(expires)
            except (TypeError, ValueError):
                pass
        for key in ("httpOnly", "secure"):
            if key in item:
                cookie[key] = bool(item[key])
        same_site = item.get("sameSite")
        if same_site in {"Strict", "Lax", "None"}:
            cookie["sameSite"] = same_site
        out.append(cookie)
    if not out:
        raise ValueError("No Envato cookies found in cookie JSON")
    return out


class EnvatoBrowserClient:
    def __init__(self) -> None:
        configured_profile = Path(
            getattr(config, "ENVATO_BROWSER_PROFILE", "") or
            (Path(__file__).resolve().parents[1] / ".envato_profile")
        ).expanduser()
        self.browser_root = configured_profile
        user_data = configured_profile / "User Data"
        self.profile_dir = user_data if user_data.is_dir() else configured_profile
        self.cookie_json = (getattr(config, "ENVATO_COOKIE_JSON", "") or "").strip()
        executable = (getattr(config, "ENVATO_BROWSER_EXECUTABLE", "") or "").strip()
        if not executable:
            for name in ("chrome.exe", "chromium.exe", "chrome"):
                candidate = configured_profile / name
                if candidate.is_file():
                    executable = str(candidate)
                    break
        self.executable_path = executable
        self.headless = bool(getattr(config, "ENVATO_HEADLESS", True))
        self._playwright = None
        self._context = None
        self._page = None
        self._cookies_loaded = False

    def _page_or_start(self):
        if self._page is not None and not self._page.is_closed():
            return self._page
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Envato requires Playwright. Run: python -m pip install playwright "
                "&& python -m playwright install chromium"
            ) from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        launch_kwargs = {
            "user_data_dir": str(self.profile_dir),
            "headless": self.headless,
            "accept_downloads": True,
            "viewport": {"width": 1440, "height": 950},
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if self.executable_path:
            launch_kwargs["executable_path"] = self.executable_path
        self._context = self._playwright.chromium.launch_persistent_context(**launch_kwargs)
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._page.set_default_timeout(20_000)
        if self.cookie_json and not self._cookies_loaded:
            self._context.add_cookies(_cookie_items(self.cookie_json))
            self._cookies_loaded = True
        return self._page

    def _close(self) -> None:
        try:
            if self._context:
                self._context.close()
        except Exception:
            pass
        try:
            if self._playwright:
                self._playwright.stop()
        except Exception:
            pass
        self._context = self._playwright = self._page = None

    def search(self, query: str, media_type: str, limit: int) -> List[StockResult]:
        page = self._page_or_start()
        section = "stock-video" if media_type == "video" else "stock-photos"
        url = f"https://elements.envato.com/{section}/{quote(query.strip())}"
        page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(2_000)

        results: List[StockResult] = []
        seen = set()
        links = page.locator("a[href]").all()
        for link in links:
            href = link.get_attribute("href") or ""
            if not _is_asset_href(href):
                continue
            if href.startswith("/"):
                href = "https://elements.envato.com" + href
            if href in seen or href.rstrip("/") == url.rstrip("/"):
                continue
            seen.add(href)
            title = (link.inner_text(timeout=2_000) or "").strip()
            results.append(StockResult(
                source="envato",
                media_type=media_type,
                url=href,
                page_url=href,
                title=title or href.rsplit("/", 1)[-1].replace("-", " "),
                keywords=_words(f"{title} {href}"),
                license_name="Envato Elements subscription",
            ))
            if len(results) >= max(1, int(limit)):
                break
        return results

    @staticmethod
    def _visible(locator) -> bool:
        try:
            return locator.is_visible()
        except Exception:
            return False

    def _download_click(self, page, timeout_ms: int = 8_000):
        candidates = [
            page.get_by_role("button", name=re.compile(r"download|скачать", re.I)),
            page.get_by_text(re.compile(r"^download$|^скачать$", re.I)),
        ]
        for locator in candidates:
            try:
                count = locator.count()
            except Exception:
                count = 0
            for idx in range(min(count, 8)):
                item = locator.nth(idx)
                if not self._visible(item):
                    continue
                try:
                    with page.expect_download(timeout=timeout_ms) as event:
                        item.click()
                    return event.value
                except Exception:
                    # A click may only advance the project/license dialog.
                    page.wait_for_timeout(500)
        return None

    def _finish_download_dialog(self, page):
        project_name = str(getattr(config, "ENVATO_PROJECT_NAME", "") or "BegoFrame")
        for selector in (
            "input[placeholder*='Project' i]",
            "input[aria-label*='Project' i]",
            "input[name*='project' i]",
        ):
            field = page.locator(selector).first
            if not self._visible(field):
                continue
            try:
                field.fill(project_name)
                page.wait_for_timeout(300)
            except Exception:
                pass

        action_re = re.compile(
            r"create project|add to project|use project|continue|download",
            re.I,
        )
        actions = page.get_by_text(action_re)
        try:
            count = actions.count()
        except Exception:
            count = 0
        for idx in range(min(count, 12)):
            item = actions.nth(idx)
            if not self._visible(item):
                continue
            try:
                item.click()
                page.wait_for_timeout(700)
            except Exception:
                continue
            download = self._download_click(page, timeout_ms=_DOWNLOAD_TIMEOUT_MS)
            if download is not None:
                return download
        return self._download_click(page, timeout_ms=_DOWNLOAD_TIMEOUT_MS)

    def download(self, asset_url: str, destination: Path) -> bool:
        page = self._page_or_start()
        page.goto(asset_url, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(1_500)

        download = self._download_click(page)
        if download is None:
            download = self._finish_download_dialog(page)
        if download is None:
            if urlparse(page.url).path.rstrip("/") == "/pricing":
                raise RuntimeError(
                    "Envato session is not authorized for downloads: "
                    "sign in to an account with an active Elements subscription "
                    "in the selected browser profile"
                )
            raise RuntimeError(
                f"Envato download did not start; current page: {page.url}"
            )

        destination.parent.mkdir(parents=True, exist_ok=True)
        download.save_as(str(destination))
        return destination.is_file() and destination.stat().st_size > 1024


def _client() -> EnvatoBrowserClient:
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            _CLIENT = EnvatoBrowserClient()
        return _CLIENT


def search_envato(query: str, media_type: str = "video", limit: int = 20,
                  target_w: int = 1920, target_h: int = 1080) -> List[StockResult]:
    del target_w, target_h
    with _CLIENT_LOCK:
        return _client().search(query, media_type, limit)


def download_envato_asset(asset_url: str, destination: Path) -> bool:
    with _CLIENT_LOCK:
        try:
            return _client().download(asset_url, destination)
        except Exception as exc:
            logger.warning("envato: download failed: %s: %s",
                           type(exc).__name__, exc)
            return False


@atexit.register
def _shutdown() -> None:
    if _CLIENT is not None:
        _CLIENT._close()
