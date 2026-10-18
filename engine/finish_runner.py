"""Довести проект до конца, начиная с выбранной стадии («Довести проект» в Панели управления).

    python finish_runner.py settings.json

settings.json:
    {"project": "<папка проекта>", "stages": ["Direction", "Generate", "Concat"], ...}

Стадии: Direction | Prompts Only | Generate | Concat.
«Generate» — тот же маршрут, что у конвейера после режиссуры (`cli._run_post_pipeline_silent`):
источники кадра, картинки, инфографика, видео, добор пустых сцен. Провайдеры, модели и
потоки приходят окружением запуска — тем же, что у конвейера.
Настройки первого запуска проекта (pipeline_config.json) восстанавливаются в
окружение, чтобы вислайд, длина интро и т.п. не терялись при возобновлении.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ENGINE = Path(__file__).resolve().parent
sys.path.insert(0, str(ENGINE))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import params as config  # noqa: E402
from helpers.concat_guard import load_project_config, wait_ready  # noqa: E402

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def say(text: str) -> None:
    print(text, flush=True)


def restore_project_env(project: Path) -> dict:
    """Вернуть в окружение настройки первого запуска проекта."""
    cfg = load_project_config(project)
    if not cfg:
        return {}
    if cfg.get("clip_mode") == "vislide" and int(cfg.get("vislide_interval", 0) or 0) >= 2:
        os.environ["BEGO_VISLIDE_INTERVAL"] = str(int(cfg["vislide_interval"]))
    for key, env in (("intro_block_duration", "INTRO_BLOCK_DURATION"), ("clip_min", "VIDEO_CLIP_MIN_DURATION"),
                     ("clip_max", "VIDEO_CLIP_MAX_DURATION")):
        if cfg.get(key):
            os.environ[env] = str(int(cfg[key]))
            setattr(config, env, int(cfg[key]))
    return cfg


def run_direction(project: Path, s: dict, prompts_only: bool) -> None:
    from units.media_io import archive_previous_run
    from units.scene_director import direct_project
    tr_path = project / "transcription.json"
    style = str(s.get("style_path") or "")
    if not style or not Path(style).is_file():
        raise RuntimeError("нет стайлгайда: выберите его в «Конвейере» (один на все)")
    config.GPT_MODEL = s.get("dir_gpt_model", config.GPT_MODEL)
    config.GPT_PASS2_CHUNK_SIZE = int(s.get("dir_gpt_chunk_size") or 0)
    if s.get("dir_gpt_parallel"):
        config.GPT_PASS2_PARALLEL = int(s["dir_gpt_parallel"])
    two_pass = str(s.get("dir_twopass", "false")).lower() in ("true", "1", "yes")
    if not prompts_only:
        if s.get("dir_clipmode") == "video":
            config.VIDEO_CLIP_MIN_DURATION = int(s["dir_clipmin"])
            config.VIDEO_CLIP_MAX_DURATION = int(s["dir_clipmax"])
        else:
            config.SLIDESHOW_CLIP_MIN_DURATION = int(s["dir_clipmin"])
            config.SLIDESHOW_CLIP_MAX_DURATION = int(s["dir_clipmax"])
        config.INTRO_BLOCK_DURATION = int(s["dir_intro"])
        config.INTRO_CLIP_MIN_DURATION = int(s["dir_intro_min"])
        config.INTRO_CLIP_MAX_DURATION = int(s["dir_intro_max"])
        if s.get("dir_clipmode") == "vislide":
            interval = int(s.get("dir_vislide_interval", 0) or 0)
            if interval >= 2:
                os.environ["BEGO_VISLIDE_INTERVAL"] = str(interval)
            cfg_path = project / "pipeline_config.json"
            cfg = load_project_config(project)
            cfg.update(clip_mode="vislide", vislide_interval=interval, intro_block_duration=int(s["dir_intro"]),
                       clip_min=int(s["dir_clipmin"]), clip_max=int(s["dir_clipmax"]))
            cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        else:
            os.environ.pop("BEGO_VISLIDE_INTERVAL", None)
    if not prompts_only:
        # план новый — картинки и видео прошлого плана ему не принадлежат: в old_<дата>/
        archive_previous_run(project, say)
    if s.get("dir_adapt_sg") and style:
        from units.styleguide_adapter import adapt_style_guide
        say(f"SG Adapt: {project.name}…")
        style = adapt_style_guide(original_sg_path=style, project_dir=str(project), project_name=project.name)
    direct_project(str(tr_path), style, str(project), project.name, two_pass=two_pass, prompts_only=prompts_only)


def run_generate(project: Path, s: dict) -> None:
    """Маршрут конвейера после режиссуры — с последними настройками «Конвейера»."""
    import cli
    from units.inbox_manager import copy_single_prompt
    copy_single_prompt(project, s.get("single_prompt_file", ""))
    cli._run_post_pipeline_silent(str(project), s.get("post_action") or "skip",
                                  s.get("prompt_mode") or "perclip",
                                  use_codex_adapt=bool(s.get("codex_adapt")))


def run_concat(project: Path, s: dict) -> None:
    wait_ready(project, lambda m, _l="INFO": say(m), terminal=True)
    # готовая команда с настройками «Конвейера» (переходы, зум, субтитры, логотип, инфографика и т.д.)
    cmd = list(s["concat_cmd"])
    proc = subprocess.Popen(cmd, cwd=str(project), env=dict(os.environ, PYTHONIOENCODING="utf-8"),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace", creationflags=CREATE_NO_WINDOW)
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            say(line)
    if proc.wait() != 0:
        raise RuntimeError(f"Concat failed (exit {proc.returncode})")


def main() -> int:
    if len(sys.argv) < 2:
        raise SystemExit("usage: finish_runner.py settings.json")
    s = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    project = Path(s["project"])
    stages = list(s.get("stages") or [])
    master = project / "pipeline_full.log"
    started = time.time()
    cfg = restore_project_env(project)
    if cfg:
        say(f"Project config restored: clip_mode={cfg.get('clip_mode', '?')}, "
            f"intro_block={cfg.get('intro_block_duration', 0)}s")
    with open(master, "a", encoding="utf-8") as fh:
        fh.write(f"\n{'=' * 60}\n=== FINALIZE | {dt.datetime.now()} ===\nStages: {', '.join(stages)}\n{'=' * 60}\n")
    # обрезанные прошлыми запусками файлы (убитый процесс, сбой) — отложить и сделать заново
    from units.media_io import quarantine_broken
    if quarantine_broken(project, say) and "Generate" not in stages and "Direction" not in stages:
        stages.insert(stages.index("Concat") if "Concat" in stages else len(stages), "Generate")
        say(f"Stages: {', '.join(stages)}")
    current = "(init)"
    try:
        for stage in stages:
            current = stage
            t0 = time.time()
            say(f"── Finalize: {stage} ({project.name}) ──")
            if stage in ("Direction", "Prompts Only"):
                run_direction(project, s, stage == "Prompts Only")
            elif stage == "Generate":
                run_generate(project, s)
            elif stage == "Concat":
                run_concat(project, s)
            m, sec = divmod(int(time.time() - t0), 60)
            say(f"[OK] Stage {stage}: done ({m}m {sec}s)")
        m, sec = divmod(int(time.time() - started), 60)
        say(f"[OK] Finalize complete: {project.name} ({m}m {sec}s)")
        return 0
    except Exception as exc:  # noqa: BLE001
        say(f"[ERROR] Finalize FAILED at '{current}': {exc}")
        with open(master, "a", encoding="utf-8") as fh:
            fh.write(f"\n=== FINALIZE ERROR at '{current}': {exc} ===\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
