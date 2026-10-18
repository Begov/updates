"""
real_photo_markup.py — директива разметки Real Photo-клипов для Pass 1.

Одна точка правды на ОБА директора (chat_director.py и gpt_director.py) — как
stock_markup.py. Модуль намеренно лёгкий: только текст, без импортов поиска/ffmpeg.

Почему не два текста: маркеры уже один раз были размазаны по 7 файлам и это стало
миной (см. clip_sources.py). Директива живёт здесь, директора её зовут.

Real Photo vs Stock
-------------------
Оба модуля берут ЛЮБОЙ конкретный реально-снимаемый субъект (животные, растения,
места, объекты, люди, историческое, космос). Разница:
  Real Photo — источники (Wikimedia/Yandex/NASA/LoC/Met/...) держат ВСЁ: и известные
    личности/места, и историческое, и дженерик.
  Stock      — современная съёмка (Pexels/Pixabay): дженерик и современное есть, а
    известных личностей, известных мест/лендмарков и исторического материала НЕТ.
Поэтому личности/места/историческое — приоритет Real Photo (тиры ниже): их нельзя
отдать стоку или сгенерить AI без фейка. Обычные (не именованные, не исторические)
субъекты — животные, объекты, места — могут достаться любому модулю.

Pass 1 ТОЛЬКО ПОМЕЧАЕТ — поисковые запросы пишет стадия своим оператором
(REAL_PHOTO_PROMPT_PROVIDER), не режиссёр: промптаж раздул бы Pass 1 и рискует
обрезкой на длинных роликах, а на чанках запросы разъехались бы.
"""
from __future__ import annotations

import params as config
def real_photo_markup_directive() -> str:
    """Текст директивы для Pass 1. Пусто, если Real Photo выключен."""
    from units.real_source_policy import (
        director_directive as non_ai_directive,
        enabled as non_ai_enabled,
    )
    if non_ai_enabled():
        return non_ai_directive()
    if not getattr(config, 'REAL_PHOTO_ENABLED', False):
        return ''

    t = int(float(getattr(config, 'REAL_PHOTO_RATIO_TARGET', 0.40)) * 100)
    # Диапазон (min/max) убран 02.09.2026: он НЕ зависел от цели и в окне не
    # показывался вовсе — поставив цель 70%, человек получал в задании
    # «цель 70%, диапазон 30-55%», то есть настройка спорила с невидимыми
    # рамками. Осталась одна цель, ровно та, что задана.
    #
    # Ноль означает НОЛЬ: доля 0 = стадию не размечаем вовсе, иначе режиссёр
    # всё равно помечал клипы (правило пользователя 25.08).
    if t <= 0:
        return ''

    return f"""

---

## REAL PHOTO MARKUP

A separate stage replaces some AI-generated clips with REAL photographs pulled from
Wikimedia, Yandex Images, NASA, Library of Congress, Met, Europeana, Smithsonian.
These sources hold real photos of almost ANY concrete subject. Your job here is ONLY
to MARK suitable clips — a downstream stage writes the search queries, you do NOT.

MARK a clip when its voiceover names or describes a CONCRETE, real, photographable
subject. Choose candidates in PRIORITY ORDER — spend the quota on the top tiers FIRST,
across the WHOLE video, and only fill the remainder with lower tiers:

TIER 1 — mark these wherever they appear, RESERVE quota for them:
  Known / named PEOPLE (living or historical: Einstein, a named politician, a cosmonaut)
  and known / named PLACES & LANDMARKS (Louvre, Red Square, Kremlin, Eiffel Tower, Everest).
  AI generation fakes these badly and stock libraries do NOT have them — a real photo
  is essential here. Do not let a later famous person/place lose its slot because the
  quota was burned on generic clips earlier.
TIER 2:
  Historical material, museum artifacts, named historical events, space / NASA imagery,
  famous artworks — anything where authentic real material matters.
TIER 3 — fill the REMAINING quota:
  Everyday, ordinary REAL subjects — animals, plants, common objects / tools / vehicles /
  food, ordinary places and natural features. These are real photographable things (just
  not named or historical) — a real photo still beats an AI render.

Rule of thumb: "If I searched this on Google / Wikimedia, would real photos of exactly
this appear?" If yes → it is a candidate.

Do NOT mark (leave for AI generation) ONLY when the clip is genuinely un-photographable:
  - Abstract concepts / metaphors ("deep time", "the dawn of humanity", "despair")
  - Pure emotion ("a child's last breath", "tears of joy")
  - Imagined / reconstructed / speculative scenes ("imagine...", "could have...",
    "ancient humans crossing the savanna")
  - Staged interpersonal drama with no concrete subject ("a mother argues with her son")

Target: about {t}% of clips. Treat this as a goal to FILL by priority —
real photos exist for most concrete subjects, so reach the target by marking the clearest
candidates, TIER 1/2 FIRST. Fall short only if the video genuinely lacks enough concrete
subjects. Do NOT mark un-photographable clips just to hit the number.

If clip qualifies → add ONE additional field to that clip:
  "source": "real_photo_pending"
If clip does NOT qualify → do not add any source field.

NOTE: only MARK clips. Do NOT write `search_query`, `reasoning`, `confidence` or any
other fields — those belong to the downstream query stage, which has its own operator.
"""
