import os,sys,json
from pathlib import Path
HERE=Path(__file__).resolve().parent
OLD=Path('/opt/china2go-ai/releases/context-evaluation-20261126')
os.chdir(OLD);sys.path[:0]=[str(OLD/'test-deps'),'/opt/china2go-ai/backend']
os.environ.update(APP_PROFILE='evaluation',OUTBOUND_MODE='disabled',CHATWOOT_WRITE_ENABLED='false',LIVE_SOP_ENABLED='false',DATABASE_URL='sqlite://',RELAY_BASE_URL='',RELAY_API_TOKEN='')
candidate='--candidate' in sys.argv
if candidate:
 from app.reception_v3 import consultation,route_understanding,seasonal_quotes,runtime
 for name,module in [('consultation',consultation),('route_understanding',route_understanding),('seasonal_quotes',seasonal_quotes),('runtime',runtime)]:
  p=HERE/'payload/backend/app/reception_v3'/f'{name}.py';exec(compile(p.read_text(encoding='utf-8'),str(p),'exec'),module.__dict__)
import pytest
class Report:
 def __init__(self):self.results=[]
 def pytest_runtest_logreport(self,report):
  if report.when=='call' or report.failed:self.results.append({'id':report.nodeid,'outcome':report.outcome,'phase':report.when,'error':str(report.longrepr) if report.failed else ''})
r=Report()
names=['test_reception_v3_skills.py','test_reception_v3.py','test_route_understanding_20261031.py','test_contextual_pause_20261026.py','test_v3_delivery_conflicts.py','test_v3_intake_followup.py','test_visual_reception_20261106.py','test_auto_advisor_followup_20261017.py','test_context_budget_20261126.py']
code=pytest.main(['-q',*[str(OLD/'tests'/name) for name in names],'--disable-warnings'],plugins=[r])
(HERE/('regression-candidate.json' if candidate else 'regression-baseline.json')).write_text(json.dumps({'exit':int(code),'tests':r.results},ensure_ascii=False),encoding='utf-8')
