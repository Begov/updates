"""Word timing, scene normalization and context packets for Auto Visual Story."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable


DurationPolicy = Callable[[float], tuple[float, float, float]]


def normalize_words(transcription: dict[str, Any]) -> list[dict[str, Any]]:
    raw = transcription.get("words")
    if not isinstance(raw, list) or not raw:
        segments = transcription.get("transcription") or []
        if segments and all(len(str(item.get("text", "")).split()) == 1 for item in segments):
            raw = segments
        else:
            raise ValueError(
                "Auto Visual Story requires word timestamps. Re-transcribe the audio "
                "with a build that stores transcription.words[]."
            )

    words: list[dict[str, Any]] = []
    previous_end = -1.0
    for index, item in enumerate(raw):
        text = str(item.get("text") or "").strip()
        start = float(item.get("start", 0) or 0)
        end = float(item.get("end", 0) or 0)
        if not text or end <= start:
            raise ValueError(f"Invalid word at index {index}: {item!r}")
        if start < previous_end - 0.25:
            raise ValueError(f"Word timeline moves backwards at index {index}")
        words.append({
            "index": index,
            "text": text,
            "start": round(start, 3),
            "end": round(end, 3),
        })
        previous_end = end
    return words


def scene_text(words: list[dict[str, Any]], start: int, end: int) -> str:
    return " ".join(item["text"] for item in words[start:end + 1]).strip()


def _boundary_time(
    words: list[dict[str, Any]],
    start_index: int,
    audio_duration: float,
) -> float:
    if start_index <= 0:
        return 0.0
    if start_index >= len(words):
        return float(audio_duration)
    return float(words[start_index]["start"])


def _duration(
    words: list[dict[str, Any]],
    start: int,
    end: int,
    audio_duration: float,
) -> float:
    return _boundary_time(words, end + 1, audio_duration) - _boundary_time(
        words, start, audio_duration
    )


def _raw_ranges(
    words: list[dict[str, Any]],
    raw_scenes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not raw_scenes:
        raise ValueError("Scene planner returned no scenes")
    result: list[dict[str, Any]] = []
    expected_start = 0
    for position, raw in enumerate(raw_scenes, 1):
        try:
            declared_start = int(raw.get("word_start_index", expected_start))
            end = int(raw["word_end_index"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Scene {position} has invalid word indices") from exc
        # Как у стандартного режиссёра: мелкий разрыв или наложение на стыке —
        # замечание в журнал, не смерть прогона (улов 25.08: живой прогон лёг
        # из-за ОДНОГО пропущенного слова — сцена 67 началась с 961 вместо 960).
        # Разрыв поглощает текущая сцена (она начинается с ожидаемого слова),
        # наложение обрезается. Падение — только на сломанном порядке.
        if declared_start != expected_start:
            kind = "разрыв" if declared_start > expected_start else "наложение"
            logging.warning(
                "Visual Story: сцена %s начинается со слова %s, ожидалось %s — "
                "%s в %s слов(а) поправлен (замечание, не ошибка)",
                position, declared_start, expected_start, kind,
                abs(declared_start - expected_start),
            )
        if end >= len(words):
            raise ValueError(f"Scene {position} has invalid end word {end}")
        if end < expected_start:
            # Сцена целиком внутри предыдущей — это наложение, а не сломанный
            # порядок: политика выше обещает такое обрезать, а не ронять прогон.
            # Падение здесь стоило бы всего анализа из-за одной лишней сцены
            # (аудит 25.08).
            logging.warning(
                "Visual Story: сцена %s (%s..%s) целиком внутри предыдущей — "
                "пропущена (замечание, не ошибка)",
                position, raw.get("word_start_index"), end,
            )
            continue
        item = dict(raw)
        item["word_start_index"] = expected_start
        item["word_end_index"] = end
        result.append(item)
        expected_start = end + 1
    if expected_start < len(words):
        # Хвост без сцены — та же логика: последняя сцена его поглощает.
        logging.warning(
            "Visual Story: план покрыл %s/%s слов — хвост в %s слов(а) отдан "
            "последней сцене (замечание, не ошибка)",
            expected_start, len(words), len(words) - expected_start,
        )
        result[-1]["word_end_index"] = len(words) - 1
    return result


def _split_long_ranges(
    words: list[dict[str, Any]],
    ranges: list[dict[str, Any]],
    audio_duration: float,
    target_duration: float,
    max_duration: float,
    duration_policy: DurationPolicy | None = None,
) -> list[dict[str, Any]]:
    split: list[dict[str, Any]] = []
    for raw in ranges:
        start = int(raw["word_start_index"])
        final_end = int(raw["word_end_index"])
        while True:
            start_time = _boundary_time(words, start, audio_duration)
            _, target_limit, max_limit = (
                duration_policy(start_time)
                if duration_policy
                else (0.0, target_duration, max_duration)
            )
            if _duration(words, start, final_end, audio_duration) <= max_limit + 0.01:
                break
            candidates = []
            for end in range(start, final_end):
                duration = _duration(words, start, end, audio_duration)
                if duration <= max_limit + 0.01:
                    candidates.append((abs(duration - target_limit), end))
            if not candidates:
                raise ValueError(
                    f"Cannot split scene starting at word {start} under {max_limit}s"
                )
            split_end = min(candidates)[1]
            part = dict(raw)
            part["word_start_index"] = start
            part["word_end_index"] = split_end
            split.append(part)
            start = split_end + 1
        part = dict(raw)
        part["word_start_index"] = start
        part["word_end_index"] = final_end
        split.append(part)
    return split


def _absorb_scene(keeper: dict, eaten: dict) -> None:
    """Перенести в выжившую сцену всё, что несла поглощённая.

    Раньше переносились только границы и описание, а `chapter_id`, роль и
    сущности терялись. На стыке глав это уводило слипшуюся сцену в ЧУЖУЮ главу:
    этап ② получал для её слов сводку соседней главы (другая эпоха и место), а
    если у главы была единственная короткая сцена — глава оставалась пустой и
    прогон падал с диагнозом на этап ① (аудит 25.08).
    """
    keeper["description"] = " / ".join(
        value for value in (
            str(keeper.get("description") or "").strip(),
            str(eaten.get("description") or "").strip(),
        ) if value
    )
    # Глава: если сцены из разных глав — оставляем главу той, что начинается
    # раньше (keeper), но помечаем, что внутрь попали слова соседней.
    eaten_chapter = str(eaten.get("chapter_id") or "").strip()
    keeper_chapter = str(keeper.get("chapter_id") or "").strip()
    if eaten_chapter and keeper_chapter and eaten_chapter != keeper_chapter:
        spans = list(keeper.get("spans_chapters") or [keeper_chapter])
        if eaten_chapter not in spans:
            spans.append(eaten_chapter)
        keeper["spans_chapters"] = spans
    elif eaten_chapter and not keeper_chapter:
        keeper["chapter_id"] = eaten_chapter
    # Сущности объединяем: в кадре теперь и те, и другие.
    entities = list(keeper.get("entities") or [])
    for item in (eaten.get("entities") or []):
        if item not in entities:
            entities.append(item)
    if entities:
        keeper["entities"] = entities
    # Роль: «событие» важнее фоновых ролей — его и оставляем.
    if str(eaten.get("scene_role") or "") == "event":
        keeper["scene_role"] = "event"


def _same_chapter(first: dict, second: dict) -> bool:
    """Можно ли склеивать эти сцены: только внутри одной главы.

    Граница главы — смысловой рубеж, и склейка через неё дважды вредна: сцена
    уезжает в чужую эпоху и место, а глава, где короткая сцена была
    единственной, остаётся БЕЗ сцен и роняет прогон (`Chapters without
    scenes`) — прогон 6 от 25.08 лёг именно так. Короткая сцена на своём месте
    лучше, чем сломанная глава: длительности и так проверяются мягко.
    """
    left = str(first.get("chapter_id") or "").strip()
    right = str(second.get("chapter_id") or "").strip()
    return not left or not right or left == right


def _merge_short_ranges(
    words: list[dict[str, Any]],
    ranges: list[dict[str, Any]],
    audio_duration: float,
    min_duration: float,
    max_duration: float,
    duration_policy: DurationPolicy | None = None,
) -> list[dict[str, Any]]:
    result = [dict(item) for item in ranges]
    index = 0
    while index < len(result):
        current = result[index]
        duration = _duration(
            words,
            int(current["word_start_index"]),
            int(current["word_end_index"]),
            audio_duration,
        )
        current_start_time = _boundary_time(
            words, int(current["word_start_index"]), audio_duration
        )
        min_limit, _, max_limit = (
            duration_policy(current_start_time)
            if duration_policy
            else (min_duration, 0.0, max_duration)
        )
        if duration >= min_limit - 0.01:
            index += 1
            continue

        merged = False
        if index + 1 < len(result):
            following = result[index + 1]
            combined = _duration(
                words,
                int(current["word_start_index"]),
                int(following["word_end_index"]),
                audio_duration,
            )
            if combined <= max_limit + 0.01 and _same_chapter(current, following):
                current["word_end_index"] = following["word_end_index"]
                _absorb_scene(current, following)
                result.pop(index + 1)
                merged = True
        if not merged and index > 0:
            previous = result[index - 1]
            previous_start_time = _boundary_time(
                words, int(previous["word_start_index"]), audio_duration
            )
            _, _, previous_max_limit = (
                duration_policy(previous_start_time)
                if duration_policy
                else (0.0, 0.0, max_duration)
            )
            combined = _duration(
                words,
                int(previous["word_start_index"]),
                int(current["word_end_index"]),
                audio_duration,
            )
            if combined <= previous_max_limit + 0.01 and _same_chapter(previous, current):
                previous["word_end_index"] = current["word_end_index"]
                _absorb_scene(previous, current)
                result.pop(index)
                index = max(0, index - 1)
                merged = True
        if not merged:
            # Стандартный режиссёр здесь НЕ падает: нарушение мин/макс — это
            # «замечание» в лог, конкат собирает сцену любой длины
            # (gpt_director: «Валидация: N замечаний (авто-коррекция)»).
            # Ведём себя так же: хвост приклеивается к соседу с наименьшим
            # перебором максимума; сироте без соседей остаётся короткая сцена.
            forward_over = backward_over = None
            # Даже аварийная склейка не пересекает границу главы: иначе глава,
            # где короткая сцена была единственной, остаётся без сцен и прогон
            # падает «Chapters without scenes» (прогон 6 от 25.08).
            if index + 1 < len(result) and _same_chapter(current, result[index + 1]):
                following = result[index + 1]
                forward_over = _duration(
                    words,
                    int(current["word_start_index"]),
                    int(following["word_end_index"]),
                    audio_duration,
                ) - max_limit
            if index > 0 and _same_chapter(result[index - 1], current):
                previous = result[index - 1]
                previous_start_time = _boundary_time(
                    words, int(previous["word_start_index"]), audio_duration
                )
                _, _, previous_max_limit = (
                    duration_policy(previous_start_time)
                    if duration_policy
                    else (0.0, 0.0, max_duration)
                )
                backward_over = _duration(
                    words,
                    int(previous["word_start_index"]),
                    int(current["word_end_index"]),
                    audio_duration,
                ) - previous_max_limit
            if forward_over is None and backward_over is None:
                logging.warning(
                    "Visual Story: сцена со слова %s короче %.1fс и не имеет "
                    "соседей в своей главе — остаётся короткой",
                    current["word_start_index"], min_limit,
                )
                index += 1
                continue
            if backward_over is not None and (
                forward_over is None or backward_over <= forward_over
            ):
                previous = result[index - 1]
                previous["word_end_index"] = current["word_end_index"]
                _absorb_scene(previous, current)
                result.pop(index)
                index = max(0, index - 1)
                logging.warning(
                    "Visual Story: короткий хвост приклеен назад, сцена "
                    "превысила максимум на %.1fс (замечание, не ошибка)",
                    max(0.0, backward_over),
                )
            else:
                following = result[index + 1]
                current["word_end_index"] = following["word_end_index"]
                _absorb_scene(current, following)
                result.pop(index + 1)
                logging.warning(
                    "Visual Story: короткая сцена приклеена вперёд, итог "
                    "превысил максимум на %.1fс (замечание, не ошибка)",
                    max(0.0, forward_over),
                )
    return result


def materialize_scenes(
    words: list[dict[str, Any]],
    raw_scenes: list[dict[str, Any]],
    audio_duration: float,
    *,
    min_duration: float = 3.0,
    target_duration: float = 7.0,
    max_duration: float = 12.0,
    duration_policy: DurationPolicy | None = None,
) -> list[dict[str, Any]]:
    if audio_duration < min_duration:
        raise ValueError(
            f"Audio duration {audio_duration:.2f}s is shorter than the current "
            f"montage minimum {min_duration:.2f}s"
        )
    ranges = _raw_ranges(words, raw_scenes)
    ranges = _split_long_ranges(
        words,
        ranges,
        audio_duration,
        target_duration,
        max_duration,
        duration_policy=duration_policy,
    )
    ranges = _merge_short_ranges(
        words,
        ranges,
        audio_duration,
        min_duration,
        max_duration,
        duration_policy=duration_policy,
    )

    scenes: list[dict[str, Any]] = []
    for position, raw in enumerate(ranges, 1):
        start = int(raw["word_start_index"])
        end = int(raw["word_end_index"])
        scene = dict(raw)
        scene.update({
            "id": position,
            "word_start_index": start,
            "word_end_index": end,
            "startTime": round(_boundary_time(words, start, audio_duration), 3),
            "endTime": round(_boundary_time(words, end + 1, audio_duration), 3),
            "text": scene_text(words, start, end),
            # ВИЗУАЛЬНОЕ описание кадра пишет этап ②, а не этап ①: его схема
            # такого поля не знает и промпт прямо запрещает описывать экран.
            # Раньше сюда подставлялся текст диктора, и «описанием кадра» по
            # всему пайплайну оказывалась фраза озвучки — она уходила в пакеты
            # контекста и в задание вижну (улов пользователя 25.08). Пусто
            # честнее: этап ② заполнит, а до него описания просто нет.
            "description": str(
                scene.get("description") or scene.get("visual_thought") or ""
            ).strip(),
            "scene_role": str(scene.get("scene_role") or "event").strip(),
            "entities": list(scene.get("entities") or []),
            "chapter_id": str(scene.get("chapter_id") or "chapter_1"),
        })
        scenes.append(scene)
    return scenes


class ContextWindowBuilder:
    def __init__(self, before: int = 2, after: int = 2, history: int = 4):
        self.before = max(0, int(before))
        self.after = max(0, int(after))
        self.history = max(0, int(history))

    @staticmethod
    def _entity_registry(narrative_state: dict[str, Any]) -> dict[str, Any]:
        """Реестр сущностей как словарь по id.

        Модель отдаёт его массивом: строгий JSON-режим не принимает объекты со
        свободными ключами. Старые артефакты (до 22.08) хранят словарь — их
        читаем как есть, иначе resume по готовому narrative_state.json падал бы.
        """
        raw = narrative_state.get("entities")
        if isinstance(raw, dict):
            return raw
        registry: dict[str, Any] = {}
        for item in raw or []:
            if not isinstance(item, dict):
                continue
            key = str(item.get("id") or "").strip()
            if key:
                registry[key] = item
        return registry

    @staticmethod
    def _scene_overrides(narrative_state: dict[str, Any]) -> dict[int, dict[str, Any]]:
        """Пер-сценный слой непрерывности, разложенный по номеру сцены.

        Его производит этап narrative_state: он один видит весь сюжет и знает,
        кто такие «она» и «тот зал». До 22.08 слоя не было вовсе — тогда карта
        пустая, и пакеты собираются как раньше.
        """
        overrides: dict[int, dict[str, Any]] = {}
        for item in narrative_state.get("scene_overrides") or []:
            if not isinstance(item, dict):
                continue
            try:
                scene_id = int(item.get("scene_id"))
            except (TypeError, ValueError):   # нечисловое поле в пакете — пропускаем
                continue
            overrides[scene_id] = item
        return overrides

    @staticmethod
    def _references_map(raw: Any) -> dict[str, str]:
        """[{phrase, entity_id}] → {фраза: id}. Словарь на входе принимаем как есть."""
        if isinstance(raw, dict):
            return {str(k): str(v) for k, v in raw.items()}
        pairs: dict[str, str] = {}
        for item in raw or []:
            if not isinstance(item, dict):
                continue
            phrase = str(item.get("phrase") or "").strip()
            entity_id = str(item.get("entity_id") or "").strip()
            if phrase and entity_id:
                pairs[phrase] = entity_id
        return pairs

    @staticmethod
    def _clean_constraints(raw: Any) -> dict[str, Any]:
        """Убрать пустые ключи ограничений: пустое значение — это «нечего сказать»."""
        if not isinstance(raw, dict):
            return {}
        return {
            key: value for key, value in raw.items()
            if value not in (None, "", [], {})
        }

    def build_all(
        self,
        scenes: list[dict[str, Any]],
        narrative_state: dict[str, Any],
        visual_history: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        chapters = narrative_state.get("chapters") or []
        entity_registry = self._entity_registry(narrative_state)
        overrides = self._scene_overrides(narrative_state)
        history = list(visual_history or [])
        packets: list[dict[str, Any]] = []
        for index, scene in enumerate(scenes):
            chapter_id = scene.get("chapter_id")
            chapter = next(
                (item for item in chapters if item.get("id") == chapter_id),
                {},
            )
            # Слой непрерывности берём из narrative_state по номеру сцены.
            # Чтение из самой сцены оставлено запасным: если Pass 1 когда-нибудь
            # научится отдавать эти поля, ничего переписывать не придётся.
            override = overrides.get(scene["id"], {})
            references = (
                self._references_map(override.get("resolved_references"))
                or self._references_map(scene.get("resolved_references"))
            )
            constraints = (
                self._clean_constraints(override.get("continuity_constraints"))
                or self._clean_constraints(scene.get("continuity_constraints"))
            )
            packets.append({
                "scene_id": scene["id"],
                "timeline": {
                    "word_start_index": scene["word_start_index"],
                    "word_end_index": scene["word_end_index"],
                    "start": scene["startTime"],
                    "end": scene["endTime"],
                },
                "current_scene": {
                    "text": scene["text"],
                    "role": scene.get("scene_role", "event"),
                    "description": scene.get("description", ""),
                    "entities": scene.get("entities") or [],
                },
                "context_before": [
                    {
                        "scene_id": item["id"],
                        "text": item["text"],
                        "description": item.get("description", ""),
                    }
                    for item in scenes[max(0, index - self.before):index]
                ],
                "context_after": [
                    {
                        "scene_id": item["id"],
                        "text": item["text"],
                        "description": item.get("description", ""),
                    }
                    for item in scenes[index + 1:index + 1 + self.after]
                ],
                "chapter_state": chapter,
                "entity_state": {
                    "registry": entity_registry,
                    "active": scene.get("entities") or chapter.get("active_entities") or [],
                    "resolved_references": references,
                    "hidden": chapter.get("hidden_entities") or [],
                    "revealed": chapter.get("revealed_entities") or [],
                },
                "visual_history": history[-self.history:],
                "continuity_constraints": constraints,
            })
        return packets


def write_context_packets(
    story_dir: str | Path,
    packets: list[dict[str, Any]],
) -> Path:
    story = Path(story_dir)
    target = story / "context_packets"
    target.mkdir(parents=True, exist_ok=True)
    jsonl = story / "context_packets.jsonl"
    with jsonl.open("w", encoding="utf-8") as stream:
        for packet in packets:
            stream.write(json.dumps(packet, ensure_ascii=False) + "\n")
            (target / f"{packet['scene_id']:03d}.json").write_text(
                json.dumps(packet, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    return jsonl
