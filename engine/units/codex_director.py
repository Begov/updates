"""
GPT Director через Codex CLI.
Режиссёр через Codex CLI: интерфейс analyze().
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime

import params as config
from units.text_io_utils import read_user_text
from units.plan_validator import validate_montage_plan_json, check_clip_id_sequence
from units import codex_netproxy as codex_proxy
from units.proc_tools import NO_WINDOW


_CODEX_PROXY_ENV_KEYS = (
    'CODEX_PROXY',
    'HTTPS_PROXY', 'https_proxy',
    'HTTP_PROXY', 'http_proxy',
    'ALL_PROXY', 'all_proxy',
)


def _codex_env_without_proxy(env: dict) -> dict:
    direct = dict(env)
    for key in _CODEX_PROXY_ENV_KEYS:
        direct.pop(key, None)
    return direct


def _codex_transport_failed(output: str) -> bool:
    low = (output or '').lower()
    return any(marker in low for marker in (
        'unable to connect to proxy',
        'proxyerror',
        'connection refused',
        'os error 10061',
        'конечный компьютер отверг',
        'failed to connect to websocket',
        'error sending request for url',
        'stream disconnected before completion',
        'failed to refresh available models',
        'timeout waiting for child process to exit',
    ))


_CODEX_STAGE_NAMES = {
    'pass1_control': 'монтажный план (Pass 1)',
    'pass2_control': 'промптаж (Pass 2)',
}


def _file_control_phase_label(work_dir_name: str) -> str:
    """Вызовы по контрольной схеме (file_control): фаза зашита в имя рабочей папки `codex_<phase>`."""
    phase = work_dir_name[len('codex_'):] if work_dir_name.startswith('codex_') else work_dir_name
    repair = phase.endswith('_repair')
    if repair:
        phase = phase[:-len('_repair')]
    parts = phase.split('_')
    numbers = [int(chunk) for chunk in parts if chunk.isdigit()]
    name = '_'.join(chunk for chunk in parts if not chunk.isdigit()) or phase
    if numbers:
        name = f'{name}, чанк {numbers[0]}'
    return f'{name} (повтор)' if repair else name


def _codex_stage_label(schema_file: str, result_file) -> str:
    """Как назвать этап в журнале.

    Раньше здесь стояло жёсткое «Pass 1», и каждый вызов Codex — план сцен,
    нарратив, каждая из партий поисковых планов — выглядел повтором Pass 1.
    Имя берём по схеме, номер партии — из файла результата: он называется по
    фазе (visual_queries_003_result.json).
    """
    base = str(schema_file).replace('.json', '')
    if base == 'file_control':
        return _file_control_phase_label(Path(str(result_file)).parent.name)
    name = _CODEX_STAGE_NAMES.get(base) or base
    # Номера берём ТОЛЬКО из отдельных числовых кусков имени: иначе «pass1»
    # превращался бы в «пачку 1». Имя фазы поисковых планов несёт и общее
    # число пачек (visual_queries_006_of_019) — показываем «6 из 19», чтобы
    # по журналу было видно, сколько ещё осталось.
    stem = Path(str(result_file)).stem
    if stem.endswith('_result'):
        stem = stem[:-len('_result')]
    numbers = [int(chunk) for chunk in stem.split('_') if chunk.isdigit()]
    if len(numbers) >= 2:
        return f'{name}, пачка {numbers[0]} из {numbers[1]}'
    if numbers:
        return f'{name}, пачка {numbers[0]}'
    return name


def _prepare_codex_env() -> tuple[dict, str, bool]:
    """Окружение Codex с единым на процесс вердиктом о прокси.

    Проверка живёт в modules/codex_proxy: мёртвый прокси признаётся ОДИН раз,
    дальше весь прогон идёт напрямую без повторных проб и без повторных
    предупреждений в журнале.
    """
    return codex_proxy.prepare()


def _publish_visual_intent(prompts_path: Path, id_pairs: list) -> int:
    """Положить замысел кадра из Pass 2 в montage_plan как `visual_intent`.

    Зачем. Стадии-источники (сток) и вижн берут описание сцены из
    плана, а не из файла промптов. Пока Pass 2 писал только в *_prompts_*.txt,
    в стандартном режиме им доставалась сырая озвучка — то есть ЧТО ГОВОРЯТ
    вместо того, ЧТО ПОКАЗАТЬ. Запросы стадии по-прежнему пишут сами, здесь только контекст.

    Вызывать ДО приклейки стиля и негатива: стадиям нужна суть сцены.

    Не критично для пайплайна: любая ошибка записи логируется и глотается —
    промпты уже собраны, ронять из-за контекста для стадий нечего.
    Возвращает число клипов, которым проставлено описание.
    """
    pairs = [(cid, text) for cid, text in id_pairs
             if cid is not None and str(text or '').strip()]
    if not pairs:
        return 0
    montage_path = Path(prompts_path).parent / 'montage_plan.json'
    if not montage_path.exists():
        logging.info("Pass2: montage_plan рядом с промптами не найден (%s) — "
                     "visual_intent не проставлен", montage_path)
        return 0
    try:
        from units.source_ledger import apply_clip_source_delta

        delta = {int(cid): {'visual_intent': _compact_intent(text)}
                 for cid, text in pairs}
        # Дельтой под замком: стадии уже читают и правят этот же файл.
        apply_clip_source_delta(montage_path, delta,
                                log=lambda m: logging.debug("[Pass2/intent] %s", m))
        logging.info("Pass2: visual_intent проставлен %s клип(ам)", len(delta))
        return len(delta)
    except Exception as exc:                              # noqa: BLE001
        logging.warning("Pass2: visual_intent не записан в %s: %s",
                        montage_path, exc, exc_info=True)
        return 0


def _compact_intent(text: str, limit: int = 1200) -> str:
    """Обрезать описание по границе предложения, чтобы стадия читала целое.

    Промпт бывает длинным; стадиям нужен визуальный смысл, а не каждая деталь
    освещения. Режем по последней точке до лимита — обрывок на полуслове хуже
    короткого, но законченного описания.
    """
    text = ' '.join(str(text or '').split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    stop = max(cut.rfind('. '), cut.rfind('! '), cut.rfind('? '))
    return (cut[:stop + 1] if stop > limit // 2 else cut).strip()


def _build_clip_card(clip: dict, fragments: list) -> str:
    """Собирает карточку клипа для GPT: голос за кадром и, для инфографики, указание источника."""
    cs = clip.get('startTime', 0)
    ce = clip.get('endTime', 0)
    texts = [f['text'] for f in fragments
             if f.get('start', 0) < ce and f.get('end', 0) > cs]
    voiceover = ' '.join(texts).strip() or '(pause)'
    source_line = '\nSOURCE: INFOGRAPHIC (make a static information graphic, not a cinematic scene)' \
        if clip.get('source') == 'infographic' else ''
    return f"[CLIP {clip['id']}]{source_line}\nVOICEOVER: {voiceover}"


# ============================================================================
# NEGATIVE BLOCK EXTERNALIZATION (Pass 2 output-size optimization)
# ============================================================================
# Стайлгайд содержит секцию "## NEGATIVE PROMPT — APPEND TO EVERY PROMPT VERBATIM"
# с code-блоком. Негатив ОДИНАКОВ во всех промптах (≈180 слов × N клипов дублирования
# в выводе модели → упирается в лимит ответа). Выносим: модель НЕ пишет негатив,
# скрипт дописывает его при постобработке. Стиль НЕ выносим (модель его варьирует).

# Якорь "вставлять в каждый промпт" — синонимы (EN + RU) для питон-регекса.
# Юзер вписывает одну из этих фраз в заголовок блока SG → питон подхватит блок.
_VERBATIM_ANCHOR = (
    r'(?:VERBATIM|EVERY\s+PROMPT|EACH\s+PROMPT|ALL\s+PROMPTS|'
    r'КАЖД\w*\s+ПРОМПТ|В\s+КАЖДЫЙ|ВСТАВ\w*\s+В\s+КАЖД)'
)


def _extract_anchored_block(style_guide: str, kind_re: str) -> Optional[str]:
    """Достаёт code-блок секции '## <KIND> ... <VERBATIM-anchor> ... ```...```'.

    kind_re — regex имени секции, напр. 'NEGATIVE' или r'STYLE\\s*BLOCK'.
    Возвращает содержимое code-fence или None если якорной секции нет.
    """
    if not style_guide:
        return None
    m = re.search(
        rf'#+\s*{kind_re}[^\n]*{_VERBATIM_ANCHOR}[^\n]*\n\s*```[a-z]*\n(.*?)\n```',
        style_guide, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    txt = m.group(1).strip()
    return txt or None


def extract_negative_block(style_guide: str) -> Optional[str]:
    """Извлекает NEGATIVE-блок из стайлгайда по якорю (питон-регекс).

    endline обычно внутри него. None если якорной секции нет (fallback на нейро/модель).
    """
    return _extract_anchored_block(style_guide, r'NEGATIVE')


def extract_style_block(style_guide: str) -> Optional[str]:
    """Извлекает STYLE-блок из стайлгайда по якорю (питон-регекс).

    Тот же повторяющийся-в-каждом-промпте блок стиля. None если якорной секции нет.
    """
    return _extract_anchored_block(style_guide, r'STYLE\s*BLOCK')


def build_hardmatch(token: str) -> str:
    """Каноническая hard-match подпись для токена. Имя токена подставляется из него самого
    (нашли [sael_ref.jpg] → подпись про sael_ref.jpg). Шаблон один, привязка через имя."""
    return (f'(MATCH the face, skin, hair, eyes and markings of "{token}" reference image '
            f'EXACTLY — do NOT invent any facial geometry, eye shape, skin tone, hair colour '
            f'or markings; only pose, expression, action and clothing come from this prompt)')


def has_reference_images(project_dir: str | Path) -> bool:
    """Return True only when the current project has actual image refs."""
    if os.environ.get('BEGO_REFS_DISABLED', '') == '1':
        return False                  # BegoFrame: референсы выключены, старый ref/ проекта не в счёт
    ref_dir = Path(project_dir) / 'ref'
    if not ref_dir.is_dir():
        return False
    image_exts = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.tif', '.tiff'}
    return any(p.is_file() and p.suffix.lower() in image_exts for p in ref_dir.iterdir())


def apply_ref_hardmatch(prompt: str) -> str:
    """Дописывает hard-match подпись к ПЕРВОМУ появлению каждого ref-токена в промпте.

    Модель пишет короткий [name_ref.jpg], скрипт расширяет до [name_ref.jpg] (MATCH ...).
    Если подпись уже есть после токена (модель не послушала override) — не дублируем.
    Имя в подписи = имя самого токена (sael→sael, ravon→ravon).
    """
    if not prompt:
        return prompt
    seen = set()
    out = prompt
    # Идём по всем токенам в порядке появления, расширяем первое вхождение каждого
    for m in re.finditer(r'\[([a-z0-9_]+_ref\.jpg)\]', prompt, re.IGNORECASE):
        tok = m.group(1)
        if tok in seen:
            continue
        seen.add(tok)
        # найти первое вхождение этого токена в АКТУАЛЬНОМ out (позиции сдвигаются при вставке)
        mm = re.search(re.escape(f'[{tok}]'), out, re.IGNORECASE)
        if not mm:
            continue
        tail = out[mm.end():mm.end() + 12].lstrip()
        if tail[:6].upper().startswith('(MATCH'):
            continue  # подпись уже есть — не дублируем
        hm = build_hardmatch(tok)
        out = out[:mm.end()] + ' ' + hm + out[mm.end():]
    return out


def strip_negative_section(style_guide: str) -> str:
    """Убирает из стайлгайда NEGATIVE-секцию (заголовок + code-блок) + правит чеклист,
    чтобы модель НЕ видела требования писать негатив verbatim (источник конфликта с override).

    Контент негатива у нас уже извлечён (extract_negative_block) и допишется постобработкой.
    Стиль НЕ трогаем — он остаётся в стайлгайде, модель пишет его сама.
    """
    if not style_guide:
        return style_guide
    sg = style_guide
    # 1) вырезать секцию "## NEGATIVE PROMPT — APPEND ... VERBATIM" + следующий code-блок
    sg = re.sub(
        r'#+\s*NEGATIVE[^\n]*VERBATIM[^\n]*\n\s*```[a-z]*\n.*?\n```\s*',
        '\n[NEGATIVE PROMPT: omitted — appended automatically by the system, do NOT write it]\n\n',
        sg, flags=re.IGNORECASE | re.DOTALL)
    # 2) смягчить упоминания "full NEGATIVE ... verbatim" в чеклисте/blueprint
    sg = re.sub(r'\+?\s*full\s+NEGATIVE\s+(prompt\s+)?(appended\s+)?verbatim',
                ' (negative prompt added automatically)', sg, flags=re.IGNORECASE)
    return sg


def strip_hardmatch_section(style_guide: str) -> str:
    """Убирает из стайлгайда требование писать hard-match подпись после токенов.

    Контент подписи добавляется постобработкой (apply_ref_hardmatch). Модель должна
    писать только короткий [name_ref.jpg]. Сам список токенов и COUNT RULE НЕ трогаем
    (токены модель писать обязана — это привязка персонажа).
    """
    if not style_guide:
        return style_guide
    sg = style_guide
    # Вырезать блок "HARD-MATCH — MANDATORY ... [пример подписи]" (до следующего ** или ##)
    sg = re.sub(
        r'\*\*HARD-MATCH[^\n]*\*\*.*?(?=\n\*\*[A-Z]|\n##|\Z)',
        '**REF TOKENS:** write each character as a short token only, e.g. [name_ref.jpg]. '
        'Do NOT add the "(MATCH the face...)" hard-match description — it is appended '
        'automatically by the system. Keep the token short to save response space.\n\n',
        sg, flags=re.IGNORECASE | re.DOTALL)
    # Смягчить упоминания "hard-match on first mention" в blueprint/checklist
    sg = re.sub(r',?\s*hard-match\s+(on\s+)?(first\s+mention)?',
                '', sg, flags=re.IGNORECASE)
    return sg


def append_negative_tail(prompt: str, negative: str) -> str:
    """Гарантирует канонический 'Negative prompt: <negative>' в конце промпта.

    Режим ЗАМЕНЫ (не дополнения): если модель всё же написала свой негатив (не послушала
    override) — обрезаем его и ставим канонический из стайлгайда. Так негатив всегда полный
    и точный, независимо от поведения модели.
    """
    if not negative:
        return prompt
    p = prompt.rstrip()
    # Обрезать всё от первого упоминания "Negative prompt:" (модель могла написать свой)
    m = re.search(r'\.?\s*negative\s+prompt\s*:', p, re.IGNORECASE)
    if m:
        p = p[:m.start()].rstrip()
    p = p.rstrip().rstrip(',').rstrip().rstrip('.')
    return f"{p}. Negative prompt: {negative}"


def append_style_tail(prompt: str, style: str) -> str:
    """Приклеивает STYLE-блок в конец промпта-сцены, ПЕРЕД негативом.

    Порядок финального промпта: scene, STYLE. Negative prompt: NEGATIVE.
    Если негатив уже стоит — вставляет стиль перед ним (устойчив к порядку вызова).
    """
    if not style:
        return prompt
    p = prompt.rstrip()
    m = re.search(r'\.?\s*negative\s+prompt\s*:', p, re.IGNORECASE)
    if m:
        head = p[:m.start()].rstrip().rstrip(',').rstrip().rstrip('.')
        neg = p[m.start():].lstrip('. ')
        return f"{head}, {style}. {neg}"
    p = p.rstrip().rstrip(',').rstrip().rstrip('.')
    return f"{p}, {style}"


def strip_style_section(style_guide: str) -> str:
    """Убирает из стайлгайда STYLE-секцию (заголовок + code-блок) + правит чеклист,
    чтобы модель НЕ выписывала стиль verbatim. Контент уже извлечён и допишется скриптом.
    """
    if not style_guide:
        return style_guide
    sg = style_guide
    sg = re.sub(
        rf'#+\s*STYLE\s*BLOCK[^\n]*{_VERBATIM_ANCHOR}[^\n]*\n\s*```[a-z]*\n.*?\n```\s*',
        '\n[STYLE BLOCK: omitted — appended automatically by the system, do NOT write it]\n\n',
        sg, flags=re.IGNORECASE | re.DOTALL)
    sg = re.sub(r'\+?\s*full\s+STYLE\s+BLOCK(\s+verbatim)?',
                ' (style block added automatically)', sg, flags=re.IGNORECASE)
    return sg


# ============================================================================
# GPT DIRECTOR
# ============================================================================


class GptDirector:
    """Режиссёрский анализ через GPT (Codex CLI)"""

    def __init__(self, model_name: Optional[str] = None):
        # При DeepSeek/OpenRouter модель всегда 'moonbridge' (route в Moon Bridge), иначе GPT_MODEL/override
        self.model_name = config.codex_model_for_provider(model_name or config.GPT_MODEL)
        self._is_deepseek = config.gpt_provider_is_deepseek()
        self._is_openrouter = config.gpt_provider_is_openrouter()
        self._uses_moonbridge = config.gpt_provider_uses_moonbridge()
        self.codex_exe = Path(config.CODEX_EXE_PATH)
        self.schemas_dir = Path(__file__).parent.parent / 'schemas'
        self.prompts_dir = Path(__file__).parent.parent / 'instructions'
        self.timeout = config.GPT_DIRECTOR_TIMEOUT
        self._ext_negative = None  # вынесенный negative-блок (устанавливается в _run_pass2)
        self._ext_style = None  # вынесенный style-блок (устанавливается в _run_pass2)
        self._ext_refmatch = False  # выносить hard-match подпись рефов (устанавливается в _run_pass2)

        if not config.CODEX_EXE_PATH or not self.codex_exe.is_file():
            raise FileNotFoundError(f"Codex CLI не найден: {self.codex_exe}")

        # DeepSeek/OpenRouter: гарантируем, что Moon Bridge (прокси Codex->chat/completions) поднят и
        # codex config.toml сгенерён. Идемпотентно: если GUI уже поднял — мгновенно.
        if self._uses_moonbridge:
            from units import bridge_manager as _mb
            _mb.ensure_ready()

    def _write_run_log(self, output_dir, project_name, audio_duration,
                       num_clips, num_prompts, *, two_pass=False,
                       prompts_only=False, started=None,
                       stage='РЕЖИССУРА') -> None:
        """Запись стадии в общий `<проект>_run_log.txt`.

        Без неё в логе прогона была одна строка про транскрипцию, а самая
        дорогая стадия не отмечалась вовсе (улов 01.09.2026). Стоимость не
        пишем: подписка ChatGPT её не отдаёт.
        """
        try:
            from units.inbox_manager import append_run_log
            params = {
                'Бэкенд:':     'GPT (Codex CLI)',
                'Модель:':     self.model_name,
                'Проход:':     ('только промпты' if prompts_only
                                else '2-проходный' if two_pass else '1-проходный'),
                'Клипов:':     num_clips,
                'Промптов:':   f'{num_prompts} / {num_clips}' if num_clips else num_prompts,
            }
            if audio_duration:
                params['Аудио:'] = f'{audio_duration:.1f} сек ({audio_duration / 60:.1f} мин)'
            if started:
                _el = max(0.0, time.time() - started)
                params['Время:'] = f'{int(_el) // 60}м {int(_el) % 60:02d}с'
            append_run_log(Path(output_dir), project_name, stage, params)
        except Exception as exc:                                   # noqa: BLE001
            logging.debug("run_log: запись режиссуры не удалась: %s", exc)

    # =========================================================================
    # ГЛАВНЫЙ МЕТОД — analyze()
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
        pass1_only: bool = False,
    ) -> Dict[str, Any]:
        output_dir = Path(output_dir)
        transcription_path = Path(transcription_path)
        style_guide_path = Path(style_guide_path)

        # Загрузить транскрипцию
        transcription = json.loads(transcription_path.read_text(encoding='utf-8-sig'))
        audio_duration = transcription.get('audioDuration', 0)
        style_guide = read_user_text(style_guide_path)

        results = {}
        _run_started = time.time()

        if prompts_only:
            mp_path = output_dir / 'montage_plan.json'
            if not mp_path.exists():
                raise FileNotFoundError(f"montage_plan.json не найден: {mp_path}")
            montage_plan = json.loads(mp_path.read_text(encoding='utf-8-sig'))
        else:
            # === PASS 1 — montage_plan.json ===
            montage_plan, mp_path = self._run_pass1(
                transcription_path, style_guide_path,
                output_dir, project_name, audio_duration, transcription
            )

        results['montage_plan'] = str(mp_path)
        results['num_clips'] = len(montage_plan.get('clips', []))

        if pass1_only:
            results['prompts'] = None
            results['num_prompts'] = 0
            results['cost'] = {
                'total_cost': 0,
                'input_tokens': 0,
                'output_tokens': 0,
                'note': 'Pass 1 only',
            }
            self._write_run_log(output_dir, project_name, audio_duration,
                                results.get('num_clips', 0), 0,
                                two_pass=two_pass, prompts_only=prompts_only,
                                started=_run_started, stage='РЕЖИССУРА (только план)')
            return results

        # === PASS 2 — prompts ===
        # GPT через Codex всегда работает в two-pass режиме (два отдельных exec вызова)
        prompts_path, num_prompts = self._run_pass2(
            mp_path, transcription_path, style_guide_path,
            output_dir, project_name, montage_plan,
            resume_path=resume_path
        )
        if num_prompts == 0:
            raise RuntimeError(
                f"GPT Pass 2 вернул 0 промптов — файл пустой или Codex не записал. "
                f"Пайплайн остановлен. Перезапустите с режимом 'Prompts only'."
            )
        results['prompts'] = str(prompts_path) if prompts_path else None
        results['num_prompts'] = num_prompts
        results['cost'] = {
            'total_cost': 0,
            'input_tokens': 0,
            'output_tokens': 0,
            'note': 'GPT via ChatGPT subscription'
        }
        self._write_run_log(output_dir, project_name, audio_duration,
                            results.get('num_clips', 0), num_prompts,
                            two_pass=two_pass, prompts_only=prompts_only,
                            started=_run_started)
        return results

    # =========================================================================
    # CODEX SUBPROCESS WRAPPER
    # =========================================================================

    def _run_codex(
        self,
        task_prompt: str,
        schema_file: str,
        result_file: Path,
        working_dir: Path,
    ) -> dict:
        """Запуск codex exec с ПОВТОРОМ при разовом отказе на старте.

        Codex перед работой обновляет список доступных моделей, и этот запрос
        иногда рвётся: «failed to refresh available models: stream disconnected
        before completion». Процесс выходит с кодом 1 за несколько секунд, не
        начав задачу. Улов 27.08: на этом упал Pass 1, и весь прогон встал за
        0.7 минуты — при том, что повтор проходит нормально. Повтор здесь
        бесплатен: модель ничего не успела сделать, платить не за что.

        Не повторяем только то, что повтором не лечится: «модель новее CLI».
        """
        attempts = max(1, int(getattr(config, 'CODEX_RETRIES', 2) or 1))
        last_error: Optional[BaseException] = None
        for attempt in range(1, attempts + 1):
            try:
                return self._run_codex_once(
                    task_prompt, schema_file, result_file, working_dir)
            except RuntimeError as exc:
                if 'требует более новую версию' in str(exc) or attempt >= attempts:
                    raise
                last_error = exc
                logging.warning(
                    "Codex: попытка %s из %s не удалась — повтор через 5с. %s",
                    attempt, attempts, str(exc)[:300])
                print(f"   ⚠️  Codex не ответил (попытка {attempt}/{attempts}) "
                      f"— повторяю", flush=True)
                time.sleep(5.0)
        raise last_error

    def _run_codex_once(
        self,
        task_prompt: str,
        schema_file: str,
        result_file: Path,
        working_dir: Path,
    ) -> dict:
        """
        Запуск codex exec и чтение control JSON.
        Возвращает распарсенный control dict.
        """
        schema_path = self.schemas_dir / schema_file

        if not schema_path.exists():
            raise FileNotFoundError(f"Schema не найдена: {schema_path}")

        # Удалить предыдущий result если есть
        if result_file.exists():
            result_file.unlink()

        # Write task prompt to file to avoid Windows MAX_PATH / cmd length limits
        task_file = working_dir / f'_codex_{schema_file.replace(".json", "")}_task.md'
        task_file.write_text(task_prompt, encoding='utf-8')
        short_prompt = f"Read and follow ALL instructions from this file: {task_file.as_posix()}"

        cmd = [
            str(self.codex_exe), 'exec',
            short_prompt,
            '--output-schema', str(schema_path),
            '--output-last-message', str(result_file),
            '--dangerously-bypass-approvals-and-sandbox',
            '--skip-git-repo-check',
            *config.codex_cli_args_for_provider(),
            '-m', self.model_name,
            '-C', str(working_dir),
        ]

        stage_label = _codex_stage_label(schema_file, result_file)
        logging.info(
            f"Codex exec: {stage_label} | model={self.model_name} "
            f"schema={schema_file} cwd={working_dir}")
        t0 = time.time()

        # Run with real-time output streaming
        _stdout_lines = []
        _stderr_lines = []
        _returncode = [None]
        _done = threading.Event()
        _last_activity = [t0]

        def _exec():
            try:
                run_env, fallback_reason, proxy_in_use = _prepare_codex_env()
                if fallback_reason:
                    msg = (
                        f"Codex proxy недоступен; «{stage_label}» и весь "
                        f"остаток прогона идут напрямую ({fallback_reason})"
                    )
                    logging.warning(msg)
                    print(f"   ⚠️  {msg}", flush=True)

                attempts = [('configured route', run_env)]
                # Запасной «прямой» повтор нужен только когда первая попытка
                # реально идёт через прокси: иначе это дубль той же команды.
                if proxy_in_use:
                    attempts.append(('direct fallback', _codex_env_without_proxy(run_env)))

                for attempt_index, (route_name, attempt_env) in enumerate(attempts):
                    attempt_stdout = []
                    attempt_stderr = []
                    p = None
                    try:
                        p = subprocess.Popen(
                            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding='utf-8', errors='replace',
                            env=attempt_env,
                            **NO_WINDOW,
                        )

                        def _read_stderr():
                            for line in p.stderr:
                                attempt_stderr.append(line.rstrip())
                                _last_activity[0] = time.time()

                        serr_t = threading.Thread(target=_read_stderr, daemon=True)
                        serr_t.start()

                        for line in p.stdout:
                            line = line.rstrip()
                            if line:
                                attempt_stdout.append(line)
                                _last_activity[0] = time.time()
                                ll = line.lower()
                                if any(k in ll for k in (
                                    'write', 'read', 'wrote', 'append', 'file', 'prompt',
                                )):
                                    sys.stdout.write(f'\r   📝 {line[:100]:<100}\n')
                                    sys.stdout.flush()

                        p.wait(timeout=self.timeout)
                        serr_t.join(timeout=5)
                        _returncode[0] = p.returncode
                    except subprocess.TimeoutExpired:
                        if p is not None:
                            p.kill()
                        _returncode[0] = -1
                    except Exception as exc:
                        attempt_stderr.append(str(exc))
                        _returncode[0] = -1

                    _stdout_lines.extend(attempt_stdout)
                    _stderr_lines.extend(attempt_stderr)
                    combined_attempt = '\n'.join(attempt_stderr + attempt_stdout)
                    if _returncode[0] == 0 or result_file.exists():
                        break
                    can_retry_direct = (
                        attempt_index == 0
                        and len(attempts) > 1
                        and _codex_transport_failed(combined_attempt)
                    )
                    if not can_retry_direct:
                        break
                    if result_file.exists():
                        result_file.unlink()
                    msg = (
                        f"Codex {route_name} завершился сетевой ошибкой; "
                        "повторяем Pass 1 напрямую"
                    )
                    logging.warning(msg)
                    print(f"   ⚠️  {msg}", flush=True)
            except subprocess.TimeoutExpired:
                _returncode[0] = -1
            except Exception as exc:
                _stderr_lines.append(str(exc))
                _returncode[0] = -1
            finally:
                _done.set()

        thread = threading.Thread(target=_exec, daemon=True)
        thread.start()

        # Progress monitor — спиннер каждую секунду (GUI перехватывает в нижний лейбл)
        _last_size = 0
        _last_prompts = 0
        _monitor_file = None
        _spinner_chars = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
        _spin_i = 0

        while not _done.is_set():
            elapsed = time.time() - t0
            mins = int(elapsed) // 60
            secs = int(elapsed) % 60
            time_str = f"{mins}м {secs:02d}с" if mins > 0 else f"{secs}с"

            # Re-scan for prompts file periodically (Pass 1 не пишет промпты, но на всякий)
            if not _monitor_file:
                _mf = list(working_dir.glob('*_prompts_*.txt'))
                if _mf:
                    _monitor_file = max(_mf, key=lambda f: f.stat().st_mtime)

            if _monitor_file and _monitor_file.exists():
                try:
                    sz = _monitor_file.stat().st_size
                    if sz != _last_size:
                        _last_size = sz
                        txt = _monitor_file.read_text(encoding='utf-8-sig', errors='replace')
                        n = len([p for p in txt.strip().split('\n\n') if p.strip()])
                        if n != _last_prompts:
                            _last_prompts = n
                except Exception:
                    pass

            # Спиннер каждую секунду
            spin = _spinner_chars[_spin_i % len(_spinner_chars)]
            _spin_i += 1
            idle = time.time() - _last_activity[0]
            idle_warn = f" idle {int(idle)}s" if idle > 60 else ""
            extra = f" | {_last_prompts} prompts" if _last_prompts > 0 else ""
            sys.stdout.write(f'\r{spin} GPT Pass1: {time_str}{extra}{idle_warn}   \n')
            sys.stdout.flush()

            _done.wait(1)

            # Hard timeout
            if elapsed > self.timeout:
                print(f"   ⏰ GPT timeout ({self.timeout}s)")
                logging.error(f"Codex timeout after {self.timeout}s")
                raise RuntimeError(f"Codex timeout after {self.timeout}s")

        elapsed = time.time() - t0
        mins = int(elapsed) // 60
        secs = int(elapsed) % 60
        time_str = f"{mins}м {secs:02d}с" if mins > 0 else f"{secs}с"

        if _returncode[0] == -1 and not _stdout_lines:
            sys.stdout.write(f'\r   ⏰ GPT timeout ({self.timeout}s)              \n')
            sys.stdout.flush()
            logging.error(f"Codex timeout after {self.timeout}s")
            raise RuntimeError(f"Codex timeout after {self.timeout}s")

        if _returncode[0] == 0 and result_file.exists():
            sys.stdout.write(f'\r   ✅ GPT — готово за {time_str}              \n')
        else:
            sys.stdout.write(
                f'\r   ❌ GPT — ошибка за {time_str} (exit {_returncode[0]})       \n')
        sys.stdout.flush()

        proc_stdout = '\n'.join(_stdout_lines)
        proc_stderr = '\n'.join(_stderr_lines)

        logging.info(f"Codex finished in {elapsed:.1f}s, exit_code={_returncode[0]}")

        # Извлечь tokens used из stderr/stdout
        for line in (proc_stderr).split('\n') + (proc_stdout).split('\n'):
            if 'tokens used' in line.lower():
                logging.info(f"Codex tokens: {line.strip()}")
                print(f"   📊 {line.strip()}")

        if _returncode[0] != 0:
            logging.error(f"Codex stderr (last 2000): {proc_stderr[-2000:]}")

        # Понятный хинт: выбранная модель новее, чем установленный Codex CLI
        # (OpenAI отвечает 400 "requires a newer version of Codex"). Юзер видит
        # сырой дамп и не понимает, что делать — переводим в инструкцию.
        _combined = (proc_stderr + '\n' + proc_stdout).lower()
        if 'requires a newer version of codex' in _combined or 'upgrade to the latest' in _combined:
            _msg = (
                f"⛔ Модель '{self.model_name}' требует более новую версию Codex CLI, "
                f"чем у вас установлена.\n"
                f"   РЕШЕНИЕ: вкладка Settings → кнопка «Update Codex CLI» (обновит Codex),\n"
                f"   либо выберите модель постарше (gpt-5.4 / gpt-5.5) в блоке GPT."
            )
            print(f"\n{_msg}\n", flush=True)
            logging.error(f"Codex model too new for CLI: {self.model_name}")
            raise RuntimeError(_msg)

        if not result_file.exists():
            # Начало stderr, а не хвост: настоящая причина у Codex стоит именно
            # там (сбой обновления моделей, отказ авторизации), а в конце —
            # баннер с настройками, по которому ничего не понять.
            _err = (proc_stderr or '').strip()
            _tail = f"  ...конец: {_err[-300:]}" if len(_err) > 800 else ""
            raise RuntimeError(
                f"Codex не создал {result_file.name} "
                f"(exit {_returncode[0]}): {_err[:500]}{_tail}"
            )

        raw = result_file.read_text(encoding='utf-8-sig').strip()
        try:
            control = json.loads(raw)
        except json.JSONDecodeError as e:
            # DeepSeek (через Moon Bridge) не соблюдает strict --output-schema:
            # last-message приходит обычным текстом, а не JSON-control. Codex при
            # этом реально пишет целевой файл на диск. Синтезируем control из
            # последнего сообщения, чтобы Pass 1 читал montage_plan.json с диска.
            if self._uses_moonbridge:
                m = re.search(r'\{.*\}', raw, re.DOTALL)
                if m:
                    try:
                        control = json.loads(m.group(0))
                        logging.info("DeepSeek: control извлечён из текстового last-message")
                        print("   ⏱  Codex(DeepSeek): control из текста")
                        return control
                    except json.JSONDecodeError:
                        pass
                status = 'error' if re.search(r'\berror\b', raw, re.IGNORECASE) and 'ok' not in raw.lower() else 'ok'
                control = {'status': status, 'error_message': raw[:300]}
                logging.warning(f"DeepSeek: schema не соблюдена, fallback control status={status}")
                print(f"   ⏱  Codex(DeepSeek): schema-fallback status={status} (план читается с диска)")
                return control
            logging.error(f"Invalid JSON in {result_file}: {raw[:500]}")
            raise RuntimeError(f"Codex вернул невалидный JSON: {e}")

        logging.info(f"Codex control: {json.dumps(control, ensure_ascii=False)}")
        print(f"   ⏱  Codex: {elapsed:.0f}s, model={self.model_name}")
        return control

    # =========================================================================
    # PASS 2 — FILE MONITORING + TRIVIAL CONTROL SCHEMA
    # =========================================================================

    def _codex_session_dir(self, codex_home: Optional[Path] = None, when: Optional[datetime] = None) -> Path:
        """Return the Codex sessions dir for either isolated CODEX_HOME or the user home."""
        when = when or datetime.now()
        root = Path(codex_home) if codex_home is not None else Path.home() / '.codex'
        return root / 'sessions' / str(when.year) / f'{when.month:02d}' / f'{when.day:02d}'

    def _find_latest_session_file(
        self,
        after_ts: float,
        codex_home: Optional[Path] = None,
        session_id: Optional[str] = None,
    ) -> Optional[Path]:
        """Find the latest Codex JSONL in the same home used by the subprocess."""
        try:
            session_dir = self._codex_session_dir(codex_home)
            if not session_dir.exists():
                return None
            if session_id:
                candidates = list(session_dir.glob(f'*{session_id[:16]}*.jsonl'))
            else:
                candidates = [
                    f for f in session_dir.glob('rollout-*.jsonl')
                    if f.stat().st_mtime >= after_ts - 5
                ]
            return max(candidates, key=lambda f: f.stat().st_mtime) if candidates else None
        except Exception:
            return None

    def _parse_codex_prompts_text(self, raw: str) -> list:
        """Parse Pass2 prompts from Codex text/JSON and reject non-prompt chatter."""
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

        # Codex sometimes wraps JSON in surrounding text despite instructions.
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

    def _extract_last_codex_message(self, session_file: Path, start_size: int = 0) -> str:
        """Read final assistant text from Codex JSONL (event_msg or response_item formats)."""
        if not session_file or not session_file.exists():
            return ''
        try:
            if start_size > 0:
                with open(session_file, 'rb') as f:
                    f.seek(start_size)
                    content = f.read().decode('utf-8', errors='replace')
            else:
                content = session_file.read_text(encoding='utf-8-sig', errors='replace')
        except Exception:
            return ''

        messages = []
        for line in content.strip().split('\n'):
            try:
                ev = json.loads(line)
            except Exception:
                continue
            typ = ev.get('type')
            payload = ev.get('payload', {})
            if typ == 'event_msg' and payload.get('type') == 'agent_message':
                msg = payload.get('message', '')
                if msg:
                    messages.append(msg)
            elif typ == 'response_item':
                item = payload.get('item') if isinstance(payload.get('item'), dict) else payload
                if item.get('type') == 'message' and item.get('role') == 'assistant':
                    parts = []
                    for c in item.get('content') or []:
                        if isinstance(c, dict):
                            txt = c.get('text') or c.get('output_text')
                            if txt:
                                parts.append(str(txt))
                    if parts:
                        messages.append('\n'.join(parts))
        return messages[-1].strip() if messages else ''

    def _run_codex_pass2(
        self,
        task_prompt: str,
        prompts_path: Path,
        n_clips: int,
        working_dir: Path,
        prompts_offset: int = 0,
        session_id: str = None,
        total_clips: int = 0,
        global_start: float = None,
        codex_home: Path = None,
        silent: bool = False,
        keep_ids: bool = False,
        rate_limit_reported: threading.Event = None,
        rate_limit_info: list = None,
    ) -> tuple:
        """
        Запуск codex для Pass 2 — JSON stdout mode.
        GPT возвращает {"prompts":["p1","p2",...]} в stdout (через --output-last-message).
        Мы парсим JSON и сами записываем в prompts_path (append).
        DeepSeek: если codex_home не задан — используем изолированный DeepSeek-home,
        чтобы env и поиск session JSONL смотрели в одно место.
        0 tool calls = 0 context growth = быстро.

        При resume: codex exec resume SESSION_ID — контекст в сессии.
        Fallback: если resume не работает — полный exec с текущего cursor.

        Returns: (total_prompts_in_file, session_id)
        """
        # DeepSeek: дефолтный изолированный CODEX_HOME (config.toml -> Moon Bridge)
        if codex_home is None and self._is_deepseek:
            codex_home = Path(config.DEEPSEEK_CODEX_HOME)
        # Truncate stem to avoid Windows MAX_PATH (260 chars) — long project names
        # with em dashes caused Codex to silently create 8.3 short names, breaking read.
        _stem = prompts_path.stem
        if len(_stem) > 40:
            import hashlib as _hashlib
            _hash = _hashlib.md5(_stem.encode('utf-8', errors='replace')).hexdigest()[:8]
            _stem = _stem[:32] + '_' + _hash
        result_file = working_dir / f'_codex_pass2_control_{_stem}.json'
        if result_file.exists():
            result_file.unlink()

        # Save task prompt to file for debugging, but deliver via stdin (no file read tool calls)
        task_file = working_dir / f'_codex_pass2_task_{_stem}.md'
        task_file.write_text(task_prompt, encoding='utf-8')

        if session_id:
            cmd = [
                str(self.codex_exe), 'exec', 'resume',
                session_id,
                '-',  # read prompt from stdin
                '--output-last-message', str(result_file),
                '--dangerously-bypass-approvals-and-sandbox',
                '--skip-git-repo-check',
                *config.codex_cli_args_for_provider(),
                '-m', self.model_name,
            ]
            logging.info(f"Codex Pass2 RESUME: session={session_id[:16]}..., expect={n_clips} prompts")
        else:
            cmd = [
                str(self.codex_exe), 'exec',
                '-',  # read prompt from stdin
                '--output-last-message', str(result_file),
                '--dangerously-bypass-approvals-and-sandbox',
                '--skip-git-repo-check',
                *config.codex_cli_args_for_provider(),
                '-m', self.model_name,
                '-C', str(working_dir),
            ]
            logging.info(f"Codex Pass2: model={self.model_name}, expect={n_clips} prompts, cwd={working_dir}")

        t0 = time.time()
        _stdout_lines = []
        _stderr_lines = []
        _last_activity = [t0]  # updated on any stdout/stderr line

        _env, _proxy_fallback_reason, _ = _prepare_codex_env()
        if _proxy_fallback_reason:
            msg = (
                "Codex proxy недоступен; дальше весь прогон идёт напрямую "
                f"({_proxy_fallback_reason})"
            )
            logging.warning(msg)
            if not silent:
                print(f"   ⚠️  {msg}", flush=True)
        if codex_home is not None:
            _env['CODEX_HOME'] = str(codex_home)
        p = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
            text=True, encoding='utf-8', errors='replace',
            env=_env,
            **NO_WINDOW,
        )
        # Deliver prompt via stdin — no file read tool calls
        p.stdin.write(task_prompt)
        p.stdin.close()
        def _drain(src, dst):
            for line in src:
                dst.append(line.rstrip())
                _last_activity[0] = time.time()
        threading.Thread(target=_drain, args=(p.stdout, _stdout_lines), daemon=True).start()
        threading.Thread(target=_drain, args=(p.stderr, _stderr_lines), daemon=True).start()

        # --- Session JSONL monitoring ---
        _session_file = None
        _last_session_size = 0
        _last_session_activity = t0  # updated when session file grows
        _last_token_log_time = 0.0
        _last_logged_input_tokens = 0
        _TOKEN_LOG_INTERVAL = 30
        _spinner_chars = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
        _spin_i = 0
        _detected_prompts = 0

        # For resume chunks: record session file size BEFORE this chunk starts,
        # so _read_session_info skips old events from previous chunks.
        _session_start_size = 0
        if session_id:
            try:
                _sf = self._find_latest_session_file(t0, codex_home=codex_home, session_id=session_id)
                if _sf:
                    _session_start_size = _sf.stat().st_size
                    logging.info(f"Pass2 resume: session JSONL start_offset={_session_start_size} bytes")
            except Exception:
                pass

        def _find_session_file():
            try:
                return self._find_latest_session_file(t0, codex_home=codex_home)
            except Exception:
                return None

        def _read_session_info(sf: Path):
            """Read token count and prompt count from session JSONL.
            For resume chunks, reads only bytes after _session_start_size to skip
            agent_message events from previous chunks (avoids double-counting in display)."""
            inp, out, n_prompts = 0, 0, 0
            try:
                if _session_start_size > 0:
                    with open(sf, 'rb') as _f:
                        _f.seek(_session_start_size)
                        _content = _f.read().decode('utf-8', errors='replace')
                else:
                    _content = sf.read_text(encoding='utf-8-sig', errors='replace')
                for line in reversed(_content.strip().split('\n')):
                    try:
                        ev = json.loads(line)
                        if ev.get('type') == 'event_msg':
                            pl = ev.get('payload', {})
                            if pl.get('type') == 'token_count' and inp == 0:
                                u = pl.get('info', {}).get('total_token_usage', {})
                                inp, out = u.get('input_tokens', 0), u.get('output_tokens', 0)
                            elif pl.get('type') == 'agent_message' and n_prompts == 0:
                                msg = pl.get('message', '')
                                if '"prompts"' in msg:
                                    try:
                                        d = json.loads(msg)
                                        n_prompts = len(d.get('prompts', []))
                                    except Exception:
                                        pass
                        if inp > 0 and n_prompts > 0:
                            break
                    except Exception:
                        pass
            except Exception:
                pass
            return inp, out, n_prompts

        # --- Main wait loop ---
        while True:
            elapsed = time.time() - t0
            mins, secs = int(elapsed) // 60, int(elapsed) % 60
            time_str = f"{mins}м {secs:02d}с" if mins else f"{secs}с"

            # Session monitoring
            now = time.time()
            if _session_file is None and now - t0 > 3:
                _session_file = _find_session_file()
            if _session_file and _session_file.exists():
                try:
                    sz = _session_file.stat().st_size
                    if sz != _last_session_size:
                        _last_session_size = sz
                        _last_session_activity = now
                except Exception:
                    pass
            if _session_file and now - _last_token_log_time >= _TOKEN_LOG_INTERVAL:
                inp, out, np = _read_session_info(_session_file)
                if np > 0:
                    _detected_prompts = np
                if inp > 0 and inp != _last_logged_input_tokens:
                    _last_logged_input_tokens = inp
                    print(f"   🔢 GPT токены: {inp:,} input / {out:,} output")
                _last_token_log_time = now

            # Spinner
            if not silent:
                spin = _spinner_chars[_spin_i % len(_spinner_chars)]
                _spin_i += 1
                _display_total = total_clips if total_clips > 0 else n_clips
                _display_done = prompts_offset + _detected_prompts
                extra = f" | {_display_done}/{_display_total} промптов" if _detected_prompts > 0 else ""
                if global_start is not None and global_start != t0:
                    g_el = time.time() - global_start
                    g_mins, g_secs = int(g_el) // 60, int(g_el) % 60
                    g_str = f"{g_mins}м {g_secs:02d}с" if g_mins > 0 else f"{g_secs}с"
                    timer_str = f"чанк {time_str} | итого {g_str}"
                else:
                    timer_str = time_str
                sys.stdout.write(f'\r{spin} GPT Pass2: {timer_str}{extra}   \n')
                sys.stdout.flush()

            if p.poll() is not None:
                break
            if elapsed > self.timeout:
                p.kill()
                logging.warning(f"Pass2 timeout ({self.timeout}s)")
                print(f"   ⏰ Таймаут {self.timeout}s")
                break
            time.sleep(1)

        elapsed = time.time() - t0
        mins, secs = int(elapsed) // 60, int(elapsed) % 60
        if not silent:
            print(f"   ⏱  Codex Pass2: {mins}м {secs:02d}с")

        # --- Parse JSON from result file ---
        prompts_list = []
        _last_codex_message = ''
        if result_file.exists():
            raw = result_file.read_text(encoding='utf-8', errors='replace').strip()
            _last_codex_message = raw
            prompts_list = self._parse_codex_prompts_text(raw)
            if raw and not prompts_list:
                logging.warning("Pass2: result file has no valid [CLIP N] prompts")

        if not prompts_list:
            # Parse stderr for known error patterns and surface them to user
            _is_limit_error = False
            _limit_reset_time = None
            _first_limit_line = None
            _is_high_demand = False
            for _line in _stderr_lines + _stdout_lines:
                _ll = _line.strip()
                if not _ll:
                    continue
                _low = _ll.lower()
                if 'usage limit' in _low or 'rate limit' in _low:
                    if not _is_limit_error:  # dedup: print only once
                        _is_limit_error = True
                        _first_limit_line = _ll
                        _try_again = re.search(r'try again at (.+?)\.?\s*$', _ll, re.IGNORECASE)
                        if _try_again:
                            _limit_reset_time = _try_again.group(1).strip()
                elif 'high demand' in _low or 'temporary errors' in _low or 'reconnecting' in _low:
                    _is_high_demand = True
                elif _ll.startswith('ERROR:') or _ll.startswith('error:'):
                    print(f"   ❌ Codex: {_ll}")
            if _is_limit_error:
                # Dedup: first chunk to detect sets the event and stores reset_time
                _already_reported = rate_limit_reported is not None and rate_limit_reported.is_set()
                if not _already_reported:
                    if rate_limit_reported is not None:
                        rate_limit_reported.set()
                    # Store reset_time for deferred printing AFTER all chunks finish
                    if rate_limit_info is not None and not rate_limit_info:
                        rate_limit_info.append(_limit_reset_time)
                return prompts_offset, session_id, None  # None = fatal, не делать ретраи
            elif _is_high_demand:
                # OpenAI high-demand: Codex already tried 5x reconnects itself.
                # Sleep управляется ретрай-циклом снаружи (_run_one_parallel),
                # чтобы прогрессивный кулдаун не дублировался.
                logging.warning("Pass2: OpenAI high demand — handing back to retry loop")
                print("   ⏰ OpenAI high demand — будем ретраить с прогрессивным кулдауном")
            else:
                logging.warning(f"Pass2: no prompts in result. rc={p.returncode}")
                print(f"   ⚠️  GPT не вернул промпты (rc={p.returncode})")
            # Fallback: check the Codex session JSONL, not just rc=0/result file.
            if _session_file:
                _session_msg = self._extract_last_codex_message(_session_file, _session_start_size)
                if _session_msg:
                    _last_codex_message = _session_msg
                    prompts_list = self._parse_codex_prompts_text(_session_msg)
                    if prompts_list:
                        logging.info(f"Pass2: recovered {len(prompts_list)} prompts from session JSONL")
                        print(f"   ↩️  Восстановлено {len(prompts_list)} промптов из Codex-сессии")

            if not prompts_list and _last_codex_message:
                _raw = _last_codex_message.strip()
                _flat = re.sub(r'\s+', ' ', _raw).strip()
                # Диагностика причины — чтобы юзер понимал ЧТО вернула модель.
                # Нормализуем типографские апострофы (’‘) → ' иначе "can't"/"I'm" не матчатся.
                _low = _flat.lower().replace('’', "'").replace('‘', "'").replace('ʼ', "'")
                _reason = None
                if re.search(r'\{\s*"prompts"\s*:\s*\[\s*\]\s*\}', _flat):
                    _reason = (f"модель вернула ПУСТОЙ список (chunk={n_clips} клипов — "
                               f"возможно слишком большой для одного ответа, уменьшите chunk size)")
                elif any(k in _low for k in ("i'm sorry", "i am sorry", "i can't", "i cannot",
                                              "can't fit", "cannot fit", "unable to", "i won't", "i will not")):
                    _reason = "модель ОТКАЗАЛАСЬ генерировать (отказ/извинение в ответе)"
                elif not _flat.rstrip().endswith(('}', ']', '"')):
                    _reason = "ответ ОБРЫВАЕТСЯ (truncation — превышен лимит токенов вывода, уменьшите chunk size)"
                # Сырой ответ: показываем полнее (до 1500), при обрезке — начало + конец
                if len(_flat) > 1500:
                    _shown = _flat[:1000] + '  …[обрезано в логе]…  ' + _flat[-400:]
                else:
                    _shown = _flat
                if _reason:
                    logging.warning(f"Pass2 Codex без промптов [{_reason}]: {_flat[:800]}")
                    print(f"   🧾 Codex без промптов — ПРИЧИНА: {_reason}")
                    print(f"   📄 Сырой ответ Codex: {_shown}")
                else:
                    logging.warning(f"Pass2 Codex final message without prompts: {_flat[:800]}")
                    print(f"   📄 Сырой ответ Codex (без промптов): {_shown}")

        # DeepSeek fallback: модель иногда создаёт отдельный файл prompts.json вместо
        # возврата JSON-массива через --output-last-message. Проверим рабочую директорию.
        if not prompts_list and self._is_deepseek:
            _prompt_files = list(working_dir.glob('*prompts*.json'))
            _prompt_files = [f for f in _prompt_files if f.is_file() and f.stat().st_size > 0]
            # Предпочесть prompts.json, если есть (он обычно валидный), иначе свежайший
            _exact = working_dir / 'prompts.json'
            if _exact.exists() and _exact.is_file():
                _prompt_files = [_exact] + [f for f in _prompt_files if f != _exact]
            elif _prompt_files:
                _prompt_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
            # Пробуем парсить каждый (первый валидный побеждает), пропускаем битые
            for _pf in _prompt_files:
                try:
                    _data = json.loads(_pf.read_text(encoding='utf-8-sig', errors='replace'))
                    if isinstance(_data, dict) and 'prompts' in _data:
                        _raw_prompts = _data['prompts']
                    elif isinstance(_data, list):
                        _raw_prompts = _data
                    else:
                        continue
                    prompts_list = self._parse_codex_prompts_text(json.dumps({'prompts': _raw_prompts}))
                    if prompts_list:
                        logging.info(f"Pass2 DeepSeek: recovered {len(prompts_list)} prompts from {_pf.name}")
                        print(f"   ↩️  DeepSeek: восстановлено {len(prompts_list)} промптов из {_pf.name}")
                        break
                except Exception as e:
                    logging.warning(f"Pass2 DeepSeek: failed to parse {_pf.name}: {e}")
                    continue

        if not prompts_list:
            return prompts_offset, session_id, set()  # retry

        # Transcript pollution check — detect raw voiceover text echoed back
        # [CLIP N] prefix is expected (we require it), so strip it before checking
        def _is_pollution(p):
            s = re.sub(r'^\[CLIP\s+\d+\]\s*', '', p.strip())
            return s.startswith('VOICEOVER:') or s.startswith('[CLIP ')
        _vo_count = sum(1 for p in prompts_list if _is_pollution(p))
        if _vo_count > len(prompts_list) * 0.3:
            logging.error(f"Pass2: {_vo_count}/{len(prompts_list)} VOICEOVER blocks — pollution")
            print(f"   ❌ Транскрипция вместо промптов ({_vo_count} VOICEOVER блоков)")
            return prompts_offset, session_id, set()

        # Strip [CLIP N] or [N] prefixes that GPT wrote, track which clip IDs were returned
        _id_re = re.compile(r'^\[(?:CLIP\s+)?(\d+)\]\s*')
        _found_ids: set = set()
        _id_pairs: list = []  # [(clip_id_or_None, stripped_prompt), ...]
        for _p in prompts_list:
            _m = _id_re.match(_p.strip())
            if _m:
                _cid = int(_m.group(1))
                _found_ids.add(_cid)
                _id_pairs.append((_cid, _id_re.sub('', _p.strip(), count=1).strip()))
            else:
                _id_pairs.append((None, _p.strip()))

        # Замысел кадра — в план, ДО приклейки стиля и негатива ниже.
        #
        # Стадии-источники (сток, вижн) читают описание сцены из
        # montage_plan (`description` / `visual_intent`), а Pass 2 писал его
        # только в *_prompts_*.txt — файл для ГЕНЕРАТОРОВ. Моста между ними не
        # было, поэтому в стандартном режиме стадии
        # получали сырую озвучку: артефакт прогона 26.08 — `SCENE: brzegi.
        # Ludzie stoją w przejściach…`, и оператор искал несуществующую
        # польскую деревню Бжеги вместо переполненного зала.
        #
        # Это бесплатно: Pass 2 уже пишет промпт КАЖДОМУ клипу, включая внешние.
        # Берём именно сейчас — стиль и негатив
        # приклеиваются следующими строками, а стадиям нужна суть сцены, а не
        # стилевая простыня.
        _publish_visual_intent(prompts_path, _id_pairs)

        # Externalized ref hard-match: дописываем подпись к первому появлению каждого токена (inline)
        if getattr(self, '_ext_refmatch', False):
            _id_pairs = [(_cid, apply_ref_hardmatch(_p)) for _cid, _p in _id_pairs]
        # Externalized style: канонический стиль-блок ПЕРЕД негативом (порядок scene→style→neg)
        if getattr(self, '_ext_style', None):
            _id_pairs = [(_cid, append_style_tail(_p, self._ext_style)) for _cid, _p in _id_pairs]
        # Externalized negative: дописываем канонический негатив+endline к каждому ядру (в конец)
        if getattr(self, '_ext_negative', None):
            _id_pairs = [(_cid, append_negative_tail(_p, self._ext_negative)) for _cid, _p in _id_pairs]
        prompts_list = [_p for _, _p in _id_pairs]

        # Trim to expected count (GPT sometimes returns more than requested)
        if len(prompts_list) > n_clips:
            logging.info(f"Pass2: trimming {len(prompts_list)} → {n_clips} prompts")
            prompts_list = prompts_list[:n_clips]
            _id_pairs = _id_pairs[:n_clips]

        # Append to prompts file
        existing = ''
        if prompts_path.exists() and prompts_path.stat().st_size > 0:
            existing = prompts_path.read_text(encoding='utf-8', errors='replace').rstrip()
        if keep_ids:
            # Write [N] prefixed prompts so parallel merge can sort by clip ID
            new_block = '\n\n'.join(
                f'[{_cid}] {_p}' if _cid is not None else _p
                for _cid, _p in _id_pairs if _p.strip()
            )
        else:
            new_block = '\n\n'.join(p.strip() for p in prompts_list if p.strip())
        if existing:
            combined = existing + '\n\n' + new_block
        else:
            combined = new_block
        prompts_path.write_text(combined, encoding='utf-8')

        total_count = prompts_offset + len(prompts_list)

        # Extract session ID
        _found_session_id = session_id
        if not _found_session_id:
            logging.info(f"Session ID not in result file, searching Codex sessions")
            _found_session_id = self._find_latest_session_id(t0, codex_home=codex_home)
            if not _found_session_id:
                logging.info(f"Could not find session ID from Codex session file")
            else:
                logging.info(f"Found session ID from JSONL: {_found_session_id[:30]}")

        written = len(prompts_list)
        _chars = len(combined)
        _est_tokens = _chars // 3
        _ids_info = f", IDs={sorted(_found_ids)[:5]}{'...' if len(_found_ids) > 5 else ''}" if _found_ids else ""
        print(f"   → Записано: {written}, всего: {total_count}/{prompts_offset + n_clips}{_ids_info}")
        print(f"   📊 Output: {_chars:,} chars ≈ {_est_tokens:,} tokens ({prompts_path.stat().st_size:,} bytes)")
        print(f"   🔑 Output session_id: {_found_session_id[:20] if _found_session_id else 'NONE'}")

        logging.info(f"Codex Pass2 done: {written} new, {total_count} total, ids={len(_found_ids)}, elapsed={elapsed:.1f}s, session={_found_session_id[:30] if _found_session_id else 'none'}")
        return total_count, _found_session_id, _found_ids

    def _find_latest_session_id(self, after_ts: float, codex_home: Optional[Path] = None) -> Optional[str]:
        """Find Codex session ID from JSONL file created after after_ts."""
        try:
            session_dir = self._codex_session_dir(codex_home)
            logging.info(f"[DEBUG] Session dir: {session_dir}")
            logging.info(f"[DEBUG] Dir exists: {session_dir.exists()}")

            if not session_dir.exists():
                logging.info(f"[DEBUG] Session dir does not exist!")
                return None

            all_files = list(session_dir.glob('rollout-*.jsonl'))
            logging.info(f"[DEBUG] Found {len(all_files)} rollout files in dir")

            candidates = [
                f for f in all_files
                if f.stat().st_mtime >= after_ts - 5
            ]
            logging.info(f"[DEBUG] Candidates with mtime>={after_ts-5}: {len(candidates)}")
            for c in candidates:
                logging.info(f"[DEBUG]   - {c.name}: mtime={c.stat().st_mtime}")

            if not candidates:
                logging.info(f"[DEBUG] No candidates found in time range")
                return None

            latest = max(candidates, key=lambda f: f.stat().st_mtime)
            logging.info(f"[DEBUG] Latest file: {latest.name}")

            # Read first line to get session ID
            content = latest.read_text(encoding='utf-8', errors='replace')
            first_line = content.split('\n')[0]
            logging.info(f"[DEBUG] First line length: {len(first_line)}")

            meta = json.loads(first_line)
            sid = meta.get('payload', {}).get('id')
            logging.info(f"[DEBUG] Parsed session ID: {sid}")

            if sid:
                logging.info(f"Found Codex session: {sid}")
            return sid
        except Exception as e:
            logging.warning(f"Could not find Codex session ID: {e}")
            import traceback
            logging.warning(f"[DEBUG] Exception traceback: {traceback.format_exc()}")
            return None

    # =========================================================================
    # PASS 1 — MONTAGE PLAN
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
        """
        Pass 1: транскрипция + стайлгайд → montage_plan.json.
        Один exec вызов.
        """
        mp_path = output_dir / 'montage_plan.json'
        result_file = output_dir / '_codex_pass1_control.json'

        # Загрузить системную инструкцию
        sys_instr_path = self.prompts_dir / 'director_instruction_pass1.txt'
        pause_guide_path = self.prompts_dir / 'director_pause_guide.txt'

        task_prompt = self._build_pass1_prompt(
            transcription_path, style_guide_path, sys_instr_path,
            pause_guide_path, mp_path, project_name, audio_duration
        )

        print(f"\n🎬 GPT Pass 1: создание монтажного плана...")
        print(f"   Модель: {self.model_name}")
        print(f"   Аудио: {audio_duration:.0f}с ({audio_duration/60:.1f} мин)")

        control = self._run_codex(task_prompt, 'pass1_control.json', result_file, output_dir)

        if control.get('status') != 'ok':
            raise RuntimeError(f"GPT Pass 1 failed: {control.get('error_message', 'unknown')}")

        # Прочитать и провалидировать montage_plan.json
        actual_mp_path = Path(control.get('montage_plan_path', str(mp_path)))
        if not actual_mp_path.exists():
            actual_mp_path = mp_path

        if not actual_mp_path.exists():
            raise RuntimeError(f"montage_plan.json не создан: {actual_mp_path}")

        montage_plan = json.loads(actual_mp_path.read_text(encoding='utf-8-sig'))

        # Нормализация обёртки
        if 'montage_plan' in montage_plan and 'clips' in montage_plan['montage_plan']:
            montage_plan = montage_plan['montage_plan']

        # Валидация через существующий валидатор
        is_valid, errors = validate_montage_plan_json(montage_plan, audio_duration)
        if not is_valid:
            logging.warning(f"GPT montage plan validation: {errors}")
            print(f"   ⚠️  Валидация: {len(errors)} замечаний (авто-коррекция)")
        else:
            print(f"   ✅ Валидация монтажного плана — OK")

        # Проверка ID последовательности
        id_problems = check_clip_id_sequence(montage_plan.get('clips', []))
        if id_problems:
            logging.warning(f"GPT montage plan ID issues: {id_problems}")
            print(f"   ⚠️  ID последовательность: {len(id_problems)} проблем (авто-коррекция)")
            # Авто-коррекция: перенумерация 1..N
            for i, clip in enumerate(montage_plan.get('clips', [])):
                clip['id'] = i + 1
                clip['file'] = f"{i + 1}.mp4"

        # Перезаписать нормализованный план
        mp_path.write_text(
            json.dumps(montage_plan, indent=2, ensure_ascii=False),
            encoding='utf-8'
        )
        # «Состав видео» — предпочтение: разметку сводим с долями ДО Pass 2,
        # чтобы промпты писались уже под итоговые метки (см. source_quota.py).
        try:
            from units.source_quota import enforce_source_mix
            if enforce_source_mix(mp_path, transcription).get('changed'):
                montage_plan = json.loads(mp_path.read_text(encoding='utf-8-sig'))
        except Exception as exc:                       # noqa: BLE001
            print(f"   ⚠️  Состав видео не применён: {exc}")
            logging.warning("enforce_source_mix failed: %s", exc, exc_info=True)

        clips = montage_plan.get('clips', [])
        _mp_chars = len(mp_path.read_text(encoding='utf-8'))
        print(f"   ✅ Монтажный план: {len(clips)} клипов, {audio_duration:.0f}с")
        print(f"   📊 Plan output: {_mp_chars:,} chars ≈ {_mp_chars // 3:,} tokens")
        from units.source_ledger import log_source_markup
        log_source_markup(clips)
        return montage_plan, mp_path

    def _build_pass1_prompt(
        self,
        transcription_path: Path,
        style_guide_path: Path,
        sys_instr_path: Path,
        pause_guide_path: Path,
        mp_path: Path,
        project_name: str,
        audio_duration: float,
    ) -> str:
        """Сборка промпта для Pass 1."""
        t_path = transcription_path.as_posix()
        sg_path = style_guide_path.as_posix()
        si_path = sys_instr_path.as_posix()
        pg_path = pause_guide_path.as_posix()
        out_path = mp_path.as_posix()

        clip_min = config.ACTIVE_CLIP_MIN_DURATION
        clip_max = config.ACTIVE_CLIP_MAX_DURATION
        intro_min = config.get_intro_clip_min()
        intro_max = config.get_intro_clip_max()
        intro_block = config.INTRO_BLOCK_DURATION

        # Оживление обложки. Указание добавляется ТОЛЬКО когда обложка реально
        # приложена: нет её — ни строки лишней, контекст не засоряем.
        cover_note = ''
        try:
            from units import cover_opener as cover_hook

            if cover_hook.enabled(mp_path.parent):
                cover_note = '\n\n## ОБЛОЖКА\n' + cover_hook.director_note()
        except Exception as _ce:                            # noqa: BLE001
            logging.debug('cover_hook: указание не собрано (%s)', _ce)

        # Stock markup (если STOCK_ENABLED=true). Текст — из units/stock_tags.py:
        # одна точка правды на оба директора (chat_director зовёт её же).
        from units.stock_tags import stock_markup_directive
        stock_suffix = stock_markup_directive()
        from units.infographic_tags import infographic_markup_directive
        infographic_suffix = infographic_markup_directive()
        from units.mograph_tags import motion_graphics_markup_directive
        mograph_suffix = motion_graphics_markup_directive()

        # Тема ролика — якорь для поиска стоковых материалов (узкий запрос про субъект,
        # не про страну/эпоху/войну). Нужна только если включён сток.
        _theme_needed = getattr(config, 'STOCK_ENABLED', False)
        theme_line = ('    "theme": "<one clear sentence: genre + main subject + setting/period, from the whole video>",\n'
                      if _theme_needed else '')
        theme_rule = ('- "theme": ONE clear, specific sentence giving the video\'s GENRE, its MAIN '
                      'SUBJECT, and the SETTING/PERIOD context — from the WHOLE transcription. '
                      'Be concrete and informative, not vague.\n'
                      if _theme_needed else '')

        return f"""You are a film director creating a montage plan for a documentary.

## SYSTEM INSTRUCTION
Read the director instruction file: {si_path}
Also read the pause guide: {pg_path}

When reading the instruction, apply these substitutions:
- {{clip_min_dur}} = {clip_min}
- {{clip_max_dur}} = {clip_max}
- {{clip_intro_min_dur}} = {intro_min}
- {{clip_intro_max_dur}} = {intro_max}
- {{intro_block_dur}} = {intro_block}{cover_note}

## STYLE GUIDE
Read the style guide file: {sg_path}

## TRANSCRIPTION
Read the transcription file: {t_path}

## PROJECT
Name: {project_name}
Audio duration: {audio_duration:.2f} seconds

## TASK
1. Read ALL files listed above
2. Generate a montage plan following the director instruction rules
3. Write the result as JSON to: {out_path}

The JSON must have this structure:
{{
  "montage_plan": {{
    "audioDuration": {audio_duration:.2f},
{theme_line}    "clips": [
      {{"id": 1, "file": "1.mp4", "startTime": 0.0, "endTime": 7.8}},
      {{"id": 2, "file": "2.mp4", "startTime": 7.8, "endTime": 14.2}},
      ...
    ]
  }}
}}

Rules:
- Each clip duration: {clip_min}–{clip_max} seconds
- Clips must be continuous (no gaps): clip N endTime = clip N+1 startTime
- First clip startTime = 0.0
- Last clip endTime must equal audioDuration ({audio_duration:.2f})
- IDs sequential: 1, 2, 3, ...
- File format: "{{id}}.mp4"
{theme_rule}{stock_suffix}{infographic_suffix}{mograph_suffix}
Write the JSON file and then report your status in your final response."""

    # =========================================================================
    # PASS 2 — PROMPTS
    # =========================================================================

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
        """
        Pass 2: montage_plan + стайлгайд → промпты per clip.
        Возвращает (prompts_path, num_prompts).
        """
        # Externalize negative: если включено и стайлгайд имеет NEGATIVE-секцию —
        # модель НЕ пишет негатив (экономия вывода), скрипт допишет его постобработкой.
        # Externalize style + negative через resolve_style_blocks (нейро/regex/off + кэш).
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

        total_clips = len(clips)
        if total_clips == 0:
            print("   ❌ Монтажный план пуст — нечего промптировать")
            return None, 0

        chunk_size = config.GPT_PASS2_CHUNK_SIZE or total_clips

        timestamp = datetime.now().strftime('%Y%m%d_%H%M')
        prompts_path = output_dir / f'{project_name}_prompts_{timestamp}.txt'
        total_prompts = 0
        cursor = 0
        chunk_idx = 0
        _chunk_retries = 0
        _MAX_CHUNK_RETRIES = 3

        # Load transcription fragments for clip cards
        transcription = json.loads(transcription_path.read_text(encoding='utf-8-sig'))
        fragments = transcription.get('transcription', [])

        # Resume: подсчитать существующие промпты
        if resume_path:
            rp = Path(resume_path)
            if rp.exists():
                prompts_path = rp
                existing = rp.read_text(encoding='utf-8').strip().split('\n\n')
                total_prompts = len([p for p in existing if p.strip()])
                cursor = total_prompts
                print(f"   ↩️  Resume: {total_prompts} промптов уже есть, продолжаем с клипа {cursor + 1}")

        _pass2_session_id = None  # Codex session ID for resume between chunks
        _pass2_total_start = time.time()

        # --- Параллельный режим Pass 2 (GPT_PASS2_PARALLEL > 1) ---
        _parallel = getattr(config, 'GPT_PASS2_PARALLEL', 1)
        if _parallel > 1 and total_clips > chunk_size:
            # Предварительная сборка всех чанков
            _initial_prev_sample = ""
            if prompts_path.exists() and total_prompts > 0:
                _ex = prompts_path.read_text(encoding='utf-8').strip().split('\n\n')
                _initial_prev_sample = '\n\n'.join([p for p in _ex if p.strip()][-5:])

            # Фиксированные пачки по chunk_size
            _chunks_lists = []
            _cur_pos = cursor
            while _cur_pos < total_clips:
                _end_pos = min(_cur_pos + chunk_size, total_clips)
                _chunks_lists.append(clips[_cur_pos:_end_pos])
                _cur_pos = _end_pos

            _chunks_to_run = []
            for _cclips in _chunks_lists:
                _fid = _cclips[0]['id']
                _tid = _cclips[-1]['id']
                _nc = len(_cclips)
                # Глобальный индекс начала этого чанка в clips (для prev plot context)
                _clip_start = clips.index(_cclips[0])
                _cards = [_build_clip_card(_c, fragments) for _c in _cclips]
                _pp = ""
                if _clip_start > 0:
                    _pp_lines = []
                    for _pc in clips[max(0, _clip_start - 5):_clip_start]:
                        _cs, _cend = _pc.get('startTime', 0), _pc.get('endTime', 0)
                        _txts = [f['text'] for f in fragments if f.get('start', 0) < _cend and f.get('end', 0) > _cs]
                        _pp_lines.append(f"Clip {_pc['id']} ({_cs:.1f}-{_cend:.1f}s): {' '.join(_txts).strip()}")
                    _pp = '\n'.join(_pp_lines)
                _tp = self._build_pass2_prompt(
                    style_guide_path, prompts_path,
                    '\n\n'.join(_cards), _fid, _tid, _nc, total_clips,
                    _initial_prev_sample, _pp, total_prompts > 0 or _clip_start > 0,
                )
                _chunks_to_run.append({
                    'idx': len(_chunks_to_run), 'from_id': _fid, 'to_id': _tid,
                    'n_clips': _nc, 'task_prompt': _tp, 'clips': _cclips,
                    'clip_start': _clip_start,
                })

            _nch = len(_chunks_to_run)
            # GPT_PASS2_PARALLEL — реальный лимитер воркеров (раньше был просто on/off флагом).
            # max(1, ...) защита от 0/отрицательных. min(_parallel, _nch) — нет смысла иметь
            # больше воркеров чем чанков (ThreadPoolExecutor сам ограничит, но явный clamp удобнее).
            _max_par = max(1, min(_parallel, _nch))
            print(f"\n🚀 Pass 2 параллельный режим ({_max_par} воркеров на {_nch} чанков)")

            _MAX_PAR_RETRIES = 10
            _RETRY_COOLDOWN_BASE = 90    # стартовый кулдаун (сек) перед ретраем
            _RETRY_COOLDOWN_STEP = 60    # +Nсек каждый следующий ретрай (90/150/210/.../630)
            _rate_limit_evt = threading.Event()   # shared across all parallel chunks
            _rate_limit_info = []                  # [reset_time_str|None] — filled by first chunk

            def _run_one_parallel(cd):
                import shutil as _shutil
                _i = cd['idx']
                _ich = len(cd['task_prompt'])
                print(f"\n✍️  GPT Pass 2: генерация промптов (клипы {cd['from_id']}–{cd['to_id']})...")
                print(f"   📊 Input: {_ich:,} chars ≈ {_ich // 3:,} tokens")
                print(f"   🔑 Input session_id: NONE (fresh exec, parallel {_i + 1}/{_nch})")
                # Имя проекта в название НЕ вставляем: оно уже есть в пути папки,
                # а второй раз внутри имени файла путь перерастал 260 символов
                # Windows и файл «не находился» (Errno 2) — на длинном заголовке
                # ролика падал весь Pass 2 (жалоба 29.08, путь 272 символа).
                # Финализация читает чанки по объектам путей, не по именам, так
                # что от имени требуется только номер чанка.
                _tmp = output_dir / f'_tmp_p2_{_i}.txt'

                def _run_chunk(task_p, n_clips, offset):
                    _ch = Path(tempfile.mkdtemp(prefix=f'codex_p2_{_i}_'))
                    if self._uses_moonbridge:
                        # DeepSeek: копируем config.toml + models_catalog из изолированного home
                        _codex_src = Path(config.DEEPSEEK_CODEX_HOME)
                        _copy_files = ['config.toml', 'models_catalog.json']
                    else:
                        _codex_src = Path.home() / '.codex'
                        _copy_files = ['auth.json', 'config.toml']
                    for _cf in _copy_files:
                        _src = _codex_src / _cf
                        if _src.exists():
                            _shutil.copy2(str(_src), str(_ch / _cf))
                    try:
                        return self._run_codex_pass2(
                            task_p, _tmp, n_clips, output_dir,
                            prompts_offset=offset, session_id=None,
                            total_clips=total_clips, global_start=_pass2_total_start,
                            codex_home=_ch, silent=True, keep_ids=True,
                            rate_limit_reported=_rate_limit_evt,
                            rate_limit_info=_rate_limit_info,
                        )
                    finally:
                        _shutil.rmtree(str(_ch), ignore_errors=True)

                # Copy raw Codex session to project raw_gpt/ after each chunk run
                def _copy_raw_session(chunk_idx, attempt=0):
                    try:
                        _raw_dir = output_dir / 'raw_gpt'
                        _raw_dir.mkdir(exist_ok=True)
                        _today = datetime.now()
                        _ch_root = Path(config.DEEPSEEK_CODEX_HOME) if self._is_deepseek else (Path.home() / '.codex')
                        _sess_dir = _ch_root / 'sessions' / str(_today.year) / f'{_today.month:02d}' / f'{_today.day:02d}'
                        if _sess_dir.exists():
                            _candidates = sorted([f for f in _sess_dir.glob('rollout-*.jsonl')
                                                  if f.stat().st_mtime >= _pass2_total_start - 5],
                                                 key=lambda f: f.stat().st_mtime)
                            if _candidates:
                                _sf = _candidates[-1]
                                _label = f'pass2_par{chunk_idx}_att{attempt}' if attempt else f'pass2_par{chunk_idx}'
                                _dst = _raw_dir / f'{_label}_{_sf.name}'
                                _shutil.copy2(str(_sf), str(_dst))
                                logging.info(f"Raw GPT session copied: {_dst.name}")
                    except Exception as _e:
                        logging.warning(f"Could not copy raw session: {_e}")

                # Initial run — track which clip IDs were returned
                _ft, _, _found_ids = _run_chunk(cd['task_prompt'], cd['n_clips'], 0)
                _copy_raw_session(_i)
                _expected_ids = {c['id'] for c in cd['clips']}
                _retries = 0

                # None = fatal error (rate limit etc) — не ретраим
                if _found_ids is None:
                    return _i, 0, _tmp

                # Retry loop: 0 prompts → retry whole chunk; partial → retry EXACT missing clips
                while _found_ids < _expected_ids and _retries < _MAX_PAR_RETRIES:
                    _retries += 1
                    _missing_ids = _expected_ids - _found_ids
                    _missing = len(_missing_ids)

                    _cooldown = _RETRY_COOLDOWN_BASE + (_retries - 1) * _RETRY_COOLDOWN_STEP
                    if not _found_ids:
                        # Complete failure — retry whole chunk
                        print(f"   🔄 Чанк {_i+1}: 0 промптов — ретрай {_retries}/{_MAX_PAR_RETRIES} (cooldown {_cooldown}s)...")
                        time.sleep(_cooldown)
                        _ft, _, _new_ids = _run_chunk(cd['task_prompt'], cd['n_clips'], 0)
                        if _new_ids is None:
                            break  # rate limit — стоп
                        _found_ids |= _new_ids
                    else:
                        # Partial: build prompt for EXACT missing clips (by ID, not positional)
                        _rem_clips = [c for c in cd['clips'] if c['id'] in _missing_ids]
                        _rem_start = cd['clip_start'] + len(_found_ids)
                        print(f"   🔄 Чанк {_i+1}: {len(_found_ids)}/{cd['n_clips']} — досылаем {_missing} клипов (IDs: {sorted(_missing_ids)[:5]}{'...' if _missing > 5 else ''}) ретрай {_retries}/{_MAX_PAR_RETRIES} (cooldown {_cooldown}s)...")
                        time.sleep(_cooldown)
                        _cards = [_build_clip_card(_c, fragments) for _c in _rem_clips]
                        _pp_lines = []
                        for _pc in clips[max(0, _rem_start - 5):_rem_start]:
                            _cs2, _cend2 = _pc.get('startTime', 0), _pc.get('endTime', 0)
                            _txts2 = [f['text'] for f in fragments if f.get('start', 0) < _cend2 and f.get('end', 0) > _cs2]
                            _pp_lines.append(f"Clip {_pc['id']} ({_cs2:.1f}-{_cend2:.1f}s): {' '.join(_txts2).strip()}")
                        _rem_tp = self._build_pass2_prompt(
                            style_guide_path, prompts_path,
                            '\n\n'.join(_cards), _rem_clips[0]['id'], _rem_clips[-1]['id'],
                            _missing, total_clips,
                            '', '\n'.join(_pp_lines), True,
                        )
                        _new_ft, _, _new_ids = _run_chunk(_rem_tp, _missing, len(_found_ids))
                        if _new_ids is None:
                            break  # rate limit — стоп
                        _found_ids |= _new_ids

                _ft = len(_found_ids)
                if _ft < cd['n_clips']:
                    print(f"   ⚠️  Чанк {_i+1}: {_ft}/{cd['n_clips']} после {_MAX_PAR_RETRIES} ретраев (пропущены IDs: {sorted(_expected_ids - _found_ids)})")

                # Reorder _tmp by clip order (retries may have appended out-of-order)
                # Parse [N] prefixed prompts, sort by cd['clips'] order, strip IDs
                if _tmp.exists() and _ft > 0:
                    _id_re2 = re.compile(r'^\[(\d+)\]\s*')
                    _id_map2 = {}
                    for _blk in _tmp.read_text(encoding='utf-8').split('\n\n'):
                        _blk = _blk.strip()
                        if not _blk:
                            continue
                        _m2 = _id_re2.match(_blk)
                        if _m2:
                            _id_map2[int(_m2.group(1))] = _id_re2.sub('', _blk, count=1).strip()
                        else:
                            pass  # no ID — skip (shouldn't happen in keep_ids mode)
                    _ordered = [_id_map2[c['id']] for c in cd['clips'] if c['id'] in _id_map2]
                    _tmp.write_text('\n\n'.join(_ordered), encoding='utf-8')
                    _ft = len(_ordered)

                return _i, _ft, _tmp

            _par_done = [0]
            _par_stop = [False]
            _spin_chars = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'

            def _bottom_spin():
                _si = 0
                while not _par_stop[0]:
                    el = time.time() - _pass2_total_start
                    mins_, secs_ = int(el) // 60, int(el) % 60
                    t_str = f"{mins_}м {secs_:02d}с" if mins_ > 0 else f"{secs_}с"
                    spin_ = _spin_chars[_si % len(_spin_chars)]
                    _si += 1
                    sys.stdout.write(f'\r{spin_} GPT Pass2 [{_par_done[0]}/{_nch} чанков]: итого {t_str}   \n')
                    sys.stdout.flush()
                    time.sleep(1)

            _spin_t = threading.Thread(target=_bottom_spin, daemon=True)
            _spin_t.start()

            # --- Resume: проверяем существующие _tmp_p2_{idx}_*.txt чанки от прошлых прогонов ---
            # Если в папке проекта уже есть полный чанк (все expected clip_ids покрыты) —
            # переиспользуем его, не дёргаем GPT повторно.
            _id_re_resume = re.compile(r'^\[(\d+)\]\s*')

            def _try_resume_chunk(cd):
                _expected = {c['id'] for c in cd['clips']}
                # Новое короткое имя + старый формат с именем проекта (чанки от
                # прогонов до 29.08 должны подхватываться, иначе resume их не
                # увидит и пользователь заплатит за повторную генерацию).
                # Шаблон именно `{idx}_*`, а не `{idx}*`: последний поймал бы
                # `_tmp_p2_10.txt` для чанка 1.
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
                        _blk = _blk.strip()
                        if not _blk:
                            continue
                        _m = _id_re_resume.match(_blk)
                        if _m:
                            _found.add(int(_m.group(1)))
                    if _expected.issubset(_found):
                        return _cand
                return None

            _par_results = []
            _chunks_to_submit = []
            for cd in _chunks_to_run:
                _resume_path = _try_resume_chunk(cd)
                if _resume_path is not None:
                    _n_resumed = len({c['id'] for c in cd['clips']})
                    print(f"   ♻️  Чанк {cd['idx']+1}: resume из {_resume_path.name} ({_n_resumed} промптов уже есть)")
                    # Приводим к короткому имени: чанк от старого прогона мог
                    # называться по имени проекта, а такой путь на длинных
                    # заголовках не открывается (см. выше).
                    _new_tmp = output_dir / f"_tmp_p2_{cd['idx']}.txt"
                    if _resume_path.resolve() != _new_tmp.resolve():
                        try:
                            if _new_tmp.exists():
                                _new_tmp.unlink()
                            _resume_path.rename(_new_tmp)
                        except Exception:
                            import shutil as _sh
                            _sh.copy2(str(_resume_path), str(_new_tmp))
                    _par_results.append((cd['idx'], _n_resumed, _new_tmp))
                    _par_done[0] += 1
                else:
                    _chunks_to_submit.append(cd)

            if _chunks_to_submit:
                with ThreadPoolExecutor(max_workers=_max_par) as _pool:
                    _futs = {_pool.submit(_run_one_parallel, cd): cd['idx'] for cd in _chunks_to_submit}
                    for _fut in as_completed(_futs):
                        _ri, _rft, _rtp = _fut.result()
                        _par_results.append((_ri, _rft, _rtp))
                        _par_done[0] += 1
                        print(f"   ✅ Чанк {_ri + 1}/{_nch} завершён: {_rft} промптов")
            else:
                print(f"   ♻️  Все {_nch} чанков восстановлены из tmp файлов — GPT вызовы пропущены")

            _par_stop[0] = True
            _spin_t.join(timeout=2)

            # Print rate limit message AFTER all chunk completions (deferred from threads)
            if _rate_limit_evt.is_set():
                _reset_t = _rate_limit_info[0] if _rate_limit_info else None
                if _reset_t:
                    print(f"   ⏳ Лимит GPT вашего тарифа исчерпан — лимит обновится в {_reset_t}. Дождитесь и запустите заново")
                else:
                    print(f"   ⏳ Лимит GPT вашего тарифа исчерпан. Дождитесь обновления и запустите заново")
                logging.warning(f"Pass2: rate/usage limit hit, reset_time={_reset_t}")

            _par_results.sort(key=lambda x: x[0])
            _all_prompts = []
            _tmp_paths_to_clean = []  # удалим только если итог 100% успех
            for _ri, _rft, _rtp in _par_results:
                if Path(_rtp).exists():
                    _rc = Path(_rtp).read_text(encoding='utf-8').strip()
                    _all_prompts.extend([p.strip() for p in _rc.split('\n\n') if p.strip()])
                    _tmp_paths_to_clean.append(Path(_rtp))
            prompts_path.write_text('\n\n'.join(_all_prompts), encoding='utf-8')
            total_prompts = len(_all_prompts)
            # ВАЖНО: fail-stop если хоть один промпт не получен после всех ретраев.
            # Tmp файлы НЕ удаляем — они нужны для resume при следующем запуске.
            if total_prompts < total_clips:
                _missing_cnt = total_clips - total_prompts
                logging.error(
                    f"Pass2 incomplete: {total_prompts}/{total_clips} prompts "
                    f"({_missing_cnt} missing after {_MAX_PAR_RETRIES} retries per chunk). "
                    f"Tmp файлы оставлены для resume."
                )
                raise RuntimeError(
                    f"Pass 2 не дополучил {_missing_cnt} промптов из {total_clips} "
                    f"даже после {_MAX_PAR_RETRIES} ретраев на чанк. "
                    f"Tmp чанки сохранены — при перезапуске Direction подхватятся (resume). "
                    f"Пайплайн остановлен."
                )
            # Успех — чистим tmp файлы только теперь
            for _tp in _tmp_paths_to_clean:
                try:
                    _tp.unlink()
                except Exception:
                    pass
            cursor = total_clips  # пропускаем sequential while loop ниже

        # --- Sequential fallback (GPT_PASS2_PARALLEL=1 или один чанк) ---
        while cursor < total_clips:
            chunk_end = min(cursor + chunk_size, total_clips)
            chunk_clips = clips[cursor:chunk_end]
            from_id = chunk_clips[0]['id']
            to_id = chunk_clips[-1]['id']
            n_clips = len(chunk_clips)

            # Build clip cards with voiceover text
            clip_cards = []
            for clip in chunk_clips:
                cs = clip.get('startTime', 0)
                ce = clip.get('endTime', 0)
                texts = [f['text'] for f in fragments
                         if f.get('start', 0) < ce and f.get('end', 0) > cs]
                text = ' '.join(texts).strip() or '(pause)'
                source_line = '\nSOURCE: INFOGRAPHIC (make a static information graphic, not a cinematic scene)' \
                    if clip.get('source') == 'infographic' else ''
                clip_cards.append(
                    f"[CLIP {clip['id']}] {cs:.1f}–{ce:.1f}s\n"
                    f"{source_line}\nVOICEOVER: {text}"
                )
            clip_block = '\n\n'.join(clip_cards)

            # Previous plot context (last 5 clips before this chunk)
            prev_plot = ""
            if cursor > 0:
                prev_start = max(0, cursor - 5)
                plot_lines = []
                for c in clips[prev_start:cursor]:
                    cs, ce = c.get('startTime', 0), c.get('endTime', 0)
                    texts = [f['text'] for f in fragments
                             if f.get('start', 0) < ce and f.get('end', 0) > cs]
                    plot_lines.append(f"Clip {c['id']} ({cs:.1f}-{ce:.1f}s): {' '.join(texts).strip()}")
                prev_plot = '\n'.join(plot_lines)

            # Контекст предыдущих промптов
            prev_sample = ""
            if prompts_path.exists() and total_prompts > 0:
                existing = prompts_path.read_text(encoding='utf-8').strip().split('\n\n')
                prev_sample = '\n\n'.join(existing[-5:])

            # Always full prompt — no session resume (testing: fresh exec per chunk)
            task_prompt = self._build_pass2_prompt(
                style_guide_path, prompts_path,
                clip_block, from_id, to_id, n_clips, total_clips,
                prev_sample, prev_plot, total_prompts > 0
            )

            chunk_label = f" (клипы {from_id}–{to_id})" if total_clips > chunk_size else ""
            print(f"\n✍️  GPT Pass 2: генерация промптов{chunk_label}...")
            _input_chars = len(task_prompt)
            print(f"   📊 Input: {_input_chars:,} chars ≈ {_input_chars // 3:,} tokens")
            print(f"   🔑 Input session_id: NONE (fresh exec, no resume)")
            logging.info(f"Pass2 chunk {chunk_idx+1}: clips {from_id}-{to_id}, input_session=NONE (no-resume mode)")

            # Бэкап существующего контента — защита от перезаписи Codex
            _backup_content = None
            if total_prompts > 0 and prompts_path.exists():
                _backup_content = prompts_path.read_text(encoding='utf-8')

            file_total, _session_id, _ = self._run_codex_pass2(
                task_prompt, prompts_path, n_clips, output_dir,
                prompts_offset=total_prompts,
                session_id=None,  # no resume — fresh exec every chunk
                total_clips=total_clips,
                global_start=_pass2_total_start,
                keep_ids=True,
            )
            # Log session created (for debug only — not reused)
            if _session_id:
                logging.info(f"Pass2 chunk {chunk_idx+1} session: {_session_id[:30]} (not reused)")

            # Copy raw Codex session JSONL to project raw_gpt/ folder
            if _session_id:
                try:
                    _raw_dir = output_dir / 'raw_gpt'
                    _raw_dir.mkdir(exist_ok=True)
                    _today = datetime.now()
                    _ch_root = Path(config.DEEPSEEK_CODEX_HOME) if self._is_deepseek else (Path.home() / '.codex')
                    _sess_dir = _ch_root / 'sessions' / str(_today.year) / f'{_today.month:02d}' / f'{_today.day:02d}'
                    _sess_files = list(_sess_dir.glob(f'*{_session_id[:16]}*.jsonl')) if _sess_dir.exists() else []
                    if not _sess_files:
                        _sess_files = sorted([f for f in _sess_dir.glob('rollout-*.jsonl') if f.stat().st_mtime >= _pass2_total_start - 5], key=lambda f: f.stat().st_mtime)
                        _sess_files = _sess_files[-1:] if _sess_files else []
                    for _sf in _sess_files:
                        _dst = _raw_dir / f'pass2_chunk{chunk_idx}_{_sf.name}'
                        shutil.copy2(str(_sf), str(_dst))
                        logging.info(f"Raw GPT session copied: {_dst.name}")
                        print(f"   📁 Raw session → raw_gpt/{_dst.name}")
                except Exception as _e:
                    logging.warning(f"Could not copy raw session: {_e}")

            # Проверка: Codex не перезаписал предыдущие промпты
            if _backup_content is not None and prompts_path.exists():
                current = prompts_path.read_text(encoding='utf-8')
                _backup_start = _backup_content[:120].strip()
                if _backup_start and not current.startswith(_backup_start[:80]):
                    # Codex перезаписал файл — восстанавливаем
                    merged = _backup_content.rstrip() + '\n\n' + current.strip()
                    prompts_path.write_text(merged, encoding='utf-8')
                    file_total = len([p for p in merged.split('\n\n') if p.strip()])
                    logging.warning(f"Pass2: Codex overwrote file! Restored {total_prompts} prev + appended new → {file_total} total")
                    print(f"   ⚠️  Codex перезаписал файл — восстановлено {file_total} промптов")

            written = file_total - total_prompts  # добавлено в этом чанке
            chunk_idx += 1

            # Output stats (cap display at total_clips to avoid confusion)
            _disp_total = min(file_total, total_clips)
            _disp_written = min(written, n_clips)
            if prompts_path.exists():
                _fsize = prompts_path.stat().st_size
                _chars = len(prompts_path.read_text(encoding='utf-8'))
                _est_tokens = _chars // 3
                print(f"   → Записано: {_disp_written}, всего: {_disp_total}/{total_clips}")
                print(f"   📊 Output: {_chars:,} chars ≈ {_est_tokens:,} tokens ({_fsize:,} bytes)")
            else:
                print(f"   → Записано: {_disp_written}, всего: {_disp_total}/{total_clips}")

            total_prompts = file_total

            if written < n_clips:
                if written == 0:
                    _chunk_retries += 1
                    if _chunk_retries > _MAX_CHUNK_RETRIES:
                        logging.warning(f"Pass2: no progress after {_MAX_CHUNK_RETRIES} retries ({total_prompts}/{total_clips}) — stopping")
                        print(f"   ❌ {_MAX_CHUNK_RETRIES} попыток без прогресса — останавливаюсь")
                        break
                    logging.warning(f"Pass2: no progress ({total_prompts}/{total_clips}) — retry {_chunk_retries}/{_MAX_CHUNK_RETRIES}")
                    print(f"   🔄 0 промптов — ретрай чанка ({_chunk_retries}/{_MAX_CHUNK_RETRIES})...")
                    time.sleep(5)
                    continue
                # Частичная запись (таймаут?) — продолжаем со следующего клипа
                logging.warning(f"Pass2 partial chunk: {written}/{n_clips}, resuming from clip {total_prompts + 1}")
                print(f"   ⚡ Частичная запись {written}/{n_clips} — продолжаем с клипа {total_prompts + 1}")
                cursor = total_prompts
                continue

            _chunk_retries = 0  # успешный чанк — сброс ретраев
            cursor = chunk_end

            if chunk_end >= total_clips:
                break

        # Финальная валидация
        if prompts_path.exists():
            actual = prompts_path.read_text(encoding='utf-8').strip().split('\n\n')
            actual_count = len([p for p in actual if p.strip()])
            if actual_count != total_prompts:
                logging.warning(f"Prompts count mismatch: control={total_prompts}, file={actual_count}")
                total_prompts = actual_count

            # Trim если больше чем клипов
            if total_prompts > total_clips:
                logging.warning(f"Overprompt: {total_prompts}>{total_clips}, trimming")
                all_p = [p for p in actual if p.strip()]
                prompts_path.write_text(
                    '\n\n'.join(all_p[:total_clips]),
                    encoding='utf-8'
                )
                total_prompts = total_clips

        _pass2_elapsed = time.time() - _pass2_total_start
        _p2m, _p2s = int(_pass2_elapsed) // 60, int(_pass2_elapsed) % 60
        print(f"\n✅ GPT Pass 2 завершён: {total_prompts}/{total_clips} промптов ({_p2m}м {_p2s:02d}с)")
        return prompts_path if total_prompts > 0 else None, total_prompts

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
        """Сборка промпта для Pass 2 с clip cards."""
        # Read style guide inline — avoids Codex file read tool calls
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

        # Load template from external file (editable via GUI Prompts tab)
        _tpl_path = self.prompts_dir / 'gpt_director_pass2.txt'
        try:
            _tpl = _tpl_path.read_text(encoding='utf-8')
        except Exception:
            logging.error(f"Could not read GPT Pass 2 template: {_tpl_path}")
            raise RuntimeError(f"GPT Pass 2 template not found: {_tpl_path}")

        # Use manual replacement instead of .format() to avoid issues
        # with curly braces in style guide / clip block content
        result = _tpl
        result = result.replace('{style_guide}', _sg_text)
        result = result.replace('{plot_section}', plot_section)
        result = result.replace('{prev_section}', prev_section)
        result = result.replace('{n_clips}', str(n_clips))
        result = result.replace('{from_id}', str(from_id))
        result = result.replace('{to_id}', str(to_id))
        result = result.replace('{total_clips}', str(total_clips))
        result = result.replace('{clip_block}', clip_block)
        # {chain_hint} — место подсказки режима цепочки (его в программе нет): пустая строка,
        # чтобы метка не попала в промпт из изменённых пользователем шаблонов
        result = result.replace('{chain_hint}', '')
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


# =========================================================================
# Wrapper function — аналог direct_project() для GptDirector
# =========================================================================

