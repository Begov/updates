"""
VeoNonStop API Client — видеогенерация и генерация изображений.

Эндпоинты:
  Видео:
    POST /video/image-to-video     — оживление картинки
    POST /video/text-to-video      — видео из текста
    POST /video/multi-image-to-video — видео из текста + несколько референсных картинок
  Картинки:
    POST /image/generate           — Imagen 3.5 (text-to-image)
    POST /image/banana/generate    — Banana Pro (text-to-image + optional style refs)
    POST /image/banana/upscale     — Upscale до 4K
  Статус/Download:
    GET  /video/status/:taskId   — video and Banana image task status
    GET  /video/download/:taskId?video_index=0

Документация: https://veononstop.org/api-docs.html
"""

import os
import shutil
import sys
import base64
import json
import subprocess
import re
import threading
import time
import logging
import requests
try:  # файл появляется под своим именем только целиком (см. units/media_io.py)
    from units.media_io import atomic_open, atomic_target, write_bytes_atomic
except ImportError:                                               # pragma: no cover
    from media_io import atomic_open, atomic_target, write_bytes_atomic
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List, Any


def _verify_video_integrity(path: Path) -> bool:
    """Быстрая проверка скачанного видеофайла через ffprobe.
    Возвращает True если файл валиден (есть видео-стрим и положительная длительность).
    Нужно потому что download может завершиться "успешно" (write без exception),
    но payload оказаться truncated/corrupt — VEO cloud-relay иногда такое отдаёт.
    """
    ffprobe = (
        os.environ.get('BEGO_FFPROBE')
        or shutil.which('ffprobe')
    )
    if not ffprobe:
        for candidate in (
            Path('/opt/homebrew/bin/ffprobe'),
            Path('/usr/local/bin/ffprobe'),
        ):
            if candidate.is_file():
                ffprobe = str(candidate)
                break
    if not ffprobe:
        raise FileNotFoundError(
            'ffprobe is unavailable; the macOS runtime installer must run '
            'before video integrity checks'
        )

    try:
        if not path.exists() or path.stat().st_size < 1024:
            return False
        res = subprocess.run(
            [ffprobe, '-v', 'error',
             '-select_streams', 'v:0',
             '-show_entries', 'stream=codec_type:format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1',
             str(path)],
            capture_output=True, text=True, timeout=15,
        )
        out = (res.stdout or '').strip().splitlines()
        # Ожидаем как минимум codec_type=video и duration > 0
        has_video = any(line.strip() == 'video' for line in out)
        dur = 0.0
        for line in out:
            line = line.strip()
            try:
                dur = max(dur, float(line))
            except ValueError:
                continue
        valid = res.returncode == 0 and has_video and dur > 0.0
        if not valid:
            logging.warning(
                "ffprobe rejected %s (size=%s, rc=%s): %s",
                path,
                path.stat().st_size,
                res.returncode,
                (res.stderr or '')[-500:],
            )
        return valid
    except FileNotFoundError:
        raise
    except Exception as exc:
        logging.warning("ffprobe integrity check failed for %s: %s", path, exc)
        return False

# Ensure agent/ dir is on sys.path (config.py lives there)
_agent_dir = str(Path(__file__).resolve().parent.parent)
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

import params as config
# Поддерживаемые форматы изображений
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp'}

# MIME-типы по расширению
MIME_TYPES = {
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.webp': 'image/webp',
}


class BananaResult(str):
    """str subclass carrying upscale metadata. str(result) == path."""
    def __new__(cls, path: str, media_id: str = '', project_id: str = ''):
        obj = str.__new__(cls, path)
        obj.media_id = media_id
        obj.project_id = project_id
        return obj


class VeoNonStopError(Exception):
    """Базовая ошибка VeoNonStop API"""
    pass


class VeoNonStopTimeoutError(VeoNonStopError):
    """Таймаут ожидания задачи"""
    pass


class VeoNonStopGenerationError(VeoNonStopError):
    """Задача завершилась с ошибкой"""
    pass


class VeoNonStopRateLimitError(VeoNonStopError):
    """429 — превышен лимит запросов"""
    pass


class VeoNonStopTokenExpiredError(VeoNonStopError):
    """TOKEN_EXPIRED — временная ошибка авторизации на стороне сервера"""
    pass


class VeoNonStopHungError(VeoNonStopError):
    """Task stuck in session_init/uploading > hung_timeout seconds"""
    pass


def _image_to_base64(image_path: str) -> tuple:
    """
    Читает изображение и возвращает (base64_string, mime_type).
    API поддерживает JPEG, PNG, WebP — отправляем в оригинальном формате
    без конвертации, чтобы минимизировать размер тела запроса.
    (JPEG ~1MB vs PNG ~5-8MB → write timeout при больших PNG)
    """
    path = Path(image_path)
    if not path.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")

    mime = MIME_TYPES.get(path.suffix.lower(), 'image/png')
    with open(path, 'rb') as f:
        b64 = base64.b64encode(f.read()).decode('utf-8')
    return b64, mime


def _load_ref_images(ref_dir: str) -> List[Dict[str, str]]:
    """
    Загружает все изображения из папки ref/ как массив
    [{image_base64, mime_type}, ...] для multi-image-to-video и banana.
    Форматы не конвертируются — отправляются как есть (JPEG, PNG, WebP).
    """
    ref_path = Path(ref_dir)
    if not ref_path.exists():
        return []

    images = []
    for f in sorted(ref_path.iterdir(), key=lambda p: p.name.lower()):
        if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS:
            b64, mime = _image_to_base64(str(f))
            images.append({'name': _api_name(f.stem, len(images) + 1),
                           'image_base64': b64, 'mime_type': mime})
    return images


class ApiError:
    """Разобранная ошибка VeoNonStop — в одном виде для обеих версий API.

    Зачем понадобилось. Сервис отвечает ошибкой в РАЗНЫХ формах:

      • строка:  {"success": false, "error": "prompt is required"}
      • объект:  {"success": false, "error": {"code": "ADMISSION_SERVICE_UNAVAILABLE",
                                              "retryable": true}}
      • вложенный текст от Google внутри строки:
                 {"success": false, "error": "BAD_REQUEST: {\\"error\\": {...}}"}
      • не-JSON вовсе (HTML-страница, обрыв связи)

    Всё это проверено живыми запросами 01.09.2026 на обеих версиях. Раньше код
    искал подстроки `TOKEN_EXPIRED` / `RATE_LIMIT` прямо в сыром тексте, а в
    сообщение исключения клал `resp.text` целиком: причина в логе была, но
    признак «можно повторить» (`retryable`) терялся, а длинные ответы забивали
    строку.
    """

    __slots__ = ('http', 'code', 'message', 'retryable', 'raw')

    def __init__(self, http: int, code: str, message: str, retryable: Optional[bool], raw: str):
        self.http = http
        self.code = code
        self.message = message
        self.retryable = retryable
        self.raw = raw

    def __str__(self) -> str:
        parts = [f"HTTP {self.http}"]
        if self.code:
            parts.append(self.code)
        if self.message and self.message != self.code:
            parts.append(self.message)
        if self.retryable is True:
            parts.append('можно повторить')
        elif self.retryable is False:
            parts.append('повтор бесполезен')
        return ' | '.join(parts)

    @property
    def is_retryable(self) -> bool:
        """Сервис явно сказал «повторяемо», либо это временная беда сети."""
        if self.retryable is not None:
            return bool(self.retryable)
        return self.http in (429, 500, 502, 503, 504)


def _error_dump_path() -> Optional[Path]:
    """Куда складывать сырые ответы с ошибками (для отладки).

    Файл общий на прогон: `agent/logs/veononstop_errors_ГГГГММДД.log`. Пишем
    туда ПОЛНЫЙ ответ — в обычный лог он не влезает и режется, а разбираться
    потом приходится именно по нему.
    """
    try:
        log_dir = Path(__file__).resolve().parent.parent / 'logs'
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir / f"veononstop_errors_{datetime.now():%Y%m%d}.log"
    except OSError:
        return None


def parse_api_error(resp, context: str = '') -> ApiError:
    """Разобрать ответ с ошибкой, записать его в лог и в файл отладки."""
    raw = ''
    code = ''
    message = ''
    retryable: Optional[bool] = None
    try:
        raw = resp.text or ''
    except Exception:                                              # noqa: BLE001
        raw = ''
    try:
        body = resp.json()
    except Exception:                                              # noqa: BLE001
        body = None

    if isinstance(body, dict):
        err = body.get('error', body.get('message'))
        if isinstance(err, dict):
            code = str(err.get('code') or err.get('status') or '').strip()
            message = str(err.get('message') or '').strip()
            if 'retryable' in err:
                retryable = bool(err.get('retryable'))
            # Google кладёт свою ошибку ещё на уровень глубже
            inner = err.get('error')
            if isinstance(inner, dict):
                code = code or str(inner.get('status') or inner.get('code') or '')
                message = message or str(inner.get('message') or '')
        elif err is not None:
            message = str(err).strip()
            # Сервис нередко кладёт КОД прямо в строку: «IMAGE_DAILY_LIMIT_EXCEEDED»,
            # «BAD_REQUEST: {...}». Опознаём по виду: ВЕРХНИЙ_РЕГИСТР_С_ПОДЧЁРКИВАНИЯМИ
            # в начале строки. Без этого код терялся, а в логе оставался сырой текст.
            head = message.split(':', 1)[0].strip()
            if head and head.replace('_', '').isalnum() and head.upper() == head \
                    and len(head) >= 4 and not head.isdigit():
                code = head
        if not code:
            # `error_code` — отдельное поле верхнего уровня (так сервис шлёт
            # RATE_LIMIT_EXCEEDED: {"success": false, "error": "...", "error_code": ...})
            code = str(body.get('code') or body.get('error_code') or '').strip()

    http = int(getattr(resp, 'status_code', 0) or 0)
    if not message:
        message = (raw or '').strip()[:300]
    # Если код уже есть, а «сообщение» — это просто копия сырого ответа, второй
    # раз его не показываем: полный текст всё равно уходит в файл отладки.
    if code and message and (message == raw.strip() or message.startswith('{')):
        message = ''
    error = ApiError(http, code, message, retryable, raw)

    where = f" [{context}]" if context else ''
    logging.warning("VeoNonStop%s: %s", where, error)
    dump = _error_dump_path()
    if dump and raw:
        try:
            with open(dump, 'a', encoding='utf-8', errors='replace') as fh:
                fh.write(f"\n{'=' * 70}\n[{datetime.now():%Y-%m-%d %H:%M:%S}]"
                         f"{where} HTTP {http}\n{raw}\n")
        except OSError as exc:
            logging.debug("не удалось записать дамп ошибки: %s", exc)
    return error


def duration_for_scene(seconds: float) -> str:
    """Длительность клипа под сцену — БЛИЖАЙШАЯ СВЕРХУ из 4/5/6/8 секунд.

    | длина сцены          | запрашиваем |
    |----------------------|-------------|
    | до 4 с включительно  | 4 с         |
    | от 4 до 5 с          | 5 с         |
    | от 5 до 6 с          | 6 с         |
    | больше 6 с           | 8 с (потолок сервиса) |

    Ряд 4/5/6/8 — по актуальной документации сервиса (image-to-video).

    Почему сверху, а не снизу: короткий клип пришлось бы растягивать под
    сцену, а приходит он с 24 кадрами в секунду — замедление на таком
    материале выглядит плохо. Лишний хвост подрезает склейка.

    Работает и для дробных длительностей: берём первое значение ряда, которое
    НЕ МЕНЬШЕ длины сцены, поэтому 6.4 с уходит в восьмёрку, а ровно 6.0 — в
    шестёрку.
    """
    try:
        need = float(seconds)
    except (TypeError, ValueError):
        return '8s'
    for option in (4, 5, 6, 8):
        if need <= option:
            return f'{option}s'
    return '8s'


# Форматы видео. У видео-эндпоинтов только два (1:1 есть лишь у картинок и
# апскейла), поэтому «квадрат» из общих настроек для видео заменяем.
VIDEO_ASPECTS = ('16:9', '9:16')


def _video_aspect(aspect_ratio: Optional[str]) -> str:
    """Формат для ВИДЕО: 16:9 или 9:16, остальное сервис отвергает (HTTP 400)."""
    value = str(aspect_ratio or config.VEONONSTOP_ASPECT_RATIO or '').strip()
    if value in VIDEO_ASPECTS:
        return value
    logging.warning("Формат %r не поддерживается для видео — беру 16:9", value)
    return '16:9'


def _api_name(raw: str, index: int) -> str:
    """Имя референса для API: только латиница, цифры и «_».

    Сервис ищет имя среди слов промпта и по документации понимает ТОЛЬКО
    латиницу — кириллица в имени ломает сопоставление.
    """
    name = re.sub(r'[^A-Za-z0-9_]+', '', str(raw))
    return name or f'ref{index}'


class VeoNonStopClient:
    """Клиент VeoNonStop API"""

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None,
                 api_version: Optional[int] = None):
        self.api_key = api_key or config.VEONONSTOP_API_KEY
        self.base_url = (
            base_url or config.VEONONSTOP_API_BASE
        ).strip().rstrip('/')
        # Версия API читается из адреса: он и так задаётся отдельно для каждого
        # ключа (VEONONSTOP_API_BASE_2/3/4), значит новый сервис подключается
        # обычным слотом, а клиент сам понимает, с кем говорит.
        #
        # Зачем вообще: у второй версии (https://new.veononstop.org/api/v2)
        # три отличия, проверенных живыми запросами 01.09.2026 —
        #   • результат КАРТИНОК лежит только в /tasks/:id, а /video/status/:id
        #     отдаёт завершённую задачу без картинок вовсе;
        #   • массовая отмена /video/cancel-all мертва (три HTTP 500 подряд,
        #     ADMISSION_SERVICE_UNAVAILABLE) — работает только поштучная;
        #   • реальный лимит параллели лежит в admission_usage.limit, а
        #     привычное max_concurrent_tasks возвращает 0.
        # Подробности и замеры — PLAN_VEONONSTOP_V2.md.
        self.api_version = int(api_version) if api_version else (
            2 if '/api/v2' in self.base_url else 1)
        self.headers = {
            'X-API-Key': self.api_key,
            'Content-Type': 'application/json',
        }
        # For localhost desk backend: bypass system proxy (HTTPS_PROXY env breaks 127.0.0.1)
        is_local = ('localhost' in self.base_url) or ('127.0.0.1' in self.base_url)
        self.proxies = {'http': None, 'https': None} if is_local else None
        if not self.api_key:
            raise ValueError("VEONONSTOP_API_KEY not set in .env")

    # ==================================================================
    # Лимит запросов: 2 в секунду на ключ (по доке сервиса)
    # ==================================================================

    # Минимальный зазор между запросами одного ключа. Лимит — 2/с, берём 0.6 с
    # с запасом на разброс часов сервера. Опрос статусов десятков задач и
    # отправки идут параллельно, без общего затвора они упирались в HTTP 429.
    _MIN_REQUEST_GAP = 0.6
    _gate_lock = threading.Lock()
    _next_slot: Dict[str, float] = {}

    def _throttle(self) -> None:
        """Дождаться своей очереди: запросы одного ключа идут не чаще лимита.

        Затвор общий на процесс (на ключ). Стадии конвейера работают в разных
        процессах и друг друга не видят — для них остаётся повтор при 429.
        """
        with VeoNonStopClient._gate_lock:
            now = time.monotonic()
            slot = max(now, VeoNonStopClient._next_slot.get(self.api_key, 0.0))
            VeoNonStopClient._next_slot[self.api_key] = slot + self._MIN_REQUEST_GAP
        if slot > now:
            time.sleep(slot - now)

    def _request(self, method: str, url: str, **kwargs):
        """Запрос к API с затвором и одним повтором при HTTP 429."""
        send = requests.post if method == 'POST' else requests.get
        self._throttle()
        resp = send(url, **kwargs)
        if resp.status_code == 429:
            logging.info("HTTP 429 от %s — пауза 2с и повтор", url.rsplit('/', 2)[-2:])
            time.sleep(2.0)
            self._throttle()
            resp = send(url, **kwargs)
        return resp

    def _post(self, url: str, **kwargs):
        return self._request('POST', url, **kwargs)

    def _get(self, url: str, **kwargs):
        return self._request('GET', url, **kwargs)

    # ==================================================================
    # Видео: submit
    # ==================================================================

    # Длительности по доке: 4/5/6/8 секунд. У составного видео из референсов
    # (multi-image-to-video) длительность не задаётся — клип всегда 8 с.
    VIDEO_DURATIONS = (4, 5, 6, 8)

    def _apply_duration(self, body: dict, duration, supported: bool = True) -> None:
        """Положить `duration` в тело запроса.

        `supported` — принимает ли длительность ЭТОТ эндпоинт. По актуальной
        доке API v1 её знает только image-to-video (целое число секунд);
        для остальных эндпоинтов v1 параметр не отправляем, там клип 8 с.
        API v2 принимает строку «4s/6s/8s» на всех видео-эндпоинтах.

        Принимает и `6`, и `'6s'`. Неверное значение молча пропускаем: сервис
        ответил бы HTTP 400, а клип пусть выйдет стандартной длины.
        """
        if not duration:
            return
        try:
            seconds = int(float(str(duration).strip().lower().rstrip('s')))
        except ValueError:
            seconds = 0
        if seconds not in self.VIDEO_DURATIONS:
            logging.warning(
                f"duration {duration!r} не поддержан (допустимо: "
                f"{', '.join(map(str, self.VIDEO_DURATIONS))}) — отправляю без него")
            return
        if self.api_version >= 2:
            # у v2 ряд 4s/6s/8s — пятёрку округляем вверх до шестёрки
            body['duration'] = f'{6 if seconds == 5 else seconds}s'
        elif supported:
            body['duration'] = seconds

    def submit_image_to_video(
        self,
        image_path: str,
        prompt: str,
        aspect_ratio: Optional[str] = None,
        count: int = 1,
        duration: Optional[str] = None,
    ) -> str:
        """
        Оживить картинку → видео (image-to-video).

        `duration` — секунды (4/5/6/8) или «6s»; см. `_apply_duration`.

        Returns:
            taskId для поллинга
        """
        b64, mime = _image_to_base64(image_path)
        body = {
            'prompt': prompt,
            'image_base64': b64,
            'mime_type': mime,
            'aspect_ratio': _video_aspect(aspect_ratio),
            'count': count,
        }
        self._apply_duration(body, duration, supported=True)
        return self._submit('/video/image-to-video', body)

    def submit_text_to_video(
        self,
        prompt: str,
        aspect_ratio: Optional[str] = None,
        count: int = 1,
        duration: Optional[str] = None,
    ) -> str:
        """
        Видео из текста (text-to-video).

        `duration` — секунды (4/5/6/8) или «6s»; см. `_apply_duration`.

        Returns:
            taskId для поллинга
        """
        body = {
            'prompt': prompt,
            'aspect_ratio': _video_aspect(aspect_ratio),
            'count': count,
        }
        self._apply_duration(body, duration, supported=False)
        return self._submit('/video/text-to-video', body)

    def submit_multi_image_to_video(
        self,
        image_paths: List[str],
        prompt: str,
        aspect_ratio: Optional[str] = None,
        count: int = 1,
    ) -> str:
        """
        Видео из текста + референсные картинки (components).

        Args:
            image_paths: Список путей к изображениям-референсам
            prompt: Текстовый промпт
        Returns:
            taskId для поллинга
        """
        images = []
        for n, p in enumerate(image_paths, 1):
            b64, mime = _image_to_base64(p)
            images.append({'name': _api_name(Path(p).stem, n), 'image_base64': b64, 'mime_type': mime})

        body = {
            'prompt': prompt,
            'images': images,
            'aspect_ratio': _video_aspect(aspect_ratio),
            'count': count,
        }
        return self._submit('/video/multi-image-to-video', body)

    def submit_multi_image_to_video_from_ref_dir(
        self,
        ref_dir: str,
        prompt: str,
        aspect_ratio: Optional[str] = None,
        count: int = 1,
    ) -> str:
        """
        Components: загружает все картинки из ref/ и шлёт multi-image-to-video.
        """
        images = _load_ref_images(ref_dir)
        if not images:
            raise FileNotFoundError(f"No reference images found in {ref_dir}")

        body = {
            'prompt': prompt,
            'images': images,
            'aspect_ratio': _video_aspect(aspect_ratio),
            'count': count,
        }
        return self._submit('/video/multi-image-to-video', body)

    def submit_batch_frame(
        self,
        start_image_path: str,
        end_image_path: str,
        prompt: str,
        aspect_ratio: Optional[str] = None,
        count: int = 1,
        duration: Optional[str] = None,
    ) -> str:
        """
        Batch-frame: переход между двумя кадрами (start → end).
        POST /video/batch-frame

        `duration` — секунды (4/5/6/8) или «6s»; см. `_apply_duration`.

        Returns:
            taskId для поллинга
        """
        start_b64, _ = _image_to_base64(start_image_path)
        end_b64, _ = _image_to_base64(end_image_path)
        body = {
            'prompt': prompt,
            'start_image_base64': start_b64,
            'end_image_base64': end_b64,
            'aspect_ratio': _video_aspect(aspect_ratio),
            'count': count,
        }
        self._apply_duration(body, duration, supported=False)
        return self._submit('/video/batch-frame', body)

    def submit_upsample_video(
        self,
        media_generation_id: str,
        aspect_ratio: Optional[str] = None,
        video_url: Optional[str] = None,
    ) -> str:
        """
        Upsample (upscale) видео.
        POST /video/upsample

        Args:
            media_generation_id: из completed video result (videos[].mediaGenerationId)
            video_url: ссылка на готовый MP4 из ТОГО ЖЕ ответа. Необязательное
                поле API v2. Ссылка подписанная и живёт ограниченное время
                (`Expires` в самом адресе), поэтому годится только СВЕЖАЯ —
                сразу после генерации. Просроченную сервис не заберёт и вернёт
                `NOT_FOUND: Requested entity was not found` (замер 01.09.2026:
                со старой ссылкой отказ за 24с, со свежей — успех за 60с).
        Returns:
            taskId для поллинга
        """
        body = {
            'media_generation_id': media_generation_id,
            'aspect_ratio': aspect_ratio or config.VEONONSTOP_ASPECT_RATIO,
        }
        if video_url and self.api_version >= 2:
            body['video_url'] = video_url
        return self._submit('/video/upsample', body)

    # ==================================================================
    # Картинки: submit
    # ==================================================================

    def submit_imagen(
        self,
        prompt: str,
        aspect_ratio: Optional[str] = None,
    ) -> str:
        """
        Imagen 3.5 text-to-image (синхронный API).

        Returns:
            mediaGenerationId
        """
        body = {
            'prompt': prompt,
            'aspect_ratio': aspect_ratio or config.VEONONSTOP_ASPECT_RATIO,
        }
        return self._submit('/image/generate', body)

    def generate_imagen(
        self,
        prompt: str,
        output_path: str,
        aspect_ratio: Optional[str] = None,
    ) -> str:
        """
        Imagen 3.5 — /image/generate возвращает только mediaGenerationId без URL.
        По доке VeoNonStop: для получения изображения нужно использовать Banana.
        Fallback: вызываем generate_banana с model_key по умолчанию.

        Returns:
            путь к сохранённому файлу
        """
        logging.info("generate_imagen: /image/generate не возвращает URL, fallback на banana")
        return self.generate_banana(
            prompt=prompt,
            output_path=output_path,
            aspect_ratio=aspect_ratio,
        )

    def submit_banana(
        self,
        prompt: str,
        aspect_ratio: Optional[str] = None,
        num_images: int = 1,
        model_key: Optional[str] = None,
        reference_images: Optional[List[Dict[str, str]]] = None,
    ) -> str:
        """
        Banana Pro text-to-image (+ optional style references).

        Args:
            reference_images: [{image_base64, mime_type}, ...] для стиль-трансфера
        Returns:
            taskId для поллинга
        """
        body = {
            'prompt': prompt,
            'aspect_ratio': aspect_ratio or config.VEONONSTOP_ASPECT_RATIO,
            'num_images': num_images,
            'model_key': model_key or config.VEONONSTOP_BANANA_MODEL,
        }
        if reference_images:
            body['reference_images'] = reference_images
        return self._submit('/image/banana/generate', body)

    def generate_banana(
        self,
        prompt: str,
        output_path: str,
        aspect_ratio: Optional[str] = None,
        num_images: int = 1,
        model_key: Optional[str] = None,
        reference_images: Optional[List[Dict[str, str]]] = None,
        use_all_ref_images: bool = False,
    ) -> str:
        """
        Banana — постановка задачи, ожидание готовности и скачивание файла.

        Снаружи выглядит синхронным: метод возвращает управление, когда файл
        уже на диске. Сам протокол при этом асинхронный — сервис отвечает
        номером задачи, дальше идёт опрос статуса. Прежний комментарий обещал
        «синхронную генерацию, API возвращает media[].fifeUrl прямо в ответе»,
        и это вводило в заблуждение: ссылок в ответе на постановку нет уже
        давно (проверено на обеих версиях API 01.09.2026).

        Returns:
            путь к сохранённому файлу
        """
        body = {
            'prompt': prompt,
            'aspect_ratio': aspect_ratio or config.VEONONSTOP_ASPECT_RATIO,
            'num_images': num_images,
            'model_key': model_key or config.VEONONSTOP_BANANA_MODEL,
        }
        if reference_images:
            body['reference_images'] = reference_images
            if use_all_ref_images:
                body['use_all_ref_images'] = True

        logging.info(
            f"generate_banana: prompt={prompt[:60]!r}  refs={len(reference_images) if reference_images else 0}"
            f"  use_all_ref_images={use_all_ref_images}"
            f"  model={body['model_key']}  aspect={body['aspect_ratio']}"
        )

        url = f"{self.base_url}/image/banana/generate"
        from units.frame_format import with_frame_format
        body['prompt'] = with_frame_format(body['prompt'], body.get('aspect_ratio'))
        resp = self._post(url, headers=self.headers, proxies=self.proxies, json=body, timeout=300)

        if resp.status_code not in (200, 201, 202):
            err = parse_api_error(resp, context='image/banana/generate')
            if 'TOKEN_EXPIRED' in (err.raw or '') or err.code == 'TOKEN_EXPIRED':
                raise VeoNonStopTokenExpiredError(f"Token expired: {err}")
            if resp.status_code == 429 or 'RATE_LIMIT' in (err.raw or ''):
                raise VeoNonStopRateLimitError(f"Rate limit: {err}")
            if resp.status_code >= 500:
                # Пятисотка обычно временная; `is_retryable` учитывает и явный
                # признак от сервиса, если тот его прислал.
                raise VeoNonStopTimeoutError(f"Server error: {err}")
            raise VeoNonStopError(f"Submit /image/banana/generate failed: {err}")

        resp_json = resp.json()
        data = resp_json.get('data', resp_json) if isinstance(resp_json.get('data'), dict) else resp_json

        # Сохраняем project_id для upscale и matched_references для дебага
        self._last_project_id = data.get('project_id')
        matched = data.get('matched_references')
        if matched is not None:
            matched_count = matched if isinstance(matched, int) else len(matched)
            logging.info(f"Banana matched_references ({matched_count} refs matched by server): {matched}")
        elif reference_images:
            logging.warning("Banana matched_references: absent in response (refs may not have been applied)")

        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)

        _proj_id = self._last_project_id or ''
        # Объявляем ДО ветвления: ниже в запасной ветке (основной путь v2)
        # переменная читается, а объявлялась она только внутри ветки `media`.
        _mid = ''

        # Banana returns media[] with fifeUrl
        media = data.get('media', [])
        if media and isinstance(media, list):
            item = media[0]
            _mid = item.get('mediaGenerationId', '')
            fife_url = item.get('fifeUrl') or item.get('url') or item.get('image_url')
            if fife_url:
                self._download_url(fife_url, out)
                logging.info(f"Banana generated -> {output_path}")
                return BananaResult(str(out), media_id=_mid, project_id=_proj_id)
            if _mid:
                # Fallback: poll + download
                status = self.poll_image_status(_mid)
                self._write_image_result(status, out, task_id=_mid)
                logging.info(f"Banana generated (polled) -> {output_path}")
                return BananaResult(str(out), media_id=_mid, project_id=_proj_id)

        # Fallback: poll. На API v2 это ОСНОВНОЙ путь — там ответ на submit несёт
        # только task_id, а картинка и её метаданные приходят в готовой задаче.
        task_id = data.get('mediaGenerationId') or data.get('taskId') or data.get('task_id')
        if task_id:
            status = self.poll_image_status(task_id)
            self._write_image_result(status, out, task_id=task_id)
            # media_id и project_id для АПСКЕЙЛА берём из ГОТОВОЙ ЗАДАЧИ.
            # Раньше сюда уходил `task_id` (идентификатор ЗАДАЧИ, а не
            # картинки) и пустой project_id — тот читался из ответа на submit,
            # где на v2 его нет вовсе. Апскейл на новом ключе отвечал
            # «project_id is required» на каждом кадре (проверено живьём
            # 01.09.2026). Настоящие значения лежат в `/tasks/:id`:
            # `project_id` в корне, `mediaGenerationId` — в images[0].
            _st = (status.get('data', status)
                   if isinstance(status.get('data'), dict) else status)
            _images = _st.get('images') or _st.get('result') or []
            _first = _images[0] if isinstance(_images, list) and _images else {}
            if isinstance(_first, dict):
                _mid = _first.get('mediaGenerationId') or _mid or task_id
            else:
                _mid = _mid or task_id
            _proj_id = _st.get('project_id') or _proj_id or ''
            logging.info(f"Banana generated (polled fallback) -> {output_path}")
            return BananaResult(str(out), media_id=_mid, project_id=_proj_id)

        raise VeoNonStopError(f"Cannot extract image from banana response: {resp_json}")

    def submit_banana_with_refs(
        self,
        prompt: str,
        ref_dir: str,
        aspect_ratio: Optional[str] = None,
        num_images: int = 1,
        model_key: Optional[str] = None,
    ) -> str:
        """Banana Pro + загрузка reference_images из папки ref/."""
        refs = _load_ref_images(ref_dir)
        return self.submit_banana(
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            num_images=num_images,
            model_key=model_key,
            reference_images=refs or None,
        )

    def upscale_banana(
        self,
        media_id: str,
        project_id: str,
        output_path: str,
        target_resolution: str = None,
    ) -> str:
        """
        Upscale изображения через Banana.
        POST /image/banana/upscale

        target_resolution: UPSAMPLE_IMAGE_RESOLUTION_2K (default) | UPSAMPLE_IMAGE_RESOLUTION_4K

        Returns:
            путь к сохранённому файлу
        """
        body = {
            'media_id': media_id,
            'project_id': project_id,
        }
        if target_resolution:
            body['target_resolution'] = target_resolution
        url = f"{self.base_url}/image/banana/upscale"
        resp = self._post(url, headers=self.headers, proxies=self.proxies, json=body, timeout=120)

        if resp.status_code not in (200, 201, 202):
            err = parse_api_error(resp, context='image/banana/upscale')
            if 'TOKEN_EXPIRED' in (err.raw or '') or err.code == 'TOKEN_EXPIRED':
                raise VeoNonStopTokenExpiredError(f"Token expired: {err}")
            if resp.status_code == 429 or 'RATE_LIMIT' in (err.raw or ''):
                raise VeoNonStopRateLimitError(f"Rate limit: {err}")
            raise VeoNonStopError(f"Upscale failed: {err}")

        resp_json = resp.json()
        data = resp_json.get('data', resp_json) if isinstance(resp_json.get('data'), dict) else resp_json
        encoded = data.get('encodedImage')

        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)

        if not encoded:
            # API v2: апскейл АСИНХРОННЫЙ — в ответе только task_id, картинка
            # приходит в готовой задаче (проверено живьём 01.09.2026: ответ
            # `{task_id, type: banana_upscale, status: pending}`). Прежний код
            # ждал картинку прямо здесь и падал «No encodedImage».
            _task = data.get('task_id') or data.get('taskId')
            if not _task:
                raise VeoNonStopError(
                    f"No encodedImage and no task_id in upscale response: {resp_json}")
            logging.info("Upscale %s принят задачей %s — жду результат",
                         media_id, _task)
            status = self.poll_image_status(_task)
            # Готовую картинку разбирает общий писатель: он понимает и base64,
            # и ссылку (в апскейле приходит либо `encodedImage`, либо `fifeUrl`).
            self._write_image_result(status, out, task_id=_task)
            logging.info(f"Upscaled (polled) {media_id} -> {output_path}")
            return str(out)

        write_bytes_atomic(out, base64.b64decode(encoded))

        logging.info(f"Upscaled {media_id} -> {output_path}")
        return str(out)

    # ==================================================================
    # Общее: submit / poll / download
    # ==================================================================

    def _submit(self, endpoint: str, body: dict) -> str:
        """Отправить POST-запрос и вернуть taskId."""
        url = f"{self.base_url}{endpoint}"
        if isinstance(body.get('prompt'), str):
            # Формат кадра ролика — в конец промпта всегда. Берётся формат РОЛИКА, а не
            # поле тела: видео сервис принимает только 16:9/9:16, и для 1:1 в теле
            # остаётся 16:9 — сказать модели про квадрат можно только текстом.
            from units.frame_format import with_frame_format
            body = {**body, 'prompt': with_frame_format(body['prompt'])}
        # Таймаут (connect, read): 30s на подключение, 120s на передачу
        # base64-картинки могут быть несколько MB → write timeout при 60s
        resp = self._post(url, headers=self.headers, proxies=self.proxies, json=body, timeout=(30, 120))

        if resp.status_code not in (200, 201, 202):
            # Разбор ОДИН на все формы ответа: строка, объект с code/retryable,
            # вложенная ошибка Google, не-JSON. Полный ответ уходит в файл
            # `agent/logs/veononstop_errors_ГГГГММДД.log`.
            err = parse_api_error(resp, context=f"submit {endpoint}")
            if 'TOKEN_EXPIRED' in (err.raw or '') or err.code == 'TOKEN_EXPIRED':
                raise VeoNonStopTokenExpiredError(f"Token expired: {err}")
            if resp.status_code == 429 or 'RATE_LIMIT' in (err.raw or '') \
                    or 'RATE_LIMIT' in err.code:
                raise VeoNonStopRateLimitError(f"Rate limit: {err}")
            raise VeoNonStopError(f"Submit {endpoint} failed: {err}")

        resp_json = resp.json()
        # API may return {"success": true, "data": {"task_id": "..."}} or flat {"taskId": "..."}
        data = resp_json.get('data', resp_json) if isinstance(resp_json.get('data'), dict) else resp_json
        task_id = data.get('taskId') or data.get('task_id') or data.get('id') or data.get('mediaGenerationId')
        if not task_id:
            raise VeoNonStopError(f"No taskId in response: {resp_json}")

        logging.info(f"VeoNonStop submitted {endpoint} -> taskId={task_id}")
        return task_id

    def poll_video_status(
        self,
        task_id: str,
        timeout: Optional[int] = None,
        interval: Optional[int] = None,
        hung_timeout: int = 120,
    ) -> dict:
        """
        Поллить статус видеозадачи до завершения.

        Returns:
            Полный ответ API при completed
        Raises:
            VeoNonStopTimeoutError, VeoNonStopGenerationError, VeoNonStopHungError
        """
        return self._poll(f'/video/status/{task_id}', task_id, timeout, interval, hung_timeout=hung_timeout)

    def poll_image_status(
        self,
        task_id: str,
        timeout: Optional[int] = None,
        interval: Optional[int] = None,
        verbose: bool = False,
    ) -> dict:
        """Поллить статус задачи генерации изображения.
        verbose=True — печатать каждую смену статуса (для preflight-этапов в GUI).

        Адрес зависит от версии API, и это НЕ косметика:

          • v1 — `/video/status/:id`, общий эндпоинт на видео и картинки
            (`/tasks/:id` там не существует вовсе, отвечает 404);
          • v2 — только `/tasks/:id`. По `/video/status/:id` завершённая
            картиночная задача приходит БЕЗ картинок: одни служебные поля
            (task_id, type, status, даты), и `generate_banana` падает с
            «Unexpected image result format: NoneType».

        Проверено живыми запросами 01.09.2026 на обеих версиях; замеры — в
        PLAN_VEONONSTOP_V2.md. Ставить `/tasks/:id` всем подряд нельзя: на v1
        это три подряд HTTP 404, а три 404 здесь означают «задача потеряна».
        """
        endpoint = (f'/tasks/{task_id}' if self.api_version >= 2
                    else f'/video/status/{task_id}')
        return self._poll(endpoint, task_id, timeout, interval, verbose=verbose)

    def _poll(
        self,
        endpoint: str,
        task_id: str,
        timeout: Optional[int] = None,
        interval: Optional[int] = None,
        hung_timeout: int = 120,
        verbose: bool = False,
    ) -> dict:
        """Общий метод поллинга."""
        timeout = timeout or config.VEONONSTOP_POLL_TIMEOUT
        interval = interval or config.VEONONSTOP_POLL_INTERVAL
        url = f"{self.base_url}{endpoint}"
        deadline = time.time() + timeout

        # Hung detection: track time spent in session_init/uploading
        _hung_state: Optional[str] = None
        _hung_since: float = 0.0
        _consecutive_404 = 0
        _last_verbose_status: Optional[str] = None  # для verbose — печатаем только смены статуса

        def _is_module_shutdown() -> bool:
            """Возвращает True если любой из верхнеуровневых модулей запросил shutdown.
            Lazy import'ы — избегаем circular dependency."""
            try:
                import veo_motion as _vv
                if getattr(_vv, '_shutdown_requested', None) and _vv._shutdown_requested.is_set():
                    return True
            except Exception:
                pass
            try:
                import veo_stills as _ig
                if getattr(_ig, '_shutdown_event', None) and _ig._shutdown_event.is_set():
                    return True
            except Exception:
                pass
            try:
                import fastgen_engine as _fg
                if getattr(_fg, '_flowultra_shutdown_event', None) and _fg._flowultra_shutdown_event.is_set():
                    return True
            except Exception:
                pass
            return False

        while time.time() < deadline:
            # Shutdown check — чтобы Stop/Hard Stop реально обрывал зависшие pollers.
            if _is_module_shutdown():
                raise VeoNonStopGenerationError(f"Task {task_id} polling aborted — shutdown requested")
            try:
                resp = self._get(url, headers=self.headers, proxies=self.proxies, timeout=30)
            except requests.RequestException as e:
                logging.warning(f"Poll {task_id}: network error — {e}")
                if verbose:
                    print(f"  [POLL {task_id[:10]}] network error: {str(e)[:80]}", flush=True)
                time.sleep(interval)
                continue

            if resp.status_code == 404:
                _consecutive_404 += 1
                logging.warning(f"Poll {task_id}: HTTP 404 ({_consecutive_404}/3)")
                if verbose:
                    print(f"  [POLL {task_id[:10]}] HTTP 404 ({_consecutive_404}/3)", flush=True)
                if _consecutive_404 >= 3:
                    raise VeoNonStopGenerationError(
                        f"Task {task_id} lost — 3x HTTP 404 (server dropped the task)")
                time.sleep(10)
                continue
            _consecutive_404 = 0  # reset on any non-404 response

            if resp.status_code != 200:
                logging.warning(f"Poll {task_id}: HTTP {resp.status_code}")
                if verbose:
                    print(f"  [POLL {task_id[:10]}] HTTP {resp.status_code}", flush=True)
                time.sleep(interval)
                continue

            resp_json = resp.json()
            # Handle nested {"success": true, "data": {...}} or flat {...}
            data = resp_json.get('data', resp_json) if isinstance(resp_json.get('data'), dict) else resp_json
            status = (data.get('status') or '').lower()

            if verbose and status != _last_verbose_status:
                _el = int(time.time() - (deadline - timeout))
                print(f"  [POLL {task_id[:10]}] status={status or 'unknown'} ({_el}s)", flush=True)
                _last_verbose_status = status

            if status == 'completed':
                return data
            elif status in ('failed', 'error', 'cancelled'):
                # Ошибка задачи приходит и строкой, и объектом (в т.ч. с
                # вложенной ошибкой Google: {'code': 3, 'message':
                # 'PUBLIC_ERROR_AUDIO_FILTERED'}). Разворачиваем в читаемый вид
                # и кладём полный ответ в файл отладки.
                raw_err = data.get('error') or data.get('message') or 'Unknown error'
                if isinstance(raw_err, dict):
                    _code = raw_err.get('code') or raw_err.get('status') or ''
                    _msg = raw_err.get('message') or ''
                    _inner = raw_err.get('error')
                    if isinstance(_inner, dict):
                        _code = _code or _inner.get('status') or _inner.get('code') or ''
                        _msg = _msg or _inner.get('message') or ''
                    error_msg = ' | '.join(str(x) for x in (_code, _msg) if x) or str(raw_err)
                else:
                    error_msg = str(raw_err)
                _dump = _error_dump_path()
                if _dump:
                    try:
                        with open(_dump, 'a', encoding='utf-8', errors='replace') as _fh:
                            _fh.write(f"\n{'=' * 70}\n[{datetime.now():%Y-%m-%d %H:%M:%S}]"
                                      f" task {task_id} status={status}\n"
                                      f"{json.dumps(data, ensure_ascii=False)[:4000]}\n")
                    except OSError:
                        pass
                logging.error("Task %s %s: %s", task_id, status, error_msg)
                raise VeoNonStopGenerationError(
                    f"Task {task_id} failed: {error_msg}"
                )
            elif status == 'queued':
                if _hung_state != 'queued':
                    logging.info(f"Task {task_id}: queued (waiting for server slot)...")
                    _hung_state = 'queued'
            elif status in ('session_init', 'uploading'):
                if _hung_state != status:
                    _hung_state = status
                    _hung_since = time.time()
                    logging.info(f"Task {task_id}: {status}")
                elif time.time() - _hung_since > hung_timeout:
                    raise VeoNonStopHungError(
                        f"Task {task_id} hung in '{status}' for >{hung_timeout}s"
                    )
            else:
                # generating / polling / pending — reset hung tracker
                _hung_state = None

                # показываем прогресс если есть (только для count>1)
                progress = data.get('progress')
                if progress and isinstance(progress, dict):
                    total = progress.get('total', 0)
                    completed_cnt = progress.get('completed', 0)
                    if total > 1:
                        pct = int(completed_cnt / total * 100)
                        print(f"    task {task_id[:8]}... {status} [{completed_cnt}/{total}] {pct}%")
                        logging.info(f"Task {task_id}: {status} [{completed_cnt}/{total}]")

            time.sleep(interval)

        raise VeoNonStopTimeoutError(
            f"Task {task_id} timed out after {timeout}s"
        )

    def download_video(
        self,
        task_id: str,
        output_path: str,
        video_index: int = 0,
    ) -> str:
        """
        Скачать готовое видео по taskId.

        Internal retry (up to 3 attempts with backoff 5/10/20s): когда upstream
        Google → cloud-relay → us stream обрывается/таймаутит, часто помогает подождать
        и запросить повторно — Google к тому времени уже успевает дофинализировать видео.
        На наших плечах сэкономлено: не пересоздавать задачу из-за проблем со скачкой.

        Returns:
            Путь к сохранённому файлу
        """
        url = f"{self.base_url}/video/download/{task_id}"
        params = {'video_index': video_index}
        _backoffs = [5, 10, 20]  # retry after Nth fail: wait 5/10/20s before re-try
        _last_err: Optional[Exception] = None
        _integrity_retried = False  # один перескач при broken-bitstream, потом raise → resubmit

        for _att in range(1, 4):
            try:
                # connect=30s (fast fail on dead host), read=300s (tolerate slow streaming
                # from upstream Google → veononstop.org cloud relay → us).
                resp = self._get(url, headers=self.headers, proxies=self.proxies, params=params,
                                    timeout=(30, 300), stream=True)
                if resp.status_code != 200:
                    # 5xx / stream-aborted: retry; 4xx: fail fast
                    if resp.status_code >= 500 and _att < 3:
                        _delay = _backoffs[_att - 1]
                        logging.warning(
                            f"Download video {task_id} attempt {_att}/3: HTTP {resp.status_code} "
                            f"{resp.text[:120]} — retry in {_delay}s"
                        )
                        time.sleep(_delay)
                        continue
                    raise VeoNonStopError(
                        f"Download video {task_id} failed ({resp.status_code}): {resp.text[:200]}"
                    )

                out = Path(output_path)
                out.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with atomic_open(out) as f:
                        for chunk in resp.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                except (requests.exceptions.ChunkedEncodingError,
                        requests.exceptions.ConnectionError,
                        requests.exceptions.ReadTimeout) as _se:
                    # Stream broke mid-download — remove partial + retry
                    try:
                        out.unlink(missing_ok=True)
                    except Exception:
                        pass
                    if _att < 3:
                        _delay = _backoffs[_att - 1]
                        logging.warning(
                            f"Download video {task_id} attempt {_att}/3: stream broken ({_se.__class__.__name__}) "
                            f"— retry in {_delay}s"
                        )
                        time.sleep(_delay)
                        _last_err = _se
                        continue
                    raise VeoNonStopError(
                        f"Download video {task_id} failed: stream broken after 3 attempts: {_se}"
                    )
                # Integrity check: ffprobe должен вернуть duration > 0 и хотя бы один видеострим.
                # Даже успешно "скачавшиеся" файлы бывают побитыми на уровне bitstream'а
                # (VEO/cloud-relay иногда отдаёт truncated payload, который пишется без stream-exception).
                # Политика: один перескач того же task_id, и если опять битый — raise, пусть
                # вызывающий (veo_video.py) делает resubmit новой генерации.
                if not _verify_video_integrity(out):
                    try:
                        out.unlink(missing_ok=True)
                    except Exception:
                        pass
                    if not _integrity_retried:
                        _integrity_retried = True
                        logging.warning(
                            f"Download video {task_id}: integrity check failed "
                            f"(ffprobe empty/invalid) — one re-download in 5s"
                        )
                        time.sleep(5)
                        _last_err = VeoNonStopError("downloaded file failed ffprobe integrity check")
                        continue
                    raise VeoNonStopError(
                        f"Download video {task_id} failed integrity check after re-download"
                    )
                # Successful download
                break
            except VeoNonStopError:
                raise
            except requests.exceptions.RequestException as _re:
                if _att < 3:
                    _delay = _backoffs[_att - 1]
                    logging.warning(
                        f"Download video {task_id} attempt {_att}/3: {_re.__class__.__name__} "
                        f"{str(_re)[:120]} — retry in {_delay}s"
                    )
                    time.sleep(_delay)
                    _last_err = _re
                    continue
                raise VeoNonStopError(f"Download video {task_id} failed after 3 attempts: {_re}")

        logging.info(f"Downloaded video {task_id} -> {output_path}")
        return str(out)

    def download_image(
        self,
        task_id: str,
        output_path: str,
    ) -> str:
        """
        Скачать сгенерированное изображение по taskId.
        Ответ API содержит base64 или прямую ссылку — обрабатываем оба варианта.

        Returns:
            Путь к сохранённому файлу
        """
        # Сначала получаем статус (в нём будет result с картинкой)
        status = self.poll_image_status(task_id, timeout=10, interval=2)
        return self._write_image_result(status, Path(output_path), task_id=task_id)

    def _write_image_result(
        self,
        status: dict,
        out: Path,
        task_id: str = '',
    ) -> str:
        """Write one image from a completed VeoNonStop status response."""
        result = status.get('result') or status.get('images') or status.get('output')

        out.parent.mkdir(parents=True, exist_ok=True)

        if isinstance(result, list) and result:
            item = result[0]
            # Может быть base64 строка или URL
            if isinstance(item, str):
                if item.startswith('data:') or not item.startswith('http'):
                    # base64
                    b64 = item.split(',', 1)[-1] if ',' in item else item
                    write_bytes_atomic(out, base64.b64decode(b64))
                else:
                    # URL — скачиваем
                    self._download_url(item, out)
            elif isinstance(item, dict):
                url = item.get('fifeUrl') or item.get('url') or item.get('image_url')
                # `encodedImage` — так называет картинку АПСКЕЙЛ в готовой
                # задаче (API v2). Без него результат апскейла не записался бы:
                # ссылка там есть не всегда, а base64 лежит именно под этим
                # именем.
                b64 = (item.get('image_base64') or item.get('base64')
                       or item.get('encodedImage'))
                if b64:
                    write_bytes_atomic(out, base64.b64decode(b64))
                elif url:
                    self._download_url(url, out)
                else:
                    raise VeoNonStopError(f"Cannot extract image from result item: {item}")
        elif isinstance(result, str):
            if result.startswith('http'):
                self._download_url(result, out)
            else:
                b64 = result.split(',', 1)[-1] if ',' in result else result
                write_bytes_atomic(out, base64.b64decode(b64))
        else:
            raise VeoNonStopError(f"Unexpected image result format: {type(result)} — {status}")

        logging.info(f"Downloaded image {task_id} -> {out}")
        return str(out)

    def cancel_task(self, task_id: str) -> bool:
        """
        Отменить задачу и немедленно освободить слот.
        POST /video/cancel/:taskId

        Работает для статусов: pending, queued, session_init, uploading, generating, polling.

        Returns:
            True если успешно отменено
        """
        url = f"{self.base_url}/video/cancel/{task_id}"
        try:
            resp = self._post(url, headers=self.headers, proxies=self.proxies, timeout=15)
            if resp.status_code in (200, 201):
                logging.info(f"Task {task_id} cancelled successfully")
                return True
            logging.warning(f"Cancel task {task_id} failed ({resp.status_code}): {resp.text[:100]}")
            return False
        except Exception as e:
            logging.warning(f"Cancel task {task_id} error: {e}")
            return False

    # Типы задач картинок — всё остальное считаем видео. Списки взяты из живых
    # ответов /account/usage и доки v2: banana_generate, banana_upscale,
    # image_generate (task_imageg_…).
    _IMAGE_TASK_TYPES = ('banana_generate', 'banana_upscale', 'image_generate')
    _TERMINAL_STATUSES = ('completed', 'failed', 'cancelled', 'result_delivered')

    def _cancel_all_by_walk(self, images: bool) -> int:
        """Обход живых задач с поштучной отменой — путь для API v2.

        Массовой отмены на v2 нет: `/video/cancel-all` отвечает HTTP 500
        `ADMISSION_SERVICE_UNAVAILABLE` — три попытки подряд, несмотря на
        `"retryable": true` (проверено 01.09.2026). Поштучная отмена при этом
        работает и возвращает 200.

        Список задач берём из `/account/usage`: там поле `tasks` со всеми
        живыми и недавними задачами аккаунта — по одному объекту с `task_id`,
        `type`, `status`. Видео и картинки лежат ВМЕСТЕ, поэтому обход общий, а
        `images` только выбирает, какие типы отменять.
        """
        try:
            usage = self.get_account_usage() or {}
        except Exception as exc:                                   # noqa: BLE001
            logging.warning(f"cancel-all (v2): не удалось прочитать usage — {exc}")
            return 0
        tasks = usage.get('tasks')
        if not isinstance(tasks, list):
            logging.warning("cancel-all (v2): в usage нет списка tasks")
            return 0

        cancelled, targets = 0, []
        for item in tasks:
            if not isinstance(item, dict):
                continue
            task_id = str(item.get('task_id') or item.get('id') or '').strip()
            if not task_id:
                continue
            if str(item.get('status') or '').lower() in self._TERMINAL_STATUSES:
                continue
            is_image = str(item.get('type') or '') in self._IMAGE_TASK_TYPES
            if is_image != bool(images):
                continue
            targets.append(task_id)

        for task_id in targets:
            if self.cancel_task(task_id):
                cancelled += 1
        kind = 'image' if images else 'video'
        if targets:
            logging.warning(f"cancel-all (v2, {kind}): отменено {cancelled} из {len(targets)}: {targets}")
        else:
            logging.info(f"cancel-all (v2, {kind}): живых задач нет")
        return cancelled

    def cancel_all_tasks(self) -> int:
        """
        POST /video/cancel-all — отменить ВСЕ активные задачи и освободить все слоты.
        Экстренный сброс зомби-тасков с прошлого прогона.

        Отменяет ВСЕ задачи аккаунта, включая картиночные, — это поведение
        самого сервиса, одинаковое у обеих версий.

        На v2 этот эндпоинт какое-то время лежал (HTTP 500
        ADMISSION_SERVICE_UNAVAILABLE, три попытки подряд, 01.09.2026), и его
        починили в тот же день. Поэтому основной путь снова родной, а обход с
        поштучной отменой остаётся ЗАПАСНЫМ — если сервис опять отдаст ошибку,
        старт прогона не будет спотыкаться.

        Returns:
            Количество отменённых задач (0 если нет активных или ошибка)
        """
        url = f"{self.base_url}/video/cancel-all"
        try:
            resp = self._post(url, headers=self.headers, proxies=self.proxies, timeout=15)
            if resp.status_code in (200, 201):
                data = resp.json().get('data', {})
                count = data.get('cancelled_count', 0)
                tasks = data.get('tasks', [])
                logging.warning(f"cancel-all: {count} tasks cancelled: {tasks}")
                return count
            logging.warning(f"cancel-all failed ({resp.status_code}): {resp.text[:100]}")
            if self.api_version >= 2:
                logging.warning("cancel-all: перехожу на поштучную отмену (v2)")
                return self._cancel_all_by_walk(images=False)
            return 0
        except Exception as e:
            logging.warning(f"cancel-all error: {e}")
            if self.api_version >= 2:
                return self._cancel_all_by_walk(images=False)
            return 0

    def cancel_all_image_tasks(self) -> int:
        """
        POST /image/cancel-all — отменить ВСЕ активные задачи генерации изображений.
        Освобождает слоты banana/imagen занятые предыдущим прогоном.

        На API v2 этого эндпоинта НЕТ вовсе: `POST /api/v2/image/cancel-all`
        отвечает 404 «Endpoint not found» (проверено 01.09.2026, уже после того
        как владелец сервиса починил видеошную массовую отмену). Поэтому там
        единственный путь — обход живых задач с поштучной отменой, отбирая
        картиночные типы из `_IMAGE_TASK_TYPES`.

        Общий `/video/cancel-all` для этого не годится: он сносит ВСЕ задачи
        аккаунта, включая видео, а стадии в прогоне идут параллельно.

        Returns:
            Количество отменённых задач (0 если нет активных или ошибка)
        """
        if self.api_version >= 2:
            return self._cancel_all_by_walk(images=True)
        url = f"{self.base_url}/image/cancel-all"
        try:
            resp = self._post(url, headers=self.headers, proxies=self.proxies, timeout=15)
            if resp.status_code in (200, 201):
                data = resp.json().get('data', {})
                count = data.get('cancelled_count', 0)
                tasks = data.get('tasks', [])
                logging.warning(f"image cancel-all: {count} tasks cancelled: {tasks}")
                return count
            logging.warning(f"image cancel-all failed ({resp.status_code}): {resp.text[:100]}")
            return 0
        except Exception as e:
            logging.warning(f"image cancel-all error: {e}")
            return 0

    def get_account_info(self) -> dict:
        """
        GET /account/info — информация об аккаунте.
        Возвращает: username, plan, max_cookies (concurrent tasks), expiration.
        """
        url = f"{self.base_url}/account/info"
        resp = self._get(url, headers=self.headers, proxies=self.proxies, timeout=15)
        if resp.status_code != 200:
            raise VeoNonStopError(f"Account info failed ({resp.status_code}): {resp.text[:200]}")
        resp_json = resp.json()
        return resp_json.get('data', resp_json) if isinstance(resp_json.get('data'), dict) else resp_json

    @staticmethod
    def _normalize_usage(data: dict) -> dict:
        """Привести ответ /account/usage к единому виду для всего движка.

        По актуальной доке счётчики слотов лежат в `total_slots` / `busy_slots` /
        `available_slots`, а движок и мониторы читают `max_concurrent_tasks` и
        `active_tasks` (так их отдавал /account/tasks). Недостающие поля
        дополняем — существующие значения не трогаем.
        """
        if not isinstance(data, dict):
            return data
        if data.get('active_tasks') is None and data.get('busy_slots') is not None:
            data['active_tasks'] = data['busy_slots']
        if not data.get('max_concurrent_tasks') and data.get('total_slots') is not None:
            data['max_concurrent_tasks'] = data['total_slots']
        return data

    def get_account_usage(self) -> dict:
        """GET /account/usage with bounded retry for a busy relay."""
        url = f"{self.base_url}/account/usage"
        try:
            usage_timeout = max(
                15, int(getattr(config, 'VEONONSTOP_USAGE_TIMEOUT', 30))
            )
        except (TypeError, ValueError):
            usage_timeout = 30
        try:
            retries = max(
                0, int(getattr(config, 'VEONONSTOP_USAGE_RETRIES', 1))
            )
        except (TypeError, ValueError):
            retries = 1
        try:
            retry_delay = max(
                0.25, float(getattr(config, 'VEONONSTOP_USAGE_RETRY_DELAY', 1.5))
            )
        except (TypeError, ValueError):
            retry_delay = 1.5

        last_error = None
        for attempt in range(retries + 1):
            try:
                resp = self._get(
                    url,
                    headers=self.headers,
                    proxies=self.proxies,
                    timeout=(10, usage_timeout),
                )
                if resp.status_code == 200:
                    resp_json = resp.json()
                    return self._normalize_usage(
                        resp_json.get('data', resp_json)
                        if isinstance(resp_json.get('data'), dict)
                        else resp_json
                    )
                body = resp.text[:200]
                last_error = VeoNonStopError(
                    f"Account usage failed ({resp.status_code}): {body}"
                )
                if resp.status_code not in (408, 425, 429) and resp.status_code < 500:
                    raise last_error
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                last_error = exc
            if attempt < retries:
                time.sleep(retry_delay * (2 ** attempt))

        if last_error:
            raise last_error
        raise VeoNonStopError("Account usage failed without a response")

    def _download_url(self, url: str, output_path: Path):
        """Скачать файл по URL."""
        # Bypass proxy for localhost downloads (desk backend)
        dl_proxies = {'http': None, 'https': None} if ('localhost' in url or '127.0.0.1' in url) else None
        resp = requests.get(url, timeout=120, stream=True, proxies=dl_proxies)
        if resp.status_code != 200:
            raise VeoNonStopError(f"Download URL failed ({resp.status_code}): {url}")
        with atomic_open(output_path) as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
