"""CinAI.tv image generation client.

This client targets the authenticated Studio reverse API documented in
``BEGO_API/docs/CINAI_TV_REVERSE_API.md``. It intentionally keeps the
credential in configuration and never prints authorization headers.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

import requests
try:  # файл появляется под своим именем только целиком (см. units/media_io.py)
    from units.media_io import atomic_open, atomic_target, write_bytes_atomic
except ImportError:                                               # pragma: no cover
    from media_io import atomic_open, atomic_target, write_bytes_atomic

import params as config
from prompt_extract import extract_all_prompts, find_prompts_file


CINAI_STUDIO_URL = "https://cinai.tv/StudioChat?tab=image"
_TOKEN_STORAGE_KEYS = (
    "base44_access_token",
    "access_token",
    "token",
)
_REFRESH_TOKEN_STORAGE_KEY = "refresh_token"

def _jwt_expiry_epoch(token: Optional[str]) -> Optional[int]:
    value = (token or "").strip()
    parts = value.split(".")
    if len(parts) != 3:
        return None
    try:
        padded = parts[1] + ("=" * (-len(parts[1]) % 4))
        claims = json.loads(base64.urlsafe_b64decode(padded))
        expiry = claims.get("exp")
        return int(expiry) if isinstance(expiry, (int, float)) else None
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def token_expires_in(token: Optional[str] = None, now: Optional[float] = None) -> Optional[int]:
    expiry = _jwt_expiry_epoch(token)
    if expiry is None:
        return None
    return int(expiry - (time.time() if now is None else now))


def _persist_local_env_value(key: str, value: str) -> None:
    """Update only the API master's local .env; never copy credentials to portable."""
    env_path = Path(config.__file__).resolve().parent / ".env"
    existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    lines = existing.splitlines()
    replacement = f"{key}={value}"
    for index, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[index] = replacement
            break
    else:
        lines.append(replacement)
    temp_path = env_path.with_name(f"{env_path.name}.refresh-tmp")
    temp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(temp_path, env_path)


def persist_cinai_credentials(access_token: str, refresh_token: Optional[str] = None) -> None:
    """Persist the current local session pair without exposing either secret."""
    access = (access_token or "").strip()
    refresh = (refresh_token or "").strip()
    if not access:
        raise CinAIAuthError("CinAI refresh returned no access token")
    config.CINAI_ACCESS_TOKEN = access
    os.environ["CINAI_ACCESS_TOKEN"] = access
    _persist_local_env_value("CINAI_ACCESS_TOKEN", access)
    if refresh:
        config.CINAI_REFRESH_TOKEN = refresh
        os.environ["CINAI_REFRESH_TOKEN"] = refresh
        _persist_local_env_value("CINAI_REFRESH_TOKEN", refresh)


def refresh_cinai_session(
    refresh_token: Optional[str] = None,
    *,
    base_url: Optional[str] = None,
    timeout: Optional[int] = None,
) -> Dict[str, str]:
    """Refresh the Studio session through HTTP, without a browser."""
    refresh = (refresh_token or getattr(config, "CINAI_REFRESH_TOKEN", "")).strip()
    if not refresh:
        raise CinAIAuthError(
            "CinAI access token expired and no CINAI_REFRESH_TOKEN is configured; "
            "authorize once in Settings"
        )
    endpoint = (
        base_url or getattr(config, "CINAI_API_BASE", "https://api.cinai.tv")
    ).rstrip("/") + "/api/auth/refresh"
    try:
        response = requests.post(
            endpoint,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            json={"refresh_token": refresh},
            timeout=(30, int(timeout or getattr(config, "CINAI_TIMEOUT", 900))),
        )
    except requests.RequestException as exc:
        raise CinAIRetryableError(
            f"CinAI token refresh failed: {_clean_error(str(exc))}"
        ) from exc
    if response.status_code in (401, 403):
        raise CinAIAuthError("CinAI refresh token was rejected; authorize once in Settings")
    if not response.ok:
        raise CinAIError(
            f"CinAI token refresh failed ({response.status_code}): "
            f"{_clean_error(response.text)}"
        )
    try:
        payload = response.json() or {}
    except ValueError as exc:
        raise CinAIError("CinAI token refresh returned invalid JSON") from exc
    access = str(payload.get("access_token") or payload.get("token") or "").strip()
    rotated_refresh = str(payload.get("refresh_token") or "").strip()
    persist_cinai_credentials(access, rotated_refresh or refresh)
    return {
        "access_token": access,
        "refresh_token": rotated_refresh or refresh,
    }


def ensure_cinai_access_token(
    access_token: Optional[str] = None,
    *,
    refresh_token: Optional[str] = None,
    force_refresh: bool = False,
    skew_seconds: Optional[int] = None,
) -> str:
    """Return a usable token, refreshing it headlessly when it is near expiry."""
    token = (access_token or getattr(config, "CINAI_ACCESS_TOKEN", "")).strip()
    if not token:
        raise CinAIAuthError("CINAI_ACCESS_TOKEN is not configured")
    skew = max(
        0,
        int(
            skew_seconds
            if skew_seconds is not None
            else getattr(config, "CINAI_TOKEN_REFRESH_SKEW_SECONDS", 300)
        ),
    )
    remaining = token_expires_in(token)
    if not force_refresh and (remaining is None or remaining > skew):
        return token
    refreshed = refresh_cinai_session(refresh_token)
    return refreshed["access_token"]


class CinAIError(RuntimeError):
    """Base error for CinAI requests."""


class CinAIAuthError(CinAIError):
    """The configured bearer token was rejected."""


class CinAIRetryableError(CinAIError):
    """A transient HTTP, stream, or download failure."""


def _clean_error(text: str, limit: int = 300) -> str:
    text = (text or "").replace("\r", " ").replace("\n", " ").strip()
    return text[:limit]


# Классификация ошибок провайдера — единая для image и video клиентов
# (video импортирует отсюда). Список маркеров — из журнала аудита
# docs/CINAI_TV_VIDEO_AUDIT.md (Error Classification for Pipeline Retries).
def _is_retryable_message(text: str) -> bool:
    value = (text or "").lower()
    markers = (
        "temporary error interrupted generation",
        "temporary model",
        "provider halcion unavailable",
        "generation failed. submit the request again",
        "submit the request again",
        "reconnecting",
        "stream disconnected",
        "billing to be enabled",
        "vertex 403",
        # ── сигнатуры из журнала самого CinAI (GET /api/executions) ──
        # воркер провайдера перестал отвечать и запуск бросили
        "worker heartbeat timeout",
        "execution was orphaned",
        # circuit breaker upstream-провайдера ("Provider cinapi unavailable:
        # open (retry in 53s)") — ждём и пробуем снова
        "unavailable: open",
        "unavailable: half_open",
        "provider cinapi unavailable",
        # модель отработала, но媒 не отдала (омни): "Omni interaction không trả media"
        "không trả media",
        "no media returned",
        # обрывы транспорта
        "response ended prematurely",
        "connection aborted",
        "connection reset",
        "stream closed before",
        "execution stream lost",
        # 5xx от апстрима внутри текста ошибки запуска
        "http 500",
        "http 502",
        "http 503",
        "http 504",
    )
    return any(marker in value for marker in markers)


_RETRY_AFTER_RE = re.compile(r"retry in (\d+(?:\.\d+)?)\s*s", re.IGNORECASE)


def retry_after_seconds(text: str) -> float:
    """«…unavailable: open (retry in 53s)» → 53.0. Нет подсказки → 0.

    Circuit breaker провайдера говорит, когда он снова откроется — долбить
    раньше бессмысленно, только жжём попытки."""
    match = _RETRY_AFTER_RE.search(str(text or ""))
    if not match:
        return 0.0
    try:
        return max(0.0, min(300.0, float(match.group(1))))
    except ValueError:
        return 0.0


def _is_content_decline(text: str) -> bool:
    value = (text or "").lower()
    return any(
        marker in value
        for marker in (
            "model declined",
            "copyrighted",
            "sensitive subject",
            "sensitive content",
            "content policy",
            "safety policy",
            "content violation",
            "blocked by safety",
            "reference image",
        )
    )


def _is_retryable_status(status: int) -> bool:
    return status == 429 or 500 <= status <= 599


def _safe_parallel(value: Optional[int]) -> int:
    try:
        value = int(value or getattr(config, "CINAI_MAX_PARALLEL", 6))
    except (TypeError, ValueError):
        value = 6
    return max(1, min(value, 6))


def _image_dimensions(payload: bytes) -> Tuple[int, int]:
    try:
        from PIL import Image

        with Image.open(BytesIO(payload)) as image:
            return image.size
    except Exception:
        return 0, 0


def _download_variants(output_url: str) -> List[str]:
    """Return the original CinAI URL plus likely full-size CDN variants."""
    urls = [output_url]
    parsed = urlsplit(output_url)
    parts = parsed.path.rstrip("/").split("/")
    if (
        parsed.netloc.lower() == "imagedelivery.net"
        and len(parts) >= 2
        and parts[-1].lower() == "md"
    ):
        for variant in ("original", "public", "full", "hd"):
            candidate = urlunsplit(
                (parsed.scheme, parsed.netloc, "/".join(parts[:-1] + [variant]), parsed.query, "")
            )
            if candidate not in urls:
                urls.insert(0, candidate)
    return urls


class CinAIClient:
    def __init__(
        self,
        access_token: Optional[str] = None,
        base_url: Optional[str] = None,
        app_id: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> None:
        self.access_token = ensure_cinai_access_token(access_token)
        self.base_url = (base_url or getattr(config, "CINAI_API_BASE", "https://api.cinai.tv")).rstrip("/")
        self.app_id = app_id or getattr(config, "CINAI_APP_ID", "runway-workflows")
        self.timeout = int(timeout or getattr(config, "CINAI_TIMEOUT", 900))
        if not self.access_token:
            raise CinAIAuthError("CINAI_ACCESS_TOKEN is not configured")

    def _refresh_auth_once(self) -> None:
        self.access_token = ensure_cinai_access_token(
            self.access_token,
            force_refresh=True,
        )

    def _headers(self, accept: str = "application/json") -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "X-App-Id": self.app_id,
            "Accept": accept,
            "Content-Type": "application/json",
        }

    def get_billing_balance(self, _auth_retry: bool = False) -> Dict[str, Any]:
        response = requests.post(
            f"{self.base_url}/api/functions/getBillingBalance",
            headers=self._headers(),
            timeout=(30, 60),
        )
        if response.status_code in (401, 403):
            if not _auth_retry:
                self._refresh_auth_once()
                return self.get_billing_balance(_auth_retry=True)
            raise CinAIAuthError("CinAI access token was rejected")
        if not response.ok:
            raise CinAIError(
                f"CinAI billing request failed ({response.status_code}): "
                f"{_clean_error(response.text)}"
            )
        return response.json()

    def get_model_perks_status(self, _auth_retry: bool = False) -> Dict[str, Any]:
        """Return the signed-in account's current per-model perk/quota state."""
        response = requests.get(
            f"{self.base_url}/api/model-perks/status",
            headers=self._headers(),
            timeout=(30, 60),
        )
        if response.status_code in (401, 403):
            if not _auth_retry:
                self._refresh_auth_once()
                return self.get_model_perks_status(_auth_retry=True)
            raise CinAIAuthError("CinAI access token was rejected")
        if not response.ok:
            raise CinAIError(
                f"CinAI model perks request failed ({response.status_code}): "
                f"{_clean_error(response.text)}"
            )
        return response.json()

    def _parse_sse(self, response: requests.Response) -> Dict[str, Any]:
        event_name: Optional[str] = None
        data_lines: List[str] = []
        terminal: Optional[Dict[str, Any]] = None

        def consume() -> Optional[Dict[str, Any]]:
            nonlocal event_name, data_lines
            if not data_lines:
                event_name = None
                return None
            raw = "\n".join(data_lines)
            data_lines = []
            current_event = event_name
            event_name = None
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                logging.debug("CinAI ignored non-JSON SSE event %s", current_event)
                return None
            if current_event == "error" or payload.get("error"):
                message = payload.get("error") or payload.get("message") or "CinAI stream error"
                text = _clean_error(str(message))
                # Transient-ошибки провайдера ретраятся (как в video-клиенте);
                # контент-отказы и прочее — честный фейл без повторов.
                if _is_retryable_message(text):
                    raise CinAIRetryableError(text)
                raise CinAIError(text)
            if current_event == "result" or payload.get("result", {}).get("output_url"):
                return payload.get("result", payload)
            return None

        for raw_line in response.iter_lines(decode_unicode=True):
            line = raw_line or ""
            if not line:
                result = consume()
                if result is not None:
                    terminal = result
                    break
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())

        if terminal is None:
            terminal = consume()
        if not terminal or not terminal.get("output_url"):
            # Обрыв SSE без терминального результата — transient по журналу
            # аудита («incomplete SSE before terminal result → retry»).
            # Раньше кидался плоский CinAIError → клип падал БЕЗ ретраев
            # (инцидент clip #24, 15.08 05:19), хотя video-клиент такой же
            # обрыв ретраит.
            raise CinAIRetryableError("CinAI stream ended without an image output_url")
        return terminal

    def generate_image(
        self,
        prompt: str,
        model: Optional[str] = None,
        aspect_ratio: Optional[str] = None,
        quality: Optional[str] = None,
        _auth_retry: bool = False,
    ) -> Dict[str, Any]:
        model = model or config.CINAI_MODEL
        aspect_ratio = aspect_ratio or getattr(config, "CINAI_ASPECT_RATIO", "16:9")
        quality = (quality or getattr(config, "CINAI_QUALITY", "1K")).upper()
        from units.frame_format import with_frame_format
        prompt = with_frame_format(prompt, aspect_ratio)     # формат и текстом: не все модели знают поле
        directives = [
            {"key": "aspect_ratio", "value": aspect_ratio, "origin": "ui_control", "turn": 1},
            {"key": "image_count", "value": 1, "origin": "ui_control", "turn": 1},
            {"key": "model_id", "value": model, "origin": "ui_control", "turn": 1},
        ]
        body = {
            "prompt": prompt,
            "payload": {
                "workflow_context": None,
                "surface": "studio-image",
                "chat_history": [],
                "mode_hint": "image",
                "model_preferences": {"image": model},
                "aspect_ratio": aspect_ratio,
                "image_count": 1,
                "settings": {"resolution": quality},
                "context_envelope": {
                    "v": 1,
                    "items": [],
                    "directives": directives,
                    "prompt": prompt,
                },
                "client_request_id": str(uuid.uuid4()),
            },
        }
        try:
            response = requests.post(
                f"{self.base_url}/api/agent/dispatch/stream",
                headers=self._headers("text/event-stream"),
                json=body,
                stream=True,
                timeout=(30, self.timeout),
            )
        except requests.RequestException as exc:
            raise CinAIRetryableError(f"CinAI connection failed: {_clean_error(str(exc))}") from exc
        if response.status_code in (401, 403):
            if not _auth_retry:
                self._refresh_auth_once()
                return self.generate_image(
                    prompt,
                    model=model,
                    aspect_ratio=aspect_ratio,
                    quality=quality,
                    _auth_retry=True,
                )
            raise CinAIAuthError("CinAI access token was rejected")
        if _is_retryable_status(response.status_code):
            raise CinAIRetryableError(
                f"CinAI generation returned HTTP {response.status_code}"
            )
        if not response.ok:
            raise CinAIError(
                f"CinAI generation failed ({response.status_code}): "
                f"{_clean_error(response.text)}"
            )
        try:
            return self._parse_sse(response)
        finally:
            response.close()

    def download_image(self, output_url: str, output_path: Path) -> None:
        best_payload = b""
        best_size = (0, 0)
        last_status: Optional[int] = None

        for candidate_url in _download_variants(output_url):
            try:
                response = requests.get(candidate_url, timeout=(30, 180))
            except requests.RequestException as exc:
                logging.debug("CinAI image download failed for %s: %s", candidate_url, exc)
                continue
            last_status = response.status_code
            if not response.ok:
                continue
            payload = response.content
            size = _image_dimensions(payload)
            if size[0] * size[1] > best_size[0] * best_size[1]:
                best_payload = payload
                best_size = size

        if not best_payload:
            if last_status == 429 or (last_status is not None and last_status >= 500):
                raise CinAIRetryableError(
                    f"CinAI image download returned HTTP {last_status}"
                )
            raise CinAIError(
                f"CinAI image download failed ({last_status or 'network error'})"
            )

        if max(best_size) and max(best_size) < 768:
            raise CinAIRetryableError(
                f"CinAI returned only a {best_size[0]}x{best_size[1]} preview, not a usable image"
            )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        write_bytes_atomic(output_path, best_payload)
        logging.info(
            "CinAI image downloaded: %s -> %s (%sx%s)",
            output_url,
            output_path.name,
            best_size[0],
            best_size[1],
        )

    def upload_image(self, image_path: str, _auth_retry: bool = False) -> str:
        path = Path(image_path)
        if not path.is_file():
            raise FileNotFoundError(path)
        mime = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
        }.get(path.suffix.lower(), "application/octet-stream")
        headers = self._headers()
        headers.pop("Content-Type", None)
        try:
            with path.open("rb") as handle:
                response = requests.post(
                    f"{self.base_url}/api/integrations/Core/UploadFile",
                    headers=headers,
                    files={"file": (path.name, handle, mime)},
                    timeout=(30, 180),
                )
        except requests.RequestException as exc:
            raise CinAIRetryableError(
                f"CinAI upload failed: {_clean_error(str(exc))}"
            ) from exc
        if response.status_code in (401, 403):
            if not _auth_retry:
                self._refresh_auth_once()
                return self.upload_image(image_path, _auth_retry=True)
            raise CinAIAuthError("CinAI access token was rejected")
        if not response.ok:
            raise CinAIError(
                f"CinAI upload failed ({response.status_code}): "
                f"{_clean_error(response.text)}"
            )
        file_url = (response.json().get("file_url") or "").strip()
        if not file_url:
            raise CinAIError("CinAI upload returned no file_url")
        return file_url

    def edit_image(
        self,
        image_path: str,
        prompt: str,
        output_path: str,
        model: Optional[str] = None,
        aspect_ratio: Optional[str] = None,
        quality: Optional[str] = None,
        _auth_retry: bool = False,
    ) -> str:
        model = model or config.CINAI_REMIX_MODEL
        image_url = self.upload_image(image_path)
        aspect_ratio = aspect_ratio or getattr(config, "CINAI_ASPECT_RATIO", "16:9")
        quality = (quality or getattr(config, "CINAI_QUALITY", "1K")).upper()
        from units.frame_format import with_frame_format
        prompt = with_frame_format(prompt, aspect_ratio)
        body = {
            "prompt": prompt,
            "payload": {
                "workflow_context": None,
                "surface": "studio-image",
                "chat_history": [],
                "mode_hint": "image",
                "model_preferences": {"image": model},
                "aspect_ratio": aspect_ratio,
                "image_count": 1,
                "settings": {"resolution": quality},
                "images": [image_url],
                "context_envelope": {
                    "v": 1,
                    "items": [{
                        "idx": 0,
                        "kind": "image",
                        "url": image_url,
                        "origin": "composer_upload",
                        "role": None,
                    }],
                    "directives": [
                        {
                            "key": "aspect_ratio",
                            "value": aspect_ratio,
                            "origin": "ui_control",
                            "turn": 1,
                        },
                        {
                            "key": "image_count",
                            "value": 1,
                            "origin": "ui_control",
                            "turn": 1,
                        },
                        {
                            "key": "model_id",
                            "value": model,
                            "origin": "ui_control",
                            "turn": 1,
                        },
                    ],
                    "prompt": prompt,
                },
                "client_request_id": str(uuid.uuid4()),
            },
        }
        try:
            response = requests.post(
                f"{self.base_url}/api/agent/dispatch/stream",
                headers=self._headers("text/event-stream"),
                json=body,
                stream=True,
                timeout=(30, self.timeout),
            )
        except requests.RequestException as exc:
            raise CinAIRetryableError(
                f"CinAI edit failed: {_clean_error(str(exc))}"
            ) from exc
        if response.status_code in (401, 403):
            if not _auth_retry:
                self._refresh_auth_once()
                return self.edit_image(
                    image_path,
                    prompt,
                    output_path,
                    model=model,
                    aspect_ratio=aspect_ratio,
                    quality=quality,
                    _auth_retry=True,
                )
            raise CinAIAuthError("CinAI access token was rejected")
        if not response.ok:
            raise CinAIError(
                f"CinAI edit failed ({response.status_code}): "
                f"{_clean_error(response.text)}"
            )
        try:
            result = self._parse_sse(response)
        finally:
            response.close()
        target = Path(output_path)
        self.download_image(str(result["output_url"]), target)
        return str(target)


def remix_image(
    image_path: str,
    prompt: str,
    output_path: str,
    model: Optional[str] = None,
    aspect_ratio: Optional[str] = None,
    quality: Optional[str] = None,
) -> str:
    """Перерисовка картинки по промпту (ремиксер CinAI).

    Транспорт тот же, что у generate_all_images: workflow — граф
    «INPUT-нода с кадром → ребро → img2img-нода», промпт доходит дословно;
    agent — legacy dispatch/stream (с 15.08 агент переписывает промпт)."""
    model = model or config.CINAI_REMIX_MODEL
    transport = str(getattr(config, "CINAI_TRANSPORT", "workflow") or "workflow").lower()
    if transport != "workflow":
        return CinAIClient().edit_image(
            image_path=image_path, prompt=prompt, output_path=output_path,
            model=model, aspect_ratio=aspect_ratio, quality=quality)

    from units.cinai_graph import (
        CinAIWorkflowClient, ModelCatalog, attach_images, build_generator_node, download_media,
    )
    aspect_ratio = aspect_ratio or getattr(config, "CINAI_ASPECT_RATIO", "16:9")
    quality = (quality or getattr(config, "CINAI_QUALITY", "1K")).upper()
    client = CinAIWorkflowClient()
    catalog = ModelCatalog.load(client)
    # Настройки, которых у модели нет, build_settings отсеет: у модели без
    # aspect_ratio формат уходит только строкой в промпте (её дописывает
    # build_generator_node). scene_preset=off — без «скиллов» студии поверх промпта.
    wanted_settings = {
        "aspect_ratio": aspect_ratio,
        "resolution": quality,
        "max_number": 1,
        "scene_preset": "off",
    }
    # ранний отказ до трат (charge_phase=pre): чужая настройка или нет порта картинки
    catalog.build_settings(model, wanted_settings)
    if catalog.image_port(model) is None:
        raise CinAIError(f"CinAI model {model} has no image input — it cannot remix")
    # кадр заливаем один раз: повторы графа ссылаются на тот же URL
    image_url = client.upload_image(image_path)

    def build() -> Dict[str, Any]:
        node = build_generator_node(catalog, model, prompt, wanted_settings)
        input_nodes, edges = attach_images(catalog, node, [image_url])
        return {"nodes": input_nodes + [node], "edges": edges, "target": node["id"]}

    result = client.run_with_retries(build, title=f"BegoFrame remix {Path(image_path).stem}")
    target = Path(output_path)
    download_media(str(result["url"]), target, min_bytes=1024)
    logging.info("CinAI remix: %s -> %s (%s, %s attempt(s))", Path(image_path).name,
                 target.name, model, result.get("attempts", 1))
    return str(target)


def _load_prompts(
    project_path: Path,
    clip_ids: Optional[Iterable[int]],
    include_infographics: bool = False,
) -> Dict[int, str]:
    prompts_file = find_prompts_file(str(project_path))
    if not prompts_file:
        raise FileNotFoundError(f"Prompts file not found in {project_path}")
    prompts = extract_all_prompts(prompts_file)
    if not prompts:
        raise ValueError(f"No prompts found in {prompts_file}")
    if clip_ids is not None:
        wanted = {int(value) for value in clip_ids}
        prompts = {cid: text for cid, text in prompts.items() if cid in wanted}
    # Единый source-фильтр как в imggen.py/fastgen.py: внешние стадии
    # (real_photo + сток) обслуживают свои клипы сами, инфографика идёт через
    # выделенный маршрут (include_infographics=True). Раньше здесь резались
    # только real_photo* — CinAI генерил картинки поверх стока (jpg от стока
    # против png от AI — конкат молча брал png) и поверх инфографики.
    # Заодно: чтение через utf-8-sig + разворачивание обёртки montage_plan —
    # как во всех остальных модулях (старый вариант ломался на BOM/обёртке).
    montage_path = project_path / "montage_plan.json"
    if montage_path.exists():
        try:
            from units.source_ledger import EXTERNAL_SOURCES, INFOGRAPHIC_SOURCES
            raw = json.loads(montage_path.read_text(encoding="utf-8-sig"))
            plan = raw.get("montage_plan", raw)
            skip_sources = (
                EXTERNAL_SOURCES if include_infographics
                else (EXTERNAL_SOURCES | INFOGRAPHIC_SOURCES)
            )
            skip_ids = {
                int(clip["id"])
                for clip in plan.get("clips", [])
                if clip.get("id") is not None
                and clip.get("source") in skip_sources
            }
        except (OSError, ValueError, TypeError, KeyError) as exc:
            logging.warning("CinAI could not read source markers: %s", exc)
        else:
            skipped_ids = sorted(skip_ids.intersection(prompts))
            if skipped_ids:
                prompts = {
                    clip_id: prompt
                    for clip_id, prompt in prompts.items()
                    if clip_id not in skip_ids
                }
                logging.info(
                    "CinAI skipping %d external/static clip(s): %s",
                    len(skipped_ids),
                    ", ".join(f"{clip_id:03d}" for clip_id in skipped_ids),
                )
    return prompts


def generate_all_images(
    project_dir: str,
    max_parallel: Optional[int] = None,
    clip_ids: Optional[List[int]] = None,
    model: Optional[str] = None,
    aspect_ratio: Optional[str] = None,
    quality: Optional[str] = None,
    silent: bool = False,
    include_infographics: bool = False,
    **_: Any,
) -> Dict[str, Any]:
    project_path = Path(project_dir)
    output_dir = project_path / "generated"
    output_dir.mkdir(parents=True, exist_ok=True)
    prompts = _load_prompts(
        project_path, clip_ids, include_infographics=include_infographics)
    skipped: List[int] = []
    pending: Dict[int, str] = {}
    for clip_id, prompt in prompts.items():
        existing = next(
            (output_dir / f"{clip_id:03d}{ext}" for ext in (".png", ".jpg", ".jpeg", ".webp")
             if (output_dir / f"{clip_id:03d}{ext}").is_file()
             and (output_dir / f"{clip_id:03d}{ext}").stat().st_size > 0),
            None,
        )
        if existing:
            skipped.append(clip_id)
        else:
            pending[clip_id] = prompt

    if not pending:
        return {"generated": len(skipped), "failed": 0, "skipped": len(skipped), "results": []}

    workers = _safe_parallel(max_parallel)
    retries = max(0, int(getattr(config, "CINAI_MAX_RETRIES", 3)))
    results: List[Dict[str, Any]] = []
    failed: List[int] = []

    requested_model = model or config.CINAI_MODEL
    requested_ratio = aspect_ratio or getattr(config, "CINAI_ASPECT_RATIO", "16:9")
    requested_quality = (quality or getattr(config, "CINAI_QUALITY", "1K")).upper()

    # Транспорт: workflow (прямой, дефолт) | agent (legacy dispatch/stream).
    # Агентный путь с 15.08 переписывает промпт LLM'ом и задаёт опросник —
    # см. docs/CINAI_WORKFLOW_API_PLAN.md. Флаг оставлен на случай отката
    # провайдера к старому контракту.
    transport = str(getattr(config, "CINAI_TRANSPORT", "workflow") or "workflow").lower()

    if transport == "workflow":
        from units.cinai_graph import (
            CinAIWorkflowClient, ModelCatalog, build_generator_node, download_media,
        )
        wf_client = CinAIWorkflowClient()
        catalog = ModelCatalog.load(wf_client)
        # scene_preset=off: иначе студия накладывает свои «кинематографичные
        # скиллы» поверх промпта режиссёра
        wanted_settings = {
            "aspect_ratio": requested_ratio,
            "resolution": requested_quality,
            "max_number": 1,
            "scene_preset": "off",
        }
        # Модель без aspect_ratio ВСЕГДА отдаёт квадрат — заказанный формат
        # (16:9/9:16) она игнорирует. В форматном пайплайне это порча всего
        # ролика (инцидент ВВВ_20260816_0343: выбрали grok-image-i2i → 480×480).
        # Молча пропускать нельзя: громко предупреждаем в лог.
        if requested_ratio not in ("1:1", None, ""):
            _has_aspect = ("aspect_ratio" in catalog.settings(requested_model)
                           or catalog.ratio_key(requested_model))
            if not _has_aspect:
                msg = (f"CinAI: модель {requested_model} НЕ поддерживает "
                       f"соотношение сторон — кадры будут КВАДРАТНЫМИ, а не "
                       f"{requested_ratio}. Выберите модель с aspect_ratio "
                       f"(любая из CINAI_FREE_IMAGE_MODELS).")
                logging.warning(msg)
                if not silent:
                    print(f"\n  ⚠️  {msg}\n", flush=True)
        # ранний отказ одной строкой, до трат (charge_phase=pre)
        catalog.build_settings(requested_model, wanted_settings)

        def generate_one(clip_id: int, prompt: str) -> Dict[str, Any]:
            def build():
                node = build_generator_node(
                    catalog, requested_model, prompt, wanted_settings)
                return {"nodes": [node], "edges": [], "target": node["id"]}

            result = wf_client.run_with_retries(
                build, title=f"BegoFrame img #{clip_id:03d}")
            url = str(result["url"])
            suffix = ".png"
            for ext in (".png", ".jpg", ".jpeg", ".webp"):
                if url.lower().split("?")[0].endswith(ext):
                    suffix = ext
                    break
            path = output_dir / f"{clip_id:03d}{suffix}"
            download_media(url, path, min_bytes=1024)
            return {"id": clip_id, "file": str(path), "url": url,
                    "model": requested_model, "attempts": result.get("attempts", 1)}
    else:
        client = CinAIClient()

        def generate_one(clip_id: int, prompt: str) -> Dict[str, Any]:
            last_error: Optional[Exception] = None
            for attempt in range(retries + 1):
                try:
                    result = client.generate_image(
                        prompt, model=model, aspect_ratio=aspect_ratio, quality=quality)
                    path = output_dir / f"{clip_id:03d}.png"
                    client.download_image(str(result["output_url"]), path)
                    return {"id": clip_id, "file": str(path), "url": result["output_url"],
                            "model": result.get("model_slug", model)}
                except (CinAIRetryableError, requests.RequestException) as exc:
                    last_error = exc
                    if attempt >= retries:
                        break
                    time.sleep(min(30, 2 ** attempt))
                except Exception as exc:
                    last_error = exc
                    break
            raise CinAIError(f"clip #{clip_id} failed: {_clean_error(str(last_error))}")

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="cinai") as executor:
        futures = {
            executor.submit(generate_one, cid, prompt): cid
            for cid, prompt in pending.items()
        }
        for future in as_completed(futures):
            clip_id = futures[future]
            try:
                result = future.result()
                results.append(result)
                print(f"  [CinAI] #{clip_id}: OK", flush=True)
            except Exception as exc:
                failed.append(clip_id)
                print(f"  [CinAI] #{clip_id}: FAILED - {_clean_error(str(exc))}", flush=True)
                logging.warning("CinAI clip %s failed: %s", clip_id, exc)

    return {
        "generated": len(results) + len(skipped),
        "failed": len(failed),
        "skipped": len(skipped),
        "results": sorted(results, key=lambda item: item["id"]),
        "failed_ids": sorted(failed),
    }
