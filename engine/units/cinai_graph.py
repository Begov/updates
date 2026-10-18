"""CinAI Workflow API — прямой транспорт генерации, без агентного раннера.

Зачем модуль: 15.08.2026 CinAI перевёл `/api/agent/dispatch/stream` на
агентный toolloop. Тот путь ЛОМАЕТ пайплайн: LLM переписывает промпт (стиль и
попклиповая связность Pass 2 умирают) и задаёт опросник `ask_human`. Студийный
редактор воркфлоу ходит другим маршрутом — без агента:

    POST /api/entities/Workflow            -> workflow_id
    PUT  /api/entities/Workflow/{id}       -> граф: ноды + рёбра
    POST /api/executions/submit            -> execution_id + stream_url
    GET  /api/executions/{id}/stream       -> SSE до execution_completed

Промпт доходит до генератора ДОСЛОВНО (проверено), опросника нет.
Контракт, спеки моделей и грабли: docs/CINAI_WORKFLOW_API_PLAN.md.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

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
    retry_after_seconds,
)


# Офлайн-снимок каталога: страховка, когда /api/entities/Model недоступен.
CATALOG_PATH = Path(__file__).resolve().parents[1] / "data" / "cinai_models_catalog.json"

# Порядок предпочтения имени порта картинки — реальное имя всегда берётся из
# спеки модели, эти значения только для сообщений об ошибке.
KNOWN_IMAGE_PORTS = ("image", "image_urls")

# ── Глобальный бюджет параллелизма ─────────────────────────────────────────
# Лимит одновременных выполнений задан ТАРИФОМ и меняется: 16.08.2026 CinAI
# срезал Pro с 6 до 4. Держать его в коде нельзя — будем слать больше, чем
# разрешено, и лишнее просто повиснет в их очереди. Поэтому спрашиваем
# аккаунт (getBillingBalance.max_concurrent_executions) и подстраиваемся.
# Стадии пайплайна ходят параллельно (инфографика + ImageGen + цепочка i2v),
# поэтому семафор ОДИН на процесс: каждое выполнение (submit → completed)
# держит слот, лишние потоки ждут. Uploads слот не занимают.
_EXECUTION_LIMIT_MAX = 12          # верхняя планка на случай странных ответов


def _configured_limit() -> int:
    try:
        return max(1, min(_EXECUTION_LIMIT_MAX, int(
            getattr(config, "CINAI_MAX_CONCURRENT_EXECUTIONS", 4) or 4)))
    except (TypeError, ValueError):
        return 4


_EXECUTION_LIMIT = _configured_limit()
_EXECUTION_SLOTS = threading.BoundedSemaphore(_EXECUTION_LIMIT)
_LIMIT_LOCK = threading.Lock()
_LIMIT_SYNCED = False


def account_execution_limit(client: Optional["CinAIWorkflowClient"] = None) -> int:
    """Реальный лимит тарифа; при недоступности — значение из настроек."""
    try:
        owner = client or CinAIWorkflowClient()
        balance = owner.billing_balance()
        value = int(balance.get("max_concurrent_executions") or 0)
        if value > 0:
            return max(1, min(_EXECUTION_LIMIT_MAX, value))
    except Exception as exc:
        logging.debug("CinAI: лимит параллели не прочитан (%s)", exc)
    return _configured_limit()


def sync_execution_limit(client: Optional["CinAIWorkflowClient"] = None,
                         *, force: bool = False) -> int:
    """Один раз за процесс подтянуть лимит тарифа под семафор."""
    global _EXECUTION_LIMIT, _EXECUTION_SLOTS, _LIMIT_SYNCED
    with _LIMIT_LOCK:
        if _LIMIT_SYNCED and not force:
            return _EXECUTION_LIMIT
        limit = account_execution_limit(client)
        _LIMIT_SYNCED = True
        if limit != _EXECUTION_LIMIT:
            logging.info("CinAI: лимит параллели тарифа %s (было %s) — "
                         "подстраиваю", limit, _EXECUTION_LIMIT)
            _EXECUTION_LIMIT = limit
            _EXECUTION_SLOTS = threading.BoundedSemaphore(limit)
        return _EXECUTION_LIMIT


@contextmanager
def execution_slot():
    """Слот аккаунта на одно выполнение. Держать от submit до конца стрима."""
    slots = _EXECUTION_SLOTS
    slots.acquire()
    try:
        yield
    finally:
        slots.release()

# События SSE, которые стоит показывать пользователю в логе.
_PROGRESS_EVENTS = ("node_started", "node_completed", "level_started",
                    "execution_progress", "execution_started")

_MEDIA_RE = re.compile(r'https://[^"\\\s]+')


class _StreamDropped(Exception):
    """Внутренний сигнал: соединение с потоком событий оборвалось.

    Наружу не выходит — обрабатывается в `_stream` (спросить сервер о запуске)."""


class CinAIWorkflowError(CinAIError):
    """Ошибка Workflow API, не относящаяся к транспортным сбоям."""


class CinAIUnsupportedError(CinAIError):
    """Модель/режим/настройка вне снятого контракта."""


# ===========================================================================
# Каталог моделей
# ===========================================================================

class ModelCatalog:
    """Спеки моделей из `/api/entities/Model` с офлайн-фолбэком.

    Порт картинки, формат duration и ключ соотношения сторон у моделей
    РАЗНЫЕ (см. грабли в плане) — поэтому берём их из спеки, а не из кода.
    """

    _cache: Optional["ModelCatalog"] = None

    def __init__(self, models: Sequence[Dict[str, Any]]):
        self.by_slug: Dict[str, Dict[str, Any]] = {
            str(m.get("slug")): m for m in models if m.get("slug")
        }

    # -- загрузка -----------------------------------------------------------

    @classmethod
    def load(cls, client: Optional["CinAIWorkflowClient"] = None,
             force: bool = False) -> "ModelCatalog":
        if cls._cache is not None and not force:
            return cls._cache
        models: List[Dict[str, Any]] = []
        try:
            owner = client or CinAIWorkflowClient()
            models = owner.list_models()
        except Exception as exc:
            logging.warning("CinAI: live model catalog unavailable (%s), using offline snapshot",
                            _clean_error(str(exc), 120))
        if not models and CATALOG_PATH.is_file():
            try:
                models = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                logging.warning("CinAI: offline catalog unreadable: %s", exc)
        if not models:
            raise CinAIWorkflowError(
                "CinAI model catalog is unavailable (neither live API nor "
                f"offline snapshot {CATALOG_PATH.name})"
            )
        cls._cache = cls(models)
        return cls._cache

    # -- доступ к спеке -----------------------------------------------------

    def spec(self, slug: str) -> Dict[str, Any]:
        model = self.by_slug.get(str(slug or "").strip())
        if model is None:
            raise CinAIUnsupportedError(f"Unknown CinAI model: {slug!r}")
        return model

    def ports_in(self, slug: str) -> List[Dict[str, Any]]:
        return list(self.spec(slug).get("input_ports") or [])

    def ports_out(self, slug: str) -> List[Dict[str, Any]]:
        return list(self.spec(slug).get("output_ports") or [])

    def image_port(self, slug: str) -> Optional[Dict[str, Any]]:
        """Входной порт картинок. None — модель их не принимает вовсе."""
        for port in self.ports_in(slug):
            if port.get("type") == "image":
                return port
        return None

    def image_port_id(self, slug: str) -> Optional[str]:
        port = self.image_port(slug)
        return port.get("id") if port else None

    def max_images(self, slug: str) -> int:
        port = self.image_port(slug)
        if not port:
            return 0
        try:
            return int(port.get("maxItems") or 1)
        except (TypeError, ValueError):
            return 1

    def settings(self, slug: str) -> Dict[str, Dict[str, Any]]:
        return {str(s.get("id")): s for s in (self.spec(slug).get("settings") or [])}

    def option_values(self, slug: str, setting_id: str) -> List[str]:
        setting = self.settings(slug).get(setting_id)
        if not setting:
            return []
        values: List[str] = []
        for opt in setting.get("options") or []:
            values.append(str(opt.get("value")) if isinstance(opt, dict) else str(opt))
        return values

    def ratio_key(self, slug: str) -> Optional[str]:
        """`aspect_ratio` у большинства, но `ratio` у seedance-2-5, а у
        seedream-семейства ориентация задаётся через `size`."""
        keys = self.settings(slug)
        for candidate in ("aspect_ratio", "ratio"):
            if candidate in keys:
                return candidate
        return None

    def duration_key(self, slug: str) -> Optional[str]:
        return "duration" if "duration" in self.settings(slug) else None

    def credit_multiplier(self, slug: str, setting_id: str, value: Any) -> float:
        setting = self.settings(slug).get(setting_id) or {}
        for opt in setting.get("options") or []:
            if isinstance(opt, dict) and str(opt.get("value")) == str(value):
                try:
                    return float(opt.get("credit_multiplier") or 1.0)
                except (TypeError, ValueError):
                    return 1.0
        return 1.0

    def estimate_credits(self, slug: str, settings_values: Dict[str, Any]) -> float:
        """Цена = cost_per_unit × множители выбранных опций (charge_phase=pre)."""
        pricing = self.spec(slug).get("pricing") or {}
        try:
            base = float(pricing.get("cost_per_unit") or 0)
        except (TypeError, ValueError):
            base = 0.0
        total = base
        for key, value in (settings_values or {}).items():
            total *= self.credit_multiplier(slug, key, value)
        return round(total, 2)

    # -- сборка settingsValues ---------------------------------------------

    def nearest_duration(self, slug: str, seconds: float) -> Optional[str]:
        """Ближайшая длительность из тех, что модель реально умеет.

        Режиссёр нарезает клипы по смыслу (в вилке из GUI), а мы раньше слали
        ОДНУ длину на все клипы — и потом растягивали/ужимали видео ретаймингом.
        Теперь под каждый клип заказываем свою: omni принимает целые 3-10с,
        seedance — только 5s/10s, grok — 6/10/15. Возвращаем строку в формате
        модели или None, если у модели длительности нет вовсе.
        """
        options = self.option_values(slug, "duration")
        if not options:
            return None
        try:
            target = float(seconds)
        except (TypeError, ValueError):
            return None
        if target <= 0:
            return None

        def as_number(option: str) -> Optional[float]:
            try:
                return float(str(option).strip().rstrip("sS"))
            except ValueError:
                return None

        numeric = [(as_number(o), o) for o in options]
        numeric = [(value, option) for value, option in numeric if value is not None]
        if not numeric:
            return None
        # Подбор под клип имеет смысл только у моделей с ПЛАВНЫМ рядом длин
        # (omni: 3,4,5…10). У seedance-lite всего 5s/10s, у grok 6/10/15 —
        # там «ближайшее» либо совпадает с настройкой, либо раздувает клип
        # вдвое и тратит кредиты впустую. Такие модели оставляем на настройке.
        if len(numeric) < 4:
            return None
        # при равном отдалении берём БОЛЬШЕЕ: лишнее подрежем, недостачу
        # пришлось бы тянуть замедлением — оно заметнее на глаз
        return min(numeric, key=lambda pair: (abs(pair[0] - target), -pair[0]))[1]

    def build_settings(self, slug: str, desired: Dict[str, Any]) -> Dict[str, Any]:
        """Отбирает только те настройки, которые модель реально принимает.

        Нормализует формат: duration у одних моделей `"5"`, у других `"5s"`;
        ratio живёт под своим ключом. Значение вне списка опций — ошибка
        ДО отправки (charge_phase=pre, фейл всё равно списывает кредиты).
        """
        known = self.settings(slug)
        out: Dict[str, Any] = {}
        for key, value in (desired or {}).items():
            if value is None or key not in known:
                continue
            options = self.option_values(slug, key)
            if options:
                value = self._coerce_option(key, value, options, slug)
            out[key] = value
        return out

    @staticmethod
    def _coerce_option(key: str, value: Any, options: Sequence[str], slug: str) -> str:
        text = str(value).strip()
        if text in options:
            return text
        # duration: "5" <-> "5s" — формат зависит от модели
        if key == "duration":
            digits = text.rstrip("sS")
            for candidate in (digits, f"{digits}s"):
                if candidate in options:
                    return candidate
        # регистр (Auto/auto)
        lowered = {opt.lower(): opt for opt in options}
        if text.lower() in lowered:
            return lowered[text.lower()]
        raise CinAIUnsupportedError(
            f"Model {slug} does not support {key}={value!r}; allowed: {list(options)}"
        )


# ===========================================================================
# Билдеры графа
# ===========================================================================

def _node_id(prefix: str) -> str:
    return f"{prefix}-{int(time.time() * 1000)}-0.{uuid.uuid4().hex[:16]}"


def build_generator_node(
    catalog: ModelCatalog,
    slug: str,
    prompt: str,
    settings_values: Dict[str, Any],
    *,
    position: Optional[Dict[str, int]] = None,
    node_id: Optional[str] = None,
    connected_ports: Optional[Sequence[str]] = None,
    number_of_images: int = 1,
) -> Dict[str, Any]:
    """Нода генератора. `promptText` уходит генератору ДОСЛОВНО."""
    spec = catalog.spec(slug)
    group = str(spec.get("model_group") or "")
    node_type = "video-generator" if "video" in group else "image-generator"
    nid = node_id or _node_id(node_type)
    # Формат кадра — и настройкой (если модель её знает), и последней строкой промпта:
    # модель без aspect_ratio иначе молча отдаёт квадрат (см. cinai_stills).
    from units.frame_format import with_frame_format
    prompt = with_frame_format(prompt, (settings_values or {}).get("aspect_ratio"))
    data: Dict[str, Any] = {
        "label": spec.get("name") or slug,
        "modelSlug": slug,
        "promptText": prompt,
        "settingsValues": catalog.build_settings(slug, settings_values),
        "inputs": catalog.ports_in(slug),
        "outputs": catalog.ports_out(slug),
    }
    if node_type == "image-generator":
        data["numberOfImages"] = int(number_of_images or 1)
    if connected_ports:
        data["connectedPorts"] = list(connected_ports)
    return {"id": nid, "type": node_type,
            "position": position or {"x": 0, "y": 0}, "data": data}


def build_image_input_node(
    url: str, *, node_id: Optional[str] = None,
    position: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """INPUT-нода с готовой картинкой (тип `imageInput`, выход `image`).

    Не тратит ни кредитов, ни времени, только отдаёт URL следующей ноде.
    Формат — как у редактора cinai.tv: прежний тип `image-input` с выходом
    `output` сервер с 10.2026 отвергает («Unknown node type: image-input»),
    и вместе с ним падали ремиксер CinAI и картинка → видео CinAI.
    """
    nid = node_id or _node_id("imageInput")
    return {
        "id": nid, "type": "imageInput",
        "position": position or {"x": 0, "y": 0},
        "data": {
            "label": "Image",
            "previewUrl": url,
            "fileUrl": url,
            "imageUrl": url,
            "mimeType": "image/png",
            "outputs": [{"id": "image", "label": "Image", "type": "image"}],
        },
    }


def build_edge(source_id: str, target_id: str, target_handle: str,
               *, port_type: str = "image", source_handle: str = "image") -> Dict[str, Any]:
    """Ребро. `target_handle` — имя входного порта КОНКРЕТНОЙ модели,
    `source_handle` — выход источника (у `imageInput` он называется `image`)."""
    return {
        "id": f"e-{source_id}-{source_handle}-{target_id}-{target_handle}",
        "source": source_id, "sourceHandle": source_handle,
        "target": target_id, "targetHandle": target_handle,
        "type": "smart", "animated": False, "className": "edge-runway",
        "data": {"portType": port_type,
                 "sourceNodeId": source_id, "targetNodeId": target_id},
        "style": {"stroke": "#60A5FA", "strokeWidth": 2.5,
                  "strokeLinecap": "round", "strokeLinejoin": "round"},
    }


def attach_images(
    catalog: ModelCatalog, target_node: Dict[str, Any],
    image_urls: Sequence[str],
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """INPUT-ноды + рёбра для списка картинок (i2v / first_last / reference).

    Порядок списка = порядок кадров: для first_last [0] — первый кадр.
    """
    slug = target_node["data"]["modelSlug"]
    port = catalog.image_port(slug)
    if port is None:
        raise CinAIUnsupportedError(
            f"Model {slug} has no image input port — i2v/reference is impossible; "
            "use an img2img/banana/seedream model"
        )
    limit = catalog.max_images(slug)
    if len(image_urls) > limit:
        raise CinAIUnsupportedError(
            f"Model {slug} accepts at most {limit} source image(s), got {len(image_urls)}"
        )
    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    handle = str(port.get("id"))
    for index, url in enumerate(image_urls):
        node = build_image_input_node(url, position={"x": 0, "y": index * 260})
        nodes.append(node)
        edges.append(build_edge(node["id"], target_node["id"], handle))
    ports = set(target_node["data"].get("connectedPorts") or [])
    ports.add(handle)
    target_node["data"]["connectedPorts"] = sorted(ports)
    return nodes, edges


# ===========================================================================
# Транспорт
# ===========================================================================

class CinAIWorkflowClient:
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
        self.base_url = (base_url or getattr(config, "CINAI_API_BASE",
                                             "https://api.cinai.tv")).rstrip("/")
        self.app_id = app_id or getattr(config, "CINAI_APP_ID", "runway-workflows")
        self.timeout = int(timeout or getattr(config, "CINAI_VIDEO_TIMEOUT", 1200))
        self.max_retries = max(0, int(
            max_retries if max_retries is not None
            else getattr(config, "CINAI_VIDEO_MAX_RETRIES", 3)))
        self.retry_base_seconds = max(0.0, float(
            retry_base_seconds if retry_base_seconds is not None
            else getattr(config, "CINAI_VIDEO_RETRY_BASE_SECONDS", 5)))

    # -- низкий уровень -----------------------------------------------------

    def _headers(self, accept: str = "application/json") -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}",
                "X-App-Id": self.app_id, "Accept": accept,
                "Content-Type": "application/json"}

    def _refresh_auth_once(self) -> None:
        self.access_token = ensure_cinai_access_token(self.access_token,
                                                      force_refresh=True)

    def _request(self, method: str, path: str, *, _auth_retry: bool = False,
                 **kwargs: Any) -> Any:
        try:
            response = requests.request(
                method, f"{self.base_url}{path}",
                headers=self._headers(), timeout=(30, 120), **kwargs)
        except requests.RequestException as exc:
            raise CinAIRetryableError(
                f"CinAI workflow request failed: {_clean_error(str(exc))}") from exc
        if response.status_code in (401, 403):
            if not _auth_retry:
                self._refresh_auth_once()
                return self._request(method, path, _auth_retry=True, **kwargs)
            raise CinAIAuthError("CinAI access token was rejected")
        if response.status_code == 429 or response.status_code >= 500:
            raise CinAIRetryableError(
                f"CinAI workflow {path} returned HTTP {response.status_code}")
        if not response.ok:
            text = _clean_error(response.text)
            raise CinAIWorkflowError(
                f"CinAI workflow {path} failed ({response.status_code}): {text}")
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {}

    # -- операции -----------------------------------------------------------

    def billing_balance(self) -> Dict[str, Any]:
        """Баланс и лимиты тарифа (в т.ч. max_concurrent_executions).

        Лимит параллели задаётся тарифом и меняется на стороне провайдера,
        поэтому спрашиваем его, а не держим в коде."""
        try:
            response = requests.post(
                f"{self.base_url}/api/functions/getBillingBalance",
                headers=self._headers(), timeout=30)
        except requests.RequestException as exc:
            raise CinAIRetryableError(
                f"CinAI billing request failed: {_clean_error(str(exc))}") from exc
        if response.status_code == 401:
            raise CinAIAuthError("CinAI access token was rejected")
        if not response.ok:
            raise CinAIWorkflowError(
                f"CinAI billing failed ({response.status_code})")
        try:
            data = response.json()
        except ValueError as exc:
            raise CinAIWorkflowError("CinAI billing returned invalid JSON") from exc
        return data if isinstance(data, dict) else {}

    def list_models(self) -> List[Dict[str, Any]]:
        data = self._request("GET", "/api/entities/Model")
        return data if isinstance(data, list) else list(data.get("items") or [])

    def create_workflow(self, title: str = "BegoFrame") -> str:
        data = self._request("POST", "/api/entities/Workflow",
                             json={"title": title, "nodes": [], "edges": []})
        workflow_id = str(data.get("id") or "").strip()
        if not workflow_id:
            raise CinAIWorkflowError("CinAI did not return a workflow id")
        return workflow_id

    def put_graph(self, workflow_id: str, nodes: Sequence[Dict[str, Any]],
                  edges: Sequence[Dict[str, Any]], title: str = "BegoFrame") -> None:
        self._request("PUT", f"/api/entities/Workflow/{workflow_id}",
                      json={"title": title, "nodes": list(nodes),
                            "edges": list(edges)})

    def delete_workflow(self, workflow_id: str) -> bool:
        """Убираем за собой. Метод может быть не реализован — не критично."""
        try:
            self._request("DELETE", f"/api/entities/Workflow/{workflow_id}")
            return True
        except Exception:
            return False

    def submit(self, workflow_id: str, target_node_id: str,
               *, force_run: bool = True) -> Dict[str, Any]:
        data = self._request("POST", "/api/executions/submit",
                             json={"workflow_id": workflow_id, "mode": "single",
                                   "target_node_id": target_node_id,
                                   "force_run": bool(force_run)})
        if not data.get("execution_id"):
            raise CinAIWorkflowError(f"CinAI submit returned no execution_id: {data}")
        return data

    def stream_all_results(self, execution_id: str,
                           on_progress=None) -> Dict[str, Dict[str, Any]]:
        """Все выходы графа: {node_id: output}. Нужно для цепочек, где
        промежуточная нода (картинка) — такой же полезный результат.

        `on_progress(kind, payload)` — необязательный колбэк для лога: клип
        по 5 минут без единой строки выглядит как зависание."""
        outputs = self._stream(execution_id, on_progress=on_progress)
        return {node_id: self._with_primary_url(out) for node_id, out in outputs.items()}

    @staticmethod
    def _with_primary_url(output: Any) -> Any:
        """Выход с полем `url`. Часть моделей (Seedream 5 pro) отдаёт не одну
        картинку, а пачку: `{"count": 4, "items": [{"url": …}, …]}` — без этого
        такой результат считался «без медиа» и уходил в бесконечные повторы."""
        if not isinstance(output, dict) or output.get("url"):
            return output
        for item in output.get("items") or []:
            if isinstance(item, dict) and item.get("url"):
                return {**output, "url": item["url"]}
        return output

    def stream_result(self, execution_id: str, target_node_id: str,
                      on_progress=None) -> Dict[str, Any]:
        """Слушает SSE до терминального события. Возвращает выход целевой ноды.

        ВАЖНО: токен идёт query-параметром `access_token`; заголовком сервер
        отвечает 422.
        """
        outputs = self._stream(execution_id, on_progress=on_progress)
        output = self._with_primary_url(outputs.get(target_node_id))
        if not output or not output.get("url"):
            raise CinAIRetryableError(
                "CinAI execution finished without media for the target node")
        return output

    def get_execution(self, execution_id: str) -> Dict[str, Any]:
        """Карточка запуска: status / error / node_results с готовыми URL.

        Это спасательный круг после обрыва SSE: результат уже посчитан на
        сервере, и его можно забрать без повторной генерации."""
        url = f"{self.base_url}/api/executions/{execution_id}"
        try:
            response = requests.get(url, headers=self._headers(), timeout=60)
        except requests.RequestException as exc:
            raise CinAIRetryableError(
                f"CinAI execution status failed: {_clean_error(str(exc))}") from exc
        if response.status_code == 401:
            raise CinAIAuthError("CinAI access token was rejected")
        if response.status_code == 429 or response.status_code >= 500:
            raise CinAIRetryableError(
                f"CinAI execution status returned HTTP {response.status_code}")
        if not response.ok:
            raise CinAIWorkflowError(
                f"CinAI execution status failed ({response.status_code}): "
                f"{_clean_error(response.text)}")
        try:
            data = response.json()
        except ValueError as exc:
            raise CinAIRetryableError("CinAI execution status is not JSON") from exc
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _outputs_from_record(record: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
        """node_results карточки → тот же формат, что отдаёт стрим."""
        outputs: Dict[str, Dict[str, Any]] = {}
        for node_id, result in (record.get("node_results") or {}).items():
            if isinstance(result, dict) and isinstance(result.get("output"), dict):
                outputs[node_id] = result["output"]
        return outputs

    def _stream(self, execution_id: str,
                on_progress=None) -> Dict[str, Dict[str, Any]]:
        """Слушает поток; при обрыве переходит на опрос карточки запуска.

        Провайдер регулярно рвёт SSE у всех соединений разом — в его же логах
        это `Worker heartbeat timeout (240s). Execution was orphaned.` и
        `Provider ... unavailable`. Сама генерация при этом живёт (или уже
        посчитана) на сервере, поэтому вместо фейла клипа мы:
          1) спрашиваем `GET /api/executions/{id}`;
          2) `completed` — забираем готовые URL, НЕ перегенерируя;
          3) `running/queued` — снова слушаем стрим;
          4) `failed` — разбираем текст ошибки сервера (цензура / временный
             сбой) и отдаём наверх уже классифицированной."""
        outputs: Dict[str, Dict[str, Any]] = {}
        deadline = time.time() + max(60.0, float(self.timeout))
        attempt = 0
        last_error: Optional[Exception] = None
        while time.time() < deadline:
            try:
                return self._stream_once(execution_id, outputs,
                                         on_progress=on_progress)
            except _StreamDropped as exc:
                last_error = exc
                attempt += 1
                logging.warning(
                    "CinAI stream dropped (%s); asking the server about "
                    "execution %s", _clean_error(str(exc)), execution_id)
            # 1-2) что там на сервере на самом деле
            try:
                record = self.get_execution(execution_id)
            except CinAIRetryableError as exc:
                last_error = exc
                record = {}
            status = str(record.get("status") or "").lower()
            if on_progress is not None and status:
                try:
                    on_progress("server_status", {"status": status,
                                                  "progress": record.get("progress")})
                except Exception:
                    pass
            if status == "completed":
                recovered = self._outputs_from_record(record)
                if recovered:
                    logging.info(
                        "CinAI execution %s finished server-side; result "
                        "recovered without a re-run", execution_id)
                    outputs.update(recovered)
                    return outputs
            if status == "failed":
                self._raise_node_error(record)
            # 3) ещё крутится — пауза и снова слушаем
            delay = min(30.0, max(2.0, self.retry_base_seconds) * (2 ** min(attempt, 3)))
            time.sleep(min(delay, max(1.0, deadline - time.time())))
        raise CinAIRetryableError(
            f"CinAI execution stream lost: {_clean_error(str(last_error))}")

    def _stream_once(self, execution_id: str,
                     outputs: Dict[str, Dict[str, Any]],
                     on_progress=None) -> Dict[str, Dict[str, Any]]:
        url = f"{self.base_url}/api/executions/{execution_id}/stream"
        try:
            response = requests.get(
                url, params={"access_token": self.access_token},
                headers={"Accept": "text/event-stream"},
                stream=True, timeout=(30, self.timeout))
        except requests.RequestException as exc:
            raise CinAIRetryableError(
                f"CinAI execution stream failed: {_clean_error(str(exc))}") from exc
        if response.status_code == 401:
            raise CinAIAuthError("CinAI access token was rejected by execution stream")
        if response.status_code == 429 or response.status_code >= 500:
            raise CinAIRetryableError(
                f"CinAI execution stream returned HTTP {response.status_code}")
        if not response.ok:
            raise CinAIWorkflowError(
                f"CinAI execution stream failed ({response.status_code}): "
                f"{_clean_error(response.text)}")
        completed = False
        try:
            for raw in response.iter_lines(decode_unicode=True):
                line = raw or ""
                if not line or line.startswith(":") or not line.startswith("data:"):
                    continue
                try:
                    event = json.loads(line[5:].strip())
                except ValueError:
                    continue
                name = event.get("event")
                payload = event.get("data") or {}
                if on_progress is not None and name in _PROGRESS_EVENTS:
                    try:
                        on_progress(str(name), payload if isinstance(payload, dict) else {})
                    except Exception:      # лог не имеет права ронять генерацию
                        pass
                if name == "node_completed":
                    node_id = payload.get("node_id")
                    if node_id:
                        outputs[node_id] = payload.get("output") or {}
                elif name == "node_failed":
                    self._raise_node_error(payload)
                elif name == "execution_failed":
                    self._raise_node_error(payload)
                elif name == "execution_completed":
                    for node_id, result in (payload.get("node_results") or {}).items():
                        if isinstance(result, dict) and result.get("output"):
                            outputs.setdefault(node_id, result["output"])
                    status = str(payload.get("status") or "").lower()
                    if status and status != "completed":
                        self._raise_node_error(payload)
                    completed = True
                    break
        except requests.RequestException as exc:
            # обрыв соединения (ChunkedEncodingError и родня) — не фейл клипа:
            # выше спросим сервер, что стало с запуском
            raise _StreamDropped(_clean_error(str(exc))) from exc
        finally:
            response.close()
        if not completed:
            # поток закончился молча, без execution_completed
            raise _StreamDropped("stream closed before execution_completed")
        return outputs

    @staticmethod
    def _raise_node_error(payload: Dict[str, Any]) -> None:
        text = _clean_error(str(payload.get("error") or payload.get("message")
                                or payload.get("status") or payload))
        if _is_content_decline(text):
            from units.cinai_motion import CinAIVideoContentDeclinedError
            raise CinAIVideoContentDeclinedError(text)
        if _is_retryable_message(text):
            raise CinAIRetryableError(text)
        raise CinAIWorkflowError(f"CinAI execution failed: {text}")

    # -- загрузка исходников ------------------------------------------------

    def upload_image(self, image_path: str) -> str:
        """Загружает файл; transient-сбои ретраятся (политика как у генерации)."""
        last_error: Optional[Exception] = None
        for attempt in range(max(1, self.max_retries + 1)):
            try:
                return self._upload_once(image_path)
            except CinAIRetryableError as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                delay = min(60.0, self.retry_base_seconds * (2 ** attempt))
                logging.warning("CinAI upload retry %d/%d in %.1fs: %s",
                                attempt + 1, self.max_retries, delay,
                                _clean_error(str(exc)))
                if delay:
                    time.sleep(delay)
        raise CinAIRetryableError(
            f"CinAI upload retries exhausted: {_clean_error(str(last_error))}")

    def _upload_once(self, image_path: str, _auth_retry: bool = False) -> str:
        import mimetypes
        path = Path(image_path)
        if not path.is_file():
            raise FileNotFoundError(path)
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        headers = {"Authorization": f"Bearer {self.access_token}",
                   "X-App-Id": self.app_id, "Accept": "application/json"}
        try:
            with path.open("rb") as handle:
                response = requests.post(
                    f"{self.base_url}/api/integrations/Core/UploadFile",
                    headers=headers, files={"file": (path.name, handle, mime)},
                    timeout=(30, 180))
        except requests.RequestException as exc:
            raise CinAIRetryableError(
                f"CinAI upload failed: {_clean_error(str(exc))}") from exc
        if response.status_code in (401, 403):
            if not _auth_retry:
                self._refresh_auth_once()
                return self._upload_once(image_path, _auth_retry=True)
            raise CinAIAuthError("CinAI access token was rejected")
        if response.status_code == 429 or response.status_code >= 500:
            raise CinAIRetryableError(
                f"CinAI upload returned HTTP {response.status_code}")
        if not response.ok:
            raise CinAIWorkflowError(
                f"CinAI upload failed ({response.status_code}): "
                f"{_clean_error(response.text)}")
        file_url = str((response.json() or {}).get("file_url") or "").strip()
        if not file_url:
            raise CinAIWorkflowError("CinAI upload returned no file_url")
        return file_url

    # -- высокий уровень ----------------------------------------------------

    def run_graph(
        self, nodes: Sequence[Dict[str, Any]], edges: Sequence[Dict[str, Any]],
        target_node_id: str, *, title: str = "BegoFrame", cleanup: bool = True,
    ) -> Dict[str, Any]:
        """Один прогон графа: создать → залить → запустить → дождаться медиа."""
        workflow_id = self.create_workflow(title)
        try:
            self.put_graph(workflow_id, nodes, edges, title=title)
            with execution_slot():
                submission = self.submit(workflow_id, target_node_id)
                output = self.stream_result(submission["execution_id"], target_node_id)
            output["workflow_id"] = workflow_id
            output["execution_id"] = submission["execution_id"]
            return output
        finally:
            if cleanup:
                self.delete_workflow(workflow_id)

    def run_with_retries(self, build_graph, target_key: str = "target",
                         *, title: str = "BegoFrame") -> Dict[str, Any]:
        """`build_graph()` пересобирает граф на каждой попытке (свежие id).

        Контент-отказы и неподдержанные настройки не ретраятся — только
        транспорт/провайдер (charge_phase=pre, лишние попытки стоят кредитов).
        """
        last_error: Optional[Exception] = None
        for attempt in range(max(1, self.max_retries + 1)):
            graph = build_graph()
            try:
                result = self.run_graph(graph["nodes"], graph["edges"],
                                        graph[target_key], title=title)
                result["attempts"] = attempt + 1
                return result
            except (CinAIUnsupportedError, CinAIAuthError):
                raise
            except CinAIRetryableError as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                # circuit breaker провайдера сам говорит, когда откроется
                # («Provider cinapi unavailable: open (retry in 53s)»)
                delay = max(retry_after_seconds(str(exc)),
                            min(60.0, self.retry_base_seconds * (2 ** attempt)))
                logging.warning("CinAI workflow retry %d/%d in %.1fs: %s",
                                attempt + 1, self.max_retries, delay,
                                _clean_error(str(exc)))
                if delay:
                    time.sleep(delay)
            except CinAIError as exc:
                text = _clean_error(str(exc))
                if _is_content_decline(text):
                    raise
                last_error = exc
                if attempt >= self.max_retries:
                    break
                time.sleep(min(60.0, self.retry_base_seconds * (2 ** attempt)))
        raise CinAIRetryableError(
            f"CinAI workflow retries exhausted: {_clean_error(str(last_error))}")


# ===========================================================================
# Скачивание результата
# ===========================================================================

def full_size_url(url: str) -> str:
    """Ссылка на ОРИГИНАЛ вместо уменьшенной копии.

    Картинки CinAI раздаёт Cloudflare Images (`imagedelivery.net`), и последний
    кусок адреса — это ВАРИАНТ размера, а не часть имени файла. Сервис отдаёт
    нам `md` (480×267), хотя рядом по тому же адресу лежит `full` (1376×768) —
    ровно тот размер, в котором картинка и сгенерирована.

    Замер 01.09.2026 по одному адресу:
        sm 240×133 · md 480×267 · lg 960×535 · public 1366×762 · full 1376×768

    Без этой замены В РОЛИК ВСЕГДА ЕХАЛО МЫЛО: кадр 480×267 растягивался
    оживлением до 1280×720. Настройка `CINAI_QUALITY` тут ни при чём —
    генерируется картинка нормальной, теряется на скачивании.
    """
    if 'imagedelivery.net' not in url:
        return url
    head, _, variant = url.rstrip('/').rpartition('/')
    # Меняем ТОЛЬКО известные варианты-уменьшалки: иначе рискуем испортить
    # адрес, если служба когда-нибудь начнёт отдавать что-то другое.
    if head and variant in ('sm', 'md', 'lg', 'public', 'thumbnail'):
        return f'{head}/full'
    return url


def download_media(url: str, output_path: Path, *, min_bytes: int = 1024,
                   timeout: tuple = (30, 300)) -> int:
    """Скачивает медиа атомарно (tmp → replace). Возвращает размер.

    Уменьшенная копия подменяется оригиналом (`full_size_url`); если оригинал
    почему-то недоступен, скачиваем то, что дали, — кадр с мылом лучше, чем
    отсутствие кадра.
    """
    import tempfile

    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    _asked = full_size_url(url)
    try:
        response = requests.get(_asked, stream=True, timeout=timeout)
        if _asked != url and not response.ok:
            logging.warning(
                "CinAI: оригинал недоступен (HTTP %s) — качаю исходную ссылку",
                response.status_code)
            response.close()
            response = requests.get(url, stream=True, timeout=timeout)
    except requests.RequestException as exc:
        raise CinAIRetryableError(
            f"CinAI media download failed: {_clean_error(str(exc))}") from exc
    if response.status_code == 429 or response.status_code >= 500:
        raise CinAIRetryableError(
            f"CinAI media download returned HTTP {response.status_code}")
    if not response.ok:
        raise CinAIWorkflowError(
            f"CinAI media download failed ({response.status_code})")
    temp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="wb", suffix=target.suffix or ".bin",
                dir=str(target.parent), delete=False) as handle:
            temp_path = Path(handle.name)
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)
        size = temp_path.stat().st_size
        if size < min_bytes:
            raise CinAIWorkflowError(
                f"CinAI returned an empty or preview-sized file: {size} bytes")
        temp_path.replace(target)
        return size
    finally:
        response.close()
        if temp_path is not None and temp_path.exists():
            temp_path.unlink(missing_ok=True)
