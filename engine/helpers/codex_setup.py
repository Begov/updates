"""Установка и обновление Codex CLI в папку runtime/codex.

Windows-релиз Codex — это согласованный набор из трёх исполняемых файлов
(сам codex, code-mode host и command runner): обновлять их нужно вместе,
поэтому файлы сначала скачиваются во временные *.part и подменяются только
когда скачались все три. При ошибке возвращаются резервные копии.

    python codex_setup.py            # установить / обновить
    python codex_setup.py --version  # только показать текущую версию
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

CODEX_DIR = Path(__file__).resolve().parents[2] / "runtime" / "codex"
CODEX_EXE_WIN = CODEX_DIR / "codex-x86_64-pc-windows-msvc.exe"
RELEASE = "https://github.com/openai/codex/releases/latest/download/"
WINDOWS_ASSETS = {
    "codex-x86_64-pc-windows-msvc.exe": RELEASE + "codex-x86_64-pc-windows-msvc.exe",
    "codex-code-mode-host.exe": RELEASE + "codex-code-mode-host-x86_64-pc-windows-msvc.exe",
    "codex-command-runner.exe": RELEASE + "codex-command-runner-x86_64-pc-windows-msvc.exe",
}
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


def find_codex() -> str | None:
    """Путь к рабочему codex: сначала своя копия, затем PATH и ~/.local/bin."""
    if CODEX_EXE_WIN.exists():
        return str(CODEX_EXE_WIN)
    found = shutil.which("codex")
    if found:
        return found
    native = Path.home() / ".local" / "bin" / "codex"
    return str(native) if native.is_file() else None


def current_version() -> str | None:
    exe = find_codex()
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10,
                             creationflags=NO_WINDOW)
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


def _download(url: str, destination: Path) -> None:
    """Скачать файл в destination.part с повторами при лимитах GitHub."""
    part = destination.with_name(destination.name + ".part")
    part.unlink(missing_ok=True)
    # Без User-Agent GitHub быстро отвечает 429 — представляемся явно
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) BegoFrame-Setup",
        "Accept": "application/octet-stream",
    })
    attempts = 5
    response = None
    for n in range(1, attempts + 1):
        try:
            response = urllib.request.urlopen(req, timeout=300)
            break
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429, 503) and n < attempts:
                try:
                    wait = int(exc.headers.get("Retry-After") or 0)
                except (TypeError, ValueError):
                    wait = 0
                wait = max(wait, min(60, 5 * 2 ** (n - 1)))
                print(f"\n  GitHub {exc.code}: жду {wait}с и повторяю ({n}/{attempts - 1})…", flush=True)
                time.sleep(wait)
                continue
            raise
    with response:
        total = int(response.headers.get("content-length", 0))
        done = 0
        with open(part, "wb") as fh:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if total:
                    print(f"PROGRESS: {int(done * 100 / total)} {destination.name}", flush=True)
    if total and done != total:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"неполная загрузка: {done}/{total}")
    if done < 1_000_000:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"подозрительно маленький файл: {done} байт")
    with open(part, "rb") as fh:
        if fh.read(2) != b"MZ":
            part.unlink(missing_ok=True)
            raise RuntimeError("скачанный файл не является программой Windows")


def install_windows() -> bool:
    CODEX_DIR.mkdir(parents=True, exist_ok=True)
    targets = {name: CODEX_DIR / name for name in WINDOWS_ASSETS}
    backups: dict[Path, Path] = {}
    try:
        for name, url in WINDOWS_ASSETS.items():
            print(f"Скачиваю {name}…", flush=True)
            _download(url, targets[name])
        for dst in targets.values():
            if dst.exists():
                bak = dst.with_name(dst.name + ".bak")
                shutil.copy2(dst, bak)
                backups[dst] = bak
        for dst in targets.values():
            os.replace(dst.with_name(dst.name + ".part"), dst)
        if not current_version():
            raise RuntimeError("новый codex не сообщает версию")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] Установка не удалась: {exc}", flush=True)
        for dst in targets.values():
            dst.with_name(dst.name + ".part").unlink(missing_ok=True)
        for dst, bak in backups.items():
            if bak.exists():
                shutil.copy2(bak, dst)
        return False


def install_unix() -> bool:
    try:
        rc = subprocess.run(["/bin/sh", "-c", "curl -fsSL https://chatgpt.com/codex/install.sh | sh"]).returncode
        return rc == 0
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] {exc}")
        return False


def main() -> int:
    if "--version" in sys.argv:
        print(current_version() or "not installed")
        return 0
    before = current_version()
    print(f"Codex CLI ({platform.system()} {platform.machine()}): {before or 'не установлен'}", flush=True)
    system = platform.system()
    if system == "Windows":
        ok = install_windows()
    elif system == "Linux":
        ok = install_unix()
    else:
        print(f"[ERROR] Платформа {system} не поддерживается: https://github.com/openai/codex/releases/latest")
        return 1
    if not ok:
        return 1
    after = current_version()
    if after and after == before:
        print(f"[OK] Уже последняя версия: {after}")
    else:
        print(f"[OK] Установлено: {after or '?'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
