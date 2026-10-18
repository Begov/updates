"""
Конфигурация агента Video Production Agent
"""
import os
import sys
from pathlib import Path
from dotenv import dotenv_values, load_dotenv

# Загрузка переменных окружения из .env
_ENV_PATH = Path(__file__).parent / '.env'
if not _ENV_PATH.exists():
    # Свежая установка: в дистрибутиве едет только .env.example — сам .env не
    # кладётся ни в DMG, ни в патч (он в SKIP_ON_INSTALL). Без файла редактор
    # настроек в GUI молча пуст, и человек не понимает, куда вписывать ключи
    # Создаём из примера — со всеми пустыми полями.
    # Берём `default_env.txt` — это копия раздаточного .env: все 397 строк с
    # дефолтами и токен приёма обновлений, личные ключи вычищены. Имя без
    # точки в начале намеренно: publisher исключает из патча всё, что
    # начинается с «.env», и файл с таким именем до пользователя не доехал бы.
    # `.env.example` остаётся запасным — он старее и знает лишь 124 строки.
    try:
        for _source in (_ENV_PATH.with_name('default_env.txt'),
                        _ENV_PATH.with_name('.env.example')):
            if _source.is_file():
                _ENV_PATH.write_bytes(_source.read_bytes())
                break
    except OSError:
        pass                      # только чтение — работаем на умолчаниях
load_dotenv(_ENV_PATH)


def _shared_env_value(key: str, default: str = '') -> str:
    """Read shared reseller credentials without importing unrelated app settings."""
    value = str(os.getenv(key, '') or '').strip()
    if value:
        return value
    app_dir = Path(__file__).resolve().parent.parent
    workspace_dir = app_dir.parent
    for env_path in (
        app_dir / '.env',
        workspace_dir / '.env',
        workspace_dir / 'STORY_REWRITER' / '.env',
    ):
        if not env_path.is_file():
            continue
        try:
            value = str(dotenv_values(env_path).get(key) or '').strip()
        except Exception:
            value = ''
        if value:
            return value
    return default


def _model(key: str, default: str = '') -> str:
    """Читает имя модели из env, обрезая инлайн-комментарии (# ...)."""
    return os.getenv(key, default).split('#')[0].strip()


# ============================================================================
# API КЛЮЧИ
# ============================================================================

# AssemblyAI API (транскрипция)
ASSEMBLYAI_API_KEY = os.getenv('ASSEMBLYAI_API_KEY', '')

# ============================================================================
# ПУТИ К ФАЙЛАМ
# ============================================================================

# Базовая директория проекта
BASE_DIR = Path(__file__).parent

# Директория с промптами
PROMPTS_DIR = BASE_DIR / 'instructions'

# Директория для временных файлов
TEMP_DIR = BASE_DIR / 'temp'

# Директория для логов
LOGS_DIR = BASE_DIR / 'logs'

# ============================================================================
# НАСТРОЙКИ ASSEMBLYAI
# ============================================================================

# Модели транскрипции (приоритетный список, через запятую)
# universal-3-pro — лучшее качество ($0.21/ч)
# universal-2     — быстрее и дешевле ($0.15/ч, -40%)
_speech_models_raw = os.getenv('ASSEMBLYAI_SPEECH_MODELS', 'universal-3-pro,universal-2')
ASSEMBLYAI_SPEECH_MODELS = [m.strip() for m in _speech_models_raw.split(',') if m.strip()]

# Язык транскрипции (по умолчанию, можно переопределить в CLI)
ASSEMBLYAI_LANGUAGE_CODE = os.getenv('ASSEMBLYAI_LANGUAGE_CODE', 'ru')

# Поддерживаемые языки
ASSEMBLYAI_SUPPORTED_LANGUAGES = [
    'en',  # English
    'ru',  # Russian
    'es',  # Spanish
    'fr',  # French
    'de',  # German
    'it',  # Italian
    'pt',  # Portuguese
    'nl',  # Dutch
    'pl',  # Polish
    'ja',  # Japanese
    'zh',  # Chinese
    'ko',  # Korean
    'hi',  # Hindi
    'ar',  # Arabic
]

# Режим сегментации транскрипции: 'sentences' или 'paragraphs'
# sentences  — каждое предложение отдельный блок (по умолчанию)
# paragraphs — группирует предложения в смысловые абзацы (меньше блоков, шире охват)
ASSEMBLYAI_SEGMENTATION_MODE = os.getenv('ASSEMBLYAI_SEGMENTATION_MODE', 'sentences')

# HTTP timeout for AssemblyAI SDK (seconds). Default 30s is too low for large files on slow connections.
ASSEMBLYAI_HTTP_TIMEOUT = float(os.getenv('ASSEMBLYAI_HTTP_TIMEOUT', '600'))

# Браузер для cookies yt-dlp: chrome, edge, firefox, opera, brave (пустой = авто-детект)
YTDLP_COOKIES_BROWSER = os.getenv('YTDLP_COOKIES_BROWSER', '')

# ============================================================================
# РЕЖИССУРА: ОБЩИЕ НАСТРОЙКИ
# ============================================================================

# Модель для rewrite промптов (при цензуре VEO). Формат: 'provider:model', например 'gpt:gpt-5.4-mini'
REWRITE_MODEL = os.getenv('REWRITE_MODEL', 'gpt:gpt-5.4-mini')

# Двухпроходный режим: проход 1 = JSON план, проход 2 = промпты (для длинных проектов)
DIRECTOR_TWO_PASS = os.getenv('DIRECTOR_TWO_PASS', 'false').lower() in ('true', '1', 'yes')

# Генерация director_analysis.md (по умолчанию включена для отладки)
GENERATE_DIRECTOR_ANALYSIS = os.getenv('GENERATE_DIRECTOR_ANALYSIS', 'true').lower() in ('true', '1', 'yes')

# Максимальный размер чанка промптов в проходе 2.
# Если клипов больше — pass 2 разбивается на чанки по PROMPT_CHUNK_SIZE клипов.
# 0 = чанкование отключено (один запрос на все клипы).
_chunk_raw = os.getenv('PROMPT_CHUNK_SIZE', '150')
PROMPT_CHUNK_SIZE = int(_chunk_raw) if _chunk_raw.isdigit() else 150

# Чанкование Pass 1 (монтажный план) для длинных озвучек.
# THRESHOLD: если audioDuration > этого порога (сек), Pass 1 разбивается на чанки.
# 0 = всегда монолит (старый режим).
DIRECTOR_PASS1_CHUNK_THRESHOLD = int(os.getenv('DIRECTOR_PASS1_CHUNK_THRESHOLD', '2400'))
# Длина одного чанка Pass 1 (сек). 2400 сек = 40 мин, 1200 = 20 мин.
DIRECTOR_PASS1_CHUNK_DURATION  = int(os.getenv('DIRECTOR_PASS1_CHUNK_DURATION',  '1200'))

# Откат при авто-дополнении (rollback).
# Continuation loop отсекает последние N "уставших" промптов и просит их заново.
# 0 = откат отключён (дописываем строго с позиции обрыва).
_rollback_raw = os.getenv('CONT_ROLLBACK_SIZE', '20')
CONT_ROLLBACK_SIZE = int(_rollback_raw) if _rollback_raw.isdigit() else 20

# ---- Чанкинг Pass 1 для АКТИВНОГО режиссёра (Codex / chat-провайдеры) ----
# Отдельные ключи от DIRECTOR_PASS1_CHUNK_*. Управляются
# ЧЕКБОКСОМ в GUI; .env — только хранилище дефолтов.
#
# PASS1_CHUNK_ENABLED: чекбокс «Чанкинг Pass 1». true → длинное аудио режется на окна по
#   PASS1_CHUNK_MINUTES минут, план строится по окну и сшивается (id_offset). false → монолит
#   (одно окно на весь ролик). В ОБОИХ режимах работает валидатор обрезки окна (докач).
_p1chunk_raw = (os.getenv('PASS1_CHUNK_ENABLED', '') or 'false').strip().lower()
PASS1_CHUNK_ENABLED = _p1chunk_raw in ('1', 'true', 'yes', 'on')
# Размер окна чанка Pass 1 в МИНУТАХ (клипов заранее нет — меряем временем). Дефолт 20 мин.
_p1min_raw = os.getenv('PASS1_CHUNK_MINUTES', '') or '20'
PASS1_CHUNK_MINUTES = int(_p1min_raw) if _p1min_raw.isdigit() and int(_p1min_raw) > 0 else 20
# Параллель Pass 1: число воркеров. 1 = последовательно, >1 = параллель (окна непересекающиеся).
# Та же семантика, что у GPT_PASS2_PARALLEL.
_p1par_raw = os.getenv('PASS1_PARALLEL', '') or '1'
PASS1_PARALLEL = int(_p1par_raw) if _p1par_raw.isdigit() and int(_p1par_raw) > 0 else 1

# ============================================================================
# GPT / CODEX DIRECTOR
# ============================================================================

# Backend режиссуры: 'gpt' (Codex CLI, по умолчанию), 'claude_code' или 'claude_api'
DIRECTOR_BACKEND = (os.getenv('DIRECTOR_BACKEND', '') or 'gpt').strip().lower()

# must_have ЗАМОРОЖЕН (решение 25.08): выкл. → модель его не сочиняет (токены),
# ютуб-вижн его не получает (жёсткий список может отбраковать всех кандидатов).
# Поле в схемах/клипах остаётся; вернуть — включить флаг.
VISUAL_STORY_MUST_HAVE = (
    os.getenv('VISUAL_STORY_MUST_HAVE', 'false').strip().lower()
    in ('1', 'true', 'yes', 'on')
)
# Ютуб-стадия: отсекать ролики, чья ориентация не совпадает с целевой (Shorts
# в 16:9-проекте и наоборот). У поиска YouTube фильтра ориентации нет —
# режем по width/height из деталей yt-dlp, до окон и превью (улов 25.08).
YOUTUBE_MATCH_ORIENTATION = (
    os.getenv('YOUTUBE_MATCH_ORIENTATION', 'true').strip().lower()
    in ('1', 'true', 'yes', 'on')
)
# Сколько роликов брать из поиска на клип (отдельно от размера пула кусков
# YOUTUBE_POOL_TARGET): при пределе = пулу два негодных ролика из четырёх
# оставляли пул на двух оставшихся (улов 25.08).
YOUTUBE_DISCOVERY_LIMIT = max(
    1, int(os.getenv('YOUTUBE_DISCOVERY_LIMIT', '12') or 12)
)
# Сцена закрывается РАВНЫМИ кусками ютуба (n = round(длина / предел)); кусок
# может превысить YOUTUBE_MAX_FRAGMENT_SEC не больше, чем на этот допуск
# (6.5 с → 2 × 3.25, не 3 + 3 + стоп-кадр 0.5 с). Решение 25.08.
YOUTUBE_FRAGMENT_TOLERANCE_SEC = max(
    0.0, float(os.getenv('YOUTUBE_FRAGMENT_TOLERANCE_SEC', '1.0') or 1.0)
)

# Путь к Codex CLI exe (auto-detect per platform)
import shutil as _shutil
_codex_env = os.getenv('CODEX_EXE_PATH', '')
if _codex_env and os.path.exists(_codex_env):
    CODEX_EXE_PATH = _codex_env
elif sys.platform == 'win32':
    # Сначала — собственная копия Codex в runtime/codex, затем системный PATH
    _codex_win = str(BASE_DIR.parent / 'runtime' / 'codex' / 'codex-x86_64-pc-windows-msvc.exe')
    if os.path.exists(_codex_win):
        CODEX_EXE_PATH = _codex_win
    else:
        CODEX_EXE_PATH = _shutil.which('codex') or ''
else:
    CODEX_EXE_PATH = _shutil.which('codex') or ''

# Модель GPT для режиссуры через Codex
GPT_MODEL = _model('GPT_MODEL', 'gpt-5.4')  # default: full model, not mini

# Таймаут на один exec вызов Codex (секунды)
GPT_DIRECTOR_TIMEOUT = int(os.getenv('GPT_DIRECTOR_TIMEOUT', '1200'))
# Отдельный короткий таймаут для одного вызова Codex в rewrite-цепочке цензуры.
# Не должен зависать на 20 мин из-за одного клипа — rewrite быстрый по природе.
GPT_REWRITE_TIMEOUT = int(os.getenv('GPT_REWRITE_TIMEOUT', '90'))

# ============================================================================
# CLAUDE DIRECTOR (alternative backend: Claude Code CLI + Claude Nexus)
# ============================================================================
_claude_env = os.getenv('CLAUDE_CODE_EXE_PATH', '').strip()
if _claude_env:
    CLAUDE_CODE_EXE_PATH = _claude_env
else:
    CLAUDE_CODE_EXE_PATH = _shutil.which('claude') or _shutil.which('claude.cmd') or ''

# Claude Code accepts aliases like "sonnet" / "opus" and full model names.
CLAUDE_MODEL = _model('CLAUDE_MODEL', 'sonnet')
CLAUDE_DIRECTOR_TIMEOUT = int(os.getenv('CLAUDE_DIRECTOR_TIMEOUT', str(GPT_DIRECTOR_TIMEOUT)))
CLAUDE_CODE_MAX_TURNS = int(os.getenv('CLAUDE_CODE_MAX_TURNS', '20'))
CLAUDE_CODE_PERMISSION_MODE = os.getenv('CLAUDE_CODE_PERMISSION_MODE', 'bypassPermissions').strip()

# true = prefer logged-in Claude Pro/Max subscription. We remove ANTHROPIC_API_KEY
# from the child process so Claude Code does not silently switch to API billing.
CLAUDE_CODE_USE_SUBSCRIPTION = os.getenv(
    'CLAUDE_CODE_USE_SUBSCRIPTION', 'true'
).lower() in ('true', '1', 'yes')

# CLAUDE API means the Nexus Claude reseller everywhere in BegoFrame.
# Credentials remain shared with STORY_REWRITER/root .env; they are not copied
# into the portable distribution.
CLAUDE_RESELLER_ANTHROPIC_URL = _shared_env_value(
    'CLAUDE_RESELLER_ANTHROPIC_URL',
    'https://ccmaxshop.de5.net/v1/messages',
)
CLAUDE_RESELLER_ANTHROPIC_API_KEY = _shared_env_value(
    'CLAUDE_RESELLER_ANTHROPIC_API_KEY',
)
CLAUDE_RESELLER_ANTHROPIC_EXTRA_API_KEYS = _shared_env_value(
    'CLAUDE_RESELLER_ANTHROPIC_EXTRA_API_KEYS',
)
CLAUDE_RESELLER_ANTHROPIC_MODEL = _shared_env_value(
    'CLAUDE_RESELLER_ANTHROPIC_MODEL',
    'claude-sonnet-4-6',
)

# Backward-compatible aliases used by the existing claude_api backend.
CLAUDE_API_KEY = (
    CLAUDE_RESELLER_ANTHROPIC_API_KEY
    or os.getenv('CLAUDE_API_KEY', '').strip()
)
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY', '').strip()
CLAUDE_API_BASE = CLAUDE_RESELLER_ANTHROPIC_URL
CLAUDE_API_FORMAT = 'anthropic'
_claude_api_model_raw = (
    _model('CLAUDE_API_MODEL', '')
    or CLAUDE_RESELLER_ANTHROPIC_MODEL
)
_CLAUDE_NEXUS_MODEL_ALIASES = {
    'claude-sonnet-4.8': 'claude-sonnet-4-8',
    'claude-sonnet-4.6': 'claude-sonnet-4-6',
    'claude-sonnet-4.5': 'claude-sonnet-4-5-20250929',
    'claude-opus-4.8': 'claude-opus-4-8',
    'claude-opus-4.7': 'claude-opus-4-7',
    'claude-opus-4.6': 'claude-opus-4-6',
    'claude-haiku-4.5': 'claude-haiku-4-5',
}


def _normalize_claude_nexus_model(model: str) -> str:
    model = str(model or '').strip()
    if model.startswith('kr/'):
        model = model[3:]
    return _CLAUDE_NEXUS_MODEL_ALIASES.get(model, model)


CLAUDE_API_MODEL = _normalize_claude_nexus_model(
    _claude_api_model_raw)
CLAUDE_API_VERSION = os.getenv('CLAUDE_API_VERSION', '2023-06-01').strip()
CLAUDE_API_BETA = os.getenv('CLAUDE_API_BETA', '').strip()
CLAUDE_API_MAX_TOKENS = int(os.getenv('CLAUDE_API_MAX_TOKENS', '16000'))
CLAUDE_API_TEMPERATURE = float(os.getenv('CLAUDE_API_TEMPERATURE', '0.4'))
CLAUDE_API_RESPONSE_FORMAT = os.getenv('CLAUDE_API_RESPONSE_FORMAT', 'json_object').strip()
_claude_api_debug_dir = os.getenv('CLAUDE_API_DEBUG_DIR', str(LOGS_DIR)).strip()
CLAUDE_API_DEBUG_DIR = str(
    (BASE_DIR / _claude_api_debug_dir) if _claude_api_debug_dir and not Path(_claude_api_debug_dir).is_absolute()
    else Path(_claude_api_debug_dir or LOGS_DIR)
)
_claude_api_pass2_chunk_raw = os.getenv('CLAUDE_API_PASS2_CHUNK_SIZE', '20')
CLAUDE_API_PASS2_CHUNK_SIZE = (
    int(_claude_api_pass2_chunk_raw) if _claude_api_pass2_chunk_raw.isdigit() else 20
)
# Claude Code (CLI) pass2 chunk — отдельный от api; по умолчанию как api (20)
_claude_code_pass2_chunk_raw = os.getenv('CLAUDE_CODE_PASS2_CHUNK_SIZE', '20')
CLAUDE_CODE_PASS2_CHUNK_SIZE = (
    int(_claude_code_pass2_chunk_raw) if _claude_code_pass2_chunk_raw.isdigit() else 20
)
CLAUDE_API_USER_AGENT = os.getenv(
    'CLAUDE_API_USER_AGENT',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) BegoFrame/1.0'
).strip()
CLAUDE_API_REFERER = os.getenv(
    'CLAUDE_API_REFERER',
    'https://ccmaxshop.de5.net/',
).strip()


# ============================================================================
# STOCK (Pexels / Pixabay / Local Stock Library) — сток-футаж и фото
# ============================================================================
STOCK_ENABLED = os.getenv('STOCK_ENABLED', 'false').lower() in ('true', '1', 'yes', 'on')
# Источники через запятую: local | pexels | pixabay
STOCK_SOURCES = [s.strip().lower() for s in os.getenv('STOCK_SOURCES', 'local,pexels,pixabay').split(',') if s.strip()]
# Доля ВСЕХ клипов под сток; внутри неё — доля видео (остальное фото)
STOCK_RATIO_TARGET = float(os.getenv('STOCK_RATIO_TARGET', '0.30') or '0.30')
STOCK_VIDEO_SHARE = float(os.getenv('STOCK_VIDEO_SHARE', '0.70') or '0.70')
# Вижн-отбор стока (25.08): сколько футажей из выдачи скачать и показать вижну
# раскадровкой (3 кадра с каждого в одном контактном листе). 0 = без вижна —
# первый результат поиска без просмотра кадра, как раньше.
STOCK_VISION_CANDIDATES = max(0, int(os.getenv('STOCK_VISION_CANDIDATES', '5') or 5))
# Сколько клипов стадия обрабатывает одновременно (28.08). Раньше сток шёл
# строго по одному, а вижн на клип занимает ~40 с: десять клипов — восемь
# минут, и хвост очереди не доживал до стопа. Сеть не мешает (Pexels 200
# запросов/час, Pixabay 100/минуту — нам до них далеко), а семафор вижна
# (CODEX_MAX_PARALLEL) потоки не тормозит: поиск и закачку кандидатов они
# проходят свободно и подходят к вижну с уже готовыми файлами.
STOCK_PARALLEL = max(1, int(os.getenv('STOCK_PARALLEL', '12') or 12))
# Сколько ждать сток после того, как остальные стадии готовы, — прежде чем
# рубить загрузку и отдавать остаток нейрогенерации. Ровно тот же приём, что
# у ютуба (YOUTUBE_WAIT_TIMEOUT): раньше в обычном режиме стоп прилетал
# мгновенно, и на короткой генерации сток терял хвост очереди.
STOCK_PIPELINE_WAIT_TIMEOUT = max(0, int(os.getenv('STOCK_PIPELINE_WAIT_TIMEOUT', '500') or 500))

# Сток: сколько секунд ТИШИНЫ (ни одной новой закрытой сцены) ждать перед остановкой. Заменяет глухой отсчёт: пока стадия работает, ждём.
STOCK_IDLE_TIMEOUT = max(60, int(os.getenv('STOCK_IDLE_TIMEOUT', '300') or '300'))
# YouTube: то же самое. Заменяет глухой отсчёт: пока стадия работает, ждём.
YOUTUBE_IDLE_TIMEOUT = max(60, int(os.getenv('YOUTUBE_IDLE_TIMEOUT', '300') or '300'))
# Real Photo: то же самое. Заменяет глухой отсчёт: пока стадия работает, ждём.
REAL_PHOTO_IDLE_TIMEOUT = max(60, int(os.getenv('REAL_PHOTO_IDLE_TIMEOUT', '300') or '300'))
# Отказ стока (429, авария 5xx, обрыв связи) — не «ничего не нашлось»: ждём и
# повторяем уже другим ключом. Пауза удваивается с каждой попыткой, а
# присланный стоком Retry-After имеет приоритет.
STOCK_HTTP_RETRIES = max(0, int(os.getenv('STOCK_HTTP_RETRIES', '3') or 3))
STOCK_HTTP_COOLDOWN = max(0.5, float(os.getenv('STOCK_HTTP_COOLDOWN', '5') or 5))
# Звук футажа: убирать по умолчанию (музыка футажа поверх голоса + канал Content ID-клеймов)
STOCK_MUTE_AUDIO = os.getenv('STOCK_MUTE_AUDIO', 'true').lower() in ('true', '1', 'yes', 'on')
# Стартовая точка сегмента и её случайный разброс
STOCK_CLIP_START_SEC = float(os.getenv('STOCK_CLIP_START_SEC', '0') or '0')

# С какой секунды РОЛИКА режиссёру можно ставить сток-маркер. Вступление —
# хук, там нужна своя динамика, а не стоковая нарезка. Клипы раньше порога
# теряют маркер и уходят в обычную генерацию. 0 = без ограничения.
# У каждой стадии своя граница: REAL_PHOTO_START_SEC / STOCK_START_SEC /
# INFOGRAPHIC_START_SEC — они независимы.
STOCK_START_SEC = float(os.getenv('STOCK_START_SEC', '0') or '0')
STOCK_CLIP_START_JITTER = os.getenv('STOCK_CLIP_START_JITTER', 'false').lower() in ('true', '1', 'yes', 'on')
STOCK_CLIP_START_JITTER_SEC = float(os.getenv('STOCK_CLIP_START_JITTER_SEC', '2') or '2')
# Не нашлось нигде → клип уходит в обычную нейрогенерацию
STOCK_PROMPT_FALLBACK = os.getenv('STOCK_PROMPT_FALLBACK', 'true').lower() in ('true', '1', 'yes', 'on')
# Макс. время фоновой загрузки стока (сек). Дедлайн = МИН(это, готовность пайплайна):
# конкат не ждёт — недокачанные клипы уходят в нейрогенерацию по промпту режиссёра.
# Своя настройка (не общая с real_photo): видео качается дольше архивных фото.
STOCK_WAIT_TIMEOUT = int(os.getenv('STOCK_WAIT_TIMEOUT', '1800'))
# Оператор промптажа стока (запросы + сопоставление своей библиотеки).
# Пусто/auto = тот же, кем размечает режиссёр.
STOCK_PROMPT_PROVIDER = os.getenv('STOCK_PROMPT_PROVIDER', '').strip().lower()
STOCK_PROMPT_MODEL = _model('STOCK_PROMPT_MODEL', '')
if STOCK_PROMPT_PROVIDER == 'claude_api':
    STOCK_PROMPT_MODEL = _normalize_claude_nexus_model(
        STOCK_PROMPT_MODEL)
# Отсекать ИИ-сгенерированный сток (умеет только Pixabay: isAiGenerated)
STOCK_EXCLUDE_AI = os.getenv('STOCK_EXCLUDE_AI', 'true').lower() in ('true', '1', 'yes', 'on')
# Своя папка с футажами (Local Stock Library). Пусто = источник выключен
STOCK_LIBRARY_DIR = os.getenv('STOCK_LIBRARY_DIR', '').strip()
STOCK_PROXY_URL = os.getenv('STOCK_PROXY_URL', '').strip()

# YouTube Footage is an opt-in stage with one explicitly selected source.
YOUTUBE_FOOTAGE_ENABLED = os.getenv(
    'YOUTUBE_FOOTAGE_ENABLED', 'false'
).lower() in ('true', '1', 'yes', 'on')
YOUTUBE_POOL_TARGET = max(1, int(os.getenv('YOUTUBE_POOL_TARGET', '6') or '6'))
YOUTUBE_SEARCH_TIMEOUT = max(30, int(os.getenv('YOUTUBE_SEARCH_TIMEOUT', '120') or '120'))
YOUTUBE_DOWNLOAD_TIMEOUT = max(60, int(os.getenv('YOUTUBE_DOWNLOAD_TIMEOUT', '900') or '900'))
# Extra wait only after all other stages are ready and concat is blocked by YouTube.
YOUTUBE_WAIT_TIMEOUT = max(1, int(os.getenv('YOUTUBE_WAIT_TIMEOUT', '500') or '500'))
YOUTUBE_MAX_FRAGMENT_SEC = max(0.5, float(os.getenv('YOUTUBE_MAX_FRAGMENT_SEC', '3') or '3'))
YOUTUBE_SAME_VIDEO_MIN_SOURCE_GAP_SEC = max(
    0.0, float(os.getenv('YOUTUBE_SAME_VIDEO_MIN_SOURCE_GAP_SEC', '20') or '20')
)
# Квоты одного ролика-источника (решение 23.08, HYPOTHESIS_VISUAL_STORY_QUERIES H9).
# Доля хронометража фильма, которую может дать один video_id (0 = без лимита).
YOUTUBE_SOURCE_MAX_SHARE = max(
    0.0, float(os.getenv('YOUTUBE_SOURCE_MAX_SHARE', '0.15') or '0.15')
)
# Сколько сцен ПОДРЯД может кормиться одним роликом (переиспользование).
YOUTUBE_SOURCE_MAX_CHAIN = max(
    1, int(os.getenv('YOUTUBE_SOURCE_MAX_CHAIN', '3') or '3')
)
# Свежий поиск: тот же ролик не ближе K клипов к последнему его появлению.
YOUTUBE_SAME_VIDEO_MIN_CLIP_GAP = max(
    0, int(os.getenv('YOUTUBE_SAME_VIDEO_MIN_CLIP_GAP', '5') or '5')
)
YOUTUBE_DLP_PATH = os.getenv('YOUTUBE_DLP_PATH', '').strip()
YOUTUBE_SEARCH_BACKEND = os.getenv('YOUTUBE_SEARCH_BACKEND', 'auto').strip().lower()
if YOUTUBE_SEARCH_BACKEND not in ('auto', 'data_api', 'yt_dlp'):
    YOUTUBE_SEARCH_BACKEND = 'auto'
YOUTUBE_DATA_API_KEY = os.getenv('YOUTUBE_DATA_API_KEY', '').strip()
YOUTUBE_LICENSE_POLICY = os.getenv(
    'YOUTUBE_LICENSE_POLICY', 'creative_common'
).strip().lower()
if YOUTUBE_LICENSE_POLICY not in ('creative_common', 'any'):
    YOUTUBE_LICENSE_POLICY = 'creative_common'
YOUTUBE_RIGHTS_TEXT_POLICY = os.getenv(
    'YOUTUBE_RIGHTS_TEXT_POLICY', 'exclude'
).strip().lower()
if YOUTUBE_RIGHTS_TEXT_POLICY not in ('exclude', 'warn'):
    YOUTUBE_RIGHTS_TEXT_POLICY = 'exclude'
YOUTUBE_SELECTOR_PROVIDER = os.getenv(
    'YOUTUBE_SELECTOR_PROVIDER', 'auto'
).strip().lower()
YOUTUBE_SELECTOR_MODEL = _model('YOUTUBE_SELECTOR_MODEL', '')
# Отбор роликов по названию и описанию из выдачи Data API — этап лёгкий, ему
# отдельный оператор: прочесть десяток карточек и расставить порядок тяжёлой
# модели незачем. Пусто/auto — общий оператор стадии.
# Окон с одного ролика. 0 — по старой карте пар; иначе пара «роликов x окон»
# из GUI: 3x3, 4x2, 4x6 и т.п.
# Брать ли ролики ниже 720p («sd» в Data API). По умолчанию нет: такие ролики
# на деле оказываются 360-480p, и качать их незачем.
YOUTUBE_ALLOW_SD = os.getenv('YOUTUBE_ALLOW_SD', 'false').strip().lower() in ('1', 'true', 'yes', 'on')
YOUTUBE_WINDOWS_PER_VIDEO = int(os.getenv('YOUTUBE_WINDOWS_PER_VIDEO', '0') or '0')
YOUTUBE_RANK_PROVIDER = os.getenv(
    'YOUTUBE_RANK_PROVIDER', 'auto'
).strip().lower()
YOUTUBE_RANK_MODEL = _model('YOUTUBE_RANK_MODEL', '')
# Сколько запросов метаданных к yt-dlp вести разом. Каждое пойманное
# ограничение частоты делит это число пополам (8 → 4 → 2 → 1).
YOUTUBE_META_PARALLEL = int(os.getenv('YOUTUBE_META_PARALLEL', '8') or '8')
# Сколько клипов стадия обрабатывает одновременно.
YOUTUBE_CLIP_PARALLEL = int(os.getenv('YOUTUBE_CLIP_PARALLEL', '4') or '4')
YOUTUBE_RATIO_TARGET = float(os.getenv('YOUTUBE_RATIO_TARGET', '0.0') or '0.0')
YOUTUBE_RATIO_MAX = float(os.getenv('YOUTUBE_RATIO_MAX', '1.0') or '1.0')


# Сколько кандидатов берём от КАЖДОГО включённого стока, прежде чем добирать
# остальными. Обычный режим — три стока сразу, и без квоты лист перекашивал тот,
# кто вернул больше: дальше идёт срез STOCK_VISION_CANDIDATES. 0 = как раньше.
STOCK_VISION_PER_SOURCE = max(0, int(os.getenv('STOCK_VISION_PER_SOURCE', '2') or 2))

# ============================================================================
# ФУТАЖ КОРОЧЕ СЦЕНЫ: замедлить или добрать вторым куском
# ============================================================================
# Сцена длиннее найденного футажа — обычное дело: восьмисекундных роликов под
# восьмисекундную сцену может не быть вовсе, и выбрасывать за это хорошие кадры нельзя.
#
# До этого порога разницу тянет склейка: `video_concat.process_video` считает
# `speed_factor = длина / сцена` и растягивает фрагмент через `setpts`. Проверено
# живьём на стоковом футаже 24 к/с: растяжение 3 с → 5 с даёт 14 уникальных
# кадров в секунду, и на стоковом движении это глазу не мешает (решение
# пользователя 01.09 по итогам просмотра).
#
# Больше порога — сцена собирается из ДВУХ кусков (см. `_materialize_parts`).
STOCK_STRETCH_TOLERANCE_SEC = max(0.0, float(
    os.getenv('STOCK_STRETCH_TOLERANCE_SEC', '2') or 2))
# Короче этого куска футаж бесполезен: из него не выйдет ни одной части сцены.
STOCK_MIN_PART_SEC = max(0.5, float(os.getenv('STOCK_MIN_PART_SEC', '1.5') or 1.5))

# Прокси для ВСЕГО Codex/ChatGPT (одна подписка = один IP): режиссёр + промптеры + адаптеры
# + запрос квоты. Пусто = напрямую. Задаётся юзером в GUI (Settings). http:// или socks5://.
# Пробрасывается в env запускаемого codex.exe (HTTPS_PROXY/HTTP_PROXY/ALL_PROXY) и в usage-запрос.
CODEX_PROXY = os.getenv('CODEX_PROXY', '').strip()

# Прокси для Claude (подписка Pro/Max, claude.ai блокит РФ). Пусто = напрямую.
# Юзер задаёт в GUI (формат host:port:user:pass — нормализуется в URL). НЕ тянем из config.yaml втихую.
CLAUDE_PROXY = os.getenv('CLAUDE_PROXY', '').strip()
# Ключи (бесплатные). Пул: _2.._8 для ротации
PEXELS_API_KEY = os.getenv('PEXELS_API_KEY', '').strip()
PEXELS_API_KEY_2 = os.getenv('PEXELS_API_KEY_2', '').strip()
PEXELS_API_KEY_3 = os.getenv('PEXELS_API_KEY_3', '').strip()
# «Временный API-ключ» Pexels: id сборки сайта из ссылки /_next/data/<id>/…/search/….json.
# Работает, только пока ни одного PEXELS_API_KEY нет. Можно вставить и всю ссылку целиком.
PEXELS_TEMP_KEY = os.getenv('PEXELS_TEMP_KEY', '').strip()
PIXABAY_API_KEY = os.getenv('PIXABAY_API_KEY', '').strip()
PIXABAY_API_KEY_2 = os.getenv('PIXABAY_API_KEY_2', '').strip()
PIXABAY_API_KEY_3 = os.getenv('PIXABAY_API_KEY_3', '').strip()


# ============================================================================
# ENTITY OVERLAY (карточки персон/мест из Wikipedia поверх видео)
# ============================================================================
ENTITY_OVERLAY_ENABLED = os.getenv('ENTITY_OVERLAY_ENABLED', 'false').lower() in ('true', '1', 'yes', 'on')

# Плашка источника на кадре («Wikimedia», «Google Images») для архивных кадров.
# ВЫКЛЮЧЕНА по умолчанию: надпись в углу портит продакшн-качество ролика
# (решение пользователя 26.08). Атрибуция при этом не теряется — остаётся в
# real_photos_credits.json и в real_meta плана, откуда её берут в описание.
ATTRIBUTION_OVERLAY = os.getenv('ATTRIBUTION_OVERLAY', 'false').lower() in ('true', '1', 'yes', 'on')

# Какая доля слов поискового запроса должна совпасть с тегами стокового кандидата,
# чтобы он дошёл до вижна. 0.5 = половина — то же, что у локальной библиотеки.
# Ниже порог → в кандидаты лезет случайный материал по одному общему слову
# (волк с тегом `staring` по запросу «Man staring window»), выше → в узких нишах
# выдача может оказаться пустой и клип уйдёт в нейрогенерацию.
STOCK_MATCH_MIN_SHARE = float(os.getenv('STOCK_MATCH_MIN_SHARE', '0.5') or 0.5)

# Сколько элементов просить у стока в ОДНОМ запросе (per_page). Это не число
# запросов — лимиты стоков считаются по запросам, поэтому 80 стоит столько же,
# сколько 20, а кандидатов после отсева даёт заметно больше. Потолки провайдеров:
# Pexels 80, Pixabay 200 — они клампят сами.
STOCK_SEARCH_LIMIT = max(3, int(os.getenv('STOCK_SEARCH_LIMIT', '80') or 80))

# Сколько экземпляров EPF работает одновременно. У EPF есть штатная настройка
# «Multiply program instances» — мост снимает запрет сам, а каждой дорожке даёт
# свою папку данных (общая база = драка за файлы). 1 = как раньше, по одному.
REAL_PHOTO_EPF_LANES = max(1, min(8, int(os.getenv('REAL_PHOTO_EPF_LANES', '4') or 4)))

# Сколько вызовов Codex CLI идут одновременно. Дорожки EPF, промптеры стадий,
# вижн и адаптеры дёргают его независимо; без лимита набегает больше десятка и
# вызовы душат друг друга таймаутами (прогон 26.08: промптер стока получил
# «timed out after 180s», а в одиночку тот же запрос отвечает за 21с).
CODEX_MAX_PARALLEL = max(1, min(16, int(os.getenv('CODEX_MAX_PARALLEL', '4') or 4)))
# Сколько раз пробовать один вызов Codex. Отказ бывает разовым: 27.08 из
# четырёх одновременных вызовов три отработали за 24 с, а четвёртый упал за
# 6.7 с — и сцена ушла на механический фолбэк вместо повтора.
CODEX_RETRIES = max(1, min(5, int(os.getenv('CODEX_RETRIES', '2') or 2)))
ENTITY_OVERLAY_BACKEND = os.getenv('ENTITY_OVERLAY_BACKEND', 'auto').strip().lower()  # auto|gpt|deepseek|openrouter|claude_api|claude|regex
# Модель детекции сущностей. Пусто → дефолт выбранного провайдера.
ENTITY_OVERLAY_MODEL = _model('ENTITY_OVERLAY_MODEL', '')
_entity_overlay_max_raw = os.getenv('ENTITY_OVERLAY_MAX', '20').strip().lower()
try:
    ENTITY_OVERLAY_MAX = 0 if _entity_overlay_max_raw in ('0', 'all', 'auto', 'max') else int(_entity_overlay_max_raw or '20')
except ValueError:
    ENTITY_OVERLAY_MAX = 20
ENTITY_OVERLAY_HOLD_SEC = float(os.getenv('ENTITY_OVERLAY_HOLD_SEC', '4.2'))
ENTITY_OVERLAY_MIN_GAP_SEC = float(os.getenv('ENTITY_OVERLAY_MIN_GAP_SEC', '24'))
ENTITY_OVERLAY_PROVIDERS = os.getenv('ENTITY_OVERLAY_PROVIDERS', 'wikipedia,yandex').strip()
ENTITY_OVERLAY_WIKI_LANGS = os.getenv('ENTITY_OVERLAY_WIKI_LANGS', 'ru,en').strip()
ENTITY_OVERLAY_MAX_DOWNLOAD_MB = int(os.getenv('ENTITY_OVERLAY_MAX_DOWNLOAD_MB', '12'))
# Сколько карточек качать одновременно. Раньше качались строго по одной, и на 20 карточках
# это давало минуты ожидания. 1 = старое поведение (последовательно).
ENTITY_OVERLAY_PARALLEL = max(1, int(os.getenv('ENTITY_OVERLAY_PARALLEL', '4') or '4'))
# Макс. время фоновой подготовки карточек (сек). Дедлайн = МИН(это, готовность пайплайна):
# конкат не ждёт — берёт из кэша те карточки, что успели.
ENTITY_OVERLAY_WAIT_TIMEOUT = int(os.getenv('ENTITY_OVERLAY_WAIT_TIMEOUT', '1800'))
ENTITY_OVERLAY_PLACE_CAPTION = os.getenv('ENTITY_OVERLAY_PLACE_CAPTION', 'false').lower() in ('true', '1', 'yes', 'on')

# YT Packer: длинные multi-lang промпты (титры+описание+теги × N языков)
# 90с мало — пакет может быть на 10+ минут. 600с = 10 мин.
YT_PACK_GPT_TIMEOUT = int(os.getenv('YT_PACK_GPT_TIMEOUT', '600'))

# Codex "high demand" cooldown (s) — пауза перед retry когда OpenAI возвращает
# "We're currently experiencing high demand". Retry сразу = опять та же стена.
GPT_HIGH_DEMAND_COOLDOWN = int(os.getenv('GPT_HIGH_DEMAND_COOLDOWN', '90'))

# Sandbox mode: 'danger-full-access' или 'workspace-write'
GPT_SANDBOX_MODE = os.getenv('GPT_SANDBOX_MODE', 'danger-full-access')

# Количество клипов на один exec вызов pass 2 (0 = все за раз).
# Default 80 — для chain mode жадная сборка чанков по цепочкам (см. _run_pass2).
_gpt_chunk_raw = os.getenv('GPT_PASS2_CHUNK_SIZE', '') or '80'
GPT_PASS2_CHUNK_SIZE = int(_gpt_chunk_raw) if _gpt_chunk_raw.isdigit() else 80

# Параллельных Codex процессов для Pass 2: max workers
# (1 = последовательно, N = до N одновременно). Дефолт 6 — баланс между скоростью
# и rate-limit ChatGPT Plus.
_gpt_parallel_raw = os.getenv('GPT_PASS2_PARALLEL', '') or '6'
GPT_PASS2_PARALLEL = int(_gpt_parallel_raw) if _gpt_parallel_raw.isdigit() else 6

# ============================================================================
# GPT PROVIDER: 'openai' | direct chat providers. Only DeepSeek/OpenRouter use Moon Bridge.
# ============================================================================
# Codex общается с моделью через OpenAI Responses API. DeepSeek и OpenRouter его не поддерживают,
# поэтому используется локальный прокси Moon Bridge (транслирует Responses → chat/completions).
# OpenRouter = унифицированный прокси ко всем моделям (Qwen, Claude и др.) через OpenAI-формат.
# См. https://github.com/deepseek-ai/awesome-deepseek-agent (DeepSeek), https://openrouter.ai/docs (OpenRouter)
GPT_PROVIDER = (os.getenv('GPT_PROVIDER', '') or 'openai').strip().lower()

# DeepSeek API ключ (используется Moon Bridge'ом, прокидывается в его config.yml)
DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY', '').strip()
# Модель DeepSeek для режиссуры
DEEPSEEK_MODEL = _model('DEEPSEEK_MODEL', 'deepseek-v4-flash')

# OpenRouter API ключ (https://openrouter.ai/settings/keys)
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY', '').strip()
# Модель OpenRouter: 'openrouter/free' (auto router), 'qwen/qwen3-coder:free', etc
OPENROUTER_MODEL = _model('OPENROUTER_MODEL', 'openrouter/free')
# Web Search для OpenRouter (прямой chat_director): 'enabled' → к модели добавляется
# суффикс ':online', 'disabled' → чистый slug (без интернета). GUI: web_on/web_off.
OPENROUTER_WEB_SEARCH = (os.getenv('OPENROUTER_WEB_SEARCH', '') or 'disabled').strip().lower()

# Codex/GPT через OpenAI-совместимый Ailink reseller-шлюз.
CODEX_RESELLER_URL = (
    os.getenv('CODEX_RESELLER_URL', '')
    or 'https://ai.ailink1.com/v1/chat/completions'
).strip()
CODEX_RESELLER_API_KEY = os.getenv('CODEX_RESELLER_API_KEY', '').strip()
CODEX_RESELLER_MODEL = _model('CODEX_RESELLER_MODEL', 'gpt-5.6-terra')

# GPT через Nexus reseller. Это отдельный OpenAI-compatible маршрут, не
# Claude Nexus /v1/messages и не Ailink CODEX_RESELLER_*.
GPT_NEXUS_URL = _shared_env_value(
    'GPT_NEXUS_URL',
    'https://ccmaxshop.de5.net/v1/chat/completions',
).strip()
GPT_NEXUS_API_KEY = _shared_env_value('GPT_NEXUS_API_KEY').strip()
GPT_NEXUS_EXTRA_API_KEYS = _shared_env_value('GPT_NEXUS_EXTRA_API_KEYS').strip()
GPT_NEXUS_MODEL = _shared_env_value('GPT_NEXUS_MODEL', 'gpt-5.5').strip()
GPT_NEXUS_USER_AGENT = _shared_env_value(
    'GPT_NEXUS_USER_AGENT', 'codex_cli_rs/latest').strip()

# Meta Muse Spark is a text-only OpenAI-compatible chat provider.
# The Contributor model is the only currently approved model in this app.
META_MUSE_URL = (
    os.getenv('META_MUSE_URL', '')
    or 'https://api.meta.ai/v1/chat/completions'
).strip()
META_MUSE_API_KEY = os.getenv('META_MUSE_API_KEY', '').strip()
META_MUSE_MODEL = _model('META_MUSE_MODEL', 'muse-spark-1.2-contributor')
try:
    META_MUSE_MAX_TOKENS = max(
        1, int(os.getenv('META_MUSE_MAX_TOKENS', '') or 96000))
except ValueError:
    META_MUSE_MAX_TOKENS = 96000

# ============================================================================
# CHAT DIRECTOR (прямой HTTP для text providers, без Codex/Moon Bridge)
# ============================================================================
# Уровень reasoning/thinking режиссёра: off | low | medium | high | max.
# Маппится в нативные параметры провайдера (см. reasoning_params_for_provider).
CHAT_REASONING_LEVEL = (os.getenv('CHAT_REASONING_LEVEL', '') or 'medium').strip().lower()
# Максимум output-токенов на один chat-запрос (Pass2-чанк). Reasoning-модели жгут
# токены на размышления — нужен запас. Один общий лимит на каждый чанк.
try:
    CHAT_MAX_TOKENS = int(os.getenv('CHAT_MAX_TOKENS', '') or 64000)
except ValueError:
    CHAT_MAX_TOKENS = 64000

# Pass 2 оптимизация: выносить NEGATIVE-блок из вывода модели (дописывать скриптом).
# Стайлгайд должен иметь секцию "## NEGATIVE PROMPT — APPEND ... VERBATIM" с code-блоком.
# Срезает ~180 слов × N клипов дублирования → модель влезает в лимит ответа на больших чанках.
# Если секции нет — авто-fallback (модель пишет негатив сама). 1=вкл (дефолт), 0=выкл.
PASS2_EXTERNALIZE_NEGATIVE = (os.getenv('PASS2_EXTERNALIZE_NEGATIVE', '') or '1').strip() not in ('0', 'false', 'no', '')
# Pass 2 оптимизация: выносить hard-match подпись рефов '(MATCH the face...)' из вывода.
# Модель пишет короткий [name_ref.jpg], скрипт дописывает подпись к ПЕРВОМУ появлению токена
# (имя берётся из самого токена). Срезает ~40-80 слов в 96% промптов. 1=вкл (дефолт), 0=выкл.
PASS2_EXTERNALIZE_REFMATCH = (os.getenv('PASS2_EXTERNALIZE_REFMATCH', '') or '1').strip() not in ('0', 'false', 'no', '')
# Pass 2 оптимизация: выносить STYLE-блок из вывода модели (дописывать скриптом, как negative).
# Срезает ещё ~120 слов × N клипов. 1=вкл (дефолт), 0=выкл.
PASS2_EXTERNALIZE_STYLE = (os.getenv('PASS2_EXTERNALIZE_STYLE', '') or '1').strip() not in ('0', 'false', 'no', '')
# КАК выделять style/negative блоки из стайлгайда:
#   neuro  — семантически нейрой (style_extractor, любой SG, +1 дешёвый вызов) — ДЕФОЛТ
#   regex  — питон по якорям ("...VERBATIM..." + code) — 0 токенов, нужна конвенция в SG
#   off    — не выносить (модель пишет стиль/негатив сама, как раньше)
STYLE_EXTRACT_MODE = (os.getenv('STYLE_EXTRACT_MODE', '') or 'neuro').strip().lower()
# Backend для нейро-выделения блоков: direct chat provider|claude_api|codex.
# Пусто = оператор режиссёра, иначе Codex.
STYLE_EXTRACT_BACKEND = os.getenv('STYLE_EXTRACT_BACKEND', '').strip().lower()
# Конкретная модель (slug) для нейро-выделения. Пусто = провайдерский дефолт.
STYLE_EXTRACT_MODEL = _model('STYLE_EXTRACT_MODEL', '')
if STYLE_EXTRACT_BACKEND == 'claude_api':
    STYLE_EXTRACT_MODEL = _normalize_claude_nexus_model(
        STYLE_EXTRACT_MODEL)


def reasoning_params_for_provider(provider: str, level: str = None) -> dict:
    """Маппит GUI-уровень reasoning (off/low/medium/high/max) в нативные параметры
    провайдера для тела chat/completions запроса.

    DeepSeek: thinking {enabled|disabled} + reasoning_effort (только high|max).
    OpenRouter: reasoning {effort: none|low|medium|high|xhigh, exclude: true}.
    Возвращает dict, который мёрджится в request body (пустой при неизвестном провайдере).
    """
    level = (level or CHAT_REASONING_LEVEL or 'medium').strip().lower()
    provider = (provider or '').strip().lower()

    if provider == 'deepseek':
        if level == 'off':
            return {'thinking': {'type': 'disabled'}}
        # DeepSeek знает только high|max (low/medium → high по их же compat-слою)
        effort = 'max' if level == 'max' else 'high'
        return {'thinking': {'type': 'enabled'}, 'reasoning_effort': effort}

    if provider == 'openrouter':
        # off: НЕ шлём effort:none — на reasoning-обязательных моделях free-роутера это
        # даёт HTTP 400 ("Reasoning is mandatory..."). Вместо этого exclude:true —
        # модель думает минимально, но COT не возвращает. Совместимо со всеми моделями.
        if level == 'off':
            return {'reasoning': {'exclude': True}}
        _map = {'low': 'low', 'medium': 'medium', 'high': 'high', 'max': 'xhigh'}
        effort = _map.get(level, 'medium')
        # exclude=true: модель думает, но COT не возвращает (нам нужен только финальный JSON)
        return {'reasoning': {'effort': effort, 'exclude': True}}

    if provider == 'meta_muse':
        # Meta accepts one top-level depth field. Keep the GUI vocabulary aligned
        # with the API vocabulary while making "off" the smallest safe depth.
        effort = {
            'off': 'minimal',
            'low': 'low',
            'medium': 'medium',
            'high': 'high',
            'max': 'xhigh',
        }.get(level, 'medium')
        return {'reasoning_effort': effort}

    return {}

# Moon Bridge: бинарь (собранный go build) + config.yml + изолированный CODEX_HOME
MOONBRIDGE_DIR = BASE_DIR.parent / 'moonbridge'
_mb_exe_env = os.getenv('MOONBRIDGE_EXE_PATH', '')
if _mb_exe_env and os.path.exists(_mb_exe_env):
    MOONBRIDGE_EXE_PATH = _mb_exe_env
elif sys.platform == 'win32':
    MOONBRIDGE_EXE_PATH = str(MOONBRIDGE_DIR / 'moonbridge.exe')
else:
    MOONBRIDGE_EXE_PATH = str(MOONBRIDGE_DIR / 'moonbridge')
# Адрес прокси (host:port). Codex ходит на http://<addr>/v1
MOONBRIDGE_ADDR = os.getenv('MOONBRIDGE_ADDR', '127.0.0.1:38440').strip()
# Изолированный CODEX_HOME для DeepSeek-режима (чтобы не трогать пользовательский ~/.codex с OpenAI-авторизацией)
DEEPSEEK_CODEX_HOME = str(MOONBRIDGE_DIR / '_codex_home')


def gpt_provider_is_deepseek() -> bool:
    """True если активен DeepSeek-провайдер (и есть ключ)."""
    return GPT_PROVIDER == 'deepseek' and bool(DEEPSEEK_API_KEY)


def gpt_provider_is_openrouter() -> bool:
    """True если активен OpenRouter-провайдер (и есть ключ)."""
    return GPT_PROVIDER == 'openrouter' and bool(OPENROUTER_API_KEY)


def gpt_provider_is_codex_reseller() -> bool:
    """True если активен Codex/GPT reseller и задан его API-ключ."""
    return GPT_PROVIDER == 'codex_reseller' and bool(CODEX_RESELLER_API_KEY)


def gpt_provider_is_gpt_nexus() -> bool:
    """True если активен GPT Nexus OpenAI-compatible reseller."""
    return GPT_PROVIDER == 'gpt_nexus' and bool(GPT_NEXUS_API_KEY)


def gpt_provider_is_meta_muse() -> bool:
    """True если активен текстовый Meta Muse API."""
    return GPT_PROVIDER == 'meta_muse' and bool(META_MUSE_API_KEY)


def gpt_provider_uses_moonbridge() -> bool:
    """True если текущий провайдер требует Moon Bridge (DeepSeek или OpenRouter)."""
    return gpt_provider_is_deepseek() or gpt_provider_is_openrouter()


def codex_model_for_provider(default_model: str = None) -> str:
    """Имя модели для передачи в codex -m. При DeepSeek/OpenRouter всегда 'moonbridge' (route)."""
    if gpt_provider_uses_moonbridge():
        return 'moonbridge'
    return default_model or GPT_MODEL


def codex_cli_args_for_provider(provider: str = None) -> list:
    """Codex CLI args that keep the OpenAI subscription route isolated from custom providers."""
    active_provider = (provider or GPT_PROVIDER or 'openai').strip().lower()
    if active_provider == 'openai':
        return ['--ignore-user-config', '-c', 'model_provider="openai"']
    return []


def codex_env_for_provider(base_env: dict = None, provider: str = None) -> dict:
    """Окружение для subprocess codex. При DeepSeek/OpenRouter подменяет CODEX_HOME на
    изолированный (с config.toml, указывающим на Moon Bridge).
    Если задан CODEX_PROXY — весь трафик codex.exe (Node) идёт через него (обход гео-блока
    ChatGPT без Proxifier). Ставим и UPPER, и lower варианты — Node/разные либы читают по-разному."""
    from units.codex_credentials import codex_env
    env = codex_env(base_env)
    active_provider = (provider or GPT_PROVIDER or 'openai').strip().lower()
    uses_moonbridge = (
        (active_provider == 'deepseek' and bool(DEEPSEEK_API_KEY))
        or (active_provider == 'openrouter' and bool(OPENROUTER_API_KEY))
    )
    if uses_moonbridge:
        env['CODEX_HOME'] = DEEPSEEK_CODEX_HOME
    _px = CODEX_PROXY
    if _px:
        for _k in ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy', 'ALL_PROXY', 'all_proxy'):
            env[_k] = _px
        # localhost мимо прокси (локальные сервисы и callback)
        env.setdefault('NO_PROXY', '127.0.0.1,localhost')
        env.setdefault('no_proxy', '127.0.0.1,localhost')
    return env


# VEO style guide для Codex adapt (дефолт = универсальный, можно заменить в GUI)
VEO_ADAPT_STYLE_GUIDE = os.getenv('VEO_ADAPT_STYLE_GUIDE',
    str(BASE_DIR / 'instructions' / 'veo_i2v_universal_style.md'))

# Claude web chat (unofficial claude-api package).
# Prefer CLAUDE_COOKIE_JSON with a browser-exported JSON file. CLAUDE_COOKIE can
# hold a raw "name=value; name2=value2" Cookie header as a fallback.
CLAUDE_COOKIE_JSON = os.getenv('CLAUDE_COOKIE_JSON', '')
CLAUDE_COOKIE = os.getenv('CLAUDE_COOKIE', '')
CLAUDE_WORK_DIR = os.getenv('CLAUDE_WORK_DIR', '')

# ============================================================================
# НАСТРОЙКИ YT PACKER (YouTube-упаковщик)
# ============================================================================

# Языки (через запятую). Первый = исходный язык транскрипции
_yt_langs_raw = os.getenv('YT_PACK_LANGUAGES', 'ru,en')
YT_PACK_LANGUAGES = [lang.strip() for lang in _yt_langs_raw.split(',') if lang.strip()]

# Оператор YT Packer (заголовки/описание/теги/перевод). Явный выбор:
#   codex | deepseek | openrouter | meta_muse | claude_api
YT_PACK_BACKEND = (os.getenv('YT_PACK_BACKEND', '') or '').strip().lower()
# Модель по умолчанию (используется как fallback для перевода и контента)
YT_PACK_MODEL = _model('YT_PACK_MODEL', 'gpt-5.4-mini')
# Модель для перевода субтитров (можно дешевле — flash/flash-lite)
YT_PACK_TRANSLATE_MODEL = _model('YT_PACK_TRANSLATE_MODEL') or YT_PACK_MODEL
# Модель для контента: заголовки, описание, теги, промпты обложек (можно мощнее — pro)
YT_PACK_CONTENT_MODEL = _model('YT_PACK_CONTENT_MODEL') or YT_PACK_MODEL
if YT_PACK_BACKEND == 'claude_api':
    YT_PACK_MODEL = _normalize_claude_nexus_model(YT_PACK_MODEL)
    YT_PACK_TRANSLATE_MODEL = _normalize_claude_nexus_model(
        YT_PACK_TRANSLATE_MODEL)
    YT_PACK_CONTENT_MODEL = _normalize_claude_nexus_model(
        YT_PACK_CONTENT_MODEL)

# Переключатели подэтапов
YT_PACK_SUBTITLES = os.getenv('YT_PACK_SUBTITLES', 'true').lower() in ('true', '1', 'yes')

# Длина фразы субтитров (для subtitles.srt, выжигаемых конкатом).
# UNIT=chars → по символам (дефолт 50). UNIT=words → по СЛОВАМ (1 = каждое слово отдельным
# субтитром, динамичная смена по слову). Задаётся в GUI (блок Subtitles).
SUBTITLE_PHRASE_UNIT = (os.getenv('SUBTITLE_PHRASE_UNIT', 'chars').strip().lower()
                        if os.getenv('SUBTITLE_PHRASE_UNIT', 'chars').strip().lower() in ('chars', 'words')
                        else 'chars')
try:
    SUBTITLE_PHRASE_LEN = int(os.getenv('SUBTITLE_PHRASE_LEN', '') or ('50' if SUBTITLE_PHRASE_UNIT == 'chars' else '1'))
except ValueError:
    SUBTITLE_PHRASE_LEN = 50 if SUBTITLE_PHRASE_UNIT == 'chars' else 1
YT_PACK_DESCRIPTION = os.getenv('YT_PACK_DESCRIPTION', 'true').lower() in ('true', '1', 'yes')
YT_PACK_COVERS = os.getenv('YT_PACK_COVERS', 'true').lower() in ('true', '1', 'yes')

# Настройки обложек
YT_PACK_COVER_PROVIDER = os.getenv('YT_PACK_COVER_PROVIDER', 'fastgen')  # fastgen | veononstop
YT_PACK_COVER_MODEL = _model('YT_PACK_COVER_MODEL', 'GEM_PIX_2')
YT_PACK_COVER_COUNT = int(os.getenv('YT_PACK_COVER_COUNT', '4'))

# ============================================================================
# НАСТРОЙКИ FASTGEN API (генерация изображений)
# ============================================================================

# API ключ FastGen (основной)
FASTGEN_API_KEY = os.getenv('FASTGEN_API_KEY', '')

# Резервный API ключ FastGen (используется когда основной исчерпал квоту/лицензию)
# Если не задан — fallback на основной ключ (чтобы auto-switch не падал)
FASTGEN_API_KEY_BACKUP = os.getenv('FASTGEN_API_KEY_BACKUP', '') or FASTGEN_API_KEY

# Второй API ключ FastGen (dual-key: параллельная работа двумя аккаунтами)
# Если FASTGEN_API_KEY_2 не задан — fallback на FASTGEN_API_KEY_BACKUP
FASTGEN_API_KEY_2 = os.getenv('FASTGEN_API_KEY_2', '') or os.getenv('FASTGEN_API_KEY_BACKUP', '')
FASTGEN_MAX_PARALLEL_2 = int(os.getenv('FASTGEN_MAX_PARALLEL_2',
                                        os.getenv('FASTGEN_MAX_PARALLEL', '25')))

# Дополнительные API ключи FastGen для multi-key ImageGen worker pool.
FASTGEN_API_KEY_3 = os.getenv('FASTGEN_API_KEY_3', '')
FASTGEN_API_KEY_4 = os.getenv('FASTGEN_API_KEY_4', '')
FASTGEN_MAX_PARALLEL_3 = int(os.getenv('FASTGEN_MAX_PARALLEL_3',
                                        os.getenv('FASTGEN_MAX_PARALLEL', '25')))
FASTGEN_MAX_PARALLEL_4 = int(os.getenv('FASTGEN_MAX_PARALLEL_4',
                                        os.getenv('FASTGEN_MAX_PARALLEL', '25')))

# Базовые URL
FASTGEN_API_BASE = 'https://api.fast-gen.ai/api'
FASTGEN_STORAGE_URL = 'https://storage.fast-gen.ai'

# Движок/модель генерации изображений:
#   Flow API:  GEM_PIX_2 (Nano Pro, рекомендуется), GEM_PIX (Nano), IMAGEN_3_5, NARWHAL (Nano Banana 2), FLOWER (Nano Banana 2 via Flower)
#   Grok:      GROK    (Grok, возвращает 4 варианта)
#   Whisk:     WHISK   (Whisk, категоризированные рефы subject/scene/style)
FASTGEN_MODEL = os.getenv('FASTGEN_MODEL', 'FLOWER')
# Мёртвые движки (API V6: таких провайдеров нет → 404). Авто-миграция
# старых .env на FLOWER (flower_image_generate, 1 credit — дефолт).
if FASTGEN_MODEL in ('GEMINI', 'WHISK', 'IMAGEN_3_5'):
    FASTGEN_MODEL = 'FLOWER'

# Соотношение сторон: portrait, landscape, square
FASTGEN_ASPECT_RATIO = os.getenv('FASTGEN_ASPECT_RATIO', 'landscape')

# Максимум параллельных генераций
FASTGEN_MAX_PARALLEL = int(os.getenv('FASTGEN_MAX_PARALLEL', '25'))

# ImageGen на FastGen: когда часовая квота выжжена на ВСЕХ FastGen-ключах —
# отдать весь остаток клипов VeoNonStop (Banana Pro) одним перемаппингом.
# Если ни у одного ключа VeoNonStop не осталось квоты картинок — не жжём его
# впустую, а ждём обновления часовой квоты FastGen (граница часа) и продолжаем
# генерить FastGen'ом. off — старое поведение: этап просто останавливается.
FASTGEN_QUOTA_FALLBACK_VEONONSTOP = (
    os.getenv('FASTGEN_QUOTA_FALLBACK_VEONONSTOP', 'on') or 'on'
).strip().lower() in ('on', 'true', '1', 'yes')

# Интервал поллинга статуса (секунды)
# If VeoNonStop ImageGen and FastGen hourly capacity are both unavailable,
# use T2V for the remaining clips instead of waiting for FastGen.
VEONONSTOP_QUOTA_FALLBACK_T2V = (
    os.getenv('VEONONSTOP_QUOTA_FALLBACK_T2V', 'off') or 'off'
).strip().lower() in ('on', 'true', '1', 'yes')

FASTGEN_POLL_INTERVAL = 8

# Таймаут ожидания одной генерации (секунды)
FASTGEN_POLL_TIMEOUT = 300

# Предполётная проверка рефов: тест до начала генерации (True = включена)
FASTGEN_PREFLIGHT_REFS = os.getenv('FASTGEN_PREFLIGHT_REFS', 'true').lower() in ('true', '1', 'yes')

# Автоматический Grok-ремикс заблокированных рефов и цензурированных i2v кадров
FASTGEN_GROK_REMIX = os.getenv('FASTGEN_GROK_REMIX', 'true').lower() in ('true', '1', 'yes')

# ============================================================================
# РЕМИКСЕР (перерисовка рефов и i2v кадров, отклонённых модерацией)
# ============================================================================

# Выбор ремиксера: FastGen (grok / flower / openai), CinAI (cinai — модель из CINAI_REMIX_MODEL) или off
REMIX_ENGINE = os.getenv('REMIX_ENGINE', 'grok').lower()
if REMIX_ENGINE in ('cinai_grok', 'cinai_gpt'):
    REMIX_ENGINE = 'cinai'          # прежние движки CinAI на платных/квотных моделях
if REMIX_ENGINE not in (
    'grok', 'flower', 'openai', 'cinai', 'off'
):
    REMIX_ENGINE = 'grok'

# Ремиксер выбран и для него есть доступ (ключ FastGen или сессия CinAI)
REMIX_AVAILABLE = REMIX_ENGINE != 'off' and (
    FASTGEN_GROK_REMIX or bool(FASTGEN_API_KEY)
    or bool(os.getenv('CINAI_ACCESS_TOKEN', '').strip())
)

# ============================================================================
# НАСТРОЙКИ VEONONSTOP API (видеогенерация + картинки)
# ============================================================================

# API ключ VeoNonStop (https://veononstop.org)
VEONONSTOP_API_KEY = os.getenv('VEONONSTOP_API_KEY', '')
# Второй API ключ (опционально): если задан — задачи распределяются round-robin
VEONONSTOP_API_KEY_2 = os.getenv('VEONONSTOP_API_KEY_2', '')
VEONONSTOP_MAX_PARALLEL_2 = int(os.getenv('VEONONSTOP_MAX_PARALLEL_2',
                                           os.getenv('VEONONSTOP_MAX_PARALLEL', '4')))
# Третий API ключ (опционально)
VEONONSTOP_API_KEY_3 = os.getenv('VEONONSTOP_API_KEY_3', '')
VEONONSTOP_MAX_PARALLEL_3 = int(os.getenv('VEONONSTOP_MAX_PARALLEL_3',
                                           os.getenv('VEONONSTOP_MAX_PARALLEL', '4')))
# Четвёртый API ключ (опционально)
VEONONSTOP_API_KEY_4 = os.getenv('VEONONSTOP_API_KEY_4', '')
VEONONSTOP_MAX_PARALLEL_4 = int(os.getenv('VEONONSTOP_MAX_PARALLEL_4',
                                           os.getenv('VEONONSTOP_MAX_PARALLEL', '4')))
# Названия планов для Key1-Key4 (показываются в GUI)
VEONONSTOP_KEY1_NAME = os.getenv('VEONONSTOP_KEY1_NAME', 'Key1')
VEONONSTOP_KEY2_NAME = os.getenv('VEONONSTOP_KEY2_NAME', 'Key2')
VEONONSTOP_KEY3_NAME = os.getenv('VEONONSTOP_KEY3_NAME', 'Key3')
VEONONSTOP_KEY4_NAME = os.getenv('VEONONSTOP_KEY4_NAME', 'Key4')

# Базовый URL API (можно переопределить через env для переключения хоста)
# Варианты: 'https://veononstop.org/api/v1' | 'https://eu.veononstop.org/api/v1' | 'https://viet.veo.kotsklad.ru/api/v1'
VEONONSTOP_API_BASE = os.getenv('VEONONSTOP_API_BASE', 'https://veononstop.org/api/v1').strip()
# Per-key base URL override. Пусто → fallback на VEONONSTOP_API_BASE.
# Позволяет использовать разные URL для разных ключей одновременно.
VEONONSTOP_API_BASE_2 = (os.getenv('VEONONSTOP_API_BASE_2', '') or '').strip() or VEONONSTOP_API_BASE
VEONONSTOP_API_BASE_3 = (os.getenv('VEONONSTOP_API_BASE_3', '') or '').strip() or VEONONSTOP_API_BASE
VEONONSTOP_API_BASE_4 = (os.getenv('VEONONSTOP_API_BASE_4', '') or '').strip() or VEONONSTOP_API_BASE

# ---- Vertex AI (Google Cloud): картинки Imagen/Gemini Image и видео Veo ----
VERTEX_API_KEY = os.getenv('VERTEX_API_KEY', '').strip()
VERTEX_PROJECT_ID = os.getenv('VERTEX_PROJECT_ID', '').strip()
VERTEX_LOCATION = os.getenv('VERTEX_LOCATION', '').strip() or 'us-central1'
VERTEX_IMAGE_MODEL = os.getenv('VERTEX_IMAGE_MODEL', '').strip() or 'gemini-3.1-flash-lite-image'
VERTEX_VIDEO_MODEL = os.getenv('VERTEX_VIDEO_MODEL', '').strip() or 'veo-3.1-lite-generate-001'
VERTEX_VIDEO_RESOLUTION = os.getenv('VERTEX_VIDEO_RESOLUTION', '').strip()
VERTEX_IMAGE_PARALLEL = int(os.getenv('VERTEX_IMAGE_PARALLEL', '') or '2')
VERTEX_VIDEO_PARALLEL = int(os.getenv('VERTEX_VIDEO_PARALLEL', '') or '2')

# ---- CheapVibeCode: картинки gpt-image-2 / grok-imagine-image по OpenAI-совместимому API
# (units/cheapvibe_client.py) ----
CHEAPVIBE_API_KEY = os.getenv('CHEAPVIBE_API_KEY', '').strip()
# RoyalTechno AI — видео Veo 3.1 (720p, $0.02 за клип), units/royaltechno_client.py.
# Выбирается в «Генерации видео» (BEGO_VIDEO_PROVIDER=royaltechno).
ROYALTECHNO_API_KEY = os.getenv('ROYALTECHNO_API_KEY', '').strip()
ROYALTECHNO_API_BASE = os.getenv('ROYALTECHNO_API_BASE', '').strip()
ROYALTECHNO_VIDEO_PARALLEL = int(os.getenv('ROYALTECHNO_VIDEO_PARALLEL', '') or '4')
# VARAG (Inflight API) — картинки и видео по ключу mk_…, units/varag_client.py.
# Выбирается в «Генерации картинок» / «Инфографике» (BEGO_IMAGE_PROVIDER=varag) и
# в «Генерации видео» (BEGO_VIDEO_PROVIDER=varag).
VARAG_API_KEY = os.getenv('VARAG_API_KEY', '').strip()
VARAG_API_BASE = os.getenv('VARAG_API_BASE', '').strip()
VARAG_IMAGE_PARALLEL = int(os.getenv('VARAG_IMAGE_PARALLEL', '') or '4')
VARAG_VIDEO_PARALLEL = int(os.getenv('VARAG_VIDEO_PARALLEL', '') or '4')
CHEAPVIBE_API_BASE = (os.getenv('CHEAPVIBE_API_BASE', '').strip() or 'https://ru.cheapvibecode.ru/v1').rstrip('/')
CHEAPVIBE_IMAGE_MODEL = os.getenv('CHEAPVIBE_IMAGE_MODEL', '').strip() or 'gpt-image-2'
CHEAPVIBE_IMAGE_SIZE = os.getenv('CHEAPVIBE_IMAGE_SIZE', '').strip() or '1536x1024'
CHEAPVIBE_IMAGE_QUALITY = os.getenv('CHEAPVIBE_IMAGE_QUALITY', '').strip().lower() or 'medium'
CHEAPVIBE_IMAGE_PARALLEL = int(os.getenv('CHEAPVIBE_IMAGE_PARALLEL', '') or '2')

# Параллельных задач (default=4, max=concurrent_tasks из API).
# 4 — безопасное значение без slots_full. Можно повысить через .env до 12.
VEONONSTOP_MAX_PARALLEL = int(os.getenv('VEONONSTOP_MAX_PARALLEL', '4'))
try:
    VEONONSTOP_MAX_DOWNLOADS = max(
        1, int(os.getenv('VEONONSTOP_MAX_DOWNLOADS', '12') or '12')
    )
except ValueError:
    VEONONSTOP_MAX_DOWNLOADS = 12

# Соотношение сторон видео/картинок: 16:9 или 9:16 (формат 1:1 из программы убран —
# старое значение из .env становится 16:9)
VEONONSTOP_ASPECT_RATIO = os.getenv('VEONONSTOP_ASPECT_RATIO', '16:9')
if VEONONSTOP_ASPECT_RATIO not in ('16:9', '9:16'):
    VEONONSTOP_ASPECT_RATIO = '16:9'

# Подгонять длительность клипа под длину сцены: 4с / 5с / 6с / 8с, ближайшее СВЕРХУ.
# По актуальной доке API v1 параметр `duration` принимает режим «картинка → видео»
# (image-to-video). В остальных режимах клип всегда 8 секунд.
#
# Умолчание — включено: на минутном ролике с коротким вступлением это заметно
# меньше лишнего материала (замер 01.09.2026 на 15 сценах: 84с вместо 120с),
# а подрезка остаётся такой же, как была. Выключить: off / 0 / false / no.
# В интерфейсе — «Длительность под сцену» (VeoNonStop, Vertex, CinAI).
VEONONSTOP_CUSTOM_DURATION = os.getenv('VEONONSTOP_CUSTOM_DURATION', 'on')

# Режим imggen: imagen, banana, banana_ref
VEONONSTOP_IMGGEN_MODE = os.getenv('VEONONSTOP_IMGGEN_MODE', 'banana')

# ImageGen fallback for OUR VeoNonStop API-key quota (429 «Daily image
# generation limit exceeded»). This is separate from the provider-side
# per-model cookie pool exhaustion (500 «ALL_COOKIES_EXHAUSTED_FOR_MODEL»),
# which is handled by switching Banana models below.
# off — старое поведение: докручивать циклы и останавливаться.
VEONONSTOP_QUOTA_FALLBACK_FASTGEN = (
    os.getenv('VEONONSTOP_QUOTA_FALLBACK_FASTGEN', 'on') or 'on'
).strip().lower() in ('on', 'true', '1', 'yes')

# Порядок перебора banana-моделей, когда сервис отвечает
# «ALL_COOKIES_EXHAUSTED_FOR_MODEL: <модель>» (HTTP 500). Это отдельный пул
# Google-аккаунтов (cookies) НА СТОРОНЕ СЕРВИСА под конкретную модель, а не наша
# квота: у ключа image_remaining остаётся. Смена ключа не помогает (пул общий),
# помогает смена модели — перебор идёт ПО КЛИПУ, а не по всему этапу.
# Выжженные модели пропускаются; когда пусты все — остаток уходит на FastGen.
VEONONSTOP_BANANA_MODEL_CHAIN = (
    os.getenv('VEONONSTOP_BANANA_MODEL_CHAIN', '') or 'GEM_PIX_2,NARWHAL,HARBOR_SEAL'
).strip()

# Интервал поллинга статуса (секунды)
VEONONSTOP_POLL_INTERVAL = int(os.getenv('VEONONSTOP_POLL_INTERVAL', '10'))

# Таймаут ожидания одной задачи (секунды, 15 мин по умолчанию). Сама генерация —
# 1–3 минуты, но задача может ещё стоять в очереди за свободным слотом.
VEONONSTOP_POLL_TIMEOUT = int(os.getenv('VEONONSTOP_POLL_TIMEOUT', '') or '900')

# Advisory /account/usage polling; the relay may answer it slower than submit/status.
try:
    VEONONSTOP_USAGE_TIMEOUT = max(
        15, int(os.getenv('VEONONSTOP_USAGE_TIMEOUT', '30') or '30')
    )
except ValueError:
    VEONONSTOP_USAGE_TIMEOUT = 30
try:
    VEONONSTOP_USAGE_RETRIES = max(
        0, int(os.getenv('VEONONSTOP_USAGE_RETRIES', '1') or '1')
    )
except ValueError:
    VEONONSTOP_USAGE_RETRIES = 1
try:
    VEONONSTOP_USAGE_RETRY_DELAY = max(
        0.25, float(os.getenv('VEONONSTOP_USAGE_RETRY_DELAY', '1.5') or '1.5')
    )
except ValueError:
    VEONONSTOP_USAGE_RETRY_DELAY = 1.5
try:
    VEONONSTOP_USAGE_FAIL_SOFT_ERRORS = max(
        1, int(os.getenv('VEONONSTOP_USAGE_FAIL_SOFT_ERRORS', '1') or '1')
    )
except ValueError:
    VEONONSTOP_USAGE_FAIL_SOFT_ERRORS = 1

# Модель Banana VeoNonStop imggen: GEM_PIX_2 (Pro, дефолт) | NARWHAL (Banana 2) | HARBOR_SEAL (Banana 2 Lite)
VEONONSTOP_BANANA_MODEL = os.getenv('VEONONSTOP_BANANA_MODEL', 'GEM_PIX_2')
VEONONSTOP_CENSOR_STREAK_LIMIT = max(
    1, int(os.getenv('VEONONSTOP_CENSOR_STREAK_LIMIT', '2'))
)
VEONONSTOP_STATIC_FALLBACK_MODEL = os.getenv(
    'VEONONSTOP_STATIC_FALLBACK_MODEL', 'HARBOR_SEAL'
)

# Режим применения рефов: name_match (по именам из промпта) или use_all (все рефы на каждый промпт)
VEONONSTOP_REF_MODE = os.getenv('VEONONSTOP_REF_MODE', 'name_match')

# Задержка между запросами воркеров (секунды). Увеличь если много reCAPTCHA ошибок.
# 3.0 = 24 воркера стартуют за 72с (было 10.0 = 230с). Уменьши если нужен более плотный старт.
VEONONSTOP_REQUEST_DELAY = float(os.getenv('VEONONSTOP_REQUEST_DELAY', '3.0'))

# Макс. попыток на шаг генерации (Phase 1 и каждый шаг цепочки при техн. ошибке)
VEONONSTOP_MAX_RETRIES = int(os.getenv('VEONONSTOP_MAX_RETRIES', '') or '5')

# Макс. server-timeout ретраев на клип (после превышения — тратим попытку, идём к static fallback)
VEONONSTOP_MAX_TIMEOUT_RETRIES = int(os.getenv('VEONONSTOP_MAX_TIMEOUT_RETRIES', '') or '5')

# Повторные запуски в silent режиме (для клипов с техн. ошибками)
VEONONSTOP_MAX_SILENT_CYCLES = int(os.getenv('VEONONSTOP_MAX_SILENT_CYCLES', '5'))
VEONONSTOP_MAX_RESUBMITS       = int(os.getenv('VEONONSTOP_MAX_RESUBMITS', '') or '3')
VEONONSTOP_RECAPTCHA_DELAY     = float(os.getenv('VEONONSTOP_RECAPTCHA_DELAY', '8.0'))   # base delay (s) before reCAPTCHA retry
VEONONSTOP_RECAPTCHA_JITTER    = float(os.getenv('VEONONSTOP_RECAPTCHA_JITTER', '3.0'))  # max jitter (s) added to base

# Режим быстрой цензуры: off | after_rewrites | ultrafast
#   off            — полная цепочка (реврайты + ремиксы + минималистик)
#   after_rewrites — реврайты да, ремиксы нет, сразу static fallback
#   ultrafast      — сразу static fallback без реврайтов и ремиксов
VEONONSTOP_FAST_CENSOR = os.getenv('VEONONSTOP_FAST_CENSOR', 'off').lower()

# Апскейл изображений после генерации: off | 2K | 4K
VEONONSTOP_IMAGE_UPSCALE = os.getenv('VEONONSTOP_IMAGE_UPSCALE', 'off').lower()

# Апскейл видео после генерации (1080p): true/false
VEONONSTOP_VIDEO_UPSCALE = os.getenv('VEONONSTOP_VIDEO_UPSCALE', '').lower() in ('true', '1', 'yes')

# ============================================================================
# НАСТРОЙКИ ВИДЕО
# ============================================================================

# FPS для финального видео
VIDEO_FPS = 60

# ============================================================================
# ДЛИТЕЛЬНОСТЬ КЛИПОВ МОНТАЖНОГО ПЛАНА
# ============================================================================

# Видеорежим (Veo3-фрагменты)
VIDEO_CLIP_MIN_DURATION    = int(os.getenv('VIDEO_CLIP_MIN_DURATION',    '4'))
VIDEO_CLIP_MAX_DURATION    = int(os.getenv('VIDEO_CLIP_MAX_DURATION',    '8'))

# Слайдшоурежим (картинки)
SLIDESHOW_CLIP_MIN_DURATION = int(os.getenv('SLIDESHOW_CLIP_MIN_DURATION', '2'))
SLIDESHOW_CLIP_MAX_DURATION = int(os.getenv('SLIDESHOW_CLIP_MAX_DURATION', '7'))

# Активный режим — выставляется агентом при старте (по умолчанию видео)
ACTIVE_CLIP_MIN_DURATION = VIDEO_CLIP_MIN_DURATION
ACTIVE_CLIP_MAX_DURATION = VIDEO_CLIP_MAX_DURATION

# ============================================================================
# ВСТУПИТЕЛЬНЫЙ БЛОК (intro)
# ============================================================================

# Длительность вступительной зоны таймлайна (секунды)
# Все клипы до этой отметки используют intro-настройки длительности
INTRO_BLOCK_DURATION = int(os.getenv('INTRO_BLOCK_DURATION', '30'))

# Мин/макс длительность клипов во вступительном блоке (0 = авто)
# Если 0 — берётся из ACTIVE с ограничением max≤7
INTRO_CLIP_MIN_DURATION = int(os.getenv('INTRO_CLIP_MIN_DURATION', '0'))
INTRO_CLIP_MAX_DURATION = int(os.getenv('INTRO_CLIP_MAX_DURATION', '0'))


# ── Оживление обложки первым кадром ─────────────────────────────────────────
# Зритель кликает на обложку — и ролик начинается с неё же, только ожившей.
# Работает на удержание первых секунд: то, что человек выбрал глазами,
# продолжается в движении.
#
# Включается САМИМ ФАКТОМ приложенной обложки, отдельной галки нет.
# Длину задаёт человек, а НЕ режиссёр: отдай решение ему — растянет до четырёх
# секунд, и зритель столько времени смотрит на неподвижную картинку.
# Битое значение НЕ роняет конфигурацию: без отката весь софт не запустился бы
# из-за одной опечатки в .env (поймано аудитом 02.09.2026).
try:
    COVER_HOOK_SEC = max(0.0, float(os.getenv('COVER_HOOK_SEC', '2') or 2))
except (TypeError, ValueError):
    COVER_HOOK_SEC = 2.0

# Чем оживлять: zoom_in (наезд) | zoom_out (отъезд) | drift (дрейф) |
# fly_in (залёт в сцену) | awaken (оживание — статика на кадре начинает
# шевелиться). Тексты промптов — agent/prompts/cover_hook_prompts.txt,
# правятся во вкладке Prompts.
COVER_HOOK_MODE = (os.getenv('COVER_HOOK_MODE', '') or 'zoom_in').strip().lower()
if COVER_HOOK_MODE not in ('zoom_in', 'zoom_out', 'drift', 'fly_in', 'awaken'):
    COVER_HOOK_MODE = 'zoom_in'


def get_intro_clip_min():
    return INTRO_CLIP_MIN_DURATION if INTRO_CLIP_MIN_DURATION > 0 else ACTIVE_CLIP_MIN_DURATION


def get_intro_clip_max():
    return INTRO_CLIP_MAX_DURATION if INTRO_CLIP_MAX_DURATION > 0 else min(7, ACTIVE_CLIP_MAX_DURATION)

# ============================================================================
# ZOOM на статичных кадрах (video_concat: Ken Burns)
# ============================================================================
# Амплитуда зума за клип (0.05 / 0.10 / 0.15 / 0.20)
try:
    ZOOM_AMOUNT = float(os.getenv('ZOOM_AMOUNT', '0.10'))
except (TypeError, ValueError):
    ZOOM_AMOUNT = 0.10
# Кривая времени (linear / constant_speed / ease_out)
ZOOM_CURVE = os.getenv('ZOOM_CURVE', 'linear')
# Плавность зума: во сколько раз считать движение крупнее кадра (1 / 2 / 3).
# ffmpeg меняет размер целыми пикселями, и при медленном наезде картинка через
# кадр стоит — это и есть дрожание. Считая крупнее и ужимая в конце, шаг
# делается субпиксельным. 1 — прежнее поведение, дальше дороже по времени.
try:
    ZOOM_SUPERSAMPLE = min(3, max(1, int(os.getenv('ZOOM_SUPERSAMPLE', '2') or 2)))
except (TypeError, ValueError):
    ZOOM_SUPERSAMPLE = 2

# ============================================================================
# НАСТРОЙКИ ЛОГГИРОВАНИЯ
# ============================================================================

# Уровень логгирования
LOG_LEVEL = 'DEBUG'  # 'DEBUG', 'INFO', 'WARNING', 'ERROR'

# Формат логов
LOG_FORMAT = '%(asctime)s | %(levelname)-8s | %(message)s'

# ============================================================================
# НАСТРОЙКИ GUI ПАЙПЛАЙНА (умолчания вкладки Pipeline Launcher)
# ============================================================================

# Точка старта: audio | script
PIPELINE_START_POINT  = os.getenv('PIPELINE_START_POINT', '') or 'audio'
# Режим клипов: video | slideshow | vislide
PIPELINE_CLIP_MODE         = os.getenv('PIPELINE_CLIP_MODE', '') or 'video'
PIPELINE_VISLIDE_INTERVAL  = int(os.getenv('PIPELINE_VISLIDE_INTERVAL', '') or '3')
# Источник изображений: fastgen | veononstop | cinai | skip
PIPELINE_IMG_GEN      = os.getenv('PIPELINE_IMG_GEN', '') or 'veononstop'
# Режим видеогенерации: i2v | t2v | components | skip
PIPELINE_VIDEO_MODE   = os.getenv('PIPELINE_VIDEO_MODE', '') or 'i2v'
# I2V: replace per-clip motion prompts with one of the user-editable parallax
# templates. Mixed mode alternates parallax and normal per-clip prompts.
VEONONSTOP_I2V_PARALLAX = (
    os.getenv('VEONONSTOP_I2V_PARALLAX', 'false') or 'false'
).strip().lower() in ('on', 'true', '1', 'yes')
try:
    VEONONSTOP_I2V_PARALLAX_METHOD = int(
        os.getenv('VEONONSTOP_I2V_PARALLAX_METHOD', '1') or '1')
except ValueError:
    VEONONSTOP_I2V_PARALLAX_METHOD = 1
# Нижняя граница 0, а НЕ 1: ноль — это режим «вперемешку» (parallax.PARALLAX_RANDOM),
# в нём шаблон выбирается по номеру клипа. Прежний зажим `max(1, ...)` превращал его
# в единицу прямо при чтении .env, поэтому пункт был виден в списке, выбирался,
# сохранялся — и никогда не работал: генерация всегда шла первым шаблоном (29.08).
VEONONSTOP_I2V_PARALLAX_METHOD = max(0, min(3, VEONONSTOP_I2V_PARALLAX_METHOD))
VEONONSTOP_I2V_PARALLAX_MIX_PERCLIP = (
    os.getenv('VEONONSTOP_I2V_PARALLAX_MIX_PERCLIP', 'false') or 'false'
).strip().lower() in ('on', 'true', '1', 'yes')
try:
    VEONONSTOP_I2V_PARALLAX_EVERY_N = max(
        1, int(os.getenv('VEONONSTOP_I2V_PARALLAX_EVERY_N', '2') or '2'))
except ValueError:
    VEONONSTOP_I2V_PARALLAX_EVERY_N = 2
# КОМУ достаётся параллакс (галки рядом с самим параллаксом, 28.08). Раньше в
# стандартном режиме он доставался только нейрокадрам — просто потому, что
# сток и архивные фото в i2v не попадают вовсе: клипы с внешним источником
# пропускаются генератором. Теперь охват выбирает пользователь.
VEONONSTOP_I2V_PARALLAX_AI = (
    os.getenv('VEONONSTOP_I2V_PARALLAX_AI', 'true') or 'true'
).strip().lower() in ('on', 'true', '1', 'yes')
VEONONSTOP_I2V_PARALLAX_STOCK = (
    os.getenv('VEONONSTOP_I2V_PARALLAX_STOCK', 'false') or 'false'
).strip().lower() in ('on', 'true', '1', 'yes')
VEONONSTOP_I2V_PARALLAX_REAL_PHOTO = (
    os.getenv('VEONONSTOP_I2V_PARALLAX_REAL_PHOTO', 'false') or 'false'
).strip().lower() in ('on', 'true', '1', 'yes')
# Сколько ждать параллакс, когда все стадии закончили. Кадры, закрытые в самом
# конце прогона, только-только легли в generated/ — резкий стоп не дал бы их
# оживить. Не дождались за это время — стопаем и уходим в склейку, такие кадры
# останутся статикой с зумом.
VEONONSTOP_I2V_PARALLAX_WAIT_TIMEOUT = max(0, int(
    os.getenv('VEONONSTOP_I2V_PARALLAX_WAIT_TIMEOUT', '500') or 500))
# Оживлять кадры, НЕ дожидаясь конца генерации картинок. Решает только «когда»,
# а не «что»: состав задаёт режим видео и галки параллакса. Выключено —
# оживление начинается после ImageGen, как было раньше.
#
# Поверх этой настройки работает шлюз, который пользователь не переключает:
# когда картинки делает VeoNonStop на ТОМ ЖЕ ключе, оживление всё равно ждёт их
# окончания — слоты у ключа общие на картинки и видео, и одновременная работа
# только замедляет обе стадии.
VIDEO_LIVE_ANIMATE = (
    os.getenv('VIDEO_LIVE_ANIMATE', 'false') or 'false'
).strip().lower() in ('on', 'true', '1', 'yes')
# Режим промптов: single | per_clip
PIPELINE_PROMPT_MODE  = os.getenv('PIPELINE_PROMPT_MODE', '') or 'single'
# Рендер: 720p | 1080p | fast
PIPELINE_RENDER_MODE  = os.getenv('PIPELINE_RENDER_MODE', '') or '720p'
# Ватермарка: as_is | blur_borders | skip
PIPELINE_WATERMARK    = os.getenv('PIPELINE_WATERMARK', '') or 'as_is'

# ============================================================================
# НАСТРОЙКИ CINAI.TV (reverse API StudioChat, image generation)
# ============================================================================

CINAI_ACCESS_TOKEN = os.getenv('CINAI_ACCESS_TOKEN', '').strip()
CINAI_REFRESH_TOKEN = os.getenv('CINAI_REFRESH_TOKEN', '').strip()
CINAI_API_KEY = os.getenv('CINAI_API_KEY', '').strip()
# Транспорт генерации:
#   workflow — прямой Workflow API (ноды/рёбра/executions). Промпт доходит до
#              генератора ДОСЛОВНО, опросника ask_human нет. ДЕФОЛТ.
#   agent    — legacy /api/agent/dispatch/stream. С 15.08.2026 провайдер
#              поставил туда LLM-раннер: он переписывает промпт и задаёт
#              вопросы — для пайплайна непригоден. Оставлен на случай отката
#              провайдера. Подробности: docs/CINAI_WORKFLOW_API_PLAN.md
CINAI_TRANSPORT = (os.getenv('CINAI_TRANSPORT', 'workflow') or 'workflow').strip().lower()
if CINAI_TRANSPORT not in ('workflow', 'agent'):
    CINAI_TRANSPORT = 'workflow'
CINAI_API_BASE = (os.getenv('CINAI_API_BASE', 'https://api.cinai.tv') or 'https://api.cinai.tv').strip().rstrip('/')
CINAI_APP_ID = (os.getenv('CINAI_APP_ID', 'runway-workflows') or 'runway-workflows').strip()
# Модели картинок CinAI — ТОЛЬКО бесплатные: безлимитные на тарифе аккаунта
# (pricing.plan_perks.<тариф>.type = unlimited в /api/entities/Model,
# подтверждено /api/model-perks/status). Каждая проверена реальной генерацией
# 11.10.2026. Платные и квотные (Grok, Midjourney — 50 в месяц, дальше кредиты)
# сюда не входят: значение из старого .env вне списка заменяется умолчанием.
# Cin Lite Flash (nano-banana-2-lite) тоже безлимитна, но на проверке CinAI
# держал её отключённой («слишком много ошибок подряд») — в список не вошла.
CINAI_FREE_IMAGE_MODELS = (
    'nano-banana-pro-test', 'nano-banana-2', 'nano-banana-2-lite-flow',
    'cinai-g-image', 'cinai-lite-flash-1-1', 'seedream-5-pro-text-2-img',
)
# Бесплатные модели «картинка → картинка» для ремиксера (у них есть вход картинки)
CINAI_FREE_REMIX_MODELS = ('seedream-5-pro', 'nano-banana-pro-test', 'nano-banana-2', 'cinai-g-image')


def _cinai_free(value: str, allowed: tuple) -> str:
    value = (value or '').strip()
    return value if value in allowed else allowed[0]


CINAI_MODEL = _cinai_free(os.getenv('CINAI_MODEL', ''), CINAI_FREE_IMAGE_MODELS)
CINAI_REMIX_MODEL = _cinai_free(os.getenv('CINAI_REMIX_MODEL', ''), CINAI_FREE_REMIX_MODELS)
CINAI_IMAGE_POOL_ENABLED = (
    os.getenv('CINAI_IMAGE_POOL_ENABLED', 'false').strip().lower()
    in ('true', '1', 'yes', 'on')
)
CINAI_ASPECT_RATIO = (os.getenv('CINAI_ASPECT_RATIO', '16:9') or '16:9').strip()
if CINAI_ASPECT_RATIO not in ('16:9', '9:16'):          # формат 1:1 из программы убран
    CINAI_ASPECT_RATIO = '16:9'
CINAI_QUALITY = (os.getenv('CINAI_QUALITY', '1K') or '1K').strip().upper()
if CINAI_QUALITY not in ('1K', '2K', '4K'):
    CINAI_QUALITY = '1K'
CINAI_REMIX_ASPECT_RATIO = (
    os.getenv('CINAI_REMIX_ASPECT_RATIO', CINAI_ASPECT_RATIO)
    or CINAI_ASPECT_RATIO
).strip()
CINAI_REMIX_QUALITY = (
    os.getenv('CINAI_REMIX_QUALITY', CINAI_QUALITY) or CINAI_QUALITY
).strip().upper()
if CINAI_REMIX_QUALITY not in ('1K', '2K', '4K'):
    CINAI_REMIX_QUALITY = CINAI_QUALITY
try:
    CINAI_MAX_PARALLEL = max(1, min(6, int(os.getenv('CINAI_MAX_PARALLEL', '6') or '6')))
except ValueError:
    CINAI_MAX_PARALLEL = 6
try:
    CINAI_TIMEOUT = max(60, int(os.getenv('CINAI_TIMEOUT', '900') or '900'))
except ValueError:
    CINAI_TIMEOUT = 900
try:
    CINAI_MAX_RETRIES = max(0, int(os.getenv('CINAI_MAX_RETRIES', '3') or '3'))
except ValueError:
    CINAI_MAX_RETRIES = 3

# Директ-тайминг: заказывать у видео-модели длительность КАЖДОГО клипа
# (из montage_plan), а не одну общую из настроек: ближайшую сверху из того, что
# умеет модель (CinGen Video — 3..10с, Veo 3.1 Fast и Lite — 4/6/8с).
# Лишний хвост подрезает concat.
# В интерфейсе — та же галка «Длительность под сцену». У бесплатных моделей длину
# соблюдает только t2v: при оживлении картинки они всегда отдают 8-10 с.
CINAI_VIDEO_DIRECT_TIMING = os.getenv(
    'CINAI_VIDEO_DIRECT_TIMING', '1').strip().lower() in ('1', 'true', 'yes', 'on')

# Липкая нода: кадр и видео одним графом (не качаем кадр и не заливаем обратно).
# Выключено — раздельный путь: сперва все кадры, потом i2v по ним.
CINAI_VIDEO_CHAIN = os.getenv(
    'CINAI_VIDEO_CHAIN', '1').strip().lower() in ('1', 'true', 'yes', 'on')

# CinAI TV video provider. Disabled until the provider passes its opt-in
# contract and pipeline acceptance gates.
# Движок видеогенерации, выбранный в GUI (радио Engine в Video Generation).
# Раньше не персистился вовсе: Save Defaults его не писал, GUI при старте
# жёстко ставил veononstop — выбор CinAI слетал после перезапуска.
PIPELINE_VIDEO_ENGINE = (
    os.getenv('PIPELINE_VIDEO_ENGINE', 'veononstop') or 'veononstop'
).strip().lower()
if PIPELINE_VIDEO_ENGINE not in ('veononstop', 'cinai'):
    PIPELINE_VIDEO_ENGINE = 'veononstop'
CINAI_VIDEO_ENABLED = os.getenv('CINAI_VIDEO_ENABLED', 'false').lower() in ('true', '1', 'yes')
# Видеомодели CinAI — ТОЛЬКО бесплатные (безлимитные на тарифе, проверены t2v и i2v
# 11.10.2026; см. VIDEO_PROFILES в units/cinai_motion.py). Квотные Seedance 2.0 Lite,
# Omni Flash и Grok Video (20 в месяц, дальше кредиты) и платные сюда не входят.
CINAI_FREE_VIDEO_MODELS = ('cinai-g-video', 'veo-3-1-fast-v2', 'veo-3-1-lite')
CINAI_VIDEO_MODEL = _cinai_free(os.getenv('CINAI_VIDEO_MODEL', ''), CINAI_FREE_VIDEO_MODELS)
CINAI_VIDEO_MODE = (os.getenv('CINAI_VIDEO_MODE', 't2v') or 't2v').strip().lower()
CINAI_VIDEO_ASPECT_RATIO = (
    os.getenv('CINAI_VIDEO_ASPECT_RATIO', '16:9') or '16:9'
).strip()
CINAI_VIDEO_RESOLUTION = (
    os.getenv('CINAI_VIDEO_RESOLUTION', '') or ''
).strip()          # пусто — разрешение модели по умолчанию (у бесплатных его не выбрать)
try:
    CINAI_VIDEO_DURATION = max(3, int(os.getenv('CINAI_VIDEO_DURATION', '5') or '5'))
except ValueError:
    CINAI_VIDEO_DURATION = 5
CINAI_VIDEO_AUDIO = os.getenv('CINAI_VIDEO_AUDIO', '').strip().lower()
if CINAI_VIDEO_AUDIO not in ('', 'true', 'false', '1', '0', 'yes', 'no'):
    CINAI_VIDEO_AUDIO = ''
# Воркеры CinAI VideoGen. Раньше переменной в config не было вовсе: GUI писал
# env, но config его не читал — спинбокс Parallel молча игнорировался
# (стадия всегда работала min(video_parallel, 6)). Дефолт 6 = как у
# image-стороны и задокументированного max_concurrent_executions аккаунта.
try:
    CINAI_VIDEO_MAX_PARALLEL = max(
        1, min(6, int(os.getenv('CINAI_VIDEO_MAX_PARALLEL', '6') or '6'))
    )
except ValueError:
    CINAI_VIDEO_MAX_PARALLEL = 6
# Общий бюджет одновременных executions НА АККАУНТ (getBillingBalance:
# max_concurrent_executions=6). Семафор в cinai_workflow держит суммарный
# параллелизм всех стадий (инфографика + ImageGen + цепочка) в этом лимите.
try:
    CINAI_MAX_CONCURRENT_EXECUTIONS = max(
        1, min(6, int(os.getenv('CINAI_MAX_CONCURRENT_EXECUTIONS', '6') or '6'))
    )
except ValueError:
    CINAI_MAX_CONCURRENT_EXECUTIONS = 6
try:
    CINAI_VIDEO_MAX_RETRIES = max(
        0, int(os.getenv('CINAI_VIDEO_MAX_RETRIES', '3') or '3')
    )
except ValueError:
    CINAI_VIDEO_MAX_RETRIES = 3
try:
    CINAI_VIDEO_RETRY_BASE_SECONDS = max(
        0.0, float(os.getenv('CINAI_VIDEO_RETRY_BASE_SECONDS', '5') or '5')
    )
except ValueError:
    CINAI_VIDEO_RETRY_BASE_SECONDS = 5.0
try:
    CINAI_VIDEO_TIMEOUT = max(
        60, int(os.getenv('CINAI_VIDEO_TIMEOUT', '1200') or '1200')
    )
except ValueError:
    CINAI_VIDEO_TIMEOUT = 1200
try:
    CINAI_TOKEN_REFRESH_SKEW_SECONDS = max(
        0, int(os.getenv('CINAI_TOKEN_REFRESH_SKEW_SECONDS', '300') or '300')
    )
except ValueError:
    CINAI_TOKEN_REFRESH_SKEW_SECONDS = 300

# Превентивное продление сессии CinAI из GUI: если access живёт меньше этого
# срока — обновляем заранее, чтобы refresh не умер от простоя (он протухает,
# если им долго не пользоваться, и тогда нужен ручной вход).
try:
    CINAI_TOKEN_PREFRESH_HOURS = float(
        os.getenv('CINAI_TOKEN_PREFRESH_HOURS', '12') or '12')
except ValueError:
    CINAI_TOKEN_PREFRESH_HOURS = 12.0
try:
    CINAI_VIDEO_MIN_BYTES = max(
        1024, int(os.getenv('CINAI_VIDEO_MIN_BYTES', '32768') or '32768')
    )
except ValueError:
    CINAI_VIDEO_MIN_BYTES = 32768
try:
    CINAI_VIDEO_INFLIGHT_DRAIN_SECONDS = max(
        0, int(os.getenv('CINAI_VIDEO_INFLIGHT_DRAIN_SECONDS', '120') or '120')
    )
except ValueError:
    CINAI_VIDEO_INFLIGHT_DRAIN_SECONDS = 120
try:
    CINAI_VIDEO_INFLIGHT_ACTIVE_GRACE_SECONDS = max(
        15,
        int(os.getenv('CINAI_VIDEO_INFLIGHT_ACTIVE_GRACE_SECONDS', '120') or '120')
    )
except ValueError:
    CINAI_VIDEO_INFLIGHT_ACTIVE_GRACE_SECONDS = 120

# Переходы между клипами (dip-to-X) + Ken Burns zoom-стиль — опции video_concat (дефолт выкл/старое)
PIPELINE_TRANSITIONS  = (os.getenv('PIPELINE_TRANSITIONS', '') or 'false').lower() in ('true', '1', 'yes')
PIPELINE_TRANSITION_DURATION = float(os.getenv('PIPELINE_TRANSITION_DURATION', '') or 0.5)
PIPELINE_TRANSITION_STYLES   = os.getenv('PIPELINE_TRANSITION_STYLES', '') or 'fade'
PIPELINE_ZOOM_STYLES  = os.getenv('PIPELINE_ZOOM_STYLES', '') or 'in-center'
# Оставить ретайминг файлы: true | false
PIPELINE_KEEP_FILES   = (os.getenv('PIPELINE_KEEP_FILES', '') or 'false').lower() in ('true', '1', 'yes')
# Оставить оригинальный звук из Veo-видео: true | false
PIPELINE_KEEP_VEO_SOUND = (os.getenv('PIPELINE_KEEP_VEO_SOUND', '') or 'false').lower() in ('true', '1', 'yes')
# Очередь склейки: хвост пайплайна (музыка+concat) уходит в фоновую последовательную
# очередь (Control Panel), оркестратор сразу берёт следующий файл. false = старый
# inline-путь (склейка блокирует очередь генерации до завершения).
CONCAT_QUEUE_ENABLED  = (os.getenv('CONCAT_QUEUE_ENABLED', '') or 'true').lower() in ('true', '1', 'yes')

# ============================================================================
# REAL PHOTO STAGE (archival photo/video insertion)
# ============================================================================
# Replace AI-generated imagery with real archival material from PD/CC sources
# (Wikimedia / NASA / Library of Congress / Internet Archive) for selected clips.
# Stage runs between Director and ImgGen. Produces vid_img/{id}.mp4 directly,
# so ImgGen and Veo i2v skip these clips automatically.

REAL_PHOTO_ENABLED       = os.getenv('REAL_PHOTO_ENABLED',       'false').lower() in ('true', '1', 'yes')
REAL_PHOTO_RATIO_TARGET  = float(os.getenv('REAL_PHOTO_RATIO_TARGET', '0.40'))   # target % of clips to replace
REAL_PHOTO_RATIO_MIN     = float(os.getenv('REAL_PHOTO_RATIO_MIN',    '0.30'))   # hard floor
REAL_PHOTO_RATIO_MAX     = float(os.getenv('REAL_PHOTO_RATIO_MAX',    '0.55'))   # hard ceiling
# Optional automatic EPF sidecar: Real Photo supplies its selected clip queries
# to Extreme Picture Finder. EPF downloads candidates, then a vision model
# chooses one image per clip before the bridge imports it into generated/.
REAL_PHOTO_EPF_BRIDGE = os.getenv('REAL_PHOTO_EPF_BRIDGE', '0').lower() in ('true', '1', 'yes')
REAL_PHOTO_EPF_VISIBLE = os.getenv('REAL_PHOTO_EPF_VISIBLE', '0').lower() in ('true', '1', 'yes')
REAL_PHOTO_EPF_EXE = os.getenv('REAL_PHOTO_EPF_EXE', '').strip()
REAL_PHOTO_EPF_DATA_DIR = os.getenv('REAL_PHOTO_EPF_DATA_DIR', '').strip()
REAL_PHOTO_EPF_ENGINES = os.getenv('REAL_PHOTO_EPF_ENGINES', 'google,bing').strip()
REAL_PHOTO_EPF_CANDIDATES = max(1, int(os.getenv('REAL_PHOTO_EPF_CANDIDATES', '12') or '12'))
# Вижн-отбор АРХИВНОГО пути (Wikimedia/NASA/LoC/…; 25.08): сколько кандидатов
# из выдачи источника скачать и показать вижну одним контактным листом.
# 0 = как раньше — первый скачавшийся кандидат без просмотра кадра.
REAL_PHOTO_VISION_CANDIDATES = max(0, int(os.getenv('REAL_PHOTO_VISION_CANDIDATES', '4') or 4))
REAL_PHOTO_EPF_SEARCH_TIMEOUT = max(
    30, int(os.getenv('REAL_PHOTO_EPF_SEARCH_TIMEOUT', '180') or '180')
)
REAL_PHOTO_VISION_PROVIDER = (
    os.getenv('REAL_PHOTO_VISION_PROVIDER', 'codex') or 'codex'
).strip().lower()
REAL_PHOTO_VISION_MODEL = _model('REAL_PHOTO_VISION_MODEL', '')
if REAL_PHOTO_VISION_PROVIDER == 'claude_api':
    REAL_PHOTO_VISION_MODEL = _normalize_claude_nexus_model(
        REAL_PHOTO_VISION_MODEL)
# Архив-фото легитимны только с этой секунды ролика. Клипы раньше неё (вступительный хук —
# динамичные атмосферные кадры) режиссёр часто ошибочно метит под архив, потому что интро
# насыщено топонимами. Гейт снимает маркер с таких клипов → они идут в обычную генерацию.
# 0 = выключено (метить с самого начала). Дефолт 15 ≈ зона «динамичного вступления» Pass1.
REAL_PHOTO_START_SEC     = float(os.getenv('REAL_PHOTO_START_SEC',    '15'))
# Глубина выдачи АРХИВНЫХ источников (wikimedia/nasa/loc/met/getty/europeana/smithsonian/archive):
# сколько записей/статей копнуть в каждом, прежде чем перейти к следующему источнику каскада.
# Для wikimedia это число вики-СТАТЕЙ (берём pageimage каждой, дедуп по странице → «иная статья»).
# Yandex — веб-хранилище картинок, глубину «статей» к нему не применяем (берёт топ релевантных).
REAL_PHOTO_SEARCH_DEPTH  = max(1, int(os.getenv('REAL_PHOTO_SEARCH_DEPTH', '6') or '6'))

# Comma-separated source list; order = priority.
# Allowed: wikimedia, nasa, loc, met, getty, europeana, smithsonian, rijksmuseum, archive
# - wikimedia, nasa, loc, met, getty — no API key needed
# - europeana                        — free key from https://pro.europeana.eu/page/get-api
# - smithsonian                      — free key from https://api.data.gov/signup/
# - rijksmuseum                      — FROZEN (legacy API requires key, new API needs Linked Art rewrite)
# - archive                          — Internet Archive (archive.org), no key needed
# - openverse                        — Openverse (CC-фото Flickr/Commons/музеи), без ключа 200 запросов/день;
#                                      с OPENVERSE_CLIENT_ID/SECRET — больше (см. api.openverse.org)
# - serper                           — Google Images через serper.dev (ключ SERPER_API_KEY, 1 кредит/запрос);
#                                      веб-картинки без лицензионных метаданных — как yandex
# - epf                             — Extreme Picture Finder (локальный поисковик картинок).
#                                      С 25.08 он такой же источник каскада: его позиция в
#                                      списке = приоритет. Работает, только если включён
#                                      REAL_PHOTO_EPF_BRIDGE (галка ставит и то, и другое).
_real_photo_sources_raw  = os.getenv('REAL_PHOTO_SOURCES', 'wikimedia,yandex,nasa,loc,met,getty,europeana,smithsonian,archive')
REAL_PHOTO_SOURCES       = [s.strip() for s in _real_photo_sources_raw.split(',') if s.strip()]
# Совместимость со старыми .env: раньше EPF шёл ОТДЕЛЬНЫМ проходом впереди всех и в
# списке источников его не было. Если мост включён, а в списке epf нет — ставим первым,
# чтобы обновление не поменяло приоритет молча.
if REAL_PHOTO_EPF_BRIDGE and 'epf' not in REAL_PHOTO_SOURCES:
    REAL_PHOTO_SOURCES.insert(0, 'epf')
# Микс источников: кандидаты со ВСЕХ включённых источников идут вижну одним контактным
# листом (он сравнивает их между собой), вместо каскада «первый подошедший побеждает».
REAL_PHOTO_SOURCE_MIX    = os.getenv('REAL_PHOTO_SOURCE_MIX', '1').lower() in ('true', '1', 'yes')
# Вижн стока: свой выбор в GUI; пусто = «как у Real Photo» (REAL_PHOTO_VISION_*).
STOCK_VISION_PROVIDER    = os.getenv('STOCK_VISION_PROVIDER', '').strip()
STOCK_VISION_MODEL       = os.getenv('STOCK_VISION_MODEL', '').strip()
# Потолок кандидатов на общем листе микса (сливаются по кругу от каждого источника).
REAL_PHOTO_MIX_MAX_CANDIDATES = max(2, int(os.getenv('REAL_PHOTO_MIX_MAX_CANDIDATES', '16') or 16))
# «Без стоковых фото»: минус-слова фотостоков в поисковую фразу EPF + отбрасывание
# скачанного со стоковых доменов (домен берётся из файла состояния EPF). Выключено —
# полагаемся только на вижн (он видит водяной знак на кадре).
REAL_PHOTO_NO_STOCK_PHOTOS = os.getenv('REAL_PHOTO_NO_STOCK_PHOTOS', '1').lower() in ('true', '1', 'yes')
# Товарные карточки маркетплейсов: у них нет водяного знака, поэтому вижн их
# пропускает — отсекаем по домену ДО выдачи (улов живого прогона 01.09).
REAL_PHOTO_NO_MARKETPLACES = os.getenv('REAL_PHOTO_NO_MARKETPLACES', '1').lower() in ('true', '1', 'yes')
# Yandex Картинки: минус-флаги исключения AI/3D-генераций из выдачи (гравюры/карты/схемы НЕ режем)
REAL_PHOTO_YANDEX_EXCLUDE = os.getenv('REAL_PHOTO_YANDEX_EXCLUDE', '') or '-нейросеть -нейро -ии -ai -midjourney -render -3d -рендер'

# Optional API keys for extra sources (leave blank if not registered)
EUROPEANA_API_KEY        = os.getenv('EUROPEANA_API_KEY',        '').strip()
SMITHSONIAN_API_KEY      = os.getenv('SMITHSONIAN_API_KEY',      '').strip()
RIJKSMUSEUM_API_KEY      = os.getenv('RIJKSMUSEUM_API_KEY',      '').strip()
SERPER_API_KEY           = os.getenv('SERPER_API_KEY',           '').strip()   # serper.dev — Google Images
# Bright Data — тот же поиск картинок Google, но через их шлюз. Второй вход
# нужен потому, что модуль EPF работает только на Windows (это .exe), и на маке
# архивная ветка оставалась без Google вовсе.
BRIGHTDATA_API_TOKEN     = os.getenv('BRIGHTDATA_API_TOKEN',     '').strip()
# Пусто = имя зоны спросим у самой Bright Data по токену (/zone/get_active_zones,
# берётся зона типа serp). Раньше здесь стояло запасное имя зоны РАЗРАБОТЧИКА —
# у любого другого человека запрос уходил бы в несуществующую зону и источник
# молча не отдавал бы ничего.
BRIGHTDATA_ZONE          = os.getenv('BRIGHTDATA_ZONE',          '').strip()
OPENVERSE_CLIENT_ID      = os.getenv('OPENVERSE_CLIENT_ID',      '').strip()   # пусто = анонимно (200/день)
OPENVERSE_CLIENT_SECRET  = os.getenv('OPENVERSE_CLIENT_SECRET',  '').strip()

# Codex model for selection + keyframe pick + fallback queries.
# Real Photo stage prefers gpt-5.5 (better at structured output and rejecting weak candidates).
# Override in .env if needed; leave the env var blank to inherit the global GPT_MODEL.
REAL_PHOTO_CODEX_MODEL   = os.getenv('REAL_PHOTO_CODEX_MODEL',   'gpt-5.5').strip()
# Оператор промптажа Real Photo (генерация search_query + альтернатив при 0 hits).
# НЕ зависит от выбранного режиссёра — задаётся отдельно в GUI/env.
#   codex      — Codex CLI (агент, пишет файлы; падает если CLI не установлен/не авторизован)
#   deepseek | openrouter | meta_muse — прямой chat API
# chat-провайдеры надёжнее (inline, без агента и файловой системы).
REAL_PHOTO_PROMPT_PROVIDER = (os.getenv('REAL_PHOTO_PROMPT_PROVIDER', '') or 'codex').strip().lower()
# Модель промптера. Пусто → дефолт выбранного провайдера (DEEPSEEK_MODEL / OPENROUTER_MODEL / ...).
REAL_PHOTO_PROMPT_MODEL    = os.getenv('REAL_PHOTO_PROMPT_MODEL', '').strip()
if REAL_PHOTO_PROMPT_PROVIDER == 'claude_api':
    REAL_PHOTO_PROMPT_MODEL = _normalize_claude_nexus_model(
        REAL_PHOTO_PROMPT_MODEL)
# Cache lives inside each project at <project>/real_photo_cache/. No global limit needed —
# caches are bounded by project lifetime and disposed when project is deleted.

# Fallback-страховка: Pass 2 пишет промпты ДЛЯ ВСЕХ клипов (включая real_photo_pending).
# Если real_photo не нашёл/не скачал/завис — клип догенерится нейросетью по готовому
# промпту (картинкой, без анимации). 1=вкл (дефолт). 0=старое поведение (Pass2 скип).
REAL_PHOTO_PROMPT_FALLBACK = (os.getenv('REAL_PHOTO_PROMPT_FALLBACK', '') or '1').strip() not in ('0', 'false', 'no', '')
# Общий дедлайн фоновой Real Photo стадии с момента старта (сек).
REAL_PHOTO_WAIT_TIMEOUT = int(os.getenv('REAL_PHOTO_WAIT_TIMEOUT', '1800'))
# После готовности основной генерации даём Real Photo отдельную фору, не посылая stop.
# По её истечении остающиеся pending-клипы снимаются и идут в AI fallback.
REAL_PHOTO_PIPELINE_WAIT_TIMEOUT = int(
    os.getenv('REAL_PHOTO_PIPELINE_WAIT_TIMEOUT', '100')
)

# ============================================================================
# INFOGRAPHIC STAGE (AI stills routed separately from normal image generation)
# ============================================================================
NON_AI_IMAGE_MODE = os.getenv('NON_AI_IMAGE_MODE', 'false').lower() in ('true', '1', 'yes', 'on')
NON_AI_REAL_PHOTO_PERCENT = max(
    0, min(100, int(float(os.getenv('NON_AI_REAL_PHOTO_PERCENT', '50') or '50')))
)
NON_AI_INFOGRAPHIC_PERCENT = max(
    0, min(100, int(float(os.getenv('NON_AI_INFOGRAPHIC_PERCENT', '25') or '25')))
)
NON_AI_STOCK_PERCENT = max(
    0, min(100, int(float(os.getenv('NON_AI_STOCK_PERCENT', '25') or '25')))
)
NON_AI_YOUTUBE_PERCENT = max(
    0, min(100, int(float(os.getenv('NON_AI_YOUTUBE_PERCENT', '25') or '25')))
)

INFOGRAPHIC_ENABLED = os.getenv('INFOGRAPHIC_ENABLED', 'false').lower() in ('true', '1', 'yes')
INFOGRAPHIC_RATIO_TARGET = max(0.0, min(1.0, float(os.getenv('INFOGRAPHIC_RATIO_TARGET', '0.15'))))
# С какой секунды ролика допустима инфографика (см. STOCK_START_SEC)
INFOGRAPHIC_START_SEC = float(os.getenv('INFOGRAPHIC_START_SEC', '0') or '0')
INFOGRAPHIC_RATIO_MAX = max(
    INFOGRAPHIC_RATIO_TARGET,
    min(1.0, float(os.getenv('INFOGRAPHIC_RATIO_MAX', '0.25'))),
)
# Separate image route: fastgen | veononstop. This lets normal artwork and
# information graphics use different models in one project.
INFOGRAPHIC_IMG_PROVIDER = (os.getenv('INFOGRAPHIC_IMG_PROVIDER', 'fastgen') or 'fastgen').strip().lower()
if INFOGRAPHIC_IMG_PROVIDER not in ('fastgen', 'veononstop', 'vertex', 'cheapvibe', 'varag', 'cinai'):
    INFOGRAPHIC_IMG_PROVIDER = 'fastgen'
# Свои модели инфографики у Vertex и CheapVibeCode (ключи — общие, из «Настроек»)
INFOGRAPHIC_VERTEX_MODEL = (os.getenv('INFOGRAPHIC_VERTEX_MODEL', '') or 'gemini-3.1-flash-lite-image').strip()
INFOGRAPHIC_CHEAPVIBE_MODEL = (os.getenv('INFOGRAPHIC_CHEAPVIBE_MODEL', '') or 'gpt-image-2').strip()
INFOGRAPHIC_CHEAPVIBE_SIZE = (os.getenv('INFOGRAPHIC_CHEAPVIBE_SIZE', '') or '1536x1024').strip()
INFOGRAPHIC_CHEAPVIBE_QUALITY = (os.getenv('INFOGRAPHIC_CHEAPVIBE_QUALITY', '') or 'medium').strip().lower()
INFOGRAPHIC_FASTGEN_MODEL = (os.getenv('INFOGRAPHIC_FASTGEN_MODEL', 'NARWHAL') or 'NARWHAL').strip()
INFOGRAPHIC_CINAI_MODEL = _cinai_free(
    os.getenv('INFOGRAPHIC_CINAI_MODEL', CINAI_MODEL) or CINAI_MODEL, CINAI_FREE_IMAGE_MODELS)
INFOGRAPHIC_CINAI_ASPECT_RATIO = (
    os.getenv('INFOGRAPHIC_CINAI_ASPECT_RATIO', CINAI_ASPECT_RATIO)
    or CINAI_ASPECT_RATIO
).strip()
INFOGRAPHIC_CINAI_QUALITY = (
    os.getenv('INFOGRAPHIC_CINAI_QUALITY', CINAI_QUALITY) or CINAI_QUALITY
).strip().upper()
if INFOGRAPHIC_CINAI_QUALITY not in ('1K', '2K', '4K'):
    INFOGRAPHIC_CINAI_QUALITY = CINAI_QUALITY
# У VeoNonStop инфографика рисуется только Banana (режима Imagen больше нет)
INFOGRAPHIC_VEONONSTOP_BANANA_MODEL = (
    os.getenv('INFOGRAPHIC_VEONONSTOP_BANANA_MODEL', 'GEM_PIX_2') or 'GEM_PIX_2'
).strip()
# false: normal slideshow movement remains available. true: infographic
# stills are rendered at a fixed frame for the full clip duration.
INFOGRAPHIC_STATIC_CONCAT = os.getenv('INFOGRAPHIC_STATIC_CONCAT', 'true').lower() in ('true', '1', 'yes')

# ============================================================================
# MOTION GRAPHICS (Remotion + Codex)
# ============================================================================
# Режиссёр размечает клипы `source: motion_graphics_pending`, Codex пишет на них
# сцену Remotion, стадия рендерит её в vid_img/{id:03d}.mp4. Не вышло — клип
# уходит в обычную нейрогенерацию (Pre-concat fallback). См. units/mograph_runner.py.
MOTION_GRAPHICS_ENABLED = os.getenv('MOTION_GRAPHICS_ENABLED', 'false').lower() in ('true', '1', 'yes')
MOTION_GRAPHICS_RATIO_TARGET = max(0.0, min(1.0, float(os.getenv('MOTION_GRAPHICS_RATIO_TARGET', '0.10') or '0.10')))
# С какой секунды ролика допустима графика (см. STOCK_START_SEC)
MOTION_GRAPHICS_START_SEC = float(os.getenv('MOTION_GRAPHICS_START_SEC', '0') or '0')
# Модель Codex, которая пишет сцены; пусто — та же, что у режиссёра
MOTION_GRAPHICS_MODEL = (os.getenv('MOTION_GRAPHICS_MODEL', '') or '').strip()
# Сколько клипов готовить одновременно (каждый — отдельный вызов Codex + рендер)
MOTION_GRAPHICS_PARALLEL = max(1, min(8, int(os.getenv('MOTION_GRAPHICS_PARALLEL', '2') or '2')))
# Таймаут одного вызова Codex на сцену, секунды
MOTION_GRAPHICS_CODEX_TIMEOUT = int(os.getenv('MOTION_GRAPHICS_CODEX_TIMEOUT', '900') or '900')
# Сколько раз отдавать Codex ошибку рендера на исправление
MOTION_GRAPHICS_FIX_ATTEMPTS = max(0, int(os.getenv('MOTION_GRAPHICS_FIX_ATTEMPTS', '2') or '2'))
# Таймаут рендера одного клипа, секунды
MOTION_GRAPHICS_RENDER_TIMEOUT = int(os.getenv('MOTION_GRAPHICS_RENDER_TIMEOUT', '600') or '600')

# ============================================================================
# CHAIN MODE (seamless video continuation)
# ============================================================================
# Story mode: off=independent clips, batch=morph between imggen pairs,
#             morph=one big chain (all extends), chain=LLM-decided chains.
# Сюжетный режим из программы убран: клипы всегда независимые, значение из .env
# не учитывается.
STORY_MODE = 'off'

# Gen images only for chain heads (skip imggen for extends) — saves ~85% imggen
CHAIN_IMGGEN_HEADS_ONLY = os.getenv('CHAIN_IMGGEN_HEADS_ONLY', '1').lower() in ('true', '1', 'yes')

# Seconds from end to extract last frame (ffmpeg -sseof offset)
VIDEO_EXTEND_FRAME_OFFSET = float(os.getenv('VIDEO_EXTEND_FRAME_OFFSET', '0.5'))

# assemble.py: skip N first frames of extend clips (fps-independent via select filter)
VIDEO_EXTEND_TRIM_FRAMES = int(os.getenv('VIDEO_EXTEND_TRIM_FRAMES', '1'))

# Mute audio for extend clips (VEO sound artifacts on each extend start)
VIDEO_EXTEND_MUTE_AUDIO = os.getenv('VIDEO_EXTEND_MUTE_AUDIO', '1').lower() in ('true', '1', 'yes')

# Soft safety net: max clips in one chain before forced break (0=unlimited)
VIDEO_EXTEND_MAX_CHAIN = int(os.getenv('VIDEO_EXTEND_MAX_CHAIN', '10'))

# Cascade recovery: regen prev clip on fail instead of immediate force break
CHAIN_CASCADE_RECOVERY = os.getenv('CHAIN_CASCADE_RECOVERY', '1').lower() in ('true', '1', 'yes')

# Max fails of same clip before forced sub-chain head (prevents infinite loop)
CHAIN_MAX_SAME_CLIP_FAILS = int(os.getenv('CHAIN_MAX_SAME_CLIP_FAILS', '3'))

# ============================================================================
# ПРОВЕРКА КЛЮЧЕЙ
# ============================================================================

# ============================================================================
# СОЗДАНИЕ ДИРЕКТОРИЙ
# ============================================================================

