"""
Small Claude Code CLI runner for director-style tasks.

The runner is intentionally file-oriented: large prompts are written to a task
file, Claude Code is asked to read it, and control JSON is written back to a
known path. That keeps Windows command lines short and makes retries debuggable.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import params as config
from units.proc_tools import NO_WINDOW


class ClaudeCodeRunner:
    def __init__(self, model_name: Optional[str] = None, timeout: Optional[int] = None):
        self.model_name = model_name or getattr(config, 'CLAUDE_MODEL', 'sonnet')
        self.timeout = int(timeout or getattr(config, 'CLAUDE_DIRECTOR_TIMEOUT', 1200))
        self.max_turns = int(getattr(config, 'CLAUDE_CODE_MAX_TURNS', 20))
        self.permission_mode = getattr(config, 'CLAUDE_CODE_PERMISSION_MODE', 'bypassPermissions') or 'bypassPermissions'
        self.exe = self._resolve_exe()

    @staticmethod
    def _resolve_exe() -> str:
        configured = (getattr(config, 'CLAUDE_CODE_EXE_PATH', '') or '').strip()
        candidates = [configured] if configured else []
        if os.name == 'nt':
            candidates.extend([shutil.which('claude.cmd'), shutil.which('claude')])
        else:
            candidates.append(shutil.which('claude'))
        for candidate in candidates:
            if candidate and Path(candidate).exists():
                return str(Path(candidate))
        raise FileNotFoundError(
            "Claude Code CLI не найден. Установите Claude Code и войдите в аккаунт, "
            "или укажите CLAUDE_CODE_EXE_PATH в agent/.env"
        )

    @classmethod
    def auth_status(cls, timeout: int = 15) -> dict:
        """Return the local Claude Code subscription status as reported by its CLI."""
        exe = cls._resolve_exe()
        result = subprocess.run(
            [exe, 'auth', 'status'],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=timeout,
            env=os.environ.copy(),
            **NO_WINDOW,
        )
        raw = (result.stdout or result.stderr or '').strip()
        if result.returncode != 0:
            return {'loggedIn': False, 'exe': exe, 'error': raw[:500]}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {'loggedIn': 'loggedin' in raw.lower(), 'raw': raw[:500]}
        payload['exe'] = exe
        return payload

    def _child_env(self) -> dict:
        env = os.environ.copy()
        if getattr(config, 'CLAUDE_CODE_USE_SUBSCRIPTION', True):
            env.pop('ANTHROPIC_API_KEY', None)
        return env

    def run_task_file(
        self,
        task_file: Path,
        working_dir: Path,
        label: str,
        extra_instruction: str = '',
    ) -> tuple[int, str, str, float]:
        prompt = f"Read and follow ALL instructions from this file: {task_file.as_posix()}"
        if extra_instruction:
            prompt += "\n\n" + extra_instruction.strip()

        # Claude Code parses options reliably when they are placed before the
        # positional prompt. If they follow the prompt, Windows/npm invocations
        # can treat them as plain prompt text, leaving Read/Write permissions in
        # interactive mode.
        cmd = [
            self.exe,
            '--output-format', 'json',
            '--model', self.model_name,
            '--permission-mode', self.permission_mode,
            '--dangerously-skip-permissions',
            '--allowedTools', 'Read,Write,Edit,Bash',
            '--add-dir', str(config.BASE_DIR.parent),
            '--max-turns', str(self.max_turns),
            '-p',
            prompt,
        ]

        logging.info("Claude Code: label=%s model=%s cwd=%s", label, self.model_name, working_dir)
        t0 = time.time()
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(working_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                errors='replace',
                env=self._child_env(),
                **NO_WINDOW,
            )
        except FileNotFoundError as e:
            raise FileNotFoundError(
                f"Claude Code CLI не запускается: {self.exe}. "
                f"Проверьте CLAUDE_CODE_EXE_PATH или PATH."
            ) from e

        def _drain(src, dst):
            for line in src:
                dst.append(line.rstrip())

        threading.Thread(target=_drain, args=(proc.stdout, stdout_lines), daemon=True).start()
        threading.Thread(target=_drain, args=(proc.stderr, stderr_lines), daemon=True).start()

        spinner = '|/-\\'
        i = 0
        while proc.poll() is None:
            elapsed = time.time() - t0
            mins, secs = int(elapsed) // 60, int(elapsed) % 60
            t_str = f"{mins}м {secs:02d}с" if mins else f"{secs}с"
            sys.stdout.write(f"\r{spinner[i % len(spinner)]} Claude Code {label}: {t_str}   \n")
            sys.stdout.flush()
            i += 1
            if elapsed > self.timeout:
                proc.kill()
                raise RuntimeError(f"Claude Code timeout after {self.timeout}s ({label})")
            time.sleep(1)

        elapsed = time.time() - t0
        stdout = "\n".join(stdout_lines)
        stderr = "\n".join(stderr_lines)
        safe_label = ''.join(ch if ch.isalnum() or ch in ('-', '_') else '_' for ch in label)[:60] or 'run'
        try:
            (working_dir / f'_claude_{safe_label}_stdout.jsonl').write_text(
                stdout, encoding='utf-8', errors='replace'
            )
            (working_dir / f'_claude_{safe_label}_stderr.txt').write_text(
                stderr, encoding='utf-8', errors='replace'
            )
        except Exception as e:
            logging.warning("Could not save Claude Code raw output: %s", e)
        if proc.returncode != 0:
            logging.error("Claude Code stderr: %s", stderr[-2000:])
        return proc.returncode or 0, stdout, stderr, elapsed


def load_json_file(path: Path) -> dict:
    raw = path.read_text(encoding='utf-8-sig', errors='replace').strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Claude Code вернул невалидный JSON в {path.name}: {e}\n{raw[:800]}")


def extract_result_text(stdout: str) -> str:
    """Extract the assistant result text from Claude Code --output-format json."""
    for line in reversed([l.strip() for l in stdout.splitlines() if l.strip()]):
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        for key in ('result', 'text', 'message', 'content'):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return stdout.strip()


def extract_json_object(text: str) -> dict:
    """Best-effort parse of a JSON object from raw text or a fenced block."""
    text = text.strip()
    if not text:
        raise json.JSONDecodeError("empty", text, 0)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    import re
    fenced = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL | re.IGNORECASE)
    if fenced:
        return json.loads(fenced.group(1))

    start = text.find('{')
    end = text.rfind('}')
    if start >= 0 and end > start:
        return json.loads(text[start:end + 1])
    raise json.JSONDecodeError("no JSON object found", text, 0)
