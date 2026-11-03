"""Metered streaming HTTP transport, without reply policy or verification."""
import asyncio,json,time,threading
import httpx
from copy import deepcopy
from app.config import settings
_model_tls=None
_model_tls_lock=threading.Lock()
_transport=threading.local()
_primary_lock=threading.Lock()
_primary_failures={}


def gemini_endpoint():
    base = settings.google_gemini_base_url.rstrip('/')
    return base + ('/chat/completions' if base.endswith('/v1') else '/v1/chat/completions')


def gemini_available():
    return bool(settings.gemini_fallback_enabled and settings.gemini_api_key
                and settings.google_gemini_base_url and settings.gemini_model)


def _retryable(exc):
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {401, 403, 408, 429} or exc.response.status_code >= 500
    return isinstance(exc, (httpx.TransportError, TimeoutError))


def _gemini_payload(payload):
    # Translate provider-only controls, without changing business messages/tools.
    result = deepcopy(payload)
    result['model'] = settings.gemini_model
    result['temperature'] = settings.gemini_temperature
    thinking = result.pop('thinking', None)
    if thinking:
        result['reasoning_effort'] = ('none' if thinking.get('type') == 'disabled'
                                      else result.get('reasoning_effort', 'low'))
    for message in result.get('messages', []):
        message.pop('reasoning_content', None)
    return result

def _model_ssl_context():
    """Share immutable trust configuration, never clients or event loops.

    Loading the certificate bundle repeatedly is expensive on Windows. HTTPX's
    public factory preserves its normal certificate and environment settings.
    """
    global _model_tls
    with _model_tls_lock:
        if _model_tls is None:
            _model_tls = httpx.create_ssl_context(verify=True, trust_env=True)
        return _model_tls


class EvaluationCallError(RuntimeError):
    def __init__(self, code: str, logs: list[dict], digest: str):
        super().__init__(code)
        self.code = code
        self.logs = logs
        self.digest = digest


def _post_with_deadline(payload: dict, timeout: float) -> httpx.Response:
    """One shared deadline, separately metered attempts, no partial reply delivery."""
    from app.model_metering import calls
    deadline = time.monotonic() + timeout
    available = gemini_available()
    identity = (settings.deepseek_base_url, settings.deepseek_api_key)
    records = calls.get() or []
    # Keep a tool conversation on the provider that successfully took it over.
    sticky = any(r.get('provider') == 'gemini' and r['status'] == 'completed' for r in records)
    with _primary_lock:
        cooling = _primary_failures.get(identity, 0) > time.monotonic()
    reason = 'turn_continuation' if sticky else 'primary_cooldown' if cooling else None
    if not available or not (sticky or cooling):
        primary_timeout = min(timeout / 2, settings.model_primary_probe_seconds) if available else timeout
        try:
            response = _metered_attempt(payload, primary_timeout, 'deepseek')
            with _primary_lock:
                _primary_failures.pop(identity, None)
            return response
        except Exception as exc:
            if not available or not _retryable(exc):
                raise
            reason = 'http_' + str(exc.response.status_code) if isinstance(exc, httpx.HTTPStatusError) else type(exc).__name__
            with _primary_lock:
                _primary_failures[identity] = time.monotonic() + settings.model_primary_cooldown_seconds
            if deadline <= time.monotonic():
                raise
    return _metered_attempt(_gemini_payload(payload), max(0, deadline-time.monotonic()), 'gemini', reason)


def _metered_attempt(payload, timeout, provider, fallback_reason=None):
    from app.model_metering import begin_request, end_request
    handle = begin_request(payload, provider=provider, fallback_reason=fallback_reason)
    body = None
    try:
        response = (_post_unmetered(payload, timeout) if provider == 'deepseek' else
                    _post_unmetered(payload, timeout, endpoint=gemini_endpoint(), api_key=settings.gemini_api_key))
        response.extensions['model_provider'] = provider
        response.extensions['fallback_reason'] = fallback_reason
        response.extensions.setdefault('call_timing', {}).update(provider=provider, fallback_reason=fallback_reason)
        try:
            body = response.json()
            if (payload.get('response_format') or {}).get('type') == 'json_object':
                from app.model_gateway import _json_object
                message = body['choices'][0]['message']
                if not message.get('tool_calls'):
                    _json_object(message.get('content') or '')
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            raise httpx.ReadError('invalid_model_response_format', request=response.request) from exc
        end_request(handle, body)
        return response
    except Exception as exc:
        end_request(handle, body if isinstance(body, dict) else None, error=exc)
        raise


def _post_unmetered(payload: dict, timeout: float, *, endpoint=None, api_key=None) -> httpx.Response:
    # Each calling thread owns its loop and connection pool; API threads never share an asyncio client.
    if not hasattr(_transport, "runner"):
        _transport.runner = asyncio.Runner()
    async def send():
        started = time.monotonic()
        timing = {"phase": "connect", "headers_ms": None, "first_token_ms": None}
        url = endpoint or f"{settings.deepseek_base_url.rstrip('/')}/chat/completions"
        key = settings.deepseek_api_key if api_key is None else api_key
        identity = (endpoint or settings.deepseek_base_url, key)
        if getattr(_transport, "identity", None) != identity:
            if getattr(_transport, "client", None) is not None:
                await _transport.client.aclose()
            _transport.client = httpx.AsyncClient(verify=_model_ssl_context(), limits=httpx.Limits(max_connections=2, max_keepalive_connections=1, keepalive_expiry=60))
            _transport.identity = identity
        timing['initialization_ms'] = int((time.monotonic() - started) * 1000)
        try:
            async with asyncio.timeout(max(0, timeout - (time.monotonic() - started))):
                async with _transport.client.stream("POST", url, json=payload,
                            timeout=httpx.Timeout(timeout, connect=min(5, timeout)),
                            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}) as response:
                        response.raise_for_status()
                        timing.update(phase="waiting_for_model", headers_ms=int((time.monotonic() - started) * 1000))
                        if "text/event-stream" not in response.headers.get("content-type", ""):
                            await response.aread()
                            response.extensions["call_timing"] = timing
                            return response
                        parts, reasoning, usage, finish, done = [], [], {}, None, False
                        tool_calls, model, fingerprint = {}, None, None
                        async for line in response.aiter_lines():
                            if not line.startswith("data:"):
                                continue  # SSE comments/keep-alives are not generated content.
                            data = line[5:].strip()
                            if data == "[DONE]":
                                done = True
                                break
                            chunk = json.loads(data)
                            model = chunk.get('model') or model
                            fingerprint = chunk.get('system_fingerprint') or fingerprint
                            usage = chunk.get("usage") or usage
                            for choice in chunk.get("choices") or []:
                                delta = choice.get('delta') or {}
                                text = delta.get("content") or ""
                                if delta.get('reasoning_content'):
                                    reasoning.append(delta['reasoning_content'])
                                for piece in delta.get('tool_calls') or []:
                                    index = piece.get('index', 0)
                                    call = tool_calls.setdefault(index, {'id':'', 'type':'function',
                                        'function':{'name':'','arguments':''}})
                                    if piece.get('id'): call['id'] = piece['id']
                                    for field in ('name','arguments'):
                                        call['function'][field] += (piece.get('function') or {}).get(field) or ''
                                if text:
                                    if timing["first_token_ms"] is None:
                                        timing["first_token_ms"] = int((time.monotonic() - started) * 1000)
                                    timing["phase"] = "generating"
                                    parts.append(text)
                                finish = choice.get("finish_reason") or finish
                        timing['finish_reason'] = finish
                        if done and finish == 'length':
                            raise ValueError('deepseek_output_token_limit')
                        if not done or finish not in {'stop', 'tool_calls'} or (finish == 'tool_calls' and not tool_calls):
                            raise httpx.ReadError("incomplete_model_stream")
                        timing["phase"] = "completed"
                        message = {'content': ''.join(parts)}
                        if reasoning:
                            message['reasoning_content'] = ''.join(reasoning)
                        if tool_calls:
                            if any(not c['id'] or not c['function']['name'] for c in tool_calls.values()):
                                raise httpx.ReadError('incomplete_model_tool_stream')
                            message['tool_calls'] = [tool_calls[i] for i in sorted(tool_calls)]
                        return httpx.Response(200, request=response.request, extensions={"call_timing": timing},
                            json={"choices": [{"message": message, "finish_reason": finish}], "usage": usage,
                                  'model':model, 'system_fingerprint':fingerprint})
        except Exception as exc:
            exc.call_timing = timing
            raise
    return _transport.runner.run(send())


def close_deepseek_transport():
    runner = getattr(_transport, "runner", None)
    if runner is not None:
        if getattr(_transport, "client", None) is not None:
            runner.run(_transport.client.aclose())
        runner.close()
        _transport.__dict__.clear()


def _call_log(attempt: int, started: float, body: dict, status: str, error: str | None) -> dict:
    usage = body.get("usage") if isinstance(body, dict) and isinstance(body.get("usage"), dict) else {}
    return {"attempt": attempt, "duration_ms": int((time.monotonic() - started) * 1000), "input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens"), "status": status, "error_code": error, "response_meta": {"model": body.get('model') if isinstance(body, dict) else None, "finish_reason": ((body.get("choices") or [{}])[0].get("finish_reason") if isinstance(body, dict) else None)}}
