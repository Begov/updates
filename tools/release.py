"""Публикация в Begov/updates (локальная копия — D:\\Projects\\updates).

    python tools/release.py sync -m "что изменено"   # исходники → updates: commit + push (после КАЖДОГО изменения)
    python tools/release.py publish                  # выпуск: sync + GitHub Release v<версия> с файлами из dist/<версия>

Перед publish: поднять APP_VERSION в studio/__init__.py, дописать раздел в CHANGELOG.md,
собрать `python tools/build.py`. Программы пользователей увидят выпуск сразу после publish.

Токен GitHub для создания Release берётся из GITHUB_TOKEN, затем из `gh auth token`,
затем из диспетчера учётных данных git (тот же вход, которым делается git push).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build import DIST, app_version, release_notes  # noqa: E402

UPDATES_DIR = Path(os.environ.get("BEGO_UPDATES_REPO", r"D:\Projects\updates"))
REPO = "Begov/updates"
API = "https://api.github.com"
# В публичный репозиторий не попадает никогда
NEVER = (".env", "engine/.env")


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def say(text: str) -> None:
    print(text, flush=True)


def git(*args: str, cwd: Path, check: bool = True) -> str:
    res = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    if check and res.returncode != 0:
        raise SystemExit(f"git {' '.join(args)}: {res.stderr.strip() or res.stdout.strip()}")
    return res.stdout


def repo_files() -> list[str]:
    """Все файлы рабочей копии BegoFrame, кроме игнорируемых (.gitignore): ключи, проекты, сборки."""
    out = git("ls-files", "--cached", "--others", "--exclude-standard", "-z", cwd=ROOT)
    files = []
    for rel in filter(None, out.split("\0")):
        rel = rel.replace("\\", "/")
        name = rel.rsplit("/", 1)[-1]
        if rel in NEVER or name == ".env" or name.startswith(".env.") and name != ".env.example":
            continue
        if (ROOT / rel).is_file():
            files.append(rel)
    return sorted(files)


# ------------------------------------------------------------------ sync
def sync(message: str) -> bool:
    if not (UPDATES_DIR / ".git").is_dir():
        raise SystemExit(f"{UPDATES_DIR} — не git-репозиторий")
    files = repo_files()
    wanted = set(files)
    # удалить то, чего в BegoFrame больше нет
    existing = git("ls-files", "-z", cwd=UPDATES_DIR).split("\0")
    for rel in filter(None, existing):
        if rel not in wanted:
            (UPDATES_DIR / rel).unlink(missing_ok=True)
    for rel in files:
        dst = UPDATES_DIR / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dst)
    git("add", "-A", cwd=UPDATES_DIR)
    if not git("status", "--porcelain", cwd=UPDATES_DIR).strip():
        say("[sync] изменений нет")
        return False
    staged = git("diff", "--cached", "--name-only", cwd=UPDATES_DIR).split()
    bad = [f for f in staged if f in NEVER or f.endswith("/.env")]
    if bad:
        git("reset", cwd=UPDATES_DIR)
        raise SystemExit(f"Остановлено: в коммит попадали секреты {bad}")
    git("commit", "-q", "-m", message, cwd=UPDATES_DIR)
    branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=UPDATES_DIR).strip()
    git("push", "-q", "-u", "origin", branch, cwd=UPDATES_DIR)
    say(f"[sync] {len(staged)} файлов → {REPO} ({branch}): {message}")
    return True


# --------------------------------------------------------------- publish
def token() -> str:
    if os.environ.get("GITHUB_TOKEN"):
        return os.environ["GITHUB_TOKEN"]
    try:
        res = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except OSError:
        pass
    res = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                         capture_output=True, text=True, cwd=UPDATES_DIR)
    for line in res.stdout.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1]
    raise SystemExit("Нет токена GitHub: задайте GITHUB_TOKEN или выполните `gh auth login`")


def api(method: str, url: str, tok: str, data=None, content_type="application/json"):
    body = None
    if data is not None:
        body = data if isinstance(data, (bytes, bytearray)) else json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "Content-Type": content_type, "User-Agent": "BegoFrame-release"})
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and method == "GET":
            return None
        raise SystemExit(f"GitHub {method} {url}: {exc.code} {exc.read().decode(errors='replace')[:400]}")


def publish() -> None:
    version = app_version()
    out = DIST / version
    files = [out / "latest.json", out / f"BegoFrame-update-{version}.zip", out / f"BegoFrame-Setup-{version}.exe"]
    missing = [f.name for f in files if not f.exists()]
    if missing:
        raise SystemExit(f"Нет файлов {missing} — сначала `python tools/build.py`")
    info = json.loads(files[0].read_text(encoding="utf-8"))
    if info.get("version") != version or not info.get("installer"):
        raise SystemExit("dist/latest.json не от этой версии или без установщика — пересоберите")
    tok = token()
    tag = f"v{version}"
    if api("GET", f"{API}/repos/{REPO}/releases/tags/{tag}", tok):
        raise SystemExit(f"Выпуск {tag} уже есть. Поднимите APP_VERSION в studio/__init__.py")
    sync(f"BegoFrame {version}")
    branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=UPDATES_DIR).strip()
    rel = api("POST", f"{API}/repos/{REPO}/releases", tok, {
        "tag_name": tag, "target_commitish": branch, "name": f"BegoFrame {version}",
        "body": release_notes(version) or f"BegoFrame {version}", "draft": True, "prerelease": False})
    upload = rel["upload_url"].split("{", 1)[0]
    for f in files:
        say(f"[publish] загружаю {f.name} ({f.stat().st_size / 1048576:.1f} МБ)")
        ctype = "application/json" if f.suffix == ".json" else "application/octet-stream"
        api("POST", f"{upload}?name={urllib.parse.quote(f.name)}", tok, f.read_bytes(), ctype)
    # черновик публикуется, только когда загружены все файлы — программы не увидят неполный выпуск
    api("PATCH", f"{API}/repos/{REPO}/releases/{rel['id']}", tok, {"draft": False, "make_latest": "true"})
    say(f"[publish] готово: https://github.com/{REPO}/releases/tag/{tag}")


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync", help="исходники → Begov/updates (commit + push)")
    s.add_argument("-m", "--message", help="сообщение коммита (по умолчанию — последнего коммита BegoFrame)")
    sub.add_parser("publish", help="sync + GitHub Release с файлами из dist/<версия>")
    a = ap.parse_args()
    if a.cmd == "sync":
        msg = a.message or git("log", "-1", "--format=%s", cwd=ROOT).strip() or "update"
        sync(msg)
    else:
        publish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
