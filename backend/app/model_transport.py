"""Metered streaming HTTP transport, without reply policy or verification."""
import asyncio,json,time,threading
import httpx
from app.config import settings
_model_tls=None
_model_tls_lock=threading.Lock()
_transport=threading.local()

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
    from app.model_metering import begin_request, end_request
    handle = begin_request(payload)
    try:
        response = _post_unmetered(payload, timeout)
        end_request(handle, response.json())
        return response
    except Exception as exc:
        end_request(handle, error=exc)
        raise


def _post_unmetered(payload: dict, timeout: float) -> httpx.Response:
    # Each calling thread owns its loop and connection pool; API threads never share an asyncio client.
    if not hasattr(_transport, "runner"):
        _transport.runner = asyncio.Runner()
    async def send():
        started = time.monotonic()
        timing = {"phase": "connect", "headers_ms": None, "first_token_ms": None}
        identity = (settings.deepseek_base_url, settings.deepseek_api_key)
        if getattr(_transport, "identity", None) != identity:
            if getattr(_transport, "client", None) is not None:
                await _transport.client.aclose()
            _transport.client = httpx.AsyncClient(verify=_model_ssl_context(), limits=httpx.Limits(max_connections=2, max_keepalive_connections=1, keepalive_expiry=60))
            _transport.identity = identity
        timing['initialization_ms'] = int((time.monotonic() - started) * 1000)
        try:
            async with asyncio.timeout(max(0, timeout - (time.monotonic() - started))):
                async with _transport.client.stream("POST", f"{settings.deepseek_base_url.rstrip('/')}/chat/completions", json=payload,
                            timeout=httpx.Timeout(timeout, connect=min(5, timeout)),
                            headers={"Authorization": f"Bearer {settings.deepseek_api_key}", "Content-Type": "application/json"}) as response:
                        response.raise_for_status()
                        timing.update(phase="waiting_for_model", headers_ms=int((time.monotonic() - started) * 1000))
                        if "text/event-stream" not in response.headers.get("content-type", ""):
                            await response.aread()
                            response.extensions["call_timing"] = timing
                            return response
                        parts, usage, finish, done = [], {}, None, False
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
    return {"attempt": attempt, "duration_ms": int((time.monotonic() - started) * 1000), "input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens"), "status": status, "error_code": error, "response_meta": {"finish_reason": ((body.get("choices") or [{}])[0].get("finish_reason") if isinstance(body, dict) else None)}}
