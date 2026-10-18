"""
Клиент RoyalTechno AI — видео Veo 3.1 (720p, 8 с, $0.02 за клип).

Повторяет интерфейс `VeoNonStopClient` в части, нужной стадиям видео (как и
`VertexClient`), поэтому стадии i2v / t2v / batch_frame / параллакс работают с
ним без переписывания: провайдер выбирается при создании клиентов
(`BEGO_VIDEO_PROVIDER=royaltechno`).

API: https://api.royaltechno.cc (OpenAPI: /openapi.json, правила: /llms.txt).
  POST /v1/jobs            {model, input} → 201 {id, status, ...}
  GET  /v1/jobs/{id}       статус (ETag / If-None-Match → 304 без изменений)
  POST /v1/jobs/{id}/cancel
  GET  /v1/account         лимиты ключа (recommended_max_in_flight, recommended_poll_sec)
Авторизация: Authorization: Bearer rt_live_…

Модель: `veo-3.1` с `resolution: 720p` — тариф «Veo 3.1» $0.02 за клип (1080p той
же модели — отдельный тариф $0.05, поэтому разрешение передаётся ЯВНО). Длина
у Veo фиксирована — 8 с, `duration_sec` сервис игнорирует.

Правила сервиса для клиентов (версия 2026-10-05) — все соблюдаются здесь:
  1. Опрос — ОДНИМ циклом на ключ (не поток на задачу), видео не чаще раза в 10 с,
     всегда с If-None-Match; общий темп ниже 1 запроса/с.
  2. Не больше recommended_max_in_flight (≤ 20) задач в работе; следующая — когда
     освободится место.
  3. Задачу в queued/processing НИКОГДА не отправляем заново (долго ≠ упало).
     Таймаут ожидания — с отменой задачи, чтобы повтор стадии не удвоил расход.
  4. 429: ждать Retry-After, повтор с ТЕМ ЖЕ Idempotency-Key, дальше экспонента
     (max(Retry-After, min(60, 2^n)) ± 20 %), не больше 6 попыток. queue_full —
     POST не повторяем, ждём завершения своей задачи.
  5. 400/401/402/403 — не повторяем; 401/402/403 выключают ключ до конца прогона.
  6. Повтор только на 503 (кроме model_unavailable по разрешению — это не лечится).
  7. Один Idempotency-Key (UUID4) на задачу, повторы — с ним же.
"""
from __future__ import annotations

import base64
import io
import logging
import os
import random
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

_agent_dir = str(Path(__file__).resolve().parent.parent)
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

try:  # те же классы исключений, что ловят стадии
    from veo_client import (MIME_TYPES, VeoNonStopError, VeoNonStopGenerationError,
                            VeoNonStopRateLimitError, VeoNonStopTimeoutError,
                            _verify_video_integrity)
except ImportError:                                               # pragma: no cover
    from units.veo_client import (MIME_TYPES, VeoNonStopError, VeoNonStopGenerationError,
                                  VeoNonStopRateLimitError, VeoNonStopTimeoutError,
                                  _verify_video_integrity)
from units import job_journal       # id задач в работе на диске — их снимает «Стоп»

DEFAULT_API_BASE = 'https://api.royaltechno.cc'
VIDEO_MODEL = 'veo-3.1'
VIDEO_RESOLUTION = '720p'          # $0.02 за клип; 1080p — другой, дорогой тариф
CLIP_SECONDS = 8                   # у Veo длина фиксирована
MAX_IN_FLIGHT = 20                 # жёсткий потолок сервиса на ключ
VIDEO_POLL_SEC = 10                # правило 1: видео — раз в 10 с
MAX_ATTEMPTS = 6                   # правило 4
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # data URI после декодирования — не больше 5 МиБ
_USER_AGENT = 'BegoFrame/1.0 (+royaltechno video)'


class RoyalTechnoFatalError(VeoNonStopError):
    """401/402/403: ключ, баланс или бан — повторы бесполезны (правило 5)."""


def _api_error(resp) -> tuple:
    """(code, message, param) из тела ошибки {"error": {type, code, message, param}}."""
    try:
        body = resp.json()
    except ValueError:
        return '', (resp.text or '')[:300], ''
    err = body.get('error') if isinstance(body, dict) else None
    if isinstance(err, dict):
        return str(err.get('code') or ''), str(err.get('message') or ''), str(err.get('param') or '')
    return '', str(body)[:300], ''


def _retry_after(resp, default: float) -> float:
    try:
        return max(0.0, float(resp.headers.get('Retry-After') or default))
    except (TypeError, ValueError):
        return default


def _backoff(attempt: int, retry_after: float) -> float:
    """Правило 4: max(Retry-After, min(60, 2^attempt)) ± 20 %."""
    base = max(retry_after, min(60.0, 2.0 ** attempt))
    return base * random.uniform(0.8, 1.2)


def _image_data_uri(path: str) -> str:
    """Картинка → data URI. Больше 5 МиБ — пережимаем в JPEG и при нужде уменьшаем."""
    raw = Path(path).read_bytes()
    mime = MIME_TYPES.get(Path(path).suffix.lower(), 'image/png')
    if len(raw) > MAX_IMAGE_BYTES or mime not in ('image/jpeg', 'image/png', 'image/webp'):
        from PIL import Image
        im = Image.open(io.BytesIO(raw)).convert('RGB')
        quality = 92
        while True:
            buf = io.BytesIO()
            im.save(buf, format='JPEG', quality=quality)
            raw = buf.getvalue()
            if len(raw) <= MAX_IMAGE_BYTES:
                break
            if quality > 75:
                quality -= 8
            else:
                im = im.resize((max(1, im.width * 3 // 4), max(1, im.height * 3 // 4)))
        mime = 'image/jpeg'
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


class _KeyState:
    """Общее на ОДИН ключ: окно задач в работе, единый цикл опроса, «ключ мёртв»."""

    def __init__(self):
        self.cond = threading.Condition()
        self.in_flight = 0
        self.max_in_flight = 4
        self.jobs: Dict[str, Dict[str, Any]] = {}     # id → {event, etag, data, next_poll}
        self.fatal = ''                               # причина, если ключ выключен
        self.poller: Optional[threading.Thread] = None
        self.lock = threading.Lock()


_STATES: Dict[str, _KeyState] = {}
_STATES_LOCK = threading.Lock()


def _state_for(api_key: str) -> _KeyState:
    with _STATES_LOCK:
        if api_key not in _STATES:
            _STATES[api_key] = _KeyState()
        return _STATES[api_key]


class RoyalTechnoClient:
    """Видео Veo 3.1 через RoyalTechno с интерфейсом VeoNonStopClient."""

    api_version = 1
    provider = 'royaltechno'

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None):
        self.api_key = (api_key or os.environ.get('ROYALTECHNO_API_KEY', '')).strip()
        if not self.api_key:
            raise ValueError("ROYALTECHNO_API_KEY не задан (Настройки → RoyalTechno)")
        self.base_url = (base_url or os.environ.get('ROYALTECHNO_API_BASE', '') or DEFAULT_API_BASE).rstrip('/')
        self.headers = {'Authorization': f'Bearer {self.api_key}', 'User-Agent': _USER_AGENT,
                        'Accept': 'application/json'}
        self._state = _state_for(self.api_key)
        self._session = requests.Session()

    # ------------------------------------------------------------------ HTTP
    def _check_fatal(self) -> None:
        if self._state.fatal:
            raise RoyalTechnoFatalError(f"RoyalTechno выключен до конца прогона: {self._state.fatal}")

    def _fatal(self, status: int, code: str, message: str) -> None:
        reason = f"HTTP {status} {code}: {message}".strip()
        with self._state.cond:
            first = not self._state.fatal
            self._state.fatal = reason
            self._state.cond.notify_all()
        if first:
            print(f"\n  [RoyalTechno] ⛔ {reason} — ключ/баланс/доступ: повторы бесполезны, "
                  f"проверьте ключ и баланс (Настройки → RoyalTechno)")
            logging.error("RoyalTechno disabled: %s", reason)

    def _post_job(self, payload: dict) -> dict:
        """POST /v1/jobs по правилам 4–7. Возвращает тело 201."""
        idem = str(uuid.uuid4())                       # правило 7: один ключ на задачу
        url = f'{self.base_url}/v1/jobs'
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._check_fatal()
            try:
                resp = self._session.post(url, json=payload, timeout=(30, 120),
                                          headers={**self.headers, 'Idempotency-Key': idem})
            except requests.RequestException as exc:
                if attempt >= MAX_ATTEMPTS:
                    raise VeoNonStopTimeoutError(f"RoyalTechno: сеть недоступна: {exc}") from exc
                time.sleep(_backoff(attempt, 0))
                continue
            if resp.status_code in (200, 201):
                return resp.json()
            code, message, param = _api_error(resp)
            if resp.status_code in (401, 402, 403):
                self._fatal(resp.status_code, code, message)
                self._check_fatal()
            if resp.status_code in (400, 413):
                # правило 5: запрос неверен — повтор той же формы бесполезен
                text = f"RoyalTechno отклонил задачу ({resp.status_code} {code}): {message}"
                if any(m in (code + message).lower() for m in ('policy', 'safety', 'moderat', 'prohibit')):
                    text += ' (content policy)'
                raise VeoNonStopGenerationError(text)
            if resp.status_code == 429 and code == 'queue_full':
                # правило 4: POST не повторяем — ждём, пока закончится своя задача
                logging.warning("RoyalTechno: queue_full — жду завершения своей задачи")
                with self._state.cond:
                    self._state.cond.wait(timeout=_retry_after(resp, 60))
                continue
            if resp.status_code == 503 and code == 'model_unavailable' and param == 'input.resolution':
                raise VeoNonStopError(f"RoyalTechno: разрешение {VIDEO_RESOLUTION} сейчас не продаётся: {message}")
            if resp.status_code in (429, 503) and attempt < MAX_ATTEMPTS:
                wait = _backoff(attempt, _retry_after(resp, 2))
                logging.warning("RoyalTechno POST: HTTP %s %s — повтор через %.0fс (тот же Idempotency-Key)",
                                resp.status_code, code, wait)
                time.sleep(wait)
                continue
            if resp.status_code == 429:
                raise VeoNonStopRateLimitError(f"RoyalTechno rate limit: {code} {message}")
            raise VeoNonStopError(f"RoyalTechno POST /v1/jobs: HTTP {resp.status_code} {code}: {message}")
        raise VeoNonStopRateLimitError("RoyalTechno: исчерпаны попытки отправки задачи")

    # ----------------------------------------------------- окно задач в работе
    def _acquire_slot(self) -> None:
        with self._state.cond:
            while self._state.in_flight >= self._state.max_in_flight and not self._state.fatal:
                self._state.cond.wait(timeout=30)
            self._check_fatal()
            self._state.in_flight += 1

    def _release_slot(self) -> None:
        with self._state.cond:
            self._state.in_flight = max(0, self._state.in_flight - 1)
            self._state.cond.notify_all()

    # ------------------------------------------------------------ единый опрос
    def _ensure_poller(self) -> None:
        st = self._state
        with st.lock:
            if st.poller is None or not st.poller.is_alive():
                st.poller = threading.Thread(target=self._poll_loop, daemon=True,
                                             name='royaltechno-poll')
                st.poller.start()

    def _poll_loop(self) -> None:
        """Правило 1: один цикл на ключ, каждая задача — не чаще раза в 10 с, общий
        темп < 1 запроса/с (интервал растёт с числом задач), всегда If-None-Match."""
        st = self._state
        while True:
            with st.lock:
                active = {jid: j for jid, j in st.jobs.items() if not j['event'].is_set()}
            if not active:
                time.sleep(1.0)
                continue
            interval = max(VIDEO_POLL_SEC, len(active) * 1.2)
            now = time.time()
            due = [jid for jid, j in active.items() if j['next_poll'] <= now]
            if not due:
                time.sleep(1.0)
                continue
            jid = min(due, key=lambda k: active[k]['next_poll'])
            job = active[jid]
            job['next_poll'] = time.time() + interval
            try:
                headers = dict(self.headers)
                if job.get('etag'):
                    headers['If-None-Match'] = job['etag']
                resp = self._session.get(f'{self.base_url}/v1/jobs/{jid}', headers=headers, timeout=(20, 60))
                if resp.status_code == 200:
                    job['etag'] = resp.headers.get('ETag') or job.get('etag')
                    job['data'] = resp.json()
                    if job['data'].get('status') in ('succeeded', 'failed', 'canceled'):
                        job['event'].set()
                        job_journal.forget('royaltechno', jid)
                elif resp.status_code == 304:
                    pass
                elif resp.status_code == 429:
                    job['next_poll'] = time.time() + max(interval, _retry_after(resp, 10))
                elif resp.status_code in (401, 403):
                    code, message, _ = _api_error(resp)
                    self._fatal(resp.status_code, code, message)
                    for j in active.values():
                        j['event'].set()
                elif resp.status_code == 404:
                    job['data'] = {'status': 'failed', 'error': {'code': 'not_found',
                                                                 'message': 'задача не найдена'}}
                    job['event'].set()
                    job_journal.forget('royaltechno', jid)
            except (requests.RequestException, ValueError) as exc:
                logging.debug("RoyalTechno poll %s: %s", jid, exc)
            time.sleep(1.2)                            # между любыми двумя опросами

    # ------------------------------------------------------------------ видео
    @staticmethod
    def _aspect(aspect_ratio: Optional[str]) -> str:
        value = str(aspect_ratio or os.environ.get('VEONONSTOP_ASPECT_RATIO', '') or '16:9').strip()
        if value == '9:16':
            return 'portrait'
        if value != '16:9':
            logging.warning("RoyalTechno: формат %r не поддерживается (только 16:9 и 9:16) — беру 16:9", value)
        return 'landscape'

    def _submit(self, extra: dict, prompt: str, aspect_ratio: Optional[str]) -> str:
        from units.frame_format import with_frame_format
        from units.frame_format import resolve
        fmt = resolve(aspect_ratio)
        job_input = {'prompt': with_frame_format(prompt, fmt), 'resolution': VIDEO_RESOLUTION, **extra}
        # Поле формата сервис знает только для 16:9/9:16 и не для i2v (там формат берётся
        # из картинки); 1:1 — только текстом в промпте.
        if 'start_image_url' not in extra and fmt in ('16:9', '9:16'):
            job_input['aspect_ratio'] = self._aspect(fmt)
        self._acquire_slot()
        try:
            data = self._post_job({'model': VIDEO_MODEL, 'input': job_input})
        except BaseException:
            self._release_slot()
            raise
        jid = data.get('id')
        if not jid:
            self._release_slot()
            raise VeoNonStopError(f"RoyalTechno: в ответе нет id задачи: {str(data)[:300]}")
        event = threading.Event()
        with self._state.lock:
            self._state.jobs[jid] = {'event': event, 'etag': None, 'data': data,
                                     'next_poll': time.time() + VIDEO_POLL_SEC, 'released': False}
        if data.get('status') in ('succeeded', 'failed', 'canceled'):
            event.set()
        else:
            job_journal.record('royaltechno', jid)
        self._ensure_poller()
        logging.info("RoyalTechno submitted %s (%s, %s) -> %s, %s¢", VIDEO_MODEL, VIDEO_RESOLUTION,
                     'i2v' if 'start_image_url' in extra else 't2v', jid, data.get('cost_usd_cents'))
        return jid

    def _finish_slot(self, jid: str) -> None:
        with self._state.lock:
            job = self._state.jobs.get(jid)
            if not job or job.get('released'):
                return
            job['released'] = True
        self._release_slot()

    def submit_image_to_video(self, image_path: str, prompt: str, aspect_ratio: Optional[str] = None,
                              count: int = 1, duration=None) -> str:
        return self._submit({'start_image_url': _image_data_uri(image_path)}, prompt, aspect_ratio)

    def submit_text_to_video(self, prompt: str, aspect_ratio: Optional[str] = None,
                             count: int = 1, duration=None) -> str:
        return self._submit({}, prompt, aspect_ratio)

    def submit_batch_frame(self, start_image_path: str, end_image_path: str, prompt: str,
                           aspect_ratio: Optional[str] = None, count: int = 1, duration=None) -> str:
        """Переход «первый кадр → последний кадр» (у Veo 3.1 поддерживается)."""
        return self._submit({'start_image_url': _image_data_uri(start_image_path),
                             'end_image_url': _image_data_uri(end_image_path)}, prompt, aspect_ratio)

    def submit_multi_image_to_video(self, image_paths: List[str], prompt: str,
                                    aspect_ratio: Optional[str] = None, count: int = 1) -> str:
        """Видео по референсам — у Veo 3.1 до 3 картинок."""
        refs = [_image_data_uri(p) for p in list(image_paths)[:3]]
        return self._submit({'reference_image_urls': refs}, prompt, aspect_ratio)

    def submit_multi_image_to_video_from_ref_dir(self, ref_dir: str, prompt: str,
                                                 aspect_ratio: Optional[str] = None, count: int = 1) -> str:
        paths = [str(p) for p in sorted(Path(ref_dir).iterdir(), key=lambda p: p.name.lower())
                 if p.is_file() and p.suffix.lower() in MIME_TYPES]
        if not paths:
            raise FileNotFoundError(f"No reference images found in {ref_dir}")
        return self.submit_multi_image_to_video(paths, prompt, aspect_ratio, count)

    def submit_upsample_video(self, *_a, **_kw) -> str:
        raise VeoNonStopError("Апскейл видео у RoyalTechno не поддерживается — выключите его в настройках видео")

    def poll_video_status(self, task_id: str, timeout: Optional[int] = None,
                          interval: Optional[int] = None, hung_timeout: int = 120) -> dict:
        """Ждать готовности задачи (опрашивает общий цикл). Ответ — в формате VeoNonStop."""
        timeout = timeout or int(os.environ.get('ROYALTECHNO_POLL_TIMEOUT', '') or 3600)
        with self._state.lock:
            job = self._state.jobs.get(task_id)
        if job is None:                                # задача не наша (повторный запуск) — следим
            job = {'event': threading.Event(), 'etag': None, 'data': {}, 'next_poll': 0, 'released': True}
            with self._state.lock:
                self._state.jobs[task_id] = job
        self._ensure_poller()
        if not job['event'].wait(timeout):
            # Правило 3: долго — не значит упало. Но стадия после таймаута повторит
            # клип, поэтому задачу отменяем — иначе расход удвоится.
            self.cancel_task(task_id)
            self._finish_slot(task_id)
            raise VeoNonStopTimeoutError(f"Task {task_id} timed out after {timeout}s (отменена)")
        self._finish_slot(task_id)
        self._check_fatal()
        data = job.get('data') or {}
        status = data.get('status')
        if status == 'succeeded':
            output = data.get('output') or {}
            if data.get('notice'):
                logging.warning("RoyalTechno %s: %s", task_id, data['notice'])
            if not output.get('url'):
                raise VeoNonStopGenerationError(f"Task {task_id} failed: результат без ссылки "
                                                f"(media_expired={data.get('media_expired')})")
            return {'status': 'completed', 'task_id': task_id,
                    'videos': [{'mediaGenerationId': None, 'fifeUrl': None}]}
        err = data.get('error') or {}
        code, message = str(err.get('code') or status or ''), str(err.get('message') or '')
        text = f"Task {task_id} failed: {code}: {message}".strip()
        if any(m in (code + ' ' + message).lower() for m in ('policy', 'safety', 'moderat', 'prohibit', 'blocked')):
            text += ' (content policy)'
        raise VeoNonStopGenerationError(text)

    def download_video(self, task_id: str, output_path: str, video_index: int = 0) -> str:
        with self._state.lock:
            job = self._state.jobs.get(task_id) or {}
        url = ((job.get('data') or {}).get('output') or {}).get('url')
        if not url:
            raise VeoNonStopError(f"Download video {task_id}: нет ссылки (опрос не выполнялся?)")
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(out.suffix + '.part')
        last_exc: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                with self._session.get(url, stream=True, timeout=(30, 300),
                                       headers={'User-Agent': _USER_AGENT}) as resp:
                    resp.raise_for_status()
                    with open(tmp, 'wb') as fh:
                        for chunk in resp.iter_content(256 * 1024):
                            fh.write(chunk)
                tmp.replace(out)
                break
            except (requests.RequestException, OSError) as exc:
                last_exc = exc
                tmp.unlink(missing_ok=True)
                time.sleep(_backoff(attempt, 0))
        else:
            raise VeoNonStopError(f"Download video {task_id}: {last_exc}")
        if not _verify_video_integrity(out):
            out.unlink(missing_ok=True)
            raise VeoNonStopError(f"Download video {task_id}: файл не прошёл проверку ffprobe")
        with self._state.lock:
            self._state.jobs.pop(task_id, None)
        logging.info("RoyalTechno video %s -> %s", task_id, output_path)
        return str(out)

    # --------------------------------------------------------- служебные вызовы
    def cancel_task(self, task_id: str) -> bool:
        try:
            resp = self._session.post(f'{self.base_url}/v1/jobs/{task_id}/cancel',
                                      headers=self.headers, timeout=(20, 60))
            if resp.status_code == 200:
                job_journal.forget('royaltechno', task_id)
            return resp.status_code == 200
        except requests.RequestException:
            return False

    def cancel_all_tasks(self) -> int:
        """Отменить ТОЛЬКО свои задачи этого процесса (чужие на ключе не трогаем)."""
        with self._state.lock:
            mine = [jid for jid, j in self._state.jobs.items() if not j['event'].is_set()]
        return sum(1 for jid in mine if self.cancel_task(jid))

    def cancel_all_image_tasks(self) -> int:
        return 0

    def get_account_info(self) -> dict:
        """План и лимиты ключа. Заодно ставит окно задач в работе (правило 2)."""
        self._check_fatal()
        resp = self._session.get(f'{self.base_url}/v1/account', headers=self.headers, timeout=(20, 60))
        if resp.status_code in (401, 402, 403):
            code, message, _ = _api_error(resp)
            self._fatal(resp.status_code, code, message)
            self._check_fatal()
        if resp.status_code != 200:
            code, message, _ = _api_error(resp)
            raise VeoNonStopError(f"RoyalTechno /v1/account: HTTP {resp.status_code} {code}: {message}")
        data = resp.json()
        limits = data.get('limits') or {}
        rec = int(limits.get('recommended_max_in_flight') or 4)
        with self._state.cond:
            self._state.max_in_flight = max(1, min(MAX_IN_FLIGHT, rec))
        plans = ', '.join(p.get('name', '') for p in (data.get('plans') or []) if isinstance(p, dict))
        return {'plan': plans or f"баланс {data.get('balance_usd', '?')}",
                'concurrent_tasks': self._state.max_in_flight,
                'balance_usd': data.get('balance_usd')}

    def get_account_usage(self) -> dict:
        return {}                     # окно задач ведёт сам клиент; лишних запросов не шлём


def royaltechno_selected() -> bool:
    return os.environ.get('BEGO_VIDEO_PROVIDER', '').strip().lower() == 'royaltechno'


def royaltechno_ready() -> bool:
    return royaltechno_selected() and bool(os.environ.get('ROYALTECHNO_API_KEY', '').strip())


def build_royaltechno_cfg() -> List[Dict[str, Any]]:
    """Конфигурация клиентов в формате стадий: `[{client, mp, sem, label, slot}]`."""
    try:
        workers = max(1, int(os.environ.get('ROYALTECHNO_VIDEO_PARALLEL', '') or 4))
    except ValueError:
        workers = 4
    client = RoyalTechnoClient()
    try:
        acc = client.get_account_info()
        workers = max(1, min(workers, int(acc.get('concurrent_tasks') or workers)))
        print(f"  [RoyalTechno] {VIDEO_MODEL} {VIDEO_RESOLUTION} ($0.02/клип) | {acc.get('plan')} | "
              f"рабочих {workers}")
    except RoyalTechnoFatalError:
        raise
    except Exception as exc:                                       # noqa: BLE001
        workers = min(workers, 4)
        print(f"  [RoyalTechno] лимиты ключа не получены ({exc}) — рабочих {workers}")
    with client._state.cond:
        client._state.max_in_flight = max(1, min(MAX_IN_FLIGHT, max(client._state.max_in_flight, workers)))
    sem = threading.Semaphore(workers)
    return [{'client': client, 'mp': workers, 'sem': sem, 'sem_slots': workers,
             'label': 'RoyalTechno', 'slot': 1}]
