"""
Style Extractor — НЕЙРО-выделение повторяющихся блоков из стайлгайда.

В отличие от питон-регекса (gpt_director.extract_style_block / extract_negative_block),
который ищет блоки по СТРОГИМ ЯКОРЯМ ("...VERBATIM..." + ```code```), здесь модель
СЕМАНТИЧЕСКИ определяет, какой блок СТИЛЯ и какой блок НЕГАТИВА должны одинаково
повторяться в каждом промпте сцены — без опоры на фразы-якоря. Работает на любом
стайлгайде, где у разных юзеров блоки названы/оформлены по-разному.

Один дешёвый вызов на проект. Результат кэшируется в style_blocks.json.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional, Dict

import params as config
_EXTRACT_PROMPT = """You are given a VISUAL STYLE GUIDE for an AI image/video pipeline.
The guide instructs a director to write ONE prompt per scene. In each generated prompt,
some content is UNIQUE per scene (camera angle, action, subjects, chosen light, mood)
and some content is a FIXED block that is appended IDENTICALLY (verbatim) to EVERY prompt:
the visual STYLE block and the NEGATIVE-prompt block.

Your task: read the style guide and determine, BY MEANING (not by looking for any marker
phrase), the two fixed blocks that should be appended unchanged to the end of EVERY scene
prompt:
1. style_block — the recurring visual style / render / palette / film-look description
   that every prompt shares.
2. negative_block — the recurring negative prompt (things to avoid / forbidden), including
   any fixed ending clause that always closes the prompt.

Rules:
- Return the text EXACTLY as it must be appended (verbatim, ready to concatenate).
- Do NOT invent or summarize — lift the actual recurring wording from the guide.
- Do NOT include the per-scene parts (camera/action/subjects/light) — only the fixed blocks.
- If a block genuinely does not exist in this guide, return an empty string for it.

Return STRICT JSON only, no markdown, no commentary:
{"style_block": "<verbatim style text or empty>", "negative_block": "<verbatim negative text or empty>"}

STYLE GUIDE:
---
%s
---
Return ONLY the JSON object."""


def _parse(text: str) -> Dict[str, str]:
    text = (text or '').strip()
    if not text:
        return {}
    def _norm(d):
        if not isinstance(d, dict):
            return {}
        return {
            'style_block': str(d.get('style_block', '') or '').strip(),
            'negative_block': str(d.get('negative_block', '') or '').strip(),
        }
    try:
        return _norm(json.loads(text))
    except json.JSONDecodeError:
        pass
    s, e = text.find('{'), text.rfind('}')
    if s >= 0 and e > s:
        try:
            return _norm(json.loads(text[s:e + 1]))
        except json.JSONDecodeError:
            pass
    return {}


def extract_style_blocks_via_llm(
    style_guide: str,
    backend: str,
    model_name: Optional[str] = None,
    work_dir: Optional[Path] = None,
) -> Dict[str, str]:
    """Семантически выделяет {style_block, negative_block} из стайлгайда через LLM.

    backend:
      chat (inline)   — 'deepseek' | 'openrouter'
      прямой API      — 'claude_api'/'anthropic'
      CLI-агенты      — 'codex'/'gpt' | 'claude_code' (по ПОДПИСКЕ, токены не тарифицируются;
                        требуют work_dir — пишут task/result файлы)
    Возвращает {} при ошибке/неподдерживаемом backend → fallback на regex.
    """
    if not style_guide:
        return {}
    backend = (backend or '').strip().lower()
    prompt = _EXTRACT_PROMPT % style_guide

    try:
        if backend in (
            'deepseek', 'openrouter', 'meta_muse',
            'codex_reseller', 'gpt_nexus',
        ):
            from units import llm_gateway as chat_client
            if backend == 'deepseek':
                default_model = config.DEEPSEEK_MODEL
            elif backend == 'openrouter':
                default_model = config.OPENROUTER_MODEL
            elif backend == 'codex_reseller':
                default_model = getattr(config, 'CODEX_RESELLER_MODEL', '') or 'gpt-5.6-terra'
            elif backend == 'meta_muse':
                default_model = (
                    getattr(config, 'META_MUSE_MODEL', '')
                    or 'muse-spark-1.2-contributor'
                )
            else:
                default_model = getattr(config, 'GPT_NEXUS_MODEL', '') or 'gpt-5.5'
            model = model_name or default_model
            raw = chat_client.chat_completion(
                backend, model, [{"role": "user", "content": prompt}],
                max_tokens=8000, reasoning_level='off', web=False,
                json_mode=True, timeout=180,
            )
            return _parse(raw)

        if backend in ('claude_api', 'anthropic'):
            from units.nexus_runner import ClaudeApiRunner
            runner = ClaudeApiRunner(model_name=model_name)
            out = runner.complete(prompt, label='Style Extract', temperature=0.1,
                                  max_tokens=8000)
            text = out[0] if isinstance(out, (tuple, list)) else out
            return _parse(text)

        # CLI-агенты по ПОДПИСКЕ (Codex / Claude Code): токены не тарифицируются, поэтому
        # использовать их для выделения — нормально. Нужен work_dir (агент пишет файлы).
        if backend in ('codex', 'gpt'):
            if work_dir is None:
                logging.info("Style extract: codex требует work_dir — пропуск")
                return {}
            return _extract_via_codex(prompt, model_name, Path(work_dir))

        if backend == 'claude_code':
            if work_dir is None:
                logging.info("Style extract: claude_code требует work_dir — пропуск")
                return {}
            return _extract_via_claude_code(prompt, model_name, Path(work_dir))

    except Exception as e:
        logging.warning(f"Style extract via {backend} failed: {e}")
        return {}

    logging.info(f"Style extract: backend '{backend}' не поддержан для нейро-выделения")
    return {}


def _extract_via_codex(prompt: str, model_name: Optional[str], work_dir: Path) -> Dict[str, str]:
    """Codex CLI (подписка): агент пишет style_extract.json, читаем его."""
    from units.archive_photos import CodexRunner  # единственное определение CodexRunner
    work_dir.mkdir(parents=True, exist_ok=True)
    out_file = work_dir / 'style_extract.json'
    if out_file.exists():
        out_file.unlink()
    task = (f"{prompt}\n\n---\n"
            f"Write the resulting JSON object to this exact file: {out_file.as_posix()}\n"
            f"Then output the control JSON with output_path set to that absolute path.")
    ctrl_file = work_dir / '_codex_style_extract_control.json'
    runner = CodexRunner(model_name=model_name or getattr(config, 'REAL_PHOTO_CODEX_MODEL', 'gpt-5.5'))
    runner.run(task_prompt=task, schema_file='style_extract_control.json',
               result_file=ctrl_file, working_dir=work_dir)
    if not out_file.exists():
        return {}
    return _parse(out_file.read_text(encoding='utf-8', errors='replace'))


def _extract_via_claude_code(prompt: str, model_name: Optional[str], work_dir: Path) -> Dict[str, str]:
    """Claude Code CLI (подписка): агент пишет style_extract.json, читаем его."""
    from units.claudecli_runner import ClaudeCodeRunner
    work_dir.mkdir(parents=True, exist_ok=True)
    out_file = work_dir / 'style_extract.json'
    if out_file.exists():
        out_file.unlink()
    task_file = work_dir / '_claude_style_extract_task.md'
    task_file.write_text(
        f"{prompt}\n\n---\nWrite ONLY the resulting JSON object to this exact file: "
        f"{out_file.as_posix()}\n", encoding='utf-8')
    ClaudeCodeRunner(model_name=model_name or getattr(config, 'CLAUDE_MODEL', 'sonnet')).run_task_file(
        task_file, work_dir, label='style_extract')
    if not out_file.exists():
        return {}
    return _parse(out_file.read_text(encoding='utf-8', errors='replace'))


def _pick_extract_operator() -> tuple:
    """(бэкенд, модель) для нейро-выделения.

    «Авто» = ТОТ ЖЕ ОПЕРАТОР, ЧТО У РЕЖИССЁРА, а не перебор по наличию ключей.

    Раньше при пустом `STYLE_EXTRACT_BACKEND` шёл перебор ключей, и первым в
    очереди стоял `gpt_nexus`. Модель при этом бралась из `STYLE_EXTRACT_MODEL`
    — одной настройки на все бэкенды. Стоило автовыбору дать не того оператора,
    чья модель прописана, и стадия падала: в живом прогоне 01.09.2026 нексусу
    ушла модель `gpt-5.6-luna`, которой у него нет
    (`model_not_found: not supported by any configured account in this group`),
    и нейро-выделение молча свалилось в разбор по якорям.

    Теперь пара «оператор + модель» берётся из общей точки
    `text_operator.resolve_text_operator`, где порядок такой: явная настройка
    стадии → оператор режиссёра → промптер Real Photo. Модель под провайдера
    подбирается там же, поэтому чужая модель к чужому транспорту не уедет.
    """
    from units.operator_hub import resolve_text_operator

    provider, model = resolve_text_operator('style_extract')
    if provider:
        return provider, model
    # Совсем ничего не настроено — Codex (подписка GPT), модель из настроек режиссёра.
    return 'codex', ''


def _regex_blocks(style_guide: str) -> Dict[str, str]:
    """Питон-регекс по якорям (lazy import gpt_director, чтобы не плодить цикл)."""
    from units.codex_director import extract_style_block, extract_negative_block
    return {
        'style_block': extract_style_block(style_guide) or '',
        'negative_block': extract_negative_block(style_guide) or '',
    }


def resolve_style_blocks(style_guide: str, output_dir) -> Dict[str, str]:
    """Возвращает {style_block, negative_block} с приоритетом и кэшем style_blocks.json.

    Логика по config.STYLE_EXTRACT_MODE:
      neuro → нейро-выделение (fallback на regex при пустом результате)
      regex → питон по якорям
      off   → {} (модель пишет блоки сама)
    Результат кэшируется в <output_dir>/style_blocks.json (не дёргать нейру повторно;
    юзер может вручную поправить файл). Кэш имеет приоритет над любым режимом.
    """
    cache = Path(output_dir) / 'style_blocks.json'
    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding='utf-8-sig'))
            if isinstance(data, dict):
                return {'style_block': str(data.get('style_block', '') or ''),
                        'negative_block': str(data.get('negative_block', '') or '')}
        except Exception:
            pass

    mode = (getattr(config, 'STYLE_EXTRACT_MODE', 'neuro') or 'neuro').strip().lower()
    if mode == 'off':
        return {}

    result: Dict[str, str] = {'style_block': '', 'negative_block': ''}
    if mode == 'neuro':
        # Пара «оператор + модель» приходит вместе: модель уже подобрана под
        # этого оператора. Брать её отдельной настройкой нельзя — так чужая
        # модель уезжала к чужому транспорту (улов 01.09.2026).
        backend, model = _pick_extract_operator()
        model = model or None
        print(f"   🧠 Style Extract (нейро, {backend}/{model or '—'}): "
              f"выделяю style/negative блоки из стайлгайда...")
        # work_dir нужен CLI-агентам (codex/claude_code) — они пишут task/result файлы
        r = extract_style_blocks_via_llm(style_guide, backend, model,
                                         work_dir=Path(output_dir) / '_style_extract_work')
        if r and (r.get('style_block') or r.get('negative_block')):
            result = r
        else:
            print("   ↩️  Нейро-выделение пусто — fallback на питон-регекс по якорям")
            result = _regex_blocks(style_guide)
    else:  # regex
        result = _regex_blocks(style_guide)

    if result.get('style_block') or result.get('negative_block'):
        try:
            cache.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception as _e:
            logging.warning(f"style_blocks.json write failed: {_e}")
        # печать отдельно — эмодзи/charmap на Windows-консоли не должны маскировать запись
        try:
            print(f"   style_blocks.json: style={len(result.get('style_block','').split())} words, "
                  f"negative={len(result.get('negative_block','').split())} words (editable by hand)")
        except Exception:
            pass
    return result
