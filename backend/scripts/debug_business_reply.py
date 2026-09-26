"""Inspect customer-visible wording for selected, non-contact replay cases."""
import json
import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import model_gateway

original = model_gateway._post_with_deadline


def inspect_reply(payload, timeout):
    response = original(payload, timeout)
    content = response.json().get('choices', [{}])[0].get('message', {}).get('content', '')
    try:
        value = json.loads(content)
    except ValueError:
        value = {}
    if 'body' in value:
        print(json.dumps({'message_count': len(payload['messages']), 'body': value['body']}, ensure_ascii=False), flush=True)
    return response


model_gateway._post_with_deadline = inspect_reply
runpy.run_path(str(Path(__file__).with_name('evaluate_two_routes_real_questions.py')), run_name='__main__')
