"""
SG Adapt — адаптирует мастер-стайлгайд под конкретный сюжет через GPT (Codex CLI).
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

import params as config
def adapt_style_guide(
    original_sg_path: str,
    project_dir: str,
    project_name: str = "",
) -> str:
    """
    Адаптирует мастер-стайлгайд под конкретный сюжет через GPT (Codex CLI).

    1. Читает чистый текст скрипта из SRT/{project}_скрипт.txt
    2. Читает оригинальный SG
    3. Читает промпт-инструкцию из agent/prompts/sg_adapt_prompt.txt
    4. Формирует task prompt (инструкция + оригинальный SG + скрипт)
    5. Записывает task prompt в файл (обход WinError 206)
    6. Запускает codex exec — результат пишется в output файл
    7. Сохраняет: {project_dir}/style_guide_adapted.txt
    8. Возвращает путь к адаптированному SG

    При ошибке: возвращает путь к оригинальному SG (fallback).
    """
    original_sg_path = str(original_sg_path)
    project_dir = Path(project_dir)
    prompts_dir = Path(__file__).parent.parent / 'instructions'
    prompt_template_path = prompts_dir / 'sg_adapt_prompt.txt'
    output_path = project_dir / 'style_guide_adapted.txt'
    # Свой оператор адаптации: SG_ADAPT_BACKEND/SG_ADAPT_MODEL из .env.
    # Пусто — работаем оператором режиссёра, как было раньше.
    provider = (os.getenv('SG_ADAPT_BACKEND', '') or '').strip().lower()
    if not provider or provider == 'auto':
        provider = (getattr(config, 'GPT_PROVIDER', '') or 'openai').strip().lower()
    model_override = (os.getenv('SG_ADAPT_MODEL', '') or '').strip()
    chat_providers = {
        'deepseek', 'openrouter', 'meta_muse', 'codex_reseller',
        'gpt_nexus',
    }

    # ── 1. Проверяем Codex ───────────────────────────────────────────────────
    if provider not in chat_providers and (
        not config.CODEX_EXE_PATH or not Path(config.CODEX_EXE_PATH).is_file()
    ):
        logging.warning(f"SG Adapt: Codex CLI не найден ({config.CODEX_EXE_PATH}), используем оригинальный SG")
        return original_sg_path

    codex_exe = Path(config.CODEX_EXE_PATH) if config.CODEX_EXE_PATH else None

    # ── 2. Читаем оригинальный SG ────────────────────────────────────────────
    try:
        original_sg_text = Path(original_sg_path).read_text(encoding='utf-8', errors='replace')
    except Exception as e:
        logging.warning(f"SG Adapt: не удалось прочитать оригинальный SG ({e}), fallback")
        return original_sg_path

    # ── 3. Ищем текст скрипта ────────────────────────────────────────────────
    script_text = _find_script_text(project_dir, project_name)
    if not script_text:
        logging.warning(f"SG Adapt: текст скрипта не найден в {project_dir}, fallback")
        return original_sg_path

    # ── 4. Читаем промпт-инструкцию ──────────────────────────────────────────
    try:
        prompt_template = prompt_template_path.read_text(encoding='utf-8', errors='replace')
    except Exception as e:
        logging.warning(f"SG Adapt: не найден промпт {prompt_template_path} ({e}), fallback")
        return original_sg_path

    # ── 5. Собираем task prompt ──────────────────────────────────────────────
    task_prompt = prompt_template.replace('{original_sg}', original_sg_text).replace('{script_text}', script_text)

    if provider in chat_providers:
        from units import llm_gateway as chat_client

        model = {
            'deepseek': getattr(config, 'DEEPSEEK_MODEL', '') or 'deepseek-v4-flash',
            'openrouter': getattr(config, 'OPENROUTER_MODEL', '') or 'openrouter/free',
            'codex_reseller': (
                getattr(config, 'CODEX_RESELLER_MODEL', '') or 'gpt-5.6-terra'
            ),
            'gpt_nexus': getattr(config, 'GPT_NEXUS_MODEL', '') or 'gpt-5.5',
            'meta_muse': (
                getattr(config, 'META_MUSE_MODEL', '')
                or 'muse-spark-1.2-contributor'
            ),
        }[provider]
        if model_override:
            model = model_override
        try:
            result = chat_client.chat_completion(
                provider,
                model,
                [{'role': 'user', 'content': task_prompt}],
                max_tokens=None,
                reasoning_level=getattr(
                    config, 'CHAT_REASONING_LEVEL', 'medium'),
                web=(
                    provider == 'openrouter'
                    and getattr(
                        config, 'OPENROUTER_WEB_SEARCH', 'disabled'
                    ) == 'enabled'
                ),
                json_mode=False,
                timeout=getattr(config, 'GPT_DIRECTOR_TIMEOUT', 900),
            ).strip()
            if result.startswith('```') and result.endswith('```'):
                result = result.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
            if len(result) < 100:
                raise RuntimeError(
                    'provider returned an empty or truncated style guide')
            output_path.write_text(result, encoding='utf-8')
            print(
                f"  [OK] SG Adapt: {provider}/{model} -> "
                f"{output_path.name}"
            )
            return str(output_path)
        except Exception as exc:
            logging.warning(
                "SG Adapt via %s failed (%s), using original SG",
                provider,
                exc,
            )
            return original_sg_path

    # Пишем в файл (обход WinError 206 / длинные аргументы командной строки)
    task_file = project_dir / '_codex_sg_adapt_task.md'
    try:
        task_file.write_text(task_prompt, encoding='utf-8')
    except Exception as e:
        logging.warning(f"SG Adapt: не удалось записать task file ({e}), fallback")
        return original_sg_path

    short_prompt = f"Read the task file at {task_file.as_posix()} and follow ALL instructions exactly. Write the result (complete adapted style guide) to the file: {output_path.as_posix()}"

    # ── 6. Удаляем предыдущий output ────────────────────────────────────────
    if output_path.exists():
        try:
            output_path.unlink()
        except Exception:
            pass

    # ── 7. Запускаем codex exec ──────────────────────────────────────────────
    cmd = [
        str(codex_exe), 'exec',
        short_prompt,
        '--dangerously-bypass-approvals-and-sandbox',
        '--skip-git-repo-check',
        '-m', config.GPT_MODEL,
        '-C', str(project_dir),
    ]

    logging.info(f"SG Adapt: запуск codex exec, model={config.GPT_MODEL}, cwd={project_dir}")
    t0 = time.time()

    _done = threading.Event()
    _returncode = [None]
    _stderr_lines: list = []

    def _exec():
        try:
            p = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding='utf-8', errors='replace',
                cwd=str(project_dir),
                # provider='openai' — иначе при deepseek/openrouter CODEX_HOME уедет
                # на домик Moon Bridge, чей прокси (:38440) давно не поднимается.
                env=config.codex_env_for_provider(provider='openai'),  # прокси CODEX_PROXY
            )

            def _drain_stderr():
                for line in p.stderr:
                    _stderr_lines.append(line.rstrip())
            t_err = threading.Thread(target=_drain_stderr, daemon=True)
            t_err.start()

            for line in p.stdout:
                line = line.rstrip()
                if line:
                    ll = line.lower()
                    if any(k in ll for k in ('write', 'wrote', 'file', 'style')):
                        import sys as _sys
                        _sys.stdout.write(f'\r   SG Adapt: {line[:100]:<100}\n')
                        _sys.stdout.flush()

            p.wait(timeout=config.GPT_DIRECTOR_TIMEOUT)
            t_err.join(timeout=5)
            _returncode[0] = p.returncode
        except subprocess.TimeoutExpired:
            p.kill()
            _returncode[0] = -1
            logging.warning("SG Adapt: timeout")
        except Exception as e:
            _stderr_lines.append(str(e))
            _returncode[0] = -1
        finally:
            _done.set()

    thread = threading.Thread(target=_exec, daemon=True)
    thread.start()

    # Спиннер прогресса
    _spinner_chars = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
    _spin_i = 0
    while not _done.is_set():
        elapsed = int(time.time() - t0)
        mins = elapsed // 60
        secs = elapsed % 60
        time_str = f"{mins}м {secs:02d}с" if mins > 0 else f"{secs}с"
        spin_char = _spinner_chars[_spin_i % len(_spinner_chars)]
        import sys as _sys
        _sys.stdout.write(f'\r   {spin_char} SG Adapt: {time_str}  ')
        _sys.stdout.flush()
        _spin_i += 1
        _done.wait(timeout=1.0)

    import sys as _sys
    _sys.stdout.write('\r' + ' ' * 60 + '\r')
    _sys.stdout.flush()

    elapsed = time.time() - t0
    logging.info(f"SG Adapt: codex завершён за {elapsed:.0f}с, returncode={_returncode[0]}")

    # ── 8. Проверяем результат ───────────────────────────────────────────────
    if output_path.exists() and output_path.stat().st_size > 100:
        print(f"  [OK] SG Adapt: адаптированный стайлгайд сохранён ({output_path.stat().st_size} байт)")
        return str(output_path)
    else:
        if _stderr_lines:
            logging.warning(f"SG Adapt stderr: {' | '.join(_stderr_lines[-5:])}")
        logging.warning(f"SG Adapt: output не создан или пустой, fallback на оригинальный SG")
        return original_sg_path


def _find_script_text(project_dir: Path, project_name: str) -> Optional[str]:
    """
    Ищет чистый текст скрипта:
    1. SRT/{project_name}_скрипт.txt  — создаётся AssemblyAI
    2. SRT/*_скрипт.txt              — любой скрипт в SRT/
    3. *_script.txt в корне проекта  — script mode
    4. *_скрипт.txt в корне проекта
    """
    srt_dir = project_dir / 'SRT'

    # 1. По имени проекта
    if project_name:
        candidate = srt_dir / f'{project_name}_скрипт.txt'
        if candidate.exists():
            return candidate.read_text(encoding='utf-8-sig', errors='replace').strip()

    # 2. Любой _скрипт.txt в SRT/
    if srt_dir.exists():
        for f in sorted(srt_dir.glob('*_скрипт.txt')):
            text = f.read_text(encoding='utf-8-sig', errors='replace').strip()
            if text:
                return text

    # 3. *_script.txt в корне (script mode — английские)
    for f in sorted(project_dir.glob('*_script.txt')):
        text = f.read_text(encoding='utf-8-sig', errors='replace').strip()
        if text:
            return text

    # 4. *_скрипт.txt в корне (script mode — русские)
    for f in sorted(project_dir.glob('*_скрипт.txt')):
        text = f.read_text(encoding='utf-8-sig', errors='replace').strip()
        if text:
            return text

    return None
