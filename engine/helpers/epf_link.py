"""Automatic bridge between Real Photo and Extreme Picture Finder (EPF).

BegoFrame owns the semantic work: Real Photo produces one web-search query for
each selected clip, EPF collects image candidates, then the configured vision
model selects one candidate and the bridge imports it into ``generated/``.
EPF is driven through its documented ``update SearchFileName=...`` CLI; no
manual project opening, phrase import, or folder selection is part of the
normal pipeline.
"""

from __future__ import annotations

import argparse
import base64
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import json
import logging
import mimetypes
import os
import re
import shutil
import threading
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger("ExtremePictureFinderBridge")

IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".tif", ".tiff", ".bmp"})
DEFAULT_ENGINES = ("google", "bing")
DEFAULT_EXE = Path(r"X:\ExtremePictureFinder\App\EPF\EPF.exe")
AGENT_DIR = Path(__file__).resolve().parents[1]
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))
BUNDLED_ROOT = Path(__file__).resolve().parents[2] / "runtime" / "ExtremePictureFinder"
BUNDLED_DATA_ROOT = Path(__file__).resolve().parents[2] / "runtime" / "epf_data"
BUNDLED_DATA_DIR = BUNDLED_DATA_ROOT / "Extreme Picture Finder"


def _bridge_event(job: Path, event: str, **fields: Any) -> None:
    """Persist bridge progress beside the EPF job and mirror it to the logger."""
    payload = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "event": event,
        **fields,
    }
    try:
        with (job / "bridge_events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        logger.debug("Unable to write EPF bridge event", exc_info=True)
    message = fields.get("message") or event
    logger.info("  [EPF] %s", message)


@dataclass(frozen=True)
class BridgeSelection:
    clip_id: int
    query: str
    local_query: str = ""
    preferred_type: str = "photo"
    confidence: str = ""
    # Описание сцены и запреты для вижн-отбора (Auto Visual Story, 25.08):
    # раньше селектор видел только строку запроса.
    context_text: str = ""


def _epf_encode(value: str) -> str:
    """Encode an EPF INI value in the format used by its native .epr files."""
    return "".join(f"%{byte:02X}" for byte in str(value).encode("utf-8"))


def _safe_clip_id(value: Any) -> int:
    clip_id = int(value)
    if clip_id < 0:
        raise ValueError(f"clip_id must be non-negative: {value!r}")
    return clip_id


def _landscape_search_query(query: str) -> str:
    """Bias image search toward landscape results without changing semantic selection."""
    normalized = query.strip()
    lowered = normalized.lower()
    if not any(token in lowered for token in ("landscape", "horizontal", "widescreen", "16:9")):
        normalized = f"{normalized} landscape horizontal photograph"
    return _with_stock_exclusions(normalized)


# Фотостоки: их превью идут с водяным знаком поперёк кадра и в документалку не
# годятся. При включённой галке «Без стоковых фото» они режутся дважды: минус-
# словами в поисковой фразе (Google/Bing понимают, часть всё равно просачивается)
# и чёрным списком доменов уже по факту скачивания — домен берётся из файла
# состояния EPF, где лежат исходные URL.
_STOCK_MINUS_WORDS = (
    "alamy", "shutterstock", "dreamstime", "istockphoto", "gettyimages",
    "depositphotos", "freepik", "vecteezy", "123rf",
)
# EPF работает через ГЛОБАЛЬНОЕ состояние: `_set_epf_runtime_paths` прописывает
# пути к данным на весь процесс, а поиск идёт в общий data_dir. Два экземпляра
# одновременно перетирают друг другу настройки, и второй выходит с кодом 0 и
# нулём кандидатов через 2-4 секунды вместо честных 12+.
#
# В прогоне 26.08 так терялись сцены: основная стадия Real Photo и EPF-фолбэк
# ютуба (`fallback_pending_youtube` создаёт СВОЙ processor) шли параллельно —
# клипы 8, 15 и 17 получили по нулю кандидатов ровно в те моменты, когда поверх
# них стартовал второй EPF. У клипа 15 из-за этого остались только два битых
# кандидата от Serper, лист для вижна не собрался, и в кадр попал HTML-файл.
#
# Замок делает запуски последовательными: ждать 12 секунд честного поиска
# дешевле, чем терять сцену.
_EPF_RUN_LOCK = threading.Lock()
# Дорожки: EPF УМЕЕТ работать в несколько экземпляров — у него есть штатная
# настройка «Multiply program instances» (`AllowMultiplyInstances` в реестре,
# по умолчанию False, отсюда мьютекс `EPF3Mutex` и мгновенный выход второго
# процесса). Патчить бинарник не нужно: снимаем запрет и даём каждой дорожке
# СВОЮ папку данных, иначе экземпляры дерутся за общую базу.
#
# Замок при этом остаётся, но короткий: он держится только на время «выставить
# DataDir в реестре → запустить процесс». Ключ читается при инициализации
# EPF (единственное место чтения в бинарнике), поэтому дальше запись можно
# отдавать следующей дорожке, а поиск идёт параллельно.
_EPF_LANE_POOL: list[int] = []
_EPF_LANE_COND = threading.Condition()
_EPF_LANES_READY = False


def _lane_count() -> int:
    try:
        return max(1, min(8, int(_config_value("REAL_PHOTO_EPF_LANES", "4") or 4)))
    except (TypeError, ValueError):
        return 4


def _apply_epf_settings() -> None:
    """Штатные настройки EPF перед запуском: экземпляры и режим окна.

    Всё это — собственные опции программы, никаких костылей поверх:
    - `AllowMultiplyInstances` снимает запрет на второй экземпляр (иначе
      мьютекс `EPF3Mutex` роняет дорожки со второй по счёту);
    - `StartMinimized` + `MinimizeToTray` при выключенной галке «Показывать
      окно EPF» — окно не разворачивается на экране и уходит в трей.
      `wShowWindow=SW_HIDE` на EPF не действует: это Delphi/VCL, главную форму
      показывает `Application.Run`, а `nCmdShow` родителя оно игнорирует —
      поэтому режим окна задаётся ИМЕННО настройкой самой программы.

    Значения пишутся только когда отличаются от нужных, чтобы не дёргать
    реестр на каждом клипе.
    """
    if os.name != "nt":
        return
    import winreg

    # Галка «Показывать окно EPF» управляет режимом окна В ОБЕ СТОРОНЫ: выключена —
    # программа стартует свёрнутой в трей, включена — разворачивается как обычно.
    # Иначе, один раз спрятав окно, мы бы не смогли вернуть его галкой обратно.
    hidden = "False" if _epf_visible() else "True"
    wanted = {
        "AllowMultiplyInstances": "True",
        "StartMinimized": hidden,
        "MinimizeToTray": hidden,
        # Иконка в трее — тоже «окно на экране» с точки зрения пользователя:
        # четыре дорожки дают четыре значка. При выключенной галке убираем и её,
        # при включённой возвращаем (замечание пользователя 26.08).
        "ShowTrayIcon": "True" if _epf_visible() else "False",
    }

    key_path = r"SOFTWARE\Extreme Internet Software\Extreme Picture Finder 3\Settings"
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            changed = []
            for name, value in wanted.items():
                current = None
                try:
                    current, _ = winreg.QueryValueEx(key, name)
                except FileNotFoundError:
                    pass
                if str(current) != value:
                    winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
                    changed.append(f"{name}={value}")
            if changed:
                logger.info("  [EPF] настройки применены: %s", ", ".join(changed))
    except OSError as exc:
        logger.warning("  [EPF] настройки EPF не применены (%s) — возможны окна "
                       "на экране и работа по одной дорожке", exc)


def _allow_multiple_instances() -> None:
    """Совместимость: прежнее имя вызывает общую настройку."""
    _apply_epf_settings()


# Что копируем дорожке из базовой папки: КОНФИГУРАЦИЯ, а не накопленные данные.
# Data/Searches у каждой дорожки свои, иначе смысла в разведении нет.
_LANE_CONFIG_FILES = ("searchengines.dat", "target_files.dat", "default_search.epr")
_LANE_CONFIG_DIRS = ("Templates",)
_LANE_EMPTY_DIRS = ("Data", "Searches", "Cache", "Temp", "Projects", "DBProjects")


def _prepare_lane_data_dir(base_data_dir: Path, lane: int) -> Path:
    """Папка данных дорожки: своя база поиска, общая конфигурация."""
    if lane <= 0:
        return base_data_dir
    lane_dir = Path(base_data_dir).parent / f"{Path(base_data_dir).name} lane{lane}"
    lane_dir.mkdir(parents=True, exist_ok=True)
    for name in _LANE_EMPTY_DIRS:
        (lane_dir / name).mkdir(exist_ok=True)
    for name in _LANE_CONFIG_FILES:
        source = Path(base_data_dir) / name
        target = lane_dir / name
        if source.is_file() and not target.exists():
            shutil.copy2(source, target)
    for name in _LANE_CONFIG_DIRS:
        source = Path(base_data_dir) / name
        target = lane_dir / name
        if source.is_dir() and not target.exists():
            shutil.copytree(source, target)
    return lane_dir


class _EpfLane:
    """Свободная дорожка EPF. Возвращает номер (0 = дорожки выключены)."""

    def __init__(self) -> None:
        self.lane = 0

    def __enter__(self) -> int:
        global _EPF_LANES_READY
        count = _lane_count()
        # Настройки EPF (режим окна, трей, несколько экземпляров) применяем
        # ВСЕГДА — раньше вызов стоял после раннего выхода, и при одной дорожке
        # окно вылезало на экран, хотя галка «Показывать окно EPF» выключена.
        _apply_epf_settings()
        if count <= 1:
            return 0
        with _EPF_LANE_COND:
            if not _EPF_LANES_READY:
                _EPF_LANE_POOL.extend(range(1, count + 1))
                _EPF_LANES_READY = True
            while not _EPF_LANE_POOL:
                _EPF_LANE_COND.wait(timeout=5.0)
                if not _EPF_LANE_POOL:
                    logger.info("  [EPF] все %d дорожек заняты — жду", count)
            self.lane = _EPF_LANE_POOL.pop(0)
        return self.lane

    def __exit__(self, *_exc) -> None:
        if not self.lane:
            return
        with _EPF_LANE_COND:
            _EPF_LANE_POOL.append(self.lane)
            _EPF_LANE_COND.notify()
        self.lane = 0

# Выход быстрее этого — не «ничего не нашлось», а сбой запуска: настоящий поиск
# у нас занимает 12-48 секунд.
_EPF_SUSPICIOUS_EXIT_SEC = 6.0

_STOCK_DOMAINS = (
    "alamy.com", "shutterstock.com", "dreamstime.com", "istockphoto.com",
    "gettyimages.", "depositphotos.com", "freepik.com", "vecteezy.com",
    "123rf.com", "stock.adobe.com", "ftcdn.net", "canstockphoto.",
    "agefotostock.com", "superstock.com", "bigstockphoto.com", "stockvault.net",
    "shutterstock.", "dissolve.com", "pond5.com",
    "bridgemanimages.",
    # ytimg — обложки роликов YouTube: это маркетинговый монтаж с крупным
    # текстом и стрелками, а не кадр сцены.
    "ytimg.com",
    # Pinterest сознательно НЕ в списке: водяных знаков он не ставит, а
    # перезалитая мелочь отсекается порогами размера (EPF не берёт меньше
    # 256×256, вижн — меньше 640 по короткой стороне). Проверено 26.08:
    # картинка с `i.pinimg.com` пришла как 2000×1482 и была вполне годной.
)

# Маркетплейсы и доски объявлений: товарные карточки со вшитой рекламой,
# коллажами «до/после» и надписями поперёк кадра. Отдельный список и отдельная
# галка — у стоков барьером служит водяной знак, здесь его нет вовсе, и вижн на
# превью такую подпись часто не замечает (улов 01.09).
# Домены картиночных CDN важнее самих сайтов: в выдачу попадает именно
# `m.media-amazon.com/images/I/719sqKoQg9L.jpg`, а не страница товара.
_MARKETPLACE_DOMAINS = (
    "media-amazon.com", "ssl-images-amazon.com", "images-amazon.com", "amazon.",
    "ebayimg.com", "ebay.",
    "alicdn.com", "aliexpress.", "alibaba.com", "taobao.",
    "wbbasket.ru", "wildberries.ru", "basket-", "wbstatic.net",
    "ozone.ru", "ozon.ru",
    "etsystatic.com", "etsy.com",
    "walmartimages.com", "walmart.com",
    "target.scene7.com", "shopify.com", "cdn.shopify.com",
    "avito.st", "avito.ru", "joom.com", "temu.com", "shein.com",
)


def _min_image_side() -> int:
    """Минимальная сторона картинки для EPF — та же, что у вижна.

    Источник правды один: `vision_pick.MIN_FRAME_SIDE`. Если вижн перестанет
    смотреть мелкие кадры при другом пороге, EPF подстроится сам.
    """
    try:
        from units.vision_picker import MIN_FRAME_SIDE

        return max(64, int(MIN_FRAME_SIDE))
    except Exception:                                  # noqa: BLE001
        return 640


def _excluded_urls_rows() -> list[str]:
    """Секция `[ExcludedURLs]` для задания EPF: домены, которые не качаем.

    Проверено живьём (26.08): с этой секцией EPF не скачивает ни одного файла
    с перечисленных доменов, и запрошенное число кандидатов означает число
    ПРИГОДНЫХ. Без неё стоки занимали все слоты, а отсев по домену уже после
    скачивания оставлял сцену ни с чем.

    Пустой список правил секцию не пишет: обе галки выключены — значит
    пользователь готов смотреть и стоки, и товарные карточки, единственным
    барьером остаётся вижн.
    """
    domains: list[str] = []
    if _no_stock_enabled():
        domains += list(_STOCK_DOMAINS)
    if _no_marketplaces_enabled():
        domains += list(_MARKETPLACE_DOMAINS)
    if not domains:
        return []
    rows = ["[ExcludedURLs]"]
    for index, domain in enumerate(domains):
        # EPF принимает регулярное выражение: точки экранируем, чтобы
        # `alamy.com` не совпал с `alamyXcom`.
        rows.append(f"URL{index:03d}={re.escape(domain)}")
    rows.append("")
    return rows


def _no_marketplaces_enabled() -> bool:
    """Галка «Без товарных карточек» (маркетплейсы).

    Отдельная от стоков: там водяные знаки, здесь — товарные фото с вшитой
    рекламой. Улов живого прогона 01.09: в ролик про сыроварню попал кадр с
    Amazon (`719sqKoQg9L.jpg`) с надписью «Professional cheese maker choose our
    set» поперёк нижней трети. Вижн соседнюю карточку из той же серии отверг
    («рекламная карточка товара с крупной надписью»), а эту пропустил — на
    превью подпись почти не читается.
    """
    try:
        import params as _cfg

        value = getattr(_cfg, "REAL_PHOTO_NO_MARKETPLACES", True)
    except ImportError:   # запуск из agent/, не пакетом
        value = os.getenv("REAL_PHOTO_NO_MARKETPLACES", "1")
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _no_stock_enabled() -> bool:
    """Галка «Без стоковых фото». Читаем config НАПРЯМУЮ: `_config_value` подменяет
    любое falsy-значение дефолтом (`False or "1"` → "1"), и галку было бы не выключить."""
    try:
        import params as _cfg

        value = getattr(_cfg, "REAL_PHOTO_NO_STOCK_PHOTOS", True)
    except ImportError:   # запуск из agent/, не пакетом
        value = os.getenv("REAL_PHOTO_NO_STOCK_PHOTOS", "1")
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _with_stock_exclusions(query: str) -> str:
    """Минус-слова фотостоков в поисковую фразу (галка «Без стоковых фото»)."""
    if not _no_stock_enabled():
        return query
    return query + " " + " ".join(f"-{word}" for word in _STOCK_MINUS_WORDS)


def _downloaded_source_urls(epf_data_dir: Path, data_file_name: str) -> dict[str, str]:
    """{имя файла: исходный URL} из файла состояния EPF (`Data/<job>.dat`).

    EPF не пишет источник в лог, но в своём .dat хранит полные URL найденного
    открытым текстом — этого хватает, чтобы узнать домен каждого скачанного
    файла. Сборка EPF у нас своя и зафиксированная, формат стабилен."""
    path = Path(epf_data_dir) / "Data" / str(data_file_name or "")
    if not data_file_name or not path.is_file():
        return {}
    try:
        raw = path.read_bytes()
    except OSError as exc:
        logger.info("[EPF bridge] .dat не прочитан (%s): %s", path.name, exc)
        return {}
    by_name: dict[str, str] = {}
    # Потолок длины поднят с 400: поисковые URL с процент-кодированием бывают
    # под тысячу знаков, и обрезка посередине давала неверное имя файла —
    # кандидат оставался без опознанного источника (прогон 26.08: URL резались
    # ровно на 408 символах, дальше шёл «хвост» того же адреса).
    for match in re.finditer(rb"https?://[\x21-\x7e]{10,3000}", raw):
        url = match.group(0).decode("ascii", "replace").split("|")[0]
        name = url.rsplit("/", 1)[-1].split("?")[0]
        # Точку в имени НЕ требуем: CDN соцсетей отдают картинки без расширения
        # (`avatars.dzeninfra.ru/.../scale_1200`, `sun9-…userapi.com/.../scale_1200`).
        # Прежний фильтр `"." in name` выбрасывал такие URL целиком, и файлы
        # вроде `scale_1200.jpg` оставались без опознанного источника — в логе
        # это выглядело как «источник не определён у N из 5» (прогон 26.08).
        if name and name not in by_name:
            by_name[name] = url
            # Тот же URL под именем со стандартным расширением: EPF сохраняет
            # `scale_1200` на диск как `scale_1200.jpg`.
            if "." not in name:
                for ext in (".jpg", ".jpeg", ".png", ".webp"):
                    by_name.setdefault(name + ext, url)
    return by_name


def _match_source_url(file_name: str, urls: dict[str, str]) -> str:
    """Найти URL, из которого скачан файл, — с поправкой на обрезку имени.

    EPF режет длинные имена при сохранении: из `katowice-poland-february-2-
    auditorium-of-modern-KYT88Y.jpg` получается `katowice-poland-february-2-
    2ditorium-of-modern-KYT88Y.jpg`, а из `kato…-radio-nospr-84429027.jpg` —
    `wice-poland-january-interiolish-radio-nospr-84429027.jpg`. Точное
    сопоставление по имени промахивалось на 63 файлах из 115 (прогон 26.08), и
    именно среди них были Alamy, Dreamstime и Vecteezy: домен не определялся,
    фильтр молча пропускал их вижну.

    Порядок: точное совпадение → совпадение по ХВОСТУ имени (он у стоков несёт
    id картинки и переживает обрезку) → вхождение одного имени в другое.
    """
    exact = urls.get(file_name, "")
    if exact:
        return exact
    stem = file_name.rsplit(".", 1)[0]
    if len(stem) < 8:
        return ""
    tail = stem[-16:]
    for name, url in urls.items():
        candidate_stem = name.rsplit(".", 1)[0]
        if candidate_stem.endswith(tail):
            return url
    for name, url in urls.items():
        candidate_stem = name.rsplit(".", 1)[0]
        if stem in candidate_stem or candidate_stem in stem:
            return url
    return ""


def _wait_dat_settled(path: Path, *, timeout: float = 4.0, quiet: float = 0.4) -> bool:
    """Дождаться, пока EPF допишет свой `.dat`, и только потом читать источники.

    EPF сбрасывает файл состояния асинхронно: процесс уже завершён, а часть URL
    ещё в пути. Ждём не вслепую, а по признаку — размер файла перестал расти.
    Как только он не меняется `quiet` секунд, запись закончена.

    Прогон 26.08: без этого получали «не определено 2 из 5», хотя постфактум те
    же джобы сопоставлялись полностью (предупреждение в 17:02:53 против записи
    файла в 17:02:52). Возвращает True, если файл успокоился в срок.
    """
    if not path or not path.name:
        return False
    deadline = time.monotonic() + timeout
    last_size = -1
    stable_since = None
    while time.monotonic() < deadline:
        try:
            size = path.stat().st_size
        except OSError:
            size = -1                     # файла ещё нет — ждём его появления
        if size != last_size:
            last_size, stable_since = size, time.monotonic()
        elif stable_since is not None and time.monotonic() - stable_since >= quiet:
            return size > 0
        time.sleep(0.1)
    logger.info("[EPF bridge] %s не успокоился за %.1fс — читаю как есть",
                path.name, timeout)
    return False


def drop_stock_downloads(
    candidates: list[Path],
    *,
    epf_data_dir: Path | str,
    data_file_name: str,
) -> tuple[list[Path], list[tuple[Path, str]]]:
    """Выбросить скачанное с фотостоков. Возвращает (оставшиеся, [(файл, домен)]).

    Галка выключена → ничего не режем: тогда единственный барьер — вижн."""
    if not _no_stock_enabled() or not candidates:
        return candidates, []
    # EPF дописывает `.dat` асинхронно: процесс уже завершён, а часть URL ещё
    # не сброшена на диск. В прогоне 26.08 это давало «не определено 2 из 5»,
    # хотя постфактум те же джобы сопоставлялись полностью (предупреждение в
    # 17:02:53 против записи файла в 17:02:52). Поэтому при неполном
    # сопоставлении ждём и перечитываем — дешевле, чем пропустить фотосток.
    def _resolve(urls: dict[str, str]) -> tuple[list[Path], list[tuple[Path, str]], int]:
        kept_local: list[Path] = []
        dropped_local: list[tuple[Path, str]] = []
        unknown_local = 0
        for path in candidates:
            url = _match_source_url(path.name, urls)
            host = url.split("/")[2].lower() if url.count("/") >= 2 else ""
            if not host:
                unknown_local += 1
            if host and any(domain in host for domain in _STOCK_DOMAINS):
                dropped_local.append((path, host))
            else:
                kept_local.append(path)
        return kept_local, dropped_local, unknown_local

    _wait_dat_settled(Path(epf_data_dir) / "Data" / str(data_file_name or ""))
    urls = _downloaded_source_urls(Path(epf_data_dir), data_file_name)
    kept, dropped, unknown = _resolve(urls) if urls else ([], [], len(candidates))
    if not urls:
        logger.warning(
            "[EPF bridge] источники не извлечены из %s — домены НЕ проверены, "
            "фотостоки может отсеять только вижн", data_file_name or "(нет .dat)")
        return candidates, []
    if unknown:
        # Молчать нельзя: непроверенный файл идёт вижну как обычный кандидат, и
        # водяной знак всплывает уже в готовом ролике (прогон 26.08: Vecteezy).
        logger.warning(
            "[EPF bridge] у %d из %d файлов источник не определён — эти "
            "кандидаты по домену НЕ проверены", unknown, len(candidates))
    return kept, dropped


def _selection_dict(selection: Any) -> BridgeSelection:
    if isinstance(selection, BridgeSelection):
        return selection
    if hasattr(selection, "clip_id") and hasattr(selection, "search_query"):
        return BridgeSelection(
            clip_id=_safe_clip_id(selection.clip_id),
            query=str(selection.search_query or "").strip(),
            local_query=str(getattr(selection, "search_query_local", "") or "").strip(),
            preferred_type=str(getattr(selection, "preferred_type", "photo") or "photo"),
            confidence=str(getattr(selection, "confidence", "") or ""),
            context_text=str(getattr(selection, "context_text", "") or "").strip(),
        )
    if isinstance(selection, dict):
        return BridgeSelection(
            clip_id=_safe_clip_id(selection["clip_id"]),
            query=str(selection.get("query") or selection.get("search_query") or "").strip(),
            local_query=str(
                selection.get("local_query") or selection.get("search_query_local") or ""
            ).strip(),
            preferred_type=str(selection.get("preferred_type") or "photo"),
            confidence=str(selection.get("confidence") or ""),
            context_text=str(selection.get("context_text") or "").strip(),
        )
    raise TypeError(f"Unsupported selection type: {type(selection).__name__}")


def _config_value(name: str, default: str = "") -> str:
    try:
        import params as config
        return str(getattr(config, name, default) or default)
    except ImportError:   # запуск из agent/, не пакетом
        return os.getenv(name, default)


def _configured_engines(engines: Iterable[str] | str | None = None) -> list[str]:
    if engines is None:
        engines = _config_value("REAL_PHOTO_EPF_ENGINES", ",".join(DEFAULT_ENGINES))
    if isinstance(engines, str):
        engines = engines.split(",")
    result: list[str] = []
    for engine in engines:
        name = str(engine or "").strip().lower()
        if name and name not in result:
            result.append(name)
    return result or list(DEFAULT_ENGINES)


def _safe_file_token(value: str, fallback: str = "project") -> str:
    token = re.sub(r"[^a-zA-Z0-9_-]+", "_", value).strip("_").lower()
    return token[:42] or fallback


def _epf_root(executable: Path) -> Path:
    """Return the portable root for either launcher or core executable."""
    if executable.name.lower() == "expicturefinderportable.exe":
        return executable.parent
    if executable.parent.name.lower() == "epf" and executable.parent.parent.name.lower() == "app":
        return executable.parent.parent.parent
    return executable.parent


def resolve_epf_install(
    executable: str | Path | None = None,
    data_dir: str | Path | None = None,
) -> tuple[Path, Path]:
    """Resolve the EPF portable launcher and its private data directory."""
    if os.name != "nt":
        raise OSError("Extreme Picture Finder integration is available only on Windows")
    configured_exe = str(executable or _config_value("REAL_PHOTO_EPF_EXE", "")).strip()
    candidates = [Path(configured_exe)] if configured_exe else []
    candidates.extend([
        BUNDLED_ROOT / "App" / "EPF" / "EPF.exe",
        DEFAULT_EXE,
        Path("EPF.exe"),
    ])

    epf_exe: Path | None = None
    for candidate in candidates:
        if candidate.name.lower() == "expicturefinderportable.exe":
            core = candidate.parent / "App" / "EPF" / "EPF.exe"
            if core.is_file():
                candidate = core
        if candidate.is_file():
            epf_exe = candidate.resolve()
            break
    if epf_exe is None:
        raise FileNotFoundError(
            "Extreme Picture Finder executable not found. Set REAL_PHOTO_EPF_EXE "
            "to App\\EPF\\EPF.exe or install the verified bundle from BegoFrame."
        )

    configured_data = str(data_dir or _config_value("REAL_PHOTO_EPF_DATA_DIR", "")).strip()
    data_candidates = [Path(configured_data)] if configured_data else []
    data_candidates.append(BUNDLED_DATA_DIR)
    data_candidates.append(_epf_root(epf_exe) / "Settings" / "Extreme Picture Finder")

    epf_data: Path | None = None
    for candidate in data_candidates:
        if candidate.is_dir():
            epf_data = candidate.resolve()
            break
    if epf_data is None:
        raise FileNotFoundError(
            "Extreme Picture Finder data directory not found. Set "
            "REAL_PHOTO_EPF_DATA_DIR to the folder containing Searches and Data."
        )
    return epf_exe, epf_data


def _available_engines(data_dir: Path) -> set[str]:
    path = data_dir / "searchengines.dat"
    if not path.exists():
        return set()
    text = path.read_text(encoding="utf-8", errors="replace")
    return {
        match.group(1).strip().lower()
        for match in re.finditer(r"(?m)^\[([^\]]+)\]\s*$", text)
    }


def _ensure_search_engines(epf_exe: Path, data_dir: Path) -> set[str]:
    """Bootstrap a missing global engine list from the installed portable copy."""
    destination = data_dir / "searchengines.dat"
    if not destination.exists():
        root = _epf_root(epf_exe)
        sources = [
            root / "Settings" / "Extreme Picture Finder" / "searchengines.dat",
            root / "App" / "DefaultData" / "Extreme Picture Finder" / "searchengines.dat",
        ]
        source = next((path for path in sources if path.is_file()), None)
        if source is not None:
            data_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            logger.info("[EPF bridge] Installed missing searchengines.dat from %s", source)
    return _available_engines(data_dir)


def _native_search_epr(
    *,
    query: str,
    destination: Path,
    data_file_name: str,
    title: str,
    engines: Iterable[str],
    max_files: int,
) -> str:
    """Create a native EPF search .epr, including phrase and engine sections."""
    engine_rows = [f"Engine{index:03d}={engine}" for index, engine in enumerate(engines)]
    return "\n".join(
        [
            "[Settings]",
            "AcceptCookies=True",
            "AfterCompleteAction=acaNone",
            "AfterCompleteExportFileList=False",
            "AfterCompleteExportFileListAddHeaderRow=False",
            "AfterCompleteExportFileListCustomSeparator=%7C",
            "AfterCompleteExportFileListFileName=",
            "AfterCompleteExportFileListMode=200",
            "AfterCompleteExportFileListSearchOnlyExisting=True",
            "AfterCompleteExportFileListSeparator=100",
            "AfterCompleteProject=",
            "AfterCompleteProjectStartType=stResume",
            "AllowRedirectBackToParent=False",
            "AutoContinueDownloadSeconds=1",
            "AutoContinueDownloadType=acdSeconds",
            "AutoContinueDownloadURLRE=",
            "AutoContinueManualLogin=False",
            "Completed=False",
            "ConflictsRenameNumberOfDigits=3",
            "DatabaseCategoryID=0",
            "DatabaseCategoryName=",
            "DatabaseID=0",
            f"DataFileName={_epf_encode(data_file_name)}",
            "DefaultRefererCustomText=",
            "DefaultRefererType=hrtNatural",
            f"DestinationFolder={_epf_encode(str(destination.resolve()))}",
            "DownloadTargetFilesFromExternalSites=True",
            "ExplorationDepth=2",
            "ExplorationLimit=selEntireSite",
            "ExplorationType=setRegular",
            "FolderForEachSearchEngine=False",
            "FolderForEachSearchPhrase=False",
            "ForceFileExt=False",
            "ManualLogin=False",
            "ManualLoginMessage=",
            "MaxDelayBetweenDownloads=4",
            "MaxFileDate=46242,1900847917",
            "MaxFileSizeLimit=30",
            "MaxFileSizeUnit=fsuMB",
            "MaxImageHeight=4096",
            "MaxImageWidth=4096",
            f"MaxSearchResNumber={max_files}",
            "MinDelayBetweenDownloads=1",
            "MinFileDate=46242,1900847917",
            "MinFileSizeLimit=10",
            "MinFileSizeUnit=fsuKB",
            # Порог EPF держим РАВНЫМ порогу вижна (`vision_pick.MIN_FRAME_SIDE`).
            # Было 256: кадры 256-640 скачивались, занимали слоты
            # `StopAfterFileNumber` и выбрасывались уже вижном — те же
            # выброшенные слоты, что и у фотостоков (замечание пользователя
            # 26.08). Качать то, что заведомо не пройдёт, незачем.
            f"MinImageHeight={_min_image_side()}",
            f"MinImageWidth={_min_image_side()}",
            "NamingParentPageTitleURLRE=",
            "NamingRemoveCharsEnd=False",
            "NamingRemoveCharsEndNum=5",
            "NamingRemoveCharsStart=False",
            "NamingRemoveCharsStartNum=5",
            "NamingTagAttributeName=",
            "PageCustomParsersAddedURLsPosition=aupBefore",
            "PageCustomParsersAddedURLsType=autNone",
            "PartiallyDownloadedFilesCount=0",
            "Password=",
            "ProcessedAddresses=0",
            "ReplaceSlashesInSubFolders=False",
            "ResolveNamingConflictsType=rncRename",
            "SafeSearch=True",
            "SaveDigitsStartFrom=0",
            "SavedTargetFiles=0",
            "SaveExt=",
            "SaveNamingType=sntOriginal",
            "SaveNumberOfDigits=3",
            "SavePrefix=",
            "SaveQueryStringParamName=",
            "SaveSuffix=",
            "SearchMode=smOnlyImages",
            f"StopAfterFileNumber={max_files}",
            "StopAfterFileSizeMB=10",
            "StopAfterTimeMinutes=60",
            "StopAtTime=23:59",
            "SubFoldersAddTextEnd=False",
            "SubFoldersAddTextStart=False",
            "SubFoldersEndText=",
            "SubFoldersFileNum=200",
            "SubFoldersMode=sfmNone",
            "SubFoldersPageTitleURLRE=",
            "SubFoldersRemoveCharsEnd=False",
            "SubFoldersRemoveCharsEndNum=5",
            "SubFoldersReplaceSlashesWith=%2D",
            "SubFoldersStartingURLAddDomain=True",
            "SubFoldersStartingURLAddFullURL=False",
            "SubFoldersStartingURLAddNum=True",
            "SubFoldersTargetFileAddDomain=True",
            "SubFoldersWebsiteFoldersAddDocument=False",
            "SubFoldersWebsiteFoldersAddDomain=False",
            "SuggestedSimultaneousConnections=0",
            "TemplateFileName=",
            "TGPGalleryTitleContains=",
            "TGPGalleryURLContains=",
            "TGPMaxPageDepth=4",
            "TGPMaxTargetFileDepth=5",
            f"Title={_epf_encode(title)}",
            "UseAfterCompleteSound=False",
            "UseExplorationDepth=True",
            "UseMaxFileDate=False",
            "UseMaxFileSizeLimit=True",
            "UseMaxImageHeight=True",
            "UseMaxImageWidth=True",
            "UseMinFileDate=False",
            "UseMinFileSizeLimit=True",
            "UseMinImageHeight=True",
            "UseMinImageWidth=True",
            "UseQueryStringParam=False",
            "UserAgent=",
            "UseRandomDelayBetweenDownloads=False",
            "Username=",
            "UseSavePrefix=False",
            "UseSaveSuffix=False",
            "UseStopAfterFileNumber=True",
            "UseStopAfterFileSizeMB=False",
            "UseStopAfterTime=False",
            "UseStopAtTime=False",
            "",
            "[SearchPhrases]",
            f"Phrase00000={_epf_encode(query)}",
            "",
            "[TargetFiles]",
            "File000=*.jp*",
            "File001=*.pn*",
            "File002=*.webp",
            "File003=*.gif",
            "File004=*.bmp",
            "",
            # Фотостоки EPF не качает ВОВСЕ — они не занимают мест в
            # `StopAfterFileNumber`. Раньше отсев шёл ПОСЛЕ скачивания, и
            # запрошенные 5 кандидатов оборачивались нулём: на «интерьерных»
            # запросах поисковик отдаёт почти одни стоки (проба 26.08 — 5 из 6
            # файлов Dreamstime/Getty/Alamy, после отсева оставался один).
            # С этой секцией тот же запрос дал 6 из 6 пригодных за то же время.
            *_excluded_urls_rows(),
            "[SearchEngines]",
            *engine_rows,
            "",
            "[ExportColumns]",
            "Column000=100",
            "Column001=102",
            "",
        ]
    )


def create_job(
    project_dir: str | Path,
    selections: Iterable[Any],
    *,
    destination_root: str | Path | None = None,
    max_files_per_clip: int = 12,
    register_with_epf: bool = False,
    epf_executable: str | Path | None = None,
    epf_data_dir: str | Path | None = None,
    engines: Iterable[str] | str | None = None,
) -> Path:
    """Create a traceable bridge job and optionally register native EPF searches."""
    project = Path(project_dir).resolve()
    if not project.is_dir():
        raise FileNotFoundError(project)
    if max_files_per_clip < 1:
        raise ValueError("max_files_per_clip must be positive")

    epf_exe: Path | None = None
    epf_data: Path | None = None
    selected_engines = _configured_engines(engines)
    if register_with_epf:
        epf_exe, epf_data = resolve_epf_install(epf_executable, epf_data_dir)
        available = _ensure_search_engines(epf_exe, epf_data)
        missing = [engine for engine in selected_engines if engine not in available]
        selected_engines = [engine for engine in selected_engines if engine in available]
        if missing:
            logger.warning("[EPF bridge] Unavailable engines ignored: %s", ", ".join(missing))
        if not selected_engines:
            raise RuntimeError(
                f"EPF has no configured requested engines in {epf_data / 'searchengines.dat'}"
            )

    run_token = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    job_dir = project / "_real_photo_work" / "extreme_picture_finder" / run_token
    job_dir.mkdir(parents=True, exist_ok=True)
    destination = Path(destination_root).resolve() if destination_root else job_dir / "downloads"
    destination.mkdir(parents=True, exist_ok=True)
    project_token = _safe_file_token(project.name)

    normalized: list[dict[str, Any]] = []
    for raw in selections:
        item = _selection_dict(raw)
        if not item.query:
            logger.warning("Skipping clip %s with empty Real Photo query", item.clip_id)
            continue
        clip_dir = job_dir / f"clip_{item.clip_id:03d}"
        download_dir = destination / f"clip_{item.clip_id:03d}"
        clip_dir.mkdir(parents=True, exist_ok=True)
        download_dir.mkdir(parents=True, exist_ok=True)
        _warn_if_path_too_long(download_dir, item.clip_id)

        search_stem = f"begoframe_{project_token}_{item.clip_id:03d}_{uuid.uuid4().hex[:8]}"
        search_file_name = f"{search_stem}.epr"
        data_file_name = f"{search_stem}.dat"
        title = f"BegoFrame | {project.name} | clip {item.clip_id:03d}"
        epf_query = _landscape_search_query(item.query)
        epr_text = _native_search_epr(
            query=epf_query,
            destination=download_dir,
            data_file_name=data_file_name,
            title=title,
            engines=selected_engines,
            max_files=max_files_per_clip,
        )
        local_epr = clip_dir / "project.epr"
        local_epr.write_text(epr_text, encoding="utf-8")
        (clip_dir / "search_phrase.txt").write_text(item.query + "\n", encoding="utf-8")
        if item.local_query and item.local_query != item.query:
            (clip_dir / "search_phrase_local.txt").write_text(
                item.local_query + "\n", encoding="utf-8"
            )

        epf_search_path = None
        if epf_data is not None:
            epf_search_path = epf_data / "Searches" / search_file_name
            epf_search_path.parent.mkdir(parents=True, exist_ok=True)
            epf_search_path.write_text(epr_text, encoding="utf-8")

        normalized.append(
            {
                **asdict(item),
                "epf_query": epf_query,
                "clip_dir": str(clip_dir),
                "download_dir": str(download_dir),
                "max_files": max_files_per_clip,
                "source": "extreme_picture_finder",
                "license": "unknown",
                "epf_search_file": search_file_name if epf_search_path else "",
                "epf_search_path": str(epf_search_path) if epf_search_path else "",
                "epf_data_file": data_file_name,
            }
        )

    manifest = {
        "version": 2,
        "backend": "extreme_picture_finder",
        "automatic": bool(register_with_epf),
        "project": project.name,
        "project_dir": str(project),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "destination_root": str(destination),
        "epf_executable": str(epf_exe) if epf_exe else "",
        "epf_data_dir": str(epf_data) if epf_data else "",
        "engines": selected_engines,
        "items": normalized,
    }
    (job_dir / "bridge_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return job_dir


# Windows не даёт создать файл, если полный путь длиннее MAX_PATH (260). EPF
# при этом НЕ ругается: он честно находит картинки (в его `.dat` лежали 79 URL),
# молча не может их сохранить и выходит с нулём кандидатов. В логе это выглядит
# как «в интернете ничего не нашлось» — проба 26.08 в каталоге с длинным именем
# потеряла так оба клипа. Имена у стоков длинные, поэтому запас берём с оглядкой
# на них.
_EPF_TYPICAL_FILENAME_LEN = 80


def _warn_if_path_too_long(download_dir: Path, clip_id: Any) -> None:
    """Предупредить, если папке загрузки не хватит места под имя файла."""
    if os.name != "nt":
        return
    budget = 260 - len(str(download_dir)) - 1
    if budget >= _EPF_TYPICAL_FILENAME_LEN:
        return
    logger.warning(
        "  [EPF] clip %s: путь загрузки %d символов, на имя файла остаётся %d "
        "(нужно ~%d) — Windows не даст сохранить длинные имена, EPF вернёт ноль "
        "кандидатов молча. Перенесите проект в папку с более коротким путём: %s",
        clip_id, len(str(download_dir)), max(0, budget),
        _EPF_TYPICAL_FILENAME_LEN, download_dir)


def _valid_downloads(download_dir: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in download_dir.rglob("*")
            if path.is_file()
            and path.suffix.lower() in IMAGE_EXTENSIONS
            and path.stat().st_size >= 10 * 1024
        ),
        key=lambda path: (-path.stat().st_size, path.name.lower()),
    )


def _readable_images(candidates: Iterable[Path]) -> list[Path]:
    """Drop incomplete or otherwise unreadable image downloads."""
    from PIL import Image

    readable: list[Path] = []
    for path in candidates:
        try:
            with Image.open(path) as image:
                image.load()
        except Exception as exc:
            logger.warning("Skipping unreadable EPF download %s: %s", path.name, exc)
            continue
        readable.append(path)
    return readable


def _epr_is_completed(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("[EPF bridge] отчёт задания не прочитан (%s): %s", path.name, exc)
        return False
    match = re.search(r"(?mi)^Completed=(.+?)\s*$", text)
    return bool(match and match.group(1).strip().lower() == "true")


def _set_epf_runtime_paths(epf_exe: Path, data_dir: Path) -> dict[str, tuple[Any, int] | None]:
    """Temporarily point EPF at BegoFrame-owned data without requiring UAC."""
    if os.name != "nt":
        return {}
    import winreg

    key_path = r"SOFTWARE\Extreme Internet Software\Extreme Picture Finder 3\Settings"
    previous: dict[str, tuple[Any, int] | None] = {}
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        for name in ("DataDir", "InstallDir"):
            try:
                previous[name] = winreg.QueryValueEx(key, name)
            except FileNotFoundError:   # файла задания нет — считаем задание пустым
                previous[name] = None
        winreg.SetValueEx(key, "DataDir", 0, winreg.REG_SZ, str(data_dir.resolve()))
        winreg.SetValueEx(key, "InstallDir", 0, winreg.REG_SZ, str(epf_exe.parent.resolve()))
    return previous


def _restore_epf_runtime_paths(previous: dict[str, tuple[Any, int] | None]) -> None:
    if os.name != "nt" or not previous:
        return
    import winreg

    key_path = r"SOFTWARE\Extreme Internet Software\Extreme Picture Finder 3\Settings"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        for name, saved in previous.items():
            if saved is None:
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:   # файла нет — удалять нечего
                    pass
            else:
                value, value_type = saved
                winreg.SetValueEx(key, name, 0, value_type, value)


def _stop_epf_process(process: subprocess.Popen | None,
                      *, graceful_sec: float = 3.0) -> None:
    """Завершить EPF, дав ему сперва выйти САМОМУ.

    `_wait_for_search` возвращает результат, как только в `.epr` появился
    признак `Completed`, — процесс в этот момент ещё жив и дописывает свой
    `.dat` (файл состояния с исходными URL). Прежний код сразу слал
    `terminate()`, и файл оставался неполным: в прогоне 26.08 это давало
    «источник не определён у 2-3 файлов из 5», то есть фотостоки проходили
    непроверенными. Постфактум те же джобы сопоставлялись полностью — значит
    EPF дописал бы всё, если бы ему дали закончить.

    Поэтому сначала ждём штатного выхода `graceful_sec`, и только упрямый
    процесс получает `terminate()`, а совсем упрямый — `kill()`.
    """
    if process is None or process.poll() is not None:
        return
    try:
        process.wait(timeout=graceful_sec)
        return                            # вышел сам — файлы закрыты корректно
    except subprocess.TimeoutExpired:
        pass
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        logger.warning("[EPF bridge] процесс не завершился за 5 с — убиваю принудительно")
        process.kill()
        process.wait(timeout=5)


_HIDDEN_DESKTOP_NAME = "begoframe_epf"
_hidden_desktop = {"handle": None, "failed": False}


def _hidden_desktop_name() -> str | None:
    """Имя отдельного рабочего стола для окон EPF (или None, если не вышло).

    Единственный НАДЁЖНЫЙ способ не показывать окно Delphi-программы: запустить
    её на другом рабочем столе Windows. Окно там создаётся как обычно, но на
    видимом столе его нет физически.

    Почему не хватило прочего (проверено 26.08):
      • `STARTF_USESHOWWINDOW`/`SW_HIDE` — VCL показывает главную форму сам и
        `nCmdShow` родителя игнорирует;
      • `CREATE_NO_WINDOW` — только для консольных приложений;
      • `StartMinimized`/`MinimizeToTray` — сворачивают ПОСЛЕ показа, окно
        успевает мелькнуть;
      • скрытие по pid через `ShowWindow` — догоняет окно с задержкой и при
        девяти процессах не успевает: тест показал 3 окна на экране;
      • ключа тихого запуска у EPF нет (`/silent` оказался куском URL).

    Стол создаётся один на процесс и живёт до его конца.
    """
    if os.name != "nt" or _hidden_desktop["failed"]:
        return None
    if _hidden_desktop["handle"] is not None:
        return _HIDDEN_DESKTOP_NAME
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.CreateDesktopW.restype = wintypes.HANDLE
        user32.CreateDesktopW.argtypes = [
            wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p,
            wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        ]
        GENERIC_ALL = 0x10000000
        handle = user32.CreateDesktopW(
            _HIDDEN_DESKTOP_NAME, None, None, 0, GENERIC_ALL, None)
        if not handle:
            raise OSError(ctypes.get_last_error() or "CreateDesktopW failed")
        _hidden_desktop["handle"] = handle
        logger.info("  [EPF] окна уходят на отдельный рабочий стол «%s»",
                    _HIDDEN_DESKTOP_NAME)
        return _HIDDEN_DESKTOP_NAME
    except Exception as exc:                          # noqa: BLE001
        _hidden_desktop["failed"] = True
        logger.warning("  [EPF] отдельный рабочий стол недоступен (%s) — "
                       "окна будут прятаться по pid, возможно мелькание", exc)
        return None


def _epf_visible() -> bool:
    """Показывать ли окно EPF (галка «Показывать окно EPF» в интерфейсе)."""
    return _config_value("REAL_PHOTO_EPF_VISIBLE", "0").strip().lower() in (
        "1", "true", "yes", "on")


def _hide_windows_of(pid: int, *, seconds: float = 12.0) -> None:
    """Спрятать окна процесса EPF — в фоне, пока они появляются.

    `STARTUPINFO.wShowWindow = SW_HIDE` на EPF НЕ действует: это Delphi/VCL —
    главную форму показывает `Application.Run`, а `nCmdShow` от родителя оно
    игнорирует. Проверено 26.08: запуск с REAL_PHOTO_EPF_VISIBLE=0 всё равно
    даёт видимое окно «Extreme Picture Finder». Единственный рабочий способ —
    догнать окно по pid и скрыть его через ShowWindow.

    Окно появляется не мгновенно и может пересоздаваться, поэтому несколько
    секунд повторяем попытки. Любая ошибка тут не важна для поиска: не смогли
    спрятать — окно просто останется на экране.
    """
    if os.name != "nt":
        return

    def _worker() -> None:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        SW_HIDE = 0
        enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        deadline = time.monotonic() + seconds
        hidden: set[int] = set()

        def _callback(hwnd, _lparam):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid and hwnd not in hidden and user32.IsWindowVisible(hwnd):
                user32.ShowWindow(hwnd, SW_HIDE)
                hidden.add(hwnd)
            return True

        callback = enum_proc(_callback)
        # Опрос частый: при 0.3с окно EPF успевало моргнуть на экране, и это
        # выглядело как «оно всё равно выскакивает» (замечание пользователя
        # 26.08). 50 мс ловят его практически сразу после создания; нагрузка
        # ничтожна — EnumWindows проходит по списку окон, не по процессам.
        while time.monotonic() < deadline:
            try:
                user32.EnumWindows(callback, 0)
            except OSError:
                return
            time.sleep(0.05)
        if hidden:
            logger.debug("  [EPF] скрыто окон процесса %s: %d", pid, len(hidden))

    threading.Thread(target=_worker, daemon=True,
                     name=f"epf-hide-{pid}").start()


class _DesktopProcess:
    """То же, что `subprocess.Popen`, но процесс запущен своим `CreateProcessW`.

    Нужен ровно из-за одного поля — `STARTUPINFOW.lpDesktop`: стандартный
    `subprocess` его не передаёт, а без него окно EPF не увести с экрана.
    Наружу отдаёт тот минимум, которым мост пользуется: `pid`, `poll`, `wait`,
    `terminate`, `kill`.
    """

    def __init__(self, handle: int, pid: int) -> None:
        self._handle = handle
        self.pid = pid
        self.returncode: int | None = None

    def _exit_code(self) -> int | None:
        import ctypes
        from ctypes import wintypes
        code = ctypes.c_ulong()
        if not ctypes.windll.kernel32.GetExitCodeProcess(
                wintypes.HANDLE(self._handle), ctypes.byref(code)):
            return None
        STILL_ACTIVE = 259
        return None if code.value == STILL_ACTIVE else int(code.value)

    def poll(self) -> int | None:
        if self.returncode is None:
            self.returncode = self._exit_code()
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        import ctypes
        from ctypes import wintypes
        if self.returncode is not None:
            return self.returncode
        ms = 0xFFFFFFFF if timeout is None else int(timeout * 1000)
        WAIT_TIMEOUT = 0x102
        result = ctypes.windll.kernel32.WaitForSingleObject(
            wintypes.HANDLE(self._handle), ms)
        if result == WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired("EPF", timeout or 0)
        self.returncode = self._exit_code()
        return self.returncode if self.returncode is not None else 0

    def terminate(self) -> None:
        import ctypes
        from ctypes import wintypes
        if self.poll() is None:
            ctypes.windll.kernel32.TerminateProcess(
                wintypes.HANDLE(self._handle), 1)

    kill = terminate

    def __del__(self) -> None:
        try:
            import ctypes
            from ctypes import wintypes
            if self._handle:
                ctypes.windll.kernel32.CloseHandle(
                    wintypes.HANDLE(self._handle))
        except Exception:                              # noqa: BLE001
            pass


def _popen_on_desktop(command: str, *, cwd: str, stdout, stderr,
                      desktop: str) -> _DesktopProcess:
    """Запускает команду на указанном рабочем столе."""
    import ctypes
    from ctypes import wintypes

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
            ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD),
        ]

    import msvcrt
    kernel32 = ctypes.windll.kernel32
    HANDLE_FLAG_INHERIT = 0x1

    def inheritable(stream) -> int:
        handle = msvcrt.get_osfhandle(stream.fileno())
        kernel32.SetHandleInformation(wintypes.HANDLE(handle),
                                      HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT)
        return handle

    startup = STARTUPINFOW()
    startup.cb = ctypes.sizeof(STARTUPINFOW)
    startup.lpDesktop = desktop
    STARTF_USESTDHANDLES = 0x100
    STARTF_USESHOWWINDOW = 0x1
    startup.dwFlags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW
    startup.wShowWindow = 0                            # SW_HIDE
    startup.hStdInput = None
    startup.hStdOutput = wintypes.HANDLE(inheritable(stdout))
    startup.hStdError = wintypes.HANDLE(inheritable(stderr))

    info = PROCESS_INFORMATION()
    CREATE_NO_WINDOW = 0x08000000
    buffer = ctypes.create_unicode_buffer(command)
    ok = kernel32.CreateProcessW(
        None, buffer, None, None, True, CREATE_NO_WINDOW, None, cwd,
        ctypes.byref(startup), ctypes.byref(info))
    if not ok:
        raise OSError(f"CreateProcessW failed: {ctypes.get_last_error()}")
    kernel32.CloseHandle(info.hThread)
    return _DesktopProcess(int(info.hProcess), int(info.dwProcessId))


def _launch_search(
    epf_exe: Path,
    data_dir: Path,
    search_file_name: str,
    *,
    item: dict[str, Any],
) -> tuple[subprocess.Popen, dict[str, tuple[Any, int] | None]]:
    if not search_file_name:
        raise RuntimeError("EPF search file was not registered")
    startupinfo = None
    creationflags = 0
    if os.name == "nt":
        visible = _epf_visible()
        if visible:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = getattr(subprocess, "SW_SHOWNORMAL", 1)
        else:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0
            creationflags = 0x08000000
    clip_dir = Path(item.get("clip_dir") or Path(item["epf_search_path"]).parent)
    stdout_path = clip_dir / "epf_stdout.log"
    stderr_path = clip_dir / "epf_stderr.log"
    # Ключа тихого запуска у EPF НЕТ. Строка `/silent`, которую я принял за
    # аргумент, на деле кусок URL отчёта о шаблонах
    # (`.../templates/silent-template-report.php`) — проверено по контексту в
    # бинарнике 26.08, и проба подтвердила: с ней и без неё окно появляется
    # одинаково. Режим окна задаётся только настройками программы
    # (`StartMinimized`, `MinimizeToTray`, `ShowTrayIcon`) и принудительным
    # скрытием по pid.
    command = (
        f'"{epf_exe}" update '
        f'SearchFileName="{search_file_name}"'
    )
    previous = _set_epf_runtime_paths(epf_exe, data_dir)
    desktop = None if _epf_visible() else _hidden_desktop_name()
    try:
        with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
            if desktop:
                # окно уедет на другой рабочий стол — на экране его не будет
                process = _popen_on_desktop(
                    command,
                    cwd=str(epf_exe.parent),
                    stdout=stdout,
                    stderr=stderr,
                    desktop=desktop,
                )
            else:
                process = subprocess.Popen(
                    command,
                    cwd=str(epf_exe.parent),
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    env=os.environ.copy(),
                    startupinfo=startupinfo,
                    creationflags=creationflags,
                )
        return process, previous
    except Exception:
        _restore_epf_runtime_paths(previous)
        raise


def _wait_for_search(
    item: dict[str, Any],
    *,
    timeout_sec: int,
    stop_event=None,
    deadline_ts: float | None = None,
    poll_sec: float = 1.5,
    process: subprocess.Popen | None = None,
    progress_callback=None,
) -> dict[str, Any]:
    search_path = Path(item["epf_search_path"])
    started = time.monotonic()
    last_report = -10.0
    while True:
        candidates = _valid_downloads(Path(item["download_dir"]))
        elapsed = time.monotonic() - started
        if progress_callback is not None and elapsed - last_report >= 10:
            progress_callback(
                candidates=len(candidates),
                elapsed_sec=round(elapsed, 1),
                process_returncode=process.poll() if process is not None else None,
            )
            last_report = elapsed
        if _epr_is_completed(search_path):
            return {
                "clip_id": _safe_clip_id(item["clip_id"]),
                "status": "completed",
                "candidates": len(candidates),
                "elapsed_sec": round(elapsed, 1),
                "process_returncode": process.poll() if process is not None else None,
            }
        # Процесс EPF уже вышел, а отметки «Completed» в отчёте нет: он либо
        # ничего не нашёл, либо упал. Ждать больше нечего — раньше цикл досиживал
        # весь таймаут (клип 013 прогона 25.08 простоял 180 с при `exit 0` и нуле
        # кандидатов) и столько же терял каждый пустой клип.
        returncode = process.poll() if process is not None else None
        if returncode is not None:
            # Небольшая отсрочка: файлы могли дописаться в последний момент.
            time.sleep(min(poll_sec, 1.0))
            candidates = _valid_downloads(Path(item["download_dir"]))
            if not _epr_is_completed(search_path):
                logger.info("  [EPF] clip %03d: процесс вышел (код %s), кандидатов %s — "
                            "жду дальше нечего", _safe_clip_id(item["clip_id"]),
                            returncode, len(candidates))
                return {
                    "clip_id": _safe_clip_id(item["clip_id"]),
                    "status": "finished" if candidates else "empty",
                    "candidates": len(candidates),
                    "elapsed_sec": round(elapsed, 1),
                    "process_returncode": returncode,
                }
        if stop_event is not None and stop_event.is_set():
            return {
                "clip_id": _safe_clip_id(item["clip_id"]),
                "status": "stopped",
                "candidates": len(candidates),
                "elapsed_sec": round(elapsed, 1),
            }
        if deadline_ts is not None and time.time() >= deadline_ts:
            return {
                "clip_id": _safe_clip_id(item["clip_id"]),
                "status": "deadline",
                "candidates": len(candidates),
                "elapsed_sec": round(elapsed, 1),
            }
        if elapsed >= timeout_sec:
            return {
                "clip_id": _safe_clip_id(item["clip_id"]),
                "status": "timeout",
                "candidates": len(candidates),
                "elapsed_sec": round(elapsed, 1),
                "process_returncode": process.poll() if process is not None else None,
            }
        time.sleep(poll_sec)


def run_job(
    job_dir: str | Path,
    *,
    provider: str | None = None,
    model: str | None = None,
    timeout_sec: int | None = None,
    stop_event=None,
    deadline_ts: float | None = None,
) -> dict[str, Any]:
    """Run EPF searches while vision-selecting finished clips in the background."""
    job = Path(job_dir).resolve()
    manifest_path = job / "bridge_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not manifest.get("automatic"):
        raise RuntimeError("Job was not registered with EPF and cannot run automatically")

    epf_exe = Path(manifest["epf_executable"])
    if not epf_exe.is_file():
        raise FileNotFoundError(f"EPF executable missing: {epf_exe}")
    epf_data_dir = Path(manifest["epf_data_dir"])
    if not epf_data_dir.is_dir():
        raise FileNotFoundError(f"EPF data directory missing: {epf_data_dir}")
    per_search_timeout = max(
        30,
        int(timeout_sec or _config_value("REAL_PHOTO_EPF_SEARCH_TIMEOUT", "180") or 180),
    )
    _bridge_event(
        job,
        "job_started",
        items=len(manifest.get("items", [])),
        timeout_sec=per_search_timeout,
        epf_executable=str(epf_exe),
        message=(
            f"automatic job started: {len(manifest.get('items', []))} clips, "
            f"{per_search_timeout}s timeout per clip"
        ),
    )
    items = manifest.get("items", [])
    search_results: list[dict[str, Any]] = []
    vision_results: list[dict[str, Any]] = []
    ingest_results: list[dict[str, Any]] = []
    vision_futures: dict[Future, int] = {}

    def _select_and_ingest(item: dict[str, Any]) -> dict[str, Any]:
        clip_id = _safe_clip_id(item["clip_id"])
        if stop_event is not None and stop_event.is_set():
            return {"clip_id": clip_id, "stopped": True, "vision": {}, "ingest": {}}
        _bridge_event(
            job,
            "vision_started",
            clip_id=clip_id,
            provider=provider or "",
            model=model or "",
            message=f"clip {clip_id:03d}: vision selection started",
        )
        vision = select_job_with_vision(
            job,
            provider=provider,
            model=model,
            clip_ids=[clip_id],
            write_summary=False,
        )
        _bridge_event(
            job,
            "vision_finished",
            clip_id=clip_id,
            message=f"clip {clip_id:03d}: vision selection finished",
            **vision,
        )
        if stop_event is not None and stop_event.is_set():
            return {"clip_id": clip_id, "stopped": True, "vision": vision, "ingest": {}}
        # Real Photo has priority over an already-created AI placeholder.
        ingested = ingest_job(
            job,
            replace=True,
            clip_ids=[clip_id],
            write_summary=False,
        )
        _bridge_event(
            job,
            "ingest_finished",
            clip_id=clip_id,
            message=f"clip {clip_id:03d}: imported {ingested.get('ingested', 0)} image",
            **ingested,
        )
        return {"clip_id": clip_id, "vision": vision, "ingest": ingested}

    def _collect_finished() -> None:
        for future in [f for f in vision_futures if f.done()]:
            clip_id = vision_futures.pop(future)
            try:
                stage = future.result()
            except Exception as exc:
                logger.warning("  [EPF] clip %03d vision/import failed: %s", clip_id, exc)
                vision_results.append(
                    {"clip_id": clip_id, "status": "selection_failed", "error": str(exc)}
                )
                continue
            vision_results.extend(stage.get("vision", {}).get("results", []))
            ingest_results.extend(stage.get("ingest", {}).get("results", []))

    vision_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="epf-vision")
    try:
        for index, item in enumerate(items, 1):
            _collect_finished()
            clip_id = _safe_clip_id(item["clip_id"])
            if stop_event is not None and stop_event.is_set():
                search_results.append({"clip_id": clip_id, "status": "stopped", "candidates": 0})
                continue
            if deadline_ts is not None and time.time() >= deadline_ts:
                search_results.append({"clip_id": clip_id, "status": "deadline", "candidates": 0})
                continue
            logger.info(
                "  [EPF %s/%s] clip %03d: %s",
                index,
                len(items),
                clip_id,
                item["query"],
            )
            process = None
            registry_snapshot: dict[str, tuple[Any, int] | None] = {}
            try:
                process, registry_snapshot = _launch_search(
                    epf_exe,
                    epf_data_dir,
                    str(item["epf_search_file"]),
                    item=item,
                )
                _bridge_event(
                    job,
                    "search_started",
                    clip_id=clip_id,
                    index=index,
                    total=len(items),
                    pid=process.pid,
                    command=[
                        str(epf_exe),
                        "update",
                        f'SearchFileName="{item["epf_search_file"]}"',
                    ],
                    message=f"clip {clip_id:03d}: EPF update started (pid {process.pid})",
                )

                def _report_progress(**fields):
                    return_code = fields.get("process_returncode")
                    process_state = "running" if return_code is None else f"exit {return_code}"
                    _bridge_event(
                        job,
                        "search_progress",
                        clip_id=clip_id,
                        message=(
                            f"clip {clip_id:03d}: waiting {fields.get('elapsed_sec', 0):.1f}s, "
                            f"{fields.get('candidates', 0)} candidates, EPF {process_state}"
                        ),
                        **fields,
                    )

                result = _wait_for_search(
                    item,
                    timeout_sec=per_search_timeout,
                    stop_event=stop_event,
                    deadline_ts=deadline_ts,
                    process=process,
                    progress_callback=_report_progress,
                )
            except Exception as exc:
                logger.warning("  [EPF] clip %03d failed: %s", clip_id, exc)
                result = {
                    "clip_id": clip_id,
                    "status": "launch_failed",
                    "error": str(exc),
                    "candidates": 0,
                }
            finally:
                _stop_epf_process(process)
                _restore_epf_runtime_paths(registry_snapshot)
            search_results.append(result)
            logger.info(
                "  [EPF] clip %03d: %s (%s candidates)",
                clip_id,
                result["status"],
                result.get("candidates", 0),
            )
            _bridge_event(
                job,
                "search_finished",
                message=(
                    f"clip {clip_id:03d}: {result['status']}, "
                    f"{result.get('candidates', 0)} candidates"
                ),
                **result,
            )
            # Кандидаты решают, а не отметка в отчёте EPF. Статусы «finished»
            # (процесс вышел, Completed=True не проставлен) и «timeout» с уже
            # скачанными файлами раньше выбрасывались целиком: картинки лежали
            # на диске, но вижн их не видел, и сцена уходила в добор (аудит
            # 25.08 — ровно тот случай, ради которого делали ранний выход).
            if result.get("candidates", 0) and result.get("status") not in ("stopped",):
                vision_futures[vision_worker.submit(_select_and_ingest, item)] = clip_id

        while vision_futures:
            _collect_finished()
            if not vision_futures:
                break
            if ((stop_event is not None and stop_event.is_set())
                    or (deadline_ts is not None and time.time() >= deadline_ts)):
                break
            wait(vision_futures, timeout=0.25, return_when=FIRST_COMPLETED)
    finally:
        _collect_finished()
        for future in vision_futures:
            future.cancel()
        vision_worker.shutdown(wait=False, cancel_futures=True)

    vision = {
        "backend": "extreme_picture_finder",
        "provider": provider or _config_value("REAL_PHOTO_VISION_PROVIDER", "codex"),
        "model": model or _config_value("REAL_PHOTO_VISION_MODEL", ""),
        "results": sorted(vision_results, key=lambda item: item.get("clip_id", 0)),
    }
    (job / "vision_selection_result.json").write_text(
        json.dumps(vision, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    project = Path(manifest["project_dir"]) if manifest.get("project_dir") else job.parents[3]
    ingested = {
        "backend": "extreme_picture_finder",
        "project": project.name,
        "generated_dir": str(project / "generated"),
        "results": sorted(ingest_results, key=lambda item: item.get("clip_id", 0)),
        "ingested": sum(item.get("status") == "ingested" for item in ingest_results),
        "missing": sum(
            item.get("status") in {"no_download", "awaiting_selection"}
            for item in ingest_results
        ),
        "awaiting_selection": sum(
            item.get("status") == "awaiting_selection" for item in ingest_results
        ),
    }
    (job / "ingest_result.json").write_text(
        json.dumps(ingested, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    summary = {
        "backend": "extreme_picture_finder",
        "job_dir": str(job),
        "searches": search_results,
        "vision": vision,
        "ingest": ingested,
        "ingested": ingested.get("ingested", 0),
    }
    (job / "automatic_result.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def run_automatic_job(
    project_dir: str | Path,
    selections: Iterable[Any],
    *,
    provider: str | None = None,
    model: str | None = None,
    max_files_per_clip: int | None = None,
    timeout_sec: int | None = None,
    stop_event=None,
    deadline_ts: float | None = None,
) -> dict[str, Any]:
    """Create and execute an EPF bridge job in one automatic pipeline call."""
    max_files = max(
        1,
        int(max_files_per_clip or _config_value("REAL_PHOTO_EPF_CANDIDATES", "12") or 12),
    )
    job = create_job(
        project_dir,
        selections,
        max_files_per_clip=max_files,
        register_with_epf=True,
    )
    return run_job(
        job,
        provider=provider,
        model=model,
        timeout_sec=timeout_sec,
        stop_event=stop_event,
        deadline_ts=deadline_ts,
    )


def _taken_frames(manifest: dict[str, Any]) -> set[str]:
    """Имена кадров, уже импортированных другими сценами этого задания.

    EPF ищет по схожим фразам, и соседние сцены получают ОДИН И ТОТ ЖЕ файл:
    в прогоне 25.08 кадр КПП `5fb3ce…jpg` выиграл и у сцены 3, и у сцены 12 —
    в ролике два подряд одинаковых кадра. Докстринг реюза обещает «дедуп по
    адресам гарантирует другой кадр», но на пути EPF такого дедупа не было.
    """
    project = manifest.get("project_dir")
    if not project:
        return set()
    taken: set[str] = set()
    for record in (manifest.get("imported") or []):
        name = str(record.get("origin_name") or "").strip().lower()
        if name:
            taken.add(name)
    return taken


def clip_candidates(item: dict[str, Any], manifest: dict[str, Any]) -> list[Path]:
    """Кандидаты клипа из папки закачек EPF, уже без фотостоков.

    Единая точка для ВСЕХ маршрутов моста (пакетный `run_job` и поштучный
    `download_candidates`): галка «Без стоковых фото» отсекает превью с водяными
    знаками по домену источника. Улов 25.08: отсев висел только на поштучном
    пути, а Auto Visual Story ходит пакетным — и стоки доезжали до вижна."""
    found = _readable_images(_valid_downloads(Path(item["download_dir"])))
    kept, dropped = drop_stock_downloads(
        found,
        epf_data_dir=manifest.get("epf_data_dir") or "",
        data_file_name=str(item.get("epf_data_file") or ""),
    )
    if dropped:
        logger.info(
            "  [EPF] clip %03d: отброшено стоков %s (%s)",
            _safe_clip_id(item.get("clip_id")), len(dropped),
            ", ".join(sorted({host for _, host in dropped})),
        )
    taken = _taken_frames(manifest)
    if taken:
        fresh = [path for path in kept if path.name.lower() not in taken]
        if len(fresh) != len(kept):
            logger.info("  [EPF] clip %03d: %s кадр(ов) уже заняты другими сценами — "
                        "не показываю вижну",
                        _safe_clip_id(item.get("clip_id")), len(kept) - len(fresh))
        kept = fresh
    # Мелкие картинки до вижна не доходят: на панели контактного листа они
    # выглядят нормально, а в ролике это мыло во весь экран (улов 25.08 —
    # сцена получила кадр 239x317).
    from units.vision_picker import drop_tiny_images

    kept, tiny = drop_tiny_images(kept)
    if tiny:
        logger.info("  [EPF] clip %03d: отброшено мелких кадров %s (%s)",
                    _safe_clip_id(item.get("clip_id")), len(tiny),
                    ", ".join(size for _, size in tiny[:4]))
    return kept


def download_candidates(
    project_dir: str | Path,
    selection: Any,
    *,
    max_files: int | None = None,
    timeout_sec: int | None = None,
    stop_event=None,
    deadline_ts: float | None = None,
) -> tuple[list[Path], Path]:
    """Запустить EPF на ОДИН клип и отдать скачанные картинки БЕЗ выбора и импорта.

    Нужна Real Photo с 25.08: EPF там стал обычным источником каскада (его
    позицию задаёт пользователь стрелками), а решение принимает общий вижн —
    либо по листу этого источника, либо по общему листу в режиме «микс».
    Возвращает (пути кандидатов по убыванию размера, папка клипа в job'е)."""
    # Дорожку берём ДО подготовки задания: EPF ищет файл задания в СВОЕЙ папке
    # (`Searches/`), поэтому регистрировать его надо там же, откуда экземпляр
    # будет читать. Первая версия дорожек регистрировала задание в базовой
    # папке, а запускала EPF с дорожечной — процесс крутился вхолостую по 90
    # секунд и возвращал ноль кандидатов (проба 26.08).
    wanted = max(1, int(max_files or _config_value("REAL_PHOTO_EPF_CANDIDATES", "12") or 12))
    # Запас перебора нужен ТОЛЬКО когда EPF качает стоки сам. С секцией
    # `[ExcludedURLs]` он их не берёт вовсе, и запрошенное число = число
    # пригодных: проба 26.08 на одном запросе дала 5 стоков из 6 без секции и
    # 0 из 6 с ней, за то же время. Тогда утраивать скачивание незачем.
    # Когда галка «Без стоковых фото» выключена, секции нет — фильтровать будем
    # уже после скачивания, и запас снова имеет смысл.
    if _no_stock_enabled():
        overfetch = 1
    else:
        overfetch = max(1, min(6, int(_config_value("REAL_PHOTO_EPF_OVERFETCH", "3") or 3)))
    lane_holder = _EpfLane()
    lane = lane_holder.__enter__()
    try:
        _base_exe, _base_data = resolve_epf_install(None, None)
        lane_data_dir = _prepare_lane_data_dir(_base_data, lane)
        job = create_job(
            project_dir,
            [selection],
            max_files_per_clip=wanted * overfetch,
            register_with_epf=True,
            epf_data_dir=lane_data_dir,
        )
        paths, clip_dir = _download_on_lane(
            job, lane, lane_data_dir, timeout_sec=timeout_sec,
            stop_event=stop_event, deadline_ts=deadline_ts)
        if len(paths) > wanted:
            logger.info("  [EPF] clip %s: после отсева осталось %d кандидатов, "
                        "беру первые %d", _safe_clip_id(selection_clip_id(selection)),
                        len(paths), wanted)
            paths = paths[:wanted]
        elif not paths:
            logger.warning(
                "  [EPF] clip %s: запрашивали %d файл(ов) (запас x%d), после отсева "
                "фотостоков не осталось НИ ОДНОГО — на этом запросе поисковик "
                "отдаёт только стоки",
                _safe_clip_id(selection_clip_id(selection)), wanted * overfetch, overfetch)
        return paths, clip_dir
    finally:
        lane_holder.__exit__(None, None, None)


def selection_clip_id(selection: Any) -> Any:
    """Номер клипа из любого вида выборки (объект Real Photo или словарь)."""
    if hasattr(selection, "clip_id"):
        return selection.clip_id
    if isinstance(selection, dict):
        return selection.get("clip_id")
    return "?"


def _download_on_lane(
    job: Path,
    lane: int,
    lane_data_dir: Path,
    *,
    timeout_sec: int | None,
    stop_event,
    deadline_ts: float | None,
) -> tuple[list[Path], Path]:
    """Запуск EPF на уже захваченной дорожке (задание зарегистрировано в её папке)."""
    manifest = json.loads((job / "bridge_manifest.json").read_text(encoding="utf-8"))
    items = manifest.get("items") or []
    if not items:
        return [], job
    item = items[0]
    clip_id = _safe_clip_id(item["clip_id"])
    clip_dir = Path(item["clip_dir"])
    epf_exe = Path(manifest["epf_executable"])
    epf_data_dir = Path(manifest["epf_data_dir"])
    per_search_timeout = max(
        30, int(timeout_sec or _config_value("REAL_PHOTO_EPF_SEARCH_TIMEOUT", "180") or 180))
    def _run_once() -> dict[str, Any]:
        """Один запуск EPF на уже захваченной дорожке.

        Дорожка — своя папка данных (там же лежит и файл задания); запрет на
        второй экземпляр снят штатной настройкой EPF. Общий замок держим ТОЛЬКО
        на запись `DataDir` в реестр и старт процесса: ключ читается при
        инициализации, дальше EPF работает со своей папкой, и запись можно
        отдать следующей дорожке.
        """
        process = None
        registry_snapshot: dict[str, tuple[Any, int] | None] = {}
        started_at = time.monotonic()
        try:
            # Короткий замок: старт по одному, поиск — параллельно.
            with _EPF_RUN_LOCK:
                process, registry_snapshot = _launch_search(
                    epf_exe, lane_data_dir, str(item["epf_search_file"]), item=item)
            if not _epf_visible():
                _hide_windows_of(process.pid)
            _bridge_event(job, "search_started", clip_id=clip_id, pid=process.pid,
                          message=(f"clip {clip_id:03d}: EPF update started "
                                   f"(pid {process.pid}"
                                   + (f", дорожка {lane}" if lane else "") + ")"))
            outcome = _wait_for_search(
                item,
                timeout_sec=per_search_timeout,
                stop_event=stop_event,
                deadline_ts=deadline_ts,
                process=process,
                progress_callback=lambda **fields: _bridge_event(
                    job, "search_progress", clip_id=clip_id,
                    message=(f"clip {clip_id:03d}: waiting {fields.get('elapsed_sec', 0):.1f}s, "
                             f"{fields.get('candidates', 0)} candidates"),
                    **fields),
            )
        except Exception as exc:
            logger.warning("  [EPF] clip %03d failed: %s", clip_id, exc)
            outcome = {"clip_id": clip_id, "status": "launch_failed",
                       "error": str(exc), "candidates": 0}
        finally:
            _stop_epf_process(process)
            with _EPF_RUN_LOCK:
                _restore_epf_runtime_paths(registry_snapshot)
        outcome["_elapsed_sec"] = time.monotonic() - started_at
        outcome["_lane"] = lane
        outcome["_data_dir"] = str(lane_data_dir)
        return outcome

    result = _run_once()
    # Сторож: мгновенный выход с нулём — это сбой запуска, а не «в интернете
    # ничего нет». Настоящий поиск идёт 12-48 секунд. Один повтор под замком
    # (прогон 26.08: клипы 8, 15, 17 вышли за 2-4 секунды с нулём, а на
    # повторе тот же запрос давал по 5 кандидатов).
    if (not result.get("candidates")
            and result.get("_elapsed_sec", 999) < _EPF_SUSPICIOUS_EXIT_SEC
            and not (stop_event is not None and stop_event.is_set())):
        logger.warning(
            "  [EPF] clip %03d: вышел за %.1fс с нулём кандидатов — похоже на сбой "
            "запуска, повторяю", clip_id, result.get("_elapsed_sec", 0))
        _bridge_event(job, "search_retry", clip_id=clip_id,
                      message=(f"clip {clip_id:03d}: подозрительно быстрый выход "
                               f"({result.get('_elapsed_sec', 0):.1f}s) — повтор"))
        result = _run_once()
        if not result.get("candidates"):
            logger.warning("  [EPF] clip %03d: повтор тоже пуст (%.1fс)",
                           clip_id, result.get("_elapsed_sec", 0))
    result.pop("_elapsed_sec", None)
    # Домены фотостоков ищем в .dat ТОЙ дорожки, на которой шёл поиск.
    lane_data_dir = Path(result.pop("_data_dir", "") or epf_data_dir)
    result.pop("_lane", None)
    _bridge_event(job, "search_finished",
                  message=(f"clip {clip_id:03d}: {result['status']}, "
                           f"{result.get('candidates', 0)} candidates"), **result)
    logger.info("  [EPF] clip %03d: %s (%s candidates)", clip_id,
                result["status"], result.get("candidates", 0))
    # Манифест указывает на БАЗОВУЮ папку, а поиск шёл в папке дорожки — домены
    # фотостоков лежат в её `.dat`, туда и смотрим.
    lane_manifest = dict(manifest)
    lane_manifest["epf_data_dir"] = str(lane_data_dir)
    return clip_candidates(item, lane_manifest), clip_dir


def render_contained_landscape(source: Path, target: Path) -> dict[str, Any]:
    """Публичная обёртка: EPF-победитель вписывается в 16:9 с размытым фоном.
    Нужна Real Photo, когда победителя общего листа выбрали из EPF-кандидатов."""
    return _render_contained_landscape(source, target)


def _vision_image_data(path: Path, *, max_bytes: int = 60 * 1024) -> str:
    """Return a compact data URI suitable for multimodal chat APIs."""
    raw = path.read_bytes()
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    try:
        from io import BytesIO

        from PIL import Image

        with Image.open(path) as image:
            image = image.convert("RGB")
            image.thumbnail((640, 640))
            for quality in (72, 62, 52, 42, 34):
                stream = BytesIO()
                image.save(stream, format="JPEG", quality=quality, optimize=True)
                raw = stream.getvalue()
                if len(raw) <= max_bytes:
                    break
            if len(raw) > max_bytes:
                image.thumbnail((448, 448))
                stream = BytesIO()
                image.save(stream, format="JPEG", quality=38, optimize=True)
                raw = stream.getvalue()
            mime = "image/jpeg"
    except Exception as exc:                             # noqa: BLE001
        logger.warning("[EPF bridge] картинка не ужата (%s) — шлю как есть", exc)
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _image_dimensions(path: Path) -> tuple[int, int]:
    try:
        from PIL import Image

        with Image.open(path) as image:
            return image.size
    except Exception as exc:                             # noqa: BLE001
        logger.warning("[EPF bridge] размер картинки не прочитан (%s): %s", path.name, exc)
        return 0, 0


def _candidate_label(path: Path) -> str:
    width, height = _image_dimensions(path)
    if width and height:
        orientation = "landscape" if width >= height else "portrait"
        return f"{path.name} ({width}x{height}, {orientation})"
    return path.name


def _parse_selection(text: str, candidates: list[Path]) -> str:
    """Extract a selected filename from a JSON vision response."""
    payload = text.strip()
    if not payload:
        raise ValueError("vision selector returned an empty response")
    if "```" in payload:
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", payload, flags=re.I | re.S)
        if fenced:
            payload = fenced.group(1)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        start = payload.find("{")
        end = payload.rfind("}")
        if start < 0 or end <= start:
            raise ValueError(
                "vision selector did not return a JSON object: "
                f"{payload[:240]!r}"
            ) from exc
        try:
            data = json.loads(payload[start:end + 1])
        except json.JSONDecodeError as nested_exc:
            raise ValueError(
                "vision selector returned malformed JSON: "
                f"{payload[:240]!r}"
            ) from nested_exc
    if not isinstance(data, dict):
        raise ValueError(f"vision selector returned {type(data).__name__}, expected object")
    chosen = str(data.get("file") or "").strip()
    names = {path.name: path for path in candidates}
    if chosen not in names:
        raise ValueError(f"vision selector returned unknown file: {chosen!r}")
    return chosen


def _write_vision_diagnostic(
    clip_dir: Path,
    *,
    provider: str,
    model: str,
    text: str,
    error: Exception | None = None,
) -> None:
    """Keep the exact model reply beside the EPF clip, including failed choices."""
    clip_dir.mkdir(parents=True, exist_ok=True)
    (clip_dir / "vision_response.txt").write_text(text or "", encoding="utf-8")
    if error is not None:
        (clip_dir / "vision_error.json").write_text(
            json.dumps(
                {
                    "provider": provider,
                    "model": model,
                    "error": str(error),
                    "response_file": "vision_response.txt",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


def _pick_epf_candidate_with_sheet(
    clip_dir: Path,
    candidates: list[Path],
    *,
    provider: str,
    model: str,
    scene_brief: str,
    search_query: str,
) -> tuple[str, Path | None, dict[str, str]]:
    """Отбор EPF-кандидатов общим модулем `units.vision_picker` (контактный лист).

    Возвращает (сырой ответ вижна, путь победителя или None, причины по кандидатам).
    None — вижн забраковал всех: клип не импортируется и достаётся архивам.
    Ошибку вижна пробрасывает наверх (старое поведение: `selection_failed`)."""
    from units.vision_picker import Candidate, pick_with_vision

    numbered = [
        (Candidate(candidate_id=f"C{index}", path=path, kind="photo",
                   title=_candidate_label(path)), path)
        for index, path in enumerate(candidates, 1)
    ]
    verdict = pick_with_vision(
        [candidate for candidate, _ in numbered],
        work_dir=clip_dir,
        provider=provider,
        model=model,
        scene_brief=scene_brief,
        forbidden=[],                      # запреты уже внутри scene_brief
        search_query=search_query,
        media_kind="photo",
        stage="epf",
    )
    raw = ""
    response_file = clip_dir / "vision_response.txt"
    if response_file.exists():
        raw = response_file.read_text(encoding="utf-8", errors="replace")
    if verdict.error:
        raise RuntimeError(verdict.error)
    if verdict.pick is None:
        return raw, None, verdict.reasons
    for candidate, path in numbered:
        if candidate.candidate_id == verdict.pick:
            return raw, path, verdict.reasons
    raise RuntimeError(f"vision selector returned unknown candidate: {verdict.pick!r}")


def select_job_with_vision(
    job_dir: str | Path,
    *,
    provider: str | None = None,
    model: str | None = None,
    clip_ids: Iterable[int] | None = None,
    write_summary: bool = True,
) -> dict[str, Any]:
    """Ask the configured vision LLM to choose one EPF candidate per clip."""
    job = Path(job_dir).resolve()
    manifest = json.loads((job / "bridge_manifest.json").read_text(encoding="utf-8"))
    provider = (provider or _config_value("REAL_PHOTO_VISION_PROVIDER", "codex")).strip().lower()
    model = (model or _config_value("REAL_PHOTO_VISION_MODEL", "")).strip()
    wanted = {int(clip_id) for clip_id in clip_ids} if clip_ids is not None else None
    results: list[dict[str, Any]] = []

    for item in manifest.get("items", []):
        clip_id = _safe_clip_id(item["clip_id"])
        if wanted is not None and clip_id not in wanted:
            continue
        clip_dir = job / f"clip_{clip_id:03d}"
        text = ""
        candidates = clip_candidates(item, manifest)[:12]
        if not candidates:
            results.append({"clip_id": clip_id, "status": "no_download"})
            continue
        # Контекст сцены от режиссёра (Auto Visual Story): описание того, что
        # должно быть в кадре, и запреты. Обещано планом 18.08, доехало 25.08.
        # Для листа он уходит как scene_brief, для claude_code — в текст задания.
        context_text = str(item.get("context_text") or "").strip()
        # Контактный лист (25.08): один общий лист вместо россыпи ужатых картинок.
        # Раньше бюджет 520 КБ делился на число кандидатов — при 12 штуках это
        # 43 КБ и 448 px на кадр, на таком мыле вижн не видит деталей. Заодно
        # появляется право забраковать всех: клип тогда уходит архивному каскаду,
        # а не закрывается мусором (решение пользователя 25.08).
        if provider != "claude_code":
            try:
                verdict, chosen_path, reasons = _pick_epf_candidate_with_sheet(
                    clip_dir,
                    candidates,
                    provider=provider,
                    model=model,
                    scene_brief=context_text or str(item.get("query") or ""),
                    search_query=str(item.get("query") or ""),
                )
            except Exception as exc:
                _write_vision_diagnostic(clip_dir, provider=provider, model=model,
                                         text="", error=exc)
                logger.warning("Vision selection failed for clip %s: %s", clip_id, exc)
                results.append({"clip_id": clip_id, "status": "selection_failed",
                                "error": str(exc)})
                continue
            selection_path = clip_dir / "selected.json"
            selection_path.write_text(
                json.dumps(
                    {
                        "file": chosen_path.name if chosen_path is not None else "",
                        "provider": provider,
                        "model": model,
                        "rejected_all": chosen_path is None,
                        "reasons": reasons,
                        "raw": verdict,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            if chosen_path is None:
                logger.info(
                    "  [EPF] clip %03d: вижн забраковал всех кандидатов — клип уйдёт "
                    "архивным источникам", clip_id,
                )
                results.append({"clip_id": clip_id, "status": "rejected_all"})
            else:
                results.append({"clip_id": clip_id, "status": "selected",
                                "file": chosen_path.name})
                # Кадр занят этой сценой — соседним его больше не показываем.
                manifest.setdefault("imported", []).append(
                    {"clip_id": clip_id, "origin_name": chosen_path.name}
                )
            continue
        # Сюда доходит только claude_code: он смотрит файлы своим Read-инструментом,
        # контактный лист ему не нужен. Остальные провайдеры ушли выше на лист.
        try:
            if provider == "claude_code":
                from units.claudecli_runner import ClaudeCodeRunner

                result_file = clip_dir / "claude_vision_selection.json"
                result_file.unlink(missing_ok=True)
                candidate_lines = "\n".join(
                    f"- {_candidate_label(path)}: {path.resolve().as_posix()}"
                    for path in candidates
                )
                task_file = clip_dir / "_claude_vision_task.md"
                task_file.write_text(
                    (
                        "Use the Read tool to inspect every image listed below. "
                        "Choose the single image that best matches the search query. "
                        "Prefer a real subject, clear composition, and useful resolution. "
                        "Reject logos, thumbnails, screenshots, watermarks, icons, AI art, "
                        "and unrelated results.\n\n"
                        + (
                            "Scene brief from the director (what the frame must show, "
                            f"and what must NOT appear): {context_text}\n"
                            "The search query below is only how the candidates were "
                            "found; judge them against the scene brief.\n\n"
                            if context_text else ""
                        )
                        + f"Search query: {item['query']}\n\n"
                        f"Candidates:\n{candidate_lines}\n\n"
                        "Write strict JSON only to this exact file:\n"
                        f"{result_file.resolve().as_posix()}\n\n"
                        'Schema: {"file":"exact_filename","reason":"brief reason"}\n'
                        "The file field must contain only one filename from the list."
                    ),
                    encoding="utf-8",
                )
                runner = ClaudeCodeRunner(model_name=model, timeout=300)
                returncode, stdout, stderr, _elapsed = runner.run_task_file(
                    task_file,
                    clip_dir,
                    label=f"epf_vision_{clip_id}",
                )
                if returncode != 0:
                    raise RuntimeError(
                        f"Claude Code vision failed (exit {returncode}): "
                        f"{stderr[-500:] or stdout[-500:]}"
                    )
                if not result_file.exists():
                    raise RuntimeError("Claude Code did not create the vision result JSON")
                text = result_file.read_text(encoding="utf-8-sig", errors="replace")
            else:                       # недостижимо: см. переход на лист выше
                raise RuntimeError(f"unexpected vision provider path: {provider}")
            chosen = _parse_selection(text, candidates)
            _write_vision_diagnostic(
                clip_dir,
                provider=provider,
                model=model,
                text=text,
            )
            selection_path = clip_dir / "selected.json"
            selection_path.write_text(
                json.dumps(
                    {"file": chosen, "provider": provider, "model": model, "raw": text},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            results.append({"clip_id": clip_id, "status": "selected", "file": chosen})
        except Exception as exc:
            _write_vision_diagnostic(
                clip_dir,
                provider=provider,
                model=model,
                text=text,
                error=exc,
            )
            logger.warning("Vision selection failed for clip %s: %s", clip_id, exc)
            results.append({"clip_id": clip_id, "status": "selection_failed", "error": str(exc)})

    result = {
        "backend": "extreme_picture_finder",
        "provider": provider,
        "model": model,
        "results": results,
    }
    if write_summary:
        (job / "vision_selection_result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return result


def _render_contained_landscape(source: Path, target: Path) -> dict[str, Any]:
    """Render a full uncropped image over a blurred 16:9 background."""
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps

    frame_size = (1920, 1080)
    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
        original_size = image.size
        background = ImageOps.fit(image, frame_size, method=Image.Resampling.LANCZOS)
        background = background.filter(ImageFilter.GaussianBlur(radius=32))
        background = ImageEnhance.Brightness(background).enhance(0.55)
        foreground = ImageOps.contain(image, frame_size, method=Image.Resampling.LANCZOS)
        x = (frame_size[0] - foreground.width) // 2
        y = (frame_size[1] - foreground.height) // 2
        background.paste(foreground, (x, y))
        background.save(target, format="JPEG", quality=94, optimize=True, subsampling=0)
    return {
        "original_width": original_size[0],
        "original_height": original_size[1],
        "output_width": frame_size[0],
        "output_height": frame_size[1],
        "fit_mode": "contain_blur",
    }


def ingest_job(
    job_dir: str | Path,
    *,
    replace: bool = False,
    clip_ids: Iterable[int] | None = None,
    write_summary: bool = True,
) -> dict[str, Any]:
    """Import vision-selected EPF files into ``generated/{id:03d}``."""
    job = Path(job_dir).resolve()
    manifest_path = job / "bridge_manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    project = Path(manifest["project_dir"]) if manifest.get("project_dir") else job.parents[3]
    generated = project / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    wanted = {int(clip_id) for clip_id in clip_ids} if clip_ids is not None else None

    results: list[dict[str, Any]] = []
    for item in manifest.get("items", []):
        clip_id = _safe_clip_id(item["clip_id"])
        if wanted is not None and clip_id not in wanted:
            continue
        source_dir = Path(item["download_dir"])
        candidates = clip_candidates(item, manifest)
        if not candidates:
            results.append({"clip_id": clip_id, "status": "no_download"})
            continue
        selection_path = job / f"clip_{clip_id:03d}" / "selected.json"
        selected_file: Path | None = None
        if selection_path.exists():
            selected_data = json.loads(selection_path.read_text(encoding="utf-8"))
            raw_selected = str(selected_data.get("file") or "").strip()
            if raw_selected:
                selected_file = Path(raw_selected)
                if not selected_file.is_absolute():
                    download_path = (source_dir / selected_file).resolve()
                    local_path = (selection_path.parent / selected_file).resolve()
                    selected_file = download_path if download_path.exists() else local_path
        if selected_file is None or selected_file not in candidates:
            results.append(
                {
                    "clip_id": clip_id,
                    "status": "awaiting_selection",
                    "candidates": [str(path) for path in candidates],
                    "selection_file": str(selection_path),
                }
            )
            continue
        target = generated / f"{clip_id:03d}.jpg"
        existing = list(generated.glob(f"{clip_id:03d}.*"))
        if existing and not replace:
            results.append({"clip_id": clip_id, "status": "exists", "file": str(existing[0])})
            continue
        staged_target = target.with_suffix(".new.jpg")
        staged_target.unlink(missing_ok=True)
        try:
            render_info = _render_contained_landscape(selected_file, staged_target)
        except Exception as exc:   # аргумент разобран не будет — идём дальше
            staged_target.unlink(missing_ok=True)
            results.append(
                {
                    "clip_id": clip_id,
                    "status": "render_failed",
                    "source_file": str(selected_file),
                    "error": str(exc),
                }
            )
            continue
        for old in existing:
            old.unlink()
        staged_target.replace(target)
        results.append(
            {
                "clip_id": clip_id,
                "status": "ingested",
                "file": str(target),
                "source_file": str(selected_file),
                "license": "unknown",
                **render_info,
            }
        )

    summary = {
        "backend": "extreme_picture_finder",
        "project": project.name,
        "generated_dir": str(generated),
        "results": results,
        "ingested": sum(result["status"] == "ingested" for result in results),
        "missing": sum(
            result["status"] in {"no_download", "awaiting_selection"} for result in results
        ),
        "awaiting_selection": sum(
            result["status"] == "awaiting_selection" for result in results
        ),
    }
    if write_summary:
        (job / "ingest_result.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return summary


def _cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create-job")
    create.add_argument("project_dir")
    create.add_argument("selections_json")
    create.add_argument("--register", action="store_true")
    run = sub.add_parser("run")
    run.add_argument("job_dir")
    run.add_argument("--provider", default=None)
    run.add_argument("--model", default=None)
    run.add_argument("--timeout", type=int, default=None)
    ingest = sub.add_parser("ingest")
    ingest.add_argument("job_dir")
    ingest.add_argument("--replace", action="store_true")
    select = sub.add_parser("select")
    select.add_argument("job_dir")
    select.add_argument("--provider", default=None)
    select.add_argument("--model", default=None)
    args = parser.parse_args()

    if args.command == "create-job":
        data = json.loads(Path(args.selections_json).read_text(encoding="utf-8"))
        print(create_job(args.project_dir, data, register_with_epf=args.register))
    elif args.command == "run":
        print(
            json.dumps(
                run_job(args.job_dir, provider=args.provider, model=args.model, timeout_sec=args.timeout),
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.command == "ingest":
        print(json.dumps(ingest_job(args.job_dir, replace=args.replace), ensure_ascii=False, indent=2))
    else:
        print(
            json.dumps(
                select_job_with_vision(args.job_dir, provider=args.provider, model=args.model),
                ensure_ascii=False,
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
