"""Установка Remotion (моушн-графика) в папку runtime/remotion.

Remotion — это npm-пакеты, поэтому нужен Node.js (node и npm в PATH). Рабочая
папка одна на все проекты: в ней node_modules и браузер для рендера
(Chrome Headless Shell, ~100 МБ — Remotion кладёт его в node_modules/.remotion).
Сцены клипов пишутся в её подпапку scenes/, чтобы импорт `remotion` находил пакеты.

    python remotion_setup.py            # установить / обновить
    python remotion_setup.py --status   # состояние в JSON
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = ROOT / "runtime" / "remotion"
TEMPLATE = ROOT / "engine" / "remotion" / "package.json"
CLI_JS = WORKSPACE / "node_modules" / "@remotion" / "cli" / "remotion-cli.js"
BROWSER_DIR = WORKSPACE / "node_modules" / ".remotion" / "chrome-headless-shell"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
INSTALL_TIMEOUT = 1800

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


def find_node() -> str | None:
    return shutil.which("node")


def node_version() -> str | None:
    node = find_node()
    if not node:
        return None
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=15,
                             creationflags=NO_WINDOW)
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


def _wanted() -> dict:
    return json.loads(TEMPLATE.read_text(encoding="utf-8")).get("dependencies", {})


def _installed_version(package: str) -> str | None:
    manifest = WORKSPACE / "node_modules" / Path(*package.split("/")) / "package.json"
    try:
        return json.loads(manifest.read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError):
        return None


def packages_ready() -> bool:
    """Все пакеты шаблона стоят ровно тех версий, что в engine/remotion/package.json."""
    try:
        wanted = _wanted()
    except (OSError, ValueError):
        return False
    return CLI_JS.is_file() and all(_installed_version(name) == ver for name, ver in wanted.items())


def browser_ready() -> bool:
    return BROWSER_DIR.is_dir() and any(BROWSER_DIR.iterdir())


def status() -> dict:
    version = node_version()
    packages = packages_ready()
    browser = browser_ready()
    return {"node": version, "packages": packages, "browser": browser,
            "ready": bool(version and packages and browser), "workspace": str(WORKSPACE)}


def remotion_cmd(*args: str) -> list[str]:
    """Команда Remotion CLI: node + JS-файл напрямую.

    Не через npx.cmd: на Windows .cmd запускается через cmd.exe, и аргументы
    (пути с пробелами и кириллицей) проходят лишний разбор оболочкой."""
    node = find_node()
    if not node:
        raise RuntimeError("Node.js не найден: установите его с nodejs.org (нужны node и npm в PATH)")
    return [node, str(CLI_JS), *args]


def _run(cmd: list[str], log, timeout: int) -> bool:
    proc = subprocess.Popen(cmd, cwd=str(WORKSPACE), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                log(f"  {line}")
        return proc.wait(timeout=timeout) == 0
    except subprocess.TimeoutExpired:
        proc.kill()
        log(f"[ERROR] Не уложилось в {timeout} с")
        return False


@contextmanager
def _install_lock():
    """Межпроцессная блокировка установки.

    filelock есть в requirements, но у части установок его нет (программа идёт на
    системном Python) — тогда блокировка на файле с эксклюзивным созданием."""
    path = WORKSPACE / ".install.lock"
    try:
        from filelock import FileLock
    except ImportError:
        FileLock = None
    if FileLock is not None:
        with FileLock(str(path), timeout=INSTALL_TIMEOUT):
            yield
        return
    deadline = time.time() + INSTALL_TIMEOUT
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            # Файл старше таймаута установки остался от упавшего процесса
            try:
                if time.time() - path.stat().st_mtime > INSTALL_TIMEOUT:
                    path.unlink(missing_ok=True)
                    continue
            except OSError:
                continue
            if time.time() > deadline:
                raise TimeoutError("установка Remotion занята другим процессом")
            time.sleep(2)
    try:
        yield
    finally:
        os.close(fd)
        path.unlink(missing_ok=True)


def ensure(log=print) -> bool:
    """Поставить пакеты и браузер, если их нет. Повторный вызов ничего не делает.

    Под файловой блокировкой: две стадии (или стадия и кнопка в интерфейсе) не
    запустят npm install в одну папку одновременно."""
    if not find_node():
        log("[ERROR] Node.js не найден: установите его с nodejs.org (нужны node и npm в PATH)")
        return False
    npm = shutil.which("npm")
    if not npm:
        log("[ERROR] npm не найден рядом с Node.js")
        return False
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    with _install_lock():
        if not packages_ready():
            shutil.copy2(TEMPLATE, WORKSPACE / "package.json")
            log("STEP: Устанавливаю пакеты Remotion (npm install)…")
            # npm — это npm.cmd: аргументы здесь фиксированные, без путей пользователя
            if not _run([npm, "install", "--no-audit", "--no-fund", "--loglevel=error"], log, INSTALL_TIMEOUT):
                log("[ERROR] npm install не удался")
                return False
            if not packages_ready():
                log("[ERROR] После npm install пакеты не на месте")
                return False
        if not browser_ready():
            log("STEP: Скачиваю браузер для рендера (Chrome Headless Shell)…")
            if not _run(remotion_cmd("browser", "ensure"), log, INSTALL_TIMEOUT) or not browser_ready():
                log("[ERROR] Браузер для рендера не скачался")
                return False
    return True


def main() -> int:
    if "--status" in sys.argv:
        print(json.dumps(status(), ensure_ascii=False))
        return 0
    print(f"Node.js: {node_version() or 'не найден'}", flush=True)
    ok = ensure(lambda m: print(m, flush=True))
    print("[OK] Remotion готов" if ok else "[ERROR] Remotion не установлен", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
