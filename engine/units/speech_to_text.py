"""
Модуль транскрипции аудио через AssemblyAI API
"""
import assemblyai as aai
import json
import re
import time
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any
from tqdm import tqdm

import params as config
from units.plan_validator import validate_transcription_json


def _models_rejected_by_assemblyai(error_text: str, models: list) -> list:
    text = str(error_text or "").lower()
    rejected = []
    for model in models:
        name = re.escape(str(model).lower())
        if re.search(
            rf"(?:{name}.{{0,100}}(?:deprecated|unsupported|invalid|not available|not enabled)"
            rf"|(?:deprecated|unsupported|invalid|not available|not enabled).{{0,100}}{name})",
            text,
        ):
            rejected.append(model)
    return rejected


def _models_suggested_by_assemblyai(error_text: str) -> list:
    return list(dict.fromkeys(re.findall(
        r"\b(?:universal|best)[-.][a-z0-9.-]+",
        str(error_text or "").lower(),
    )))


def _sec_to_srt(seconds: float) -> str:
    """Конвертация секунд в SRT формат: HH:MM:SS,mmm"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _merge_to_phrases(segments: list, max_chars: int = 70, pause_threshold: float = 0.5) -> list:
    """Сливает мелкие сегменты (слова) в читаемые фразы для SRT.
    Разрыв происходит при паузе > pause_threshold сек, превышении max_chars
    или после знаков конца предложения (. ! ?).
    """
    if not segments:
        return segments

    phrases = []
    buf_words: list = []
    buf_start: float = 0.0
    buf_end: float = 0.0

    for i, seg in enumerate(segments):
        text = seg['text'].strip()
        start = seg['start']
        end = seg['end']

        if not buf_words:
            buf_start = start
            buf_words.append(text)
            buf_end = end
            continue

        pause = start - buf_end
        new_text = ' '.join(buf_words + [text])
        ends_sentence = buf_words[-1][-1] in '.!?'

        if ends_sentence or pause > pause_threshold or len(new_text) > max_chars:
            phrases.append({'id': len(phrases) + 1, 'text': ' '.join(buf_words),
                            'start': buf_start, 'end': buf_end})
            buf_words = [text]
            buf_start = start
        else:
            buf_words.append(text)

        buf_end = end

    if buf_words:
        phrases.append({'id': len(phrases) + 1, 'text': ' '.join(buf_words),
                        'start': buf_start, 'end': buf_end})

    return phrases


def _split_phrases(phrases: list, max_chars: int = 50, unit: str = 'chars') -> list:
    """Разбивает фразы из SRT на короткие куски. unit='chars' → ≤ max_chars символов;
    unit='words' → по max_chars СЛОВ (1 = каждое слово отдельным субтитром).
    Время распределяется пропорционально длине каждого куска.
    """
    result = []
    idx = 1
    for seg in phrases:
        text = seg['text'].strip()
        t0, t1 = seg['start'], seg['end']
        words = text.split()
        if unit == 'words':
            # группируем по N слов
            n = max(1, int(max_chars))
            chunks = [' '.join(words[i:i + n]) for i in range(0, len(words), n)]
        else:
            # по символам (как раньше)
            chunks, cur = [], ''
            for w in words:
                test = (cur + ' ' + w).strip()
                if len(test) <= max_chars:
                    cur = test
                else:
                    if cur:
                        chunks.append(cur)
                    cur = w
            if cur:
                chunks.append(cur)
        if not chunks:
            chunks = [text]
        # Пропорциональное распределение времени
        total_len = sum(len(c) for c in chunks)
        dur = t1 - t0
        t = t0
        for i, chunk in enumerate(chunks):
            if i == len(chunks) - 1:
                end = t1
            else:
                end = t + dur * len(chunk) / total_len if total_len else t1
            result.append({'id': idx, 'text': chunk, 'start': t, 'end': end})
            t = end
            idx += 1
    return result


def get_audio_duration_ffprobe(audio_path: str) -> float:
    """
    Получить длительность аудиофайла через ffprobe

    Args:
        audio_path: Путь к аудиофайлу

    Returns:
        Длительность в секундах
    """
    try:
        cmd = [
            'ffprobe', '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            audio_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        duration = float(result.stdout.strip())
        return round(duration, 2)
    except Exception as e:
        print(f"[WARN] Ne udalos' poluchit' dlinu cherez ffprobe: {e}")
        return None


class AssemblyAITranscriber:
    """Транскрибация аудио через AssemblyAI API"""

    def __init__(self, api_key: Optional[str] = None, language_code: Optional[str] = None):
        """
        Инициализация транскрибера

        Args:
            api_key: API ключ AssemblyAI (если None, берётся из config)
            language_code: Код языка (если None, берётся из config)
        """
        self.api_key = api_key or config.ASSEMBLYAI_API_KEY
        self.language_code = language_code or config.ASSEMBLYAI_LANGUAGE_CODE

        if not self.api_key:
            raise ValueError("AssemblyAI API ключ не указан")
        
        # Проверка языка
        if self.language_code not in config.ASSEMBLYAI_SUPPORTED_LANGUAGES:
            raise ValueError(
                f"Неподдерживаемый язык: {self.language_code}. "
                f"Поддерживаемые: {', '.join(config.ASSEMBLYAI_SUPPORTED_LANGUAGES)}"
            )

        # Настройка API
        aai.settings.api_key = self.api_key
        aai.settings.http_timeout = config.ASSEMBLYAI_HTTP_TIMEOUT
        self.speech_models = list(config.ASSEMBLYAI_SPEECH_MODELS)
        self.active_model = self.speech_models[0] if len(self.speech_models) == 1 else None

        # Конфигурация транскрипции
        self.config = aai.TranscriptionConfig(
            speech_models=self.speech_models,
            language_code=self.language_code,
            auto_highlights=False,
            content_safety=False,
            iab_categories=False,
        )
        
    def _transcribe_with_model_fallback(self, audio_path: str):
        transcriber = aai.Transcriber(config=self.config)
        try:
            transcript = transcriber.transcribe(audio_path)
            if transcript.status == aai.TranscriptStatus.error:
                raise RuntimeError(str(transcript.error))
            reported_model = getattr(transcript, 'speech_model', None)
            self.active_model = (
                str(reported_model)
                if reported_model
                else (
                    self.speech_models[0]
                    if len(self.speech_models) == 1
                    else None
                )
            )
            return transcript, transcriber
        except Exception as first_error:
            rejected = _models_rejected_by_assemblyai(
                str(first_error),
                self.speech_models,
            )
            suggested = _models_suggested_by_assemblyai(str(first_error))
            candidates = [
                model for model in self.speech_models
                if model not in rejected
            ]
            candidates.extend(
                model for model in suggested
                if model not in rejected and model not in candidates
            )
            if not rejected or not candidates:
                raise

            print(
                f"[WARN] AssemblyAI rejected: {', '.join(rejected)}; "
                f"trying individually: {', '.join(candidates)}"
            )
            last_error = first_error
            attempted = set()
            while candidates:
                model = candidates.pop(0)
                if model in attempted:
                    continue
                attempted.add(model)
                fallback_config = aai.TranscriptionConfig(
                    speech_models=[model],
                    language_code=self.language_code,
                    auto_highlights=False,
                    content_safety=False,
                    iab_categories=False,
                )
                fallback_transcriber = aai.Transcriber(config=fallback_config)
                try:
                    transcript = fallback_transcriber.transcribe(audio_path)
                    if transcript.status == aai.TranscriptStatus.error:
                        raise RuntimeError(str(transcript.error))
                    reported_model = getattr(transcript, 'speech_model', None)
                    self.active_model = str(reported_model or model)
                    return transcript, fallback_transcriber
                except Exception as error:
                    last_error = error
                    for suggested_model in _models_suggested_by_assemblyai(str(error)):
                        if suggested_model not in attempted and suggested_model not in candidates:
                            candidates.append(suggested_model)
            raise last_error

    def transcribe(
        self,
        audio_path: str,
        output_path: Optional[str] = None,
        wait_for_completion: bool = True
    ) -> Dict[str, Any]:
        """
        Транскрибация аудиофайла
        
        Args:
            audio_path: Путь к аудиофайлу
            output_path: Путь для сохранения JSON (если None, не сохраняется)
            wait_for_completion: Ждать завершения транскрипции
            
        Returns:
            Словарь с транскрипцией в формате:
            {
                "audioDuration": 60.50,
                "transcription": [
                    {"id": 1, "text": "...", "start": 0.00, "end": 3.45},
                    {"id": 2, "text": "...", "start": 3.45, "end": 6.80}
                ]
            }
        """
        audio_path = Path(audio_path)
        
        if not audio_path.exists():
            raise FileNotFoundError(f"Аудиофайл не найден: {audio_path}")
        
        print(f"\n🎙️  Начало транскрипции: {audio_path.name}")
        print("="*60)

        # Retry loop при ошибках соединения
        max_retries = 3
        retry_delay = 30  # секунд
        last_error = None
        
        for attempt in range(1, max_retries + 1):
            try:
                # Запуск транскрипции
                transcript, transcriber = self._transcribe_with_model_fallback(str(audio_path))

                if attempt > 1:
                    print(f"\n🔄 Попытка {attempt}/{max_retries}...")
                
                print("📤 Загрузка аудиофайла...")

                # Ожидание завершения
                if wait_for_completion:
                    print("⏳ Обработка аудио (это может занять время)...")

                    # Прогресс-бар для ожидания
                    with tqdm(total=100, desc="Транскрипция", unit="%") as pbar:
                        while transcript.status == aai.TranscriptStatus.processing:
                            time.sleep(5.0)
                            transcript = transcriber.get_transcript(transcript.id)
                            # Примерный прогресс
                            pbar.update(min(5, 100 - pbar.n))

                    pbar.update(100 - pbar.n)

                if transcript.status == aai.TranscriptStatus.error:
                    raise RuntimeError(f"Ошибка транскрипции: {transcript.error}")

                # Успех!
                print(f"\n✅ Транскрипция завершена!")
                break  # Выход из retry loop

            except Exception as e:
                last_error = e
                error_msg = str(e)
                
                # Проверяем, можно ли retry
                if attempt < max_retries and ('disconnected' in error_msg.lower() or 'timeout' in error_msg.lower() or 'timed out' in error_msg.lower() or 'connection' in error_msg.lower()):
                    print(f"\n⚠️  Ошибка соединения: {error_msg}")
                    print(f"   Повтор через {retry_delay} сек...")
                    time.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff
                else:
                    raise  # Не retry, выбрасываем ошибку

        # Преобразование в нужный формат
        result = self._convert_to_json(transcript)
        result['language'] = self.language_code

        # Сохранение в файл
        if output_path:
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)

            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(result, f, ensure_ascii=False, indent=2)

            print(f"💾 Сохранено: {output_path}")

            # ИСПРАВЛЕНИЕ: Получаем реальную длительность через ffprobe
            audio_duration = get_audio_duration_ffprobe(str(audio_path))
            if audio_duration:
                result['audioDuration'] = audio_duration
                print(f"   ✓ audioDuration ispravlen: {audio_duration} sek (ffprobe)")

            # ВАЛИДАЦИЯ: audioDuration должен покрывать конец последнего клипа
            _clips = result.get('transcription', [])
            if _clips:
                _last_end = _clips[-1].get('end', 0)
                if _last_end > result.get('audioDuration', 0):
                    _old = result.get('audioDuration', 0)
                    result['audioDuration'] = _last_end
                    print(f"   ⚠️  audioDuration {_old:.2f}s < last clip end {_last_end:.2f}s — corrected to {_last_end:.2f}s")

                # АВТОФИКС: отрицательные длительности → swap start/end
                _neg_dur = 0
                for _c in _clips:
                    if _c.get('end', 0) < _c.get('start', 0):
                        _c['start'], _c['end'] = _c['end'], _c['start']
                        _neg_dur += 1

                # АВТОФИКС: неправильный порядок → сортировка по start
                _ooo = sum(1 for _i in range(len(_clips)-1) if _clips[_i]['start'] > _clips[_i+1]['start'])
                if _ooo:
                    _clips.sort(key=lambda x: x.get('start', 0))
                    result['transcription'] = _clips

                # АВТОФИКС: нулевая длительность (end == start) — AssemblyAI иногда ставит
                # слова как "граничные метки" с dur=0. Фильтр директора требует end > start,
                # иначе слово может быть выкинуто на edge case. start НЕ трогаем (момент фиксирован),
                # добавляем end = start + 0.001 (1мс). Overlap-fix ниже подхватит мини-нахлёст.
                _zero_dur = 0
                for _c in _clips:
                    _s = _c.get('start', 0)
                    if _c.get('end', 0) <= _s:
                        _c['end'] = _s + 0.001
                        _zero_dur += 1

                # АВТОФИКС: нахлёсты → сдвигаем start следующего к end предыдущего.
                # ВАЖНО: после сдвига start может стать ≥ end (если end[i-1] > end[i]).
                # Такое бывает на AssemblyAI word-level glitch-ах, когда одиночное слово
                # получает «прыгающие» таймкоды. Поэтому после сдвига start
                # приподнимаем end, если он ≤ нового start — иначе получим reversed-фрагмент.
                _overlaps = 0
                _reverses_after = 0
                for _i in range(1, len(_clips)):
                    if _clips[_i].get('start', 0) < _clips[_i-1].get('end', 0):
                        _new_start = _clips[_i-1]['end']
                        _clips[_i]['start'] = _new_start
                        if _clips[_i].get('end', 0) <= _new_start:
                            _clips[_i]['end'] = _new_start + 0.001
                            _reverses_after += 1
                        _overlaps += 1

                _fixes = []
                if _neg_dur:          _fixes.append(f"neg dur fixed: {_neg_dur}")
                if _ooo:              _fixes.append(f"out-of-order fixed: {_ooo}")
                if _zero_dur:         _fixes.append(f"zero dur fixed: {_zero_dur}")
                if _overlaps:         _fixes.append(f"overlaps fixed: {_overlaps}")
                if _reverses_after:   _fixes.append(f"reversed-after-overlap fixed: {_reverses_after}")
                if _fixes:
                    print(f"   ⚠️  Transcription clip fixes applied: {', '.join(_fixes)}")
                else:
                    print(f"   ✓ Transcription clips OK (no overlaps/order issues)")

            # Перезаписываем файл с правильной длительностью
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(result, f, ensure_ascii=False, indent=2)

            # Генерация SRT/ папки с субтитрами и чистым текстом
            project_dir = output_path.parent
            srt_dir = project_dir / 'SRT'
            srt_dir.mkdir(exist_ok=True)
            segments = result.get('transcription', [])

            # Чистый текст без таймкодов → SRT/{project}_скрипт.txt
            scenariy_path = srt_dir / f'{project_dir.name}_скрипт.txt'
            with open(scenariy_path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(seg['text'] for seg in segments))
            print(f"📝 Сценарий: {scenariy_path}")

            # Субтитры .srt → SRT/subtitles.srt (длина фразы из config: символы или слова)
            srt_path = srt_dir / 'subtitles.srt'
            _sub_unit = getattr(config, 'SUBTITLE_PHRASE_UNIT', 'chars')
            _sub_len = int(getattr(config, 'SUBTITLE_PHRASE_LEN', 50) or (50 if _sub_unit == 'chars' else 1))
            srt_segments = _merge_to_phrases(segments, max_chars=40)
            srt_segments = _split_phrases(srt_segments, max_chars=_sub_len, unit=_sub_unit)
            with open(srt_path, 'w', encoding='utf-8-sig') as f:
                for seg in srt_segments:
                    f.write(f"{seg['id']}\n")
                    f.write(f"{_sec_to_srt(seg['start'])} --> {_sec_to_srt(seg['end'])}\n")
                    f.write(f"{seg['text']}\n\n")
            _unit_lbl = f"{_sub_len} слов" if _sub_unit == 'words' else f"<={_sub_len} симв"
            print(f"🎬 Субтитры:  {srt_path} ({len(srt_segments)} фраз, {_unit_lbl})")

            # Лог прогона
            dur = result.get('audioDuration', 0)
            from units.inbox_manager import append_run_log
            project_name = project_dir.name
            append_run_log(project_dir, project_name, 'ТРАНСКРИПЦИЯ', {
                'Аудиофайл:':    Path(audio_path).name,
                'Язык:':         self.language_code,
                'Режим:':        config.ASSEMBLYAI_SEGMENTATION_MODE,
                'Сегментов:':    len(segments),
                'Длительность:': f'{dur:.1f} сек ({dur/60:.1f} мин)',
            })

        return result
    
    def _convert_to_json(self, transcript: aai.Transcript) -> Dict[str, Any]:
        """
        Конвертация результата AssemblyAI в нужный JSON формат.
        Использует get_sentences() для разбивки по предложениям (не по паузам).

        Args:
            transcript: Результат транскрипции AssemblyAI

        Returns:
            Словарь в нужном формате
        """
        # Получение длительности аудио (в миллисекундах от AssemblyAI)
        audio_duration_ms = transcript.audio_duration or 0
        audio_duration_sec = round(audio_duration_ms / 1000.0, 2)

        transcription = []

        # Разбивка по предложениям, абзацам или словам через AssemblyAI NLP
        seg_mode = config.ASSEMBLYAI_SEGMENTATION_MODE

        if seg_mode == 'words':
            # Пословная разбивка — каждое слово = отдельный сегмент
            words = transcript.words or []
            for i, word in enumerate(words, start=1):
                transcription.append({
                    "id": i,
                    "text": word.text.strip(),
                    "start": round(word.start / 1000.0, 2),
                    "end": round(word.end / 1000.0, 2),
                })
            if transcription:
                print(f"   ✓ Разбивка по словам (words): {len(transcription)} сегментов")
        else:
            try:
                if seg_mode == 'paragraphs':
                    segments = transcript.get_paragraphs()
                    mode_label = "абзацам"
                else:
                    segments = transcript.get_sentences()
                    mode_label = "предложениям"

                for i, seg in enumerate(segments, start=1):
                    transcription.append({
                        "id": i,
                        "text": seg.text.strip(),
                        "start": round(seg.start / 1000.0, 2),
                        "end": round(seg.end / 1000.0, 2),
                    })
                if transcription:
                    print(f"   ✓ Разбивка по {mode_label} ({seg_mode}): {len(transcription)} сегментов")
            except Exception as e:
                print(f"[WARN] get_{seg_mode}() failed: {e} — fallback to utterances")

        # Fallback: utterances (разбивка по паузам/спикеру)
        if not transcription:
            utterances = transcript.utterances or []
            for i, utterance in enumerate(utterances, start=1):
                transcription.append({
                    "id": i,
                    "text": utterance.text.strip(),
                    "start": round(utterance.start / 1000.0, 2),
                    "end": round(utterance.end / 1000.0, 2),
                })
            if transcription:
                print(f"   ✓ Fallback utterances: {len(transcription)} сегментов")

        # Fallback: слова (по 7 слов или паузе > 0.5с)
        if not transcription and transcript.words:
            words = transcript.words
            phrase_words = []
            phrase_start = 0
            for i, word in enumerate(words):
                if not phrase_words:
                    phrase_start = word.start
                phrase_words.append(word.text)
                if len(phrase_words) >= 7 or (i < len(words) - 1 and
                        words[i + 1].start - word.end > 500):
                    transcription.append({
                        "id": len(transcription) + 1,
                        "text": " ".join(phrase_words),
                        "start": round(phrase_start / 1000.0, 2),
                        "end": round(word.end / 1000.0, 2),
                    })
                    phrase_words = []
            if phrase_words and words:
                transcription.append({
                    "id": len(transcription) + 1,
                    "text": " ".join(phrase_words),
                    "start": round(phrase_start / 1000.0, 2),
                    "end": round(words[-1].end / 1000.0, 2),
                })
            if transcription:
                print(f"   ✓ Fallback words: {len(transcription)} сегментов")

        word_timeline = []
        for index, word in enumerate(transcript.words or []):
            text = str(getattr(word, 'text', '') or '').strip()
            start = round(float(getattr(word, 'start', 0) or 0) / 1000.0, 3)
            end = round(float(getattr(word, 'end', 0) or 0) / 1000.0, 3)
            if text and end > start:
                word_timeline.append({
                    "index": index,
                    "text": text,
                    "start": start,
                    "end": end,
                })

        result = {
            "audioDuration": audio_duration_sec,
            "transcription": transcription,
        }
        if word_timeline:
            # Backward-compatible addition: ordinary directors continue to read
            # transcription[], while profile directors can use exact word timing.
            result["words"] = word_timeline

        return result
    
def transcribe_audio(
    audio_path: str,
    output_path: str,
    api_key: Optional[str] = None,
    language_code: Optional[str] = None
) -> Dict[str, Any]:
    """
    Удобная функция для транскрипции

    Args:
        audio_path: Путь к аудиофайлу
        output_path: Путь для сохранения JSON
        api_key: API ключ (опционально)
        language_code: Код языка (опционально)

    Returns:
        Словарь с транскрипцией
    """
    transcriber = AssemblyAITranscriber(api_key=api_key, language_code=language_code)
    result = transcriber.transcribe(audio_path, output_path)

    # Валидация формата
    is_valid, errors = validate_transcription_json(result)

    if not is_valid:
        print(f"\n⚠️  Предупреждение валидации формата:")
        for error in errors:
            print(f"   • {error}")

    # Валидация качества (транслит, покрытие, плотность)
    from units.plan_validator import validate_transcription_quality

    lang = language_code or config.ASSEMBLYAI_LANGUAGE_CODE
    quality_warnings, quality_is_critical = validate_transcription_quality(result, lang)

    # Universal 2 can occasionally complete successfully with unusable
    # coverage/density. Retry that specific result on the higher-quality model.
    if (
        quality_is_critical
        and 'universal-2' in transcriber.speech_models
        and not any(
            model_id in str(transcriber.active_model or '').lower().replace('_', '-')
            for model_id in ('universal-3.5-pro', 'universal-3-5-pro')
        )
    ):
        quality_fallback_model = 'universal-3-5-pro'
        print(
            f"[WARN] Universal 2 returned critical-quality transcription; "
            f"retrying with {quality_fallback_model}"
        )
        fallback_transcriber = AssemblyAITranscriber(
            api_key=api_key,
            language_code=language_code,
        )
        fallback_transcriber.speech_models = [quality_fallback_model]
        fallback_transcriber.active_model = quality_fallback_model
        fallback_transcriber.config = aai.TranscriptionConfig(
            speech_models=[quality_fallback_model],
            language_code=fallback_transcriber.language_code,
            auto_highlights=False,
            content_safety=False,
            iab_categories=False,
        )
        result = fallback_transcriber.transcribe(audio_path, output_path)
        transcriber = fallback_transcriber
        is_valid, errors = validate_transcription_json(result)
        quality_warnings, quality_is_critical = validate_transcription_quality(
            result,
            lang,
        )

    if quality_warnings:
        print()
        if quality_is_critical:
            print("=" * 60)
            print("⛔ КРИТИЧЕСКИЕ ПРОБЛЕМЫ ТРАНСКРИПЦИИ:")
            print("=" * 60)
        else:
            print("⚠️  Предупреждения по качеству транскрипции:")

        for w in quality_warnings:
            print(f"   • {w}")
        print()

    # Сохраняем флаг критичности в результат для проверки в pipeline
    result['_quality_critical'] = quality_is_critical

    return result


