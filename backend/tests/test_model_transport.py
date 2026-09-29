import json

import httpx

from app import model_transport as transport
from app.config import settings


def test_stream_retains_thinking_for_tool_continuation_without_merging_into_answer():
    chunks = [
        {'choices': [{'delta': {'reasoning_content': 'Read '}}]},
        {'choices': [{'delta': {'reasoning_content': 'the route.'}}]},
        {'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'call-1',
            'function': {'name': 'load_skill', 'arguments': '{"name":'}}]}}]},
        {'choices': [{'delta': {'tool_calls': [{'index': 0,
            'function': {'arguments': '"peach-9d-2027"}'}}]}, 'finish_reason': 'tool_calls'}]},
        {'choices': [], 'usage': {'prompt_tokens': 12, 'completion_tokens': 10}},
    ]
    data = ''.join('data: ' + json.dumps(c) + '\n\n' for c in chunks) + 'data: [DONE]\n\n'
    transport.close_deepseek_transport()
    transport._transport.client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=data)))
    transport._transport.identity = (settings.deepseek_base_url, settings.deepseek_api_key)
    try:
        body = transport._post_unmetered({'stream': True}, 5).json()
        message = body['choices'][0]['message']
        assert message['content'] == ''
        assert message['reasoning_content'] == 'Read the route.'
        assert json.loads(message['tool_calls'][0]['function']['arguments']) == {'name': 'peach-9d-2027'}
        assert body['usage']['completion_tokens'] == 10
    finally:
        transport.close_deepseek_transport()
