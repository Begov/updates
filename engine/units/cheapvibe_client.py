"""
Клиент CheapVibeCode — картинки GPT Image 2 и Grok Imagine через OpenAI-совместимый API.

Как и VertexClient, повторяет интерфейс `VeoNonStopClient` в части, нужной
стадии картинок (`generate_banana`, `generate_imagen`, служебные вызовы), поэтому
стадия veo_stills работает с ним без переписывания: провайдер выбирается при
создании клиентов (BEGO_IMAGE_PROVIDER=cheapvibe).

Эндпоинт (документация провайдера):
  POST {base}/images/generations, JSON:
  {"model": "gpt-image-2" | "grok-imagine-image" | "nano-banana-2", "prompt": ..., "size": "1536x1024",
   "quality": "low|medium|high", "n": 1, "response_format": "b64_json"}
Ответ OpenAI-формата: {"data": [{"b64_json": ...}]} (или {"url": ...}).
Авторизация — `Authorization: Bearer <ключ>`. Модель — CHEAPVIBE_IMAGE_MODEL.

Списание фиксированное за картинку и зависит от модели и качества (токенов):
  gpt-image-2:        low 50K,  medium 100K, high 150K
  grok-imagine-image: low 100K, medium 250K, high 350K
  nano-banana-2:      только low 100K
"""

import base64
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

try:  # те же классы исключений, что ловят стадии
    from veo_client import (
        BananaResult, VeoNonStopError, VeoNonStopGenerationError, VeoNonStopRateLimitError,
        VeoNonStopTimeoutError,
    )
except ImportError:                                               # pragma: no cover
    from units.veo_client import (
        BananaResult, VeoNonStopError, VeoNonStopGenerationError, VeoNonStopRateLimitError,
        VeoNonStopTimeoutError,
    )

DEFAULT_BASE = 'https://ru.cheapvibecode.ru/v1'
DEFAULT_MODEL = 'gpt-image-2'
MODELS = ('gpt-image-2', 'grok-imagine-image', 'nano-banana-2')
# Качества по моделям: у Nano Banana 2 провайдер принимает только low
MODEL_QUALITIES = {'nano-banana-2': ('low',)}
SIZES = ('1024x1024', '1536x1024', '1024x1536', '2048x2048')
QUALITIES = ('low', 'medium', 'high')


def _error_text(resp) -> str:
    """Сообщение об ошибке OpenAI-формата: {"error": {"message", "code", "type"}}."""
    try:
        body = resp.json()
    except Exception:                                              # noqa: BLE001
        return (resp.text or '')[:300]
    err = body.get('error') if isinstance(body, dict) else None
    if isinstance(err, dict):
        parts = [str(err.get('code') or err.get('type') or ''), str(err.get('message') or '')]
        return ' | '.join(p for p in parts if p)[:500]
    if isinstance(err, str):
        return err[:500]
    return str(body)[:300]


class CheapVibeClient:
    """Клиент CheapVibeCode с интерфейсом VeoNonStopClient (в части, нужной стадии картинок)."""

    api_version = 1                       # метка для логов
    provider = 'cheapvibe'

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None,
                 model: Optional[str] = None, size: Optional[str] = None, quality: Optional[str] = None):
        self.api_key = (api_key or os.environ.get('CHEAPVIBE_API_KEY', '')).strip()
        self.base_url = (base_url or os.environ.get('CHEAPVIBE_API_BASE', '') or DEFAULT_BASE).strip().rstrip('/')
        model = (model or os.environ.get('CHEAPVIBE_IMAGE_MODEL', '') or DEFAULT_MODEL).strip()
        self.model = model if model in MODELS else DEFAULT_MODEL
        size = (size or os.environ.get('CHEAPVIBE_IMAGE_SIZE', '') or '1536x1024').strip().lower()
        self.size = size if size in SIZES else '1536x1024'
        quality = (quality or os.environ.get('CHEAPVIBE_IMAGE_QUALITY', '') or 'medium').strip().lower()
        allowed = MODEL_QUALITIES.get(self.model, QUALITIES)
        self.quality = quality if quality in allowed else ('medium' if 'medium' in allowed else allowed[0])
        if not self.api_key:
            raise ValueError("CHEAPVIBE_API_KEY не задан (Настройки → CheapVibeCode)")
        self.headers = {'Authorization': f'Bearer {self.api_key}', 'Content-Type': 'application/json'}

    # ------------------------------------------------------------------ HTTP
    def _post(self, path: str, body: dict, timeout=(30, 600)) -> dict:
        """POST к API. Повтор при сети/429/5xx с нарастающей паузой."""
        url = f'{self.base_url}/{path}'
        delays = (10, 30, 60)
        for attempt in range(len(delays) + 1):
            try:
                resp = requests.post(url, headers=self.headers, json=body, timeout=timeout)
            except requests.RequestException as exc:
                if attempt < len(delays):
                    logging.warning("CheapVibeCode: сеть — %s, повтор через %ss", exc, delays[attempt])
                    time.sleep(delays[attempt])
                    continue
                raise VeoNonStopTimeoutError(f"CheapVibeCode: сеть недоступна: {exc}") from exc
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError as exc:
                    raise VeoNonStopError(f"CheapVibeCode: ответ не JSON: {(resp.text or '')[:200]}") from exc
            text = _error_text(resp)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < len(delays):
                logging.warning("CheapVibeCode: HTTP %s %s — повтор через %ss",
                                resp.status_code, text[:120], delays[attempt])
                time.sleep(delays[attempt])
                continue
            if resp.status_code == 429:
                raise VeoNonStopRateLimitError(f"CheapVibeCode rate limit: {text}")
            if resp.status_code >= 500:
                raise VeoNonStopTimeoutError(f"CheapVibeCode server error {resp.status_code}: {text}")
            if resp.status_code in (401, 403):
                raise VeoNonStopError(f"CheapVibeCode: ключ не принят (HTTP {resp.status_code}): {text}")
            if resp.status_code == 402:
                raise VeoNonStopError(f"CheapVibeCode: недостаточно токенов на балансе: {text}")
            # 400 с текстом модерации («safety system», «moderation_blocked», «content_policy»)
            # стадия опознаёт по словам и отправляет промпт на переписывание
            raise VeoNonStopGenerationError(f"CheapVibeCode HTTP {resp.status_code}: {text}")
        raise VeoNonStopError("CheapVibeCode: исчерпаны повторы")      # недостижимо

    def _image_bytes(self, data: dict) -> bytes:
        items = data.get('data') if isinstance(data, dict) else None
        if not items:
            raise VeoNonStopGenerationError(f"CheapVibeCode не вернул картинку: {str(data)[:300]}")
        item = items[0] or {}
        if item.get('b64_json'):
            return base64.b64decode(item['b64_json'])
        if item.get('url'):
            resp = requests.get(item['url'], timeout=(30, 300))
            resp.raise_for_status()
            return resp.content
        raise VeoNonStopGenerationError(f"CheapVibeCode: в ответе нет картинки: {str(item)[:300]}")

    # --------------------------------------------------------------- картинки
    def generate_banana(self, prompt: str, output_path: str, aspect_ratio: Optional[str] = None,
                        num_images: int = 1, model_key: Optional[str] = None,
                        reference_images: Optional[List[Dict[str, str]]] = None,
                        use_all_ref_images: bool = False) -> str:
        """Картинка по тексту. Имя и параметры оставлены для совместимости со стадиями:
        размер задаётся в настройках (CHEAPVIBE_IMAGE_SIZE), референсы не используются."""
        logging.info("CheapVibeCode image: model=%s size=%s quality=%s", self.model, self.size, self.quality)
        # Формат в теле — только размером (size подобран под формат ролика); текстом — всегда.
        from units.frame_format import with_frame_format
        data = self._post('images/generations', {
            'model': self.model, 'prompt': with_frame_format(prompt, aspect_ratio), 'size': self.size,
            'quality': self.quality, 'n': 1, 'response_format': 'b64_json',
        })
        raw = self._image_bytes(data)
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        write_bytes_atomic(out, raw)
        return BananaResult(str(out))

    def generate_imagen(self, prompt: str, output_path: str, aspect_ratio: Optional[str] = None) -> str:
        return self.generate_banana(prompt, output_path, aspect_ratio)

    # --------------------------------------------------------- служебные вызовы
    def cancel_task(self, task_id: str) -> bool:
        return False                          # запрос синхронный — отменять нечего

    def cancel_all_tasks(self) -> int:
        return 0

    def cancel_all_image_tasks(self) -> int:
        return 0

    def get_account_info(self) -> dict:
        return {'plan': 'cheapvibe',
                'concurrent_tasks': int(os.environ.get('CHEAPVIBE_IMAGE_PARALLEL', '') or 2)}

    def get_account_usage(self) -> dict:
        return {}                             # счётчиков квоты через API нет


def cheapvibe_selected() -> bool:
    """Выбран ли CheapVibeCode провайдером картинок (по окружению запуска)."""
    return os.environ.get('BEGO_IMAGE_PROVIDER', '').strip().lower() == 'cheapvibe'


def cheapvibe_ready() -> bool:
    """CheapVibeCode выбран провайдером картинок и его ключ задан."""
    return cheapvibe_selected() and bool(os.environ.get('CHEAPVIBE_API_KEY', '').strip())


def build_cheapvibe_cfg(model: Optional[str] = None, size: Optional[str] = None,
                        quality: Optional[str] = None) -> List[Dict[str, Any]]:
    """Конфигурация клиентов в формате стадий: `[{client, mp, sem, label, slot, ...}]`."""
    try:
        workers = max(1, int(os.environ.get('CHEAPVIBE_IMAGE_PARALLEL', '') or 2))
    except ValueError:
        workers = 2
    client = CheapVibeClient(model=model, size=size, quality=quality)   # None — из CHEAPVIBE_IMAGE_*
    sem = threading.Semaphore(workers)
    print(f"  [CheapVibeCode] model={client.model} size={client.size} quality={client.quality} "
          f"workers={workers}")
    return [{'client': client, 'mp': workers, 'sem': sem, 'sem_slots': workers,
             'label': 'CheapVibeCode', 'slot': 1}]
