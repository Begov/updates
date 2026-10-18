"""Pass 1 directive for AI-generated static infographic clips."""
from __future__ import annotations

import params as config
from pathlib import Path


def infographic_markup_directive() -> str:
    """Return the optional Pass 1 source-markup directive."""
    if not getattr(config, 'INFOGRAPHIC_ENABLED', False):
        return ''

    target_pct = int(float(getattr(config, 'INFOGRAPHIC_RATIO_TARGET', 0.15)) * 100)
    max_pct = int(float(getattr(config, 'INFOGRAPHIC_RATIO_MAX', 0.25)) * 100)
    # Ноль означает НОЛЬ: раньше при доле 0 директива всё равно уходила режиссёру
    # («цель около 0%, но не более 25%»), и клипы под инфографику появлялись —
    # верхнюю границу брали из другого поля (улов пользователя 25.08).
    if target_pct <= 0 or max_pct <= 0:
        return ''
    return f"""

---

## INFOGRAPHIC MARKUP

A separate stage turns selected clips into a STILL information graphic. Mark only
clips that become clearer as a diagram, chart, map, timeline, comparison,
labelled process, or structured data visualization.

GOOD candidates:
- Statistics, quantities, rankings, proportions, before/after comparisons
- Dates, chronological sequences, historical timelines, evolution/process flows
- Geography, routes, maps, scale comparisons, systems and causal relationships
- Clear explanatory lists where a visual hierarchy communicates the point faster

DO NOT mark:
- Character drama, emotional moments, action, dialogue, landscapes, atmosphere
- A clip whose whole point is a specific person, place or object: the viewer
  needs to SEE it, and a diagram cannot show what it looked like
- An ordinary scene that simply needs to be shown rather than explained
- A clip that already carries another `source` — the field holds one value
- Consecutive clips unless the narration genuinely requires separate diagrams

Preferred share: up to about {target_pct}% of all clips. It is the user's PREFERENCE,
not a quota: mark only clips that are genuinely better as a diagram. Never exceed
{max_pct}% and never replace a stronger cinematic, archival, or stock choice merely
to fill it — unmarked clips are covered another way.

If a clip qualifies, add exactly:
  "source": "infographic"

The style guide may contain an INFOGRAPHIC / ИНФОГРАФИКА section. If it does not,
Pass 2 will use the user's editable default infographic style. Only MARK here;
do not write a prompt, chart data, or extra source fields.
"""


_LANGUAGE_NAMES = {
    'en': 'English',
    'ru': 'Russian',
    'es': 'Spanish',
    'fr': 'French',
    'de': 'German',
    'it': 'Italian',
    'pt': 'Portuguese',
    'nl': 'Dutch',
    'pl': 'Polish',
    'ja': 'Japanese',
    'zh': 'Chinese',
    'ko': 'Korean',
    'hi': 'Hindi',
    'ar': 'Arabic',
    'uk': 'Ukrainian',
    'tr': 'Turkish',
}


def infographic_text_language(language_code: str | None = None) -> tuple:
    """(код, англ. название) языка надписей на инфографике.

    Берём язык озвучки пайплайна (ASSEMBLYAI_LANGUAGE_CODE) — испанский ролик
    должен получить испанские подписи, а не английские."""
    code = (language_code or getattr(config, 'ASSEMBLYAI_LANGUAGE_CODE', 'en') or 'en')
    code = str(code).strip().lower()
    base = code.split('-')[0].split('_')[0]
    return code, _LANGUAGE_NAMES.get(base, _LANGUAGE_NAMES.get(code, base.upper()))


def infographic_style_anchor_rule() -> str:
    """Инфографика должна выглядеть частью того же фильма, а не чужой вставкой.

    Раньше при отсутствии секции INFOGRAPHIC в стайлгайде бралcя нейтральный
    редакционный шаблон — и диаграмма контрастировала с общим видеорядом."""
    return (
        "- STYLE ANCHOR: even when the style guide has NO dedicated infographic "
        "section, derive the look FROM THE STYLE GUIDE anyway — same colour "
        "palette, same era and materials, same lighting/grain/texture treatment, "
        "same level of realism. The infographic must read as a frame of the SAME "
        "film: an archival chart in an archival film, a clean modern diagram in a "
        "modern one. The default style below governs only legibility (hierarchy, "
        "spacing, large labels), never the palette or mood."
    )


def infographic_language_rule(language_code: str | None = None) -> str:
    """Правило языка НАДПИСЕЙ на кадре (сам промпт остаётся английским)."""
    code, name = infographic_text_language(language_code)
    rule = (
        f"- ON-IMAGE TEXT LANGUAGE: the prompt itself stays in English, but every "
        f"word that must appear INSIDE the picture (title, labels, captions, axis "
        f"names, legend, units, callouts) MUST be written in {name} ({code}) - the "
        f"voiceover language of this project. Spell that text out in the prompt "
        f"exactly as it has to be rendered, e.g. "
        f"labels read \"...\" written in {name}."
    )
    if name != 'English':
        rule += (" Do NOT label the infographic in English; English labels on a "
                 f"{name} video are a defect.")
    return rule


def default_infographic_style() -> str:
    """Load the GUI-editable fallback style used when a style guide lacks one."""
    path = Path(__file__).resolve().parent.parent / 'instructions' / 'infographic_default_style.txt'
    try:
        text = path.read_text(encoding='utf-8-sig').strip()
        if text:
            return text
    except OSError:
        pass
    return (
        'Clean editorial infographic, strong hierarchy, readable large labels, '
        'limited palette, generous spacing, no decorative clutter, no watermark.'
    )


