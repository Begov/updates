"""Запуск одной стадии движка по описанию задания (JSON).

Используется «Панелью управления» BegoFrame: каждая стадия идёт в отдельном
процессе, поэтому переопределённые параметры не «прилипают» к следующим
запускам, а кнопка «Стоп» может остановить стадию целиком.

    python stage_runner.py job.json

Формат задания:
    {"task": "transcribe" | "direct" | "imagegen" | "videogen"
             | "cancel_all" | "cancel_veo",
     "env":    {...},   # переменные окружения до импорта параметров
     "config": {...},   # переопределения атрибутов params.py
     "args":   {...}}   # аргументы задачи
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parent
sys.path.insert(0, str(ENGINE))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _load_job() -> dict:
    if len(sys.argv) < 2:
        raise SystemExit("usage: stage_runner.py job.json")
    return json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))


def _veo_active(selection: str) -> list[int]:
    """«1,3» / «all» -> список номеров ключей."""
    import params as config
    names = ["VEONONSTOP_API_KEY", "VEONONSTOP_API_KEY_2", "VEONONSTOP_API_KEY_3", "VEONONSTOP_API_KEY_4"]
    sel = str(selection or "1")
    if sel in ("all", "both"):
        return [i + 1 for i, n in enumerate(names) if getattr(config, n, "")] or [1]
    try:
        return [int(x) for x in sel.replace("key", "").split(",") if x.strip()]
    except ValueError:
        return [1]


def cancel_server_tasks(only_veononstop: bool = False) -> None:
    """Снять серверные задачи всех провайдеров («Стоп»). Процессы к этому моменту уже убиты.

    • VeoNonStop — все задачи на каждом настроенном ключе;
    • VARAG — все активные задачи ключа (hard-cancel {"all": true}), где бы они ни были запущены;
    • RoyalTechno — задачи из журнала (units/job_journal.py): снять можно только по id.
    У Vertex, CheapVibeCode и CinAI отмены в API нет — их запросы обрываются вместе с процессом.

    `only_veononstop` — «Перезапуск ключа»: только задачи VeoNonStop, генерация продолжается.
    """
    import params as config
    totals: dict = {}

    try:
        from units.veo_client import VeoNonStopClient
        totals["VeoNonStop"] = 0
        for key_attr, base_attr in (("VEONONSTOP_API_KEY", "VEONONSTOP_API_BASE"),
                                    ("VEONONSTOP_API_KEY_2", "VEONONSTOP_API_BASE_2"),
                                    ("VEONONSTOP_API_KEY_3", "VEONONSTOP_API_BASE_3"),
                                    ("VEONONSTOP_API_KEY_4", "VEONONSTOP_API_BASE_4")):
            key = getattr(config, key_attr, "")
            if not key:
                continue
            base = getattr(config, base_attr, "") or config.VEONONSTOP_API_BASE
            try:
                cancelled = VeoNonStopClient(api_key=key, base_url=base).cancel_all_tasks()
                totals["VeoNonStop"] += len(cancelled) if isinstance(cancelled, (list, dict)) else int(cancelled or 0)
            except Exception as exc:  # noqa: BLE001
                print(f"[WARN] VeoNonStop cancel ({key_attr}): {exc}")
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] VeoNonStop: {exc}")

    if not only_veononstop:
        if getattr(config, "VARAG_API_KEY", ""):
            try:
                from units.varag_client import VaragClient
                totals["VARAG"] = VaragClient(api_key=config.VARAG_API_KEY).hard_cancel_all()
            except Exception as exc:  # noqa: BLE001
                print(f"[WARN] VARAG cancel: {exc}")

        from units import job_journal
        rt_ids = job_journal.pending("royaltechno")
        if rt_ids and getattr(config, "ROYALTECHNO_API_KEY", ""):
            try:
                from units.royaltechno_client import RoyalTechnoClient
                client = RoyalTechnoClient(api_key=config.ROYALTECHNO_API_KEY)
                totals["RoyalTechno"] = sum(1 for jid in rt_ids if client.cancel_task(jid))
            except Exception as exc:  # noqa: BLE001
                print(f"[WARN] RoyalTechno cancel: {exc}")
        job_journal.clear("royaltechno")

    print("[OK] Server tasks cancelled: " + ", ".join(f"{name}={n}" for name, n in totals.items()))


def _resolve_single_prompt(project: Path, mode: str):
    if mode != "single":
        return None
    path = project / "single_prompt.txt"
    if path.exists():
        text = path.read_text(encoding="utf-8-sig").strip()
        if text:
            return text
    print("[WARN] single_prompt.txt not found — using 'default' (first prompt)")
    return "default"


def main() -> int:
    job = _load_job()
    for key, value in (job.get("env") or {}).items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = str(value)
    import params as config
    for key, value in (job.get("config") or {}).items():
        setattr(config, key, value)
    task = job.get("task")
    a = job.get("args") or {}

    if task == "cancel_all":
        cancel_server_tasks()
        return 0
    if task == "cancel_veo":
        cancel_server_tasks(only_veononstop=True)
        return 0

    if task == "transcribe":
        from units.speech_to_text import transcribe_audio
        transcribe_audio(a["audio"], a["output"], language_code=a.get("language"))
        return 0

    if task == "direct":
        from units.scene_director import direct_project
        if a.get("archive_old"):
            # новый план не совпадёт с кадрами прошлого — они уходят в old_<дата>/ (подтверждено в Панели)
            from units.media_io import archive_previous_run
            archive_previous_run(a["project_dir"])
        style = a.get("style", "")
        if a.get("adapt_sg") and style:
            # «Адаптировать стайлгайд под сюжет (GPT)» — как в конвейере, перед режиссурой
            from units.styleguide_adapter import adapt_style_guide
            print("  Adapting style guide to current script...")
            style = adapt_style_guide(original_sg_path=style, project_dir=a["project_dir"],
                                      project_name=a.get("name", ""))
        direct_project(a["transcription"], style, a["project_dir"], a.get("name", "Untitled"),
                       two_pass=bool(a.get("two_pass")), prompts_only=bool(a.get("prompts_only")),
                       resume_path=a.get("resume_path"))
        return 0

    if task == "imagegen":
        engine = a.get("engine", "veononstop")
        project = a["project_dir"]
        from units.media_io import quarantine_broken
        quarantine_broken(project)                       # обрезанные прошлыми запусками — сделать заново
        # как в маршруте конвейера: обложка — в generated/ до генерации (её клип не рисуется),
        # интро-гейт инфографики — до генерации (иначе клип не достанется никому)
        import cli
        from units import cover_opener
        try:
            cover_opener.stage_cover_image(project)
        except Exception as exc:  # noqa: BLE001
            print(f"  [Обложка] не подложена: {exc}")
        cli._apply_infographic_intro_gate(project)
        result = None
        if engine == "none":
            pass                                         # картинки «Пропустить» — только инфографика
        elif engine == "veononstop":
            from units.veo_stills import generate_all_images
            ups = {"2k": "UPSAMPLE_IMAGE_RESOLUTION_2K", "4k": "UPSAMPLE_IMAGE_RESOLUTION_4K"}.get(
                str(a.get("upscale", "off")).lower())
            result = generate_all_images(project, mode=a.get("mode", "banana"), ref_dir=a.get("ref_dir") or None,
                                         aspect_ratio=a.get("aspect"), max_parallel=int(a.get("parallel", 4)),
                                         active_keys=_veo_active(a.get("keys", "1")), upscale_resolution=ups,
                                         preflight=not a.get("skip_preflight", False))
        else:
            from units.cinai_stills import generate_all_images
            result = generate_all_images(project, model=a.get("model"), aspect_ratio=a.get("aspect"),
                                         quality=a.get("quality"), max_parallel=max(1, min(6, int(a.get("parallel", 6)))),
                                         silent=True)
        if result is not None:
            print(f"[OK] ImageGen: {result}")
        # инфографика — своим провайдером и моделью из блока «Инфографика»
        info = cli._run_infographic_images(project)
        if info.get("generated") or info.get("failed"):
            print(f"[OK] Infographics: {info}")
        return 0

    if task == "videogen":
        from units.inbox_manager import copy_single_prompt
        from units.media_io import quarantine_broken
        from units.vislide import copy_static, project_animated_ids
        project = Path(a["project_dir"])
        quarantine_broken(project)                       # обрезанные прошлыми запусками — сделать заново
        # общий промпт — файл, выбранный в «Конвейере» сейчас, а не копия первого запуска
        copy_single_prompt(project, a.get("single_prompt_file", ""))
        # список параллакса — по текущим настройкам (как в конвейере), а не прошлого прогона
        if getattr(config, "VEONONSTOP_I2V_PARALLAX", False):
            try:
                from units import parallax_schedule
                parallax_schedule.build(str(project))
            except Exception as exc:  # noqa: BLE001
                print(f"  [Параллакс] список не посчитан: {exc}")
        # вислайд: оживляются только свои клипы, остальные идут слайдами (у t2v картинок нет)
        vislide = (project_animated_ids(project, int(a.get("vislide_interval", 0) or 0))
                   if a.get("mode", "i2v") != "t2v" else None)
        if vislide is not None:
            print(f"[Vislide] {len(vislide)} clips → video, the rest stay slides")
            if not vislide:
                copy_static(project, vislide)
                print("[OK] VideoGen: nothing to animate")
                return 0
        prompts_file = a.get("prompts_file") or None
        if not prompts_file and a.get("codex_adapt") and a.get("mode", "i2v") == "i2v":
            # «Адаптировать промпты под видео (GPT)»: готовая адаптация проекта, иначе новая.
            # Выключено — промпты сцен как есть, даже если адаптация от прошлых запусков лежит.
            adapted = sorted(project.glob("*_veo_adapt_*.txt"), key=lambda f: f.stat().st_mtime)
            if adapted:
                prompts_file = str(adapted[-1])
                print(f"  Using veo_adapt prompts: {adapted[-1].name}")
            else:
                try:
                    from units.codex_motion_adapter import adapt_prompts_for_veo
                    prompts_file = adapt_prompts_for_veo(str(project)) or None
                    print(f"  [codex-adapt] {'done: ' + Path(prompts_file).name if prompts_file else 'empty — original prompts'}")
                except Exception as exc:  # noqa: BLE001
                    print(f"  [codex-adapt] failed: {exc} — original prompts")

    if task == "videogen" and a.get("engine") == "cinai":
        import params as config
        from units.cinai_motion import generate_all_videos as cinai_generate_all_videos
        model = config._CINAI_VIDEO_RENAMED.get(a.get("model"), a.get("model"))
        model = model if model in config.CINAI_VIDEO_MODELS else config.CINAI_VIDEO_MODEL
        result = cinai_generate_all_videos(str(project), mode=a.get("mode", "i2v"),
                                           max_parallel=int(a.get("parallel", 3)), aspect_ratio=a.get("aspect"),
                                           model=model, duration=a.get("duration"), resolution="",
                                           clip_ids=vislide, single_prompt=_resolve_single_prompt(
                                               project, a.get("prompt_mode", "per-clip")),
                                           prompts_file_path=prompts_file, silent=True)
        if vislide:
            copy_static(project, vislide)
        print(f"[OK] VideoGen (CinAI {model}): {result}")
        return 0

    if task == "videogen":
        from units.veo_motion import generate_all_videos
        result = generate_all_videos(str(project), mode=a.get("mode", "i2v"), max_parallel=int(a.get("parallel", 4)),
                                     aspect_ratio=a.get("aspect"), active_keys=_veo_active(a.get("keys", "1")),
                                     single_prompt=_resolve_single_prompt(project, a.get("prompt_mode", "per-clip")),
                                     upscale_video=bool(a.get("upscale")), prompts_file_path=prompts_file,
                                     clip_ids=vislide)
        if vislide:
            copy_static(project, vislide)
        print(f"[OK] VideoGen: {result}")
        return 0

    print(f"[ERROR] unknown task: {task}")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
