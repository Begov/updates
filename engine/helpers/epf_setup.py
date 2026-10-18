"""Install the verified BegoFrame Extreme Picture Finder portable bundle."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

import requests


DEFAULT_SHA256 = "C3AE2A7F574511F915E66C6C717372BE3AE58B01331D4EA3C3836F4843489A5F"
DEFAULT_URL = (
    "https://drive.google.com/file/d/"
    "1xDYIqq6SKQDBEKJKaa9U_mB10I8nfZsy/view?usp=sharing"
)
EPF_REGISTRY_KEY = r"SOFTWARE\Extreme Internet Software\Extreme Picture Finder 3"


def default_install_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "runtime" / "ExtremePictureFinder"


def default_data_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "runtime" / "epf_data" / "Extreme Picture Finder"


def _google_drive_id(url: str) -> str:
    parsed = urlparse(url)
    query_id = parse_qs(parsed.query).get("id", [""])[0]
    if query_id:
        return query_id
    match = re.search(r"/file/d/([^/]+)", parsed.path)
    return match.group(1) if match else ""


def _stream_response(response, target: Path, progress: Callable[[str], None]) -> None:
    total = int(response.headers.get("Content-Length") or 0)
    downloaded = 0
    next_report = 0
    with target.open("wb") as stream:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if not chunk:
                continue
            stream.write(chunk)
            downloaded += len(chunk)
            if downloaded >= next_report:
                progress(
                    f"EPF download: {downloaded / total:.0%}"
                    if total else f"EPF download: {downloaded // (1024 * 1024)} MB"
                )
                next_report = downloaded + 10 * 1024 * 1024


def _download(url: str, target: Path, progress: Callable[[str], None]) -> None:
    file_id = _google_drive_id(url)
    session = requests.Session()
    if not file_id:
        response = session.get(url, stream=True, timeout=60)
        response.raise_for_status()
        _stream_response(response, target, progress)
        return

    response = session.get(
        "https://drive.google.com/uc",
        params={"export": "download", "id": file_id},
        stream=True,
        timeout=60,
    )
    response.raise_for_status()
    if "text/html" in (response.headers.get("Content-Type") or "").lower():
        html = response.text
        form = re.search(r'<form[^>]+action="([^"]+)"[^>]*>(.*?)</form>', html, re.S | re.I)
        if not form:
            raise RuntimeError("Google Drive did not return a downloadable file")
        params = {
            name: value
            for name, value in re.findall(
                r'<input[^>]+name="([^"]+)"[^>]+value="([^"]*)"', form.group(2), re.I
            )
        }
        response = session.get(
            form.group(1).replace("&amp;", "&"),
            params=params,
            stream=True,
            timeout=60,
        )
        response.raise_for_status()
    _stream_response(response, target, progress)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _safe_extract(archive: Path, destination: Path) -> None:
    root = destination.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            target = (destination / member.filename).resolve()
            if target != root and root not in target.parents:
                raise RuntimeError(f"Unsafe path in EPF archive: {member.filename}")
        bundle.extractall(destination)


def _patch_portable_settings(install_dir: Path, data_dir: Path) -> None:
    settings = install_dir / "Settings" / "tryroom.set"
    if not settings.is_file():
        raise FileNotFoundError(f"EPF portable settings missing: {settings}")
    text = settings.read_text(encoding="utf-16")
    core_dir = install_dir / "App" / "EPF"

    def escaped(path: Path) -> str:
        return str(path.resolve()).replace("\\", "\\\\")

    data_value = escaped(data_dir)
    install_value = escaped(core_dir)
    text = text.replace("__BegoFrame_EPF_DATA_DIR__", data_value)
    text = text.replace("__BegoFrame_EPF_INSTALL_DIR__", install_value)
    text = re.sub(
        r'(?m)^"DataDir"=".*"$',
        lambda _match: f'"DataDir"="{data_value}"',
        text,
    )
    text = re.sub(
        r'(?m)^"InstallDir"=".*"$',
        lambda _match: f'"InstallDir"="{install_value}"',
        text,
    )
    settings.write_text(text, encoding="utf-16")


def _prepare_portable_data(install_dir: Path, data_path: Path) -> None:
    data_path.mkdir(parents=True, exist_ok=True)
    bundled_data = install_dir / "Settings" / "Extreme Picture Finder"
    for name in ("searchengines.dat", "default_search.epr", "target_files.dat"):
        source = bundled_data / name
        destination = data_path / name
        if source.is_file() and not destination.exists():
            shutil.copy2(source, destination)

def install_epf_bundle(
    *,
    url: str = "",
    archive_path: str | Path | None = None,
    expected_sha256: str = "",
    install_dir: str | Path | None = None,
    data_dir: str | Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> Path:
    if os.name != "nt":
        raise OSError("Extreme Picture Finder is available only on Windows")
    progress = progress or (lambda message: print(message, flush=True))
    install_root = Path(install_dir).resolve() if install_dir else default_install_dir()
    data_root = Path(data_dir).resolve() if data_dir else default_data_dir()
    expected = (
        expected_sha256
        or os.getenv("REAL_PHOTO_EPF_DOWNLOAD_SHA256", "")
        or DEFAULT_SHA256
    ).strip().upper()
    source_url = (
        url
        or os.getenv("REAL_PHOTO_EPF_DOWNLOAD_URL", "")
        or DEFAULT_URL
    ).strip()

    with tempfile.TemporaryDirectory(prefix="begoframe_epf_install_") as tmp:
        temp_root = Path(tmp)
        archive = Path(archive_path).resolve() if archive_path else temp_root / "epf.zip"
        if archive_path is None:
            if not source_url:
                raise RuntimeError("REAL_PHOTO_EPF_DOWNLOAD_URL is not configured")
            progress("Downloading verified EPF bundle...")
            _download(source_url, archive, progress)
        if not archive.is_file():
            raise FileNotFoundError(archive)

        actual = _sha256(archive)
        if actual != expected:
            raise RuntimeError(
                f"EPF archive SHA-256 mismatch: expected {expected}, got {actual}"
            )
        progress("EPF archive hash verified.")

        extracted = temp_root / "extracted"
        extracted.mkdir()
        _safe_extract(archive, extracted)
        payload = extracted / "ExtremePictureFinder"
        if not (payload / "App" / "EPF" / "EPF.exe").is_file():
            raise RuntimeError("EPF archive does not contain App/EPF/EPF.exe")
        install_root.parent.mkdir(parents=True, exist_ok=True)
        if install_root.exists():
            backup = install_root.with_name(
                f"{install_root.name}.backup_{datetime.now():%Y%m%d_%H%M%S}"
            )
            install_root.rename(backup)
            progress(f"Previous EPF installation backed up to {backup.name}.")
        shutil.move(str(payload), str(install_root))

    _patch_portable_settings(install_root, data_root)
    _prepare_portable_data(install_root, data_root)
    progress(f"EPF installed: {install_root}")
    return install_root


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="")
    parser.add_argument("--archive", default="")
    parser.add_argument("--sha256", default="")
    parser.add_argument("--install-dir", default="")
    parser.add_argument("--data-dir", default="")
    args = parser.parse_args()
    install_epf_bundle(
        url=args.url,
        archive_path=args.archive or None,
        expected_sha256=args.sha256,
        install_dir=args.install_dir or None,
        data_dir=args.data_dir or None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
