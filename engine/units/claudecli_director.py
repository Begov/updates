"""
Claude Code Director.

Drop-in backend for units.scene_director.direct_project(). It reuses the mature
Codex/GPT director orchestration and swaps only the executor to Claude Code CLI.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Optional, Dict, Any

import params as config
from units.claudecli_runner import (
    ClaudeCodeRunner,
    extract_json_object,
    extract_result_text,
    load_json_file,
)
from units.codex_director import GptDirector, resolve_pass2_chunk_size


class ClaudeCodeDirector(GptDirector):
    """Director backend through Claude Code CLI using the same analyze() API."""

    def __init__(self, model_name: Optional[str] = None):
        self.model_name = model_name or config.CLAUDE_MODEL
        self.schemas_dir = Path(__file__).parent.parent / 'schemas'
        self.prompts_dir = Path(__file__).parent.parent / 'instructions'
        self.timeout = config.CLAUDE_DIRECTOR_TIMEOUT
        self.claude = ClaudeCodeRunner(self.model_name, self.timeout)

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
        prompt = super()._build_pass1_prompt(
            transcription_path,
            style_guide_path,
            sys_instr_path,
            pause_guide_path,
            mp_path,
            project_name,
            audio_duration,
        )
        try:
            sys_instr = sys_instr_path.read_text(encoding='utf-8', errors='replace')
            sys_instr = (
                sys_instr
                .replace('{clip_min_dur}', str(config.ACTIVE_CLIP_MIN_DURATION))
                .replace('{clip_max_dur}', str(config.ACTIVE_CLIP_MAX_DURATION))
                .replace('{clip_intro_min_dur}', str(config.get_intro_clip_min()))
                .replace('{clip_intro_max_dur}', str(config.get_intro_clip_max()))
                .replace('{intro_block_dur}', str(config.INTRO_BLOCK_DURATION))
            )
        except Exception as e:
            sys_instr = f"(Could not inline director instruction: {e})"
        try:
            pause_guide = pause_guide_path.read_text(encoding='utf-8', errors='replace')
        except Exception as e:
            pause_guide = f"(Could not inline pause guide: {e})"

        return prompt + f"""

## INLINE DIRECTOR INSTRUCTION COPY
If the instruction file path above is outside your allowed directory, use this inline copy instead:
{sys_instr}

## INLINE PAUSE GUIDE COPY
If the pause guide file path above is outside your allowed directory, use this inline copy instead:
{pause_guide}
"""

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
        # Pass2 chunk override (как claude_api): claude не тянет большие чанки →
        # ограничиваем GPT_PASS2_CHUNK_SIZE значением CLAUDE_CODE_PASS2_CHUNK_SIZE,
        # parallel принудительно 1. Восстанавливаем после.
        old_chunk = getattr(config, 'GPT_PASS2_CHUNK_SIZE', 150)
        old_parallel = getattr(config, 'GPT_PASS2_PARALLEL', 1)
        claude_chunk = resolve_pass2_chunk_size(
            getattr(config, 'CLAUDE_CODE_PASS2_CHUNK_SIZE', 0)
        )
        config.GPT_PASS2_CHUNK_SIZE = claude_chunk
        config.GPT_PASS2_PARALLEL = 1
        try:
            results = super().analyze(
                transcription_path, style_guide_path, output_dir, project_name,
                two_pass=two_pass, prompts_only=prompts_only, resume_path=resume_path,
                pass1_only=pass1_only,
            )
        finally:
            config.GPT_PASS2_CHUNK_SIZE = old_chunk
            config.GPT_PASS2_PARALLEL = old_parallel
        results['cost'] = {
            'total_cost': 0,
            'input_tokens': 0,
            'output_tokens': 0,
            'note': 'Claude Code via Claude subscription'
                    if config.CLAUDE_CODE_USE_SUBSCRIPTION
                    else 'Claude Code via configured Anthropic billing',
        }
        return results

    def _run_codex(
        self,
        task_prompt: str,
        schema_file: str,
        result_file: Path,
        working_dir: Path,
    ) -> dict:
        schema_path = self.schemas_dir / schema_file
        if not schema_path.exists():
            raise FileNotFoundError(f"Schema не найдена: {schema_path}")

        if result_file.exists():
            result_file.unlink()

        task_file = working_dir / f'_claude_{schema_file.replace(".json", "")}_task.md'
        task_file.write_text(task_prompt, encoding='utf-8')

        extra = f"""
Expected control schema: {schema_path.as_posix()}
After completing the task and writing the project output file, write a valid
control JSON object to this exact path: {result_file.as_posix()}
The control JSON must match the schema. Do not wrap it in Markdown.
Your final response must also be the same control JSON object only.
"""
        rc, _stdout, stderr, elapsed = self.claude.run_task_file(
            task_file=task_file,
            working_dir=working_dir,
            label=schema_file.replace('_control.json', ''),
            extra_instruction=extra,
        )

        if not result_file.exists():
            try:
                stdout_control = extract_json_object(extract_result_text(_stdout))
                if stdout_control:
                    result_file.write_text(
                        json.dumps(stdout_control, ensure_ascii=False, indent=2),
                        encoding='utf-8',
                    )
            except Exception:
                pass

        if not result_file.exists() and schema_file == 'pass1_control.json':
            mp_path = working_dir / 'montage_plan.json'
            if mp_path.exists():
                try:
                    plan = json.loads(mp_path.read_text(encoding='utf-8-sig'))
                    clips = plan.get('clips') or plan.get('montage_plan', {}).get('clips', [])
                    audio_duration = plan.get('audioDuration') or plan.get('montage_plan', {}).get('audioDuration', 0)
                    result_file.write_text(json.dumps({
                        'montage_plan_path': str(mp_path),
                        'status': 'ok',
                        'clips_count': len(clips),
                        'audio_duration': audio_duration,
                        'error_message': '',
                    }, ensure_ascii=False, indent=2), encoding='utf-8')
                except Exception as e:
                    logging.warning("Could not synthesize Claude pass1 control: %s", e)

        if not result_file.exists():
            result_text = extract_result_text(_stdout)
            raise RuntimeError(
                f"Claude Code не создал {result_file.name} (exit {rc})\n"
                f"stdout: {result_text[-1200:]}\n"
                f"stderr: {stderr[-1200:]}"
            )

        control = load_json_file(result_file)
        logging.info("Claude Code control: %s", json.dumps(control, ensure_ascii=False))
        print(f"   ⏱  Claude Code: {elapsed:.0f}с, model={self.model_name}")
        return control

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
        _stem = prompts_path.stem
        if len(_stem) > 40:
            import hashlib as _hashlib
            _hash = _hashlib.md5(_stem.encode('utf-8', errors='replace')).hexdigest()[:8]
            _stem = _stem[:32] + '_' + _hash

        result_file = working_dir / f'_claude_pass2_control_{_stem}.json'
        if result_file.exists():
            result_file.unlink()

        task_file = working_dir / f'_claude_pass2_task_{_stem}.md'
        wrapped_prompt = task_prompt + f"""

## CLAUDE CODE OUTPUT HANDOFF
Write the final JSON object with the `prompts` array to this exact file:
{result_file.as_posix()}

The file content must be raw JSON only:
{{"prompts":["[CLIP 1] prompt text", "[CLIP 2] prompt text"]}}

Do not write or overwrite the final prompts TXT file directly.
"""
        task_file.write_text(wrapped_prompt, encoding='utf-8')

        rc, stdout, stderr, elapsed = self.claude.run_task_file(
            task_file=task_file,
            working_dir=working_dir,
            label='Pass2',
            extra_instruction='Write the requested JSON handoff file before you finish.',
        )

        prompts_list = []
        if result_file.exists():
            try:
                data = load_json_file(result_file)
                prompts_list = data.get('prompts', [])
            except Exception as e:
                logging.warning("Claude Pass2 result parse failed: %s", e)

        if not prompts_list:
            low_text = (stderr + "\n" + stdout).lower()
            if 'usage limit' in low_text or 'rate limit' in low_text:
                if rate_limit_reported is not None:
                    rate_limit_reported.set()
                if rate_limit_info is not None and not rate_limit_info:
                    m = re.search(r'try again(?: at| after)? ([^\n.]+)', stderr + "\n" + stdout, re.IGNORECASE)
                    rate_limit_info.append(m.group(1).strip() if m else None)
                return prompts_offset, session_id, None

            logging.warning("Claude Pass2: no prompts. rc=%s stderr=%s", rc, stderr[-1000:])
            print(f"   ⚠️  Claude Code не вернул промпты (rc={rc})")
            return prompts_offset, session_id, set()

        def _is_pollution(p: str) -> bool:
            s = re.sub(r'^\[CLIP\s+\d+\]\s*', '', p.strip())
            return s.startswith('VOICEOVER:') or s.startswith('[CLIP ')

        vo_count = sum(1 for p in prompts_list if _is_pollution(str(p)))
        if vo_count > len(prompts_list) * 0.3:
            logging.error("Claude Pass2 pollution: %s/%s", vo_count, len(prompts_list))
            print(f"   ❌ Транскрипция вместо промптов ({vo_count} VOICEOVER блоков)")
            return prompts_offset, session_id, set()

        id_re = re.compile(r'^\[(?:CLIP\s+)?(\d+)\]\s*')
        found_ids: set[int] = set()
        id_pairs: list[tuple[Optional[int], str]] = []
        for raw_prompt in prompts_list:
            prompt = str(raw_prompt).strip()
            m = id_re.match(prompt)
            if m:
                cid = int(m.group(1))
                found_ids.add(cid)
                id_pairs.append((cid, id_re.sub('', prompt, count=1).strip()))
            else:
                id_pairs.append((None, prompt))

        if len(id_pairs) > n_clips:
            id_pairs = id_pairs[:n_clips]

        clean_prompts = [p for _, p in id_pairs if p.strip()]
        existing = ''
        if prompts_path.exists() and prompts_path.stat().st_size > 0:
            existing = prompts_path.read_text(encoding='utf-8', errors='replace').rstrip()

        if keep_ids:
            new_block = '\n\n'.join(
                f'[{cid}] {p}' if cid is not None else p
                for cid, p in id_pairs if p.strip()
            )
        else:
            new_block = '\n\n'.join(clean_prompts)

        combined = existing + '\n\n' + new_block if existing else new_block
        prompts_path.write_text(combined, encoding='utf-8')

        total_count = prompts_offset + len(clean_prompts)
        ids_info = f", IDs={sorted(found_ids)[:5]}{'...' if len(found_ids) > 5 else ''}" if found_ids else ""
        if not silent:
            mins, secs = int(elapsed) // 60, int(elapsed) % 60
            print(f"   ⏱  Claude Pass2: {mins}м {secs:02d}с")
        print(f"   → Записано: {len(clean_prompts)}, всего: {total_count}/{prompts_offset + n_clips}{ids_info}")
        return total_count, None, found_ids


def claude_code_direct_project(
    transcription_path: str,
    style_guide_path: str,
    output_dir: str,
    project_name: str = "Untitled",
    model_name: Optional[str] = None,
    two_pass: bool = False,
    prompts_only: bool = False,
    resume_path: Optional[str] = None,
) -> Dict[str, Any]:
    director = ClaudeCodeDirector(model_name=model_name)
    return director.analyze(
        transcription_path, style_guide_path, output_dir, project_name,
        two_pass=two_pass, prompts_only=prompts_only, resume_path=resume_path,
    )
