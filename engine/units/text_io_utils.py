from __future__ import annotations

from pathlib import Path


def read_user_text(path: str | Path) -> str:
    """Read user-supplied text files saved by macOS/Windows editors."""
    p = Path(path)
    data = p.read_bytes()

    if data.startswith(b'\xff\xfe\x00\x00'):
        return data.decode('utf-32-le')
    if data.startswith(b'\x00\x00\xfe\xff'):
        return data.decode('utf-32-be')
    if data.startswith(b'\xff\xfe') or data.startswith(b'\xfe\xff'):
        return data.decode('utf-16')
    if data.startswith(b'\xef\xbb\xbf'):
        return data.decode('utf-8-sig')

    sample = data[:200]
    if sample:
        odd_nuls = sample[1::2].count(0)
        even_nuls = sample[0::2].count(0)
        half = max(1, len(sample) // 4)
        if odd_nuls > half:
            return data.decode('utf-16-le')
        if even_nuls > half:
            return data.decode('utf-16-be')

    for enc in ('utf-8', 'cp1251', 'mac_roman'):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass

    return data.decode('utf-8', errors='replace')
