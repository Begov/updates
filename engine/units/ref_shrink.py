"""
ref_compress.py — Preflight compression of reference images.

Scans a ref/ directory and compresses any image that exceeds max_mb:
  - JPEG/WEBP : binary-search quality reduction (highest quality that fits)
  - PNG       : progressive 10%-step downscale (lossless → size only via resize)

Writes result to print() (console / PL log window) and logging.info() (pipeline log file).

Usage:
    from units.ref_shrink import compress_refs
    compress_refs('/path/to/project/ref', max_mb=5.0)
"""

import logging
from io import BytesIO
from pathlib import Path

_YELLOW = '\033[33m'
_GREEN  = '\033[32m'
_RESET  = '\033[0m'

_IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp'}


def compress_refs(ref_dir: str, max_mb: float = 5.0) -> dict:
    """
    Check all images in ref_dir, compress in-place those exceeding max_mb.

    Returns dict: {checked, compressed, skipped, errors}
    """
    ref_path = Path(ref_dir)
    max_bytes = int(max_mb * 1_000_000)

    result = {'checked': 0, 'compressed': 0, 'skipped': 0, 'errors': 0}

    if not ref_path.exists():
        logging.debug(f"ref_compress: ref dir not found: {ref_dir}")
        return result

    images = sorted(f for f in ref_path.iterdir()
                    if f.is_file() and f.suffix.lower() in _IMAGE_EXTS)
    if not images:
        logging.info(f"ref_compress: no images in {ref_dir}")
        return result

    print(f"\n  {_YELLOW}[REF PREFLIGHT]{_RESET} Checking {len(images)} ref image(s) — limit {max_mb} MB")
    logging.info(f"ref_compress: checking {len(images)} images in {ref_dir}, limit={max_mb}MB")

    try:
        from PIL import Image as _Image
    except ImportError:
        import subprocess as _sp, sys as _sys
        print(f"  {_YELLOW}[REF PREFLIGHT]{_RESET} Pillow not found — installing...")
        _sp.run([_sys.executable, '-m', 'pip', 'install', 'pillow', '-q'], check=False)
        try:
            from PIL import Image as _Image
        except ImportError:
            print(f"  {_YELLOW}[REF PREFLIGHT] WARN{_RESET} Pillow install failed — skipping ref size check")
            logging.warning("ref_compress: Pillow not installed, skipping")
            return result

    for f in images:
        result['checked'] += 1
        size = f.stat().st_size

        if size <= max_bytes:
            logging.debug(f"ref_compress: OK {f.name} ({size // 1024} KB)")
            result['skipped'] += 1
            continue

        size_mb = size / 1_000_000
        print(f"  {_YELLOW}[REF PREFLIGHT]{_RESET} {f.name}: {size_mb:.1f} MB > {max_mb} MB — compressing...")
        logging.info(f"ref_compress: {f.name} {size // 1024} KB exceeds limit, compressing")

        try:
            img = _Image.open(f)
            if img.mode in ('RGBA', 'LA', 'P'):
                img = img.convert('RGB')

            ext = f.suffix.lower()

            if ext in ('.jpg', '.jpeg', '.webp'):
                best_data, best_q = _compress_jpeg(img, max_bytes)
                if best_data:
                    f.write_bytes(best_data)
                    new_kb = len(best_data) // 1024
                    print(f"  {_GREEN}[REF PREFLIGHT]{_RESET} {f.name}: "
                          f"{size_mb:.1f} MB → {new_kb} KB (JPEG q={best_q})")
                    logging.info(f"ref_compress: {f.name} {size // 1024}KB → {new_kb}KB (jpeg q={best_q})")
                    result['compressed'] += 1
                else:
                    print(f"  {_YELLOW}[REF PREFLIGHT] WARN{_RESET} {f.name}: "
                          f"cannot reach <{max_mb} MB even at lowest quality")
                    logging.warning(f"ref_compress: {f.name} could not compress below {max_mb}MB")
                    result['errors'] += 1

            elif ext == '.png':
                best_data, scale = _compress_png(img, max_bytes)
                if best_data:
                    f.write_bytes(best_data)
                    new_kb = len(best_data) // 1024
                    print(f"  {_GREEN}[REF PREFLIGHT]{_RESET} {f.name}: "
                          f"{size_mb:.1f} MB → {new_kb} KB (PNG {scale:.0%} resize)")
                    logging.info(f"ref_compress: {f.name} {size // 1024}KB → {new_kb}KB (png {scale:.0%})")
                    result['compressed'] += 1
                else:
                    print(f"  {_YELLOW}[REF PREFLIGHT] WARN{_RESET} {f.name}: "
                          f"cannot reach <{max_mb} MB even at 20% scale")
                    logging.warning(f"ref_compress: {f.name} could not compress PNG below {max_mb}MB")
                    result['errors'] += 1

            else:
                # webp as JPEG fallback
                best_data, best_q = _compress_jpeg(img, max_bytes)
                if best_data:
                    out = f.with_suffix('.jpg')
                    out.write_bytes(best_data)
                    f.unlink()
                    new_kb = len(best_data) // 1024
                    print(f"  {_GREEN}[REF PREFLIGHT]{_RESET} {f.name} → {out.name}: "
                          f"{size_mb:.1f} MB → {new_kb} KB")
                    logging.info(f"ref_compress: {f.name} → {out.name} {size // 1024}KB → {new_kb}KB")
                    result['compressed'] += 1
                else:
                    result['errors'] += 1

        except Exception as e:
            print(f"  {_YELLOW}[REF PREFLIGHT] ERR{_RESET} {f.name}: {e}")
            logging.warning(f"ref_compress: {f.name} error: {e}")
            result['errors'] += 1

    comp = result['compressed']
    errs = result['errors']
    checked = result['checked']
    if comp > 0:
        print(f"  {_GREEN}[REF PREFLIGHT]{_RESET} "
              f"Done: {comp}/{checked} compressed, {errs} error(s)")
    else:
        print(f"  {_GREEN}[REF PREFLIGHT]{_RESET} "
              f"All refs OK — {checked} checked, none exceed {max_mb} MB")
    logging.info(f"ref_compress: done checked={checked} compressed={comp} errors={errs}")

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _compress_jpeg(img, max_bytes: int):
    """Binary-search JPEG quality в [20..100] — берёт МАКСИМАЛЬНОЕ качество, влезающее
    под лимит. Для рефов важно качество лица: берём наивысший q (вплоть до 100),
    который укладывается в max_bytes. Если даже q100 < лимита — это нормально
    (картинка такого разрешения столько и весит при идеальном качестве; раздувать
    до лимита смысла нет — то же изображение, только тяжелее для upload).
    Returns (data, quality) or (None, 0)."""
    lo, hi = 20, 100
    best_data, best_q = None, 0
    while lo <= hi:
        q = (lo + hi) // 2
        buf = BytesIO()
        img.save(buf, 'JPEG', quality=q)
        data = buf.getvalue()
        if len(data) <= max_bytes:
            best_data, best_q = data, q
            lo = q + 1
        else:
            hi = q - 1
    return best_data, best_q


def _compress_png(img, max_bytes: int):
    """Downscale PNG by 10% steps from 90% to 20%. Returns (data, scale) or (None, 0)."""
    from PIL import Image as _Image
    w, h = img.size
    for scale_pct in range(90, 15, -10):
        scale = scale_pct / 100
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        resized = img.resize((nw, nh), _Image.LANCZOS)
        buf = BytesIO()
        resized.save(buf, 'PNG', optimize=True)
        data = buf.getvalue()
        if len(data) <= max_bytes:
            return data, scale
    return None, 0
