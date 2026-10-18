"""
Режиссёрский анализ: выбор бэкенда режиссёра и запуск анализа проекта.
"""
from __future__ import annotations

from typing import Optional, Dict, Any



def direct_project(
    transcription_path: str,
    style_guide_path: str,
    output_dir: str,
    project_name: str = "Untitled",
    model_name: Optional[str] = None,
    two_pass: bool = False,
    prompts_only: bool = False,
    resume_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Режиссёрский анализ через Codex CLI (GptDirector).

    Args:
        two_pass: Двухпроходный режим (для длинных проектов > ~15 мин)
        prompts_only: Только проход 2 — читает готовый montage_plan.json и генерирует промпты
        resume_path: Путь к существующему файлу промптов для продолжения (только с prompts_only)
    """
    from units.codex_director import GptDirector
    director = GptDirector(model_name=model_name)
    return director.analyze(
        transcription_path, style_guide_path, output_dir, project_name,
        two_pass=two_pass, prompts_only=prompts_only, resume_path=resume_path,
    )
