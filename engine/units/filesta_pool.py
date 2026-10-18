"""filesta_accounts.py — несколько аккаунтов Filesta вместо одного.

Зачем
-----
Лицензии Filesta НЕ складывают суточный лимит: вторая купленная подписка на
300 загрузок в сутки не даёт 600, а продлевает СРОК на том же тарифе (замер
пользователя 01.09.2026). Значит нарастить сток можно только несколькими
АККАУНТАМИ: десять аккаунтов по 300 — три тысячи загрузок в сутки.

Число аккаунтов не ограничено: сколько человек завёл, столько и работает.

Что здесь есть
--------------
Реестр в `agent/.filesta/accounts.json` — список аккаунтов, у каждого свой
профиль Chrome, свой отпечаток, своя сессия и свой порт отладки. Плюс выбор
аккаунта на очередную покупку — двумя способами (см. `pick`).

Чего здесь НЕТ
--------------
Ни сети, ни браузера, ни загрузки. Это только учёт: кто есть, где его файлы,
чья очередь. Работу ведёт `filesta_envato`.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import params as config
logger = logging.getLogger(__name__)

REGISTRY_NAME = 'accounts.json'
BASE_CDP_PORT = 9222

# Раскладка покупок по аккаунтам. Что лучше — покажут прогоны, поэтому оба:
#   sequential — вычерпать первый до нуля, потом второй, третий…
#   spread     — раскидывать поровну, чтобы суточный запас тратился ровно.
ROTATION_SEQUENTIAL = 'sequential'
ROTATION_SPREAD = 'spread'

_lock = threading.Lock()
_round_robin = {'next': 0}          # указатель для режима «вперемешку»


def home() -> Path:
    """Папка провайдера — та же, что у `filesta_envato._home`."""
    raw = str(getattr(config, 'FILESTA_HOME', '') or '').strip()
    if raw:
        return Path(raw).expanduser()
    return Path(__file__).resolve().parents[1] / '.filesta'


def registry_path() -> Path:
    raw = str(getattr(config, 'FILESTA_ACCOUNTS_FILE', '') or '').strip()
    return Path(raw).expanduser() if raw else home() / REGISTRY_NAME


def rotation_mode() -> str:
    """Как раскладывать покупки. Умолчание — по очереди."""
    raw = str(getattr(config, 'FILESTA_ROTATION', '') or '').strip().lower()
    return ROTATION_SPREAD if raw == ROTATION_SPREAD else ROTATION_SEQUENTIAL


class Account(dict):
    """Один аккаунт: где его файлы и включён ли он.

    Обычный словарь — чтобы читаться и писаться в JSON без превращений.
    """

    @property
    def id(self) -> str:
        return str(self.get('id') or '')

    @property
    def name(self) -> str:
        return str(self.get('name') or self.id)

    @property
    def enabled(self) -> bool:
        return bool(self.get('enabled', True))

    def _path(self, key: str, default_name: str) -> Path:
        raw = str(self.get(key) or '').strip()
        if raw:
            return Path(raw).expanduser()
        return home() / 'profiles' / self.id / default_name

    @property
    def profile_dir(self) -> Path:
        return self._path('profile', 'chrome_profile')

    @property
    def fingerprint_file(self) -> Path:
        return self._path('fingerprint', 'fp.json')

    @property
    def session_file(self) -> Path:
        return self._path('session', 'session.json')

    @property
    def cdp_port(self) -> int:
        try:
            return int(self.get('cdp_port') or BASE_CDP_PORT)
        except (TypeError, ValueError):
            return BASE_CDP_PORT

    @property
    def cdp_url(self) -> str:
        return f'http://127.0.0.1:{self.cdp_port}'

    def ensure_dirs(self) -> None:
        """Создать папки аккаунта. Зовётся перед записью сессии и отпечатка:
        у первого аккаунта пути смотрят на старую установку, а у заведённых
        руками папки `profiles/<id>/` ещё не существует."""
        for path in (self.profile_dir, self.fingerprint_file.parent,
                     self.session_file.parent):
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.warning('[filesta] %s: не создать %s (%s)',
                               self.id, path, exc)

    def has_fingerprint(self) -> bool:
        try:
            return self.fingerprint_file.is_file()
        except OSError:
            return False

    def has_session(self) -> bool:
        """Сессия снята и в ней есть то, без чего подпись не собрать."""
        try:
            data = json.loads(self.session_file.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return False
        return bool(data.get('sessionId') and data.get('accessToken'))

    def ready(self) -> bool:
        return self.enabled and self.has_fingerprint() and self.has_session()

    def status(self) -> str:
        """Короткая строка для интерфейса."""
        if not self.enabled:
            return 'выключен'
        if not self.has_fingerprint():
            return 'нет отпечатка'
        if not self.has_session():
            return 'нужен вход'
        return 'готов'


def load() -> List[Account]:
    """Список аккаунтов из реестра. Файла нет — пустой список."""
    path = registry_path()
    try:
        raw = json.loads(path.read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        logger.warning('[filesta] реестр аккаунтов не читается (%s) — считаю пустым', exc)
        return []
    if not isinstance(raw, list):
        return []
    return [Account(item) for item in raw if isinstance(item, dict) and item.get('id')]


def save(accounts: List[Dict[str, Any]]) -> None:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(list(accounts), ensure_ascii=False, indent=2),
                    encoding='utf-8')


def next_free_port(accounts: Optional[List[Account]] = None) -> int:
    """Свободный порт отладки: два Chrome на одном порту не живут."""
    taken = {acc.cdp_port for acc in (accounts if accounts is not None else load())}
    port = BASE_CDP_PORT
    while port in taken:
        port += 1
    return port


def _free_id(accounts: List[Account]) -> str:
    taken = {acc.id for acc in accounts}
    n = 1
    while f'acc{n}' in taken:
        n += 1
    return f'acc{n}'


def add(name: str = '') -> Account:
    """Завести аккаунт. Файлы появятся, когда человек войдёт и снимет отпечаток."""
    with _lock:
        accounts = load()
        account = Account({
            'id': _free_id(accounts),
            'name': (name or '').strip() or f'аккаунт {len(accounts) + 1}',
            'cdp_port': next_free_port(accounts),
            'enabled': True,
        })
        accounts.append(account)
        save(accounts)
        account.ensure_dirs()
        logger.info('[filesta] заведён аккаунт %s (%s), порт %d',
                    account.id, account.name, account.cdp_port)
        return account


def remove(account_id: str) -> bool:
    """Убрать аккаунт из реестра. ФАЙЛЫ профиля не трогаем: вход в них ещё
    может пригодиться, а удаление чужих данных без спроса — не наше дело."""
    with _lock:
        accounts = load()
        left = [acc for acc in accounts if acc.id != account_id]
        if len(left) == len(accounts):
            return False
        save(left)
        logger.info('[filesta] аккаунт %s убран из реестра', account_id)
        return True


def update(account_id: str, **fields) -> Optional[Account]:
    """Поменять поля аккаунта (имя, enabled, пути)."""
    with _lock:
        accounts = load()
        for account in accounts:
            if account.id == account_id:
                account.update(fields)
                save(accounts)
                return account
    return None


def get(account_id: str) -> Optional[Account]:
    for account in load():
        if account.id == account_id:
            return account
    return None


def ensure_default() -> Account:
    """Первый аккаунт из уже настроенного одиночного источника.

    У кого Filesta уже работает, профиль и отпечаток лежат по старым путям —
    заводим аккаунт, который смотрит ИМЕННО на них, чтобы обновление не
    заставило входить заново.
    """
    accounts = load()
    if accounts:
        return accounts[0]
    from units import filesta_source as fe

    account = Account({
        'id': 'acc1',
        'name': 'основной',
        'profile': str(fe.profile_path()),
        'fingerprint': str(fe.fingerprint_path()),
        'cdp_port': BASE_CDP_PORT,
        'enabled': True,
    })
    save([account])
    logger.info('[filesta] реестр создан, первый аккаунт смотрит на старые пути')
    return account


def pick(quota_of: Optional[Callable[[Account], Optional[int]]] = None,
         skip: Optional[set] = None) -> Optional[Account]:
    """Чей черёд покупать.

    `quota_of` — как узнать остаток аккаунта (в тестах подменяется). None
    означает «спросить не удалось»: такой аккаунт не пропускаем, пусть попробует.
    `skip` — кого не брать (уже отказал в этом прогоне).
    """
    skip = skip or set()
    ready = [acc for acc in load() if acc.ready() and acc.id not in skip]
    if not ready:
        return None
    if quota_of is None:
        return ready[0]

    alive = []
    for account in ready:
        left = quota_of(account)
        if left is not None and left <= 0:
            continue                      # суточная квота выбрана
        alive.append((account, left))
    if not alive:
        return None

    if rotation_mode() == ROTATION_SPREAD:
        # Вперемешку: по кругу, чтобы запас тратился ровно у всех.
        with _lock:
            index = _round_robin['next'] % len(alive)
            _round_robin['next'] = index + 1
        return alive[index][0]
    # По очереди: первый, у кого ещё есть запас.
    return alive[0][0]


def reset_rotation() -> None:
    """Начать раскладку заново (новый прогон)."""
    with _lock:
        _round_robin['next'] = 0
