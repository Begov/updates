"""Shared helpers for non-standard Director behavior profiles."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def normalize_director_profile(value: str | None) -> str:
    profile = (value or "standard").strip().lower()
    if profile not in {"standard", "auto_visual_story"}:
        raise ValueError(f"Unknown director profile: {profile}")
    return profile


def parse_json_response(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("Director profile transport returned no JSON object")
        payload = json.loads(text[start:end + 1])
    if not isinstance(payload, dict):
        raise ValueError("Director profile response must be a JSON object")
    return payload


def coerce_json_list(value: Any, field_name: str) -> list[Any]:
    """Accept an API field as a JSON list or a string-serialized JSON list."""
    if isinstance(value, str):
        text = value.strip()
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Director profile field {field_name!r} is not a JSON list"
            ) from exc
    if not isinstance(value, list):
        raise ValueError(
            f"Director profile field {field_name!r} must be a list, "
            f"got {type(value).__name__}"
        )
    # Некоторые провайдеры сериализуют КАЖДЫЙ элемент отдельной JSON-строкой
    # (замечено у Meta Muse 23.08: "scenes": ["{\"word_start_index\"..."]).
    # Оба потребителя (scenes, plans) ждут список объектов — разворачиваем.
    # Строка, не являющаяся JSON-объектом/списком, остаётся как есть: пусть
    # потребитель упадёт со своей внятной ошибкой, а не с нашей догадкой.
    decoded = []
    for item in value:
        if isinstance(item, str):
            text = item.strip()
            if text.startswith("{") or text.startswith("["):
                try:
                    item = json.loads(text)
                except json.JSONDecodeError:
                    pass
        decoded.append(item)
    return decoded


class DirectorProfileTransport:
    """Use the already-selected standard Director instance as a JSON transport."""

    def __init__(self, director: Any, project_dir: str | Path):
        self.director = director
        self.project_dir = Path(project_dir).resolve()
        self.raw_dir = self.project_dir / "visual_story" / "raw"
        self.raw_dir.mkdir(parents=True, exist_ok=True)

    @property
    def provider_label(self) -> str:
        provider = getattr(self.director, "provider", "")
        model = getattr(self.director, "model_name", "")
        return "/".join(part for part in (provider, model) if part) or type(self.director).__name__

    @property
    def wants_files(self) -> bool:
        """Агентный транспорт (Codex): большие входы давать ПУТЯМИ, не текстом."""
        return hasattr(self.director, "_run_codex") and not hasattr(self.director, "_chat")

    def _call_codex_file_contract(
        self,
        *,
        phase: str,
        prompt: str,
        schema_file: str,
        result_path: Path,
    ) -> dict[str, Any]:
        """Контракт стандартного Pass 1 для агента Codex (см. gpt_director._build_pass1_prompt).

        Раньше весь результат ждали ПОСЛЕДНИМ СООБЩЕНИЕМ агента по схеме — на
        47-минутном задании агент записал план в свой файл и вернул отписку
        (улов 25.08). Как Pass 1: агенту велят записать JSON по явному пути,
        последним сообщением он отдаёт маленький контрольный JSON, а результат
        читаем с диска. Инструменты не запрещаем. Рабочая папка — своя на вызов,
        чтобы параллельные агенты не делили task-файлы.
        """
        schemas_dir = Path(__file__).parents[1] / "schemas"
        schema_text = (schemas_dir / schema_file).read_text(encoding="utf-8")
        work_dir = self.raw_dir / f"codex_{phase}"
        work_dir.mkdir(parents=True, exist_ok=True)
        if result_path.exists():
            result_path.unlink()
        wrapped = (
            f"{prompt}\n\n"
            "## OUTPUT CONTRACT\n"
            f"Write the result as a single JSON file to: {result_path.as_posix()}\n"
            "The file must match this JSON schema exactly (no extra keys):\n"
            f"{schema_text}\n"
            "Write the file, then report your status in your final response as "
            "JSON: status (\"ok\" or \"error\"), result_path (the path above), "
            "item_count (number of items in the top-level array), error_message "
            "(empty string when ok). Do NOT put the result itself into the final "
            "response — only into the file."
        )
        control_file = work_dir / "_control.json"
        control = self.director._run_codex(
            wrapped,
            "visual_story_file_control.json",
            control_file,
            work_dir,
        )
        if not isinstance(control, dict) or control.get("status") != "ok":
            raise RuntimeError(
                f"Codex «{phase}» failed: "
                f"{(control or {}).get('error_message') if isinstance(control, dict) else control}"
            )
        # Путь из контроля — подсказка; источник правды — наш явный путь.
        candidates = [result_path]
        hinted = str(control.get("result_path") or "").strip()
        if hinted:
            candidates.append(Path(hinted))
        for candidate in candidates:
            if candidate.exists() and candidate.stat().st_size > 2:
                payload = json.loads(candidate.read_text(encoding="utf-8-sig"))
                if not isinstance(payload, dict):
                    raise ValueError(f"Codex «{phase}» wrote a non-object JSON to {candidate}")
                if candidate != result_path:
                    result_path.write_text(
                        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
                    )
                return payload
        raise RuntimeError(
            f"Codex «{phase}» reported ok but wrote no result file at {result_path}"
        )

    def call_json(
        self,
        *,
        phase: str,
        prompt: str,
        schema_file: str,
    ) -> dict[str, Any]:
        result_path = self.raw_dir / f"{phase}_result.json"

        if hasattr(self.director, "_chat"):
            raw = self.director._chat(
                [{"role": "user", "content": prompt}],
                json_mode=True,
            )
            (self.raw_dir / f"{phase}_response.txt").write_text(
                raw or "", encoding="utf-8"
            )
            payload = parse_json_response(raw)
        elif hasattr(self.director, "_run_codex"):
            payload = self._call_codex_file_contract(
                phase=phase,
                prompt=prompt,
                schema_file=schema_file,
                result_path=result_path,
            )
        elif hasattr(self.director, "_make_api_call"):
            response, _cost = self.director._make_api_call(
                prompt,
                spinner_msg=f"Visual Story: {phase}",
            )
            raw = getattr(response, "text", "") or ""
            (self.raw_dir / f"{phase}_response.txt").write_text(
                raw, encoding="utf-8"
            )
            payload = parse_json_response(raw)
        else:
            raise TypeError(
                f"Unsupported Director transport: {type(self.director).__name__}"
            )

        result_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return payload
