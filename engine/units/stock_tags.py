"""
stock_markup.py — директива разметки сток-клипов для Pass 1.

Одна точка правды на ОБА директора (`chat_director.py` и `gpt_director.py`).
Модуль намеренно лёгкий: только текст, без импортов поиска/ffmpeg — его тянут
директора, им лишние зависимости не нужны.

Почему не два текста: маркеры уже один раз были скопированы в 7 файлов и это
стало миной (см. clip_sources.py). Директива живёт здесь, директора её зовут.

Взаимодействие с Real Photo
---------------------------
Оба модуля берут ЛЮБОЙ конкретный реально-снимаемый субъект. Разница только в том,
чего у стока НЕТ: известных личностей, известных мест/лендмарков (Лувр, Красная
площадь) и исторического материала — это отдаём Real Photo. Всё остальное (дженерик,
современное, вневременно́е) сток тянет сам. Поле `source` одно, поэтому клип получает
либо тот маркер, либо этот; режиссёр решает per-clip и добивает оба таргета.

Pass 1 ТОЛЬКО ПОМЕЧАЕТ — запросы пишет стадия
---------------------------------------------
Задача Pass 1 — монтажный план: нарезка и тайминг. Ничего больше.
Поисковые запросы (`stock_query`) и выбор video/photo делает стоковая стадия
своим оператором (STOCK_PROMPT_PROVIDER) — ровно как Real Photo делает это
отдельным промптером, а не руками режиссёра.

Почему так, а не «заодно и запрос напишет»:
  • промптаж раздувает ответ Pass 1 → лишние токены и риск обрезки на длинных роликах;
  • Pass 1 чанкуется на длинном аудио (DIRECTOR_PASS1_CHUNK_THRESHOLD) — запросы
    разъехались бы по чанкам;
  • режиссёр может быть дорогой моделью, а сочинить «lightning storm clouds» —
    работа для дешёвой;
  • у стадии свой оператор ещё и сопоставляет локальную библиотеку — режиссёр
    про её содержимое не знает вовсе.
"""
from __future__ import annotations

import params as config
def stock_markup_directive() -> str:
    """Текст директивы для Pass 1. Пусто, если сток выключен."""
    if not getattr(config, 'STOCK_ENABLED', False):
        return ''

    target_pct = int(float(getattr(config, 'STOCK_RATIO_TARGET', 0.30)) * 100)
    # Ноль означает НОЛЬ: доля 0 = стока в разметке нет (правило 25.08).
    if target_pct <= 0:
        return ''
    # (доля видео/фото в директиву не идёт — её решает сама стоковая стадия)

    return f"""

---

## STOCK FOOTAGE MARKUP

A separate stage replaces some AI-generated clips with REAL modern stock material
(Pexels / Pixabay / the user's own footage library). Mark suitable candidates AND
write a short search query for each.

GOOD candidates (mark them):
- Generic contemporary scenes: nature, landscapes, weather, seasons
- City life, traffic, crowds, streets, architecture (unnamed/generic)
- Technology, machinery, laboratories, screens, industry
- Everyday human actions filmed today: walking, working, cooking, typing, driving
  (only when the scene itself is set TODAY — see the PERIOD TEST below)
- Atmospheric/establishing shots: fog, rain, waves, clouds, forest, fire
- Generic objects and textures: money, documents, food, plants, tools

BAD candidates (do NOT mark) — stock is MODERN footage and lacks public/historical material:
- PUBLIC FIGURES (living or historical) — Einstein, a head of state, a celebrity.
  Stock has no footage of them → Real Photo.
  TEST: would this person have their own encyclopedia article? If yes → Real Photo.
  If you have never heard of them → they are an ordinary person, mark as stock.
  A person simply HAVING a name is NOT a disqualifier: a farmer, neighbour, worker
  or interviewee the narration happens to name is a generic person — stock has
  thousands of such shots.
- LANDMARKS with their own encyclopedia article (Louvre, Red Square, Eiffel Tower).
  Stock has generic buildings, not THE landmark → Real Photo.
  Same test: no article → generic place. An unnamed street, a village, a garden,
  a house, a field, a local shop are all generic — mark them.
- Historical MATERIAL needing period authenticity — historical events, periods, era-specific
  scenes/objects (WW2 battle, medieval city, 1920s street). Modern stock inside a historical
  scene looks absurd → Real Photo.
- Fictional / speculative / imagined scenes.
- Clips already marked "source": "real_photo_pending".

FIRST, THE HARD RULE. If the clip's own narration DATES the scene to the past — a
year ("in 1345", "in 1983"), a decade ("the eighties"), a reign, a war, a named
historical event — that clip is NEVER stock, whatever it shows. Stock footage is
filmed today; dropped into a scene the narration has just dated, it contradicts the
words themselves. Send it to Real Photo and move on.

A date pointing at TODAY does not disqualify anything: "today", "now", "this year"
are exactly what stock is for.

PERIOD TEST — for every clip the hard rule did not disqualify, one question: would
the SHOT ITSELF reveal its period?

Judge the CONTENT OF THE FRAME, not the subject of the film: a film about the past
can still contain shots that reveal no period at all.

A shot reveals its period when the era is visible in it: people, their clothing and
grooming; the everyday objects around them; vehicles; storefronts, signage, printed
matter and packaging. Modern stock cannot supply those for a scene set in another
time — they belong to the archive.

A shot reveals nothing about its period when everything in frame looks the same
across eras:
- nature, weather, water, fire, seasons, landscape;
- materials and textures — wood, metal, stone, fabric, dust, smoke, steam;
- close-ups of food, produce, raw material, tools, hands at work;
- process and craft — mixing, pouring, cutting, forging, assembling;
- places without people or period fittings — a hall, a staircase, a corridor, a
  road, a field, a wall, a shoreline.
Such a shot may be marked whatever era the film covers — it carries the scene
without contradicting it.

A wrong stock clip is worse than none: it puts today's world into a scene that is
not today, and the viewer sees it instantly. So the tests above are absolute —
never mark named people, named places or period-revealing material to reach a
number.

Preferred share: up to about {target_pct}% of clips. It is the user's PREFERENCE,
not a quota: you decide which clips are better as real footage. Work through the
whole plan and mark the clips that pass the tests and would genuinely look right as
a real shot. Most films have more timeless shots than they look like at first
glance — nature, materials, close-ups of food and produce, hands at work, process
and craft, places without people — so look for them deliberately.

But never mark a doubtful clip to get closer to the number: clips you leave
unmarked are covered another way, so falling short costs nothing, while a stock
shot that does not fit the scene spoils it.

If clip qualifies → add ONE additional field to that clip:
  "source": "stock_pending"

If clip does NOT qualify → do not add any source field.

Example output:
{{"id": 5, "file": "5.mp4", "startTime": 0.0, "endTime": 4.8, "source": "stock_pending"}},
{{"id": 6, "file": "6.mp4", "startTime": 4.8, "endTime": 9.2}},

NOTE: only MARK clips. Do NOT write `stock_query`, `stock_media`, `reasoning` or any
other fields — those belong to the downstream stock stage, which has its own operator.
"""
