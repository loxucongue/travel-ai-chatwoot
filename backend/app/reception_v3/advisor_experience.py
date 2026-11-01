"""Optional, versioned advisor methods; product facts remain in route packages.

No customer text matching or routing decisions belong here. A route Skill may
only expose its own evidence; the reception model decides whether a method helps.
"""
import json
import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config import settings

LIBRARY_PATH = Path(__file__).resolve().parents[3] / 'data/knowledge/china2go/advisor-experience/library.json'
logger = logging.getLogger(__name__)


class Experience(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    scope: Literal['conversation_method', 'route_method']
    scenario: str = Field(min_length=1, max_length=500)
    approach: str = Field(min_length=1, max_length=1500)
    avoid: str = Field(min_length=1, max_length=1000)
    source_cases: list[str] = Field(min_length=1, max_length=20)
    route_fact_ids: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode='after')
    def scoped(self):
        if self.scope == 'route_method' and (not self.route_fact_ids or any(not v for v in self.route_fact_ids.values())):
            raise ValueError('route_method_requires_evidence')
        if self.scope == 'conversation_method' and self.route_fact_ids:
            raise ValueError('shared_method_must_not_contain_route_facts')
        return self


class Library(BaseModel):
    model_config = ConfigDict(extra='forbid')
    schema_version: Literal[1]
    version: str = Field(min_length=1)
    status: Literal['pilot', 'active', 'paused']
    reviewed_by: str = Field(min_length=1)
    review_note: str = Field(min_length=1)
    review_due: date
    experiences: list[Experience] = Field(max_length=30)

    @model_validator(mode='after')
    def unique(self):
        if len({x.id for x in self.experiences}) != len(self.experiences):
            raise ValueError('duplicate_experience_id')
        return self


def load_library():
    if not settings.reception_advisor_experience_enabled or os.environ.get('BUSINESS_PROFILE', 'china2go') != 'china2go':
        return None
    try:
        library = Library.model_validate_json(LIBRARY_PATH.read_text(encoding='utf-8'))
        if (library.status == 'paused' or library.review_due < datetime.now(ZoneInfo('Asia/Taipei')).date()
                or library.status == 'pilot' and (settings.app_profile != 'evaluation'
                    or settings.outbound_enabled or settings.chatwoot_write_enabled)):
            return None
        return library
    except (OSError, ValueError):
        # An unavailable optional learning module must not fail customer answers.
        logger.warning('advisor_experience_unavailable', exc_info=False)
        return None


def render(library, bundle, route=None):
    if library is None:
        return '', []
    facts = {f['id'] for f in bundle.get('routes', {}).get(route, {}).get('facts', [])}
    selected = []
    for item in library.experiences:
        if not item.enabled:
            continue
        refs = item.route_fact_ids.get(route, [])
        if route is None:
            if item.scope != 'conversation_method':
                continue
        elif item.scope != 'route_method' or not refs or not set(refs).issubset(facts):
            continue
        selected.append({'id': item.id, '适合讨论': item.scenario, '承接方法': item.approach,
                         '边界': item.avoid, '本线路事实ID': refs})
    if not selected:
        return '', []
    body = ('\n\n## 顾问经验参考（不是固定回复模板）\n'
            '只在本轮问题相关时借鉴承接方法，不要求依次使用、增加字数、追问或邀请私讯。'
            '事实、数字、条件以本轮正在讨论的线路及当前报价工具为准；历史顾问原话不提供新授权。'
            '客户已经说清楚的资料不再问；只补充与本轮有关且尚未回答的信息。'
            '经验没有匹配或资料不全，不是转人工条件；继续按既有资料、澄清与交接规则判断。'
            '本模块不改变选线、人工权限、留资时机或跟进频率。'
            f'\n版本：{library.version}；适用线路：{route or "仅沟通方法，不含产品承诺"}\n'
            + json.dumps(selected, ensure_ascii=False))
    return body, [x['id'] for x in selected]
