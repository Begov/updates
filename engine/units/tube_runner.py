"""Canonical YouTube footage pipeline for montage scenes.

The stage has one runtime path:

planner queries -> licensed discovery -> temporal locator -> fixed parts ->
preview contact sheet -> Vision ranking by candidate ID -> range downloads ->
multi-part composition -> provenance and terminal fallback.

The director supplies the scene and its duration. The module owns YouTube
video IDs, source timestamps, part durations, downloads, and composition.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

import requests
from PIL import Image, ImageDraw

import params as config
from units.footage_trimmer import cut_footage, probe_video

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
TOKEN_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]{3,}")
YOUTUBE_DATA_API_URL = "https://www.googleapis.com/youtube/v3"
CHAT_PROVIDERS = frozenset({
    "deepseek",
    "openrouter",
    "meta_muse",
    "claude_api",
    "codex_reseller",
    "gpt_nexus",
})
RIGHTS_RESTRICTION_PATTERNS = (
    ("non-commercial", re.compile(r"\bnon[- ]?commercial\b", re.I)),
    ("no commercial use", re.compile(r"\bno commercial use\b", re.I)),
    ("not for commercial use", re.compile(r"\bnot for commercial use\b", re.I)),
    ("commercial use prohibited", re.compile(r"\bcommercial use (?:is )?prohibited\b", re.I)),
    ("permission required", re.compile(r"\bpermission (?:is )?required\b", re.I)),
    ("may not be used", re.compile(r"\bmay not be used\b", re.I)),
)


class YouTubeDiscoveryError(RuntimeError):
    """The selected discovery backend could not return a usable pool."""


def _tokens(text: str) -> set[str]:
    return {item.lower() for item in TOKEN_RE.findall(text or "")}


def _rights_text_flags(text: str) -> list[str]:
    return [
        label for label, pattern in RIGHTS_RESTRICTION_PATTERNS
        if pattern.search(text or "")
    ]


def _yt_dlp_path() -> str:
    """Наш yt-dlp рядом с софтом, иначе системный.

    Имя файла зависит от системы: `yt-dlp.exe` под Windows, `yt-dlp_macos`
    под macOS, `yt-dlp_linux` под Linux — ровно то, что кладёт кнопка
    «Установить YouTube-модуль». Раньше искался только `.exe`, и на macOS
    рядом лежащий бинарник не подхватывался вовсе (разбор 29.08).
    """
    configured = str(getattr(config, "YOUTUBE_DLP_PATH", "") or "").strip()
    if configured:
        return configured
    root = Path(__file__).resolve().parents[2]
    # Имя для СВОЕЙ системы идёт первым: на macOS рядом мог остаться
    # `yt-dlp.exe` от прежней версии установщика, и он бы победил.
    names = ["yt-dlp.exe", "yt-dlp_macos", "yt-dlp_linux", "yt-dlp"]
    try:
        from helpers.youtube_setup import ytdlp_name

        mine = ytdlp_name()
        names = [mine] + [n for n in names if n != mine]
    except Exception:                                # noqa: BLE001
        pass
    for name in names:
        bundled = root / name
        if bundled.is_file():
            return str(bundled)
    return shutil.which("yt-dlp") or "yt-dlp"


BOT_CHECK_RE = re.compile(r"sign in to confirm you.{0,3}re not a bot", re.I)
# Обратная беда: с куками ЗАЛОГИНЕННОГО аккаунта yt-dlp уходит на клиентов
# web_embedded/tv, и ролик, запрещённый к встраиванию, отвечает UNPLAYABLE —
# хотя без аккаунта отдаётся нормально (улов 25.08). Раньше от этого берегли
# весь прогон, пуская «без куков» первым вариантом; теперь ловим точечно: на
# конкретном ролике возвращаемся к попытке без куков.
UNPLAYABLE_RE = re.compile(
    r"unplayable|playback on other websites has been disabled|"
    r"video is not available on this app", re.I)
# Куки браузера физически не читаются (запущенный Chrome держит базу, DPAPI и
# т.п.). Проба `--version` этого не ловит — она куки не открывает, — поэтому
# такой браузер вылетает из плана по первому реальному ответу yt-dlp.
COOKIE_DB_ERROR_RE = re.compile(
    r"could not copy .{0,30}cookie database|unable to (?:read|open) .{0,30}cookie|"
    r"failed to decrypt|permission denied .{0,30}cookie", re.I)
# Ограничение частоты — НЕ бот-проверка (поправка пользователя 27.08).
# Бот-проверка значит «ютуб не видит залогиненного профиля» и лечится куками;
# здесь ютуб говорит «слишком часто», и лечится это ТОЛЬКО паузой. Смешивать их
# нельзя: на ограничение частоты перебор браузеров лишь добавляет запросов.
RATE_LIMIT_RE = re.compile(
    r"http error 429|too many requests|\brate[- ]?limit", re.I)
# Пауза после первого ограничения; каждое следующее удваивает её.
_THROTTLE_FIRST_PAUSE = 30.0
_THROTTLE_MAX_PAUSE = 300.0
_THROTTLE: dict[str, float] = {"hits": 0.0, "until": 0.0}
_THROTTLE_LOCK = threading.Lock()


# Что писать в лог вместо обрезка stderr. Порядок важен: сначала узкие случаи,
# потом общие — иначе «403» перебьёт более точное «нужен вход в аккаунт».
_DOWNLOAD_FAILURE_HINTS = (
    ("sign in to confirm", "ютуб требует вход в аккаунт (возраст/подтверждение) — нужны куки браузера"),
    ("private video", "ролик приватный"),
    ("video unavailable", "ролик недоступен (удалён или заблокирован в регионе)"),
    ("members-only", "ролик только для подписчиков канала"),
    ("http error 429", "ютуб ограничил частоту запросов (429)"),
    ("too many requests", "ютуб ограничил частоту запросов"),
    ("403 forbidden", "403 от сервера видео: ссылка на поток не принимает наш запрос — "
                      "обычно устаревший yt-dlp"),
    ("http error 403", "403 от ютуба — обычно устаревший yt-dlp"),
    ("unable to download webpage", "страница ролика не открылась (сеть или блокировка)"),
    ("ffmpeg exited", "ffmpeg не смог забрать поток"),
    ("timed out", "истекло время ожидания сети"),
)


def _download_failure_reason(tail: str, returncode: int | None = None) -> str:
    """Человеческая причина неудачи вместо хвоста чужого лога.

    В stderr yt-dlp/ffmpeg попадают подписанные URL длиной в сотни символов, и
    обрезка «последние 160» давала бессмысленный набор букв. Здесь мы ищем
    знакомые признаки и называем их словами; если ничего не узнали — отдаём
    короткую строку из лога, но из НАЧАЛА сообщения, а не из середины ссылки.
    """
    text = (tail or "").lower()
    for needle, human in _DOWNLOAD_FAILURE_HINTS:
        if needle in text:
            return human
    for line in reversed((tail or "").splitlines()):
        line = line.strip()
        if line.lower().startswith("error") and len(line) < 200:
            return line
    return f"yt-dlp завершился с кодом {returncode}" if returncode else "причина не распознана"


def rate_limited() -> int:
    """Сколько раз ютуб ограничивал частоту в этом прогоне (0 — ни разу).

    Число работает ступенью торможения: каждое срабатывание делит параллель
    пополам и удваивает паузу. Сваливаться с восьми потоков сразу в один
    незачем — ограничение могло прийти на пике, а не на всей нашей скорости;
    и вверх мы уже не возвращаемся, так что шаг вниз должен быть осторожным.
    """
    with _THROTTLE_LOCK:
        return int(_THROTTLE["hits"])


_BOT_ADVICE_SHOWN = {"seen": False}


def _bot_check_advice(tried: set[str]) -> None:
    """Сказать вслух, что бот-проверку сняло бы: вход в аккаунт в браузере.

    Главная причина «Sign in to confirm you're not a bot» — куки БЕЗ
    авторизации: они есть, но профиль анонимный. Человеку это неоткуда узнать,
    в логе видна только ошибка yt-dlp. Пишем один раз за прогон, чтобы не
    засорять (замечание пользователя 30.08: не сваливать всё на темп).
    """
    if _BOT_ADVICE_SHOWN["seen"]:
        return
    _BOT_ADVICE_SHOWN["seen"] = True
    browsers = ", ".join(sorted(b for b in tried if b)) or "ни одного браузера"
    message = (
        "  [ютуб] ютуб просит подтвердить, что вы не робот, и это повторилось "
        f"на всех вариантах куков ({browsers}). Чаще всего причина одна: ни в "
        "одном профиле нет входа в аккаунт Google — анонимные куки проверку не "
        "снимают. Войдите в аккаунт в том браузере, откуда берутся куки (лучше "
        "всего Firefox), закройте его, чтобы куки записались на диск, и "
        "повторите. Если вход есть, попробуйте убавить «Запросов к ютубу "
        "разом» в настройках стадии."
    )
    logging.warning(message)
    print(message)


def _note_rate_limit(where: str, detail: str = "") -> float:
    """Ютуб притормозил — ступень вниз и пауза всей стадии. Вернёт её длину."""
    with _THROTTLE_LOCK:
        _THROTTLE["hits"] += 1
        hits = int(_THROTTLE["hits"])
        pause = min(
            _THROTTLE_MAX_PAUSE,
            _THROTTLE_FIRST_PAUSE * (2 ** (hits - 1)),
        )
        _THROTTLE["until"] = max(_THROTTLE["until"], time.time() + pause)
    workers = _meta_workers(64)
    logging.warning(
        "  [ютуб/темп] ЮТУБ ОГРАНИЧИЛ ЧАСТОТУ (%s, случай %s) — пауза %.0f с, "
        "дальше не больше %s поток(ов)%s",
        where, hits, pause, workers, f": {detail[:160]}" if detail else "")
    print(f"  [YouTube] ютуб ограничил частоту запросов ({where}) — "
          f"пауза {pause:.0f} с, дальше не больше {workers} поток(ов)")
    return pause


def _throttle_wait(
    stop_event: Optional[threading.Event],
    deadline_ts: Optional[float] = None,
) -> None:
    """Дождаться конца назначенной паузы, если стадию притормозили."""
    while True:
        with _THROTTLE_LOCK:
            left = _THROTTLE["until"] - time.time()
        if left <= 0:
            return
        if stop_event is not None and stop_event.is_set():
            return
        if deadline_ts is not None and time.time() >= deadline_ts:
            return
        time.sleep(min(1.0, left))


class _RequestGate:
    """Общий потолок одновременных обращений к ютубу — на ВСЮ стадию.

    Считать параллель поклипно нельзя: четыре клипа по восемь запросов дают
    тридцать два одновременных обращения, а замеряли мы восемь (улов
    пользователя 27.08). Ворота держат общее число независимо от того, сколько
    клипов идёт разом и что именно они делают — метаданные, поиск или загрузку.

    Потолок читается на каждой проверке, поэтому пойманное ограничение частоты
    сужает ворота немедленно, не дожидаясь конца текущих запросов.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._active = 0

    @contextmanager
    def slot(self, stop_event: Optional[threading.Event] = None):
        with self._cond:
            while self._active >= _meta_workers(64):
                if stop_event is not None and stop_event.is_set():
                    break
                self._cond.wait(0.5)
            self._active += 1
        try:
            yield
        finally:
            with self._cond:
                self._active -= 1
                self._cond.notify()


_YT_GATE = _RequestGate()


# Порядок попыток на один вызов yt-dlp: сперва без куков, затем куки браузеров
# по приоритету. Удачный вариант запоминается, чтобы следующий ролик не платил
# за перебор заново.
#
# Когда рабочий вариант найден, план ФИКСИРУЕТСЯ: дальше все запросы идут сразу
# к нему, перебора нет вовсе. Это нужно для параллельной работы — иначе
# несколько потоков одновременно перебирали бы браузеры и переписывали
# «победителя» друг у друга (решение пользователя 27.08). Фиксация снимается
# только если зафиксированный вариант перестал работать: ютуб умеет выдать
# бот-проверку в середине прогона, и без этого прогон умер бы на первой осечке.
_COOKIE_PLAN: dict[str, Any] = {}
_COOKIE_LOCK = threading.Lock()


_YTDLP_VERSION_LOGGED = threading.Event()


def log_ytdlp_version(printer=None) -> str:
    """Один раз за прогон записать версию yt-dlp — и сказать, если она стара.

    Версия нигде не печаталась, а yt-dlp НЕ едет с обновлениями (он исключён из
    патча как крупный бинарник) — у пользователя остаётся тот файл, что был в
    скачанном дистрибутиве. Ютуб ломает совместимость регулярно, и старый
    бинарник даёт 403 на ровном месте: разбирая жалобу 29.08, версию нельзя
    было узнать ни из логов, ни из отчётов — приходилось просить пользователя
    выполнить команду руками.
    """
    if _YTDLP_VERSION_LOGGED.is_set():
        return ""
    _YTDLP_VERSION_LOGGED.set()
    path = _yt_dlp_path()
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        done = subprocess.run([path, "--version"], capture_output=True, text=True,
                              timeout=30, **flags)
        version = (done.stdout or "").strip().splitlines()[0].strip()
    except Exception as exc:                       # noqa: BLE001
        logging.warning("   [ютуб] версия yt-dlp не определилась (%s): %s",
                        type(exc).__name__, exc)
        return ""
    if not version:
        return ""
    # Версия yt-dlp — это дата (2026.08.19). Считаем возраст: старше двух
    # месяцев почти всегда означает отказы на стороне ютуба.
    note = ""
    try:
        from datetime import date

        parts = [int(x) for x in version.split(".")[:3]]
        age_days = (date.today() - date(parts[0], parts[1], parts[2])).days
        if age_days > 60:
            note = (f" — БИНАРНИК СТАРЫЙ ({age_days} дн.); ютуб часто отвечает "
                    f"403 именно поэтому, обнови yt-dlp.exe в папке софта")
        else:
            note = f" (свежий, {age_days} дн.)"
    except Exception:                              # noqa: BLE001
        pass                                       # версия нестандартного вида
    message = f"   [ютуб] yt-dlp {version}{note}"
    logging.info(message)
    if note and "СТАРЫЙ" in note and printer is not None:
        printer(message)                           # такое пользователь должен видеть
    return version


def _cookie_attempts(ytdlp_path: str = "") -> list[str]:
    """План попыток: браузеры по приоритету, последним '' — без куков.

    До 29.08 «без куков» стояло первым. Стоило первому же запросу пройти (поиск
    и первые метаданные ютуб отдаёт без куков спокойно) — план лочился на этом
    варианте, и когда через полчаса включалась бот-проверка, переключаться было
    уже некуда: 112 клипов из 132 подряд получили «без куков» и ни одной
    попытки с браузером. Теперь наоборот: куки вперёд, «без куков» — запасной
    вариант, а невстраиваемые ролики откатываются к нему точечно
    (см. `UNPLAYABLE_RE`).
    """
    with _COOKIE_LOCK:
        if "attempts" not in _COOKIE_PLAN:
            try:
                from units.yt_transcriber import browsers_ranked

                browsers = browsers_ranked(ytdlp_path or _yt_dlp_path())
            except Exception:                    # noqa: BLE001
                browsers = []
            _COOKIE_PLAN["attempts"] = [*browsers, ""]
        attempts: list[str] = list(_COOKIE_PLAN["attempts"])
        winner = _COOKIE_PLAN.get("winner")
        locked = bool(_COOKIE_PLAN.get("locked"))
    if winner is not None and winner in attempts:
        if locked:
            # План зафиксирован — идём прямо в цель, без запасных вариантов.
            return [winner]
        # Рабочий вариант вперёд, остальные остаются запасными.
        attempts.remove(winner)
        attempts.insert(0, winner)
    return attempts


def _cookie_plan_lock_in(browser: str) -> None:
    """Запомнить рабочий вариант и закрыть перебор до конца прогона."""
    with _COOKIE_LOCK:
        first = not _COOKIE_PLAN.get("locked")
        _COOKIE_PLAN["winner"] = browser
        _COOKIE_PLAN["locked"] = True
    if first:
        logging.info("   [ютуб/куки] рабочий вариант — «%s», дальше без перебора",
                     browser or "без куков")


def _cookie_plan_release(browser: str) -> None:
    """Зафиксированный вариант отказал — вернуть перебор."""
    with _COOKIE_LOCK:
        if _COOKIE_PLAN.get("winner") == browser:
            _COOKIE_PLAN.pop("winner", None)
            _COOKIE_PLAN.pop("locked", None)
            released = True
        else:
            released = False
    if released:
        logging.info("   [ютуб/куки] «%s» перестал работать — снова перебираю варианты",
                     browser or "без куков")


def _drop_cookie_browser(browser: str) -> None:
    """Выбросить браузер из плана попыток до конца прогона."""
    attempts = _COOKIE_PLAN.get("attempts") or []
    if browser in attempts:
        attempts.remove(browser)
        _COOKIE_PLAN["attempts"] = attempts
    if _COOKIE_PLAN.get("winner") == browser:
        _COOKIE_PLAN.pop("winner", None)
    logging.info("   [ютуб/куки] «%s» не отдаёт базу куков — исключён из плана, осталось: %s",
                 browser, ", ".join(x or "без куков" for x in attempts) or "ничего")


def _cookie_followups(tail: str, browser: str, tried: set[str],
                      planned: list[str]) -> list[str]:
    """Чем ещё стоит попробовать ЭТОТ ЖЕ ролик, судя по ответу ютуба.

    Раньше на бот-проверке мы только снимали замок с плана — и уходили ни с
    чем, потому что список попыток был получен ДО этого и состоял из одного
    залоченного варианта. Замок снимался для следующего клипа, а текущий
    терялся. Теперь недостающие варианты дописываются в конец текущего
    перебора, и клип доигрывается здесь же.

    `tried` — что уже пробовали в этом вызове, `planned` — что осталось
    впереди; повторов не выдаём.
    """
    if BOT_CHECK_RE.search(tail):
        # Бот-проверка лечится куками залогиненного профиля — открываем перебор.
        _cookie_plan_release(browser)
        extra = [item for item in _cookie_attempts()
                 if item not in tried and item not in planned]
        if extra:
            logging.info("   [ютуб/куки] бот-проверка на «%s» → добавляю в перебор: %s",
                         browser or "без куков",
                         ", ".join(item or "без куков" for item in extra))
        return extra
    if browser and UNPLAYABLE_RE.search(tail):
        # Невстраиваемый ролик: как раз тот случай, когда аккаунт мешает.
        if "" not in tried and "" not in planned:
            logging.info("   [ютуб/куки] «%s»: ролик запрещён к встраиванию → "
                         "пробую без куков", browser)
            return [""]
    return []


def _prepare_ytdlp_command(
    cmd: list[str],
    *,
    include_browser_cookies: bool = True,
    browser: Optional[str] = None,
) -> list[str]:
    inject: list[str] = []
    # Личный конфиг пользователя (%APPDATA%\yt-dlp\config.txt) применяется и к нашим
    # вызовам: его -f роняет --dump-single-json на роликах без такого формата
    # («Requested format is not available» на старых 240p без прогрессивного avc+aac,
    #  проверено 25.08). Пайплайн задаёт всё нужное сам → чужой конфиг игнорируем.
    if "--ignore-config" not in cmd and "--no-config" not in cmd:
        inject.append("--ignore-config")
    # JS-рантайм. Раньше здесь стояло `shutil.which("node")`: у кого Node не
    # установлен — флага нет, и yt-dlp работает без рантайма. А без него он сам
    # предупреждает: «YouTube extraction without a JS runtime has been
    # deprecated, and some formats may be missing» — часть форматов пропадает и
    # ссылки на поток не проходят проверку подписи, отсюда 403 (разбор 29.08:
    # у разработчика Node стоял, у пользователей нет — потому и «у меня
    # работает»). Теперь ищем ещё и deno, включая свой в agent/tools/,
    # положенный кнопкой «Установить YouTube-модуль».
    if "--js-runtimes" not in cmd:
        try:
            from helpers.youtube_setup import js_runtime_args

            inject.extend(js_runtime_args())
        except Exception:                            # noqa: BLE001
            if shutil.which("node"):                 # запасной путь как раньше
                inject.extend(["--js-runtimes", "node"])
    if include_browser_cookies and "--cookies-from-browser" not in cmd:
        # Явно названный браузер (ротация попыток) сильнее автоподбора.
        if browser is None:
            browser = str(
                getattr(config, "YTDLP_COOKIES_BROWSER", "") or ""
            ).strip().lower()
            if browser in ("none", "off", "no", "нет", "без", "без куков"):
                browser = ""
            elif not browser:
                try:
                    from units.yt_transcriber import _detect_browser

                    browser = _detect_browser(_yt_dlp_path())
                except Exception as exc:                 # noqa: BLE001
                    logging.warning("   [ютуб/куки] автоподбор браузера не удался: %s "
                                    "— идём без куков", exc)
                    browser = ""
        if browser:
            inject.extend(["--cookies-from-browser", browser])
    return [cmd[0], *inject, *cmd[1:]] if inject else cmd


def _yt_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _load_plan(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    return raw.get("montage_plan", raw) if isinstance(raw, dict) else {}


def _archive_query(project_dir: Path, clip: dict[str, Any]) -> str:
    """Короткий поисковый запрос для архивов, когда ютуб сцену не закрыл.

    Планировщик уже написал клипу 3-6-словные запросы и положил их в план
    (`youtube_queries`). Раньше запасной EPF брал `_clip_text(...)[:500]` —
    абзац описания или дикторский текст, — и поисковик отдавал на него что
    угодно (Дженис Джоплин на сцену про польскую певицу). Тот же приём уже
    применён в `visual_story_epf._plan_photo_query` (аудит 25.08).
    """
    ready = clip.get("youtube_queries") or clip.get("search_queries") or []
    for item in ready if isinstance(ready, (list, tuple)) else []:
        text = str(item or "").strip()
        if text:
            return text
    for key in ("search_query", "youtube_query", "query"):
        text = str(clip.get(key) or "").strip()
        if text:
            return text
    # Готового запроса нет — просим промптера архивов написать его по сцене.
    # Механическое сжатие (`compact_query`) для этого не годится: оно берёт первые
    # значимые слова описания, а Pass 2 начинает с места действия, не с предмета.
    # 26.08 бриф про руки, укладывающие сыр в ящик, дал запрос «Timeless dispatch
    # area beside small delivery vehicle» — поиск принёс электрофургоны, вижн
    # отверг всё. Промптер читает сцену целиком и называет её предмет.
    raw = _clip_text(project_dir, clip)
    try:
        from units.archive_photos import archive_query_from_scene

        written = archive_query_from_scene(
            raw,
            voiceover=str(clip.get("voiceover") or clip.get("text") or ""),
            clip_id=clip.get("id"),
            work_dir=project_dir,
        )
        if written:
            return written
    except Exception as exc:                         # noqa: BLE001
        logging.warning("  [YouTube] клип %s: промптер запроса недоступен (%s)",
                        clip.get("id"), exc)
    # Промптер молчит — старое сжатие как аварийный вариант, лучше чем абзац.
    try:
        from units.story_ai import compact_query

        short = compact_query(raw)
        if short:
            logging.warning("  [YouTube] клип %s: запрос не написан — сжал описание до «%s»",
                            clip.get("id"), short[:70])
            return short
    except Exception as exc:                         # noqa: BLE001
        logging.warning("  [YouTube] клип %s: описание не сжато (%s) — беру начало текста",
                        clip.get("id"), exc)
    return raw[:120]


def _clip_text(project_dir: Path, clip: dict[str, Any]) -> str:
    for key in (
        "visual_intent",
        "youtube_query",
        "query",
        "voiceover",
        "text",
        "narration",
        "prompt",
    ):
        value = clip.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    transcript = project_dir / "transcription.json"
    try:
        data = json.loads(transcript.read_text(encoding="utf-8-sig"))
        fragments = data.get("transcription") or data.get("segments") or []
        start = float(clip.get("startTime", 0) or 0)
        end = float(clip.get("endTime", 0) or 0)
        return " ".join(
            str(item.get("text", ""))
            for item in fragments
            if float(item.get("start", 0) or 0) < end
            and float(item.get("end", 0) or 0) > start
        ).strip()
    except Exception as exc:                             # noqa: BLE001
        logging.warning("  [YouTube] транскрипт не разобран (%s) — сцена без текста", exc)
        return ""


def _load_visual_context(project_dir: Path, clip: dict[str, Any]) -> dict[str, Any]:
    """Load the Auto Visual Story context packet for this scene when present."""
    inline = clip.get("context")
    if isinstance(inline, dict):
        return inline
    try:
        clip_id = int(clip.get("id"))
    except (TypeError, ValueError):   # нечисловое время клипа — берём ноль
        return {}
    packet = (
        project_dir
        / "visual_story"
        / "context_packets"
        / f"{clip_id:03d}.json"
    )
    if not packet.exists():
        return {}
    try:
        data = json.loads(packet.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):   # транскрипт не прочитан — сцена без текста
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        key: data[key]
        for key in (
            "current_scene",
            "context_before",
            "context_after",
            "chapter_state",
            "entity_state",
            "visual_history",
            "continuity_constraints",
        )
        if data.get(key) not in (None, "", [], {})
    }


def _clip_duration(clip: dict[str, Any]) -> float:
    try:
        return max(
            0.1,
            float(clip.get("endTime", 0) or 0)
            - float(clip.get("startTime", 0) or 0),
        )
    except (TypeError, ValueError):   # нечисловые границы — окно пропускаем
        return 1.0


def _run_json(
    cmd: list[str],
    timeout: int,
    stop_event: Optional[threading.Event],
    deadline_ts: Optional[float] = None,
) -> Any:
    if stop_event is not None and stop_event.is_set():
        return None
    flags = {}
    if os.name == "nt":
        flags["creationflags"] = subprocess.CREATE_NO_WINDOW
    # Сначала БЕЗ куков, потом куки браузеров по приоритету: аккаунтные куки
    # уводят yt-dlp на клиентов web_embedded/tv, и невстраиваемые ролики
    # становятся UNPLAYABLE, хотя без аккаунта отдаются нормально. Но обратное
    # тоже бывает — на «Sign in to confirm you're not a bot» спасает как раз
    # залогиненный профиль, поэтому перебираем браузеры, а не долбим один
    # (улов прогона 25.08).
    _throttle_wait(stop_event, deadline_ts)
    attempts = _cookie_attempts()
    index = 0
    rate_retries = 0
    tried: set[str] = set()
    widened = False              # добор вариантов делаем один раз за вызов
    bot_checks = 0               # сколько вариантов упёрлось в бот-проверку
    while index < len(attempts):
        browser = attempts[index]
        attempt_no = index + 1
        tried.add(browser)
        try:
            # Слот общих ворот: это обращение к ютубу, и считать их надо на всю
            # стадию, а не на клип (иначе четыре клипа по восемь запросов дают
            # тридцать два разом — улов пользователя 27.08). Загрузка отрезков
            # сюда НЕ входит: она долгая и держала бы слот, придерживая
            # метаданные соседних клипов (решение пользователя 27.08).
            with _YT_GATE.slot(stop_event):
                proc = subprocess.Popen(
                    _prepare_ytdlp_command(
                        cmd,
                        include_browser_cookies=bool(browser),
                        browser=browser or None,
                    ),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=_yt_env(),
                    **flags,
                )
                started = time.time()
                while True:
                    try:
                        stdout, stderr = proc.communicate(timeout=0.2)
                        break
                    except subprocess.TimeoutExpired:
                        pass      # ждём дальше: тайм-аут цикла считается ниже
                    if stop_event is not None and stop_event.is_set():
                        proc.kill()
                        proc.communicate()
                        return None
                    if time.time() - started > timeout or (
                        deadline_ts is not None and time.time() >= deadline_ts
                    ):
                        proc.kill()
                        proc.communicate()
                        return None
            if proc.returncode == 0:
                _cookie_plan_lock_in(browser)
                try:
                    return json.loads(stdout or "{}")
                except json.JSONDecodeError:   # ответ yt-dlp не JSON — попытка не засчитана
                    return None
            tail = (stderr or "").strip()[-500:]
            logging.warning(
                "yt-dlp JSON query failed (попытка %s, куки: %s): %s",
                attempt_no, browser or "без куков", tail,
            )
            if RATE_LIMIT_RE.search(tail):
                # Ограничение частоты — не про куки: следующий браузер отправит
                # ещё один запрос и сделает только хуже. Ждём назначенную паузу
                # и пробуем ТОТ ЖЕ вариант; не вышло со второго — уходим.
                _note_rate_limit("метаданные", tail)
                if rate_retries >= 1:
                    return None
                rate_retries += 1
                _throttle_wait(stop_event, deadline_ts)
                continue
            if BOT_CHECK_RE.search(tail):
                bot_checks += 1
            if browser and COOKIE_DB_ERROR_RE.search(tail):
                # Браузер держит базу открытой — на этом прогоне он бесполезен.
                _drop_cookie_browser(browser)
            elif not widened:
                extra = _cookie_followups(tail, browser, tried, attempts[index + 1:])
                if extra:
                    attempts = [*attempts, *extra]
                    widened = True
        except Exception as exc:
            logging.warning(
                "yt-dlp JSON query exception (куки: %s): %s",
                browser or "без куков", exc,
            )
        # Инкремент здесь, а не в `for`: повтор после паузы должен достаться
        # ТОМУ ЖЕ варианту куков, поэтому он уходит на `continue` мимо строки.
        index += 1
    if bot_checks and bot_checks >= len(tried):
        # Бот-проверку получили ВСЕ варианты куков. Причина бывает двух родов:
        # ни в одном профиле нет авторизации (тогда лечится входом в аккаунт в
        # том браузере, откуда берутся куки) — или ютуб придирается к темпу.
        # Что именно, отсюда не видно, но тормозить полезно в обоих случаях:
        # продолжать долбить в прежнем темпе точно хуже.
        #
        # До 30.08 это не считалось торможением вовсе: потолок запросов двигал
        # только 429, а на бот-проверку стадия отвечала новой очередью —
        # десять обращений за шесть секунд (лог пользователя 30.08). Теперь
        # ворота сужаются, как при ограничении частоты: 8 → 4 → 2 → 1.
        _bot_check_advice(tried)
        _note_rate_limit("бот-проверка", "все варианты куков отклонены")
        _throttle_wait(stop_event, deadline_ts)
    return None


def _looks_like_shorts(item: dict[str, Any]) -> bool:
    """Shorts узнаём по адресу и названию: у поиска нет фильтра ориентации."""
    url = " ".join(
        str(item.get(key) or "") for key in ("url", "webpage_url", "original_url")
    ).lower()
    title = str(item.get("title") or "").lower()
    if "/shorts/" in url or "#shorts" in title or "#short " in title + " ":
        return True
    width, height = item.get("width"), item.get("height")
    if isinstance(width, (int, float)) and isinstance(height, (int, float)) and width and height:
        return height > width
    return False


def _orientation_mismatch(detail: dict[str, Any], target_w: int, target_h: int) -> str:
    """Причина отказа, если ориентация ролика не совпадает с целевой, иначе ''.

    Фильтра ориентации у поиска YouTube нет; ширину и высоту знает yt-dlp в
    деталях ролика. Проект бывает и 9:16 — тогда отсекаем горизонтальное.
    Выключается `YOUTUBE_MATCH_ORIENTATION=false`.
    """
    if not getattr(config, "YOUTUBE_MATCH_ORIENTATION", True):
        return ""
    width, height = detail.get("width"), detail.get("height")
    ratio = detail.get("aspect_ratio")
    try:
        if isinstance(width, (int, float)) and isinstance(height, (int, float)) and width and height:
            ratio = float(width) / float(height)
        elif ratio is not None:
            ratio = float(ratio)
        else:
            return ""
    except (TypeError, ValueError):   # нечисловая длительность ролика — кандидат без окна
        return ""
    want_portrait = int(target_h) > int(target_w)
    if want_portrait and ratio > 1.0:
        return f"landscape {ratio:.2f} for a portrait project"
    if not want_portrait and ratio < 1.0:
        return f"portrait {ratio:.2f} for a landscape project"
    return ""


def _discover_youtube(
    query: str,
    limit: int,
    stop_event: Optional[threading.Event],
    deadline_ts: Optional[float] = None,
) -> list[dict[str, Any]]:
    payload = _run_json(
        [
            _yt_dlp_path(),
            f"ytsearch{max(1, int(limit))}:{query}",
            "--flat-playlist",
            "--dump-single-json",
            "--skip-download",
            "--no-warnings",
        ],
        int(getattr(config, "YOUTUBE_SEARCH_TIMEOUT", 120) or 120),
        stop_event,
        deadline_ts,
    )
    entries = payload.get("entries", []) if isinstance(payload, dict) else []
    result = []
    seen: set[str] = set()
    for item in entries:
        video_id = str(item.get("id") or "").strip()
        if not video_id or video_id in seen:
            continue
        # Shorts — вертикальные «говорящие головы», в документальную нарезку не
        # годятся (улов живого теста 25.08: половина кандидатов — Shorts).
        if _looks_like_shorts(item):
            continue
        seen.add(video_id)
        result.append({
            "video_id": video_id,
            "title": str(item.get("title") or ""),
            "url": str(
                item.get("webpage_url")
                or f"https://www.youtube.com/watch?v={video_id}"
            ),
            "source": "youtube",
            "license": "unknown",
            "license_verified": False,
        })
    return result


def _iso8601_duration_seconds(value: str) -> int:
    match = re.fullmatch(
        r"P(?:\d+D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?",
        str(value or "").strip(),
    )
    if not match:
        return 0
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def _discover_youtube_data_api(
    query: str,
    limit: int,
    stop_event: Optional[threading.Event],
    deadline_ts: Optional[float] = None,
) -> list[dict[str, Any]]:
    key = str(getattr(config, "YOUTUBE_DATA_API_KEY", "") or "").strip()
    if not key:
        raise YouTubeDiscoveryError("YouTube Data API key is not configured")
    if stop_event is not None and stop_event.is_set():
        return []
    timeout = min(
        int(getattr(config, "YOUTUBE_SEARCH_TIMEOUT", 120) or 120),
        max(1, int(deadline_ts - time.time())) if deadline_ts else 120,
    )
    policy = str(
        getattr(config, "YOUTUBE_LICENSE_POLICY", "creative_common")
        or "creative_common"
    ).lower()
    try:
        search = requests.get(
            f"{YOUTUBE_DATA_API_URL}/search",
            params={
                "key": key,
                "part": "snippet",
                "q": query,
                "type": "video",
                # videoEmbeddable убран (25.08): мы скачиваем через yt-dlp, а не
                # встраиваем плеер; фильтр лишь выкидывал каналы с отключённым
                # встраиванием — часто как раз вещателей и архивы.
                "videoLicense": (
                    "creativeCommon" if policy == "creative_common" else "any"
                ),
                "maxResults": 50,
            },
            timeout=timeout,
        )
        search.raise_for_status()
        items = list((search.json() or {}).get("items") or [])
        video_ids = [
            str((item.get("id") or {}).get("videoId") or "").strip()
            for item in items
        ]
        video_ids = list(dict.fromkeys(item for item in video_ids if item))
        if not video_ids:
            return []
        details = requests.get(
            f"{YOUTUBE_DATA_API_URL}/videos",
            params={
                "key": key,
                "part": "snippet,contentDetails,status,statistics",
                "id": ",".join(video_ids[:50]),
                "maxResults": 50,
            },
            timeout=timeout,
        )
        details.raise_for_status()
    except requests.RequestException as exc:
        raise YouTubeDiscoveryError(
            f"YouTube Data API request failed: {exc}"
        ) from exc

    rights_policy = str(
        getattr(config, "YOUTUBE_RIGHTS_TEXT_POLICY", "exclude")
        or "exclude"
    ).lower()
    result = []
    # Журнал выдачи: сколько вернул YouTube и что отброшено фильтрами —
    # чтобы по пулу было видно, почему в кандидатах два ролика из пятидесяти.
    discovery_log: dict[str, Any] = {
        "api_results": len(items), "kept": 0, "dropped": {}, "dropped_titles": [],
    }
    for item in (details.json() or {}).get("items") or []:
        status = item.get("status") or {}
        snippet = item.get("snippet") or {}
        license_name = str(status.get("license") or "").strip()
        title = str(snippet.get("title") or "")

        def _drop(reason: str) -> None:
            discovery_log["dropped"][reason] = discovery_log["dropped"].get(reason, 0) + 1
            if len(discovery_log["dropped_titles"]) < 20:
                discovery_log["dropped_titles"].append({"title": title[:80], "reason": reason})

        if policy == "creative_common" and license_name != "creativeCommon":
            _drop("license")
            continue
        content = item.get("contentDetails") or {}
        stats = item.get("statistics") or {}
        description = str(snippet.get("description") or "")
        rights_flags = _rights_text_flags(description)
        if rights_policy == "exclude" and rights_flags:
            _drop("rights_text:" + ",".join(rights_flags))
            continue
        if _looks_like_shorts({"title": title}):
            _drop("shorts")
            continue
        # Качество ролика Data API отдаёт в том же ответе, даром: «sd» означает
        # ниже 720p. Проверено 27.08: sd-ролики оказались 384x288 и 640x480 —
        # ради таких незачем тратить запрос yt-dlp и трафик. Галка «Брать SD» в
        # GUI пускает их обратно, когда материала мало.
        # Заметка: «hd» НЕ означает горизонтальный — вертикальные Shorts бывают
        # и в hd, ориентацию по-прежнему знает только yt-dlp.
        definition = str(content.get("definition") or "").strip().lower()
        if definition == "sd" and not getattr(config, "YOUTUBE_ALLOW_SD", False):
            _drop("sd_quality")
            continue
        video_id = str(item.get("id") or "").strip()
        if not video_id:
            continue
        discovery_log["kept"] += 1
        result.append({
            "video_id": video_id,
            "title": str(snippet.get("title") or ""),
            "description": description,
            "channel_title": str(snippet.get("channelTitle") or ""),
            "published_at": str(snippet.get("publishedAt") or ""),
            "duration_sec": _iso8601_duration_seconds(
                content.get("duration") or ""
            ),
            "definition": definition or "unknown",
            "view_count": int(stats.get("viewCount") or 0),
            "license": license_name or "unknown",
            "license_verified": True,
            "license_metadata_verified": True,
            "rights_text_flags": rights_flags,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "source": "youtube",
        })
        if len(result) >= max(1, int(limit)):
            break
    _DISCOVERY_LOGS[query] = discovery_log
    with _API_UNITS_LOCK:
        _API_UNITS["search"] += 1
        _API_UNITS["videos"] += 1
        spent = _API_UNITS["search"] * 100 + _API_UNITS["videos"]
    dropped = discovery_log["dropped"]
    logging.info(
        "  [ютуб/поиск] «%s»: вернулось %s, оставлено %s%s | квота Data API: "
        "%s из 10000 за прогон",
        query[:60], discovery_log["api_results"], discovery_log["kept"],
        (" | отброшено — " + ", ".join(
            f"{reason}: {count}" for reason, count in sorted(dropped.items()))
         ) if dropped else "",
        spent,
    )
    return result


# Журнал выдачи по запросу (что вернул YouTube, что отброшено и почему) —
# читается при записи пул-файла, чтобы отбор был виден целиком.
_DISCOVERY_LOGS: dict[str, dict[str, Any]] = {}
# Расход дневной квоты Data API. У ключа 10 000 единиц в сутки, и стоят методы
# по-разному: search.list — 100 единиц, videos.list — 1. То есть квоту жжёт
# ИМЕННО поиск: три запроса на клип по сотне — и полтора десятка клипов съедают
# половину суток. До 27.08 расход не считался и в логе не появлялся вовсе.
_API_UNITS = {"search": 0, "videos": 0}
_API_UNITS_LOCK = threading.Lock()


def api_units_spent() -> dict[str, int]:
    with _API_UNITS_LOCK:
        search, videos = _API_UNITS["search"], _API_UNITS["videos"]
    return {
        "search_calls": search,
        "videos_calls": videos,
        "units": search * 100 + videos,
    }


def _discover_candidates(
    query: str,
    limit: int,
    stop_event: Optional[threading.Event],
    deadline_ts: Optional[float] = None,
) -> list[dict[str, Any]]:
    backend = str(
        getattr(config, "YOUTUBE_SEARCH_BACKEND", "auto") or "auto"
    ).lower()
    has_api_key = bool(str(getattr(config, "YOUTUBE_DATA_API_KEY", "") or "").strip())
    if backend == "data_api" or (backend == "auto" and has_api_key):
        return _discover_youtube_data_api(query, limit, stop_event, deadline_ts)
    if (
        str(getattr(config, "YOUTUBE_LICENSE_POLICY", "creative_common") or "")
        .lower()
        == "creative_common"
    ):
        raise YouTubeDiscoveryError(
            "Creative Commons policy requires YouTube Data API discovery"
        )
    return _discover_youtube(query, limit, stop_event, deadline_ts)


def _clip_search_queries(clip: dict[str, Any], fallback_query: str) -> list[str]:
    # youtube_queries впереди: этап обслуживает только ютуб-клипы, и когда
    # планировщик написал запросы под движущийся кадр, фотонабор их не глушит.
    values = (
        clip.get("youtube_queries")
        or clip.get("search_queries")
        or clip.get("queries")
        or []
    )
    if isinstance(values, str):
        values = [values]
    queries = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in queries:
            queries.append(text)
    fallback = str(fallback_query or "").strip()
    if fallback and fallback not in queries:
        queries.append(fallback)
    return queries or ["documentary footage"]


def _query_provider_model() -> tuple[str, str]:
    backend = str(
        getattr(config, "DIRECTOR_BACKEND", "") or ""
    ).strip().lower()
    provider = str(
        getattr(config, "GPT_PROVIDER", "") if backend == "gpt" else backend
    ).strip().lower()
    if provider not in CHAT_PROVIDERS:
        provider = str(
            getattr(config, "GPT_PROVIDER", "") or ""
        ).strip().lower()
    if provider not in CHAT_PROVIDERS:
        return "", ""
    model_names = {
        "deepseek": "DEEPSEEK_MODEL",
        "openrouter": "OPENROUTER_MODEL",
        "meta_muse": "META_MUSE_MODEL",
        "claude_api": "CLAUDE_API_MODEL",
        "codex_reseller": "CODEX_RESELLER_MODEL",
        "gpt_nexus": "GPT_NEXUS_MODEL",
    }
    model = str(
        getattr(config, model_names[provider], "") or ""
    ).strip()
    return provider, model


def _normalize_search_queries(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [values]
    result = []
    for value in values or []:
        query = re.sub(r"\s+", " ", str(value or "")).strip(" \t\r\n\"'")
        if not query:
            continue
        words = query.split()
        # Потолок 7 слов: поиск YouTube на длинную фразу отдаёт 0-2 ролика
        # (живая проба 25.08: 10-словный запрос → 2 результата, оба мусор).
        query = " ".join(words[:7])[:120].strip()
        if query and query.casefold() not in {
            item.casefold() for item in result
        }:
            result.append(query)
        if len(result) >= 4:
            break
    return result


# Слова о СЪЁМКЕ и КАРТИНКЕ, а не о предмете сцены. Описание пишется для
# генератора кадра и кончается перечнем требований к изображению: «no text, no
# logos, no watermarks, no collage, no interface overlays, single full-frame
# continuous camera view edge-to-edge». Прежний фолбэк брал ПОСЛЕДНИЕ десять
# слов — то есть ровно этот хвост, — и клип 1 прогона 27.08 ушёл искать по
# запросу «collage no interface overlays single full-frame continuous».
#
# Список нарочно про технику кадра, а не про темы: ролики делаются на тысячи
# разных сюжетов, и предметные слова здесь фильтровать нельзя.
_TECHNICAL_WORDS = frozenset("""
no not none text texts lettering logo logos watermark watermarks caption
captions subtitle subtitles title titles collage interface interfaces overlay
overlays border borders split grid frame frames full-frame edge-to-edge
continuous single double shot shots wide medium closeup close-up macro angle
angles view views viewpoint camera cameras lens lenses focal focus composition
uncluttered cluttered foreground background midground lighting light lights
lit daylight backlit shadow shadows highlight grading grade graded colour
colours color colors palette tone tones toned saturation desaturated contrast
grain bokeh depth field cinematic photoreal photorealistic realism stylized
styled style render rendered rendering resolution 4k 8k hd uhd aspect ratio
atmosphere mood calm purposeful soft gentle subtle natural neutral muted
visible clear crisp sharp blurred blurry dramatic timeless seamless
""".split())


def _scene_subject_words(text: str) -> list[str]:
    """Слова о ПРЕДМЕТЕ сцены: без технических требований к кадру."""
    # Хвост с запретами отрезаем целиком: он всегда начинается с «no <что-то>»
    # и до конца описания идёт только про картинку.
    cut = re.split(r"(?i)(?:^|[.,;]\s*)no\s+(?:text|logos?|watermarks?|captions?"
                   r"|collage|interface|overlays?|stylized|dramatic)\b",
                   str(text or ""), maxsplit=1)[0]
    compact = re.sub(r"[^\w\s-]+", " ", cut, flags=re.UNICODE)
    words = [w for w in re.sub(r"\s+", " ", compact).strip().split()
             if w.lower() not in _TECHNICAL_WORDS]
    return words


def _fallback_search_queries(clip: dict[str, Any], scene_text: str) -> list[str]:
    explicit = _normalize_search_queries(
        clip.get("youtube_queries")
        or clip.get("search_queries")
        or clip.get("queries")
        or []
    )
    if explicit:
        return explicit
    words = _scene_subject_words(scene_text)
    if not words:
        return ["documentary footage"]
    # Только НАЧАЛО описания: там место и предмет. Хвост брать нельзя — даже
    # после чистки в конце остаются слова о свете и атмосфере, а не о том, что
    # в кадре.
    queries = [" ".join(words[:7])]
    if len(words) > 12:
        queries.append(" ".join(words[7:14]))
    return _normalize_search_queries(queries) or ["documentary footage"]


def _plan_search_queries(
    project: Path,
    clip: dict[str, Any],
    artifact_dir: Path,
    *,
    context: Optional[dict[str, Any]] = None,
) -> list[str]:
    scene_text = _clip_text(project, clip)
    context = context or _load_visual_context(project, clip)
    seed_queries = _normalize_search_queries(
        clip.get("youtube_queries")
        or clip.get("search_queries")
        or clip.get("queries")
        or []
    )
    # Оператор любой, кто умеет текст: chat-провайдер ИЛИ CLI-агент (Codex,
    # Claude Code). Раньше здесь принимались только chat-провайдеры, и при
    # DIRECTOR_BACKEND=gpt пара выходила пустой — планировщик не вызывался
    # вовсе, а описание сцены резалось на куски по семь слов (улов 25.08).
    from units.operator_hub import ask_text, resolve_text_operator

    provider, model = resolve_text_operator("youtube")
    artifact: dict[str, Any] = {
        "method": "llm_query_planner",
        "provider": provider,
        "model": model,
        "scene_text": scene_text,
        "seed_queries": seed_queries,
        "context": context,
        "queries": [],
    }
    queries: list[str] = []
    clip_id = clip.get("id")
    logging.info("   [ютуб/запросы] клип %s: оператор %s/%s, сцена %s символов, подсказок %s",
                 clip_id, provider or "не настроен", model or "—",
                 len(scene_text or ""), len(seed_queries))
    if not provider:
        logging.warning(
            "   [ютуб/запросы] клип %s: оператор не настроен — ищу по тексту сцены", clip_id)
    if provider and scene_text:
        task = (
            "Create 3 concise YouTube search queries for real footage that "
            "visually illustrates the scene below. Write them the way people "
            "search YouTube: 3 to 6 words each — the concrete subject plus "
            "place, era or context; optionally one word about the kind of "
            "footage that FITS the scene (footage, documentary, drone, 4K, "
            "wildlife, archival, newsreel) — a modern subject gets a modern "
            "query, never force 'archival' on it. A long descriptive sentence "
            "finds nothing on YouTube: the search matches only a few metadata "
            "words. Queries must be search phrases, not narration, and should "
            "name the concrete subject, place, era, event, action, or filming "
            "context. Prefer English phrases because YouTube metadata is often "
            "English. Do not invent "
            "URLs, video IDs, timestamps, durations, or licensing claims. "
            "Use the supplied story context to disambiguate who, where, when, "
            "and which event the scene refers to. Preserve concrete names and "
            "locations from the context or seed queries. Do not replace a "
            "specific subject with a generic phrase such as 'a singer'. "
            "The SCENE below is written for an image GENERATOR: it stages a "
            "frame — invented props, a person placed just so, lighting, lens "
            "and mood. Read it for WHAT the frame is about and leave the "
            "staging out of the query: no camera work, no light or weather, no "
            "mood words, no exact postures, no negative demands. Leave out the "
            "phase of the action as well — footage of the same subject a moment "
            "earlier or later is still the right footage. "
            "Return strict JSON only: {\"queries\":[\"...\",\"...\",\"...\"]}.\n\n"
            f"SCENE:\n{scene_text[:2000]}\n\n"
            f"SEED QUERIES:\n{json.dumps(seed_queries, ensure_ascii=False)}\n\n"
            f"STORY CONTEXT:\n{json.dumps(context, ensure_ascii=False)[:12000]}"
        )
        started = time.time()
        try:
            raw = ask_text(
                provider, model, task,
                work_dir=artifact_dir, phase="youtube_queries",
                schema_file="stage_queries.json", max_tokens=300, timeout=180,
            )
            parsed = _parse_json_object(raw)
            queries = _normalize_search_queries(parsed.get("queries") or [])
            artifact["raw"] = raw
            artifact["seconds"] = round(time.time() - started, 1)
            logging.info("   [ютуб/запросы] клип %s: написано %s за %.0f с — %s",
                         clip_id, len(queries), time.time() - started,
                         " | ".join(queries[:3]))
        except Exception as exc:
            artifact["error"] = str(exc)
            logging.warning(
                "   [ютуб/запросы] клип %s: оператор %s/%s не ответил: %s",
                clip_id, provider, model or "—", str(exc)[:160])
    if not queries:
        queries = _fallback_search_queries(clip, scene_text)
        artifact["method"] = "deterministic_query_fallback"
        # Молчать здесь нельзя: дальше стадия ищет по обрубкам описания, и в
        # логе прогона это выглядит как нормальная работа (улов 25.08).
        logging.warning(
            "   [ютуб/запросы] клип %s: запрос НЕ написан — ищу по нарезке текста сцены: %s",
            clip_id, "; ".join(queries[:2]))
    # Director/Auto Visual Story queries are concrete hints and must survive
    # the per-stage planner even when the short scene text is ambiguous.
    queries = _normalize_search_queries([*seed_queries, *queries])
    artifact["queries"] = queries
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "query_planner.json").write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # Готовый запрос кладём В ПЛАН, а не только в артефакт стадии: когда сцена
    # не закрылась и уходит архивам, те берут запрос из плана. Раньше он там не
    # появлялся, и запасной EPF искал по ЦЕЛОМУ описанию сцены — поисковик на
    # такую фразу отдавал Дженис Джоплин и логотипы телеканалов вместо польских
    # артистов (улов прогона 25.08; пользователь: «юзать уже готовый запрос»).
    if queries:
        try:
            from units.source_ledger import apply_clip_source_delta

            apply_clip_source_delta(
                project / "montage_plan.json",
                {int(clip_id): {"youtube_queries": queries}},
                log=lambda message: logging.debug("[ютуб/запросы] %s", message),
            )
        except Exception as exc:                     # noqa: BLE001
            logging.warning("   [ютуб/запросы] клип %s: запросы не записаны в план (%s) — "
                            "архивам достанется описание сцены", clip_id, exc)
    return queries


def _discover_candidates_multi(
    queries: list[str],
    limit: int,
    stop_event: Optional[threading.Event],
    deadline_ts: Optional[float] = None,
) -> list[dict[str, Any]]:
    result = []
    seen: set[str] = set()

    def _pass(batch: list[str]) -> bool:
        """Один проход по запросам; True — предел набран."""
        for query in batch:
            if stop_event is not None and stop_event.is_set():
                return True
            if deadline_ts is not None and time.time() >= deadline_ts:
                return True
            for item in _discover_candidates(query, limit, stop_event, deadline_ts):
                video_id = str(item.get("video_id") or "").strip()
                if not video_id or video_id in seen:
                    continue
                seen.add(video_id)
                item = dict(item)
                item["discovery_queries"] = [query]
                result.append(item)
                if len(result) >= max(1, int(limit)):
                    return True
        return False

    if _pass(list(queries)):
        return result
    # Бедная выдача при политике Creative Commons — второй проход теми же
    # запросами без ограничения лицензии (решение пользователя 25.08; при
    # YOUTUBE_LICENSE_POLICY=any первый проход уже без ограничения, второго нет).
    minimum = max(1, int(getattr(config, "YOUTUBE_MIN_DISCOVERY", 6) or 6))
    policy = str(getattr(config, "YOUTUBE_LICENSE_POLICY", "creative_common") or "").lower()
    if policy == "creative_common" and len(result) < min(minimum, max(1, int(limit))):
        logging.info(
            "  [YouTube] бедная выдача под Creative Commons (%d роликов) — "
            "повтор тех же запросов без ограничения лицензии", len(result),
        )
        old_policy = config.YOUTUBE_LICENSE_POLICY
        config.YOUTUBE_LICENSE_POLICY = "any"
        try:
            _pass(list(queries))
        finally:
            config.YOUTUBE_LICENSE_POLICY = old_policy
        for item in result:
            if str(item.get("license") or "").lower() != "creativecommon":
                item["license_note"] = "found on the no-license-filter pass"
    return result


def _timestamp_seconds(value: str) -> Optional[float]:
    parts = str(value or "").strip().split(":")
    if len(parts) not in (2, 3) or not all(part.isdigit() for part in parts):
        return None
    values = [int(part) for part in parts]
    if len(values) == 2:
        return float(values[0] * 60 + values[1])
    return float(values[0] * 3600 + values[1] * 60 + values[2])


def _description_timestamps(description: str) -> list[float]:
    values = []
    for match in re.finditer(
        r"(?<!\d)(\d{1,2}:\d{2}(?::\d{2})?)(?!\d)",
        description or "",
    ):
        timestamp = _timestamp_seconds(match.group(1))
        if timestamp is not None and timestamp not in values:
            values.append(timestamp)
    return values


def _load_video_locator_detail(
    candidate: dict[str, Any],
    stop_event: Optional[threading.Event],
    deadline_ts: Optional[float] = None,
) -> dict[str, Any]:
    payload = _run_json(
        [
            _yt_dlp_path(),
            str(candidate.get("url") or ""),
            "--dump-single-json",
            "--skip-download",
            "--no-warnings",
        ],
        int(getattr(config, "YOUTUBE_SEARCH_TIMEOUT", 120) or 120),
        stop_event,
        deadline_ts,
    )
    if not isinstance(payload, dict):
        return dict(candidate)
    merged = dict(candidate)
    # width/height/aspect_ratio — единственное место, где ориентация ролика
    # известна (у поиска фильтра нет); по ним отсекаем Shorts до окон.
    # `formats` нужен ради раскадровки (sb0/sb1/sb2): из неё берутся кадры для
    # контактного листа вижна, без скачивания самого видео. Без этого ключа
    # раскадровка не находится и стадия молча откатывается на загрузку превью.
    for key in ("duration", "description", "chapters", "width", "height",
                "aspect_ratio", "formats"):
        if payload.get(key) is not None:
            merged[key] = payload[key]
    if payload.get("duration") is not None:
        merged["duration_sec"] = float(payload.get("duration") or 0)
    return merged


# Что именно добавляет к ролику yt-dlp: ориентацию, хронометраж, описание, главы
# и раскадровку. Кэшируем ТОЛЬКО это — остальные поля у кандидата свои на каждом
# клипе (каким запросом он найден, например).
_DETAIL_KEYS = ("duration", "duration_sec", "description", "chapters",
                "width", "height", "aspect_ratio", "formats")
# Один и тот же ролик попадает в пулы разных клипов, а опрашивался заново каждый
# раз: на прогоне 27.08 это давало десятки лишних запросов по 10-15 секунд.
_DETAIL_CACHE: dict[str, dict[str, Any]] = {}
_DETAIL_LOCK = threading.Lock()


def _cookie_plan_locked() -> bool:
    with _COOKIE_LOCK:
        return bool(_COOKIE_PLAN.get("locked"))


def _cached_video_detail(
    candidate: dict[str, Any],
    stop_event: Optional[threading.Event],
    deadline_ts: Optional[float] = None,
) -> dict[str, Any]:
    """Детали ролика — один запрос на ролик за весь прогон."""
    video_id = str(candidate.get("video_id") or "").strip()
    if video_id:
        with _DETAIL_LOCK:
            cached = _DETAIL_CACHE.get(video_id)
        if cached is not None:
            return {**candidate, **cached}
    detail = _load_video_locator_detail(candidate, stop_event, deadline_ts)
    if video_id and isinstance(detail, dict):
        with _DETAIL_LOCK:
            _DETAIL_CACHE[video_id] = {
                key: detail[key]
                for key in _DETAIL_KEYS
                if detail.get(key) is not None
            }
    return detail


def _meta_workers(count: int) -> int:
    """Сколько запросов метаданных вести разом, с учётом торможения.

    Каждое пойманное ограничение частоты делит потолок пополам: 8 → 4 → 2 → 1.
    Вверх в этом прогоне не поднимаемся — запаса до предела мы не знаем.
    """
    try:
        value = int(getattr(config, "YOUTUBE_META_PARALLEL", 8) or 8)
    except (TypeError, ValueError):
        value = 8
    steps = rate_limited()
    if steps:
        value = max(1, value >> min(steps, 16))
    return max(1, min(value, max(1, int(count))))


def _collect_details(
    candidates: list[dict[str, Any]],
    stop_event: Optional[threading.Event],
    deadline_ts: Optional[float] = None,
) -> dict[str, dict[str, Any]]:
    """Детали всех кандидатов клипа: video_id → ролик с данными yt-dlp.

    Запросы к разным роликам друг от друга не зависят и почти целиком состоят из
    ожидания сети, а шли строго по очереди: восемь роликов по 10-15 секунд —
    полторы-две минуты на клип, две трети времени всей стадии (замер 27.08).
    Здесь они идут пулом.

    Отказаться от этих запросов и взять всё из Data API нельзя: он отдаёт
    ПРЕВЬЮ, а превью у YouTube приведены к 16:9 для любого ролика. Проба 27.08:
    три ролика с превью 1280x720 на деле оказались 1080x1920 и 720x1280 — по
    таким данным вертикальные Shorts прошли бы фильтр ориентации насквозь.
    Раскадровки и глав в Data API тоже нет.

    Первый запрос идёт в одиночку, пока рабочий вариант куков не найден: иначе
    потоки разом начали бы перебор браузеров и переписывали «победителя» друг у
    друга (см. `_cookie_plan_lock_in`).
    """
    result: dict[str, dict[str, Any]] = {}
    queue = [item for item in candidates if isinstance(item, dict)]
    if not queue:
        return result

    def _one(candidate: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        return (
            str(candidate.get("video_id") or ""),
            _cached_video_detail(candidate, stop_event, deadline_ts),
        )

    if not _cookie_plan_locked():
        video_id, detail = _one(queue.pop(0))
        result[video_id] = detail
    if not queue:
        return result
    workers = _meta_workers(len(queue))
    if workers <= 1:
        for candidate in queue:
            video_id, detail = _one(candidate)
            result[video_id] = detail
        return result
    from concurrent.futures import ThreadPoolExecutor

    started = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for video_id, detail in pool.map(_one, queue):
            result[video_id] = detail
    logging.info("  [YouTube] детали %s ролик(ов) в %s поток(ов) — %.1f с",
                 len(queue), workers, time.time() - started)
    return result


def _metadata_rank_fallback(
    candidates: list[dict[str, Any]],
    queries: list[str],
    scene_text: str,
) -> list[dict[str, Any]]:
    """Запасной порядок — по совпадению слов запроса с названием и описанием."""
    wanted = _tokens(" ".join(queries or []) + " " + (scene_text or ""))
    if not wanted:
        return list(candidates)

    def _score(item: dict[str, Any]) -> tuple[float, int]:
        title = _tokens(str(item.get("title") or ""))
        body = _tokens(str(item.get("description") or "")[:1200])
        # Название весит больше описания: в описании часто висят ссылки на
        # соцсети и списки оборудования, дающие случайные совпадения.
        return (
            len(wanted & title) * 2.0 + len(wanted & body) * 0.5,
            int(item.get("view_count") or 0),
        )

    return sorted(candidates, key=_score, reverse=True)


def _rank_provider_model() -> tuple[str, str]:
    """Оператор ранжирования пула — своя настройка, обычно ЛЁГКАЯ модель.

    Этап простой: прочитать десяток названий с описаниями и расставить порядок.
    Грузить на него тяжёлую модель режиссуры незачем — она долго думает и стоит
    дорого, а решение здесь очевидное (решение пользователя 27.08). Поле пустое
    или «auto» → берём общего оператора стадии, как было до появления настройки.
    """
    from units.operator_hub import resolve_text_operator

    provider = str(
        getattr(config, "YOUTUBE_RANK_PROVIDER", "") or ""
    ).strip().lower()
    if provider in ("", "auto"):
        return resolve_text_operator("youtube")
    model = str(getattr(config, "YOUTUBE_RANK_MODEL", "") or "").strip()
    if not model:
        from units.operator_hub import _model_for

        model = _model_for(provider)
    return provider, model


def _rank_pool_by_metadata(
    clip: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    queries: list[str],
    scene_text: str,
    artifact_dir: Path,
) -> list[dict[str, Any]]:
    """Порядок опроса роликов — по названию и описанию из выдачи Data API.

    Поиск отдаёт 12 кандидатов, а вижну показывается пул из 4-8 роликов, и до
    27.08 стадия опрашивала yt-dlp ВСЕ 12: от трети до половины запросов по
    10-15 секунд уходило на ролики, которые в лист всё равно не попадут
    (предложение пользователя 27.08).

    Название и описание Data API отдаёт даром — вместе с выдачей поиска. Их
    достаточно, чтобы отличить «как делают пармезан на ферме» от «десять фактов
    о сыре» ещё до всякого запроса. Ориентацию по ним не узнать (превью у
    YouTube всегда 16:9), поэтому это именно ПОРЯДОК, а не отсев: кандидаты,
    отпавшие по ориентации, добираются из хвоста этого же списка.
    """
    if len(candidates) <= 1:
        return list(candidates)
    fallback = _metadata_rank_fallback(candidates, queries, scene_text)
    clip_id = clip.get("id")
    started = time.time()
    try:
        from units.operator_hub import ask_text

        provider, model = _rank_provider_model()
        if not provider:
            logging.warning("   [ютуб/отбор] клип %s: оператор ранжирования не настроен — "
                            "иду по совпадению слов запроса", clip_id)
            return fallback
        cards = [
            {
                "id": f"V{index + 1}",
                "title": str(item.get("title") or "")[:200],
                "channel": str(item.get("channel_title") or "")[:80],
                "duration_sec": int(item.get("duration_sec") or 0),
                "description": str(item.get("description") or "")[:600],
            }
            for index, item in enumerate(candidates)
        ]
        task = (
            "Order these YouTube videos by how likely each one CONTAINS footage "
            "for the scene below. Judge only by title, channel and description. "
            "Rank every id exactly once, best first.\n"
            "What makes a video likely: it shows the subject being filmed — a "
            "process, a place, a craft, an event, an animal, machinery. What "
            "makes it unlikely: a talking head, a list of facts, a reaction or "
            "review, a compilation of pictures, a lecture with slides, a recipe "
            "read to camera. A long video from a channel that films the real "
            "thing beats a short clever one about it.\n"
            "The SCENE is written for an image generator: it stages a frame. "
            "Read it for WHAT is in the frame, ignore the staging.\n"
            "Return strict JSON only: {\"ranked_ids\":[\"V1\",\"V2\"]}.\n\n"
            f"SCENE:\n{(scene_text or '')[:1200]}\n\n"
            f"SEARCH QUERIES:\n{json.dumps(queries or [], ensure_ascii=False)}\n\n"
            f"VIDEOS:\n{json.dumps(cards, ensure_ascii=False)}"
        )
        raw = ask_text(
            provider, model, task,
            work_dir=artifact_dir, phase="youtube_pool_rank",
            schema_file="youtube_pool_rank.json", max_tokens=400, timeout=180,
        )
        parsed = _parse_json_object(raw)
        by_id = {f"V{index + 1}": item for index, item in enumerate(candidates)}
        ordered: list[dict[str, Any]] = []
        for value in parsed.get("ranked_ids") or []:
            item = by_id.pop(str(value).strip(), None)
            if item is not None:
                ordered.append(item)
        if not ordered:
            raise ValueError("оператор не назвал ни одного известного ролика")
        # Кого оператор забыл — в хвост, в исходном порядке выдачи.
        ordered.extend(item for item in candidates if item in by_id.values())
        logging.info("   [ютуб/отбор] клип %s: %s ролик(ов) упорядочены оператором за %.0f с, "
                     "первый — %s",
                     clip_id, len(ordered), time.time() - started,
                     str(ordered[0].get("title") or "")[:60])
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "pool_rank.json").write_text(
            json.dumps({
                "provider": provider, "model": model, "cards": cards,
                "ranked": [str(item.get("video_id")) for item in ordered],
                "raw": raw,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return ordered
    except Exception as exc:                             # noqa: BLE001
        logging.warning("   [ютуб/отбор] клип %s: оператор не упорядочил пул (%s) — "
                        "иду по совпадению слов запроса", clip_id, str(exc)[:160])
        return fallback


# Сколько окон брать с одного ролика — по числу роликов в листе. Пары подобраны
# так, чтобы лист вижна оставался в 24-35 кадрах: больше он читает хуже, меньше —
# теряется покрытие. Решение пользователя 27.08.
#
# Особняком стоит тройка: 3 ролика по 2 окна — это лист всего из шести кадров,
# сетка 2x3, самая быстрая из возможных. Покрытие вдвое беднее, зато вижну
# думать не над чем и запросов к ютубу втрое меньше (решение пользователя
# 27.08: «хочу ускорить выбор, уменьшив число кандидатов»).
_WINDOWS_BY_POOL = {3: 2, 4: 6, 5: 7, 6: 4, 7: 5, 8: 4}


def youtube_pool_size() -> int:
    """Сколько роликов показываем вижну. Только 3-8 — фиксированный выбор в GUI."""
    try:
        value = int(getattr(config, "YOUTUBE_POOL_TARGET", 6) or 6)
    except (TypeError, ValueError):
        value = 6
    return min(8, max(3, value))


def windows_per_video(pool: Optional[int] = None) -> int:
    """Окон с одного ролика.

    Пара «роликов x окон» задаётся в GUI напрямую (решение пользователя 27.08:
    в дропдауне видно сетку целиком, а не одно число). Старая карта
    `_WINDOWS_BY_POOL` осталась запасной — для .env, где число окон не задано.
    """
    try:
        explicit = int(getattr(config, "YOUTUBE_WINDOWS_PER_VIDEO", 0) or 0)
    except (TypeError, ValueError):    # нечисловое значение — идём по карте
        explicit = 0
    if explicit > 0:
        return max(1, min(12, explicit))
    size = youtube_pool_size() if pool is None else min(8, max(3, int(pool)))
    return _WINDOWS_BY_POOL.get(size, 5)


def _storyboard_format(detail: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Самая крупная раскадровка ролика (sb0/sb1/sb2), если она есть."""
    boards = [
        item for item in (detail.get("formats") or [])
        if str(item.get("format_id") or "").startswith("sb")
        and item.get("fragments") and item.get("rows") and item.get("columns")
    ]
    if not boards:
        return None
    return max(boards, key=lambda item: int(item.get("width") or 0))


# Лист раскадровки зависит ТОЛЬКО от ролика, а лежал в папке каждого клипа — и
# один ролик качался заново для каждой сцены. На прогоне 27.08: 49 уникальных
# роликов на 77 пар «клип x ролик», лишних 118 листов. Теперь папка общая на
# прогон, а замок не даёт двум клипам качать один лист разом и читать
# недокачанный файл.
_SHEET_LOCKS: dict[str, threading.Lock] = {}
_SHEET_LOCKS_GUARD = threading.Lock()


def _sheet_lock(key: str) -> threading.Lock:
    with _SHEET_LOCKS_GUARD:
        return _SHEET_LOCKS.setdefault(key, threading.Lock())


def _storyboard_frames(detail: dict[str, Any], moments: list[float],
                       work_dir: Path, video_id: str,
                       clip_id: Optional[int] = None) -> dict[float, Path]:
    """Кадры ролика на заданные моменты — БЕЗ скачивания видео.

    Ютуб держит для каждого ролика раскадровку: спрайты с кадрами по всему
    хронометражу, которые плеер показывает при перемотке. Один лёгкий запрос
    даёт десятки кадров, тогда как скачивание превью-кусков стоило 125 секунд на
    клип — две трети всего времени стадии (замер 26.08).

    Кадры мелкие (обычно 160x90), но проверка того же дня показала, что вижн
    читает их лучше, чем нарезку из видео: раскадровка составлена из чистых
    кадров, а нарезка попадает на смазанное движение. Из 32 ячеек нечитаемых
    не было ни одной, тогда как у превью — две.

    Качаем ТОЛЬКО те листы спрайтов, где лежат нужные моменты. Раньше
    скачивались все: у ролика на 14 минут это 7 листов, у часового — за
    тридцать, а нужно из них шесть кадров. Замер 27.08: раскадровка занимала
    56 секунд на клип — второе место по времени после вижна, при том что
    метаданные всей стадии укладывались в 15 секунд.

    Границы листов берём из `duration` каждого фрагмента: yt-dlp отдаёт их
    честно (проверено живьём — sb0 ролика на 858 с: 7 фрагментов по ~124 с),
    и только при их отсутствии делим хронометраж поровну.
    """
    board = _storyboard_format(detail)
    if not board:
        return {}
    rows = int(board.get("rows") or 0)
    cols = int(board.get("columns") or 0)
    fragments = list(board.get("fragments") or [])
    if rows <= 0 or cols <= 0 or not fragments:
        return {}
    total_duration = float(detail.get("duration") or 0)
    if total_duration <= 0:
        return {}

    spans: list[float] = []
    for fragment in fragments:
        try:
            spans.append(max(0.0, float(fragment.get("duration") or 0)))
        except (TypeError, ValueError):    # длительность листа не число
            spans.append(0.0)
    if sum(spans) <= 0:
        spans = [total_duration / len(fragments)] * len(fragments)
    offsets: list[float] = []
    acc = 0.0
    for span in spans:
        offsets.append(acc)
        acc += span

    def _sheet_for(moment: float) -> tuple[int, float, float]:
        """Лист спрайтов, смещение внутри него и его длительность.

        Номер ячейки здесь НЕ считаем: сколько кадров в листе на самом деле,
        видно только после скачивания — последний лист обрезан.
        """
        point = min(max(0.0, float(moment)), max(0.0, total_duration - 1e-3))
        index = len(fragments) - 1
        for position, start in enumerate(offsets):
            if point < start + spans[position]:
                index = position
                break
        return index, point - offsets[index], spans[index] or 1.0

    work_dir.mkdir(parents=True, exist_ok=True)
    tag = f"клип {clip_id}: " if clip_id is not None else ""
    sheets: dict[int, Image.Image] = {}
    out: dict[float, Path] = {}
    for moment in moments:
        index, local, span = _sheet_for(moment)
        ready = work_dir / f"{video_id}_{int(round(float(moment)))}.jpg"
        if ready.is_file() and ready.stat().st_size > 512:
            # Кадр уже нарезан для другой сцены — лист открывать незачем.
            out[float(moment)] = ready
            continue
        try:
            sheet = sheets.get(index)
            if sheet is None:
                url = str(fragments[index].get("url") or "").strip()
                if not url:
                    continue
                raw = work_dir / f"sb_{video_id}_{index}.jpg"
                with _sheet_lock(str(raw)):
                    if not raw.exists():
                        # Качаем во временный файл и переименовываем: иначе
                        # соседний клип увидит наполовину скачанный лист
                        # существующим и попытается его открыть.
                        tmp = raw.with_suffix(f".part{threading.get_ident()}")
                        urllib.request.urlretrieve(url, tmp)
                        tmp.replace(raw)
                sheet = Image.open(raw)
                sheet.load()
                sheets[index] = sheet
            # Размер ячейки берём из ОПИСАНИЯ формата, а не делением высоты на
            # число рядов. Последний лист ролика обрезан по высоте — кадров в
            # нём осталось меньше, чем ячеек в сетке: у ролика 98tBEx_dVoE листы
            # 0-6 были 925x450 (5 рядов), а седьмой — 925x180 (два ряда). Деление
            # 180 на 5 давало ячейку высотой 36 вместо 90, и вижну уходила
            # сплющенная полоска вместо кадра. Так портился КАЖДЫЙ ВОСЬМОЙ кадр:
            # 43 из 367 за прогон 27.08.
            tile_w = int(board.get("width") or 0) or (sheet.width // cols)
            tile_h = int(board.get("height") or 0) or (sheet.height // rows)
            real_cols = max(1, sheet.width // tile_w)
            real_rows = max(1, sheet.height // tile_h)
            # Ячейки может не быть вовсе — ролик кончился раньше, чем обещала
            # сетка. Тогда берём ближайшую существующую, а не битую (решение
            # пользователя 27.08: «может просто получив битые брать не битые»).
            # Позицию считаем от РЕАЛЬНОГО числа кадров в этом листе: у
            # обрезанного их 10, а не 25, и деление по заявленной сетке увело бы
            # момент к чужому кадру.
            cells = max(1, real_cols * real_rows)
            cell = min(cells - 1, max(0, int(local / (span / cells))))
            row, col = divmod(cell, real_cols)
            path = work_dir / f"{video_id}_{int(round(float(moment)))}.jpg"
            sheet.crop((
                col * tile_w, row * tile_h,
                (col + 1) * tile_w, (row + 1) * tile_h,
            )).save(path, quality=88)
            out[float(moment)] = path
        except Exception as exc:                       # noqa: BLE001
            logging.warning("  [YouTube] %s%s: кадр %.0fс не взят из раскадровки (%s)",
                            tag, video_id, float(moment), exc)
    for sheet in sheets.values():
        sheet.close()
    logging.info("  [YouTube] %s%s: раскадровка %s — скачано %s лист(ов) из %s, кадров %s",
                 tag, video_id, board.get("format_id"), len(sheets), len(fragments), len(out))
    return out


def _locate_candidate_windows(
    candidate: dict[str, Any],
    *,
    detail: Optional[dict[str, Any]] = None,
    max_windows: int = 6,
    scan_window_sec: float = 10.0,
) -> list[dict[str, Any]]:
    detail = detail or candidate
    video_id = str(candidate.get("video_id") or "").strip()
    duration = max(
        0.0,
        float(
            detail.get("duration_sec")
            or detail.get("duration")
            or candidate.get("duration_sec")
            or 0
        ),
    )
    windows = []
    chapters = detail.get("chapters") or []
    for index, chapter in enumerate(chapters):
        try:
            start = max(
                0.0,
                float(chapter.get("start_time", chapter.get("start", 0))),
            )
            next_chapter = (
                chapters[index + 1] if index + 1 < len(chapters) else None
            )
            end = float(
                chapter.get(
                    "end_time",
                    chapter.get(
                        "end",
                        next_chapter.get("start_time")
                        if next_chapter
                        else duration,
                    ),
                )
            )
        except (AttributeError, TypeError, ValueError):   # нет размеров кадра — ориентацию не проверяем
            continue
        if end > start:
            windows.append({
                "video_id": video_id,
                "window_id": f"{video_id}:chapter:{index}",
                "start": start,
                "end": min(end, duration) if duration else end,
                "origin": "chapter",
                "pre_score": 0.85,
            })
    for index, timestamp in enumerate(
        _description_timestamps(
            str(detail.get("description") or candidate.get("description") or "")
        )
    ):
        if duration and timestamp >= duration:
            continue
        end = min(
            duration or timestamp + scan_window_sec,
            timestamp + max(3.0, scan_window_sec),
        )
        if end > timestamp:
            windows.append({
                "video_id": video_id,
                "window_id": f"{video_id}:description:{index}",
                "start": timestamp,
                "end": end,
                "origin": "description_timestamp",
                "pre_score": 0.75,
            })
    if not windows and duration > 0:
        # Равномерная сетка — последнее средство, когда у ролика нет ни глав, ни
        # тайм-кодов в описании. Начинать её С НУЛЯ нельзя: первые секунды почти
        # любого ролика это заставка, логотип канала, титульная карточка или
        # ведущий в кадре. Улов 26.08: клип 6 получил шесть кандидатов подряд —
        # «брендированная заставка», «интро и аэросъёмка», «титр поверх фермы»,
        # «ведущий у вывески», — вижн отверг все шесть, хотя само сыроварение в
        # этих роликах было, просто дальше по таймлайну. Конец тоже пропускаем:
        # там прощание, титры и призывы подписаться.
        # Ориентир — начинать со ВТОРОЙ МИНУТЫ: к этому времени заканчиваются и
        # заставка, и вступительное слово, и ролик показывает то, ради чего снят.
        # На коротких роликах две минуты съели бы почти всё, поэтому берём пятую
        # часть хронометража, но не больше 120 секунд.
        head_skip = min(120.0, duration * 0.2)
        tail_skip = min(30.0, duration * 0.08)
        first = head_skip if duration > head_skip + scan_window_sec else 0.0
        last = max(first + scan_window_sec, duration - tail_skip)
        span = max(scan_window_sec, last - first)
        # Окна ставим по ЦЕНТРАМ равных долей полезного диапазона — (i+0.5)/N, —
        # а не от его начала с шагом. Раньше первое окно ложилось ровно на
        # границу заставки, а последняя треть ролика не попадала в выборку вовсе.
        # При двух окнах это особенно важно: они встают на четверть и три
        # четверти хронометража — два внутренних диапазона, ни один не с края
        # (требование пользователя 27.08).
        for index in range(max_windows):
            center = first + span * (index + 0.5) / max(1, max_windows)
            start = min(
                max(0.0, duration - scan_window_sec),
                max(0.0, center - scan_window_sec / 2.0),
            )
            end = min(duration, start + scan_window_sec)
            if end > start:
                windows.append({
                    "video_id": video_id,
                    "window_id": f"{video_id}:sparse:{index}",
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "origin": "sparse_scan",
                    "pre_score": 0.25,
                })
    # Голова и хвост ролика непригодны, каким бы способом окно ни нашлось:
    # в начале заставка, логотип канала и вступительное слово, в конце — прощание,
    # титры и призывы подписаться. Для равномерной сетки это уже учтено выше, но
    # главы и тайм-коды приходят как есть, а первая глава почти всегда «Intro»,
    # последняя — «Outro».
    if duration > 0:
        head = min(120.0, duration * 0.2)
        tail = min(30.0, duration * 0.08)

        def _keep(window: dict[str, Any]) -> bool:
            start = float(window.get("start", 0))
            end = float(window.get("end", 0))
            if str(window.get("origin") or "") == "sparse_scan":
                # Слепая сетка ничего не знает о содержании — чистим строго.
                return start >= head and end <= duration - tail
            # Главу и тайм-код расставил автор ролика: это осмысленные метки, и
            # отбрасывать их по общей линейке нельзя — глава на 10-й секунде
            # вполне может быть содержательной. Режем только то, что начинается
            # прямо с заставки или упирается в самый конец.
            return start >= 5.0 and start <= max(0.0, duration - 5.0)

        usable = [w for w in windows if _keep(w)]
        # Если после отсева не осталось ничего (короткий ролик, одна глава на
        # весь хронометраж) — работаем с тем, что есть: пустой список окон
        # означает потерю кандидата целиком.
        if usable:
            dropped = len(windows) - len(usable)
            if dropped:
                logging.info("  [YouTube] %s: отброшено %s окно(окон) из начала/конца ролика",
                             video_id, dropped)
            windows = usable

    return windows[:max(1, int(max_windows))]


def _fragment_tolerance() -> float:
    return max(0.0, float(getattr(config, "YOUTUBE_FRAGMENT_TOLERANCE_SEC", 1.0) or 0.0))


def _plan_piece_length(duration: float, max_fragment_sec: float) -> tuple[float, int]:
    """Длина и число РАВНЫХ кусков, закрывающих сцену без хвоста.

    n = round(длина / предел), кусок = длина / n; кусок длиннее предела на
    больше, чем допуск, → на кусок больше. 6.5/3 → 2 × 3.25; 8.5/3 → 3 × 2.83;
    9/3 → 3 × 3.0; 3.8/3 → 1 × 3.8; 4.5/3 → 2 × 2.25 (4.5 > 3 + 1 допуск).
    """
    duration = max(0.0, float(duration or 0.0))
    maximum = max(0.5, float(max_fragment_sec or 3.0))
    if duration <= 0:
        return maximum, 1
    count = max(1, int(round(duration / maximum)))
    piece = duration / count
    while piece > maximum + _fragment_tolerance() + 1e-6:
        count += 1
        piece = duration / count
    return round(piece, 3), count


def _fixed_candidate_parts(
    windows: list[dict[str, Any]],
    *,
    max_fragment_sec: float,
    limit: int = 0,
    requested_duration_sec: Optional[float] = None,
) -> list[dict[str, Any]]:
    maximum = max(0.5, float(max_fragment_sec or 3.0))
    requested = (
        max(0.0, float(requested_duration_sec))
        if requested_duration_sec is not None
        else maximum
    )
    # Кусок может быть чуть длиннее предела — в рамках допуска (равные куски
    # под сцену, 25.08); дальше предела с допуском — отказ, как раньше.
    if requested <= 0 or requested > maximum + _fragment_tolerance() + 1e-6:
        return []
    # Куски собираем ПО ОКНАМ отдельно, а в корзину ролика выкладываем
    # чередованием окон: сперва по одному куску с каждого, потом вторые и так
    # далее. Раньше корзина заполнялась подряд — все куски первого окна, потом
    # второго, — и лимит (он равен числу одобренных окон) целиком съедали
    # соседние куски ПЕРВОГО окна.
    #
    # Улов AVS-прогона 27.08, клип 4: вижн одобрил пять окон в разных местах
    # ролика, а кандидатами стали 223.8-226.4, 226.8-229.4, 229.8-232.4 — три
    # куска встык из одного окна. Сцене нужно два куска, но встык их брать
    # нельзя (разнос по таймлайну), других окон среди кандидатов не оказалось —
    # и остаток сцены пришлось закрывать архивным фото. Так треть сцен AVS
    # получила статичный кадр вместо движущегося.
    by_window: dict[str, list[list[dict[str, Any]]]] = {}
    seen: set[tuple[str, float, float]] = set()
    for window in windows:
        video_id = str(window.get("video_id") or "").strip()
        if not video_id:
            continue
        try:
            start = max(0.0, float(window.get("start", 0)))
            end = max(start, float(window.get("end", start)))
        except (TypeError, ValueError):   # нечисловой рейтинг — считаем нулём
            continue
        pieces: list[dict[str, Any]] = []
        cursor = start
        while cursor + requested <= end + 1e-6:
            part_start = round(cursor, 3)
            part_end = round(cursor + requested, 3)
            key = (video_id, part_start, part_end)
            if key not in seen:
                seen.add(key)
                pieces.append({
                    "video_id": video_id,
                    "start": part_start,
                    "end": part_end,
                    "duration": round(part_end - part_start, 3),
                    "origin": str(window.get("origin") or "unknown"),
                    "window_id": str(window.get("window_id") or ""),
                    "pre_score": float(window.get("pre_score", 0) or 0),
                })
            cursor += maximum
        if pieces:
            by_window.setdefault(video_id, []).append(pieces)

    by_video: dict[str, list[dict[str, Any]]] = {}
    for video_id, windows_pieces in by_window.items():
        bucket: list[dict[str, Any]] = []
        depth = 0
        while any(len(items) > depth for items in windows_pieces):
            for items in windows_pieces:
                if len(items) > depth:
                    bucket.append(items[depth])
            depth += 1
        by_video[video_id] = bucket
    # limit=0 — без ограничения: кандидатами становятся ВСЕ куски одобренных
    # вижном окон. Отдельное число тут лишнее (решение пользователя 27.08):
    # сколько материала рассматривать, решает вижн своим одобрением, а сцена
    # всё равно берёт ровно столько, сколько ей нужно — цикл резервации
    # прерывается, как только она закрыта.
    #
    # Смысл у лимита был, пока куски скачивались ЗАРАНЕЕ: длинный список означал
    # лишние загрузки. С 27.08 порядок обратный — сперва бронь, качается только
    # забронированное, — и лишний кандидат в списке не стоит ничего.
    result = []
    total_pieces = sum(len(bucket) for bucket in by_video.values())
    limit = total_pieces if int(limit or 0) <= 0 else max(1, int(limit))
    while len(result) < limit:
        added = False
        for bucket in by_video.values():
            if not bucket:
                continue
            part = bucket.pop(0)
            part["candidate_id"] = f"C{len(result) + 1}"
            result.append(part)
            added = True
            if len(result) >= limit:
                break
        if not added:
            break
    return result


def _parse_json_object(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.I | re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:   # ответ не JSON — пробуем следующий разбор
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            value = json.loads(text[start:end + 1])
        except json.JSONDecodeError:   # ответ не JSON и во второй форме — вернём пусто
            return {}
    return value if isinstance(value, dict) else {}


def _validate_candidate_ranking(
    raw: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> list[str]:
    expected = [str(item.get("candidate_id") or "") for item in candidates]
    ranked = [
        str(value).strip()
        for value in raw.get("ranked_candidate_ids") or []
    ]
    if (
        len(ranked) != len(expected)
        or len(set(ranked)) != len(ranked)
        or set(ranked) != set(expected)
    ):
        raise ValueError(
            "Vision returned an incomplete or unknown candidate ID list"
        )
    return ranked


def _vision_candidate_contract(
    clip: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    scene_text: str = "",
    search_queries: Optional[list[str]] = None,
    context: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    # В контракт идёт ТОЛЬКО то, что участвует в решении. Пустые поля не шлём:
    # каждое из них — строка в промпте, которую вижн читает и пропускает.
    contract: dict[str, Any] = {
        "visual_intent": str(
            clip.get("visual_intent")
            or clip.get("query")
            or clip.get("youtube_query")
            or scene_text
            or ""
        ).strip(),
        # Только идентификаторы. Длительность вижну слали зря: он судит по
        # видимому содержанию кадра, в задании про время нет ни слова, а куски и
        # так все одной длины — различить по этому полю нечего.
        "candidates": [
            {"candidate_id": str(item["candidate_id"])}
            for item in candidates
        ],
        "response_schema": {
            "ranked_candidate_ids": ["C1", "C2"],
            "rejected_ids": ["C2"],
            "reasons": {
                "C1": "why it fits the scene brief (kept)",
                "C2": "what is wrong (rejected)",
            },
        },
    }
    if search_queries:
        contract["search_queries"] = list(search_queries)
    if context:
        contract["story_context"] = context
    # must_have ЗАМОРОЖЕН (25.08): жёсткий список «обязательно в кадре» может
    # отбраковать всех кандидатов разом. Ключ появляется только когда флаг
    # включён И список непустой — раньше он уходил вижну пустым всегда.
    if getattr(config, "VISUAL_STORY_MUST_HAVE", False):
        must_have = list(clip.get("must_have") or [])
        if must_have:
            contract["must_have"] = must_have
    avoid = list(clip.get("avoid") or clip.get("negative_constraints") or [])
    if avoid:
        contract["avoid"] = avoid
    return contract


def _extract_preview_frame(
    video_path: Path,
    timestamp: float,
    output_path: Path,
) -> Optional[Path]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    flags = {}
    if os.name == "nt":
        flags["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{max(0.0, float(timestamp)):.3f}",
                "-i",
                str(video_path),
                "-frames:v",
                "1",
                "-vf",
                "scale=480:-2",
                "-q:v",
                "5",
                "-y",
                str(output_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=60,
            **flags,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logging.warning("  [YouTube] кадр превью не снят (%s): %s", output_path.name, exc)
        return None
    if result.returncode != 0 or not output_path.exists():
        logging.warning("  [YouTube] кадр превью пуст (%s), ffmpeg rc=%s",
                        output_path.name, result.returncode)
        return None
    return output_path


def _window_sheet_grid(count: int) -> tuple[int, int]:
    """Колонки и ряды для листа: форма ближе к квадрату читается лучше.

    Ячейка шире, чем выше (кадр 16:9 плюс строка подписи), поэтому рядов нужно
    примерно вдвое больше, чем колонок. Замер 27.08: лист 4x8 при ужатии до
    предела модели теряет больше всего, 5x8 и 4x7 — почти квадратные.
    """
    best = (1, count)
    best_score = None
    for cols in range(2, 9):
        rows = (count + cols - 1) // cols
        width, height = cols * 480, rows * 296
        score = max(width, height) / max(1, min(width, height))
        if best_score is None or score < best_score:
            best_score, best = score, (cols, rows)
    return best


def _build_window_contact_sheet(frames: list[tuple[str, Path]],
                                output_path: Path) -> Optional[Path]:
    """Один кадр — одно окно ролика. Подпись ячейки = id кандидата."""
    images = []
    for candidate_id, path in frames:
        try:
            with Image.open(path) as raw:
                images.append((candidate_id, raw.convert("RGB").copy()))
        except (OSError, ValueError) as exc:
            logging.warning("  [YouTube] кадр %s не открыт (%s)", candidate_id, exc)
    if not images:
        return None
    # приводим к общей ширине: раскадровка мелкая, увеличиваем до читаемого
    width = 480
    scaled = []
    for candidate_id, image in images:
        height = max(2, int(round(width * image.height / max(1, image.width))))
        scaled.append((candidate_id, image.resize((width, height))))
    cell_h = max(image.height for _, image in scaled) + 26
    cols, rows = _window_sheet_grid(len(scaled))
    sheet = Image.new("RGB", (cols * width, rows * cell_h), "white")
    draw = ImageDraw.Draw(sheet)
    for index, (candidate_id, image) in enumerate(scaled):
        row, col = divmod(index, cols)
        x, y = col * width, row * cell_h
        sheet.paste(image, (x, y + 26))
        draw.text((x + 6, y + 6), candidate_id, fill="black")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path, quality=88)
    logging.info("  [YouTube] лист окон: %s кадров, сетка %sx%s, %s КБ",
                 len(scaled), cols, rows, output_path.stat().st_size // 1024)
    return output_path


def _windows_as_candidates(windows: list[dict[str, Any]],
                           details: dict[str, dict[str, Any]],
                           work_dir: Path,
                           clip_id: Optional[int] = None,
                           ) -> tuple[list[dict[str, Any]], list[tuple[str, Path]]]:
    """Окна → кандидаты для вижна плюс кадры их середины из раскадровки.

    Середина окна взята намеренно: у краёв чаще попадается монтажная склейка,
    а в центре — то, ради чего окно и найдено (решение пользователя 27.08).
    """
    by_video: dict[str, list[dict[str, Any]]] = {}
    for window in windows:
        by_video.setdefault(str(window.get("video_id") or ""), []).append(window)

    candidates: list[dict[str, Any]] = []
    frames: list[tuple[str, Path]] = []
    for video_id, items in by_video.items():
        detail = details.get(video_id) or {}
        moments = [
            (float(w.get("start", 0)) + float(w.get("end", 0))) / 2.0
            for w in items
        ]
        shots = _storyboard_frames(detail, moments, work_dir, video_id or "video",
                                   clip_id=clip_id)
        for window, moment in zip(items, moments):
            shot = shots.get(float(moment))
            if shot is None:
                continue
            # Префикс W — свой, чтобы имена окон и кусков НЕ пересекались.
            # Куски нумеруются заново с C1, и при общем префиксе кусок C1
            # отбрасывался за грехи постороннего окна C1: 27.08 так погибли
            # четыре клипа из восьми, в логе стояло «all 1 candidates rejected
            # by vision», хотя вижн оставил годными полтора десятка окон.
            candidate_id = f"W{len(candidates) + 1}"
            # Длительность окна нужна нарезке, вижну её не шлём.
            span = max(0.1, float(window.get("end", 0)) - float(window.get("start", 0)))
            candidates.append({
                **window,
                "candidate_id": candidate_id,
                "moment": moment,
                "duration": round(span, 3),
            })
            frames.append((candidate_id, shot))
    return candidates, frames


def _build_candidate_contact_sheet(
    candidates: list[dict[str, Any]],
    preview_paths: dict[str, Path],
    output_path: Path,
) -> Optional[Path]:
    panels = []
    frame_dir = output_path.parent / "frames"
    for candidate in candidates:
        candidate_id = str(candidate.get("candidate_id") or "")
        video_path = preview_paths.get(candidate_id)
        if not candidate_id or not video_path or not video_path.exists():
            continue
        duration = max(0.1, float(candidate.get("duration", 0) or 0))
        images = []
        # Отступ от конца — 0.1с вместо прежних 0.05. Ролики идут с разной
        # частотой кадров, и отступ должен быть длиннее одного кадра: при 30 к/с
        # кадр длится 0.033с, при 15 — уже 0.067. Замер 26.08: у кандидата с
        # 15 к/с файл длился ровно 3.000с, но последний кадр лежал на 2.933, и
        # запрос на 2.950 уходил в пустоту — ffmpeg возвращал -22, панель
        # собиралась без последнего кадра. 0.1с покрывает всё вплоть до 10 к/с.
        for label, timestamp in (
            ("start", 0.0),
            ("mid", duration / 2),
            ("end", max(0.0, duration - 0.1)),
        ):
            frame = _extract_preview_frame(
                video_path,
                timestamp,
                frame_dir / f"{candidate_id}_{label}.jpg",
            )
            if frame:
                try:
                    images.append(Image.open(frame).convert("RGB"))
                except (OSError, ValueError):   # кадр не сохранён — панель без него
                    pass
        if not images:
            continue
        width = max(image.width for image in images)
        height = max(image.height for image in images)
        panel = Image.new(
            "RGB",
            (width * len(images), height + 32),
            "white",
        )
        ImageDraw.Draw(panel).text((8, 8), candidate_id, fill="black")
        for index, image in enumerate(images):
            panel.paste(image, (index * width, 32))
        panels.append(panel)
    if not panels:
        return None
    columns = 2
    rows = (len(panels) + columns - 1) // columns
    panel_width = max(panel.width for panel in panels)
    panel_height = max(panel.height for panel in panels)
    sheet = Image.new(
        "RGB",
        (panel_width * columns, panel_height * rows),
        "#d8d8d8",
    )
    for index, panel in enumerate(panels):
        sheet.paste(
            panel,
            ((index % columns) * panel_width, (index // columns) * panel_height),
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path, format="JPEG", quality=82, optimize=True)
    return output_path


def _selector_provider_model() -> tuple[str, str]:
    """Провайдер/модель проверки кадров ютуба.

    «auto» из GUI убран 25.08: он разворачивался в бэкенд режиссёра, тот часто
    оказывался не chat-провайдером (codex), пара выходила пустой — и проверка
    кадров молча не работала весь прогон. Пустое/«auto» из старых .env теперь
    подхватывает настройку вижна Real Photo, а при неудаче громко пишет в лог,
    а не молчит."""
    provider = str(
        getattr(config, "YOUTUBE_SELECTOR_PROVIDER", "") or ""
    ).strip().lower()
    if provider in ("", "auto"):
        provider = str(
            getattr(config, "REAL_PHOTO_VISION_PROVIDER", "") or ""
        ).strip().lower()
    # «Умеет ли смотреть» решает vision_pick — один список на всю базу. Свой
    # CHAT_PROVIDERS здесь выключал проверку кадров у CLI-агентов (codex смотрит
    # лист файлом и картинки видит) — улов прогона 25.08.
    from units.operator_hub import _model_for
    from units.vision_picker import vision_capable

    if not vision_capable(provider):
        logging.warning(
            "YouTube: проверка кадров выключена — провайдер %r не умеет смотреть "
            "картинки. Задайте «Проверка кадров» в GUI (YOUTUBE_SELECTOR_PROVIDER).",
            provider or "не задан",
        )
        return "", ""
    configured = str(
        getattr(config, "YOUTUBE_SELECTOR_MODEL", "") or ""
    ).strip()
    if configured:
        return provider, configured
    return provider, _model_for(provider)


def _vision_rank_candidate_parts(
    clip: dict[str, Any],
    candidates: list[dict[str, Any]],
    preview_paths: dict[str, Path],
    artifact_dir: Path,
    *,
    scene_text: str = "",
    search_queries: Optional[list[str]] = None,
    context: Optional[dict[str, Any]] = None,
    sheet_path: Optional[Path] = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    ids = [str(item.get("candidate_id") or "") for item in candidates]
    fallback = {
        "method": "pre_score_fallback",
        "ranked_candidate_ids": ids,
        "raw": "",
    }
    provider, model = _selector_provider_model()
    # У CLI-агента (Codex, Claude Code) модель берётся из настроек режиссёра —
    # пустое поле не порок. Своя проверка «не задана модель» выключала здесь
    # вижн там, где общий модуль работает, и молча (аудит 25.08).
    from units.operator_hub import CLI_AGENTS

    if not provider or (not model and provider not in CLI_AGENTS):
        logging.warning("   [вижн/youtube] провайдер/модель не настроены (%r/%r) — "
                        "ранжирую по pre_score", provider or "—", model or "—")
        fallback["reason"] = "Vision provider/model is not configured"
        fallback["method"] = "pre_score_sorted"
        return sorted(
            candidates,
            key=lambda item: float(item.get("pre_score", 0) or 0),
            reverse=True,
        ), fallback
    # Лист может быть собран заранее — из раскадровки роликов, по кадру на окно.
    sheet = sheet_path or _build_candidate_contact_sheet(
        candidates,
        preview_paths,
        artifact_dir / "contact_sheet.jpg",
    )
    if not sheet:
        logging.warning("   [вижн/youtube] контактный лист не собран — ранжирую по pre_score")
        fallback["reason"] = "Could not create contact sheet"
        fallback["method"] = "pre_score_sorted"
        return sorted(candidates, key=lambda item: float(item.get("pre_score", 0) or 0),
                      reverse=True), fallback
    contract = _vision_candidate_contract(
        clip,
        candidates,
        scene_text=scene_text,
        search_queries=search_queries,
        context=context,
    )
    task = (
        "Rank the fixed YouTube candidate parts shown in the labeled contact "
        "sheet for this scene. Judge only visible content. Return strict JSON. "
        "Rank every supplied candidate exactly once. Do not invent timestamps, "
        "video IDs, URLs, durations, or candidates. Candidate ranges are fixed. "
        "Judge whether the footage WORKS for the scene, not whether every detail "
        "of the description is on screen: incidental details (aisles, an exact "
        "camera angle, a specific prop or count of people) are not requirements. "
        "When the PLACE and the PERIOD are right, the shot works even if the "
        "people or objects the description mentions are not in frame — archival "
        "footage of the right station, hall or street carries the scene, and a "
        "documentary cuts to such a shot all the time. Absence of a named person "
        "is a reason to rank a candidate lower, not to reject it. "
        "Reject only on a real mismatch — wrong place, wrong period, wrong "
        "person or country, or unusable material. A rejection costs a whole new "
        "search and can leave the scene empty, so do not reject a fitting frame "
        "over a missing minor element. "
        "rejected_ids: candidate IDs whose footage does not fit this scene — "
        "wrong subject, wrong place, wrong era, or unusable image. Rejected "
        "candidates still appear in ranked_candidate_ids (at the bottom). "
        "When NO candidate matches the scene brief, put ALL of them into "
        "rejected_ids — the scene will be covered by another source; never "
        "keep a weak match just to fill the slot. reasons: one short visual "
        "reason for EVERY candidate — for "
        "the ones you keep, what in the frame matches the scene brief; for "
        "the rejected ones, what is wrong. The reason for the top-ranked "
        "candidate is mandatory: it is how a human audits the pick.\n\n"
        + json.dumps(contract, ensure_ascii=False)
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "vision_prompt.json").write_text(
        json.dumps(contract, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logging.info("   [вижн/youtube] клип %s: %s/%s ← лист %s КБ, кандидатов %s",
                 clip.get("id"), provider, model,
                 sheet.stat().st_size // 1024, len(candidates))
    vision_started = time.time()
    try:
        # Единый транспорт вижна (chat API или CLI-агент) — свой вызов
        # `chat_completion` здесь бросал «неизвестный chat-провайдер» на codex,
        # исключение уходило в except, и стадия молча ранжировала по эвристике:
        # в логе не оставалось НИ ОДНОЙ строки о вижне (улов прогона 25.08).
        from units.vision_picker import _ask_vision

        raw = _ask_vision(provider, model, task, sheet, work_dir=artifact_dir,
                          schema_file="vision_rank.json")
        parsed = _parse_json_object(raw)
        ranked_ids = _validate_candidate_ranking(parsed, candidates)
        by_id = {str(item["candidate_id"]): item for item in candidates}
        (artifact_dir / "vision_response.txt").write_text(
            str(raw or ""),
            encoding="utf-8",
        )
        from units.vision_picker import normalize_reasons

        reasons = normalize_reasons(parsed.get("reasons"))
        rejected = [str(value).strip() for value in (parsed.get("rejected_ids") or [])]
        logging.info("   [вижн/youtube] клип %s: лучший %s за %.0f с — %s%s",
                     clip.get("id"),
                     ranked_ids[0] if ranked_ids else "?",
                     time.time() - vision_started,
                     str(reasons.get(ranked_ids[0] if ranked_ids else "", ""))[:70],
                     f"; забраковано {len(rejected)}" if rejected else "")
        return [by_id[item] for item in ranked_ids], {
            "method": "vision_contact_sheet",
            "provider": provider,
            "model": model,
            "ranked_candidate_ids": ranked_ids,
            "rejected_ids": rejected,
            "reasons": reasons,
            "contact_sheet": str(sheet),
            "raw": raw,
        }
    except Exception as exc:
        # Раньше провал вижна оседал только в манифесте, и стадия выглядела
        # здоровой, хотя ранжировала эвристикой (улов прогона 25.08).
        logging.warning("   [вижн/youtube] %s/%s не ответил: %s — ранжирую по pre_score",
                        provider, model, str(exc)[:160])
        fallback["reason"] = str(exc)
        fallback["method"] = "pre_score_sorted"
        return sorted(candidates, key=lambda item: float(item.get("pre_score", 0) or 0),
                      reverse=True), fallback
        (artifact_dir / "vision_error.json").write_text(
            json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return candidates, fallback


def _download(
    candidate: dict[str, Any],
    target: Path,
    stop_event: Optional[threading.Event],
    deadline_ts: Optional[float] = None,
    *,
    start_sec: float,
    duration_sec: float,
) -> Optional[Path]:
    url = str(candidate.get("url") or "").strip()
    if not url:
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    end_sec = float(start_sec) + float(duration_sec)
    command = [
        _yt_dlp_path(),
        url,
        "--no-playlist",
        "--no-warnings",
        "-f",
        "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b",
        "--merge-output-format",
        "mp4",
        "--download-sections",
        f"*{float(start_sec):.3f}-{end_sec:.3f}",
        "--force-keyframes-at-cuts",
        "-o",
        str(target.with_suffix(".%(ext)s")),
    ]
    flags = {}
    if os.name == "nt":
        flags["creationflags"] = subprocess.CREATE_NO_WINDOW
    # Порядок как в _run_json: без куков → браузеры по приоритету, с переходом
    # к следующему при неудаче (см. комментарий там же).
    #
    # Ошибки пишем в файл, а не в никуда: скачивание отрезков — такой же поход к
    # ютубу, как запрос метаданных, и ограничение частоты приходит и сюда. Пока
    # stderr уходил в DEVNULL, стадия видела только «не скачалось» и бодро шла
    # перебирать браузеры, то есть добавляла запросов ровно там, где их надо
    # убавить (аудит 27.08).
    log_path = target.parent / f".{target.stem}.ytdlp.log"
    _throttle_wait(stop_event, deadline_ts)
    attempts = _cookie_attempts()
    index = 0
    rate_retries = 0
    tried: set[str] = set()
    widened = False              # добор вариантов делаем один раз за вызов
    bot_checks = 0               # сколько вариантов упёрлось в бот-проверку
    while index < len(attempts):
        browser = attempts[index]
        tried.add(browser)
        try:
            with open(log_path, "w", encoding="utf-8", errors="replace") as err:
                proc = subprocess.Popen(
                    _prepare_ytdlp_command(
                        command,
                        include_browser_cookies=bool(browser),
                        browser=browser or None,
                    ),
                    stdout=subprocess.DEVNULL,
                    stderr=err,
                    env=_yt_env(),
                    **flags,
                )
                started = time.time()
                while proc.poll() is None:
                    if stop_event is not None and stop_event.is_set():
                        proc.kill()
                        proc.wait()
                        return None
                    if time.time() - started > int(
                        getattr(config, "YOUTUBE_DOWNLOAD_TIMEOUT", 900) or 900
                    ):
                        proc.kill()
                        proc.wait()
                        return None
                    if deadline_ts is not None and time.time() >= deadline_ts:
                        proc.kill()
                        proc.wait()
                        return None
                    time.sleep(0.25)
            if proc.returncode != 0:
                try:
                    tail = log_path.read_text(
                        encoding="utf-8", errors="replace").strip()[-500:]
                except OSError:          # лог не прочитан — судим по коду возврата
                    tail = ""
                if RATE_LIMIT_RE.search(tail):
                    _note_rate_limit("скачивание", tail)
                    if rate_retries >= 1:
                        return None
                    rate_retries += 1
                    _throttle_wait(stop_event, deadline_ts)
                    continue          # тот же вариант куков ещё раз, после паузы
                # Сообщение должно называть ПРИЧИНУ и СЛЕДУЮЩИЙ вариант, а не
                # хвост чужого лога. Раньше после «следующий вариант» падал
                # обрезок stderr — обычно кусок подписанного URL, — и строка
                # читалась так, будто следующий вариант куков называется
                # «zxYJHHSR3H1r…». По такому логу нельзя было понять, куки
                # виноваты или что-то другое (разбор жалобы 29.08).
                # Разбор ответа — как в запросе метаданных: до 29.08 здесь
                # смотрели ТОЛЬКО ограничение частоты, а бот-проверка и
                # нечитаемая база куков проходили мимо. Из-за этого замок с
                # плана попыток не снимался никогда, и все ролики подряд
                # уходили в «варианты куков кончились» (разбор жалобы 29.08).
                if BOT_CHECK_RE.search(tail):
                    bot_checks += 1
                if browser and COOKIE_DB_ERROR_RE.search(tail):
                    _drop_cookie_browser(browser)
                elif not widened:
                    extra = _cookie_followups(tail, browser, tried,
                                              attempts[index + 1:])
                    if extra:
                        attempts = [*attempts, *extra]
                        widened = True
                _next = attempts[index + 1] if index + 1 < len(attempts) else None
                _where = (f" → пробую «{_next or 'без куков'}»" if _next is not None
                          else " — варианты куков кончились")
                logging.info("   [ютуб/куки] не вышло с «%s»%s | причина: %s",
                             browser or "без куков", _where,
                             _download_failure_reason(tail, proc.returncode))
            else:
                matches = [
                    path for path in target.parent.glob(target.stem + ".*")
                    if path.suffix.lower() in VIDEO_EXTENSIONS
                    and path.stat().st_size > 1024
                ]
                if matches:
                    _cookie_plan_lock_in(browser)
                    log_path.unlink(missing_ok=True)
                    return matches[0]
        except Exception as exc:
            logging.warning("YouTube download failed: %s", exc)
        index += 1
    if bot_checks and bot_checks >= len(tried):
        # Все варианты куков упёрлись в бот-проверку — тормозим, как при 429.
        _bot_check_advice(tried)
        _note_rate_limit("бот-проверка", "все варианты куков отклонены")
        _throttle_wait(stop_event, deadline_ts)
    log_path.unlink(missing_ok=True)
    return None


def _usage_path(project_dir: Path) -> Path:
    return project_dir / "youtube" / "source_usage.json"


def _load_usage(project_dir: Path) -> dict[str, Any]:
    try:
        value = json.loads(_usage_path(project_dir).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        return {}                                        # первый клип прогона — файла ещё нет
    except Exception as exc:                             # noqa: BLE001
        logging.warning("  [YouTube] счётчик квот не прочитан (%s) — считаю с нуля", exc)
        return {}


def _save_usage(project_dir: Path, usage: dict[str, Any]) -> None:
    path = _usage_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(usage, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


@contextmanager
def _usage_lock(project_dir: Path):
    try:
        from filelock import FileLock
    except ImportError:
        logging.debug("  [YouTube] filelock не установлен — счётчик квот без блокировки")
        yield
        return
    with FileLock(str(_usage_path(project_dir)) + ".lock"):
        yield


def _video_used_seconds(entry: dict[str, Any]) -> float:
    total = 0.0
    for interval in entry.get("intervals") or []:
        try:
            total += max(0.0, float(interval[1]) - float(interval[0]))
        except (IndexError, TypeError, ValueError):   # нечисловая квота — источник без ограничения
            continue
    return total


def _video_clip_ids(entry: dict[str, Any]) -> set[int]:
    """Номера клипов, бравших ролик. Из интервалов-троек [start, end, clip_id].

    Старые двухэлементные интервалы номера не несут — для них остаётся только
    ``last_clip``. На запись ролика он один, поэтому по старому файлу дистанция
    и цепочка видят лишь последнего взявшего; новый формат помнит всех.
    """
    ids: set[int] = set()
    for interval in entry.get("intervals") or []:
        if len(interval) >= 3:
            try:
                ids.add(int(interval[2]))
            except (TypeError, ValueError):   # нечисловое значение квоты — пропускаем
                continue
    if not ids and entry.get("last_clip") is not None:
        try:
            ids.add(int(entry["last_clip"]))
        except (TypeError, ValueError):   # нечисловая доля — квота не применяется
            pass
    return ids


def _quota_block_reason(
    entry: dict[str, Any],
    clip_id: int,
    total_duration: float,
    *,
    donor_sourced: bool,
) -> str:
    """Пустая строка — ролик доступен клипу; иначе человекочитаемая причина.

    Три квоты решения H9 (23.08): доля хронометража — всем; цепочка подряд —
    донорским источникам переиспользования; дистанция в клипах — свежему поиску.
    """
    if not entry:
        return ""
    share = float(getattr(config, "YOUTUBE_SOURCE_MAX_SHARE", 0.15) or 0.0)
    if share > 0 and total_duration > 0:
        used = _video_used_seconds(entry)
        if used >= share * total_duration:
            return (
                f"share {used:.1f}s >= {share:.0%} of {total_duration:.1f}s"
            )
    clip_ids = _video_clip_ids(entry)
    if not clip_ids:
        return ""
    if donor_sourced:
        chain = int(getattr(config, "YOUTUBE_SOURCE_MAX_CHAIN", 3) or 3)
        if all(
            (clip_id - offset) in clip_ids
            for offset in range(1, chain + 1)
        ):
            return f"chain of {chain} consecutive scenes"
    else:
        min_gap = int(
            getattr(config, "YOUTUBE_SAME_VIDEO_MIN_CLIP_GAP", 15) or 0
        )
        # СВОЙ клип из проверки исключаем. Дистанция стережёт повтор ролика в
        # РАЗНЫХ сценах, а сцена из нескольких кусков берёт один ролик законно:
        # разнос кусков внутри ролика стережёт отдельная квота
        # (`YOUTUBE_SAME_VIDEO_MIN_SOURCE_GAP_SEC`).
        #
        # Пока проверка стояла только в фильтре кандидатов, клип себя не видел —
        # он ещё ничего не занял. После переноса в резервацию (27.08) первый же
        # кусок делал ролик «занятым» для второго: «clip distance 0 < 15», и
        # пятисекундные сцены из двух кусков разваливались.
        others = [value for value in clip_ids if int(value) != int(clip_id)]
        if min_gap > 0 and others:
            nearest = min(abs(clip_id - used) for used in others)
            if nearest < min_gap:
                return f"clip distance {nearest} < {min_gap}"
    return ""


def _quota_filter_candidates(
    usage: dict[str, Any],
    candidates: list[dict[str, Any]],
    clip_id: int,
    total_duration: float,
    *,
    donor_sourced: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    allowed: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for candidate in candidates:
        video_id = str(candidate.get("video_id") or "").strip()
        reason = _quota_block_reason(
            usage.get(video_id) or {},
            clip_id,
            total_duration,
            donor_sourced=donor_sourced,
        )
        if reason:
            blocked.append({"video_id": video_id, "reason": reason})
            logging.info(
                "[YouTube] clip %s: source %s skipped by quota (%s)",
                clip_id, video_id, reason,
            )
        else:
            allowed.append(candidate)
    return allowed, blocked


def _reserve_locked(
    project_dir: Path,
    usage: dict[str, Any],
    video_id: str,
    start: float,
    end: float,
    clip_id: int,
    total_duration: float = 0.0,
    *,
    donor_sourced: bool = False,
) -> bool:
    gap = float(
        getattr(config, "YOUTUBE_SAME_VIDEO_MIN_SOURCE_GAP_SEC", 20.0)
        or 20.0
    )
    share = float(getattr(config, "YOUTUBE_SOURCE_MAX_SHARE", 0.15) or 0.0)
    with _usage_lock(project_dir):
        fresh = _load_usage(project_dir)
        entry = fresh.get(video_id) or {}
        # Дистанция между клипами и цепочка подряд — тоже под блокировкой.
        # Раньше их проверял только фильтр кандидатов, по СНИМКУ леджера: пока
        # клипы шли по очереди, снимок был свежим, а с параллелью два клипа
        # успевают взять один ролик ближе допустимого — каждый видел леджер до
        # чужой записи (аудит 27.08).
        blocked = _quota_block_reason(
            entry, clip_id, total_duration, donor_sourced=donor_sourced,
        )
        if blocked:
            logging.info("  [YouTube] клип %s: ролик %s занят по квоте (%s) — беру другой",
                         clip_id, video_id, blocked)
            usage.clear()
            usage.update(fresh)
            return False
        for interval in entry.get("intervals", []):
            # Разнос действует и на СВОИ куски: два отрезка встык склеились бы в
            # непрерывный кусок чужого ролика, а сцена должна собираться из
            # РАЗНЫХ его мест (требование пользователя 27.08). Поэтому свой
            # клип здесь НЕ исключается — в отличие от дистанции между сценами.
            old_start, old_end = float(interval[0]), float(interval[1])
            distance = max(
                old_start - end,
                start - old_end,
                0.0,
            )
            if distance < gap:
                logging.info("  [YouTube] клип %s: отрезок %s %.1f-%.1f с ближе %.0f с "
                             "к уже занятому — беру другой",
                             clip_id, video_id, start, end, gap)
                usage.clear()
                usage.update(fresh)
                return False
        # Точная проверка доли в момент записи: фильтр кандидатов работает по
        # снимку леджера и при параллельных клипах может отстать от него.
        if share > 0 and total_duration > 0:
            if _video_used_seconds(entry) + max(0.0, end - start) > (
                share * total_duration
            ):
                usage.clear()
                usage.update(fresh)
                return False
        item = fresh.setdefault(
            video_id,
            {"uses": 0, "intervals": [], "last_clip": None},
        )
        item["uses"] = int(item.get("uses", 0)) + 1
        item["last_clip"] = clip_id
        item.setdefault("intervals", []).append(
            [round(start, 3), round(end, 3), int(clip_id)]
        )
        _save_usage(project_dir, fresh)
        usage.clear()
        usage.update(fresh)
        return True


def _release_reservations(
    project_dir: Path,
    usage: dict[str, Any],
    reservations: list[dict[str, Any]],
) -> None:
    if not reservations:
        return
    with _usage_lock(project_dir):
        fresh = _load_usage(project_dir)
        for reservation in reservations:
            video_id = str(reservation.get("video_id") or "")
            item = fresh.get(video_id)
            if not isinstance(item, dict):
                continue
            target = [
                round(float(reservation.get("start", 0)), 3),
                round(float(reservation.get("end", 0)), 3),
            ]
            target_clip = reservation.get("clip_id")
            intervals = list(item.get("intervals") or [])
            for index, interval in enumerate(intervals):
                if [
                    round(float(interval[0]), 3),
                    round(float(interval[1]), 3),
                ] != target:
                    continue
                # Тройка сверяется и по номеру клипа, чтобы не снять чужую
                # резервацию с теми же границами; старую пару (без номера)
                # различить не по чему — совпадения границ достаточно.
                if (
                    len(interval) >= 3
                    and target_clip is not None
                    and int(interval[2]) != int(target_clip)
                ):
                    continue
                intervals.pop(index)
                item["uses"] = max(0, int(item.get("uses", 0)) - 1)
                break
            item["intervals"] = intervals
            if not intervals:
                fresh.pop(video_id, None)
        _save_usage(project_dir, fresh)
        usage.clear()
        usage.update(fresh)


def _compose_parts(
    source_parts: list[tuple[Path, float]],
    output: Path,
    *,
    target_w: int,
    target_h: int,
    fps: int,
    stop_event: Optional[threading.Event],
) -> dict[str, Any]:
    if not source_parts:
        return {"ok": False, "error": "no selected YouTube parts"}
    work = output.parent / f".{output.stem}_youtube_parts"
    work.mkdir(parents=True, exist_ok=True)
    rendered = []
    try:
        for index, (source, duration) in enumerate(source_parts, start=1):
            target = work / f"{index:03d}.mp4"
            cut = cut_footage(
                source,
                target,
                duration,
                target_w=target_w,
                target_h=target_h,
                fps=fps,
                start_sec=0,
                mute=True,
                stop_event=stop_event,
            )
            if not cut.get("ok"):
                return cut
            rendered.append(target)
        list_path = work / "concat.txt"
        lines = []
        for path in rendered:
            lines.append(f"file '{path.as_posix().replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n")
        list_path.write_text("".join(lines), encoding="utf-8")
        flags = {}
        if os.name == "nt":
            flags["creationflags"] = subprocess.CREATE_NO_WINDOW
        result = subprocess.run(
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_path),
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                "-an",
                str(output),
            ],
            capture_output=True,
            timeout=int(getattr(config, "YOUTUBE_DOWNLOAD_TIMEOUT", 900) or 900),
            **flags,
        )
        if result.returncode != 0 or not output.exists():
            return {
                "ok": False,
                "error": (
                    result.stderr or b""
                ).decode("utf-8", "replace")[-500:] or "concat failed",
            }
        return {"ok": True, "file": str(output), "parts": len(rendered)}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _find_generated_image(project: Path, clip_id: int) -> Optional[Path]:
    generated = project / "generated"
    for suffix in (".jpg", ".jpeg", ".png", ".webp"):
        candidate = generated / f"{clip_id:03d}{suffix}"
        if candidate.is_file() and candidate.stat().st_size > 1024:
            return candidate
    return None


def _render_epf_tail(
    image_path: Path,
    output_path: Path,
    *,
    duration: float,
    target_w: int,
    target_h: int,
    fps: int,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Зум делаем тем же способом, что и склейка. Здесь стоял `zoompan`, и он
    # ощутимо дрожал: центр кадра он считает целыми пикселями, а приращение зума
    # за кадр меньше пикселя — координата то стоит, то прыгает на единицу. На
    # фото с тонкими прямыми это видно сразу (улов 26.08).
    from units.kenburns_motion import still_zoom_filter

    video_filter = still_zoom_filter(
        target_w, target_h, duration, zoom_amount=0.06, fps=fps)
    flags = {}
    if os.name == "nt":
        flags["creationflags"] = subprocess.CREATE_NO_WINDOW
    result = subprocess.run(
        [
            shutil.which("ffmpeg") or "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-loop",
            "1",
            "-i",
            str(image_path),
            "-vf",
            video_filter,
            "-t",
            f"{max(0.1, float(duration)):.3f}",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(output_path),
        ],
        capture_output=True,
        timeout=max(
            120,
            int(getattr(config, "YOUTUBE_DOWNLOAD_TIMEOUT", 900) or 900),
        ),
        **flags,
    )
    if result.returncode != 0 or not output_path.is_file():
        error = (result.stderr or b"").decode("utf-8", "replace")[-500:]
        raise RuntimeError(error or "EPF tail render failed")
    return output_path


def _run_epf_fallback(
    project: Path,
    clip: dict[str, Any],
    *,
    stop_event: Optional[threading.Event],
    deadline_ts: Optional[float],
    target_w: int,
    target_h: int,
) -> dict[str, Any]:
    from units.archive_photos import ClipSelection, RealPhotoProcessor

    selection = ClipSelection(
        clip_id=int(clip.get("id")),
        search_query=_archive_query(project, clip),
        search_query_local="",
        preferred_type="photo",
        preferred_sources=list(getattr(
            config,
            "REAL_PHOTO_SOURCES",
            ["wikimedia", "nasa", "loc", "archive"],
        )),
        reasoning="YouTube pipeline failed; route to EPF/Real Photo",
        confidence="medium",
        query_provider="youtube_fallback",
        query_path="fallback",
    )
    processor = RealPhotoProcessor(
        project_dir=project,
        target_ratio=1.0,
        min_ratio=1.0,
        max_ratio=1.0,
        sources=selection.preferred_sources,
        target_w=target_w,
        target_h=target_h,
        fps=getattr(config, "VIDEO_FPS", 60),
    )
    old_bridge = getattr(config, "REAL_PHOTO_EPF_BRIDGE", False)
    config.REAL_PHOTO_EPF_BRIDGE = True
    try:
        # The YouTube wait timer may already have expired. Once useful YouTube
        # parts are selected, finish only the missing tail with a fresh EPF
        # budget instead of cancelling the whole scene.
        fallback_timeout = max(
            30,
            int(getattr(config, "REAL_PHOTO_EPF_SEARCH_TIMEOUT", 180) or 180),
        )
        return processor.process_phase(
            [selection],
            stop_event=None,
            deadline_ts=time.time() + fallback_timeout,
        )
    finally:
        config.REAL_PHOTO_EPF_BRIDGE = old_bridge


def fallback_pending_youtube(
    project_dir: str | Path,
    *,
    target_w: int = 1920,
    target_h: int = 1080,
    fps: Optional[int] = None,
) -> dict[str, Any]:
    """Route every remaining ``youtube_pending`` clip through EPF/archive."""
    project = Path(project_dir).resolve()
    montage = project / "montage_plan.json"
    if not montage.exists():
        return {"selected": 0, "error": "montage_plan.json missing"}
    plan = _load_plan(montage)
    pending = [
        clip for clip in plan.get("clips", [])
        if clip.get("source") == "youtube_pending"
    ]
    if not pending:
        return {"selected": 0, "rendered": 0}

    from units.source_ledger import apply_clip_source_delta
    from units.archive_photos import ClipSelection, RealPhotoProcessor

    clip_ids = [int(clip.get("id")) for clip in pending]
    apply_clip_source_delta(
        montage,
        {clip_id: {"source": "real_photo_pending"} for clip_id in clip_ids},
        log=lambda message: print(f"  [YouTube fallback] {message}"),
    )
    selections = [
        ClipSelection(
            clip_id=int(clip.get("id")),
            search_query=_archive_query(project, clip),
            search_query_local="",
            preferred_type="photo",
            preferred_sources=list(getattr(
                config,
                "REAL_PHOTO_SOURCES",
                ["wikimedia", "nasa", "loc", "archive"],
            )),
            reasoning="YouTube wait expired; route to EPF/Real Photo",
            confidence="medium",
            query_provider="youtube_wait_fallback",
            query_path="fallback",
        )
        for clip in pending
    ]
    processor = RealPhotoProcessor(
        project_dir=project,
        target_ratio=1.0,
        min_ratio=1.0,
        max_ratio=1.0,
        sources=selections[0].preferred_sources,
        target_w=target_w,
        target_h=target_h,
        fps=int(fps or getattr(config, "VIDEO_FPS", 60)),
    )
    timeout_per_clip = max(
        30,
        int(getattr(config, "REAL_PHOTO_EPF_SEARCH_TIMEOUT", 180) or 180),
    )
    old_bridge = getattr(config, "REAL_PHOTO_EPF_BRIDGE", False)
    config.REAL_PHOTO_EPF_BRIDGE = True
    try:
        result = processor.process_phase(
            selections,
            stop_event=None,
            deadline_ts=time.time() + timeout_per_clip * len(selections),
        )
        return {
            "selected": len(selections),
            "clip_ids": clip_ids,
            "result": result,
        }
    except Exception as exc:
        apply_clip_source_delta(
            montage,
            {clip_id: {"source": None} for clip_id in clip_ids},
            log=lambda message: print(f"  [YouTube fallback] {message}"),
        )
        return {
            "selected": len(selections),
            "clip_ids": clip_ids,
            "error": str(exc),
        }
    finally:
        config.REAL_PHOTO_EPF_BRIDGE = old_bridge


def _process_clip(
    project: Path,
    clip: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    output_dir: Path,
    usage: dict[str, Any],
    target_w: int,
    target_h: int,
    fps: int,
    max_fragment: float,
    stop_event: Optional[threading.Event],
    deadline_ts: Optional[float],
    search_queries: Optional[list[str]] = None,
    context: Optional[dict[str, Any]] = None,
    total_duration: float = 0.0,
    donor_sourced: bool = False,
) -> dict[str, Any]:
    clip_id = int(clip.get("id"))
    duration = _clip_duration(clip)
    clip_dir = project / "youtube" / "clips" / f"{clip_id:03d}"
    preview_dir = clip_dir / "previews"
    preview_dir.mkdir(parents=True, exist_ok=True)
    # Квоты источника проверяются ДО разметки окон: превью ролика, который
    # всё равно не пройдёт резервацию, не должно даже скачиваться.
    candidates, quota_blocked = _quota_filter_candidates(
        usage,
        candidates,
        clip_id,
        total_duration,
        donor_sourced=donor_sourced,
    )
    if not candidates:
        raise RuntimeError(
            "all YouTube candidates are blocked by source quotas: "
            + json.dumps(quota_blocked, ensure_ascii=False)
        )
    windows = []
    locator = []
    # Детали роликов держим до конца клипа: из них берётся раскадровка для листа
    # вижна. Раньше `detail` жил только внутри цикла и терялся.
    details_by_video: dict[str, dict[str, Any]] = {}
    # Секундомер по этапам: до 27.08 разбирать, куда ушло время клипа,
    # приходилось по времени создания файлов в его папке — в логе таких цифр не
    # было вовсе (требование пользователя: «внедряй логи»).
    timings: dict[str, float] = {}
    _t0 = time.time()

    def _mark(stage: str) -> None:
        nonlocal _t0
        now = time.time()
        timings[stage] = timings.get(stage, 0.0) + (now - _t0)
        _t0 = now

    # Порядок опроса — по названию и описанию, которые Data API отдал вместе с
    # выдачей. Опрашиваем не весь пул, а ровно столько роликов, сколько нужно
    # листу вижна, и добираем из хвоста тех, кто отпал по ориентации: раньше
    # yt-dlp вызывался для всех двенадцати найденных (решение пользователя 27.08).
    ordered = _rank_pool_by_metadata(
        clip,
        candidates,
        queries=list(search_queries or []),
        scene_text=_clip_text(project, clip),
        artifact_dir=clip_dir,
    )
    _mark("отбор")
    pool_target = youtube_pool_size()
    queue = list(ordered)
    usable = asked = 0
    while queue and usable < pool_target:
        batch = queue[:max(1, pool_target - usable)]
        del queue[:len(batch)]
        asked += len(batch)
        batch_details = _collect_details(batch, stop_event, deadline_ts)
        details_by_video.update(batch_details)
        for candidate in batch:
            detail = batch_details.get(
                str(candidate.get("video_id") or "")
            ) or dict(candidate)
            # Ориентация ролика против целевой (улов 25.08: вертикальные Shorts
            # доходили до вижна и занимали пул). Отсекаем ДО окон и превью.
            mismatch = _orientation_mismatch(detail, target_w, target_h)
            if mismatch:
                logging.info(
                    "  [YouTube] clip %s: %s пропущен — %s",
                    clip_id, candidate.get("video_id"), mismatch,
                )
                locator.append({
                    "video_id": candidate.get("video_id"),
                    "windows": 0,
                    "origins": [],
                    "skipped": mismatch,
                })
                continue
            found = _locate_candidate_windows(
                candidate,
                detail=detail,
                max_windows=windows_per_video(),
            )
            windows.extend(found)
            locator.append({
                "video_id": candidate.get("video_id"),
                "windows": len(found),
                "origins": sorted({item.get("origin") for item in found}),
            })
            if found:
                usable += 1
        if stop_event is not None and stop_event.is_set():
            break
        if deadline_ts is not None and time.time() >= deadline_ts:
            break
    _mark("метаданные")
    if asked < len(ordered):
        logging.info("  [YouTube] clip %s: опрошено %s ролик(ов) из %s — "
                     "пул набран, остальные не понадобились",
                     clip_id, asked, len(ordered))
    # Сцену закрываем РАВНЫМИ кусками, посчитанными заранее (решение
    # пользователя 25.08): 6.5 с → 2 × 3.25, 8.5 → 3 × 2.83, 3.2 → 1 × 3.2.
    # Раньше куски были ровно по пределу, а остаток (хоть 0.1 с) уходил в
    # EPF-стоп-кадр — мигание для зрителя.
    piece_len, piece_count = _plan_piece_length(duration, max_fragment)
    logging.info(
        "  [YouTube] clip %s: сцена %.2f с → %d кус. по %.2f с",
        clip_id, duration, piece_count, piece_len,
    )
    metadata = {
        str(item.get("video_id")): item for item in candidates
    }
    preview_rejections = []
    previews: dict[str, Path] = {}

    # Вижн смотрит ОКНА по раскадровке, а не скачанные куски. Раньше стадия
    # качала превью каждого кандидата до всякого отбора — 125 секунд на клип,
    # две трети её времени, и всё это ради кадров, большинство из которых вижн
    # тут же отвергал. Раскадровка отдаёт кадры всего ролика одним лёгким
    # запросом, а видео качается только за теми окнами, что вижн выбрал
    # (решение пользователя 27.08).
    # Папка раскадровки ОБЩАЯ на прогон: лист зависит от ролика, а не от сцены.
    window_candidates, window_frames = _windows_as_candidates(
        windows, details_by_video, project / "youtube" / "storyboard",
        clip_id=clip_id)
    _mark("раскадровка")
    ranked_windows: list[dict[str, Any]] = []
    vision: dict[str, Any] = {}
    if window_candidates and window_frames:
        sheet = _build_window_contact_sheet(
            window_frames, clip_dir / "contact_sheet.jpg")
        if sheet:
            logging.info("  [YouTube] clip %s: вижну %s окон с %s ролик(ов)",
                         clip_id, len(window_candidates),
                         len({str(w.get("video_id")) for w in window_candidates}))
            ranked_windows, vision = _vision_rank_candidate_parts(
                clip,
                window_candidates,
                {},
                clip_dir,
                scene_text=_clip_text(project, clip),
                search_queries=search_queries,
                context=context,
                sheet_path=sheet,
            )
            _mark("вижн")
    if not ranked_windows:
        # Раскадровки нет (редкие ролики) — работаем как раньше, по окнам в
        # порядке их веса; кадры вижн увидит уже скачанными.
        logging.info("  [YouTube] clip %s: раскадровка недоступна — обычный путь",
                     clip_id)
        ranked_windows = sorted(
            windows, key=lambda item: float(item.get("pre_score", 0) or 0), reverse=True)

    rejected_windows = {
        str(value).strip() for value in (vision.get("rejected_ids") or [])
    }
    chosen_windows = [
        window for window in ranked_windows
        if str(window.get("candidate_id") or "") not in rejected_windows
    ]
    if not chosen_windows:
        raise RuntimeError(
            f"all {len(ranked_windows)} candidates rejected by vision"
        )

    # Из выбранных окон режем куски и качаем ТОЛЬКО их.
    parts = _fixed_candidate_parts(
        chosen_windows,
        max_fragment_sec=max_fragment,
        limit=0,                      # все куски одобренных окон
        requested_duration_sec=piece_len,
    )
    enriched = [
        {**metadata[str(part["video_id"])], **part}
        for part in parts
        if str(part["video_id"]) in metadata
    ]
    if not enriched:
        raise RuntimeError("temporal locator produced no fixed candidate parts")
    ranked = enriched
    remaining = duration
    selected = []
    source_parts = []
    reservations = []
    epf_tail = None
    # Отбракованных вижном не берём никогда (решение 23.08): лучше EPF-хвост,
    # чем кадр, который отборщик прямо назвал непригодным.
    #
    # Имена окон (W1…Wn) и кусков (C1…Cn) намеренно разные, поэтому вердикт по
    # окнам сюда просто не попадёт: фильтр по `known_ids` его отсеет. На старом
    # пути, где вижн смотрит сами куски, список работает как прежде.
    known_ids = {str(part["candidate_id"]) for part in ranked}
    rejected_ids = {
        str(value).strip()
        for value in vision.get("rejected_ids") or []
        if str(value).strip() in known_ids
    }
    try:
        for part in ranked:
            if remaining <= 0.05:
                break
            if str(part["candidate_id"]) in rejected_ids:
                continue
            take = min(float(part["duration"]), remaining)
            reservation = {
                "video_id": str(part["video_id"]),
                "start": float(part["start"]),
                "end": round(float(part["start"]) + take, 3),
                "clip_id": clip_id,
            }
            if not _reserve_locked(
                project,
                usage,
                reservation["video_id"],
                reservation["start"],
                reservation["end"],
                clip_id,
                total_duration=total_duration,
                donor_sourced=donor_sourced,
            ):
                continue
            # Качаем ТОЛЬКО то, что удалось забронировать. Раньше порядок был
            # обратный: скачивались первые `piece_count` кусков, и в резервацию
            # попадали только они. Сцене на 3 секунды нужен один кусок — если
            # именно он упирался в квоту дистанции, клип падал с «candidate
            # parts are short», хотя вижн оставил ещё десяток годных окон из
            # других роликов: до них дело не доходило, они не были скачаны.
            # Так на прогоне 27.08 погибли 6 клипов из 20. Бронь мгновенная и
            # локальная, скачивание — дорогое: сначала бронь.
            preview = _download(
                part,
                preview_dir / str(part["candidate_id"]),
                stop_event,
                deadline_ts,
                start_sec=float(part["start"]),
                # Ровно столько, сколько берёт сцена: последний кусок часто
                # короче окна, а лишние секунды потом всё равно отрезались.
                duration_sec=float(take),
            )
            if preview is None:
                # Не скачалось — бронь сразу возвращаем, иначе интервал остался
                # бы занятым впустую и закрыл ролик соседним сценам.
                _release_reservations(project, usage, [reservation])
                preview_rejections.append({
                    "candidate_id": part["candidate_id"],
                    "reason": "preview_download_failed",
                })
                continue
            previews[str(part["candidate_id"])] = preview
            reservations.append(reservation)
            selected.append({
                "candidate_id": part["candidate_id"],
                **reservation,
                "duration": round(take, 3),
                "url": part.get("url", ""),
                "license": part.get("license", "unknown"),
                "license_verified": bool(part.get("license_verified", False)),
            })
            source_parts.append((preview, take))
            remaining -= take
        _mark("загрузка")
        if remaining > 0.05:
            if not selected:
                # Причину называем честно: по ней потом видно, чинить вижн,
                # квоты или сеть. «Не хватило длины» — последний вариант.
                if rejected_ids and rejected_ids >= known_ids:
                    raise RuntimeError(
                        f"all {len(known_ids)} candidates rejected by vision"
                    )
                if len(preview_rejections) >= len(known_ids):
                    raise RuntimeError(
                        f"ни один из {len(known_ids)} отрезков не скачался"
                    )
                if preview_rejections:
                    raise RuntimeError(
                        f"свободных по квоте кусков нет, а скачать удалось "
                        f"{len(known_ids) - len(preview_rejections)} из "
                        f"{len(known_ids)}"
                    )
                raise RuntimeError(
                    f"все {len(known_ids)} кусков заняты по квоте "
                    f"(нужно ещё {remaining:.1f} с)"
                )
            fallback_result = _run_epf_fallback(
                project,
                clip,
                stop_event=stop_event,
                deadline_ts=deadline_ts,
                target_w=target_w,
                target_h=target_h,
            )
            fallback_image = _find_generated_image(project, clip_id)
            if fallback_image is None:
                raise RuntimeError(
                    "EPF did not provide an image for the YouTube remainder"
                )
            fallback_video = _render_epf_tail(
                fallback_image,
                clip_dir / "epf_tail.mp4",
                duration=remaining,
                target_w=target_w,
                target_h=target_h,
                fps=fps,
            )
            source_parts.append((fallback_video, remaining))
            epf_tail = {
                "source": "epf",
                "duration": round(remaining, 3),
                "image": str(fallback_image),
                "video": str(fallback_video),
                "result": fallback_result,
            }
            remaining = 0.0
            # Хвост архивов — отдельная метка. Он падал в «сборку» и врал в
            # разборе: сборки как таковой больше нет, а строка показывала
            # 60-70 секунд (замер AVS 27.08) — это работа EPF, а не склейка.
            _mark("архивный хвост")
        # Отрезки кладём КАК ЕСТЬ, склейку не делаем. Раньше стадия гоняла
        # пиксели дважды: `cut_footage` приводил каждый кусок к 1920x1080@60
        # libx264, затем `_compose_parts` кодировал результат ВТОРОЙ раз, — а
        # потом склейка проекта делала то же самое в ТРЕТИЙ. На пятисекундную
        # сцену уходило под минуту, больше любого другого этапа клипа (замер
        # 27.08: сборка 56 с медиана против 24 с у вижна).
        #
        # Теперь формат наводится один раз — в `video_concat.process_video`,
        # который и так делает ретайминг, масштаб, fps и кодек. Куски лежат
        # рядом с обычными клипами: `001_1.mp4`, `001_2.mp4`; их видят и
        # `clip_media_exists` (генерация не рисует поверх), и склейка
        # (`_resolve_clip_parts`), а `rename_videos` их не трогает.
        output = output_dir / f"{clip_id:03d}.mp4"
        for stale in output_dir.glob(f"{clip_id:03d}_*.mp4"):
            stale.unlink(missing_ok=True)
        output.unlink(missing_ok=True)
        stored_parts: list[str] = []
        if len(source_parts) == 1:
            # Один отрезок — это и есть сцена, отдаём обычным именем.
            shutil.copy2(source_parts[0][0], output)
            stored_parts.append(f"vid_img/{clip_id:03d}.mp4")
        else:
            for index, (source, _take) in enumerate(source_parts, start=1):
                target = output_dir / f"{clip_id:03d}_{index}.mp4"
                shutil.copy2(source, target)
                stored_parts.append(f"vid_img/{target.name}")
        rendered = {
            "ok": True,
            "file": stored_parts[0],
            "parts": stored_parts,
        }
        logging.info("  [YouTube] clip %s: отдано %s кус. без перекодирования — %s",
                     clip_id, len(stored_parts), ", ".join(stored_parts))
        _mark("сборка")
    except Exception:
        _release_reservations(project, usage, reservations)
        raise
    selection = {
        "method": vision.get("method"),
        "queries": list(
            search_queries
            or _clip_search_queries(clip, _clip_text(project, clip))
        ),
        "context": context or {},
        "locator": locator,
        "candidate_parts": [
            {
                "candidate_id": part["candidate_id"],
                "video_id": part["video_id"],
                "start": part["start"],
                "end": part["end"],
                "duration": part["duration"],
                "origin": part["origin"],
                "window_id": part["window_id"],
            }
            for part in ranked
        ],
        "vision": vision,
        "preview_rejections": preview_rejections,
        "quota_blocked": quota_blocked,
        "selected_parts": selected,
        "epf_tail": epf_tail,
        "scene_duration": duration,
        # Тайминги этапов кладём В САМ ФАЙЛ, а не только в лог: разбирать, куда
        # ушло время клипа, по времени создания файлов в папке — это гадание
        # (замечание пользователя 27.08). Присвоение стояло ПОСЛЕ записи файла,
        # поэтому в selection.json не попадало вовсе.
        "timings": {name: round(value, 1) for name, value in timings.items()},
    }
    clip_dir.mkdir(parents=True, exist_ok=True)
    (clip_dir / "selection.json").write_text(
        json.dumps(selection, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logging.info(
        "  [YouTube] клип %s ГОТОВ за %.0f с — %s | сцена %.1f с из %s кус., "
        "роликов опрошено %s, окон вижну %s",
        clip_id, sum(timings.values()),
        ", ".join(f"{name} {value:.0f}с" for name, value in timings.items()),
        duration, len(selected), asked, len(window_candidates),
    )
    (clip_dir / "provenance.json").write_text(
        json.dumps({
            "clip_id": clip_id,
            "scene_duration": duration,
            "queries": selection["queries"],
            "parts": selected,
            "epf_tail": epf_tail,
            "output": str(output),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return rendered


def run_youtube_stage(
    project_dir: str | Path,
    *,
    target_w: int = 1920,
    target_h: int = 1080,
    fps: Optional[int] = None,
    stop_event: Optional[threading.Event] = None,
    deadline_ts: Optional[float] = None,
    sources_by_clip: Optional[dict[int, list[dict[str, Any]]]] = None,
) -> dict[str, Any]:
    """``sources_by_clip``: готовые ролики вместо поиска (переиспользование H8).

    Клип из этой карты пропускает discovery и подбирает НОВЫЙ отрезок в
    перечисленных роликах — под свою длительность и с оглядкой на леджер.
    Ролики выбрали квоту или отрезок не собрался → обычный поиск по запросам
    клипа (у переиспользования это запросы донора).
    """
    project = Path(project_dir).resolve()
    # Свой лог стадии рядом с проектом: у ютуба его не было ни в каком режиме,
    # и отказы приходилось искать в общем логе прогона среди тысяч строк.
    from units.stage_journal import attach as _attach_log, detach as _detach_log
    _log_handler, _log_path = _attach_log(project, 'youtube')
    if _log_path:
        print(f"  [YouTube] лог стадии: {_log_path}")
    montage = project / "montage_plan.json"
    if not montage.exists():
        _detach_log(_log_handler)
        return {
            "ok": 0,
            "failed": 0,
            "total": 0,
            "error": "montage_plan.json missing",
        }
    plan = _load_plan(montage)
    pending = [
        clip for clip in plan.get("clips", [])
        if clip.get("source") == "youtube_pending"
    ]
    if not pending:
        _detach_log(_log_handler)
        return {"ok": 0, "failed": 0, "total": 0}
    from units.source_ledger import apply_clip_source_delta
    from units.real_source_policy import enabled as non_ai_enabled
    from units.motion_claims import acquire_clip_claim

    # Кэш деталей живёт ровно один прогон: ролик мог смениться качеством, а
    # держать его между прогонами незачем — пулы всё равно набираются заново.
    with _DETAIL_LOCK:
        _DETAIL_CACHE.clear()
    # Ограничение частоты — состояние прогона, а не программы: новый прогон
    # начинается с полной параллели и без унаследованной паузы.
    with _THROTTLE_LOCK:
        _THROTTLE["hits"] = 0.0
        _THROTTLE["until"] = 0.0
    with _API_UNITS_LOCK:
        _API_UNITS["search"] = 0
        _API_UNITS["videos"] = 0
    usage = _load_usage(project)
    output_dir = project / "vid_img"
    output_dir.mkdir(parents=True, exist_ok=True)
    fps = int(fps or getattr(config, "VIDEO_FPS", 60))
    max_fragment = max(
        0.5,
        float(getattr(config, "YOUTUBE_MAX_FRAGMENT_SEC", 3) or 3),
    )
    # Хронометраж фильма — знаменатель квоты «доля одного ролика».
    total_duration = float(plan.get("audioDuration") or 0)
    if total_duration <= 0:
        total_duration = max(
            (float(c.get("endTime", 0) or 0) for c in plan.get("clips", [])),
            default=0.0,
        )
    pool_cache: dict[str, list[dict[str, Any]]] = {}
    failures: dict[int, Any] = {}
    ok = failed = 0
    # Клипы независимы, а обходились СТРОГО по очереди: замер прогона 27.08 —
    # пятнадцать клипов, каждый начинался ровно тогда, когда закончился
    # предыдущий, по три минуты на клип. Пятьдесят семь минут на ютуб при ролике
    # в две минуты, и почти всё это — ожидание сети, процессор простаивал.
    #
    # Что при параллельной работе уже защищено и трогать не нужно:
    #   • леджер квот — файловой блокировкой внутри `_reserve_locked`;
    #   • двойная обработка клипа — `acquire_clip_claim`;
    #   • запись в план — `apply_clip_source_delta` (атомарно, с ретраями);
    #   • вызовы оператора — общим семафором `CODEX_MAX_PARALLEL`.
    # Здесь остаётся замок на кэш пулов и счётчики.
    stage_lock = threading.Lock()

    def _handle_clip(clip: dict[str, Any]) -> None:
        nonlocal ok, failed
        if stop_event is not None and stop_event.is_set():
            return
        if deadline_ts is not None and time.time() >= deadline_ts:
            return
        clip_id = int(clip.get("id"))
        claim = acquire_clip_claim(output_dir, clip_id, owner="youtube")
        if claim is None:
            # `None` означает ДВА разных случая: материал уже готов либо клип
            # занят другим процессом. Раньше оба молча пропускались, и готовая
            # сцена оставалась «youtube_pending» — её потом уводили архивам,
            # выбрасывая уже скачанное видео (аудит 25.08).
            from units.motion_claims import clip_media_exists

            if clip_media_exists(output_dir, clip_id):
                logging.info("  [YouTube] клип %s: материал уже готов — закрываю сцену", clip_id)
                from units.motion_claims import clip_parts_paths

                _ready = [f"vid_img/{item.name}"
                          for item in clip_parts_paths(output_dir, clip_id)]
                _ready_delta = {"source": "youtube",
                                "file": _ready[0] if _ready else f"vid_img/{clip_id:03d}.mp4"}
                if len(_ready) > 1:
                    _ready_delta["parts"] = _ready
                apply_clip_source_delta(
                    montage,
                    {clip_id: _ready_delta},
                    log=lambda message: print(f"  [YouTube] {message}"),
                )
                with stage_lock:
                    ok += 1
            else:
                logging.info("  [YouTube] клип %s: занят другим процессом — пропускаю", clip_id)
            return
        try:
            clip_dir = project / "youtube" / "clips" / f"{clip_id:03d}"
            context = _load_visual_context(project, clip)
            if context:
                # Папку создаём сами: раньше это делал только планировщик
                # запросов ПОЗЖЕ, и первый же непустой контекст (слой
                # непрерывности 22.08) ронял клип на записи (улов 23.08).
                clip_dir.mkdir(parents=True, exist_ok=True)
                (clip_dir / "context.json").write_text(
                    json.dumps(context, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            processed = False
            processed_result = None
            donor_candidates = (sources_by_clip or {}).get(clip_id) or []
            if donor_candidates:
                allowed, _blocked = _quota_filter_candidates(
                    usage,
                    donor_candidates,
                    clip_id,
                    total_duration,
                    donor_sourced=True,
                )
                if not allowed:
                    print(
                        f"  [YouTube] clip {clip_id}: donor sources are "
                        "blocked by quotas -> fresh search"
                    )
                else:
                    donor_queries = _clip_search_queries(
                        clip, _clip_text(project, clip)
                    )
                    try:
                        processed_result = _process_clip(
                            project,
                            clip,
                            allowed,
                            output_dir=output_dir,
                            usage=usage,
                            target_w=target_w,
                            target_h=target_h,
                            fps=fps,
                            max_fragment=max_fragment,
                            stop_event=stop_event,
                            deadline_ts=deadline_ts,
                            search_queries=donor_queries,
                            context=context,
                            total_duration=total_duration,
                            donor_sourced=True,
                        )
                        processed = True
                    except Exception as donor_exc:
                        if stop_event is not None and stop_event.is_set():
                            raise
                        if (
                            deadline_ts is not None
                            and time.time() >= deadline_ts
                        ):
                            raise
                        print(
                            f"  [YouTube] clip {clip_id}: donor sources "
                            f"failed ({donor_exc}) -> fresh search"
                        )
            if not processed:
                queries = _plan_search_queries(
                    project,
                    clip,
                    clip_dir,
                    context=context,
                )
                cache_key = json.dumps(queries, ensure_ascii=False)
                with stage_lock:
                    cached = pool_cache.get(cache_key)
                if cached is None:
                    # Сколько РОЛИКОВ искать — отдельно от размера пула кусков:
                    # раньше предел равнялся YOUTUBE_POOL_TARGET (4), два из
                    # четырёх отсеивались, и пул набивался окнами двух
                    # оставшихся, включая мусор (улов живого теста 25.08).
                    cached = _discover_candidates_multi(
                        queries,
                        max(
                            int(getattr(config, "YOUTUBE_POOL_TARGET", 6) or 6),
                            int(getattr(config, "YOUTUBE_DISCOVERY_LIMIT", 12) or 12),
                        ),
                        stop_event,
                        deadline_ts,
                    )
                    with stage_lock:
                        pool_cache[cache_key] = cached
                    pool_path = project / "youtube" / "pools" / (
                        f"{hashlib.md5(cache_key.encode()).hexdigest()}.json"
                    )
                    pool_path.parent.mkdir(parents=True, exist_ok=True)
                    pool_path.write_text(
                        json.dumps({
                            "queries": queries,
                            # По каждому запросу: сколько вернул YouTube, что
                            # оставлено, что отброшено и почему (25.08).
                            "discovery": {
                                q: _DISCOVERY_LOGS.get(q) for q in queries
                            },
                            "candidates": cached,
                        }, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                candidates = cached
                if not candidates:
                    raise RuntimeError(
                        "licensed YouTube discovery returned no candidates"
                    )
                processed_result = _process_clip(
                    project,
                    clip,
                    candidates,
                    output_dir=output_dir,
                    usage=usage,
                    target_w=target_w,
                    target_h=target_h,
                    fps=fps,
                    max_fragment=max_fragment,
                    stop_event=stop_event,
                    deadline_ts=deadline_ts,
                    search_queries=queries,
                    context=context,
                    total_duration=total_duration,
                )
            # Сцена может состоять из нескольких скачанных отрезков — тогда в
            # план идёт их СПИСОК, а склейка приводит куски к формату одним
            # проходом (`_process_clip_parts` в assemble.py). `file` при
            # этом остаётся: его читают места, которым нужен один путь.
            _delta = {"source": "youtube", "file": f"vid_img/{clip_id:03d}.mp4"}
            _parts = list((processed_result or {}).get("parts") or [])                 if isinstance(processed_result, dict) else []
            if _parts:
                _delta["file"] = _parts[0]
                if len(_parts) > 1:
                    _delta["parts"] = _parts
            apply_clip_source_delta(
                montage,
                {clip_id: _delta},
                log=lambda message: print(f"  [YouTube] {message}"),
            )
            with stage_lock:
                ok += 1
        except Exception as exc:
            with stage_lock:
                failed += 1
                failures[clip_id] = str(exc)
            failure_dir = (
                project / "youtube" / "clips" / f"{clip_id:03d}"
            )
            failure_dir.mkdir(parents=True, exist_ok=True)
            (failure_dir / "failure.json").write_text(
                json.dumps(
                    {"clip_id": clip_id, "error": str(exc)},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            if stop_event is not None and stop_event.is_set():
                return
            # Маркер провала — той стадии, которая сцену РЕАЛЬНО подберёт.
            # Раньше вне режима Non-AI ставился «stock_pending», но сток
            # набирает клипы по preferred_source плана (там остаётся «youtube»),
            # поэтому такую сцену он не видел никогда: метка висела мёртвой до
            # конца прогона (улов 25.08, клип 4). Архивы разбирают именно
            # `real_photo_pending`, а нейрогенерация подхватит в доборе.
            apply_clip_source_delta(
                montage,
                {clip_id: {"source": "real_photo_pending"}},
                log=lambda message: print(f"  [YouTube] {message}"),
            )
            if non_ai_enabled():
                try:
                    failures[clip_id] = {
                        "youtube_error": str(exc),
                        "epf_fallback": _run_epf_fallback(
                            project,
                            clip,
                            stop_event=stop_event,
                            deadline_ts=deadline_ts,
                            target_w=target_w,
                            target_h=target_h,
                        ),
                    }
                except Exception as fallback_exc:     # noqa: BLE001
                    logging.warning("  [YouTube] клип %s: ютуб упал (%s), запасной EPF тоже (%s) "
                                    "— клип уходит в добор", clip_id,
                                    str(exc)[:120], str(fallback_exc)[:120])
                    with stage_lock:
                        failures[clip_id] = {
                            "youtube_error": str(exc),
                            "epf_fallback_error": str(fallback_exc),
                        }
        finally:
            claim.release()

    # Версия yt-dlp — первым делом: он не едет с обновлениями, и старый
    # бинарник даёт 403 на ровном месте. Пусть это будет видно в логе ДО
    # первых отказов, а не выясняется потом расспросами (жалоба 29.08).
    log_ytdlp_version(printer=print)

    workers = max(1, min(
        len(pending),
        int(getattr(config, "YOUTUBE_CLIP_PARALLEL", 4) or 4),
    ))
    if workers > 1:
        logging.info("  [YouTube] клипов %s, обрабатываю по %s одновременно "
                     "(запросов к ютубу разом — не больше %s)",
                     len(pending), workers, _meta_workers(64))
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(_handle_clip, pending))
    else:
        for clip in pending:
            _handle_clip(clip)
    # Лог стадии закрываем на КАЖДОМ выходе: GUI живёт часами, оставленный
    # хендлер продолжил бы писать в файл прошлого проекта.
    if _log_path:
        print(f"  [YouTube] полный лог стадии: {_log_path}")
    _detach_log(_log_handler)
    return {
        "ok": ok,
        "failed": failed,
        "total": len(pending),
        "reasons": failures,
    }
