#!/usr/bin/env python3
"""
Скачивание фото с Яндекс Картинок по текстовому запросу.
Использование:
    python image_search.py "Брежнев портрет" --out ./refs --count 2
    python image_search.py "cute cat" -o /tmp -n 3
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import unquote

import requests

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml"}


def search_yandex_images(query: str, count: int = 2) -> list[str]:
    """Ищет картинки на Яндекс Картинках, возвращает список URL оригиналов."""
    resp = requests.get(
        "https://yandex.ru/images/search",
        params={"text": query, "isize": "medium"},
        headers=_HEADERS,
        timeout=20,
    )
    resp.raise_for_status()
    raw = re.findall(r"img_url=([^&;\"]+)", resp.text)
    urls = list(dict.fromkeys(unquote(unquote(u)) for u in raw))
    # Фильтруем: только jpg/jpeg/png/webp, минимум 10кб ожидаемо
    filtered = [u for u in urls if re.search(r"\.(jpe?g|png|webp)", u, re.I)]
    return filtered[:count] if filtered else urls[:count]


def download_images(query: str, out_dir: str, count: int = 2, prefix: str = "") -> list[str]:
    """Скачивает картинки по запросу. Возвращает список путей к файлам."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    urls = search_yandex_images(query, count=count + 3)  # запас на ошибки скачивания

    saved = []
    for i, url in enumerate(urls):
        if len(saved) >= count:
            break
        try:
            r = requests.get(url, headers=_HEADERS, timeout=20)
            if r.status_code != 200 or len(r.content) < 5000:
                continue
            ct = r.headers.get("content-type", "")
            if "png" in ct:
                ext = ".png"
            elif "webp" in ct:
                ext = ".webp"
            else:
                ext = ".jpg"
            idx = len(saved)
            if prefix:
                suffix = "_" * idx  # 0: "", 1: "_", 2: "__", 3: "___"
                fname = f"{prefix}{suffix}{ext}"
            else:
                fname = f"ref_{idx+1}{ext}"
            path = out / fname
            path.write_bytes(r.content)
            saved.append(str(path))
            print(f"  [OK] {fname} ({len(r.content)//1024}KB)")
        except Exception as e:
            print(f"  [SKIP] {url[:60]}... — {e}")
    return saved


def main():
    parser = argparse.ArgumentParser(description="Скачать фото с Яндекс Картинок")
    parser.add_argument("query", help="Поисковый запрос")
    parser.add_argument("-o", "--out", default=".", help="Папка для сохранения")
    parser.add_argument("-n", "--count", type=int, default=2, help="Количество фото")
    parser.add_argument("-p", "--prefix", default="", help="Префикс имени файла")
    args = parser.parse_args()

    paths = download_images(args.query, args.out, args.count, args.prefix)
    if paths:
        print(f"\nСкачано: {len(paths)} файл(ов)")
        for p in paths:
            print(f"  {p}")
    else:
        print("\nНе удалось скачать ни одного фото", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
