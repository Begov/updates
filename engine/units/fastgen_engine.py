"""
FastGen API - генерация изображений (этап 3 пайплайна)

Поддерживает два режима:
- Flow API: до 10 референсов как общий массив
- Whisk Remix: категоризированные референсы (subject/scene/style)
"""
import base64
import json
import sys
import time
import random
import logging

def _stream_is_tty(stream) -> bool:
    """Это интерактивная консоль? Безопасно при её отсутствии.

    Под `pythonw` у процесса нет stdout вовсе (`sys.stdout is None`), и прямой
    `sys.stdout.isatty()` на уровне модуля ронял импорт с AttributeError —
    стадия не запускалась совсем (проверено 30.08). Нет потока или он не умеет
    отвечать — считаем, что консоли нет: это же режим subprocess/GUI.
    """
    try:
        return bool(stream is not None and stream.isatty())
    except Exception:                                          # noqa: BLE001
        return False


IS_PIPE = not _stream_is_tty(sys.stdout)  # True когда запущен как subprocess (pipeline mode)
import threading
import re
import requests
try:  # файл появляется под своим именем только целиком (см. units/media_io.py)
    from units.media_io import atomic_target, write_bytes_atomic
except ImportError:                                               # pragma: no cover
    from media_io import atomic_target, write_bytes_atomic
from pathlib import Path
from typing import Optional, Dict, List, Any, Tuple
from datetime import datetime, timedelta

# FastGen — общий сигнал остановки («Стоп») и реестр активных клиентов.
# Control Panel's Stop button sets event + calls cancel_all_tasks on each registered client.
_flowultra_shutdown_event: threading.Event = threading.Event()
_flowultra_active_clients: 'list' = []


def _flowultra_sleep_interruptible(seconds: float, step: float = 0.25) -> bool:
    if seconds <= 0:
        return _flowultra_shutdown_event.is_set()
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _flowultra_shutdown_event.is_set():
            return True
        time.sleep(min(step, max(0.0, deadline - time.time())))
    return _flowultra_shutdown_event.is_set()

from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
import colorama
from colorama import Fore, Style
from units.still_claims import acquire_image_claim

colorama.init(autoreset=True)

# Worker tracking для dual-key стаггера (первый запуск vs повторный)
_fg_started_workers: set = set()
_fg_started_workers_lock = threading.Lock()
_fg_worker_map: dict = {}
_fg_worker_lock = threading.Lock()
_fg_worker_counter: dict = {'next': 0}

# Цветовые алиасы для консольного вывода
_YELLOW = Fore.YELLOW    # обычный retry (сеть, таймаут, ratelimit)
_RED    = Fore.RED       # цензура — переписывание промпта
_GREEN  = Fore.GREEN     # OK
_CYAN   = Fore.CYAN      # speed metrics
_RESET  = Style.RESET_ALL

# ── Детектор временных техсбоев апстрима (ЕДИНЫЙ источник правды) ──────────────
# Раньше этот список был инлайн-скопирован в 5 местах с РАЗНЫМИ наборами фраз.
# Из-за рассинхрона фраза "service is temporarily unavailable for maintenance" не
# ловилась (искали слитное "service unavailable") → OpenAI-бэкенд не переключался
# на Banana Pro, а уходил в 5 бесполезных циклов реврайта. Одна функция — один список.
#
# Семантика: «это временный сбой инфраструктуры/перегрузка апстрима, а НЕ цензура и
# НЕ ref-block». Здесь СОЗНАТЕЛЬНО нет цензурных слов (blocked/censored/policy/…) —
# они обрабатываются отдельной веткой (rewrite). Пересечение проверяется тестом.
_TRANSIENT_ERROR_MARKERS = (
    # сеть / соединение
    'timeout', 'timed out', 'operation timed out', 'read timed out',
    'connection', 'econnreset', 'socket', 'network', 'curl:',
    # HTTP 5xx / шлюзы
    '5xx', '500', '502', '503', '504',
    'internal server error', 'server error', 'bad gateway', 'gateway timeout',
    'cloudflare', 'storage server',
    # rate limit
    'rate limit', '429',
    # апстрим недоступен / на обслуживании / перегружен  ← недостающие фразы (корень бага)
    'service unavailable', 'temporarily unavailable', 'unavailable for maintenance',
    'maintenance', 'under maintenance', 'overloaded', 'high demand', 'lots of people',
    # прочие транзиентные статусы FastGen
    'did not finish', 'please try again', 'try again later',
    'processing image', 'running',
)


def _is_transient_error(err: str) -> bool:
    """True — ошибка это временный техсбой/перегрузка апстрима (ретрай/смена модели),
    а не цензура и не ref-block. Единый детектор для всех мест."""
    low = (err or '').lower()
    return any(k in low for k in _TRANSIENT_ERROR_MARKERS)


# ── Ошибки ЁМКОСТИ апстрима под конкретную модель ─────────────────────────────
# «No accounts available» — в пуле FastGen нет свободных аккаунтов под эту
# операцию (например openai_image_generate). Повтор тем же путём через 5с даёт
# ровно то же: пул не меняется за секунды, а модель осталась та же.
# Прогон «пикассо» 17.08.2026: 343 клипа отработали ВСЕ 3 попытки phase1
# (1095 запросов) и лишь потом ушли на Banana Pro, накопив 5725с пауз по
# воркерам. Поэтому такой класс ошибок = мгновенный выход из фазы: наверху
# клип уходит на Banana Pro, которая идёт мимо OpenAI-пула.
# Сеть/таймауты/5xx/429 сюда СОЗНАТЕЛЬНО не входят — там ретрай осмыслен.
_CAPACITY_ERROR_MARKERS = (
    'no accounts available',
    'no account available',
    'no accounts left',
    'overloaded',
    'high demand',
    'lots of people',
)


def _is_capacity_error(err: str) -> bool:
    """True — у апстрима нет свободной ёмкости под текущую модель.
    Ретрай той же моделью бесполезен, нужна смена модели/провайдера."""
    low = (err or '').lower()
    return any(k in low for k in _CAPACITY_ERROR_MARKERS)

import os
import params as config
from prompt_extract import extract_all_prompts, find_prompts_file, remap_dense_prompts_for_montage

# Ensure agent/modules/ and agent/tools/ are on sys.path
_modules_dir = str(Path(__file__).resolve().parent)
_agent_dir   = str(Path(__file__).resolve().parent.parent)
for _p in (_modules_dir, _agent_dir):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ref_remixer import remix_ref


# ======================================================================
# SynthID inflight watermark removal
# ======================================================================



# Поддерживаемые форматы изображений
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp'}


def _clean_resp_text(text: str, limit: int = 300) -> str:
    """Обрезает resp.text — убирает HTML-мусор (Cloudflare/502) из сообщений об ошибках."""
    if '<html' in text.lower() or '<!doctype' in text.lower():
        return f"[HTML response, likely Cloudflare/5xx] {text[:150].replace(chr(10), ' ')}"
    return text[:limit]


class FastGenClient:
    """Клиент FastGen API для генерации изображений"""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or config.FASTGEN_API_KEY
        self.base_url = config.FASTGEN_API_BASE
        self.storage_url = config.FASTGEN_STORAGE_URL
        self.headers = {
            'X-API-Key': self.api_key,
            'Content-Type': 'application/json',
        }
        # Set to True when _preflight_test_refs returns True due to exception
        # (not actual success). Caller checks this to avoid logging false "OK".
        self._last_preflight_skipped = False

        if not self.api_key:
            raise ValueError("FASTGEN_API_KEY not set in .env")

    # ------------------------------------------------------------------
    # Storage: загрузка референсов
    # ------------------------------------------------------------------

    def upload_reference(self, image_path: str, max_retries: int = 3) -> str:
        """
        Загрузить референсное изображение в FastGen Storage.

        Ретраит загрузку (timeout/сетевая ошибка/non-200) до max_retries раз.
        Если все попытки провалились — RAISE (генерация без рефа = брак, левые
        персонажи; вызывающий код НЕ должен продолжать без рефа).

        Args:
            image_path: Путь к файлу изображения
            max_retries: число попыток (timeout 120с каждая)

        Returns:
            Raw 32-char storage id (без префикса) — V6 V6MediaInput.
        """
        path = Path(image_path)
        if not path.exists():
            raise FileNotFoundError(f"Reference image not found: {image_path}")

        # V6 storage: /v2/upload → raw storage id (V1 /upload давал file:<hash>, legacy)
        url = f"{self.storage_url}/v2/upload"

        # Отправляем в оригинальном формате — не конвертируем в PNG,
        # чтобы не раздувать тело запроса (JPEG ~1MB vs PNG ~5-8MB)
        mime_map = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                    '.webp': 'image/webp', '.png': 'image/png'}
        mime = mime_map.get(path.suffix.lower(), 'image/png')

        _last_err = None
        data = None
        for _attempt in range(1, max_retries + 1):
            try:
                with open(path, 'rb') as f:
                    resp = requests.post(
                        url,
                        headers={'X-API-Key': self.api_key},
                        files={'file': (path.name, f, mime)},
                        timeout=120,  # рефы могут быть тяжёлыми + медленная сеть
                    )
                if resp.status_code != 200:
                    _last_err = f"HTTP {resp.status_code}: {_clean_resp_text(resp.text)}"
                    logging.warning(f"upload {path.name} attempt {_attempt}/{max_retries}: {_last_err}")
                    time.sleep(2 * _attempt)
                    continue
                data = resp.json()
                break
            except requests.exceptions.RequestException as e:
                _last_err = f"network: {e}"
                logging.warning(f"upload {path.name} attempt {_attempt}/{max_retries}: {_last_err}")
                time.sleep(2 * _attempt)
                continue

        if data is None:
            raise RuntimeError(
                f"Storage upload failed for {path.name} after {max_retries} retries: {_last_err}")

        # V2 upload returns {"id": "<raw 32hex>", "file_hash": "<raw 32hex>", "deduplicated": ..., ...}
        uri = data.get('id') or data.get('file_hash')
        if not uri:
            raise RuntimeError(f"Storage V2 upload: no id in response: {data}")

        # V6 принимает чистый 32-hex — префикс file: НЕ добавляем (это был legacy V1).
        if isinstance(uri, str) and uri.startswith('file:'):
            uri = uri[len('file:'):]

        logging.info(f"Uploaded reference {path.name} -> {uri} (dedup={data.get('deduplicated')})")
        return uri

    # ------------------------------------------------------------------
    # Flow API: генерация изображений (async)
    # ------------------------------------------------------------------

    # Маппинг движков по имени модели (V6 providers: flow, flower, grok, openai).
    # whisk/imagen — УМЕРЛИ (провайдеров нет в V6), выпилены.
    _ENGINE_MAP = {
        'GEM_PIX':    'flow',
        'GEM_PIX_2':  'flow',
        'NARWHAL':    'flow',   # Nano Banana 2
        'FLOWER':     'flower', # Nano Banana 2 via Flower
        'GROK':       'grok',
        'OPENAI':     'openai', # ChatGPT web image (openai_image_generate)
    }

    @staticmethod
    def _get_engine(model: str) -> str:
        """Определить API-движок по имени модели"""
        return FastGenClient._ENGINE_MAP.get((model or '').upper(), 'flow')

    # aspect_ratio aliases → полный Google enum (для Flow/Whisk)
    _ASPECT_MAP = {
        'landscape': 'IMAGE_ASPECT_RATIO_LANDSCAPE',
        '16:9':      'IMAGE_ASPECT_RATIO_LANDSCAPE',
        'portrait':  'IMAGE_ASPECT_RATIO_PORTRAIT',
        '9:16':      'IMAGE_ASPECT_RATIO_PORTRAIT',
        'square':    'IMAGE_ASPECT_RATIO_SQUARE',
    }

    # aspect_ratio aliases → формат Grok (1:1, 2:3, 3:2, 9:16, 16:9)
    _ASPECT_MAP_GROK = {
        'landscape': '16:9',
        '16:9':      '16:9',
        'portrait':  '9:16',
        '9:16':      '9:16',
        'square':    '1:1',
        '1:1':       '1:1',
        '2:3':       '2:3',
        '3:2':       '3:2',
    }

    @staticmethod
    def _map_aspect(aspect: str) -> str:
        """Нормализовать aspect_ratio в полный Google формат (Flow/Whisk)"""
        if aspect and aspect.startswith('IMAGE_ASPECT_RATIO_'):
            return aspect
        return FastGenClient._ASPECT_MAP.get(aspect.lower() if aspect else '', 'IMAGE_ASPECT_RATIO_LANDSCAPE')

    # ------------------------------------------------------------------
    # V6 API: универсальный endpoint /v6/generations с named refs.
    # Используется для flow-моделей когда требуется token-based ref-binding
    # (когда промпт содержит [name_ref] и нужен точный матчинг лица из конкретного ref).
    # ------------------------------------------------------------------

    # Маппинг model → v6 operation_id (см. GET /api/v6/capabilities)
    # Используется для flow-engine (выбор операции по model).
    _V6_MODEL_TO_OPERATION = {
        'GEM_PIX':    'nano_banana_2_image_generate',
        'GEM_PIX_2':  'nano_banana_pro_image_generate',
        'NARWHAL':    'nano_banana_2_image_generate',
    }

    # Маппинг engine → v6 operation_id для НЕ-flow engines.
    # Для grok/flower есть отдельные generate/edit операции — выбор по числу refs
    # делается в caller'е (см. _v6_operation_for_engine).
    _V6_ENGINE_TO_OPERATION = {
        'openai':      'openai_image_generate',
        'grok':        'grok_image_generate',
        'grok_edit':   'grok_image_edit',
        'flower':      'flower_image_generate',
        'flower_edit': 'flower_image_edit',
    }

    @staticmethod
    def _v6_operation_for_engine(engine: str, has_refs: bool) -> Optional[str]:
        """Выбрать v6 operation_id по engine + наличию refs.
        grok / flower имеют отдельные T2I и edit операции."""
        e = (engine or '').lower()
        if e == 'grok':
            return FastGenClient._V6_ENGINE_TO_OPERATION['grok_edit' if has_refs else 'grok']
        if e == 'flower':
            return FastGenClient._V6_ENGINE_TO_OPERATION['flower_edit' if has_refs else 'flower']
        if e in ('openai',):
            return FastGenClient._V6_ENGINE_TO_OPERATION['openai']
        return None

    # aspect_ratio для v6: формат n:n (не Google enum)
    _V6_ASPECT_MAP = {
        'landscape':                    '16:9',
        '16:9':                         '16:9',
        'portrait':                     '9:16',
        '9:16':                         '9:16',
        'square':                       '1:1',
        '1:1':                          '1:1',
        '4:3':                          '4:3',
        '3:4':                          '3:4',
        'IMAGE_ASPECT_RATIO_LANDSCAPE': '16:9',
        'IMAGE_ASPECT_RATIO_PORTRAIT':  '9:16',
        'IMAGE_ASPECT_RATIO_SQUARE':    '1:1',
    }

    @staticmethod
    def _v6_model_to_operation(model: str) -> Optional[str]:
        return FastGenClient._V6_MODEL_TO_OPERATION.get((model or '').upper())

    @staticmethod
    def _v6_map_aspect(aspect: str) -> str:
        return FastGenClient._V6_ASPECT_MAP.get(aspect or '', '16:9')

    @staticmethod
    def _v6_normalize_input(value: str) -> str:
        """V6 V6MediaInput принимает либо data URI, либо ЧИСТЫЙ 32-char hex.
        Наш legacy upload мог вернуть 'file:<hex>' — стрипаем префикс для V6.
        Если data URI или уже чистый hex — оставляем как есть."""
        if isinstance(value, str) and value.startswith('file:'):
            return value[len('file:'):]
        return value

    def _v6_submit(
        self,
        operation: str,
        prompt: str,
        named_inputs: Optional[List[Dict[str, str]]] = None,
        positional_inputs: Optional[List[str]] = None,
        aspect_ratio: str = '16:9',
        seed: Optional[int] = None,
        keyframes: bool = False,
        ultra: bool = False,
        quality: Optional[str] = None,
        resolution: Optional[str] = None,
        duration_seconds: Optional[int] = None,
        count: Optional[int] = None,
        upscale_2x: bool = False,
    ) -> str:
        """POST /api/v6/generations — единый endpoint для image+video.

        inputs — ОДНО из двух:
          named_inputs      = [{"filename": "sael_ref", "input": "<hex>|data:..."}] — рефы
                              персонажей/объектов (по имени; промпт ссылается на [имя]).
                              Работает и для картинок, и для video-components (ingredients).
          positional_inputs = ["<hex>", "<hex>"] — режим СТАРТ-ЭНД (keyframes): inputs[0]=старт-кадр,
                              inputs[1]=конечный кадр (опц.). Требует keyframes=True.
        Префикс 'file:' стрипается — V6 принимает чистый 32-hex или data URI."""
        url = f"{self.base_url}/v6/generations"
        from units.frame_format import with_frame_format
        body = {
            'operation': operation,
            # формат кадра — и полем, и последней строкой промпта (единая точка V6:
            # через неё идут картинки, видео и ремиксы Grok/Flower/OpenAI)
            'prompt': with_frame_format(prompt, self._v6_map_aspect(aspect_ratio)),
            'aspect_ratio': self._v6_map_aspect(aspect_ratio),
        }
        if positional_inputs:
            body['inputs'] = [self._v6_normalize_input(v) for v in positional_inputs if v]
        elif named_inputs:
            body['inputs'] = [
                {**ni, 'input': self._v6_normalize_input(ni.get('input', ''))}
                for ni in named_inputs
            ]
        if keyframes:
            body['keyframes'] = True
        if ultra:
            body['ultra'] = True
        if seed is not None:
            body['seed'] = seed
        if quality:
            body['quality'] = quality
        if resolution:
            body['resolution'] = resolution
        if duration_seconds:
            body['duration_seconds'] = duration_seconds
        if count:
            body['count'] = count
        if upscale_2x:
            body['generation_config'] = {'upscale': {'type': '2x'}}
        resp = requests.post(url, headers=self.headers, json=body, timeout=60)
        if resp.status_code == 429:
            raise RateLimitError("Rate limit exceeded (429)")
        if resp.status_code != 200:
            raise RuntimeError(f"V6 submit failed ({resp.status_code}): {_clean_resp_text(resp.text)}")
        data = resp.json()
        gen_id = data.get('id')
        if not gen_id:
            raise RuntimeError(f"V6 submit: no id in response: {data}")
        return gen_id

    def _v6_poll(self, gen_id: str, timeout: int = 240, interval: float = 3.0) -> Dict[str, Any]:
        """GET /api/v6/generations/{id} пока статус не финальный.
        V6 status enum: queued | running | succeeded | failed."""
        url = f"{self.base_url}/v6/generations/{gen_id}"
        t0 = time.time()
        last_data: Dict[str, Any] = {}
        while time.time() - t0 < timeout:
            # Stop/Hard Stop: прерываем длинный поллинг сразу, не ждём до timeout
            if _flowultra_shutdown_event.is_set():
                last_data['status'] = 'cancelled'
                return last_data
            resp = requests.get(url, headers=self.headers, timeout=30)
            if resp.status_code == 200:
                last_data = resp.json()
                status = last_data.get('status')
                if status in ('succeeded', 'failed', 'cancelled', 'error'):
                    return last_data
            # Прерываемый sleep: реагирует на Stop в течение ~0.25с, а не interval секунд
            if _flowultra_sleep_interruptible(interval):
                last_data['status'] = 'cancelled'
                return last_data
        last_data['status'] = last_data.get('status') or 'timeout'
        return last_data

    def _v6_download(self, response: Dict[str, Any], output_path: str) -> None:
        """Скачать первый результат из ответа V6 в output_path.
        V6 GenerationResultItem: download_url (полный URL файла) ИЛИ data (inline data URI).
        Generated result TTL 30 минут."""
        results = response.get('results') or []
        if not results:
            raise RuntimeError("V6 response has no results")
        first = results[0]
        dl_url = first.get('download_url')
        if dl_url:
            rr = requests.get(dl_url, headers=self.headers, timeout=120)
            if rr.status_code != 200:
                raise RuntimeError(f"V6 download failed ({rr.status_code}): {_clean_resp_text(rr.text)}")
            write_bytes_atomic(output_path, rr.content)
            return
        # inline data URI fallback (провайдер вернул данные inline)
        inline = first.get('data')
        if inline and isinstance(inline, str) and inline.startswith('data:') and ',' in inline:
            b64 = inline.split(',', 1)[1]
            write_bytes_atomic(output_path, base64.b64decode(b64))
            return
        raise RuntimeError(f"V6 response missing download_url and inline data: {first}")

    def generate_v6_image(
        self,
        prompt: str,
        reference_pairs: Optional[List[Tuple[str, str]]] = None,
        model: Optional[str] = None,
        operation: Optional[str] = None,
        aspect_ratio: Optional[str] = None,
        output_path: Optional[str] = None,
        seed: Optional[int] = None,
        poll_timeout: int = 240,
    ) -> Dict[str, Any]:
        """
        Полный цикл V6 для image: submit + poll + download.

        Args:
            prompt: текст промпта (может содержать токены [name_ref])
            reference_pairs: список (name, uri) пар, например [("sael_ref", "file:abc..."), ...].
                             name — имя по которому промпт может ссылаться на ref.
                             uri — file:hash или data:image/png;base64,...
            model: GEM_PIX_2 / NARWHAL / IMAGEN_3_5 (мапится в operation через _v6_model_to_operation;
                   используется когда operation не задан явно)
            operation: явный v6 operation_id (приоритетнее model). Например 'openai_image_generate'
                       или 'grok_image_edit'.
            aspect_ratio: 16:9 / 9:16 / 1:1 / 4:3 / 3:4
            output_path: куда сохранить готовое изображение
            seed: опциональный seed (не у всех операций поддерживается — у flower/openai нет)

        Returns:
            dict с ключами: id, status, results, error, (saved_path если output_path задан)
        """
        op = operation or self._v6_model_to_operation(model or config.FASTGEN_MODEL)
        if not op:
            raise RuntimeError(f"V6: no operation mapping for model={model!r} / engine")
        if op == 'openai_image_generate':
            _original_prompt = prompt
            prompt = _sanitize_openai_image_prompt(prompt)
            if prompt != _original_prompt:
                logging.info(
                    "V6 OpenAI prompt policy-sanitized (%s -> %s chars)",
                    len(_original_prompt),
                    len(prompt),
                )
        named_inputs = []
        for name, uri in (reference_pairs or []):
            if uri:
                # API field renamed 'name' → 'filename' (api-1.json от 03.06.2026, V6NamedMediaInput).
                named_inputs.append({'filename': name, 'input': uri})
        # seed: только для операций которые его поддерживают (см. capabilities options_keys).
        # Если operation не из flow-семейства — seed не передаём, иначе 422.
        _seed = seed if op in (
            'nano_banana_pro_image_generate',
            'nano_banana_2_image_generate',
        ) else None
        gen_id = self._v6_submit(
            operation=op,
            prompt=prompt,
            named_inputs=named_inputs,
            aspect_ratio=aspect_ratio or config.FASTGEN_ASPECT_RATIO,
            seed=_seed,
        )
        response = self._v6_poll(gen_id, timeout=poll_timeout)
        status = response.get('status')
        if status == 'succeeded' and output_path:
            self._v6_download(response, output_path)
            response['saved_path'] = output_path
        return response

    def generate_flow(
        self,
        prompt: str,
        reference_uris: Optional[List[str]] = None,
        model: Optional[str] = None,
        aspect_ratio: Optional[str] = None,
        seed: Optional[int] = None,
    ) -> str:
        """
        Запустить генерацию через V4 Flow API (асинхронный).

        Args:
            prompt: Текст промпта
            reference_uris: Список URI референсов (file:... или data:...)
            model: Модель (GEM_PIX, GEM_PIX_2, IMAGEN_3_5)
            aspect_ratio: Соотношение сторон

        Returns:
            operation_id для поллинга
        """
        url = f"{self.base_url}/v4/flow/image/generate"
        body = {
            'prompt': prompt,
            'model': model or config.FASTGEN_MODEL,
            'aspect_ratio': self._map_aspect(aspect_ratio or config.FASTGEN_ASPECT_RATIO),
        }
        if reference_uris:
            body['reference_images'] = reference_uris[:10]  # max 10
        if seed is not None:
            body['seed'] = seed

        resp = requests.post(url, headers=self.headers, json=body, timeout=30)

        if resp.status_code == 429:
            raise RateLimitError("Rate limit exceeded (429)")
        if resp.status_code != 200:
            raise RuntimeError(f"Flow generate failed ({resp.status_code}): {_clean_resp_text(resp.text)}")

        data = resp.json()
        op_id = data.get('operation_id') or data.get('id')
        if not op_id:
            raise RuntimeError(f"No operation_id in response: {data}")

        return op_id

    # ------------------------------------------------------------------
    # Whisk Remix API: генерация с категоризированными референсами
    # ------------------------------------------------------------------

    def generate_flower(
        self,
        prompt: str,
        aspect_ratio: Optional[str] = None,
        reference_image: Optional[str] = None,
    ) -> str:
        """Flower image generation (Nano Banana 2 via Flower). Supports img2img via reference_image."""
        url = f"{self.base_url}/v4/flower/image/generate"
        _FLOWER_ASPECT = {'landscape': '16:9', '16:9': '16:9', 'portrait': '9:16', '9:16': '9:16',
                          'square': '1:1', '1:1': '1:1'}
        body = {
            'prompt': prompt,
            'aspect_ratio': _FLOWER_ASPECT.get((aspect_ratio or '16:9').lower(), '16:9'),
        }
        if reference_image:
            body['reference_image'] = reference_image
        resp = requests.post(url, headers=self.headers, json=body, timeout=30)
        if resp.status_code == 429:
            raise RateLimitError("Rate limit exceeded (429)")
        if resp.status_code != 200:
            raise RuntimeError(f"Flower generate failed ({resp.status_code}): {_clean_resp_text(resp.text)}")
        data = resp.json()
        op_id = data.get('operation_id') or data.get('id')
        if not op_id:
            raise RuntimeError(f"No operation_id in response: {data}")
        return op_id

    def generate_openai(
        self,
        prompt: str,
        aspect_ratio: Optional[str] = None,
        reference_uris: Optional[List[str]] = None,
    ) -> str:
        """OpenAI ChatGPT web image generation.
        Endpoint: POST /api/v4/openai/image/generate.
        Body: {prompt, aspect_ratio, reference_images?}
        aspect_ratio: '16:9' / '9:16' / '1:1' / 'landscape' / 'portrait' / 'square'
                      или 'W:H' произвольно (валидируется паттерном на сервере).
        reference_images: до 10 data URI или 'file:<hash>'.
        Returns op_id (poll через poll_operation как остальные движки)."""
        url = f"{self.base_url}/v4/openai/image/generate"
        # OpenAI принимает оба формата (alias 'landscape' и точный '16:9') — пропускаем как есть.
        # Если задан alias — нормализуем в формат 'W:H' для предсказуемости.
        _OPENAI_ASPECT = {'landscape': '16:9', 'portrait': '9:16', 'square': '1:1'}
        _ar = (aspect_ratio or '16:9').lower()
        body = {
            'prompt': prompt,
            'aspect_ratio': _OPENAI_ASPECT.get(_ar, _ar if _ar else '16:9'),
        }
        if reference_uris:
            # API принимает максимум 10 рефов (пер OpenAPI spec).
            body['reference_images'] = list(reference_uris)[:10]
        resp = requests.post(url, headers=self.headers, json=body, timeout=30)
        if resp.status_code == 429:
            raise RateLimitError("Rate limit exceeded (429)")
        if resp.status_code != 200:
            raise RuntimeError(f"OpenAI generate failed ({resp.status_code}): {_clean_resp_text(resp.text)}")
        data = resp.json()
        op_id = data.get('operation_id') or data.get('id')
        if not op_id:
            raise RuntimeError(f"No operation_id in response: {data}")
        return op_id

    # ------------------------------------------------------------------
    # Поллинг операций
    # ------------------------------------------------------------------

    def poll_operation(
        self,
        operation_id: str,
        timeout: Optional[int] = None,
        interval: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Поллить статус операции до завершения.

        Args:
            operation_id: ID операции
            timeout: Макс. время ожидания (секунды)
            interval: Интервал между проверками (секунды)

        Returns:
            Результат операции (содержит images или error)
        """
        timeout = timeout or config.FASTGEN_POLL_TIMEOUT
        interval = interval or config.FASTGEN_POLL_INTERVAL
        url = f"{self.base_url}/v4/operations/{operation_id}"
        deadline = time.time() + timeout

        while time.time() < deadline:
            # Hard Stop / shutdown — abort immediately so hung polls release
            if _flowultra_shutdown_event.is_set():
                raise InterruptedError(f"Operation {operation_id} aborted by shutdown signal")
            resp = requests.get(url, headers=self.headers, timeout=15)
            if resp.status_code != 200:
                raise RuntimeError(f"Poll failed ({resp.status_code}): {_clean_resp_text(resp.text)}")

            data = resp.json()
            # Docs: status enum = pending | processing | success | error (lowercase)
            status = data.get('status', '').lower()

            if status == 'success':
                return data
            elif status in ('error', 'failed', 'cancelled'):
                error_msg = data.get('error', '') or data.get('message', 'Unknown error')
                # Если сервер вернул HTML (Cloudflare/502/503) — заменяем мусор чистым сообщением
                if '<html' in error_msg.lower() or '<!doctype' in error_msg.lower():
                    short = error_msg[:200].replace('\n', ' ')
                    logging.warning(f"Operation {operation_id} server error (HTML response): {short}")
                    raise GenerationError(f"Operation {operation_id} failed: server returned HTML (Cloudflare/503)")
                logging.warning(f"Operation {operation_id} failed: {error_msg[:300]}")
                if is_reference_blocked_error(error_msg):
                    raise ReferenceBlockedError(error_msg)
                if is_content_policy_error(error_msg):
                    raise ContentPolicyError(error_msg)
                raise GenerationError(f"Operation {operation_id} failed: {error_msg}")

            time.sleep(interval)

        raise TimeoutError(f"Operation {operation_id} timed out after {timeout}s")

    # ------------------------------------------------------------------
    # Переписка промпта через FastGen Prompt API
    # ------------------------------------------------------------------

    def rewrite_prompt(self, original_prompt: str, project_dir=None, clip_id=None) -> str:
        """
        Rewrite a blocked prompt via the shared rewriter (REWRITE_MODEL).
        Instruction loaded from agent/prompts/rewrite_prompt.txt (user-editable).
        """
        from prompt_softener import rewrite_prompt as _gr
        return _gr(original_prompt, project_dir=project_dir, clip_id=clip_id)

    def rewrite_prompt_for_person(self, original_prompt: str, project_dir=None, clip_id=None) -> str:
        """
        Rewrite a blocked prompt with famous person name via the shared rewriter.
        Unified with rewrite_prompt — the instruction handles both cases.
        """
        from prompt_softener import rewrite_prompt as _gr
        return _gr(original_prompt, project_dir=project_dir, clip_id=clip_id)

    # ------------------------------------------------------------------
    # Grok remix (censorship bypass)
    # ------------------------------------------------------------------

    # Дефолтные промпты — используются если файл промпта удалён
    _DEFAULT_REF_REMIX = (
        "Remake this photo in graphic novel / comic book art style. "
        "Preserve the exact facial features, face structure and likeness precisely. "
        "Change only the art rendering style, not the face or identity."
    )
    _DEFAULT_I2V_REMIX = (
        "Same scene from a different camera angle. "
        "Use medium shot or wide shot. Avoid direct face close-up. "
        "Keep same lighting, mood and composition."
    )

    def _load_remix_prompt(self, filename: str, default: str) -> str:
        """Читает промпт из agent/prompts/{filename}. Fallback — default."""
        p = Path(__file__).parent.parent / 'instructions' / filename
        try:
            return p.read_text('utf-8').strip() if p.exists() else default
        except Exception:
            return default

    def _image_to_data_uri(self, image_path: str) -> str:
        """Читает локальный файл изображения и возвращает data URI."""
        p = Path(image_path)
        mime_map = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                    '.webp': 'image/webp', '.png': 'image/png'}
        mime = mime_map.get(p.suffix.lower(), 'image/png')
        data = base64.b64encode(p.read_bytes()).decode()
        return f"data:{mime};base64,{data}"

    def _v6_remix(self, image_path: str, output_path: str, prompt: str,
                  operation: str, poll_timeout: int = 180) -> str:
        """V6 img2img edit (grok_image_edit / flower_image_edit): reference-картинка
        позиционным inputs[0], prompt — инструкция трансформации. Полный цикл через
        единый /api/v6/generations. При 429 — ретрай по backup/2/3/4 ключам.

        poll_timeout: макс. время ожидания (default 180s; ref-remix передаёт 15s —
        если висит, это скорее censorship)."""
        data_uri = self._image_to_data_uri(image_path)
        # Список клиентов: текущий ключ → backup/2/3/4
        _clients = [self]
        for _attr in ('FASTGEN_API_KEY_BACKUP', 'FASTGEN_API_KEY_2',
                      'FASTGEN_API_KEY_3', 'FASTGEN_API_KEY_4'):
            _k = getattr(config, _attr, '')
            if _k and _k != self.api_key:
                _clients.append(FastGenClient(api_key=_k))
        name = Path(image_path).name
        _last_err = None
        for _ci, _client in enumerate(_clients):
            _key_label = f"key{_ci+1}" if _ci > 0 else "main"
            try:
                gen_id = _client._v6_submit(
                    operation=operation, prompt=prompt, positional_inputs=[data_uri])
                _res: list = [None]
                _exc: list = [None]
                def _poll(_c=_client, _gid=gen_id, _to=poll_timeout):
                    try:
                        _res[0] = _c._v6_poll(_gid, timeout=_to)
                    except Exception as e:
                        _exc[0] = e
                t = threading.Thread(target=_poll, daemon=True)
                t.start()
                with tqdm(total=poll_timeout, desc=f"  [REMIX {_key_label}] {name}", unit="s",
                          bar_format="{desc}: {bar}| {n:.0f}/{total}s", leave=False) as pbar:
                    while t.is_alive():
                        if _flowultra_shutdown_event.is_set():
                            raise InterruptedError(f"Remix {name} aborted by shutdown signal")
                        time.sleep(1)
                        pbar.update(1)
                t.join()
                if _exc[0] is not None:
                    raise _exc[0]
                response = _res[0] or {}
                if response.get('status') != 'succeeded':
                    err = response.get('error') or response.get('status') or 'unknown'
                    raise RuntimeError(f"V6 remix {operation} failed: {err}")
                _client._v6_download(response, output_path)
                return output_path
            except (RateLimitError, Exception) as e:
                _last_err = e
                if '429' in str(e) or 'Too many' in str(e) or isinstance(e, RateLimitError):
                    print(f"  {_YELLOW}[REMIX 429]{_RESET} {name}: rate limit on {_key_label}, trying next key...")
                    logging.warning(f"Remix 429 on {_key_label}: {e}")
                    continue
                raise  # non-429 error — don't fallback
        raise _last_err or RuntimeError(f"V6 remix {operation}: all keys returned 429")

    def remix_ref_grok(self, image_path: str, output_path: str) -> str:
        """Ремикс референса через Grok edit (V6 grok_image_edit). Промпт из ref_remix_prompt.txt.
        poll_timeout=15: если висит >15s на рефе — скрытый ref-block, быстрее переключаемся."""
        prompt = self._load_remix_prompt('ref_remix_prompt.txt', self._DEFAULT_REF_REMIX)
        print(f"  {_YELLOW}[GROK REMIX]{_RESET} {Path(image_path).name}")
        return self._v6_remix(image_path, output_path, prompt, 'grok_image_edit', poll_timeout=15)

    def remix_ref_flower(self, image_path: str, output_path: str) -> str:
        """Ремикс референса через Flower img2img (V6 flower_image_edit).
        Альтернатива Grok edit когда xAI Cloudflare блокирует edit."""
        prompt = self._load_remix_prompt('ref_remix_prompt.txt', self._DEFAULT_REF_REMIX)
        print(f"  {_YELLOW}[FLOWER REMIX]{_RESET} {Path(image_path).name}")
        return self._v6_remix(image_path, output_path, prompt, 'flower_image_edit', poll_timeout=120)

    def remix_ref_openai(self, image_path: str, output_path: str) -> str:
        """GPT Image remix using the V6 OpenAI image generation operation with an input image."""
        prompt = self._load_remix_prompt('ref_remix_prompt.txt', self._DEFAULT_REF_REMIX)
        print(f"  {_YELLOW}[GPT IMAGE REMIX]{_RESET} {Path(image_path).name}")
        return self._v6_remix(image_path, output_path, prompt, 'openai_image_generate', poll_timeout=180)

    def remix_i2v_flower(self, image_path: str, output_path: str, prompt: str) -> str:
        """Ремикс i2v-кадра через Flower img2img (V6 flower_image_edit)."""
        print(f"  {_YELLOW}[FLOWER I2V REMIX]{_RESET} {Path(image_path).name}")
        return self._v6_remix(image_path, output_path, prompt, 'flower_image_edit', poll_timeout=120)

    def remix_i2v_openai(self, image_path: str, output_path: str, prompt: str) -> str:
        """GPT Image remix of an i2v keyframe using the V6 OpenAI image operation."""
        print(f"  {_YELLOW}[GPT IMAGE I2V REMIX]{_RESET} {Path(image_path).name}")
        return self._v6_remix(image_path, output_path, prompt, 'openai_image_generate', poll_timeout=180)

    def _preflight_test_refs(self, ref_uris: List[str], desc: str = "  [PREFLIGHT] Testing refs",
                             prompt: Optional[str] = None,
                             model: Optional[str] = None,
                             ref_paths: Optional[List] = None,
                             real_prompts: Optional[List[str]] = None) -> bool:
        """
        Тестирует список ref_uris промптом используя тот же движок что и основная генерация.
        True = рефы приняты, False = ReferenceBlockedError.

        Если передан real_prompts — берём первый реальный промпт из FINAL.txt
        который содержит токены этих ref'ов (по stem'у файла). Это ловит content-trigger
        в реальной формулировке промпта, а не в синтетическом 'sitting on a chair'.
        Поиск токенов: regex \\[(stem)(\\.\\w+)?\\] — match с и без расширения (.jpg/.png).

        Fallback цепочка для prompt:
            1) явно задан → используем его
            2) real_prompts + ref_paths → ищем первый реальный промпт где упомянут хоть один ref
            3) ref_paths без real_prompts → auto-prompt из stem'ов
            4) ничего → 'abstract cinematic background, no people'
        """
        if not ref_uris:
            return True
        # Auto-prompt: приоритет реальному промпту из FINAL.txt
        if prompt is None:
            if real_prompts and ref_paths:
                _stems = [Path(p).stem for p in ref_paths[:len(ref_uris)]]
                # regex: [stem] или [stem.ext]
                _patterns = [re.compile(r'\[' + re.escape(s) + r'(?:\.\w+)?\]', re.IGNORECASE) for s in _stems]
                for _rp in real_prompts:
                    if any(p.search(_rp) for p in _patterns):
                        prompt = _rp
                        logging.info(f"PREFLIGHT: using real prompt from FINAL.txt (len={len(_rp)})")
                        # Стрипаем токены НЕ из subset'а (они "висят без binding" → ServerError
                        # → ложно обвиняем subset refs в block'е).
                        # Находим все токены в промпте, оставляем те что в subset stems.
                        _all_tokens_in_prompt = set(re.findall(r'\[(\w+_ref)(?:\.\w+)?\]', prompt, re.IGNORECASE))
                        _subset_stems_lower = {s.lower() for s in _stems}
                        _foreign = [t for t in _all_tokens_in_prompt if t.lower() not in _subset_stems_lower]
                        if _foreign:
                            for _ftok in _foreign:
                                prompt = re.sub(r'\[' + re.escape(_ftok) + r'(?:\.\w+)?\]',
                                                'a background person',
                                                prompt, flags=re.IGNORECASE)
                            logging.info(f"PREFLIGHT: stripped {len(_foreign)} foreign tokens from prompt: {_foreign}")
                        break
            if prompt is None:
                if ref_paths:
                    _tokens = [Path(p).stem for p in ref_paths[:len(ref_uris)]]
                    prompt = f"{', '.join(_tokens)} sitting on a chair, cinematic scene"
                else:
                    prompt = "abstract cinematic background, no people"
        self._last_preflight_skipped = False
        _effective_model = model or config.FASTGEN_MODEL
        engine = self._get_engine(_effective_model)

        # ── V6 path для preflight — ЕДИНСТВЕННЫЙ (V4/V5 удалены сервером → 404) ──
        # Покрывает flow / openai / grok / flower. whisk выпилен (нет в V6).
        if engine != 'whisk':
            if engine == 'flow':
                _op = FastGenClient._v6_model_to_operation(_effective_model)
            else:
                _op = FastGenClient._v6_operation_for_engine(engine, bool(ref_uris))
            if _op is not None:
                # Собрать reference_pairs из (stem, uri) — имена биндятся к токенам в auto-prompt'е.
                _pairs: List[Tuple[str, str]] = []
                if ref_paths and ref_uris and len(ref_paths) == len(ref_uris):
                    for p, u in zip(ref_paths, ref_uris):
                        if u:
                            # V6 adapter (особенно openai_image_generate) требует имя файла С РАСШИРЕНИЕМ.
                            # Токен в промпте по-прежнему [name_ref] без расширения — это OK,
                            # сервер делает substring matching name vs token base.
                            _pairs.append((Path(p).name, u))
                # Для flower preflight исторически берётся 1 ref (его endpoint не принимает >1).
                # В v6 flower_image_edit тоже только 1 input — обрезаем.
                if engine == 'flower' and len(_pairs) > 1:
                    _pairs = _pairs[:1]
                # edit-операции требуют refs; если у нас их нет — упасть на v4 fallback.
                if not (_op.endswith('_image_edit') and not _pairs):
                    try:
                        # tqdm wrapper вокруг blocking call — даёт визуальный прогресс
                        _result: list = [None]
                        _exc: list = [None]
                        _PREFLIGHT_POLL_TIMEOUT = 180
                        def _run_v6():
                            try:
                                _result[0] = self.generate_v6_image(
                                    prompt=prompt,
                                    reference_pairs=_pairs,
                                    operation=_op,
                                    model=_effective_model,
                                    seed=42,
                                    poll_timeout=_PREFLIGHT_POLL_TIMEOUT,
                                )
                            except Exception as e:
                                _exc[0] = e
                        t = threading.Thread(target=_run_v6, daemon=True)
                        t.start()
                        with tqdm(total=_PREFLIGHT_POLL_TIMEOUT, desc=desc, unit="s",
                                  bar_format="{desc}: {bar}| {n:.0f}/{total}s",
                                  leave=False) as pbar:
                            while t.is_alive():
                                if _flowultra_shutdown_event.is_set():
                                    raise InterruptedError("Preflight aborted by shutdown signal")
                                time.sleep(1)
                                pbar.update(1)
                        t.join()
                        if _exc[0] is not None:
                            raise _exc[0]
                        resp = _result[0] or {}
                        if resp.get('status') == 'succeeded':
                            return True
                        err = str(resp.get('error') or resp.get('status') or 'unknown v6 preflight failure')
                        # Печатаем сырую ошибку сервера ВСЕГДА — без этого preflight = чёрный ящик.
                        _ref_names = [n for (n, _u) in _pairs] if _pairs else []
                        logging.warning(f"PREFLIGHT v6 RAW_ERROR (refs={_ref_names}): {err[:500]}")
                        print(f"  {_YELLOW}[PREFLIGHT]{_RESET} v6 RAW_ERROR: {err[:300]}", flush=True)
                        err_low = err.lower()
                        # PROMPT violation markers — refs тут не виноваты, текст плохой.
                        # Preflight тестирует РЕФЫ, не тексты. Если текст плохой → per-clip worker
                        # сам отрерайтит при реальной генерации. Помечаем как SKIPPED → return True.
                        _prompt_violation_markers = (
                            'violence', 'violent', 'aggressive', 'confrontational', 'confrontation',
                            'guardrails', 'we created may violate', 'prompt may violate',
                            'may violate our content policy', 'may violate our content policies',
                            'content policy', 'content policies', 'retry or edit your prompt',
                            'cannot generate requested photorealistic image from text prompt',
                            'unsafe content', 'unsafe interpersonal',
                            'hateful', 'hate speech', 'gore', 'graphic content', 'graphic violence',
                            'disturbing', 'frightening',
                            'sexual', 'sexuality', 'erotic', 'provocative', 'lightly sexual',
                            'nudity', 'nude', 'nsfw',
                            'self-harm', 'self harm', 'suicide', 'csam', 'minor', 'underage',
                            'safety system', 'safety guidelines',
                        )
                        if any(m in err_low for m in _prompt_violation_markers):
                            self._last_preflight_skipped = True
                            logging.warning(f"PREFLIGHT v6: PROMPT violation (refs OK) — skipping preflight, per-clip rewrite will handle")
                            print(f"  {_YELLOW}[PREFLIGHT]{_RESET} PROMPT block (refs not at fault) — skip preflight, per-clip rewrite will handle")
                            return True
                        # Other censorship / policy → real ReferenceBlocked semantics
                        if any(kw in err_low for kw in ('policy violation',
                                                        'safety filter', 'blocked', 'censored', 'harmful',
                                                        'prominent', 'invalid argument')):
                            return False
                        # Generic v6 fail with refs → suspect ref-block
                        _generic = any(m in err_low for m in ('failed to generate', 'generation failed', 'operation failed'))
                        _transient = _is_transient_error(err_low)
                        if ref_uris and _generic and not _transient:
                            logging.warning(f"PREFLIGHT v6: generic fail with refs — treating as ref-block suspect")
                            print(f"  {_YELLOW}[PREFLIGHT]{_RESET} v6 generic fail — treating as ref-block (will isolate)")
                            return False
                        self._last_preflight_skipped = True
                        logging.warning(f"PREFLIGHT v6: skipped due to non-fatal failure")
                        print(f"  [PREFLIGHT] v6 SKIPPED")
                        return True
                    except ReferenceBlockedError:
                        return False
                    except (InterruptedError, KeyboardInterrupt):
                        raise
                    except TimeoutError as _te:
                        self._last_preflight_skipped = True
                        logging.warning(f"PREFLIGHT v6: timeout — SKIPPED (transient): {_te}")
                        print(f"  {_YELLOW}[PREFLIGHT]{_RESET} v6 TIMEOUT — SKIPPED (will retry)")
                        return True
                    except Exception as _ev6:
                        # Не классифицируется как блок — выкидываем в общий handler ниже
                        # (он различит transient / suspect-block по err_low).
                        self._last_preflight_skipped = True
                        logging.warning(f"PREFLIGHT v6: exception {type(_ev6).__name__}: {_ev6}")
                        print(f"  [PREFLIGHT] v6 SKIPPED ({type(_ev6).__name__}: {str(_ev6)[:200]})")
                        return True

        # ── Legacy v4 path (если v6 выкл / engine не покрыт / edit без refs) ──
        try:
            if engine == 'flower':
                # Flower endpoint (/v4/flower/image/generate) не принимает `model`
                # и берёт только один reference_image. Для preflight берём первый ref
                # из списка — этого достаточно чтобы Google классификатор среагировал
                # на celebrity-триггер, и валидация Flow API не упала на FLOWER в body.
                op_id = self.generate_flower(
                    prompt=prompt,
                    reference_image=(list(ref_uris)[0] if ref_uris else None),
                )
            elif engine == 'openai':
                # OpenAI endpoint принимает массив reference_images (до 10) — передаём весь.
                op_id = self.generate_openai(
                    prompt=prompt,
                    reference_uris=list(ref_uris) if ref_uris else None,
                )
            else:
                # Передаём model явно — иначе generate_flow берёт config.FASTGEN_MODEL,
                # который может не совпадать с тем что выбрано в GUI (→ 422 validation).
                op_id = self.generate_flow(
                    prompt,
                    reference_uris=list(ref_uris),
                    model=_effective_model,
                    seed=42,
                )
            # Показываем таймер пока poll_operation блокирует
            _result: list = [None]
            _exc:    list = [None]
            # Timeout 180s — Banana cold-start + очередь FastGen бывает до 90-120s легитимно.
            # Раньше было 15s → cold-start постоянно trigger'ил ложный "ref-block".
            _PREFLIGHT_POLL_TIMEOUT = 180
            def _poll():
                try:
                    _result[0] = self.poll_operation(op_id, timeout=_PREFLIGHT_POLL_TIMEOUT)
                except Exception as e:
                    _exc[0] = e
            t = threading.Thread(target=_poll, daemon=True)
            t.start()
            with tqdm(total=_PREFLIGHT_POLL_TIMEOUT, desc=desc, unit="s",
                      bar_format="{desc}: {bar}| {n:.0f}/{total}s",
                      leave=False) as pbar:
                while t.is_alive():
                    if _flowultra_shutdown_event.is_set():
                        raise InterruptedError("Preflight aborted by shutdown signal")
                    time.sleep(1)
                    pbar.update(1)
            t.join()
            if _exc[0] is not None:
                raise _exc[0]
            return True
        except ReferenceBlockedError:
            return False
        except (InterruptedError, KeyboardInterrupt):
            # Hard Stop — пробрасываем наверх, чтобы retry-циклы в _isolate_blocked_refs
            # и главном caller'е прервались, а не крутили SKIPPED→retry бесконечно.
            raise
        except TimeoutError as _te:
            # Timeout = SKIPPED (transient), НЕ BLOCKED. Cold-start Banana
            # легитимно > 60s. Caller (_isolate_blocked_refs или main) делает retry
            # через _last_preflight_skipped=True. Если после 2 retries всё ещё
            # timeout → там принимается pessimistic BLOCKED. Раньше TimeoutError
            # → return False мгновенно → безобидные рефы ложно помечались bad.
            self._last_preflight_skipped = True
            logging.warning(f"PREFLIGHT: timeout (>{_PREFLIGHT_POLL_TIMEOUT}s) — treating as SKIPPED (transient): {_te}")
            print(f"  {_YELLOW}[PREFLIGHT]{_RESET} TIMEOUT (>{_PREFLIGHT_POLL_TIMEOUT}s) — SKIPPED (transient, will retry)")
            return True
        except Exception as _e:
            _err_low = str(_e).lower()
            # PROMPT violation markers — текст промпта плохой, refs ни при чём.
            # Preflight тестирует РЕФЫ. Если сервер ругается на violence/sexuality/etc —
            # пропускаем (per-clip worker сам rewrite'нет промпт при реальной генерации).
            _prompt_violation_markers = (
                'violence', 'violent', 'aggressive', 'confrontational', 'confrontation',
                'guardrails', 'we created may violate', 'prompt may violate',
                'may violate our content policy', 'may violate our content policies',
                'content policy', 'content policies', 'retry or edit your prompt',
                'cannot generate requested photorealistic image from text prompt',
                'unsafe content', 'unsafe interpersonal',
                'hateful', 'hate speech', 'gore', 'graphic content', 'graphic violence',
                'disturbing', 'frightening',
                'sexual', 'sexuality', 'erotic', 'provocative', 'lightly sexual',
                'nudity', 'nude', 'nsfw',
                'self-harm', 'self harm', 'suicide', 'csam', 'minor', 'underage',
                'safety system', 'safety guidelines',
            )
            if any(m in _err_low for m in _prompt_violation_markers):
                self._last_preflight_skipped = True
                logging.warning(f"PREFLIGHT v4: PROMPT violation (refs OK) — skipping preflight")
                print(f"  {_YELLOW}[PREFLIGHT]{_RESET} PROMPT block (refs not at fault) — skip preflight, per-clip rewrite will handle")
                return True
            # Suspect ref-block: FastGen API иногда маскирует ref-block под generic
            # "Failed to generate" / "Generation failed" без указания причины.
            # Если рефы передавались и ошибка общая (не network/rate/auth) —
            # считаем потенциальным ref-block → caller запустит binary search.
            _generic_fail_markers = ('failed to generate', 'generation failed', 'operation failed')
            _is_generic_fail  = any(m in _err_low for m in _generic_fail_markers)
            _is_transient     = _is_transient_error(_err_low)
            if ref_uris and _is_generic_fail and not _is_transient:
                logging.warning(f"PREFLIGHT: generic '{type(_e).__name__}' with refs — treating as ref-block suspect")
                print(f"  {_YELLOW}[PREFLIGHT]{_RESET} generic fail with refs — treating as ref-block (will isolate)")
                return False  # caller (main preflight or isolate) запустит binary search
            # Не цензура рефов, но и не "OK" — preflight фактически не выполнен.
            # Выставляем флаг, чтобы caller не печатал ложное "OK — refs accepted".
            self._last_preflight_skipped = True
            logging.warning(f"PREFLIGHT: skipped due to exception ({type(_e).__name__}): {_e}")
            print(f"  [PREFLIGHT] SKIPPED ({type(_e).__name__}: {str(_e)[:200]})")
            return True  # пропускаем пайплайн дальше, рефы не забракованы

    def _isolate_blocked_refs(self, uris: List[str], prompt: Optional[str] = None,
                              _step: list = None, model: Optional[str] = None,
                              paths: Optional[List] = None,
                              real_prompts: Optional[List[str]] = None) -> List[int]:
        """
        Бинарный поиск заблокированных рефов.
        Возвращает список индексов плохих рефов в исходном списке.

        paths: список ref_paths (параллельный uris). Если передан — используется для
        auto-промпта в _preflight_test_refs (с токенами из имён файлов).

        real_prompts: реальные промпты из FINAL.txt. Если задан и prompt=None — каждый
        sub-test выберет промпт где упомянут хоть один ref из subset'а. Это воспроизводит
        реальную блокировку (а synthetic prompt этого не делает → bad refs=[]).
        """
        if _step is None:
            _step = [0]
        if not uris:
            return []
        # helper: читаемое имя subset'а (показываем .name с расширением —
        # ровно то что уходит на сервер как filename в v6 named input).
        def _subset_label(sub_paths, sub_uris):
            if sub_paths:
                _names = [Path(p).name for p in sub_paths]
                if len(_names) <= 3:
                    return ', '.join(_names)
                return f"{_names[0]}..{_names[-1]} (x{len(_names)})"
            return f"{len(sub_uris)} refs"

        # helper: test refs with retry on SKIPPED (для transient network errors / timeouts).
        # InterruptedError (Hard Stop) пробрасывается наверх через _preflight_test_refs —
        # retry-цикл прерывается, не крутится на SKIPPED после Stop.
        def _test_with_retry(sub_uris, sub_paths, sub_desc, sub_lbl, step_num, max_retries=2):
            # Check shutdown BEFORE first attempt (user pressed Stop before this step)
            if _flowultra_shutdown_event.is_set():
                raise InterruptedError(f"Isolate step {step_num} aborted by shutdown signal")
            _ok = self._preflight_test_refs(sub_uris, desc=sub_desc, prompt=prompt, model=model,
                                              ref_paths=sub_paths, real_prompts=real_prompts)
            _retries_done = 0
            while getattr(self, '_last_preflight_skipped', False) and _retries_done < max_retries:
                _retries_done += 1
                # Check shutdown BEFORE sleep+retry — иначе после Stop будет крутиться 10s+10s попусту
                if _flowultra_shutdown_event.is_set():
                    raise InterruptedError(f"Isolate step {step_num} aborted by shutdown signal (during retry)")
                print(f"  {_YELLOW}[ISOLATE Step {step_num}]{_RESET} SKIPPED — retry {_retries_done}/{max_retries} in 10s: {sub_lbl}", flush=True)
                # Interruptible sleep: 10 × 1s ticks, check shutdown each
                for _ in range(10):
                    if _flowultra_shutdown_event.is_set():
                        raise InterruptedError(f"Isolate step {step_num} aborted by shutdown signal (in retry wait)")
                    time.sleep(1)
                _ok = self._preflight_test_refs(sub_uris, desc=sub_desc, prompt=prompt, model=model,
                                                  ref_paths=sub_paths, real_prompts=real_prompts)
            if getattr(self, '_last_preflight_skipped', False):
                # после всех retries всё равно SKIPPED — пессимистично BLOCKED
                return False, 'SKIPPED→BLOCKED (all retries failed)'
            return _ok, ('BLOCKED' if not _ok else 'OK')
        if len(uris) == 1:
            _step[0] += 1
            _lbl = _subset_label(paths, uris)
            desc = f"  [ISOLATE] Step {_step[0]} — testing 1 ref ({_lbl})"
            print(f"  {_YELLOW}[ISOLATE Step {_step[0]}]{_RESET} testing 1 ref: {_lbl}", flush=True)
            _ok, _verdict = _test_with_retry(uris, paths, desc, _lbl, _step[0])
            print(f"  {_YELLOW}[ISOLATE Step {_step[0]}]{_RESET} → {_verdict}: {_lbl}", flush=True)
            return [0] if not _ok else []
        mid = len(uris) // 2
        left, right = uris[:mid], uris[mid:]
        left_paths  = paths[:mid] if paths else None
        right_paths = paths[mid:] if paths else None
        bad: List[int] = []
        if left:
            _step[0] += 1
            _lbl = _subset_label(left_paths, left)
            desc = f"  [ISOLATE] Step {_step[0]} — left 1-{mid} ({_lbl})"
            print(f"  {_YELLOW}[ISOLATE Step {_step[0]}]{_RESET} testing left {len(left)} ref(s): {_lbl}", flush=True)
            _ok, _verdict = _test_with_retry(left, left_paths, desc, _lbl, _step[0])
            print(f"  {_YELLOW}[ISOLATE Step {_step[0]}]{_RESET} → {_verdict}: {_lbl}", flush=True)
            if not _ok:
                bad.extend(self._isolate_blocked_refs(left, prompt=prompt, _step=_step, model=model,
                                                       paths=left_paths, real_prompts=real_prompts))
        if right:
            _step[0] += 1
            _lbl = _subset_label(right_paths, right)
            desc = f"  [ISOLATE] Step {_step[0]} — right {mid+1}-{len(uris)} ({_lbl})"
            print(f"  {_YELLOW}[ISOLATE Step {_step[0]}]{_RESET} testing right {len(right)} ref(s): {_lbl}", flush=True)
            _ok, _verdict = _test_with_retry(right, right_paths, desc, _lbl, _step[0])
            print(f"  {_YELLOW}[ISOLATE Step {_step[0]}]{_RESET} → {_verdict}: {_lbl}", flush=True)
            if not _ok:
                bad.extend(i + mid for i in self._isolate_blocked_refs(right, prompt=prompt, _step=_step,
                                                                          model=model, paths=right_paths,
                                                                          real_prompts=real_prompts))
        return bad

    # ------------------------------------------------------------------
    # Сохранение изображения
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Проверка квоты
    # ------------------------------------------------------------------

    def get_usage(self) -> Dict[str, Any]:
        """
        Получить информацию об использовании API (квоты, лимиты).

        Returns:
            Словарь с данными usage
        """
        # API v6: usage на /api/v6/usage (v3/v4/v2 пути удалены → 404). См. api-1.json.
        url = f"{self.base_url}/v6/usage"
        try:
            resp = requests.get(url, headers=self.headers, timeout=15)
        except requests.exceptions.RequestException as e:
            raise FastGenUsageError(f"Usage check request failed: {e}") from e
        if resp.status_code != 200:
            body = _clean_resp_text(resp.text)
            logging.warning(f"Usage check failed ({resp.status_code}): {body}")
            raise FastGenUsageError(f"Usage check failed ({resp.status_code}): {body}")
        return resp.json()

    def cancel_all_tasks(self) -> dict:
        """
        Отменить все активные операции (pending/processing) для этого API ключа.
        Pending-задачи получают рефанд токенов.
        Returns: {'total_found': N, 'total_cancelled': N, 'total_refunded': N}
        """
        # V6: GET /api/v6/operations/cancel-all → CancelAllResponse (total_found/cancelled/refunded)
        url = f"{self.base_url}/v6/operations/cancel-all"
        try:
            resp = requests.get(url, headers=self.headers, timeout=15)
            if resp.status_code == 200:
                return resp.json()
            logging.warning(f"cancel_all_tasks failed ({resp.status_code}): {_clean_resp_text(resp.text)}")
        except Exception as e:
            logging.warning(f"cancel_all_tasks error: {e}")
        return {}


def parse_usage_remaining(usage: Dict[str, Any]):
    """
    Вычисляет оставшиеся генерации из ответа /v2/usage.

    Структура ответа API:
      account_limits.img_gen_per_hour_limit  — лимит в час
      current_usage.hourly_usage.image_generation.current_usage — использовано

    Возвращает int или None если данные недоступны.
    """
    if not usage:
        return None
    limit = usage.get('account_limits', {}).get('img_gen_per_hour_limit')
    if limit is None:
        return None
    hourly = usage.get('current_usage', {}).get('hourly_usage', {})
    img_gen = hourly.get('image_generation') if hourly else None
    used = img_gen.get('current_usage', 0) if img_gen else 0
    return max(0, int(limit) - int(used))


def parse_usage_threads(usage: Dict[str, Any]):
    """Возвращает кол-во разрешённых потоков генерации или '?'."""
    if not usage:
        return '?'
    return usage.get('account_limits', {}).get('img_generation_threads_allowed', '?')


# ======================================================================
# Квота исчерпана: живые ключи VeoNonStop + кулдаун до обновления квоты
# ======================================================================

def live_veononstop_slots() -> List[Dict[str, Any]]:
    """Слоты VeoNonStop с реальной квотой картинок (живой ключ, не «выбранный»).

    Тонкая обёртка над общей проверкой: тот же вызов /account/usage использует
    imggen.py, когда его собственный ключ выжигает дневную квоту.
    """
    from units import veo_accounts as _vau
    return _vau.live_image_slots(config)


def seconds_until_next_hour(offset_minutes: int = 1) -> float:
    """Секунды до HH:offset_minutes следующего часа.

    Часовая квота FastGen обновляется на границе часа, поэтому просыпаемся
    чуть позже неё (00:01 по умолчанию), а не ровно в 00:00.
    """
    now = datetime.now()
    next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    target = next_hour + timedelta(minutes=max(0, offset_minutes))
    return max(1.0, (target - now).total_seconds())


# ======================================================================
# Кастомные исключения
# ======================================================================

class RateLimitError(Exception):
    """429 — превышен лимит запросов"""
    pass


class QuotaExhaustedError(Exception):
    """Часовая квота полностью исчерпана (img_gen_per_hour_remaining=0)"""
    def __init__(self, message: str, key_label: Optional[str] = None):
        super().__init__(message)
        self.key_label = key_label


class FastGenUsageError(Exception):
    """Account usage endpoint is unavailable or returned a non-200 response."""
    pass


class ContentPolicyError(Exception):
    """Генерация отклонена из-за нарушения content policy"""
    pass


class ReferenceBlockedError(Exception):
    """Генерация отклонена из-за референсных изображений (лица известных людей и т.п.)"""
    pass


class GenerationError(Exception):
    """Общая ошибка генерации"""
    pass


def is_content_policy_error(error_msg: str) -> bool:
    """Определить, является ли ошибка нарушением content policy"""
    msg_lower = error_msg.lower()
    return any(phrase in msg_lower for phrase in [
        'content policy',
        'content policies',
        'policy violation',
        'safety filter',
        'blocked by safety',
        'harmful content',
        'unsafe content',
        'violates',
        'may violate',
        'guardrails',
        'not allowed',
        'prohibited',
        'moderation',
    ])


def is_reference_blocked_error(error_msg: str) -> bool:
    """Определить, является ли ошибка блокировкой из-за референсов"""
    msg_lower = error_msg.lower()
    return any(phrase in msg_lower for phrase in [
        'prominent people',
        'well-known individuals',
        'known individuals',
        'celebrity',
        'public figure',
        'image contains protected content',
        'protected content',
        'trademark',
        'copyright',
        'copyrighted',
        'commercial design',
        'brand logo',
        'logo',
    ])


def _sanitize_openai_image_prompt(prompt: str) -> str:
    """Make OpenAI image prompts less likely to trip policy on safety wording.

    OpenAI/FastGen appears to scan the whole text, including "Negative prompt:".
    So terms like "nudity" or "explicit gore" can still trigger even when they
    are written as things to avoid.
    """
    if not prompt:
        return prompt

    def _clean_negative(match: re.Match) -> str:
        prefix, body = match.group(1), match.group(2)
        drop_terms = (
            'nudity', 'nude', 'nsfw', 'sexual', 'erotic', 'provocative',
            'revealing armor', 'pin-up', 'skintight', 'latex',
            'explicit gore', 'gore', 'blood spectacle', 'blood',
            'graphic torture', 'torture', 'graphic content', 'graphic violence',
            'violence', 'violent', 'aggressive', 'weapon', 'weapons',
            'minor', 'underage', 'children in graphic peril',
            # death/horror лексика — OpenAI violence guardrail (грубо матчит и в негативе)
            'corpse', 'grotesque', 'horror', 'decay', 'rotting', 'dead body',
            'mutilation', 'dismember', 'wound', 'injury', 'massacre',
        )
        kept = []
        for item in re.split(r'\s*,\s*', body):
            item = item.strip()
            if not item:
                continue
            low = item.lower()
            if any(term in low for term in drop_terms):
                continue
            kept.append(item)
        return prefix + ', '.join(kept)

    cleaned = re.sub(r'(?is)(Negative prompt:\s*)(.*)$', _clean_negative, prompt)

    replacements = (
        (r'\bsinking slaver hold\b', 'sinking cargo hold'),
        (r'\bslaver cage\b', 'iron holding bay'),
        (r'\bslaver barges\b', 'criminal cargo barges'),
        (r'\bslaver barge\b', 'criminal cargo barge'),
        (r'\bslaver deck\b', 'wet deck'),
        (r'\bslaver lantern\b', 'deck lantern'),
        (r'\bslavers\b', 'criminal handlers'),
        (r'\bslaver\b', 'criminal handler'),
        (r'\bcaptives\b', 'survivors'),
        (r'\bcaptive\b', 'survivor'),
        (r'\bcage bars\b', 'iron bars'),
        (r'\bcage\b', 'iron enclosure'),
        (r'\bchildren\b', 'shorter survivors'),
        (r'\bchild\b', 'shadowed survivor'),
        (r'\byoung apprentice\b', 'apprentice'),
        (r'\byoung Heron-Reed girl\b', 'Heron-Reed apprentice'),
        (r'\bgirl with the bone fish\b', 'apprentice with the bone fish'),
        (r'\bthe girl\b', 'the apprentice'),
        (r'\brope-burned\b', 'rope-marked'),
        (r'\brope burns\b', 'rope marks'),
        (r'\brope-burned wrist\b', 'rope-marked wrist'),
        (r'\bweeping sores\b', 'irritated marks'),
        (r'\braw wrist\b', 'tender wrist'),
        (r'\bruined left eye\b', 'clouded left eye'),
        (r'\bwater at waist height\b', 'dark water around the group'),
        (r'\bwater reaching captive chests\b', 'dark water high around the group'),
        (r'\bwater reaches chests\b', 'water climbs higher'),
        (r'\bdrowning lantern\b', 'failing lantern'),
        (r'\bexplicit gore\b', 'graphic excess'),
        (r'\bblood spectacle\b', 'graphic excess'),
        (r'\bgraphic torture\b', 'graphic harm'),
    )
    for pattern, replacement in replacements:
        cleaned = re.sub(pattern, replacement, cleaned, flags=re.IGNORECASE)

    return cleaned


# ======================================================================
# Сканирование референсов
# ======================================================================

def scan_references(project_dir: str) -> Dict[str, List[Path]]:
    """
    Сканировать папку ref/ в проекте на наличие изображений.

    Структура:
        ref/            ← корневые файлы (для Flow)
        ref/subject/    ← персонажи (для Whisk)
        ref/scene/      ← локации (для Whisk)
        ref/style/      ← стиль (для Whisk)

    Returns:
        {
            'root': [Path, ...],
            'subject': [Path, ...],
            'scene': [Path, ...],
            'style': [Path, ...],
        }
    """
    ref_dir = Path(project_dir) / 'ref'
    result = {'root': [], 'subject': [], 'scene': [], 'style': []}

    # BegoFrame: референсы выключены — старый ref/ проекта не подхватываем
    if not ref_dir.exists() or os.environ.get('BEGO_REFS_DISABLED', '') == '1':
        return result

    # Сжать тяжёлые рефы ДО использования: >2 МБ валит upload по таймауту
    # (write timeout на медленной сети — рефы не грузятся, генерация идёт без них).
    # quality 100 (макс), лимит 2 МБ. Идемпотентно: <2 МБ пропускаются. Все модели.
    try:
        from units.ref_shrink import compress_refs
        compress_refs(str(ref_dir), max_mb=2.0)
        for _sub in ('subject', 'scene', 'style'):
            _sd = ref_dir / _sub
            if _sd.exists():
                compress_refs(str(_sd), max_mb=2.0)
    except Exception as _e:
        logging.warning(f"ref_compress skipped in scan_references: {_e}")

    # Корневые файлы (не в подпапках)
    for f in ref_dir.iterdir():
        if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS:
            result['root'].append(f)

    # Подпапки
    for category in ('subject', 'scene', 'style'):
        cat_dir = ref_dir / category
        if cat_dir.exists() and cat_dir.is_dir():
            for f in cat_dir.iterdir():
                if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS:
                    result[category].append(f)

    # Сортировка по имени
    for key in result:
        result[key].sort(key=lambda p: p.name.lower())

    return result


def detect_reference_mode(refs: Dict[str, List[Path]]) -> Optional[str]:
    """
    Автоматически определить режим референсов.

    Returns:
        'flow' — если файлы только в корне
        'whisk' — если файлы только в подпапках
        'ask' — если файлы и там и там (нужен выбор)
        None — если нет файлов
    """
    has_root = len(refs['root']) > 0
    has_categories = any(len(refs[cat]) > 0 for cat in ('subject', 'scene', 'style'))

    if not has_root and not has_categories:
        return None
    if has_root and not has_categories:
        return 'flow'
    if has_categories and not has_root:
        return 'whisk'
    return 'ask'


def print_reference_summary(refs: Dict[str, List[Path]]):
    """Вывести информацию о найденных референсах"""
    print("\n" + "=" * 60)
    print("  REFERENCES")
    print("=" * 60)

    for category, label in [('root', 'ref/ (root)'), ('subject', 'ref/subject/'),
                             ('scene', 'ref/scene/'), ('style', 'ref/style/')]:
        files = refs[category]
        if files:
            names = ', '.join(f.name for f in files[:5])
            if len(files) > 5:
                names += f", ... (+{len(files) - 5})"
            print(f"  {label:<18} {len(files)} file(s)  ({names})")
        else:
            print(f"  {label:<18} 0 files")


def ask_reference_mode() -> str:
    """Спросить у пользователя режим референсов"""
    print("\n  Available modes:")
    print("    [1] Flow API  - all files as general references (up to 10)")
    print("    [2] Whisk Remix - categorized: subject, scene, style (up to 3 total)")
    print()

    while True:
        choice = input("  Select mode [1/2]: ").strip()
        if choice == '1':
            return 'flow'
        elif choice == '2':
            return 'whisk'
        print("  Invalid choice, enter 1 or 2")


# ======================================================================
# Selective per-prompt reference filtering
# ======================================================================
# Pipeline uploads all *_ref.png from the project folder once at start.
# Without filtering, all uploaded refs are passed to every prompt — which
# causes facial bleed between visually similar refs (e.g. young Sael and
# old Lyra of the same kindred). This filter keeps only refs whose token
# (file stem) is mentioned in the prompt body for THIS specific clip.

import re as _re_tokens
_REF_TOKEN_RE = _re_tokens.compile(
    r'\[([a-zA-Z0-9_]+_ref(?:\.(?:png|jpg|jpeg|webp))?)\]',
    _re_tokens.IGNORECASE,
)


def _filter_refs_by_prompt_tokens(
    prompt: str,
    ref_uris: Optional[List[str]],
    ref_paths: Optional[List[Path]],
) -> Optional[List[str]]:
    """Return only the ref URIs whose file stem (token) appears in the prompt body.

    The pipeline uploads all reference images from the project folder upfront and
    reuses them across all prompts. But Banana / FastGen ref-mode binds every
    passed reference image to the generated frame, so passing irrelevant refs
    (e.g. an elderly aunt's reference when the scene is about her young niece
    alone with a third character) causes facial-feature bleed between similar
    references.

    This helper extracts [name_ref] tokens from the prompt body and returns only
    the URIs whose corresponding ref file stem matches an in-prompt token. If the
    prompt contains no character tokens, returns an empty list (the frame is an
    object-only macro — no character refs needed).

    Falls back to passing the original ref_uris unchanged if filtering cannot be
    performed safely (no ref_paths, or list length mismatch).
    """
    if not ref_uris:
        return ref_uris
    if not ref_paths or len(ref_paths) != len(ref_uris):
        return ref_uris  # cannot filter without parallel paths — safe fallback

    tokens_in_prompt = {token.lower() for token in _REF_TOKEN_RE.findall(prompt)}
    if not tokens_in_prompt:
        return []  # no tokens in prompt = no character refs for this frame

    filtered: List[str] = []
    for path, uri in zip(ref_paths, ref_uris):
        if path.stem.lower() in tokens_in_prompt or path.name.lower() in tokens_in_prompt:
            filtered.append(uri)
    return filtered


def _filter_ref_pairs_by_prompt_tokens(
    prompt: str,
    ref_uris: Optional[List[str]],
    ref_paths: Optional[List[Path]],
) -> Tuple[Optional[List[str]], Optional[List[Path]]]:
    """Filter refs and paths together so V6 named-input binding stays aligned."""
    if not ref_uris:
        return ref_uris, ref_paths
    if not ref_paths or len(ref_paths) != len(ref_uris):
        return ref_uris, ref_paths

    tokens_in_prompt = {token.lower() for token in _REF_TOKEN_RE.findall(prompt)}
    if not tokens_in_prompt:
        return [], []

    filtered_uris: List[str] = []
    filtered_paths: List[Path] = []
    for path, uri in zip(ref_paths, ref_uris):
        if path.stem.lower() in tokens_in_prompt or path.name.lower() in tokens_in_prompt:
            filtered_uris.append(uri)
            filtered_paths.append(path)
    return filtered_uris, filtered_paths


# ======================================================================
# Генерация одного клипа с retry + rewrite
# ======================================================================

def _run_generate_attempt(
    client: FastGenClient,
    clip_id: int,
    prompt: str,
    ref_uris: Optional[List[str]],
    mode: str,
    whisk_refs: Optional[Dict[str, List[str]]],
    output_dir: Path,
    model: str,
    aspect_ratio: str,
    seed: Optional[int],
    ref_paths: Optional[List[Path]] = None,
) -> Dict[str, Any]:
    """
    Один полный цикл: запуск генерации + поллинг + сохранение.
    Возвращает {'ok': True, 'file': str, 'elapsed': float}
    или бросает исключение.
    """
    import random as _random
    effective_seed = seed if seed is not None else _random.randint(1, 2**31)
    engine = FastGenClient._get_engine(model)

    # Selective ref filtering: pass only refs whose token appears in the prompt.
    # If ref_paths is None (legacy callers), behaviour is unchanged.
    if ref_paths is not None and mode != 'whisk':
        _orig_count = len(ref_uris) if ref_uris else 0
        ref_uris, ref_paths = _filter_ref_pairs_by_prompt_tokens(prompt, ref_uris, ref_paths)
        _new_count = len(ref_uris) if ref_uris else 0
        if _orig_count != _new_count:
            logging.info(f"Clip #{clip_id}: ref-filter narrowed {_orig_count}→{_new_count} refs by prompt tokens")

    # ── V6 — ЕДИНСТВЕННЫЙ путь генерации. V4/V5 API удалены сервером (404). ──
    # named-refs персонажей + operation-based через единый /api/v6/generations.
    _v6_result = _try_v6_path(
        client=client, engine=engine, model=model, prompt=prompt,
        ref_paths=ref_paths, ref_uris=ref_uris, aspect_ratio=aspect_ratio,
        output_dir=output_dir, clip_id=clip_id, effective_seed=effective_seed,
        mode=mode, whisk_refs=whisk_refs,
    )
    if _v6_result is not None:
        return _v6_result
    raise GenerationError(
        f"Clip #{clip_id}: V6 не смог определить operation "
        f"(engine={engine!r}, model={model!r}). Legacy V4/V5 API удалены сервером."
    )


def _try_v6_path(
    client, engine, model, prompt,
    ref_paths, ref_uris, aspect_ratio,
    output_dir, clip_id, effective_seed,
    mode=None, whisk_refs=None,
):
    """Генерация через единый /api/v6/generations. Возвращает result dict
    {ok, file, operation_id} при успехе ИЛИ None если operation не определился.
    V6 — единственный путь (V4/V5 удалены сервером → 404)."""
    # Выбираем v6 operation_id по engine/model (flow → nano_banana; flower/grok/openai → свои).
    if engine == 'flow':
        op = FastGenClient._v6_model_to_operation(model) or 'nano_banana_2_image_generate'
    else:
        op = FastGenClient._v6_operation_for_engine(engine, bool(ref_uris))
    if not op:
        return None

    # Current FastGen V6 docs support named inputs for OpenAI image generation.
    # Keep OpenAI+refs on V6 so prompt tokens like [reska_ref.jpg] can bind to
    # inputs[].filename instead of falling back to legacy V4 loose references.

    # Готовим reference_pairs (name из stem файла). Если paths/uris не парные —
    # сами refs валидны но без имени binding'а не будет (модель сматчит эвристически).
    reference_pairs: List[Tuple[str, str]] = []
    if ref_paths and ref_uris and len(ref_paths) == len(ref_uris):
        for p, u in zip(ref_paths, ref_uris):
            if u:
                # name с расширением — требование v6 adapter'а (особенно openai).
                reference_pairs.append((Path(p).name, u))

    # Edit-операции (grok_image_edit / flower_image_edit) требуют refs обязательно.
    if op.endswith('_image_edit') and not reference_pairs:
        return None

    if reference_pairs:
        logging.info(
            "Clip #%s: V6 %s refs=%s filenames=%s",
            clip_id,
            op,
            len(reference_pairs),
            ", ".join(name for name, _ in reference_pairs),
        )
    else:
        logging.info("Clip #%s: V6 %s refs=0", clip_id, op)

    output_path = output_dir / f"{clip_id:03d}.png"
    response = client.generate_v6_image(
        prompt=prompt,
        reference_pairs=reference_pairs,
        operation=op,
        model=model,
        aspect_ratio=aspect_ratio,
        output_path=str(output_path),
        seed=effective_seed,
    )
    if response.get('status') == 'succeeded':
        return {'ok': True, 'file': str(output_path),
                'operation_id': response.get('id')}
    # Не succeeded — превращаем в GenerationError; верхний слой через err_lower
    # классифицирует censored / prominent / network / etc.
    err = response.get('error') or response.get('status') or 'unknown v6 failure'
    raise GenerationError(f"V6 generation failed ({op}): {err}")


def _attempt_phase(
    client: FastGenClient,
    clip_id: int,
    prompt: str,
    ref_uris, mode, whisk_refs, output_dir,
    model: str, aspect_ratio: str,
    max_attempts: int,
    phase_label: str,
    stop_event=None,
    ref_paths: Optional[List[Path]] = None,
) -> Dict[str, Any]:
    """
    Фаза: max_attempts попыток с новым случайным сидом каждый раз.
    Возвращает {'ok': True, ...} или {'ok': False, 'last_error': str, 'censored': bool}.
    stop_event: threading.Event — если установлен, прерывает попытки досрочно (рефреш рефов).
    """
    import random as _random
    censored = False
    prominent_people = False
    ref_blocked = False
    last_error = ''

    for attempt in range(1, max_attempts + 1):
        # Проверяем стоп-сигнал (досрочный рефреш рефов)
        if stop_event is not None and stop_event.is_set():
            last_error = 'interrupted: ref TTL refresh requested'
            break
        # Hard Stop — мгновенно выходим из цикла попыток
        if _flowultra_shutdown_event.is_set():
            last_error = 'interrupted: hard stop'
            break
        seed = _random.randint(1, 2**31)
        try:
            start = time.time()
            res = _run_generate_attempt(
                client, clip_id, prompt, ref_uris, mode, whisk_refs,
                output_dir, model, aspect_ratio, seed,
                ref_paths=ref_paths,
            )
            elapsed = round(time.time() - start, 1)
            res['elapsed'] = elapsed
            return res

        except ReferenceBlockedError as e:
            last_error = str(e)
            has_refs = bool(ref_uris or whisk_refs)
            if has_refs:
                ref_blocked = True
                logging.error(f"Clip #{clip_id} [{phase_label}]: REF BLOCKED — reference images contain a prominent person")
                print(f"\n  {_RED}[REF BLOCKED]{_RESET} #{clip_id}: ⚠  Reference image contains a prominent/famous person!")
                print(f"               Fix: remove or replace the reference images for this clip.")
                print(f"               Prompt rewrite cannot help — problem is in the ref image, not the text.")
            else:
                prominent_people = True
                logging.warning(f"Clip #{clip_id} [{phase_label}]: PROMINENT PERSON IN PROMPT — will rewrite with appearance description")
                print(f"\n  {_RED}[PERSON NAME]{_RESET} #{clip_id}: prompt mentions a famous person by name — will rewrite using appearance description")
            break

        except ContentPolicyError as e:
            censored = True
            last_error = str(e)
            logging.warning(f"Clip #{clip_id} [{phase_label}] attempt {attempt}: CENSORED — {e}")
            print(f"\n  {_RED}[CENSORED]{_RESET} #{clip_id} [{phase_label}] attempt {attempt}/{max_attempts}")
            # При цензуре нет смысла ретраить с новым сидом — сразу выходим из фазы
            break

        except RateLimitError:
            # Проверяем: это обычный rate limit или часовая квота исчерпана?
            try:
                usage = client.get_usage()
                remaining = parse_usage_remaining(usage)
                if remaining is not None and remaining == 0:
                    raise QuotaExhaustedError(
                        "Hourly quota exhausted (remaining=0)",
                        key_label=getattr(client, 'key_label', None),
                    )
            except QuotaExhaustedError:
                raise  # пробрасываем наверх
            except Exception:
                pass  # не смогли проверить — продолжаем стандартный ретрай
            _rl_wait = int(5 + random.uniform(1, 5))
            msg = f"#{clip_id} [{phase_label}] attempt {attempt}: rate limit, waiting {_rl_wait}s..."
            logging.warning(f"Clip {msg}")
            print(f"\n  {_YELLOW}[RATELIMIT]{_RESET} {msg}")
            with tqdm(total=_rl_wait, desc=f"  [RATELIMIT] #{clip_id}", unit="s",
                      bar_format="{desc}: {bar}| {n:.0f}/{total}s", leave=False) as _pbar:
                for _ in range(_rl_wait):
                    time.sleep(1)
                    _pbar.update(1)
            # Не считаем за попытку

        except TimeoutError as e:
            last_error = str(e)
            logging.warning(f"Clip #{clip_id} [{phase_label}] attempt {attempt}/{max_attempts}: timeout")
            print(f"\n  {_YELLOW}[TIMEOUT]{_RESET}   #{clip_id} [{phase_label}] {attempt}/{max_attempts}, seed={seed}")
            if attempt < max_attempts:
                delay = min(5 * attempt, 30)
                print(f"  {_YELLOW}[RETRY]{_RESET}     new seed, waiting {delay}s...")
                time.sleep(delay)

        except InterruptedError as e:
            # Hard Stop / shutdown — не ретраим, выходим немедленно
            last_error = str(e)
            logging.info(f"Clip #{clip_id} [{phase_label}]: hard stop — no retry")
            break

        except Exception as e:
            last_error = str(e)
            _err_low = str(e).lower()
            # Fatal auth errors — no point retrying
            _fatal_kw = ['license_expired', 'license expired', 'auth.license_expired',
                         'credits insufficient', 'account suspended']
            if any(kw in _err_low for kw in _fatal_kw):
                logging.error(f"Clip #{clip_id} [{phase_label}]: FATAL auth error — {e}")
                print(f"\n  {_RED}[AUTH FATAL]{_RESET} #{clip_id}: {e}")
                print(f"  {_RED}[AUTH FATAL]{_RESET} Skipping all retries — key/license is dead")
                break
            # Ёмкость апстрима под эту модель кончилась (нет свободных аккаунтов,
            # пул перегружен). Ретрай тем же путём = тот же ответ, только с паузой:
            # выходим из фазы с ПЕРВОЙ такой ошибки, наверху клип уйдёт на Banana Pro.
            if _is_capacity_error(_err_low):
                logging.warning(
                    f"Clip #{clip_id} [{phase_label}] attempt {attempt}: UPSTREAM CAPACITY "
                    f"({model}) — leaving phase for model switch: {last_error[:200]}")
                print(f"\n  {_YELLOW}[NO CAPACITY]{_RESET} #{clip_id} [{phase_label}] "
                      f"attempt {attempt}: {last_error[:160]}")
                print(f"  {_YELLOW}[NO CAPACITY]{_RESET} #{clip_id}: skipping remaining "
                      f"attempts on {model} — switching model now")
                break
            # EXPLICIT prompt-violation markers — сервер v6 пишет в чистом виде что заблокировано.
            # ЭТО НЕ ref-block, это PROMPT-block → отправляем на rewrite (censored=True),
            # а не на ref-remix (binary search впустую крутит refs).
            _prompt_violation_markers = (
                'violence', 'violent',                                    # "interpersonal violence content"
                'aggressive', 'confrontational', 'confrontation',         # "aggressive or confrontational behavior"
                'guardrails',                                             # "guardrails around violence"
                'we created may violate',                                 # OpenAI post-gen safety wording
                'prompt may violate',
                'may violate our content policy',
                'may violate our content policies',
                'content policies',
                'retry or edit your prompt',
                'cannot generate requested photorealistic image from text prompt',
                'unsafe content', 'unsafe interpersonal',                 # generic safety verdict
                'hateful', 'hate speech',
                'gore', 'graphic content', 'graphic violence',
                'disturbing', 'frightening',
                'sexual', 'sexuality', 'erotic', 'nudity', 'nude', 'nsfw',
                'self-harm', 'self harm', 'suicide',
                'csam', 'minor', 'underage',
                'policy violation', 'content policy', 'safety filter',
                'safety system', 'safety guidelines',
            )
            if any(m in _err_low for m in _prompt_violation_markers):
                censored = True
                logging.warning(f"Clip #{clip_id} [{phase_label}]: PROMPT BLOCKED (not ref) — RAW_ERROR: {last_error[:500]}")
                print(f"\n  {_RED}[PROMPT BLOCKED]{_RESET} #{clip_id}: {last_error[:200]}")
                print(f"  {_RED}[PROMPT BLOCKED]{_RESET} → will rewrite prompt (NOT remix refs)")
                break
            # Suspect ref-block: FastGen маскирует ref-block под generic "Failed to generate".
            # Если рефы переданы и ошибка общая (не network/auth/rate) — триггерим ref_blocked.
            _generic_fail_markers = ('failed to generate', 'generation failed', 'operation failed')
            _has_refs = bool(ref_uris or whisk_refs)
            if _has_refs and any(m in _err_low for m in _generic_fail_markers) \
                    and not _is_transient_error(_err_low):
                ref_blocked = True
                # Логируем RAW текст сервера — без этого нельзя отличить реальный
                # ref-block от content-policy на промпт (оба идут как generic fail).
                logging.warning(f"Clip #{clip_id} [{phase_label}]: generic fail with refs — RAW_ERROR: {last_error[:500]}")
                print(f"\n  {_YELLOW}[SUSPECT REF-BLOCK]{_RESET} #{clip_id}: {last_error[:200]}")
                break
            logging.warning(f"Clip #{clip_id} [{phase_label}] attempt {attempt}/{max_attempts}: {e}")
            print(f"\n  {_YELLOW}[ERROR]{_RESET}     #{clip_id} [{phase_label}] {attempt}/{max_attempts}, seed={seed}: {e}")
            if attempt < max_attempts:
                delay = min(5 * attempt, 30)
                print(f"  {_YELLOW}[RETRY]{_RESET}     new seed, waiting {delay}s...")
                time.sleep(delay)

    _fatal_kw = ['license_expired', 'license expired', 'auth.license_expired',
                 'credits insufficient', 'account suspended']
    _is_dead = any(kw in last_error.lower() for kw in _fatal_kw) if last_error else False
    return {
        'ok': False,
        'last_error': last_error,
        'censored': censored,
        'prominent_people': prominent_people,
        'ref_blocked': ref_blocked,
        'license_dead': _is_dead,
    }


def generate_single_clip(
    client,   # FastGenClient or None (None when clients_cfg_list provided)
    clip_id: int,
    prompt: str,
    ref_uris: Optional[List[str]],
    mode: str,
    whisk_refs: Optional[Dict[str, List[str]]],
    output_dir: Path,
    model: Optional[str] = None,
    aspect_ratio: Optional[str] = None,
    stop_event=None,
    stagger_delay: float = 0.0,
    worker_id_fn=None,       # если задан — трекаем worker_id для dual-key
    clients_cfg_list=None,   # если задан — worker-assignment вместо round-robin
    on_ref_blocked=None,     # callback(): ремикс рефов через Grok, обновляет cfg в-place
    cfg_ref=None,            # dict cfg текущего ключа (для re-read ref_uris после ремикса)
) -> Dict[str, Any]:
    """
    Генерация одного изображения.

    Логика:
      Фаза 1: 10 попыток с новым сидом каждый раз.
      Если все 10 провалились → rewrite промпта через Prompt API.
      Фаза 2: ещё 10 попыток с переписанным промптом (новый сид каждый раз).
      Если снова провал → возвращает статус 'error'/'censored' с деталями.

    stop_event: threading.Event — при установке прерывает попытки между итерациями.

    Returns:
        {'id': int, 'status': 'ok'|'error'|'censored', 'file': str,
         'rewritten': bool, 'phase': int, 'error': str, ...}
    """
    # Дефолты для single-key path (если if ниже не выполнится — переменные остаются от параметров).
    # ref_paths не приходит параметром, поэтому дефолтим до if иначе UnboundLocalError ниже на _attempt_phase.
    ref_paths: List = []

    # ── Worker-assignment: определяем client/refs по worker_id (если dual-key) ──
    if clients_cfg_list and worker_id_fn:
        worker_id = worker_id_fn()
        cumulative = 0
        _selected = clients_cfg_list[-1]
        _group_start = 0
        for _cfg in clients_cfg_list:
            if worker_id < cumulative + _cfg['mp']:
                _selected = _cfg
                _group_start = cumulative
                break
            cumulative += _cfg['mp']
        client     = _selected['client']
        ref_uris   = _selected.get('ref_uris') or []
        ref_paths  = _selected.get('ref_paths_flow') or []  # parallel to ref_uris, for selective filter
        whisk_refs = _selected.get('whisk_refs') or {}
        cfg_ref    = _selected   # для re-read после remix
        _local_id  = worker_id - _group_start
        # Стаггер: первый запуск воркера — разброс; повторный — 1s + jitter
        with _fg_started_workers_lock:
            is_first = worker_id not in _fg_started_workers
            _fg_started_workers.add(worker_id)
        stagger_delay = (_local_id * 2.0 + random.uniform(1, 5)) if is_first else (1.0 + random.uniform(0.5, 1.5))

    if stagger_delay > 0:
        time.sleep(stagger_delay)

    # Берём общий конфиг ретраев (та же ручка GUI что и для VeoNonStop).
    # Раньше было хардкод 10 → клипы могли молотить часами при high-demand OpenAI.
    MAX_ATTEMPTS = getattr(config, 'VEONONSTOP_MAX_RETRIES', 5)
    effective_model = model or config.FASTGEN_MODEL
    # Banana Pro fallback — per-clip; не лезет в OpenAI, поэтому high-demand не страдает.
    BANANA_PRO_KEY = 'GEM_PIX_2'

    result = {
        'id': clip_id,
        'status': 'error',
        'file': None,
        'operation_id': None,
        'error': None,
        'duration_sec': 0,
        'rewritten': False,
        'phase': 1,
        'key_label': getattr(client, 'key_label', None),
    }

    # ── ФАЗА 1: оригинальный промпт, 10 попыток ──────────────────────
    phase1 = _attempt_phase(
        client, clip_id, prompt, ref_uris, mode, whisk_refs,
        output_dir, effective_model, aspect_ratio or config.FASTGEN_ASPECT_RATIO,
        MAX_ATTEMPTS, 'phase1', stop_event=stop_event,
        ref_paths=ref_paths,
    )

    if phase1['ok']:
        result.update({
            'status': 'ok',
            'file': phase1['file'],
            'operation_id': phase1.get('operation_id'),
            'duration_sec': phase1.get('elapsed', 0),
            'phase': 1,
        })
        suffix = ""
        logging.info(f"Clip #{clip_id}: OK phase1 ({result['duration_sec']}s)")
        print(f"  {_GREEN}[OK]{_RESET} #{clip_id}: {result['duration_sec']}s")
        return result

    # Фаза 1 провалилась
    # License dead — no rewrite/remix, signal to caller to switch key
    if phase1.get('license_dead'):
        result['error'] = phase1['last_error']
        result['license_dead'] = True
        return result

    # ── Banana Pro fallback (per-clip, для этого клипа) ──────────────────
    # Триггер: технический фейл phase1 (timeout / network / high-demand OpenAI),
    # и текущая модель НЕ Banana Pro. Banana Pro идёт мимо OpenAI — на high-demand
    # OpenAI не упирается. Если этот клип не получится и через Banana Pro,
    # тогда уже стандартный путь (ref_blocked / phase2 rewrite).
    _err_str = str(phase1.get('last_error') or '').lower()
    # capacity-фейл считаем тех-фейлом явно: формулировка «No accounts available»
    # может прийти без хвоста «try again later», и тогда _is_transient_error даёт
    # False — клип уходил бы в реврайты вместо смены модели.
    _is_tech_fail = _is_transient_error(_err_str) or _is_capacity_error(_err_str)
    if (_is_tech_fail
            and effective_model != BANANA_PRO_KEY
            and not phase1.get('ref_blocked')
            and not phase1.get('censored')):
        print(f"  {_YELLOW}[HIGH-DEMAND → BANANA-PRO FALLBACK]{_RESET} #{clip_id}: tech fail on {effective_model}, switching model for this clip only")
        logging.warning(f"Clip #{clip_id}: tech-fail phase1 on {effective_model} ({_err_str[:120]}); per-clip fallback to Banana Pro")
        phase1_banana = _attempt_phase(
            client, clip_id, prompt, ref_uris, mode, whisk_refs,
            output_dir, BANANA_PRO_KEY, aspect_ratio or config.FASTGEN_ASPECT_RATIO,
            MAX_ATTEMPTS, 'phase1_banana_fallback', stop_event=stop_event,
            ref_paths=ref_paths,
        )
        if phase1_banana['ok']:
            result.update({
                'status': 'ok',
                'file': phase1_banana['file'],
                'operation_id': phase1_banana.get('operation_id'),
                'duration_sec': phase1_banana.get('elapsed', 0),
                'phase': 1,
                'fallback_model': BANANA_PRO_KEY,
            })
            logging.info(f"Clip #{clip_id}: OK after Banana Pro fallback ({result['duration_sec']}s)")
            print(f"  {_GREEN}[OK-BANANA-FALLBACK]{_RESET} #{clip_id}: {result['duration_sec']}s")
            return result
        # Banana Pro тоже не помог. Если она дала блок — пускаем дальше через ref_blocked/phase2.
        # Если опять tech-fail — выходим, дальше rewrite не поможет (это перегрузка апстрима).
        _banana_err = str(phase1_banana.get('last_error') or '').lower()
        _banana_tech = _is_transient_error(_banana_err) or _is_capacity_error(_banana_err)
        if _banana_tech and not phase1_banana.get('ref_blocked') and not phase1_banana.get('censored'):
            print(f"  {_RED}[FAILED]{_RESET} #{clip_id}: tech-fail на обеих моделях, пропускаем клип")
            logging.error(f"Clip #{clip_id}: FAILED — tech-fail на оригинальной модели И Banana Pro fallback")
            result['error'] = phase1_banana.get('last_error') or phase1.get('last_error', '')
            return result
        # Banana Pro дал блок / censorship — продолжаем по стандартному пути,
        # но используем phase1_banana как точку отсчёта для ref_blocked/phase2 проверок.
        phase1 = phase1_banana

    # Ref-blocked: ремиксим через remix engine (REMIX_ENGINE) до 3 раз. Без рефов НЕ генерим.
    if phase1.get('ref_blocked'):
        REF_REMIX_ATTEMPTS = 3
        for _remix_attempt in range(1, REF_REMIX_ATTEMPTS + 1):
            if _remix_attempt == 1 and on_ref_blocked and ref_uris:
                # Первый ремикс — через session-level callback (binary search + upload всех ключей)
                print(f"  {_YELLOW}[REF BLOCKED → REMIX {_remix_attempt}/{REF_REMIX_ATTEMPTS}]{_RESET} #{clip_id}: remixing refs...")
                logging.warning(f"Clip #{clip_id}: REF BLOCKED — ref remix attempt {_remix_attempt}/{REF_REMIX_ATTEMPTS}")
                on_ref_blocked()
                if cfg_ref is not None:
                    ref_uris = cfg_ref.get('ref_uris') or []
            elif _remix_attempt > 1 and cfg_ref is not None:
                # Повторные ремиксы — прямой вызов remix_ref для свежего варианта
                ref_paths = cfg_ref.get('ref_paths_flow') or []
                if not ref_paths:
                    break
                print(f"  {_YELLOW}[REF BLOCKED → REMIX {_remix_attempt}/{REF_REMIX_ATTEMPTS}]{_RESET} #{clip_id}: fresh remix...")
                logging.warning(f"Clip #{clip_id}: REF BLOCKED — ref remix attempt {_remix_attempt}/{REF_REMIX_ATTEMPTS}")
                new_uris = []
                try:
                    for orig in ref_paths:
                        remix_ref(str(orig), str(orig))
                        new_uris.append(client.upload_reference(str(orig)))
                    cfg_ref['ref_uris'] = new_uris
                    ref_uris = new_uris
                except Exception as _re:
                    logging.error(f"Clip #{clip_id}: remix attempt {_remix_attempt} failed: {_re}")
                    continue
            else:
                break

            if not ref_uris:
                break

            phase1_remixed = _attempt_phase(
                client, clip_id, prompt, ref_uris, mode, whisk_refs,
                output_dir, effective_model, aspect_ratio or config.FASTGEN_ASPECT_RATIO,
                MAX_ATTEMPTS, f'phase1_remix{_remix_attempt}', stop_event=stop_event,
                ref_paths=ref_paths,
            )
            if phase1_remixed['ok']:
                result.update({
                    'status': 'ok',
                    'file': phase1_remixed['file'],
                    'operation_id': phase1_remixed.get('operation_id'),
                    'duration_sec': phase1_remixed.get('elapsed', 0),
                    'phase': 1,
                    'rewritten': False,
                })
                logging.info(f"Clip #{clip_id}: OK after ref remix #{_remix_attempt} ({result['duration_sec']}s)")
                print(f"  {_GREEN}[OK-REMIX-{_remix_attempt}]{_RESET} #{clip_id}: {result['duration_sec']}s")
                return result
            phase1 = phase1_remixed
            if not phase1.get('ref_blocked'):
                break  # другая ошибка — дальше ремикс не поможет

        # Попытка 4: ремикс последнего ремикса (remix of remix)
        if cfg_ref is not None and phase1.get('ref_blocked'):
            ref_paths = cfg_ref.get('ref_paths_flow') or []
            _ror_uris = []
            _ror_ok = False
            try:
                print(f"  {_YELLOW}[REF BLOCKED → REMIX-OF-REMIX]{_RESET} #{clip_id}: remixing last remix output...")
                logging.warning(f"Clip #{clip_id}: REF BLOCKED — remix-of-remix attempt")
                for orig in ref_paths:
                    remix_ref(str(orig), str(orig))
                    _ror_uris.append(client.upload_reference(str(orig)))
                _ror_ok = bool(_ror_uris)
            except Exception as _re:
                logging.error(f"Clip #{clip_id}: remix-of-remix failed: {_re}")

            if _ror_ok:
                phase1_ror = _attempt_phase(
                    client, clip_id, prompt, _ror_uris, mode, whisk_refs,
                    output_dir, effective_model, aspect_ratio or config.FASTGEN_ASPECT_RATIO,
                    MAX_ATTEMPTS, 'phase1_remix_ror', stop_event=stop_event,
                    ref_paths=ref_paths,
                )
                if phase1_ror['ok']:
                    result.update({
                        'status': 'ok',
                        'file': phase1_ror['file'],
                        'operation_id': phase1_ror.get('operation_id'),
                        'duration_sec': phase1_ror.get('elapsed', 0),
                        'phase': 1,
                        'rewritten': False,
                    })
                    logging.info(f"Clip #{clip_id}: OK after remix-of-remix ({result['duration_sec']}s)")
                    print(f"  {_GREEN}[OK-REMIX-ROR]{_RESET} #{clip_id}: {result['duration_sec']}s")
                    return result
                phase1 = phase1_ror

        # Все попытки ремикса не помогли — клип пропускаем, без рефов не генерим
        print(f"  {_RED}[REF BLOCKED — SKIP]{_RESET} #{clip_id}: still blocked after all remix attempts, skipping")
        logging.error(f"Clip #{clip_id}: ref blocked after all remix attempts — clip skipped")
        result.update({'status': 'skipped', 'error': 'ref_blocked_after_remix'})
        return result

    # Prominent person в промпте → специальный rewrite с заменой имени на внешность
    _proj_dir = output_dir.parent if output_dir else None
    # ── ФАЗА 2+: до 5 циклов реврайта, каждый MAX_ATTEMPTS попыток ──────
    _is_person = bool(phase1.get('prominent_people'))
    _current_prompt = prompt
    _last_rw_phase = phase1
    MAX_REWRITES = 5
    MAX_REWRITE_ATTEMPTS = MAX_ATTEMPTS  # 10 попыток на каждый реврайт
    result['phase'] = 2

    for _rw_n in range(1, MAX_REWRITES + 1):
        # Hard Stop — не тратим квоту на rewrite если юзер остановил
        if _flowultra_shutdown_event.is_set():
            logging.info(f"Clip #{clip_id}: rewrite loop aborted by shutdown")
            result['status'] = 'error'
            result['error'] = 'hard stop'
            return result
        _lbl = 'PERSON REWRITE' if (_is_person and _rw_n == 1) else 'REWRITE'
        print(f"  {_RED}[{_lbl} #{_rw_n}/{MAX_REWRITES}]{_RESET} #{clip_id}: rewriting prompt...")
        logging.info(f"Clip #{clip_id}: rewrite #{_rw_n}/{MAX_REWRITES} (is_person={_is_person and _rw_n == 1})")

        try:
            if _is_person and _rw_n == 1:
                _new_prompt = client.rewrite_prompt_for_person(_current_prompt, project_dir=_proj_dir, clip_id=clip_id)
            else:
                _new_prompt = client.rewrite_prompt(_current_prompt, project_dir=_proj_dir, clip_id=clip_id)
        except Exception as _rw_err:
            logging.warning(f"Clip #{clip_id}: rewrite #{_rw_n} API error ({_rw_err}), using previous prompt")
            _new_prompt = _current_prompt

        if _new_prompt and _new_prompt != _current_prompt:
            result['rewritten'] = True
            _current_prompt = _new_prompt
            logging.info(f"Clip #{clip_id}: rewrite #{_rw_n} new prompt, starting phase2_rw{_rw_n}...")
        else:
            print(f"  {_YELLOW}[{_lbl} #{_rw_n}]{_RESET} #{clip_id}: rewrite returned same prompt")
            logging.warning(f"Clip #{clip_id}: rewrite #{_rw_n} returned same prompt")

        _ph = _attempt_phase(
            client, clip_id, _current_prompt, ref_uris, mode, whisk_refs,
            output_dir, effective_model, aspect_ratio or config.FASTGEN_ASPECT_RATIO,
            MAX_REWRITE_ATTEMPTS, f'phase2_rw{_rw_n}', stop_event=stop_event,
            ref_paths=ref_paths,
        )
        _last_rw_phase = _ph

        if _ph.get('license_dead'):
            result['error'] = _ph['last_error']
            result['license_dead'] = True
            return result

        if _ph['ok']:
            result.update({
                'status': 'ok',
                'file': _ph['file'],
                'operation_id': _ph.get('operation_id'),
                'duration_sec': _ph.get('elapsed', 0),
                'total_rewrite_phases': _rw_n,
            })
            suffix = f" (rewrite #{_rw_n})" if result['rewritten'] else f" (phase2 rw#{_rw_n})"
            logging.info(f"Clip #{clip_id}: OK phase2 (rewrite #{_rw_n}, {result['duration_sec']}s)")
            print(f"  {_GREEN}[OK]{_RESET} #{clip_id}: {result['duration_sec']}s{suffix}")
            return result
        # Не прерываем цикл на технических ошибках — пробуем все 10 реврайтов

    # Все реврайты исчерпаны
    result['total_rewrite_phases'] = MAX_REWRITES
    censored = phase1['censored'] or _last_rw_phase.get('censored', False)
    result['status'] = 'censored' if censored else 'error'
    result['error'] = _last_rw_phase.get('last_error') or phase1.get('last_error', '')

    if censored:
        logging.error(f"Clip #{clip_id}: CENSORED after {MAX_REWRITES} rewrites")
        print(f"  {_RED}[SKIP]{_RESET}     #{clip_id}: censored after {MAX_REWRITES} rewrites, giving up")
    else:
        logging.error(f"Clip #{clip_id}: FAILED after {MAX_REWRITES} rewrites ({MAX_REWRITES * MAX_REWRITE_ATTEMPTS} attempts total)")
        print(f"  {_YELLOW}[FAILED]{_RESET}    #{clip_id}: {MAX_REWRITES} rewrites x {MAX_REWRITE_ATTEMPTS} attempts exhausted")

    return result


# ======================================================================
# Основная функция генерации (этап 3)
# ======================================================================

def generate_all_images(
    project_dir: str,
    max_parallel: Optional[int] = None,
    clip_ids: Optional[List[int]] = None,
    mode: Optional[str] = None,
    model: Optional[str] = None,
    aspect_ratio: Optional[str] = None,
    api_key: Optional[str] = None,
    preflight: bool = True,
    dual_key: bool = False,
    silent: bool = False,
    include_infographics: bool = False,
    allow_veo_fallback: bool = True,
    use_claims: bool = False,
    claim_owner: str = "fastgen",
) -> Dict[str, Any]:
    """
    Основная функция этапа 3: генерация изображений для всех клипов.

    Args:
        project_dir: Путь к папке проекта
        max_parallel: Максимум параллельных генераций
        clip_ids: Список ID клипов (если None — все)
        mode: Режим референсов: 'flow', 'whisk', None=auto
        model: Модель FastGen
        aspect_ratio: Соотношение сторон
        api_key: API ключ (если не из .env)
        allow_veo_fallback: False — не уводить остаток на VeoNonStop при
            исчерпании квоты (ставится, когда нас САМИХ позвал imggen как
            фолбэк: его ключи уже мертвы, звать их обратно = кольцо).
            Кулдаун до обновления часовой квоты FastGen при этом остаётся.

    Returns:
        Лог генерации (dict)
    """
    project_path = Path(project_dir)
    max_parallel = max_parallel or config.FASTGEN_MAX_PARALLEL

    # Подавить DEBUG-спам от urllib3/requests в консоли
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('requests').setLevel(logging.WARNING)

    # Лог-файл для генерации (в папке проекта, skip if path too long)
    from datetime import datetime as _dt
    log_ts = _dt.now().strftime('%Y%m%d_%H%M%S')
    gen_log_path = project_path / f'gen_{log_ts}.log'
    file_handler = None
    try:
        file_handler = logging.FileHandler(str(gen_log_path), encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)  # файл пишет всё включая DEBUG
        file_handler.setFormatter(logging.Formatter('%(asctime)s | %(levelname)-8s | %(message)s'))
        logging.getLogger().addHandler(file_handler)
    except OSError:
        logging.warning(f"Cannot create log file (path too long?): {gen_log_path}")
    logging.info(f"=== FastGen generation started for {project_path.name} ===")

    print("\n" + "=" * 60)
    print("STAGE 3: IMAGE GENERATION (FastGen)")
    print("=" * 60)
    print(f"  Log: {gen_log_path}")

    # ------------------------------------------------------------------
    # 1. Чтение промптов
    # ------------------------------------------------------------------
    prompts_file = find_prompts_file(str(project_path))
    if not prompts_file:
        raise FileNotFoundError(
            f"Prompts file not found in {project_path} "
            f"(expected *_prompts_*.txt)"
        )

    _mp_clips_for_prompts = []
    try:
        _mp_path_for_prompts = project_path / 'montage_plan.json'
        if _mp_path_for_prompts.exists():
            import json as _json
            _mp_for_prompts = _json.loads(_mp_path_for_prompts.read_text(encoding='utf-8-sig'))
            _mp_clips_for_prompts = _mp_for_prompts.get('clips', [])
    except Exception as _e:
        logging.warning(f"Could not load montage plan for prompt ID remap: {_e}")

    all_prompts = extract_all_prompts(prompts_file)
    if _mp_clips_for_prompts:
        all_prompts, _remapped = remap_dense_prompts_for_montage(all_prompts, _mp_clips_for_prompts)
        if _remapped:
            print("  [Prompts] Remapped dense prompt file to real montage clip IDs")
    if not all_prompts:
        raise ValueError(f"No prompts found in {prompts_file}")

    print(f"\n  Prompts file: {Path(prompts_file).name}")
    print(f"  Total prompts: {len(all_prompts)}")

    # Фильтрация по ID
    if clip_ids:
        all_prompts = {k: v for k, v in all_prompts.items() if k in clip_ids}
        print(f"  Selected clips: {len(all_prompts)} ({', '.join(map(str, sorted(all_prompts.keys())))})")


    # Real Photo skip filter — same logic as imggen.py / veo_video.py
    try:
        _mp_path2 = project_path / 'montage_plan.json'
        if _mp_path2.exists():
            import json as _json2
            _mp2 = _json2.loads(_mp_path2.read_text(encoding='utf-8-sig'))
            _mp_clips2 = _mp2.get('montage_plan', _mp2).get('clips', [])
            from units.source_ledger import EXTERNAL_SOURCES, INFOGRAPHIC_SOURCES
            _external_ids = {c['id'] for c in _mp_clips2
                             if c.get('source') in EXTERNAL_SOURCES}
            _infographic_ids = {c['id'] for c in _mp_clips2
                                if c.get('source') in INFOGRAPHIC_SOURCES}
            _skip_ids = _external_ids if include_infographics else (_external_ids | _infographic_ids)
            if _skip_ids:
                _before = len(all_prompts)
                all_prompts = {k: v for k, v in all_prompts.items() if k not in _skip_ids}
                _label = 'external sources' if include_infographics else 'external sources + infographics'
                print(f"  [Source filter] Skipping {len(_skip_ids)} {_label} "
                      f"({_before} -> {len(all_prompts)})")
    except Exception as _e:
        logging.warning(f"Real Photo filter failed: {_e}")

    if not all_prompts:
        raise ValueError("No prompts to generate after filtering")

    # ------------------------------------------------------------------
    # 2. Инициализация клиентов (multi-key если включено в GUI/pipeline)
    # ------------------------------------------------------------------
    def _safe_parallel(value, default_value):
        try:
            value = int(value)
        except Exception:
            value = int(default_value or 1)
        return max(1, value)

    def _fastgen_key_defs() -> List[Dict[str, Any]]:
        raw_keys = [
            ('Key1', getattr(config, 'FASTGEN_API_KEY', ''), max_parallel),
            ('Key2', getattr(config, 'FASTGEN_API_KEY_2', '') or getattr(config, 'FASTGEN_API_KEY_BACKUP', ''),
             getattr(config, 'FASTGEN_MAX_PARALLEL_2', max_parallel)),
            ('Key3', getattr(config, 'FASTGEN_API_KEY_3', ''),
             getattr(config, 'FASTGEN_MAX_PARALLEL_3', max_parallel)),
            ('Key4', getattr(config, 'FASTGEN_API_KEY_4', ''),
             getattr(config, 'FASTGEN_MAX_PARALLEL_4', max_parallel)),
        ]
        result: List[Dict[str, Any]] = []
        seen = set()
        for label, key_value, key_mp in raw_keys:
            key_value = str(key_value or '').strip()
            if not key_value or key_value in seen:
                continue
            seen.add(key_value)
            result.append({
                'label': label,
                'api_key': key_value,
                'mp': _safe_parallel(key_mp, max_parallel),
            })
        return result

    _all_fastgen_key_defs = _fastgen_key_defs()
    _selected_api_key = str(api_key or getattr(config, 'FASTGEN_API_KEY', '') or '').strip()
    _selected_key_def = next(
        (d for d in _all_fastgen_key_defs if d['api_key'] == _selected_api_key),
        None,
    )
    if _selected_key_def is None:
        _selected_key_def = {
            'label': 'Key1',
            'api_key': _selected_api_key,
            'mp': _safe_parallel(max_parallel, max_parallel),
        }

    client = FastGenClient(api_key=_selected_key_def['api_key'] or api_key)
    client.key_label = _selected_key_def['label']
    mp1 = min(max_parallel, _safe_parallel(_selected_key_def.get('mp'), max_parallel))

    # Отменяем зависшие задачи от прошлых прогонов (освобождаем слоты)
    print("\n  Cancelling leftover tasks from previous runs...")
    _ca = client.cancel_all_tasks()
    _found = _ca.get('total_found', 0)
    if _found:
        _cancelled = _ca.get('total_cancelled', 0)
        _refunded = _ca.get('total_refunded', 0)
        print(f"  Cancelled: {_cancelled}/{_found} tasks, refunded: {_refunded}")
        if _cancelled:
            time.sleep(3)  # даём серверу освободить слоты
    else:
        print("  No leftover tasks.")

    # Проверка квоты выбранного FastGen-ключа
    try:
        usage = client.get_usage()
    except Exception:
        usage = {}
    if usage:
        remaining = parse_usage_remaining(usage)
        threads = parse_usage_threads(usage)
        remaining_str = remaining if remaining is not None else '?'
        print(f"\n  [{_selected_key_def['label']}] API quota: {remaining_str} gen/h remaining | threads: {threads}")
        if isinstance(threads, int) and threads > 0:
            mp1 = min(mp1, threads)

    clients_cfg: list = [{'client': client, 'mp': mp1,
                          'ref_uris': [], 'whisk_refs': {}, 'label': _selected_key_def['label']}]

    if dual_key:
        _seen_keys = {_selected_key_def['api_key']}
        for _key_def in _all_fastgen_key_defs:
            _label = _key_def['label']
            _key = _key_def['api_key']
            _cfg_mp = _key_def['mp']
            if not _key or _key in _seen_keys:
                continue
            _seen_keys.add(_key)
            _client_n = FastGenClient(api_key=_key)
            _client_n.key_label = _label
            _ca_n = _client_n.cancel_all_tasks()
            _found_n = _ca_n.get('total_found', 0)
            if _found_n:
                print(f"  [{_label}] Cancelled {_ca_n.get('total_cancelled',0)}/{_found_n} leftover tasks")
            # Сверка квоты с retry. Если не удалось — НЕ доверяем раздутой настройке
            # (_cfg_mp может быть 75 при реальной квоте 25), берём квоту первого ключа mp1.
            _mp_n = _cfg_mp
            _synced = False
            for _ua in range(3):
                try:
                    _usage_n = _client_n.get_usage()
                    if _usage_n:
                        _threads_n = parse_usage_threads(_usage_n)
                        _remaining_n = parse_usage_remaining(_usage_n)
                        print(f"  [{_label}] API quota: {_remaining_n if _remaining_n is not None else '?'} gen/h remaining | threads: {_threads_n}")
                        if isinstance(_threads_n, int) and _threads_n > 0:
                            _mp_n = _threads_n
                            _synced = True
                        break
                except Exception as _ue:
                    logging.warning(f"[{_label}] usage attempt {_ua+1}/3 failed: {_ue}")
                    time.sleep(2)
            if not _synced:
                # квота не сверена — консервативно: не больше квоты первого ключа
                _mp_n = min(_cfg_mp, mp1)
                print(f"  [{_label}] quota sync FAILED — fallback to {_mp_n} (Key1 quota, не настройка)")
            clients_cfg.append({'client': _client_n, 'mp': _mp_n,
                                'ref_uris': [], 'whisk_refs': {}, 'label': _label})

    max_parallel = sum(cfg['mp'] for cfg in clients_cfg)
    if len(clients_cfg) > 1:
        _parts = ' + '.join(f"{c['label']}:{c['mp']}" for c in clients_cfg)
        print(f"  Multi-key: {_parts} = {max_parallel} workers")

    # ------------------------------------------------------------------
    # 3. Пропуск уже сгенерированных (ДО загрузки рефов)
    # ------------------------------------------------------------------
    output_dir = project_path / 'generated'
    output_dir.mkdir(exist_ok=True)

    def _generate_claimed(**kwargs):
        clip_id = int(kwargs['clip_id'])
        if not use_claims:
            return generate_single_clip(**kwargs)
        claim = acquire_image_claim(output_dir, clip_id, owner=claim_owner)
        if claim is None:
            return {
                'id': clip_id,
                'status': 'claimed_elsewhere',
                'file': None,
                'operation_id': None,
                'duration_sec': 0,
            }
        try:
            return generate_single_clip(**kwargs)
        finally:
            claim.release()

    skipped = []
    for cid in list(all_prompts.keys()):
        found = False
        for ext in ('.png', '.jpeg', '.jpg', '.webp'):
            p = output_dir / f"{cid:03d}{ext}"
            if p.exists() and p.stat().st_size > 0:
                found = True
                break
        if found:
            skipped.append(cid)
            del all_prompts[cid]
    if skipped:
        print(f"\n  Skipped (already exist): {len(skipped)}, remaining: {len(all_prompts)}")

    if not all_prompts:
        print("  All images already exist. Nothing to generate.")
        return {'generated': len(skipped), 'failed': 0, 'results': []}

    # ------------------------------------------------------------------
    # 4. Сканирование и загрузка референсов
    # ------------------------------------------------------------------
    refs = scan_references(str(project_path))
    total_refs = sum(len(v) for v in refs.values())

    ref_uris = []        # для Flow (Key1) — backward compat для preflight
    whisk_refs = {}      # для Whisk (Key1) — backward compat
    ref_paths_flow: List[Path] = []  # параллельный список путей для ремикса
    effective_mode = 'flow'  # по умолчанию

    if total_refs > 0:
        print_reference_summary(refs)

        # Определение режима
        if mode:
            effective_mode = mode
        else:
            detected = detect_reference_mode(refs)
            if detected == 'ask':
                effective_mode = ask_reference_mode()
            elif detected:
                effective_mode = detected
                print(f"  Auto-detected: {effective_mode.upper()}")

        print(f"  Reference mode: {effective_mode.upper()}")

        def _upload_refs_to_cfg(cfg: dict, label: str):
            """Загрузить рефы в Storage конкретного клиента."""
            _cl = cfg['client']
            print(f"\n  [{label}] Uploading {total_refs} reference(s)...")
            if effective_mode == 'flow':
                all_ref_files = refs['root'] + refs['subject'] + refs['scene'] + refs['style']
                _uris: List[str] = []
                _paths: List[Path] = []
                for rf in tqdm(all_ref_files[:10], desc=f"  [{label}] Upload refs", unit="file"):
                    # upload_reference сам ретраит 3×; если всё равно провал — RAISE,
                    # генерация останавливается (НЕ генерим без рефа = без левых персонажей)
                    uri = _cl.upload_reference(str(rf))
                    _uris.append(uri)
                    _paths.append(rf)
                cfg['ref_uris'] = _uris
                cfg['ref_paths_flow'] = _paths
                print(f"  [{label}] Uploaded: {len(_uris)} reference(s)")
                return _uris, _paths
            elif effective_mode == 'whisk':
                _wr: Dict[str, List[str]] = {'subject': [], 'scene': [], 'style': []}
                for category in ('subject', 'scene', 'style'):
                    for rf in refs[category]:
                        # retry внутри upload_reference; провал → RAISE (не генерим без рефа)
                        uri = _cl.upload_reference(str(rf))
                        _wr[category].append(uri)
                cfg['whisk_refs'] = _wr
                tw = sum(len(v) for v in _wr.values())
                print(f"  [{label}] Uploaded: {tw} ref(s) (sub={len(_wr['subject'])} sce={len(_wr['scene'])} sty={len(_wr['style'])})")
                return _wr, []
            return [], []

        # Выбранный ключ всегда
        ref_uris, ref_paths_flow = _upload_refs_to_cfg(clients_cfg[0], clients_cfg[0]['label'])
        clients_cfg[0]['ref_paths_flow'] = ref_paths_flow

        # Key2 whisk: заливаем сразу (preflight для whisk не запускается)
        # Key2 flow: заливаем ПОСЛЕ preflight (чтобы получить remixed файлы если нужно)
        if effective_mode == 'whisk':
            for cfg in clients_cfg[1:]:
                _upload_refs_to_cfg(cfg, cfg['label'])

    else:
        print("\n  No references found (ref/ folder missing or empty)")
        print("  Generating without reference images")

    refs_uploaded_at = time.time()

    # ------------------------------------------------------------------
    # 4b. Предполётная проверка рефов (Preflight)
    # ------------------------------------------------------------------
    if (preflight and effective_mode == 'flow' and ref_uris
            and config.FASTGEN_PREFLIGHT_REFS and config.REMIX_AVAILABLE):
        _preflight_model = model or config.FASTGEN_MODEL
        _preflight_key_label = clients_cfg[0]['label']
        print(f"\n  [PREFLIGHT] Testing reference images before generation ({_preflight_key_label}, model={_preflight_model})...")
        logging.info(f"PREFLIGHT: testing ref images via {_preflight_key_label}, model={_preflight_model}")

        # Main preflight: до 2 retries на SKIPPED (transient timeouts / network).
        # Это защита от cold-start сервера: первый вызов может уйти в timeout 180s,
        # второй — в уже прогретый backend за несколько секунд.
        # Только если ВСЕ попытки SKIPPED → pessimistic BLOCKED → isolate.
        # Hard Stop: InterruptedError пробрасывается наверх — retry цикл прерывается.
        # Подаём в preflight РЕАЛЬНЫЕ промпты из FINAL.txt — preflight выберет первый,
        # содержащий токены тестируемых ref'ов. Это ловит content-trigger в боевой
        # формулировке (например chained alien) ДО запуска массовой генерации.
        _pf_real_prompts = list(all_prompts.values()) if all_prompts else None
        _pf_result = client._preflight_test_refs(ref_uris, model=_preflight_model,
                                                  ref_paths=ref_paths_flow,
                                                  real_prompts=_pf_real_prompts)
        _pf_retries = 0
        _PF_MAX_RETRIES = 2
        while getattr(client, '_last_preflight_skipped', False) and _pf_retries < _PF_MAX_RETRIES:
            _pf_retries += 1
            # Check shutdown before sleep+retry
            if _flowultra_shutdown_event.is_set():
                raise InterruptedError("Main preflight aborted by shutdown signal")
            print(f"  {_YELLOW}[PREFLIGHT]{_RESET} SKIPPED — retry {_pf_retries}/{_PF_MAX_RETRIES} in 15s (cold-start wait)...")
            logging.warning(f"PREFLIGHT: SKIPPED, retry {_pf_retries}/{_PF_MAX_RETRIES}")
            # Interruptible sleep: 15 × 1s, check shutdown each
            for _ in range(15):
                if _flowultra_shutdown_event.is_set():
                    raise InterruptedError("Main preflight aborted by shutdown signal (in retry wait)")
                time.sleep(1)
            _pf_result = client._preflight_test_refs(ref_uris, model=_preflight_model,
                                                      ref_paths=ref_paths_flow,
                                                      real_prompts=_pf_real_prompts)
        if getattr(client, '_last_preflight_skipped', False):
            print(f"  {_YELLOW}[PREFLIGHT]{_RESET} Still SKIPPED after {_PF_MAX_RETRIES} retries → isolating via binary search")
            logging.warning(f"PREFLIGHT: {_PF_MAX_RETRIES}x SKIPPED — treating as BLOCKED, running isolate")
            _pf_result = False  # падаем в isolate

        if False:  # placeholder to keep elif structure
            pass
        elif not _pf_result:
            print(f"  {_RED}[PREFLIGHT]{_RESET} REF BLOCKED — isolating bad refs...")
            logging.warning("PREFLIGHT: REF BLOCKED — isolating bad refs via binary search")
            bad_idxs = client._isolate_blocked_refs(ref_uris, model=_preflight_model,
                                                     paths=ref_paths_flow,
                                                     real_prompts=_pf_real_prompts)
            bad_names = [ref_paths_flow[i].name for i in bad_idxs if i < len(ref_paths_flow)]
            print(f"  {_RED}[PREFLIGHT]{_RESET} Bad refs: {bad_names}")
            logging.warning(f"PREFLIGHT: bad refs identified: {bad_names}")
            for idx in bad_idxs:
                if idx >= len(ref_paths_flow):
                    continue
                orig = ref_paths_flow[idx]
                try:
                    remix_ref(str(orig), str(orig))
                    new_uri = client.upload_reference(str(orig))
                    ref_uris[idx] = new_uri
                    logging.info(f"PREFLIGHT: remixed {orig.name} in-place, re-uploaded")
                except Exception as e:
                    logging.error(f"PREFLIGHT: failed to remix {orig.name}: {e}")
                    print(f"  {_RED}[PREFLIGHT]{_RESET} Remix failed for {orig.name}: {e}")
            # Повторный preflight после remix — одна попытка.
            # Если SKIPPED → оптимистично считаем OK (inline handler catch'нет ref-block при генерации)
            _pf_result2 = client._preflight_test_refs(ref_uris, model=_preflight_model, ref_paths=ref_paths_flow)
            _pf2_skipped = getattr(client, '_last_preflight_skipped', False)
            if _pf2_skipped:
                logging.warning("PREFLIGHT post-remix: SKIPPED — assuming OK, inline handler catches blocks")
                print(f"  {_YELLOW}[PREFLIGHT]{_RESET} Post-remix check SKIPPED — proceeding with remixed refs")
                clients_cfg[0]['ref_uris'] = ref_uris
                clients_cfg[0]['ref_paths_flow'] = ref_paths_flow
            elif not _pf_result2:
                print(f"  {_RED}[PREFLIGHT]{_RESET} STILL BLOCKED after remix — dropping refs")
                logging.warning("PREFLIGHT: still blocked after remix — refs dropped")
                ref_uris = []
                clients_cfg[0]['ref_uris'] = []
            else:
                print(f"  {_GREEN}[PREFLIGHT]{_RESET} OK after remix — continuing with remixed refs")
                logging.info("PREFLIGHT: OK after remix")
                clients_cfg[0]['ref_uris'] = ref_uris
                clients_cfg[0]['ref_paths_flow'] = ref_paths_flow
        else:
            if getattr(client, '_last_preflight_skipped', False):
                # Exception уже залогирован внутри _preflight_test_refs.
                # Не печатаем "OK" — рефы не протестированы фактически.
                pass
            else:
                print(f"  {_GREEN}[PREFLIGHT]{_RESET} OK — refs accepted")
                logging.info("PREFLIGHT: refs OK")

        # Sync финальных рефов (оригинальные или remixed) во все остальные ключи
        if len(clients_cfg) > 1 and ref_paths_flow:
            for _k2cfg in clients_cfg[1:]:
                _lbl = _k2cfg['label']
                print(f"  Uploading final refs to {_lbl}...")
                _k2uris: List[str] = []
                for rf in tqdm(ref_paths_flow, desc=f"  [{_lbl}] Upload refs", unit="file"):
                    try:
                        uri = _k2cfg['client'].upload_reference(str(rf))
                        _k2uris.append(uri)
                    except Exception as e:
                        logging.warning(f"[{_lbl}] failed to upload {rf.name}: {e}")
                _k2cfg['ref_uris'] = _k2uris
                _k2cfg['ref_paths_flow'] = ref_paths_flow
                print(f"  [{_lbl}] Uploaded: {len(_k2uris)} reference(s)")

    # Если preflight не запускался для flow — синкаем оригинальные рефы в Key2 сейчас
    _preflight_ran = (preflight and effective_mode == 'flow' and ref_uris
                      and config.FASTGEN_PREFLIGHT_REFS and config.REMIX_AVAILABLE)
    if not _preflight_ran and effective_mode == 'flow' and ref_paths_flow and len(clients_cfg) > 1:
        for _k2cfg in clients_cfg[1:]:
            _lbl = _k2cfg['label']
            print(f"  Uploading refs to {_lbl}...")
            _k2uris = []
            for rf in tqdm(ref_paths_flow, desc=f"  [{_lbl}] Upload refs", unit="file"):
                try:
                    _k2uris.append(_k2cfg['client'].upload_reference(str(rf)))
                except Exception as e:
                    logging.warning(f"[{_lbl}] failed to upload {rf.name}: {e}")
            _k2cfg['ref_uris'] = _k2uris
            print(f"  [{_lbl}] Uploaded: {len(_k2uris)} reference(s)")

    # ------------------------------------------------------------------
    # 5. Генерация изображений
    # ------------------------------------------------------------------
    used_model = model or config.FASTGEN_MODEL
    used_aspect = aspect_ratio or config.FASTGEN_ASPECT_RATIO

    print(f"\n  Model: {used_model}")
    print(f"  Aspect ratio: {used_aspect}")
    if len(clients_cfg) > 1:
        print(f"  Parallel workers: {max_parallel} ({' + '.join(str(c['mp']) for c in clients_cfg)} keys)")
    else:
        print(f"  Parallel workers: {max_parallel}")
    print("=" * 60)

    # Накопленные OK-результаты по всем циклам (не перегенерируются)
    accumulated_ok: Dict[int, Dict] = {}   # clip_id -> result
    pending_prompts = dict(all_prompts)    # clip_id -> prompt_text (убывает при OK)

    # Worker ID tracking для dual-key worker-assignment
    def _get_fg_worker_id() -> int:
        tid = threading.get_ident()
        with _fg_worker_lock:
            if tid not in _fg_worker_map:
                _fg_worker_map[tid] = _fg_worker_counter['next']
                _fg_worker_counter['next'] += 1
            return _fg_worker_map[tid]

    cycle_num = 0
    quota_exhausted = False  # флаг: в этом цикле была ошибка QuotaExhaustedError
    _quota_dead_labels = set()
    _pending_quota_switch_def = None
    start_total = time.time()
    cycle_results: List[Dict] = []  # результаты последнего цикла
    ctrlc_count = 0  # счётчик Ctrl+C (нужно 3 нажатия для выхода)

    # ── Auto-fallback state (non-interactive / silent mode only) ──
    _original_fg_model = used_model
    _fallback_raw = (
        os.getenv('FASTGEN_FALLBACK_MODELS')
        or os.getenv('FASTGEN_FALLBACK_MODEL')
        or 'FLOWER,GROK'
    )
    _fallback_fg_models: List[str] = []
    _seen_fallback_models = {_original_fg_model.upper()}
    for _m in str(_fallback_raw).split(','):
        _m = _m.strip().upper()
        if _m and _m not in _seen_fallback_models:
            _fallback_fg_models.append(_m)
            _seen_fallback_models.add(_m)
    if not _fallback_fg_models:
        _fallback_fg_models = ['GEM_PIX_2' if _original_fg_model.upper() != 'GEM_PIX_2' else 'FLOWER']
    _fallback_model_index = 0

    REF_REFRESH_INTERVAL = 50 * 60  # 50 минут в секундах (TTL Storage = 1 час)

    # Ремикс заблокированных рефов (вызывается любым воркером при REF BLOCKED)
    # Обновляет ref_uris во ВСЕХ clients_cfg, чтобы последующие клипы тоже получили новые рефы
    _ref_remix_lock = threading.Lock()
    _ref_remixed = [False]  # выполнить только один раз (первый воркер, остальные ждут)

    def _on_ref_blocked():
        # Вызывается когда реальный промпт + рефы = REF BLOCKED.
        # Тестируем с промптом содержащим имя — это триггерит "prominent people"
        # даже без реального промпта клипа.
        with _ref_remix_lock:
            if _ref_remixed[0]:
                return   # уже сделано другим воркером
            key1_cfg  = clients_cfg[0]
            k1_client = key1_cfg['client']
            k1_uris   = key1_cfg.get('ref_uris') or []
            k1_paths  = key1_cfg.get('ref_paths_flow') or []
            if not k1_uris or not k1_paths:
                _ref_remixed[0] = True
                return
            # Используем реальные промпты из FINAL.txt — для каждого subset выбираем
            # промпт где упомянут хоть один из refs. Это воспроизводит реальную
            # блокировку (а synthetic prompt этого не делает → bad refs=[]).
            _on_block_real_prompts = list(all_prompts.values()) if all_prompts else None
            print(f"\n  {_YELLOW}[REF REMIX]{_RESET} Isolating blocked ref via binary search (real prompts from FINAL.txt)...")
            logging.warning("REF BLOCKED: isolating with real prompts from FINAL.txt")
            bad_idxs = k1_client._isolate_blocked_refs(k1_uris, prompt=None, paths=k1_paths,
                                                       real_prompts=_on_block_real_prompts)
            if not bad_idxs:
                # Изоляция не нашла плохой реф — ремиксим все
                print(f"  {_YELLOW}[REF REMIX]{_RESET} Could not isolate — remixing all {len(k1_paths)} refs...")
                bad_idxs = list(range(len(k1_paths)))
            else:
                bad_names = [k1_paths[i].name for i in bad_idxs if i < len(k1_paths)]
                print(f"  {_YELLOW}[REF REMIX]{_RESET} Bad refs: {bad_names} — remixing...")
                logging.warning(f"REF BLOCKED: bad refs={bad_names}")
            new_paths = list(k1_paths)
            _remix_failed_indices: list = []  # индексы рефов которые не удалось ремикснуть
            for idx in bad_idxs:
                if _flowultra_shutdown_event.is_set():
                    logging.warning("_on_ref_blocked: shutdown during remix loop — aborting")
                    print(f"  {_YELLOW}[REF REMIX]{_RESET} aborted by shutdown signal")
                    return  # НЕ ставим _ref_remixed[0]=True — следующие вызовы увидят что ничего не сделано
                if idx >= len(k1_paths):
                    continue
                orig = k1_paths[idx]
                try:
                    remix_ref(str(orig), str(orig))
                    logging.info(f"Remixed: {orig.name} in-place")
                except Exception as e:
                    _remix_failed_indices.append(idx)
                    print(f"  {_RED}[REF REMIX FAILED]{_RESET} {orig.name}: {str(e)[:200]}")
                    logging.error(f"Remix failed for {orig.name}: {e}")

            # ФИКС А: если не удалось отремиксить ВСЕ плохие — дропаем их совсем
            # (иначе следующие клипы снова упрутся в тот же celebrity-реф).
            if _remix_failed_indices:
                _dropped_names = [k1_paths[i].name for i in _remix_failed_indices if i < len(k1_paths)]
                print(f"  {_RED}[REF REMIX]{_RESET} {len(_remix_failed_indices)} ref(s) could not be remixed — DROPPING: {_dropped_names}")
                logging.error(f"REF REMIX: dropping {len(_remix_failed_indices)} unremixable refs: {_dropped_names}")
                # Фильтруем bad_idxs и new_paths — удаляем failed напрямую
                _drop_set = set(_remix_failed_indices)
                new_paths = [p for i, p in enumerate(k1_paths) if i not in _drop_set]
                if not new_paths:
                    print(f"  {_RED}[REF REMIX]{_RESET} ВСЕ рефы непригодны — пайплайн продолжит БЕЗ рефов")
                    logging.error("REF REMIX: all refs dropped — generation will proceed without refs")
            # Re-upload в Storage каждого ключа
            for cfg in clients_cfg:
                if _flowultra_shutdown_event.is_set():
                    logging.warning("_on_ref_blocked: shutdown during re-upload — aborting")
                    return
                lbl = cfg['label']
                new_uris: List[str] = []
                for rf in tqdm(new_paths, desc=f"  [{lbl}] Re-upload remixed", unit="file"):
                    if _flowultra_shutdown_event.is_set():
                        break
                    try:
                        new_uris.append(cfg['client'].upload_reference(str(rf)))
                    except Exception as e:
                        logging.warning(f"[{lbl}] remix re-upload failed {Path(rf).name}: {e}")
                cfg['ref_uris']       = new_uris
                cfg['ref_paths_flow'] = new_paths
                print(f"  [{lbl}] Remixed refs uploaded: {len(new_uris)}")
            _ref_remixed[0] = True

    def _do_ref_refresh():
        """Перезагрузить рефы в FastGen Storage для всех ключей (вызывается при TTL)."""
        elapsed_min = int((time.time() - refs_uploaded_at) / 60)
        print(f"\n  {_YELLOW}[REFS REFRESH]{_RESET} {elapsed_min}m since last upload (TTL=60m) — re-uploading...")
        logging.info(f"Re-uploading {total_refs} refs (elapsed={elapsed_min}m)")
        for _cfg in clients_cfg:
            _lbl = _cfg['label']
            if effective_mode == 'flow':
                _paths = _cfg.get('ref_paths_flow') or (refs['root'] + refs['subject'] + refs['scene'] + refs['style'])
                _new_uris: List[str] = []
                for rf in tqdm(_paths[:10], desc=f"  [{_lbl}] Re-upload", unit="file"):
                    try:
                        _new_uris.append(_cfg['client'].upload_reference(str(rf)))
                    except Exception as e:
                        logging.warning(f"[{_lbl}] Re-upload failed {Path(rf).name}: {e}")
                _cfg['ref_uris'] = _new_uris
                print(f"  [{_lbl}] Re-uploaded: {len(_new_uris)} reference(s)")
            elif effective_mode == 'whisk':
                _wr: Dict[str, List[str]] = {'subject': [], 'scene': [], 'style': []}
                for category in ('subject', 'scene', 'style'):
                    for rf in refs[category]:
                        try:
                            _wr[category].append(_cfg['client'].upload_reference(str(rf)))
                        except Exception as e:
                            logging.warning(f"[{_lbl}] Re-upload failed {rf.name}: {e}")
                _cfg['whisk_refs'] = _wr
                print(f"  [{_lbl}] Re-uploaded: {sum(len(v) for v in _wr.values())} reference(s)")
        # Обновляем backward-compat vars для Key1
        ref_uris.clear()
        ref_uris.extend(clients_cfg[0].get('ref_uris', []))
        return time.time()

    def _next_fastgen_key_def(skip_dead: bool = True) -> Optional[Dict[str, Any]]:
        if not _all_fastgen_key_defs:
            return None
        current_labels = {cfg.get('label') for cfg in clients_cfg}
        current_label = clients_cfg[0].get('label') if clients_cfg else None
        labels = [d['label'] for d in _all_fastgen_key_defs]
        start = labels.index(current_label) + 1 if current_label in labels else 0
        ordered = _all_fastgen_key_defs[start:] + _all_fastgen_key_defs[:start]
        for key_def in ordered:
            label = key_def['label']
            if label in current_labels:
                continue
            if skip_dead and label in _quota_dead_labels:
                continue
            return key_def
        return None

    def _switch_to_fastgen_key(key_def: Dict[str, Any]) -> str:
        nonlocal client, max_parallel
        label = key_def['label']
        print(f"\n  {_YELLOW}Switching to {label} API key...{_RESET}")
        client = FastGenClient(api_key=key_def['api_key'])
        client.key_label = label

        new_cfg = {
            'client': client,
            'mp': _safe_parallel(key_def.get('mp'), max_parallel),
            'ref_uris': [],
            'whisk_refs': {},
            'label': label,
        }

        if total_refs > 0:
            print(f"  Re-uploading {total_refs} reference(s) with {label} key...")
            if effective_mode == 'flow':
                ref_uris.clear()
                all_ref_files = refs['root'] + refs['subject'] + refs['scene'] + refs['style']
                ref_paths_flow[:] = list(all_ref_files[:10])
                for ref_file in tqdm(ref_paths_flow, desc=f"  [{label}] Re-upload", unit="file"):
                    try:
                        uri = client.upload_reference(str(ref_file))
                        ref_uris.append(uri)
                    except Exception as e:
                        logging.warning(f"[{label}] Re-upload failed {ref_file.name}: {e}")
                new_cfg['ref_uris'] = list(ref_uris)
                new_cfg['ref_paths_flow'] = list(ref_paths_flow)
            elif effective_mode == 'whisk':
                for cat in whisk_refs:
                    whisk_refs[cat].clear()
                for category in ('subject', 'scene', 'style'):
                    for ref_file in refs[category]:
                        try:
                            uri = client.upload_reference(str(ref_file))
                            whisk_refs[category].append(uri)
                        except Exception as e:
                            logging.warning(f"[{label}] Re-upload failed {ref_file.name}: {e}")
                new_cfg['whisk_refs'] = {k: list(v) for k, v in whisk_refs.items()}

        clients_cfg[:] = [new_cfg]
        max_parallel = new_cfg['mp']
        print(f"  {_GREEN}[OK]{_RESET} Switched to {label} key ({max_parallel} workers).")
        logging.warning(f"FastGen switched to {label} key after quota/key switch")
        return label

    # ------------------------------------------------------------------
    # Часовая квота FastGen выжжена на ВСЕХ ключах: остаток уходит на живой
    # VeoNonStop (Banana Pro) одним перемаппингом — не по клипу, а на уровне
    # провайдера. Живого VeoNonStop нет → не жжём его впустую, а ждём
    # обновления часовой квоты FastGen и продолжаем им же.
    # ------------------------------------------------------------------
    def _veo_fallback_enabled() -> bool:
        return bool(getattr(config, 'FASTGEN_QUOTA_FALLBACK_VEONONSTOP', True))

    t2v_fallback_required = False
    t2v_fallback_reason = ''

    def _harvest_generated(ids: List[int]) -> int:
        """Забрать в accumulated_ok клипы, чьи файлы появились в generated/.

        Источник правды — диск: картинки пишет чужой движок (imggen), и его
        собственная сводка нам не нужна.
        """
        moved = 0
        for cid in list(ids):
            for ext in ('.png', '.jpeg', '.jpg', '.webp'):
                p = output_dir / f"{cid:03d}{ext}"
                try:
                    exists = p.exists() and p.stat().st_size > 0
                except OSError:
                    exists = False
                if exists:
                    accumulated_ok[cid] = {
                        'id': cid, 'status': 'ok', 'file': str(p),
                        'operation_id': None, 'error': None, 'duration_sec': 0,
                        'rewritten': False, 'phase': 1,
                        'fallback_provider': 'veononstop',
                    }
                    pending_prompts.pop(cid, None)
                    moved += 1
                    break
        return moved

    def _delegate_to_veononstop(live_slots: List[Dict[str, Any]]) -> int:
        """Отдать ВЕСЬ остаток VeoNonStop Banana Pro одним вызовом imggen."""
        nonlocal t2v_fallback_required, t2v_fallback_reason
        _ids = sorted(pending_prompts.keys())
        if not _ids:
            return 0
        _slots = [s['slot'] for s in live_slots]
        # imggen читает рефы только из корня project/ref/ (подпапки subject/scene/
        # style — это формат Whisk у FastGen, Banana Pro их не увидит).
        _use_refs = bool(refs['root'])
        _veo_mode = 'banana_ref' if _use_refs else 'banana'
        _veo_aspect = FastGenClient._v6_map_aspect(used_aspect)
        _veo_cycles = max(1, int(getattr(config, 'VEONONSTOP_MAX_SILENT_CYCLES', 5) or 5))
        _veo_parallel = max(1, int(getattr(config, 'VEONONSTOP_MAX_PARALLEL', 12) or 12))
        _slot_text = ', '.join(f"{s['name']}({s['remaining']})" for s in live_slots)

        print(f"\n  {_GREEN}[VEO FALLBACK]{_RESET} Remapping {len(_ids)} remaining clip(s) "
              f"to VeoNonStop Banana Pro (mode={_veo_mode}, keys: {_slot_text})")
        if total_refs > 0 and not _use_refs:
            print(f"  {_YELLOW}[VEO FALLBACK]{_RESET} refs live in ref/ subfolders (whisk layout) — "
                  f"Banana Pro cannot use them, generating without refs")
            logging.warning("VEO fallback: refs only in ref/ subfolders — banana runs without refs")
        logging.warning(
            f"VEO fallback: delegating {len(_ids)} clips to imggen "
            f"(mode={_veo_mode}, model=GEM_PIX_2, keys={_slots}, "
            f"aspect={_veo_aspect}, parallel={_veo_parallel}, cycles={_veo_cycles})")

        try:
            from units.veo_stills import generate_all_images as _veo_generate
            _veo_result = _veo_generate(
                project_dir=str(project_path),
                mode=_veo_mode,
                max_parallel=_veo_parallel,
                clip_ids=_ids,
                aspect_ratio=_veo_aspect,
                ref_dir=str(project_path / 'ref') if _use_refs else None,
                silent=True,
                preflight=_use_refs,
                # max_cycles обязателен: без него imggen после своих циклов
                # позовёт FastGen обратно — получим кольцо провайдеров.
                max_cycles=_veo_cycles,
                active_keys=_slots,
                include_infographics=include_infographics,
                banana_model_key='GEM_PIX_2',   # Banana Pro
            )
            if _veo_result.get('t2v_fallback_required'):
                t2v_fallback_required = True
                t2v_fallback_reason = str(
                    _veo_result.get('t2v_fallback_reason')
                    or 'VeoNonStop ran out during FastGen fallback'
                )
        except Exception as exc:
            logging.exception(f"VEO fallback failed: {exc}")
            print(f"  {_RED}[VEO FALLBACK]{_RESET} failed: {str(exc)[:200]}")

        _moved = _harvest_generated(_ids)
        print(f"  {_GREEN}[VEO FALLBACK]{_RESET} VeoNonStop generated {_moved}/{len(_ids)} clip(s); "
              f"still missing: {len(pending_prompts)}")
        logging.warning(f"VEO fallback result: {_moved}/{len(_ids)} generated, "
                        f"{len(pending_prompts)} still missing")
        return _moved

    def _cooldown_until_quota(reason: str) -> bool:
        """Дождаться обновления часовой квоты FastGen. True — квота вернулась.

        Часовая квота обновляется на границе часа, поэтому первый подъём —
        в HH:01 следующего часа, дальше проверки каждые 5 минут. Сон
        прерываемый: кнопка Stop гасит ожидание.
        """
        _labels = ', '.join(cfg['label'] for cfg in clients_cfg) or 'Key?'
        _wait = seconds_until_next_hour(1)
        _wake_at = (datetime.now() + timedelta(seconds=_wait)).strftime('%H:%M')
        print(f"\n  {_YELLOW}[QUOTA COOLDOWN]{_RESET} {reason} — "
              f"waiting for FastGen hourly reset ({_labels})")
        print(f"  {_YELLOW}[QUOTA COOLDOWN]{_RESET} sleeping {int(_wait // 60)}m {int(_wait % 60)}s "
              f"→ wake at {_wake_at}, then re-check every 5m")
        logging.warning(f"FastGen quota cooldown: sleeping {int(_wait)}s until {_wake_at}")

        _check = 0
        while True:
            if _flowultra_sleep_interruptible(_wait):
                print(f"  {_YELLOW}[QUOTA COOLDOWN]{_RESET} interrupted by stop signal")
                logging.warning("FastGen quota cooldown interrupted by shutdown signal")
                return False
            _check += 1
            _restored = []
            for cfg in clients_cfg:
                try:
                    _rem = parse_usage_remaining(cfg['client'].get_usage())
                except Exception as exc:
                    logging.warning(f"[QUOTA COOLDOWN] {cfg['label']}: usage check failed — {exc}")
                    continue
                if _rem is None or _rem > 0:
                    _restored.append(f"{cfg['label']}:{'?' if _rem is None else _rem}")
            if _restored:
                print(f"  {_GREEN}[QUOTA COOLDOWN]{_RESET} quota restored ({', '.join(_restored)}) "
                      f"— resuming FastGen for {len(pending_prompts)} clip(s)")
                logging.warning(f"FastGen quota restored at cooldown check #{_check}: {_restored}")
                return True
            _wait = 5 * 60
            _next_at = (datetime.now() + timedelta(seconds=_wait)).strftime('%H:%M')
            print(f"  {_YELLOW}[QUOTA COOLDOWN]{_RESET} still empty at check #{_check} "
                  f"— next check at {_next_at}")
            logging.warning(f"FastGen quota still empty at cooldown check #{_check}")

    def _probe_live_veo_slots() -> List[Dict[str, Any]]:
        try:
            return live_veononstop_slots()
        except Exception as exc:
            logging.warning(f"VeoNonStop probe failed: {exc}")
            print(f"  {_YELLOW}[VEO CHECK]{_RESET} probe failed: {str(exc)[:160]}")
            return []

    def _fastgen_quota_available() -> bool:
        """Есть ли уже квота у текущих FastGen-ключей (без ожидания).

        Нужна после возврата из VeoNonStop: пока он молотил остаток, граница
        часа могла наступить — тогда ждать нечего, сразу продолжаем FastGen.
        """
        for cfg in clients_cfg:
            try:
                _rem = parse_usage_remaining(cfg['client'].get_usage())
            except Exception as exc:
                logging.warning(f"[QUOTA CHECK] {cfg['label']}: usage check failed — {exc}")
                continue
            if _rem is None or _rem > 0:
                logging.info(f"[QUOTA CHECK] {cfg['label']}: quota available "
                             f"({'?' if _rem is None else _rem})")
                return True
        return False

    def _resume_fastgen_or_wait(reason: str) -> str:
        """Вернуться на FastGen: сразу, если квота уже есть, иначе — после кулдауна."""
        if _fastgen_quota_available():
            _quota_dead_labels.clear()
            print(f"  {_GREEN}[QUOTA]{_RESET} FastGen quota already refreshed — "
                  f"resuming FastGen for {len(pending_prompts)} clip(s)")
            logging.warning(f"FastGen quota refreshed already; "
                            f"resuming for {len(pending_prompts)} clips")
            return '1'
        if _cooldown_until_quota(reason):
            # Часовая квота обновилась — снимаем метки «выжжен», иначе следующий
            # виток не увидит ни одного живого FastGen-ключа.
            _quota_dead_labels.clear()
            return '1'
        return '2'

    def _handle_quota_dead_end() -> str:
        """Квота FastGen выжжена, других ключей нет. Возвращает choice цикла:
        '1' — продолжаем FastGen (квота уже вернулась либо вернулась после кулдауна);
        '2' — этап закончен (остаток добит VeoNonStop либо стоп по сигналу).
        """
        if not allow_veo_fallback:
            # Нас позвал imggen: его ключи VeoNonStop уже выжжены, обратно звать
            # некого. Но час FastGen придёт — ждём его и добиваем остаток.
            print(f"  {_YELLOW}[QUOTA]{_RESET} FastGen quota exhausted; VeoNonStop already "
                  f"exhausted (we are its fallback) — waiting for FastGen hourly reset")
            logging.warning("FastGen quota exhausted while running as imggen fallback — "
                            "cooldown instead of calling VeoNonStop back")
            return _resume_fastgen_or_wait('VeoNonStop already exhausted (imggen fallback)')

        if not _veo_fallback_enabled():
            print(f"  [AUTO] FastGen quota exhausted - VeoNonStop fallback disabled in settings")
            logging.error("AUTO: FastGen quota exhausted; VeoNonStop fallback disabled "
                          "(FASTGEN_QUOTA_FALLBACK_VEONONSTOP=off)")
            return '2'

        print(f"\n  {_YELLOW}[QUOTA]{_RESET} FastGen quota exhausted on all keys — "
              f"probing VeoNonStop keys for live image quota...")
        logging.warning("FastGen quota exhausted on all keys — probing VeoNonStop slots")
        _live = _probe_live_veo_slots()
        if _live:
            _delegate_to_veononstop(_live)
            if not pending_prompts:
                return '2'
            if t2v_fallback_required:
                print(f"  {_YELLOW}[T2V FALLBACK]{_RESET} {t2v_fallback_reason}; "
                      f"leaving {len(pending_prompts)} clip(s) for T2V")
                logging.warning(
                    "Veo fallback requested T2V for %d clip(s): %s",
                    len(pending_prompts),
                    t2v_fallback_reason,
                )
                return '2'
            # VeoNonStop выдохся посреди остатка (его квота тоже конечна) —
            # возвращаемся на FastGen: если его час уже обновился, догенерим им,
            # иначе ждём границы часа и продолжаем FastGen'ом.
            print(f"  {_YELLOW}[VEO FALLBACK]{_RESET} VeoNonStop ran out with "
                  f"{len(pending_prompts)} clip(s) left — switching back to FastGen")
            logging.warning(f"VeoNonStop exhausted mid-fallback, {len(pending_prompts)} clips "
                            f"left — switching back to FastGen")
            return _resume_fastgen_or_wait('VeoNonStop ran out mid-fallback')

        return _resume_fastgen_or_wait('No VeoNonStop key has image quota')

    while True:
        cycle_num += 1
        cycle_ids = sorted(pending_prompts.keys())
        print(f"\n  [Cycle {cycle_num}] Generating {len(cycle_ids)} images... (model={used_model})")

        # Перезагрузка рефов если прошло ≥50 минут с последней загрузки (до старта цикла)
        if total_refs > 0 and (time.time() - refs_uploaded_at) >= REF_REFRESH_INTERVAL:
            refs_uploaded_at = _do_ref_refresh()

        cycle_results = []
        quota_exhausted = False  # сбрасываем в начале каждого цикла
        quota_exhausted_labels = set()
        license_dead_labels = set()
        interrupted = False  # флаг KeyboardInterrupt
        ref_refresh_needed = False  # флаг досрочного рефреша внутри цикла
        stop_event = threading.Event()  # сигнал воркерам: прекратить попытки между итерациями

        # Сброс worker tracking для нового цикла
        with _fg_worker_lock:
            _fg_worker_map.clear()
            _fg_worker_counter['next'] = 0
        with _fg_started_workers_lock:
            _fg_started_workers.clear()

        try:
            with ThreadPoolExecutor(max_workers=max_parallel) as executor:
                futures = {}
                for i, cid in enumerate(cycle_ids):
                    if len(clients_cfg) > 1:
                        # Dual-key: worker-assignment внутри generate_single_clip
                        future = executor.submit(
                            _generate_claimed,
                            client=None,
                            clip_id=cid,
                            prompt=pending_prompts[cid],
                            ref_uris=None,
                            mode=effective_mode,
                            whisk_refs=None,
                            output_dir=output_dir,
                            model=used_model,
                            aspect_ratio=used_aspect,
                            stop_event=stop_event,
                            worker_id_fn=_get_fg_worker_id,
                            clients_cfg_list=clients_cfg,
                            on_ref_blocked=_on_ref_blocked if effective_mode == 'flow' else None,
                        )
                    else:
                        # Single-key: классический режим
                        future = executor.submit(
                            _generate_claimed,
                            client=client,
                            clip_id=cid,
                            prompt=pending_prompts[cid],
                            ref_uris=clients_cfg[0].get('ref_uris') if effective_mode == 'flow' else None,
                            mode=effective_mode,
                            whisk_refs=clients_cfg[0].get('whisk_refs') if effective_mode == 'whisk' else None,
                            output_dir=output_dir,
                            model=used_model,
                            aspect_ratio=used_aspect,
                            stop_event=stop_event,
                            stagger_delay=i * 2.0 if i < max_parallel else 0.0,
                            on_ref_blocked=_on_ref_blocked if effective_mode == 'flow' else None,
                            cfg_ref=clients_cfg[0] if effective_mode == 'flow' else None,
                        )
                    futures[future] = cid

                _cycle_ok_durs = []
                _cycle_total = len(futures)
                _cycle_done = 0
                _wall_start = time.time()
                if IS_PIPE:
                    print(f"  Cycle {cycle_num}: 0/{_cycle_total} clips", flush=True)
                _tqdm_ctx = tqdm(total=_cycle_total, desc=f"  Cycle {cycle_num}", unit="clip", smoothing=0) if not IS_PIPE else None
                for future in as_completed(futures):
                    cid = futures[future]
                    try:
                        clip_result = future.result()
                        cycle_results.append(clip_result)
                        status_icon = {
                            'ok': '+',
                            'claimed_elsewhere': '=',
                            'censored': 'C',
                            'error': 'X',
                        }.get(
                            clip_result['status'], '?')
                        dur = clip_result.get('duration_sec', 0)
                        if clip_result['status'] == 'ok' and dur > 0:
                            _cycle_ok_durs.append(dur)
                        if _tqdm_ctx:
                            avg_s = f" avg={sum(_cycle_ok_durs)/len(_cycle_ok_durs):.0f}s" if _cycle_ok_durs else ""
                            _tqdm_ctx.set_postfix_str(f"#{cid}: {status_icon}{avg_s}")
                        if clip_result['status'] == 'ok' and _cycle_ok_durs:
                            _avg = sum(_cycle_ok_durs) / len(_cycle_ok_durs)
                            _wall = time.time() - _wall_start
                            _wall_cpm = _cycle_done / (_wall / 60) if _wall > 0 else 0
                            _wall_spc = 60.0 / _wall_cpm if _wall_cpm > 0 else 0
                            print(f"  {_CYAN}[SPEED]{_RESET} worker avg={_avg:.0f}s/clip  |  pool {_wall_spc:.1f}s/clip ({_wall_cpm:.1f} clip/min)")
                        if clip_result.get('license_dead'):
                            dead_label = (clip_result.get('key_label')
                                          or (clients_cfg[0].get('label') if clients_cfg else 'Key?'))
                            first_dead = dead_label not in license_dead_labels
                            license_dead_labels.add(dead_label)
                            stop_event.set()
                            if first_dead:
                                print(f"\n  {_RED}[AUTH FATAL]{_RESET} {dead_label} license/key is dead; "
                                      "stopping this cycle for key failover")
                                logging.error(
                                    f"[AUTH FATAL] {dead_label} license/key is dead; "
                                    "stopping cycle for key failover")
                    except QuotaExhaustedError as e:
                        quota_label = getattr(e, 'key_label', None) or (clients_cfg[0].get('label') if clients_cfg else 'Key?')
                        quota_exhausted_labels.add(quota_label)
                        if not quota_exhausted:
                            msg = f"FastGen quota exhausted in cycle {cycle_num} (model={used_model}, key={quota_label}); no more submits on this key"
                            print(f"  {_YELLOW}[QUOTA]{_RESET} {msg}")
                            logging.warning(msg)
                            # Квота — состояние ПРОВАЙДЕРА, не одного клипа: гасим
                            # новые попытки у всех воркеров сразу (как при license_dead),
                            # иначе остаток цикла молотит 429 до последнего клипа,
                            # хотя решение уже принято на уровне ключа.
                            if len(clients_cfg) <= 1:
                                stop_event.set()
                                print(f"  {_YELLOW}[QUOTA]{_RESET} stop_event set — "
                                      f"workers finish current tasks, no new attempts")
                                logging.warning("FastGen quota: stop_event set for provider-level switch")
                        cycle_results.append({
                            'id': cid, 'status': 'error', 'error': f'quota_exhausted:{quota_label}',
                            'file': None, 'operation_id': None, 'duration_sec': 0,
                            'key_label': quota_label,
                        })
                        quota_exhausted = True
                        if _tqdm_ctx:
                            _tqdm_ctx.set_postfix_str(f"#{cid}: QUOTA!")
                    except Exception as e:
                        err_msg = f"Clip #{cid} worker exception in cycle {cycle_num} (model={used_model}): {e}"
                        print(f"  {_YELLOW}[ERROR]{_RESET} {err_msg}")
                        logging.exception(err_msg)
                        cycle_results.append({
                            'id': cid, 'status': 'error', 'error': str(e),
                            'file': None, 'operation_id': None, 'duration_sec': 0,
                        })
                        if _tqdm_ctx:
                            _tqdm_ctx.set_postfix_str(f"#{cid}: X")
                    _cycle_done += 1
                    if _tqdm_ctx:
                        _tqdm_ctx.update(1)
                    if IS_PIPE:
                        print(f"PROGRESS:{_cycle_done}/{_cycle_total}", flush=True)

                    # Проверяем TTL рефов после каждого завершённого клипа
                    if total_refs > 0 and not ref_refresh_needed and (time.time() - refs_uploaded_at) >= REF_REFRESH_INTERVAL:
                        ref_refresh_needed = True
                        stop_event.set()
                        msg = f"\n  {_YELLOW}[REFS TTL]{_RESET} 50m elapsed — stopping retries and refreshing refs..."
                        if _tqdm_ctx:
                            _tqdm_ctx.write(msg)
                        else:
                            print(msg, flush=True)
                        logging.info("Ref TTL reached — stop_event set, waiting for current operations to finish")
                if _tqdm_ctx:
                    _tqdm_ctx.close()

        except KeyboardInterrupt:
            ctrlc_count += 1
            stop_event.set()  # останавливаем воркеры между попытками при любом Ctrl+C
            if ctrlc_count < 3:
                print(f"\n\n  {_YELLOW}[Ctrl+C #{ctrlc_count}]{_RESET} Press Ctrl+C {3 - ctrlc_count} more time(s) to stop. Continuing...")
                logging.info(f"KeyboardInterrupt #{ctrlc_count} ignored — stop_event set, waiting for workers...")
            else:
                interrupted = True
                print(f"\n\n  {_YELLOW}[INTERRUPTED]{_RESET} Ctrl+C x3 — saving results and finishing gracefully...")
                logging.warning("KeyboardInterrupt x3 caught — finishing with partial results")

        # Разбираем результаты цикла: перемещаем OK в накопитель
        for r in cycle_results:
            if r['status'] in ('ok', 'claimed_elsewhere'):
                if r['status'] == 'ok':
                    accumulated_ok[r['id']] = r
                pending_prompts.pop(r['id'], None)

        cycle_ok_new = sum(1 for r in cycle_results if r['status'] == 'ok')
        failed_ids = sorted(pending_prompts.keys())
        _quota_continue_with_remaining_keys = False
        _pending_quota_switch_def = None

        if license_dead_labels and failed_ids:
            _quota_dead_labels.update(license_dead_labels)
            alive_cfgs = [
                cfg for cfg in clients_cfg
                if cfg.get('label') not in _quota_dead_labels
            ]
            if alive_cfgs:
                clients_cfg[:] = alive_cfgs
                max_parallel = sum(_safe_parallel(cfg.get('mp'), 1) for cfg in clients_cfg)
                client = clients_cfg[0]['client']
                ref_uris.clear()
                ref_uris.extend(clients_cfg[0].get('ref_uris', []))
                remaining = ', '.join(
                    f"{cfg['label']}:{cfg['mp']}" for cfg in clients_cfg)
                print(f"  {_YELLOW}[AUTH FAILOVER]{_RESET} Disabled dead key(s): "
                      f"{', '.join(sorted(license_dead_labels))}; remaining: {remaining}")
                logging.warning(
                    f"FastGen auth failover: disabled {sorted(license_dead_labels)}; "
                    f"remaining {remaining}")
                continue

            switch_def = _next_fastgen_key_def(skip_dead=True)
            if switch_def:
                old_labels = ', '.join(sorted(license_dead_labels))
                new_label = _switch_to_fastgen_key(switch_def)
                print(f"  {_GREEN}[AUTH FAILOVER]{_RESET} {old_labels} -> {new_label}; "
                      f"retrying {len(failed_ids)} clip(s)")
                logging.warning(
                    f"FastGen auth failover: {old_labels} -> {new_label}; "
                    f"retrying {len(failed_ids)} clips")
                continue

            print(f"  {_RED}[AUTH FATAL]{_RESET} No other configured FastGen key is available.")
            logging.error(
                f"FastGen auth failover failed: dead={sorted(license_dead_labels)}, "
                "no other configured key")
            break

        if quota_exhausted_labels:
            _quota_dead_labels.update(quota_exhausted_labels)
            if len(clients_cfg) > 1:
                alive_cfgs = [cfg for cfg in clients_cfg if cfg.get('label') not in _quota_dead_labels]
                if alive_cfgs:
                    disabled = ', '.join(sorted(quota_exhausted_labels))
                    clients_cfg[:] = alive_cfgs
                    max_parallel = sum(_safe_parallel(cfg.get('mp'), 1) for cfg in clients_cfg)
                    client = clients_cfg[0]['client']
                    ref_uris.clear()
                    ref_uris.extend(clients_cfg[0].get('ref_uris', []))
                    remaining = ', '.join(f"{cfg['label']}:{cfg['mp']}" for cfg in clients_cfg)
                    print(f"  {_YELLOW}[QUOTA]{_RESET} Disabled exhausted FastGen key(s): {disabled}; remaining: {remaining}")
                    logging.warning(f"FastGen quota: disabled key(s) {disabled}; remaining {remaining}")
                    _quota_continue_with_remaining_keys = True

            if not _quota_continue_with_remaining_keys:
                _pending_quota_switch_def = _next_fastgen_key_def(skip_dead=True)

        print(f"\n  [Cycle {cycle_num}] OK this cycle: {cycle_ok_new},  still missing: {len(failed_ids)}")

        # Прерывание по Ctrl+C x3 — выходим с тем что есть
        if interrupted:
            print(f"  Stopped by user. {len(accumulated_ok)} clips saved to generated/")
            break

        # Если TTL истёк во время цикла — обновляем рефы и сразу повторяем без вопросов
        if ref_refresh_needed and failed_ids:
            refs_uploaded_at = _do_ref_refresh()
            print(f"  Refs refreshed — restarting for {len(failed_ids)} remaining clips...")
            continue

        # Все сгенерированы — выходим
        if not failed_ids:
            break

        # ------------------------------------------------------------------
        # Интерактивный финал: есть проваленные клипы
        # ------------------------------------------------------------------
        print("\n" + "=" * 60)
        print(f"  {_RED}MISSING CLIPS ({len(failed_ids)} total):{_RESET}")
        print("=" * 60)

        # Вывод промптов отсутствующих клипов
        for cid in failed_ids:
            last_r = next((r for r in reversed(cycle_results) if r['id'] == cid), None)
            status_label = 'CENSORED' if (last_r and last_r.get('status') == 'censored') else 'FAILED'
            print(f"\n  {'─' * 56}")
            print(f"  {_RED}Clip #{cid} [{status_label}]{_RESET}")
            print(f"  Prompt:")
            prompt_text = pending_prompts[cid]
            # Перенос строк по 72 символа для читаемости
            words = prompt_text.split()
            line = "    "
            for word in words:
                if len(line) + len(word) + 1 > 76:
                    print(line)
                    line = "    " + word
                else:
                    line = (line + " " + word) if line != "    " else "    " + word
            if line.strip():
                print(line)

        print(f"\n  {'─' * 56}")
        print(f"\n  {_YELLOW}What would you like to do?{_RESET}")
        print(f"    [1] Repeat full cycle for missing clips ({len(failed_ids)} clips)")
        print(f"    [2] Finish — use prompts above for manual generation in Veo3")

        # Переключение ключа — по всем заполненным FastGen слотам Key1..Key4.
        _manual_switch_def = _pending_quota_switch_def or _next_fastgen_key_def(skip_dead=False)
        can_switch_key = bool(_manual_switch_def)
        if can_switch_key:
            print(f"    [3] Switch to {_manual_switch_def['label']} API key and repeat ({len(failed_ids)} clips)")

        print(f"    [4] Edit prompt manually and retry")

        _veo_manual_ok = allow_veo_fallback and _veo_fallback_enabled()
        if _veo_manual_ok:
            print(f"    [5] Send remaining {len(failed_ids)} clip(s) to VeoNonStop (Banana Pro)")

        if quota_exhausted and not (_quota_continue_with_remaining_keys or _pending_quota_switch_def):
            print(f"  {_YELLOW}  [!] Hourly quota exhausted. No other FastGen API key in settings/.env.{_RESET}")
        print()

        valid_choices = ({'1', '2', '4'}
                         | ({'3'} if can_switch_key else set())
                         | ({'5'} if _veo_manual_ok else set()))
        if silent or not sys.stdin.isatty():
            # ── Cycles 1-2: standard FastGen retry (same model) ──
            if quota_exhausted:
                if _quota_continue_with_remaining_keys:
                    choice = '1'
                    remaining = ', '.join(cfg['label'] for cfg in clients_cfg)
                    print(f"  [AUTO] FastGen quota exhausted - continuing with remaining key(s): {remaining}")
                    logging.warning(f"AUTO: FastGen quota exhausted, continuing with remaining key(s): {remaining}")
                elif _pending_quota_switch_def:
                    choice = '3'
                    target_key = _pending_quota_switch_def['label']
                    print(f"  [AUTO] FastGen quota exhausted - switching to {target_key} key")
                    logging.warning(f"AUTO: FastGen quota exhausted, switching to {target_key} key")
                else:
                    # Провайдер-уровневая развилка: живой VeoNonStop забирает весь
                    # остаток, иначе ждём обновления часовой квоты FastGen.
                    choice = _handle_quota_dead_end()
            elif cycle_num <= 2:
                choice = '1'
                print(f"  [AUTO] Non-interactive: retry same model (cycle {cycle_num})")

            # ── Cycle 3+: switch through FastGen fallback model chain ──
            elif _fallback_model_index < len(_fallback_fg_models):
                _prev_model = used_model
                _next_model = _fallback_fg_models[_fallback_model_index]
                _fallback_model_index += 1
                print(f"\n  {_YELLOW}[AUTO FALLBACK]{_RESET} FastGen: switching model {_prev_model} → {_next_model} (service may be down)")
                logging.warning(f"AUTO FALLBACK: FastGen model switch {_prev_model} → {_next_model}")
                used_model = _next_model
                choice = '1'

            # After FastGen fallback chain, stop. Do not silently spend VeoNonStop keys:
            # автоматический переход на VeoNonStop зарезервирован за исчерпанием КВОТЫ
            # (там FastGen физически не может продолжать), а не за отказами моделей.
            else:
                choice = '2'
                print(f"\n  {_RED}[AUTO FALLBACK]{_RESET} FastGen fallback models exhausted - {len(pending_prompts)} clips still missing.")
                print(f"  {_RED}[AUTO FALLBACK]{_RESET} Stopping here; auto-switch to VeoNonStop covers quota exhaustion only.")
                logging.error(f"AUTO FALLBACK: FastGen fallback models exhausted, {len(pending_prompts)} clips still missing; VeoNonStop auto-switch is quota-only")
                break
        else:
            while True:
                prompt_str = f"  Enter choice [{'/'.join(sorted(valid_choices))}]: "
                try:
                    choice = input(prompt_str).strip()
                except (EOFError, KeyboardInterrupt):
                    choice = '2'
                    print("2")
                if choice in valid_choices:
                    break
                print(f"  Invalid choice, enter {'/'.join(sorted(valid_choices))}")

        if choice == '1':
            print(f"\n  Repeating for clips: {', '.join(f'#{i}' for i in failed_ids)}")
        elif choice == '2':
            print(f"\n  Finishing. Prompts above — copy to Veo3 for manual generation.")
            break
        elif choice == '3':
            # Переключение на следующий доступный FastGen-слот.
            switch_def = _pending_quota_switch_def or _manual_switch_def
            if not switch_def:
                print(f"  {_YELLOW}[WARN]{_RESET} No other FastGen key available; stopping.")
                break
            key_label = _switch_to_fastgen_key(switch_def)
            _pending_quota_switch_def = None
            print(f"  {_GREEN}[OK]{_RESET} Repeating {len(failed_ids)} clips with {key_label}...")
        elif choice == '4':
            # Ручной ввод промпта для каждого пропущенного клипа
            for cid in list(failed_ids):
                old_prompt = pending_prompts.get(cid, '')
                print(f"\n  {_YELLOW}--- Clip #{cid:03d} ---{_RESET}")
                print(f"  Current prompt:")
                print(f"    {old_prompt[:200]}")
                print()
                new_text = input("  New prompt (Enter = keep current, 's' = skip this clip): ").strip()
                if new_text.lower() == 's':
                    failed_ids.remove(cid)
                    pending_prompts.pop(cid, None)
                    print(f"  #{cid:03d} skipped")
                elif new_text:
                    pending_prompts[cid] = new_text
                    print(f"  #{cid:03d} prompt updated")
                else:
                    print(f"  #{cid:03d} keeping current prompt")
            if not failed_ids:
                break
        elif choice == '5':
            # Ручной провайдер-уровневый перевод остатка на VeoNonStop Banana Pro.
            _live_manual = _probe_live_veo_slots()
            if _live_manual:
                _delegate_to_veononstop(_live_manual)
                break
            print(f"  {_YELLOW}[VEO]{_RESET} No VeoNonStop key has image quota left — "
                  f"repeating on FastGen instead.")
        # choice '1', '3', '4' или '5'-без-квоты → продолжаем цикл

    elapsed_total = round(time.time() - start_total, 1)

    # ------------------------------------------------------------------
    # 6. Итоги
    # ------------------------------------------------------------------
    # Финальный список: ok из накопителя + failed из pending (результат последнего цикла)
    all_results = list(accumulated_ok.values())
    for cid in sorted(pending_prompts.keys()):
        last_r = next((r for r in reversed(cycle_results) if r['id'] == cid), None)
        if last_r:
            all_results.append(last_r)
        else:
            all_results.append({
                'id': cid, 'status': 'error', 'error': 'not attempted',
                'file': None, 'operation_id': None, 'duration_sec': 0,
            })
    all_results.sort(key=lambda r: r['id'])

    ok_count = sum(1 for r in all_results if r['status'] == 'ok')
    err_count = sum(1 for r in all_results if r['status'] == 'error')
    cen_count = sum(1 for r in all_results if r['status'] == 'censored')

    print("\n" + "=" * 60)
    print(f"  FINAL RESULTS:")
    print(f"    OK:       {ok_count}")
    if cen_count:
        print(f"    Censored: {cen_count}")
    if err_count:
        print(f"    Errors:   {err_count}")
    print(f"    Total:    {len(all_results)}")
    print(f"    Time:     {elapsed_total}s ({cycle_num} cycle(s))")
    print("=" * 60)

    # ------------------------------------------------------------------
    # 7. Сохранение лога
    # ------------------------------------------------------------------
    log_data = {
        'timestamp': datetime.now().isoformat(),
        'model': used_model,
        'aspect_ratio': used_aspect,
        'reference_mode': effective_mode if total_refs > 0 else None,
        'references_used': total_refs,
        'total_clips': len(all_prompts),
        'generated': ok_count,
        'censored': cen_count,
        'failed': err_count,
        'failed_ids': sorted(
            int(r['id']) for r in all_results
            if r.get('status') in ('error', 'censored')
        ),
        'cycles': cycle_num,
        'duration_sec': elapsed_total,
        'clips': all_results,
        't2v_fallback_required': t2v_fallback_required,
        't2v_fallback_reason': t2v_fallback_reason,
    }

    log_path = project_path / 'generation_log.json'
    with open(log_path, 'w', encoding='utf-8') as f:
        json.dump(log_data, f, ensure_ascii=False, indent=2)

    logging.info(f"=== Generation done: {ok_count} OK, {err_count} errors, {cen_count} censored, {cycle_num} cycle(s) ===")
    logging.getLogger().removeHandler(file_handler)
    file_handler.close()

    print(f"\n  Log: {log_path}")
    print(f"  Detailed log: {gen_log_path}")

    return log_data
