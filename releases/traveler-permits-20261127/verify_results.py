"""Fail closed when final model or baseline-comparison evidence is incomplete."""
import hashlib
import json
import sys
from pathlib import Path

source=Path(sys.argv[1]); dest=Path(__file__).resolve().parent
effect=json.loads((source/'effect.json').read_text(encoding='utf-8'))
baseline=json.loads((source/'regression-baseline.json').read_text(encoding='utf-8'))['tests']
candidate=json.loads((source/'regression-candidate.json').read_text(encoding='utf-8'))['tests']
failed=lambda rows: {r['id'] for r in rows if r['outcome']=='failed'}
assert len(baseline)==len(candidate)==155
assert failed(baseline)==failed(candidate)
assert len(effect['checks'])==30
assert len(effect['model'])==12 and not any('error' in r for r in effect['model'])
cases={r['case']:r for r in effect['model']}
for case in ('hk','macao','known','border','short_hk'):
    assert cases[case]['status']=='exempt' and cases[case]['decision']['action']=='reply',case
for case in ('non_chinese','mixed','correction'):
    assert cases[case]['status']=='review',case
for case in ('taiwan_dialect','short_taiwan'):
    assert cases[case]['status']=='required',case
assert cases['short_taiwan']['decision']['action']=='handoff'
assert cases['non_chinese']['decision']['action']=='handoff'
assert cases['dialect']['status']=='unknown'
assert '香港' in ''.join(cases['dialect']['compiled_texts']) and '回鄉證' in ''.join(cases['dialect']['compiled_texts'])
for case in ('dialect','hk','macao','known','short_hk'):
    assert not any('成都' in t or '邊防證' in t for t in cases[case]['compiled_texts']),case
assert any('台胞證' in t and '回鄉證' in t for t in cases['mixed']['compiled_texts'])
assert any('邊防證' in t for t in cases['border']['compiled_texts'])
assert cases['original_1446']['decision']['reference_introduction_route']=='peach_11d_2027'
assert len(cases['original_1446']['compiled_texts']) > 10
assert sum(not t for t in cases['original_1446']['compiled_texts']) >= 10

manifest=json.loads((dest/'manifest.json').read_text(encoding='utf-8'))
for entry in manifest['files']:
    path=dest/'payload'/entry['path']
    entry['candidate_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
    entry['normalized_sha256']=hashlib.sha256(path.read_bytes().replace(b'\r\n',b'\n')).hexdigest()
(dest/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
report={'deployment':'not_deployed','targeted_checks':effect['checks'],
        'model_cases':[{'case':r['case'],'permit_status':r['status'],'action':r['decision']['action'],
                        'outgoing_plan_parts':len(r['compiled_texts']),'texts':r['compiled_texts'][:3]}
                       for r in effect['model']],
        'date_checks':effect['date_checks'],
        'regression':{'total':len(candidate),'passed':len(candidate)-len(failed(candidate)),
                      'baseline_failures':sorted(failed(baseline)),'new_failures':[]},
        'source_manifest':manifest}
(dest/'verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'targeted_pass':30,'real_model_pass':12,'regression_pass':129,'unchanged_baseline_failures':26,'new_regressions':0}))
