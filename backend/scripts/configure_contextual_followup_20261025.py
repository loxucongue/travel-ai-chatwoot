"""Reviewable content migration; never writes production or changes product facts."""
from copy import deepcopy
import hashlib
import json

FOLLOWUP_GUIDANCE = (
    '沉默跟進先承接客戶已表達的興趣與尚未解決的顧慮。客戶明確說主要想看某景點或體驗時，'
    '本輪主題應直接圍繞該興趣；不要換成其他體驗，也不要用「已去過所以換個角度」忽略其選擇。'
    '尚無具體需求時才從未發送素材選一組，不按固定順序。原話可按客戶問題適度改寫，'
    '不得補未確認事實，不附加留資或催日期人數。asset_ids僅在followup_materials中仍可用時發送；'
    '不借用其他線路圖片，不重發已發內容，不承諾拍攝時點、天氣或未確認服務。'
)


def update_scripts(scripts):
    result = deepcopy(scripts)
    for script in result:
        if 'silence_followup' in script.get('topics', []):
            script['usage_note'] = FOLLOWUP_GUIDANCE
    return result


def package_delta(package):
    updated = deepcopy(package)
    updated['fixed_answers'] = update_scripts(package.get('fixed_answers', []))
    if updated != package:
        updated['package_version'] = package['package_version'].split('+contextual-followup-')[0] + '+contextual-followup-20261025'
    return updated


def material_rules_from_routes(routes, existing=()):
    """Migrate only approved silence scripts; never broaden route eligibility."""
    rules = deepcopy(list(existing))
    known = {r['id'] for r in rules}
    for route_id, route in routes.items():
        for script in route.get('scripts', route.get('fixed_answers', [])):
            if 'silence_followup' not in script.get('topics', []) or script.get('status', 'active') != 'active':
                continue
            if not script.get('answer_text', '').strip():
                continue
            key = 'followup-' + hashlib.sha256(f"{route_id}:{script['id']}".encode()).hexdigest()[:16]
            if key in known:
                continue
            rules.append({'id':key, 'name':f"{route.get('name', route_id)} · {script.get('name', script['id'])}",
                'enabled':True, 'purpose':'followup', 'route_variants':[route_id], 'any_labels':[],
                'scenario':f"{script.get('name', script['id'])}。{FOLLOWUP_GUIDANCE}",
                'approved_text':script['answer_text'], 'asset_keys':list(script.get('asset_ids', []))})
            known.add(key)
    return rules


def main():
    import argparse
    from pathlib import Path
    parser=argparse.ArgumentParser(description='Generate review-only configuration patch from a frozen compiled snapshot')
    parser.add_argument('--snapshot',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    source=Path(args.snapshot).resolve(); target=Path(args.output).resolve()
    if source == target:
        raise ValueError('output_must_not_overwrite_snapshot')
    data=source.read_bytes(); snapshot=json.loads(data); bundle=snapshot['bundle']
    patch={'silence':{'material_selection':'configured_rules'},
           'material_rules':material_rules_from_routes(bundle['routes'],bundle.get('material_rules', []))}
    result={'review_only':True,'deployed':False,'source_snapshot_sha256':hashlib.sha256(data).hexdigest(),
        'precondition':'Before applying, compare latest configuration and route versions to this snapshot; merge newer rules. Do not overwrite concurrent business edits.',
        'reception_config_patch':patch,
        'route_usage_updates':{key:[{'id':s['id'],'usage_note':FOLLOWUP_GUIDANCE} for s in route.get('scripts',[]) if 'silence_followup' in s.get('topics',[])] for key,route in bundle['routes'].items()}}
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')


if __name__=='__main__':
    main()
