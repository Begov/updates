"""
Режиссёрский анализ: выбор бэкенда режиссёра и запуск анализа проекта.
"""
from __future__ import annotations

from typing import Optional, Dict, Any

import params as config


def direct_project(
    transcription_path: str,
    style_guide_path: str,
    output_dir: str,
    project_name: str = "Untitled",
    model_name: Optional[str] = None,
    two_pass: bool = False,
    prompts_only: bool = False,
    resume_path: Optional[str] = None,
    director_profile: Optional[str] = None,
    visual_story_step: str = "full",
    visual_story_mock: bool = False,
    visual_story_mock_assets: bool = False,
) -> Dict[str, Any]:
    """
    Удобная функция для режиссерского анализа.
    Роутинг по config.DIRECTOR_BACKEND: 'gpt' (Codex CLI, по умолчанию) или Claude.

    Args:
        two_pass: Двухпроходный режим (для длинных проектов > ~15 мин)
        prompts_only: Только проход 2 — читает готовый montage_plan.json и генерирует промпты
        resume_path: Путь к существующему файлу промптов для продолжения (только с prompts_only)
    """
    backend = getattr(config, 'DIRECTOR_BACKEND', 'gpt') or 'gpt'
    profile = (
        director_profile
        or getattr(config, 'DIRECTOR_PROFILE', 'standard')
        or 'standard'
    ).strip().lower()
    if profile not in ('standard', 'auto_visual_story'):
        raise ValueError(f"Unknown director profile: {profile}")

    if backend in ('claude', 'claude_code'):
        # Claude Code CLI (подписка Claude Pro/Max или ANTHROPIC_API_KEY)
        from units.claudecli_director import ClaudeCodeDirector
        director = ClaudeCodeDirector(model_name=model_name)
    elif backend in ('claude_api', 'anthropic'):
        # Claude через прямой API (reseller codexhub.click или api.anthropic.com)
        from units.nexus_director import ClaudeApiDirector
        director = ClaudeApiDirector(model_name=model_name)
    elif backend == 'gpt':
        # GPT-бэкенд: провайдер выбирает транспорт.
        #   openai      → Codex CLI (GptDirector)
        #   deepseek    → прямой chat API (ChatDirector, без Codex/Moon Bridge)
        #   openrouter  → прямой chat API (ChatDirector)
        provider = getattr(config, 'GPT_PROVIDER', 'openai')
        if provider in (
            'deepseek', 'openrouter', 'meta_muse',
            'codex_reseller', 'gpt_nexus',
        ):
            from units.gateway_director import ChatDirector
            director = ChatDirector(model_name=model_name)
        else:
            from units.codex_director import GptDirector
            director = GptDirector(model_name=model_name)
    else:
        raise ValueError(f"Unknown director backend: {backend}")

    if profile == 'auto_visual_story':
        if prompts_only:
            raise ValueError(
                "Auto Visual Story does not use standard Pass 2 prompts; "
                "--prompts-only is incompatible with this profile"
            )
        from units.story_director import analyze_visual_story_project
        return analyze_visual_story_project(
            director=director,
            transcription_path=transcription_path,
            style_guide_path=style_guide_path,
            output_dir=output_dir,
            project_name=project_name,
            resume=bool(resume_path),
            step=visual_story_step,
            mock=visual_story_mock,
            mock_assets=visual_story_mock_assets,
        )

    return director.analyze(
        transcription_path, style_guide_path, output_dir, project_name,
        two_pass=two_pass, prompts_only=prompts_only, resume_path=resume_path,
    )


