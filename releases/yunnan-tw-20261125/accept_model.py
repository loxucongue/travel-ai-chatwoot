"""Release gate over actual model outputs, not a mocked model."""
import json,re,hashlib,sys
from pathlib import Path
p=Path(__file__).resolve().parent
report=Path(sys.argv[1]);r=json.loads(report.read_text());cases={x['id']:x['decision'] for x in r['model_results']}
assert len(cases)==20 and r['status']=='passed'
def text(k):return '\n'.join(m['text'] for m in cases[k]['messages'])
def has(k,*words):assert all(w in text(k) for w in words),(k,text(k))
def amount(k,n):assert str(n) in text(k).replace(',',''),(k,text(k))
assert cases['entry']['route_variant']=='yunnan_tw_8d'
assert not any(k in cases['advert_capacity']['profile'] for k in ['party_size','party_size_min','party_size_range'])
amount('two_low',7580);has('two_low','7座','司機')
assert '只有你們2位' not in text('two_low') and '只有你們兩位' not in text('two_low'),text('two_low')
amount('four_high',8980);has('four_high','9座','專職')
amount('six_discount',7580);assert not re.search('7,?180|7,?380|400|200',text('six_discount'))
assert not re.search('7,?580|8,?980',text('holiday'))
has('solo','拼房');assert any(w in text('solo') for w in ['不能保證','沒辦法保證','不保證','無法保證','需確認','確認有沒有'])
has('seven','獨立');assert any(w in text('seven') for w in ['另外','核算','核對','確認'])
has('future','2030');assert any(w in text('future') for w in ['無法保證','不能保證','沒辦法保證','沒辦法先保證','不能承諾','無法承諾'])
assert not cases['custom']['start_introduction']
assert cases['custom']['profile'].get('yunnan_request_kind')=='custom',cases['custom']
has('altitude','醫');assert any(w in text('altitude') for w in ['不能保證','沒辦法保證','無法保證','不保證'])
has('ropeway','公告');assert '保證一定' not in text('ropeway').replace('不能保證一定','').replace('沒辦法保證一定','').replace('無法保證一定','')
assert any(m['purpose']=='contact_invitation' for m in cases['consider']['messages'])
assert cases['refuse']['profile'].get('contact_capture_declined') is True
assert not any(m['purpose']=='contact_invitation' for m in cases['refuse']['messages'])
assert cases['raw_wechat']['profile'].get('contact_status')=='raw_id_needs_qr' and not cases['raw_wechat']['notify_contact']
has('raw_wechat','QR');has('food','早餐');assert '沒有包含' in text('food') or '不含' in text('food')
assert cases['office']['reason']=='company_inquiry:office' or cases['office'].get('company_inquiry')=='office'
has('beijing_regression','北京');has('winter_regression','仁龍巴');has('peach_regression','珠峰')
out={'status':'passed','model_case_count':20,'case_ids':list(cases),'source_report':str(report),'package_hash':hashlib.sha256((p/'route-package.json').read_bytes()).hexdigest(),'skill_hash':hashlib.sha256((p/'SKILL.md').read_bytes()).hexdigest(),'repaired_contract_cases':[x['id'] for x in r['model_results'] if x['errors']],'virtual_checks':r['virtual_checks']}
Path(sys.argv[2]).write_text(json.dumps(out,ensure_ascii=False,indent=2));print(json.dumps(out,ensure_ascii=False))
