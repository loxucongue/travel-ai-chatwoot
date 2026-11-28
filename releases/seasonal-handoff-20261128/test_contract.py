import os,sys,copy,json
from pathlib import Path
HERE=Path(__file__).resolve().parent
os.chdir('/opt/china2go-ai/backend');sys.path.insert(0,os.getcwd())
os.environ.update(APP_PROFILE='evaluation',OUTBOUND_MODE='disabled',CHATWOOT_WRITE_ENABLED='false',DATABASE_URL='sqlite://')
import app.reception_v3 as package
package.__path__.insert(0,str(HERE/'payload/backend/app/reception_v3'))
from app.reception_v3.reference_introduction import validation_error
from app.reception_v3.runtime import Decision
passed=[]
for target,days in [('peach_11d_2027',11),('tibet_overview_10d',10),('tibet_ali_15d',15)]:
 d=Decision(action='reply',route_choice='recommend',reference_introduction_route=target,profile={'discussion_route':target}).model_dump()
 c={'event':'customer_message','skills':{'routes':{target:{}}},'profile':{},'state':{},'messages':[]}
 assert not validation_error(d,c);passed.append(target+' accepted as reference')
 bad=copy.deepcopy(d);bad['route_variant']=target
 assert validation_error(bad,c);passed.append(target+' cannot silently select')
 bad=copy.deepcopy(c);bad['profile']['trip_days']=days-1
 assert validation_error(d,bad);passed.append(target+' short duration protected')
 for status in ['sent','delivered','read','failed']:
  bad=copy.deepcopy(c);bad['messages']=[{'status':status,'reference_introduction_route':target}]
  assert bool(validation_error(d,bad)) == (status!='failed');passed.append(target+' dedup '+status)
print(json.dumps({'passed':len(passed),'checks':passed}))
