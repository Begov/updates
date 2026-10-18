"""
VEO Prompt Adapter via Codex CLI.
Переписывает image-промпты в VEO i2v промпты по стайлгайду.
Запускается параллельно с imggen — нулевая потеря времени.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Optional
from datetime import datetime

import params as config
from prompt_extract import find_prompts_file
from units.proc_tools import NO_WINDOW


def adapt_prompts_for_veo(
    project_dir: str,
    codex_exe: Optional[str] = None,
    model_name: Optional[str] = None,
    timeout: int = 600,
) -> Optional[str]:
    """
    Адаптация image-промптов для Veo i2v через Codex CLI.
    Промпт-шаблон читается из agent/prompts/veo_adapt_prompt.txt.

    Returns:
        Путь к файлу с адаптированными промптами, или None при ошибке.
    """
    project_dir = Path(project_dir)
    # Модель VEO-адаптера: VEO_ADAPT_MODEL из .env, иначе модель режиссёра.
    model_name = (os.getenv('VEO_ADAPT_MODEL', '') or '').strip() or model_name
    model = model_name or config.GPT_MODEL
    codex_path = codex_exe or config.CODEX_EXE_PATH
    codex_exe_path = Path(codex_path) if codex_path else None
    schemas_dir = Path(__file__).parent.parent / 'schemas'
    schema_path = schemas_dir / 'veo_adapt_control.json'

    # Validate prerequisites
    if codex_exe_path is None or not codex_exe_path.exists():
        print(f"  [codex-adapt] Codex CLI not found: {codex_path}")
        return None
    if not schema_path.exists():
        print(f"  [codex-adapt] Schema not found: {schema_path}")
        return None

    # Find input prompts
    input_prompts = find_prompts_file(str(project_dir))
    if not input_prompts:
        print("  [codex-adapt] No prompts file found in project")
        return None
    input_path = Path(input_prompts)

    # Count prompts for validation
    raw = input_path.read_text(encoding='utf-8').strip()
    prompt_count = len([p for p in raw.split('\n\n') if p.strip()])
    if prompt_count == 0:
        print("  [codex-adapt] Prompts file is empty")
        return None

    # Output path — NO _prompts_ in name to avoid find_prompts_file collision
    project_name = project_dir.name
    timestamp = datetime.now().strftime('%Y%m%d_%H%M')
    output_path = project_dir / f'{project_name}_veo_adapt_{timestamp}.txt'
    result_file = project_dir / '_codex_veo_adapt_control.json'

    # Clean previous result
    if result_file.exists():
        result_file.unlink()

    # Build task prompt from template file
    prompt_template_path = Path(__file__).parent.parent / 'instructions' / 'veo_adapt_prompt.txt'
    if prompt_template_path.exists():
        task_prompt = prompt_template_path.read_text(encoding='utf-8').format(
            input_prompts_path=input_path.as_posix(),
            prompt_count=prompt_count,
            output_path=output_path.as_posix(),
        )
    else:
        print(f"  [codex-adapt] Prompt template not found: {prompt_template_path}")
        return None

    # Build command
    cmd = [
        str(codex_exe_path), 'exec',
        task_prompt,
        '--output-schema', str(schema_path),
        '--output-last-message', str(result_file),
        '--dangerously-bypass-approvals-and-sandbox',
        '--skip-git-repo-check',
        '-m', model,
        '-C', str(project_dir),
    ]

    logging.info(f"[codex-adapt] Starting: model={model} prompts={prompt_count}")
    print(f"  [codex-adapt] Adapting {prompt_count} prompts with {model}...")
    t0 = time.time()

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding='utf-8',
            **NO_WINDOW,
            errors='replace',
            env=config.codex_env_for_provider(),  # прокси CODEX_PROXY
        )
    except subprocess.TimeoutExpired:
        print(f"  [codex-adapt] Timeout ({timeout}s) — using original prompts")
        logging.error(f"[codex-adapt] Timeout after {timeout}s")
        return None

    elapsed = time.time() - t0
    logging.info(f"[codex-adapt] Codex finished in {elapsed:.1f}s, exit={proc.returncode}")

    if proc.returncode != 0:
        logging.error(f"[codex-adapt] stderr: {(proc.stderr or '')[-1000:]}")

    # Read control JSON
    if not result_file.exists():
        print(f"  [codex-adapt] No control JSON — Codex failed")
        logging.error(f"[codex-adapt] No result file: {result_file}")
        return None

    try:
        control = json.loads(result_file.read_text(encoding='utf-8-sig'))
    except json.JSONDecodeError as e:
        print(f"  [codex-adapt] Invalid control JSON: {e}")
        return None

    logging.info(f"[codex-adapt] Control: {json.dumps(control, ensure_ascii=False)}")

    if control.get('status') != 'ok':
        print(f"  [codex-adapt] Error: {control.get('error_message', 'unknown')}")
        return None

    # Verify output file
    actual_path = Path(control.get('output_file_path', str(output_path)))
    if not actual_path.exists():
        actual_path = output_path
    if not actual_path.exists():
        print("  [codex-adapt] Output file not created")
        return None

    # Validate prompt count
    adapted_raw = actual_path.read_text(encoding='utf-8').strip()
    adapted_count = len([p for p in adapted_raw.split('\n\n') if p.strip()])

    if adapted_count != prompt_count:
        logging.warning(f"[codex-adapt] Count mismatch: {adapted_count} vs {prompt_count}")
        if adapted_count > prompt_count:
            # Trim
            parts = [p for p in adapted_raw.split('\n\n') if p.strip()]
            actual_path.write_text('\n\n'.join(parts[:prompt_count]), encoding='utf-8')
            adapted_count = prompt_count

    print(f"  [codex-adapt] Done: {adapted_count} prompts in {elapsed:.0f}s → {actual_path.name}")
    return str(actual_path)
