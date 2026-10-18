"""Страница «Настройки»: вход в Codex, сессия CinAI, ключи и параметры движка, внешний вид."""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QScrollArea, QSpinBox, QToolButton, QVBoxLayout, QWidget,
)
from qfluentwidgets import DoubleSpinBox, LineEdit, ListWidget, SmoothScrollArea, SpinBox
from ..ui.widgets import Combo, Toggle, card_icon

from .. import catalog, engine_link, envfile, i18n, paths, theme
from ..i18n import T, tr
from ..services import monitors as mon
from ..services import procs
from ..ui.widgets import LABEL_GAP, Card, Chip, button, label, set_tip

# Описание поля: (ключ, подпись, подсказка, тип, опции)
#   тип: secret | text | int | float | bool | combo | path | dir
S = "secret"


def fld(key, text, tip=None, kind="text", options=None, lo=0, hi=999999, step=1.0, placeholder=None):
    return {"key": key, "label": text, "tip": tip, "kind": kind, "options": options, "lo": lo, "hi": hi, "step": step,
            "placeholder": placeholder}


SECTIONS = [
    ("codex", T("Codex (OpenAI)", "Codex (OpenAI)"), "◎", [
        fld("CODEX_EXE_PATH", T("Путь к codex.exe", "Path to codex.exe"),
            T("Необязательно. Пусто — своя копия в runtime/codex, затем системный PATH.",
              "Optional. Empty — own copy in runtime/codex, then system PATH."), "path"),
    ]),
    ("cinai", T("CinAI", "CinAI"), "◈", [
        fld("CINAI_ACCESS_TOKEN", T("Access-токен", "Access token"),
            T("Заполняется автоматически кнопкой «Забрать сессию из браузера».",
              "Filled automatically by “Import browser session”."), S),
        fld("CINAI_REFRESH_TOKEN", T("Refresh-токен", "Refresh token"),
            T("Нужен для продления сессии без повторного входа.", "Used to extend the session without re-login."), S),
        fld("CINAI_API_KEY", T("API-ключ", "API key"), T("Необязательно, если есть токены.", "Optional when tokens exist."), S),
        fld("CINAI_TOKEN_PREFRESH_HOURS", T("Продлевать за, ч", "Refresh ahead, h"),
            T("Продлевать сессию, когда до конца осталось меньше этого числа часов.",
              "Extend the session when fewer hours than this remain."), "float", lo=1, hi=72),
    ]),
    ("ai", T("Распознавание речи", "Speech recognition"), "✦", [
        fld("ASSEMBLYAI_API_KEY", "AssemblyAI API key",
            T("Расшифровка речи. Ключ: assemblyai.com → Dashboard.", "Speech transcription. Key: assemblyai.com → Dashboard."), S),
    ]),
    ("fastgen", T("FastGen", "FastGen"), "⚡", [
        fld("FASTGEN_API_KEY", "FastGen API key", T("Генерация картинок и Flow Ultra.", "Image generation and Flow Ultra."), S),
    ]),
    ("veo", T("VeoNonStop", "VeoNonStop"), "▶", [
        fld("VEONONSTOP_API_KEY", "VeoNonStop API key",
            T("Ключ VeoNonStop (картинки Banana и видео VEO).", "VeoNonStop key (Banana images and VEO video)."), S),
        fld("VEONONSTOP_API_BASE", T("Сервер API", "API server"),
            T("Адрес API: v1 или v2. Пусто — по умолчанию.", "API address: v1 or v2. Empty — default."), "combo",
            [""] + list(catalog.VEO_BASE_URLS)),
        fld("VEONONSTOP_POLL_TIMEOUT", T("Ожидание результата, с", "Result wait, s"), None, "int", lo=60, hi=7200),
    ]),
    ("vertex", T("Vertex AI", "Vertex AI"), "◈", [
        fld("VERTEX_API_KEY", "Vertex AI API key",
            T("API-ключ Google Cloud / Vertex AI (картинки Banana и видео Veo).",
              "Google Cloud / Vertex AI API key (Banana images and Veo video)."), S),
        fld("VERTEX_PROJECT_ID", "PROJECT_ID",
            T("Идентификатор проекта Google Cloud, к которому относится ключ.",
              "ID of the Google Cloud project the key belongs to.")),
        fld("VERTEX_LOCATION", T("Регион", "Region"),
            T("Регион запросов, например us-central1 или global. Пусто — us-central1.",
              "Request region, e.g. us-central1 or global. Empty — us-central1.")),
    ]),
    ("cheapvibe", T("CheapVibeCode", "CheapVibeCode"), "◇", [
        fld("CHEAPVIBE_API_KEY", "CheapVibeCode API key",
            T("Ключ sk-cvc-… для картинок GPT Image 2, Grok Imagine и Banana 2\n"
              "(Конвейер → Генерация картинок → CheapVibeCode).",
              "sk-cvc-… key for GPT Image 2, Grok Imagine and Banana 2 images\n"
              "(Pipeline → Image generation → CheapVibeCode)."), S),
        fld("CHEAPVIBE_API_BASE", T("Адрес API", "API address"),
            T("Необязательно. Пусто — https://ru.cheapvibecode.ru/v1.",
              "Optional. Empty — https://ru.cheapvibecode.ru/v1.")),
    ]),
    ("royaltechno", T("RoyalTechno", "RoyalTechno"), "◆", [
        fld("ROYALTECHNO_API_KEY", "RoyalTechno API key",
            T("Ключ rt_live_… для видео Veo 3.1 (720p, $0.02 за клип). Выпускается в Telegram-боте\n"
              "RoyalTechno командой /api_key_new. Конвейер → Генерация видео → RoyalTechno.",
              "rt_live_… key for Veo 3.1 video (720p, $0.02 per clip). Issued in the RoyalTechno\n"
              "Telegram bot with /api_key_new. Pipeline → Video generation → RoyalTechno."), S),
        fld("ROYALTECHNO_API_BASE", T("Адрес API", "API address"),
            T("Необязательно. Пусто — https://api.royaltechno.cc.",
              "Optional. Empty — https://api.royaltechno.cc.")),
    ]),
    ("varag", T("VARAG", "VARAG"), "◆", [
        fld("VARAG_API_KEY", "VARAG API key",
            T("Ключ mk_… сервиса VARAG (Inflight API): картинки и видео. Что доступно — картинки,\n"
              "видео или оба — решает тариф ключа. Конвейер → Генерация картинок / Генерация видео /\n"
              "Инфографика → VARAG.",
              "VARAG (Inflight API) mk_… key: images and video. The key's plan decides what is\n"
              "available — images, video or both. Pipeline → Image generation / Video generation /\n"
              "Infographics → VARAG."), S),
        fld("VARAG_API_BASE", T("Адрес API", "API address"),
            T("Необязательно. Пусто — https://api.varag.video/v1.",
              "Optional. Empty — https://api.varag.video/v1.")),
    ]),
    ("stock", T("Сток", "Stock"), "▦", [
        fld("PEXELS_API_KEY", "Pexels API key",
            T("Бесплатно: pexels.com/api. Если ключ указан, Pexels ищет через API,\n"
              "временный API-ключ ниже тогда не используется.",
              "Free: pexels.com/api. When set, Pexels searches via the API\n"
              "and the temporary API key below is not used."), S),
        fld("PEXELS_TEMP_KEY", T("Временный API-ключ", "Temporary API key"),
            T("Поиск Pexels (фото и видео) без обычного ключа — через данные сайта:\n"
              "  pexels.com/_next/data/<ключ>/ru-RU/search/….json\n"
              "<ключ> — строка вида DxM6xto7574x9jRaCa2a8, сайт меняет её с каждым обновлением.\n"
              "Можно вставить и всю ссылку целиком — ключ возьмётся из неё.\n"
              "Поле можно оставить пустым: пустой или устаревший ключ программа сама\n"
              "берёт с pexels.com. Работает, только когда Pexels API key пуст.",
              "Pexels search (photos and videos) without a regular key — via the site data:\n"
              "  pexels.com/_next/data/<key>/ru-RU/search/….json\n"
              "<key> is a string like DxM6xto7574x9jRaCa2a8; the site changes it with every update.\n"
              "You can paste the whole link — the key is taken from it.\n"
              "The field may stay empty: an empty or outdated key is fetched from\n"
              "pexels.com automatically. Used only when Pexels API key is empty."),
            "text", placeholder="DxM6xto7574x9jRaCa2a8"),
        fld("PIXABAY_API_KEY", "Pixabay API key", T("Бесплатно: pixabay.com/api/docs.", "Free: pixabay.com/api/docs."), S),
        fld("STOCK_PROXY_URL", T("Прокси для стоков", "Stock proxy"),
            T("Необязательно. Через этот прокси идут запросы к стокам и скачивание их файлов.\n"
              "Формат — HTTP-прокси:\n"
              "  http://host:port\n"
              "  http://логин:пароль@host:port\n"
              "Если в логине или пароле есть спецсимволы, запишите их кодами: @ → %40, : → %3A, / → %2F.\n"
              "SOCKS-прокси не поддерживается. Пусто — без прокси.",
              "Optional. Stock requests and file downloads go through this proxy.\n"
              "Format — HTTP proxy:\n"
              "  http://host:port\n"
              "  http://user:password@host:port\n"
              "Special characters in the login or password must be percent-encoded: @ → %40, : → %3A, / → %2F.\n"
              "SOCKS proxies are not supported. Empty — no proxy."),
            "text", placeholder="http://user:password@host:port"),
    ]),
]


def _jwt_email(token: str) -> str:
    """Почта из id_token Codex (без проверки подписи — только для показа)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return str(data.get("email") or data.get("https://api.openai.com/profile", {}).get("email") or "")
    except Exception:  # noqa: BLE001
        return ""


def codex_account() -> dict:
    """Состояние входа в Codex: {'ok', 'email', 'mode', 'path'}."""
    try:
        creds = engine_link.unit("codex_credentials")
        auth = creds.load_auth() or {}
        path = str(creds.auth_file())
    except Exception:  # noqa: BLE001
        auth, path = {}, str(Path.home() / ".codex" / "auth.json")
    tokens = auth.get("tokens") or {}
    email = _jwt_email(str(tokens.get("id_token") or ""))
    if tokens.get("access_token") or tokens.get("id_token"):
        return {"ok": True, "email": email, "mode": "chatgpt", "path": path}
    if auth.get("OPENAI_API_KEY"):
        return {"ok": True, "email": "", "mode": "api_key", "path": path}
    return {"ok": False, "email": "", "mode": "", "path": path}


class _Bridge(QObject):
    """Передача результатов фоновых потоков в интерфейс."""
    text = Signal(str, str)          # (куда, текст)
    done = Signal(str, bool, str)    # (что, успех, текст)


class SecretEdit(QWidget):
    """Поле ключа со скрытием и кнопкой «показать»."""

    def __init__(self):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.edit = LineEdit()
        self.edit.setEchoMode(LineEdit.Password)
        lay.addWidget(self.edit, 1)
        eye = QToolButton()
        eye.setText("👁")
        eye.setCheckable(True)
        eye.setCursor(Qt.PointingHandCursor)
        set_tip(eye, T("Показать / скрыть", "Show / hide"))
        eye.toggled.connect(lambda on: self.edit.setEchoMode(LineEdit.Normal if on else LineEdit.Password))
        lay.addWidget(eye)


def _stack(caption, control) -> QVBoxLayout:
    """Подпись сверху, поле под ней."""
    box = QVBoxLayout()
    box.setSpacing(LABEL_GAP)
    box.addWidget(caption)
    box.addWidget(control)
    return box


class SettingsPage(QWidget):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self._loading = True
        self._baseline: dict = {}
        self.bridge = _Bridge()
        self.bridge.text.connect(self._on_text)
        self.bridge.done.connect(self._on_done)
        self.fields: dict[str, dict] = {}
        self.rows: dict[str, QWidget] = {}
        self.cards: dict[str, Card] = {}
        self._build()
        self.load_values()
        app.monitors.codex_quota.connect(self._on_quota)
        app.monitors.cinai_status.connect(self._on_cinai)
        QTimer.singleShot(400, self.refresh_codex_state)

    # ================================================================ сборка
    def _build(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(12)

        # левая колонка: разделы и поиск
        nav = QFrame()
        nav.setObjectName("Panel")
        nav.setFixedWidth(220)
        nl = QVBoxLayout(nav)
        nl.setContentsMargins(10, 10, 10, 10)
        self.search = LineEdit()
        self.search.setPlaceholderText(tr(T("Поиск параметра…", "Search setting…")))
        self.search.setClearButtonEnabled(True)
        set_tip(self.search, T("Фильтр по названию, ключу .env или подсказке.", "Filter by name, .env key or tooltip."))
        self.search.textChanged.connect(self._filter)
        nl.addWidget(self.search)
        self.nav = ListWidget()
        self.nav.setObjectName("Transparent")
        head_items = [("look", T("Интерфейс", "Interface"), "◐"), ("updates", T("Обновления", "Updates"), "⟳")]
        for key, title, icon in head_items + [(k, t, i) for k, t, i, _ in SECTIONS]:
            item = QListWidgetItem(card_icon("set_" + key).icon(), tr(title))
            item.setData(Qt.UserRole, key)
            self.nav.addItem(item)
        self.nav.currentItemChanged.connect(lambda it, _p: it and self._scroll_to(it.data(Qt.UserRole)))
        nl.addWidget(self.nav, 1)
        nl.addWidget(button(T("Импорт ключей из .env…", "Import keys from .env…"), "",
                            T("Взять непустые значения известных параметров из другого файла .env\n"
                              "(например, из прежней установки). Перед записью можно проверить.",
                              "Take non-empty values of known settings from another .env file\n"
                              "(e.g. a previous install). You can review before saving."), self.import_env))
        nl.addWidget(button(T("Открыть .env", "Open .env"), "ghost", T("Открыть файл настроек в редакторе.",
                                                                       "Open the settings file in an editor."),
                            lambda: procs.open_path(envfile.ensure_env_file())))
        root.addWidget(nav)

        # правая часть: карточки с прокруткой + нижняя панель
        right = QVBoxLayout()
        right.setSpacing(8)
        self.scroll = SmoothScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setObjectName("Transparent")
        body = QWidget()
        body.setObjectName("Transparent")
        self.body = QVBoxLayout(body)
        self.body.setContentsMargins(0, 0, 18, 0)
        self.body.setSpacing(12)
        self.body.addWidget(self._build_look())
        self.body.addWidget(self._build_updates())
        for key, title, icon, items in SECTIONS:
            card = Card(title, icon, key=f"set_{key}", collapsible=False)
            if key == "codex":
                self._build_codex(card)
            elif key == "cinai":
                self._build_cinai(card)
            self._add_fields(card, items)
            self.cards[key] = card
            self.body.addWidget(card)
        self.body.addStretch(1)
        self.scroll.setWidget(body)
        right.addWidget(self.scroll, 1)

        bar = QHBoxLayout()
        self.save_state = label("", "muted")
        bar.addWidget(self.save_state, 1)
        bar.addWidget(button(T("Перечитать", "Reload"), "", T("Отменить несохранённые изменения.", "Discard unsaved changes."),
                             self.load_values, icon="SYNC"))
        bar.addWidget(button(T("Сохранить", "Save"), "primary", T("Записать изменения в engine/.env (Ctrl+S).",
                                                                  "Write changes to engine/.env (Ctrl+S)."), self.save, icon="SAVE"))
        right.addLayout(bar)
        root.addLayout(right, 1)

    def _build_look(self) -> Card:
        card = Card(T("Интерфейс", "Interface"), "◐", key="set_look", collapsible=False)
        self.cards["look"] = card
        row = QHBoxLayout()
        self.lang = Combo()
        for code, name in i18n.LANGUAGES:
            self.lang.addItem(name, code)
        self.lang.setCurrentIndex(0 if i18n.language() == "ru" else 1)
        self.theme = Combo()
        self.theme.addItem(tr(T("Тёмная", "Dark")), "dark")
        self.theme.addItem(tr(T("Светлая", "Light")), "light")
        self.theme.setCurrentIndex(0 if theme.name() == "dark" else 1)
        row.addLayout(_stack(label(T("Язык", "Language"), "label", T("Язык интерфейса. Окно перестроится сразу.",
                                                                    "Interface language. The window rebuilds at once.")),
                             self.lang))
        row.addSpacing(24)
        row.addLayout(_stack(label(T("Тема", "Theme")), self.theme))
        row.addStretch(1)
        self.lang.currentIndexChanged.connect(self._apply_look)
        self.theme.currentIndexChanged.connect(self._apply_look)
        card.add(row)
        card.add(label(T("Настройки интерфейса сохраняются сразу; ключи и параметры — кнопкой «Сохранить».",
                         "Interface settings apply at once; keys and parameters — with “Save”."), "muted", wrap=True))
        return card

    def _build_updates(self) -> Card:
        from .. import APP_VERSION
        from ..services import updater
        card = Card(T("Обновления", "Updates"), "⟳", key="set_updates", collapsible=False)
        self.cards["updates"] = card
        ctl = self.app.updates
        row = QHBoxLayout()
        where = (T("установленная программа", "installed program") if updater.is_installed()
                 else T("рабочая копия — обновления не ставятся", "working copy — updates are not applied"))
        row.addWidget(label(T(f"Версия {APP_VERSION}", f"Version {APP_VERSION}"), "label",
                            T("Выпуски публикуются на GitHub: " + updater.RELEASES_PAGE,
                              "Releases are published on GitHub: " + updater.RELEASES_PAGE)))
        self.update_state = Chip(tr(where))
        row.addWidget(self.update_state)
        row.addStretch(1)
        row.addWidget(button(T("Проверить обновления", "Check for updates"), "primary",
                             T("Узнать, есть ли новая версия, и предложить установить её.",
                               "See whether a new version exists and offer to install it."),
                             lambda: ctl.check(manual=True), icon="SYNC"))
        row.addWidget(button(T("Все выпуски", "All releases"), "ghost", T("Открыть страницу выпусков на GitHub.",
                                                                          "Open the releases page on GitHub."),
                             lambda: procs.open_path(updater.RELEASES_PAGE)))
        card.add(row)
        self.update_auto = Toggle(tr(T("Проверять при запуске", "Check on start")))
        self.update_auto.setChecked(ctl.auto_check())
        self.update_auto.toggled.connect(ctl.set_auto_check)
        set_tip(self.update_auto, T("Через несколько секунд после запуска программа проверит новую версию\n"
                                    "и, если она есть, предложит скачать и установить.",
                                    "A few seconds after start the program checks for a new version\n"
                                    "and offers to download and install it."))
        card.add(self.update_auto)
        card.add(label(T("Проекты, ключи (.env), пресеты, журналы и изменённые вами промпты при обновлении сохраняются.",
                         "Projects, keys (.env), presets, logs and prompts you edited are kept when updating."),
                       "muted", wrap=True))
        ctl.status.connect(lambda text: self.update_state.show_state(text, "warn" if "✖" in text else "ok"))
        return card

    def _build_codex(self, card: Card) -> None:
        self.codex_state = Chip(tr(T("проверяю…", "checking…")))
        self.codex_version = label("", "muted")
        head = QHBoxLayout()
        head.addWidget(label(T("Аккаунт", "Account"), "label",
                             T("Codex работает по подписке ChatGPT (Plus/Pro/Business). Вход один раз —\n"
                               "учётные данные хранятся в ~/.codex/auth.json.",
                               "Codex works via a ChatGPT subscription (Plus/Pro/Business). Sign in once —\n"
                               "credentials are stored in ~/.codex/auth.json.")))
        head.addWidget(self.codex_state)
        head.addWidget(self.codex_version, 1)
        card.add(head)
        self.quota = label("", "muted")
        card.add(self.quota)
        btns = QHBoxLayout()
        btns.addWidget(button(T("Войти через браузер", "Sign in via browser"), "primary",
                              T("Откроется окно терминала и страница входа OpenAI.", "A terminal and the OpenAI sign-in page open."),
                              lambda: self.codex_login(False)))
        btns.addWidget(button(T("Вход по коду", "Sign in with code"), "",
                              T("Для удалённых машин: показывается код, который вводится на любом устройстве.",
                                "For remote machines: shows a code you enter on any device."), lambda: self.codex_login(True)))
        btns.addWidget(button(T("Установить / обновить", "Install / update"), "",
                              T("Скачать последнюю версию Codex CLI в runtime/codex.", "Download the latest Codex CLI into runtime/codex."),
                              self.codex_install))
        btns.addWidget(button(T("Квота", "Quota"), "small", T("Обновить квоту подписки.", "Refresh subscription quota."),
                              self.app.monitors.refresh_codex))
        btns.addWidget(button(T("Папка", "Folder"), "small", T("Открыть ~/.codex.", "Open ~/.codex."),
                              lambda: procs.open_path(Path(codex_account()["path"]).parent)))
        btns.addStretch(1)
        card.add(btns)
        # прокси для Codex: одна строка, тот же формат, что у «Прокси для стоков»
        card.add(label(T("Прокси Codex", "Codex proxy"), "label",
                       T("Необязательно. Нужен, если OpenAI недоступен из вашей сети.\n"
                         "Формат: схема://логин:пароль@host:port, например:\n"
                         "  http://host:port\n"
                         "  http://логин:пароль@host:port\n"
                         "  socks5://логин:пароль@host:port\n"
                         "Без схемы считается http. Если в логине или пароле есть спецсимволы, запишите их\n"
                         "кодами: @ → %40, : → %3A, / → %2F. Пусто — без прокси.",
                         "Optional. Needed when OpenAI is unreachable from your network.\n"
                         "Format: scheme://login:password@host:port, for example:\n"
                         "  http://host:port\n"
                         "  http://login:password@host:port\n"
                         "  socks5://login:password@host:port\n"
                         "No scheme means http. Special characters in the login or password must be\n"
                         "percent-encoded: @ → %40, : → %3A, / → %2F. Empty — no proxy.")))
        prow = QHBoxLayout()
        self.proxy = LineEdit()
        self.proxy.setPlaceholderText("http://user:password@host:port")
        self.proxy.textChanged.connect(self._mark_dirty)
        prow.addWidget(self.proxy, 1)
        prow.addWidget(button(T("Проверить", "Test"), "small", T("Узнать внешний IP через прокси.", "Get external IP via the proxy."),
                              self.test_proxy))
        card.add(prow)
        self.proxy_state = label("", "muted")
        card.add(self.proxy_state)

    def _build_cinai(self, card: Card) -> None:
        head = QHBoxLayout()
        self.cinai_state = Chip(tr(T("нет данных", "no data")))
        head.addWidget(label(T("Сессия", "Session")))
        head.addWidget(self.cinai_state)
        head.addStretch(1)
        card.add(head)
        btns = QHBoxLayout()
        btns.addWidget(button(T("Открыть cinai.tv", "Open cinai.tv"), "",
                              T("Войдите на сайте в браузере, затем заберите сессию.", "Sign in on the site, then import the session."),
                              self.cinai_open))
        btns.addWidget(button(T("Забрать сессию из браузера", "Import browser session"), "primary",
                              T("Найти токены CinAI в Firefox/Chrome/Edge, проверить на сервере и записать в .env.",
                                "Find CinAI tokens in Firefox/Chrome/Edge, verify on the server and write to .env."),
                              self.cinai_import))
        btns.addWidget(button(T("Баланс", "Balance"), "small", None, self.app.monitors.refresh_cinai))
        btns.addStretch(1)
        card.add(btns)

    def _add_fields(self, card: Card, items: list) -> None:
        """Короткие поля (числа, списки) идут по три в ряд, длинные (ключи, пути) — на всю ширину."""
        narrow: list = []

        def flush() -> None:
            if not narrow:
                return
            grid = QGridLayout()
            grid.setHorizontalSpacing(24)
            grid.setVerticalSpacing(12)
            for i, row in enumerate(narrow):
                grid.addWidget(row, i // 3, i % 3, Qt.AlignLeft | Qt.AlignTop)
            for col in range(3):
                grid.setColumnMinimumWidth(col, 250)   # компактные колонки одинаковой ширины
                grid.setColumnStretch(col, 0)
            grid.setColumnStretch(3, 1)                # свободное место уходит вправо
            card.add(grid)
            narrow.clear()

        for spec in items:
            row = self._field_row(spec)
            if spec["kind"] in ("int", "float", "combo", "bool"):
                narrow.append(row)
            else:
                flush()
                card.add(row)
        flush()

    def _field_row(self, spec: dict) -> QWidget:
        row = QWidget()
        outer = QVBoxLayout(row)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(LABEL_GAP)
        lay = QHBoxLayout()
        lay.setContentsMargins(0, 0, 0, 0)
        tip = spec["tip"]
        full_tip = (tr(tip) + "\n\n" if tip else "") + f".env: {spec['key']}"
        outer.addWidget(label(spec["label"], "label", full_tip))
        outer.addLayout(lay)
        kind = spec["kind"]
        default = engine_link.cfg(spec["key"], None)
        if kind == S:
            w = SecretEdit()
            w.edit.textChanged.connect(self._mark_dirty)
        elif kind in ("text", "path", "dir"):
            w = LineEdit()
            if spec.get("placeholder"):
                w.setPlaceholderText(spec["placeholder"])
            elif default not in (None, "") and not isinstance(default, (list, dict)):
                w.setPlaceholderText(str(default))
            w.textChanged.connect(self._mark_dirty)
        elif kind == "int":
            w = SpinBox()
            w.setRange(int(spec["lo"]), int(spec["hi"]))
            w.valueChanged.connect(self._mark_dirty)
        elif kind == "float":
            w = DoubleSpinBox()
            w.setRange(float(spec["lo"]), float(spec["hi"]))
            w.setSingleStep(float(spec["step"]) if spec["step"] != 1.0 else 0.5)
            w.setDecimals(2)
            w.valueChanged.connect(self._mark_dirty)
        elif kind == "bool":
            w = Toggle()
            w.toggled.connect(self._mark_dirty)
        else:
            w = Combo()
            for opt in spec["options"] or []:
                value, text = opt if isinstance(opt, tuple) else (opt, opt)
                w.addItem(tr(text) if text else tr(T("(по умолчанию)", "(default)")), value)
            w.currentIndexChanged.connect(self._mark_dirty)
        set_tip(w, full_tip)
        if kind == "combo":
            w.setMaximumWidth(380)
        lay.addWidget(w, 1 if kind not in ("int", "float", "bool", "combo") else 0)
        if kind == "combo":
            lay.addStretch(1)
        if kind in ("path", "dir"):
            def browse(_=False, edit=w, k=kind):
                path = (QFileDialog.getExistingDirectory(self, tr(spec["label"])) if k == "dir"
                        else QFileDialog.getOpenFileName(self, tr(spec["label"]))[0])
                if path:
                    edit.setText(path)
            lay.addWidget(button("…", "small", None, browse))
        if kind in ("int", "float", "bool"):
            lay.addStretch(1)
        self.fields[spec["key"]] = {"spec": spec, "widget": w}
        row.setProperty("search", " ".join([tr(spec["label"]), spec["key"], tr(tip) if tip else ""]).lower())
        self.rows[spec["key"]] = row
        return row

    # ============================================================ значения
    def _set_widget(self, key: str, raw: str | None) -> None:
        f = self.fields[key]
        w, kind = f["widget"], f["spec"]["kind"]
        default = engine_link.cfg(key, None)
        value = raw if raw not in (None, "") else None
        if kind == S:
            w.edit.setText(value or "")
        elif kind in ("text", "path", "dir"):
            w.setText(value or "")
        elif kind in ("int", "float"):
            src = value if value is not None else default
            try:
                num = float(src) if src not in (None, "") else w.minimum()
            except (TypeError, ValueError):
                num = w.minimum()
            w.setValue(int(num) if kind == "int" else num)
        elif kind == "bool":
            src = value if value is not None else default
            w.setChecked(str(src).strip().lower() in ("1", "true", "yes", "on"))
        else:
            src = value if value is not None else (default if isinstance(default, str) else "")
            idx = w.findData(src)
            if idx < 0 and src:
                w.addItem(str(src), src)
                idx = w.count() - 1
            w.setCurrentIndex(max(0, idx))

    def _get_widget(self, key: str) -> str:
        f = self.fields[key]
        w, kind = f["widget"], f["spec"]["kind"]
        if kind == S:
            return w.edit.text().strip()
        if kind in ("text", "path", "dir"):
            return w.text().strip()
        if kind == "int":
            return str(w.value())
        if kind == "float":
            return f"{w.value():g}"
        if kind == "bool":
            return "true" if w.isChecked() else "false"
        return str(w.currentData() or "")

    def load_values(self) -> None:
        self._loading = True
        env = envfile.read_env(envfile.ensure_env_file())
        for key in self.fields:
            self._set_widget(key, env.get(key))
        self.proxy.setText(env.get("CODEX_PROXY", ""))
        self._baseline = self._collect()
        self._loading = False
        self.save_state.setText(tr(T(f"Файл: {paths.ENV_FILE}", f"File: {paths.ENV_FILE}")))

    def _collect(self) -> dict:
        values = {key: self._get_widget(key) for key in self.fields}
        rm = engine_link.unit("relay_manager")
        raw = self.proxy.text().strip()
        scheme = raw.split("://", 1)[0].lower() if "://" in raw else "http"
        values["CODEX_PROXY"] = rm.apply_proxy_scheme(raw, scheme)
        return values

    def _mark_dirty(self, *_):
        if getattr(self, "_loading", False):
            return
        changed = [k for k, v in self._collect().items() if self._baseline.get(k) != v]
        self.save_state.setText(tr(T(f"Изменено параметров: {len(changed)} — не забудьте сохранить",
                                     f"Changed settings: {len(changed)} — don't forget to save")) if changed
                                else tr(T("Нет изменений", "No changes")))
        self.save_state.setProperty("role", "warn" if changed else "muted")
        self.save_state.style().unpolish(self.save_state)
        self.save_state.style().polish(self.save_state)

    def save(self) -> None:
        current = self._collect()
        updates = {k: v for k, v in current.items() if self._baseline.get(k) != v}
        if not updates:
            self.save_state.setText(tr(T("Нет изменений", "No changes")))
            return
        envfile.write_env(updates, section_title="BegoFrame · Settings")
        engine_link.reload_params()
        self._baseline = current
        self._mark_dirty()
        self.save_state.setText(tr(T(f"Сохранено: {len(updates)}", f"Saved: {len(updates)}")))
        self.app.toast(tr(T("Настройки сохранены", "Settings saved")))
        # ключи могли смениться — обновить мониторы
        self.app.monitors.refresh_veo_keys()
        self.app.monitors.refresh_veo()
        self.app.monitors.refresh_cinai()
        QTimer.singleShot(200, self.refresh_codex_state)

    def has_unsaved(self) -> bool:
        return any(self._baseline.get(k) != v for k, v in self._collect().items())

    def import_env(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, tr(T("Файл .env", ".env file")), "", ".env (*.env .env*);;All (*.*)")
        if not path:
            return
        other = envfile.read_env(Path(path))
        taken = [k for k in self.fields if other.get(k)]
        if other.get("CODEX_PROXY"):
            self.proxy.setText(other["CODEX_PROXY"])
            taken.append("CODEX_PROXY")
        for key in taken:
            if key in self.fields:
                self._set_widget(key, other[key])
        self._mark_dirty()
        QMessageBox.information(self, tr(T("Импорт", "Import")),
                                tr(T(f"Подставлено значений: {len(taken)}.\nПроверьте и нажмите «Сохранить».",
                                     f"Values filled in: {len(taken)}.\nReview and press “Save”.")))

    def _filter(self, text: str) -> None:
        q = text.strip().lower()
        for key, row in self.rows.items():
            row.setVisible(not q or q in row.property("search"))
        for skey, card in self.cards.items():
            if skey == "look":
                card.setVisible(not q)
                continue
            keys = [s["key"] for k, _t, _i, items in SECTIONS if k == skey for s in items]
            card.setVisible(not q or any(self.rows[k].isVisible() for k in keys) or skey in ("codex", "cinai") and
                            q in tr(dict((k, t) for k, t, _i, _it in SECTIONS)[skey]).lower())

    def _scroll_to(self, key: str) -> None:
        card = self.cards.get(key)
        if card:
            self.scroll.ensureWidgetVisible(card, 0, 0)
            self.scroll.verticalScrollBar().setValue(card.y())

    # ============================================================ внешний вид
    def _apply_look(self, *_):
        lang = self.lang.currentData()
        th = self.theme.currentData()
        if lang == i18n.language() and th == theme.name():
            return
        if not self.app.apply_appearance(lang, th):
            # окно занято задачами — вернуть прежние значения
            self.lang.blockSignals(True)
            self.theme.blockSignals(True)
            self.lang.setCurrentIndex(0 if i18n.language() == "ru" else 1)
            self.theme.setCurrentIndex(0 if theme.name() == "dark" else 1)
            self.lang.blockSignals(False)
            self.theme.blockSignals(False)

    # ================================================================ Codex
    def refresh_codex_state(self) -> None:
        acc = codex_account()
        if acc["ok"]:
            who = acc["email"] or (tr(T("API-ключ", "API key")) if acc["mode"] == "api_key" else "ChatGPT")
            self.codex_state.show_state(tr(T(f"вход выполнен: {who}", f"signed in: {who}")), "ok", acc["path"])
        else:
            self.codex_state.show_state(tr(T("не выполнен вход", "not signed in")), "err", acc["path"])

        def work():
            exe = str(engine_link.cfg("CODEX_EXE_PATH", "") or "")
            version = ""
            if exe and Path(exe).exists():
                try:
                    out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10,
                                         creationflags=procs.CREATE_NO_WINDOW)
                    version = out.stdout.strip()
                except Exception:  # noqa: BLE001
                    version = ""
            self.bridge.text.emit("codex_version", version or tr(T("Codex CLI не найден — нажмите «Установить»",
                                                                   "Codex CLI not found — press “Install”")))
        threading.Thread(target=work, daemon=True).start()

    def _on_quota(self, data: dict) -> None:
        q = mon.quota_texts(data)
        if q["ok"]:
            self.quota.setText(tr(T(f"Квота ({q['plan']}): 5 ч — {q['p_pct']}% (сброс через {q['p_reset']}), "
                                    f"неделя — {q['s_pct']}% (сброс через {q['s_reset']})",
                                    f"Quota ({q['plan']}): 5 h — {q['p_pct']}% (resets in {q['p_reset']}), "
                                    f"week — {q['s_pct']}% (resets in {q['s_reset']})")))
        else:
            self.quota.setText(tr(T("Квота недоступна: ", "Quota unavailable: ")) + q["error"][:120])

    def codex_login(self, device: bool) -> None:
        exe = str(engine_link.cfg("CODEX_EXE_PATH", "") or "")
        if not exe or not Path(exe).exists():
            if QMessageBox.question(self, "Codex", tr(T("Codex CLI не установлен. Скачать сейчас?",
                                                        "Codex CLI is not installed. Download now?"))) == QMessageBox.Yes:
                self.codex_install()
            return
        # учётные данные — строго в файле, чтобы квоту читали все процессы
        args = [exe, "login", "-c", 'cli_auth_credentials_store="file"']
        if device:
            args.append("--device-auth")
        env = engine_link.unit("codex_credentials").codex_env()
        proxy = self._collect().get("CODEX_PROXY", "")
        if proxy:
            env.update(HTTPS_PROXY=proxy, HTTP_PROXY=proxy, ALL_PROXY=proxy)
        env["NO_COLOR"] = "1"
        if sys.platform == "win32":
            subprocess.Popen(f'start "Codex Login" cmd /k {subprocess.list2cmdline(args)}', shell=True,
                             cwd=str(Path(exe).parent), env=env)
        else:
            subprocess.Popen(args, cwd=str(Path(exe).parent), env=env)
        self.codex_state.show_state(tr(T("ожидаю вход в браузере…", "waiting for browser sign-in…")), "warn")
        # следить за появлением auth.json
        self._login_polls = 0
        QTimer.singleShot(3000, self._poll_login)

    def _poll_login(self) -> None:
        self._login_polls += 1
        if codex_account()["ok"]:
            self.refresh_codex_state()
            self.app.monitors.refresh_codex()
            self.app.toast(tr(T("Codex: вход выполнен", "Codex: signed in")))
        elif self._login_polls < 100:
            QTimer.singleShot(3000, self._poll_login)
        else:
            self.refresh_codex_state()

    def codex_install(self) -> None:
        self.codex_version.setText(tr(T("скачиваю Codex CLI…", "downloading Codex CLI…")))

        def work():
            last = ""
            try:
                proc = procs.popen([paths.python_exe(), str(paths.CODEX_SETUP)], str(paths.ENGINE),
                                   dict(os.environ, PYTHONIOENCODING="utf-8"))
                for raw in proc.stdout:
                    line = raw.strip()
                    if line.startswith("PROGRESS:"):
                        self.bridge.text.emit("codex_version", "⬇ " + line.split(":", 1)[1].strip() + "%")
                    elif line:
                        last = line
                ok = proc.wait() == 0
            except Exception as exc:  # noqa: BLE001
                ok, last = False, str(exc)
            self.bridge.done.emit("codex_install", ok, last)
        threading.Thread(target=work, daemon=True).start()

    def test_proxy(self) -> None:
        url = self._collect()["CODEX_PROXY"]
        self.proxy_state.setText(tr(T("проверяю…", "testing…")))

        def work():
            try:
                ok, msg = engine_link.unit("relay_manager").test_proxy(url)
            except Exception as exc:  # noqa: BLE001
                ok, msg = False, str(exc)
            self.bridge.done.emit("proxy", ok, msg)
        threading.Thread(target=work, daemon=True).start()

    # ================================================================ CinAI
    def cinai_open(self) -> None:
        try:
            engine_link.unit("cinai_login").open_cinai_in_firefox()
        except Exception:  # noqa: BLE001
            procs.open_path("https://cinai.tv/StudioChat?tab=image")

    def cinai_import(self) -> None:
        self.cinai_state.show_state(tr(T("ищу сессию в браузерах…", "looking for a browser session…")), "warn")

        def work():
            try:
                res = engine_link.unit("cinai_login").import_browser_session()
                hours = int(res.get("expires_in", 0)) // 3600
                self.bridge.done.emit("cinai", True, f"{res.get('source', '?')} · {hours} h")
            except Exception as exc:  # noqa: BLE001
                self.bridge.done.emit("cinai", False, str(exc))
        threading.Thread(target=work, daemon=True).start()

    def _on_cinai(self, data: dict) -> None:
        if data.get("missing"):
            self.cinai_state.show_state(tr(T("не настроено", "not configured")), "")
        elif data.get("ok"):
            bal = data.get("balance") or {}
            amount = bal.get("balance", bal.get("credits", bal.get("amount", "?")))
            self.cinai_state.show_state(tr(T(f"активна · баланс {amount}", f"active · balance {amount}")), "ok")
        else:
            self.cinai_state.show_state(tr(T("ошибка", "error")), "err", data.get("error", ""))

    # ======================================================== ответы потоков
    def _on_text(self, where: str, text: str) -> None:
        if where == "codex_version":
            self.codex_version.setText(text)

    def _on_done(self, what: str, ok: bool, text: str) -> None:
        if what == "codex_install":
            engine_link.reload_params()
            self.codex_version.setText(text)
            self.refresh_codex_state()
            if ok:
                self.app.toast(tr(T("Codex CLI установлен", "Codex CLI installed")))
        elif what == "proxy":
            self.proxy_state.setText(("✔ " if ok else "✖ ") + text)
            self.proxy_state.setProperty("role", "ok" if ok else "err")
            self.proxy_state.style().unpolish(self.proxy_state)
            self.proxy_state.style().polish(self.proxy_state)
        elif what == "cinai":
            if ok:
                # токены записаны движком в .env — показать их в полях
                engine_link.reload_params()
                env = envfile.read_env()
                self._loading = True
                for key in ("CINAI_ACCESS_TOKEN", "CINAI_REFRESH_TOKEN"):
                    self._set_widget(key, env.get(key))
                    self._baseline[key] = self._get_widget(key)
                self._loading = False
                self.cinai_state.show_state(tr(T("сессия получена: ", "session imported: ")) + text, "ok")
                self.app.monitors.refresh_cinai()
            else:
                self.cinai_state.show_state(tr(T("не получено", "failed")), "err", text)
                QMessageBox.warning(self, "CinAI", text)
