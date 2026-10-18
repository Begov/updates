"""Auto Visual Story handoff for selective EPF/stock-photo parallax."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import params as config
from units.source_ledger import AUTO_VISUAL_PARALLAX_PHOTO_SOURCES


IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")


def photo_ready_for_parallax(project_dir: str | Path, clip_id: int) -> bool:
    """Готов ли кадр к отправке: картинка на месте, видео ещё нет."""
    project = Path(project_dir)
    if (project / "vid_img" / f"{clip_id:03d}.mp4").is_file():
        return False                       # уже оживлён — второй раз не шлём
    generated = project / "generated"
    return any((generated / f"{clip_id:03d}{ext}").is_file()
               for ext in IMAGE_EXTENSIONS)


def run_auto_visual_parallax(
    project_dir: str | Path,
    generate_videos: Callable[..., dict[str, Any]],
    sources=None,
    include_ai: bool = False,
    *,
    active_keys=None,
) -> dict[str, Any]:
    """Run normal Veo I2V for every Nth ready photo of the given sources.

    ``generate_videos`` is the regular ``veo_generate_all_videos`` entrypoint,
    injected here so the selection behavior can be tested without network calls.
    ``sources`` — набор источников; None = набор Auto Visual Story. Прореживание
    (`VEONONSTOP_I2V_PARALLAX_EVERY_N`) здесь ровно то же, что у нейрокадров в
    обычном i2v, — настройка одна на обе ветки.
    """
    project = Path(project_dir)
    # Тот же ОБЩИЙ список, что у вотчера и у стадии i2v: доля «каждый N-й»
    # считается один раз по всем отмеченным клипам (units/parallax_schedule.py).
    # Кто не попал — остаётся картинкой и получает зум в склейке. Нужно оживить
    # все — ставится Every N = 1 (замечание юзера 28.08).
    from units import parallax_schedule as _px_plan

    # `sources=None` означает «набор Auto Visual Story» (сток + архивные фото), а
    # НЕ «смотри галки в настройках»: так эта функция вела себя всегда, и на
    # умолчании держатся ручные запуски и тесты. Галки приходят сюда явным
    # аргументом из пайплайна.
    _sources = (set(sources) if sources is not None
                else set(AUTO_VISUAL_PARALLAX_PHOTO_SOURCES))
    plan = _px_plan.load(project)
    if plan is None:
        plan = _px_plan.build(project, sources=_sources,
                              include_ai=include_ai, save=False)
    every_n = int(plan.get("every_n") or 1)
    selected = {int(cid) for cid in (plan.get("ids") or [])}
    mine = {int(cid) for cid in (plan.get("external_eligible") or [])}
    if include_ai:
        mine |= {int(cid) for cid in (plan.get("ai_eligible") or [])}
    eligible_ids = [cid for cid in (plan.get("eligible") or []) if cid in mine]
    chosen = [cid for cid in eligible_ids if cid in selected]
    selected_ids = [cid for cid in chosen
                    if photo_ready_for_parallax(project, cid)]
    summary: dict[str, Any] = {
        "eligible_ids": eligible_ids,
        "chosen_ids": chosen,
        "selected_ids": selected_ids,
        "every_n": every_n,
        "result": None,
    }
    if not selected_ids:
        return summary
    try:
        summary["result"] = generate_videos(
            project_dir=str(project),
            mode="i2v",
            clip_ids=selected_ids,
            silent=True,
            no_cancel=True,
            allow_external_image_clip_ids=selected_ids,
            parallax_only=True,
            active_keys=active_keys,
        )
    except Exception as exc:                              # noqa: BLE001
        # Как и в `watch_parallax`: параллакс — украшение, кадр останется
        # картинкой и получит зум в склейке. Ронять из-за него прогон нельзя
        # (пропавший файл шаблонов делал ровно это — 29.08).
        summary["error"] = str(exc)
        print(f"  [Параллакс] проход не удался: {str(exc)[:200]}")
    return summary


def _shared_queue(active_keys):
    """Общая очередь прогона для этих ключей (если пайплайн её завёл)."""
    try:
        from units.motion_queue import get_queue

        return get_queue(active_keys)
    except Exception:                                     # noqa: BLE001
        return None


def _parallax_prompt_for(clip_id: int) -> str | None:
    """Параллакс-шаблон этого клипа. None — шаблонов нет вовсе."""
    from units.parallax_core import (parallax_method_for_clip,
                                  selected_parallax_prompt)

    method = parallax_method_for_clip(
        clip_id, getattr(config, "VEONONSTOP_I2V_PARALLAX_METHOD", 1))
    return selected_parallax_prompt(method)


def _watch_via_queue(project: Path, queue, chosen: list[int],
                     summary: dict[str, Any], *, stop_event=None,
                     poll_sec: float = 6.0, printer=print) -> dict[str, Any]:
    """Наблюдать за папкой и КЛАСТЬ кадры в общую очередь по одному.

    Наблюдатель здесь только смотрит и кладёт — генерацию ведут рабочие
    очереди. Раньше он сам уходил генерировать целую пачку и всё это время в
    папку не смотрел: в прогоне 30.08 пачка из 96 кадров держала его 15 минут,
    за которые доготовилось ещё около сотни картинок, и подхватить их было
    некому — 84 кадра так и не уехали.
    """
    import threading
    import time as _time

    from units.veo_motion import _process_single_clip
    from units.motion_claims import acquire_clip_claim, clip_media_exists

    output_dir = project / "vid_img"
    output_dir.mkdir(exist_ok=True)
    generated = project / "generated"
    aspect = getattr(config, "VEONONSTOP_ASPECT_RATIO", "16:9")

    worker_counter = {"next": 0}
    worker_map: dict[int, int] = {}
    worker_lock = threading.Lock()

    def _worker_id() -> int:
        tid = threading.get_ident()
        with worker_lock:
            if tid not in worker_map:
                worker_map[tid] = worker_counter["next"]
                worker_counter["next"] += 1
            return worker_map[tid]

    def _find_image(clip_id: int) -> str | None:
        for ext in IMAGE_EXTENSIONS:
            path = generated / f"{clip_id:03d}{ext}"
            if path.is_file():
                return str(path)
        return None

    def _run(claim, **kwargs):
        try:
            return _process_single_clip(**kwargs)
        finally:
            claim.release()

    sent: dict[Any, int] = {}          # future -> clip_id
    done: set = set()
    failed: set = set()
    no_template = False

    while True:
        finishing = stop_event is not None and stop_event.is_set()
        # 1) собрать завершённые — только чтобы вести счёт
        for future in [f for f in list(sent) if f.done()]:
            clip_id = sent.pop(future)
            try:
                result = future.result()
                if (result or {}).get("status") == "ok":
                    done.add(clip_id)
                else:
                    failed.add(clip_id)
            except Exception as exc:                      # noqa: BLE001
                failed.add(clip_id)
                printer(f"  [Параллакс] клип {clip_id:03d} не оживился: "
                        f"{str(exc)[:160]}")
            summary["done_ids"] = sorted(done)

        # 2) положить в очередь всё, что появилось
        for clip_id in chosen:
            if clip_id in done or clip_id in failed or clip_id in sent.values():
                continue
            if clip_media_exists(output_dir, clip_id):
                done.add(clip_id)                          # оживил кто-то другой
                continue
            image_path = _find_image(clip_id)
            if not image_path:
                continue
            prompt = _parallax_prompt_for(clip_id)
            if prompt is None:
                no_template = True
                break
            claim = acquire_clip_claim(output_dir, clip_id, owner="parallax")
            if not claim:
                continue                                   # клип взят другим путём
            future = queue.put(
                _run, claim,
                label=f"parallax {clip_id:03d}",
                client=None,
                clip_id=clip_id,
                prompt=prompt,
                mode="i2v",
                image_path=image_path,
                ref_dir=None,
                output_dir=output_dir,
                aspect_ratio=aspect,
                worker_id_fn=_worker_id,
                single_prompt_mode=True,
                clients_cfg_list=queue.clients_cfg,
            )
            sent[future] = clip_id
            summary["rounds"] += 1
            printer(f"  [Параллакс] кадр {clip_id:03d} → в очередь "
                    f"(в работе {len(sent)})")

        if no_template:
            printer("  [Параллакс] шаблоны не найдены "
                    "(agent/prompts/parallax_prompts.txt) — кадры остаются "
                    "картинками с зумом")
            break
        if len(done) + len(failed) >= len(chosen) and not sent:
            break
        if finishing and not sent:
            break
        _time.sleep(max(1.0, float(poll_sec)))

    # Начатые доигрывают: рабочие уже держат слоты, бросать их нельзя.
    for future, clip_id in list(sent.items()):
        try:
            result = future.result()
            if (result or {}).get("status") == "ok":
                done.add(clip_id)
            else:
                failed.add(clip_id)
        except Exception:                                  # noqa: BLE001
            failed.add(clip_id)
    summary["done_ids"] = sorted(done)
    summary["failed_ids"] = sorted(failed)
    waiting = [cid for cid in chosen if cid not in done and cid not in failed]
    if waiting:
        summary["waiting_ids"] = waiting
        printer(f"  [Параллакс] без параллакса осталось {len(waiting)} кадр(ов) "
                f"— идут статикой с зумом")
    printer(f"  [Параллакс] оживлено {len(done)} из {len(chosen)}")
    return summary


def watch_parallax(
    project_dir: str | Path,
    generate_videos: Callable[..., dict[str, Any]],
    sources=None,
    include_ai: bool = False,
    *,
    stop_event=None,
    poll_sec: float = 6.0,
    printer=print,
    active_keys=None,
) -> dict[str, Any]:
    """Оживлять кадры параллаксом ПО МЕРЕ ГОТОВНОСТИ, не дожидаясь стадий.

    `active_keys` — ключи VeoNonStop из чекбоксов «Vid Key». Без них генератор
    подставлял слот 1 (`veo_video.generate_all_videos`), и параллакс садился на
    первый ключ, что бы пользователь ни отметил: развёл картинки и видео по
    разным ключам — а вотчер всё равно лез в тот, где идёт ImageGen.

    Список «кого паралаксить» считается ОДИН раз по плану — каждый N-й из
    подходящих клипов, — поэтому он не зависит от того, что успело скачаться.
    Дальше цикл просто ждёт, когда у выбранных появятся картинки: готовые
    уходят в VeoNonStop сразу, остальные ждут следующего круга.

    Останавливается по `stop_event` (его ставит пайплайн, когда стадии-источники
    закончили) — после чего делает последний круг по остаткам.
    """
    project = Path(project_dir)
    # Список «кого паралаксить» берём ОБЩИЙ на прогон, а не считаем свой:
    # тот же список видит стадия i2v, поэтому доля «каждый N-й» соблюдается по
    # всем отмеченным клипам сразу, а не отдельно у каждого пути
    # (см. units/parallax_schedule.py).
    from units import parallax_schedule as _px_plan

    # `sources=None` означает «набор Auto Visual Story» (сток + архивные фото), а
    # НЕ «смотри галки в настройках»: так эта функция вела себя всегда, и на
    # умолчании держатся ручные запуски и тесты. Галки приходят сюда явным
    # аргументом из пайплайна.
    _sources = (set(sources) if sources is not None
                else set(AUTO_VISUAL_PARALLAX_PHOTO_SOURCES))
    plan = _px_plan.load(project)
    if plan is None:
        plan = _px_plan.build(project, sources=_sources,
                              include_ai=include_ai, save=False)
    every_n = int(plan.get("every_n") or 1)
    selected = {int(cid) for cid in (plan.get("ids") or [])}
    # Из общего списка берём ТОЛЬКО своё: сток и архивы всегда, нейрокадры —
    # если их отдали наблюдателю (видеогенерация выключена). Иначе кадр уехал бы
    # дважды: и здесь, и в обычной стадии i2v.
    mine = {int(cid) for cid in (plan.get("external_eligible") or [])}
    if include_ai:
        mine |= {int(cid) for cid in (plan.get("ai_eligible") or [])}
    eligible_ids = [cid for cid in (plan.get("eligible") or []) if cid in mine]
    chosen = [cid for cid in eligible_ids if cid in selected]
    summary: dict[str, Any] = {
        "eligible_ids": eligible_ids,
        "chosen_ids": chosen,
        "done_ids": [],
        "every_n": every_n,
        "rounds": 0,
    }
    if not chosen:
        return summary
    printer(f"  [Параллакс] в очереди {len(chosen)} кадр(ов) из "
            f"{len(eligible_ids)} (каждый {every_n}-й по общему списку), "
            f"жду их готовности")

    # Есть общая очередь прогона — кладём кадры В НЕЁ по одному, как только
    # появился файл, и НЕ ждём. Прежний путь (собрать пачку и ждать её целиком)
    # оставлен запасным: он нужен, когда стадию запускают отдельным BAT и
    # очереди не существует.
    queue = _shared_queue(active_keys)
    if queue is not None and queue.clients_cfg:
        return _watch_via_queue(project, queue, chosen, summary,
                                stop_event=stop_event, poll_sec=poll_sec,
                                printer=printer)

    import time as _time

    done: set = set()
    while True:
        finishing = stop_event is not None and stop_event.is_set()
        ready = [cid for cid in chosen
                 if cid not in done and photo_ready_for_parallax(project, cid)]
        if ready:
            summary["rounds"] += 1
            printer(f"  [Параллакс] отправляю {len(ready)} кадр(ов): "
                    + ", ".join(f"{cid:03d}" for cid in ready))
            try:
                generate_videos(
                    project_dir=str(project),
                    mode="i2v",
                    clip_ids=ready,
                    silent=True,
                    no_cancel=True,
                    allow_external_image_clip_ids=ready,
                    parallax_only=True,
                    active_keys=active_keys,
                )
            except Exception as exc:                      # noqa: BLE001
                # Параллакс — украшение: кадр останется картинкой и получит
                # зум в склейке, ронять из-за него прогон нельзя.
                printer(f"  [Параллакс] партия не удалась: {str(exc)[:200]}")
            done.update(ready)
            summary["done_ids"] = sorted(done)
        if finishing or len(done) >= len(chosen):
            break
        _time.sleep(max(1.0, float(poll_sec)))
    waiting = [cid for cid in chosen if cid not in done]
    if waiting:
        printer(f"  [Параллакс] без картинки к концу стадий: "
                + ", ".join(f"{cid:03d}" for cid in waiting)
                + " — останутся статикой с зумом")
    return summary
