"""
Chat Director — режиссёр через прямой chat/completions API (deepseek | openrouter).

Drop-in замена GptDirector для провайдеров с прямым API-ключом. Тот же интерфейс
analyze(). БЕЗ Codex и Moon Bridge: модель отвечает текстом, не пишет файлы/скрипты.

openai остаётся на GptDirector (Codex).
Переиспользует из gpt_director: модульные chain-хелперы, шаблон Pass2.
Транспорт — units.llm_gateway.chat_completion.
"""
from __future__ import annotations

import json
import logging
import math
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any

import params as config
from units.text_io_utils import read_user_text
from units.plan_validator import validate_montage_plan_json, check_clip_id_sequence
from units import llm_gateway as chat_client
# Переиспользуем модульные хелперы из gpt_director (chain mode, clip cards)
from units.codex_director import (
    _build_chain_meta,
    _build_clip_card,
    _build_chain_chunks,
    _build_chain_hint,
    extract_negative_block,
    append_negative_tail,
    strip_negative_section,
    strip_hardmatch_section,
    apply_ref_hardmatch,
    has_reference_images,
    append_style_tail,
    strip_style_section,
)


class ChatDirector:
    """Режиссёрский анализ через прямой chat API (deepseek | openrouter)."""

    def __init__(self, model_name: Optional[str] = None):
        # Модель — явный override, иначе из провайдерского config (DEEPSEEK_MODEL / …).
        self.provider = config.GPT_PROVIDER

        def _valid_override(name: str) -> bool:
            return bool(name)

        if self.provider == 'deepseek':
            self.model_name = model_name if _valid_override(model_name) else (config.DEEPSEEK_MODEL or 'deepseek-v4-flash')
            self.web = False
        elif self.provider == 'openrouter':
            self.model_name = model_name if _valid_override(model_name) else (config.OPENROUTER_MODEL or 'openrouter/free')
            self.web = (getattr(config, 'OPENROUTER_WEB_SEARCH', 'disabled') == 'enabled')
        elif self.provider == 'meta_muse':
            self.model_name = model_name if _valid_override(model_name) else (
                getattr(config, 'META_MUSE_MODEL', '')
                or 'muse-spark-1.2-contributor'
            )
            self.web = False
        elif self.provider in ('codex_reseller', 'gpt_nexus'):
            self.model_name = model_name if _valid_override(model_name) else (
                getattr(config, 'CODEX_RESELLER_MODEL', '') if self.provider == 'codex_reseller'
                else getattr(config, 'GPT_NEXUS_MODEL', '')
            ) or ('gpt-5.6-terra' if self.provider == 'codex_reseller' else 'gpt-5.5')
            self.web = False
        else:
            raise ValueError(f"ChatDirector не поддерживает провайдер: {self.provider}")

        self.reasoning_level = getattr(config, 'CHAT_REASONING_LEVEL', 'medium')
        # Лимит output-токенов: None — провайдер применяет свой потолок (Meta Muse
        # транспорт ставит 96k сам). Обрезки ловит валидатор.
        self.max_tokens = None
        self.timeout = config.GPT_DIRECTOR_TIMEOUT
        self.schemas_dir = Path(__file__).parent.parent / 'schemas'
        self.prompts_dir = Path(__file__).parent.parent / 'instructions'
        self._ext_negative = None  # вынесенный negative-блок (устанавливается в _run_pass2)
        self._ext_style = None  # вынесенный style-блок (устанавливается в _run_pass2)
        self._ext_refmatch = False  # выносить hard-match подпись рефов (устанавливается в _run_pass2)

        if not chat_client._api_key(self.provider):
            raise ValueError(f"ChatDirector: API ключ для {self.provider} не задан")

    # =========================================================================
    # ТРАНСПОРТ
    # =========================================================================

    def _chat(self, messages: list, *, json_mode: bool, max_tokens: Optional[int] = None) -> str:
        """Один запрос к провайдеру. Возвращает content."""
        return chat_client.chat_completion(
            self.provider, self.model_name, messages,
            max_tokens=max_tokens or self.max_tokens,
            reasoning_level=self.reasoning_level,
            web=self.web,
            json_mode=json_mode,
            timeout=self.timeout,
        )

    # =========================================================================
    # ГЛАВНЫЙ МЕТОД — интерфейс как у GptDirector
    # =========================================================================

    def analyze(
        self,
        transcription_path: str,
        style_guide_path: str,
        output_dir: str,
        project_name: str = "Untitled",
        two_pass: bool = False,
        prompts_only: bool = False,
        resume_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        output_dir = Path(output_dir)
        transcription_path = Path(transcription_path)
        style_guide_path = Path(style_guide_path)

        transcription = json.loads(transcription_path.read_text(encoding='utf-8-sig'))
        audio_duration = transcription.get('audioDuration', 0)

        results = {}

        if prompts_only:
            mp_path = output_dir / 'montage_plan.json'
            if not mp_path.exists():
                raise FileNotFoundError(f"montage_plan.json не найден: {mp_path}")
            montage_plan = json.loads(mp_path.read_text(encoding='utf-8-sig'))
        else:
            montage_plan, mp_path = self._run_pass1(
                transcription_path, style_guide_path,
                output_dir, project_name, audio_duration, transcription
            )

        results['montage_plan'] = str(mp_path)
        results['num_clips'] = len(montage_plan.get('clips', []))

        prompts_path, num_prompts = self._run_pass2(
            mp_path, transcription_path, style_guide_path,
            output_dir, project_name, montage_plan,
            resume_path=resume_path
        )
        if num_prompts == 0:
            raise RuntimeError(
                f"Chat Pass 2 вернул 0 промптов. Пайплайн остановлен. "
                f"Перезапустите с режимом 'Prompts only'."
            )
        results['prompts'] = str(prompts_path) if prompts_path else None
        results['num_prompts'] = num_prompts
        results['cost'] = {
            'total_cost': 0, 'input_tokens': 0, 'output_tokens': 0,
            'note': f'{self.provider} direct API'
        }
        return results

    # =========================================================================
    # PASS 1 — montage_plan.json (INLINE контент, модель не читает файлы)
    # =========================================================================

    def _run_pass1(
        self,
        transcription_path: Path,
        style_guide_path: Path,
        output_dir: Path,
        project_name: str,
        audio_duration: float,
        transcription: dict,
    ) -> tuple:
        """Pass 1: транскрипция + стайлгайд → montage_plan.json.

        Строит план по «окнам» времени (1 окно = весь ролик, если чанкинг выключен;
        иначе окна по PASS1_CHUNK_MINUTES). КАЖДОЕ окно проходит валидатор обрезки с
        докачом (см. _generate_window_plan) — страховка от молчаливой обрезки вывода
        (лимит токенов) как в 1-чанковом, так и в многочанковом режиме.
        """
        mp_path = output_dir / 'montage_plan.json'
        self._dbg_dir = output_dir  # для дампа сырого ответа при пустом окне

        print(f"\n🎬 Chat Pass 1: создание монтажного плана...")
        print(f"   Провайдер: {self.provider} | Модель: {self.model_name}")
        print(f"   Аудио: {audio_duration:.0f}с ({audio_duration/60:.1f} мин)")

        clips = self._run_pass1_windows(
            transcription, style_guide_path, transcription_path,
            audio_duration, project_name,
        )
        if not clips:
            raise RuntimeError(
                "Chat Pass 1: не удалось собрать ни одного клипа (все окна вернули пусто). "
                "Сырые ответы (если были) → _chat_pass1_raw_*.txt в папке проекта."
            )

        # Глобальная перенумерация + починка стыков окон (непрерывность таймингов)
        clips = self._stitch_pass1_clips(clips, audio_duration)
        montage_plan = {'audioDuration': round(audio_duration, 2), 'clips': clips}

        # Валидация (тот же валидатор что у gpt_director)
        is_valid, errors = validate_montage_plan_json(montage_plan, audio_duration)
        if not is_valid:
            logging.warning(f"Chat montage plan validation: {errors}")
            # Не пишем «авто-коррекция»: по длительностям и стыкам план НЕ
            # правится, это только замечания в лог. Авто-коррекция ниже и
            # только для ID клипов.
            print(f"   ⚠️  Валидация: {len(errors)} замечаний")
        else:
            print(f"   ✅ Валидация монтажного плана — OK")

        id_problems = check_clip_id_sequence(montage_plan.get('clips', []))
        if id_problems:
            logging.warning(f"Chat montage plan ID issues: {id_problems}")
            print(f"   ⚠️  ID последовательность: {len(id_problems)} проблем (авто-коррекция)")
            for i, clip in enumerate(montage_plan.get('clips', [])):
                clip['id'] = i + 1
                clip['file'] = f"{i + 1}.mp4"

        mp_path.write_text(
            json.dumps(montage_plan, indent=2, ensure_ascii=False), encoding='utf-8'
        )
        from units.source_router import apply_youtube_ratio_policy
        apply_youtube_ratio_policy(mp_path)
        from units.real_source_policy import (
            apply_policy as apply_non_ai_policy,
            enabled as non_ai_enabled,
        )
        if non_ai_enabled():
            apply_non_ai_policy(mp_path)
            montage_plan = json.loads(mp_path.read_text(encoding='utf-8-sig'))
        # «Состав видео» — предпочтение: разметку сводим с долями ДО Pass 2
        # (см. source_quota.py; то же самое делает codex_director).
        try:
            from units.source_quota import enforce_source_mix
            if enforce_source_mix(mp_path, transcription).get('changed'):
                montage_plan = json.loads(mp_path.read_text(encoding='utf-8-sig'))
        except Exception as exc:                       # noqa: BLE001
            print(f"   ⚠️  Состав видео не применён: {exc}")
            logging.warning("enforce_source_mix failed: %s", exc, exc_info=True)

        clips = montage_plan.get('clips', [])
        print(f"   ✅ Монтажный план: {len(clips)} клипов, {audio_duration:.0f}с")
        from units.source_ledger import log_source_markup
        log_source_markup(clips)
        return montage_plan, mp_path

    # ------------------------------------------------------------------ окна
    def _run_pass1_windows(
        self,
        transcription: dict,
        style_guide_path: Path,
        transcription_path: Path,
        audio_duration: float,
        project_name: str,
    ) -> list:
        """Оркестратор Pass 1 по окнам времени.

        • Чанкинг выкл (PASS1_CHUNK_ENABLED=false) ИЛИ аудио короче окна → 1 окно = весь ролик.
        • Иначе режем на окна по PASS1_CHUNK_MINUTES минут.
        • PASS1_PARALLEL=1 → окна последовательно; >1 → параллельно (окна непересекающиеся).
        Каждое окно → _generate_window_plan (валидатор обрезки + докач). Возвращает клипы
        в ГЛОБАЛЬНОМ времени, отсортированные по началу окна (без глобальной перенумерации —
        её делает _stitch_pass1_clips).
        """
        win_seconds = max(60, config.PASS1_CHUNK_MINUTES * 60)
        chunk_on = bool(getattr(config, 'PASS1_CHUNK_ENABLED', False)) and audio_duration > win_seconds

        # Границы окон
        if chunk_on:
            n_win = math.ceil(audio_duration / win_seconds)
            windows = [
                (i, i * win_seconds, min((i + 1) * win_seconds, audio_duration))
                for i in range(n_win)
            ]
            print(f"   🪟 Чанкинг Pass 1: {n_win} окон × {config.PASS1_CHUNK_MINUTES} мин")
        else:
            windows = [(0, 0.0, audio_duration)]
            if getattr(config, 'PASS1_CHUNK_ENABLED', False):
                print(f"   🪟 Чанкинг Pass 1 вкл, но аудио ≤ окна — одно окно на весь ролик")

        parallel = max(1, int(getattr(config, 'PASS1_PARALLEL', 1)))

        def _do(win):
            w_idx, w_start, w_end = win
            label = f"окно {w_idx + 1}/{len(windows)}" if len(windows) > 1 else "весь ролик"
            return w_idx, self._generate_window_plan(
                transcription, style_guide_path, transcription_path,
                w_start, w_end, audio_duration, project_name, label,
            )

        results = {}
        if parallel > 1 and len(windows) > 1:
            _mp = min(parallel, len(windows))
            print(f"   🚀 Параллель Pass 1: {_mp} воркеров на {len(windows)} окон")
            with ThreadPoolExecutor(max_workers=_mp) as pool:
                _futs = {pool.submit(_do, w): w[0] for w in windows}
                for _f in as_completed(_futs):
                    _wi, _wclips = _f.result()
                    results[_wi] = _wclips
        else:
            for w in windows:
                _wi, _wclips = _do(w)
                results[_wi] = _wclips

        # Сборка по порядку окон
        merged = []
        for w_idx, _, _ in windows:
            merged.extend(results.get(w_idx, []))
        return merged

    def _generate_window_plan(
        self,
        transcription: dict,
        style_guide_path: Path,
        transcription_path: Path,
        win_start: float,
        win_end: float,
        audio_duration: float,
        project_name: str,
        label: str,
    ) -> list:
        """План одного окна [win_start..win_end] с ВАЛИДАТОРОМ ОБРЕЗКИ и докачом.

        Крутит запросы, пока клипы не покроют окно (last endTime ≥ конец окна). Если ответ
        обрезан (покрытие < порога) — отсекает последние CONT_ROLLBACK_SIZE «уставших» клипов
        и досылает запрос по ОСТАВШЕЙСЯ транскрипции окна. Возвращает клипы в ГЛОБАЛЬНОМ
        времени (id ещё не глобальные — перенумерует _stitch_pass1_clips).
        """
        _COVERAGE = 0.8            # доля окна, покрытие которой считаем «дошёл до конца»
        _MAX_ITER = 8             # предохранитель от бесконечного докача
        _rollback = int(getattr(config, 'CONT_ROLLBACK_SIZE', 20))

        win_clips = []            # накопленные клипы окна (глобальное время)
        cursor = win_start        # глобальная позиция, докуда окно уже покрыто
        stall = 0

        for _it in range(_MAX_ITER):
            seg_dur = win_end - cursor
            if seg_dur <= 1.0:
                break

            seg_text, seg_frag_count = self._slice_window_transcription(
                transcription, cursor, win_end
            )
            if seg_frag_count == 0:
                break  # нет речи в остатке — окно закрыто

            note = (
                f"\n\n📌 IMPORTANT: this is a SEGMENT of the full audio "
                f"({cursor:.1f}–{win_end:.1f}s of {audio_duration:.1f}s total). "
                f"Timecodes are NORMALIZED: 0.00 = {cursor:.1f}s from the very beginning. "
                f"audioDuration for this segment = {seg_dur:.2f}s. "
                f"Generate clips STRICTLY within 0..{seg_dur:.2f}s and cover the WHOLE segment."
            )
            task_prompt = self._build_pass1_prompt(
                transcription_path, style_guide_path, project_name, seg_dur,
                transcription_text=seg_text, window_note=note,
            )

            _sub = f"{label}" + (f" · докач {_it}" if _it else "")
            print(f"\n✍️  Chat Pass 1: {_sub} ({cursor:.0f}–{win_end:.0f}с)...")
            t0 = time.time()
            try:
                raw = self._chat(
                    [{"role": "user", "content": task_prompt}],
                    json_mode=True,
                    max_tokens=self.max_tokens,
                )
            except chat_client.ChatError as e:
                logging.warning(f"Pass1 {_sub} chat error: {e}")
                print(f"   ❌ {self.provider}: {str(e)[:150]}")
                stall += 1
                if stall >= 2:
                    break
                time.sleep(5)
                continue
            elapsed = time.time() - t0

            plan = self._parse_pass1_json(raw)
            seg_clips = plan.get('clips', []) if isinstance(plan, dict) else []
            if not seg_clips:
                # пусто/не распарсилось (часто = обрезка мид-JSON) → ретрай того же остатка
                if getattr(self, '_dbg_dir', None):
                    try:
                        (self._dbg_dir / f'_chat_pass1_raw_{cursor:.0f}.txt').write_text(
                            raw or '', encoding='utf-8'
                        )
                    except Exception:
                        pass
                stall += 1
                print(f"   ⚠️  {_sub}: 0 клипов ({elapsed:.0f}с) — ретрай {stall}")
                if stall >= 2:
                    break
                time.sleep(3)
                continue
            stall = 0

            last_end = seg_clips[-1].get('endTime', 0)  # sub-local
            coverage = last_end / seg_dur if seg_dur > 0 else 1.0

            if coverage >= _COVERAGE or _it == _MAX_ITER - 1:
                # окно (или его остаток) покрыто — принимаем всё, окно закрыто
                self._offset_clips(seg_clips, base=cursor, clamp_end=win_end)
                win_clips.extend(seg_clips)
                print(f"   ✅ {_sub}: {len(seg_clips)} клипов, покрытие {coverage:.0%} ({elapsed:.0f}с)")
                break

            # ── ОБРЕЗКА: покрыт не весь остаток → откат уставшего хвоста + докач ──
            if _rollback > 0 and len(seg_clips) > _rollback:
                kept = seg_clips[:-_rollback]
            else:
                kept = seg_clips
            if not kept:
                kept = seg_clips[:1]

            self._offset_clips(kept, base=cursor, clamp_end=win_end)
            win_clips.extend(kept)

            new_cursor = kept[-1]['endTime']  # уже глобальное после offset
            advance = new_cursor - cursor
            print(f"   ⚠️  {_sub}: покрытие {coverage:.0%} — обрезка. Сохранено {len(kept)} "
                  f"(откат {len(seg_clips) - len(kept)}), докач с {new_cursor:.0f}с...")
            if advance <= 1.0:
                stall += 1
                if stall >= 2:
                    print(f"   ⛔ {_sub}: курсор не двигается — стоп окна")
                    break
            else:
                stall = 0
            cursor = new_cursor

        return win_clips

    def _slice_window_transcription(
        self, transcription: dict, start: float, end: float
    ) -> tuple:
        """Вырезает фрагменты речи в [start..end], нормализует таймкоды к началу (start→0).

        Возвращает (json_text, fragment_count). Формат совместим с _build_pass1_prompt.
        """
        fragments = transcription.get('transcription', [])
        seg = [
            f for f in fragments
            if f.get('end', 0) > start and f.get('start', 0) < end
        ]
        norm = [
            {
                **f,
                # клампим к границам окна: фрагмент, заезжающий из соседнего окна,
                # не должен давать отрицательный start или end за концом окна
                'start': round(max(f.get('start', 0), start) - start, 3),
                'end': round(min(f.get('end', 0), end) - start, 3),
            }
            for f in seg
        ]
        payload = {
            'transcription': norm,
            'audioDuration': round(end - start, 3),
            'language': transcription.get('language'),
        }
        return json.dumps(payload, ensure_ascii=False), len(norm)

    def _offset_clips(self, clips_list: list, base: float, clamp_end: float) -> None:
        """Сдвигает клипы из локального времени окна в глобальное + клампит к концу окна.

        In-place. id/file не трогает — глобальная перенумерация в _stitch_pass1_clips.
        """
        for c in clips_list:
            c['startTime'] = round(base + c.get('startTime', 0), 3)
            c['endTime'] = round(base + c.get('endTime', 0), 3)
            c['startTime'] = max(c['startTime'], base)
            c['endTime'] = min(c['endTime'], clamp_end)
            if c['endTime'] <= c['startTime']:
                c['endTime'] = round(c['startTime'] + 1.0, 3)

    def _stitch_pass1_clips(self, clips: list, audio_duration: float) -> list:
        """Сшивает клипы всех окон: сортировка по времени, непрерывность стыков, перенумерация.

        Убирает нахлёсты/дыры на границах окон (endTime[i] = startTime[i+1]) и клампит хвост
        к audioDuration. Возвращает клипы с id 1..N и file "{id}.mp4".
        """
        if not clips:
            return clips
        clips = sorted(clips, key=lambda c: c.get('startTime', 0))
        first_start = 0.0
        prev_end = first_start
        out = []
        for c in clips:
            s = round(prev_end, 3)
            e = round(max(c.get('endTime', s + 1.0), s + 0.5), 3)
            e = min(e, round(audio_duration, 3))
            if e <= s:
                # клип целиком за концом аудио — отбрасываем
                continue
            c['startTime'] = s
            c['endTime'] = e
            out.append(c)
            prev_end = e
            if prev_end >= audio_duration:
                break
        # хвост дотягиваем до audioDuration
        if out and out[-1]['endTime'] < audio_duration:
            out[-1]['endTime'] = round(audio_duration, 3)
        for i, c in enumerate(out):
            c['id'] = i + 1
            c['file'] = f"{i + 1}.mp4"
        return out

    def _parse_pass1_json(self, raw: str) -> dict:
        """Извлекает montage_plan dict из ответа модели (json_object режим)."""
        raw = (raw or '').strip()
        if not raw:
            return {}

        def _unwrap(d):
            if isinstance(d, dict) and 'montage_plan' in d and isinstance(d['montage_plan'], dict):
                return d['montage_plan']
            return d

        try:
            return _unwrap(json.loads(raw))
        except json.JSONDecodeError:
            pass
        # модель могла обернуть JSON в текст — вытащим первый {...}
        start, end = raw.find('{'), raw.rfind('}')
        if start >= 0 and end > start:
            try:
                return _unwrap(json.loads(raw[start:end + 1]))
            except json.JSONDecodeError:
                pass
        return {}

    def _build_pass1_prompt(
        self,
        transcription_path: Path,
        style_guide_path: Path,
        project_name: str,
        audio_duration: float,
        transcription_text: Optional[str] = None,
        window_note: str = "",
    ) -> str:
        """Pass 1 промпт с INLINE контентом (модель не имеет доступа к ФС).

        Структура/правила взяты из gpt_director._build_pass1_prompt, но содержимое
        файлов (транскрипция, стайлгайд, director-инструкция, pause-guide) вшито
        прямо в текст вместо путей.

        transcription_text: если задан — используется вместо чтения файла (транскрипция
            конкретного ОКНА, таймкоды нормализованы). None → читаем весь файл.
        window_note: приписка о нормализации таймкодов окна (в конец промпта).
        """
        # Inline контент файлов
        try:
            sg_text = read_user_text(style_guide_path)
        except Exception:
            sg_text = "(style guide unavailable)"
        if transcription_text is None:
            try:
                transcription_text = transcription_path.read_text(encoding='utf-8-sig')
            except Exception:
                transcription_text = "{}"
        try:
            si_text = (self.prompts_dir / 'director_instruction_pass1.txt').read_text(encoding='utf-8')
        except Exception:
            si_text = ""
        try:
            pg_text = (self.prompts_dir / 'director_pause_guide.txt').read_text(encoding='utf-8')
        except Exception:
            pg_text = ""

        clip_min = config.ACTIVE_CLIP_MIN_DURATION
        clip_max = config.ACTIVE_CLIP_MAX_DURATION
        intro_min = config.get_intro_clip_min()
        intro_max = config.get_intro_clip_max()
        intro_block = config.INTRO_BLOCK_DURATION

        # Подставляем плейсхолдеры длительностей в director-инструкцию (как делал Codex)
        si_text = (si_text
                   .replace('{clip_min_dur}', str(clip_min))
                   .replace('{clip_max_dur}', str(clip_max))
                   .replace('{clip_intro_min_dur}', str(intro_min))
                   .replace('{clip_intro_max_dur}', str(intro_max))
                   .replace('{intro_block_dur}', str(intro_block)))

        # Chain story mode (если STORY_MODE=chain)
        chain_suffix = ""
        if config.STORY_MODE == 'chain':
            chain_suffix = """

---

## CHAIN STORY MODE

Split clips into "chains". A chain = continuous visual/narrative flow:
same location, same camera POV, continuous action, same main character.

Add "chain_id" to each clip:
- Clips in same chain → same chain_id (consecutive by id)
- Chain break → new chain_id (+1). Break criteria:
  * Location change (street → apartment)
  * Major camera angle shift (close-up → wide, low → high)
  * POV change (character A eyes → character B eyes, third person → first person)
  * Sharp editorial transition (fade, time skip)

Numbering: chain_id = 1, 2, 3, ... (no gaps). First clip always chain_id=1.
Clips within a chain are consecutive by id (no breaks).
Single-clip chain → unique chain_id.
"""

        # Real Photo markup (если REAL_PHOTO_ENABLED=1). Текст — из
        # units/archive_tags.py: одна точка правды на оба директора.
        from units.archive_tags import real_photo_markup_directive
        real_photo_suffix = real_photo_markup_directive()

        # Stock markup (если STOCK_ENABLED=true). Текст — из units/stock_tags.py:
        # одна точка правды на оба директора, чтобы не повторить историю с маркерами,
        # размазанными по 7 файлам.
        from units.stock_tags import stock_markup_directive
        stock_suffix = stock_markup_directive()
        from units.tube_tags import youtube_markup_directive
        youtube_suffix = youtube_markup_directive()
        from units.infographic_tags import infographic_markup_directive
        infographic_suffix = infographic_markup_directive()
        from units.mograph_tags import motion_graphics_markup_directive
        mograph_suffix = motion_graphics_markup_directive()

        # Тема ролика — якорь для поиска архив/сток материалов (узкий запрос про субъект).
        _theme_needed = getattr(config, 'REAL_PHOTO_ENABLED', False) or getattr(config, 'STOCK_ENABLED', False)
        theme_line = ('    "theme": "<one clear sentence: genre + main subject + setting/period, from the whole video>",\n'
                      if _theme_needed else '')
        theme_rule = ('- "theme": ONE clear, specific sentence giving the video\'s GENRE, its MAIN '
                      'SUBJECT, and the SETTING/PERIOD context — from the WHOLE transcription. '
                      'Be concrete and informative, not vague.\n'
                      if _theme_needed else '')

        return f"""You are a film director creating a montage plan for a documentary.

## DIRECTOR INSTRUCTION
{si_text}

## PAUSE GUIDE
{pg_text}

## STYLE GUIDE
{sg_text}

## TRANSCRIPTION (JSON)
{transcription_text}

## PROJECT
Name: {project_name}
Audio duration: {audio_duration:.2f} seconds

## TASK
Generate a montage plan following the director instruction rules above.

Return STRICT JSON (no markdown, no commentary) with this structure:
{{
  "montage_plan": {{
    "audioDuration": {audio_duration:.2f},
{theme_line}    "clips": [
      {{"id": 1, "file": "1.mp4", "startTime": 0.0, "endTime": 7.8}},
      {{"id": 2, "file": "2.mp4", "startTime": 7.8, "endTime": 14.2}}
    ]
  }}
}}

Rules:
- Each clip duration: {clip_min}–{clip_max} seconds
- Clips continuous (no gaps): clip N endTime = clip N+1 startTime
- First clip startTime = 0.0
- Last clip endTime must equal audioDuration ({audio_duration:.2f})
- IDs sequential: 1, 2, 3, ...
- File format: "{{id}}.mp4"
{theme_rule}{chain_suffix}{real_photo_suffix}{stock_suffix}{youtube_suffix}{infographic_suffix}{mograph_suffix}
Return ONLY the JSON object. Do NOT create files. Do NOT write scripts.{window_note}"""

    # =========================================================================
    # PASS 2 — промпты (оркестратор портирован из gpt_director)
    # =========================================================================

    def _build_pass2_prompt(
        self,
        style_guide_path: Path,
        prompts_path: Path,
        clip_block: str,
        from_id: int,
        to_id: int,
        n_clips: int,
        total_clips: int,
        prev_sample: str,
        prev_plot: str,
        append_mode: bool,
    ) -> str:
        """Сборка Pass2-промпта из шаблона gpt_director_pass2.txt (уже inline).

        Идентична GptDirector._build_pass2_prompt — переиспользуем тот же шаблон.
        """
        try:
            _sg_text = read_user_text(style_guide_path)
        except Exception:
            _sg_text = f"(could not read style guide: {style_guide_path})"
        # Если negative вынесен — убрать его секцию из стайлгайда (источник конфликта с override)
        if getattr(self, '_ext_negative', None):
            _sg_text = strip_negative_section(_sg_text)
        # Если style вынесен — убрать его секцию из стайлгайда
        if getattr(self, '_ext_style', None):
            _sg_text = strip_style_section(_sg_text)
        # Если ref hard-match вынесен — убрать требование писать подпись после токенов
        if getattr(self, '_ext_refmatch', False):
            _sg_text = strip_hardmatch_section(_sg_text)

        prev_section = ""
        if prev_sample:
            prev_section = f"""
## PREVIOUS PROMPTS (for style consistency — continue in the same format)
{prev_sample}
"""
        plot_section = ""
        if prev_plot:
            plot_section = f"""
## PREVIOUS SCENE CONTEXT (what was just visualized)
{prev_plot}
"""

        chain_hint = _build_chain_hint()

        _tpl_path = self.prompts_dir / 'gpt_director_pass2.txt'
        try:
            _tpl = _tpl_path.read_text(encoding='utf-8')
        except Exception:
            raise RuntimeError(f"Pass 2 template not found: {_tpl_path}")

        result = _tpl
        result = result.replace('{style_guide}', _sg_text)
        result = result.replace('{plot_section}', plot_section)
        result = result.replace('{prev_section}', prev_section)
        result = result.replace('{n_clips}', str(n_clips))
        result = result.replace('{from_id}', str(from_id))
        result = result.replace('{to_id}', str(to_id))
        result = result.replace('{total_clips}', str(total_clips))
        result = result.replace('{clip_block}', clip_block)
        result = result.replace('{chain_hint}', chain_hint)
        from units.motion_prompts import pass2_video_directive
        result += pass2_video_directive()
        from units.infographic_tags import (
            default_infographic_style, infographic_language_rule,
            infographic_style_anchor_rule,
        )
        # надписи НА кадре — на языке озвучки пайплайна, не на английском;
        # вид — от стайлгайда, иначе диаграмма контрастирует с видеорядом
        result = result.replace(
            '{infographic_default_style}',
            default_infographic_style() + "\n"
            + infographic_style_anchor_rule() + "\n"
            + infographic_language_rule())
        # Override: если style/negative/ref вынесены — запретить модели писать их (экономия вывода)
        _ov = []
        _ext_s = getattr(self, '_ext_style', None)
        _ext_n = getattr(self, '_ext_negative', None)
        if _ext_s and _ext_n:
            _ov.append(
                "Do NOT write the STYLE BLOCK and do NOT write the NEGATIVE prompt block "
                "(including the 'single full-frame continuous camera view edge-to-edge' ending). "
                "Write ONLY the per-scene content (camera, anchor, tokens, action, environment, "
                "light, mood, intention) and END the prompt there. Style and negative are appended "
                "automatically by the system.")
        elif _ext_n:
            _ov.append(
                "Do NOT write the negative prompt block. Do NOT write the "
                "'single full-frame continuous camera view edge-to-edge' ending. "
                "End each prompt right after the STYLE BLOCK.")
        elif _ext_s:
            _ov.append(
                "Do NOT write the STYLE BLOCK — it is appended automatically. "
                "Write the negative prompt block as usual after the scene content.")
        if getattr(self, '_ext_refmatch', False):
            _ov.append(
                "Write each character as a SHORT token only, e.g. [ravon_ref.jpg]. Do NOT add "
                "the '(MATCH the face...)' hard-match description after tokens — it is appended "
                "automatically to the first token mention.")
        if _ov:
            result += (
                "\n\n⚠️ OUTPUT SIZE OVERRIDE (mandatory, overrides any rule above):\n"
                + " ".join(_ov)
                + " These parts are appended automatically by the system — omitting them is "
                "REQUIRED to fit all prompts in one response. Each prompt still starts with [CLIP N]."
            )
        return result

    def _parse_prompts_text(self, raw: str) -> list:
        """Парсит Pass2-промпты из JSON/текста, требует [CLIP N]/[N] префикс.

        Портирован из GptDirector._parse_codex_prompts_text.
        """
        raw = (raw or '').strip()
        if not raw:
            return []

        def _normalize(items):
            if not isinstance(items, list):
                return []
            out = []
            for item in items:
                s = str(item).strip()
                if re.match(r'^\[(?:CLIP\s+)?\d+\]\s+', s, re.IGNORECASE):
                    out.append(s)
            return out

        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                prompts = _normalize(data.get('prompts', []))
                if prompts:
                    return prompts
            elif isinstance(data, list):
                prompts = _normalize(data)
                if prompts:
                    return prompts
        except Exception:
            pass

        start, end = raw.find('{'), raw.rfind('}')
        if start >= 0 and end > start:
            try:
                data = json.loads(raw[start:end + 1])
                if isinstance(data, dict):
                    prompts = _normalize(data.get('prompts', []))
                    if prompts:
                        return prompts
            except Exception:
                pass

        blocks = [p.strip() for p in raw.split('\n\n') if p.strip()]
        return _normalize(blocks)

    def _run_chat_pass2(
        self,
        task_prompt: str,
        prompts_path: Path,
        n_clips: int,
        prompts_offset: int = 0,
        keep_ids: bool = False,
    ) -> tuple:
        """Один chat-запрос Pass2. Парсит промпты, аппендит в prompts_path.

        Транспортная замена GptDirector._run_codex_pass2. БЕЗ session/CODEX_HOME.
        Returns: (total_prompts_in_file, found_clip_ids:set)
        """
        t0 = time.time()
        try:
            raw = self._chat(
                [{"role": "user", "content": task_prompt}],
                json_mode=True,
            )
        except chat_client.ChatError as e:
            logging.warning(f"Pass2 chat error: {e}")
            print(f"   ❌ {self.provider}: {str(e)[:150]}")
            return prompts_offset, set()

        elapsed = time.time() - t0
        prompts_list = self._parse_prompts_text(raw)
        if raw and not prompts_list:
            logging.warning("Pass2: ответ без валидных [CLIP N] промптов")
        if not prompts_list:
            return prompts_offset, set()

        # Transcript pollution check (как в gpt_director)
        def _is_pollution(p):
            s = re.sub(r'^\[CLIP\s+\d+\]\s*', '', p.strip())
            return s.startswith('VOICEOVER:') or s.startswith('[CLIP ')
        _vo = sum(1 for p in prompts_list if _is_pollution(p))
        if _vo > len(prompts_list) * 0.3:
            logging.error(f"Pass2: {_vo}/{len(prompts_list)} VOICEOVER blocks — pollution")
            print(f"   ❌ Транскрипция вместо промптов ({_vo} VOICEOVER блоков)")
            return prompts_offset, set()

        # Извлечь clip_id, снять префиксы
        _id_re = re.compile(r'^\[(?:CLIP\s+)?(\d+)\]\s*')
        _found_ids = set()
        _id_pairs = []
        for _p in prompts_list:
            _m = _id_re.match(_p.strip())
            if _m:
                _cid = int(_m.group(1))
                _found_ids.add(_cid)
                _id_pairs.append((_cid, _id_re.sub('', _p.strip(), count=1).strip()))
            else:
                _id_pairs.append((None, _p.strip()))

        # Замысел кадра — в план, ДО приклейки стиля и негатива ниже. Тот же
        # приём, что в gpt_director: стадии-источники читают описание сцены из
        # montage_plan, а без него получают сырую озвучку. Функция общая, чтобы
        # два бэкенда не разъехались (историю см. в её докстроке).
        from units.codex_director import _publish_visual_intent
        _publish_visual_intent(prompts_path, _id_pairs)

        # Externalized ref hard-match: подпись к первому появлению каждого токена (inline)
        if getattr(self, '_ext_refmatch', False):
            _id_pairs = [(_cid, apply_ref_hardmatch(_p)) for _cid, _p in _id_pairs]
        # Externalized style: канонический стиль-блок ПЕРЕД негативом (порядок scene→style→neg)
        if getattr(self, '_ext_style', None):
            _id_pairs = [(_cid, append_style_tail(_p, self._ext_style)) for _cid, _p in _id_pairs]
        # Externalized negative: канонический негатив+endline в конец каждого ядра
        if getattr(self, '_ext_negative', None):
            _id_pairs = [(_cid, append_negative_tail(_p, self._ext_negative)) for _cid, _p in _id_pairs]
        prompts_list = [_p for _, _p in _id_pairs]

        # Trim до ожидаемого
        if len(prompts_list) > n_clips:
            prompts_list = prompts_list[:n_clips]
            _id_pairs = _id_pairs[:n_clips]

        # Append в файл
        existing = ''
        if prompts_path.exists() and prompts_path.stat().st_size > 0:
            existing = prompts_path.read_text(encoding='utf-8', errors='replace').rstrip()
        if keep_ids:
            new_block = '\n\n'.join(
                f'[{_cid}] {_p}' if _cid is not None else _p
                for _cid, _p in _id_pairs if _p.strip()
            )
        else:
            new_block = '\n\n'.join(p.strip() for p in prompts_list if p.strip())
        combined = (existing + '\n\n' + new_block) if existing else new_block
        prompts_path.write_text(combined, encoding='utf-8')

        total_count = prompts_offset + len(prompts_list)
        _ids_info = f", IDs={sorted(_found_ids)[:5]}{'...' if len(_found_ids) > 5 else ''}" if _found_ids else ""
        print(f"   ⏱  Chat Pass2: {elapsed:.0f}с | записано {len(prompts_list)}, всего {total_count}/{prompts_offset + n_clips}{_ids_info}")
        return total_count, _found_ids

    def _run_pass2(
        self,
        mp_path: Path,
        transcription_path: Path,
        style_guide_path: Path,
        output_dir: Path,
        project_name: str,
        montage_plan: dict,
        resume_path: Optional[str] = None,
    ) -> tuple:
        """Pass 2 оркестратор: chunking + parallel/sequential + retry + merge + resume.

        Портирован из GptDirector._run_pass2. Транспорт → _run_chat_pass2.
        """
        # Externalize style + negative: модель не пишет их, допишет скрипт (экономия вывода).
        # resolve_style_blocks: нейро/regex/off + кэш style_blocks.json (см. STYLE_EXTRACT_MODE).
        self._ext_negative = None
        self._ext_style = None
        _want_neg = getattr(config, 'PASS2_EXTERNALIZE_NEGATIVE', True)
        _want_sty = getattr(config, 'PASS2_EXTERNALIZE_STYLE', True)
        if _want_neg or _want_sty:
            try:
                from units.style_blocks import resolve_style_blocks
                _sg = read_user_text(style_guide_path)
                _blocks = resolve_style_blocks(_sg, output_dir)
                if _want_neg and _blocks.get('negative_block'):
                    self._ext_negative = _blocks['negative_block']
                    print(f"   ♻️  Negative-блок вынесен ({len(self._ext_negative.split())} слов × клипы) — допишется скриптом")
                if _want_sty and _blocks.get('style_block'):
                    self._ext_style = _blocks['style_block']
                    print(f"   ♻️  Style-блок вынесен ({len(self._ext_style.split())} слов × клипы) — допишется скриптом")
            except Exception as _e:
                logging.warning(f"Pass2 style/negative extraction failed: {_e}")
        # Ref hard-match вынос: модель пишет короткий токен, подпись допишет скрипт
        self._ext_refmatch = bool(
            getattr(config, 'PASS2_EXTERNALIZE_REFMATCH', True)
            and has_reference_images(output_dir)
        )
        if self._ext_refmatch:
            print(f"   ♻️  Hard-match подписи рефов вынесены — допишутся скриптом к первому токену")

        clips = montage_plan.get('clips', [])

        # Real Photo: REAL_PHOTO_PROMPT_FALLBACK=1 (дефолт) → пишем промпты И для real_photo
        # клипов (страховка: провал/таймаут загрузки → догенерим нейросетью). =0 → старый скип.
        _rp_skipped = 0
        if not getattr(config, 'REAL_PHOTO_PROMPT_FALLBACK', True) and any(c.get('source') == 'real_photo_pending' for c in clips):
            _all_n = len(clips)
            clips = [c for c in clips if c.get('source') != 'real_photo_pending']
            _rp_skipped = _all_n - len(clips)
            if _rp_skipped > 0:
                print(f"   [Real Photo] Pass 2 skip: {_rp_skipped} clips (archival path, fallback OFF)")

        total_clips = len(clips)
        if total_clips == 0:
            print("   ❌ Монтажный план пуст — нечего промптировать")
            return None, 0

        chunk_size = config.GPT_PASS2_CHUNK_SIZE or total_clips

        timestamp = datetime.now().strftime('%Y%m%d_%H%M')
        prompts_path = output_dir / f'{project_name}_prompts_{timestamp}.txt'
        total_prompts = 0
        cursor = 0

        transcription = json.loads(transcription_path.read_text(encoding='utf-8-sig'))
        fragments = transcription.get('transcription', [])

        chain_meta = _build_chain_meta(clips) if getattr(config, 'STORY_MODE', 'off') == 'chain' else {}

        # Resume
        if resume_path:
            rp = Path(resume_path)
            if rp.exists():
                prompts_path = rp
                existing = rp.read_text(encoding='utf-8').strip().split('\n\n')
                total_prompts = len([p for p in existing if p.strip()])
                cursor = total_prompts
                print(f"   ↩️  Resume: {total_prompts} промптов уже есть, продолжаем с клипа {cursor + 1}")

        _pass2_total_start = time.time()
        _parallel = getattr(config, 'GPT_PASS2_PARALLEL', 1)

        # ============ ПАРАЛЛЕЛЬНЫЙ РЕЖИМ ============
        if _parallel > 1 and total_clips > chunk_size:
            cursor = self._run_pass2_parallel(
                clips, fragments, chain_meta, chunk_size, total_clips,
                cursor, total_prompts, prompts_path, style_guide_path,
                output_dir, _parallel, _pass2_total_start
            )
            total_prompts = len([p for p in prompts_path.read_text(encoding='utf-8').strip().split('\n\n') if p.strip()]) if prompts_path.exists() else 0
        else:
            # ============ ПОСЛЕДОВАТЕЛЬНЫЙ РЕЖИМ ============
            total_prompts = self._run_pass2_sequential(
                clips, fragments, chunk_size, total_clips,
                cursor, total_prompts, prompts_path, style_guide_path
            )

        # Финальная валидация
        if prompts_path.exists():
            actual = [p for p in prompts_path.read_text(encoding='utf-8').strip().split('\n\n') if p.strip()]
            total_prompts = len(actual)
            if total_prompts > total_clips:
                logging.warning(f"Overprompt {total_prompts}>{total_clips}, trimming")
                prompts_path.write_text('\n\n'.join(actual[:total_clips]), encoding='utf-8')
                total_prompts = total_clips

        _el = time.time() - _pass2_total_start
        print(f"\n✅ Chat Pass 2 завершён: {total_prompts}/{total_clips} промптов ({int(_el)//60}м {int(_el)%60:02d}с)")
        return prompts_path if total_prompts > 0 else None, total_prompts

    def _run_pass2_sequential(
        self, clips, fragments, chunk_size, total_clips,
        cursor, total_prompts, prompts_path, style_guide_path,
    ) -> int:
        """Последовательный Pass2: чанк за чанком."""
        _MAX_CHUNK_RETRIES = 3
        _chunk_retries = 0

        while cursor < total_clips:
            chunk_end = min(cursor + chunk_size, total_clips)
            chunk_clips = clips[cursor:chunk_end]
            from_id = chunk_clips[0]['id']
            to_id = chunk_clips[-1]['id']
            n_clips = len(chunk_clips)

            clip_cards = []
            for clip in chunk_clips:
                cs, ce = clip.get('startTime', 0), clip.get('endTime', 0)
                texts = [f['text'] for f in fragments if f.get('start', 0) < ce and f.get('end', 0) > cs]
                text = ' '.join(texts).strip() or '(pause)'
                source_line = '\nSOURCE: INFOGRAPHIC (make a static information graphic, not a cinematic scene)' \
                    if clip.get('source') == 'infographic' else ''
                clip_cards.append(
                    f"[CLIP {clip['id']}] {cs:.1f}–{ce:.1f}s{source_line}\nVOICEOVER: {text}"
                )
            clip_block = '\n\n'.join(clip_cards)

            prev_plot = ""
            if cursor > 0:
                plot_lines = []
                for c in clips[max(0, cursor - 5):cursor]:
                    cs, ce = c.get('startTime', 0), c.get('endTime', 0)
                    texts = [f['text'] for f in fragments if f.get('start', 0) < ce and f.get('end', 0) > cs]
                    plot_lines.append(f"Clip {c['id']} ({cs:.1f}-{ce:.1f}s): {' '.join(texts).strip()}")
                prev_plot = '\n'.join(plot_lines)

            prev_sample = ""
            if prompts_path.exists() and total_prompts > 0:
                existing = prompts_path.read_text(encoding='utf-8').strip().split('\n\n')
                prev_sample = '\n\n'.join(existing[-5:])

            task_prompt = self._build_pass2_prompt(
                style_guide_path, prompts_path, clip_block, from_id, to_id,
                n_clips, total_clips, prev_sample, prev_plot, total_prompts > 0
            )

            chunk_label = f" (клипы {from_id}–{to_id})" if total_clips > chunk_size else ""
            print(f"\n✍️  Chat Pass 2: генерация промптов{chunk_label}...")

            file_total, _found = self._run_chat_pass2(
                task_prompt, prompts_path, n_clips,
                prompts_offset=total_prompts, keep_ids=False,
            )
            written = file_total - total_prompts
            total_prompts = file_total

            if written < n_clips:
                if written == 0:
                    _chunk_retries += 1
                    if _chunk_retries > _MAX_CHUNK_RETRIES:
                        print(f"   ❌ {_MAX_CHUNK_RETRIES} попыток без прогресса — останавливаюсь")
                        break
                    print(f"   🔄 0 промптов — ретрай чанка ({_chunk_retries}/{_MAX_CHUNK_RETRIES})...")
                    time.sleep(5)
                    continue
                print(f"   ⚡ Частичная запись {written}/{n_clips} — продолжаем с клипа {total_prompts + 1}")
                cursor = total_prompts
                continue

            _chunk_retries = 0
            cursor = chunk_end

        return total_prompts

    def _run_pass2_parallel(
        self, clips, fragments, chain_meta, chunk_size, total_clips,
        cursor, total_prompts, prompts_path, style_guide_path,
        output_dir, _parallel, _pass2_total_start,
    ) -> int:
        """Параллельный Pass2: ThreadPoolExecutor + точечный retry по missing clip_id."""
        _initial_prev_sample = ""
        if prompts_path.exists() and total_prompts > 0:
            _ex = prompts_path.read_text(encoding='utf-8').strip().split('\n\n')
            _initial_prev_sample = '\n\n'.join([p for p in _ex if p.strip()][-5:])

        # Сборка чанков (chain-aware или обычная)
        if chain_meta:
            _chunks_lists = _build_chain_chunks(clips[cursor:], chunk_size)
        else:
            _chunks_lists = []
            _cur = cursor
            while _cur < total_clips:
                _end = min(_cur + chunk_size, total_clips)
                _chunks_lists.append(clips[_cur:_end])
                _cur = _end

        _chunks_to_run = []
        for _cclips in _chunks_lists:
            _fid, _tid, _nc = _cclips[0]['id'], _cclips[-1]['id'], len(_cclips)
            _clip_start = clips.index(_cclips[0])
            _cards = [_build_clip_card(_c, fragments, chain_meta) for _c in _cclips]
            _pp = ""
            if _clip_start > 0:
                _pp_lines = []
                for _pc in clips[max(0, _clip_start - 5):_clip_start]:
                    _cs, _cend = _pc.get('startTime', 0), _pc.get('endTime', 0)
                    _txts = [f['text'] for f in fragments if f.get('start', 0) < _cend and f.get('end', 0) > _cs]
                    _pp_lines.append(f"Clip {_pc['id']} ({_cs:.1f}-{_cend:.1f}s): {' '.join(_txts).strip()}")
                _pp = '\n'.join(_pp_lines)
            _tp = self._build_pass2_prompt(
                style_guide_path, prompts_path, '\n\n'.join(_cards),
                _fid, _tid, _nc, total_clips,
                _initial_prev_sample, _pp, total_prompts > 0 or _clip_start > 0,
            )
            _chunks_to_run.append({
                'idx': len(_chunks_to_run), 'from_id': _fid, 'to_id': _tid,
                'n_clips': _nc, 'task_prompt': _tp, 'clips': _cclips, 'clip_start': _clip_start,
            })

        _nch = len(_chunks_to_run)
        _max_par = max(1, min(_parallel, _nch))
        print(f"\n🚀 Chat Pass 2 параллельный режим ({_max_par} воркеров на {_nch} чанков)")

        _MAX_PAR_RETRIES = 10
        _RETRY_COOLDOWN_BASE = 30   # chat API быстрее восстанавливается чем Codex
        _RETRY_COOLDOWN_STEP = 20

        def _run_one(cd):
            _i = cd['idx']
            # Без имени проекта: оно уже в пути папки, а второй раз внутри имени
            # файла путь перерастал 260 символов Windows и файл «не находился»
            # (Errno 2) — на длинном заголовке ролика падал весь Pass 2
            # (жалоба 29.08). Финализация читает чанки по объектам путей.
            _tmp = output_dir / f'_tmp_p2_{_i}.txt'
            print(f"\n✍️  Chat Pass 2: клипы {cd['from_id']}–{cd['to_id']}...")

            def _run_chunk(task_p, n, offset):
                # Свежий tmp на каждый вызов чанка — пишем с keep_ids для merge
                return self._run_chat_pass2(task_p, _tmp, n, prompts_offset=offset, keep_ids=True)

            _ft, _found_ids = _run_chunk(cd['task_prompt'], cd['n_clips'], 0)
            _expected_ids = {c['id'] for c in cd['clips']}
            _retries = 0

            while _found_ids < _expected_ids and _retries < _MAX_PAR_RETRIES:
                _retries += 1
                _missing_ids = _expected_ids - _found_ids
                _cooldown = _RETRY_COOLDOWN_BASE + (_retries - 1) * _RETRY_COOLDOWN_STEP
                if not _found_ids:
                    print(f"   🔄 Чанк {_i+1}: 0 промптов — ретрай {_retries}/{_MAX_PAR_RETRIES} (cooldown {_cooldown}s)...")
                    time.sleep(_cooldown)
                    _ft, _new = _run_chunk(cd['task_prompt'], cd['n_clips'], 0)
                    _found_ids |= _new
                else:
                    _rem_clips = [c for c in cd['clips'] if c['id'] in _missing_ids]
                    _rem_start = cd['clip_start'] + len(_found_ids)
                    print(f"   🔄 Чанк {_i+1}: {len(_found_ids)}/{cd['n_clips']} — досылаем {len(_missing_ids)} клипов (IDs: {sorted(_missing_ids)[:5]}) ретрай {_retries}/{_MAX_PAR_RETRIES} (cooldown {_cooldown}s)...")
                    time.sleep(_cooldown)
                    _cards = [_build_clip_card(_c, fragments, chain_meta) for _c in _rem_clips]
                    _pp_lines = []
                    for _pc in clips[max(0, _rem_start - 5):_rem_start]:
                        _cs2, _ce2 = _pc.get('startTime', 0), _pc.get('endTime', 0)
                        _t2 = [f['text'] for f in fragments if f.get('start', 0) < _ce2 and f.get('end', 0) > _cs2]
                        _pp_lines.append(f"Clip {_pc['id']} ({_cs2:.1f}-{_ce2:.1f}s): {' '.join(_t2).strip()}")
                    _rem_tp = self._build_pass2_prompt(
                        style_guide_path, prompts_path, '\n\n'.join(_cards),
                        _rem_clips[0]['id'], _rem_clips[-1]['id'], len(_missing_ids), total_clips,
                        '', '\n'.join(_pp_lines), True,
                    )
                    _nft, _new = _run_chunk(_rem_tp, len(_missing_ids), len(_found_ids))
                    _found_ids |= _new

            _ft = len(_found_ids)
            if _ft < cd['n_clips']:
                print(f"   ⚠️  Чанк {_i+1}: {_ft}/{cd['n_clips']} после {_MAX_PAR_RETRIES} ретраев (пропущены: {sorted(_expected_ids - _found_ids)})")

            # Reorder _tmp по clip order, снять [N] префиксы
            if _tmp.exists() and _ft > 0:
                _id_re2 = re.compile(r'^\[(\d+)\]\s*')
                _id_map = {}
                for _blk in _tmp.read_text(encoding='utf-8').split('\n\n'):
                    _blk = _blk.strip()
                    if not _blk:
                        continue
                    _m2 = _id_re2.match(_blk)
                    if _m2:
                        _id_map[int(_m2.group(1))] = _id_re2.sub('', _blk, count=1).strip()
                _ordered = [_id_map[c['id']] for c in cd['clips'] if c['id'] in _id_map]
                _tmp.write_text('\n\n'.join(_ordered), encoding='utf-8')
                _ft = len(_ordered)

            return _i, _ft, _tmp

        # Resume из существующих _tmp чанков
        _id_re_resume = re.compile(r'^\[(\d+)\]\s*')

        def _try_resume(cd):
            _expected = {c['id'] for c in cd['clips']}
            # Новое короткое имя + старый формат с именем проекта (чанки от
            # прогонов до 29.08). Шаблон именно `{idx}_*`, а не `{idx}*`:
            # последний поймал бы `_tmp_p2_10.txt` для чанка 1.
            _cands = [output_dir / f"_tmp_p2_{cd['idx']}.txt"]
            _cands += sorted(output_dir.glob(f"_tmp_p2_{cd['idx']}_*.txt"))
            for _cand in _cands:
                if not _cand.exists():
                    continue
                try:
                    _txt = _cand.read_text(encoding='utf-8')
                except Exception:
                    continue
                _found = set()
                for _blk in _txt.split('\n\n'):
                    _m = _id_re_resume.match(_blk.strip())
                    if _m:
                        _found.add(int(_m.group(1)))
                if _expected.issubset(_found):
                    return _cand
            return None

        _par_results = []
        _to_submit = []
        for cd in _chunks_to_run:
            _rp = _try_resume(cd)
            if _rp is not None:
                _n = len({c['id'] for c in cd['clips']})
                print(f"   ♻️  Чанк {cd['idx']+1}: resume из {_rp.name} ({_n} промптов)")
                # К короткому имени: чанк старого прогона мог называться по
                # имени проекта, такой путь на длинных заголовках не открыть.
                _new_tmp = output_dir / f"_tmp_p2_{cd['idx']}.txt"
                if _rp.resolve() != _new_tmp.resolve():
                    try:
                        if _new_tmp.exists():
                            _new_tmp.unlink()
                        _rp.rename(_new_tmp)
                    except Exception:
                        import shutil as _sh
                        _sh.copy2(str(_rp), str(_new_tmp))
                _par_results.append((cd['idx'], _n, _new_tmp))
            else:
                _to_submit.append(cd)

        if _to_submit:
            with ThreadPoolExecutor(max_workers=_max_par) as _pool:
                _futs = {_pool.submit(_run_one, cd): cd['idx'] for cd in _to_submit}
                for _fut in as_completed(_futs):
                    _ri, _rft, _rtp = _fut.result()
                    _par_results.append((_ri, _rft, _rtp))
                    print(f"   ✅ Чанк {_ri + 1}/{_nch} завершён: {_rft} промптов")
        else:
            print(f"   ♻️  Все {_nch} чанков восстановлены из tmp — chat вызовы пропущены")

        # Merge по порядку чанков
        _par_results.sort(key=lambda x: x[0])
        _all = []
        _tmp_clean = []
        for _ri, _rft, _rtp in _par_results:
            if Path(_rtp).exists():
                _rc = Path(_rtp).read_text(encoding='utf-8').strip()
                _all.extend([p.strip() for p in _rc.split('\n\n') if p.strip()])
                _tmp_clean.append(Path(_rtp))
        prompts_path.write_text('\n\n'.join(_all), encoding='utf-8')
        total_written = len(_all)

        # Fail-stop если недобор (tmp НЕ чистим — нужны для resume)
        if total_written < total_clips:
            _miss = total_clips - total_written
            logging.error(f"Pass2 incomplete: {total_written}/{total_clips} ({_miss} missing). Tmp оставлены для resume.")
            raise RuntimeError(
                f"Chat Pass 2 не дополучил {_miss} промптов из {total_clips} "
                f"после {_MAX_PAR_RETRIES} ретраев на чанк. Tmp чанки сохранены (resume при перезапуске). "
                f"Пайплайн остановлен."
            )

        # Успех — чистим tmp
        for _tp in _tmp_clean:
            try:
                _tp.unlink()
            except Exception:
                pass
        return total_clips


# =========================================================================
# Wrapper — аналог gpt_direct_project / direct_project
# =========================================================================

def chat_direct_project(
    transcription_path: str,
    style_guide_path: str,
    output_dir: str,
    project_name: str = "Untitled",
    model_name: Optional[str] = None,
    two_pass: bool = False,
    prompts_only: bool = False,
    resume_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Wrapper: создаёт ChatDirector и запускает analyze()."""
    director = ChatDirector(model_name=model_name)
    return director.analyze(
        transcription_path, style_guide_path, output_dir, project_name,
        two_pass=two_pass, prompts_only=prompts_only, resume_path=resume_path,
    )
