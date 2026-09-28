"""One model owns customer, completion and timer decisions."""
from pathlib import Path
from typing import Literal

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
    handoff_reason: str = ''
    reason: str = ''


def run_agent(context: dict):
    return call_json_node(
        node='reception_v3',
        system_prompt=Path(__file__).with_name('SKILL.md').read_text(encoding='utf-8'),
        input_data=context, parser=lambda value: Decision.model_validate(value).model_dump(),
        max_tokens=4000, repair_prompt='只修正 JSON 字段和类型，不审核、裁剪或改写业务话术。',
    )
