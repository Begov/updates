"""Единый текстовый вызов оператора для стадий.

Оператор один — Codex CLI (подписка ChatGPT): он работает агентом и пишет ответ
файлом по схеме. Стадии зовут его через

    ask_text(provider, model, prompt, work_dir=…)  -> str
    resolve_text_operator(stage)                   -> (provider, model)

`resolve_text_operator` берёт модель из настройки стадии, иначе — модель режиссёра.
Имена провайдеров из старых настроек (`gpt`, `openai`, `auto` и т.п.) сводятся к Codex.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Операторы, живущие CLI-агентом по файлам. В программе такой один — Codex.
CLI_AGENTS = ("codex",)


def normalize_provider(provider: str) -> str:
    """Имя оператора к канону. Транспорт один, поэтому любое имя — это Codex."""
    return "codex"


def _model_for(provider: str = "codex") -> str:
    import params as config
    return str(getattr(config, "GPT_MODEL", "") or "").strip()


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
    """(провайдер, модель) для текстовой задачи стадии: Codex с моделью стадии или режиссёра."""
    import params as config

    model = ""
    if (stage or "").lower() == "stock":
        model = str(getattr(config, "STOCK_PROMPT_MODEL", "") or "").strip()
    return "codex", model or _model_for()


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
    """Спросить текст у Codex. Исключения не глушит.

    `json_mode` и `max_tokens` оставлены для совместимости вызовов: Codex отвечает по схеме.
    """
    logger.info("   [оператор/%s] codex/%s ← задача %s символов",
                phase, model or "—", len(prompt or ""))
    # CLI-агент пишет ответ в файл.
    work = Path(work_dir or Path.cwd())
    work.mkdir(parents=True, exist_ok=True)
    result_file = work / f"{phase}_result.json"
    # Фаза бывает составной («queries/48») — путь получает ПОДПАПКУ, её нужно
    # создать, иначе Codex падает на записи ответа («Failed to write last message
    # file … (os error 3)»), а питон переживает это молча (жалоба 29.08).
    result_file.parent.mkdir(parents=True, exist_ok=True)
    result_file.unlink(missing_ok=True)
    # Просить «запиши файл и НЕ печатай ответ» можно только при контрольной
    # схеме, которая описывает отчёт, а не результат.
    #
    # Со схемой ОТВЕТА такая приписка — прямое противоречие: `--output-schema`
    # требует напечатать результат, `--output-last-message` кладёт напечатанное
    # в ТОТ ЖЕ файл, а текст запрещает печатать. Модель тратит ход на лишнюю
    # запись файла и хуже читает саму задачу. Замер на реальной задаче клипа 4
    # (26.08): с припиской 30.0с и запросы про выдуманную деревню «Brzegi», без
    # неё 19.7с и запросы по смыслу сцены.
    task = prompt
    if schema_is_control(schema_file):
        task += (
            "\n\nWrite the result as a single JSON file to this exact path:\n"
            + result_file.resolve().as_posix()
            + "\nWrite nothing else and do not print the answer."
        )
    else:
        # Файл всё равно пишется — его пишет сам CLI (флаг «сохранить последнее
        # сообщение»), и ответ читается оттуда.
        logger.info("   [оператор/%s] схема %s описывает ответ — беру его из "
                    "последнего сообщения агента (файл пишет сам CLI)",
                    phase, schema_file)
    from units.codex_runner import CodexRunner

    runner = CodexRunner(model_name=model or None, timeout=timeout)
    # Codex работает по схеме вывода: без неё он волен ответить чем угодно.
    runner.run(task, schema_file or "file_control.json", result_file, work)
    if not result_file.exists():
        raise RuntimeError("codex: result file was not created")
    answer = result_file.read_text(encoding="utf-8-sig", errors="replace")
    logger.info("   [оператор/%s] ответ %s символов (файл %s)",
                phase, len(answer), result_file.name)
    return answer


def ask_json(provider: str, model: str, prompt: str, **kwargs) -> dict:
    """`ask_text` + разбор JSON-объекта из ответа."""
    from units.vision_picker import _parse_json_object

    raw = ask_text(provider, model, prompt, **kwargs)
    try:
        return _parse_json_object(raw)
    except Exception:  # noqa: BLE001
        return json.loads(raw)
