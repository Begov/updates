"""Claude Nexus director backend.

This reuses the mature Codex/GPT director orchestration, but replaces file-tool
execution with direct Anthropic Messages API calls. Because the API cannot read
or write local files, prompts are inlined and returned JSON is written locally.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import params as config
# Опциональные фичи форка (auto-references / source_context) — если модулей нет,
# director работает без Pass 1.5 авто-референсов и без source-context инъекции.
try:
    from units.auto_reference_images import generate_auto_reference_images
    from units.auto_references import (
        auto_references_enabled,
        auto_references_path,
        build_gemini_auto_reference_prompt as build_auto_reference_prompt,
        load_auto_references,
        normalize_auto_references,
        reference_counts,
        save_auto_references,
    )
    _AUTO_REFS_AVAILABLE = True
except ImportError:
    _AUTO_REFS_AVAILABLE = False
    generate_auto_reference_images = None
    def auto_references_enabled():
        return False
    def auto_references_path(output_dir):
        from pathlib import Path as _P
        return _P(output_dir) / 'auto_references.json'
    build_auto_reference_prompt = None
    load_auto_references = lambda *a, **k: None
    normalize_auto_references = lambda x: x
    reference_counts = lambda x: {'characters': 0, 'locations': 0, 'props': 0}
    save_auto_references = lambda *a, **k: None
from units.nexus_runner import ClaudeApiRunner, extract_json_object
from units.codex_director import GptDirector, resolve_pass2_chunk_size
try:
    from units.source_context import append_sources_to_style_guide
except ImportError:
    def append_sources_to_style_guide(style, **kwargs):
        return style


class ClaudeApiDirector(GptDirector):
    """Director backend through Claude Nexus using the same analyze() API."""

    def __init__(self, model_name: Optional[str] = None):
        self.model_name = model_name or config.CLAUDE_API_MODEL
        self.schemas_dir = Path(__file__).parent.parent / 'schemas'
        self.prompts_dir = Path(__file__).parent.parent / 'instructions'
        self.timeout = config.CLAUDE_DIRECTOR_TIMEOUT
        self.claude = ClaudeApiRunner(self.model_name, self.timeout)

    def analyze(self, *args, **kwargs) -> Dict[str, Any]:
        old_chunk = getattr(config, 'GPT_PASS2_CHUNK_SIZE', 150)
        old_parallel = getattr(config, 'GPT_PASS2_PARALLEL', 1)
        claude_chunk = resolve_pass2_chunk_size(
            getattr(config, 'CLAUDE_API_PASS2_CHUNK_SIZE', 0)
        )
        config.GPT_PASS2_CHUNK_SIZE = claude_chunk
        config.GPT_PASS2_PARALLEL = 1
        try:
            results = super().analyze(*args, **kwargs)
        finally:
            config.GPT_PASS2_CHUNK_SIZE = old_chunk
            config.GPT_PASS2_PARALLEL = old_parallel
        results['cost'] = {
            'total_cost': 0,
            'input_tokens': self.claude.input_tokens,
            'output_tokens': self.claude.output_tokens,
            'note': 'Claude Nexus token usage only; pricing not estimated locally',
        }
        return results

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
            transcription = transcription_path.read_text(encoding='utf-8-sig', errors='replace')
        except Exception as exc:
            transcription = f"(could not read transcription: {exc})"
        try:
            style = style_guide_path.read_text(encoding='utf-8-sig', errors='replace')
            style = append_sources_to_style_guide(
                style,
                project_dir=mp_path.parent,
                style_guide_path=style_guide_path,
                announce=False,
            )
        except Exception as exc:
            style = f"(could not read style guide: {exc})"
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
        except Exception as exc:
            sys_instr = f"(could not read director instruction: {exc})"
        try:
            pause_guide = pause_guide_path.read_text(encoding='utf-8', errors='replace')
        except Exception as exc:
            pause_guide = f"(could not read pause guide: {exc})"

        return prompt + f"""

## CLAUDE API INLINE FILE CONTENTS
You are running through Claude Nexus, so you cannot open local paths. Use the
inline copies below as the source of truth.

### director_instruction_pass1.txt
{sys_instr}

### director_pause_guide.txt
{pause_guide}

### style guide
{style}

### transcription.json
```json
{transcription}
```
"""

    def _run_codex(
        self,
        task_prompt: str,
        schema_file: str,
        result_file: Path,
        working_dir: Path,
    ) -> dict:
        if result_file.exists():
            result_file.unlink()

        if schema_file != 'pass1_control.json':
            raise RuntimeError(f"Claude Nexus: unsupported generic control schema {schema_file}")

        mp_path = working_dir / 'montage_plan.json'
        api_prompt = task_prompt + f"""

## CLAUDE API OUTPUT CONTRACT
You cannot write files directly. Return ONLY one valid JSON object in this
shape, with no Markdown:
{{
  "control": {{
    "montage_plan_path": "{mp_path.as_posix()}",
    "status": "ok",
    "clips_count": 0,
    "audio_duration": 0,
    "error_message": ""
  }},
  "montage_plan": {{
    "audioDuration": 0,
    "clips": []
  }}
}}

The `montage_plan` value must be exactly the JSON content that should be saved
to montage_plan.json.
"""
        text, _cost, elapsed = self.claude.complete(
            api_prompt,
            label='Claude Nexus Pass 1',
            temperature=0.25,
        )
        data = extract_json_object(text)
        plan = data.get('montage_plan') or data.get('plan') or {}
        if 'montage_plan' in plan and isinstance(plan.get('montage_plan'), dict):
            plan = plan['montage_plan']
        if not plan.get('clips') and data.get('clips'):
            plan = {'audioDuration': data.get('audioDuration'), 'clips': data.get('clips')}
        if not plan.get('clips'):
            raise RuntimeError(f"Claude Nexus Pass 1 не вернул clips\n{text[:1000]}")

        mp_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
        control = data.get('control') if isinstance(data.get('control'), dict) else {}
        control = {
            'montage_plan_path': str(mp_path),
            'status': control.get('status') or 'ok',
            'clips_count': int(control.get('clips_count') or len(plan.get('clips', []))),
            'audio_duration': control.get('audio_duration') or plan.get('audioDuration') or 0,
            'error_message': control.get('error_message') or '',
        }
        result_file.write_text(json.dumps(control, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f"   ⏱  Claude Nexus: {elapsed:.0f}с, model={self.model_name}")
        return control

    def _run_auto_references(
        self,
        transcription_path: Path,
        style_guide_path: Path,
        mp_path: Path,
        output_dir: Path,
        project_name: str,
    ) -> Optional[dict]:
        if not auto_references_enabled():
            return None

        ref_path = auto_references_path(output_dir)
        existing = load_auto_references(output_dir)
        if existing:
            counts = reference_counts(existing)
            print(
                f"\n🧩 Auto References: найден готовый {ref_path.name} "
                f"(characters={counts['characters']}, locations={counts['locations']}, props={counts['props']})"
            )
            generate_auto_reference_images(output_dir, existing)
            return existing

        print("\n🧩 Claude Nexus Pass 1.5: авто-референсы персонажей/локаций/реквизита...")
        try:
            transcription = json.loads(transcription_path.read_text(encoding='utf-8-sig'))
            montage_plan = json.loads(mp_path.read_text(encoding='utf-8-sig'))
            style = style_guide_path.read_text(encoding='utf-8-sig', errors='replace')
            style = append_sources_to_style_guide(
                style,
                project_dir=output_dir,
                style_guide_path=style_guide_path,
                announce=False,
            )
            prompt = build_auto_reference_prompt(
                transcription=transcription,
                style_guide=style,
                montage_plan=montage_plan,
                project_name=project_name,
            )
            text, _cost, elapsed = self.claude.complete(
                prompt,
                label='Claude Nexus AutoRefs',
                temperature=0.25,
                max_tokens=min(config.CLAUDE_API_MAX_TOKENS, 12000),
            )
            data = normalize_auto_references(extract_json_object(text))
            save_auto_references(data, output_dir)
            counts = reference_counts(data)
            print(
                f"   ✅ auto_references.json: "
                f"characters={counts['characters']}, locations={counts['locations']}, props={counts['props']}"
            )
            print(f"   ⏱  Claude Nexus AutoRefs: {elapsed:.0f}с")
            generate_auto_reference_images(output_dir, data)
            return data
        except Exception as exc:
            print(f"   ⚠️  Auto References skipped: {exc}")
            logging.warning("Claude Nexus auto_references failed: %s", exc, exc_info=True)
            return None

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
        api_prompt = task_prompt + f"""

## CLAUDE API OUTPUT CONTRACT
You cannot write files directly. Return ONLY one valid JSON object:
{{"prompts":["[CLIP 1] prompt text", "[CLIP 2] prompt text"]}}

Return exactly {n_clips} prompts for this chunk. Do not include Markdown.
"""
        try:
            text, _cost, elapsed = self.claude.complete(
                api_prompt,
                label='Claude Nexus Pass 2',
                max_tokens=min(config.CLAUDE_API_MAX_TOKENS, max(3000, n_clips * 260)),
                temperature=0.45,
            )
            data = extract_json_object(text)
            prompts_list = data.get('prompts') or data.get('items') or []
        except Exception as exc:
            low = str(exc).lower()
            if 'suspicious activity' in low or 'temporary limits' in low:
                raise RuntimeError(
                    "CodexHub temporary anti-abuse limit hit. Wait 10-15 minutes, "
                    "then resume director generation; do not keep retrying immediately."
                ) from exc
            if 'rate' in low or '429' in low or 'overloaded' in low:
                time.sleep(90)
                if rate_limit_reported is not None:
                    rate_limit_reported.set()
                if rate_limit_info is not None and not rate_limit_info:
                    rate_limit_info.append(None)
                return prompts_offset, session_id, None
            if '403' in low and 'reset after' in low:
                logging.warning("Claude Nexus Pass2 gateway reset; cooldown before chunk retry: %s", exc)
                time.sleep(30)
                return prompts_offset, session_id, set()
            logging.warning("Claude Nexus Pass2 failed: %s", exc, exc_info=True)
            print(f"   ⚠️  Claude Nexus не вернул промпты: {exc}")
            return prompts_offset, session_id, set()

        if not prompts_list:
            print("   ⚠️  Claude Nexus вернул 0 промптов")
            return prompts_offset, session_id, set()

        def _is_pollution(p: str) -> bool:
            s = re.sub(r'^\[CLIP\s+\d+\]\s*', '', p.strip())
            return s.startswith('VOICEOVER:') or s.startswith('[CLIP ')

        prompts_list = [str(p).strip() for p in prompts_list if str(p).strip()]
        vo_count = sum(1 for p in prompts_list if _is_pollution(p))
        if vo_count > len(prompts_list) * 0.3:
            logging.error("Claude Nexus Pass2 pollution: %s/%s", vo_count, len(prompts_list))
            print(f"   ❌ Транскрипция вместо промптов ({vo_count} VOICEOVER блоков)")
            return prompts_offset, session_id, set()

        id_re = re.compile(r'^\[(?:CLIP\s+)?(\d+)\]\s*')
        found_ids: set[int] = set()
        id_pairs: list[tuple[Optional[int], str]] = []
        for raw_prompt in prompts_list[:n_clips]:
            m = id_re.match(raw_prompt)
            if m:
                cid = int(m.group(1))
                found_ids.add(cid)
                id_pairs.append((cid, id_re.sub('', raw_prompt, count=1).strip()))
            else:
                id_pairs.append((None, raw_prompt))

        existing = ''
        if prompts_path.exists() and prompts_path.stat().st_size > 0:
            existing = prompts_path.read_text(encoding='utf-8', errors='replace').rstrip()

        if keep_ids:
            new_block = '\n\n'.join(
                f'[{cid}] {p}' if cid is not None else p
                for cid, p in id_pairs if p.strip()
            )
        else:
            new_block = '\n\n'.join(p for _, p in id_pairs if p.strip())

        combined = existing + '\n\n' + new_block if existing else new_block
        prompts_path.write_text(combined, encoding='utf-8')

        clean_count = len([p for _, p in id_pairs if p.strip()])
        total_count = prompts_offset + clean_count
        if not silent:
            mins, secs = int(elapsed) // 60, int(elapsed) % 60
            print(f"   ⏱  Claude Nexus Pass2: {mins}м {secs:02d}с")
        ids_info = f", IDs={sorted(found_ids)[:5]}{'...' if len(found_ids) > 5 else ''}" if found_ids else ""
        print(f"   → Записано: {clean_count}, всего: {total_count}/{prompts_offset + n_clips}{ids_info}")
        return total_count, None, found_ids


def claude_api_direct_project(
    transcription_path: str,
    style_guide_path: str,
    output_dir: str,
    project_name: str = "Untitled",
    model_name: Optional[str] = None,
    two_pass: bool = False,
    prompts_only: bool = False,
    resume_path: Optional[str] = None,
) -> Dict[str, Any]:
    director = ClaudeApiDirector(model_name=model_name)
    return director.analyze(
        transcription_path, style_guide_path, output_dir, project_name,
        two_pass=two_pass, prompts_only=prompts_only, resume_path=resume_path,
    )
