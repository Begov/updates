"""CinAI.tv video generation client.

The image operator lives in ``units.cinai_stills``.  This module keeps the video
reverse-API contract separate until the provider has passed its local and live
acceptance gates.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import mimetypes
import os
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import requests

import params as config
from units.cinai_stills import (
    CinAIAuthError,
    CinAIError,
    CinAIRetryableError,
    _clean_error,
    _is_content_decline,
    _is_retryable_message,
    ensure_cinai_access_token,
    retry_after_seconds as _retry_after_seconds,
)
from prompt_extract import extract_all_prompts, find_prompts_file, remap_dense_prompts_for_montage
from units.source_ledger import VIDEO_SKIP_SOURCES


VIDEO_MODES = ("t2v", "i2v", "first_last", "reference", "components")


@dataclass(frozen=True)
class CinAIVideoProfile:
    slug: str
    modes: Tuple[str, ...]
    min_images: int = 0
    max_images: Optional[int] = 0
    reference_min_images: int = 3
    reference_max_images: Optional[int] = None
    aspect_ratios: Tuple[str, ...] = ("16:9",)
    resolutions: Tuple[str, ...] = ()
    min_duration: int = 3
    max_duration: int = 10
    # Non-empty means the Studio exposes discrete duration choices rather
    # than every integer in the min/max range.
    duration_values: Tuple[int, ...] = ()
    duration_wire: str = "string"
    resolution_wire: bool = False
    audio_wire: Optional[str] = None
    enabled: bool = True


VIDEO_PROFILES: Dict[str, CinAIVideoProfile] = {
    # Только бесплатные видеомодели: безлимитные на тарифе аккаунта
    # (pricing.plan_perks.<тариф>.type = unlimited, /api/model-perks/status),
    # проверены реальной генерацией t2v и i2v 11.10.2026. Квотные (Seedance 2.0
    # Lite, Omni Flash, Grok Video — 20 в месяц, дальше кредиты) и платные убраны.
    # Формат 1:1 ни одна из них не умеет. Настройки уходят через каталог модели
    # (ModelCatalog.build_settings) — профиль лишь отсекает неверный запрос заранее.
    "cinai-g-video": CinAIVideoProfile(
        slug="cinai-g-video",
        modes=("t2v", "i2v"),
        max_images=None,
        aspect_ratios=("16:9", "9:16"),
        min_duration=3,
        max_duration=10,
        duration_wire="seconds_suffix",
    ),
    "veo-3-1-fast-v2": CinAIVideoProfile(
        slug="veo-3-1-fast-v2",
        modes=("t2v", "i2v"),
        max_images=None,
        aspect_ratios=("16:9", "9:16"),
        resolutions=("720p",),
        min_duration=4,
        max_duration=8,
        duration_values=(4, 6, 8),
        duration_wire="seconds_suffix",
        resolution_wire=True,
    ),
    "veo-3-1-lite": CinAIVideoProfile(
        slug="veo-3-1-lite",
        modes=("t2v", "i2v"),
        max_images=None,
        aspect_ratios=("16:9", "9:16"),
        resolutions=("720p",),
        min_duration=4,
        max_duration=8,
        duration_values=(4, 6, 8),
        duration_wire="seconds_suffix",
        resolution_wire=True,
    ),
}


def snap_duration(model: str, seconds: Any) -> int:
    """Длительность, которую модель действительно умеет: ближайшая допустимая
    сверху (лишнее склейка подрежет), а если просят больше максимума — максимум.
    У Veo 3.1 Lite / Fast есть только 4, 6 и 8 с, и «5» из настроек иначе
    отвергалось бы до отправки."""
    profile = VIDEO_PROFILES.get((model or "").strip())
    try:
        value = int(round(float(seconds)))
    except (TypeError, ValueError):
        value = 5
    if profile is None:
        return value
    allowed = sorted(profile.duration_values) or list(range(profile.min_duration, profile.max_duration + 1))
    return next((d for d in allowed if d >= value), allowed[-1])


def scene_duration(catalog: Any, model: str, seconds: float) -> Optional[str]:
    """Длина клипа под сцену («Длительность под сцену»): ближайшая СВЕРХУ из тех,
    что умеет модель, как у VeoNonStop, — лишний хвост подрежет склейка, а
    растягивать короткий клип замедлением хуже. Длиннее максимума — максимум.

    Бесплатные модели берём по профилю (у Veo 3.1 Lite / Fast ряд 4/6/8, его
    «ближайшее» по каталогу пропускало). Незнакомые — по каталогу, там подбор
    включается лишь при плавном ряде длин (крупный ряд раздувал бы кредиты)."""
    if (model or "").strip() in VIDEO_PROFILES:
        try:
            wanted = math.ceil(float(seconds) - 1e-6)
        except (TypeError, ValueError):
            return None
        return str(snap_duration(model, wanted)) if wanted > 0 else None
    return catalog.nearest_duration(model, seconds)


class CinAIVideoUnsupportedError(CinAIError):
    """The requested model, mode, or setting is not in the audited contract."""


class CinAIVideoContentDeclinedError(CinAIError):
    """The provider rejected the content or references; retrying is wasteful."""


class CinAIVideoDownloadError(CinAIError):
    """The terminal URL did not yield a usable video file."""


def token_expiry_epoch(token: Optional[str] = None) -> Optional[int]:
    """Read a JWT ``exp`` claim without exposing the token or its payload."""
    value = (token or getattr(config, "CINAI_ACCESS_TOKEN", "")).strip()
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


def token_expires_in(
    token: Optional[str] = None,
    now: Optional[float] = None,
) -> Optional[int]:
    """Return whole seconds until JWT expiry; ``None`` means non-JWT/unknown."""
    expiry = token_expiry_epoch(token)
    if expiry is None:
        return None
    return int(expiry - (time.time() if now is None else now))


# NB: экспирация токена проверяется в ensure_cinai_access_token (units.cinai_stills)
# с CINAI_TOKEN_REFRESH_SKEW_SECONDS — свой дублирующий префлайт здесь был
# мёртвым кодом и удалён (аудит 2026-08-15).


# _is_retryable_message / _is_content_decline живут в units.cinai_stills —
# единая классификация ошибок для image и video клиентов (дедуп 15.08).


def get_video_profile(model: str) -> CinAIVideoProfile:
    profile = VIDEO_PROFILES.get((model or "").strip())
    if profile is None:
        raise CinAIVideoUnsupportedError(f"Unknown CinAI video model: {model!r}")
    if not profile.enabled:
        raise CinAIVideoUnsupportedError(
            f"CinAI video model {profile.slug!r} is not enabled until live payload capture"
        )
    return profile


def _duration_value(profile: CinAIVideoProfile, duration: int) -> str:
    # Формат снят с живых дисатчей: seedance-lite/grok шлют "10s" (с суффиксом),
    # omni/seedance-2-5 — "10" (plain). Раньше обе ветки возвращали plain и
    # поле duration_wire было мёртвым.
    if profile.duration_wire == "seconds_suffix":
        return f"{duration}s"
    return str(duration)


def _validate_static_settings(
    profile: CinAIVideoProfile,
    aspect_ratio: str,
    duration: int,
    resolution: Optional[str],
    audio: Optional[bool],
) -> None:
    """Model-level settings checks, independent of per-clip image counts.

    Вынесены отдельно, чтобы батч мог отказаться ОДНОЙ строкой до первого
    запроса (а не 43 одинаковыми ошибками посреди прогона)."""
    if aspect_ratio not in profile.aspect_ratios:
        raise CinAIVideoUnsupportedError(
            f"Model {profile.slug} does not support aspect ratio {aspect_ratio}"
        )
    if not profile.min_duration <= duration <= profile.max_duration:
        raise CinAIVideoUnsupportedError(
            f"Model {profile.slug} duration must be {profile.min_duration}-{profile.max_duration}s"
        )
    if profile.duration_values and int(duration) not in profile.duration_values:
        allowed = "/".join(str(value) for value in profile.duration_values)
        raise CinAIVideoUnsupportedError(
            f"Model {profile.slug} supports only discrete durations: {allowed}s"
        )
    if resolution and resolution not in profile.resolutions:
        raise CinAIVideoUnsupportedError(
            f"Model {profile.slug} does not support resolution {resolution}"
        )
    if audio is True and profile.audio_wire is None:
        raise CinAIVideoUnsupportedError(
            f"Audio is not supported by the audited profile for {profile.slug}"
        )


def _validate_video_request(
    profile: CinAIVideoProfile,
    mode: str,
    aspect_ratio: str,
    duration: int,
    images: Sequence[str],
    resolution: Optional[str],
    audio: Optional[bool],
) -> None:
    if mode not in VIDEO_MODES:
        raise CinAIVideoUnsupportedError(f"Unsupported CinAI video mode: {mode}")
    if mode not in profile.modes:
        raise CinAIVideoUnsupportedError(
            f"Model {profile.slug} does not support mode {mode}"
        )
    _validate_static_settings(profile, aspect_ratio, duration, resolution, audio)
    image_count = len(images)
    if image_count < profile.min_images:
        raise CinAIVideoUnsupportedError(
            f"Mode {mode} requires at least {profile.min_images} image(s)"
        )
    if profile.max_images is not None and image_count > profile.max_images:
        raise CinAIVideoUnsupportedError(
            f"Mode {mode} accepts at most {profile.max_images} image(s)"
        )
    if mode == "t2v" and image_count:
        raise CinAIVideoUnsupportedError("t2v must not include source images")
    if mode == "i2v" and image_count != 1:
        raise CinAIVideoUnsupportedError("i2v requires exactly one source image")
    if mode == "first_last" and image_count != 2:
        raise CinAIVideoUnsupportedError("first_last requires exactly two ordered images")
    if mode == "reference" and image_count < profile.reference_min_images:
        raise CinAIVideoUnsupportedError(
            f"reference requires at least {profile.reference_min_images} image(s)"
        )
    if (
        mode == "reference"
        and profile.reference_max_images is not None
        and image_count > profile.reference_max_images
    ):
        raise CinAIVideoUnsupportedError(
            f"reference accepts at most {profile.reference_max_images} image(s)"
        )


def build_video_payload(
    prompt: str,
    model: str,
    mode: str = "t2v",
    aspect_ratio: str = "16:9",
    duration: int = 5,
    images: Optional[Sequence[str]] = None,
    resolution: Optional[str] = None,
    audio: Optional[bool] = None,
) -> Dict[str, Any]:
    """Build a standalone payload with no inherited Studio state."""
    if mode == "components":
        raise CinAIVideoUnsupportedError(
            "CinAI components is intentionally disabled pending live payload capture"
        )
    profile = get_video_profile(model)
    source_images = tuple(str(value).strip() for value in (images or ()) if str(value).strip())
    _validate_video_request(
        profile,
        mode,
        aspect_ratio,
        int(duration),
        source_images,
        resolution,
        audio,
    )

    directives: List[Dict[str, Any]] = [
        {"key": "aspect_ratio", "value": aspect_ratio, "origin": "ui_control", "turn": 1},
        {"key": "model_id", "value": model, "origin": "ui_control", "turn": 1},
        {"key": "duration", "value": str(duration), "origin": "ui_control", "turn": 1},
    ]
    settings: Dict[str, Any] = {"duration": _duration_value(profile, int(duration))}
    if resolution and profile.resolution_wire:
        settings["resolution"] = resolution
        directives.append(
            {"key": "resolution", "value": resolution, "origin": "ui_control", "turn": 1}
        )
    if audio is not None and profile.audio_wire:
        settings[profile.audio_wire] = bool(audio)
        directives.append(
            {"key": profile.audio_wire, "value": bool(audio), "origin": "ui_control", "turn": 1}
        )

    items = [
        {
            "idx": idx,
            "kind": "image",
            "url": url,
            "origin": "composer_upload",
            "role": None,
        }
        for idx, url in enumerate(source_images)
    ]
    payload: Dict[str, Any] = {
        "workflow_context": None,
        "surface": "studio-video",
        "chat_history": [],
        "mode_hint": "video",
        "model_preferences": {"video": model},
        "aspect_ratio": aspect_ratio,
        "settings": settings,
        "context_envelope": {
            "v": 1,
            "items": items,
            "directives": directives,
            "prompt": prompt,
        },
        "client_request_id": str(uuid.uuid4()),
    }
    if source_images:
        payload["images"] = list(source_images)
    return {"prompt": prompt, "payload": payload}


def _probe_video(path: Path) -> Dict[str, Any]:
    """Return ffprobe metadata when available, otherwise basic MP4 metadata."""
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=format_name,duration,size:stream=codec_type,codec_name,width,height",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        return json.loads(completed.stdout or "{}")
    except (FileNotFoundError, subprocess.SubprocessError, json.JSONDecodeError):
        with path.open("rb") as handle:
            header = handle.read(16)
        if len(header) < 8 or header[4:8] != b"ftyp":
            raise CinAIVideoDownloadError("CinAI result is not an MP4 video")
        return {"format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "size": path.stat().st_size}}


def _validate_video_file(path: Path, min_bytes: int = 32768) -> Dict[str, Any]:
    if not path.is_file() or path.stat().st_size < min_bytes:
        raise CinAIVideoDownloadError(
            f"CinAI returned an empty or preview-sized video: {path.stat().st_size if path.exists() else 0} bytes"
        )
    metadata = _probe_video(path)
    streams = metadata.get("streams") or []
    if streams and not any(item.get("codec_type") == "video" for item in streams):
        raise CinAIVideoDownloadError("CinAI result has no video stream")
    return metadata


class CinAIVideoClient:
    def __init__(
        self,
        access_token: Optional[str] = None,
        base_url: Optional[str] = None,
        app_id: Optional[str] = None,
        timeout: Optional[int] = None,
        max_retries: Optional[int] = None,
        retry_base_seconds: Optional[float] = None,
    ) -> None:
        self.access_token = ensure_cinai_access_token(access_token)
        self.base_url = (
            base_url or getattr(config, "CINAI_API_BASE", "https://api.cinai.tv")
        ).rstrip("/")
        self.app_id = app_id or getattr(config, "CINAI_APP_ID", "runway-workflows")
        self.timeout = int(timeout or getattr(config, "CINAI_VIDEO_TIMEOUT", 1200))
        self.max_retries = max(
            0,
            int(max_retries if max_retries is not None else getattr(config, "CINAI_VIDEO_MAX_RETRIES", 3)),
        )
        self.retry_base_seconds = max(
            0.0,
            float(
                retry_base_seconds
                if retry_base_seconds is not None
                else getattr(config, "CINAI_VIDEO_RETRY_BASE_SECONDS", 5)
            ),
        )
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

    def _parse_sse(self, response: requests.Response) -> Dict[str, Any]:
        event_name: Optional[str] = None
        data_lines: List[str] = []

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
                logging.debug("CinAI video ignored non-JSON SSE event %s", current_event)
                return None
            if current_event == "error" or payload.get("error"):
                message = payload.get("error") or payload.get("message") or "CinAI video stream error"
                text = _clean_error(str(message))
                if _is_content_decline(text):
                    raise CinAIVideoContentDeclinedError(text)
                if _is_retryable_message(text):
                    raise CinAIRetryableError(text)
                raise CinAIError(text)
            result = payload.get("result", payload)
            if current_event == "result" or isinstance(result, Mapping) and result.get("output_url"):
                return dict(result)
            return None

        for raw_line in response.iter_lines(decode_unicode=True):
            line = raw_line or ""
            if not line:
                result = consume()
                if result is not None:
                    if not result.get("output_url"):
                        raise CinAIError("CinAI video result has no output_url")
                    return result
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())

        result = consume()
        if result and result.get("output_url"):
            return result
        raise CinAIRetryableError("CinAI video stream ended without output_url")

    def upload_image(self, image_path: str) -> str:
        """Upload a source still with bounded transient retries.

        Раньше transient-ошибка аплоада (сеть/5xx/429) пролетала мимо всех
        ретраев и валила клип с первой икоты — вопреки политике ретраев
        провайдера. Сам файл переиспользуется: повторная отправка идёт только
        когда URL ещё не получен (совместимо с "no repeated upload unless the
        upload URL itself is rejected").
        """
        last_error: Optional[Exception] = None
        attempts = max(1, self.max_retries + 1)
        for attempt in range(attempts):
            try:
                return self._upload_image_once(image_path)
            except CinAIRetryableError as exc:
                last_error = exc
                if attempt >= attempts - 1:
                    break
                delay = min(60.0, self.retry_base_seconds * (2 ** attempt))
                logging.warning(
                    "CinAI upload transient failure; retry %d/%d in %.1fs: %s",
                    attempt + 1,
                    attempts - 1,
                    delay,
                    _clean_error(str(exc)),
                )
                if delay:
                    time.sleep(delay)
        raise CinAIRetryableError(
            f"CinAI upload retries exhausted: {_clean_error(str(last_error))}"
        )

    def _upload_image_once(self, image_path: str, _auth_retry: bool = False) -> str:
        path = Path(image_path)
        if not path.is_file():
            raise FileNotFoundError(path)
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
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
            raise CinAIRetryableError(f"CinAI upload failed: {_clean_error(str(exc))}") from exc
        if response.status_code in (401, 403):
            if not _auth_retry:
                self._refresh_auth_once()
                return self._upload_image_once(image_path, _auth_retry=True)
            raise CinAIAuthError("CinAI access token was rejected")
        if response.status_code == 429 or response.status_code >= 500:
            raise CinAIRetryableError(f"CinAI upload returned HTTP {response.status_code}")
        if not response.ok:
            raise CinAIError(
                f"CinAI upload failed ({response.status_code}): {_clean_error(response.text)}"
            )
        file_url = str((response.json() or {}).get("file_url") or "").strip()
        if not file_url:
            raise CinAIError("CinAI upload returned no file_url")
        return file_url

    def submit_video(self, **request_kwargs: Any) -> Dict[str, Any]:
        body = build_video_payload(**request_kwargs)
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
                f"CinAI video connection failed: {_clean_error(str(exc))}"
            ) from exc
        if response.status_code == 401:
            raise CinAIAuthError("CinAI access token was rejected")
        if response.status_code == 403:
            text = _clean_error(response.text)
            if _is_retryable_message(text):
                raise CinAIRetryableError(f"CinAI video HTTP 403: {text}")
            raise CinAIAuthError("CinAI access token was rejected")
        if response.status_code == 429 or response.status_code >= 500:
            raise CinAIRetryableError(
                f"CinAI video generation returned HTTP {response.status_code}"
            )
        if not response.ok:
            text = _clean_error(response.text)
            if _is_content_decline(text):
                raise CinAIVideoContentDeclinedError(text)
            raise CinAIError(
                f"CinAI video generation failed ({response.status_code}): {text}"
            )
        try:
            return self._parse_sse(response)
        finally:
            response.close()

    def generate_video(self, **request_kwargs: Any) -> Dict[str, Any]:
        last_error: Optional[Exception] = None
        auth_refreshed = False
        attempt = 0
        while True:
            try:
                result = self.submit_video(**request_kwargs)
                result["attempts"] = attempt + 1
                return result
            except (CinAIVideoContentDeclinedError, CinAIVideoUnsupportedError):
                raise
            except CinAIAuthError:
                if auth_refreshed:
                    raise
                auth_refreshed = True
                self._refresh_auth_once()
                continue
            except (CinAIRetryableError, requests.RequestException) as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                delay = min(60.0, self.retry_base_seconds * (2 ** attempt))
                logging.warning(
                    "CinAI video transient failure; retry %d/%d in %.1fs: %s",
                    attempt + 1,
                    self.max_retries,
                    delay,
                    _clean_error(str(exc)),
                )
                attempt += 1
                time.sleep(delay)
        raise CinAIRetryableError(
            f"CinAI video retries exhausted: {_clean_error(str(last_error))}"
        )

    def download_video(
        self,
        output_url: str,
        output_path: Path,
        min_bytes: Optional[int] = None,
    ) -> Dict[str, Any]:
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        minimum = int(
            min_bytes
            if min_bytes is not None
            else getattr(config, "CINAI_VIDEO_MIN_BYTES", 32768)
        )
        try:
            response = requests.get(output_url, stream=True, timeout=(30, 300))
        except requests.RequestException as exc:
            raise CinAIRetryableError(
                f"CinAI video download failed: {_clean_error(str(exc))}"
            ) from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise CinAIRetryableError(
                f"CinAI video download returned HTTP {response.status_code}"
            )
        if not response.ok:
            raise CinAIVideoDownloadError(
                f"CinAI video download failed ({response.status_code})"
            )
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", suffix=".mp4", dir=str(target.parent), delete=False
            ) as handle:
                temp_path = Path(handle.name)
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        handle.write(chunk)
            if temp_path.stat().st_size < minimum:
                raise CinAIVideoDownloadError(
                    f"CinAI returned an empty or preview-sized video: {temp_path.stat().st_size} bytes"
                )
            metadata = _validate_video_file(temp_path, min_bytes=minimum)
            temp_path.replace(target)
            return metadata
        finally:
            response.close()
            if "temp_path" in locals() and temp_path.exists():
                temp_path.unlink(missing_ok=True)


def _find_image(directory: Path, clip_id: int) -> Optional[Path]:
    for suffix in (".png", ".jpg", ".jpeg", ".webp"):
        candidate = directory / f"{clip_id:03d}{suffix}"
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    return None


def _load_project_clips(project_path: Path) -> List[Dict[str, Any]]:
    montage_path = project_path / "montage_plan.json"
    if not montage_path.exists():
        raise FileNotFoundError(f"montage_plan.json not found in {project_path}")
    raw = json.loads(montage_path.read_text(encoding="utf-8-sig"))
    plan = raw.get("montage_plan", raw)
    return list(plan.get("clips") or [])


def _load_video_prompts(
    project_path: Path,
    clip_ids: Iterable[int],
    single_prompt: Optional[str] = None,
    prompts_file_path: Optional[str] = None,
    motion_mode: Optional[str] = None,
) -> Dict[int, str]:
    """Промпты клипов. `motion_mode` задаётся для промптов ОЖИВЛЕНИЯ: в i2v поверх
    них ложатся параллакс и промпт обложки — так же, как у VeoNonStop."""
    ids = [int(clip_id) for clip_id in clip_ids]
    if single_prompt and single_prompt != "default":
        result = {clip_id: single_prompt for clip_id in ids}
    else:
        prompts_file = prompts_file_path or find_prompts_file(str(project_path))
        if not prompts_file:
            raise FileNotFoundError(f"Prompts file not found in {project_path}")
        prompts = extract_all_prompts(prompts_file)
        # файл с промптами подряд (1..N) при пропусках в плане — к настоящим id клипов
        prompts, _ = remap_dense_prompts_for_montage(prompts, _load_project_clips(project_path))
        if single_prompt == "default":
            # «Один промпт», но single_prompt.txt нет — как у VeoNonStop: первый промпт на все клипы
            first = prompts[min(prompts)] if prompts else ""
            result = {clip_id: first for clip_id in ids} if first else {}
        else:
            result = {clip_id: prompts[clip_id] for clip_id in ids if clip_id in prompts}
    if motion_mode == "i2v" and result:
        from units.i2v_overrides import apply_i2v_prompt_overrides
        apply_i2v_prompt_overrides(project_path, result, "i2v")
    return result


def _source_paths_for_clip(
    project_path: Path,
    clip_id: int,
    mode: str,
    model: Optional[str] = None,
    reference_dir: Optional[str] = None,
    source_paths_by_clip: Optional[Mapping[int, Sequence[str]]] = None,
) -> List[Path]:
    if mode == "t2v":
        return []

    explicit = (source_paths_by_clip or {}).get(clip_id)
    if explicit:
        paths = [Path(value) for value in explicit]
        if not all(path.is_file() for path in paths):
            raise FileNotFoundError(f"One or more CinAI sources are missing for clip #{clip_id}")
        return paths

    generated_dir = project_path / "generated"
    if mode == "i2v":
        image = _find_image(generated_dir, clip_id)
        if not image:
            raise FileNotFoundError(f"CinAI i2v source missing for clip #{clip_id}")
        return [image]

    if mode == "first_last":
        first = _find_image(generated_dir, clip_id)
        last = _find_image(generated_dir, clip_id + 1)
        if not first or not last:
            raise FileNotFoundError(
                f"CinAI first_last requires generated/{clip_id:03d} and generated/{clip_id + 1:03d}"
            )
        return [first, last]

    if mode == "reference":
        directory = Path(reference_dir or project_path / "ref")
        paths = sorted(
            path
            for path in directory.iterdir()
            if path.is_file() and path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
        ) if directory.is_dir() else []
        reference_min = 3
        reference_max: Optional[int] = None
        if model:
            profile = get_video_profile(model)
            reference_min = profile.reference_min_images
            reference_max = profile.reference_max_images
        if len(paths) < reference_min:
            raise FileNotFoundError(
                f"CinAI reference mode for {model or 'unknown model'} requires "
                f"at least {reference_min} image(s) in {directory}"
            )
        if reference_max is not None and len(paths) > reference_max:
            raise CinAIVideoUnsupportedError(
                f"CinAI reference mode for {model or 'unknown model'} accepts "
                f"at most {reference_max} image(s), found {len(paths)} in {directory}"
            )
        return paths

    if mode == "components":
        raise CinAIVideoUnsupportedError(
            "CinAI components payload is not enabled until a live source contract is captured"
        )
    raise CinAIVideoUnsupportedError(f"Unsupported CinAI batch mode: {mode}")


# ── Цензор-цепочка (Skiper mod) ────────────────────────────────────────────
# При контент-отказе CinAI ведём себя как VeoNonStop-путь, согласно
# VEONONSTOP_FAST_CENSOR (GUI: «Skiper mod»):
#   ultrafast      — сразу фейл клипа (Pre-concat подставит статик-кадр);
#   after_rewrites — 2 реврайта промпта -> фейл;
#   off (полный)   — 2 реврайта -> (i2v) 2 ремикса картинки -> минималистик -> фейл.

_MINIMALISTIC_VIDEO_PROMPT = (
    "Animate a frame in a very minimalistic way without changing the logic of the scene."
)


def _skiper_mode() -> str:
    return str(getattr(config, 'VEONONSTOP_FAST_CENSOR', 'off') or 'off').strip().lower()


def _censor_rewrite(prompt: str, project_dir: Optional[str] = None,
                    clip_id: Optional[int] = None) -> Optional[str]:
    """Реврайт промпта под цензуру — тем же реврайтером, что у VeoNonStop.

    None = реврайт не удался или не изменил текст (дальше по цепочке)."""
    try:
        from units.prompt_softener import rewrite_prompt
        rewritten = (rewrite_prompt(prompt, project_dir=project_dir,
                                    clip_id=clip_id) or '').strip()
    except Exception as exc:
        logging.warning("CinAI censor rewrite failed: %s", _clean_error(str(exc)))
        return None
    if not rewritten or rewritten == prompt:
        return None
    return rewritten


def _direct_timing_enabled() -> bool:
    """Заказывать ли у модели длину КАЖДОГО клипа вместо одной общей.

    Работает только с моделями, у которых плавный ряд длительностей (omni
    3..10с). Выключено — шлём длительность из настроек, как раньше.
    """
    raw = getattr(config, 'CINAI_VIDEO_DIRECT_TIMING', None)
    if raw is None:
        raw = os.getenv('CINAI_VIDEO_DIRECT_TIMING', '1')
    return str(raw).strip().lower() in ('1', 'true', 'yes', 'on')


def _clip_durations(project_path: Path) -> Dict[int, float]:
    """{clip_id: длительность в секундах} из montage_plan.

    Режиссёр нарезает клипы по смыслу внутри вилки, заданной в GUI, поэтому
    длина у каждого своя — её и заказываем у модели вместо одной общей.
    """
    out: Dict[int, float] = {}
    path = Path(project_path) / "montage_plan.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return out
    clips = data.get("clips") or data.get("montage_plan", {}).get("clips", []) or []
    for clip in clips:
        try:
            clip_id = int(clip.get("id"))
            span = float(clip.get("endTime", 0) or 0) - float(clip.get("startTime", 0) or 0)
        except (TypeError, ValueError):
            continue
        if span > 0:
            out[clip_id] = round(span, 2)
    return out


def _chain_progress_printer(clip_id: int, image_node_id: str, video_node_id: str,
                           on_image_ready=None):
    """События сервера → короткие строки лога по одному клипу.

    Печатаем только переходы (кадр пошёл / кадр готов / видео пошло / статус
    запуска), иначе 30 клипов зальют лог. Ошибки внутри печати глушим —
    логирование не должно ронять генерацию."""
    seen: set = set()
    started_at = time.time()

    def label(node_id: str) -> str:
        if node_id == image_node_id:
            return "кадр"
        if node_id == video_node_id:
            return "видео"
        return "нода"

    def on_progress(kind: str, payload: Mapping[str, Any]) -> None:
        node_id = str((payload or {}).get("node_id") or "")
        if node_id and node_id not in (image_node_id, video_node_id):
            return
        key = (kind, node_id)
        if key in seen:
            return
        seen.add(key)
        elapsed = time.time() - started_at
        if kind == "node_started" and node_id:
            # Для КАДРА elapsed — это ожидание старта на сервере (обычно
            # 3-5с, при перегрузке провайдера больше), и его полезно видеть.
            # Для ВИДЕО это просто время генерации кадра — оно уже написано
            # строкой «кадр готов за Nс», второй раз не повторяем.
            wait = ''
            if node_id == image_node_id and elapsed >= 10:
                wait = f", сервер принял через {elapsed:.0f}с"
            print(f"  [CinAI Chain] #{clip_id}: {label(node_id)} — старт"
                  f"{wait}", flush=True)
        elif kind == "node_completed" and node_id:
            # «кадр готов», но «видео готово» — согласуем род
            done = 'готово' if label(node_id) == 'видео' else 'готов'
            print(f"  [CinAI Chain] #{clip_id}: {label(node_id)} {done} "
                  f"за {elapsed:.0f}с", flush=True)
            # запоминаем готовый кадр: если видео упадёт, повтор возьмёт его
            # готовым вместо повторной генерации картинки
            if node_id == image_node_id and on_image_ready is not None:
                url = ((payload or {}).get("output") or {}).get("url")
                if url:
                    try:
                        on_image_ready(str(url))
                    except Exception:
                        pass
        elif kind == "server_status":
            status = str((payload or {}).get("status") or "")
            if status and status not in ("completed",):
                print(f"  [CinAI Chain] #{clip_id}: статус на сервере "
                      f"'{status}' (прошло {elapsed:.0f}с)", flush=True)

    return on_progress


def _remix_frame(image_path: str, clip_id: int,
                 project_path: Path) -> Optional[str]:
    """Ремикс исходного кадра роутером remix_i2v (FastGen / CinAI).

    Движок берётся из REMIX_ENGINE. Результат остаётся в project/remixed/ насовсем."""
    engine = str(getattr(config, 'REMIX_ENGINE', 'off') or 'off').lower()
    if engine == 'off':
        logging.info("CinAI censor: frame remix skipped (REMIX_ENGINE=%s)", engine)
        return None
    try:
        from ref_remixer import remix_i2v  # sibling-импорт: так же во всех модулях
        remixed_dir = project_path / 'remixed'
        remixed_dir.mkdir(exist_ok=True)
        out_path = remixed_dir / f"{clip_id:03d}_{engine}_remix.png"
        remix_i2v(image_path, str(out_path),
                  log_path=str(project_path / 'remix_debug.log'))
        return str(out_path)
    except Exception as exc:
        logging.warning("CinAI censor: frame remix failed (%s): %s",
                        engine, _clean_error(str(exc)))
        return None


def _write_failure_report(project_path: Path, failures: Sequence[Mapping[str, Any]]) -> None:
    failure_path = project_path / "cinai_video_failures.json"
    if failures:
        failure_path.write_text(
            json.dumps(
                sorted(failures, key=lambda item: int(item.get("id", 0))),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    else:
        failure_path.unlink(missing_ok=True)


def generate_all_videos(
    project_dir: str,
    mode: Optional[str] = None,
    max_parallel: Optional[int] = None,
    clip_ids: Optional[Sequence[int]] = None,
    single_prompt: Optional[str] = None,
    aspect_ratio: Optional[str] = None,
    model: Optional[str] = None,
    duration: Optional[int] = None,
    resolution: Optional[str] = None,
    audio: Optional[bool] = None,
    reference_dir: Optional[str] = None,
    source_paths_by_clip: Optional[Mapping[int, Sequence[str]]] = None,
    prompts_file_path: Optional[str] = None,
    client: Optional[CinAIVideoClient] = None,
    output_dir_name: str = "vid_img",
    silent: bool = False,
    rebuild: bool = False,
    **_: Any,
) -> Dict[str, Any]:
    """Generate project clips through CinAI's video reverse API.

    This is deliberately independent from VeoNonStop's batch implementation.
    It writes the same ``vid_img/{id:03d}.mp4`` contract, which lets concat
    remain provider-neutral.
    """
    project_path = Path(project_dir)
    requested_mode = (mode or getattr(config, "CINAI_VIDEO_MODE", "t2v")).strip().lower()
    requested_model = model or config.CINAI_VIDEO_MODEL
    requested_ratio = aspect_ratio or getattr(config, "CINAI_VIDEO_ASPECT_RATIO", "16:9")
    requested_duration = snap_duration(
        requested_model, duration or getattr(config, "CINAI_VIDEO_DURATION", 5))
    requested_resolution = (
        resolution if resolution is not None
        else getattr(config, "CINAI_VIDEO_RESOLUTION", "")
    )
    if audio is None:
        raw_audio = str(getattr(config, "CINAI_VIDEO_AUDIO", "") or "").lower()
        requested_audio = (
            raw_audio in ("1", "true", "yes", "on") if raw_audio else None
        )
    else:
        requested_audio = bool(audio)

    profile = get_video_profile(requested_model)
    if requested_mode == "components":
        raise CinAIVideoUnsupportedError(
            "CinAI components is intentionally disabled pending live payload capture"
        )
    if requested_mode not in profile.modes:
        raise CinAIVideoUnsupportedError(
            f"Model {requested_model} does not support mode {requested_mode}"
        )
    # Fail fast: кривая комбинация настроек режется ОДНОЙ понятной ошибкой до
    # первого запроса/аплоада, а не N одинаковыми фейлами посреди батча.
    _validate_static_settings(
        profile,
        requested_ratio,
        requested_duration,
        requested_resolution or None,
        requested_audio,
    )

    clips = _load_project_clips(project_path)
    clip_map = {int(clip.get("id")): clip for clip in clips if clip.get("id") is not None}
    ids = sorted(int(value) for value in (clip_ids or clip_map.keys()) if int(value) in clip_map)
    skip_ids = {
        clip_id
        for clip_id in ids
        if clip_map[clip_id].get("source") in VIDEO_SKIP_SOURCES
    }
    ids = [clip_id for clip_id in ids if clip_id not in skip_ids]
    prompts = _load_video_prompts(
        project_path,
        ids,
        single_prompt=single_prompt,
        prompts_file_path=prompts_file_path,
        motion_mode=requested_mode,
    )
    output_dir = project_path / output_dir_name
    output_dir.mkdir(parents=True, exist_ok=True)
    pending: List[int] = []
    skipped: List[int] = sorted(skip_ids)
    failures: List[Dict[str, Any]] = [
        {
            "id": clip_id,
            "model": requested_model,
            "mode": requested_mode,
            "error_type": "MissingPromptError",
            "error": "No prompt was found for this clip",
        }
        for clip_id in ids
        if clip_id not in prompts
    ]
    for clip_id in ids:
        target = output_dir / f"{clip_id:03d}.mp4"
        if target.is_file() and target.stat().st_size > 0 and not rebuild:
            skipped.append(clip_id)
        elif clip_id in prompts:
            pending.append(clip_id)

    if not pending:
        _write_failure_report(project_path, failures)
        return {
            "generated": 0,
            "failed": len(failures),
            "skipped": len(skipped),
            "results": [],
            "failed_ids": sorted(item["id"] for item in failures),
            "mode": requested_mode,
            "model": requested_model,
        }

    video_client = client or CinAIVideoClient()
    workers = max(
        1,
        min(
            int(max_parallel or getattr(config, "CINAI_VIDEO_MAX_PARALLEL", 2)),
            6,
        ),
    )

    # ── Транспорт ────────────────────────────────────────────────────────
    # workflow (дефолт) — прямой Workflow API без агента: промпт доходит
    # дословно, опросника нет. agent — legacy dispatch/stream (с 15.08
    # переписывает промпт LLM'ом). См. docs/CINAI_WORKFLOW_API_PLAN.md.
    # Явный client= от вызывающего означает legacy-путь.
    _use_workflow = (
        client is None
        and str(getattr(config, "CINAI_TRANSPORT", "workflow") or "workflow").lower()
        == "workflow"
    )
    _wf_client = _wf_catalog = None
    _wf_settings: Dict[str, Any] = {}
    if _use_workflow:
        from units.cinai_graph import (
            CinAIWorkflowClient as _WFClient,
            ModelCatalog as _WFCatalog,
            build_generator_node as _wf_build_generator_node,
            attach_images as _wf_attach_images,
            download_media as _wf_download,
        )
        _wf_client = _WFClient()
        _wf_catalog = _WFCatalog.load(_wf_client)
        _wf_settings = {
            "aspect_ratio": requested_ratio,
            "duration": requested_duration,
            "resolution": requested_resolution or None,
            "generate_audio": requested_audio,
        }
        # ранний отказ одной строкой до трат (charge_phase=pre) + отсев
        # настроек, которых у модели нет (у seedance-lite нет resolution)
        _wf_settings = _wf_catalog.build_settings(requested_model, _wf_settings)
        # длительность под каждый клип (см. _direct_timing_enabled)
        _wf_clip_seconds = (_clip_durations(project_path)
                            if _direct_timing_enabled() else {})
        _wf_settings_cache: Dict[str, Dict[str, Any]] = {}

        def _wf_settings_for(clip_id: int) -> Dict[str, Any]:
            want = _wf_clip_seconds.get(clip_id)
            if not want:
                return _wf_settings
            picked = scene_duration(_wf_catalog, requested_model, want)
            if not picked:
                return _wf_settings
            cached = _wf_settings_cache.get(str(picked))
            if cached is None:
                cached = _wf_catalog.build_settings(requested_model, {
                    "aspect_ratio": requested_ratio,
                    "duration": picked,
                    "resolution": requested_resolution or None,
                    "generate_audio": requested_audio,
                })
                _wf_settings_cache[str(picked)] = cached
            return cached
        if requested_mode != "t2v" and _wf_catalog.image_port(requested_model) is None:
            raise CinAIVideoUnsupportedError(
                f"Model {requested_model} has no image input port — "
                f"mode {requested_mode} is impossible"
            )

    upload_cache: Dict[str, str] = {}
    upload_lock = threading.Lock()

    def upload_once(path: Path) -> str:
        key = str(path.resolve())
        with upload_lock:
            cached = upload_cache.get(key)
        if cached:
            return cached
        uploader = _wf_client if _use_workflow else video_client
        uploaded = uploader.upload_image(str(path))
        with upload_lock:
            upload_cache[key] = uploaded
        return uploaded

    def download_with_retry(output_url: str, target: Path) -> Dict[str, Any]:
        last_error: Optional[Exception] = None
        attempts = max(1, int(getattr(video_client, "max_retries", 0)) + 1)
        for attempt in range(attempts):
            try:
                return video_client.download_video(str(output_url), target)
            except CinAIRetryableError as exc:
                last_error = exc
                if attempt >= attempts - 1:
                    break
                delay = min(
                    60.0,
                    float(getattr(video_client, "retry_base_seconds", 0.0))
                    * (2 ** attempt),
                )
                logging.warning(
                    "CinAI video download transient failure; retry %d/%d in %.1fs: %s",
                    attempt + 1,
                    attempts - 1,
                    delay,
                    _clean_error(str(exc)),
                )
                if delay:
                    time.sleep(delay)
        if last_error is not None:
            raise last_error
        raise CinAIVideoDownloadError("CinAI video download failed without an error")

    def generate_one(clip_id: int) -> Dict[str, Any]:
        clip_mode = requested_mode
        # first_last у ПОСЛЕДНЕГО клипа (или перед дырой в нумерации) физически
        # невозможен — нет generated/{id+1}. Мягкая деградация в аудированный
        # i2v по собственному кадру вместо гарантированного фейла клипа.
        if (
            requested_mode == "first_last"
            and not (source_paths_by_clip or {}).get(clip_id)
            and "i2v" in profile.modes
        ):
            gen_dir = project_path / "generated"
            if (
                _find_image(gen_dir, clip_id) is not None
                and _find_image(gen_dir, clip_id + 1) is None
            ):
                clip_mode = "i2v"
                logging.info(
                    "CinAI clip %s: no next frame for first_last - falling back to i2v",
                    clip_id,
                )
        source_paths = _source_paths_for_clip(
            project_path,
            clip_id,
            clip_mode,
            model=requested_model,
            reference_dir=reference_dir,
            source_paths_by_clip=source_paths_by_clip,
        )
        source_urls = [upload_once(path) for path in source_paths]
        target = output_dir / f"{clip_id:03d}.mp4"

        if _use_workflow:
            # Прямой путь: INPUT-ноды с картинками -> ребро -> видео-нода.
            # Промпт уходит генератору дословно, опросника нет.
            def wf_attempt(prompt_text: str, urls: List[str]) -> Dict[str, Any]:
                def build():
                    node = _wf_build_generator_node(
                        _wf_catalog, requested_model, prompt_text,
                        _wf_settings_for(clip_id))
                    nodes = [node]
                    edges: List[Dict[str, Any]] = []
                    if urls:
                        extra_nodes, extra_edges = _wf_attach_images(
                            _wf_catalog, node, urls)
                        nodes = extra_nodes + [node]
                        edges = extra_edges
                    return {"nodes": nodes, "edges": edges, "target": node["id"]}

                return _wf_client.run_with_retries(
                    build, title=f"BegoFrame {clip_mode} #{clip_id:03d}")

            try:
                wf_result = wf_attempt(prompts[clip_id], source_urls)
            except CinAIVideoContentDeclinedError as decline:
                # ── Skiper mod: цензор-цепочка как у VeoNonStop ──
                skiper = _skiper_mode()
                print(f"  [CinAI CENSOR] #{clip_id}: model declined "
                      f"(skiper={skiper})", flush=True)
                logging.warning(
                    "CinAI video clip %s: content declined (skiper=%s): %s",
                    clip_id, skiper, _clean_error(str(decline)))
                if skiper == 'ultrafast':
                    raise
                wf_result = None
                cur_prompt = prompts[clip_id]
                for rw_n in (1, 2):
                    new_prompt = _censor_rewrite(
                        cur_prompt, str(project_path), clip_id)
                    if not new_prompt:
                        print(f"  [CinAI CENSOR] #{clip_id}: rewrite #{rw_n}/2 "
                              f"produced no change - moving on", flush=True)
                        break
                    cur_prompt = new_prompt
                    print(f"  [CinAI CENSOR] #{clip_id}: rewrite #{rw_n}/2 "
                          f"-> retry", flush=True)
                    try:
                        wf_result = wf_attempt(cur_prompt, source_urls)
                        break
                    except CinAIVideoContentDeclinedError:
                        continue
                if wf_result is None and skiper != 'after_rewrites':
                    # полный путь: (i2v) ремиксы картинки + минималистик
                    if clip_mode == 'i2v' and source_paths:
                        _engine = str(getattr(
                            config, 'REMIX_ENGINE', 'off')).upper()
                        for rx_n in (1, 2):
                            print(f"  [CinAI CENSOR] #{clip_id}: image remix "
                                  f"#{rx_n}/2 via {_engine}", flush=True)
                            remixed = _remix_frame(
                                str(source_paths[0]), clip_id, project_path)
                            if not remixed:
                                continue
                            try:
                                remix_url = _wf_client.upload_image(str(remixed))
                                wf_result = wf_attempt(
                                    prompts[clip_id],
                                    [remix_url] + source_urls[1:])
                                break
                            except CinAIAuthError:
                                raise
                            except CinAIError as remix_exc:
                                logging.warning(
                                    "CinAI video clip %s: remix attempt %d "
                                    "failed: %s", clip_id, rx_n,
                                    _clean_error(str(remix_exc)))
                                continue
                    if wf_result is None:
                        print(f"  [CinAI CENSOR] #{clip_id}: minimalistic "
                              f"final attempt", flush=True)
                        try:
                            wf_result = wf_attempt(
                                _MINIMALISTIC_VIDEO_PROMPT, source_urls)
                        except CinAIVideoContentDeclinedError:
                            pass
                if wf_result is None:
                    raise CinAIVideoContentDeclinedError(
                        f"clip #{clip_id} censored after skiper chain "
                        f"({skiper}): {_clean_error(str(decline), 200)}"
                    ) from decline
            output_url = str(wf_result["url"])
            _wf_download(output_url, target,
                         min_bytes=int(getattr(config, "CINAI_VIDEO_MIN_BYTES", 32768)))
            metadata = _validate_video_file(
                target, min_bytes=int(getattr(config, "CINAI_VIDEO_MIN_BYTES", 32768)))
            attempts = wf_result.get("attempts", 1)
        else:
            result = video_client.generate_video(
                prompt=prompts[clip_id],
                model=requested_model,
                mode=clip_mode,
                aspect_ratio=requested_ratio,
                duration=requested_duration,
                images=source_urls,
                resolution=requested_resolution or None,
                audio=requested_audio,
            )
            output_url = result["output_url"]
            metadata = download_with_retry(output_url, target)
            attempts = result.get("attempts", 1)

        return {
            "id": clip_id,
            "file": str(target),
            "url": output_url,
            "model": requested_model,
            "mode": clip_mode,
            "attempts": attempts,
            "metadata": metadata,
        }

    results: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="cinai-video") as executor:
        futures = {executor.submit(generate_one, clip_id): clip_id for clip_id in pending}
        for future in as_completed(futures):
            clip_id = futures[future]
            try:
                item = future.result()
                results.append(item)
                if not silent:
                    print(f"  [CinAI Video] #{clip_id}: OK", flush=True)
            except Exception as exc:
                failure = {
                    "id": clip_id,
                    "model": requested_model,
                    "mode": requested_mode,
                    "error_type": type(exc).__name__,
                    "error": _clean_error(str(exc), 500),
                }
                failures.append(failure)
                logging.warning(
                    "CinAI video clip %s failed (%s): %s",
                    clip_id,
                    type(exc).__name__,
                    _clean_error(str(exc)),
                )
                if not silent:
                    print(
                        f"  [CinAI Video] #{clip_id}: FAILED - {failure['error']}",
                        flush=True,
                    )

    _write_failure_report(project_path, failures)

    return {
        "generated": len(results),
        "failed": len(failures),
        "skipped": len(skipped),
        "results": sorted(results, key=lambda item: item["id"]),
        "failed_ids": sorted(item["id"] for item in failures),
        "mode": requested_mode,
        "model": requested_model,
    }


# ===========================================================================
# Цепочка ImageGen -> VideoGen без обратной загрузки кадра
# ===========================================================================

def chain_eligible_clips(
    project_dir: str,
    clip_ids: Optional[Sequence[int]] = None,
    *,
    rebuild: bool = False,
) -> List[int]:
    """Клипы, которым цепочка применима: нужны И картинка, И видео.

    Отсеиваем всё, у чего картинка приходит не от CinAI или уже есть:
      * внешние источники (real_photo/сток) и инфографика — VIDEO_SKIP_SOURCES;
      * клипы с уже готовой картинкой в generated/ (там нечего генерировать —
        дешевле обычный i2v на готовом файле);
      * клипы с уже готовым видео в vid_img/ (если не rebuild).
    """
    project_path = Path(project_dir)
    clips = _load_project_clips(project_path)
    clip_map = {int(c["id"]): c for c in clips if c.get("id") is not None}
    ids = sorted(int(v) for v in (clip_ids or clip_map.keys()) if int(v) in clip_map)
    generated_dir = project_path / "generated"
    vid_dir = project_path / "vid_img"
    eligible: List[int] = []
    for clip_id in ids:
        if clip_map[clip_id].get("source") in VIDEO_SKIP_SOURCES:
            continue
        if _find_image(generated_dir, clip_id) is not None:
            continue
        target = vid_dir / f"{clip_id:03d}.mp4"
        if target.is_file() and target.stat().st_size > 0 and not rebuild:
            continue
        eligible.append(clip_id)
    return eligible


def generate_chained_i2v(
    project_dir: str,
    clip_ids: Optional[Sequence[int]] = None,
    *,
    image_model: Optional[str] = None,
    video_model: Optional[str] = None,
    image_prompts_file: Optional[str] = None,
    video_prompts_file: Optional[str] = None,
    aspect_ratio: Optional[str] = None,
    image_quality: Optional[str] = None,
    duration: Optional[int] = None,
    resolution: Optional[str] = None,
    audio: Optional[bool] = None,
    max_parallel: Optional[int] = None,
    single_prompt: Optional[str] = None,
    output_dir_name: str = "vid_img",
    rebuild: bool = False,
    silent: bool = False,
) -> Dict[str, Any]:
    """ImageGen + i2v без обратной загрузки кадра.

    Кадр генерируется отдельным прогоном, и его оригинал (`full`, 1376×768)
    подаётся видеомодели ссылкой через image-input: ни скачивать, ни заливать
    назад его не нужно. Ребро «генератор кадра → видео» в ОДНОМ графе не
    годится: по нему сервер передаёт уменьшенную копию `md` (480×267), и видео
    выходило мылом. Кадр сохраняется в ``generated/NNN.png`` (он нужен конкату,
    фолбэку и Finalize), видео в ``vid_img/NNN.mp4``.

    Возвращает тот же контракт, что generate_all_videos, плюс `images`.
    """
    from units.cinai_graph import (
        CinAIWorkflowClient, ModelCatalog, build_generator_node, build_edge,
        build_image_input_node, download_media, execution_slot, full_size_url,
        sync_execution_limit,
    )

    project_path = Path(project_dir)
    generated_dir = project_path / "generated"
    output_dir = project_path / output_dir_name
    generated_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    img_model = image_model or config.CINAI_MODEL
    vid_model = video_model or config.CINAI_VIDEO_MODEL
    ratio = aspect_ratio or getattr(config, "CINAI_VIDEO_ASPECT_RATIO", "16:9")
    quality = (image_quality or getattr(config, "CINAI_QUALITY", "1K")).upper()
    dur = snap_duration(vid_model, duration or getattr(config, "CINAI_VIDEO_DURATION", 5))
    res = resolution if resolution is not None else getattr(
        config, "CINAI_VIDEO_RESOLUTION", "")
    if audio is None:
        raw_audio = str(getattr(config, "CINAI_VIDEO_AUDIO", "") or "").lower()
        audio = raw_audio in ("1", "true", "yes", "on") if raw_audio else None

    ids = list(clip_ids or chain_eligible_clips(project_dir, rebuild=rebuild))
    if not ids:
        return {"generated": 0, "failed": 0, "skipped": 0, "results": [],
                "failed_ids": [], "images": [], "mode": "i2v_chain",
                "model": vid_model}

    # Промпты: у кадра и у движения они РАЗНЫЕ файлы, если заданы отдельно
    # «один промпт» — про движение: кадры у каждой сцены свои
    image_prompts = _load_video_prompts(
        project_path, ids, prompts_file_path=image_prompts_file)
    video_prompts = _load_video_prompts(
        project_path, ids, single_prompt=single_prompt,
        prompts_file_path=video_prompts_file, motion_mode="i2v")

    client = CinAIWorkflowClient()
    catalog = ModelCatalog.load(client)
    # лимит одновременных задач задаёт ТАРИФ и он меняется (16.08.2026 Pro
    # срезали с 6 до 4) — подстраиваем семафор под реальный, иначе лишние
    # запросы просто висят в очереди провайдера
    _limit = sync_execution_limit(client)

    handle = catalog.image_port_id(vid_model)
    if handle is None:
        raise CinAIVideoUnsupportedError(
            f"Video model {vid_model} has no image input port — chaining is impossible")

    img_settings = catalog.build_settings(img_model, {
        "aspect_ratio": ratio, "resolution": quality,
        "max_number": 1, "scene_preset": "off"})
    vid_settings = catalog.build_settings(vid_model, {
        "aspect_ratio": ratio, "duration": dur,
        "resolution": res or None, "generate_audio": audio})

    # Длительность ПОД КАЖДЫЙ КЛИП (только у моделей с плавным рядом длин —
    # сейчас это omni 3..10с). Режиссёр режет клипы по смыслу внутри вилки из
    # GUI, а мы раньше слали одну длину на все и потом тянули/ужимали видео
    # ретаймингом. Теперь заказываем близкую длину, а доли секунды по-прежнему
    # доводит ffmpeg. У seedance (5s/10s) и grok (6/10/15) — настройка из GUI.
    clip_seconds = _clip_durations(project_path) if _direct_timing_enabled() else {}
    _settings_by_duration: Dict[str, Dict[str, Any]] = {}

    def _video_settings_for(clip_id: int) -> Dict[str, Any]:
        want = clip_seconds.get(clip_id)
        if not want:
            return vid_settings
        picked = scene_duration(catalog, vid_model, want)
        if not picked:
            return vid_settings
        cached = _settings_by_duration.get(str(picked))
        if cached is None:
            cached = catalog.build_settings(vid_model, {
                "aspect_ratio": ratio, "duration": picked,
                "resolution": res or None, "generate_audio": audio})
            _settings_by_duration[str(picked)] = cached
        return cached

    workers = max(1, min(int(max_parallel or getattr(
        config, "CINAI_VIDEO_MAX_PARALLEL", 2)), _limit))
    results: List[Dict[str, Any]] = []
    images: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []

    def run_clip(clip_id: int) -> Dict[str, Any]:
        base_img_prompt = image_prompts.get(clip_id)
        base_vid_prompt = video_prompts.get(clip_id) or base_img_prompt
        if not base_img_prompt:
            raise CinAIError(f"No prompt was found for clip #{clip_id}")

        def attempt_pair(img_prompt: str, vid_prompt: str) -> Dict[str, Any]:
            """Один прогон графа кадр→видео со своей петлёй транспорт-ретраев.

            Контент-отказ (цензура) наружу пробрасывается сразу — им
            занимается skiper-цепочка выше.

            Если кадр УЖЕ сделан, а упало видео (провайдер регулярно роняет
            вторую ноду: `Provider halcion unavailable`, HTTP 520/524), повтор
            НЕ перегенерирует картинку: она подставляется готовой ссылкой в
            image-input. Экономит и кредиты, и ~2 минуты на каждой попытке."""
            ready_image: Dict[str, Any] = {}   # url готового кадра между попытками

            def _remember_frame(url: str) -> None:
                """Кадр готов — сразу кладём его на диск, не дожидаясь видео.

                Скачивание идёт в отдельном потоке и слот аккаунта не держит,
                на генерацию не влияет. Зато кадр есть при ЛЮБОМ исходе: упало
                видео, зависла нода, убили процесс — фолбэку/Pre-concat уже
                есть что подставить, а повтор не станет генерировать картинку."""
                ready_image.setdefault("url", url)
                frame_path = generated_dir / f"{clip_id:03d}.png"
                if frame_path.exists():
                    return

                def _pull():
                    try:
                        download_media(url, frame_path, min_bytes=1024)
                    except Exception as exc:
                        logging.debug("CinAI chain clip %s: ранняя загрузка кадра "
                                      "не удалась: %s", clip_id, exc)

                threading.Thread(target=_pull, daemon=True,
                                 name=f'cinai-frame-{clip_id}').start()

            def build_frame():
                img_node = build_generator_node(
                    catalog, img_model, img_prompt, img_settings,
                    position={"x": 0, "y": 0})
                return {"nodes": [img_node], "edges": [],
                        "target": img_node["id"], "image_node": img_node["id"]}

            def build():
                # Видео оживляет ПОЛНОРАЗМЕРНЫЙ кадр. Ребро «генератор кадра →
                # видео» внутри одного графа сервер кормит вариантом `md`
                # (480×267, ~10 КБ — см. full_size_url), и видео выходило мылом.
                # Поэтому кадр делается отдельным прогоном, а в видео идёт его
                # `full` (1376×768) через image-input.
                img_node = build_image_input_node(full_size_url(str(ready_image["url"])))
                vid_node = build_generator_node(
                    catalog, vid_model, vid_prompt, _video_settings_for(clip_id),
                    position={"x": 600, "y": 0}, connected_ports=[handle])
                edge = build_edge(img_node["id"], vid_node["id"], handle)
                # image-input не генерирует ничего — в журнал его не печатаем
                return {"nodes": [img_node, vid_node], "edges": [edge],
                        "target": vid_node["id"], "image_node": ""}

            def _give_up_after_video_failure() -> bool:
                """Skiper mod=ultrafast: кадр есть, видео упало — не оживляем.

                Смысл ultrafast — скорость: вместо повторных попыток оживить
                клип берём готовый кадр статикой (он уже лежит в generated/,
                конкат подставит его с Ken Burns)."""
                return _skiper_mode() == 'ultrafast' and bool(ready_image.get("url"))

            # своя петля ретраев: нужен доступ к id обеих нод графа
            last_error: Optional[Exception] = None
            attempts_used = 0
            def run_graph(graph: Dict[str, Any], waiting: str) -> Dict[str, Any]:
                workflow_id = client.create_workflow(f"BegoFrame chain #{clip_id:03d}")
                # клип идёт 1-8 минут; без вестей с сервера это выглядит как
                # зависание, поэтому переводим события SSE в строки лога
                progress = _chain_progress_printer(
                    clip_id, graph["image_node"], graph["target"],
                    on_image_ready=_remember_frame)
                try:
                    client.put_graph(workflow_id, graph["nodes"], graph["edges"],
                                     title=f"BegoFrame chain #{clip_id:03d}")
                    # слот аккаунта на всё выполнение (submit -> completed):
                    # инфографика/ImageGen идут параллельно и делят те же 6
                    with execution_slot():
                        submission = client.submit(workflow_id, graph["target"])
                        print(f"  [CinAI Chain] #{clip_id}: принят сервером, "
                              f"ждём {waiting}...", flush=True)
                        return client.stream_all_results(
                            submission["execution_id"], on_progress=progress)
                finally:
                    client.delete_workflow(workflow_id)

            for attempt in range(max(1, client.max_retries + 1)):
                attempts_used = attempt + 1
                try:
                    if not ready_image.get("url"):
                        frame_graph = build_frame()
                        frame_out = run_graph(frame_graph, "кадр").get(
                            frame_graph["target"]) or {}
                        if not frame_out.get("url"):
                            raise CinAIRetryableError(
                                "chain frame finished without an image output")
                        _remember_frame(str(frame_out["url"]))
                    graph = build()
                    outputs = run_graph(graph, "видео")
                except (CinAIVideoUnsupportedError, CinAIAuthError):
                    raise
                except CinAIRetryableError as exc:
                    last_error = exc
                    if _give_up_after_video_failure():
                        print(f"  [CinAI Chain] #{clip_id}: ultrafast — видео не "
                              f"вышло, оставляем кадр статиком", flush=True)
                        break
                    if attempt >= client.max_retries:
                        break
                    # уважаем «retry in Ns» от circuit breaker провайдера
                    time.sleep(max(_retry_after_seconds(str(exc)),
                                   min(60.0, client.retry_base_seconds * (2 ** attempt))))
                    continue
                except CinAIError as exc:
                    if _is_content_decline(_clean_error(str(exc))):
                        raise
                    last_error = exc
                    if attempt >= client.max_retries:
                        break
                    time.sleep(min(60.0, client.retry_base_seconds * (2 ** attempt)))
                    continue

                video_out = outputs.get(graph["target"]) or {}
                image_out = {"url": ready_image["url"]}
                if not video_out.get("url"):
                    last_error = CinAIRetryableError(
                        "chain finished without a video output")
                    if _give_up_after_video_failure():
                        print(f"  [CinAI Chain] #{clip_id}: ultrafast — видео не "
                              f"вышло, оставляем кадр статиком", flush=True)
                        break
                    if attempt >= client.max_retries:
                        break
                    continue

                video_path = output_dir / f"{clip_id:03d}.mp4"
                download_media(video_out["url"], video_path,
                               min_bytes=int(getattr(config, "CINAI_VIDEO_MIN_BYTES", 32768)))
                metadata = _validate_video_file(
                    video_path, min_bytes=int(getattr(config, "CINAI_VIDEO_MIN_BYTES", 32768)))

                image_entry = None
                if image_out.get("url"):
                    # кадр нужен конкату/фолбэку/Finalize — сохраняем как обычно
                    url = str(image_out["url"])
                    suffix = ".png"
                    for ext in (".png", ".jpg", ".jpeg", ".webp"):
                        if url.lower().split("?")[0].endswith(ext):
                            suffix = ext
                            break
                    image_path = generated_dir / f"{clip_id:03d}{suffix}"
                    try:
                        download_media(url, image_path, min_bytes=1024)
                        image_entry = {"id": clip_id, "file": str(image_path), "url": url}
                    except CinAIError as exc:
                        # видео уже есть — кадр не критичен, но фиксируем
                        logging.warning("CinAI chain clip %s: frame not saved: %s",
                                        clip_id, _clean_error(str(exc)))
                return {
                    "id": clip_id,
                    "file": str(video_path),
                    "url": video_out["url"],
                    "model": vid_model,
                    "mode": "i2v_chain",
                    "attempts": attempts_used,
                    "metadata": metadata,
                    "image": image_entry,
                }
            # Видео так и не вышло, но кадр мог получиться — сохраняем его:
            # Pre-concat подставит статик вместо дырки, и повторный запуск
            # клипа не будет генерировать картинку заново.
            if ready_image.get("url"):
                try:
                    frame_path = generated_dir / f"{clip_id:03d}.png"
                    if not frame_path.exists():
                        download_media(str(ready_image["url"]), frame_path,
                                       min_bytes=1024)
                        print(f"  [CinAI Chain] #{clip_id}: видео не вышло, "
                              f"кадр сохранён в generated/", flush=True)
                except CinAIError as exc:
                    logging.warning("CinAI chain clip %s: кадр не сохранён: %s",
                                    clip_id, _clean_error(str(exc)))
            raise CinAIRetryableError(
                f"CinAI chain retries exhausted: {_clean_error(str(last_error))}")

        try:
            return attempt_pair(base_img_prompt, base_vid_prompt)
        except CinAIVideoContentDeclinedError as exc:
            decline = exc  # имя из `as` умирает за пределами except-блока

        # ── Skiper mod: цензор-цепочка ──
        # В цепочке кадр рождается ВНУТРИ графа, внешней картинки нет —
        # ремиксить нечего, поэтому шаг «ремикс изображения» заменён на
        # реврайт ПРОМПТА КАДРА (источник цензуры чаще всего он).
        skiper = _skiper_mode()
        print(f"  [CinAI CENSOR] #{clip_id}: model declined (skiper={skiper})",
              flush=True)
        logging.warning("CinAI chain clip %s: content declined (skiper=%s): %s",
                        clip_id, skiper, _clean_error(str(decline)))
        if skiper == 'ultrafast':
            raise decline

        cur_vid = base_vid_prompt
        for rw_n in (1, 2):
            new_vid = _censor_rewrite(cur_vid, str(project_path), clip_id)
            if not new_vid:
                print(f"  [CinAI CENSOR] #{clip_id}: rewrite #{rw_n}/2 produced "
                      f"no change - moving on", flush=True)
                break
            cur_vid = new_vid
            print(f"  [CinAI CENSOR] #{clip_id}: motion rewrite #{rw_n}/2 -> retry",
                  flush=True)
            try:
                return attempt_pair(base_img_prompt, cur_vid)
            except CinAIVideoContentDeclinedError:
                continue

        if skiper != 'after_rewrites':
            # полный путь: правим ПРОМПТ КАДРА (2 захода), затем минималистик
            cur_img = base_img_prompt
            for rw_n in (1, 2):
                new_img = _censor_rewrite(cur_img, str(project_path), clip_id)
                if not new_img:
                    break
                cur_img = new_img
                print(f"  [CinAI CENSOR] #{clip_id}: frame rewrite #{rw_n}/2 "
                      f"-> retry", flush=True)
                try:
                    return attempt_pair(cur_img, cur_vid)
                except CinAIVideoContentDeclinedError:
                    continue
            print(f"  [CinAI CENSOR] #{clip_id}: minimalistic final attempt",
                  flush=True)
            try:
                return attempt_pair(cur_img, _MINIMALISTIC_VIDEO_PROMPT)
            except CinAIVideoContentDeclinedError:
                pass

        raise CinAIVideoContentDeclinedError(
            f"clip #{clip_id} censored after skiper chain ({skiper}): "
            f"{_clean_error(str(decline), 200)}") from decline

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="cinai-chain") as executor:
        futures = {executor.submit(run_clip, cid): cid for cid in ids}
        for future in as_completed(futures):
            clip_id = futures[future]
            try:
                item = future.result()
                results.append(item)
                if item.get("image"):
                    images.append(item["image"])
                # прогресс печатаем ВСЕГДА (как image-клиент): 34 клипа по
                # 1-8 минут без единой строки выглядели как зависание
                print(f"  [CinAI Chain] #{clip_id}: OK "
                      f"({len(results) + len(failures)}/{len(ids)})", flush=True)
            except Exception as exc:
                failures.append({
                    "id": clip_id, "model": vid_model, "mode": "i2v_chain",
                    "error_type": type(exc).__name__,
                    "error": _clean_error(str(exc), 500),
                })
                logging.warning("CinAI chain clip %s failed (%s): %s",
                                clip_id, type(exc).__name__, _clean_error(str(exc)))
                print(f"  [CinAI Chain] #{clip_id}: FAILED - "
                      f"{_clean_error(str(exc), 200)} "
                      f"({len(results) + len(failures)}/{len(ids)})", flush=True)

    _write_failure_report(project_path, failures)
    return {
        "generated": len(results),
        "failed": len(failures),
        "skipped": 0,
        "results": sorted(results, key=lambda item: item["id"]),
        "failed_ids": sorted(item["id"] for item in failures),
        "images": sorted(images, key=lambda item: item["id"]),
        "mode": "i2v_chain",
        "model": vid_model,
        "image_model": img_model,
    }
