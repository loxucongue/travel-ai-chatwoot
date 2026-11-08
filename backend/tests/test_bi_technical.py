from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from app.bi_technical import read_technical, distribution, failure_reason
from app.automation_models import AutomationSession, AutomationRun
from app.models import User, OutboundMessage, HandoffTask, UserInboxScope
from test_bi_dashboard import setup

NOW = datetime(2026,10,30,8,tzinfo=timezone.utc)

def report(db, **kwargs):
    db.flush()
    return read_technical(db, kwargs.pop('user', db.get(User,1)), '2026-10-30','2026-10-30',now=NOW,**kwargs)

def run(db, *, sid=1, cid=1, rid=1, environment='live', module='reply', calls=None, at='2026-10-30T01:00:00Z', status='completed'):
    session = db.get(AutomationSession,sid)
    if not session:
        session=AutomationSession(id=sid,owner_id=1,environment=environment,conversation_state_id=cid)
        db.add(session); db.flush()
    db.add(AutomationRun(id=rid,session_id=sid,module=module,generation=1,idempotency_key=f'run-{rid}',status=status,
        created_at=at,completed_at='2026-10-30T01:00:05Z',
        trace={} if calls is None else {'model_http_calls':calls},
        input_snapshot={'buffered_questions':[{'direction':'incoming','created_at':'2026-10-30T00:59:50Z'}]}))
    session.messages=[*(session.messages or []),{'id':f'p{rid}','run_id':rid,'direction':'outgoing'}]
    db.flush()

def sent(db,rid=1,cid=1,sid=1,status='submitted',at='2026-10-30T01:00:10Z'):
    db.add(OutboundMessage(conversation_state_id=cid,source_type='ai',source_id=sid,
        idempotency_key=f'live-v3:{sid}:p{rid}',status=status,submitted_at=at,content='test'))

def test_percentiles_empty_and_invalid():
    assert distribution([])==dict(samples=0,mean=None,p50=None,p95=None)
    assert distribution([1,2,3,4,100,-1,float('nan')])==dict(samples=5,mean=22,p50=3,p95=100)

@pytest.mark.parametrize('call,label',[
    ({'error':'ReadTimeout'},'请求超时'),({'http_status':429},'限流／额度'),
    ({'http_status':503},'厂商服务错误'),({'http_status':401},'鉴权／权限'),
    ({'error':'ConnectError'},'连接失败'),({'error':'JSONDecodeError'},'返回格式异常'),
    ({'http_status':400},'请求被拒绝'),({},'其他／原因未记录')])
def test_failure_buckets(call,label): assert failure_reason(call)==label

def test_failed_attempt_recovered_and_dedup(session_factory):
    with session_factory() as db:
        setup(db)
        bad={'id':'a','status':'failed','error':'ReadTimeout','duration_ms':3000,'model':'primary','provider':'deepseek'}
        good={'id':'b','status':'completed','fallback_reason':'timeout','duration_ms':1000,'model':'backup','provider':'bailian'}
        run(db,calls=[bad,good]); sent(db)
        run(db,rid=2,calls=[bad],status='cancelled')
        db.add(HandoffTask(conversation_state_id=1,reason_code='contact',ai_assist_allowed=True,created_at='2026-10-30T01:00:00Z'))
        r=report(db)
        assert r['calls']['total']==2 and r['calls']['failed']==1 and r['calls']['failure_rate']==50
        assert r['calls']['fallback']==1 and r['calls']['recovered_replies']==1
        assert r['reply']['latency']==dict(samples=1,mean=20,p50=20,p95=20)
        assert r['handoffs']['total']==1 and r['handoffs']['assist_allowed']==1
        assert r['calls']['reasons']==[{'name':'请求超时','value':1}]
        assert not db.dirty and not db.new

def test_scope_playground_date_and_no_samples(session_factory):
    with session_factory() as db:
        setup(db)
        run(db,calls=[])
        run(db,sid=2,cid=2,rid=2,calls=[{'id':'hidden','status':'failed'}])
        run(db,sid=3,cid=3,rid=3,environment='playground',calls=[{'status':'failed'}])
        run(db,rid=4,at='2026-10-29T15:59:59Z',calls=[{'status':'failed'}])
        r=report(db,inbox_id=10)
        assert r['runs']==1 and r['calls']['total']==0 and r['calls']['failure_rate'] is None
        assert r['reply']['latency']['mean'] is None
        assert report(db,inbox_id=999)['runs']==0
        db.add(User(id=2,email='supervisor@example.com',display_name='S',role='supervisor',password_hash='x'))
        db.add(UserInboxScope(user_id=2,inbox_binding_id=1));db.flush()
        assert report(db,user=db.get(User,2))['runs']==1

def test_followup_missing_records_and_failed_receipt(session_factory):
    with session_factory() as db:
        setup(db)
        run(db);sent(db,status='failed')
        run(db,rid=2,module='silence_touch',calls=[]);sent(db,rid=2)
        r=report(db)
        assert r['missing_call_records']==1
        assert r['reply']['sent']==0 and r['reply']['latency']['samples']==0
        assert r['delivery']['failed']==1

def test_private_and_segmented_reply(session_factory):
    with session_factory() as db:
        setup(db);run(db,calls=[]);sent(db)
        s=db.get(AutomationSession,1)
        s.messages=[*s.messages,{'id':'second','run_id':1,'direction':'outgoing'}, {'id':'note','run_id':1,'direction':'outgoing','private':True}]
        for part,at in [('second','2026-10-30T01:00:30Z'),('note','2026-10-30T01:00:01Z')]:
            db.add(OutboundMessage(conversation_state_id=1,source_type='ai',source_id=1,
                idempotency_key=f'live-v3:1:{part}',status='submitted',submitted_at=at,content='test'))
        r=report(db)
        assert r['reply']['latency']['samples']==1 and r['reply']['latency']['mean']==20
        assert r['delivery']['submitted']==2

def test_endpoint_requires_manager(authenticated,session_factory):
    client,_=authenticated
    assert client.get('/v1/bi/technical?start=bad').status_code==422
    assert client.get('/v1/bi/technical').status_code==200
    with session_factory() as db:
        user=db.get(User,1);user.role='agent';db.commit()
    assert client.get('/v1/bi/technical').status_code==403


def test_meter_records_safe_root_cause_without_raw_secrets():
    from app.model_metering import begin_request, end_request
    try:
        try: raise ValueError('empty_model_response')
        except ValueError as cause: raise RuntimeError('https://secret.example?api_key=hidden') from cause
    except RuntimeError as error:
        handle=begin_request({'model':'deepseek-flash'})
        end_request(handle,error=error)
    assert handle[0]['error_code']=='empty_model_response'
    assert failure_reason(handle[0])=='返回内容为空'
    assert 'hidden' not in str(handle[0])
