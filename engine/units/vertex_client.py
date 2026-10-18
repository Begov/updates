"""
Клиент Vertex AI (Google Cloud) — картинки Imagen / Gemini Image и видео Veo.

Клиент повторяет интерфейс `VeoNonStopClient` (то, что реально вызывают стадии
картинок и видео), поэтому готовые стадии работают с ним без переписывания:
провайдер выбирается в одном месте — при создании клиентов.

Эндпоинты (REST, документация Vertex AI):
  Картинки (Imagen):   POST .../publishers/google/models/{модель}:predict
  Картинки (Gemini):   POST .../publishers/google/models/{модель}:generateContent
  Видео (Veo):         POST .../publishers/google/models/{модель}:predictLongRunning
  Статус видео:        POST .../publishers/google/models/{модель}:fetchPredictOperation

Адрес: https://{регион}-aiplatform.googleapis.com/v1/projects/{PROJECT_ID}/
       locations/{регион}/publishers/google/models/{модель}:{метод}
Для региона `global` хост без префикса: https://aiplatform.googleapis.com.

Авторизация — API-ключ в заголовке `x-goog-api-key`.

Готовое видео приходит байтами прямо в ответе (если не задан `storageUri`), а
не ссылкой: байты запоминаются при опросе статуса и пишутся на диск в
`download_video`.
"""

import base64
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
try:  # файл появляется под своим именем только целиком (см. units/media_io.py)
    from units.media_io import atomic_open, atomic_target, write_bytes_atomic
except ImportError:                                               # pragma: no cover
    from media_io import atomic_open, atomic_target, write_bytes_atomic

_agent_dir = str(Path(__file__).resolve().parent.parent)
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

try:  # те же классы исключений, что ловят стадии (veo_motion/veo_stills импортируют их так же)
    from veo_client import (
        BananaResult, MIME_TYPES, VeoNonStopError, VeoNonStopGenerationError,
        VeoNonStopRateLimitError, VeoNonStopTimeoutError, _image_to_base64,
        _verify_video_integrity,
    )
except ImportError:                                               # pragma: no cover
    from units.veo_client import (
        BananaResult, MIME_TYPES, VeoNonStopError, VeoNonStopGenerationError,
        VeoNonStopRateLimitError, VeoNonStopTimeoutError, _image_to_base64,
        _verify_video_integrity,
    )

DEFAULT_LOCATION = 'us-central1'
DEFAULT_IMAGE_MODEL = 'gemini-3.1-flash-lite-image'
DEFAULT_VIDEO_MODEL = 'veo-3.1-lite-generate-001'

# Форматы кадра. У видео только два; у картинок Imagen — пять.
VIDEO_ASPECTS = ('16:9', '9:16')
IMAGE_ASPECTS = ('1:1', '3:4', '4:3', '16:9', '9:16')

# Длительности по моделям (документация Veo на Vertex AI).
VEO2_DURATIONS = (5, 6, 7, 8)
VEO3_DURATIONS = (4, 6, 8)


def _is_gemini_image(model: str) -> bool:
    """Модели Gemini Image отдают картинку через generateContent, а не predict."""
    return 'gemini' in model.lower()


def pick_duration(model: str, seconds: Optional[float]) -> Optional[int]:
    """Длительность клипа, которую примет модель, ближайшая сверху к длине сцены."""
    if not seconds:
        return None
    options = VEO2_DURATIONS if model.startswith('veo-2') else VEO3_DURATIONS
    try:
        need = float(str(seconds).strip().lower().rstrip('s'))
    except ValueError:
        return None
    for option in options:
        if need <= option:
            return option
    return options[-1]


def trim_letterbox(raw: bytes, aspect: str) -> bytes:
    """Срезать чёрные полосы сверху/снизу и довести кадр до нужного формата.

    Gemini Image нередко отдаёт кадр нужного размера, но с чёрными полосами
    (проверено живым запросом: 1344×768, полосы по ~53 px). Из такой картинки
    видео наследует полосы, поэтому они срезаются здесь. Если полос нет —
    файл возвращается как есть.
    """
    import io
    from PIL import Image
    im = Image.open(io.BytesIO(raw))
    gray = im.convert('L')
    w, h = gray.size

    def dark_row(y: int) -> bool:
        return gray.crop((0, y, w, y + 1)).getextrema()[1] < 12

    top = next((y for y in range(h) if not dark_row(y)), 0)
    bottom = next((y for y in range(h - 1, -1, -1) if not dark_row(y)), h - 1)
    bar_bottom = h - 1 - bottom
    if top + bar_bottom < max(8, h * 0.03):
        return raw                                   # полос нет
    im = im.convert('RGB').crop((0, top, w, bottom + 1))
    try:
        aw, ah = (int(x) for x in aspect.split(':'))
    except ValueError:
        aw, ah = 16, 9
    w, h = im.size
    if w * ah > h * aw:                              # слишком широкий — режем по бокам
        new_w = h * aw // ah
        left = (w - new_w) // 2
        im = im.crop((left, 0, left + new_w, h))
    out = io.BytesIO()
    im.save(out, format='PNG')
    logging.info("Vertex: срезаны чёрные полосы (%d+%d px), итог %dx%d", top, bar_bottom, *im.size)
    return out.getvalue()


def _error_text(resp) -> str:
    """Сообщение об ошибке Google: {"error": {"code", "message", "status"}}."""
    try:
        body = resp.json()
    except Exception:                                              # noqa: BLE001
        return (resp.text or '')[:300]
    err = body.get('error') if isinstance(body, dict) else None
    if isinstance(err, dict):
        parts = [str(err.get('status') or err.get('code') or ''), str(err.get('message') or '')]
        return ' | '.join(p for p in parts if p)[:500]
    return str(body)[:300]


class VertexClient:
    """Клиент Vertex AI с интерфейсом VeoNonStopClient (в части, нужной стадиям)."""

    api_version = 3                       # метка для логов («v3» — Vertex)
    provider = 'vertex'

    def __init__(self, api_key: Optional[str] = None, project_id: Optional[str] = None,
                 location: Optional[str] = None, video_model: Optional[str] = None,
                 image_model: Optional[str] = None, resolution: Optional[str] = None,
                 generate_audio: bool = False):
        self.api_key = (api_key or os.environ.get('VERTEX_API_KEY', '')).strip()
        self.project_id = (project_id or os.environ.get('VERTEX_PROJECT_ID', '')).strip()
        self.location = (location or os.environ.get('VERTEX_LOCATION', '') or DEFAULT_LOCATION).strip()
        self.video_model = (video_model or os.environ.get('VERTEX_VIDEO_MODEL', '') or DEFAULT_VIDEO_MODEL).strip()
        self.image_model = (image_model or os.environ.get('VERTEX_IMAGE_MODEL', '') or DEFAULT_IMAGE_MODEL).strip()
        self.resolution = (resolution or os.environ.get('VERTEX_VIDEO_RESOLUTION', '') or '').strip()
        self.generate_audio = bool(generate_audio)
        if not self.api_key:
            raise ValueError("VERTEX_API_KEY не задан (Настройки → Vertex AI)")
        if not self.project_id:
            raise ValueError("VERTEX_PROJECT_ID не задан (Настройки → Vertex AI)")
        host = ('aiplatform.googleapis.com' if self.location == 'global'
                else f'{self.location}-aiplatform.googleapis.com')
        self.base_url = (f'https://{host}/v1/projects/{self.project_id}'
                         f'/locations/{self.location}/publishers/google/models')
        self.headers = {'x-goog-api-key': self.api_key, 'Content-Type': 'application/json'}
        self._videos: Dict[str, List[dict]] = {}      # task_id -> готовые видео из ответа
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ HTTP
    def _post(self, model: str, method: str, body: dict, timeout=(30, 300)) -> dict:
        """POST к методу модели. Повтор при 429/5xx с нарастающей паузой."""
        url = f'{self.base_url}/{model}:{method}'
        delays = (10, 30, 60)
        # Gemini 3 в preview обычно доступен только на глобальном адресе: при 404 из
        # обычного региона один раз пробуем global (404 ничего не стоит).
        can_try_global = self.location != 'global' and model.startswith('gemini-3')
        for attempt in range(len(delays) + 1):
            try:
                resp = requests.post(url, headers=self.headers, json=body, timeout=timeout)
            except requests.RequestException as exc:
                if attempt < len(delays):
                    logging.warning("Vertex %s: сеть — %s, повтор через %ss", method, exc, delays[attempt])
                    time.sleep(delays[attempt])
                    continue
                raise VeoNonStopTimeoutError(f"Vertex {method}: сеть недоступна: {exc}") from exc
            if resp.status_code == 200:
                return resp.json()
            text = _error_text(resp)
            if resp.status_code == 404 and can_try_global:
                can_try_global = False
                url = (f'https://aiplatform.googleapis.com/v1/projects/{self.project_id}'
                       f'/locations/global/publishers/google/models/{model}:{method}')
                logging.info("Vertex %s: модель не найдена в %s — пробую global", model, self.location)
                continue
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < len(delays):
                logging.warning("Vertex %s: HTTP %s %s — повтор через %ss",
                                method, resp.status_code, text[:120], delays[attempt])
                time.sleep(delays[attempt])
                continue
            if resp.status_code == 429:
                raise VeoNonStopRateLimitError(f"Vertex rate limit: {text}")
            if resp.status_code >= 500:
                raise VeoNonStopTimeoutError(f"Vertex server error {resp.status_code}: {text}")
            hint = ''
            if resp.status_code == 404 and 'Publisher model' in text:
                hint = (" — модель недоступна для этого ключа/проекта. Ключ express-режима открывает "
                        "только Gemini; Imagen и Veo требуют полноценного проекта Vertex AI. "
                        "Выберите другую модель или проверьте PROJECT_ID и регион")
            raise VeoNonStopError(f"Vertex {method} failed (HTTP {resp.status_code}): {text}{hint}")
        raise VeoNonStopError(f"Vertex {method}: исчерпаны повторы")          # недостижимо

    # ----------------------------------------------------------------- видео
    @staticmethod
    def _aspect(aspect_ratio: Optional[str]) -> str:
        value = str(aspect_ratio or os.environ.get('VEONONSTOP_ASPECT_RATIO', '') or '16:9').strip()
        if value in VIDEO_ASPECTS:
            return value
        logging.warning("Формат %r не поддерживается видео Vertex — беру 16:9", value)
        return '16:9'

    def _video_params(self, aspect_ratio, count, duration) -> dict:
        params: Dict[str, Any] = {
            'aspectRatio': self._aspect(aspect_ratio),
            'sampleCount': max(1, min(4, int(count or 1))),
        }
        seconds = pick_duration(self.video_model, duration) if duration else None
        if seconds:
            params['durationSeconds'] = seconds
        if self.resolution and self.video_model.startswith(('veo-3', 'veo-3.1')):
            params['resolution'] = self.resolution
        if self.video_model.startswith(('veo-3', 'veo-3.1')):
            params['generateAudio'] = self.generate_audio
        return params

    def _submit_video(self, instance: dict, params: dict) -> str:
        from units.frame_format import with_frame_format
        instance = {**instance, 'prompt': with_frame_format(instance.get('prompt', ''))}   # формат ролика
        data = self._post(self.video_model, 'predictLongRunning',
                          {'instances': [instance], 'parameters': params})
        name = data.get('name')
        if not name:
            raise VeoNonStopError(f"Vertex: в ответе нет имени операции: {json.dumps(data)[:300]}")
        logging.info("Vertex submitted %s -> %s", self.video_model, name)
        return name

    @staticmethod
    def _image_obj(path: str) -> dict:
        b64, mime = _image_to_base64(path)
        return {'bytesBase64Encoded': b64, 'mimeType': mime}

    def submit_image_to_video(self, image_path: str, prompt: str, aspect_ratio: Optional[str] = None,
                              count: int = 1, duration=None) -> str:
        instance = {'prompt': prompt, 'image': self._image_obj(image_path)}
        return self._submit_video(instance, self._video_params(aspect_ratio, count, duration))

    def submit_text_to_video(self, prompt: str, aspect_ratio: Optional[str] = None,
                             count: int = 1, duration=None) -> str:
        return self._submit_video({'prompt': prompt}, self._video_params(aspect_ratio, count, duration))

    def submit_batch_frame(self, start_image_path: str, end_image_path: str, prompt: str,
                           aspect_ratio: Optional[str] = None, count: int = 1, duration=None) -> str:
        """Переход «первый кадр → последний кадр» (image + lastFrame)."""
        instance = {'prompt': prompt, 'image': self._image_obj(start_image_path),
                    'lastFrame': self._image_obj(end_image_path)}
        return self._submit_video(instance, self._video_params(aspect_ratio, count, duration))

    def submit_multi_image_to_video(self, image_paths: List[str], prompt: str,
                                    aspect_ratio: Optional[str] = None, count: int = 1) -> str:
        """Видео по референсам (до 3 картинок). Поддержку проверяет сам Vertex."""
        refs = [{'image': self._image_obj(p), 'referenceType': 'asset'} for p in list(image_paths)[:3]]
        params = self._video_params(aspect_ratio, count, 8)      # с референсами — только 8 секунд
        return self._submit_video({'prompt': prompt, 'referenceImages': refs}, params)

    def submit_multi_image_to_video_from_ref_dir(self, ref_dir: str, prompt: str,
                                                 aspect_ratio: Optional[str] = None, count: int = 1) -> str:
        paths = [str(p) for p in sorted(Path(ref_dir).iterdir(), key=lambda p: p.name.lower())
                 if p.is_file() and p.suffix.lower() in MIME_TYPES]
        if not paths:
            raise FileNotFoundError(f"No reference images found in {ref_dir}")
        return self.submit_multi_image_to_video(paths, prompt, aspect_ratio, count)

    def submit_upsample_video(self, *_a, **_kw) -> str:
        raise VeoNonStopError("Апскейл видео в Vertex не поддерживается — выключите его в настройках видео")

    def poll_video_status(self, task_id: str, timeout: Optional[int] = None,
                          interval: Optional[int] = None, hung_timeout: int = 120) -> dict:
        """Ждать готовности операции. Возвращает словарь в формате VeoNonStop (`videos`)."""
        timeout = timeout or int(os.environ.get('VEONONSTOP_POLL_TIMEOUT', '') or 900)
        interval = max(3, interval or int(os.environ.get('VEONONSTOP_POLL_INTERVAL', '') or 10))
        model = task_id.split('/models/')[-1].split('/operations/')[0] if '/models/' in task_id else self.video_model
        deadline = time.time() + timeout
        while time.time() < deadline:
            data = self._post(model, 'fetchPredictOperation', {'operationName': task_id}, timeout=(30, 60))
            if data.get('done'):
                return self._finish_operation(task_id, data)
            time.sleep(interval)
        raise VeoNonStopTimeoutError(f"Task {task_id} timed out after {timeout}s")

    def _finish_operation(self, task_id: str, data: dict) -> dict:
        err = data.get('error')
        if err:
            msg = err.get('message') if isinstance(err, dict) else str(err)
            code = err.get('code') if isinstance(err, dict) else ''
            raise VeoNonStopGenerationError(f"Task {task_id} failed: {code} {msg}".strip())
        response = data.get('response') or {}
        videos = response.get('videos') or []
        if not videos:
            reasons = response.get('raiMediaFilteredReasons') or []
            raise VeoNonStopGenerationError(
                f"Task {task_id} failed: Vertex не вернул видео (отфильтровано: "
                f"{response.get('raiMediaFilteredCount', 0)}; {'; '.join(map(str, reasons)) or 'причина не указана'})")
        with self._lock:
            self._videos[task_id] = videos
        return {'status': 'completed', 'task_id': task_id,
                'videos': [{'mediaGenerationId': None, 'fifeUrl': None} for _ in videos]}

    def download_video(self, task_id: str, output_path: str, video_index: int = 0) -> str:
        with self._lock:
            videos = self._videos.get(task_id)
        if not videos or video_index >= len(videos):
            raise VeoNonStopError(f"Download video {task_id}: результат не найден (опрос не выполнялся?)")
        item = videos[video_index]
        encoded = item.get('bytesBase64Encoded')
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        if encoded:
            write_bytes_atomic(out, base64.b64decode(encoded))
        elif item.get('gcsUri'):
            raise VeoNonStopError(
                f"Vertex вернул видео в Cloud Storage ({item['gcsUri']}): скачать его без "
                f"OAuth нельзя. Не задавайте storageUri — тогда видео приходит байтами")
        else:
            raise VeoNonStopError(f"Vertex: в результате {task_id} нет ни байтов, ни ссылки")
        if not _verify_video_integrity(out):
            out.unlink(missing_ok=True)
            raise VeoNonStopError(f"Download video {task_id}: файл не прошёл проверку ffprobe")
        with self._lock:
            self._videos.pop(task_id, None)             # байты тяжёлые — освобождаем память
        logging.info("Vertex video %s -> %s", task_id.rsplit('/', 1)[-1], output_path)
        return str(out)

    # --------------------------------------------------------------- картинки
    def _imagen_image(self, model: str, prompt: str, aspect_ratio: Optional[str]) -> bytes:
        aspect = str(aspect_ratio or os.environ.get('VEONONSTOP_ASPECT_RATIO', '') or '16:9')
        if aspect not in IMAGE_ASPECTS:
            logging.warning("Формат %r не поддерживается Imagen — беру 16:9", aspect)
            aspect = '16:9'
        params = {'sampleCount': 1, 'aspectRatio': aspect, 'addWatermark': False,
                  'includeRaiReason': True, 'personGeneration': 'allow_adult'}
        if model.startswith('imagen-3'):
            params['enhancePrompt'] = False
        from units.frame_format import with_frame_format
        data = self._post(model, 'predict', {'instances': [{'prompt': with_frame_format(prompt, aspect)}],
                                             'parameters': params})
        for pred in data.get('predictions') or []:
            if pred.get('bytesBase64Encoded'):
                return base64.b64decode(pred['bytesBase64Encoded'])
        reasons = [str(p.get('raiFilteredReason')) for p in data.get('predictions') or []
                   if p.get('raiFilteredReason')]
        raise VeoNonStopGenerationError(
            f"Vertex Imagen не вернул картинку: {'; '.join(reasons) or 'отфильтровано политикой безопасности'}")

    def _gemini_image(self, model: str, prompt: str, aspect_ratio: Optional[str]) -> bytes:
        from units.frame_format import with_frame_format
        aspect = str(aspect_ratio or os.environ.get('VEONONSTOP_ASPECT_RATIO', '') or '16:9')
        parts: List[dict] = [{'text': with_frame_format(prompt, aspect)}]
        body = {'contents': [{'role': 'user', 'parts': parts}],
                'generationConfig': {'responseModalities': ['IMAGE'],
                                     'imageConfig': {'aspectRatio': aspect if aspect in IMAGE_ASPECTS else '16:9'}}}
        data = self._post(model, 'generateContent', body)
        for cand in data.get('candidates') or []:
            for part in (cand.get('content') or {}).get('parts') or []:
                inline = part.get('inlineData') or part.get('inline_data')
                if inline and inline.get('data'):
                    return trim_letterbox(base64.b64decode(inline['data']), aspect)
        reason = (data.get('promptFeedback') or {}).get('blockReason') or \
                 ((data.get('candidates') or [{}])[0].get('finishReason'))
        raise VeoNonStopGenerationError(f"Vertex Gemini Image не вернул картинку: {reason or 'пустой ответ'}")

    def generate_banana(self, prompt: str, output_path: str, aspect_ratio: Optional[str] = None,
                        num_images: int = 1, model_key: Optional[str] = None,
                        reference_images: Optional[List[Dict[str, str]]] = None,
                        use_all_ref_images: bool = False) -> str:
        """Картинка только по тексту. Имя и параметры с референсами оставлены для совместимости
        со стадиями: Vertex референсы не использует, `reference_images` игнорируется."""
        model = self.image_model
        logging.info("Vertex image: model=%s aspect=%s", model, aspect_ratio)
        if _is_gemini_image(model):
            raw = self._gemini_image(model, prompt, aspect_ratio)
        else:
            raw = self._imagen_image(model, prompt, aspect_ratio)
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        write_bytes_atomic(out, raw)
        return BananaResult(str(out), media_id='', project_id=self.project_id)

    def generate_imagen(self, prompt: str, output_path: str, aspect_ratio: Optional[str] = None) -> str:
        return self.generate_banana(prompt, output_path, aspect_ratio)

    def upscale_banana(self, *_a, **_kw) -> str:
        raise VeoNonStopError("Апскейл картинок в Vertex не поддерживается — выключите его в настройках")

    # --------------------------------------------------------- служебные вызовы
    def cancel_task(self, task_id: str) -> bool:
        return False                          # у операций Veo на Vertex отмены нет

    def cancel_all_tasks(self) -> int:
        return 0

    def cancel_all_image_tasks(self) -> int:
        return 0

    def get_account_info(self) -> dict:
        return {'plan': 'vertex', 'concurrent_tasks': int(os.environ.get('VERTEX_PARALLEL', '') or 2)}

    def get_account_usage(self) -> dict:
        return {}                             # счётчиков слотов и квоты у Vertex через API ключа нет


def vertex_ready() -> bool:
    """Vertex выбран провайдером картинок или видео и его ключ с проектом заданы."""
    chosen = any(vertex_selected(kind) for kind in ('image', 'video'))
    return chosen and bool(os.environ.get('VERTEX_API_KEY', '').strip()
                           and os.environ.get('VERTEX_PROJECT_ID', '').strip())


def vertex_selected(kind: str) -> bool:
    """Выбран ли Vertex провайдером для 'image' или 'video' (по окружению запуска)."""
    return os.environ.get(f'BEGO_{kind.upper()}_PROVIDER', '').strip().lower() == 'vertex'


def build_vertex_cfg(kind: str, image_model: Optional[str] = None) -> List[Dict[str, Any]]:
    """Конфигурация клиентов в формате стадий: `[{client, mp, sem, label, slot, ...}]`."""
    try:
        workers = max(1, int(os.environ.get(f'VERTEX_{kind.upper()}_PARALLEL', '')
                             or os.environ.get('VERTEX_PARALLEL', '') or 2))
    except ValueError:
        workers = 2
    client = VertexClient(image_model=image_model)   # image_model — своя модель (например, у инфографики)
    sem = threading.Semaphore(workers)
    print(f"  [Vertex] project={client.project_id} region={client.location} "
          f"model={client.video_model if kind == 'video' else client.image_model} workers={workers}")
    return [{'client': client, 'mp': workers, 'sem': sem, 'sem_slots': workers,
             'label': 'Vertex', 'slot': 1}]
