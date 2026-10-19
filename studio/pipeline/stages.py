"""Запуск стадий вне очереди конвейера («Отдельные стадии» и «Довести проект»).

Все настройки берутся из ПОСЛЕДНИХ значений формы «Конвейер»: ни диалогов, ни
собственных копий параметров. Окружение движка строится той же функцией
`runplan.common_env`, что и для очереди конвейера, поэтому стадии ведут себя так же,
как внутри конвейера.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import NamedTuple

from .. import catalog, engine_link, paths  # noqa: F401 — engine_link кладёт engine/ в sys.path
from ..i18n import T, tr
from .defaults import image_provider
from units.source_ledger import VIDEO_SKIP_SOURCES, clip_media_ready    # engine в sys.path — см. engine_link
from .runplan import apply_env, common_env, concat_command, concat_section, post_action_for, stage_env

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")


# ------------------------------------------------------------------ значения
def values_of(app) -> dict:
    """Текущие значения формы «Конвейер» (страница создаётся при необходимости)."""
    return copy.deepcopy(app.page("pipeline").store.values)


def job_env(v: dict) -> dict:
    """Окружение для `stage_runner` (значение None — убрать переменную)."""
    return common_env(v, post_action_for(v), "control")


def process_env(v: dict) -> dict:
    """Полное окружение для процесса, запускаемого страницей «Панель управления»."""
    env = dict(os.environ)
    env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1", "TQDM_DISABLE": "1"})
    return apply_env(env, job_env(v))


def is_routed_image(v: dict) -> bool:
    """Картинки делает свой клиент (Vertex, CheapVibeCode, VARAG) на маршруте VeoNonStop."""
    return bool(image_provider(v))


def image_workers(v: dict) -> int:
    key = {"vertex": "vertex_image_parallel", "cheapvibe": "cheapvibe_parallel",
           "varag": "varag_image_parallel"}.get(image_provider(v), "veo_imggen_parallel")
    return int(v[key])


def own_video(v: dict) -> bool:
    """Видео делает свой клиент (Vertex, RoyalTechno, VARAG): без ключей-слотов и апскейла VeoNonStop."""
    return v.get("video_provider") in catalog.OWN_VIDEO_PROVIDERS


def video_workers(v: dict) -> int:
    key = {"vertex": "vertex_video_parallel", "royaltechno": "royaltechno_video_parallel",
           "varag": "varag_video_parallel"}.get(v.get("video_provider"), "veo_parallel")
    return int(v[key])


def video_mode_of(v: dict) -> str | None:
    """Режим видео для движка; None — в «Конвейере» видео выключено."""
    mode = v.get("video_mode") or "i2v"
    if mode == "skip":
        return None
    return mode


# ------------------------------------------------------- общие решения
def style_guide_for(v: dict, project: Path) -> str:
    """Стайлгайд режиссуры: «один на все» из «Конвейера», иначе копия, с которой создан проект.

    Копию кладёт конвейер при создании проекта (`<проект>_style_<время>.txt`). Другие .txt
    проекта (промпты, журналы, single_prompt.txt) стайлгайдом не считаются. '' — его нет.
    """
    path = str(v.get("sg_global_path") or "")
    if v.get("sg_mode") == "one_for_all" and path and Path(path).is_file():
        return path
    copies = sorted((f for f in project.glob("*_style_*.txt") if f.is_file()), key=lambda f: f.stat().st_mtime)
    return str(copies[-1]) if copies else ""


def single_prompt_file(v: dict) -> str:
    """Файл общего промпта видео из «Конвейера» ('' — режим «Свой для сцены» или файл не выбран)."""
    path = str(v.get("single_prompt_path") or "")
    return path if v.get("prompt_mode") == "single" and path and Path(path).is_file() else ""


def codex_adapt(v: dict) -> bool:
    """«Адаптировать промпты под видео (GPT)» — как в конвейере, только для промптов сцен."""
    return bool(v.get("codex_adapt_var")) and v.get("prompt_mode") == "perclip"


def infographics_on(v: dict) -> bool:
    """Инфографика включена и ей отдана доля (то же правило, что в окружении запуска)."""
    return stage_env(v).get("INFOGRAPHIC_ENABLED") == "true"


# ------------------------------------------------------- отдельные стадии
def transcribe_job(v: dict, audio: Path, output: Path) -> tuple[dict, dict]:
    # Модели распознавания — как в конвейере (из настроек движка, с запасной моделью)
    args = {"audio": str(audio), "output": str(output), "language": v["language"]}
    return args, {"ASSEMBLYAI_LANGUAGE_CODE": v["language"]}


def direct_job(v: dict, project: Path) -> tuple[dict, dict] | None:
    """Аргументы стадии «Режиссура»; None — стайлгайда нет (конвейер без него не режиссирует)."""
    style = style_guide_for(v, project)
    if not style:
        return None
    two_pass = v["direction_mode"] == "2-pass"
    conf = {"GPT_MODEL": v["gpt_model"],
            "DIRECTOR_TWO_PASS": two_pass, "INTRO_BLOCK_DURATION": int(v["intro_duration"]),
            "INTRO_CLIP_MIN_DURATION": int(v["intro_clip_min"]), "INTRO_CLIP_MAX_DURATION": int(v["intro_clip_max"])}
    if v["clip_mode"] == "slideshow":
        conf.update(SLIDESHOW_CLIP_MIN_DURATION=int(v["clip_min"]), SLIDESHOW_CLIP_MAX_DURATION=int(v["clip_max"]))
    else:
        conf.update(VIDEO_CLIP_MIN_DURATION=int(v["clip_min"]), VIDEO_CLIP_MAX_DURATION=int(v["clip_max"]))
    args = {"transcription": str(project / "transcription.json"), "style": style, "project_dir": str(project),
            "name": project.name, "two_pass": two_pass, "prompts_only": False, "resume_path": None,
            "adapt_sg": bool(v.get("sg_adapt"))}
    return args, conf


def imagegen_job(v: dict, project: Path) -> tuple[dict, dict] | None:
    """Аргументы стадии «Картинки»: кадры сцен и инфографика (как в конвейере).

    None — делать нечего: картинки «Пропустить» и инфографика выключена.
    """
    ig = v["img_gen"]
    if ig == "skip" and not infographics_on(v):
        return None
    conf = {"REWRITE_MODEL": v["rewrite_model"]}
    if ig == "skip":                                    # только инфографика
        args = {"engine": "none", "project_dir": str(project)}
    elif ig == "cinai":
        args = {"engine": "cinai", "project_dir": str(project), "model": v["cinai_model"], "aspect": v["veo_ratio"],
                "quality": v["cinai_quality"], "parallel": int(v["cinai_parallel"])}
    else:                                               # VeoNonStop, Vertex, CheapVibeCode, VARAG — один маршрут движка
        mode = "banana"                                 # у VeoNonStop только Banana
        conf["VEONONSTOP_BANANA_MODEL"] = v["veo_banana_model"]
        args = {"engine": "veononstop", "project_dir": str(project), "mode": mode, "ref_dir": "",
                "aspect": v["veo_ratio"], "parallel": image_workers(v), "keys": str(v["veo_imggen_key"]),
                "upscale": "off" if is_routed_image(v) else v["image_upscale"],
                "skip_preflight": bool(v["skip_preflight"])}
    return args, conf


def videogen_job(v: dict, project: Path) -> tuple[dict, dict] | None:
    """Аргументы стадии «Видео»; None — в «Конвейере» видео выключено."""
    mode = video_mode_of(v)
    if mode is None:
        return None
    args = {"project_dir": str(project), "mode": mode, "parallel": video_workers(v), "aspect": v["veo_ratio"],
            "keys": str(v["veo_video_key"]), "prompt_mode": "single" if v["prompt_mode"] == "single" else "per-clip",
            "single_prompt_file": single_prompt_file(v), "codex_adapt": codex_adapt(v),
            "prompts_file": "", "upscale": bool(v["video_upscale"]) and not own_video(v),
            # вислайд: оживляются только свои клипы, остальные идут слайдами
            "vislide_interval": int(v["vislide_interval"]) if v.get("clip_mode") == "vislide" else 0}
    if v.get("video_provider") == "cinai":
        args.update(engine="cinai", model=v.get("cinai_video_model") or catalog.CINAI_VIDEO_MODELS[0][0],
                    duration=max(3, int(v.get("clip_max", 8))), parallel=_cinai_parallel(v), upscale=False)
    return args, {}


def _cinai_parallel(v: dict) -> int:
    return max(1, min(6, int(v.get("cinai_video_parallel", 3))))


def concat_cmd(v: dict, project: Path, python: str | None = None) -> list[str]:
    """Команда склейки проекта с настройками «Конвейера»."""
    section = concat_section(v, str(v.get("logo_path") or ""))
    return concat_command(project, section, python or paths.python_exe())


# ------------------------------------------------------- «Довести проект»
VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".webm") + IMAGE_EXTS        # как у движка (`clip_media_path`)
MIN_VIDEO_BYTES = 1000


class Resume(NamedTuple):
    """Что делать с проектом при «Довести проект»."""
    stages: list          # стадии для `finish_runner` (пусто — делать нечего)
    notes: list           # пояснения для журнала: что найдено и почему выбраны эти стадии
    blocked: bool = False  # True — продолжить нельзя (нет расшифровки), notes[0] — причина


def _clip_ids(directory: Path, exts, min_bytes: int = 1, parts: bool = False) -> set[int]:
    """Номера клипов, для которых в папке есть непустой файл `NNN.ext` (и куски `NNN_k.ext`)."""
    found: set[int] = set()
    if not directory.is_dir():
        return found
    for f in directory.iterdir():
        if f.suffix.lower() not in exts:
            continue
        head = f.stem.split("_")[0] if parts else f.stem
        if not head.isdigit() or (not parts and f.stem != head):
            continue
        try:
            if f.is_file() and f.stat().st_size >= min_bytes:
                found.add(int(head))
        except OSError:
            continue
    return found


def _plan_clips(project: Path) -> list | None:
    try:
        plan = json.loads((project / "montage_plan.json").read_text(encoding="utf-8-sig"))
        clips = plan.get("montage_plan", plan).get("clips")
        return clips if isinstance(clips, list) and clips else None
    except (OSError, ValueError, AttributeError):
        return None


def resume_plan(project: Path, v: dict) -> Resume:
    """Определить по файлам проекта, с какой стадии его продолжать.

    Режиссура → генерация → склейка. «Генерация» — тот же маршрут, что у конвейера после
    режиссуры: источники кадра (сток, инфографика, моушн-графика, обложка), картинки, видео,
    добор пустых сцен. Она нужна, если у какого-то клипа нет своего материала: у внешнего
    источника — файла, у нейрокадра — картинки (когда картинки включены) или видео (когда
    включено видео). Готовое маршрут не пересоздаёт.
    """
    clips = _plan_clips(project)
    if clips is None:
        if not (project / "transcription.json").exists():
            return Resume([], [tr(T("нет расшифровки (transcription.json) — сначала запустите «Расшифровка»",
                                    "no transcript (transcription.json) — run “Transcribe” first"))], True)
        if not style_guide_for(v, project):
            return Resume([], [tr(T("нет стайлгайда: выберите его в «Конвейере» (один на все) — без него режиссуры нет",
                                    "no style guide: pick one in the Pipeline (one for all) — direction needs it"))],
                          True)
        note = (tr(T("монтажный план повреждён или пуст — режиссура заново",
                     "the montage plan is damaged or empty — direction again"))
                if (project / "montage_plan.json").exists()
                else tr(T("монтажного плана ещё нет — начинаю с режиссуры",
                          "no montage plan yet — starting from direction")))
        return Resume(["Direction", "Generate", "Concat"], [note])

    notes: list[str] = []
    by_id = {int(c["id"]): c for c in clips if str(c.get("id", "")).lstrip("-").isdigit()}
    src = {i: str(c.get("source") or "") for i, c in by_id.items()}

    # внешние источники и инфографика: у клипа должен быть свой файл
    ext_missing = [i for i, c in by_id.items()
                   if src[i] in VIDEO_SKIP_SOURCES and not clip_media_ready(project, c)]
    # нейрокадры: картинка (если картинки не «Пропустить») и видео (если видео включено)
    ai_ids = [i for i in by_id if src[i] not in VIDEO_SKIP_SOURCES]
    miss_img = []
    if v["img_gen"] != "skip":
        have_img = _clip_ids(project / "generated", IMAGE_EXTS)
        miss_img = [i for i in ai_ids if i not in have_img]
    miss_vid = []
    if video_mode_of(v) is not None:
        vid_ids = ai_ids
        if v.get("clip_mode") == "vislide" and video_mode_of(v) != "t2v":
            # вислайд: видео нужно только оживляемым клипам, остальные — слайды
            from units.vislide import animated_ids
            animated = set(animated_ids(clips, int(v.get("vislide_interval", 3) or 3),
                                        int(v.get("intro_duration", 0) or 0)))
            vid_ids = [i for i in ai_ids if i in animated]
        have_vid = _clip_ids(project / "vid_img", VIDEO_EXTS, MIN_VIDEO_BYTES, parts=True)
        miss_vid = [i for i in vid_ids if i not in have_vid]

    if ext_missing:
        kinds = sorted({src[i] for i in ext_missing})
        notes.append(tr(T(f"без материала клипов источников (сток, инфографика, графика, обложка): {len(ext_missing)} "
                          f"({', '.join(kinds)})",
                          f"source clips without media (stock, infographics, graphics, cover): {len(ext_missing)} "
                          f"({', '.join(kinds)})")))
    if miss_img:
        notes.append(tr(T(f"картинок нейрокадров не хватает: {len(miss_img)} из {len(ai_ids)}",
                          f"AI frame images missing: {len(miss_img)} of {len(ai_ids)}")))
    if miss_vid:
        notes.append(tr(T(f"видео не хватает: {len(miss_vid)}", f"videos missing: {len(miss_vid)}")))
    if ext_missing or miss_img or miss_vid:
        notes.append(tr(T("догенерирую недостающее маршрутом конвейера, затем склейка",
                          "generating what is missing via the pipeline route, then assembly")))
        return Resume(["Generate", "Concat"], notes)
    mp4 = project / f"{project.name}.mp4"
    if not mp4.exists():
        return Resume(["Concat"], [tr(T("материал всех клипов готов — собираю ролик",
                                        "media for every clip is ready — assembling the video"))])
    return Resume([], [tr(T(f"проект уже готов: {mp4.name}. Чтобы собрать заново, нажмите «Склейка»",
                            f"the project is already complete: {mp4.name}. Press “Concat” to assemble again"))])


def finish_settings(v: dict, project: Path, stages: list[str]) -> dict:
    """Настройки для `finish_runner.py`. Остальное (провайдеры, модели, потоки, источники кадра)
    приходит окружением запуска — тем же `common_env`, что у конвейера."""
    return {
        "project": str(project), "stages": stages,
        # режиссура
        "dir_gpt_model": v["gpt_model"],
        "dir_gpt_chunk_size": int(v["gpt_chunk_size"]) if v.get("pass2_chunk_enabled") else 0,
        "dir_gpt_parallel": int(v["gpt_parallel"]), "dir_twopass": v["direction_mode"] == "2-pass",
        "dir_clipmode": v["clip_mode"], "dir_vislide_interval": int(v["vislide_interval"]),
        "dir_clipmin": int(v["clip_min"]), "dir_clipmax": int(v["clip_max"]),
        "dir_intro": int(v["intro_duration"]), "dir_intro_min": int(v["intro_clip_min"]),
        "dir_intro_max": int(v["intro_clip_max"]), "dir_adapt_sg": bool(v.get("sg_adapt")),
        "style_path": style_guide_for(v, project),
        # генерация — маршрут конвейера после режиссуры (`cli._run_post_pipeline_silent`)
        "post_action": post_action_for(v), "prompt_mode": v["prompt_mode"], "codex_adapt": codex_adapt(v),
        "single_prompt_file": single_prompt_file(v),
        # склейка — той же командой, что у очереди конвейера
        "concat_cmd": concat_cmd(v, project),
    }
