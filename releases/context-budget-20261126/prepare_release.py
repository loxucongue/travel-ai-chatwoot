"""Build reproducible four-file manifest and reuse the validated rolling handoff."""
import hashlib,json
from pathlib import Path
HERE=Path(__file__).resolve().parent;PREVIOUS=HERE.parent/'march-recommendation-20261126'
for name in ('publish.py','rollback.py','worker_bootstrap.py','test_handoff.py'):
 text=(PREVIOUS/name).read_text(encoding='utf8')
 if name=='publish.py':
  text=text.replace('eight-file','four-file').replace("backups/march-recommendation-20261126","backups/context-budget-20261126").replace("'.march.tmp'","'.context.tmp'").replace("'-march-standby'","'-context-standby'").replace("len(M)==8","len(M)==4")
  text=text.replace("compile((HERE/'payload/backend/app/reception_v3/runtime.py').read_text(),'runtime.py','exec')", "for m in M:compile((HERE/m['payload']).read_text(),m['target'],'exec')")
 (HERE/name).write_text(text,encoding='utf8',newline='\n')
baseline={m['target']:m for m in json.loads((HERE/'baseline.json').read_text())};manifest=[]
for p in sorted((HERE/'payload').rglob('*.py')):
 p.write_text(p.read_text(encoding='utf8'),encoding='utf8',newline='\n')
 target=p.relative_to(HERE/'payload').as_posix()
 manifest.append({**baseline[target],'payload':p.relative_to(HERE).as_posix(),'after_sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
(HERE/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf8',newline='\n')
print(json.dumps(manifest,indent=2))
