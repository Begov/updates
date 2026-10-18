#!/usr/bin/env python3
"""
Video Production Agent - Главный скрипт
Автоматизация создания видео через AI
"""
import argparse
import sys
import os
from pathlib import Path
from datetime import datetime
import json
import logging
import traceback
import time
from datetime import datetime as dt
from typing import Optional, Tuple


from tqdm import tqdm


class _TeeStream:
    """Дублирует stdout в файл."""
    def __init__(self, original, file_handle):
        self._orig = original
        self._file = file_handle
    def write(self, data):
        self._orig.write(data)
        try:
            self._file.write(data)
            self._file.flush()
        except Exception:
            pass
    def flush(self):
        self._orig.flush()
        try:
            self._file.flush()
        except Exception:
            pass
    def __getattr__(self, name):
        return getattr(self._orig, name)

# Установка UTF-8 кодировки для Windows (консоль + вывод)
if sys.platform == 'win32':
    os.system('chcp 65001 >nul')
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

# Добавляем путь к модулям
sys.path.insert(0, str(Path(__file__).parent))

import params as config
from units.speech_to_text import AssemblyAITranscriber, transcribe_audio
from units.scene_director import direct_project
from units.plan_validator import validate_file_exists
from units.inbox_manager import (
    get_ready_files,
    get_sg_all_file,
    get_all_style_guides_for_audio,
    setup_project,
)
from units.fastgen_engine import generate_all_images
from units.veo_motion import generate_all_videos as veo_generate_all_videos
from units.veo_stills import generate_all_images as veo_generate_all_images
from units.cinai_stills import generate_all_images as cinai_generate_all_images
from units.cinai_motion import generate_all_videos as cinai_generate_all_videos
from units.ref_shrink import compress_refs


POST_ACTION_CHOICES = [
    "skip",
    "fastgen",
    "imggen",
    "cinai",
    "fastgen_i2v",
    "veo_i2v",
    "t2v",
]


def _engine_script(name: str) -> Path:
    """Скрипт движка рядом с cli: в установленной версии вместо .py лежит байткод .pyc."""
    path = Path(__file__).resolve().parent / name
    if not path.exists() and path.with_suffix('.pyc').exists():
        return path.with_suffix('.pyc')
    return path


# ============================================================================
# НАСТРОЙКА ЛОГГИРОВАНИЯ
# ============================================================================

def setup_logging(log_dir: Path) -> Path:
    """Настройка логгирования"""
    log_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_file = log_dir / f'agent_{timestamp}.log'

    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(getattr(logging, config.LOG_LEVEL))
    file_handler.setFormatter(logging.Formatter(config.LOG_FORMAT))

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.WARNING)
    stream_handler.setFormatter(logging.Formatter(config.LOG_FORMAT))

    root = logging.getLogger()
    root.setLevel(getattr(logging, config.LOG_LEVEL))
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(stream_handler)
    
    # Скрыть DEBUG логи от httpcore и других библиотек
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('google').setLevel(logging.WARNING)

    return log_file


# ============================================================================
# КОМАНДЫ АГЕНТА
# ============================================================================

def _save_failed_prompts(project_dir: str, stage: str, failed_count: int) -> None:
    """Сохраняет информацию о несгенерированных клипах в failed_prompts.txt.

    stage: 'imggen', 'i2v', 't2v', 'components'
    """
    p = Path(project_dir)
    failed_file = p / 'failed_prompts.txt'
    remap_dense_prompts_for_montage = None

    # Читаем промпты
    try:
        from prompt_extract import extract_all_prompts, find_prompts_file, remap_dense_prompts_for_montage
        prompts_file = find_prompts_file(str(p))
        all_prompts = extract_all_prompts(prompts_file) if prompts_file else {}
    except Exception:
        all_prompts = {}

    # Читаем montage_plan для списка clip IDs
    try:
        import json as _json
        mp_path = p / 'montage_plan.json'
        if mp_path.exists():
            mp_data = _json.loads(mp_path.read_text(encoding='utf-8-sig'))
            mp = mp_data.get('montage_plan', mp_data)
            clips = mp.get('clips', [])
            all_clip_ids = [c['id'] for c in clips]
            if all_prompts and remap_dense_prompts_for_montage:
                all_prompts, _ = remap_dense_prompts_for_montage(all_prompts, clips)
        else:
            all_clip_ids = sorted(all_prompts.keys())
    except Exception:
        all_clip_ids = sorted(all_prompts.keys())

    missing = []

    if stage == 'imggen':
        gen_dir = p / 'generated'
        for cid in all_clip_ids:
            found = any((gen_dir / f"{cid:03d}{ext}").exists()
                        for ext in ('.png', '.jpg', '.jpeg', '.webp'))
            if not found:
                prompt = all_prompts.get(cid, f'(prompt #{cid} not found)')
                missing.append((cid, prompt))

    elif stage in ('i2v', 't2v', 'components'):
        vid_dir = p / 'vid_img'
        for cid in all_clip_ids:
            if not (vid_dir / f"{cid:03d}.mp4").exists():
                prompt = all_prompts.get(cid, f'(prompt #{cid} not found)')
                if stage == 'i2v':
                    # Показываем какой файл-картинку нужно было оживить
                    img_name = f"generated/{cid:03d}.png"
                    missing.append((cid, f"[image: {img_name}] {prompt[:150]}"))
                else:
                    missing.append((cid, prompt))

    if not missing:
        return

    lines = [f"=== FAILED {stage.upper()} ({len(missing)} clips) ===\n"]
    for cid, prompt in missing:
        lines.append(f"#{cid:03d}: {prompt[:200]}\n")

    # Append (не перезаписываем — могут быть failed от обоих стадий)
    with open(failed_file, 'a', encoding='utf-8') as f:
        f.writelines(lines)
        f.write("\n")

    print(f"  Failed {stage} prompts saved to: {failed_file.name} ({len(missing)} clips)")
    logging.info(f"Saved {len(missing)} failed {stage} prompts to {failed_file}")


def _start_inflight_watcher(p_dir: str, video_key_slot: str, video_parallel: int,
                            ar: str, prompt_mode: str, vislide_interval: int = 0,
                            prompts_file_path: str = None,
                            fastgen_key_slot: str = None,
                            output_dir_name: str = 'vid_img'):
    """Launch watch_i2v*.py as background subprocess for inflight i2v mode.
    Engine routing: BEGO_VIDEO_ENGINE=cinai → live_cinai.py, else live_veo.py (VeoNonStop). Returns Popen handle or None on failure."""
    import subprocess as _sp
    _engine = os.environ.get('BEGO_VIDEO_ENGINE', 'veononstop').strip().lower()
    if _engine == 'cinai':
        watch_script = _engine_script('live_cinai.py')
        _script_label = 'live_cinai.py'
    else:
        watch_script = _engine_script('live_veo.py')
        _script_label = 'live_veo.py'
    if not watch_script.exists():
        print(f"  [WARN] {_script_label} not found — inflight mode disabled")
        return None
    project_path = Path(p_dir).resolve()
    if _engine == 'cinai':
        # у CinAI своя параллель из «Конвейера», лимит VeoNonStop ему чужой
        video_parallel = max(1, min(int(getattr(config, 'CINAI_VIDEO_MAX_PARALLEL', video_parallel)
                                        or video_parallel), 6))
    cmd = [sys.executable, str(watch_script),
           '--project', str(project_path),
           '--parallel', str(video_parallel),
           '--key-slot', video_key_slot,
           '--aspect', ar]
    if prompt_mode == 'single':
        cmd.append('--single-prompt')
    if vislide_interval >= 2:
        cmd += ['--vislide-interval', str(vislide_interval)]
    if prompts_file_path:
        cmd += ['--prompts-file', str(prompts_file_path)]
    if _engine == 'cinai':
        cmd += [
            '--model', str(config.CINAI_VIDEO_MODEL),
            '--mode', 'i2v',
            '--duration', str(getattr(config, 'CINAI_VIDEO_DURATION', 5)),
            '--resolution', str(getattr(config, 'CINAI_VIDEO_RESOLUTION', '') or ''),
            '--audio', str(getattr(config, 'CINAI_VIDEO_AUDIO', '') or ''),
            '--output-dir', output_dir_name,
            '--done-file', str(project_path / 'generated' / '.imagegen_done'),
            '--drain-seconds', str(getattr(config, 'CINAI_VIDEO_INFLIGHT_DRAIN_SECONDS', 120)),
        ]
    cmd.append('--no-cancel')  # imggen runs in parallel — don't cancel its tasks
    if _engine == 'cinai':
        cmd = [item for item in cmd if item != '--no-cancel']
    print(f"  [INFLIGHT] Starting {_script_label} (engine={_engine}, key={cmd[cmd.index('--key-slot')+1]}, parallel={video_parallel})"
          + (f" [vislide every {vislide_interval}]" if vislide_interval >= 2 else "") + "...")
    try:
        import os as _os
        _env = dict(_os.environ)
        _env['PYTHONUTF8'] = '1'
        proc = _sp.Popen(cmd, cwd=str(Path(__file__).parent), env=_env,
                         stdout=sys.stdout, stderr=sys.stdout)
        return proc
    except Exception as e:
        print(f"  [WARN] Inflight mode failed to start: {e}")
        return None


def _wait_inflight_watcher(proc, p_dir: str, output_dir_name: str = 'vid_img') -> bool:
    """Wait for watch_i2v subprocess, check returncode & vid_img/ contents.
    Returns True if vid_img/ has videos, False if inflight failed (caller should fallback)."""
    print("  [INFLIGHT] Waiting for live_veo.py to finish...")
    proc.wait()
    rc = proc.returncode
    vid_dir = Path(p_dir) / output_dir_name
    vids = list(vid_dir.glob('*.mp4')) if vid_dir.exists() else []
    if rc != 0:
        print(f"  [INFLIGHT] WARNING: live_veo.py exited with code {rc}")
    if rc != 0:
        print(f"  [INFLIGHT] WARNING: inflight produced an incomplete result ({len(vids)} videos)")
        print(f"  [INFLIGHT] Falling back to finalized generation for missing clips...")
        return False
    if vids:
        print(f"  [INFLIGHT] watch_i2v done -> vid_img/ ({len(vids)} videos)")
        return True
    else:
        print(f"  [INFLIGHT] WARNING: vid_img/ is empty — inflight produced no videos")
        print(f"  [INFLIGHT] Falling back to standard i2v generation...")
        return False


# ------------------------------------------------------------- инфографика
def _resolve_fastgen_slot(slot: str, allow_dual: bool = False):
    """(ключ FastGen, режим двух ключей, метка слота) по выбору слота."""
    slot_norm = str(slot or '1').lower()
    slot_norm = {
        'key1': '1', 'main': '1',
        'key2': '2', 'backup': '2',
        'key3': '3', 'key4': '4',
    }.get(slot_norm, slot_norm)
    key1 = getattr(config, 'FASTGEN_API_KEY', '') or ''
    keys = {
        '1': key1,
        '2': (getattr(config, 'FASTGEN_API_KEY_2', '') or
              getattr(config, 'FASTGEN_API_KEY_BACKUP', '') or ''),
        '3': getattr(config, 'FASTGEN_API_KEY_3', '') or '',
        '4': getattr(config, 'FASTGEN_API_KEY_4', '') or '',
    }
    if slot_norm in ('2', '3', '4'):
        _slot_key = keys.get(slot_norm) or ''
        return (_slot_key or key1 or None, False,
                slot_norm if _slot_key and _slot_key != key1 else '1')
    if allow_dual and slot_norm in ('both', 'all', '1,2', '1,2,3,4'):
        _extra = [v for k, v in keys.items() if k != '1' and v and v != key1]
        return (key1 or None, bool(_extra), 'all' if _extra else '1')
    return (key1 or None, False, '1')



def _apply_infographic_intro_gate(p_dir: str) -> None:
    """Снять маркер `infographic` со вступления — ДО старта любых стадий.

    Интро-гейт: диаграмма во вступлении убивает хук. Своя граница у стадии
    (у Real Photo и стока — собственные, независимые).

    Зовётся СИНХРОННО перед запуском фоновой инфографики и ImageGen.
    Раньше гейт жил внутри `_infographic_clip_ids`, то есть срабатывал уже
    в фоновом потоке — и ImageGen, стартовавший параллельно, успевал
    прочитать план со ЕЩЁ НЕ снятым маркером, считал клип внешним и
    пропускал его. Кадра не появлялось ни у инфографики (маркер снят), ни у
    генерации (успела отфильтровать) → клип уходил в i2v без картинки
    (прогон 26.08: `IDs: 3` в «images missing», гонка строк 477 vs 489).
    Функция идемпотентна: повторный вызов не найдёт маркеров и ничего не
    сделает.
    """
    if not getattr(config, 'INFOGRAPHIC_ENABLED', False):
        return
    _ig_gate = float(getattr(config, 'INFOGRAPHIC_START_SEC', 0) or 0)
    if _ig_gate <= 0:
        return
    try:
        from units.source_ledger import INFOGRAPHIC_SOURCES, apply_intro_gate
        apply_intro_gate(Path(p_dir) / 'montage_plan.json',
                         INFOGRAPHIC_SOURCES, _ig_gate,
                         label='Infographic', printer=print)
    except Exception as exc:
        print(f"  [Infographic] интро-гейт пропущен: {exc}")
        logging.exception(
            "Infographic intro gate skipped for %s (start_sec=%s): %s",
            p_dir, _ig_gate, exc)

def _infographic_clip_ids(p_dir: str) -> list[int]:
    """Return Pass 1 clips reserved for the dedicated static infographic route."""
    if not getattr(config, 'INFOGRAPHIC_ENABLED', False):
        return []
    try:
        import json as _json
        raw = _json.loads((Path(p_dir) / 'montage_plan.json').read_text(encoding='utf-8-sig'))
        plan = raw.get('montage_plan', raw)
        return [
            int(clip['id']) for clip in plan.get('clips', [])
            if clip.get('source') == 'infographic' and clip.get('id') is not None
        ]
    except Exception as exc:
        print(f"  [Infographic] Could not read montage plan: {exc}")
        return []

def _run_infographic_images(p_dir: str, clip_ids: list[int] | None = None) -> dict:
    """Generate static infographics through their own provider/model, then stage them.

    Общая для маршрута конвейера (`_run_post_pipeline_silent`) и отдельной стадии
    «Картинки» в Панели управления. Ключи, потоки и формат берутся из окружения
    запуска так же, как в маршруте конвейера.
    """
    project_dir = p_dir
    ar = config.VEONONSTOP_ASPECT_RATIO
    imggen_key_slot = (os.environ.get('BEGO_IMGGEN_KEY_SLOT')
                       or os.environ.get('VEONONSTOP_IMGGEN_KEY_SLOT') or '1')
    imggen_parallel = int(os.environ.get('BEGO_IMGGEN_PARALLEL',
                                         os.environ.get('VEONONSTOP_MAX_PARALLEL',
                                                        config.VEONONSTOP_MAX_PARALLEL)))
    _skip_pf = os.environ.get('BEGO_SKIP_PREFLIGHT', '0') == '1'
    from units.source_ledger import materialize_static_images

    target_ids = clip_ids if clip_ids is not None else _infographic_clip_ids(p_dir)
    if not target_ids:
        return {'generated': 0, 'failed': 0, 'results': []}

    provider = (getattr(config, 'INFOGRAPHIC_IMG_PROVIDER', '') or '').strip().lower()
    # Оператор инфографики не выбран (или доля инфографики была нулевой, а
    # сцены пришли сюда фолбэком из архивов) — берём ПЕРВЫЙ доступный
    # генератор картинок и модель GPT, если провайдер её умеет, иначе
    # Banana (требование пользователя 26.08). Без этого такие сцены
    # оставались без исполнителя.
    _auto_model = None
    if provider not in ('fastgen', 'veononstop', 'vertex', 'cheapvibe', 'varag', 'cinai'):
        for _cand, _ready in (
            ('cinai', bool(getattr(config, 'CINAI_ACCESS_TOKEN', ''))),
            ('veononstop', _veo_slot_key_present()),
            ('fastgen', bool(getattr(config, 'FASTGEN_API_KEY', ''))),
        ):
            if not _ready:
                continue
            provider = _cand
            if _cand == 'cinai':
                _auto_model = None                   # у CinAI — бесплатная модель из настроек
            elif _cand == 'veononstop':
                _auto_model = None                   # у VeoNonStop только Banana (модель из настроек)
            else:
                _auto_model = 'GEM_PIX_2'            # FastGen: Banana-семейство
            print(f"  [Infographic] оператор не задан — беру {provider}"
                  f" (модель {_auto_model})")
            break
        else:
            print("  [Infographic] нет ни одного доступного генератора картинок")
            return {'generated': 0, 'failed': len(target_ids), 'results': [],
                    'error': 'no image generator configured'}
    print(f"  [Infographic] {len(target_ids)} static clips -> {provider}")
    try:
        if provider == 'fastgen':
            info_api, info_dual, _ = _resolve_fastgen_slot(imggen_key_slot, allow_dual=True)
            if not info_api:
                raise RuntimeError("FASTGEN_API_KEY is not configured")
            result = generate_all_images(
                project_dir=p_dir,
                clip_ids=target_ids,
                model=_auto_model or getattr(config, 'INFOGRAPHIC_FASTGEN_MODEL', None),
                api_key=info_api,
                dual_key=info_dual,
                preflight=not _skip_pf,
                silent=True,
                include_infographics=True,
            )
        elif provider == 'veononstop':
            if not _veo_slot_key_present():
                raise RuntimeError("no VeoNonStop key is configured")
            # Только Banana; клиент — именно VeoNonStop, даже если основные картинки
            # идут через Vertex/CheapVibeCode (BEGO_IMAGE_PROVIDER)
            result = veo_generate_all_images(
                project_dir=p_dir,
                mode='banana',
                max_parallel=imggen_parallel,
                clip_ids=target_ids,
                aspect_ratio=ar,
                active_keys=_resolve_veo_key(imggen_key_slot),
                preflight=not _skip_pf,
                silent=True,
                include_infographics=True,
                banana_model_key=getattr(config, 'INFOGRAPHIC_VEONONSTOP_BANANA_MODEL', None),
                client_provider='veononstop',
            )
        elif provider == 'vertex':
            if not (getattr(config, 'VERTEX_API_KEY', '') and getattr(config, 'VERTEX_PROJECT_ID', '')):
                raise RuntimeError("VERTEX_API_KEY / VERTEX_PROJECT_ID are not configured")
            result = veo_generate_all_images(
                project_dir=p_dir, mode='banana', clip_ids=target_ids, aspect_ratio=ar,
                preflight=False, silent=True, include_infographics=True,
                client_provider='vertex',
                client_options={'model': getattr(config, 'INFOGRAPHIC_VERTEX_MODEL', None)},
            )
        elif provider == 'cheapvibe':
            if not getattr(config, 'CHEAPVIBE_API_KEY', ''):
                raise RuntimeError("CHEAPVIBE_API_KEY is not configured")
            result = veo_generate_all_images(
                project_dir=p_dir, mode='banana', clip_ids=target_ids, aspect_ratio=ar,
                preflight=False, silent=True, include_infographics=True,
                client_provider='cheapvibe',
                client_options={'model': getattr(config, 'INFOGRAPHIC_CHEAPVIBE_MODEL', None),
                                'size': getattr(config, 'INFOGRAPHIC_CHEAPVIBE_SIZE', None),
                                'quality': getattr(config, 'INFOGRAPHIC_CHEAPVIBE_QUALITY', None)},
            )
        elif provider == 'varag':
            if not getattr(config, 'VARAG_API_KEY', ''):
                raise RuntimeError("VARAG_API_KEY is not configured")
            result = veo_generate_all_images(
                project_dir=p_dir, mode='banana', clip_ids=target_ids, aspect_ratio=ar,
                preflight=False, silent=True, include_infographics=True,
                client_provider='varag',
            )
        elif provider == 'cinai':
            if not getattr(config, 'CINAI_ACCESS_TOKEN', ''):
                raise RuntimeError("CINAI_ACCESS_TOKEN is not configured")
            result = cinai_generate_all_images(
                project_dir=p_dir,
                clip_ids=target_ids,
                model=_auto_model or getattr(config, 'INFOGRAPHIC_CINAI_MODEL', None),
                aspect_ratio=getattr(config, 'INFOGRAPHIC_CINAI_ASPECT_RATIO', ar),
                quality=getattr(config, 'INFOGRAPHIC_CINAI_QUALITY', '1K'),
                max_parallel=getattr(config, 'CINAI_MAX_PARALLEL', 6),
                silent=True,
                include_infographics=True,
            )
        else:
            raise RuntimeError(f"unknown INFOGRAPHIC_IMG_PROVIDER={provider!r}")
    except Exception as exc:
        print(f"  [Infographic] Generation failed: {exc}")
        logging.debug(traceback.format_exc())
        # Инфографика — ПОСЛЕДНЕЕ звено цепочки: в режиме «только не-AI» за
        # ней никого нет, и упавший провайдер оставлял сцену пустой. Пробуем
        # остальных доступных, прежде чем сдаться (требование 26.08).
        _tried = {provider}
        for _next, _ready in (
            ('cinai', bool(getattr(config, 'CINAI_ACCESS_TOKEN', ''))),
            ('veononstop', _veo_slot_key_present()),
            ('fastgen', bool(getattr(config, 'FASTGEN_API_KEY', ''))),
        ):
            if _next in _tried or not _ready:
                continue
            print(f"  [Infographic] пробую запасной генератор: {_next}")
            try:
                if _next == 'cinai':
                    result = cinai_generate_all_images(
                        project_dir=p_dir, clip_ids=target_ids,
                        model=getattr(config, 'INFOGRAPHIC_CINAI_MODEL', None),
                        aspect_ratio=getattr(config, 'INFOGRAPHIC_CINAI_ASPECT_RATIO', ar),
                        quality=getattr(config, 'INFOGRAPHIC_CINAI_QUALITY', '1K'),
                        max_parallel=getattr(config, 'CINAI_MAX_PARALLEL', 6),
                        silent=True, include_infographics=True)
                elif _next == 'veononstop':
                    result = veo_generate_all_images(
                        project_dir=p_dir, mode='banana', max_parallel=imggen_parallel,
                        clip_ids=target_ids, aspect_ratio=ar,
                        active_keys=_resolve_veo_key(imggen_key_slot),
                        preflight=not _skip_pf, silent=True, include_infographics=True,
                        banana_model_key=getattr(config, 'INFOGRAPHIC_VEONONSTOP_BANANA_MODEL', None),
                        client_provider='veononstop')
                else:
                    _fa, _fd, _ = _resolve_fastgen_slot(imggen_key_slot, allow_dual=True)
                    if not _fa:
                        continue
                    result = generate_all_images(
                        project_dir=p_dir, clip_ids=target_ids, model='GEM_PIX_2',
                        api_key=_fa, dual_key=_fd, preflight=not _skip_pf,
                        silent=True, include_infographics=True)
                print(f"  [Infographic] запасной {_next} отработал")
                break
            except Exception as _exc2:              # noqa: BLE001
                _tried.add(_next)
                print(f"  [Infographic] запасной {_next} тоже не смог: {_exc2}")
        else:
            # Все генераторы легли. Возвращаем сцены АРХИВАМ — это
            # последний рабочий путь (требование 26.08). Помечаем
            # `infographic_failed`, чтобы архивы не отправили их в
            # инфографику повторно: иначе получится петля
            # EPF → Wikimedia → инфографика → EPF.
            print("  [Infographic] ни один генератор не отработал — "
                  "возвращаю сцены архивам (real_photo_pending)")
            try:
                from units.source_ledger import apply_clip_source_delta

                apply_clip_source_delta(
                    Path(p_dir) / 'montage_plan.json',
                    {int(cid): {'source': 'real_photo_pending',
                                'infographic_failed': True}
                     for cid in target_ids},
                    log=lambda message: print(f"  [Infographic] {message}"),
                )
            except Exception as _delta_exc:          # noqa: BLE001
                print(f"  [Infographic] не смог вернуть сцены архивам: {_delta_exc}")
            return {'generated': 0, 'failed': len(target_ids), 'results': [],
                    'error': str(exc), 'returned_to_archives': list(target_ids)}

    materialize_static_images(p_dir)
    print(f"  [Infographic] Done: {result.get('generated', 0)} images -> generated/ + vid_img/")
    return result


def _run_post_pipeline_silent(
    project_dir: str, action: str, prompt_mode: str = 'single',
    use_codex_adapt: bool = False,
) -> None:
    """Запуск пост-пайплайн генерации в silent-режиме (без интерактивных вопросов).

    Args:
        project_dir: Путь к проекту
        action: image-only or explicit image-provider -> video-provider route,
            for example `veo_i2v` with `PIPELINE_IMG_GEN=cinai`
        prompt_mode: 'single' (один промпт на все) или 'perclip' (оригинальные промпты)
        use_codex_adapt: GPT-адаптация промптов для VEO i2v
    """
    if not action:
        return
    if action == 'skip':
        external_stages_enabled = any(
            os.environ.get(key, '').lower() in ('1', 'true', 'yes', 'on')
            for key in (
                'REAL_PHOTO_ENABLED',
                'STOCK_ENABLED',
                'INFOGRAPHIC_ENABLED',
                'MOTION_GRAPHICS_ENABLED',
            )
        )
        if not external_stages_enabled:
            return
        print("  [Silent] Neural generation skipped; running enabled external/static stages")

    # ── Формат кадра ролика (16:9 / 9:16) ──────────────────────────────────
    # Единый источник — VEONONSTOP_ASPECT_RATIO (радио Ratio в GUI). От него
    # считаются целевые размеры для Real Photo / Stock —
    # раньше тут были захардкожены 1920x1080 и вертикальный ролик получал
    # горизонтальные стадии.
    _frame_ar = (config.VEONONSTOP_ASPECT_RATIO or '16:9').strip()
    if _frame_ar == '9:16':
        _frame_w, _frame_h = 1080, 1920
    else:
        _frame_w, _frame_h = 1920, 1080

    # ── Интро-гейты ВСЕХ стадий — до старта стадий и генерации ─────────────
    # Гейт снимает маркер со вступления, а генераторы по этому же маркеру
    # решают, брать клип или пропустить. Пока каждый гейт срабатывал внутри
    # своей (фоновой) стадии, ImageGen успевал прочитать план раньше — видел
    # ещё не снятый маркер, пропускал клип как внешний, а гейт снимал метку в
    # пустоту: кадра не делал никто (26.08: клип 3 ушёл в i2v без картинки).
    try:
        from units.source_ledger import apply_stage_intro_gates
        apply_stage_intro_gates(project_dir, printer=lambda m: print(m))
    except Exception as _gate_exc:                   # noqa: BLE001
        print(f"  [Intro gate] общий барьер пропущен: {_gate_exc}")
        logging.exception("Stage intro gates failed for %s: %s",
                          project_dir, _gate_exc)

    # ── Real Photo stage (between Director output and ImgGen/Veo) ──────────
    # Triggered by env var REAL_PHOTO_ENABLED=1 (set by GUI / .env).
    # Phase 1 (selector): синхронно — Codex picks clips, marks `source: real_photo_pending`
    # в montage_plan. ImgGen/Veo читают marker и пропускают эти клипы.
    # Phase 2 (downloads): daemon thread — параллельно с ImgGen скачивает фото в generated/{id}.png.
    # Перед video_concat (и перед i2v, если он запущен ПОСЛЕ) поток джойнится.
    _real_photo_thread = None
    _real_photo_holder = [None]   # mutable container for summary
    _real_photo_proc = None       # processor instance — needed для thread'а
    _real_photo_selections = None
    import threading as _rp_threading
    _real_photo_stop = _rp_threading.Event()  # сигнал "видео готово — стоп загрузку"
    if os.environ.get('REAL_PHOTO_ENABLED', '0').lower() in ('1', 'true', 'yes'):
        try:
            from units.archive_photos import RealPhotoProcessor
            from pathlib import Path as _PathRP
            print()
            print("  " + "=" * 56)
            print("  REAL PHOTO STAGE — archival photo selector (sync)")
            print("  " + "=" * 56)
            _rp_target = float(os.environ.get('REAL_PHOTO_RATIO_TARGET',
                                              getattr(config, 'REAL_PHOTO_RATIO_TARGET', 0.40)))
            _rp_min    = float(os.environ.get('REAL_PHOTO_RATIO_MIN',
                                              getattr(config, 'REAL_PHOTO_RATIO_MIN', 0.30)))
            _rp_max    = float(os.environ.get('REAL_PHOTO_RATIO_MAX',
                                              getattr(config, 'REAL_PHOTO_RATIO_MAX', 0.55)))
            _rp_srcs_raw = os.environ.get('REAL_PHOTO_SOURCES', '')
            if 'REAL_PHOTO_SOURCES' in os.environ:
                _rp_srcs = [s.strip() for s in _rp_srcs_raw.split(',') if s.strip()]
            else:
                _rp_srcs = getattr(config, 'REAL_PHOTO_SOURCES',
                                   ['wikimedia', 'nasa', 'loc', 'met'])
            _real_photo_proc = RealPhotoProcessor(
                project_dir=_PathRP(project_dir),
                target_ratio=_rp_target, min_ratio=_rp_min, max_ratio=_rp_max,
                sources=_rp_srcs,
                codex_model=getattr(config, 'REAL_PHOTO_CODEX_MODEL', None),
                target_w=_frame_w, target_h=_frame_h,
                fps=getattr(config, 'VIDEO_FPS', 60),
            )
            _real_photo_selections = _real_photo_proc.select_phase()
            if _real_photo_selections:
                # Spawn background thread for downloads — parallel with ImgGen.
                # Общий deadline защищает от вечного зависания; после нейрогенерации
                # _wait_real_photo даёт EPF отдельную короткую фору перед fallback.
                import threading as _rp_th
                import time as _rp_time
                _rp_deadline = _rp_time.time() + int(getattr(config, 'REAL_PHOTO_WAIT_TIMEOUT', 1800))
                def _rp_worker():
                    try:
                        _real_photo_holder[0] = _real_photo_proc.process_phase(
                            _real_photo_selections, stop_event=_real_photo_stop, deadline_ts=_rp_deadline)
                    except Exception as _rpe:
                        print(f"  [Real Photo] BG download failed: {_rpe}")
                        logging.debug(traceback.format_exc())
                        _real_photo_holder[0] = {'error': str(_rpe)}
                _real_photo_thread = _rp_th.Thread(target=_rp_worker, daemon=True, name='real-photo-bg')
                _real_photo_thread.start()
                _rp_to_min = int(getattr(config, 'REAL_PHOTO_WAIT_TIMEOUT', 1800)) // 60
                _rp_tail_wait = max(
                    0, int(getattr(config, 'REAL_PHOTO_PIPELINE_WAIT_TIMEOUT', 100))
                )
                print(f"  [Real Photo] Background download started for {len(_real_photo_selections)} clips")
                print(f"  [Real Photo] Качает параллельно генерации; после генерации ждём ещё "
                      f"{_rp_tail_wait}с, общий лимит {_rp_to_min} мин\n")
            else:
                print("  [Real Photo] No clips selected — skipping\n")
        except Exception as e:
            print(f"  [Real Photo] Selector failed (continuing pipeline): {e}")
            logging.debug(traceback.format_exc())

    # ── Stock stage (между Director и ImgGen/Veo) ──────────────────────────
    # Клипы уже размечены режиссёром в Pass 1 (`source: stock_pending`) — свой
    # селектор не нужен, сразу качаем.
    # Фоновый поток, как real_photo: качает параллельно генерации, не тормозя её.
    # От дырок страхует общий Pre-concat fallback ниже: он ловит клипы по ФАКТУ
    # отсутствия медиа, снимает любой source-маркер и догенеривает картинкой по
    # промпту из Pass 2 (промпты пишутся и для помеченных клипов —
    # STOCK_PROMPT_FALLBACK / REAL_PHOTO_PROMPT_FALLBACK).
    _stock_thread = None
    _stock_holder = [None]
    if os.environ.get('STOCK_ENABLED', 'false').lower() in ('1', 'true', 'yes'):
        try:
            from units.stock_runner import run_stock_stage
            from pathlib import Path as _PathST
            import threading as _st_th
            import time as _st_time
            print()
            print("  " + "=" * 56)
            print("  STOCK STAGE — real footage/photos (background)")
            print("  " + "=" * 56)
            # Свой таймаут, не общий с real_photo: видео качается дольше архивных фото
            _st_timeout = int(os.environ.get('STOCK_WAIT_TIMEOUT',
                                             getattr(config, 'STOCK_WAIT_TIMEOUT', 1800)))
            _st_deadline = _st_time.time() + _st_timeout

            def _stock_worker():
                try:
                    _stock_holder[0] = run_stock_stage(
                        _PathST(project_dir),
                        target_w=_frame_w, target_h=_frame_h,
                        fps=getattr(config, 'VIDEO_FPS', 60),
                        stop_event=_real_photo_stop,   # общая стоп-кнопка пайплайна
                        deadline_ts=_st_deadline,
                    )
                except Exception as _ste:
                    print(f"  [Stock] BG stage failed: {_ste}")
                    logging.debug(traceback.format_exc())
                    _stock_holder[0] = {'error': str(_ste)}

            _stock_thread = _st_th.Thread(target=_stock_worker, daemon=True, name='stock-bg')
            _stock_thread.start()
            print(f"  [Stock] Качает параллельно генерации; стоп = конец генерации ИЛИ "
                  f"{_st_timeout // 60} мин (что раньше)")
            from units.source_quota import ai_allowed as _st_ai_allowed
            print("  [Stock] Недокачанные → догенерация по промпту режиссёра\n" if _st_ai_allowed()
                  else "  [Stock] Недокачанные → повтор поиска без вижна, затем кадр соседнего "
                       "клипа (нейрогенерация выключена)\n")
        except Exception as e:
            print(f"  [Stock] стадия не стартовала (продолжаю без стока): {e}")
            logging.debug(traceback.format_exc())

    # ── Моушн-графика (Remotion + Codex), фоном ────────────────────────────
    # Клипы размечены режиссёром (`source: motion_graphics_pending`); генераторы
    # их пропускают как внешние. Стадия кладёт готовое видео в vid_img/, а то, что
    # не вышло, подберёт Pre-concat fallback по промпту Pass 2 — как у стока.
    _mograph_thread = None
    _mograph_holder = [None]
    _mograph_stop = _rp_threading.Event()
    if getattr(config, 'MOTION_GRAPHICS_ENABLED', False):
        try:
            from units.mograph_runner import run_motion_graphics_stage

            def _mograph_worker():
                try:
                    _mograph_holder[0] = run_motion_graphics_stage(
                        project_dir, target_w=_frame_w, target_h=_frame_h,
                        fps=getattr(config, 'VIDEO_FPS', 60), stop_event=_mograph_stop)
                except Exception as _mge:
                    print(f"  [Motion graphics] стадия упала: {_mge}")
                    logging.debug(traceback.format_exc())
                    _mograph_holder[0] = {'error': str(_mge)}

            print()
            print("  " + "=" * 56)
            print("  MOTION GRAPHICS STAGE — Codex + Remotion (background)")
            print("  " + "=" * 56)
            _mograph_thread = _rp_threading.Thread(target=_mograph_worker, daemon=True,
                                                   name='mograph-bg')
            _mograph_thread.start()
        except Exception as e:
            print(f"  [Motion graphics] стадия не стартовала (продолжаю без неё): {e}")
            logging.debug(traceback.format_exc())

    # ── Параллакс готовым кадрам, фоном (28.08) ────────────────────────────
    # Кому достаётся параллакс, решают галки рядом с ним: сток и Real Photo
    # всегда, нейрокадры — только при выключенной видеогенерации (при
    # работающем i2v их оживляет обычная стадия, она же ставит параллакс-промпт).
    # Вотчер не ждёт конца стадий: список «каждый N-й» считается сразу по плану,
    # а кадры уходят на оживление по мере появления картинок.
    _parallax_thread = None
    _parallax_stop = _rp_threading.Event()
    _vq_keys: list = []          # ключи видеоочереди; пусто = очередь не поднялась
    # ── Общая очередь генерации видео на прогон ────────────────────────────
    # Рабочие нанимаются ОДИН раз, и все поставщики задач кладут работу сюда:
    # стадия i2v и параллакс. Раньше каждый заводил свой пул и считал слоты
    # ключа сам, не зная про других, — отсюда взаимное ожидание и обнуление
    # учёта задач в полёте. Очередь берётся по ключам видеостадии: слоты
    # принадлежат аккаунту, значит и считать их надо на ключ.
    # Очередь видео (и чистка «зомби-задач» на ключе при её старте) нужна, только
    # если в прогоне будет видео: маршрут с i2v/t2v или параллакс. Раньше она
    # поднималась при любом ключе VeoNonStop — даже с видео «Пропустить»
    # (прогон 10.10: сток 100%, генерация выключена, а VeoNonStop всё равно дёргался).
    _video_route = action not in ('skip', 'fastgen', 'imggen', 'cinai')
    _vq_needed = _video_route or bool(getattr(config, 'VEONONSTOP_I2V_PARALLAX', False))
    try:
        if _vq_needed and _has_any_veo_key():
            from units.motion_queue import get_queue as _get_video_queue
            _vq_keys = _resolve_veo_key(
                os.environ.get('BEGO_VIDEO_KEY_SLOT')
                or os.environ.get('VEONONSTOP_VIDEO_KEY_SLOT')
                or '1'
            )
            from units.veo_motion import (
                apply_key_limits as _apply_veo_limits,
                build_clients as _build_veo_clients,
            )
            # Клиенты поднимаем ОДИН раз на прогон и кладём в очередь: и стадия
            # i2v, и параллакс возьмут готовых. Второй набор на те же ключи
            # обнулял бы учёт задач в полёте (модульные `_inflight_by_key`).
            # Чистка зомби здесь же — видеозадач этого прогона ещё нет, а
            # мусор от прошлых убрать надо, как это делала стадия при старте.
            _vq_clients = _build_veo_clients(_vq_keys, no_cancel=False)
            _apply_veo_limits(_vq_clients)
            # Сумма `mp` по ключам — это уже выбор человека: значение из поля
            # «Parallel» применено к каждому ключу в `build_clients`
            # (`mp = min(mp_cfg, ct)`). Ветка после `or` — страховка на случай
            # пустой суммы. Ограничивать эту сумму общим потолком НЕЛЬЗЯ:
            # раскладка задач по ключам идёт по номеру рабочего, и урезание
            # числа рабочих оставляет второй ключ без работы (проверено живым
            # прогоном 01.09.2026 — все 14 клипов ушли на Key1).
            _vq_workers = sum(c['mp'] for c in _vq_clients) or int(
                os.environ.get('BEGO_VIDEO_PARALLEL')
                or os.environ.get('VEONONSTOP_MAX_PARALLEL')
                or getattr(config, 'VEONONSTOP_MAX_PARALLEL', 4)
            )
            _video_queue = _get_video_queue(_vq_keys, workers=_vq_workers,
                                            clients_cfg=_vq_clients)
            print(f"  [Очередь видео] одна на прогон: рабочих {_vq_workers}, "
                  f"ключ(и) " + ', '.join(f'Key{k}' for k in _vq_keys))

            # ── Шлюз: можно ли оживлять, пока идут картинки ────────────────
            # Правило вынесено в `video_queue_hold_reason`, чтобы его можно было
            # проверить ровно на том окружении, с которым стадию запускает GUI.
            # Шлюз снимается в `_mark_inflight_imagegen_done` — общей точке всех
            # веток пост-действий.
            _hold_reason = video_queue_hold_reason(_vq_keys)
            if _hold_reason:
                _video_queue.hold(_hold_reason)
                print(f"  [Очередь видео] ждёт конца генерации кадров: "
                      f"{_hold_reason}")
            else:
                print("  [Очередь видео] оживляем по мере появления кадров")
    except Exception as _vqe:                              # noqa: BLE001
        # Без очереди стадии просто работают своими пулами, как раньше.
        print(f"  [Очередь видео] не создана ({_vqe}) — стадии пойдут "
              f"своими пулами")
        logging.debug(traceback.format_exc())
    # Единый список «кого паралаксить» — считаем ОДИН раз, здесь: маркеры стадий
    # уже проставлены (Pass 1 + селектор Real Photo), интро-гейты отработали.
    # Список ложится файлом в проект, и дальше по нему работают ОБА пути —
    # наблюдатель и стадия i2v. Раньше каждый считал сам и по своему правилу:
    # наблюдатель по позиции в списке, стадия по «номер клипа делится на N», —
    # и на смешанном ролике нейрокадрам могло не достаться ни одного параллакса.
    try:
        if getattr(config, 'VEONONSTOP_I2V_PARALLAX', False):
            from units import parallax_schedule as _px_plan_mod
            _px_plan_data = _px_plan_mod.build(project_dir)
            _px_ai_hits = len(set(_px_plan_data['ids'])
                              & set(_px_plan_data['ai_eligible']))
            _px_ext_hits = len(_px_plan_data['ids']) - _px_ai_hits
            print(f"  [Параллакс] список на прогон: {len(_px_plan_data['ids'])} "
                  f"кадр(ов) из {len(_px_plan_data['eligible'])} отмеченных "
                  f"(каждый {_px_plan_data['every_n']}-й) — "
                  f"нейрокадров {_px_ai_hits}, сток/архивы {_px_ext_hits}")
    except Exception as _pxe:                              # noqa: BLE001
        # Параллакс — украшение: без списка кадры просто останутся картинками.
        print(f"  [Параллакс] список не посчитан ({_pxe}) — "
              f"каждый путь возьмёт свой расчёт")
        logging.debug(traceback.format_exc())
    try:
        if getattr(config, 'VEONONSTOP_I2V_PARALLAX', False) and not _has_any_veo_key():
            from units.source_ledger import parallax_photo_sources as _px_photo_sources
            if _px_photo_sources():
                # нейрокадрам параллакс даёт и CinAI, а отдельный проход по сток-фото — только VeoNonStop
                print("  [Параллакс] сток-фото оживляются только через VeoNonStop, а его ключа нет — "
                      "они останутся картинками с зумом")
        if getattr(config, 'VEONONSTOP_I2V_PARALLAX', False) and _has_any_veo_key():
            from units.source_ledger import parallax_photo_sources
            _px_sources = parallax_photo_sources()
            _px_video_off = (
                str(os.environ.get('PIPELINE_VIDEO_MODE')
                    or getattr(config, 'PIPELINE_VIDEO_MODE', '')
                    or '').strip().lower() == 'skip'
            )
            _px_ai = bool(
                getattr(config, 'VEONONSTOP_I2V_PARALLAX_AI', True)
            ) and _px_video_off
            if _px_sources or _px_ai:
                from units.parallax_autopick import watch_parallax as _watch_px

                # Ключи — те же, что отмечены для видеостадии. Без этого
                # генератор подставлял слот 1, и параллакс уходил на первый
                # ключ независимо от чекбоксов: пользователь развёл картинки и
                # видео по разным ключам, а вотчер всё равно лез туда, где идёт
                # ImageGen, и они делили слоты одного аккаунта.
                _px_keys = _resolve_veo_key(
                    os.environ.get('BEGO_VIDEO_KEY_SLOT')
                    or os.environ.get('VEONONSTOP_VIDEO_KEY_SLOT')
                    or '1'
                )

                def _parallax_worker():
                    try:
                        _watch_px(project_dir, veo_generate_all_videos,
                                  _px_sources, include_ai=_px_ai,
                                  stop_event=_parallax_stop,
                                  active_keys=_px_keys)
                    except Exception as _pxe:              # noqa: BLE001
                        # Кадр просто останется картинкой с зумом — прогон целее.
                        print(f"  [Параллакс] вотчер упал: {str(_pxe)[:200]}")
                        logging.debug(traceback.format_exc())

                _parallax_thread = _rp_threading.Thread(
                    target=_parallax_worker, daemon=True, name='parallax-bg')
                _parallax_thread.start()
                _px_names = ', '.join(
                    sorted(_px_sources) + (['нейрокадры'] if _px_ai else []))
                print(f"  [Параллакс] фоном: {_px_names} | ключ(и): "
                      + ', '.join(f'Key{k}' for k in _px_keys))
    except Exception as _pxe:                              # noqa: BLE001
        print(f"  [Параллакс] не стартовал: {_pxe}")
        logging.debug(traceback.format_exc())

    def _stage_media_count(where) -> int:
        """Сколько сцен уже закрыто материалом — признак ЖИВОЙ работы стадии.

        Считаем файлы в `vid_img/` и `generated/`: растёт число — стадия
        работает, значит ждём дальше. Стоит на этом, а не на внутреннем
        счётчике стадии, потому что видно снаружи и одинаково годится любому
        источнику (сток, ютуб, архивы).
        """
        try:
            base = Path(where)
            total = 0
            for sub, mask in (('vid_img', '*.mp4'), ('generated', '*.*')):
                folder = base / sub
                if folder.is_dir():
                    total += sum(1 for _ in folder.glob(mask))
            return total
        except Exception:            # noqa: BLE001 — счётчик не должен ронять ожидание
            return -1

    def _wait_stock(*, label="i2v"):
        """Стоп + join фоновой стоковой стадии. Вызывается перед всем, что пишет
        в vid_img/ или читает montage_plan для генерации."""
        nonlocal _stock_thread
        if _stock_thread is None:
            return
        if _stock_thread.is_alive():
            # Сначала ДАЁМ стоку доработать, как ютубу (задание 28.08).
            # Раньше стоп прилетал мгновенно: на короткой генерации стадия
            # не успевала пройти очередь, и хвост клипов уходил в
            # нейрогенерацию с причиной «не дошла очередь» (прогон 28.08,
            # клипы 34 и 36).
            # СТОРОЖ ТИШИНЫ, а не глухой отсчёт (решение пользователя
            # 01.09). Прежние 500 с отсчитывались от конца генерации — и
            # это неверный якорь: когда всё размечено на сток, генерации
            # нет вовсе, она «заканчивается» мгновенно и тут же запускает
            # стоку обратный отсчёт. На часовом ролике сток получил бы 500
            # секунд на сотни сцен, а остальное свалилось бы в
            # нейрогенерацию. Уже на 14 клипах прогон cheese5 потерял так
            # клип 3 с причиной «не дошла очередь».
            #
            # Теперь ждём, ПОКА СТАДИЯ РАБОТАЕТ: каждая закрытая сцена
            # продлевает ожидание. Обрываем только по тишине — когда за
            # `STOCK_IDLE_TIMEOUT` не закрылось ни одной. Старый ключ
            # остаётся ВЕРХНИМ пределом на клинический случай.
            _idle_limit = max(60, int(getattr(
                config, 'STOCK_IDLE_TIMEOUT', 300)))
            _hard_limit = max(0, int(getattr(
                config, 'STOCK_PIPELINE_WAIT_TIMEOUT', 500)))
            if _stock_thread.is_alive():
                print(f"  [Stock] {label}: остальные стадии готовы; жду, пока "
                      f"сток закрывает сцены (обрыв только после {_idle_limit}с "
                      f"без единой новой)")
                _started = time.time()
                _last_change = time.time()
                _seen = _stage_media_count(project_dir)
                while _stock_thread.is_alive():
                    _stock_thread.join(timeout=15)
                    _now = _stage_media_count(project_dir)
                    if _now != _seen:
                        _seen = _now
                        _last_change = time.time()
                        continue
                    _quiet = time.time() - _last_change
                    if _quiet >= _idle_limit:
                        print(f"  [Stock] {label}: тишина {int(_quiet)}с — "
                              f"стадия больше ничего не закрывает, останавливаю")
                        break
                    if _hard_limit and (time.time() - _started) >= _hard_limit * 10:
                        print(f"  [Stock] {label}: верхний предел "
                              f"{_hard_limit * 10}с исчерпан, останавливаю")
                        break
            if _stock_thread.is_alive():
                from units.source_quota import ai_allowed as _ws_ai
                print(f"  [Stock] {label}: стоп загрузку (недокачанные → "
                      + ("догенерация по промпту)" if _ws_ai() else "запасной путь без нейрогенерации)"))
                _real_photo_stop.set()
                _stock_thread.join(timeout=120)   # грейс на завершение текущего клипа
            if _stock_thread.is_alive():
                print("  [Stock] wait expired; unresolved clips remain non-AI pending")
                _real_photo_stop.set()
                _stock_thread.join(timeout=15)
        st = _stock_holder[0]
        if isinstance(st, dict) and not st.get('error') and st.get('total'):
            from units.source_quota import ai_allowed as _ws_ai2
            print(f"  [Stock] итог: {st.get('ok', 0)} из стока, "
                  f"{st.get('fallback', 0)} → "
                  + ("нейрогенерация" if _ws_ai2() else "запасной путь без нейрогенерации"))
        _stock_thread = None   # idempotent: повторные вызовы — no-op

    def _wait_mograph(*, label="i2v"):
        """Дождаться моушн-графики до Pre-concat fallback и склейки.

        Сторож тишины, как у стока: пока стадия закрывает клипы, ждём; обрываем,
        когда за время одного клипа (Codex + рендер) не готов ни один. Остановленные
        клипы стадия не трогает — их подберёт Pre-concat fallback."""
        nonlocal _mograph_thread
        if _mograph_thread is None:
            return
        if _mograph_thread.is_alive():
            idle_limit = (int(getattr(config, 'MOTION_GRAPHICS_CODEX_TIMEOUT', 900))
                          + int(getattr(config, 'MOTION_GRAPHICS_RENDER_TIMEOUT', 600)) + 120)
            print(f"  [Motion graphics] {label}: остальные стадии готовы; жду графику "
                  f"(обрыв после {idle_limit}с без нового клипа)")
            last_change, seen = time.time(), _stage_media_count(project_dir)
            while _mograph_thread.is_alive():
                _mograph_thread.join(timeout=15)
                now = _stage_media_count(project_dir)
                if now != seen:
                    seen, last_change = now, time.time()
                elif time.time() - last_change >= idle_limit:
                    print(f"  [Motion graphics] {label}: тишина {int(time.time() - last_change)}с — останавливаю")
                    break
        if _mograph_thread.is_alive():
            _mograph_stop.set()
            _mograph_thread.join(timeout=60)
        mg = _mograph_holder[0]
        if isinstance(mg, dict) and mg.get('total'):
            print(f"  [Motion graphics] итог: {mg.get('ok', 0)} графика, "
                  f"{mg.get('failed', 0)} → нейрогенерация")
        _mograph_thread = None


    def _wait_real_photo(*, label="i2v"):
        """Give Real Photo a bounded final window before enabling AI fallback."""
        nonlocal _real_photo_thread
        if _real_photo_thread is None:
            return
        if _real_photo_thread.is_alive():
            _rp_tail_wait = max(0, int(
                getattr(
                    config,
                    'REAL_PHOTO_PIPELINE_WAIT_TIMEOUT',
                    100,
                )
            ))
            # Сторож тишины (как у стока и ютуба). Прежние 100 секунд были
            # особенно жестоки: EPF ищет по 20-40 секунд на клип, и три сцены
            # уже не укладывались. Ждём, пока стадия закрывает кадры, обрываем
            # по молчанию; старый ключ — верхний предел.
            _rp_idle = max(60, int(getattr(config, 'REAL_PHOTO_IDLE_TIMEOUT', 300)))
            _rp_hard = _rp_tail_wait * 10 if _rp_tail_wait else 0
            print(f"  [Real Photo] {label}: жду, пока EPF закрывает кадры "
                  f"(обрыв после {_rp_idle}с тишины)")
            _rp_started = time.time()
            _rp_last = time.time()
            _rp_seen = _stage_media_count(project_dir)
            while _real_photo_thread.is_alive():
                _real_photo_thread.join(timeout=15)
                _rp_now = _stage_media_count(project_dir)
                if _rp_now != _rp_seen:
                    _rp_seen = _rp_now
                    _rp_last = time.time()
                    continue
                _rp_quiet = time.time() - _rp_last
                if _rp_quiet >= _rp_idle:
                    print(f"  [Real Photo] {label}: тишина {int(_rp_quiet)}с — "
                          f"стадия ничего не закрывает, останавливаю")
                    break
                if _rp_hard and (time.time() - _rp_started) >= _rp_hard:
                    print(f"  [Real Photo] {label}: верхний предел {_rp_hard}с исчерпан")
                    break
        if _real_photo_thread.is_alive():
            print("  [Real Photo] финальный wait истёк — стоп остаток, "
                  "недокачанные → догенерация")
            _real_photo_stop.set()
            _real_photo_thread.join(timeout=15)
            if _real_photo_thread.is_alive():
                print("  [Real Photo] поток не завершился за 15с после стопа — иду дальше")
        rp = _real_photo_holder[0]
        if rp and 'rendered' in rp:
            print(f"  [Real Photo] Done: {rp.get('rendered', 0)}/{rp.get('selected', 0)} rendered "
                  f"({rp.get('downloaded_mb', 0)} MB)")
        elif rp and 'error' in rp:
            print(f"  [Real Photo] WARNING: download failed: {rp.get('error')}")
        _real_photo_thread = None  # idempotent: future calls no-op

    p_dir = str(project_dir)
    ar = config.VEONONSTOP_ASPECT_RATIO

    # GUI may override key slot and parallel via env vars
    imggen_key_slot  = (os.environ.get('BEGO_IMGGEN_KEY_SLOT')
                        or os.environ.get('VEONONSTOP_IMGGEN_KEY_SLOT')
                        or '1')
    video_key_slot   = (os.environ.get('BEGO_VIDEO_KEY_SLOT')
                        or os.environ.get('VEONONSTOP_VIDEO_KEY_SLOT')
                        or '1')
    imggen_parallel  = int(os.environ.get('BEGO_IMGGEN_PARALLEL',
                                          os.environ.get('VEONONSTOP_MAX_PARALLEL',
                                                         config.VEONONSTOP_MAX_PARALLEL)))
    video_parallel   = int(os.environ.get('BEGO_VIDEO_PARALLEL',
                                          os.environ.get('VEONONSTOP_MAX_PARALLEL',
                                                         config.VEONONSTOP_MAX_PARALLEL)))
    inflight_mode    = os.environ.get('BEGO_INFLIGHT_MODE', '0') == '1'
    fg_dual_key      = os.environ.get('FASTGEN_DUAL_KEY', '0') == '1'
    vislide_interval = int(os.environ.get('BEGO_VISLIDE_INTERVAL', '0'))
    _UPS_MAP = {'2k': 'UPSAMPLE_IMAGE_RESOLUTION_2K', '4k': 'UPSAMPLE_IMAGE_RESOLUTION_4K'}
    imggen_upscale   = _UPS_MAP.get(config.VEONONSTOP_IMAGE_UPSCALE.lower())
    _inflight_done_marker = Path(p_dir) / 'generated' / '.imagegen_done'
    if inflight_mode:
        _inflight_done_marker.unlink(missing_ok=True)

    def _mark_inflight_imagegen_done() -> None:
        # Картинки досчитались — открываем очередь видео, если она ждала. Это
        # общая точка всех веток пост-действий, поэтому шлюз снимается ровно
        # один раз и там, где ImageGen действительно закончил.
        try:
            from units.motion_queue import release_all as _release_video_queues
            _release_video_queues('ImageGen закончил')
        except Exception as _vqe:                          # noqa: BLE001
            logging.debug("Очередь видео: шлюз не снят: %s", _vqe)
        if not inflight_mode:
            return
        try:
            _inflight_done_marker.parent.mkdir(parents=True, exist_ok=True)
            _inflight_done_marker.write_text(str(time.time()), encoding='utf-8')
        except OSError as exc:
            print(f"  [INFLIGHT] Could not write ImageGen done marker: {exc}")

    # Legacy: VEONONSTOP_MAX_PARALLEL still applies when not overridden
    parallel = video_parallel

    # ── Codex VEO adapt (parallel with imggen) ──────────────────────────
    import threading as _th
    _adapt_result = [None]
    _adapt_event = _th.Event()

    def _run_adapt():
        try:
            from units.codex_motion_adapter import adapt_prompts_for_veo
            _adapt_result[0] = adapt_prompts_for_veo(p_dir)
        except Exception as e:
            print(f"  [codex-adapt] Failed: {e}")
        finally:
            _adapt_event.set()


    _should_adapt = (use_codex_adapt
                     and prompt_mode == 'perclip'
                     and action in ('fastgen_i2v', 'veo_i2v'))
    if _should_adapt:
        _th.Thread(target=_run_adapt, daemon=True, name='codex-adapt').start()
        print("  [codex-adapt] VEO prompt adaptation started in background")
    else:
        _adapt_event.set()
    # ────────────────────────────────────────────────────────────────────

    def _get_vislide_clip_ids() -> list:
        """Читает montage_plan.json и возвращает только анимируемые clip_ids для vislide."""
        import json as _json
        plan_path = Path(project_dir) / 'montage_plan.json'
        if not plan_path.exists():
            return []
        try:
            data = _json.loads(plan_path.read_text(encoding='utf-8-sig'))
            plan = data.get('montage_plan', data)
            clips = plan.get('clips', [])
        except Exception:
            return []
        intro_dur = config.INTRO_BLOCK_DURATION
        animated = _vislide_clip_ids(clips, vislide_interval, intro_dur=intro_dur)
        n_intro = sum(1 for c in clips if c.get('startTime', 0) < intro_dur)
        print(f"  [Vislide] interval={vislide_interval}, intro={intro_dur}s ({n_intro} clips): "
              f"{len(animated)}/{len(clips)} clips → i2v")
        return animated

    def _vislide_copy_static(animated_ids: list, output_dir_name: str = 'vid_img'):
        """Копирует картинки не-анимируемых клипов из generated/ → vid_img/ как статик-фолбек."""
        from units.vislide import copy_static
        copied = copy_static(project_dir, animated_ids, output_dir_name)
        if copied:
            print(f"  [Vislide] Static fallback: {copied} images copied to {output_dir_name}/")

    # Инфографика — общие функции модуля (их же зовёт стадия «Картинки» Панели управления)
    def _apply_infographic_intro_gate_here() -> None:
        _apply_infographic_intro_gate(p_dir)

    def _run_infographic_images_here(clip_ids: list[int] | None = None) -> dict:
        return _run_infographic_images(p_dir, clip_ids)

    # `_has_any_veo_key` здесь БОЛЬШЕ НЕ ОБЪЯВЛЯЕМ — используется модульная
    # версия (она дословно та же). Локальная копия делала имя локальным для
    # всей функции, и обращение к нему выше по коду падало с UnboundLocalError:
    # фоновый параллакс (строка ~1483) вызывает эту проверку ДО того места, где
    # стояло определение, поэтому вотчер не стартовал ни разу с 28.08 —
    # в логе значилось безобидное «[Параллакс] не стартовал: cannot access
    # local variable» (лог пользователя 29.08).

    def _count_generated_images() -> int:
        gen_dir = Path(project_dir) / 'generated'
        if not gen_dir.exists():
            return 0
        return sum(1 for ext in ('.png', '.jpg', '.jpeg', '.webp') for _ in gen_dir.glob(f'*{ext}'))

    def _image_exists(clip_id: int) -> bool:
        gen_dir = Path(project_dir) / 'generated'
        return any(
            (gen_dir / f"{int(clip_id):03d}{ext}").is_file()
            for ext in ('.png', '.jpg', '.jpeg', '.webp')
        )

    def _run_t2v_image_fallback(image_result: dict) -> Optional[dict]:
        """Generate videos for image IDs left by a terminal quota fallback."""
        if not image_result or not image_result.get('t2v_fallback_required'):
            return None
        clip_ids = sorted({
            int(cid) for cid in image_result.get('failed_ids', [])
            if not _image_exists(int(cid))
        })
        if not clip_ids:
            return {'generated': 0, 'failed': 0, 'failed_ids': []}
        if not _has_any_veo_key():
            print("  [T2V FALLBACK] Cannot start: no VeoNonStop video key configured")
            return {
                'generated': 0,
                'failed': len(clip_ids),
                'failed_ids': clip_ids,
            }
        prompts_file_path = None
        if _should_adapt:
            if not _adapt_event.wait(timeout=600):
                print("  [codex-adapt] Timeout - T2V fallback uses original prompts")
            else:
                prompts_file_path = _adapt_result[0]
        reason = image_result.get('t2v_fallback_reason') or 'image capacity exhausted'
        print(
            f"  [T2V FALLBACK] {reason}: generating {len(clip_ids)} remaining clip(s)"
        )
        return veo_generate_all_videos(
            project_dir=p_dir,
            mode='t2v',
            max_parallel=video_parallel,
            aspect_ratio=ar,
            single_prompt=_video_single_prompt(),
            active_keys=_resolve_veo_key(video_key_slot),
            clip_ids=clip_ids,
            silent=True,
            prompts_file_path=prompts_file_path,
        )

    def _call_image_provider(
        provider: str,
        *,
        use_claims: bool = False,
        clip_ids=None,
    ):
        """Run one image provider with the current pipeline settings."""
        provider = str(provider or '').strip().lower()
        if provider == 'fastgen':
            if not getattr(config, 'FASTGEN_API_KEY', ''):
                raise RuntimeError('FASTGEN_API_KEY not set for ImageGen')
            _img_api, _img_dual, _img_slot = _resolve_fastgen_slot(
                imggen_key_slot, allow_dual=True)
            return generate_all_images(
                project_dir=p_dir,
                clip_ids=clip_ids,
                api_key=_img_api,
                dual_key=_img_dual,
                preflight=not _skip_pf,
                silent=True,
                use_claims=use_claims,
                claim_owner='fastgen',
                allow_veo_fallback=not use_claims,
            )
        if provider == 'veononstop':
            if not _has_any_veo_key():
                raise RuntimeError('VeoNonStop key not set for ImageGen')
            return veo_generate_all_images(
                project_dir=p_dir,
                clip_ids=clip_ids,
                mode=config.VEONONSTOP_IMGGEN_MODE,
                max_parallel=imggen_parallel,
                aspect_ratio=ar,
                active_keys=_resolve_veo_key(imggen_key_slot),
                upscale_resolution=imggen_upscale,
                preflight=not _skip_pf,
                silent=True,
                max_cycles=(
                    getattr(config, 'VEONONSTOP_MAX_SILENT_CYCLES', 3)
                    if use_claims else None
                ),
                use_claims=use_claims,
                claim_owner='veononstop',
            )
        if provider == 'cinai':
            if not getattr(config, 'CINAI_ACCESS_TOKEN', ''):
                raise RuntimeError('CINAI_ACCESS_TOKEN not set for ImageGen')
            return cinai_generate_all_images(
                project_dir=p_dir,
                clip_ids=clip_ids,
                max_parallel=getattr(config, 'CINAI_MAX_PARALLEL', 6),
                model=config.CINAI_MODEL,
                aspect_ratio=(
                    getattr(config, 'CINAI_ASPECT_RATIO', '') or ar or '16:9'
                ),
                quality=getattr(config, 'CINAI_QUALITY', '1K'),
                silent=True,
                use_claims=use_claims,
                claim_owner='cinai',
            )
        if provider == 'skip':
            return {'generated': _count_generated_images(), 'failed': 0}
        raise RuntimeError(f'Unknown ImageGen provider: {provider!r}')

    def _run_image_generation(provider: str, *, clip_ids=None):
        """Run the selected ImageGen, optionally sharing the pool with CinAI."""
        # Обложка кладётся в generated/ ДО генерации: её клип помечен как
        # готовый, генератор такой пропустит — рисовать нечего, картинка
        # пришла файлом от пользователя.
        try:
            from units import cover_opener as cover_hook

            cover_hook.stage_cover_image(p_dir)
        except Exception as _cover_exc:                     # noqa: BLE001
            print(f"  [Обложка] не подложена: {_cover_exc}")

        provider = str(provider or '').strip().lower()
        pool_enabled = (
            str(os.environ.get(
                'CINAI_IMAGE_POOL_ENABLED',
                '1' if getattr(config, 'CINAI_IMAGE_POOL_ENABLED', False) else '0',
            )).strip().lower() in ('1', 'true', 'yes', 'on')
            and provider in ('fastgen', 'veononstop')
        )
        if not pool_enabled:
            return _call_image_provider(provider, clip_ids=clip_ids)
        if not getattr(config, 'CINAI_ACCESS_TOKEN', ''):
            print("  [+CinAI] CINAI_ACCESS_TOKEN is empty; using only the primary ImageGen")
            return _call_image_provider(provider, clip_ids=clip_ids)

        from concurrent.futures import ThreadPoolExecutor

        print(
            f"  [+CinAI] Shared image pool: {provider} + CinAI "
            f"({config.CINAI_MODEL}, "
            f"x{getattr(config, 'CINAI_MAX_PARALLEL', 6)})"
        )
        provider_results = {}
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix='image-pool') as pool:
            futures = {
                pool.submit(
                    _call_image_provider, provider, use_claims=True,
                    clip_ids=clip_ids,
                ): provider,
                pool.submit(
                    _call_image_provider, 'cinai', use_claims=True,
                    clip_ids=clip_ids,
                ): 'cinai',
            }
            for future, label in futures.items():
                try:
                    provider_results[label] = future.result()
                except Exception as exc:
                    provider_results[label] = {
                        'generated': 0,
                        'failed': 0,
                        'failed_ids': [],
                        'error': str(exc),
                    }
                    print(f"  [+CinAI] {label} failed: {exc}")
                    logging.exception("Image pool provider %s failed", label)

        primary_failed_ids = [
            int(cid)
            for cid in provider_results.get(provider, {}).get('failed_ids', [])
            if not _image_exists(int(cid))
        ]
        if primary_failed_ids:
            print(
                f"  [+CinAI] {provider} failed {len(primary_failed_ids)} clip(s); "
                "returning them to CinAI once"
            )
            try:
                provider_results['cinai_retry'] = _call_image_provider(
                    'cinai',
                    clip_ids=primary_failed_ids,
                )
            except Exception as exc:
                provider_results['cinai_retry'] = {
                    'generated': 0,
                    'failed': len(primary_failed_ids),
                    'failed_ids': primary_failed_ids,
                    'error': str(exc),
                }
                print(f"  [+CinAI] CinAI retry failed to start: {exc}")

        cinai_result = provider_results.get('cinai', {})
        cinai_failed_ids = [
            int(cid) for cid in cinai_result.get('failed_ids', [])
            if not _image_exists(int(cid))
        ]
        cinai_failed_ids.extend(
            int(cid)
            for cid in provider_results.get('cinai_retry', {}).get(
                'failed_ids', [])
            if not _image_exists(int(cid))
        )
        cinai_failed_ids = sorted(set(cinai_failed_ids))
        fallback_result = {}
        if cinai_failed_ids:
            if _has_any_veo_key():
                print(
                    f"  [+CinAI] CinAI failed {len(cinai_failed_ids)} clip(s); "
                    "sending them directly to VeoNonStop"
                )
                fallback_result = veo_generate_all_images(
                    project_dir=p_dir,
                    mode=config.VEONONSTOP_IMGGEN_MODE,
                    max_parallel=imggen_parallel,
                    clip_ids=cinai_failed_ids,
                    aspect_ratio=ar,
                    active_keys=_resolve_veo_key(imggen_key_slot),
                    upscale_resolution=imggen_upscale,
                    preflight=not _skip_pf,
                    silent=True,
                    max_cycles=getattr(
                        config, 'VEONONSTOP_MAX_SILENT_CYCLES', 3),
                    use_claims=False,
                )
            else:
                print(
                    f"  [+CinAI] VeoNonStop fallback unavailable for "
                    f"{len(cinai_failed_ids)} CinAI failure(s): no API key"
                )

        failed_candidates = set()
        for item in provider_results.values():
            failed_candidates.update(
                int(cid) for cid in item.get('failed_ids', []))
        failed_candidates.update(
            int(cid) for cid in fallback_result.get('failed_ids', []))
        unresolved = sorted(
            cid for cid in failed_candidates if not _image_exists(cid))
        result = {
            'generated': _count_generated_images(),
            'failed': len(unresolved),
            'failed_ids': unresolved,
            'results': provider_results,
            'cinai_fallback': fallback_result,
            't2v_fallback_required': any(
                item.get('t2v_fallback_required')
                for item in list(provider_results.values()) + [fallback_result]
            ),
            't2v_fallback_reason': next(
                (
                    item.get('t2v_fallback_reason')
                    for item in list(provider_results.values()) + [fallback_result]
                    if item.get('t2v_fallback_required')
                ),
                '',
            ),
        }
        print(
            f"  [+CinAI] Pool complete: images={result['generated']}, "
            f"failed={result['failed']}"
        )
        return result

    def _video_single_prompt() -> Optional[str]:
        if prompt_mode != 'single':
            return None
        sp_path = Path(p_dir) / 'single_prompt.txt'
        if sp_path.exists():
            text = sp_path.read_text(encoding='utf-8-sig').strip()
            if text:
                print(f"  Using single_prompt.txt: {text[:50]}...")
                return text
        return 'default'

    def _run_cinai_video_stage(
        mode: str,
        *,
        output_dir_name: str = "vid_img",
        reference_dir: Optional[str] = None,
        prompts_file_path: Optional[str] = None,
    ) -> dict:
        if not getattr(config, 'CINAI_VIDEO_ENABLED', False):
            print("  [SKIP] CINAI_VIDEO_ENABLED is false")
            return {'generated': 0, 'failed': 0, 'results': [], 'failed_ids': []}
        if not getattr(config, 'CINAI_ACCESS_TOKEN', ''):
            print("  [SKIP] CINAI_ACCESS_TOKEN not set for VideoGen")
            return {'generated': 0, 'failed': 0, 'results': [], 'failed_ids': []}
        print(
            f"  Starting CinAI VideoGen "
            f"(model={getattr(config, 'CINAI_VIDEO_MODEL', '')}, mode={mode})..."
        )
        # состояние провайдера ДО старта: он то отдаёт клип за 40с, то уходит в
        # шторм на 4-5 минут с падениями. Без этой строки медленный прогон
        # выглядит как поломка пайплайна.
        try:
            from units.cinai_probe import describe as _cinai_describe
            print(f"  {_cinai_describe()}")
        except Exception as _exc:
            logging.debug("CinAI health check skipped: %s", _exc)
        # Вислайд: оживляем только свои клипы, остальные остаются слайдами.
        # Без этого CinAI оживлял ВСЕ сцены, и режим не работал.
        _vislide_ids = None
        if vislide_interval >= 2 and mode in ('i2v', 'first_last'):
            _vislide_ids = _get_vislide_clip_ids()
            if not _vislide_ids:
                print("  [Vislide] no clips to animate — CinAI VideoGen skipped")
                if output_dir_name == 'vid_img':
                    _vislide_copy_static([])
                return {'generated': 0, 'failed': 0, 'results': [], 'failed_ids': []}
        result = cinai_generate_all_videos(
            project_dir=p_dir,
            mode=mode,
            clip_ids=_vislide_ids,
            max_parallel=int(
                getattr(config, 'CINAI_VIDEO_MAX_PARALLEL', video_parallel) or video_parallel
            ),
            # Формат кадра — ЕДИНЫЙ ратио пайплайна (радио Ratio в GUI), как у
            # VeoNonStop и у inflight-вотчера (--aspect ar). Раньше бралось из
            # config.CINAI_VIDEO_ASPECT_RATIO (дефолт 16:9 существует всегда,
            # GUI его не пишет) — вертикальный проект получал горизонтальные
            # клипы, причём только в finalized-ветке.
            aspect_ratio=ar,
            single_prompt=_video_single_prompt(),
            model=getattr(config, 'CINAI_VIDEO_MODEL', None),
            duration=getattr(config, 'CINAI_VIDEO_DURATION', 5),
            resolution=getattr(config, 'CINAI_VIDEO_RESOLUTION', ''),
            audio=None,          # у бесплатных видеомоделей CinAI звук не заказывается
            reference_dir=reference_dir,
            prompts_file_path=prompts_file_path,
            output_dir_name=output_dir_name,
            silent=True,
        )
        print(
            f"  CinAI VideoGen done: {result.get('generated', 0)} generated, "
            f"{result.get('failed', 0)} failed -> {output_dir_name}/"
        )
        # слайды кладём только в итоговую vid_img/: у пары VeoNonStop+CinAI
        # своя папка, её сливает merge, а слайды кладёт уже он
        if _vislide_ids and output_dir_name == 'vid_img':
            _vislide_copy_static(_vislide_ids)
        return result

    # ── REF SIZE PREFLIGHT ────────────────────────────────────────────────────
    # Compress any refs that exceed 1.5 MB before any API calls touch them.
    # (Primary compression runs in a background thread during transcription;
    #  this call is a safety net for direct invocations and any leftovers.)
    _ref_dir_pf = Path(project_dir) / 'ref'
    if _ref_dir_pf.exists():
        _ref_limit = 2.0 if action in ('fastgen', 'fastgen_i2v') else 5.0
        compress_refs(str(_ref_dir_pf), max_mb=_ref_limit)
    # ─────────────────────────────────────────────────────────────────────────

    _skip_pf = os.environ.get('BEGO_SKIP_PREFLIGHT', '0') == '1'

    # ── Шапка лога: печатаем ПРОВАЙДЕРОВ, которые реально отработают ──────
    # `action` — внутренний алиас пост-действия, один на несколько движков:
    # `veo_i2v` одинаково зовётся и для VeoNonStop, и для CinAI, а слоты
    # ключей ImgKey/VidKey существуют только у VeoNonStop/FastGen. Печатать
    # их на прогоне CinAI — врать в лог.
    _env_video_engine = (os.environ.get('BEGO_VIDEO_ENGINE', '') or '').strip().lower()
    _pipeline_img_gen = (os.environ.get('PIPELINE_IMG_GEN')
                         or getattr(config, 'PIPELINE_IMG_GEN', 'veononstop')
                         or 'veononstop').lower()

    def _img_stage_provider() -> Optional[str]:
        """Кто наполняет generated/ на этом action (None — стадии нет)."""
        if action in ('fastgen', 'fastgen_i2v'):
            return 'fastgen'
        if action == 'cinai':
            return 'cinai'
        if action in ('imggen', 'veo_i2v'):
            return _pipeline_img_gen
        return None

    def _video_stage_engine() -> Optional[str]:
        """Кто наполняет vid_img/ на этом action (None — стадии нет)."""
        if action in ('fastgen', 'imggen', 'cinai', 'skip'):
            return None
        if action == 'fastgen_i2v':
            return 'veononstop'
        return 'cinai' if _env_video_engine == 'cinai' else 'veononstop'

    def _cinai_video_summary() -> str:
        # кламп 1..6 как в самой стадии — иначе шапка печатала бы 24
        _cw = max(1, min(int(
            getattr(config, 'CINAI_VIDEO_MAX_PARALLEL', video_parallel) or video_parallel
        ), 6))
        return (f"CinAI ({getattr(config, 'CINAI_VIDEO_MODEL', '') or '?'}, "
                f"workers {_cw})")

    def _imggen_summary() -> Optional[str]:
        provider = _img_stage_provider()
        if not provider:
            return None
        if provider == 'skip':
            return "ImageGen: skip"
        if provider == 'cinai':
            # action=='cinai' идёт мимо imggen_parallel — см. ветку ниже;
            # кламп 1..6 как в _safe_parallel самой стадии
            _iw = (getattr(config, 'CINAI_MAX_PARALLEL', 6) if action == 'cinai'
                   else (imggen_parallel or getattr(config, 'CINAI_MAX_PARALLEL', 6)))
            return f"ImageGen: CinAI (workers {max(1, min(int(_iw), 6))})"
        _label = 'FastGen' if provider == 'fastgen' else 'VeoNonStop'
        summary = f"ImageGen: {_label} (key {imggen_key_slot}, workers {imggen_parallel})"
        pool_enabled = str(os.environ.get(
            'CINAI_IMAGE_POOL_ENABLED',
            '1' if getattr(config, 'CINAI_IMAGE_POOL_ENABLED', False) else '0',
        )).strip().lower() in ('1', 'true', 'yes', 'on')
        if pool_enabled:
            summary += (
                f" + CinAI ({config.CINAI_MODEL}, "
                f"workers {getattr(config, 'CINAI_MAX_PARALLEL', 6)})"
            )
        return summary

    def _videogen_summary() -> Optional[str]:
        engine = _video_stage_engine()
        if not engine:
            return None
        if engine == 'cinai':
            return f"VideoGen: {_cinai_video_summary()}"
        return f"VideoGen: VeoNonStop (key {video_key_slot}, workers {video_parallel})"

    print(f"\n  [Silent] Post-pipeline: {action.upper()}")
    print("  " + "  |  ".join(
        [f"Ratio: {ar}"]
        + [bit for bit in (_imggen_summary(), _videogen_summary()) if bit])
        + ("  | INFLIGHT" if inflight_mode else ""))
    _settings_source = os.environ.get('BEGO_SETTINGS_SOURCE', '').strip()
    _fastgen_selection = os.environ.get('BEGO_FASTGEN_KEY_SELECTION', '').strip()
    if _settings_source or _fastgen_selection:
        print(f"  Settings source: {_settings_source or 'unknown'}"
              + (f" | FastGen selection: {_fastgen_selection}" if _fastgen_selection else ""))

    # Инфографика — ВСЕГДА фоном. У неё свой генератор и свои клипы (маркер
    # `infographic`): основной ImageGen/VideoGen/вотчеры эти клипы фильтруют,
    # а finally-хвост джойнит поток ДО Pre-concat fallback и склейки — гонок
    # нет. Раньше для i2v-веток стадия шла СИНХРОННО и задерживала старт
    # генерации на минуты (15.08: CinAI Chain ждал инфографику 2.5 мин).
    _infographic_thread = None
    _infographic_holder = [{'generated': 0, 'failed': 0, 'results': []}]

    # Интро-гейт — СИНХРОННО и ДО старта потоков: он снимает маркер `infographic`
    # со вступления, а ImageGen ниже фильтрует клипы по этому самому маркеру.
    # Сделай это в фоне — и обе стороны разойдутся: гейт снимет метку уже после
    # того, как ImageGen прочитал план, и клип останется вообще без кадра.
    _apply_infographic_intro_gate_here()

    def _run_infographic_worker():
        _infographic_holder[0] = _run_infographic_images_here()

    _infographic_thread = _th.Thread(
        target=_run_infographic_worker,
        daemon=True,
        name='infographic-bg',
    )
    _infographic_thread.start()
    if getattr(config, 'INFOGRAPHIC_ENABLED', False):
        print("  [Infographic] Static generation started in parallel with the main stages")

    try:
        if action == 'fastgen':
            if not config.FASTGEN_API_KEY:
                print("  [SKIP] FASTGEN_API_KEY not set")
                return
            print("  Starting FastGen image generation...")
            result = _run_image_generation('fastgen')
            print(f"  FastGen done: {result.get('generated', 0)} images -> generated/")
            _mark_inflight_imagegen_done()

        elif action == 'fastgen_i2v':
            if not config.FASTGEN_API_KEY:
                print("  [SKIP] FASTGEN_API_KEY not set")
                return
            if not _has_any_veo_key():
                print("  [SKIP] VeoNonStop key not set in any slot (1-4)")
                return

            # Inflight: start live_veo.py so it animates images as they appear
            _watch_proc = None
            _watch_ready = _th.Event()
            if inflight_mode:
                if _should_adapt:
                    # Adapt running in background — launch watcher from a deferred thread
                    # so imggen starts immediately and isn't blocked by adapt wait
                    def _launch_watcher_after_adapt():
                        nonlocal _watch_proc
                        _inflight_prompts = None
                        if _adapt_event.wait(timeout=600) and _adapt_result[0]:
                            _inflight_prompts = _adapt_result[0]
                            print(f"  [codex-adapt] Ready: {Path(_inflight_prompts).name}")
                        else:
                            print("  [codex-adapt] Timeout — watcher uses original prompts")
                        _watch_proc = _start_inflight_watcher(p_dir, video_key_slot, video_parallel, ar, prompt_mode,
                                                               vislide_interval=vislide_interval,
                                                               prompts_file_path=_inflight_prompts)
                        _watch_ready.set()
                    _th.Thread(target=_launch_watcher_after_adapt, daemon=True, name='deferred-watcher').start()
                    print("  [INFLIGHT] Watcher will start after VEO adapt finishes")
                else:
                    _watch_proc = _start_inflight_watcher(p_dir, video_key_slot, video_parallel, ar, prompt_mode,
                                                           vislide_interval=vislide_interval)
                    _watch_ready.set()

            print("  Starting FastGen image generation...")
            result = _run_image_generation('fastgen')
            ok = result.get('generated', 0)
            img_failed = result.get('failed', 0)
            print(f"  FastGen done: {ok} images -> generated/")
            _mark_inflight_imagegen_done()

            _t2v_result = _run_t2v_image_fallback(result)
            if _t2v_result is not None:
                img_failed = int(_t2v_result.get('failed', 0) or 0)
                if img_failed > 0:
                    _save_failed_prompts(p_dir, 't2v', img_failed)
            elif img_failed > 0:
                _save_failed_prompts(p_dir, 'imggen', img_failed)

            if ok > 0:
                _inflight_ok = False
                if inflight_mode:
                    _watch_ready.wait(timeout=660)  # ensure watcher started
                if inflight_mode and _watch_proc is not None:
                    _inflight_ok = _wait_inflight_watcher(_watch_proc, p_dir)
                    if _inflight_ok:
                        vid_failed = 0  # watch_i2v manages its own retries

                if not (inflight_mode and _watch_proc is not None and _inflight_ok):
                    vid_keys = _resolve_veo_key(video_key_slot)
                    # Wait for Codex adapt if running
                    if _should_adapt:
                        if not _adapt_event.wait(timeout=600):
                            print("  [codex-adapt] Timeout — using original prompts")
                        elif _adapt_result[0]:
                            print(f"  [codex-adapt] Ready: {Path(_adapt_result[0]).name}")
                    print("  Starting VeoNonStop i2v...")
                    single_prompt = None
                    if prompt_mode == 'single':
                        sp_path = Path(p_dir) / 'single_prompt.txt'
                        if sp_path.exists():
                            single_prompt = sp_path.read_text(encoding='utf-8-sig').strip()
                            print(f"  Using single_prompt.txt: {single_prompt[:50]}...")
                        else:
                            single_prompt = 'default'
                    _vislide_ids = _get_vislide_clip_ids() if vislide_interval >= 2 else None
                    vid_result = veo_generate_all_videos(
                        project_dir=p_dir, mode='i2v',
                        max_parallel=video_parallel, aspect_ratio=ar,
                        single_prompt=single_prompt,
                        active_keys=vid_keys,
                        clip_ids=_vislide_ids,
                        silent=True,
                        prompts_file_path=_adapt_result[0],
                    )
                    vid_failed = vid_result.get('failed', 0)
                    print("  i2v done -> vid_img/")
                    if _vislide_ids:
                        _vislide_copy_static(_vislide_ids)
                    if vid_failed > 0:
                        _save_failed_prompts(p_dir, 'i2v', vid_failed)

                total_failed = img_failed + vid_failed
                if total_failed > 0:
                    marker = Path(p_dir) / 'has_failed_clips.flag'
                    marker.write_text(f"img_failed={img_failed}\nvid_failed={vid_failed}\n", encoding='utf-8')
                    print(f"  [WARNING] {total_failed} clips failed (img={img_failed}, vid={vid_failed})")
                    print(f"  Failed info saved to: failed_prompts.txt + has_failed_clips.flag")
            elif inflight_mode and _watch_proc is not None:
                _watch_proc.terminate()

        elif action == 'imggen':
            # Image generation only — no i2v (used by slideshow mode).
            # VideoGen is VeoNonStop here. ImageGen is selected independently
            # through PIPELINE_IMG_GEN, just like the GUI ImageGen stage.
            img_source = (
                os.environ.get('PIPELINE_IMG_GEN')
                or getattr(config, 'PIPELINE_IMG_GEN', 'veononstop')
                or 'veononstop'
            ).lower()
            if img_source == 'skip':
                result = {'generated': _count_generated_images(), 'failed': 0}
                print(f"  ImageGen skipped; existing images: {result['generated']}")
            else:
                print(f"  Starting {img_source} ImageGen...")
                result = _run_image_generation(img_source)
            ok = result.get('generated', 0)
            img_failed = result.get('failed', 0)
            print(f"  {img_source.capitalize()} ImageGen done: {ok} images -> generated/")
            _mark_inflight_imagegen_done()
            if img_failed > 0:
                _save_failed_prompts(p_dir, 'imggen', img_failed)

        elif action == 'cinai':
            if not getattr(config, 'CINAI_ACCESS_TOKEN', ''):
                print("  [SKIP] CINAI_ACCESS_TOKEN not configured")
                # Кадров не будет — держать очередь видео не за чем. Без этого
                # шлюз, закрытый снятой галкой, оставался закрытым навсегда:
                # снимает его только `_mark_inflight_imagegen_done`.
                _mark_inflight_imagegen_done()
                return
            print("  Starting CinAI image generation...")
            result = cinai_generate_all_images(
                project_dir=p_dir,
                max_parallel=getattr(config, 'CINAI_MAX_PARALLEL', 6),
                model=getattr(config, 'CINAI_MODEL', None),
                aspect_ratio=ar,
                quality=getattr(config, 'CINAI_QUALITY', '1K'),
                silent=True,
            )
            ok = result.get('generated', 0)
            img_failed = result.get('failed', 0)
            print(f"  CinAI done: {ok} images -> generated/")
            # Кадры досчитались — открываем очередь видео, как это делают все
            # остальные ветки пост-действий. Здесь вызова не было вовсе, и
            # связка «картинки CinAI + видео Off + галка снята» оставляла шлюз
            # закрытым до конца прогона: параллакс не оживлял ни одного кадра.
            _mark_inflight_imagegen_done()
            if img_failed > 0:
                _save_failed_prompts(p_dir, 'imggen', img_failed)

        elif action == 'veo_i2v':
            _cinai_video_selected = (
                os.environ.get('BEGO_VIDEO_ENGINE', '').strip().lower() == 'cinai'
            )
            # НЕТ раннего выхода для CinAI: ImageGen (generated/) обязан отработать
            # ДО VideoGen — ветка ниже сама зовёт _run_cinai_video_stage после
            # ImageGen. Ранний return здесь пропускал ImageGen (все i2v падали
            # "source missing"), а также _wait_real_photo/_wait_stock и
            # Pre-concat fallback в конце функции — очередь склейки валилась
            # на неснятых *_pending маркерах (инцидент ВВВ_20260815_0150).
            if not _cinai_video_selected and not _has_any_veo_key():
                print("  [SKIP] VeoNonStop key not set in any slot (1-4)")
                return

            # Реальный видеорежим CinAI в композитной ветке: i2v (дефолт) или
            # first_last — оба кормятся картинками из generated/, поэтому GUI
            # маппит их на veo_i2v, а сам режим передаёт через CINAI_VIDEO_MODE.
            # Дефолт конфига 't2v' сюда не пропускаем — t2v идёт голой веткой.
            _cinai_video_mode = (
                os.environ.get('CINAI_VIDEO_MODE')
                or getattr(config, 'CINAI_VIDEO_MODE', 'i2v')
                or 'i2v'
            ).strip().lower()
            if _cinai_video_mode not in ('i2v', 'first_last'):
                _cinai_video_mode = 'i2v'
            # Inflight-вотчер умеет только i2v (live_cinai.py --mode i2v);
            # для first_last честно уходим в финализированную ветку после ImageGen.
            _cinai_inflight_unsupported = (
                _cinai_video_selected and _cinai_video_mode != 'i2v'
            )
            if inflight_mode and _cinai_inflight_unsupported:
                print("  [INFLIGHT] CinAI first_last не поддерживает inflight — "
                      "финализированная генерация после ImageGen")

            # Inflight: start live_veo.py so it animates images as they appear
            _watch_proc = None
            _watch_ready = _th.Event()
            if inflight_mode and not _cinai_inflight_unsupported:
                if _should_adapt:
                    # Deferred watcher: wait for adapt in background, don't block imggen
                    def _launch_watcher_after_adapt_v():
                        nonlocal _watch_proc
                        _inflight_prompts = None
                        if _adapt_event.wait(timeout=600) and _adapt_result[0]:
                            _inflight_prompts = _adapt_result[0]
                            print(f"  [codex-adapt] Ready: {Path(_inflight_prompts).name}")
                        else:
                            print("  [codex-adapt] Timeout — watcher uses original prompts")
                        _watch_proc = _start_inflight_watcher(p_dir, video_key_slot, video_parallel, ar, prompt_mode,
                                                               vislide_interval=vislide_interval,
                                                               prompts_file_path=_inflight_prompts)
                        _watch_ready.set()
                    _th.Thread(target=_launch_watcher_after_adapt_v, daemon=True, name='deferred-watcher-v').start()
                    print("  [INFLIGHT] Watcher will start after VEO adapt finishes")
                else:
                    _watch_proc = _start_inflight_watcher(p_dir, video_key_slot, video_parallel, ar, prompt_mode,
                                                           vislide_interval=vislide_interval)
                    _watch_ready.set()

            # ImageGen and VideoGen are separate stages.  Keep this existing
            # VeoNonStop i2v route, but honor the ImageGen provider selected
            # by the GUI/environment.
            img_source = (
                os.environ.get('PIPELINE_IMG_GEN')
                or getattr(config, 'PIPELINE_IMG_GEN', 'veononstop')
                or 'veononstop'
            ).lower()

            # ── ЦЕПОЧКА CinAI: ImageGen+i2v без обратной загрузки кадра ─────
            # Когда и картинки, и видео делает CinAI (finalized i2v), готовый
            # кадр подаётся видеомодели ссылкой на оригинал: он НЕ качается и
            # НЕ заливается обратно (экономия времени клипа и 2-3 МБ трафика).
            # Кадр всё равно сохраняем в generated/ (нужен конкату/фолбэку/Finalize).
            # Остальные клипы (сток/real_photo/инфографика/уже готовые) идут
            # обычным путём ниже: ImageGen пропустит готовые кадры, VideoGen —
            # готовые mp4. При любой ошибке цепочки — просто продолжаем
            # классическим маршрутом, клипы ничего не теряют.
            _chain_failed = 0
            # Липкая нода включается галкой в GUI (CINAI_VIDEO_CHAIN):
            # выключил — идём раздельным путём ImageGen → i2v, чтобы можно
            # было сравнить скорость на своём материале.
            if (_cinai_video_selected and _cinai_video_mode == 'i2v'
                    and img_source == 'cinai' and not inflight_mode
                    and getattr(config, 'CINAI_VIDEO_CHAIN', True)
                    and getattr(config, 'CINAI_ACCESS_TOKEN', '')
                    and getattr(config, 'CINAI_VIDEO_ENABLED', False)):
                try:
                    from units.cinai_motion import (
                        chain_eligible_clips as _chain_ids_fn,
                        generate_chained_i2v as _chain_gen,
                    )
                    # вислайд: цепочка только для оживляемых клипов, кадры
                    # остальных сделает обычный ImageGen ниже
                    _chain_vislide = _get_vislide_clip_ids() if vislide_interval >= 2 else None
                    _chain_ids = (_chain_ids_fn(p_dir, _chain_vislide)
                                  if _chain_vislide is None or _chain_vislide else [])
                    if _chain_ids:
                        print(f"  [CinAI Chain] ImageGen+i2v: "
                              f"{len(_chain_ids)} клипов")
                        _chain_res = _chain_gen(
                            p_dir,
                            _chain_ids,
                            aspect_ratio=ar,
                            single_prompt=_video_single_prompt(),
                            video_prompts_file=_adapt_result[0] if _adapt_result[0] else None,
                            silent=True,
                        )
                        _chain_failed = int(_chain_res.get('failed', 0) or 0)
                        print(f"  [CinAI Chain] done: "
                              f"video={_chain_res.get('generated', 0)} "
                              f"frames={len(_chain_res.get('images', []))} "
                              f"failed={_chain_failed}")
                        # упавшие в цепочке клипы добираются обычным путём ниже
                except Exception as _che:
                    print(f"  [CinAI Chain] недоступна ({str(_che)[:200]}) — "
                          f"обычный маршрут ImageGen -> i2v")
                    logging.debug(traceback.format_exc())

            if img_source == 'skip':
                result = {'generated': _count_generated_images(), 'failed': 0}
                print(f"  ImageGen skipped; existing images: {result['generated']}")
            else:
                print(f"  Starting {img_source} ImageGen...")
                result = _run_image_generation(img_source)
            ok = result.get('generated', 0)
            img_failed = result.get('failed', 0)
            print(f"  {img_source.capitalize()} ImageGen done: {ok} images -> generated/")
            _mark_inflight_imagegen_done()

            _t2v_result = _run_t2v_image_fallback(result)
            if _t2v_result is not None:
                img_failed = int(_t2v_result.get('failed', 0) or 0)
                if img_failed > 0:
                    _save_failed_prompts(p_dir, 't2v', img_failed)
            elif img_failed > 0:
                _save_failed_prompts(p_dir, 'imggen', img_failed)

            if ok > 0:
                _inflight_ok = False
                if inflight_mode and not _cinai_inflight_unsupported:
                    # при first_last вотчер не стартовал — _watch_ready никто
                    # не взведёт, ждать её значит зависнуть на 11 минут
                    _watch_ready.wait(timeout=660)
                if inflight_mode and _watch_proc is not None:
                    _inflight_ok = _wait_inflight_watcher(_watch_proc, p_dir)
                    if _inflight_ok:
                        vid_failed = 0

                if (not (inflight_mode and _watch_proc is not None and _inflight_ok)
                        and _cinai_video_selected):
                    _cinai_result = _run_cinai_video_stage(
                        _cinai_video_mode,
                        prompts_file_path=_adapt_result[0] if _adapt_result[0] else None,
                    )
                    vid_failed = int(_cinai_result.get('failed', 0) or 0)
                    if vid_failed > 0:
                        _save_failed_prompts(p_dir, 'i2v', vid_failed)
                if not (inflight_mode and _watch_proc is not None and _inflight_ok) and not _cinai_video_selected:
                    vid_keys = _resolve_veo_key(video_key_slot)
                    # Wait for Codex adapt if running
                    if _should_adapt:
                        if not _adapt_event.wait(timeout=600):
                            print("  [codex-adapt] Timeout — using original prompts")
                        elif _adapt_result[0]:
                            print(f"  [codex-adapt] Ready: {Path(_adapt_result[0]).name}")
                    print("  Starting VeoNonStop i2v...")
                    single_prompt = None
                    if prompt_mode == 'single':
                        sp_path = Path(p_dir) / 'single_prompt.txt'
                        if sp_path.exists():
                            single_prompt = sp_path.read_text(encoding='utf-8-sig').strip()
                            print(f"  Using single_prompt.txt: {single_prompt[:50]}...")
                        else:
                            single_prompt = 'default'
                    _vislide_ids = _get_vislide_clip_ids() if vislide_interval >= 2 else None
                    vid_result = veo_generate_all_videos(
                        project_dir=p_dir, mode='i2v',
                        max_parallel=video_parallel, aspect_ratio=ar,
                        single_prompt=single_prompt,
                        active_keys=vid_keys,
                        clip_ids=_vislide_ids,
                        silent=True,
                        prompts_file_path=_adapt_result[0],
                    )
                    vid_failed = vid_result.get('failed', 0)
                    print("  i2v done -> vid_img/")
                    if _vislide_ids:
                        _vislide_copy_static(_vislide_ids)
                    if vid_failed > 0:
                        _save_failed_prompts(p_dir, 'i2v', vid_failed)

                total_failed = img_failed + vid_failed
                if total_failed > 0:
                    marker = Path(p_dir) / 'has_failed_clips.flag'
                    marker.write_text(f"img_failed={img_failed}\nvid_failed={vid_failed}\n", encoding='utf-8')
                    print(f"  [WARNING] {total_failed} clips failed (img={img_failed}, vid={vid_failed})")
                    print(f"  Failed info saved to: failed_prompts.txt + has_failed_clips.flag")
            elif inflight_mode and _watch_proc is not None:
                _watch_proc.terminate()

        elif action == 't2v':
            if os.environ.get('BEGO_VIDEO_ENGINE', '').strip().lower() == 'cinai':
                result = _run_cinai_video_stage(action)
                vid_failed = int(result.get('failed', 0) or 0)
                if vid_failed > 0:
                    _save_failed_prompts(p_dir, action, vid_failed)
                return
            # Любой из четырёх слотов: ниже ключ всё равно берётся по
            # video_key_slot, и проверять только первый — значит скипнуть
            # стадию у того, кто работает вторым ключом.
            if not _has_any_veo_key():
                print("  [SKIP] VeoNonStop key not set in any slot (1-4)")
                return

            # Обложка ДО основной стадии и строго до неё: слоты у ключа одни на
            # всех, и два вызова разом дрались бы за них. Стадии про обложку
            # знать не нужно — она получит список без первого клипа.
            _cover_done_id = None
            try:
                from units import cover_opener as _ch

                if _ch.enabled(p_dir):
                    _cover_done_id = _ch.animate_before_stage(
                        p_dir, veo_generate_all_videos,
                        active_keys=_resolve_veo_key(video_key_slot),
                        aspect_ratio=ar)
            except Exception as _cover_exc:              # noqa: BLE001
                print(f"  [Обложка] пропущена: {_cover_exc}")
            vid_keys = _resolve_veo_key(video_key_slot)
            _vislide_ids = None          # вислайд в t2v не применяется: кадров-слайдов нет
            # Обложку уже оживили выше — стадии её не отдаём, иначе она сделает
            # клип заново и затрёт готовый файл. Если списка не было (стадия
            # берёт все клипы), собираем его без обложки.
            if _cover_done_id is not None:
                if _vislide_ids:
                    _vislide_ids = [cid for cid in _vislide_ids
                                    if int(cid) != int(_cover_done_id)]
                else:
                    try:
                        import json as _json_cover

                        _plan_raw = _json_cover.loads(
                            (Path(p_dir) / 'montage_plan.json').read_text(
                                encoding='utf-8-sig'))
                        _plan_data = _plan_raw.get('montage_plan', _plan_raw)
                        _vislide_ids = [int(c['id'])
                                        for c in (_plan_data.get('clips') or [])
                                        if int(c.get('id', -1)) != int(_cover_done_id)]
                    except Exception as _plan_exc:        # noqa: BLE001
                        print(f"  [Обложка] список клипов не пересобран "
                              f"({_plan_exc}) — стадия возьмёт все")
            print(f"  Starting VeoNonStop {action} (key={video_key_slot})...")
            vid_result = veo_generate_all_videos(
                project_dir=p_dir, mode=action,
                max_parallel=video_parallel, aspect_ratio=ar,
                active_keys=vid_keys,
                clip_ids=_vislide_ids,
                silent=True,
            )
            vid_failed = vid_result.get('failed', 0)
            print(f"  {action} done -> vid_img/")
            if _vislide_ids:
                _vislide_copy_static(_vislide_ids)

            if vid_failed > 0:
                _save_failed_prompts(p_dir, action, vid_failed)
                marker = Path(p_dir) / 'has_failed_clips.flag'
                marker.write_text(f"vid_failed={vid_failed}\nmode={action}\n", encoding='utf-8')
                print(f"  [WARNING] {vid_failed} clips failed in {action}")
                print(f"  Failed info saved to: failed_prompts.txt + has_failed_clips.flag")

    except Exception as e:
        print(f"  [ERROR] Post-pipeline {action} failed: {e}")
        logging.debug(traceback.format_exc())
    finally:
        # ── Хвост пайплайна в finally: у веток генерации есть ранние return
        # (CinAI veo_i2v/t2v, inflight-пары). Раньше
        # каждый такой return перепрыгивал ожидание фоновых стадий и Pre-concat
        # fallback — real_photo/stock оставались *_pending в montage_plan, и
        # очередь склейки падала на ConcatGuard (инцидент ВВВ_20260815_0150).
        if _infographic_thread is not None:
            print("  [Infographic] Waiting for parallel static generation...")
            _infographic_thread.join()
            if _infographic_holder[0].get('failed', 0):
                _save_failed_prompts(p_dir, 'imggen', _infographic_holder[0]['failed'])
        # Итог стадии в общую сводку: раньше Real Photo и Сток отчитывались, а
        # инфографика молчала, и по финалу нельзя было понять, отработала ли она.
        _ig = _infographic_holder[0] if _infographic_holder else {}
        if _ig and (_ig.get('generated') or _ig.get('failed')):
            print(f"  [Infographic] итог: {_ig.get('generated', 0)} сделано, "
                  f"{_ig.get('failed', 0)} не вышло")

        # Музыка идёт отдельным фоновым процессом и в сводке не появлялась —
        # пользователь не знал, сделалась она или нет. Читаем её статус-файл.

        _wait_real_photo(label="pipeline finish")
        _wait_stock(label="pipeline finish")
        _wait_mograph(label="pipeline finish")

        # Pre-concat fallback: догенерить картинкой ЛЮБОЙ клип без медиа.
        # Ловим по ФАКТУ отсутствия медиа (нет vid_img/{id} И нет generated/{id}), а НЕ по
        # source-маркеру — потому что real_photo при провале сам делает revert (убирает source).
        # Так покрываем всё: pending без фото, revert'нутые провалы, упавшую генерацию,
        # недокачанный сток (внутри снимается ЛЮБОЙ source, включая stock_*).
        # Промпт уже есть из Pass 2 (промпты пишутся и для помеченных клипов).
        # Вход по ОБОИМ флагам: раньше висел только на REAL_PHOTO_PROMPT_FALLBACK — выключи
        # его юзер, и недокачанные сток-клипы остались бы помеченными → дырки в ролике.
        if (getattr(config, 'REAL_PHOTO_PROMPT_FALLBACK', True)
                or getattr(config, 'STOCK_PROMPT_FALLBACK', True)):
            try:
                import json as _rpj
                _mp_path = Path(project_dir) / 'montage_plan.json'
                _gen_dir = Path(project_dir) / 'generated'
                _vid_dir = Path(project_dir) / 'vid_img'

                def _clip_has_vid_media(cid):
                    for _fmt in (f"{cid:03d}", str(cid)):
                        for _e in ('mp4', 'mov', 'png', 'jpg', 'jpeg', 'webp'):
                            _f = _vid_dir / f"{_fmt}.{_e}"
                            if _f.exists() and _f.stat().st_size > 1000:
                                return True
                    return False

                def _clip_has_generated_image(cid):
                    for _fmt in (f"{cid:03d}", str(cid)):
                        for _e in ('png', 'jpg', 'jpeg', 'webp'):
                            _f = _gen_dir / f"{_fmt}.{_e}"
                            if _f.exists() and _f.stat().st_size > 1000:
                                return True
                    return False

                def _clip_has_media(cid):
                    return _clip_has_vid_media(cid) or _clip_has_generated_image(cid)

                if _mp_path.exists():
                    _mp = _rpj.loads(_mp_path.read_text(encoding='utf-8-sig'))
                    _mp_plan = _mp.get('montage_plan', _mp)
                    _undelivered, _infographic_missing, _changed = [], [], False
                    # «Удержание»: режиссёр решил, что сцене не подходит ни один источник
                    # (нейрогенерации нет) — ей сразу кадр соседа, без повтора стока.
                    from units.source_quota import HOLD_SOURCE as _fb_hold_src
                    _hold = []
                    for _c in _mp_plan.get('clips', []):
                        _cid = _c.get('id')
                        if _cid is None:
                            continue
                        _cid = int(_cid)
                        if not _clip_has_media(_cid):
                            if _c.get('source') == 'infographic':
                                _infographic_missing.append(_cid)
                                continue
                            if _c.get('source') == _fb_hold_src:
                                _hold.append(_cid)
                                continue
                            _undelivered.append(_cid)
                            # снять real_photo маркер если остался (иначе imggen отфильтрует клип)
                            if _c.pop('source', None) is not None:
                                _changed = True
                    if _changed:
                        _mp_path.write_text(_rpj.dumps(_mp, ensure_ascii=False, indent=2), encoding='utf-8')
                    if _infographic_missing:
                        _ims = sorted(_infographic_missing)
                        print(f"\n  [Infographic fallback] {len(_ims)} static clips without media -> dedicated retry: "
                              f"{_ims[:10]}{'...' if len(_ims) > 10 else ''}")
                        _run_infographic_images_here(_ims)
                    # Нейрогенерация запрещена («Состав видео» без неё или картинки
                    # и видео «Пропустить»): сцены без медиа НЕ отдаём нейросетям.
                    # Сначала сток ещё раз — без вижна и без требования субъекта,
                    # потом кадр соседнего клипа. Дыр в ролике нет.
                    from units.source_quota import ai_allowed as _fb_ai_allowed
                    if _hold and _fb_ai_allowed():
                        # метка от прогона без нейрогенерации, а теперь она есть
                        for _c in _mp_plan.get('clips', []):
                            if _c.get('id') is not None and int(_c['id']) in _hold:
                                _c.pop('source', None)
                        _mp_path.write_text(_rpj.dumps(_mp, ensure_ascii=False, indent=2), encoding='utf-8')
                        _undelivered += _hold
                        _hold = []
                    if (_undelivered or _hold) and not _fb_ai_allowed():
                        if _hold:
                            print(f"\n  [Без нейрогенерации] {len(_hold)} сцен(ы) на удержании "
                                  f"(режиссёр не нашёл подходящего источника): "
                                  f"{sorted(_hold)[:10]}{'...' if len(_hold) > 10 else ''}")
                        if _undelivered:
                            print(f"\n  [Без нейрогенерации] {len(_undelivered)} сцен(ы) без медиа: "
                                  f"{sorted(_undelivered)[:10]}{'...' if len(_undelivered) > 10 else ''}")
                        if _undelivered and os.environ.get('STOCK_ENABLED', 'false').lower() in ('1', 'true', 'yes'):
                            try:
                                from units.source_ledger import apply_clip_source_delta as _fb_delta
                                from units.stock_runner import run_stock_stage as _fb_stock
                                _fb_delta(_mp_path, {int(c): {'source': 'stock_pending'}
                                                     for c in _undelivered})
                                print("  [Без нейрогенерации] повтор стока: без вижна и без "
                                      "требования субъекта")
                                _fb_stock(Path(project_dir), target_w=_frame_w, target_h=_frame_h,
                                          fps=getattr(config, 'VIDEO_FPS', 60), relaxed=True)
                            except Exception as _fbs:             # noqa: BLE001
                                print(f"  [Без нейрогенерации] повтор стока не удался: {_fbs}")
                                logging.debug(traceback.format_exc())
                            _undelivered = [c for c in _undelivered if not _clip_has_media(c)]
                        # удержания — после повтора стока: у соседей больше готовых кадров
                        if _undelivered or _hold:
                            from units.source_quota import fill_without_ai as _fb_fill
                            _fb_fill(project_dir, _undelivered + _hold)
                        _undelivered = []             # нейро-ветки ниже не нужны
                    if _undelivered:
                        _us = sorted(_undelivered)
                        # Догенерация идёт ТЕМ ЖЕ генератором, что выбран для проекта
                        # (PIPELINE_IMG_GEN — та же настройка, по которой ветвится основной
                        # пайплайн на 1824). Раньше был хардкод VeoNonStop: проект на FastGen
                        # получал догенерацию через VeoNonStop (чужие ключи/стиль), а при
                        # banana_ref без рефов imggen падал.
                        _fb_src = (os.environ.get('PIPELINE_IMG_GEN')
                                   or getattr(config, 'PIPELINE_IMG_GEN', 'veononstop') or 'veononstop').lower()

                        # ВИДЕОРЕЖИМ t2v: догоняем сцену ВИДЕО, а не картинкой.
                        # Пользователь выбирает t2v в том числе тогда, когда у него
                        # кончилась квота на картинки; лезть в этот момент в imggen
                        # значит сломать прогон в самом конце — сцены останутся
                        # дырками, хотя видеоквота цела. Признак — выбранный режим
                        # видео t2v ЛИБО выключенная картиночная генерация
                        # (PIPELINE_IMG_GEN=skip): рисовать всё равно нечем.
                        _vid_mode = str(os.environ.get('PIPELINE_VIDEO_MODE')
                                        or getattr(config, 'PIPELINE_VIDEO_MODE', '')
                                        or '').strip().lower()
                        if _vid_mode == 't2v' or _fb_src == 'skip':
                            print(f"\n  [Pre-concat fallback] {len(_us)} клипов без медиа → "
                                  f"догенерация ВИДЕО через t2v "
                                  f"(режим видео={_vid_mode or '—'}, картинки={_fb_src}): "
                                  f"{_us[:10]}{'...' if len(_us) > 10 else ''}")
                            try:
                                _t2v_fb = veo_generate_all_videos(
                                    project_dir=p_dir,
                                    mode='t2v',
                                    clip_ids=_undelivered,
                                    max_parallel=int(getattr(config, 'VEONONSTOP_MAX_PARALLEL', 12) or 12),
                                    aspect_ratio=getattr(config, 'VEONONSTOP_ASPECT_RATIO', '16:9'),
                                )
                                print(f"  [Pre-concat fallback] t2v: "
                                      f"ok={_t2v_fb.get('generated', 0)}")
                            except Exception as _t2v_exc:      # noqa: BLE001
                                # Видео не вышло — картинка всё ещё лучше дырки,
                                # поэтому падаем в обычную ветку ниже.
                                print(f"  [Pre-concat fallback] t2v не отработал ({_t2v_exc}) "
                                      f"→ пробую картинкой")
                            else:
                                _undelivered = [c for c in _undelivered if not _clip_has_media(c)]
                                if not _undelivered:
                                    print("  [Pre-concat fallback] t2v закрыл все сцены — "
                                          "картинки не нужны")
                                else:
                                    _us = sorted(_undelivered)
                                    print(f"  [Pre-concat fallback] после t2v осталось "
                                          f"{len(_us)} — добираю картинкой")
                    # Картиночная догенерация: либо режим не t2v, либо t2v закрыл
                    # не всё. Пустой список означает, что добирать уже нечего.
                    if _undelivered:
                        _us = sorted(_undelivered)
                        print(f"\n  [Pre-concat fallback] {len(_us)} клипов без медиа → догенерация картинкой "
                              f"(без анимации, генератор={_fb_src}): "
                              f"{_us[:10]}{'...' if len(_us) > 10 else ''}")
                        try:
                            if _fb_src == 'cinai' and getattr(config, 'CINAI_ACCESS_TOKEN', ''):
                                _fb_result = cinai_generate_all_images(
                                    project_dir=p_dir,
                                    clip_ids=_undelivered,
                                    max_parallel=getattr(config, 'CINAI_MAX_PARALLEL', 6),
                                    model=config.CINAI_MODEL,
                                    aspect_ratio=getattr(config, 'CINAI_ASPECT_RATIO', '16:9'),
                                    quality=getattr(config, 'CINAI_QUALITY', '1K'),
                                    silent=True,
                                )
                                print(f"  [Pre-concat fallback] CinAI: "
                                      f"ok={_fb_result.get('generated', 0)} "
                                      f"failed={_fb_result.get('failed', 0)}")
                            elif _fb_src == 'fastgen' and getattr(config, 'FASTGEN_API_KEY', ''):
                                from units.fastgen_engine import generate_all_images as _fg_gen
                                _fb_api, _fb_dual, _fb_slot = _resolve_fastgen_slot(
                                    imggen_key_slot, allow_dual=True)
                                _fb_result = _fg_gen(
                                    project_dir=p_dir,
                                    clip_ids=_undelivered,
                                    api_key=_fb_api,
                                    dual_key=_fb_dual,
                                    silent=True,
                                    preflight=False,
                                )
                                print(f"  [Pre-concat fallback] FastGen slot={_fb_slot}: "
                                      f"ok={_fb_result.get('generated', 0)} "
                                      f"failed={_fb_result.get('failed', 0)}")
                            else:
                                # veononstop (или неизвестный источник → безопасный дефолт)
                                from units.veo_stills import generate_all_images as _rp_imggen
                                _rp_mode = (getattr(config, 'VEONONSTOP_IMGGEN_MODE', 'imagen') or 'imagen').strip()
                                # banana_ref/banana2_ref грузят style-reference из project/ref/.
                                # Для догенерации одиночных клипов рефов может не быть → imggen
                                # кинет ошибку. Проверяем и откатываемся на imagen.
                                if _rp_mode in ('banana_ref', 'banana2_ref'):
                                    _ref_dir = Path(project_dir) / 'ref'
                                    _has_refs = _ref_dir.is_dir() and any(
                                        p.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp')
                                        for p in _ref_dir.iterdir() if p.is_file()
                                    )
                                    if not _has_refs:
                                        print(f"  [Pre-concat fallback] режим {_rp_mode}, но в project/ref/ нет "
                                              f"рефов → откат на imagen")
                                        _rp_mode = 'imagen'
                                _rp_imggen(project_dir=p_dir, mode=_rp_mode, clip_ids=_undelivered,
                                           silent=True, preflight=False)
                        except Exception as _ge:
                            print(f"  [Pre-concat fallback] ⚠️  Догенерация не удалась: {_ge}")
                            logging.debug(traceback.format_exc())
            except Exception as _fe:
                print(f"  [Pre-concat fallback] пропущен: {_fe}")
                logging.debug(traceback.format_exc())

    # ── Параллакс готовым фото (сток / архивы), стандартный режим ───────────
    # Нейрокадры получают параллакс внутри обычного i2v, а сток и архивные фото
    # туда не попадают вовсе: генератор пропускает клипы с внешним источником.
    # Поэтому им нужен отдельный проход тем же veo i2v с параллакс-промптом —
    # ровно тот, что давно работает в Auto Visual Story. Идёт ПОСЛЕ фолбэка,
    # когда все источники уже разложили свои файлы, и не зависит от режима
    # видеогенерации: выключенное оживление («Off») означает «не делать
    # нейровидео», а не «не делать параллакс» (задание пользователя 28.08).
    # Вотчер работал фоном рядом со стадиями — здесь даём ему доработать.
    # Кадры, закрытые в самом конце прогона, только легли в generated/, и
    # резкий стоп не дал бы их оживить. Поэтому сначала ЖДЁМ отмашки, как ждём
    # ютуб и сток, и только потом рубим (задание пользователя 28.08).
    if _parallax_thread is not None:
        # Правило вынесено в `parallax_wait_limit` — его можно прогнать тестом
        # на том же окружении, с которым стадию запускает интерфейс.
        _px_wait, _px_no_limit = parallax_wait_limit(_vq_keys)
        if _px_wait is None and _parallax_thread.is_alive():
            print(f"  [Параллакс] остальные стадии готовы; жду без ограничения: "
                  f"{_px_no_limit}")
            # Отмашка «источники отработали, новых кадров не будет» — ОБЯЗАТЕЛЬНА
            # перед ожиданием без предела. Наблюдатель выходит из цикла, только
            # когда закрыты все выбранные кадры, а часть их может не появиться
            # никогда: при `ImgGen=Skip` нейрокадры в списке есть, а картинок
            # для них не делает никто. Раньше его вытаскивал таймаут; без
            # отмашки ожидание без предела повесило бы прогон навсегда.
            # Работу это не рубит: по отмашке наблюдатель кладёт в очередь всё
            # уже готовое и дожидается своих задач (auto_visual_parallax.py:275).
            _parallax_stop.set()
            _parallax_thread.join()
        elif _px_wait and _parallax_thread.is_alive():
            print(f"  [Параллакс] остальные стадии готовы; жду отмашки "
                  f"до {_px_wait}с")
            _parallax_thread.join(timeout=_px_wait)
        if _parallax_thread.is_alive():
            print("  [Параллакс] не уложился — стоп; оставшиеся кадры идут "
                  "статикой с зумом")
            _parallax_stop.set()
            _parallax_thread.join(timeout=120)   # грейс на текущую партию
        else:
            print("  [Параллакс] закончил")

    # Очередь видео больше не нужна: все поставщики задач отработали. Начатые
    # задачи доигрывают, новые не принимаются.
    try:
        from units.motion_queue import close_all as _close_video_queues
        _close_video_queues(wait=True)
    except Exception as _vqe:                              # noqa: BLE001
        logging.debug("Очередь видео: закрытие не удалось: %s", _vqe)



def _first_filled(all_keys: list) -> list:
    """Первый ЗАПОЛНЕННЫЙ слот. Откат при выборе слота без ключа.

    Раньше откатывались жёстко на [1]: выбран слот 2, заполнен только слот 3 —
    брался пустой первый, и прогон падал уже в клиенте («not set in .env»).
    """
    for i, key in enumerate(all_keys):
        if key:
            return [i + 1]
    return [1]


def _veo_slot_key_present() -> bool:
    """Есть ли ключ самого VeoNonStop (в любом из четырёх слотов) — без учёта Vertex/CheapVibeCode.

    `_has_any_veo_key` отвечает «есть ли чем генерировать картинки маршрутом VeoNonStop»
    и считает своих клиентов; стадиям, которым нужен именно VeoNonStop (инфографика
    с выбранным VeoNonStop), нужен этот признак."""
    return any(getattr(config, name, '') or '' for name in (
        'VEONONSTOP_API_KEY', 'VEONONSTOP_API_KEY_2', 'VEONONSTOP_API_KEY_3', 'VEONONSTOP_API_KEY_4'))


def _has_any_veo_key() -> bool:
    """Есть ли хоть один ключ VeoNonStop — в ЛЮБОМ из четырёх слотов.

    Проверять только VEONONSTOP_API_KEY (слот 1) нельзя: пользователь свободно
    снимает первый ключ и работает вторым («BRO»). Тогда стадия видео молча
    пропускалась, а падал уже concat: «Media missing for clips» — видео просто
    никто не делал (инцидент 07.08.2026, ключ слота 2).
    """
    from units.vertex_client import vertex_ready
    from units.cheapvibe_client import cheapvibe_ready
    from units.royaltechno_client import royaltechno_ready
    from units.varag_client import varag_ready
    return vertex_ready() or cheapvibe_ready() or royaltechno_ready() or varag_ready() or any([
        getattr(config, 'VEONONSTOP_API_KEY', '') or '',
        getattr(config, 'VEONONSTOP_API_KEY_2', '') or '',
        getattr(config, 'VEONONSTOP_API_KEY_3', '') or '',
        getattr(config, 'VEONONSTOP_API_KEY_4', '') or '',
    ])


def video_queue_hold_reason(video_keys: list) -> Optional[str]:
    """Надо ли придержать очередь видео, пока идут картинки. None — не надо.

    Решение целиком выводится из того, что ставит интерфейс на запуск:
    `BEGO_LIVE_ANIMATE`, `PIPELINE_IMG_GEN`, `BEGO_IMGGEN_KEY_SLOT`,
    `BEGO_VIDEO_KEY_SLOT`. Отдельной функцией — чтобы это правило можно было
    прогнать ровно на том окружении, с которым стадию запускает GUI.

    Две причины подождать:
      • пользователь не разрешал оживление по ходу (галка выключена);
      • разрешил, но картинки делает VeoNonStop на ТОМ ЖЕ ключе — слоты у ключа
        общие на картинки и видео, и одновременная работа только замедлит обе
        стадии.

    Обе причины отпадают, если генерации кадров нет вовсе: ждать нечего и
    делить не с кем. Это проверяется ПЕРВЫМ — иначе выключенная галка закрывала
    шлюз на пустом месте, а открыть его было некому: при `ImgGen=Skip`
    интерфейс отдаёт `post_action='skip'` (begoframe.py:13458), ни одна ветка
    пост-действий не выполняется, и `_mark_inflight_imagegen_done` —
    единственный, кто снимает шлюз, — не вызывается. Параллакс простаивал весь
    прогон и упирался в таймаут, хотя работать ему никто не мешал.
    """
    provider = (
        os.environ.get('PIPELINE_IMG_GEN')
        or getattr(config, 'PIPELINE_IMG_GEN', 'veononstop')
        or 'veononstop'
    ).strip().lower()
    if provider in ('skip', 'none', 'off'):
        return None                      # кадров никто не делает — держать нечего
    live = str(
        os.environ.get('BEGO_LIVE_ANIMATE')
        or ('1' if getattr(config, 'VIDEO_LIVE_ANIMATE', False) else '0')
    ).strip().lower() in ('1', 'true', 'yes', 'on')
    if not live:
        return 'оживление по ходу выключено'
    if provider != 'veononstop':
        return None                      # у картинок своя ёмкость
    # Слоты делятся, только когда И картинки, И видео идут ключами VeoNonStop. Свой
    # клиент у картинок (Vertex / CheapVibeCode / VARAG) или у видео (Vertex / RoyalTechno /
    # VARAG) — ключа VeoNonStop на двоих нет, ждать нечего. У VARAG на тарифе chain слоты
    # общие, но занятый слот клиент сам переждёт (SLOTS_BUSY → локальная очередь).
    if (os.environ.get('BEGO_IMAGE_PROVIDER', '').strip()
            or os.environ.get('BEGO_VIDEO_PROVIDER', '').strip().lower() in ('vertex', 'royaltechno', 'varag')):
        return None
    img_keys = _resolve_veo_key(
        os.environ.get('BEGO_IMGGEN_KEY_SLOT')
        or os.environ.get('VEONONSTOP_IMGGEN_KEY_SLOT')
        or '1'
    )
    shared = sorted(set(img_keys) & set(video_keys or []))
    if shared:
        return ('картинки идут на '
                + ', '.join(f'Key{k}' for k in shared)
                + ' — слоты ключа общие на картинки и видео')
    return None


def parallax_wait_limit(video_keys: list) -> Tuple[Optional[int], str]:
    """Сколько ждать параллакс, когда остальные стадии готовы.

    Возвращает `(секунды, причина)`; `секунды is None` — ждать без предела.

    Таймаут уместен, пока параллакс — украшение: основную массу оживляет стадия
    i2v, а он добирает сток с архивами, и прогон не должен висеть из-за добавки.
    Мерить его этим остатком нечестно в двух случаях:

      • видеогенерация выключена — параллакс ЕДИНСТВЕННЫЙ, кто делает движение
        в ролике; обрубить его значит отдать ролик статикой;
      • он простоял под шлюзом всю генерацию кадров — работать начал только что,
        и весь объём целиком попадает в остаток.

    Насовсем не зависнет: стоп в интерфейсе гасит задачи на ключе.
    """
    video_off = (
        str(os.environ.get('PIPELINE_VIDEO_MODE')
            or getattr(config, 'PIPELINE_VIDEO_MODE', '')
            or '').strip().lower() == 'skip'
    )
    if video_off:
        return None, 'видеогенерация выключена — оживляет только он'
    try:
        from units.motion_queue import get_queue as _get_vq
        queue = _get_vq(video_keys or [])
        if queue is not None and queue.was_held:
            return None, 'он ждал под шлюзом, пока шли кадры'
    except Exception as exc:                                   # noqa: BLE001
        logging.debug("Очередь видео: состояние шлюза неизвестно: %s", exc)
    return max(0, int(getattr(
        config, 'VEONONSTOP_I2V_PARALLAX_WAIT_TIMEOUT', 200))), ''


def _resolve_veo_key(slot: str) -> list:
    """Returns list of 1-based key indices for the given slot."""
    slot = str(slot or '1').strip().lower()
    slot = {
        'key1': '1', 'main': '1',
        'key2': '2', 'backup': '2',
        'key3': '3', 'key4': '4',
    }.get(slot, slot)
    ALL_KEYS = [
        config.VEONONSTOP_API_KEY,
        config.VEONONSTOP_API_KEY_2,
        config.VEONONSTOP_API_KEY_3,
        config.VEONONSTOP_API_KEY_4,
    ]
    if slot in ('both', 'all'):
        active = [i + 1 for i, k in enumerate(ALL_KEYS) if k]
        return active if active else [1]
    if ',' in slot:
        indices = []
        for s in slot.split(','):
            try:
                idx = int(s.strip())
                if 1 <= idx <= 4 and ALL_KEYS[idx - 1]:
                    indices.append(idx)
            except (ValueError, TypeError):
                pass
        return indices if indices else _first_filled(ALL_KEYS)
    try:
        idx = int(slot)
    except (ValueError, TypeError):
        idx = 1
    if 1 <= idx <= 4 and ALL_KEYS[idx - 1]:
        return [idx]
    return _first_filled(ALL_KEYS)


def _vislide_clip_ids(all_clips: list, interval: int, intro_dur: int = 0) -> list:
    """Вислайд: каждый N-й клип, которому нужно видео, + все клипы интро-блока (см. units/vislide)."""
    from units.vislide import animated_ids
    return animated_ids(all_clips, interval, intro_dur)


def cmd_process_queue(args):
    """Команда: обработка файлов из выбранной очереди."""
    run_pipeline = getattr(args, 'run', False)
    language_code = getattr(args, 'language', None) or config.ASSEMBLYAI_LANGUAGE_CODE

    # Логгирование — сразу при старте, всегда
    log_dir = Path(__file__).parent / 'logs'
    log_file = setup_logging(log_dir)

    print("\n" + "="*60)
    print("ОБРАБОТКА ОЧЕРЕДИ — Director: GPT (Codex)")
    if run_pipeline:
        print("+ АВТОЗАПУСК ПАЙПЛАЙНА (транскрипция + режиссура)")
    print("="*60)
    print(f"📝 Лог-файл: {log_file}")

    # Выбор режима: видео / слайдшоу / вислайд
    clip_mode = getattr(args, 'clip_mode', None)
    vislide_interval = getattr(args, 'vislide_interval', 0) or 0
    print()
    if clip_mode == 'slideshow':
        config.ACTIVE_CLIP_MIN_DURATION = config.SLIDESHOW_CLIP_MIN_DURATION
        config.ACTIVE_CLIP_MAX_DURATION = config.SLIDESHOW_CLIP_MAX_DURATION
        print(f"  → Слайдшоурежим: клипы {config.ACTIVE_CLIP_MIN_DURATION}-{config.ACTIVE_CLIP_MAX_DURATION}с")
    elif clip_mode == 'video':
        config.ACTIVE_CLIP_MIN_DURATION = config.VIDEO_CLIP_MIN_DURATION
        config.ACTIVE_CLIP_MAX_DURATION = config.VIDEO_CLIP_MAX_DURATION
        print(f"  → Видеорежим: клипы {config.ACTIVE_CLIP_MIN_DURATION}-{config.ACTIVE_CLIP_MAX_DURATION}с")
    elif clip_mode == 'vislide':
        if vislide_interval < 2:
            vislide_interval = 3
        config.ACTIVE_CLIP_MIN_DURATION = config.SLIDESHOW_CLIP_MIN_DURATION
        config.ACTIVE_CLIP_MAX_DURATION = config.SLIDESHOW_CLIP_MAX_DURATION
        print(f"  → Вислайд-режим (каждый {vislide_interval}-й оживляется): клипы {config.ACTIVE_CLIP_MIN_DURATION}-{config.ACTIVE_CLIP_MAX_DURATION}с")
    else:
        print("Что создаём?")
        print(f"  [1] Видео      (Veo3-фрагменты)   клипы {config.VIDEO_CLIP_MIN_DURATION}-{config.VIDEO_CLIP_MAX_DURATION}с  (Enter = по умолчанию)")
        print(f"  [2] Слайдшоу   (картинки)          клипы {config.SLIDESHOW_CLIP_MIN_DURATION}-{config.SLIDESHOW_CLIP_MAX_DURATION}с")
        print(f"  [3] Вислайд    (каждый N-й оживл.) клипы {config.SLIDESHOW_CLIP_MIN_DURATION}-{config.SLIDESHOW_CLIP_MAX_DURATION}с")
        print()
        while True:
            mode_choice = input("Ваш выбор [1/2/3]: ").strip()
            if mode_choice in ('', '1'):
                clip_mode = 'video'
                config.ACTIVE_CLIP_MIN_DURATION = config.VIDEO_CLIP_MIN_DURATION
                config.ACTIVE_CLIP_MAX_DURATION = config.VIDEO_CLIP_MAX_DURATION
                print(f"  → Видеорежим: клипы {config.ACTIVE_CLIP_MIN_DURATION}-{config.ACTIVE_CLIP_MAX_DURATION}с")
                break
            elif mode_choice == '2':
                clip_mode = 'slideshow'
                config.ACTIVE_CLIP_MIN_DURATION = config.SLIDESHOW_CLIP_MIN_DURATION
                config.ACTIVE_CLIP_MAX_DURATION = config.SLIDESHOW_CLIP_MAX_DURATION
                print(f"  → Слайдшоурежим: клипы {config.ACTIVE_CLIP_MIN_DURATION}-{config.ACTIVE_CLIP_MAX_DURATION}с")
                break
            elif mode_choice == '3':
                clip_mode = 'vislide'
                if vislide_interval < 2:
                    vislide_interval = 3
                config.ACTIVE_CLIP_MIN_DURATION = config.SLIDESHOW_CLIP_MIN_DURATION
                config.ACTIVE_CLIP_MAX_DURATION = config.SLIDESHOW_CLIP_MAX_DURATION
                while True:
                    n_str = input(f"  Каждый N-й клип оживлять [default={vislide_interval}]: ").strip()
                    if not n_str:
                        break
                    if n_str.isdigit() and int(n_str) >= 2:
                        vislide_interval = int(n_str)
                        break
                    print("  Введите число >= 2")
                print(f"  → Вислайд-режим (каждый {vislide_interval}-й): клипы {config.ACTIVE_CLIP_MIN_DURATION}-{config.ACTIVE_CLIP_MAX_DURATION}с")
                break
            else:
                print("  Введите 1, 2 или 3 (Enter = видео)")

    # Переопределение длительности клипов из CLI (--clip-min / --clip-max)
    clip_min_arg = getattr(args, 'clip_min', None)
    clip_max_arg = getattr(args, 'clip_max', None)
    if clip_min_arg is not None:
        config.ACTIVE_CLIP_MIN_DURATION = clip_min_arg
    if clip_max_arg is not None:
        config.ACTIVE_CLIP_MAX_DURATION = clip_max_arg
    if clip_min_arg is not None or clip_max_arg is not None:
        print(f"  → Длительность переопределена: {config.ACTIVE_CLIP_MIN_DURATION}-{config.ACTIVE_CLIP_MAX_DURATION}с")

    # Параметры вступительного блока
    intro_min = config.get_intro_clip_min()
    intro_max = config.get_intro_clip_max()
    print(f"  → Вступительный блок: первые {config.INTRO_BLOCK_DURATION}с → клипы {intro_min}-{intro_max}с")

    # Пути к папкам
    base_dir = Path(__file__).parent.parent
    _queue_dir_arg = getattr(args, 'queue_dir', None)
    if _queue_dir_arg and Path(_queue_dir_arg).is_dir():
        ready_dir = Path(_queue_dir_arg)
        print(f"\n[GUI] Queue dir: {ready_dir.name}/")
    else:
        ready_dir = base_dir / 'inbox'
    projects_dir = base_dir / 'projects'
    concat_script = _engine_script('assemble.py')

    # Проверка папок
    if not ready_dir.exists():
        ready_dir.mkdir(parents=True, exist_ok=True)
        print(f"\n✅ Папка {ready_dir.name} создана: {ready_dir}")

    if not projects_dir.exists():
        projects_dir.mkdir(parents=True, exist_ok=True)
        print(f"✅ Папка projects создана: {projects_dir}")

    # Получение списка файлов
    audio_files = get_ready_files(ready_dir)

    # --file-list из GUI: сохраняем порядок загрузки (обходит алфавитную сортировку).
    # ВАЖНО: при переданном --file-list полный скан папки очереди ЗАПРЕЩЁН.
    # Раньше пропавший файл (или битый JSON) молча оставлял audio_files = полный
    # скан inbox/ — запуск обрабатывал ЧУЖИЕ файлы очереди с чужим слепком
    # настроек и мог повторно прогнать уже обработанный файл (дубль проекта).
    # Теперь: явная ошибка + exit 1 (GUI покажет "Phase failed" и пойдёт к следующему файлу).
    _file_list_raw = getattr(args, 'file_list', None)
    if _file_list_raw:
        try:
            import json as _json_fl
            _fl_all = [Path(p) for p in _json_fl.loads(_file_list_raw)]
        except Exception as _fle:
            print(f"\n[ERROR] --file-list не разобран: {_fle}")
            print("   Скан папки очереди при --file-list запрещён — запуск прерван.")
            sys.exit(1)
        _fl_missing = [p for p in _fl_all if not p.exists()]
        _fl = [p for p in _fl_all if p.exists()]
        for _mp in _fl_missing:
            print(f"[ERROR] Файл очереди не найден: {_mp}")
        if not _fl:
            print("\n[ERROR] Ни один файл из --file-list не существует. Запуск прерван (скан папки запрещён).")
            sys.exit(1)
        audio_files = _fl
        _miss_note = f"  (пропущено отсутствующих: {len(_fl_missing)})" if _fl_missing else ""
        print(f"\n[GUI] File list: {len(audio_files)} file(s) in GUI load order{_miss_note}")

    if not audio_files:
        print(f"\n[WARN] В очереди нет входных файлов")
        print(f"   Папка: {ready_dir}")
        print(f"   Положите туда .wav .mp3 .flac .m4a .aac или видео .mp4 .mov .mkv .webm .avi .m4v")
        sys.exit(0)

    print(f"\nНайдено файлов: {len(audio_files)}")
    for f in audio_files:
        print(f"   - {f.name}")

    # Парсим --ref-map (индивидуальные рефы из GUI: {"audio.wav": "/path/.pending_refs/stem/"})
    import json as _json
    _ref_map_raw = getattr(args, 'ref_map', None)
    if _ref_map_raw:
        os.environ['BEGO_REF_MAP'] = _ref_map_raw
        print(f"\n[GUI] Ref map: индивидуальные рефы для {len(_json.loads(_ref_map_raw))} файлов")

    # Парсим --style-map (индивидуальные стили из GUI: {"audio.mp3": "/path/sg.txt"})
    _style_map_raw = getattr(args, 'style_map', None)
    style_map = {}
    if _style_map_raw:
        try:
            style_map = _json.loads(_style_map_raw)
            print(f"\n[GUI] Style map: {len(style_map)} audio->SG mapping(s) — автоматический поиск отключён")
            for _a, _s in style_map.items():
                print(f"   {_a} -> {Path(_s).name}")
        except Exception as e:
            print(f"\n[WARN] Не удалось разобрать --style-map: {e}. Используем автопоиск.")
            style_map = {}

    # ПРОВЕРКА НА КОНФЛИКТ SG_ALL + ИНДИВИДУАЛЬНЫЕ
    # Пропускаем если стили переданы явно через GUI (--style или --style-map)
    sg_all_file = None
    use_sg_all = False
    if style_map or getattr(args, 'style', None):
        # Стили переданы явно из GUI — автоматический поиск не требуется
        pass
    else:
        print("\n" + "="*60)
        print("ПРОВЕРКА STYLE GUIDES")
        print("="*60)

        sg_all_file = get_sg_all_file(ready_dir)

        if sg_all_file:
            print(f"\n✅ SG_ALL найден: {sg_all_file.name}")

            # Проверяем есть ли индивидуальные
            individual_guides = []
            for audio_file in audio_files:
                guides = get_all_style_guides_for_audio(audio_file, ready_dir)
                individual_guides.extend(guides)

            if individual_guides:
                print(f"\n⚠️  НАЙДЕНЫ ИНДИВИДУАЛЬНЫЕ STYLE GUIDES!")
                print(f"   Количество: {len(individual_guides)}")
                for g in individual_guides:
                    print(f"   • {g.name}")

                print("\n" + "="*60)
                print("ВЫБЕРИТЕ ДЕЙСТВИЕ:")
                print("="*60)
                print("  1. Использовать SG_ALL для ВСЕХ аудио")
                print("  2. Использовать индивидуальные для каждого аудио")
                print("  3. Временно заморозить SG_ALL (переименовать в .bak)")
                print()

                try:
                    choice = input("Введите номер (1/2/3): ").strip()
                except EOFError:
                    choice = '2'
                    print(f"[non-interactive] auto-selected: 2 (individual style guides)")

                if choice == '1':
                    print(f"\n✅ Будет использован SG_ALL: {sg_all_file.name}")
                    use_sg_all = True
                elif choice == '2':
                    print(f"\n✅ Будут использованы индивидуальные style guides")
                    use_sg_all = False
                elif choice == '3':
                    # Переименовываем SG_ALL
                    bak_name = sg_all_file.name + '.bak'
                    sg_all_file.rename(sg_all_file.parent / bak_name)
                    print(f"\n✅ SG_ALL заморожен: {bak_name}")
                    use_sg_all = False
                else:
                    print(f"\n⚠️  Неверный выбор, используем SG_ALL по умолчанию")
                    use_sg_all = True
            else:
                print(f"\n✅ Индивидуальные style guides не найдены")
                use_sg_all = True
        else:
            print(f"\nℹ️  SG_ALL не найден, будут использованы индивидуальные style guides")
            use_sg_all = False

    # Обработка каждого файла
    print("\n" + "="*60)
    print("ОБРАБОТКА ПРОЕКТОВ")
    print("="*60)

    # Инициализация списка ошибок
    errors = []
    success_count = 0
    _processed_names: set = set()  # track processed filenames for live queue rescan

    # Определяем нужно ли копировать аудио (если несколько style guides)
    copy_audio = False
    if not use_sg_all:
        # Проверяем есть ли несколько style guides для одного аудио
        for audio_file in audio_files:
            guides = get_all_style_guides_for_audio(audio_file, ready_dir)
            if len(guides) > 1:
                copy_audio = True
                break

    # Queue loop: process files, rescan for new ones after each project
    _queue_index = 0
    while _queue_index < len(audio_files):
        audio_file = audio_files[_queue_index]
        _queue_index += 1
        _processed_names.add(audio_file.name)
        print(f"\n  [{_queue_index}/{len(audio_files)}] {audio_file.name}")
        try:
            # Определяем какой style guide использовать
            if style_map and audio_file.name in style_map:
                style_guide = Path(style_map[audio_file.name])  # индивидуальный из GUI
            elif getattr(args, 'style', None):
                style_guide = Path(args.style)  # один для всех из GUI
            elif use_sg_all and sg_all_file:
                style_guide = sg_all_file
            else:
                style_guide = None  # Будет найден автоматически по имени в очереди
            
            # Настройка проекта
            project_info = setup_project(audio_file, ready_dir, projects_dir, concat_script, style_guide, copy_audio)

            # Удаление исходного файла из очереди (только если проект создан успешно)
            if not copy_audio and audio_file.exists():
                try:
                    audio_file.unlink()
                    print(f"   [OK] Аудиофайл перемещён: {audio_file.name}")
                except Exception as e:
                    print(f"   [WARN] Не удалось удалить оригинал: {e}")

            print(f"\n[OK] Проект создан: {project_info['name']}")
            print(f"   Папка: {project_info['project_dir']}")
            # Сигнал для GUI: папка проекта (по ней конвейер перезапускает генерацию с места)
            print(f"PROJECT_DIR: {project_info['project_dir']}", flush=True)

            # Запуск полного пайплайна сразу после создания проекта
            pipeline_ok = True
            if run_pipeline:
                audio_path = Path(project_info['audio_file'])
                style_path = Path(project_info['style_guide']) if project_info.get('style_guide') else None
                project_name = project_info['name']
                project_dir = Path(project_info['project_dir'])
                transcription_path = Path(project_info['transcription'])

                print(f"\n{'='*60}")
                print(f"ПАЙПЛАЙН: {project_name}")
                print(f"{'='*60}")

                if not style_path or not style_path.exists():
                    print(f"   [WARN] Style guide не найден, пайплайн пропущен")
                else:
                    _full_log_path = project_dir / f'{project_name}_full.log'
                    _full_log_fh = open(_full_log_path, 'w', encoding='utf-8', errors='replace')
                    _orig_stdout = sys.stdout
                    sys.stdout = _TeeStream(_orig_stdout, _full_log_fh)
                    # Лог проекта обязан быть ПОЛНЫМ. Раньше сюда шёл только
                    # stdout: всё, что идёт через logging (в т.ч. причины вроде
                    # «[stock] промптер не сработал: …»), писалось в stderr и в
                    # общий agent/logs — в папке проекта их не было вовсе, и
                    # разбор чужого прогона упирался в «пришлите второй файл»
                    # (разбор жалоб 29.08).
                    #
                    # Два канала, без дублей: хендлер логгера пишет через ТОТ ЖЕ
                    # файловый объект (второй FileHandler на тот же путь спорил
                    # бы за позицию записи), а tee на stderr ловит то, что печатают
                    # мимо logging — трейсбеки и сторонние библиотеки. Дублей нет,
                    # потому что StreamHandler из setup_logging захватил ссылку на
                    # ИСХОДНЫЙ stderr раньше этой подмены и пишет только в консоль.
                    _orig_stderr = sys.stderr
                    sys.stderr = _TeeStream(_orig_stderr, _full_log_fh)
                    _project_log_handler = logging.StreamHandler(_full_log_fh)
                    _project_log_handler.setLevel(
                        getattr(logging, getattr(config, 'LOG_LEVEL', 'INFO'), logging.INFO))
                    _project_log_handler.setFormatter(
                        logging.Formatter(getattr(config, 'LOG_FORMAT',
                                                  '%(asctime)s | %(levelname)-8s | %(message)s')))
                    logging.getLogger().addHandler(_project_log_handler)
                    # Log launch arguments
                    import shlex as _shlex
                    print(f"  [ARGS] {' '.join(_shlex.quote(str(a)) for a in sys.argv)}")
                    try:

                        # === Обычный режим: транскрипция + режиссёр ===
                        logging.info(f"Запуск пайплайна: {audio_path.name}, язык: {language_code}")

                        # -- REF PREFLIGHT: compress refs in background while transcription runs --
                        import threading as _threading
                        _ref_dir_bg = project_dir / 'ref'
                        _ref_thread = None
                        if _ref_dir_bg.exists():
                            _ref_thread = _threading.Thread(
                                target=compress_refs,
                                args=(str(_ref_dir_bg),),
                                kwargs={'max_mb': 5.0},
                                daemon=True,
                                name='ref-compress',
                            )
                            _ref_thread.start()
                            logging.info(f"ref_compress: background thread started for {_ref_dir_bg}")

                        # Готовую транскрипцию не переделываем: повторный
                        # запуск проекта (после сбоя стадии, доработки плана,
                        # смены настроек) платил за неё заново, хотя файл
                        # лежит рядом (улов 26.08).
                        _existing_words = 0
                        if transcription_path.exists():
                            try:
                                _existing = json.loads(
                                    transcription_path.read_text(encoding='utf-8-sig'))
                                _existing_words = len(_existing.get('words') or [])
                            except Exception as _exc:
                                logging.warning(
                                    f"transcription.json не прочитан ({_exc}) — транскрибирую заново")
                        if _existing_words:
                            print(f"  ✓ Транскрипция уже есть ({_existing_words} слов) — пропускаю")
                            logging.info(
                                f"Транскрипция взята с диска: {transcription_path} ({_existing_words} слов)")
                            transcription_result = _existing
                        else:
                            transcription_result = transcribe_audio(str(audio_path), str(transcription_path), language_code=language_code)
                            logging.info(f"Транскрипция завершена: {transcription_path}")

                        if transcription_result.get('_quality_critical', False):
                            print(f"\n⛔ Транскрипция '{project_name}' аномальная!")
                            print("   Режиссёр НЕ запущен. Проверьте transcription.json.")
                            pipeline_ok = False
                            continue

                        # SG Adapt: адаптируем стайлгайд под текущий сюжет
                        if getattr(args, 'adapt_sg', False) and style_path:
                            print("  Adapting style guide to current script...")
                            from units.styleguide_adapter import adapt_style_guide
                            adapted = adapt_style_guide(
                                original_sg_path=str(style_path),
                                project_dir=str(project_dir),
                                project_name=project_name,
                            )
                            style_path = Path(adapted)
                            print(f"  Adapted SG: {style_path.name}")

                        direct_project(
                            str(transcription_path),
                            str(style_path),
                            str(project_dir),
                            project_name,
                            two_pass=getattr(args, 'two_pass', False),
                        )
                        logging.info(f"Режиссерский анализ завершен: {project_dir}")
                        # Тут закончена ТОЛЬКО режиссура: план и промпты. Впереди
                        # источники кадра, генерация и склейка. Прежнее «Пайплайн
                        # завершён» пугало пользователей — они видели «завершён»,
                        # готового ролика не находили и считали, что всё сломалось
                        # (замечание пользователя 27.08).
                        print(f"\n[OK] Режиссура завершена: {project_name} — "
                              f"план и промпты готовы.")
                        # Сигнал для GUI: дальше — генерация (изменения настроек применяются с места)
                        print(f"DIRECTION_DONE: {project_dir}", flush=True)
                        print("     Впереди: сборка или генерация кадров и "
                              "склейка в цельный готовый ролик.")


                        # Join ref-compress background thread before generation uses the refs
                        if '_ref_thread' in dir() and _ref_thread and _ref_thread.is_alive():
                            logging.info("ref_compress: waiting for background thread...")
                            _ref_thread.join(timeout=120)

                        # Пост-пайплайн генерация: silent если передан --post-action, иначе интерактивно
                        post_action = getattr(args, 'post_action', None)
                        prompt_mode = getattr(args, 'prompt_mode', 'single')
                        # Передаём vislide_interval через env var (как остальные runtime-параметры)
                        if clip_mode == 'vislide' and vislide_interval >= 2:
                            os.environ['BEGO_VISLIDE_INTERVAL'] = str(vislide_interval)
                        if post_action:
                            _run_post_pipeline_silent(
                                str(project_dir), post_action, prompt_mode,
                                use_codex_adapt=getattr(args, 'codex_adapt', False),
                            )

                        # Сигнал для GUI: все этапы завершены, можно запускать конкат
                        print(f"PROJECT_DONE: {project_dir}", flush=True)

                        # Live Queue rescan — только для CLI-режима без явного списка.
                        # GUI передаёт --file-list и обрабатывает очередь как серию
                        # одиночных запусков (оркестратор begoframe подаёт по одному файлу
                        # со своим слепком настроек) → rescan отключён: он «съедал» чужие
                        # файлы и дестабилизировал очередь (готов был только 1 из N).
                        if not _file_list_raw:
                            _new_files = [f for f in get_ready_files(ready_dir)
                                          if f.name not in _processed_names and f not in audio_files]
                            if _new_files:
                                # Apply order.json if exists (GUI QM reorder)
                                _order_path = ready_dir / 'order.json'
                                if _order_path.exists():
                                    try:
                                        _oj = json.loads(_order_path.read_text(encoding='utf-8-sig'))
                                        _order_map = {name: i for i, name in enumerate(_oj)}
                                        _new_files.sort(key=lambda f: _order_map.get(f.name, 9999))
                                    except Exception:
                                        pass
                                audio_files.extend(_new_files)
                                print(f"QUEUE_UPDATE: {len(audio_files)}", flush=True)
                                print(f"  [QUEUE] +{len(_new_files)} new file(s) detected, total: {len(audio_files)}")

                    except Exception as pipeline_err:
                        print(f"\n[ERROR] Ошибка пайплайна для {project_name}: {pipeline_err}")
                        logging.debug(traceback.format_exc())
                        pipeline_ok = False
                        errors.append({
                            'file': project_name,
                            'error': str(pipeline_err),
                            'traceback': traceback.format_exc()
                        })
                    finally:
                        # Порядок важен: сначала снимаем хендлер логгера, потом
                        # возвращаем потоки и только затем закрываем файл —
                        # иначе logging продолжил бы писать в закрытый объект.
                        try:
                            logging.getLogger().removeHandler(_project_log_handler)
                            _project_log_handler.close()
                        except Exception:                      # noqa: BLE001
                            pass
                        sys.stdout = _orig_stdout
                        sys.stderr = _orig_stderr
                        _full_log_fh.close()

            if pipeline_ok:
                success_count += 1

        except Exception as e:
            error_msg = f"Ошибка при создании проекта {audio_file.name}: {e}"
            print(f"\n[ERROR] {error_msg}")
            logging.debug(traceback.format_exc())

            # Сохраняем ошибку в список
            errors.append({
                'file': audio_file.name,
                'error': str(e),
                'traceback': traceback.format_exc()
            })
            continue

    # Сохранение отчёта об ошибках
    if errors:
        error_report_path = base_dir / 'errors.json'
        with open(error_report_path, 'w', encoding='utf-8', errors='replace') as f:
            json.dump({
                'timestamp': datetime.now().isoformat(),
                'total_projects': len(audio_files),
                'successful': success_count,
                'failed': len(errors),
                'error_rate': len(errors) / len(audio_files),
                'errors': errors
            }, f, indent=2, ensure_ascii=False)
        
        print(f"\n⚠️  Отчёт об ошибках сохранён: {error_report_path}")

    # Итог
    print("\n" + "="*60)
    print("[ERROR] ОБРАБОТКА ОЧЕРЕДИ ЗАВЕРШЕНА С ОШИБКАМИ!" if errors
          else "[OK] ОБРАБОТКА ОЧЕРЕДИ ЗАВЕРШЕНА!")
    print("="*60)

    if run_pipeline:
        print(f"""
Обработано проектов: {success_count} из {len(audio_files)}
Успешно: {success_count}
Ошибки: {len(errors)}

Папка проектов: {projects_dir}
""")
        if success_count > 0:
            # Подсказка идёт от того, что прогон РЕАЛЬНО сделал. Раньше она
            # всегда звала генерировать видео вручную в Veo3 — даже когда
            # десять клипов только что сгенерировались сами, и человек читал
            # «положи видеофрагменты в папку» над готовой папкой vid_img
            # (улов 01.09.2026).
            _media_made = bool(post_action) and str(post_action).lower() != 'skip'
            print("СЛЕДУЮЩИЙ ШАГ:")
            if _media_made:
                print("   Медиа готово — осталась склейка.")
                print("   Из интерфейса она запускается сама; вручную — "
                      "process_fast.bat в папке проекта.")
            else:
                print("   Открой prompts_*.txt в папке проекта и генерируй видео в Veo3.")
                print("   Затем положи видеофрагменты в папку проекта и запусти process_fast.bat")
        if errors:
            print("\nОШИБКИ (проекты, требующие повторного запуска):")
            for err in errors:
                proj = err.get('project') or err.get('file', '?')
                msg  = err.get('error', '?')
                print(f"   [{proj}] {msg}")
            print()
            print("КАК ПОВТОРИТЬ режиссуру без повторной транскрипции:")
            print("   python agent/cli.py direct \\")
            print("     --transcription projects/<PROJECT>/transcription.json \\")
            print("     --style inbox/<STYLE>.txt \\")
            print("     --project <PROJECT>")
        print()
    else:
        print(f"""
Создано проектов: {success_count} из {len(audio_files)}
Успешно: {success_count}
Ошибки: {len(errors)}

Папка проектов: {projects_dir}

СЛЕДУЮЩИЕ ШАГИ:
   Запустите полный пайплайн:
      python cli.py process --run
   Или для отдельного проекта:
      python cli.py run --audio projects/PROJECT_NAME/audio.mp3 --style style_guide.txt --project PROJECT_NAME
""")
    if errors:
        raise SystemExit(1)


# ============================================================================
# CLI ИНТЕРФЕЙС
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='Video Production Agent - Автоматизация создания видео',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Запускается программой BegoFrame: cli.py process --run ..."
    )
    
    subparsers = parser.add_subparsers(dest='command', help='Команды')
    
    # Команда: process (обработка очереди)
    parser_process = subparsers.add_parser('process', help='Обработка файлов из очереди')
    parser_process.add_argument('--style', default=None, metavar='FILE',
                                help='Явный style guide для всех аудио (передаётся из GUI)')
    parser_process.add_argument('--style-map', default=None, metavar='JSON',
                                help='JSON-маппинг {"audio.mp3": "/path/sg.txt"} для индивидуальных style guides (из GUI)')
    parser_process.add_argument('--file-list', default=None, metavar='JSON',
                                help='JSON-массив путей к аудиофайлам (из GUI, сохраняет порядок загрузки)')
    parser_process.add_argument('--queue-dir', default=None, metavar='DIR',
                                help='Папка очереди (из GUI: inbox/)')
    parser_process.add_argument('--ref-map', default=None, metavar='JSON',
                                help='JSON-маппинг {"audio.wav": "/path/staging/"} индивидуальных рефов (из GUI)')
    parser_process.add_argument('--run', action='store_true',
                                help='Запустить полный пайплайн (транскрипция + режиссура) сразу после настройки каждого проекта')
    parser_process.add_argument('--two-pass', action='store_true', default=False,
                                help='Двухпроходный режим для режиссуры (для проектов > 15 мин)')
    parser_process.add_argument('--language', choices=config.ASSEMBLYAI_SUPPORTED_LANGUAGES,
                                default=None, help='Язык озвучки (default: из .env ASSEMBLYAI_LANGUAGE_CODE)')
    parser_process.add_argument('--clip-mode', choices=['video', 'slideshow', 'vislide'], default=None,
                                help='Тип клипов: video (Veo3, 4-8с), slideshow (картинки, 2-7с), vislide (каждый N-й клип оживляется). Если не указан — спрашивает интерактивно.')
    parser_process.add_argument('--vislide-interval', type=int, default=0, metavar='N',
                                help='В режиме vislide: каждый N-й клип идёт в i2v воркер (остальные — Ken Burns). Default=3')
    parser_process.add_argument('--clip-min', type=int, default=None,
                                help='Минимальная длительность клипа (сек, переопределяет .env)')
    parser_process.add_argument('--clip-max', type=int, default=None,
                                help='Максимальная длительность клипа (сек, переопределяет .env)')
    parser_process.add_argument('--post-action',
                                choices=POST_ACTION_CHOICES,
                                default=None,
                                help='Post-pipeline generation; ImageGen provider is selected with PIPELINE_IMG_GEN')
    parser_process.add_argument('--prompt-mode',
                                choices=['single', 'perclip'],
                                default='single',
                                help='Prompt mode for i2v: single (one prompt for all) or perclip (original prompts from prompts.txt)')
    parser_process.add_argument('--codex-adapt', action='store_true', default=False,
                                help='GPT adapt image prompts for VEO i2v animation')
    parser_process.add_argument('--adapt-sg', action='store_true', default=False,
                                help='Adapt style guide to current script via GPT (Codex CLI) before direction')
    parser_process.set_defaults(func=cmd_process_queue)

    args = parser.parse_args()
    
    if args.command is None:
        parser.print_help()
        sys.exit(0)
    
    args.func(args)


if __name__ == '__main__':
    main()
