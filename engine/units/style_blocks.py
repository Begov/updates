"""
Вынесенные блоки стиля и негатива из style_blocks.json проекта.

Файл `<проект>/style_blocks.json` ({"style_block": ..., "negative_block": ...}) можно
положить или поправить вручную — тогда Pass 2 не пишет эти блоки в каждый промпт,
а они дописываются скриптом. Нет файла — блоки пишет сама модель.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict


def resolve_style_blocks(style_guide: str, output_dir) -> Dict[str, str]:
    """{style_block, negative_block} из <output_dir>/style_blocks.json или {} (модель пишет блоки сама)."""
    cache = Path(output_dir) / 'style_blocks.json'
    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding='utf-8-sig'))
            if isinstance(data, dict):
                return {'style_block': str(data.get('style_block', '') or ''),
                        'negative_block': str(data.get('negative_block', '') or '')}
        except Exception:
            pass
    return {}
