"""
Moon Bridge manager — локальный прокси Codex (OpenAI Responses API) -> chat/completions API.

Codex общается с моделью только через Responses API. Провайдеры DeepSeek и OpenRouter
его не поддерживают (только /chat/completions). Moon Bridge (https://github.com/ZhiYi-R/moon-bridge)
транслирует Responses → chat. Этот модуль:
  - пишет config.yml с актуальными API ключами (DEEPSEEK_API_KEY, OPENROUTER_API_KEY),
  - генерит config.toml + models_catalog.json в изолированный CODEX_HOME,
  - стартует/глушит moonbridge.exe,
  - health-check /v1/models.

Активируется когда GPT_PROVIDER=deepseek или GPT_PROVIDER=openrouter. OpenAI-режим не затрагивает.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

import params as config
_proc: Optional[subprocess.Popen] = None
# Отпечаток конфига, под которым мост был поднят (для умного рестарта при смене модели/web).
_started_fingerprint: Optional[str] = None


def _addr() -> str:
    return getattr(config, 'MOONBRIDGE_ADDR', '127.0.0.1:38440')


def base_url() -> str:
    """OpenAI Responses-совместимый эндпоинт прокси."""
    return f"http://{_addr()}/v1"


def is_up(timeout: float = 3.0) -> bool:
    """True если прокси отвечает на /v1/models."""
    try:
        with urllib.request.urlopen(f"{base_url()}/models", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _write_config_yml() -> Path:
    """Пишет moonbridge/config.yml с актуальными ключами и моделями (DeepSeek + OpenRouter)."""
    mb_dir = Path(config.MOONBRIDGE_DIR)
    mb_dir.mkdir(parents=True, exist_ok=True)
    cfg = mb_dir / 'config.yml'

    host, _, port = _addr().partition(':')
    addr = f"{host}:{port or '38440'}"

    # Определяем активного провайдера и модель
    provider = config.GPT_PROVIDER
    if provider == 'deepseek':
        active_provider = 'deepseek'
        active_model = config.DEEPSEEK_MODEL or 'deepseek-v4-flash'
    elif provider == 'openrouter':
        active_provider = 'openrouter'
        active_model = config.OPENROUTER_MODEL or 'openrouter/free'
    else:
        # Fallback на DeepSeek если GPT_PROVIDER неизвестен, но есть DeepSeek ключ
        active_provider = 'deepseek'
        active_model = 'deepseek-v4-flash'

    # Модели секция (все возможные модели, которые может запросить Codex)
    models_section = """models:
  deepseek-v4-pro:
    context_window: 1000000
    max_output_tokens: 384000
    default_reasoning_level: "high"
    supported_reasoning_levels:
      - effort: "high"
        description: "High reasoning effort"
      - effort: "xhigh"
        description: "Extra high reasoning effort"
    supports_reasoning_summaries: true
    default_reasoning_summary: "auto"
    extensions:
      deepseek_v4:
        enabled: true
  deepseek-v4-flash:
    context_window: 1000000
    max_output_tokens: 384000
    default_reasoning_level: "high"
    supported_reasoning_levels:
      - effort: "high"
        description: "High reasoning effort"
      - effort: "xhigh"
        description: "Extra high reasoning effort"
    supports_reasoning_summaries: true
    default_reasoning_summary: "auto"
    extensions:
      deepseek_v4:
        enabled: true
"""
    # OpenRouter: динамически добавляем АКТИВНУЮ выбранную модель (любой slug из 337+),
    # чтобы Codex мог её запросить. Фиксированный список бы ограничивал выбор.
    if config.OPENROUTER_API_KEY:
        _or_model = config.OPENROUTER_MODEL or 'openrouter/free'
        models_section += f"""  {_or_model}:
    context_window: 200000
    max_output_tokens: 65536
"""

    # Провайдеры (добавляем оба если есть ключи)
    # web_search.support: 'disabled' убирает probe-перебор моделей на старте (быстрее старт,
    # нет 500-ошибок в логе). Для DeepSeek web в режиссуре не нужен — всегда disabled.
    # Для OpenRouter — управляется выбором юзера в GUI (config.OPENROUTER_WEB_SEARCH).
    providers_section = "providers:\n"
    if config.DEEPSEEK_API_KEY:
        providers_section += f"""  deepseek:
    base_url: "https://api.deepseek.com/anthropic"
    api_key: "{config.DEEPSEEK_API_KEY}"
    web_search:
      support: "disabled"
    offers:
      - model: deepseek-v4-pro
      - model: deepseek-v4-flash
"""
    if config.OPENROUTER_API_KEY:
        # ВАЖНО: base_url БЕЗ /v1 — Moon Bridge сам дописывает /v1/messages.
        # С /v1 получится двойной /v1/v1/messages → 404 HTML.
        _or_model = config.OPENROUTER_MODEL or 'openrouter/free'
        _or_web = getattr(config, 'OPENROUTER_WEB_SEARCH', 'disabled') or 'disabled'
        if _or_web not in ('auto', 'enabled', 'disabled', 'injected'):
            _or_web = 'disabled'
        providers_section += f"""  openrouter:
    base_url: "https://openrouter.ai/api"
    api_key: "{config.OPENROUTER_API_KEY}"
    web_search:
      support: "{_or_web}"
    offers:
      - model: {_or_model}
"""

    content = f"""mode: "Transform"

server:
  addr: "{addr}"

{models_section}
{providers_section}
routes:
  moonbridge:
    model: {active_model}
    provider: {active_provider}

defaults:
  model: moonbridge
  max_tokens: 65536
"""
    cfg.write_text(content, encoding='utf-8')
    return cfg


def generate_codex_config() -> Path:
    """Генерит config.toml + models_catalog.json в изолированный CODEX_HOME.

    Вызывает moonbridge.exe --print-codex-config. Прокси для этого запускать
    не обязательно — генерация офлайновая.
    """
    exe = Path(config.MOONBRIDGE_EXE_PATH)
    if not exe.is_file():
        raise FileNotFoundError(
            f"moonbridge.exe не найден: {exe}. Соберите: "
            f"cd moonbridge && go build -o moonbridge.exe ./cmd/moonbridge"
        )
    cfg = _write_config_yml()
    home = Path(config.DEEPSEEK_CODEX_HOME)
    home.mkdir(parents=True, exist_ok=True)

    # 1) узнать имя модели codex (обычно "moonbridge")
    model = subprocess.run(
        [str(exe), '--config', str(cfg), '--print-codex-model'],
        cwd=str(exe.parent), capture_output=True, text=True,
        encoding='utf-8', errors='replace', timeout=60,
    ).stdout.strip().splitlines()[-1].strip() or 'moonbridge'

    # 2) сгенерить config.toml (+ models_catalog.json как сайд-эффект)
    res = subprocess.run(
        [str(exe), '--config', str(cfg),
         '--print-codex-config', model,
         '--codex-base-url', base_url(),
         '--codex-home', str(home)],
        cwd=str(exe.parent), capture_output=True, text=True,
        encoding='utf-8', errors='replace', timeout=120,
    )
    if res.returncode != 0:
        raise RuntimeError(f"moonbridge --print-codex-config failed: {res.stderr[-500:]}")
    toml_text = res.stdout
    if 'model_provider' not in toml_text:
        raise RuntimeError(f"moonbridge вернул пустой config.toml: {toml_text[:300]}")
    (home / 'config.toml').write_text(toml_text, encoding='utf-8')
    return home / 'config.toml'


def start(wait_seconds: int = 25) -> bool:
    """Запускает прокси (если ещё не запущен) и ждёт готовности.

    Также (пере)генерит config.yml и codex config.toml с актуальными значениями
    (модель/web/ключ читаются из config — туда их кладёт GUI через env).
    Возвращает True если прокси отвечает.
    """
    global _proc
    if is_up():
        # уже поднят (возможно другим процессом) — обновим только codex config
        try:
            generate_codex_config()
        except Exception:
            pass
        return True

    exe = Path(config.MOONBRIDGE_EXE_PATH)
    if not exe.is_file():
        raise FileNotFoundError(
            f"moonbridge.exe не найден: {exe}. Соберите его командой: "
            f"cd moonbridge && go build -o moonbridge.exe ./cmd/moonbridge"
        )
    cfg = _write_config_yml()

    creationflags = 0
    if sys.platform == 'win32':
        creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

    _proc = subprocess.Popen(
        [str(exe), '--config', str(cfg)],
        cwd=str(exe.parent),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )

    t0 = time.time()
    while time.time() - t0 < wait_seconds:
        if is_up():
            generate_codex_config()
            return True
        if _proc.poll() is not None:
            raise RuntimeError(f"Moon Bridge упал на старте (exit {_proc.returncode})")
        time.sleep(1)
    raise RuntimeError(f"Moon Bridge не поднялся за {wait_seconds}s на {_addr()}")


def stop():
    """Глушит прокси, запущенный этим процессом."""
    global _proc
    if _proc is not None and _proc.poll() is None:
        try:
            _proc.terminate()
            _proc.wait(timeout=5)
        except Exception:
            try:
                _proc.kill()
            except Exception:
                pass
    _proc = None


def ensure_ready() -> bool:
    """Идемпотентно: гарантирует, что прокси поднят и codex config актуален.

    Используется перед запуском пайплайна когда GPT_PROVIDER=deepseek|openrouter.
    """
    return start()
