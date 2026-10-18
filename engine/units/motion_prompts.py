"""Optional Pass 2 instructions for direct text-to-video runs."""

from __future__ import annotations

import os
from pathlib import Path

import params as config
_T2V_TARGETS = {"t2v", "intro_i2v_t2v"}


def t2v_prompting_enabled() -> bool:
    target = (os.getenv("BEGO_PROMPT_TARGET", "") or "").strip().lower()
    return target in _T2V_TARGETS


def pass2_video_directive() -> str:
    """Return the editable T2V directive only when the run needs direct video."""
    if not t2v_prompting_enabled():
        return ""

    path = Path(config.PROMPTS_DIR) / "director_instruction_t2v.txt"
    try:
        text = path.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return ""
    return f"\n\n# DIRECT TEXT-TO-VIDEO REQUIREMENTS\n{text}\n"
