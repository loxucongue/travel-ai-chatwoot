"""Small budgeted model acceptance; transport permits only model inference."""
import os
import sys
import json
import time
from pathlib import Path
from dataclasses import asdict
from unittest.mock import patch
import argparse

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'backend'))
OUT=ROOT/'output/reply-consistency-20260926'
os.environ.update(APP_PROFILE='evaluation',OUTBOUND_MODE='disabled',CHATWOOT_WRITE_ENABLED='false',
                  LIVE_SOP_ENABLED='false',DATABASE_URL='sqlite://',DEEPSEEK_MODEL='deepseek-flash',
                  MODEL_TEST_COST_LEDGER=str(OUT/'model-cost.json'))
import httpx
from app.config import settings
from app.decision_service import generate_decision
from app.route_packages import ROUTES
from app.reception_v2 import ENGINE_RELEASE_ID


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--case',action='append')
    parser.add_argument('--label',default='first')
    args=parser.parse_args()
    # Exported read-only from production: identities alone are not evidence of
    # PDF contents. Keep original incomplete-fixture runs as failed evidence.
    materials=json.loads((OUT/'approved-materials.json').read_text(encoding='utf8'))
    original=httpx.AsyncClient.send
    async def guarded(client,request,*a,**kw):
        if request.method!='POST' or str(request.url)!=settings.deepseek_base_url.rstrip('/')+'/chat/completions':
            raise AssertionError('non_model_http_prohibited')
        return await original(client,request,*a,**kw)
    def no_sync(*a,**kw):
        raise AssertionError('non_model_http_prohibited')
    cases=[]
    for route,days in [('peach_9d_2027','9'),('peach_11d_2027','11')]:
        for kind,text in [
            ('optout','不要再主動聯絡我，另外小費多少？'),
            ('negation','我不是說不要再聯絡，只是想先問在哪裡集合？'),
            ('channel','不要用WhatsApp聯絡我，請問在哪集合？'),
            ('materials','請給我高原注意事項PDF和用車照片'),
            ('mixed',f'請給我{days}日完整介紹，包含行程圖、住宿和車照，另外3月28日還有位置嗎？我們4位。'),
            ('conditions',f'{days}日6個人多少錢？我是台灣旅客75歲，可以報名嗎？')]:
            cases.append((route+':'+kind,route,kind,text))
    cases.extend([(name+':repeat',route,kind,text) for name,route,kind,text in cases
                  if name in {'peach_9d_2027:optout','peach_11d_2027:materials','peach_11d_2027:conditions'}])
    rows=[]
    OUT.mkdir(parents=True,exist_ok=True)
    with patch.object(httpx.AsyncClient,'send',guarded),patch.object(httpx.Client,'send',no_sync):
        for name,route,kind,text in cases:
            if args.case and name not in args.case:
                continue
            spec=ROUTES[route]
            context={'module':'reply','engine_version':'v2','customer_text':text,'route_variant':route,
                     'source_message_id':'isolated-'+name,'context_messages':[],
                     'available_materials':materials}
            start=time.monotonic()
            row={'case':name,'input':text,'release':ENGINE_RELEASE_ID}
            try:
                decision,logs,_,trace=generate_decision(context)
                issues=[]
                if not decision.reply:issues.append('missing_reply')
                scopes=[e.get('scope') for e in decision.v2_events if e.get('type')=='contact_refused']
                if kind=='optout' and 'all' not in scopes:issues.append('optout_not_recorded')
                if kind in {'negation','channel'} and ('all' in scopes or 'stop_automation' in decision.safety_flags):issues.append('false_global_refusal')
                if kind=='channel' and 'WhatsApp' not in scopes:issues.append('channel_refusal_missing')
                if kind=='materials':
                    expected=set(spec['groups']['vehicle_reference']['assets'])|set(spec['groups'][spec['policies']['post_capture_material_group']]['assets'])
                    if not expected<=set(decision.material_keys):issues.append('missing_requested_materials')
                if kind=='mixed':
                    if decision.action!='handoff':issues.append('missing_availability_handoff')
                    for group in ['itinerary_overview','hotel_reference','vehicle_reference']:
                        if not set(spec['groups'][group]['assets'])<=set(decision.material_keys):issues.append('missing_'+group)
                if kind=='conditions':
                    body=(decision.reply or '').replace(',','').replace('，','')
                    if ('9980' if '9d' in route else '11480') not in body:issues.append('published_price_not_answered')
                    if '健康證明' not in body:issues.append('health_certificate_condition_missing')
                    if decision.action!='reply':issues.append('unnecessary_price_handoff')
                row.update(decision=asdict(decision),trace=trace,issues=issues)
            except Exception as exc:
                row.update(error=str(exc),metrics=getattr(exc,'model_metrics',{}),logs=getattr(exc,'logs',[]))
            row['seconds']=round(time.monotonic()-start,3)
            rows.append(row)
            (OUT/(args.label+'.json')).write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf8')
            print(name,row.get('error',row.get('issues')),row['seconds'],flush=True)
            if 'budget' in row.get('error',''):
                break
    if any(row.get('error') or row.get('issues') for row in rows):
        raise SystemExit(1)


if __name__=='__main__':main()
