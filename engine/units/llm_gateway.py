"""
Chat client — прямой HTTP-транспорт к OpenAI-совместимым chat/completions API.

Общий транспорт для gateway_director.py (режиссёр) и prompt_softener.py (rewrite).
Провайдеры: direct OpenAI-compatible chat API. БЕЗ Codex и Moon Bridge — модель просто
отвечает текстом, никакого агентского поведения / записи файлов.

Доказано тестами (см. reference_chat_director_api): прямой API обоих провайдеров
возвращает чистый JSON, в отличие от Codex-агента, который на больших проектах
писал Python-скрипты вместо ответа.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Optional

import requests

import params as config
# Эндпоинты провайдеров (OpenAI-совместимый chat/completions)
def _claude_api_endpoint() -> str:
    """Claude API is the native Anthropic-compatible Nexus endpoint."""
    return str(getattr(
        config,
        'CLAUDE_RESELLER_ANTHROPIC_URL',
        'https://ccmaxshop.de5.net/v1/messages',
    ) or '').strip()


_ENDPOINTS = {
    'deepseek': 'https://api.deepseek.com/chat/completions',
    'openrouter': 'https://openrouter.ai/api/v1/chat/completions',
    # Claude API means Nexus and uses native Anthropic Messages transport.
    'claude_api': _claude_api_endpoint(),
    # Codex/GPT reseller из Story Rewriter — OpenAI-совместимый chat/completions.
    'codex_reseller': getattr(
        config, 'CODEX_RESELLER_URL',
        'https://ai.ailink1.com/v1/chat/completions',
    ),
    'gpt_nexus': getattr(
        config, 'GPT_NEXUS_URL',
        'https://ccmaxshop.de5.net/v1/chat/completions',
    ),
    'meta_muse': getattr(
        config, 'META_MUSE_URL',
        'https://api.meta.ai/v1/chat/completions',
    ),
}


def _api_key(provider: str) -> str:
    if provider == 'deepseek':
        return getattr(config, 'DEEPSEEK_API_KEY', '') or ''
    if provider == 'openrouter':
        return getattr(config, 'OPENROUTER_API_KEY', '') or ''
    if provider == 'claude_api':
        return (
            getattr(config, 'CLAUDE_RESELLER_ANTHROPIC_API_KEY', '')
            or getattr(config, 'CLAUDE_API_KEY', '')
            or ''
        )
    if provider == 'codex_reseller':
        return getattr(config, 'CODEX_RESELLER_API_KEY', '') or ''
    if provider == 'gpt_nexus':
        return getattr(config, 'GPT_NEXUS_API_KEY', '') or ''
    if provider == 'meta_muse':
        return getattr(config, 'META_MUSE_API_KEY', '') or ''
    return ''


def _resolve_model(provider: str, model: str, web: bool) -> str:
    """OpenRouter web search = суффикс ':online' к модели. DeepSeek — без изменений."""
    model = (model or '').strip()
    if provider == 'openrouter' and web and not model.endswith(':online'):
        return f'{model}:online'
    if provider == 'claude_api' and model.startswith('kr/'):
        model = model[3:]
    if provider == 'claude_api':
        return {
            'claude-sonnet-4.8': 'claude-sonnet-4-8',
            'claude-sonnet-4.6': 'claude-sonnet-4-6',
            'claude-sonnet-4.5': 'claude-sonnet-4-5-20250929',
            'claude-opus-4.8': 'claude-opus-4-8',
            'claude-opus-4.7': 'claude-opus-4-7',
            'claude-opus-4.6': 'claude-opus-4-6',
            'claude-haiku-4.5': 'claude-haiku-4-5',
        }.get(model, model)
    return model


def _anthropic_content_blocks(content) -> list[dict]:
    """Convert OpenAI-style text/image content to Anthropic blocks."""
    if isinstance(content, str):
        return [{'type': 'text', 'text': content}]
    if not isinstance(content, list):
        text = str(content or '')
        return [{'type': 'text', 'text': text}] if text else []

    blocks = []
    for item in content:
        if isinstance(item, str):
            if item:
                blocks.append({'type': 'text', 'text': item})
            continue
        if not isinstance(item, dict):
            continue
        item_type = item.get('type')
        if item_type == 'text' and isinstance(item.get('text'), str):
            blocks.append({'type': 'text', 'text': item['text']})
            continue
        if item_type == 'image' and isinstance(item.get('source'), dict):
            blocks.append(item)
            continue
        if item_type != 'image_url':
            continue
        image = item.get('image_url') or {}
        url = image.get('url') if isinstance(image, dict) else image
        if not isinstance(url, str) or not url.startswith('data:') or ',' not in url:
            continue
        header, encoded = url.split(',', 1)
        if ';base64' not in header:
            continue
        blocks.append({
            'type': 'image',
            'source': {
                'type': 'base64',
                'media_type': header[5:].split(';', 1)[0] or 'image/jpeg',
                'data': encoded,
            },
        })
    return blocks


def _anthropic_request_messages(messages: list, json_mode: bool) -> tuple[str, list]:
    system_parts = []
    chat = []
    for message in messages:
        role = str(message.get('role') or 'user').lower()
        content = message.get('content')
        if role == 'system':
            if isinstance(content, str) and content.strip():
                system_parts.append(content.strip())
            continue
        role = 'assistant' if role == 'assistant' else 'user'
        blocks = _anthropic_content_blocks(content)
        if not blocks:
            continue
        if chat and chat[-1]['role'] == role:
            chat[-1]['content'].extend(blocks)
        else:
            chat.append({'role': role, 'content': blocks})
    if json_mode:
        system_parts.append(
            'Return exactly one valid JSON object without markdown or commentary.'
        )
    if not chat:
        chat = [{'role': 'user', 'content': [{'type': 'text', 'text': ''}]}]
    return '\n\n'.join(system_parts), chat


def _extract_anthropic_content(data: dict) -> str:
    text = ''.join(
        str(item.get('text') or '')
        for item in (data.get('content') or [])
        if isinstance(item, dict) and item.get('type') == 'text'
    ).strip()
    if not text:
        raise ChatError(f"claude_api/Nexus: ответ без текста: {str(data)[:300]}")
    return text


def _extract_anthropic_stream_content(resp) -> str:
    parts = []
    for raw in resp.iter_lines(decode_unicode=False):
        if not raw:
            continue
        line = raw.decode('utf-8', errors='replace') if isinstance(raw, bytes) else str(raw)
        if not line.startswith('data:'):
            continue
        payload = line[5:].strip()
        if not payload or payload == '[DONE]':
            continue
        try:
            event = json.loads(payload)
        except ValueError as exc:
            raise ChatError(
                f"claude_api/Nexus: повреждённый SSE JSON: {exc}"
            ) from exc
        if event.get('type') == 'error' or event.get('error'):
            raise ChatError(
                f"claude_api/Nexus SSE error: "
                f"{str(event.get('error') or event)[:300]}"
            )
        event_type = event.get('type')
        if event_type == 'content_block_start':
            block = event.get('content_block') or {}
            if block.get('type') == 'text' and isinstance(block.get('text'), str):
                parts.append(block['text'])
        elif event_type == 'content_block_delta':
            delta = event.get('delta') or {}
            if delta.get('type') == 'text_delta' and isinstance(delta.get('text'), str):
                parts.append(delta['text'])
    text = ''.join(parts).strip()
    if not text:
        raise ChatError('claude_api/Nexus: SSE-поток завершился без текста')
    return text


def _anthropic_chat_completion(
    model: str,
    messages: list,
    *,
    max_tokens: Optional[int],
    json_mode: bool,
    timeout: int,
    max_retries: int,
) -> str:
    key = _api_key('claude_api')
    if not key:
        raise ChatError(
            'claude_api/Nexus: не найден CLAUDE_RESELLER_ANTHROPIC_API_KEY'
        )
    system, chat = _anthropic_request_messages(messages, json_mode)
    body = {
        'model': _resolve_model('claude_api', model, False),
        'messages': chat,
        'max_tokens': int(max_tokens or getattr(config, 'CLAUDE_API_MAX_TOKENS', 16000)),
        'stream': True,
    }
    if system:
        body['system'] = system
    headers = {
        'x-api-key': key,
        'anthropic-version': str(
            getattr(config, 'CLAUDE_API_VERSION', '2023-06-01')
        ),
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    }
    url = _claude_api_endpoint()
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(
                url,
                headers=headers,
                json=body,
                stream=True,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            last_err = f'network: {exc}'
            if attempt >= max_retries:
                break
            _backoff(attempt, last_err)
            continue
        if resp.status_code == 200:
            try:
                content_type = str(resp.headers.get('content-type') or '').lower()
                if 'text/event-stream' in content_type:
                    return _extract_anthropic_stream_content(resp)
                return _extract_anthropic_content(resp.json())
            except ValueError as exc:
                raise ChatError(
                    f"claude_api/Nexus: невалидный JSON-ответ: {exc}; "
                    f"body={resp.text[:300]}"
                ) from exc
            finally:
                resp.close()
        if resp.status_code == 429 or resp.status_code >= 500:
            last_err = _http_error_summary(resp.status_code, resp.text, limit=200)
            resp.close()
            if attempt >= max_retries:
                break
            _backoff(attempt, last_err, retry_after=resp.headers.get('Retry-After'))
            continue
        raise ChatError(
            f"claude_api/Nexus {_http_error_summary(resp.status_code, resp.text)}"
        )
    raise ChatError(
        f"claude_api/Nexus: исчерпаны {max_retries} ретраев. "
        f"Последняя ошибка: {last_err}"
    )


class ChatError(Exception):
    """Ошибка chat-запроса (после исчерпания ретраев или фатальная)."""


def _http_error_summary(status: int, body: str, limit: int = 300) -> str:
    """Compact provider/Cloudflare errors so an HTML page does not flood the UI log."""
    text = (body or '').strip()
    if not text:
        return f"HTTP {status}: пустой ответ"

    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        data = None
    if isinstance(data, dict):
        code = data.get('code') or data.get('error_code')
        message = data.get('message')
        error = data.get('error')
        if isinstance(error, dict):
            code = code or error.get('code') or error.get('type')
            message = message or error.get('message')
        elif not message and isinstance(error, str):
            message = error
        if str(code or '').upper() == 'DAILY_LIMIT_EXCEEDED':
            message = "дневная квота реселлера исчерпана; выполните ручной reset квоты"
        details = ': '.join(str(v).strip() for v in (code, message) if v)
        if details:
            return f"HTTP {status}: {details[:limit]}"

    if '<html' in text.lower() or '<!doctype html' in text.lower():
        title = re.search(r'<title[^>]*>(.*?)</title>', text, flags=re.I | re.S)
        label = re.sub(r'\s+', ' ', title.group(1)).strip() if title else ''
        if status in (502, 504, 524):
            cause = "Cloudflare: upstream реселлера не ответил вовремя"
            return f"HTTP {status}: {cause}" + (f" ({label})" if label else '')
        return f"HTTP {status}: HTML-ответ шлюза" + (f" ({label})" if label else '')

    compact = re.sub(r'\s+', ' ', text)
    return f"HTTP {status}: {compact[:limit]}"


def chat_completion(
    provider: str,
    model: str,
    messages: list,
    *,
    max_tokens: Optional[int] = None,
    reasoning_level: Optional[str] = None,
    web: bool = False,
    json_mode: bool = False,
    timeout: int = 300,
    max_retries: int = 4,
) -> str:
    """Один запрос к chat/completions провайдера. Возвращает текст ответа (content).

    Args:
        provider: direct chat provider such as deepseek, openrouter, meta_muse,
                  claude_api, codex_reseller, or gpt_nexus.
        model: имя модели (slug). Для openrouter web=True → добавит ':online'.
        messages: список сообщений [{role, content}, ...]
        max_tokens: лимит output-токенов. None (дефолт) → не ограничиваем,
                    провайдер отдаёт свой максимум (обрезки ловит валидатор).
        reasoning_level: off|low|medium|high|max (дефолт config.CHAT_REASONING_LEVEL).
                         Маппится в нативные параметры провайдера.
        web: OpenRouter — web search через ':online'. DeepSeek игнорирует.
        json_mode: True → response_format json_object (строгий JSON-вывод).
        timeout: таймаут запроса в секундах.
        max_retries: ретраи на 429/5xx с экспоненциальным backoff.

    Returns:
        content финального сообщения (str). Reasoning/COT отбрасывается.

    Raises:
        ChatError: если ключ не задан, провайдер неизвестен или все ретраи исчерпаны.
    """
    provider = (provider or '').strip().lower()
    if provider not in _ENDPOINTS:
        raise ChatError(f"Неизвестный chat-провайдер: {provider}")

    if provider == 'claude_api':
        return _anthropic_chat_completion(
            model,
            messages,
            max_tokens=max_tokens,
            json_mode=json_mode,
            timeout=timeout,
            max_retries=max_retries,
        )

    key = _api_key(provider)
    if not key:
        raise ChatError(f"{provider}: API ключ не задан")

    real_model = _resolve_model(provider, model, web)

    use_stream = provider in ('codex_reseller', 'gpt_nexus')
    body = {
        'model': real_model,
        'messages': messages,
        'stream': use_stream,
    }
    if use_stream:
        body['stream_options'] = {'include_usage': True}
    # max_tokens=None → не ограничиваем вывод (провайдер отдаёт свой максимум).
    # Обрезанный план/промпт ловит валидатор и перезапрашивает недостающее.
    if provider == 'meta_muse':
        # Muse Spark counts hidden reasoning against output. Its default completion
        # reserve is too small for long plans, so it intentionally ignores a
        # smaller caller cap and uses the provider ceiling.
        body['max_tokens'] = int(
            getattr(config, 'META_MUSE_MAX_TOKENS', 96000) or 96000)
    elif max_tokens is not None:
        body['max_tokens'] = max_tokens
    if json_mode:
        body['response_format'] = {'type': 'json_object'}

    # Reasoning-параметры (нативные для провайдера). DeepSeek кладёт thinking в
    # top-level (не extra_body — мы шлём сырой JSON, а не через OpenAI SDK).
    rp = config.reasoning_params_for_provider(provider, reasoning_level)
    body.update(rp)

    headers = {
        'Authorization': f'Bearer {key}',
        'Content-Type': 'application/json',
    }
    if provider == 'openrouter':
        # OpenRouter рекомендует (опционально) для атрибуции; не критично
        headers['HTTP-Referer'] = 'https://begoframe.local'
        headers['X-Title'] = 'BegoFrame'
    if provider == 'gpt_nexus':
        headers['originator'] = 'codex_cli_rs'
        headers['User-Agent'] = (
            getattr(config, 'GPT_NEXUS_USER_AGENT', '') or 'codex_cli_rs/latest'
        )

    url = _ENDPOINTS[provider]
    # localhost-прокси не нужен (внешние хосты) — но уважаем system proxy если задан
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(
                url, headers=headers, json=body, stream=use_stream, timeout=timeout)
        except requests.RequestException as e:
            last_err = f"network: {e}"
            _backoff(attempt, last_err)
            continue

        if resp.status_code == 200:
            if use_stream:
                try:
                    return _extract_stream_content(resp)
                except (requests.RequestException, ChatError, ValueError) as e:
                    last_err = f"stream: {e}"
                    if attempt >= max_retries:
                        raise ChatError(
                            f"{provider}: поток ответа оборван после {attempt} попыток: {e}") from e
                    _backoff(attempt, last_err)
                    continue
                finally:
                    resp.close()
            try:
                data = resp.json()
            except ValueError as e:
                raise ChatError(f"{provider}: невалидный JSON-ответ: {e}; body={resp.text[:300]}")
            return _extract_content(data)

        # 429 / 5xx — ретраим с backoff
        if resp.status_code == 429 or resp.status_code >= 500:
            last_err = _http_error_summary(resp.status_code, resp.text, limit=200)
            _backoff(attempt, last_err, retry_after=resp.headers.get('Retry-After'))
            continue

        # 4xx (кроме 429) — фатально, не ретраим
        raise ChatError(f"{provider} {_http_error_summary(resp.status_code, resp.text)}")

    raise ChatError(f"{provider}: исчерпаны {max_retries} ретраев. Последняя ошибка: {last_err}")


def _backoff(attempt: int, err: str, retry_after: Optional[str] = None):
    """Экспоненциальный backoff между ретраями. Уважает Retry-After если задан."""
    if retry_after:
        try:
            delay = min(float(retry_after), 60.0)
        except (ValueError, TypeError):
            delay = min(2 ** attempt, 30)
    else:
        delay = min(2 ** attempt, 30)  # 2,4,8,16,30...
    logging.warning(f"chat_completion retry {attempt}: {err} — sleep {delay:.0f}s")
    time.sleep(delay)


def _extract_content(data: dict) -> str:
    """Достаёт content из chat/completions ответа. Reasoning/COT игнорируется."""
    choices = data.get('choices') or []
    if not choices:
        raise ChatError(f"ответ без choices: {str(data)[:300]}")
    msg = choices[0].get('message') or {}
    content = msg.get('content')
    if content is None:
        # Некоторые провайдеры могут вернуть reasoning без content при обрыве
        finish = choices[0].get('finish_reason', '?')
        raise ChatError(f"пустой content (finish_reason={finish}): {str(msg)[:200]}")
    return content.strip()


def _extract_stream_content(resp) -> str:
    """Собрать OpenAI-compatible SSE stream в итоговый текст."""
    parts = []
    for raw in resp.iter_lines(decode_unicode=False):
        if not raw:
            continue
        line = raw.decode('utf-8', errors='replace') if isinstance(raw, bytes) else str(raw)
        if not line.startswith('data:'):
            continue
        payload = line[5:].strip()
        if not payload:
            continue
        if payload == '[DONE]':
            break
        try:
            event = json.loads(payload)
        except ValueError as e:
            raise ChatError(f"OpenAI reseller: повреждённый SSE JSON: {e}") from e
        if event.get('error'):
            raise ChatError(f"OpenAI reseller SSE error: {str(event['error'])[:300]}")
        choices = event.get('choices') or []
        if not choices:
            continue
        content = (choices[0].get('delta') or {}).get('content')
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and isinstance(item.get('text'), str):
                    parts.append(item['text'])
    result = ''.join(parts).strip()
    if not result:
        raise ChatError("OpenAI reseller: SSE-поток завершился без текста")
    return result
