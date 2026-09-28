"""Website copy reaches the actual agent and configured delivery unchanged."""
import re
from pathlib import Path

import pytest

from app.reception_v2.runtime import _messages
from app.reception_v2.skill_registry import SkillRegistry
from app.route_packages import ROUTES, PACKAGE_ROOT


@pytest.mark.parametrize('days,directory', [(9,'02-peach-9d'),(11,'01-peach-everest-11d')])
def test_entire_website_branch_is_loaded_with_resolvable_images(days,directory):
    route=ROUTES[f'peach_{days}d_2027']
    skill=SkillRegistry().load(f'peach-{days}d-2027')
    source=(PACKAGE_ROOT.parent/'website-7693-full'/directory/'content.md').read_text(encoding='utf-8')
    # These three source instructions were superseded by the advisor-entry fix.
    replaced_entry_lines = {
        'Q．預計幾位一起去？',
        '依人數答案分流：自己一人→文01-2／2-3人→文01-3／4-6人或6人以上→文01-1／逾1分鐘無回應或系統未能判斷 → 直接播放文01',
        '文 01（僅限1分鐘無回應或系統未判斷出人數答案時，跳轉至此）',
    }
    assert replaced_entry_lines <= set(source.splitlines())
    for line in source.splitlines():
        if not line.strip() or '![' in line:
            continue
        if line in replaced_entry_lines:
            assert line not in skill['instructions']
            continue
        assert line in skill['instructions'], line
    assert route['groups']['entry_question']['text'] in skill['instructions']
    assert '人數未知 → 使用Q承接後直接介紹，不等待回覆' in skill['instructions']
    assert '{{script:' not in skill['instructions']
    assets={a for group in route['groups'].values() for a in group['assets']}
    assert set(re.findall(r'素材 key：(.*?)\]',skill['instructions'])) <= assets
    assert len(set(re.findall(r'素材 key：(.*?)\]',skill['instructions']))) == (11 if days==9 else 12)


@pytest.mark.parametrize('days', [9,11])
def test_model_input_uses_source_copy_and_configured_update(monkeypatch,days):
    key=f'peach_{days}d_2027'
    original=ROUTES[key]
    from copy import deepcopy
    route=deepcopy(original)
    script=next(a for a in route['fixed_answers'] if a['id']=='hotel')
    script['answer_text']='測試：這是業務剛更新的住宿話術。'
    monkeypatch.setitem(ROUTES,key,route)
    messages=_messages({'module':'reply','route_variant':key,'customer_text':'住宿如何？',
                        'context_messages':[{'role':'assistant','content':'已完成介紹'}]},SkillRegistry())
    system=messages[0]['content']
    assert script['answer_text'] in system
    assert '十人小團優惠價' in system
    assert '您有沒有微信或是Line的QR code' in system
    assert '只问台湾75岁能否报名的minimum_answer' not in system
