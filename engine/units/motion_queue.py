"""video_queue.py — одна очередь генерации видео на прогон.

Зачем
-----
Задачи на видео сегодня раздают ТРИ независимых механизма, и каждый нанимает
своих рабочих:

  • стадия i2v — свой пул внутри `veo_video.generate_all_videos`;
  • inflight — отдельный процесс `live_veo.py` со своим пулом;
  • параллакс — новый пул на КАЖДУЮ партию кадров.

Слоты у ключа VeoNonStop одни на всех (и на картинки тоже), а считает их каждый
пул сам и про других не знает. Отсюда: взаимное ожидание, простой слотов в
хвосте партии и обнуление учёта задач в полёте, когда второй вызов
переинициализирует модульное состояние `veo_video`.

Очередь решает это одним способом: рабочие нанимаются ОДИН раз на прогон, и все
поставщики задач кладут работу сюда. Освободился слот — очередь сама берёт
следующую задачу, кто бы её ни положил.

Шлюз
----
`hold()` закрывает выдачу: рабочие ждут, задачи копятся. Нужен, когда картинки
делает VeoNonStop на том же ключе — пока идёт ImageGen, свободных слотов нет, и
лезть туда бессмысленно. `release()` открывает.

Что очередь НЕ делает
---------------------
Не знает про промпты, параллакс, claim'ы и ретраи — это забота вызывающего.
Задача сюда приходит готовым вызовом: «выполни вот это». Так очередь остаётся
маленькой и не дублирует логику `_process_single_clip`.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class VideoQueue:
    """Пул рабочих на прогон + шлюз выдачи."""

    def __init__(self, workers: int, *, name: str = 'videoq',
                 keys: Optional[List[int]] = None,
                 clients_cfg: Optional[List[Dict[str, Any]]] = None) -> None:
        self.workers = max(1, int(workers or 1))
        self.keys = list(keys or [])
        # Клиенты VeoNonStop, общие на прогон. Лежат здесь, чтобы стадия и
        # наблюдатель брали ГОТОВЫХ, а не поднимали по своему набору: лишние
        # cancel-all при старте и, что хуже, повторная инициализация лимитов
        # ключа, обнуляющая учёт задач в полёте у соседа.
        self.clients_cfg = clients_cfg or []
        self._pool = ThreadPoolExecutor(max_workers=self.workers,
                                        thread_name_prefix=name)
        self._gate = threading.Event()
        self._gate.set()                      # по умолчанию открыта
        self._lock = threading.Lock()
        self._closed = False
        self._submitted = 0
        self._done = 0
        self._holders = 0                     # сколько причин держать шлюз
        # Держали ли шлюз хоть раз за прогон. Нужно тому, кто ждёт параллакс:
        # если тот простоял под шлюзом всю генерацию кадров, работать он начал
        # только под конец, и мерить ему остаток общим таймаутом нечестно.
        self._held_ever = False
        logger.info("VideoQueue: %d рабочих, ключи %s", self.workers,
                    self.keys or 'по умолчанию')

    # ── выдача ──────────────────────────────────────────────────────────

    def put(self, fn: Callable[..., Any], *args, label: str = '', **kwargs) -> Future:
        """Положить задачу. Вернёт future с результатом вызова `fn`."""
        with self._lock:
            if self._closed:
                raise RuntimeError('VideoQueue закрыта — задачи больше не принимаются')
            self._submitted += 1

        def _runner():
            # Шлюз проверяем В РАБОЧЕМ, а не при постановке: задача может ждать
            # своей очереди дольше, чем длится ImageGen, и к моменту запуска
            # слоты уже освободятся.
            if not self._gate.is_set():
                logger.info("VideoQueue: %s ждёт открытия шлюза", label or 'задача')
                self._gate.wait()
            try:
                return fn(*args, **kwargs)
            finally:
                with self._lock:
                    self._done += 1

        return self._pool.submit(_runner)

    # ── шлюз ────────────────────────────────────────────────────────────

    def hold(self, reason: str = '') -> None:
        """Закрыть выдачу. Держателей может быть несколько — считаем их."""
        with self._lock:
            self._holders += 1
            first = self._holders == 1
            self._held_ever = True
        if first:
            self._gate.clear()
            logger.info("VideoQueue: выдача закрыта%s",
                        f" ({reason})" if reason else '')

    def release(self, reason: str = '') -> None:
        """Снять одну причину держать шлюз; при нуле — открыть."""
        with self._lock:
            self._holders = max(0, self._holders - 1)
            opened = self._holders == 0
        if opened:
            self._gate.set()
            logger.info("VideoQueue: выдача открыта%s",
                        f" ({reason})" if reason else '')

    @property
    def held(self) -> bool:
        return not self._gate.is_set()

    @property
    def was_held(self) -> bool:
        """Шлюз закрывали хоть раз за прогон (пусть и уже открыли)."""
        with self._lock:
            return self._held_ever

    # ── завершение ──────────────────────────────────────────────────────

    def close(self, *, wait: bool = True) -> None:
        """Закрыть приём. Начатые задачи доигрывают."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        # Открываем шлюз: иначе рабочие останутся ждать его вечно.
        self._gate.set()
        self._pool.shutdown(wait=wait)
        logger.info("VideoQueue: закрыта (задач принято %d, выполнено %d)",
                    self._submitted, self._done)

    @property
    def closed(self) -> bool:
        return self._closed

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                'workers': self.workers,
                'keys': list(self.keys),
                'submitted': self._submitted,
                'done': self._done,
                'held': self.held,
                'was_held': self._held_ever,
                'closed': self._closed,
            }


# ── реестр очередей прогона ─────────────────────────────────────────────
# Очередь живёт весь прогон и должна быть ОДНА на набор ключей: слоты
# принадлежат аккаунту, и считать их надо в одном месте. Реестр держит их по
# ключам, чтобы стадия и наблюдатель получили один и тот же объект.

_registry: Dict[str, VideoQueue] = {}
_registry_lock = threading.Lock()


def _registry_key(keys: Optional[List[int]]) -> str:
    return ','.join(str(k) for k in sorted(keys or [])) or 'default'


def get_queue(keys: Optional[List[int]] = None,
              workers: Optional[int] = None,
              clients_cfg: Optional[List[Dict[str, Any]]] = None) -> Optional[VideoQueue]:
    """Очередь для этих ключей. Создаётся при первом обращении с `workers`."""
    name = _registry_key(keys)
    with _registry_lock:
        queue = _registry.get(name)
        if queue is not None and not queue.closed:
            if clients_cfg and not queue.clients_cfg:
                queue.clients_cfg = clients_cfg
            return queue
        if workers is None:
            return None
        queue = VideoQueue(workers, name=f'videoq-{name}', keys=keys,
                           clients_cfg=clients_cfg)
        _registry[name] = queue
        return queue


def close_all(*, wait: bool = True) -> None:
    """Закрыть все очереди прогона (конец пайплайна)."""
    with _registry_lock:
        queues = list(_registry.values())
        _registry.clear()
    for queue in queues:
        try:
            queue.close(wait=wait)
        except Exception as exc:                              # noqa: BLE001
            logger.warning("VideoQueue: закрытие не удалось: %s", exc)


def hold_all(reason: str = '') -> None:
    with _registry_lock:
        queues = list(_registry.values())
    for queue in queues:
        queue.hold(reason)


def release_all(reason: str = '') -> None:
    with _registry_lock:
        queues = list(_registry.values())
    for queue in queues:
        queue.release(reason)
