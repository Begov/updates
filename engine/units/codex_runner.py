"""Запуск Codex CLI с выходной схемой — общий для всех стадий, которым нужен Codex
(вижн-отбор стока, промптеры через operator_hub).

Один семафор на все вызовы `codex exec` в процессе (CODEX_MAX_PARALLEL), общий
вердикт о прокси (codex_netproxy), повторы при обрыве транспорта.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import params as config
from units import codex_netproxy as codex_proxy

logger = logging.getLogger(__name__)

# Suppress subprocess windows on Windows
if platform.system() == 'Windows':
    SUBPROCESS_FLAGS = {'creationflags': 0x08000000}
else:
    SUBPROCESS_FLAGS = {}

# Codex defaults
CODEX_TIMEOUT = 600  # seconds for selector


def _prepare_codex_env() -> tuple[dict, str, bool]:
    """Окружение Codex с единым на процесс вердиктом о прокси (codex_proxy)."""
    return codex_proxy.prepare()


def _codex_parallel_limit() -> int:
    """Сколько вызовов Codex CLI разрешено одновременно."""
    try:
        return max(1, min(16, int(getattr(config, 'CODEX_MAX_PARALLEL', 4) or 4)))
    except (TypeError, ValueError):
        return 4


# Один семафор на ВСЕ вызовы `codex exec` в процессе. Промптеры стадий,
# вижн и адаптеры дёргают Codex независимо
# друг от друга — без ограничения их набегает больше десятка, и вызовы начинают
# упираться в таймаут (прогон 26.08: промптер стока получил «Codex timed out
# after 180s», а в одиночку тот же запрос отвечает за 21с).
# Лимит настраивается `CODEX_MAX_PARALLEL`; очередь честная — кто пришёл раньше,
# тот раньше и запустится.
_CODEX_SLOTS = threading.Semaphore(_codex_parallel_limit())


class CodexRunner:
    """Wraps codex CLI invocation with output schema."""

    def __init__(self, model_name: Optional[str] = None, timeout: int = CODEX_TIMEOUT):
        self.model_name = model_name or getattr(config, 'GPT_MODEL', 'gpt-5.4')
        self.timeout = timeout
        # Resolve codex executable path (matches gpt_director.py logic)
        self.codex_exe = self._find_codex_exe()
        self.schemas_dir = Path(__file__).parent.parent / 'schemas'

    def _find_codex_exe(self) -> Path:
        """Find codex CLI executable.

        Единый источник правды — config.CODEX_EXE_PATH (он уже резолвит платформу:
        win32 → Codex/codex-x86_64-pc-windows-msvc.exe, macOS/Linux → which('codex'),
        плюс явный override через CODEX_EXE_PATH в .env).

        Раньше здесь был свой список кандидатов, который ИГНОРИРОВАЛ config: первым
        всегда шёл ВИНДОВЫЙ .exe и проверялся только на .exists(). В папке Codex/
        лежит именно виндовый бинарь (маководы ставят codex через brew — см.
        Codex/README.txt), поэтому на macOS находился .exe и запуск падал с
        [Errno 13] Permission denied — при том что Pass 1/Pass 2 на той же машине
        работали, т.к. gpt_director берёт путь из config.
        Бьёт по всем потребителям CodexRunner: Style Extract + Real Photo.
        """
        # 1) Общий резолвер конфига — то же, что использует gpt_director/sg_adapter/etc.
        cfg_path = getattr(config, 'CODEX_EXE_PATH', '') or ''
        if cfg_path and Path(cfg_path).is_file():
            return Path(cfg_path)

        # 2) Фолбэк: кандидаты СТРОГО под текущую платформу (не берём чужой бинарь)
        root = Path(__file__).parent.parent.parent
        if sys.platform == 'win32':
            candidates = [root / 'runtime' / 'codex' / 'codex-x86_64-pc-windows-msvc.exe']
        else:
            candidates = [root / 'runtime' / 'codex' / 'codex']
        candidates.append(Path(shutil.which('codex') or ''))

        for c in candidates:
            if not c or not c.is_file():
                continue
            # На POSIX бинарь без флага исполнения запускать нельзя — пропускаем,
            # иначе получаем Permission denied вместо понятной ошибки.
            if os.name != 'nt' and not os.access(str(c), os.X_OK):
                continue
            return c

        raise FileNotFoundError(
            f"Codex executable not found (platform={sys.platform}). "
            f"config.CODEX_EXE_PATH='{cfg_path}'. Looked in: "
            + ', '.join(str(c) for c in candidates if c)
        )

    def run(
        self,
        task_prompt: str,
        schema_file: str,
        result_file: Path,
        working_dir: Path,
        images: Optional[List[Path]] = None,
    ) -> Dict[str, Any]:
        """Run codex exec with schema. Returns parsed control dict.

        `images` прикладывает файлы к промпту через `codex exec -i` — так вижн
        работает на GPT-подписке, а не только через HTTP chat-провайдера
        (решение пользователя 25.08: «вижн можешь юзать на гпт подписке»).
        """
        schema_path = self.schemas_dir / schema_file
        if not schema_path.exists():
            raise FileNotFoundError(f"Schema not found: {schema_path}")

        if result_file.exists():
            result_file.unlink()

        # Write task prompt to file (avoid Windows MAX_PATH limits)
        task_file = working_dir / f'_codex_{schema_file.replace(".json", "")}_task.md'
        task_file.write_text(task_prompt, encoding='utf-8')
        short_prompt = f"Read and follow ALL instructions from this file: {task_file.as_posix()}"

        image_args: List[str] = []
        for image in (images or []):
            image = Path(image)
            if image.is_file():
                image_args += ['-i', str(image)]

        cmd = [
            str(self.codex_exe), 'exec',
            short_prompt,
            *image_args,
            '--output-schema', str(schema_path),
            '--output-last-message', str(result_file),
            '--dangerously-bypass-approvals-and-sandbox',
            '--skip-git-repo-check',
            *config.codex_cli_args_for_provider(),
            '-m', self.model_name,
            '-C', str(working_dir),
        ]

        logger.info(f"Codex exec: model={self.model_name} schema={schema_file} cwd={working_dir.name}"
                    + (f" images={len(image_args) // 2}" if image_args else ""))
        run_env, proxy_fallback_reason, _ = _prepare_codex_env()
        if proxy_fallback_reason:
            logger.warning(
                "Codex proxy unavailable; Real Photo selector runs directly (%s)",
                proxy_fallback_reason,
            )

        # Одиночный запуск без повтора стоил прогону целой сцены. Улов 27.08:
        # четыре клипа стартовали в одну миллисекунду, три Codex отработали за
        # 24 секунды, а четвёртый упал за 6.7с с rc=1 — и стадия молча ушла на
        # механический фолбэк, который выдал запрос из технических слов промпта.
        # Отказ разовый и лечится простым повтором.
        attempts = max(1, int(getattr(config, 'CODEX_RETRIES', 2) or 1))
        problem = ''
        for attempt in range(1, attempts + 1):
            result_file.unlink(missing_ok=True)
            t0 = time.time()
            _waited = time.time()
            # Ждём свободный слот: одновременных `codex exec` не больше
            # CODEX_MAX_PARALLEL, иначе вызовы душат друг друга таймаутами.
            _CODEX_SLOTS.acquire()
            _queued = time.time() - _waited
            if _queued > 1.0:
                logger.info("Codex: ждал очереди %.1fс (лимит %d параллельных)",
                            _queued, _codex_parallel_limit())
            try:
                p = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                    timeout=self.timeout,
                    env=run_env,
                    **SUBPROCESS_FLAGS,
                )
            except subprocess.TimeoutExpired:
                raise TimeoutError(f"Codex timed out after {self.timeout}s")
            finally:
                # Слот отдаём в ЛЮБОМ случае, включая таймаут и падение — иначе
                # одна зависшая задача навсегда съест место в очереди.
                _CODEX_SLOTS.release()

            elapsed = time.time() - t0
            logger.info(f"Codex finished in {elapsed:.1f}s, rc={p.returncode}")

            # Понятный хинт: выбранная модель новее установленного Codex CLI
            # (OpenAI отвечает 400 "requires a newer version of Codex").
            _combined = ((p.stderr or '') + '\n' + (p.stdout or '')).lower()
            if 'requires a newer version of codex' in _combined or 'upgrade to the latest' in _combined:
                _msg = (
                    f"⛔ Модель '{self.model_name}' требует более новую версию Codex CLI.\n"
                    f"   РЕШЕНИЕ: вкладка Settings → кнопка «Update Codex CLI», "
                    f"либо выберите gpt-5.4 / gpt-5.5."
                )
                print(f"\n{_msg}\n", flush=True)
                logger.error(f"Codex model too new for CLI: {self.model_name}")
                raise RuntimeError(_msg)

            if result_file.exists():
                break

            # НАЧАЛО stderr, а не только хвост: у Codex в хвосте стоит баннер
            # («provider: openai, approval: never, sandbox: …»), и прежний
            # `stderr[-500:]` показывал в логе именно его — сообщение об ошибке
            # оставалось невидимым, и разбираться было не с чем (улов 27.08).
            _err = (p.stderr or '').strip()
            _out = (p.stdout or '').strip()
            problem = (
                f"rc={p.returncode}\n"
                f"stderr начало: {_err[:400]}\n"
                f"stderr конец: {_err[-300:]}\n"
                f"stdout конец: {_out[-200:]}"
            )
            if attempt < attempts:
                logger.warning(
                    "Codex не создал %s (попытка %s из %s, %.1fс) — повторяю. %s",
                    result_file.name, attempt, attempts, elapsed, problem)
                time.sleep(3.0)

        if not result_file.exists():
            raise RuntimeError(
                f"Codex did not produce result file {result_file.name} "
                f"за {attempts} попыт(ку/ки).\n{problem}"
            )

        with open(result_file, 'r', encoding='utf-8') as f:
            return json.load(f)
