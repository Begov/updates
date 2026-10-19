"""Справочники моделей и провайдеров, которые показывает интерфейс.

В env движку уходят стабильные идентификаторы (слева), человеку показываются
подписи (справа). Функции *_to_id переводят подпись обратно в идентификатор.
"""
from __future__ import annotations

from . import codex_models
from .i18n import T

# ---------------------------------------------------- провайдеры картинок
# Один набор на все блоки, где рисуются картинки («Генерация картинок», «Инфографика»):
# наборы провайдеров и моделей не должны расходиться между блоками.
IMAGE_PROVIDERS = [
    ("veononstop", "VeoNonStop"),
    ("vertex", "Vertex"),
    ("cheapvibe", "CheapVibeCode"),
    ("varag", "VARAG"),
    ("cinai", "CinAI"),
]

# ------------------------------------------------------ VeoNonStop Banana
BANANA_MODELS = [
    ("GEM_PIX_2", "Banana Pro"),
    ("NARWHAL", "Banana 2"),
    ("HARBOR_SEAL", "Banana 2 Lite"),
]

# --------------------------------------------------------- Vertex AI
# Подписи короткие — они на кнопках; подробности — в подсказке поля.
# Набор и названия те же, что у VeoNonStop и CinAI. Preview-адреса Google отключил (июль 2026).
VERTEX_IMAGE_MODELS = [
    ("gemini-3-pro-image", "Banana Pro"),
    ("gemini-3.1-flash-image", "Banana 2"),
    ("gemini-3.1-flash-lite-image", "Banana 2 Lite"),
]
VERTEX_IMAGE_DEFAULT = "gemini-3.1-flash-lite-image"
# прежние модели Vertex -> актуальные (2.5 Flash Image Google переводит на 3.1 Flash-Lite Image)
VERTEX_IMAGE_RENAMED = {
    "gemini-2.5-flash-image": "gemini-3.1-flash-lite-image",
    "gemini-3.1-flash-image-preview": "gemini-3.1-flash-image",
    "gemini-3-pro-image-preview": "gemini-3-pro-image",
}
VERTEX_VIDEO_MODEL = "veo-3.1-lite-generate-001"        # видео в Vertex — только Veo 3.1 Lite

# ------------------------------------------------------ провайдеры видео
# Свои клиенты на маршруте VeoNonStop (BEGO_VIDEO_PROVIDER): у них нет ключей-слотов,
# капчи и апскейла VeoNonStop, а число потоков — своё поле.
VIDEO_PROVIDERS = [("veononstop", "VeoNonStop"), ("vertex", "Vertex"), ("royaltechno", "RoyalTechno"),
                   ("varag", "VARAG"), ("cinai", "CinAI")]
OWN_VIDEO_PROVIDERS = ("vertex", "royaltechno", "varag")
# RoyalTechno: модель veo-3.1 в 720p — тариф «Veo 3.1», $0.02 за клип 8 с (1080p — $0.05)
ROYALTECHNO_VIDEO_MODEL = "veo-3.1"
VERTEX_RESOLUTIONS = [("", "720p"), ("1080p", "1080p")]

# ------------------------------------------------- CheapVibeCode (картинки по API)
CHEAPVIBE_MODELS = [
    ("gpt-image-2", "GPT Image 2"),
    ("grok-imagine-image", "Grok Imagine"),
    ("nano-banana-2", "Banana 2"),
]
CHEAPVIBE_SIZES = [
    ("1536x1024", "1536×1024"),
    ("1024x1536", "1024×1536"),
]
CHEAPVIBE_QUALITIES = ("low", "medium", "high")
# Размеры под формат кадра ролика (у gpt-image-2 нет 16:9 — ближайший 3:2, склейка подгонит)
CHEAPVIBE_SIZES_BY_FORMAT = {
    "16:9": ["1536x1024"],
    "9:16": ["1024x1536"],
}


def cheapvibe_sizes(frame_format: str) -> list:
    """Размеры CheapVibeCode, подходящие формату кадра ролика."""
    allowed = CHEAPVIBE_SIZES_BY_FORMAT.get(frame_format) or CHEAPVIBE_SIZES_BY_FORMAT["16:9"]
    return [(s, label) for s, label in CHEAPVIBE_SIZES if s in allowed]


def cheapvibe_size(frame_format: str, size: str) -> str:
    """Размер, совместимый с форматом: выбранный, если подходит, иначе первый подходящий."""
    allowed = [s for s, _ in cheapvibe_sizes(frame_format)]
    return size if size in allowed else allowed[0]
# Фиксированное списание за одно изображение по модели и качеству (тарифы провайдера).
# Какие качества у модели есть — ровно ключи её словаря (у Banana 2 только low).
CHEAPVIBE_COST = {
    "gpt-image-2": {"low": "50K", "medium": "100K", "high": "150K"},
    "grok-imagine-image": {"low": "100K", "medium": "250K", "high": "350K"},
    "nano-banana-2": {"low": "100K"},
}


def cheapvibe_qualities(model: str) -> tuple:
    """Качества, доступные модели CheapVibeCode."""
    cost = CHEAPVIBE_COST.get(model) or CHEAPVIBE_COST["gpt-image-2"]
    return tuple(q for q in CHEAPVIBE_QUALITIES if q in cost)


def cheapvibe_quality(model: str, quality: str) -> str:
    """Качество, которое реально уйдёт в запрос: недоступное модели сводим к первому доступному."""
    allowed = cheapvibe_qualities(model)
    return quality if quality in allowed else allowed[0]


def cheapvibe_cost_note(model: str, quality: str):
    """Подпись под качеством: расход выбранного и всех доступных уровней качества модели."""
    cost = CHEAPVIBE_COST.get(model) or CHEAPVIBE_COST["gpt-image-2"]
    quality = cheapvibe_quality(model, quality)
    levels = ", ".join(f"{q} — {cost[q]}" for q in cheapvibe_qualities(model))
    if len(cost) == 1:
        return T(f"Расход: {cost[quality]} токенов за картинку  (у модели есть только {quality})",
                 f"Cost: {cost[quality]} tokens per image  (the model has only {quality})")
    return T(f"Расход: {cost[quality]} токенов за картинку  ({levels})",
             f"Cost: {cost[quality]} tokens per image  ({levels})")

# -------------------------------------------------------------- CinAI
# Только бесплатные модели: безлимитные на тарифе и проверенные генерацией (см. CINAI_FREE_IMAGE_MODELS
# в engine/params.py — движок с этим списком сверяется). Платные и квотные (Grok, Midjourney) убраны.
CINAI_MODELS = [
    ("nano-banana-pro-test", "Banana Pro"),
    ("nano-banana-2", "Banana 2"),
    ("nano-banana-2-lite-flow", "Banana 2 Lite"),
    ("cinai-lite-flash-1-1", "Cin Lite Flash 1.1"),
    ("cinai-g-image", "CinGen Image 1.0"),
]
CINAI_MODEL_DEFAULT = CINAI_MODELS[0][0]
# Бесплатные модели «картинка → картинка» для ремиксера CinAI
CINAI_REMIX_MODELS = [
    ("nano-banana-pro-test", "Banana Pro"),
    ("nano-banana-2", "Banana 2"),
    ("cinai-g-image", "CinGen Image 1.0"),
]
# Формат кадра ролика — один на все генераторы (картинки, инфографика, ремиксер, видео, склейка).
# Квадрат (1:1) убран: его не умеют видео CinAI и VARAG.
FRAME_FORMATS = ["16:9", "9:16"]
# Видео CinAI — только бесплатные модели (безлимитные на тарифе, проверены t2v и i2v;
# движок сверяется с CINAI_FREE_VIDEO_MODELS в engine/params.py).
CINAI_VIDEO_MODELS = [
    ("cinai-g-video", "CinGen Video 1.0"),
    ("veo-3-1-fast-v2", "Veo 3.1 Fast"),
    ("veo-3-1-lite", "Veo 3.1 Lite"),
]

# ------------------------------------------------------------ Remixer
REMIX_ENGINES = [
    ("cinai", "CinAI"),
    ("off", T("Выкл", "Off")),
]

# ----------------------------------------------- Модели Codex (подписка GPT)
# Список берётся у самого Codex (`codex debug models`), см. codex_models.py; здесь он заполняется
# при импорте, а `set_codex_models` обновляет его «на месте» — все ссылки на список видят новые модели.
CODEX_MODELS: list[str] = []
CODEX_MODEL_NAMES: dict[str, str] = {}

# Модели по оператору (для любых «оператор + модель» пар)
OPERATOR_MODELS = {
    "codex": CODEX_MODELS,
    "gpt": CODEX_MODELS,
}
VISION_MODELS = {
    "codex": CODEX_MODELS,
}

# Вижн-операторы (умеют смотреть картинки)
VISION_PROVIDERS = [
    ("codex", T("Codex (подписка GPT)", "Codex (GPT subscription)")),
]
STOCK_PROMPT_PROVIDERS = [
    ("auto", T("Авто (как режиссёр)", "Auto (as director)")),
    ("gpt", T("Codex (подписка GPT)", "Codex (GPT subscription)")),
]

# Модель переписывания промптов при блокировке цензором: подпись -> spec (модели Codex).
REWRITE_MODELS: list[tuple[str, str]] = []
REWRITE_SPECS: list[str] = []


def set_codex_models(models) -> None:
    """Обновить список моделей Codex ([(slug, имя)]) и всё, что собрано из него, не меняя ссылки на списки."""
    models = list(models) or list(codex_models.FALLBACK)
    CODEX_MODELS[:] = [slug for slug, _ in models]
    CODEX_MODEL_NAMES.clear()
    CODEX_MODEL_NAMES.update(dict(models))
    REWRITE_MODELS[:] = [(f"{name} (Codex)", f"gpt:{slug}") for slug, name in models]
    REWRITE_SPECS[:] = [spec for _, spec in REWRITE_MODELS]


def default_codex_model() -> str:
    """Модель режиссёра по умолчанию: сбалансированная Terra, если есть, иначе первая в списке."""
    return "gpt-5.6-terra" if "gpt-5.6-terra" in CODEX_MODELS else CODEX_MODELS[0]


def default_rewrite_spec() -> str:
    """Для переписывания промпта хватает быстрой модели: первая Luna, иначе первая в списке."""
    return next((s for s in REWRITE_SPECS if s.startswith("gpt:") and "luna" in s), REWRITE_SPECS[0])


set_codex_models(codex_models.load())

# Переходы и зум (совпадают со списками в engine/assemble.py)
TRANSITION_STYLES = [
    "fade", "fadeblack", "fadewhite",
    "wipeleft", "wiperight", "wipeup", "wipedown",
    "slideleft", "slideright", "slideup", "slidedown",
    "smoothleft", "smoothright",
    "dissolve", "pixelize", "circlecrop", "radial", "zoomin", "hblur", "distance",
]
ZOOM_STYLES = [f"{d}-{p}" for d in ("in", "out") for p in (
    "center", "left", "right", "up", "down", "topleft", "topright", "bottomleft", "bottomright")]

LANGUAGES = ["ru", "en", "es", "de", "fr", "it", "pt", "zh", "pl"]

VEO_BASE_URLS = [
    "https://veononstop.org/api/v1",
    "https://eu.veononstop.org/api/v1",
    "https://new.veononstop.org/api/v2",
]


def operator_model_to_id(provider: str, label: str) -> str:
    """Подпись модели из списка -> реальный идентификатор для движка."""
    return (label or "").strip()


def codex_options() -> list[tuple[str, str]]:
    """Модели Codex для кнопок: (slug, имя) — на кнопке имя, в настройки уходит slug."""
    return [(slug, CODEX_MODEL_NAMES.get(slug, slug)) for slug in CODEX_MODELS]
