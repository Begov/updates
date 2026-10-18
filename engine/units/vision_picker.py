"""Общий вижн-отбор кандидатов для стадий-источников (сток, архивы real_photo).

Одна реализация на всех (решение 25.08): собрать контактный лист из
кандидатов (видео — три кадра: начало/середина/конец, фото — один кадр),
отдать вижну лист + описание сцены + запреты + запрос, разобрать ответ
{pick, rejected_ids, reasons}. «Никто не подходит» → pick пуст, все в
rejected — вызывающая стадия отдаёт сцену другому источнику.

Модуль не качает файлы и не режет — это делает стадия; он только смотрит.
Артефакты в work_dir: contact_sheet.jpg, frames/, vision_prompt.json,
vision_response.txt, selection.json (или vision_error.json).
"""
from __future__ import annotations

import os
import subprocess
import shutil
import re
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Потолок размера контактного листа (на весь лист, не на кандидата).
_SHEET_MAX_BYTES = 1_200_000


@dataclass
class Candidate:
    candidate_id: str          # C1, C2, …
    path: Path                 # скачанный файл (видео или картинка)
    kind: str = "photo"        # video | photo
    duration: float = 0.0      # для видео — чтобы взять кадры по времени
    title: str = ""
    meta: dict = field(default_factory=dict)


@dataclass
class VisionVerdict:
    pick: Optional[str]                 # candidate_id или None
    rejected_ids: list[str]
    reasons: dict[str, str]
    # Порядок ГОДНЫХ кандидатов, лучший первым (режим ранжирования). Пусто —
    # вижн отвечал по старой схеме «один победитель».
    ranked_ids: list[str] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    error: str = ""                     # непусто — вижн не отработал (деградация на стороне стадии)


def _panel_font(size: int = 15):
    """Читаемый шрифт для подписи панели; дефолтный bitmap — фолбэк."""
    from PIL import ImageFont

    for name in ("arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except (OSError, ImportError):
            continue                    # шрифта нет в системе — пробуем следующий
    try:
        return ImageFont.load_default()
    except Exception:  # noqa: BLE001
        return None


def _parse_json_object(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.I | re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:   # ответ не JSON — пробуем следующий разбор
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            value = json.loads(text[start:end + 1])
        except json.JSONDecodeError:   # ответ не JSON и во второй форме — вернём пусто
            return {}
    return value if isinstance(value, dict) else {}


def _extract_preview_frame(
    video_path: Path,
    timestamp: float,
    output_path: Path,
) -> Optional[Path]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    flags = {}
    if os.name == "nt":
        flags["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{max(0.0, float(timestamp)):.3f}",
                "-i",
                str(video_path),
                "-frames:v",
                "1",
                "-vf",
                "scale=480:-2",
                "-q:v",
                "5",
                "-y",
                str(output_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=60,
            **flags,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logging.warning("  [вижн] кадр превью не снят (%s): %s", output_path.name, exc)
        return None
    if result.returncode != 0 or not output_path.exists():
        logging.warning("  [вижн] кадр превью пуст (%s), ffmpeg rc=%s",
                        output_path.name, result.returncode)
        return None
    return output_path



def build_contact_sheet(candidates: list[Candidate], output_path: Path) -> Optional[Path]:
    """Один лист на всех: видео — 3 кадра в ряд, фото — один.

    Подпись панели — id И название кандидата (страница источника или имя
    файла). Раньше на панели был только `C1`, а названия шли отдельным списком
    в тексте запроса, и вижн должен был сам сопоставлять картинку со строкой по
    порядку — лишнее звено, где ошибка выглядела бы как обычный вердикт
    (решение пользователя 25.08)."""
    from PIL import Image, ImageDraw

    panels = []
    frame_dir = output_path.parent / "frames"
    # Размер кадра в листе — от числа кандидатов: на 3-4 штуки можно позволить
    # крупные панели (раньше EPF слал 640 px на кадр — лист не должен быть хуже),
    # на дюжину приходится ужиматься, иначе лист не влезет в лимит.
    count = max(1, len(candidates))
    panel_side = 900 if count <= 4 else (720 if count <= 9 else 560)
    # Почему кандидат не попал в лист — обязано быть в логе. Молчание здесь
    # оборачивалось загадкой «вижн не отработал»: у клипа 15 прогона 26.08 оба
    # кандидата не открылись, лист не собрался, и сцена молча ушла в слепой
    # фолбэк, откуда битый файл попал прямо в кадр.
    skipped: dict[str, int] = {}

    def _skip(reason: str, cand_id: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1
        logger.info("   [вижн] кандидат %s не попал в лист: %s", cand_id, reason)

    for cand in candidates:
        if not cand.path:
            _skip("нет файла", cand.candidate_id)
            continue
        if not Path(cand.path).exists():
            _skip("файл не найден на диске", cand.candidate_id)
            continue
        images = []
        if cand.kind == "video":
            duration = max(0.1, float(cand.duration or 0) or 3.0)
            # «Конец» — с запасом: сик в последние 50 мс часто не даёт кадра
            # (контейнер кончается раньше заявленной длительности) — smoke 25.08.
            end_ts = max(0.0, min(duration - 0.5, duration * 0.9))
            for label, ts in (("start", 0.0), ("mid", duration / 2), ("end", end_ts)):
                frame = _extract_preview_frame(Path(cand.path), ts, frame_dir / f"{cand.candidate_id}_{label}.jpg")
                if frame:
                    try:
                        images.append(Image.open(frame).convert("RGB"))
                    except (OSError, ValueError) as exc:
                        logger.warning("   [вижн] кадр %s не открылся (%s) — панель без него",
                                       frame.name, exc)
        else:
            try:
                image = Image.open(cand.path).convert("RGB")
                image.thumbnail((panel_side, panel_side))
                images.append(image)
            except (OSError, ValueError) as exc:
                # Не открылась как изображение: почти всегда это HTML-страница,
                # сохранённая под именем .jpg (защита от хотлинка, редирект).
                logger.info("   [вижн] кандидат %s не открылся: %s",
                            cand.candidate_id, exc)
        if not images:
            _skip("не удалось получить ни одного кадра", cand.candidate_id)
            continue
        width = max(im.width for im in images)
        height = max(im.height for im in images)
        caption_height = 26
        panel = Image.new("RGB", (width * len(images), height + caption_height), "white")
        draw = ImageDraw.Draw(panel)
        font = _panel_font()
        caption = cand.candidate_id
        title = " ".join(str(cand.title or "").split())
        if title:
            # Обрезаем по ширине панели, чтобы подпись не уезжала за край
            limit = max(12, int((panel.width - 16) / 8.2) - len(caption) - 3)
            caption = f"{caption} · {title[:limit]}" + ("…" if len(title) > limit else "")
        draw.text((8, 6), caption, fill="black", font=font)
        for index, im in enumerate(images):
            panel.paste(im, (index * width, caption_height))
        panels.append(panel)
    if not panels:
        details = ", ".join(f"{reason}: {n}" for reason, n in skipped.items()) or "причина не установлена"
        logger.warning(
            "   [вижн] лист не собран: ни один из %d кандидатов не дал кадра (%s)",
            len(candidates), details)
        return None
    if skipped:
        logger.info("   [вижн] в лист попало %d из %d кандидатов (%s)",
                    len(panels), len(candidates),
                    ", ".join(f"{reason}: {n}" for reason, n in skipped.items()))
    # Колонок — по числу кандидатов: 12 штук в две колонки дают «простыню»
    # высотой в шесть рядов, на которой вижн теряет нижние кадры.
    columns = 2 if len(panels) <= 4 else (3 if len(panels) <= 9 else 4)
    rows = (len(panels) + columns - 1) // columns
    pw = max(p.width for p in panels)
    ph = max(p.height for p in panels)
    sheet = Image.new("RGB", (pw * columns, ph * rows), "#d8d8d8")
    for index, panel in enumerate(panels):
        sheet.paste(panel, ((index % columns) * pw, (index // columns) * ph))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Предел — на ВЕСЬ лист (а не на каждую картинку, как было в EPF: там бюджет
    # делился на число кандидатов и при 12 штуках падал до 43 КБ и 448 px — на
    # таком мыле вижн не отличает деталей). Сначала жмём качеством, и только
    # если не хватило — уменьшаем лист целиком.
    for quality in (82, 72, 62):
        sheet.save(output_path, format="JPEG", quality=quality, optimize=True)
        if output_path.stat().st_size <= _SHEET_MAX_BYTES:
            return output_path
    scaled = sheet.copy()
    scaled.thumbnail((sheet.width // 2, sheet.height // 2))
    scaled.save(output_path, format="JPEG", quality=70, optimize=True)
    return output_path


# Кадр мельче этого по длинной стороне на 1080p — каша: Ken Burns растянет
# его на весь экран. Порог тот же, что у Wikimedia (`_COMMONS_MIN_WIDTH`), но
# теперь по ФАКТУ скачанного файла, а не по метаданным источника.
MIN_FRAME_SIDE = 640


def normalize_reasons(value) -> dict[str, str]:
    """Причины вижна в единый вид {candidate_id: текст}.

    chat-провайдер отдаёт объект, CLI-агент по схеме — список пар: у structured
    output нет свободных ключей объекта."""
    if isinstance(value, list):
        reasons = {str(item.get("candidate_id", "")).strip(): str(item.get("reason", ""))
                   for item in value if isinstance(item, dict)}
        reasons.pop("", None)
        return reasons
    if isinstance(value, dict):
        return {str(key): str(text) for key, text in value.items()}
    return {}


def resolve_vision_provider(stage: str) -> tuple[str, str]:
    """(провайдер, модель) вижна стоковой стадии: Codex с STOCK_VISION_MODEL или моделью режиссёра.

    Провайдер задаётся в GUI (STOCK_VISION_PROVIDER); пусто/«auto» — вижн выключен.
    """
    import params as config
    from units.operator_hub import _model_for

    stage = (stage or "").split("/")[0].strip().lower()
    provider = str(getattr(config, "STOCK_VISION_PROVIDER", "") or "").strip().lower()
    if stage != "stock" or provider in ("", "auto"):
        return "", ""
    model = str(getattr(config, "STOCK_VISION_MODEL", "") or "").strip()
    return "codex", model or _model_for()


def _ask_vision(provider: str, model: str, task: str, sheet: Path,
                work_dir: Optional[Path] = None,
                schema_file: str = "vision_pick.json") -> str:
    """Один вопрос с картинкой: контактный лист уходит в `codex exec -i`, ответ пишется файлом по схеме."""
    from units.codex_runner import CodexRunner

    work = Path(work_dir or Path(sheet).parent)
    work.mkdir(parents=True, exist_ok=True)
    result_file = work / "vision_cli_result.json"
    result_file.unlink(missing_ok=True)
    runner = CodexRunner(model_name=model or None, timeout=300)
    # Схема отдаёт reasons списком пар: у structured output нет произвольных
    # ключей объекта, а вердикт нужен по КАЖДОМУ кандидату. Схема зависит от
    # задачи: выбрать один кадр (vision_pick) или отранжировать все (vision_rank).
    runner.run(task, schema_file, result_file, work, images=[Path(sheet)])
    return result_file.read_text(encoding="utf-8-sig", errors="replace")



# Постерный режим: видео-кандидат показан ОДНИМ кадром (обложкой карточки).
_SINGLE_FRAME_NOTE = (
    "Each video candidate here is a SINGLE still frame taken from the clip: the "
    "motion itself is not visible to you. Never reject a video because the pose "
    "is wrong, because the subject is standing still, or because the action is "
    "not happening in this frame — judge by the subject, the place and the "
    "setting, and take the action from the candidate's title.\n\n"
)

_COMMON_RULES = (
    "Judge each candidate by whether it WORKS for this scene, not by whether "
    "every detail of the brief is visible. The brief describes an idea; a "
    "frame that carries that idea is a keeper even when it lacks incidental "
    "details — aisles, a specific number of people, an exact camera angle, a "
    "prop mentioned in passing. Reject only on a REAL mismatch: the wrong "
    "place (an open-air field where the scene needs a hall), the wrong period, "
    "the wrong person or country, or material that is broken (see below). "
    # Прогон 28.08: сток забраковал 7 клипов подряд из 9 — «Gentoo penguins,
    # not a chinstrap colony», «African penguin, not an emperor penguin»,
    # «several penguins together, contradicting the requirement», «side
    # view, not the required rear». Кадры были правильные: пингвины во
    # льдах. Брифы пишет промптер под ГЕНЕРАТОР и называет точный подвид,
    # число и ракурс — реальный материал таких совпадений не даёт, а
    # зритель разницы в двухсекундной сцене не видит.
    "The VARIETY is not a mismatch either. A brief names a precise kind — an "
    "emperor penguin, a Border Collie, a 1950s sedan, a Baroque church — "
    "because it was written for an image generator, not because the film "
    "needs that exact variety. Any member of the same broad kind carries the "
    "scene: another penguin, another dog, another car of roughly that era. "
    "Reject on the wrong KIND (a duck where the brief needs a penguin, a bus "
    "where it needs a car), never on the wrong species, breed, model or "
    "style within the right kind. Named real people and specific real places "
    "are the exception — there the identity from the caption still rules. "
    "HOW MANY and FROM WHERE are not mismatches: one animal where the brief "
    "has a group, a group where it has one, a side view where it asks for a "
    "rear view, closer or wider framing — every one of these still shows "
    "this scene. "
    "The MOMENT is not a mismatch: the same subject caught a minute earlier or "
    "later still shows this scene. A vehicle driving rather than standing "
    "where the brief has it parked, a worker walking up to the machine rather "
    "than already at it, a process one step further along — all of these are "
    "the right frame. Reject on the wrong THING or the wrong KIND of place, "
    "never on the wrong instant of the same event. Nor is a brief's staging a "
    "requirement: it was written for an image generator, so demands like "
    "unmarked equipment, blank packaging or a person posed just so describe a "
    "frame nobody photographed. Real material carries brand names, signage and "
    "lettering on the things themselves — that is life, not a defect, and it "
    "is unrelated to the stock-agency watermarks disqualified above. "
    "Never reject a fitting frame because a minor element of the description "
    "is missing — a rejection costs a whole new search, and the scene may end "
    "up with nothing at all. When the PLACE and the PERIOD are right, the "
    "frame works even if the people or objects the brief mentions are absent: "
    "an archival shot of the right station, hall or street carries the scene. "
    "A missing named person lowers a candidate's rank; it does not reject it. "
    "Choose the single candidate that best illustrates the scene brief. The "
    "contact sheet shows every candidate captioned with its id and the title "
    "of its source page or file; video candidates show three frames (start, "
    "middle, end), photos one frame. The visible content decides whether a "
    "frame fits the brief and the must_not_appear list. The caption carries "
    "the facts the pixels cannot give you — WHO this person is, WHERE and "
    "WHEN it was taken. When the caption names a different person, country "
    "or era than the brief requires, reject that candidate even if the frame "
    "looks perfect: a singer with a microphone captioned 'Madonna' is not the "
    "Polish artist of the brief. A caption that carries no such fact (a file "
    "name assembled from keywords and a code, an auto-generated string) simply "
    "tells you nothing — ignore it, and never let a caption justify a frame "
    "whose visible content does not fit. Reject what is broken as material: "
    "logos, thumbnails, screenshots, previews, icons, AI art. A map, chart, "
    "diagram, flag, poster, magazine page or drawing is LEGITIMATE material, "
    "not a defect: keep it when it conveys what this scene is about — a "
    "border, a route, a place, a period, a scale, a printed source — even if "
    "the brief describes live action the frame does not literally show. "
    "Documentaries illustrate a border with a map all the time. Reject such a "
    "frame only when it says nothing about this scene. When NO "
    "candidate matches the brief, set pick to null and put ALL ids into "
    "rejected_ids — the scene will be covered by another source; never keep a "
    "weak match to fill the slot. reasons: one short visual reason for EVERY "
    "candidate, the picked one included. Return strict JSON only.\n\n"
)


# Режим ранжирования. Перекрывает правило «всех непобедивших в rejected_ids»:
# сцене нужен не только лучший кадр, но и ЗАПАС на случай, когда победителя
# успела занять соседняя сцена (клипы обрабатываются параллельно). Улов 01.09:
# кандидат «наполнение форм у чана, но вид менее чёткий, чем у победителя» лежал
# в браке рядом с «рукой, соскабливающей плесень», победителя занял сосед — и
# сцена ушла из стока, хотя годный кадр был в листе.
_RANK_RULE = (
    "\n\nRANKING MODE. Do not choose a single winner. Put EVERY candidate that "
    "could carry this scene into ranked_candidate_ids, BEST FIRST — the scene "
    "may end up using the second or third one, because a neighbouring scene "
    "can take the leader first. rejected_ids is ONLY for footage that does not "
    "fit this scene AT ALL — wrong subject, wrong process, wrong place. A "
    "candidate that fits but shows the subject less clearly is NOT rejected: "
    "it goes lower in the ranking. If nothing fits, leave ranked_candidate_ids "
    "empty and list every id in rejected_ids.\n\n"
)


# ── Свои запреты пользователя ───────────────────────────────────────────────
#
# Человек задаёт СВОИ правила отбора готовых кадров, не залезая в код: файл
# `agent/prompts/vision_house_rules.txt`, вкладка Prompts. Пример из жизни —
# канал для мусульманской аудитории, где в кадре не должно быть женщин с
# непокрытой головой, алкоголя, сигарет.
#
# Почему НЕ через поле договора `must_not_appear`: у него ТРИ разных
# наполнителя (сток берёт `must_not_reveal`/`avoid`, архивы —
# `hidden_entities`, а при визуальной истории запреты уже внутри текста
# брифа и списком не шлются), и он привязан к СЦЕНЕ, а не к проекту. Правила
# пользователя молча не доезжали бы в части прогонов.
_HOUSE_RULES_FILE = "vision_house_rules.txt"

# Секции по стадиям: пусто/`[all]` — на все стадии, остальные — только на свою.
_HOUSE_SECTION_STAGES = {"stock": "stock"}


def _house_rules_path() -> Path:
    """agent/prompts/vision_house_rules.txt рядом с остальными промптами."""
    return Path(__file__).resolve().parents[1] / "instructions" / _HOUSE_RULES_FILE


def load_house_rules(stage: str = "", path: Optional[Path] = None) -> list[str]:
    """Строки запретов пользователя для этой стадии. Нет файла — пустой список.

    Формат простой: по одному предмету на строку, строки с `#` — комментарии.
    Необязательные секции `[all]`, `[stock]`;
    строки до первой секции считаются общими.
    """
    src = Path(path) if path is not None else _house_rules_path()
    try:
        raw = src.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    stage_key = (stage or "").split("/")[0].strip().lower()
    current = "all"
    picked: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip().lower()
            continue
        # Секция «все» действует всегда; именная — только на своей стадии.
        if current in ("all", ""):
            picked.append(line)
        elif current in _HOUSE_SECTION_STAGES and current == stage_key:
            picked.append(line)
    # Повторы убираем, порядок человека сохраняем.
    seen: set[str] = set()
    return [x for x in picked if not (x.lower() in seen or seen.add(x.lower()))]


def build_house_rule(items: list[str]) -> str:
    """Обёртка вокруг списка пользователя. Пустой список — пустая строка.

    Формулировку пишем МЫ, а не человек: вес правила задают именно слова
    «дисквалификатор», «независимо от того, насколько кадр подходит»,
    «перекрывает все послабления ниже». Отданная человеку, она превратилась бы
    в «постарайся не выбирать» и проиграла бы соседним абзацам о
    снисходительности, которых в задании целый десяток.
    """
    if not items:
        return ""
    listed = "; ".join(items)
    return (
        "DISQUALIFIER set by the user for this channel, check it independently "
        "of how well the frame fits the scene: a candidate showing ANY of the "
        "following is UNUSABLE and goes to rejected_ids even when it matches "
        "the brief perfectly. This overrides every leniency rule below — never "
        "keep such a frame because nothing better is available, and never argue "
        "that the element is small, blurred or incidental. When in doubt about "
        "whether the element is present, reject. Forbidden content: "
        f"{listed}.\n\n"
    )


def build_task(contract_json: str, *, stage: str = "", single_frame: bool = False,
               rank: bool = False, house_rules: Optional[list[str]] = None) -> str:
    """Текст задания вижну. Отдельной функцией — чтобы правила можно было
    проверить тестом напрямую, а не регуляркой по исходнику.

    Порядок блоков подчинён одному правилу: ПЕРВЫМ идёт то, что действительно
    бракует, — запрет пользователя; он оказывается выше `_COMMON_RULES` с их
    послаблениями.
    """
    if house_rules is None:
        house_rules = load_house_rules(stage)
    house_block = build_house_rule(house_rules)

    parts: list[str] = []
    if house_block:
        parts.append(house_block)
    if single_frame:
        parts.append(_SINGLE_FRAME_NOTE)
    parts.append(_COMMON_RULES)
    if rank:
        parts.append(_RANK_RULE)
    parts.append(contract_json)
    return "".join(parts)


def pick_with_vision(
    candidates: list[Candidate],
    *,
    work_dir: Path,
    provider: str,
    model: str,
    scene_brief: str,
    forbidden: list[str],
    search_query: str = "",
    media_kind: str = "photo",
    stage: str = "",
    rank: bool = False,
) -> VisionVerdict:
    """Лист → вижн → вердикт. Ошибки не бросает: возвращает verdict.error.

    `rank=True` — вижн не просто называет победителя, а ВЫСТРАИВАЕТ годных по
    порядку и бракует только то, что сцене не подходит вовсе (схема
    `vision_rank.json`, так с самого начала работает ютуб-стадия).

    Зачем это стоку (улов живого прогона 01.09, клип 6). При одном победителе
    всё непобедившее уходило в `rejected_ids` — модель рассуждала «не выбран,
    значит отклонён». В том прогоне C5 «наполнение форм у чана, но вид менее
    чёткий, чем у C1» лежал в браке рядом с «рукой, соскабливающей плесень с
    испорченного сыра». Победителя C1 за 17 секунд до нас занял соседний клип,
    запасных не осталось — и сцена ушла из стока, хотя годный кадр был. С
    ранжированием запасным становится ВТОРОЕ МЕСТО по оценке вижна, а не
    случайный уцелевший кандидат.
    """
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    empty = VisionVerdict(pick=None, rejected_ids=[], reasons={}, provider=provider, model=model)
    # Модель может быть пустой: Codex возьмёт модель режиссёра.
    if not provider:
        empty.error = "vision provider/model is not configured"
        return empty
    sheet = build_contact_sheet(candidates, work_dir / "contact_sheet.jpg")
    if not sheet:
        empty.error = "contact sheet could not be built"
        return empty

    contract = {
        "stage": stage,
        "scene_brief": scene_brief,
        "must_not_appear": [str(x).replace("_", " ") for x in (forbidden or [])][:8],
        "search_query": search_query,
        "media_type": media_kind,
        "candidates": [{"candidate_id": c.candidate_id, "title": (c.title or "")[:80]} for c in candidates],
        "response_schema": {
            "ranked_candidate_ids": ["C1", "C2"],
            "rejected_ids": ["C3"],
            "reasons": {"C1": "why it fits the scene brief best",
                        "C3": "why it does not fit this scene at all"},
        } if rank else {
            "pick": "C1 or null",
            "rejected_ids": ["C2"],
            "reasons": {"C1": "why it fits the scene brief (kept)", "C2": "what is wrong (rejected)"},
        },
    }
    # Постерный режим: панель видео-кандидата несёт ОДИН кадр (обложку
    # карточки), а не три кадра с ролика. Вижн об этом знать не может —
    # говорим прямо, иначе он бракует статичный кадр за «нет действия».
    single_frame = (media_kind == "video"
                    and not any(c.kind == "video" for c in candidates))
    # Запреты пользователя читаем ОДИН раз и кладём рядом с договором: иначе
    # при разборе прогона не видно, действовали они вообще или файл был пуст.
    house_rules = load_house_rules(stage)
    task = build_task(json.dumps(contract, ensure_ascii=False),
                      stage=stage, single_frame=single_frame, rank=rank,
                      house_rules=house_rules)
    if house_rules:
        contract["house_rules_applied"] = house_rules
    (work_dir / "vision_prompt.json").write_text(json.dumps(contract, ensure_ascii=False, indent=2), encoding="utf-8")
    (work_dir / "vision_task.txt").write_text(task, encoding="utf-8")
    logger.info(
        "   [вижн/%s] %s/%s ← лист %s КБ, кандидатов %s%s%s",
        stage or "?", provider, model or "—",
        sheet.stat().st_size // 1024, len(candidates),
        f", запрос «{search_query[:40]}»" if search_query else "",
        f", свои запреты: {len(house_rules)}" if house_rules else "")
    try:
        raw = _ask_vision(provider, model, task, sheet, work_dir=work_dir,
                          schema_file="vision_rank.json" if rank else "vision_pick.json")
        (work_dir / "vision_response.txt").write_text(str(raw or ""), encoding="utf-8")
        parsed = _parse_json_object(raw)
    except Exception as exc:  # noqa: BLE001
        (work_dir / "vision_error.json").write_text(json.dumps({"error": str(exc)}, ensure_ascii=False), encoding="utf-8")
        # Раньше ошибка вижна оседала только в файле, и в логе прогона стадия
        # выглядела здоровой, хотя брала первого попавшегося (улов 25.08).
        logger.warning("   [вижн/%s] %s/%s не ответил: %s — беру первого кандидата",
                       stage or "?", provider, model, str(exc)[:160])
        empty.error = str(exc)
        return empty

    known = {c.candidate_id for c in candidates}
    rejected = sorted({str(x).strip() for x in (parsed.get("rejected_ids") or []) if str(x).strip() in known})
    ranked_ids: list[str] = []
    if rank:
        # Порядок годных, лучший первым. Забракованных из него убираем: модель
        # иногда перечисляет в ранге ВСЕХ, включая тех, кого сама же отвергла.
        seen: set = set()
        for value in (parsed.get("ranked_candidate_ids") or []):
            value = str(value).strip()
            if value in known and value not in rejected and value not in seen:
                seen.add(value)
                ranked_ids.append(value)
        # Годный, не попавший в ранг, всё равно остаётся запасом — в конце, в
        # порядке листа: он не забракован, значит сцену закрыть может.
        ranked_ids += [c.candidate_id for c in candidates
                       if c.candidate_id not in seen and c.candidate_id not in rejected]
        pick = ranked_ids[0] if ranked_ids else ""
    else:
        pick = parsed.get("pick")
        pick = str(pick).strip() if pick else ""
        if pick not in known or pick in rejected:
            pick = ""
    reasons = normalize_reasons(parsed.get("reasons"))
    verdict = VisionVerdict(pick=pick or None, rejected_ids=rejected, reasons=reasons,
                            ranked_ids=ranked_ids, provider=provider, model=model)
    (work_dir / "selection.json").write_text(json.dumps({
        "provider": provider, "model": model, "pick": verdict.pick, "rejected_ids": rejected,
        "ranked_ids": ranked_ids, "reasons": verdict.reasons,
        "candidates": [{"candidate_id": c.candidate_id, "title": c.title, **c.meta} for c in candidates],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    if verdict.pick is None:
        logger.info("   [вижн/%s] забракованы все %d: %s", stage or "?", len(candidates),
                    "; ".join(f"{cid} — {str(verdict.reasons.get(cid, ''))[:60]}"
                              for cid in rejected[:3]) or "ответ без выбора")
    else:
        won = next((c for c in candidates if c.candidate_id == verdict.pick), None)
        logger.info("   [вижн/%s] выбран %s «%s» — %s", stage or "?", verdict.pick,
                    (won.title if won else "")[:40],
                    str(verdict.reasons.get(verdict.pick, ""))[:70])
    # ПОЛНЫЙ разбор — в лог стадии, по строке на кандидата: что за кадр, какое
    # место в ранге и чем именно он не понравился. Раньше в общий лог уходили
    # три первые причины, обрезанные до шестидесяти знаков, и «что не устроило
    # вижн» приходилось искать в `selection.json` руками (замечание
    # пользователя 01.09). Уровень DEBUG: в консоль это не льётся, а в файле
    # стадии сохраняется целиком.
    place = {cid: index + 1 for index, cid in enumerate(ranked_ids)}
    for c in candidates:
        mark = ("брак" if c.candidate_id in rejected
                else f"место {place.get(c.candidate_id, '—')}")
        logger.debug("   [вижн/%s] %s [%s] «%s» — %s", stage or "?", mark,
                     c.candidate_id, (c.title or "")[:70],
                     str(verdict.reasons.get(c.candidate_id, "без объяснения"))[:200])
    return verdict
