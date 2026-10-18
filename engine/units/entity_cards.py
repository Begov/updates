"""Internet-backed person/place cards over the final video.

The module detects concrete named people and places in transcription.json,
downloads a representative image (Wikipedia/Wikimedia first, Yandex fallback),
renders a transparent PNG card, and overlays the card with a small pop/bounce
animation.
"""

from __future__ import annotations

import hashlib
import bisect
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import requests

_AGENT_DIR = Path(__file__).resolve().parent.parent
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

import params as config
_ENTITY_PROMPT_PATH = _AGENT_DIR / "instructions" / "entity_overlay.txt"
_DEFAULT_ENTITY_PROMPT_TEMPLATE = """Select only explicitly named real people and places.
Never infer a specific identity from a generic role such as politician, president,
scientist, historian, or professor. Return fewer items when uncertain.
Return at most {{MAX_ITEMS}} items as JSON:
{"entities":[{"start":number,"name":string,"kind":"person"|"place",
"query":string,"score":number}]}

TRANSCRIPT (lines are [seconds] text):
{{TRANSCRIPT}}
"""


@dataclass
class EntityCandidate:
    start: float
    end: float
    name: str
    kind: str = "place"
    query: str = ""
    score: float = 0.0


@dataclass
class EntitySlot:
    start: float
    end: float
    name: str
    kind: str
    query: str
    path: str
    image_path: str
    source_url: str = ""
    source_title: str = ""
    score: float = 0.0


_HEADERS = {
    "User-Agent": getattr(
        config,
        "SOURCE_MEDIA_USER_AGENT",
        "BegoFrame/1.0 (documentary video renderer; https://wikipedia.org)",
    ),
    "Accept": "application/json,text/html,image/*,*/*",
}

_STOP_ENTITY_WORDS = {
    "The", "This", "That", "Then", "There", "When", "Where", "Why", "How",
    "And", "But", "Or", "Yet", "So", "In", "On", "At", "For", "With",
    "Video", "Image", "Scene", "Narrator", "First", "Second", "Third",
    "Сегодня", "Затем", "Потом", "Когда", "Где", "Почему", "Сцена",
    "Видео", "История", "Этот", "Эта", "Они", "Но", "А", "И",
}

_LEADING_ENTITY_TITLES = {
    "Archaeologist", "Historian", "Scientist", "Professor", "Doctor", "Dr",
    "King", "Queen", "Emperor", "President", "Director", "Writer", "Politician",
    "Археолог", "Историк", "Ученый", "Учёный", "Профессор", "Доктор",
    "Император", "Царь", "Король", "Президент", "Режиссер", "Режиссёр", "Писатель",
    "Политик",
}
_LEADING_ENTITY_TITLES_CASEFOLD = {title.casefold() for title in _LEADING_ENTITY_TITLES}

_GENERIC_ENTITY_ROLES = {
    "actor", "archaeologist", "director", "doctor", "emperor", "historian", "king",
    "minister", "official", "politician", "president", "professor", "queen",
    "researcher", "scientist", "writer",
    "актер", "актёр", "археолог", "государственный деятель", "доктор", "император",
    "историк", "король", "министр", "писатель", "политик", "президент", "профессор",
    "режиссер", "режиссёр", "ученый", "учёный", "царь", "чиновник",
}

_BROAD_ENTITY_NAMES = {
    "europe", "asia", "africa", "medieval europe", "modern europe", "late medieval europe",
    "ancient dna", "modern era", "middle ages", "black death", "central asia",
    "европа", "азия", "африка", "средневековая европа", "черная смерть",
    "чёрная смерть", "средние века", "центральная азия",
}

_ENTITY_CONTEXT_WORDS = {
    "person": (
        "археолог", "учен", "учё", "историк", "профессор", "доктор", "царь", "король",
        "император", "президент", "писатель", "исследователь", "родился", "умер",
        "archaeologist", "scientist", "historian", "professor", "doctor", "king",
        "queen", "emperor", "president", "writer", "researcher", "born", "died",
    ),
    "place": (
        "город", "место", "храм", "гора", "река", "озеро", "пещер", "долин", "регион",
        "кладбищ", "могильник", "поселение", "раскоп", "у ", "в ", "на ", "возле",
        "city", "site", "temple", "mount", "river", "lake", "cave", "valley",
        "region", "cemetery", "settlement", "excavation", "near", "in ", "at ",
    ),
}


def plan_and_render_entity_overlays(
    project_dir: str | Path,
    size: str = "1920x1080",
    max_overlays: Optional[int] = None,
    backend: Optional[str] = None,
    hold_sec: Optional[float] = None,
    min_gap_sec: Optional[float] = None,
    no_cache: bool = False,
    stop_event: Optional["threading.Event"] = None,
    deadline_ts: Optional[float] = None,
    cache_only: bool = False,
) -> list[dict[str, Any]]:
    """Return rendered overlay slots ready for apply_entity_overlays().

    stop_event  — стоп-кнопка пайплайна: бросаем недокачанное, отдаём что успели;
    deadline_ts — время (time.time()), после которого новые карточки не начинаем;
    cache_only  — не ходить в сеть вообще: отдать только то, что уже в кэше
                  (для конката, когда фон уже отработал).

    Карточки независимы: 12 из 20 — нормальное видео, просто меньше карточек.
    Поэтому частичный результат корректен, а не «ошибка».
    """

    if not bool(getattr(config, "ENTITY_OVERLAY_ENABLED", False)):
        print("[entity] disabled by ENTITY_OVERLAY_ENABLED=false")
        return []

    project = Path(project_dir)
    transcript = _load_transcript(project)
    if not transcript:
        print("[entity] no transcription.json - nothing to overlay")
        return []
    montage_clips = _load_montage_clips(project)

    try:
        from PIL import Image, ImageDraw, ImageFont  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            f"Pillow (PIL) is required for entity overlays but is not installed: {exc}"
        ) from exc

    width, height = _parse_size(size)
    requested_max = int(max_overlays if max_overlays is not None else getattr(config, "ENTITY_OVERLAY_MAX", 20))
    all_found = requested_max <= 0
    max_n = 240 if all_found else max(1, requested_max)
    limit_label = "all" if all_found else str(max_n)
    hold = float(hold_sec if hold_sec is not None else getattr(config, "ENTITY_OVERLAY_HOLD_SEC", 4.2))
    gap = float(min_gap_sec if min_gap_sec is not None else getattr(config, "ENTITY_OVERLAY_MIN_GAP_SEC", 24.0))
    if all_found and min_gap_sec is None and not os.environ.get("ENTITY_OVERLAY_MIN_GAP_SEC"):
        gap = min(gap, max(6.0, hold + 1.0))
    backend_norm = (backend or getattr(config, "ENTITY_OVERLAY_BACKEND", "auto")).strip().lower()
    entity_prompt_template = _load_entity_prompt_template()
    entity_prompt_sha256 = hashlib.sha256(
        entity_prompt_template.encode("utf-8")
    ).hexdigest()
    cache_dir = project / "cache" / "entity_overlays"
    raw_dir = cache_dir / "raw"
    card_dir = cache_dir / "cards"
    raw_dir.mkdir(parents=True, exist_ok=True)
    card_dir.mkdir(parents=True, exist_ok=True)

    plan_path = cache_dir / "entity_overlay_plan.json"
    # size в хэш НЕ входит: детекция сущностей из транскрипта и скачанные картинки
    # от разрешения видео не зависят. Раньше входил — и смена Fast→4K обесценивала
    # ВСЮ работу (передетекция + перекачивание), хотя менялся только размер карточки.
    # Карточки под новый размер дорендериваются ниже локально (PIL, миллисекунды).
    legacy_hash_payload = {
        "transcript": [
            {
                "start": round(_seg_start(s), 3),
                "end": round(_seg_end(s), 3),
                "text": str(s.get("text") or "")[:500],
            }
            for s in transcript[:1800]
        ],
        "max": "all" if all_found else max_n,
        "hold": hold,
        "gap": gap,
        "backend": backend_norm,
        "providers": getattr(config, "ENTITY_OVERLAY_PROVIDERS", "wikipedia,yandex"),
        "langs": getattr(config, "ENTITY_OVERLAY_WIKI_LANGS", "ru,en"),
        "detector_prompt_sha256": entity_prompt_sha256,
    }
    legacy_plan_hash = _stable_hash(legacy_hash_payload)
    plan_hash = _stable_hash(
        {
            **legacy_hash_payload,
            "timeline_layout": "clip-bound-v1",
            "montage_clips": [
                {
                    "id": c.get("id"),
                    "start": round(float(c["start"]), 3),
                    "end": round(float(c["end"]), 3),
                }
                for c in montage_clips
            ],
        }
    )
    if not no_cache and plan_path.exists():
        cached = _load_json(plan_path)
        cached_hash = cached.get("entity_hash") if isinstance(cached, dict) else None
        if (
            isinstance(cached, dict)
            and isinstance(cached_hash, str)
            and cached_hash in {plan_hash, legacy_plan_hash}
        ):
            slots = cached.get("slots")
            if isinstance(slots, list):
                usable = _rehydrate_cached_slots(slots, card_dir=card_dir, width=width, height=height)
                if cached_hash == legacy_plan_hash and usable:
                    usable = _fit_cached_slots_to_clips(
                        usable, montage_clips=montage_clips, total=_transcript_duration(transcript))
                    _write_manifest(
                        plan_path,
                        plan_hash,
                        usable,
                        partial=bool(cached.get("partial")),
                    )
                    print(f"[entity] migrated legacy cache to clip-bound layout: {len(usable)}")
                # partial — фон прервали на полпути. Конкату (cache_only) отдаём как есть:
                # лучше 12 карточек, чем ни одной. А полный прогон такой кэш НЕ принимает,
                # иначе недостающие не докачаются никогда.
                is_partial = bool(cached.get("partial"))
                if usable and (cache_only or not is_partial):
                    print(f"[entity] loaded cached cards: {len(usable)}"
                          + (" (частичные — фон не успел)" if is_partial else ""))
                    return usable if all_found else usable[:max_n]
                if is_partial and not cache_only:
                    print(f"[entity] в кэше {len(usable)} частичных карточек — дозабираю остальные")

    # cache_only: конкат после фоновой стадии — в сеть не идём, берём что готово.
    # Кэш выше уже проверен; сюда попали, только если его нет или он протух.
    if cache_only:
        print("[entity] cache-only: готовых карточек нет — пропускаю (фон не успел)")
        return []

    if stop_event is not None and stop_event.is_set():
        print("[entity] остановлено до начала детекции")
        return []

    detect_n = 240 if all_found else max(max_n * 2, max_n)
    raw: list[EntityCandidate] = []
    # Выбор оператора детекции. auto → сначала прямые chat-API (быстро/надёжно),
    # затем Codex CLI (подписка), иначе regex.
    _b = backend_norm
    if _b == "auto":
        if getattr(config, "GPT_NEXUS_API_KEY", ""):
            _b = "gpt_nexus"
        elif getattr(config, "CODEX_RESELLER_API_KEY", ""):
            _b = "codex_reseller"
        elif getattr(config, "DEEPSEEK_API_KEY", ""):
            _b = "deepseek"
        elif getattr(config, "OPENROUTER_API_KEY", ""):
            _b = "openrouter"
        elif getattr(config, "META_MUSE_API_KEY", ""):
            _b = "meta_muse"
        elif getattr(config, "CODEX_EXE_PATH", ""):
            _b = "gpt"
        else:
            _b = "regex"
    try:
        if _b == "gpt":
            raw = _detect_gpt(
                project, transcript, max_n=detect_n,
                prompt_template=entity_prompt_template,
            )
        elif _b in (
            "deepseek", "openrouter", "meta_muse", "claude_api",
            "codex_reseller", "gpt_nexus",
        ):
            raw = _detect_chat(
                transcript, detect_n, _b,
                prompt_template=entity_prompt_template,
            )
        elif _b == "claude":
            raw = _detect_claude(
                project, transcript, detect_n,
                prompt_template=entity_prompt_template,
            )
        # _b == "regex" → попадёт в общий фолбэк ниже
    except Exception as exc:
        print(f"[entity] {_b} detect failed ({_safe_text(exc)}); regex fallback")
        raw = []
    if raw:
        print(f"[entity] {_b} backend: {len(raw)} candidate(s)")
    if not raw:
        # regex — и явно выбранный вариант, и фолбэк для всех остальных
        raw = _detect_regex(transcript)
        if raw:
            print(f"[entity] regex backend: {len(raw)} candidate(s)")

    total = _transcript_duration(transcript)
    finalize_n = 240 if all_found else max(max_n * 4, max_n)
    candidates = _finalize_candidates(
        raw, total, hold, gap, finalize_n, montage_clips=montage_clips)
    if not candidates:
        print("[entity] no concrete people/places found")
        _write_manifest(plan_path, plan_hash, [])
        return []

    providers = _csv(getattr(config, "ENTITY_OVERLAY_PROVIDERS", "wikipedia,yandex"))
    wiki_langs = _csv(getattr(config, "ENTITY_OVERLAY_WIKI_LANGS", "ru,en"))
    if not wiki_langs:
        wiki_langs = ["ru", "en"]

    # Берём в работу столько кандидатов, сколько нужно карточек. Раньше цикл шёл по всем
    # и прерывался по достижении max_n — при параллели так нельзя, иначе накачаем лишнего.
    todo = candidates if all_found else candidates[:max_n]

    workers = max(1, int(getattr(config, "ENTITY_OVERLAY_PARALLEL", 4) or 4))
    workers = min(workers, len(todo)) or 1

    # Сессия requests не потокобезопасна — по одной на поток (thread-local).
    _local = threading.local()

    def _session() -> requests.Session:
        s = getattr(_local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update(_HEADERS)
            _local.session = s
        return s

    def _should_stop() -> Optional[str]:
        if stop_event is not None and stop_event.is_set():
            return "остановлено пользователем"
        if deadline_ts and time.time() > deadline_ts:
            return "дедлайн"
        return None

    def _work(cand: EntityCandidate) -> Optional[EntitySlot]:
        # Проверяем ПЕРЕД каждой карточкой: внутри HTTP не прервать, но таймауты
        # ограничены (8/20с и 8/35с), поэтому ждать придётся не дольше одного запроса.
        if _should_stop():
            return None
        try:
            return _resolve_and_render_slot(
                cand,
                session=_session(),
                raw_dir=raw_dir,
                card_dir=card_dir,
                width=width,
                height=height,
                providers=providers,
                wiki_langs=wiki_langs,
            )
        except Exception as exc:
            print(f"  [entity] skipped {cand.name}: {_safe_text(exc)}")
            return None

    slots: list[EntitySlot] = []
    stopped_by: Optional[str] = None
    if workers <= 1:
        for cand in todo:
            stopped_by = _should_stop()
            if stopped_by:
                break
            slot = _work(cand)
            if slot:
                slots.append(slot)
                print(f"  [entity {len(slots)}/{limit_label}] {slot.name} -> {Path(slot.path).name}")
    else:
        print(f"[entity] downloading {len(todo)} card(s) in {workers} threads")
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_work, c): c for c in todo}
            for fut in as_completed(futures):
                done += 1
                slot = fut.result()
                if slot:
                    slots.append(slot)
                    print(f"  [entity {done}/{len(todo)}] {slot.name} -> {Path(slot.path).name}")
                if not stopped_by:
                    stopped_by = _should_stop()   # уже запущенные добегут, новые — нет
        # порядок задаёт таймлайн, а не порядок завершения потоков
        slots.sort(key=lambda s: s.start)

    if stopped_by:
        # Частичный результат — это нормально: карточки независимы, 12 из 20 = просто
        # меньше карточек в ролике. Пишем в манифест что успели, чтобы следующий
        # запуск дозабрал остальное из кэша, а не начинал с нуля.
        print(f"[entity] {stopped_by}: готово {len(slots)} из {len(todo)} карточек — "
              f"остальные пропускаю")

    data_slots = [asdict(s) for s in slots]
    # Пишем всегда — иначе конкат не увидит то, что фон успел скачать, и труд пропадёт.
    # Пометка partial не даёт следующему полному прогону принять неполный список
    # за финальный: он дозаберёт недостающее (скачанное уже в кэше).
    _write_manifest(plan_path, plan_hash, data_slots, partial=bool(stopped_by))

    # Человекочитаемая атрибуция для описания YouTube (карточки — Wikipedia/Wikimedia и т.п.).
    # Дедуп по source_url. В корень проекта, рядом с real_photos_credits.md / stock_credits.md.
    try:
        lines, seen = [], set()
        for s in sorted(data_slots, key=lambda x: x.get("start", 0)):
            url = (s.get("source_url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            name = (s.get("name") or "").strip()
            src = (s.get("source_title") or "Wikipedia").strip()
            row = "- " + " — ".join(p for p in (f"**{name}**" if name else "", src, url) if p)
            lines.append(row)
        md = (
            "# Источники карточек (люди и места)\n\n"
            "Изображения врезок-карточек, использованные в видео. Можно вставить в описание ролика.\n\n"
            + ("\n".join(lines) if lines else "_нет карточек с источником_")
            + "\n"
        )
        (project / "entity_credits.md").write_text(md, encoding="utf-8")
    except Exception as exc:
        print(f"[entity] credits .md write failed (non-fatal): {_safe_text(exc)}")

    if stopped_by:
        print(f"[entity] сохранил {len(data_slots)} готовых карточек (частично) — "
              f"конкат их возьмёт, остальные дозаберутся при следующем запуске")
    expected = len(candidates) if all_found else min(len(candidates), max_n)
    print(f"[entity] cards ready: {len(data_slots)}/{expected}")
    return data_slots


def apply_entity_overlays(
    base_video: str | Path,
    output_video: str | Path,
    slots: list[dict[str, Any]],
    ffmpeg_path: str = "ffmpeg",
    encoder_type: str = "cpu",
    bitrate: str = "10M",
    duration: float | None = None,
) -> str:
    """Overlay transparent PNG entity cards over base_video.

    Long 1080p projects must not be re-encoded end-to-end just to show a few
    4-second cards. Patch mode copies untouched ranges, re-encodes only card
    windows, then concatenates the short pieces back together.
    """

    active = [
        s for s in slots
        if s.get("path")
        and Path(str(s.get("path"))).is_file()
        and float(s.get("end", 0.0)) > float(s.get("start", 0.0))
    ]
    if not active:
        return str(base_video)

    base_video = Path(base_video)
    output_video = Path(output_video)
    output_video.parent.mkdir(parents=True, exist_ok=True)
    if output_video.exists():
        try:
            output_video.unlink()
        except PermissionError:
            output_video = output_video.with_name(
                f"{output_video.stem}_{os.getpid()}_{len(active)}{output_video.suffix}"
            )

    return _apply_entity_overlays_segmented(
        base_video=base_video,
        output_video=output_video,
        slots=active,
        ffmpeg_path=ffmpeg_path,
        encoder_type=encoder_type,
        bitrate=bitrate,
        duration=duration,
    )


def _apply_entity_overlays_segmented(
    *,
    base_video: Path,
    output_video: Path,
    slots: list[dict[str, Any]],
    ffmpeg_path: str,
    encoder_type: str,
    bitrate: str,
    duration: float | None,
) -> str:
    total = float(duration or 0.0)
    if total <= 0:
        total = _probe_duration(base_video, ffmpeg_path)
    if total <= 0:
        total = max(float(s.get("end", 0.0) or 0.0) for s in slots) + 1.0

    active = _normalize_active_slots(slots, total)
    if not active:
        return str(base_video)

    keyframes = _probe_keyframes(base_video, ffmpeg_path)
    reorder_delay = _probe_video_reorder_delay(base_video, ffmpeg_path)
    patches = _build_keyframe_patches(active, keyframes, total)
    work_dir = output_video.parent / f"_entity_overlay_segments_{os.getpid()}"
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    print(f"[entity] Overlaying {len(active)} card(s) with segment patch mode...")
    if len(keyframes) <= 1:
        print("[entity] keyframes unavailable; using one full-length safe patch")
    segments: list[Path] = []
    cursor = 0.0
    seg_idx = 0
    try:
        for patch_idx, patch in enumerate(patches, start=1):
            start = float(patch["start"])
            end = float(patch["end"])
            patch_slots = patch["slots"]
            if start > cursor + 0.05:
                out = work_dir / f"seg_{seg_idx:03d}_copy.mp4"
                # With B-frames, the next keyframe packet arrives earlier in DTS than
                # its presentation timestamp. Stop before that decode boundary so the
                # following GOP is not copied twice into the concat.
                copy_duration = max(0.0, start - cursor - reorder_delay)
                if copy_duration > 0.05:
                    _copy_passthrough_segment(
                        ffmpeg_path, base_video, out, cursor, copy_duration)
                    segments.append(out)
                    seg_idx += 1

            out = work_dir / f"seg_{seg_idx:03d}_entity_patch_{patch_idx:02d}.mp4"
            _overlay_cards_segment(
                ffmpeg_path=ffmpeg_path,
                src=base_video,
                out=out,
                start=start,
                duration=end - start,
                slots=patch_slots,
                encoder_type=encoder_type,
                bitrate=bitrate,
                work_dir=work_dir,
                patch_index=patch_idx,
            )
            segments.append(out)
            seg_idx += 1
            cursor = end
            for slot in patch_slots:
                print(
                    f"  [entity {slot['_index']}/{len(active)}] {slot.get('name', '')} "
                    f"at {slot['_start']:.1f}s..{slot['_end']:.1f}s"
                )

        if cursor < total - 0.05:
            out = work_dir / f"seg_{seg_idx:03d}_copy.mp4"
            _copy_passthrough_segment(ffmpeg_path, base_video, out, cursor, total - cursor)
            segments.append(out)

        if not segments:
            return str(base_video)
        video_only = work_dir / "entity_video_only.mp4"
        _concat_segments(ffmpeg_path, segments, video_only)
        _mux_original_audio(ffmpeg_path, video_only, base_video, output_video)
        if not output_video.exists() or output_video.stat().st_size < 1024:
            raise RuntimeError("entity segment concat produced no output")
        return str(output_video)
    finally:
        if not _env_truthy_local("ENTITY_OVERLAY_KEEP_TEMP", False):
            shutil.rmtree(work_dir, ignore_errors=True)


def _copy_passthrough_segment(ffmpeg_path: str, src: Path, out: Path, start: float, duration: float) -> None:
    if duration <= 0.05:
        return
    cmd = [
        ffmpeg_path, "-y", "-hide_banner", "-loglevel", "warning", "-nostdin",
        "-ss", f"{start:.6f}",
        "-i", str(src),
        "-t", f"{duration:.6f}",
        "-map", "0:v:0",
        "-an",
        "-c:v", "copy",
        "-avoid_negative_ts", "make_zero",
        "-reset_timestamps", "1",
        "-movflags", "+faststart",
        str(out),
    ]
    _run_ffmpeg_checked(cmd, f"copy entity segment {start:.2f}-{start + duration:.2f}s")


def _overlay_cards_segment(
    *,
    ffmpeg_path: str,
    src: Path,
    out: Path,
    start: float,
    duration: float,
    slots: list[dict[str, Any]],
    encoder_type: str,
    bitrate: str,
    work_dir: Path,
    patch_index: int,
) -> None:
    cmd = [
        ffmpeg_path, "-y", "-hide_banner", "-loglevel", "warning", "-nostdin",
        "-ss", f"{start:.6f}",
        "-i", str(src),
    ]
    filters = ["[0:v]setpts=PTS-STARTPTS[base0]"]
    previous = "base0"
    for local_idx, slot in enumerate(slots, start=1):
        card_duration = float(slot["_end"]) - float(slot["_start"])
        pattern = _render_card_sequence(
            Path(str(slot["path"])),
            work_dir,
            card_duration,
            fps=30,
            prefix=f"card_{patch_index:02d}_{local_idx:02d}",
        )
        cmd.extend(["-framerate", "30", "-i", str(pattern)])
        offset = max(0.0, float(slot["_start"]) - start)
        card_end = min(duration, offset + card_duration)
        card_label = f"card{local_idx}"
        next_label = f"base{local_idx}"
        y_expr = "(H-h)*0.22" if str(slot.get("kind") or "").lower() == "place" else "(H-h)*0.40"
        filters.append(
            f"[{local_idx}:v]format=rgba,"
            f"setpts=PTS-STARTPTS+{offset:.6f}/TB[{card_label}]"
        )
        filters.append(
            f"[{previous}][{card_label}]overlay=x=(W-w)/2:y={y_expr}:"
            f"eof_action=pass:repeatlast=0:"
            f"enable='between(t,{offset:.6f},{card_end:.6f})'[{next_label}]"
        )
        previous = next_label
    filters.append(f"[{previous}]format=yuv420p[v]")
    cmd.extend([
        "-t", f"{duration:.6f}",
        "-filter_complex", ";".join(filters),
        "-map", "[v]",
        "-an",
        *_encoder_flags_fast(encoder_type, bitrate),
        "-avoid_negative_ts", "make_zero",
        "-reset_timestamps", "1",
        "-movflags", "+faststart",
        str(out),
    ])
    _run_ffmpeg_checked(cmd, f"overlay entity patch {start:.2f}-{start + duration:.2f}s")


def _concat_segments(ffmpeg_path: str, segments: list[Path], output_video: Path) -> None:
    concat_list = output_video.parent / f"_entity_overlay_concat_{os.getpid()}.txt"
    concat_list.write_text(
        "".join(f"file '{_ffconcat_escape(p)}'\n" for p in segments),
        encoding="utf-8",
    )
    try:
        cmd = [
            ffmpeg_path, "-y", "-hide_banner", "-loglevel", "warning", "-nostdin",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_list),
            "-c", "copy",
            "-movflags", "+faststart",
            str(output_video),
        ]
        _run_ffmpeg_checked(cmd, "concat entity overlay segments")
    finally:
        try:
            concat_list.unlink()
        except Exception:
            pass


def _mux_original_audio(
    ffmpeg_path: str,
    video_only: Path,
    base_video: Path,
    output_video: Path,
) -> None:
    cmd = [
        ffmpeg_path, "-y", "-hide_banner", "-loglevel", "warning", "-nostdin",
        "-i", str(video_only),
        "-i", str(base_video),
        "-map", "0:v:0",
        "-map", "1:a?",
        "-c:v", "copy",
        "-c:a", "copy",
        "-shortest",
        "-movflags", "+faststart",
        str(output_video),
    ]
    _run_ffmpeg_checked(cmd, "mux original audio after entity overlays")


def _normalize_active_slots(
    slots: list[dict[str, Any]],
    total: float,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    card_cursor = 0.0
    for slot in sorted(slots, key=lambda s: float(s.get("start", 0.0) or 0.0)):
        start = max(0.0, float(slot.get("start", 0.0) or 0.0))
        end = min(
            total,
            max(start + 0.2, float(slot.get("end", start + 0.2) or (start + 0.2))),
        )
        if end <= card_cursor + 0.05:
            continue
        if start < card_cursor:
            start = card_cursor
        item = dict(slot)
        item["_start"] = start
        item["_end"] = end
        item["_index"] = len(normalized) + 1
        normalized.append(item)
        card_cursor = end
    return normalized


def _build_keyframe_patches(
    slots: list[dict[str, Any]],
    keyframes: list[float],
    total: float,
) -> list[dict[str, Any]]:
    if len(keyframes) <= 1:
        return [{"start": 0.0, "end": total, "slots": slots}]

    patches: list[dict[str, Any]] = []
    for slot in slots:
        start = float(slot["_start"])
        end = float(slot["_end"])
        left_idx = max(0, bisect.bisect_right(keyframes, start + 1e-6) - 1)
        right_idx = bisect.bisect_left(keyframes, end - 1e-6)
        patch_start = max(0.0, keyframes[left_idx])
        patch_end = keyframes[right_idx] if right_idx < len(keyframes) else total
        patch_end = min(total, max(end, patch_end))

        if patches and patch_start <= float(patches[-1]["end"]) + 0.05:
            patches[-1]["end"] = max(float(patches[-1]["end"]), patch_end)
            patches[-1]["slots"].append(slot)
        else:
            patches.append({"start": patch_start, "end": patch_end, "slots": [slot]})
    return patches


def _run_ffmpeg_checked(cmd: list[str], label: str) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "ffmpeg failed")[-2200:]
        raise RuntimeError(f"{label} failed: {tail}")


def _probe_duration(path: Path, ffmpeg_path: str) -> float:
    ffprobe = _ffprobe_path(ffmpeg_path)
    proc = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        return 0.0
    try:
        return float((proc.stdout or "").strip())
    except ValueError:
        return 0.0


def _probe_keyframes(path: Path, ffmpeg_path: str) -> list[float]:
    proc = subprocess.run(
        [
            _ffprobe_path(ffmpeg_path),
            "-v", "error",
            "-select_streams", "v:0",
            "-skip_frame", "nokey",
            "-show_entries", "frame=best_effort_timestamp_time",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        return [0.0]
    keyframes: list[float] = []
    for line in (proc.stdout or "").splitlines():
        try:
            value = float(line.strip().split(",", 1)[0])
        except (TypeError, ValueError):
            continue
        if value < 0:
            continue
        if not keyframes or abs(value - keyframes[-1]) > 0.001:
            keyframes.append(value)
    if not keyframes or keyframes[0] > 0.05:
        keyframes.insert(0, 0.0)
    return keyframes


def _probe_video_reorder_delay(path: Path, ffmpeg_path: str) -> float:
    """Return the keyframe PTS-DTS lead caused by B-frame reordering."""
    proc = subprocess.run(
        [
            _ffprobe_path(ffmpeg_path),
            "-v", "error",
            "-select_streams", "v:0",
            "-read_intervals", "%+#64",
            "-show_entries", "packet=pts_time,dts_time,flags",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        return 0.0
    try:
        packets = json.loads(proc.stdout or "{}").get("packets") or []
    except Exception:
        return 0.0
    delays: list[float] = []
    for packet in packets:
        if "K" not in str(packet.get("flags") or ""):
            continue
        try:
            pts = float(packet["pts_time"])
            dts = float(packet["dts_time"])
        except (KeyError, TypeError, ValueError):
            continue
        delay = pts - dts
        if 0.0 < delay < 0.5:
            delays.append(delay)
    return max(delays, default=0.0)


def _ffprobe_path(ffmpeg_path: str) -> str:
    ffprobe = str(Path(ffmpeg_path).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe"))
    return ffprobe if Path(ffprobe).is_file() else "ffprobe"


def _encoder_flags_fast(encoder_type: str, bitrate: str) -> list[str]:
    et = (encoder_type or "cpu").lower()
    if et == "amd":
        return ["-c:v", "h264_amf", "-quality", "speed", "-b:v", bitrate]
    if et == "nvidia":
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-b:v", bitrate]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]


def _ffconcat_escape(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/").replace("'", r"'\''")


def _env_truthy_local(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _load_entity_prompt_template() -> str:
    """Load the GUI-editable detector prompt, with a safe packaged fallback."""
    try:
        template = _ENTITY_PROMPT_PATH.read_text(
            encoding="utf-8-sig", errors="replace"
        ).strip()
        if template:
            return template
        print(f"[entity] detector prompt is empty; using built-in fallback")
    except OSError as exc:
        print(f"[entity] cannot read detector prompt ({_safe_text(exc)}); using fallback")
    return _DEFAULT_ENTITY_PROMPT_TEMPLATE.strip()


def _entity_task_prompt(
    transcript: list[dict[str, Any]],
    max_n: int,
    prompt_template: Optional[str] = None,
) -> str:
    """Единый промпт детекции сущностей (общий для всех backend)."""
    lines = []
    for seg in transcript:
        t = _seg_start(seg)
        text = _clean(str(seg.get("text") or ""))
        if text:
            lines.append(f"[{t:.1f}] {text}")
    transcript_block = "\n".join(lines[:1400])
    template = (
        prompt_template.strip()
        if isinstance(prompt_template, str) and prompt_template.strip()
        else _load_entity_prompt_template()
    )
    has_limit_placeholder = "{{MAX_ITEMS}}" in template
    has_transcript_placeholder = "{{TRANSCRIPT}}" in template
    task = (
        template
        .replace("{{MAX_ITEMS}}", str(max_n))
        .replace("{{TRANSCRIPT}}", transcript_block)
    )
    if not has_limit_placeholder:
        task += f"\n\nRuntime limit: return at most {max_n} entities."
    if not has_transcript_placeholder:
        task += f"\n\nTRANSCRIPT (lines are [seconds] text):\n{transcript_block}"
    return task


def _parse_entity_items(data: Any) -> list[EntityCandidate]:
    """Парсинг {"entities":[...]} → EntityCandidate (общий для всех backend)."""
    items = data.get("entities") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    out: list[EntityCandidate] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = _trim_leading_title(_clean(str(item.get("name") or "")))
        if not name or _is_bad_name(name):
            continue
        kind = _normalize_kind(str(item.get("kind") or "place"))
        try:
            start = float(item.get("start") or 0.0)
        except (TypeError, ValueError):
            start = 0.0
        try:
            score = float(item.get("score") or 0.8)
        except (TypeError, ValueError):
            score = 0.8
        query = _clean(str(item.get("query") or name))
        out.append(EntityCandidate(start=start, end=start, name=name, kind=kind, query=query, score=score))
    return out


def _detect_gpt(
    project: Path,
    transcript: list[dict[str, Any]],
    max_n: int,
    prompt_template: Optional[str] = None,
) -> list[EntityCandidate]:
    codex = Path(getattr(config, "CODEX_EXE_PATH", "") or "")
    if not codex.is_file():
        return []
    schema = _AGENT_DIR / "schemas" / "entity_overlay_control.json"
    if not schema.exists():
        return []

    task = _entity_task_prompt(transcript, max_n, prompt_template)
    task_file = project / "_codex_entity_overlay_task.md"
    result_file = project / "_codex_entity_overlay_control.json"
    task_file.write_text(task, encoding="utf-8")
    if result_file.exists():
        try:
            result_file.unlink()
        except OSError:
            pass

    # Модель из GUI (ENTITY_OVERLAY_MODEL) перекрывает режиссёрскую GPT_MODEL
    model = str((getattr(config, "ENTITY_OVERLAY_MODEL", "") or "").strip()
                or getattr(config, "GPT_MODEL", "") or "")
    cmd = [
        str(codex), "exec",
        f"Read and follow ALL instructions from this file: {task_file.as_posix()}",
        "--output-schema", str(schema),
        "--output-last-message", str(result_file),
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
        "-C", str(project),
    ]
    if model:
        cmd += ["-m", model]

    timeout = int(getattr(config, "GPT_DIRECTOR_TIMEOUT", 1200))
    try:
        subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=min(timeout, 600),
            # provider='openai' — иначе при deepseek/openrouter CODEX_HOME уедет
            # на домик Moon Bridge, чей прокси (:38440) давно не поднимается.
            env=config.codex_env_for_provider(provider='openai'),  # прокси CODEX_PROXY
        )
    except Exception:
        return []

    data = _loads_loose(result_file.read_text(encoding="utf-8", errors="replace")) if result_file.exists() else None
    return _parse_entity_items(data)


def _detect_chat(
    transcript: list[dict[str, Any]],
    max_n: int,
    provider: str,
    prompt_template: Optional[str] = None,
) -> list[EntityCandidate]:
    """deepseek/openrouter/claude_api — прямой chat/completions (json_mode), через chat_client."""
    try:
        from units import llm_gateway as chat_client
    except Exception:
        return []
    if not chat_client._api_key(provider):
        print(f"[entity] {provider}: API ключ не задан")
        return []
    # Явно выбранная в GUI модель (ENTITY_OVERLAY_MODEL) перекрывает провайдерский дефолт
    model = (getattr(config, "ENTITY_OVERLAY_MODEL", "") or "").strip()
    if not model:
        if provider == "deepseek":
            model = getattr(config, "DEEPSEEK_MODEL", "") or "deepseek-v4-flash"
        elif provider == "claude_api":
            model = getattr(config, "CLAUDE_API_MODEL", "") or "kr/claude-sonnet-4.6"
        elif provider == "codex_reseller":
            model = getattr(config, "CODEX_RESELLER_MODEL", "") or "gpt-5.6-terra"
        elif provider == "gpt_nexus":
            model = getattr(config, "GPT_NEXUS_MODEL", "") or "gpt-5.5"
        elif provider == "meta_muse":
            model = (
                getattr(config, "META_MUSE_MODEL", "")
                or "muse-spark-1.2-contributor"
            )
        else:
            model = getattr(config, "OPENROUTER_MODEL", "") or "openrouter/free"
    task = _entity_task_prompt(transcript, max_n, prompt_template)
    try:
        from units.operator_hub import ask_text, CLI_AGENTS

        print(f"[entity] оператор {provider}/{model or chr(8212)}")
        if provider in CLI_AGENTS:
            content = ask_text(provider, model, task, phase="entity_detect", timeout=300)
        else:
            content = chat_client.chat_completion(
                provider, model, [{"role": "user", "content": task}],
                reasoning_level="off", json_mode=True, timeout=300,
            )
    except Exception as exc:
        print(f"[entity] {provider} detect failed ({_safe_text(exc)})")
        return []
    return _parse_entity_items(_loads_loose(content))


def _detect_claude(
    project: Path,
    transcript: list[dict[str, Any]],
    max_n: int,
    prompt_template: Optional[str] = None,
) -> list[EntityCandidate]:
    """Claude Code (CLI, по подписке — БЕЗ API-ключа). Task-файл → CLI пишет JSON в файл."""
    try:
        from units.claudecli_runner import ClaudeCodeRunner
    except Exception:
        return []
    result_file = project / "_claude_entity_overlay_control.json"
    task_file = project / "_claude_entity_overlay_task.md"
    if result_file.exists():
        try:
            result_file.unlink()
        except OSError:
            pass
    task = _entity_task_prompt(transcript, max_n, prompt_template) + (
        "\n\n## OUTPUT HANDOFF\n"
        f"Write the final JSON object to this exact file: {result_file.as_posix()}\n"
        "Raw JSON only — do not wrap in markdown. Do not write any other file."
    )
    task_file.write_text(task, encoding="utf-8")
    try:
        # Модель из GUI (ENTITY_OVERLAY_MODEL) перекрывает режиссёрскую CLAUDE_MODEL
        runner = ClaudeCodeRunner((getattr(config, "ENTITY_OVERLAY_MODEL", "") or "").strip()
                                  or getattr(config, "CLAUDE_MODEL", "") or None)
        rc, stdout, stderr, _elapsed = runner.run_task_file(
            task_file, project, label="entity",
            extra_instruction="Write the requested JSON handoff file before you finish.",
        )
    except Exception as exc:
        print(f"[entity] claude(cli) detect failed ({_safe_text(exc)})")
        return []
    data = None
    if result_file.exists():
        data = _loads_loose(result_file.read_text(encoding="utf-8", errors="replace"))
    if not data:
        data = _loads_loose(stdout or "")
    return _parse_entity_items(data)


def _detect_regex(transcript: list[dict[str, Any]]) -> list[EntityCandidate]:
    token = r"[A-ZÀ-ÖØ-ÞĀ-ŽА-ЯЁ][A-Za-zÀ-ÖØ-öø-ÿĀ-žА-Яа-яЁё'’.-]{2,}"
    connector = r"(?:de|del|della|di|da|du|la|le|van|von|der|den|ibn|bin|al|el|де|фон|ван|ибн|аль)"
    pattern = re.compile(
        rf"\b{token}(?:\s+(?:{connector}\s+)?{token}){{0,3}}\b"
    )
    out: list[EntityCandidate] = []
    seen: set[tuple[str, int]] = set()
    for seg in transcript:
        text = str(seg.get("text") or "")
        start = _seg_start(seg)
        for match in pattern.finditer(text):
            raw_name = _clean(match.group(0).strip(" .,:;!?()[]{}\"'"))
            name = _trim_leading_title(raw_name)
            title_trimmed = raw_name != name
            if not _looks_like_entity_name(name):
                continue
            key = (_norm_name(name), int(start // 10))
            if key in seen:
                continue
            seen.add(key)
            before = text[max(0, match.start() - 60): match.start()].lower()
            after = text[match.end(): match.end() + 60].lower()
            kind = "person" if title_trimmed else _guess_kind(name, before, after)
            score = _regex_entity_score(name, kind, before, after, title_trimmed)
            if score < 0.62:
                continue
            out.append(EntityCandidate(start=start, end=start, name=name, kind=kind, query=name, score=score))
            if len(out) >= 240:
                return out
    return out


def _finalize_candidates(
    raw: list[EntityCandidate],
    total: float,
    hold: float,
    gap: float,
    max_n: int,
    montage_clips: Optional[list[dict[str, Any]]] = None,
) -> list[EntityCandidate]:
    if not raw:
        return []
    by_name: dict[str, EntityCandidate] = {}
    for cand in raw:
        cand.name = _clean(cand.name)
        cand.kind = _normalize_kind(cand.kind)
        cand.query = _clean(cand.query or cand.name)
        if not cand.name or cand.kind not in {"person", "place"}:
            continue
        if _is_bad_name(cand.name):
            continue
        if cand.start < 3.0 or (total > 0 and cand.start > total - 4.0):
            continue
        key = _norm_name(cand.name)
        old = by_name.get(key)
        if old is None or cand.score > old.score:
            by_name[key] = cand

    cands = list(by_name.values())
    cands.sort(key=lambda c: (-float(c.score or 0.0), c.start))
    kept: list[EntityCandidate] = []
    for cand in cands:
        if len(kept) >= max_n:
            break
        if any(abs(cand.start - other.start) < gap for other in kept):
            continue
        window = _fit_entity_window_to_clip(cand.start, hold, montage_clips or [])
        if window is not None:
            cand.start, cand.end = window
        else:
            cand.end = cand.start + hold
            if total > 0:
                cand.end = min(cand.end, total)
        if cand.end <= cand.start:
            cand.end = cand.start + 1.0
        kept.append(cand)
    kept.sort(key=lambda c: c.start)
    return kept


def _fit_entity_window_to_clip(
    mention_time: float,
    hold: float,
    montage_clips: list[dict[str, Any]],
) -> Optional[tuple[float, float]]:
    if not montage_clips:
        return None
    clip = _find_montage_clip(montage_clips, mention_time)
    if clip is None:
        return None
    clip_start = float(clip["start"])
    clip_end = float(clip["end"])
    clip_duration = clip_end - clip_start
    if clip_duration <= 0.2:
        return None

    duration = min(max(0.2, hold), clip_duration)
    start = min(max(mention_time, clip_start), clip_end - duration)
    end = min(clip_end, start + duration)
    return start, end


def _find_montage_clip(
    montage_clips: list[dict[str, Any]],
    timestamp: float,
) -> Optional[dict[str, Any]]:
    if not montage_clips:
        return None
    starts = [float(c["start"]) for c in montage_clips]
    idx = max(0, bisect.bisect_right(starts, timestamp + 1e-6) - 1)
    clip = montage_clips[idx]
    if float(clip["start"]) - 1e-6 <= timestamp < float(clip["end"]) - 1e-6:
        return clip
    return None


def _fit_cached_slots_to_clips(
    slots: list[dict[str, Any]],
    *,
    montage_clips: list[dict[str, Any]],
    total: float,
) -> list[dict[str, Any]]:
    """Migrate pre-clip-bound cached slots without redetecting or downloading."""
    fitted: list[dict[str, Any]] = []
    for slot in slots:
        item = dict(slot)
        start = max(0.0, float(item.get("start", 0.0) or 0.0))
        old_end = max(start + 0.2, float(item.get("end", start + 0.2) or (start + 0.2)))
        window = _fit_entity_window_to_clip(start, old_end - start, montage_clips)
        if window is not None:
            start, end = window
        else:
            end = min(total, old_end) if total > 0 else old_end
        if end <= start + 0.05:
            continue
        item["start"] = start
        item["end"] = end
        fitted.append(item)
    return fitted


def _rehydrate_cached_slots(
    slots: list[Any], *, card_dir: Path, width: int, height: int
) -> list[dict[str, Any]]:
    """Кэш плана → слоты под ТЕКУЩИЙ размер кадра.

    Детекция и скачанные картинки от размера не зависят и лежат в кэше, а вот PNG-карточка
    зависит: её геометрия считается от кадра (`max_card_w = width*0.46`, шрифт `height*0.047`),
    и накладывается она БЕЗ scale. Поэтому при смене Fast→4K карточку надо перерисовать —
    но это чистый PIL из уже скачанного файла, миллисекунды, без единого запроса в сеть.

    Слот выбрасываем только если пропала сама скачанная картинка (чистили кэш) — тогда
    сущность уйдёт на общий путь и будет докачана.
    """
    out: list[dict[str, Any]] = []
    rerendered = 0
    want_suffix = f"_{width}x{height}.png"
    for s in slots:
        if not isinstance(s, dict):
            continue
        image_path = Path(str(s.get("image_path") or ""))
        card_path = Path(str(s.get("path") or ""))
        # Годится только карточка ПОД ТЕКУЩИЙ размер. Просто «файл существует» мало:
        # в кэше лежит 1920x1080, а просят 4K — она встанет вдвое мельче задуманного.
        if card_path.is_file() and card_path.name.endswith(want_suffix):
            out.append(s)
            continue
        if not image_path.is_file():
            continue                       # исходник пропал — пусть перекачает
        # карточка под другой размер → перерисовываем из того же скачанного изображения
        stem_digest = card_path.stem
        stem_digest = re.sub(r"_\d+x\d+$", "", stem_digest) if stem_digest else _safe_stem(str(s.get("name") or "entity"))
        new_card = card_dir / f"{stem_digest}_{width}x{height}.png"
        if not new_card.is_file():
            try:
                _render_card_png(
                    image_path=image_path,
                    out_path=new_card,
                    caption=str(s.get("name") or ""),
                    kind=str(s.get("kind") or "place"),
                    width=width,
                    height=height,
                )
                rerendered += 1
            except Exception as exc:
                print(f"[entity] re-render skipped {s.get('name')}: {_safe_text(exc)}")
                continue
        s = dict(s)
        s["path"] = str(new_card)
        out.append(s)
    if rerendered:
        print(f"[entity] re-rendered {rerendered} card(s) for {width}x{height} (no downloads)")
    return out


def _resolve_and_render_slot(
    cand: EntityCandidate,
    session: requests.Session,
    raw_dir: Path,
    card_dir: Path,
    width: int,
    height: int,
    providers: list[str],
    wiki_langs: list[str],
) -> Optional[EntitySlot]:
    stem = _safe_stem(f"{cand.kind}_{cand.query or cand.name}")[:80]
    digest = hashlib.sha1(f"{cand.kind}|{cand.name}|{cand.query}".encode("utf-8")).hexdigest()[:12]
    card_path = card_dir / f"{stem}_{digest}_{width}x{height}.png"
    meta_path = raw_dir / f"{stem}_{digest}.json"
    source_url = ""
    source_title = ""
    image_path: Optional[Path] = None

    meta = _load_json(meta_path)
    if isinstance(meta, dict):
        cached_image = Path(str(meta.get("image_path") or ""))
        if cached_image.is_file():
            image_path = cached_image
            source_url = str(meta.get("source_url") or "")
            source_title = str(meta.get("source_title") or "")

    if image_path is None:
        found = _find_entity_image(
            cand,
            session=session,
            raw_dir=raw_dir,
            stem=f"{stem}_{digest}",
            providers=providers,
            wiki_langs=wiki_langs,
        )
        if not found:
            return None
        image_path, source_url, source_title = found
        meta_path.write_text(
            json.dumps(
                {
                    "name": cand.name,
                    "kind": cand.kind,
                    "query": cand.query,
                    "image_path": str(image_path),
                    "source_url": source_url,
                    "source_title": source_title,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    if not card_path.is_file():
        _render_card_png(
            image_path=image_path,
            out_path=card_path,
            caption=cand.name,
            kind=cand.kind,
            width=width,
            height=height,
        )

    return EntitySlot(
        start=cand.start,
        end=cand.end,
        name=cand.name,
        kind=cand.kind,
        query=cand.query,
        path=str(card_path),
        image_path=str(image_path),
        source_url=source_url,
        source_title=source_title,
        score=cand.score,
    )


def _find_entity_image(
    cand: EntityCandidate,
    session: requests.Session,
    raw_dir: Path,
    stem: str,
    providers: list[str],
    wiki_langs: list[str],
) -> Optional[tuple[Path, str, str]]:
    providers = providers or ["wikipedia"]
    for provider in providers:
        provider = provider.strip().lower()
        if provider in {"wiki", "wikipedia", "wikimedia"}:
            found = _wiki_find_image(cand, session=session, raw_dir=raw_dir, stem=stem, langs=wiki_langs)
            if found:
                return found
        elif provider in {"yandex", "images"}:
            found = _yandex_find_image(cand, raw_dir=raw_dir, stem=stem)
            if found:
                return found
    return None


def _wiki_find_image(
    cand: EntityCandidate,
    session: requests.Session,
    raw_dir: Path,
    stem: str,
    langs: list[str],
) -> Optional[tuple[Path, str, str]]:
    queries = [cand.query or cand.name, cand.name]
    if cand.kind == "person":
        queries.append(f"{cand.name} portrait")
    else:
        queries.append(f"{cand.name} landmark")
    tried: set[str] = set()
    for lang in langs:
        lang = re.sub(r"[^a-z-]", "", lang.lower()) or "en"
        for query in queries:
            q = _clean(query)
            if not q or (lang, q.lower()) in tried:
                continue
            tried.add((lang, q.lower()))
            for page in _wiki_search_pages(session, q, lang):
                title = str(page.get("title") or "")
                if not title:
                    continue
                page_data = _wiki_page_image(session, title, lang)
                if not page_data:
                    continue
                image_url, page_url, source_title = page_data
                downloaded = _download_image_url(session, image_url, raw_dir, stem)
                if downloaded:
                    return downloaded, page_url, source_title or title
    return None


def _wiki_search_pages(session: requests.Session, query: str, lang: str) -> list[dict[str, Any]]:
    url = f"https://{lang}.wikipedia.org/w/api.php"
    try:
        resp = session.get(
            url,
            params={
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srlimit": "5",
                "format": "json",
                "utf8": "1",
            },
            timeout=(8, 20),
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("query", {}).get("search", [])
        return items if isinstance(items, list) else []
    except Exception:
        return []


def _wiki_page_image(session: requests.Session, title: str, lang: str) -> Optional[tuple[str, str, str]]:
    url = f"https://{lang}.wikipedia.org/w/api.php"
    try:
        resp = session.get(
            url,
            params={
                "action": "query",
                "prop": "pageimages|info|pageprops",
                "titles": title,
                "pithumbsize": "1000",
                "inprop": "url",
                "format": "json",
                "utf8": "1",
            },
            timeout=(8, 20),
        )
        resp.raise_for_status()
        data = resp.json()
        pages = data.get("query", {}).get("pages", {})
        if not isinstance(pages, dict):
            return None
        for page in pages.values():
            if not isinstance(page, dict) or "missing" in page:
                continue
            if isinstance(page.get("pageprops"), dict) and "disambiguation" in page["pageprops"]:
                continue
            thumb = page.get("thumbnail") or {}
            image_url = str(thumb.get("source") or "")
            if not image_url:
                continue
            page_url = str(page.get("fullurl") or f"https://{lang}.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}")
            source_title = str(page.get("title") or title)
            return image_url, page_url, source_title
    except Exception:
        return None
    return None


def _yandex_find_image(cand: EntityCandidate, raw_dir: Path, stem: str) -> Optional[tuple[Path, str, str]]:
    try:
        from helpers.image_finder import download_images
    except Exception:
        return None
    query = cand.query or cand.name
    if cand.kind == "person" and "portrait" not in query.lower():
        query = f"{query} portrait"
    try:
        paths = download_images(query, str(raw_dir), count=1, prefix=f"{stem}_yandex")
    except Exception:
        return None
    for p in paths or []:
        path = Path(p)
        if path.is_file() and _image_ok(path):
            return path, "https://yandex.ru/images/", f"Yandex Images: {query}"
    return None


def _download_image_url(session: requests.Session, url: str, out_dir: Path, stem: str) -> Optional[Path]:
    if not url:
        return None
    max_mb = int(getattr(config, "ENTITY_OVERLAY_MAX_DOWNLOAD_MB", 12))
    max_bytes = max(1, max_mb) * 1024 * 1024
    try:
        resp = session.get(url, stream=True, timeout=(8, 35))
        resp.raise_for_status()
        content_type = (resp.headers.get("content-type") or "").lower()
        ext = ".jpg"
        if "png" in content_type:
            ext = ".png"
        elif "webp" in content_type:
            ext = ".webp"
        elif "jpeg" in content_type or "jpg" in content_type:
            ext = ".jpg"
        else:
            m = re.search(r"\.(jpe?g|png|webp)(?:\?|$)", url, re.I)
            if m:
                ext = "." + m.group(1).lower().replace("jpeg", "jpg")
        out = out_dir / f"{stem}{ext}"
        tmp = out.with_suffix(out.suffix + ".tmp")
        size = 0
        with tmp.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                size += len(chunk)
                if size > max_bytes:
                    raise RuntimeError(f"image larger than {max_mb}MB")
                fh.write(chunk)
        if size < 4096:
            tmp.unlink(missing_ok=True)
            return None
        _move_or_copy(tmp, out)
        if not _image_ok(out):
            out.unlink(missing_ok=True)
            return None
        return out
    except Exception:
        try:
            tmp.unlink(missing_ok=True)  # type: ignore[name-defined]
        except Exception:
            pass
        return None


def _render_card_png(image_path: Path, out_path: Path, caption: str, kind: str, width: int, height: int) -> None:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

    img = Image.open(image_path).convert("RGB")
    img_w, img_h = img.size
    if img_w < 64 or img_h < 64:
        raise RuntimeError("image is too small")
    aspect = img_w / max(1, img_h)

    is_place = kind != "person"
    if kind == "person":
        max_card_w = int(width * (0.37 if aspect < 1.0 else 0.46))
        max_img_h = int(height * 0.61)
        max_card_w = max(260, min(max_card_w, int(width * 0.72)))
        max_img_h = max(220, min(max_img_h, int(height * 0.70)))
        if aspect >= 1.05:
            target_w = max_card_w
            target_h = min(max_img_h, int(target_w / aspect))
        else:
            target_h = max_img_h
            target_w = min(max_card_w, int(target_h * aspect))
    else:
        # Places should read like the reference: a broad rounded scenic cut-in
        # that floats over the footage, leaving room below for subtitles/player UI.
        target_w = int(width * 0.90)
        target_h = int(height * 0.70)
    target_w = max(220, target_w)
    target_h = max(220, target_h)

    resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.BICUBIC)
    img = ImageOps.fit(img, (target_w, target_h), method=resample, centering=(0.5, 0.48 if is_place else 0.46))
    radius = max(10, int(height * (0.032 if is_place else 0.018)))
    pad = max(10, int(height * 0.012))
    caption_gap = max(10, int(height * 0.012)) if not is_place else 0
    show_caption = (not is_place) or bool(getattr(config, "ENTITY_OVERLAY_PLACE_CAPTION", False))
    font = _fit_font(
        caption,
        width=max(200, target_w + pad * 2),
        height=height,
        preferred=max(28, int(height * (0.047 if not is_place else 0.034))),
    )
    if show_caption:
        measure = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
        md = ImageDraw.Draw(measure)
        bbox = md.textbbox((0, 0), caption, font=font, stroke_width=max(1, int(height * 0.0025)))
        text_h = bbox[3] - bbox[1]
    else:
        text_h = 0
    canvas_w = target_w + pad * 2
    canvas_h = target_h + caption_gap + text_h + pad * 2
    shadow_margin = max(18, int(height * 0.018))
    canvas = Image.new("RGBA", (canvas_w + shadow_margin * 2, canvas_h + shadow_margin * 2), (0, 0, 0, 0))

    x0 = shadow_margin + pad
    y0 = shadow_margin + pad
    rounded_mask = Image.new("L", (target_w, target_h), 0)
    ImageDraw.Draw(rounded_mask).rounded_rectangle((0, 0, target_w, target_h), radius=radius, fill=255)

    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rounded_rectangle(
        (x0 - 1, y0 + 4, x0 + target_w + 1, y0 + target_h + 6),
        radius=radius + 2,
        fill=(0, 0, 0, 130),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(max(10, int(height * 0.014))))
    canvas.alpha_composite(shadow)

    canvas.paste(img.convert("RGBA"), (x0, y0), rounded_mask)
    border = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
    bd = ImageDraw.Draw(border)
    bd.rounded_rectangle(
        (1, 1, target_w - 2, target_h - 2),
        radius=max(1, radius - 1),
        outline=(255, 255, 255, 150),
        width=max(1, int(height * 0.0018)),
    )
    canvas.alpha_composite(border, (x0, y0))

    if show_caption:
        draw = ImageDraw.Draw(canvas)
        stroke = max(2, int(height * 0.0035))
        if is_place:
            text_x = x0 + max(16, int(width * 0.014))
            text_y = y0 + target_h - max(18, int(height * 0.025))
            anchor = "ld"
            fill = (255, 255, 255, 235)
        else:
            text_x = canvas.size[0] // 2
            text_y = y0 + target_h + caption_gap
            anchor = "ma"
            fill = (255, 255, 255, 255)
        draw.text(
            (text_x, text_y),
            caption,
            font=font,
            anchor=anchor,
            fill=fill,
            stroke_width=stroke,
            stroke_fill=(20, 20, 20, 220),
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path)


def _render_card_sequence(
    card_path: Path,
    out_dir: Path,
    duration: float,
    fps: int = 30,
    prefix: str = "frame",
) -> Path:
    from PIL import Image

    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob(f"{prefix}_*.png"):
        try:
            old.unlink()
        except Exception:
            pass
    card = Image.open(card_path).convert("RGBA")
    base_w, base_h = card.size
    max_scale = 1.06
    canvas_w = int(math.ceil(base_w * max_scale))
    canvas_h = int(math.ceil(base_h * max_scale))
    frame_count = max(2, int(math.ceil(max(0.2, duration) * fps)))
    resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.BICUBIC)

    for frame_idx in range(frame_count):
        t = frame_idx / float(fps)
        scale = _bounce_scale_value(t, duration)
        alpha = _fade_alpha_value(t, duration)
        scaled_w = max(2, int(round(base_w * scale)))
        scaled_h = max(2, int(round(base_h * scale)))
        frame_card = card.resize((scaled_w, scaled_h), resample=resample)
        if alpha < 0.999:
            r, g, b, a = frame_card.split()
            a = a.point(lambda v, alpha=alpha: int(v * alpha))
            frame_card = Image.merge("RGBA", (r, g, b, a))
        canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        x = (canvas_w - scaled_w) // 2
        y = (canvas_h - scaled_h) // 2
        canvas.alpha_composite(frame_card, (x, y))
        canvas.save(out_dir / f"{prefix}_{frame_idx + 1:04d}.png")
    return out_dir / f"{prefix}_%04d.png"


def render_entity_card_sequence(
    card_path: str | Path,
    work_dir: str | Path,
    duration: float,
    *,
    fps: int = 30,
    prefix: str = "entity_card",
) -> Path:
    """Render the animated PNG sequence used by per-clip retiming overlays."""
    return _render_card_sequence(
        Path(card_path), Path(work_dir), duration, fps=fps, prefix=prefix)


def _bounce_scale_value(t: float, duration: float) -> float:
    if t < 0.16:
        return 0.78 + 1.30 * t
    if t < 0.36:
        return 1.045 - 0.045 * ((t - 0.16) / 0.20)
    out_start = max(0.2, float(duration) - 0.28)
    if t > out_start:
        return max(0.88, 1.0 - 0.08 * ((t - out_start) / 0.28))
    return 1.0


def _fade_alpha_value(t: float, duration: float) -> float:
    alpha = 1.0
    if t < 0.18:
        alpha = max(0.0, t / 0.18)
    out_start = max(0.05, float(duration) - 0.25)
    if t > out_start:
        alpha = min(alpha, max(0.0, 1.0 - ((t - out_start) / 0.25)))
    return alpha


def _fit_font(caption: str, width: int, height: int, preferred: int):
    from PIL import Image, ImageDraw, ImageFont

    candidates = [
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "arialbd.ttf",
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "segoeuib.ttf",
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "arial.ttf",
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ]
    font_path = next((p for p in candidates if p.is_file()), None)
    size = max(16, preferred)
    measure = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
    draw = ImageDraw.Draw(measure)
    while size >= 16:
        try:
            font = ImageFont.truetype(str(font_path), size=size) if font_path else ImageFont.load_default()
        except Exception:
            font = ImageFont.load_default()
        bbox = draw.textbbox((0, 0), caption, font=font, stroke_width=max(1, int(height * 0.003)))
        if bbox[2] - bbox[0] <= width * 0.96:
            return font
        size -= 2
    return font


def _load_transcript(project: Path) -> list[dict[str, Any]]:
    path = project / "transcription.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return []
    if isinstance(data, dict):
        raw = data.get("transcription") or data.get("segments") or data.get("sentences") or []
    else:
        raw = data
    return raw if isinstance(raw, list) else []


def _load_montage_clips(project: Path) -> list[dict[str, Any]]:
    data = _load_json(project / "montage_plan.json")
    if not isinstance(data, dict):
        return []
    raw = data.get("clips")
    if not isinstance(raw, list):
        nested = data.get("montage_plan")
        raw = nested.get("clips") if isinstance(nested, dict) else []
    clips: list[dict[str, Any]] = []
    for clip in raw if isinstance(raw, list) else []:
        if not isinstance(clip, dict):
            continue
        start = _time_value(
            clip.get("startTime", clip.get("start", clip.get("start_time", 0.0))))
        end = _time_value(
            clip.get("endTime", clip.get("end", clip.get("end_time", 0.0))))
        if end <= start:
            continue
        clips.append({"id": clip.get("id"), "start": start, "end": end})
    clips.sort(key=lambda c: (float(c["start"]), float(c["end"])))
    return clips


def _seg_start(seg: dict[str, Any]) -> float:
    return _time_value(seg.get("start") or seg.get("startTime") or seg.get("start_time") or 0.0)


def _seg_end(seg: dict[str, Any]) -> float:
    return _time_value(seg.get("end") or seg.get("endTime") or seg.get("end_time") or 0.0)


def _time_value(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number > 10000:
        number /= 1000.0
    return number


def _transcript_duration(transcript: list[dict[str, Any]]) -> float:
    end = 0.0
    for seg in transcript:
        end = max(end, _seg_end(seg))
    return end


def _looks_like_entity_name(name: str) -> bool:
    words = name.split()
    if not words:
        return False
    normalized = _norm_name(name)
    if normalized in _BROAD_ENTITY_NAMES or normalized in _GENERIC_ENTITY_ROLES:
        return False
    if len(words) == 1 and (len(words[0]) < 5 or words[0] in _STOP_ENTITY_WORDS):
        return False
    if words[0] in _STOP_ENTITY_WORDS:
        return False
    if len(words) > 5:
        return False
    if re.search(r"\d", name):
        return False
    return True


def _is_bad_name(name: str) -> bool:
    low = _norm_name(name)
    if len(low) < 4:
        return True
    if low in _BROAD_ENTITY_NAMES:
        return True
    if low in _GENERIC_ENTITY_ROLES:
        return True
    if low in {_norm_name(w) for w in _STOP_ENTITY_WORDS}:
        return True
    bad_bits = {
        "video", "image", "scene", "audio", "prompt", "монтаж", "сцена", "видео",
        "история", "нарратор", "закадровый",
    }
    return low in bad_bits


def _trim_leading_title(name: str) -> str:
    words = name.split()
    while (
        len(words) > 1
        and words[0].strip(".").casefold() in _LEADING_ENTITY_TITLES_CASEFOLD
    ):
        words.pop(0)
    return " ".join(words)


def _guess_kind(name: str, before: str, after: str) -> str:
    person_markers = (
        "археолог", "учен", "историк", "профессор", "доктор", "царь", "король",
        "император", "президент", "режиссер", "писатель", "actor", "president",
        "king", "queen", "emperor", "archaeologist", "scientist", "professor",
    )
    place_markers = (
        "город", "место", "храм", "гора", "река", "пещер", "дерев", "долин",
        "страна", "регион", "археологический", "city", "site", "temple",
        "mount", "river", "cave", "valley", "region", "landmark",
    )
    near_before = before[-36:]
    near_after = after[:20]
    if re.search(r"(?:\bnear\b|\bin\b|\bat\b|\bfrom\b|\bto\b|возле|около|рядом|в|на|у)\s+$", near_before):
        return "place"
    if any(m in near_after for m in place_markers):
        return "place"
    if any(m in near_before for m in person_markers):
        return "person"
    ctx = f"{before} {after}"
    if any(m in ctx for m in place_markers) and not any(m in near_before for m in person_markers):
        return "place"
    if any(m in ctx for m in person_markers):
        return "person"
    if len(name.split()) >= 2:
        return "person"
    return "place"


def _regex_entity_score(name: str, kind: str, before: str, after: str, title_trimmed: bool) -> float:
    words = name.split()
    norm = _norm_name(name)
    ctx = f"{before} {after}".lower()
    near = f"{before[-50:]} {after[:50]}".lower()
    score = 0.42
    if len(words) >= 2:
        score += 0.18
    if title_trimmed:
        score += 0.22
    if "-" in name or " " in name:
        score += 0.04
    markers = _ENTITY_CONTEXT_WORDS.get(kind, ())
    if any(m in near for m in markers):
        score += 0.22
    elif any(m in ctx for m in markers):
        score += 0.12
    if len(words) == 1 and not any(m in near for m in markers):
        score -= 0.18
    if norm in _BROAD_ENTITY_NAMES:
        score -= 0.50
    if re.search(r"\b(?:modern|ancient|medieval|late|early)\b", name.lower()):
        score -= 0.18
    return max(0.0, min(1.0, score))


def _normalize_kind(kind: str) -> str:
    low = (kind or "").strip().lower()
    if low in {"person", "people", "human", "character", "личность", "человек"}:
        return "person"
    return "place"


def _parse_size(size) -> tuple[int, int]:
    """Принимает '1920x1080' ИЛИ (w, h)/[w, h]. Тюпл шлёт video_concat (реальный
    размер видео из ffprobe) — раньше он молча гасился фолбэком 1920x1080, из-за
    чего вертикальный (9:16) размер терялся и карточки рендерились горизонтально."""
    try:
        if isinstance(size, (tuple, list)) and len(size) == 2:
            return max(16, int(size[0])), max(16, int(size[1]))
        w, h = str(size).lower().split("x", 1)
        return max(16, int(w)), max(16, int(h))
    except Exception:
        return 1920, 1080


def _bounce_scale_expr(duration: float) -> str:
    out_start = max(0.2, float(duration) - 0.28)
    t = "(n/30)"
    return (
        f"if(lt({t},0.16),0.78+1.30*{t},"
        f"if(lt({t},0.36),1.045-0.045*(({t}-0.16)/0.20),"
        f"if(gt({t},{out_start:.3f}),1.0-0.08*(({t}-{out_start:.3f})/0.28),1.0)))"
    )


def _escape_filter_commas(expr: str) -> str:
    return expr.replace(",", r"\,")


def _encoder_flags(encoder_type: str, bitrate: str) -> list[str]:
    et = (encoder_type or "cpu").lower()
    if et == "amd":
        return ["-c:v", "h264_amf", "-quality", "quality", "-b:v", bitrate]
    if et == "nvidia":
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-b:v", bitrate]
    return ["-c:v", "libx264", "-preset", "slow", "-crf", "18"]


def _overlay_timeout(duration: float | None, slot_count: int) -> int:
    base = max(180, int((duration or 300) * 1.8))
    return min(max(base + slot_count * 45, 240), 7200)


def _image_ok(path: Path) -> bool:
    try:
        from PIL import Image
        with Image.open(path) as img:
            w, h = img.size
            return w >= 160 and h >= 120
    except Exception:
        return False


def _stable_hash(data: Any) -> str:
    raw = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _write_manifest(path: Path, plan_hash: str, slots: list[dict[str, Any]],
                    partial: bool = False) -> None:
    """partial=True — стадию прервали (стоп/дедлайн), карточек меньше задуманного.

    Такой манифест ВСЁ РАВНО пишем: конкат должен увидеть то, что фон успел скачать,
    иначе труд пропадёт зря. Но пометка не даёт следующему полному прогону принять
    неполный список за финальный — он дозаберёт остальное (скачанное уже в кэше).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "entity_hash": plan_hash,
                "partial": bool(partial),
                "slots": slots,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _loads_loose(raw: str) -> Any:
    raw = (raw or "").strip()
    try:
        return json.loads(raw)
    except Exception:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except Exception:
            return None
    return None


def _load_json(path: Path) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    return None


def _csv(raw: Any) -> list[str]:
    return [x.strip().lower() for x in str(raw or "").split(",") if x.strip()]


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _norm_name(text: str) -> str:
    return re.sub(r"[^0-9a-zа-яё]+", " ", text.lower()).strip()


def _safe_stem(text: str) -> str:
    text = re.sub(r"[^\w.-]+", "_", text, flags=re.UNICODE).strip("._")
    return text or "entity"


def _safe_text(exc: Any) -> str:
    return re.sub(r"\s+", " ", str(exc or "")).strip()[:300]


def _move_or_copy(src: Path, dst: Path) -> None:
    try:
        src.replace(dst)
    except Exception:
        shutil.copy2(src, dst)
        try:
            src.unlink()
        except Exception:
            pass


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Generate entity overlay cards for a project")
    ap.add_argument("project_dir")
    ap.add_argument("--size", default="1920x1080")
    ap.add_argument("--max", type=int, default=None)
    ap.add_argument("--backend", default=None)
    ap.add_argument("--hold", type=float, default=None)
    ap.add_argument("--gap", type=float, default=None)
    ap.add_argument("--force", action="store_true", help="ignore ENTITY_OVERLAY_ENABLED")
    args = ap.parse_args()
    if args.force:
        config.ENTITY_OVERLAY_ENABLED = True
    plan_and_render_entity_overlays(
        args.project_dir,
        size=args.size,
        max_overlays=args.max,
        backend=args.backend,
        hold_sec=args.hold,
        min_gap_sec=args.gap,
        no_cache=args.force,
    )
