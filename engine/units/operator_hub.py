"""Единый текстовый вызов оператора для стадий (25.08).

Задача «напиши текст по описанию» — поисковый запрос ютуба, запрос стока,
подсказка для музыки — не зависит от того, каким транспортом ходит оператор.
Раньше стадии умели только `chat_completion`, то есть HTTP chat API, и всё, что
работает CLI-агентом по файлам (Codex, Claude Code), для них не существовало:
провайдер не проходил проверку `CHAT_PROVIDERS`, пара выходила пустой, и стадия
молча уходила в механический фолбэк. Живой прогон 25.08: планировщик запросов
ютуба ни разу не вызвал модель и резал описание на куски по семь слов.

Здесь оба транспорта за одной дверью:
    ask_text(provider, model, prompt, work_dir=…)  -> str
    resolve_text_operator(stage)                   -> (provider, model)

`resolve_text_operator` берёт настройку стадии, затем оператора режиссёра —
любого, включая codex/claude_code, — и только если и там пусто, возвращает
пустую пару, о чём стадия обязана громко сказать в лог.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Операторы, живущие CLI-агентом по файлам, а не HTTP chat API.
CLI_AGENTS = ("codex", "claude_code", "claude")

# Псевдонимы имён операторов → канон.
#
# `gpt` — как называется бэкенд режиссёра (DIRECTOR_BACKEND=gpt), но транспорт
# у него тот же Codex CLI. Стоковый промптер отдавал сюда именно `gpt`, оно не
# попадало в CLI_AGENTS, уезжало в ветку chat API и падало с «Неизвестный
# chat-провайдер: gpt». Итог живого прогона 01.09.2026: запросами к стоку
# становились куски озвучки, сток вернул пусто, 5 клипов из 6 ушли в
# нейрогенерацию.
#
# Нормализация стоит ЗДЕСЬ, в общей точке: `ask_text` зовут пять модулей
# (сток, визуальная история, ютуб, наложение сущностей, real photo), и любой
# из них мог прислать псевдоним.
PROVIDER_ALIASES = {
    "gpt": "codex",
    "openai": "codex",
    "codex_cli": "codex",
    "claude_cli": "claude_code",
}


def normalize_provider(provider: str) -> str:
    """Имя оператора к канону: `gpt` → `codex` и т.п."""
    name = (provider or "").strip().lower()
    return PROVIDER_ALIASES.get(name, name)

_MODEL_KEYS = {
    "deepseek": "DEEPSEEK_MODEL",
    "openrouter": "OPENROUTER_MODEL",
    "meta_muse": "META_MUSE_MODEL",
    "claude_api": "CLAUDE_API_MODEL",
    "codex_reseller": "CODEX_RESELLER_MODEL",
    "gpt_nexus": "GPT_NEXUS_MODEL",
    "codex": "GPT_MODEL",
    "claude_code": "CLAUDE_CODE_MODEL",
}


def _model_for(provider: str) -> str:
    import params as config
    key = _MODEL_KEYS.get(provider)
    return str(getattr(config, key, "") or "").strip() if key else ""


def schema_is_control(schema_file: str) -> bool:
    """True, если схема — ОТЧЁТ о записанном файле, а не сам ответ.

    Контрольная схема (`*_control.json`) описывает `status` / `result_path` /
    `item_count`: модель пишет результат файлом САМА, а последним сообщением
    только отчитывается. Схема ответа (`stage_queries.json`) описывает сам
    результат — его забирает `--output-last-message`, и просить модель ещё и
    писать файл руками нельзя (см. `ask_text`).

    Схему не прочитали — считаем контрольной: это прежнее поведение, файл всё
    равно будет записан, и стадия не останется без ответа.
    """
    name = (schema_file or "").strip()
    if not name:
        return True
    schemas_dir = Path(__file__).parent.parent / "schemas"
    try:
        data = json.loads((schemas_dir / name).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        logger.warning("   [оператор] схема %s не прочитана (%s) — считаю контрольной",
                       name, exc)
        return True
    props = data.get("properties") or {}
    return "result_path" in props or "status" in props


def resolve_text_operator(stage: str = "") -> tuple[str, str]:
    """(провайдер, модель) для текстовой задачи стадии.

    Порядок: настройка самой стадии → оператор режиссёра (в т.ч. Codex) →
    промптер Real Photo как последний рабочий вариант. «auto» и пустое значение
    настройкой не считаются."""
    import params as config
    def _clean(value) -> str:
        value = str(value or "").strip().lower()
        return "" if value in ("", "auto", "none", "off") else value

    stage_keys = {
        "youtube": ("YOUTUBE_QUERY_PROVIDER", "YOUTUBE_QUERY_MODEL"),
        "stock": ("STOCK_PROMPT_PROVIDER", "STOCK_PROMPT_MODEL"),
        "music": ("MUSIC_PROMPT_PROVIDER", "MUSIC_PROMPT_MODEL"),
        "real_photo": ("REAL_PHOTO_PROMPT_PROVIDER", "REAL_PHOTO_PROMPT_MODEL"),
        "style_extract": ("STYLE_EXTRACT_BACKEND", "STYLE_EXTRACT_MODEL"),
    }
    candidates: list[tuple[str, str]] = []
    provider_key, model_key = stage_keys.get((stage or "").lower(), ("", ""))
    if provider_key:
        candidates.append((_clean(getattr(config, provider_key, "")),
                           str(getattr(config, model_key, "") or "").strip()))
    # Оператор режиссёра. DIRECTOR_BACKEND=gpt + GPT_PROVIDER=openai — это Codex
    # CLI, то есть GPT-подписка: у неё нет HTTP-эндпоинта, и раньше стадии её
    # просто не видели («не chat-провайдер») и уходили в фолбэк.
    backend = _clean(getattr(config, "DIRECTOR_BACKEND", ""))
    director = _clean(getattr(config, "GPT_PROVIDER", "")) if backend == "gpt" else backend
    if backend == "gpt" and director in ("", "openai", "codex", "gpt"):
        director = "codex"
    candidates.append((director, ""))
    candidates.append((_clean(getattr(config, "REAL_PHOTO_PROMPT_PROVIDER", "")),
                       str(getattr(config, "REAL_PHOTO_PROMPT_MODEL", "") or "").strip()))
    for provider, model in candidates:
        if not provider:
            continue
        model = model or _model_for(provider)
        if provider in CLI_AGENTS or model:
            return provider, model or _model_for(provider)
    return "", ""


def ask_text(
    provider: str,
    model: str,
    prompt: str,
    *,
    work_dir: Optional[Path] = None,
    phase: str = "text",
    schema_file: str = "",
    json_mode: bool = True,
    max_tokens: int = 400,
    timeout: int = 180,
) -> str:
    """Спросить текст у оператора любым транспортом. Исключения не глушит."""
    raw_provider = (provider or "").strip().lower()
    provider = normalize_provider(raw_provider)
    if not provider:
        raise RuntimeError("text operator is not configured")
    if provider != raw_provider:
        logger.debug("   [оператор/%s] псевдоним %r → %r", phase, raw_provider, provider)
    transport = "CLI-агент" if provider in CLI_AGENTS else "chat API"
    logger.info("   [оператор/%s] %s/%s (%s) ← задача %s символов",
                phase, provider, model or "—", transport, len(prompt or ""))

    if provider in CLI_AGENTS:
        # CLI-агент пишет ответ в файл — тот же приём, что у этапов Auto Visual
        # Story и у промптера Real Photo.
        work = Path(work_dir or Path.cwd())
        work.mkdir(parents=True, exist_ok=True)
        result_file = work / f"{phase}_result.json"
        # Фаза бывает составной: Real Photo передаёт «archive-query/48», и путь
        # получает ПОДПАПКУ. Раньше создавалась только `work`, поэтому CLI-агент
        # падал на записи ответа: «Failed to write last message file … (os error
        # 3)» — папки archive-query не существовало. Питон это переживал молча
        # (unlink на несуществующем пути), а запрос оставался ненаписанным, и
        # стадия уходила на аварийное сжатие описания (жалоба 29.08).
        #
        # У chat-провайдеров бага не было вовсе: они отдают ответ сообщением и
        # файл не пишут — поэтому проблема видна только там, где оператор codex
        # или claude_code.
        result_file.parent.mkdir(parents=True, exist_ok=True)
        result_file.unlink(missing_ok=True)
        is_claude = provider in ("claude_code", "claude")
        # Просить «запиши файл и НЕ печатай ответ» можно только там, где ответ
        # иначе не забрать: у Claude Code (он отдаёт лишь текст сессии) и при
        # контрольной схеме, которая описывает отчёт, а не результат.
        #
        # Для Codex со схемой ОТВЕТА такая приписка — прямое противоречие:
        # `--output-schema` требует напечатать результат, `--output-last-message`
        # кладёт напечатанное в ТОТ ЖЕ файл, а текст запрещает печатать. Модель
        # тратит ход на лишнюю запись файла и хуже читает саму задачу. Замер на
        # реальной задаче клипа 4 (26.08): с припиской 30.0с и запросы про
        # выдуманную деревню «Brzegi», без неё 19.7с и запросы по смыслу сцены.
        needs_file_instruction = is_claude or schema_is_control(schema_file)
        task = prompt
        if needs_file_instruction:
            task += (
                "\n\nWrite the result as a single JSON file to this exact path:\n"
                + result_file.resolve().as_posix()
                + "\nWrite nothing else and do not print the answer."
            )
        else:
            # Раньше здесь было «файл писать не прошу», и строка читалась так,
            # будто файл вообще не нужен — а через секунду прогон падал на
            # записи этого самого файла. Речь только об ИНСТРУКЦИИ в промпте:
            # файл всё равно пишется, его пишет сам CLI (флаг «сохранить
            # последнее сообщение»), и ответ читается оттуда.
            logger.info("   [оператор/%s] схема %s описывает ответ — беру его из "
                        "последнего сообщения агента (файл пишет сам CLI)",
                        phase, schema_file)
        if is_claude:
            from units.claudecli_runner import ClaudeCodeRunner

            task_file = work / f"_{phase}_task.md"
            task_file.write_text(task, encoding="utf-8")
            runner = ClaudeCodeRunner(model_name=model or None, timeout=timeout)
            code, out, err, _ = runner.run_task_file(task_file, work, label=phase)
            if code != 0:
                raise RuntimeError(f"claude_code failed ({code}): {(err or out)[-300:]}")
        else:
            from units.archive_photos import CodexRunner

            runner = CodexRunner(model_name=model or None, timeout=timeout)
            # Codex работает по схеме вывода: без неё он волен ответить чем угодно.
            runner.run(task, schema_file or "visual_story_file_control.json",
                       result_file, work)
        if not result_file.exists():
            raise RuntimeError(f"{provider}: result file was not created")
        answer = result_file.read_text(encoding="utf-8-sig", errors="replace")
        logger.info("   [оператор/%s] ответ %s символов (файл %s)",
                    phase, len(answer), result_file.name)
        return answer

    from units.llm_gateway import chat_completion

    answer = chat_completion(
        provider, model, [{"role": "user", "content": prompt}],
        max_tokens=max_tokens, json_mode=json_mode, timeout=timeout,
    )
    logger.info("   [оператор/%s] ответ %s символов", phase, len(answer or ""))
    return answer


def ask_json(provider: str, model: str, prompt: str, **kwargs) -> dict:
    """`ask_text` + разбор JSON-объекта из ответа."""
    from units.tube_runner import _parse_json_object

    raw = ask_text(provider, model, prompt, **kwargs)
    try:
        return _parse_json_object(raw)
    except Exception:  # noqa: BLE001
        return json.loads(raw)
