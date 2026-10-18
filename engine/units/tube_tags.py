"""
youtube_markup.py — директива разметки YouTube-клипов для Pass 1.

Одна точка правды на обоих директоров (`chat_director.py`, `gpt_director.py`) —
как `stock_markup.py` и `real_photo_markup.py`. Только текст, без сети.

Что умеет источник
------------------
YouTube — архив всего, что человечество снимало на видео. Это шире и стока, и
Real Photo: там есть и узкие ремесленные процессы, которых нет ни на одном
фотостоке, и названные люди, речи, хроника событий, и обычная натура.

Критерий разметки поэтому один и широкий: **снимал ли это кто-нибудь на видео**.
Снимали почти всё.

Почему директиву переписали (26.08)
-----------------------------------
Прежний текст отдавал ютубу клипы по признаку ДВИЖЕНИЯ («actions that benefit
from visible motion») и прямо запрещал названных людей — «Do not mark: named
people or exact historical claims». Обоснования этому запрету нет ни в коде, ни
в плане внедрения (`docs/YOUTUBE_FOOTAGE_IMPLEMENTATION_PLAN.md`).

Запрет бил именно туда, где ютуб незаменим: видео конкретного человека или
события больше взять негде — сток снят сегодня и дженериковый, Real Photo даст
неподвижный кадр. Результат был предсказуем: за два прогона 26.08 режиссёр не
пометил ютубом НИ ОДНОГО клипа, и всю долю добирал маршрутизатор по словарю слов
движения — из тех клипов, что никто не взял.

Источники описываются тем, что умеют САМИ, а не «чем не является сосед»: стадии
включаются независимо, и критерий, сужённый под соседа, оставит режиссёра без
кандидатов, как только тот сосед выключен.

Pass 1 ТОЛЬКО ПОМЕЧАЕТ — поисковые запросы пишет стадия своим оператором
(YOUTUBE_SELECTOR_PROVIDER), не режиссёр.
"""
from __future__ import annotations

import params as config
def youtube_markup_directive() -> str:
    """Текст директивы для Pass 1. Пусто, если ютуб выключен или доля 0."""
    if not getattr(config, "YOUTUBE_FOOTAGE_ENABLED", False):
        return ""

    target_pct = int(float(getattr(config, "YOUTUBE_RATIO_TARGET", 0.0)) * 100)
    # Ноль означает НОЛЬ: доля 0 = ютуб-клипов в разметке нет (правило 25.08).
    if target_pct <= 0:
        return ""
    max_pct = int(float(getattr(config, "YOUTUBE_RATIO_MAX", 0.0) or 0) * 100)
    range_note = f" (up to {max_pct}%)" if max_pct > target_pct else ""

    return f"""

---

## YOUTUBE FOOTAGE MARKUP

A separate stage replaces some AI-generated clips with REAL video footage found
on YouTube. YouTube holds recordings of almost anything that was ever filmed —
craft and production processes, machinery at work, nature, weather, animals,
cities and roads, interiors, experiments, sports, reports, speeches, historical
newsreel. Your job here is ONLY to MARK suitable clips — a downstream stage
writes the search queries, you do NOT.

MARK a clip when its voiceover describes something that people actually FILM.
Choose candidates in PRIORITY ORDER — spend the quota on the top tiers FIRST,
across the WHOLE video:

TIER 1 — mark these wherever they appear, RESERVE quota for them:
  Named PEOPLE, their speeches and appearances; named EVENTS and newsreel
  footage; named PLACES filmed on camera. Footage of these exists and carries
  what only a recording can: the voice, the gesture, the course of the moment.
TIER 2:
  Concrete processes and crafts — how a thing is made, repaired, harvested,
  cooked, assembled; machinery and vehicles in operation; laboratories and
  production lines; specific animals and their behaviour. Narrow subjects like
  these are filmed in abundance.
TIER 3 — fill the REMAINING quota:
  Ordinary real footage — landscapes, weather, streets, crowds, interiors,
  everyday actions.

Rule of thumb: "If I searched this on YouTube, would real footage of exactly
this come up?" If yes → it is a candidate.

A clip can suit this stage AND a photo archive at the same time — that is
normal. Decide per clip by what the moment needs, and do not pull everything
concrete here: the shares keep both stages fed.

Do NOT mark ONLY when:
  - nobody could have filmed it: abstract concepts and metaphors, imagined or
    speculative scenes, reconstructions of an unfilmed past;
  - a static frame is clearly better: a card with figures, dates or a
    comparison, which belongs to the infographic stage.

Target: about {target_pct}% of clips{range_note}. Reach it by marking the
clearest candidates, TIER 1/2 first. Fall short only if the video genuinely
lacks filmable subjects — do NOT mark unfilmable clips to hit the number.

If clip qualifies → add ONE additional field to that clip:
  "source": "youtube_pending"
If clip does NOT qualify → do not add any source field.

NOTE: only MARK clips. Do NOT invent video IDs, URLs, timestamps or any other
fields — the YouTube stage creates those after Pass 1.
"""
