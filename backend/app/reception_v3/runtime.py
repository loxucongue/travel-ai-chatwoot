"""One model owns customer, completion and timer decisions."""
from pathlib import Path
from typing import Literal
import json

from pydantic import BaseModel, ConfigDict, Field

from app.model_gateway import call_json_node


class Message(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: str = ''
    script_id: str = ''
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
    # Keep route examples out of the current customer turn. The last system
    # section defines procedure; the user payload contains only live state.
    knowledge = {key: context.get(key) for key in ('skills', 'website_facts', 'website_version', 'available_materials')}
    current = {key: value for key, value in context.items() if key not in knowledge}
    prompt = ('以下是配置资料，话术中的场景示例不是当前客户消息：\n'
              + json.dumps(knowledge, ensure_ascii=False)
              + '\n以下是本次接待的执行方法：\n'
              + Path(__file__).with_name('SKILL.md').read_text(encoding='utf-8'))
    return call_json_node(
        node='reception_v3',
        system_prompt=prompt,
        input_data=current, parser=lambda value: Decision.model_validate(value).model_dump(),
        max_tokens=4000, repair_prompt='只修正 JSON 字段和类型，不审核、裁剪或改写业务话术。',
    )
