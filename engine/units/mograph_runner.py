"""Стадия моушн-графики: Codex пишет сцену Remotion, Remotion рендерит её в видео.

Режиссёр в Pass 1 помечает клипы `source: motion_graphics_pending` и пишет идею
(`mograph_brief`). Для каждого такого клипа:

  1. в runtime/remotion/scenes/<проект>/<id>/ программа кладёт index.tsx и
     config.ts (композиция ровно длины клипа, формат и fps ролика);
  2. Codex (модель MOTION_GRAPHICS_MODEL) пишет рядом Scene.tsx и сам проверяет
     его на кадрах `remotion still`;
  3. стадия рендерит mp4; ошибка рендера уходит Codex на исправление
     (до MOTION_GRAPHICS_FIX_ATTEMPTS раз);
  4. готовое видео → vid_img/{id:03d}.mp4, метка → `motion_graphics`, исходник
     сцены → mograph/{id:03d}.tsx в проекте.

Не получилось — метка снимается, и клип догенерирует Pre-concat fallback по
промпту Pass 2 (промпты пишутся для всех клипов). Пайплайн не встаёт.

Сцены лежат внутри рабочей папки Remotion, а не в проекте: импорт `remotion`
ищет пакеты вверх по дереву папок, из папки проекта он их не найдёт.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

import params as config
from helpers import remotion_setup
from units.proc_tools import NO_WINDOW

logger = logging.getLogger(__name__)

PENDING = 'motion_graphics_pending'
DONE = 'motion_graphics'
COMPOSITION = 'Motion'
ENGINE_DIR = Path(__file__).resolve().parent.parent
SCHEMA = ENGINE_DIR / 'schemas' / 'mograph_control.json'
TEMPLATE = ENGINE_DIR / 'instructions' / 'mograph_scene.txt'
SCENES_DIR = remotion_setup.WORKSPACE / 'scenes'
_ERROR_TAIL = 3000          # сколько символов ошибки рендера показать Codex


class _Stopped(Exception):
    """Пайплайн попросил остановиться."""


# ---------------------------------------------------------------- план и текст
def _load_clips(montage_path: Path) -> tuple[List[Dict[str, Any]], str]:
    """Клипы плана и тема ролика."""
    raw = json.loads(montage_path.read_text(encoding='utf-8-sig'))
    plan = raw.get('montage_plan', raw) if isinstance(raw, dict) else {}
    return [c for c in plan.get('clips', []) if isinstance(c, dict)], str(plan.get('theme') or '')


def _fragments(project: Path) -> List[Dict[str, Any]]:
    try:
        data = json.loads((project / 'transcription.json').read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return []
    raw = data.get('transcription') or data.get('segments') or []
    return [f for f in raw if isinstance(f, dict)]


def _text_between(fragments, start: float, end: float, limit: int = 700) -> str:
    parts = [str(f.get('text', '')) for f in fragments
             if float(f.get('start', 0) or 0) < end and float(f.get('end', 0) or 0) > start]
    return ' '.join(p for p in parts if p).strip()[:limit]


def _style_guide(project: Path) -> Optional[Path]:
    """Стайлгайд, скопированный в проект при запуске (`<имя>_style_<время>.txt`)."""
    found = sorted(project.glob('*_style_*.txt'), key=lambda p: p.stat().st_mtime, reverse=True)
    return found[0] if found else None


def _model() -> str:
    return (getattr(config, 'MOTION_GRAPHICS_MODEL', '') or getattr(config, 'GPT_MODEL', '') or 'gpt-5.4').strip()


# ---------------------------------------------------------------- процессы
def _kill_tree(proc: subprocess.Popen) -> None:
    """Codex запускает дочерние node-процессы (проверочные кадры): гасим всё дерево."""
    if proc.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/T', '/F', '/PID', str(proc.pid)],
                       capture_output=True, **NO_WINDOW)
    else:
        proc.kill()


def _run(cmd: List[str], *, cwd: Path, timeout: int, stop_event: Optional[threading.Event],
         env: Optional[dict] = None) -> tuple[int, str]:
    """Запустить процесс, следя за таймаутом и стоп-кнопкой. (код, вывод)."""
    out_path = cwd / f'_proc_{threading.get_ident()}.log'
    with open(out_path, 'w', encoding='utf-8', errors='replace') as out:
        proc = subprocess.Popen(cmd, cwd=str(cwd), stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=env, **NO_WINDOW)
        deadline = time.time() + timeout
        try:
            while proc.poll() is None:
                if stop_event is not None and stop_event.is_set():
                    _kill_tree(proc)
                    raise _Stopped()
                if time.time() > deadline:
                    _kill_tree(proc)
                    return -1, f'timeout {timeout}s'
                time.sleep(0.5)
        finally:
            if proc.poll() is None:
                _kill_tree(proc)
    text = out_path.read_text(encoding='utf-8', errors='replace')
    out_path.unlink(missing_ok=True)
    return proc.returncode, text


# ---------------------------------------------------------------- сцена
def _scene_dir(project: Path, clip_id: int) -> Path:
    # Имя проекта бывает кириллическим и длинным — папку сцены называем хэшем пути
    tag = hashlib.sha1(str(project.resolve()).encode('utf-8')).hexdigest()[:12]
    return SCENES_DIR / tag / f'{clip_id:03d}'


def _write_entry(scene: Path, frames: int, fps: int, width: int, height: int) -> None:
    scene.mkdir(parents=True, exist_ok=True)
    (scene / 'config.ts').write_text(
        "// Пишет программа: длина и формат клипа. Не редактировать.\n"
        f"export const DURATION_IN_FRAMES = {frames};\n"
        f"export const FPS = {fps};\n"
        f"export const WIDTH = {width};\n"
        f"export const HEIGHT = {height};\n", encoding='utf-8')
    (scene / 'index.tsx').write_text(
        "// Пишет программа: точка входа Remotion. Сцена — в Scene.tsx.\n"
        "import React from 'react';\n"
        "import {Composition, registerRoot} from 'remotion';\n"
        "import {DURATION_IN_FRAMES, FPS, HEIGHT, WIDTH} from './config';\n"
        "import {Scene} from './Scene';\n\n"
        "const Root: React.FC = () => (\n"
        f"  <Composition id=\"{COMPOSITION}\" component={{Scene}} durationInFrames={{DURATION_IN_FRAMES}}\n"
        "    fps={FPS} width={WIDTH} height={HEIGHT} />\n"
        ");\n\n"
        "registerRoot(Root);\n", encoding='utf-8')


def _quote(arg: str) -> str:
    return f'"{arg}"' if (' ' in arg or not arg) else arg


def _task(clip: Dict[str, Any], *, fragments, theme: str, style: Optional[Path],
          frames: int, fps: int, width: int, height: int, fix_error: str = '') -> str:
    from units.infographic_tags import infographic_text_language
    start = float(clip.get('startTime', 0) or 0)
    end = float(clip.get('endTime', 0) or 0)
    code, name = infographic_text_language()
    # Команду выполнит оболочка Codex (на Windows — PowerShell): путь в кавычках первым
    # словом там не запускается, поэтому `node` по имени из PATH
    check = f"node {_quote(str(remotion_setup.CLI_JS))} still index.tsx {COMPOSITION}"
    fix_block = ''
    if fix_error:
        fix_block = ("\n## PREVIOUS RENDER FAILED\nThe program rendered your Scene.tsx and got this error. "
                     "Fix the scene so it renders:\n```\n" + fix_error + "\n```\n")
    values = {
        'clip_id': str(clip.get('id')),
        'brief': str(clip.get('mograph_brief') or '').strip() or '(not given — derive it from the narration)',
        'voiceover': _text_between(fragments, start, end) or '(no narration)',
        'context_before': _text_between(fragments, max(0.0, start - 12), start, 400) or '(start of video)',
        'context_after': _text_between(fragments, end, end + 12, 400) or '(end of video)',
        'theme': theme or '(not given)',
        'width': str(width), 'height': str(height), 'fps': str(fps), 'frames': str(frames),
        'duration_sec': f'{frames / fps:.2f}',
        'mid_frame': str(frames // 2), 'last_frame': str(frames - 1),
        'min_font': str(max(28, round(min(width, height) * 48 / 1080))),
        'min_label': str(max(20, round(min(width, height) * 32 / 1080))),
        'style_guide_path': str(style) if style else '(no style guide: choose a calm, cinematic palette)',
        'language_name': name, 'language_code': code,
        'check_cmd': check,
        'fix_block': fix_block,
    }
    text = TEMPLATE.read_text(encoding='utf-8')
    for key, value in values.items():
        text = text.replace('{' + key + '}', value)
    return text


def _ask_codex(scene: Path, task: str, stop_event) -> Dict[str, Any]:
    """Один вызов Codex: задание в файле (длинный текст в аргументе упирается в лимит Windows)."""
    exe = getattr(config, 'CODEX_EXE_PATH', '') or ''
    if not exe or not Path(exe).exists():
        raise RuntimeError(f'Codex CLI не найден: {exe or "(путь не задан)"}')
    (scene / 'TASK.md').write_text(task, encoding='utf-8')
    control = scene / '_control.json'
    control.unlink(missing_ok=True)
    cmd = [exe, 'exec', 'Read TASK.md in the current folder and complete the task exactly.',
           '--output-schema', str(SCHEMA), '--output-last-message', str(control),
           '--dangerously-bypass-approvals-and-sandbox', '--skip-git-repo-check',
           '-m', _model(), '-C', str(scene)]
    code, out = _run(cmd, cwd=scene, stop_event=stop_event,
                     timeout=int(getattr(config, 'MOTION_GRAPHICS_CODEX_TIMEOUT', 900)),
                     # подписка Codex, как у режиссёра; прокси CODEX_PROXY подхватится
                     env=config.codex_env_for_provider(provider='openai'))
    if not control.exists():
        raise RuntimeError(f'Codex не ответил (код {code}): {out[-400:].strip()}')
    try:
        result = json.loads(control.read_text(encoding='utf-8'))
    except ValueError as exc:
        raise RuntimeError(f'ответ Codex не JSON: {exc}') from exc
    if result.get('status') != 'ok':
        raise RuntimeError(f"Codex: {result.get('error_message') or 'status=error'}")
    if not (scene / 'Scene.tsx').exists():
        raise RuntimeError('Codex не создал Scene.tsx')
    return result


def _render(scene: Path, out_file: Path, stop_event) -> str:
    """Отрендерить сцену. Пустая строка — успех, иначе хвост ошибки для Codex."""
    out_file.unlink(missing_ok=True)
    cmd = remotion_setup.remotion_cmd(
        'render', 'index.tsx', COMPOSITION, out_file.name,
        '--codec=h264', '--muted', '--overwrite', '--log=error',
        f'--concurrency={max(1, min(4, (os.cpu_count() or 4) // 2))}')
    code, out = _run(cmd, cwd=scene, stop_event=stop_event,
                     timeout=int(getattr(config, 'MOTION_GRAPHICS_RENDER_TIMEOUT', 600)))
    if code == 0 and out_file.exists() and out_file.stat().st_size > 1000:
        return ''
    return (out.strip() or f'render exited with code {code}')[-_ERROR_TAIL:]


# ---------------------------------------------------------------- клип
def _make_clip(project: Path, clip: Dict[str, Any], *, fragments, theme: str, style: Optional[Path],
               width: int, height: int, fps: int, stop_event) -> Dict[str, Any]:
    cid = int(clip['id'])
    duration = max(0.1, float(clip.get('endTime', 0) or 0) - float(clip.get('startTime', 0) or 0))
    frames = max(1, int(round(duration * fps)))
    scene = _scene_dir(project, cid)
    _write_entry(scene, frames, fps, width, height)
    out_file = scene / 'out.mp4'
    task_args = dict(fragments=fragments, theme=theme, style=style,
                     frames=frames, fps=fps, width=width, height=height)
    summary, error = '', ''
    # Повторный запуск: сцена уже написана — сначала пробуем её, без нового вызова Codex
    if (scene / 'Scene.tsx').exists():
        error = _render(scene, out_file, stop_event)
        if not error:
            print(f"  [Motion graphics] клип {cid}: взята готовая сцена")
    if not (scene / 'Scene.tsx').exists() or error:
        attempts = 1 + int(getattr(config, 'MOTION_GRAPHICS_FIX_ATTEMPTS', 2))
        for attempt in range(1, attempts + 1):
            print(f"  [Motion graphics] клип {cid}: Codex пишет сцену"
                  + (f" (исправление {attempt - 1})" if error else "") + "…")
            reply = _ask_codex(scene, _task(clip, fix_error=error, **task_args), stop_event)
            summary = str(reply.get('summary') or '')
            error = _render(scene, out_file, stop_event)
            if not error:
                break
            logger.warning("[mograph] клип %s: рендер не удался (попытка %s): %s", cid, attempt, error[-500:])
        if error:
            raise RuntimeError(f'рендер не удался: {error[-300:]}')
    if stop_event is not None and stop_event.is_set():
        raise _Stopped()
    vid_dir = project / 'vid_img'
    vid_dir.mkdir(exist_ok=True)
    target = vid_dir / f'{cid:03d}.mp4'
    shutil.move(str(out_file), str(target))
    # Исходник сцены — в проект: его можно посмотреть и поправить руками
    keep = project / 'mograph'
    keep.mkdir(exist_ok=True)
    shutil.copy2(scene / 'Scene.tsx', keep / f'{cid:03d}.tsx')
    return {'source': DONE, 'file': f'vid_img/{target.name}', 'mograph_summary': summary}


# ---------------------------------------------------------------- стадия
def run_motion_graphics_stage(project_dir, *, target_w: int = 1920, target_h: int = 1080,
                              fps: Optional[int] = None,
                              stop_event: Optional[threading.Event] = None) -> Dict[str, Any]:
    """Сделать графику для клипов `motion_graphics_pending`. Возвращает сводку."""
    from units.source_ledger import apply_clip_source_delta
    from units.stage_journal import attach as _attach_log, detach as _detach_log

    project = Path(project_dir)
    montage_path = project / 'montage_plan.json'
    if not montage_path.exists():
        return {'ok': 0, 'failed': 0, 'total': 0, 'error': 'montage_plan.json не найден'}
    fps = int(fps or getattr(config, 'VIDEO_FPS', 60) or 60)
    clips, theme = _load_clips(montage_path)
    todo = []
    for clip in clips:
        try:
            cid = int(clip.get('id'))
        except (TypeError, ValueError):
            continue
        ready = (project / 'vid_img' / f'{cid:03d}.mp4').exists()
        if clip.get('source') == PENDING or (clip.get('source') == DONE and not ready):
            todo.append(clip)
    if not todo:
        print("  [Motion graphics] режиссёр не пометил ни одного клипа — стадия пропущена")
        return {'ok': 0, 'failed': 0, 'total': 0}

    stage_handler, stage_log = _attach_log(project, 'motion_graphics')
    log = lambda message: print(f"  [Motion graphics] {message}")  # noqa: E731
    try:
        if not remotion_setup.ensure(log):
            # Без Remotion графику не сделать: отдаём клипы нейрогенерации сразу
            apply_clip_source_delta(montage_path, {int(c['id']): {'source': None} for c in todo},
                                    log=log)
            log(f"Remotion не готов — {len(todo)} клип(ов) уйдут в нейрогенерацию")
            return {'ok': 0, 'failed': len(todo), 'total': len(todo), 'error': 'remotion not ready'}

        fragments = _fragments(project)
        style = _style_guide(project)
        workers = int(getattr(config, 'MOTION_GRAPHICS_PARALLEL', 2) or 2)
        log(f"{len(todo)} клип(ов), модель {_model()}, потоков {workers}, "
            f"{target_w}x{target_h} @ {fps} fps")
        result = {'ok': 0, 'failed': 0, 'total': len(todo)}
        lock = threading.Lock()

        def work(clip):
            cid = int(clip['id'])
            if stop_event is not None and stop_event.is_set():
                return
            started = time.time()
            try:
                patch = _make_clip(project, clip, fragments=fragments, theme=theme, style=style,
                                   width=target_w, height=target_h, fps=fps, stop_event=stop_event)
            except _Stopped:
                log(f"клип {cid}: остановлено — клип догенерирует нейросеть")
                return
            except Exception as exc:                          # noqa: BLE001
                logger.exception("[mograph] клип %s не удался", cid)
                log(f"клип {cid}: не вышло ({exc}) → нейрогенерация")
                apply_clip_source_delta(montage_path, {cid: {'source': None, 'mograph_failed': str(exc)[:300]}},
                                        log=log)
                with lock:
                    result['failed'] += 1
                return
            apply_clip_source_delta(montage_path, {cid: patch}, log=log)
            log(f"клип {cid}: готово за {time.time() - started:.0f} с — {patch.get('mograph_summary') or ''}")
            with lock:
                result['ok'] += 1

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='mograph') as pool:
            list(pool.map(work, todo))
        log(f"итог: {result['ok']} готово, {result['failed']} → нейрогенерация")
        return result
    finally:
        if stage_log:
            logger.info("[mograph] лог стадии: %s", stage_log)
        _detach_log(stage_handler)
