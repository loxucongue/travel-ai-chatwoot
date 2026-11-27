import os,sys,json,secrets,hashlib,socket
from pathlib import Path
import re
import httpx
from sqlalchemy import create_engine,select,update
from sqlalchemy.orm import Session
assert socket.gethostname()=='iZt4n729nx7wezpcx0v6z1Z'
root=Path('/opt/china2go-ai');os.chdir(root/'backend');sys.path.insert(0,str(root/'backend'))
from app.models import User,AppSession,utcnow
from app.security import hash_password
engine=create_engine('sqlite:///'+str(root/'backend/data/app.db'))
e=root/'releases/traveler-deploy-evidence-20261127';e.mkdir(mode=0o700,exist_ok=True);mode=sys.argv[1];assert mode in ('before','after')
password=secrets.token_urlsafe(40);email='traveler-release-'+secrets.token_hex(6)+'@example.com'
with Session(engine) as db:
 u=User(email=email,display_name='Traveler release verification (temporary)',password_hash=hash_password(password),role='admin',active=True,must_change_password=False);db.add(u);db.commit();uid=u.id
out={'mode':mode,'temporary_identity':uid,'customer_messages_sent':0}
try:
 with httpx.Client(base_url='https://ai.luoxuecong.asia',timeout=25) as web:
  page=web.get('/');assert page.status_code==200 and 'text/html' in page.headers['content-type']
  assert page.content==(root/'frontend/dist/index.html').read_bytes()
  checked_web=[]
  for asset in re.findall(r'(?:src|href)="(/assets/[^"?#]+)',page.text):
   response=web.get(asset);assert response.status_code==200 and 'text/html' not in response.headers['content-type']
   assert response.content==(root/'frontend/dist'/asset.lstrip('/')).read_bytes()
   checked_web.append(asset)
  assert checked_web
  for path in ['/login','/automation','/conversations']:
   response=web.get(path);assert response.status_code==200 and response.content==page.content
  out['frontend_assets']=checked_web
 with httpx.Client(base_url='https://api.luoxuecong.asia/v1',timeout=25) as c:
  login=c.post('/auth/login',json={'email':email,'password':password});assert login.status_code==200,login.status_code
  csrf=login.json()['csrf_token'];r=c.get('/automation/route-products');assert r.status_code==200,r.status_code
  catalog=r.json();items=catalog['items'] if isinstance(catalog,dict) else catalog
  (e/('catalog-'+mode+'.json')).write_text(json.dumps(items,ensure_ascii=False))
  cfg=c.get('/automation/reception-config');assert cfg.status_code==200;config=cfg.json()['config'];enabled=config['routing']['enabled_route_variants']
  out['enabled_routes']=enabled;out['route_count']=len(items)
  if mode=='after':
   assert 'yunnan_tw_8d' in enabled and len(enabled)==9
   old=json.loads((e/'catalog-before.json').read_text());bykey={x['route_variant']:x for x in items}
   assert all(bykey[x['route_variant']]==x for x in old),'existing public catalog changed'
   route_previews=[]
   for product in items:
    asset=next((a for a in product.get('assets',[]) if a.get('available') and a.get('preview_url')),None)
    if asset:
     response=c.get(asset['preview_url']);assert response.status_code==200 and response.headers['content-type'].startswith('image/')
     assert hashlib.sha256(response.content).hexdigest()==asset['file_hash']
     route_previews.append(product['route_variant'])
   out['route_previews']=route_previews
   y=bykey['yunnan_tw_8d'];assert y['readiness']['ai_reply_ready'] and not y['readiness']['missing_assets'],y['readiness']
   checked=[]
   for a in y['assets']:
    assert a['available'] and a['live_approved'];r=c.get(a['preview_url']);assert r.status_code==200,(a['key'],r.status_code)
    assert r.headers['content-type'].startswith('image/jpeg') and hashlib.sha256(r.content).hexdigest()==a['file_hash'],a['key']
    checked.append(a['key'])
   assert len(checked)==49 and 'yunnan_tw_8d_yn_1' not in checked
   out.update(asset_previews=len(checked),existing_catalog_unchanged=True,readiness=y['readiness'])
  else:assert 'yunnan_tw_8d' in enabled and len(enabled)==9
  r=c.post('/auth/logout',headers={'X-CSRF-Token':csrf});assert r.status_code==204,r.status_code
  assert c.get('/auth/me').status_code==401
 out['status']='passed'
finally:
 with Session(engine) as db:
  db.execute(update(User).where(User.id==uid).values(active=False,password_hash=hash_password(secrets.token_urlsafe(40))))
  db.execute(update(AppSession).where(AppSession.user_id==uid).values(revoked_at=utcnow()));db.commit()
 out['temporary_identity_disabled']=True
 (e/('public-auth-'+mode+'.json')).write_text(json.dumps(out,ensure_ascii=False,indent=2))
print(json.dumps(out))
