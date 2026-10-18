"""Pass 1 directive for animated motion-graphics clips (Remotion + Codex).

Режиссёр только РАЗМЕЧАЕТ клип (`source: motion_graphics_pending`) и одной
фразой описывает идею анимации (`mograph_brief`). Сцену пишет Codex на стадии
mograph_runner.py, рендерит Remotion.
"""
from __future__ import annotations

import params as config


def motion_graphics_markup_directive() -> str:
    """Return the optional Pass 1 source-markup directive."""
    if not getattr(config, 'MOTION_GRAPHICS_ENABLED', False):
        return ''

    target_pct = int(round(float(getattr(config, 'MOTION_GRAPHICS_RATIO_TARGET', 0.10)) * 100))
    # Ноль означает НОЛЬ: доля 0 = графики в разметке нет (как у стока и инфографики).
    if target_pct <= 0:
        return ''
    infographic_note = ''
    if getattr(config, 'INFOGRAPHIC_ENABLED', False):
        infographic_note = (
            "\nInfographics (`\"source\": \"infographic\"`) are STILL images. Choose motion "
            "graphics instead when the point is a CHANGE over time — a number growing, a "
            "route being drawn, a sequence revealing step by step."
        )
    return f"""

---

## MOTION GRAPHICS MARKUP

A separate stage replaces selected clips with an ANIMATED motion graphic (rendered
in code: kinetic typography, animated charts, counters, maps, timelines, diagrams).
Mark only clips that become clearer and more engaging as an animation.

GOOD candidates:
- Numbers, statistics, prices, growth or decline — animated counters and charts
- Dates and sequences — a timeline that unrolls, steps that appear one by one
- Routes, distances, scale, geography — a line drawn across a simple map
- Comparisons, rankings, proportions — bars racing, shares filling up
- A key phrase or quote that deserves emphasis — kinetic typography
- Abstract processes and systems — nodes and arrows building up a scheme

DO NOT mark:
- Character drama, emotions, action, atmosphere, landscapes — they need real footage
- A clip whose point is to SHOW a specific person, place or object
- The opening hook of the video
- A clip that already carries another `source` — the field holds one value
- Long runs of consecutive clips: motion graphics are an accent, not the whole film{infographic_note}

Preferred share: up to about {target_pct}% of all clips. It is the user's PREFERENCE,
not a quota: never exceed it and never mark a weak candidate just to fill it —
unmarked clips are covered another way.

If a clip qualifies, add exactly these two fields:
  "source": "motion_graphics_pending",
  "mograph_brief": "<one sentence in English: what the animation shows — the data,
                    labels and the motion, e.g. 'Counter rises from 0 to 1,914 km
                    while a dotted line crosses a minimal map from Lisbon to Rio'>"
Use the real numbers, names and dates from the narration in the brief.
"""
