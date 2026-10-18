"""
i2v_overrides.py — подмена промптов оживления (i2v) поверх промптов сцен.

Общая для всех видеопровайдеров: VeoNonStop и CinAI оживляют одни и те же кадры,
и настройки «Конвейера» должны действовать одинаково. Раньше эта логика жила
внутри veo_motion, и при видео через CinAI параллакс и промпт обложки молча
не применялись.

  * параллакс — шаблон движения камеры вместо промпта сцены у отмеченных клипов
    (VEONONSTOP_I2V_PARALLAX*, общий список units/parallax_schedule);
  * обложка — свой промпт оживления для клипа-обложки (units/cover_opener).
"""
from __future__ import annotations

import logging
from pathlib import Path

import params as config


def apply_i2v_prompt_overrides(project_path: Path, prompts_map: dict, mode: str,
                               parallax_only: bool = False) -> None:
    """Подменяет промпты в `prompts_map` на месте (только для i2v)."""
    project_path = Path(project_path)
    # Обычный i2v идёт по НЕЙРОкадрам (клипы с внешним источником генератор
    # пропускает), поэтому здесь решает галка «AI»: снял её — нейрокадры
    # оживляются обычным промптом, а параллакс остаётся только тем источникам,
    # что отмечены отдельно (28.08).
    if (not parallax_only
            and mode == 'i2v'
            and getattr(config, 'VEONONSTOP_I2V_PARALLAX', False)
            and getattr(config, 'VEONONSTOP_I2V_PARALLAX_AI', True)):
        from units.parallax_core import (parallax_method_for_clip,
                                      parallax_method_name,
                                      selected_parallax_prompt)
        from units import parallax_schedule as _px_plan

        _px_mode = getattr(config, 'VEONONSTOP_I2V_PARALLAX_METHOD', 1)
        _px_cache: dict[int, str] = {}
        _px_used: dict[int, list[int]] = {}
        mix_with_perclip = bool(
            getattr(config, 'VEONONSTOP_I2V_PARALLAX_MIX_PERCLIP', False))
        try:
            every_n = max(
                1, int(getattr(
                    config, 'VEONONSTOP_I2V_PARALLAX_EVERY_N', 2) or 2))
        except (TypeError, ValueError):
            every_n = 2
        # Кому ставить параллакс, решает ОБЩИЙ список прогона, а не формула
        # на месте. Раньше здесь считалось `id % N == 0` — по НОМЕРУ клипа, — и
        # на смешанном ролике нейрокадрам могло не достаться ни одного
        # параллакса: если чётные номера разобрали сток и архивы, среди
        # нечётных нет ни одного кратного двум. Теперь список один на все
        # стадии, считается по позиции в ряду отмеченных клипов
        # (см. units/parallax_schedule.py).
        _px_selected = _px_plan.parallax_ids(project_path)
        parallax_ids = []
        # Подмену собираем отдельно и применяем ТОЛЬКО целиком: иначе при
        # частично найденных шаблонах часть клипов уехала бы с параллакс-
        # промптом, а часть — с обычным, и в логе это выглядело бы нормой.
        _px_apply: dict[int, str] = {}
        _px_missing = False
        for cid in sorted(prompts_map):
            if cid in _px_selected:
                method = parallax_method_for_clip(cid, _px_mode)
                if method not in _px_cache:
                    _template = selected_parallax_prompt(method)
                    if _template is None:
                        # Шаблона нет — оживляем ОБЫЧНЫМИ промптами сцен.
                        # Раньше здесь вылетал FileNotFoundError и рушил всю
                        # стадию i2v: клипы оставались вообще без видео
                        # (прогон 29.08, пять сцен).
                        _px_missing = True
                        break
                    _px_cache[method] = _template
                _px_apply[cid] = _px_cache[method]
                _px_used.setdefault(method, []).append(cid)
                parallax_ids.append(cid)
        if _px_missing:
            parallax_ids = []
            _px_used.clear()
            _px_apply.clear()
            print("  [Параллакс] ⚠️  шаблоны не найдены "
                  "(agent/prompts/parallax_prompts.txt) — клипы идут обычными "
                  "промптами сцен, без параллакса")
            logging.warning("I2V parallax: templates missing — falling back "
                            "to per-clip prompts")
        else:
            prompts_map.update(_px_apply)
        mode_label = (
            f"каждый {every_n}-й из общего списка отмеченных, "
            f"остальным промпты сцен"
            if mix_with_perclip else "всем нейрокадрам"
        )
        spread = "; ".join(
            f"{m} ({parallax_method_name(m)}): {ids}"
            for m, ids in sorted(_px_used.items())
        )
        if parallax_ids:
            print(f"  [Параллакс] режим {_px_mode} ({parallax_method_name(_px_mode)}) → "
                  f"{len(parallax_ids)} клип(ов), {mode_label} — {spread}")
        else:
            # Молчать нельзя: включённый параллакс, не задевший ни одного клипа,
            # выглядит в логе как норма, хотя настройка не сработала.
            print(f"  [Параллакс] включён (режим {_px_mode}), но из нейрокадров "
                  f"в общий список «каждый {every_n}-й» не попал ни один клип "
                  f"(в списке всего {len(_px_selected)} клип(ов) — остальные "
                  f"это сток и архивы)")
        logging.info(
            "I2V parallax: режим %s (%s), клипов %s, %s — %s",
            _px_mode, parallax_method_name(_px_mode), len(parallax_ids),
            mode_label, spread or "нет",
        )

    # Обложка: свой промпт вместо промпта сцены — тем же приёмом, что параллакс.
    # Ставится ПОСЛЕ параллакса намеренно: если обложка почему-то попала и в
    # параллаксный список, побеждает обложечный промпт — он для неё точнее.
    if mode == 'i2v':
        try:
            from units import cover_opener as cover_hook

            _cover_id = cover_hook.cover_clip_id(project_path)
            if _cover_id is not None and _cover_id in prompts_map:
                _cover_prompt = cover_hook.prompt_for()
                if _cover_prompt:
                    prompts_map[_cover_id] = _cover_prompt
                    print(f"  [Обложка] клип {_cover_id:03d} оживляем режимом "
                          f"«{cover_hook.mode_name()}»")
                    logging.info("Cover hook: клип %s, режим %s",
                                 _cover_id, cover_hook.hook_mode())
                else:
                    # Шаблонов нет — кадр останется статикой, это не повод
                    # ронять стадию (тот же урок, что с параллаксом 29.08).
                    print("  [Обложка] шаблоны не найдены "
                          "(agent/prompts/cover_hook_prompts.txt) — кадр "
                          "останется статичной обложкой")
        except Exception as _cover_exc:                     # noqa: BLE001
            logging.warning("Cover hook: промпт не подставлен (%s)", _cover_exc)
