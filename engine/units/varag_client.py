"""
Клиент VARAG (Inflight HTTP API) — картинки и видео по API-ключу mk_….

Повторяет интерфейс `VeoNonStopClient` в части, нужной стадиям (как `VertexClient`):
картинки — `generate_banana` / `generate_imagen`, видео — `submit_image_to_video`,
`submit_text_to_video`, `poll_video_status`, `download_video`. Поэтому стадии картинок,
инфографики и видео работают с ним без переписывания: провайдер выбирается при
создании клиентов (BEGO_IMAGE_PROVIDER=varag / BEGO_VIDEO_PROVIDER=varag).

API: https://api.varag.video/v1, заголовок `X-API-Key: mk_…`.
  POST /image/generate          {prompt, aspect_ratio, client_ref}  → {id, state: accepted, ...}
  POST /video/text-to-video     {prompt, aspect_ratio, client_ref}
  POST /video/image-to-video    {prompt, image_base64, image_mime, aspect_ratio, client_ref}
  GET  /tasks?ids=a,b,c         состояние нескольких задач одним запросом
  POST /tasks/{id}/hard-cancel  снять задачу (в том числе running), квота не списывается
  POST /tasks/hard-cancel       {"ids": [...]} или {"all": true}
  GET  /account                 тариф: plan, слоты по видам
Подключены только базовые режимы (текст → картинка, текст → видео, картинка → видео):
референсов в программе нет, «картинка, затем видео» конвейер делает двумя стадиями.

Правила сервиса (соблюдаются здесь):
  1. Все запросы асинхронные: POST лишь принимает задачу, результат — после state=ready.
  2. Опрос — ОДНИМ циклом на ключ, все задачи одним GET /tasks?ids=…, раз в 4 с.
  3. Сервер не держит очередь: на SLOTS_BUSY задача не создана — ждём своего
     освобождения и отправляем снова (локальная очередь). Окно задач в работе —
     по слотам тарифа; у плана `chain` картинки и видео делят один пул.
  4. Картинка на входе — не больше 1 500 000 байт до base64: пережимаем сами.
  5. declined — отказ по содержанию: не повторяем тот же промпт (стадия перепишет).
     failed — сбой сервиса: стадия повторит ограниченное число раз.
  6. Сбой опроса или скачивания не повод заново заказывать генерацию — опрашиваем
     тот же id. Таймаут ожидания — с hard-cancel, чтобы повтор стадии не удвоил расход.
  7. 401/402/403 и дневная квота выключают ключ (или вид генерации) до конца прогона.
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
    from veo_client import (MIME_TYPES, BananaResult, VeoNonStopError, VeoNonStopGenerationError,
                            VeoNonStopRateLimitError, VeoNonStopTimeoutError, _verify_video_integrity)
except ImportError:                                               # pragma: no cover
    from units.veo_client import (MIME_TYPES, BananaResult, VeoNonStopError, VeoNonStopGenerationError,
                                  VeoNonStopRateLimitError, VeoNonStopTimeoutError, _verify_video_integrity)

try:  # файл появляется под своим именем только целиком (см. units/media_io.py)
    from units.media_io import atomic_target
except ImportError:                                               # pragma: no cover
    from media_io import atomic_target

DEFAULT_API_BASE = 'https://api.varag.video/v1'
MAX_IMAGE_BYTES = 1_500_000        # правило 4: на каждую картинку, до base64
MAX_PROMPT_CHARS = 6000
POLL_SEC = 4                       # правило 2: 3–5 с
IDS_PER_POLL = 50                  # задач в одном GET /tasks?ids=…
HARD_CANCEL_IDS = 200              # потолок сервиса на один hard-cancel по списку
MAX_ATTEMPTS = 6                   # повторы POST на 429 TOO_MANY_REQUESTS / 503 / сеть
FINAL_STATES = ('ready', 'declined', 'failed', 'cancelled')
_USER_AGENT = 'BegoFrame/1.0 (+varag)'


class VaragFatalError(VeoNonStopError):
    """Ключ, тариф, квота или нет услуги — повторы бесполезны до конца прогона."""


def _detail(resp) -> tuple:
    """(code, message, retry_after) из тела ошибки {"detail": {code, message}}."""
    try:
        body = resp.json()
    except ValueError:
        return '', (resp.text or '')[:300], None
    detail = body.get('detail') if isinstance(body, dict) else None
    if isinstance(detail, dict):
        return (str(detail.get('code') or ''), str(detail.get('message') or ''),
                detail.get('retry_after'))
    if isinstance(detail, list):                       # 422: список ошибок валидации
        msgs = [str(d.get('msg') or d) if isinstance(d, dict) else str(d) for d in detail]
        return 'VALIDATION', '; '.join(msgs)[:300], None
    return '', str(body)[:300], None


def _retry_after(resp, body_value, default: float) -> float:
    for value in (body_value, resp.headers.get('Retry-After')):
        try:
            if value is not None and value != '':
                return max(0.0, float(value))
        except (TypeError, ValueError):
            continue
    return default


def _backoff(attempt: int, retry_after: float = 0.0) -> float:
    base = max(retry_after, min(60.0, 2.0 ** attempt))
    return base * random.uniform(0.8, 1.2)


def _task_list(data) -> List[dict]:
    """Ответ GET /tasks?ids=… — список задач (сам список или под ключом jobs/tasks/items)."""
    if isinstance(data, list):
        return [t for t in data if isinstance(t, dict)]
    if isinstance(data, dict):
        for key in ('jobs', 'tasks', 'items', 'data'):
            if isinstance(data.get(key), list):
                return [t for t in data[key] if isinstance(t, dict)]
        if data.get('id'):
            return [data]
    return []


def _image_payload(path: str) -> tuple:
    """(base64, mime) картинки не больше MAX_IMAGE_BYTES: при нужде — JPEG и уменьшение."""
    raw = Path(path).read_bytes()
    mime = MIME_TYPES.get(Path(path).suffix.lower(), '')
    if len(raw) > MAX_IMAGE_BYTES or mime not in ('image/png', 'image/jpeg', 'image/webp'):
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
    return base64.b64encode(raw).decode('ascii'), mime


class _KeyState:
    """Общее на ОДИН ключ в процессе: тариф, окно задач по пулам, единый опрос, «выключено»."""

    def __init__(self):
        self.cond = threading.Condition()
        self.lock = threading.Lock()
        self.plan = ''
        self.slots: Dict[str, int] = {}               # 'video'/'image' -> слотов по тарифу
        self.account_loaded = False
        self.in_flight: Dict[str, int] = {}           # пул -> задач в работе
        self.jobs: Dict[str, Dict[str, Any]] = {}     # id -> {event, data, kind, released, misses}
        self.fatal: Dict[str, str] = {}               # 'video'/'image'/'*' -> причина
        self.poller: Optional[threading.Thread] = None


_STATES: Dict[str, _KeyState] = {}
_STATES_LOCK = threading.Lock()


def _state_for(api_key: str) -> _KeyState:
    with _STATES_LOCK:
        if api_key not in _STATES:
            _STATES[api_key] = _KeyState()
        return _STATES[api_key]


class VaragClient:
    """Картинки и видео через VARAG с интерфейсом VeoNonStopClient."""

    api_version = 1
    provider = 'varag'

    def __init__(self, kind: str = 'video', api_key: Optional[str] = None, base_url: Optional[str] = None):
        self.kind = 'image' if kind == 'image' else 'video'
        self.api_key = (api_key or os.environ.get('VARAG_API_KEY', '')).strip()
        if not self.api_key:
            raise ValueError("VARAG_API_KEY не задан (Настройки → VARAG)")
        self.base_url = (base_url or os.environ.get('VARAG_API_BASE', '') or DEFAULT_API_BASE).strip().rstrip('/')
        self.headers = {'X-API-Key': self.api_key, 'User-Agent': _USER_AGENT, 'Accept': 'application/json'}
        self._state = _state_for(self.api_key)

    # ------------------------------------------------------------------ HTTP
    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _get(self, path: str, **kw):
        return requests.get(self._url(path), headers=self.headers, timeout=kw.pop('timeout', (20, 60)), **kw)

    def _post(self, path: str, body: Optional[dict] = None, timeout=(30, 120)):
        return requests.post(self._url(path), headers={**self.headers, 'Content-Type': 'application/json'},
                             json=body if body is not None else {}, timeout=timeout)

    # -------------------------------------------------------- выключение ключа
    def _check_fatal(self, kind: Optional[str] = None) -> None:
        st = self._state
        reason = st.fatal.get('*') or st.fatal.get(kind or self.kind)
        if reason:
            raise VaragFatalError(f"VARAG выключен до конца прогона: {reason}")

    def _fatal(self, scope: str, status: int, code: str, message: str) -> None:
        reason = f"HTTP {status} {code}: {message}".strip()
        with self._state.cond:
            first = scope not in self._state.fatal
            self._state.fatal[scope] = reason
            self._state.cond.notify_all()
        if first:
            what = {'*': 'ключ', 'video': 'видео', 'image': 'картинки'}.get(scope, scope)
            print(f"\n  [VARAG] ⛔ {reason} — {what}: повторы бесполезны, проверьте ключ и тариф "
                  f"(Настройки → VARAG)")
            logging.error("VARAG disabled (%s): %s", scope, reason)

    def _handle_fatal(self, resp, code: str, message: str, kind: str) -> None:
        """401 / 402 / 403 / дневная квота — выключить ключ или вид генерации и бросить."""
        if resp.status_code == 401:
            self._fatal('*', 401, code, message)
        elif resp.status_code == 402:
            self._fatal('*', 402, code, message)
        elif resp.status_code == 403:
            self._fatal(kind, 403, code, message)
        elif resp.status_code == 429 and code in ('DAILY_QUOTA', 'QUOTA_EXHAUSTED'):
            self._fatal(kind, 429, code, message)
        else:
            return
        self._check_fatal(kind)

    # ------------------------------------------------------------------ тариф
    def _pool(self, kind: str) -> str:
        """У плана chain картинки и видео делят один пул слотов."""
        return 'shared' if self._state.plan == 'chain' else kind

    def _limit(self, pool: str) -> int:
        st = self._state
        if pool == 'shared':
            return max(1, max(st.slots.values() or [4]))
        return max(1, st.slots.get(pool) or 4)

    def get_account_info(self) -> dict:
        """Тариф ключа (GET /account). Заодно задаёт окно задач в работе по слотам."""
        self._check_fatal('*')
        resp = self._get('account')
        code, message, _ = _detail(resp) if resp.status_code != 200 else ('', '', None)
        if resp.status_code in (401, 402):
            self._handle_fatal(resp, code, message, self.kind)
        if resp.status_code != 200:
            raise VeoNonStopError(f"VARAG /account: HTTP {resp.status_code} {code}: {message}")
        data = resp.json()
        services = [str(s) for s in (data.get('services') or [])]
        st = self._state
        with st.cond:
            st.plan = str(data.get('plan') or '')
            for kind in ('video', 'image'):
                block = data.get(kind) or {}
                if isinstance(block, dict) and block.get('slots'):
                    st.slots[kind] = int(block['slots'])
            st.account_loaded = True
            st.cond.notify_all()
        if services and self.kind not in services:
            self._fatal(self.kind, 403, 'PLAN_NO_SERVICE',
                        f"тариф «{st.plan}» не включает {'видео' if self.kind == 'video' else 'картинки'}")
            self._check_fatal(self.kind)
        return {'plan': st.plan or '?', 'concurrent_tasks': self._limit(self._pool(self.kind)),
                'quota': data.get('quota'), 'expires': data.get('expires')}

    def get_account_usage(self) -> dict:
        return {}           # окно задач ведёт сам клиент; формат VeoNonStop здесь не подходит

    def _ensure_account(self) -> None:
        if self._state.account_loaded:
            return
        try:
            self.get_account_info()
        except VaragFatalError:
            raise
        except Exception as exc:                                       # noqa: BLE001
            logging.warning("VARAG: тариф не получен (%s) — окно 4 задачи", exc)
            self._state.account_loaded = True

    # ----------------------------------------------------- окно задач в работе
    def _acquire_slot(self, kind: str) -> str:
        pool = self._pool(kind)
        with self._state.cond:
            while (self._state.in_flight.get(pool, 0) >= self._limit(pool)
                   and not (self._state.fatal.get('*') or self._state.fatal.get(kind))):
                self._state.cond.wait(timeout=30)
            self._check_fatal(kind)
            self._state.in_flight[pool] = self._state.in_flight.get(pool, 0) + 1
        return pool

    def _release_slot(self, pool: str) -> None:
        with self._state.cond:
            self._state.in_flight[pool] = max(0, self._state.in_flight.get(pool, 0) - 1)
            self._state.cond.notify_all()

    def _finish_job(self, job: Dict[str, Any]) -> None:
        """Задача закончилась (или брошена) — её слот свободен для следующей."""
        with self._state.lock:
            if job.get('released'):
                return
            job['released'] = True
        self._release_slot(job['pool'])

    # ------------------------------------------------------------ отправка
    def _submit(self, kind: str, path: str, body: dict) -> str:
        """POST задачи с локальной очередью на SLOTS_BUSY. Возвращает id принятой задачи."""
        self._ensure_account()
        body = {**body, 'client_ref': f"bego-{uuid.uuid4().hex[:24]}"}
        slot_wait = float(os.environ.get('VARAG_SLOT_WAIT', '') or 1800)
        started = time.time()
        attempt = 0
        while True:
            self._check_fatal(kind)
            pool = self._acquire_slot(kind)
            try:
                resp = self._post(path, body)
            except requests.RequestException as exc:
                self._release_slot(pool)
                attempt += 1
                if attempt >= MAX_ATTEMPTS:
                    raise VeoNonStopTimeoutError(f"VARAG: сеть недоступна: {exc}") from exc
                time.sleep(_backoff(attempt))
                continue
            if resp.status_code in (200, 201, 202):
                try:
                    data = resp.json()
                except ValueError:
                    data = {}
                jid = str(data.get('id') or '')
                if not jid:
                    self._release_slot(pool)
                    raise VeoNonStopError(f"VARAG: в ответе нет id задачи: {str(data)[:300]}")
                if data.get('warning'):
                    logging.warning("VARAG %s: %s %s", jid, data.get('warning_code'), data.get('warning'))
                job = {'event': threading.Event(), 'data': data, 'kind': kind, 'pool': pool,
                       'released': False, 'misses': 0}
                with self._state.lock:
                    self._state.jobs[jid] = job
                if data.get('state') in FINAL_STATES:
                    job['event'].set()
                    self._finish_job(job)
                self._ensure_poller()
                logging.info("VARAG submitted %s %s -> %s (usage %s)", kind, path, jid, data.get('usage'))
                return jid

            self._release_slot(pool)
            code, message, body_retry = _detail(resp)
            self._handle_fatal(resp, code, message, kind)
            if resp.status_code == 429 and code == 'SLOTS_BUSY':
                # Сервер задачу не создал. Своих задач в работе нет — слоты заняты кем-то
                # ещё на этом ключе: ждём; есть свои — ждём, пока одна из них закончится.
                if time.time() - started > slot_wait:
                    raise VeoNonStopRateLimitError(f"VARAG: нет свободного слота дольше {slot_wait:.0f} с")
                with self._state.cond:
                    own = self._state.in_flight.get(pool, 0)
                    wait = min(60.0, _retry_after(resp, body_retry, 10.0)) if not own else 30.0
                    logging.info("VARAG: SLOTS_BUSY (своих в работе %d) — жду %.0f с", own, wait)
                    self._state.cond.wait(timeout=wait)
                continue
            if resp.status_code in (429, 500, 502, 503, 504):
                attempt += 1
                if attempt >= MAX_ATTEMPTS:
                    if resp.status_code == 429:
                        raise VeoNonStopRateLimitError(f"VARAG rate limit: {code} {message}")
                    raise VeoNonStopTimeoutError(f"VARAG: HTTP {resp.status_code} {code}: {message}")
                wait = _backoff(attempt, _retry_after(resp, body_retry, 2.0))
                logging.warning("VARAG POST %s: HTTP %s %s — повтор через %.0f с", path, resp.status_code,
                                code, wait)
                time.sleep(wait)
                continue
            # 400 / 404 / 413 / 422 — запрос неверен, повтор той же формы бесполезен
            raise VeoNonStopError(f"VARAG отклонил задачу ({resp.status_code} {code}): {message}")

    # ------------------------------------------------------------ единый опрос
    def _ensure_poller(self) -> None:
        st = self._state
        with st.lock:
            if st.poller is None or not st.poller.is_alive():
                st.poller = threading.Thread(target=self._poll_loop, daemon=True, name='varag-poll')
                st.poller.start()

    def _poll_loop(self) -> None:
        """Правило 2: все активные задачи ключа — одним запросом раз в POLL_SEC."""
        st = self._state
        while True:
            time.sleep(POLL_SEC)
            with st.lock:
                active = {jid: j for jid, j in st.jobs.items() if not j['event'].is_set()}
            if not active:
                continue
            ids = list(active)
            for i in range(0, len(ids), IDS_PER_POLL):
                chunk = ids[i:i + IDS_PER_POLL]
                try:
                    resp = self._get('tasks', params={'ids': ','.join(chunk)})
                except requests.RequestException as exc:
                    logging.debug("VARAG poll: %s", exc)
                    continue
                if resp.status_code == 200:
                    try:
                        tasks = _task_list(resp.json())
                    except ValueError:
                        continue
                    seen = set()
                    for task in tasks:
                        jid = str(task.get('id') or '')
                        job = active.get(jid)
                        if not job:
                            continue
                        seen.add(jid)
                        job['data'] = task
                        job['misses'] = 0
                        if task.get('state') in FINAL_STATES:
                            job['event'].set()
                            self._finish_job(job)
                    for jid in chunk:
                        if jid in seen:
                            continue
                        job = active[jid]
                        job['misses'] += 1
                        if job['misses'] >= 5:          # сервер задачу не знает
                            job['data'] = {'id': jid, 'state': 'failed', 'error_code': 'JOB_NOT_FOUND'}
                            job['event'].set()
                            self._finish_job(job)
                elif resp.status_code == 401:
                    code, message, _ = _detail(resp)
                    self._fatal('*', 401, code, message)
                    for job in active.values():
                        job['event'].set()
                        self._finish_job(job)
                    break
                elif resp.status_code in (429, 503):
                    code, _, body_retry = _detail(resp)
                    time.sleep(min(60.0, _retry_after(resp, body_retry, 10.0)))
                    break

    def _wait(self, task_id: str, timeout: float) -> dict:
        """Дождаться финального состояния задачи; таймаут — hard-cancel (правило 6)."""
        with self._state.lock:
            job = self._state.jobs.get(task_id)
        if job is None:                    # задача не из этого процесса — просто следим
            job = {'event': threading.Event(), 'data': {}, 'kind': self.kind, 'pool': self._pool(self.kind),
                   'released': True, 'misses': 0}
            with self._state.lock:
                self._state.jobs[task_id] = job
        self._ensure_poller()
        if not job['event'].wait(timeout):
            self.cancel_task(task_id)
            self._finish_job(job)
            raise VeoNonStopTimeoutError(f"Task {task_id} timed out after {timeout:.0f}s (снята hard-cancel)")
        self._finish_job(job)
        self._check_fatal(job.get('kind'))
        data = job.get('data') or {}
        state = data.get('state')
        if state == 'ready':
            return data
        code = str(data.get('error_code') or data.get('code') or state or '')
        message = str(data.get('error') or data.get('message') or data.get('detail') or '')
        if state == 'declined':
            # слова «content policy» стадии опознают как цензуру и переписывают промпт
            raise VeoNonStopGenerationError(f"Task {task_id} declined: content policy {code} {message}".strip())
        if state == 'cancelled':
            raise VeoNonStopError(f"Task {task_id} cancelled: {code} {message}".strip())
        raise VeoNonStopGenerationError(f"Task {task_id} failed: {code}: {message}".strip())

    # ------------------------------------------------------------- скачивание
    def _download(self, task_id: str, url: str, out: Path) -> None:
        """Скачать результат по временной ссылке (без ключа). 410 — обновить ссылку один раз."""
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(out.suffix + '.part')
        refreshed = False
        last = ''
        for attempt in range(1, 5):
            try:
                with requests.get(url, stream=True, timeout=(30, 300),
                                  headers={'User-Agent': _USER_AGENT}) as resp:
                    if resp.status_code == 410 and not refreshed:
                        refreshed = True
                        fresh = self._refresh_url(task_id)
                        if fresh:
                            url = fresh
                            continue
                    if resp.status_code != 200:
                        code, message, _ = _detail(resp)
                        last = f"HTTP {resp.status_code} {code}: {message}"
                        if resp.status_code in (404, 410):
                            break
                        raise requests.HTTPError(last)
                    with open(tmp, 'wb') as fh:
                        for chunk in resp.iter_content(256 * 1024):
                            fh.write(chunk)
                tmp.replace(out)
                return
            except (requests.RequestException, OSError) as exc:
                last = str(exc)
                tmp.unlink(missing_ok=True)
                time.sleep(_backoff(attempt))
        raise VeoNonStopError(f"VARAG download {task_id}: {last}")

    def _refresh_url(self, task_id: str) -> str:
        try:
            resp = self._get(f'tasks/{task_id}')
            if resp.status_code == 200:
                data = resp.json()
                with self._state.lock:
                    job = self._state.jobs.get(task_id)
                    if job is not None:
                        job['data'] = data
                return str(data.get('url') or '')
        except (requests.RequestException, ValueError):
            pass
        return ''

    # ------------------------------------------------------------- параметры
    @staticmethod
    def _aspect(aspect_ratio: Optional[str]) -> str:
        from units.frame_format import resolve
        fmt = resolve(aspect_ratio)
        return fmt if fmt in ('16:9', '9:16') else '16:9'

    @staticmethod
    def _prompt(prompt: str, aspect: str) -> str:
        """Промпт со строкой формата в конце, не длиннее лимита сервиса."""
        from units.frame_format import with_frame_format
        text = with_frame_format(prompt, aspect)
        if len(text) > MAX_PROMPT_CHARS:
            tail = with_frame_format('', aspect)
            text = with_frame_format(str(prompt)[:MAX_PROMPT_CHARS - len(tail) - 1].rstrip(), aspect)
        return text

    # --------------------------------------------------------------- картинки
    def generate_banana(self, prompt: str, output_path: str, aspect_ratio: Optional[str] = None,
                        num_images: int = 1, model_key: Optional[str] = None,
                        reference_images: Optional[List[Dict[str, str]]] = None,
                        use_all_ref_images: bool = False) -> str:
        """Картинка по тексту (POST /image/generate). Имя и параметры — для совместимости
        со стадиями: модель у VARAG не выбирается, референсы не используются."""
        aspect = self._aspect(aspect_ratio)
        jid = self._submit('image', 'image/generate', {'prompt': self._prompt(prompt, aspect),
                                                       'aspect_ratio': aspect})
        timeout = float(os.environ.get('VARAG_IMAGE_POLL_TIMEOUT', '') or 900)
        data = self._wait(jid, timeout)
        url = str(data.get('url') or data.get('image_url') or '')
        if not url:
            raise VeoNonStopGenerationError(f"Task {jid} failed: готово, но без ссылки на файл")
        out = Path(output_path)
        raw_path = out.with_name(out.stem + '.varag')
        self._download(jid, url, raw_path)
        try:
            # сервис отдаёт webp, стадия ждёт файл со своим расширением (обычно .png)
            from PIL import Image
            fmt = {'.jpg': 'JPEG', '.jpeg': 'JPEG', '.webp': 'WEBP'}.get(out.suffix.lower(), 'PNG')
            with Image.open(raw_path) as im, atomic_target(out) as tmp:
                im = im.convert('RGB') if fmt == 'JPEG' else im
                im.save(tmp, format=fmt)
        finally:
            raw_path.unlink(missing_ok=True)
        with self._state.lock:
            self._state.jobs.pop(jid, None)
        logging.info("VARAG image %s -> %s", jid, out)
        return BananaResult(str(out))

    def generate_imagen(self, prompt: str, output_path: str, aspect_ratio: Optional[str] = None) -> str:
        return self.generate_banana(prompt, output_path, aspect_ratio)

    def upscale_banana(self, *_a, **_kw) -> str:
        raise VeoNonStopError("Апскейл картинок у VARAG не поддерживается")

    # ------------------------------------------------------------------ видео
    def submit_text_to_video(self, prompt: str, aspect_ratio: Optional[str] = None,
                             count: int = 1, duration=None) -> str:
        """Длину клипа VARAG не принимает — `duration` игнорируется."""
        aspect = self._aspect(aspect_ratio)
        return self._submit('video', 'video/text-to-video', {'prompt': self._prompt(prompt, aspect),
                                                             'aspect_ratio': aspect})

    def submit_image_to_video(self, image_path: str, prompt: str, aspect_ratio: Optional[str] = None,
                              count: int = 1, duration=None) -> str:
        aspect = self._aspect(aspect_ratio)
        b64, mime = _image_payload(image_path)
        return self._submit('video', 'video/image-to-video', {
            'prompt': self._prompt(prompt, aspect), 'image_base64': b64, 'image_mime': mime,
            'aspect_ratio': aspect})

    def submit_batch_frame(self, *_a, **_kw) -> str:
        raise VeoNonStopError("VARAG не умеет переход между двумя кадрами")

    def submit_multi_image_to_video(self, *_a, **_kw) -> str:
        raise VeoNonStopError("Видео по референсам через VARAG в программе не подключено")

    def submit_multi_image_to_video_from_ref_dir(self, *_a, **_kw) -> str:
        raise VeoNonStopError("Видео по референсам через VARAG в программе не подключено")

    def submit_upsample_video(self, *_a, **_kw) -> str:
        raise VeoNonStopError("Апскейл видео у VARAG не поддерживается — выключите его в настройках видео")

    def poll_video_status(self, task_id: str, timeout: Optional[int] = None,
                          interval: Optional[int] = None, hung_timeout: int = 120) -> dict:
        """Ждать готовности (опрашивает общий цикл). Ответ — в формате VeoNonStop."""
        timeout = timeout or float(os.environ.get('VARAG_POLL_TIMEOUT', '') or 3600)
        self._wait(task_id, timeout)
        return {'status': 'completed', 'task_id': task_id,
                'videos': [{'mediaGenerationId': None, 'fifeUrl': None}]}

    def download_video(self, task_id: str, output_path: str, video_index: int = 0) -> str:
        with self._state.lock:
            job = self._state.jobs.get(task_id) or {}
        url = str((job.get('data') or {}).get('url') or '') or self._refresh_url(task_id)
        if not url:
            raise VeoNonStopError(f"VARAG download {task_id}: нет ссылки (задача не готова?)")
        out = Path(output_path)
        self._download(task_id, url, out)
        if not _verify_video_integrity(out):
            out.unlink(missing_ok=True)
            raise VeoNonStopError(f"VARAG download {task_id}: файл не прошёл проверку ffprobe")
        with self._state.lock:
            self._state.jobs.pop(task_id, None)
        logging.info("VARAG video %s -> %s", task_id, output_path)
        return str(out)

    # --------------------------------------------------------- снятие задач
    def cancel_task(self, task_id: str) -> bool:
        """Снять задачу сразу, в том числе running (hard-cancel: квота не списывается)."""
        try:
            resp = self._post(f'tasks/{task_id}/hard-cancel', timeout=(20, 60))
        except requests.RequestException:
            return False
        if resp.status_code == 200:
            with self._state.lock:
                job = self._state.jobs.get(task_id)
            if job is not None:
                job['data'] = {**(job.get('data') or {}), 'state': 'cancelled', 'error_code': 'HARD_CANCELLED'}
                job['event'].set()
                self._finish_job(job)
            return True
        return False

    def _hard_cancel_ids(self, ids: List[str]) -> int:
        total = 0
        for i in range(0, len(ids), HARD_CANCEL_IDS):
            try:
                resp = self._post('tasks/hard-cancel', {'ids': ids[i:i + HARD_CANCEL_IDS]}, timeout=(20, 60))
                if resp.status_code == 200:
                    total += int((resp.json() or {}).get('cancelled_count') or 0)
            except (requests.RequestException, ValueError) as exc:
                logging.warning("VARAG hard-cancel: %s", exc)
        return total

    def _own_active(self, kind: Optional[str] = None) -> List[str]:
        with self._state.lock:
            return [jid for jid, j in self._state.jobs.items()
                    if not j['event'].is_set() and (kind is None or j.get('kind') == kind)]

    def cancel_all_tasks(self) -> int:
        """Снять задачи ЭТОГО процесса (Ctrl+C, остановка стадии). Все задачи ключа —
        `hard_cancel_all` («Жёсткий стоп»)."""
        return self._hard_cancel_ids(self._own_active())

    def cancel_all_image_tasks(self) -> int:
        return self._hard_cancel_ids(self._own_active('image'))

    def hard_cancel_all(self) -> int:
        """Снять ВСЕ активные задачи ключа (POST /tasks/hard-cancel {"all": true})."""
        resp = self._post('tasks/hard-cancel', {'all': True}, timeout=(20, 60))
        if resp.status_code != 200:
            code, message, _ = _detail(resp)
            raise VeoNonStopError(f"VARAG hard-cancel all: HTTP {resp.status_code} {code}: {message}")
        return int((resp.json() or {}).get('cancelled_count') or 0)


def varag_selected(kind: Optional[str] = None) -> bool:
    """Выбран ли VARAG провайдером 'image' / 'video' (None — хоть одним) по окружению запуска."""
    kinds = (kind,) if kind else ('image', 'video')
    return any(os.environ.get(f'BEGO_{k.upper()}_PROVIDER', '').strip().lower() == 'varag' for k in kinds)


def varag_ready(kind: Optional[str] = None) -> bool:
    return varag_selected(kind) and bool(os.environ.get('VARAG_API_KEY', '').strip())


def build_varag_cfg(kind: str) -> List[Dict[str, Any]]:
    """Конфигурация клиентов в формате стадий: `[{client, mp, sem, label, slot}]`."""
    kind = 'image' if kind == 'image' else 'video'
    try:
        workers = max(1, int(os.environ.get(f'VARAG_{kind.upper()}_PARALLEL', '') or 4))
    except ValueError:
        workers = 4
    client = VaragClient(kind)
    try:
        acc = client.get_account_info()
        workers = max(1, min(workers, int(acc.get('concurrent_tasks') or workers)))
        print(f"  [VARAG] {'картинки' if kind == 'image' else 'видео'} | тариф {acc.get('plan')} "
              f"(квота: {acc.get('quota') or '?'}) | рабочих {workers}")
    except VaragFatalError:
        raise
    except Exception as exc:                                       # noqa: BLE001
        workers = min(workers, 4)
        print(f"  [VARAG] тариф не получен ({exc}) — рабочих {workers}")
    sem = threading.Semaphore(workers)
    return [{'client': client, 'mp': workers, 'sem': sem, 'sem_slots': workers,
             'label': 'VARAG', 'slot': 1}]
