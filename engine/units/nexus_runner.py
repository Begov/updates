"""Claude Nexus API helper for director-style calls."""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

import params as config
@dataclass
class ClaudeApiTextResponse:
    text: str


def _run_with_spinner(fn, message: str):
    result = [None]
    error = [None]
    done = threading.Event()

    def target():
        try:
            result[0] = fn()
        except Exception as exc:
            error[0] = exc
        finally:
            done.set()

    threading.Thread(target=target, daemon=True).start()
    interactive = bool(getattr(sys.stdout, 'isatty', lambda: False)())
    if not interactive:
        _safe_print(f"   {message}...")
        done.wait()
        if error[0]:
            raise error[0]
        return result[0]

    chars = '|/-\\'
    i = 0
    while not done.is_set():
        sys.stdout.write(f"\r{chars[i % len(chars)]} {message}   ")
        sys.stdout.flush()
        i += 1
        done.wait(0.5)
    sys.stdout.write('\r' + ' ' * (len(message) + 8) + '\r')
    sys.stdout.flush()
    if error[0]:
        raise error[0]
    return result[0]


def _safe_print(message: str) -> None:
    try:
        print(message)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, 'encoding', None) or 'utf-8'
        sys.stdout.buffer.write((message + '\n').encode(encoding, errors='replace'))
        sys.stdout.flush()


class ClaudeApiRunner:
    def __init__(self, model_name: Optional[str] = None, timeout: Optional[int] = None):
        self.api_key = (
            getattr(config, 'CLAUDE_RESELLER_ANTHROPIC_API_KEY', '').strip()
            or getattr(config, 'CLAUDE_API_KEY', '').strip()
            or getattr(config, 'ANTHROPIC_API_KEY', '').strip()
        )
        if not self.api_key:
            raise ValueError(
                "Claude API/Nexus: CLAUDE_RESELLER_ANTHROPIC_API_KEY не найден"
            )
        self.model_name = self._normalize_model_name(
            model_name or getattr(config, 'CLAUDE_API_MODEL', 'claude-sonnet-4-20250514')
        )
        self.timeout = int(timeout or getattr(config, 'CLAUDE_DIRECTOR_TIMEOUT', 1200))
        self.endpoint = str(getattr(
            config,
            'CLAUDE_RESELLER_ANTHROPIC_URL',
            'https://ccmaxshop.de5.net/v1/messages',
        )).strip()
        self.format = 'anthropic'
        self.version = getattr(config, 'CLAUDE_API_VERSION', '2023-06-01')
        self.beta = getattr(config, 'CLAUDE_API_BETA', '').strip()
        self.max_tokens = int(getattr(config, 'CLAUDE_API_MAX_TOKENS', 16000))
        self.temperature = float(getattr(config, 'CLAUDE_API_TEMPERATURE', 0.4))
        self.response_format = str(getattr(config, 'CLAUDE_API_RESPONSE_FORMAT', 'json_object') or '').strip()
        self.debug_dir = Path(
            str(getattr(config, 'CLAUDE_API_DEBUG_DIR', '') or (Path(config.BASE_DIR) / 'logs'))
        )
        self.user_agent = str(getattr(config, 'CLAUDE_API_USER_AGENT', 'BegoFrame/1.0') or 'BegoFrame/1.0').strip()
        self.referer = str(getattr(config, 'CLAUDE_API_REFERER', '') or '').strip()
        self.input_tokens = 0
        self.output_tokens = 0

    def complete(
        self,
        prompt: str,
        *,
        system_instruction: str = '',
        label: str = 'Claude Nexus',
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> Tuple[str, dict, float]:
        payload = self._payload(
            prompt,
            system_instruction=system_instruction,
            max_tokens=int(max_tokens or self.max_tokens),
            temperature=self.temperature if temperature is None else float(temperature),
        )

        retry_delays = [20, 45, 90]
        last_error = None
        t0 = time.time()
        for attempt in range(len(retry_delays) + 1):
            msg = label if attempt == 0 else f"{label} retry {attempt + 1}/{len(retry_delays) + 1}"
            try:
                data = _run_with_spinner(lambda: self._post(payload), msg)
                elapsed = time.time() - t0
                text = self._extract_text(data)
                usage = data.get('usage') or {}
                input_tokens = int(usage.get('input_tokens') or usage.get('prompt_tokens') or 0)
                output_tokens = int(usage.get('output_tokens') or usage.get('completion_tokens') or 0)
                self.input_tokens += input_tokens
                self.output_tokens += output_tokens
                cost_info = {
                    'input_tokens': input_tokens,
                    'output_tokens': output_tokens,
                    'thinking_tokens': 0,
                    'total_output_tokens': output_tokens,
                    'input_cost': 0.0,
                    'output_cost': 0.0,
                    'thinking_cost': 0.0,
                    'total_cost': 0.0,
                    'note': 'Claude Nexus token usage only; pricing not estimated locally',
                }
                logging.info(
                    "Claude Nexus OK | model=%s in=%s out=%s elapsed=%.1fs",
                    self.model_name, input_tokens, output_tokens, elapsed,
                )
                return text, cost_info, elapsed
            except Exception as exc:
                last_error = exc
                retryable = self._is_retryable(exc)
                if not retryable or attempt >= len(retry_delays):
                    raise
                delay = retry_delays[attempt]
                logging.warning("Claude Nexus retryable error (%s), retry in %ss", exc, delay)
                _safe_print(f"\n   ⚠️  Claude Nexus: {exc}. Повтор через {delay}с...")
                time.sleep(delay)
        raise last_error

    def _payload(self, prompt: str, *, system_instruction: str, max_tokens: int, temperature: float) -> dict:
        if self.format == 'openai':
            messages = []
            if system_instruction:
                messages.append({'role': 'system', 'content': system_instruction})
            messages.append({'role': 'user', 'content': prompt})
            if self.response_format == 'json_object':
                has_json_hint = any('json' in str(msg.get('content', '')).lower() for msg in messages)
                if not has_json_hint:
                    messages.insert(0, {'role': 'system', 'content': 'Return one valid JSON object only.'})
            return {
                'model': self.model_name,
                'max_tokens': max_tokens,
                'temperature': temperature,
                'messages': messages,
                **(
                    {'response_format': {'type': self.response_format}}
                    if self.response_format == 'json_object'
                    else {}
                ),
            }
        payload = {
            'model': self.model_name,
            'max_tokens': max_tokens,
            'temperature': temperature,
            'messages': [{'role': 'user', 'content': prompt}],
            'stream': True,
        }
        if system_instruction:
            payload['system'] = system_instruction
        return payload

    def _post(self, payload: dict) -> dict:
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        if self.format == 'openai':
            url = self.endpoint.rstrip('/')
            if not url.endswith('/chat/completions'):
                url += '/chat/completions'
            headers = {
                'Authorization': f'Bearer {self.api_key}',
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'User-Agent': self.user_agent,
            }
            if self.referer:
                headers['Referer'] = self.referer
                headers['Origin'] = self.referer.rstrip('/')
        else:
            url = self.endpoint
            headers = {
                'x-api-key': self.api_key,
                'anthropic-version': self.version,
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'User-Agent': self.user_agent,
            }
            if self.referer:
                headers['Referer'] = self.referer
            if self.beta:
                headers['anthropic-beta'] = self.beta
        req = urllib.request.Request(url, data=body, headers=headers, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode('utf-8', errors='replace')
                return self._parse_json_response(raw, url, getattr(resp, 'status', 200))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode('utf-8', errors='replace')
            saved = self._save_raw_response(detail, f'http_{exc.code}')
            suffix = f" | raw saved: {saved}" if saved else ""
            raise RuntimeError(f"HTTP {exc.code}: {detail[:1200]}{suffix}") from exc

    def _parse_json_response(self, raw: str, url: str, status: int) -> dict:
        text = (raw or '').strip()
        if not text:
            raise RuntimeError(
                f"Claude API returned empty response (HTTP {status}) from {url}. "
                "Gateway likely rejected, timed out, or truncated the request before JSON."
            )
        if text.startswith(('data:', 'event:')) and '\ndata:' in f'\n{text}':
            return self._parse_sse_response(text, raw, url, status)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            saved = self._save_raw_response(raw, 'non_json')
            preview = text[:500].replace('\r', '\\r').replace('\n', '\\n')
            suffix = f" Raw saved: {saved}." if saved else ""
            raise RuntimeError(
                f"Claude API returned non-JSON response (HTTP {status}) from {url}: {exc}."
                f"{suffix} First 500 chars: {preview!r}"
            ) from exc
        if not isinstance(data, dict):
            saved = self._save_raw_response(raw, 'json_not_object')
            suffix = f" Raw saved: {saved}." if saved else ""
            raise RuntimeError(f"Claude API returned JSON {type(data).__name__}, expected object.{suffix}")
        return data

    def _save_raw_response(self, raw: str, reason: str) -> str:
        try:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            ts = time.strftime('%Y%m%d_%H%M%S')
            path = self.debug_dir / f'claude_api_{reason}_{ts}.txt'
            path.write_text(raw or '', encoding='utf-8', errors='replace')
            return str(path)
        except Exception as exc:
            logging.warning("Failed to save Claude API raw response: %s", exc)
            return ''

    def _parse_sse_response(self, text: str, raw: str, url: str, status: int) -> dict:
        content_parts = []
        usage = {}
        last_obj = {}
        bad_line = ''
        response_model = self.model_name
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith('data:'):
                continue
            chunk = line[5:].strip()
            if not chunk or chunk == '[DONE]':
                continue
            try:
                obj = json.loads(chunk)
            except json.JSONDecodeError:
                bad_line = chunk[:500]
                continue
            if not isinstance(obj, dict):
                continue
            last_obj = obj
            if isinstance(obj.get('usage'), dict):
                usage = obj['usage']
            event_type = obj.get('type')
            if event_type == 'message_start':
                message = obj.get('message') or {}
                response_model = str(message.get('model') or response_model)
                if isinstance(message.get('usage'), dict):
                    usage.update(message['usage'])
            elif event_type == 'message_delta':
                if isinstance(obj.get('usage'), dict):
                    usage.update(obj['usage'])
            elif event_type == 'content_block_start':
                block = obj.get('content_block') or {}
                if block.get('type') == 'text' and isinstance(block.get('text'), str):
                    content_parts.append(block['text'])
            elif event_type == 'content_block_delta':
                delta = obj.get('delta') or {}
                if delta.get('type') == 'text_delta' and isinstance(delta.get('text'), str):
                    content_parts.append(delta['text'])
            for choice in obj.get('choices') or []:
                if not isinstance(choice, dict):
                    continue
                msg = choice.get('message')
                if isinstance(msg, dict):
                    content = msg.get('content')
                    if isinstance(content, str):
                        content_parts.append(content)
                delta = choice.get('delta')
                if isinstance(delta, dict):
                    content = delta.get('content')
                    if isinstance(content, str):
                        content_parts.append(content)
        text_out = ''.join(content_parts).strip()
        if text_out:
            if any(
                obj_type in text
                for obj_type in ('"message_start"', '"content_block_delta"')
            ):
                return {
                    'id': last_obj.get('id', 'sse'),
                    'type': 'message',
                    'role': 'assistant',
                    'model': response_model,
                    'content': [{'type': 'text', 'text': text_out}],
                    'usage': usage,
                }
            return {
                'id': last_obj.get('id', 'sse'),
                'object': last_obj.get('object', 'chat.completion'),
                'model': last_obj.get('model', self.model_name),
                'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': text_out}}],
                'usage': usage,
            }
        saved = self._save_raw_response(raw, 'sse_no_text')
        suffix = f" Raw saved: {saved}." if saved else ""
        raise RuntimeError(
            f"Claude API returned SSE chunks but no assistant text (HTTP {status}) from {url}."
            f"{suffix} Last bad line: {bad_line!r}"
        )

    @staticmethod
    def _extract_text(data: dict) -> str:
        choices = data.get('choices') or []
        if choices:
            msg = choices[0].get('message') if isinstance(choices[0], dict) else None
            if isinstance(msg, dict):
                content = msg.get('content')
                if isinstance(content, str):
                    return content.strip()
                if isinstance(content, list):
                    return '\n'.join(
                        str(item.get('text') or '')
                        for item in content
                        if isinstance(item, dict)
                    ).strip()
        chunks = []
        for item in data.get('content') or []:
            if isinstance(item, dict) and item.get('type') == 'text':
                chunks.append(item.get('text') or '')
        return '\n'.join(chunks).strip()

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        text = str(exc).lower()
        if 'suspicious activity' in text or 'temporary limits' in text:
            return False
        retry_words = ('overloaded', 'rate_limit', 'rate limit', 'temporarily unavailable', 'timeout')
        if any(word in text for word in retry_words):
            return True
        retry_http = ('429', '500', '502', '503', '504', '529')
        return any(
            marker in text
            for code in retry_http
            for marker in (f'http {code}', f'status {code}', f'error code: {code}')
        )

    @staticmethod
    def _normalize_model_name(model_name: str) -> str:
        """Canonicalize common CodexHub model spellings from the GUI/history."""
        raw = str(model_name or '').strip()
        aliases = {
            'kr/claude-sonnet-4.6': 'claude-sonnet-4-6',
            'kr/claude-sonnet-4.5': 'claude-sonnet-4-5-20250929',
            'kr/клод-сонет-4.5': 'claude-sonnet-4-5-20250929',
            'kr/claude-opus-4.6': 'claude-opus-4-6',
            'kr/claude-opus-4.7': 'claude-opus-4-7',
            'kr/claude-haiku-4.5': 'claude-haiku-4-5',
            'claude-sonnet-4.8': 'claude-sonnet-4-8',
            'claude-sonnet-4.6': 'claude-sonnet-4-6',
            'claude-sonnet-4.5': 'claude-sonnet-4-5-20250929',
            'claude-opus-4.8': 'claude-opus-4-8',
            'claude-opus-4.7': 'claude-opus-4-7',
            'claude-opus-4.6': 'claude-opus-4-6',
            'claude-haiku-4.5': 'claude-haiku-4-5',
        }
        return aliases.get(raw.lower(), raw)


def extract_json_object(text: str) -> dict:
    text = (text or '').strip()
    if not text:
        raise json.JSONDecodeError("empty", text, 0)
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    import re
    fenced = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL | re.IGNORECASE)
    if fenced:
        return json.loads(fenced.group(1))

    start = text.find('{')
    end = text.rfind('}')
    if start >= 0 and end > start:
        return json.loads(text[start:end + 1])
    raise json.JSONDecodeError("no JSON object found", text, 0)
