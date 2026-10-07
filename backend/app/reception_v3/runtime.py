"""One model owns customer, completion and timer decisions."""
from typing import Literal
import json
import time

from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.model_transport import _post_with_deadline, _call_log, EvaluationCallError
from app.model_gateway import _json_object, _request_hash, combine_digests
from app.reception_v3.skills import SkillRegistry
from app.reception_v3.prompt import SYSTEM_PROMPT


class Message(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: str = ''
    script_id: str | None = ''
    asset_keys: list[str] = Field(default_factory=list)


class Decision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['reply', 'queue', 'wait', 'handoff'] = 'reply'
    route_variant: str = ''
    messages: list[Message] = Field(default_factory=list)
    start_introduction: bool = False
    interrupt: bool = False
    profile: dict = Field(default_factory=dict)
    opt_out: bool | None = None
    next_check_minutes: float | None = Field(default=None, gt=0, le=10080)
    stop_followup: bool = False
    handoff_reason: str = ''
    reason: str = ''


def run_agent(context: dict):
    registry = SkillRegistry(context['skills'], context.get('available_materials', []))
    loaded = {}
    def load(name):
        result = registry.load(name)
        loaded[name] = result['digest']
        return result
    common = load('tibet-reception')
    preloaded = []
    selected = registry.route_skill(context.get('route_variant', ''))
    if selected:
        preloaded.append(load(selected))
    catalog = [{'id': key, 'name': route['name'], 'aliases': route.get('aliases', [])}
               for key, route in context['skills']['routes'].items()]
    prompt = (SYSTEM_PROMPT + '\n'
              'Skill 索引：\n' + json.dumps(registry.index(), ensure_ascii=False)
              + '\n线路目录（仅用于识别，不是完整产品资料）：\n' + json.dumps(catalog, ensure_ascii=False)
              + '\n已加载线路 Skill：\n' + json.dumps(preloaded, ensure_ascii=False)
              + '\n通用接待 Skill：\n' + common['instructions'])
    current = {key: value for key, value in context.items()
               if key not in ('skills', 'website_facts', 'website_version', 'available_materials', 'messages')}
    # Delivery metadata and earlier model reasons are not customer dialogue.
    # Send actual speech with chat roles so repeated invitations are visible.
    current['state'] = {key: value for key, value in current.get('state', {}).items()
                        if key != 'last_reason'}
    current['buffered_questions'] = [
        {'content': m.get('content', ''), 'created_at': m.get('created_at')}
        for m in current.get('buffered_questions', [])]
    # These are already the active, scoped facts selected by the service. Make
    # their short text visible before the model decides something is unknown;
    # the read tool still exposes the complete records and provenance.
    base_service = context['skills'].get('service_knowledge', {})
    service_facts = {fact['id']: fact for fact in base_service.get('facts', [])}
    service_facts.update({fact.get('id', f'website:{i}'): fact
                          for i, fact in enumerate(context.get('website_facts', []))})
    knowledge = {
        'version': context.get('website_version'),
        'facts': list(service_facts.values()),
    }
    if base_service:
        knowledge['service_version'] = base_service.get('version')
    current['service_knowledge'] = {**knowledge, 'facts': [
        {key: fact[key] for key in ('id', 'text', 'source', 'source_ref') if key in fact}
        for fact in knowledge['facts']]}
    # The enabled catalog is small. Expose facts for comparison without loading
    # another route's entire SOP and sales scripts into the current reception.
    current['route_facts'] = {
        key: {'version': route['version'], 'facts': route['facts']}
        for key, route in context['skills']['routes'].items()
    }
    messages = [{'role': 'system', 'content': prompt}]
    for item in context.get('messages', []):
        content = item.get('content', '')
        if item.get('content_type') == 'input_select':
            content += '\n选项：' + '、'.join(i['title'] for i in item.get('content_attributes', {}).get('items', []))
        elif item.get('content_type', 'text') != 'text':
            content = '[已交付附件] ' + json.dumps({key: item[key] for key in
                ('content_type', 'asset_key', 'name', 'filename') if key in item}, ensure_ascii=False)
        if content:
            messages.append({'role': 'user' if item.get('direction') == 'incoming' else 'assistant',
                             'content': content})
    messages.append({'role': 'user', 'content': json.dumps(current, ensure_ascii=False)})
    tools = [
        {'type': 'function', 'function': {'name': 'load_skill',
         'description': '读取指定 Skill 正文及当前配置的完整原文、SOP、事实和图片引用。',
         'parameters': {'type': 'object', 'properties': {'name': {'type': 'string',
                        'enum': [s['name'] for s in registry.index()]}}, 'required': ['name'], 'additionalProperties': False}}},
        {'type': 'function', 'function': {'name': 'get_service_facts',
         'description': '读取当前可用的官网通用服务知识，回答线路资料没有覆盖的问题。',
         'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}}},
    ]
    logs, hashes = [], []
    deadline = time.monotonic() + 60
    repaired = False
    try:
        for turn in range(6):
            payload = {'model': settings.deepseek_model, 'messages': messages, 'tools': tools,
                       'tool_choice': 'auto', 'response_format': {'type': 'json_object'},
                       'thinking': {'type': 'enabled'}, 'reasoning_effort': 'low', 'max_tokens': 4000,
                       'stream': True, 'stream_options': {'include_usage': True}}
            hashes.append(_request_hash(payload))
            if len(json.dumps(messages, ensure_ascii=False)) > settings.deepseek_max_input_characters:
                raise ValueError('v3_context_too_large')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('v3_model_timeout')
            started = time.monotonic()
            response = _post_with_deadline(payload, min(remaining, settings.deepseek_timeout_seconds))
            response.raise_for_status()
            body = response.json()
            message = body['choices'][0]['message']
            log = {**_call_log(turn + 1, started, body, 'completed', None),
                   'node': 'reception_v3', 'loaded_skills': dict(loaded),
                   'system_characters': len(prompt), 'reasoning_effort': 'low', 'tool_calls': []}
            logs.append(log)
            if message.get('tool_calls'):
                messages.append({'role': 'assistant', 'content': message.get('content'),
                                 'reasoning_content': message.get('reasoning_content', ''),
                                 'tool_calls': message['tool_calls']})
                for call in message['tool_calls']:
                    function = call['function']
                    try:
                        args = json.loads(function['arguments'])
                        if function['name'] == 'load_skill':
                            result = load(args['name'])
                        elif function['name'] == 'get_service_facts':
                            result = knowledge
                        else:
                            raise ValueError('unknown_tool')
                    except (ValueError, KeyError, TypeError) as exc:
                        result = {'error': str(exc)}
                    log['tool_calls'].append({'name': function['name'], 'arguments': function['arguments'],
                                              'digest': result.get('digest'), 'error': result.get('error')})
                    messages.append({'role': 'tool', 'tool_call_id': call['id'],
                                     'content': json.dumps(result, ensure_ascii=False)})
                log['loaded_skills'] = dict(loaded)
                continue
            try:
                decision = Decision.model_validate(_json_object(message.get('content') or '')).model_dump()
            except (ValueError, TypeError) as exc:
                if repaired:
                    raise
                repaired = True
                log.update(status='invalid_json', error_code=str(exc))
                messages.extend([{'role': 'assistant', 'content': message.get('content') or '',
                                  'reasoning_content': message.get('reasoning_content', '')},
                                 {'role': 'user', 'content': f'只修正 JSON 字段和类型，不审核或改写话术。错误：{exc}'}])
                continue
            # The model may identify a route without requesting its body (for
            # example after an earlier comparison). Activate that selected
            # Skill before completing this turn, just as for a known route at
            # entry. This loads context; it does not audit or edit reply text.
            chosen = registry.route_skill(decision['route_variant'])
            if chosen and chosen not in loaded:
                activated = load(chosen)
                log.update(status='skill_activation', loaded_skills=dict(loaded),
                           activated_skill=chosen)
                messages.append({'role': 'system', 'content':
                    '根据你本轮选择的线路，现已加载对应 Skill。上一候选尚未发送。'
                    '请依据这些原文完成本轮 JSON 决策：\n' + json.dumps(activated, ensure_ascii=False)})
                continue
            log.update(script_ids=[m['script_id'] for m in decision['messages'] if m['script_id']],
                       asset_keys=[k for m in decision['messages'] for k in m['asset_keys']])
            return decision, logs, combine_digests(*hashes)
        raise ValueError('v3_tool_round_limit')
    except Exception as exc:
        raise EvaluationCallError(str(exc), logs, combine_digests(*hashes)) from exc
