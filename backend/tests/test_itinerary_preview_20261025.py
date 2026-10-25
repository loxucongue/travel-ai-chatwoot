"""#417/#420: unselected private-group inquiries must have real map references."""
from copy import deepcopy
import json

from app.reception_v3 import runtime, service
from app.reception_v3.material_rules import itinerary_previews
from test_reception_v3_skills import context, model, final
from test_reception_v3 import answer, step
from test_v3_intake_followup import config, create


def preview_context():
    c = context()
    c['messages'] = [{'id':'entry','direction':'incoming','content':'我人數有4位以上，想自己包團'}]
    for m in c['available_materials']:
        m.update(content_type='image', content_group_key='itinerary_overview',
                 media_id=int(m['key'].split('-')[1]), media_hash='hash:'+m['key'])
    return c


def test_no_selected_route_still_exposes_real_itinerary_bindings():
    c = preview_context()
    maps = itinerary_previews(c['skills'],c['available_materials'],[])
    assert maps['peach_11d_2027']['assets'][0]['key'] == 'map-11'
    assert not maps['peach_11d_2027']['assets'][0]['already_delivered']
    c['skills']['routes'].pop('peach_9d_2027')
    c['available_materials'].extend([
        {'key':'unbound','content_type':'image','routes':['peach_11d_2027'],'content_group_key':'itinerary_overview'},
        {'key':'map-11','content_type':'image','routes':['peach_11d_2027'],'content_group_key':'hotel'},
    ])
    maps = itinerary_previews(c['skills'],c['available_materials'],[])
    assert list(maps) == ['peach_11d_2027']
    assert len(maps['peach_11d_2027']['assets']) == 1


def test_preview_respects_rejected_products_and_hash_history():
    c = preview_context()
    history = [{'direction':'outgoing','status':'delivered','media_hash':'hash:map-9'}]
    maps = itinerary_previews(c['skills'],c['available_materials'],history,{'everest_preference':'avoid'})
    assert list(maps) == ['peach_9d_2027']
    assert maps['peach_9d_2027']['assets'][0]['already_delivered']
    history[0]['status'] = 'cancelled'
    assert not itinerary_previews(c['skills'],c['available_materials'],history)['peach_9d_2027']['assets'][0]['already_delivered']
    assert itinerary_previews(c['skills'],[],[]) == {}


def test_private_group_entry_model_sees_map_without_product_skill(monkeypatch):
    c = preview_context()
    requests = model(monkeypatch,[final(messages=[{'text':'先給您看11日參考行程。','asset_keys':['map-11']}],
        profile={'party_size_range':'4位以上','party_size_min':4,'group_type':'private'})])
    decision,logs,_ = runtime.run_agent(c)
    task = json.loads(requests[0]['messages'][-1]['content'])
    assert task['itinerary_previews']['peach_11d_2027']['assets'][0]['key'] == 'map-11'
    assert decision['messages'][0]['asset_keys'] == ['map-11']
    assert decision['route_variant'] == '' and not decision['start_introduction']
    assert 'party_size' not in decision['profile']
    assert len(requests) == 1


def test_420_following_request_keeps_unselected_status_and_one_map(monkeypatch):
    c = preview_context()
    c['profile'] = {'party_size_range':'4位以上','party_size_min':4,'group_type':'private'}
    c['messages'] += [
        {'direction':'outgoing','content':'您會想上珠峰看看嗎？','status':'sent'},
        {'direction':'incoming','content':'都可以評估考量,請傳詳細行程表與價錢給我謝謝您'}]
    requests = model(monkeypatch,[final(messages=[{'text':'先給您看11日參考行程，9日少珠峰段兩天。',
                                                  'asset_keys':['map-11']}])])
    decision,_,_ = runtime.run_agent(c)
    assert decision['route_variant'] == '' and decision['profile'] == {}
    assert decision['messages'][0]['asset_keys'] == ['map-11']
    assert len(requests) == 1


def test_preview_details_request_cannot_silently_select_route(monkeypatch):
    c = preview_context()
    c['messages'] += [
        {'direction':'outgoing','status':'delivered','asset_key':'map-11'},
        {'direction':'incoming','content':'都可以評估考量,請傳詳細行程表與價錢給我謝謝您'}]
    recommendation = final(route_choice='recommend',profile={'discussion_route':'peach_11d_2027'},
                           messages=[{'text':'可先參考上方行程圖，您預計幾月出發？'}])
    requests = model(monkeypatch,[final(route_variant='peach_11d_2027'), recommendation, recommendation])
    decision, logs, _ = runtime.run_agent(c)
    assert decision['route_variant'] == '' and not decision['start_introduction']
    assert decision['route_choice'] == 'recommend'
    assert len(requests) == 3  # Rejection, route Skill activation, final reply.
    assert any(l.get('status') == 'route_choice_rejected' for l in logs)


def test_preview_explicit_choice_allowed_but_not_an_undecided_request():
    from app.reception_v3.route_choice import choice_error
    c = preview_context()
    c['messages'] += [{'direction':'outgoing','status':'delivered','asset_key':'map-11'},
                      {'direction':'incoming','content':'先給我看11日，還沒選定'}]
    decision = runtime.Decision(route_variant='peach_11d_2027',route_choice='selected').model_dump()
    assert choice_error(decision,c)
    c['messages'][-1]['content'] = '就選11日，請介紹'
    assert choice_error(decision,c) is None
    c['messages'][-1]['content'] = '桃花11日'
    assert choice_error(decision,c) is None


def test_preview_then_selected_intro_does_not_repeat_image(session_factory,monkeypatch):
    spec = config()
    materials = []
    for i,route in enumerate(('nine','eleven'),start=1):
        spec['routes'][route]['groups']['map']['assets'] = ['map-'+route]
        materials.append({'key':'map-'+route,'routes':[route],'content_type':'image',
                          'content_group_key':'itinerary_overview','media_hash':'hash:'+route,'media_id':i})
    answers = [answer(messages=[{'text':'先給您看11日參考行程。','asset_keys':['map-eleven']}],
                      profile={'party_size_range':'4位以上','party_size_min':4,'group_type':'private'}),
               answer(route_variant='eleven',route_choice='selected',start_introduction=True)]
    with session_factory() as db:
        row = create(db,monkeypatch,entry='我人數有4位以上，想自己包團',
                     model=lambda c:answers.pop(0) if answers else answer(action='wait'))
        monkeypatch.setattr(service,'compile_skills',lambda db:deepcopy(spec))
        monkeypatch.setattr(service,'candidate_materials',lambda *a:deepcopy(materials))
        monkeypatch.setattr(service,'tenant_for_session',lambda *a:1)
        def resolve(db,keys,route,tenant):
            return [{**m,'asset_key':m['key'],'content':'','route_variant':route}
                    for m in materials if m['key'] in keys and route in m['routes']]
        monkeypatch.setattr(service,'resolve_materials',resolve)
        for _ in range(7):step(db,row,2)
        sent = lambda:[m for m in row.messages if m.get('status')=='simulated_delivered']
        assert [m['asset_key'] for m in sent() if m.get('content_type')=='image'] == ['map-eleven']
        assert not row.controls.get('route_variant') and not service.state(row).get('completed_introductions')
        assert not any(m.get('content_type')=='input_select' for m in sent())
        assert 'party_size' not in row.memory
        service.add_message(db,row,'就選十一日，請介紹','choose');db.commit()
        for _ in range(8):step(db,row,2)
        assert row.controls['route_variant'] == 'eleven'
        assert service.state(row)['completed_introductions'] == ['eleven']
        assert [m['asset_key'] for m in sent() if m.get('content_type')=='image'] == ['map-eleven']
        assert any(m.get('content')=='住宿原文' for m in sent())


def test_417_three_buffered_messages_preserve_private_range_and_preview_only(session_factory,monkeypatch):
    spec = config()
    spec['routes']['eleven']['groups']['map']['assets'] = ['map-eleven']
    material = {'key':'map-eleven','routes':['eleven'],'content_type':'image',
                'content_group_key':'itinerary_overview','media_id':2,'media_hash':'hash:eleven'}
    seen = []
    def reply(c):
        seen.append(c)
        return answer(route_variant='nine',route_choice='recommend',interrupt=True,
                      profile={'party_size_range':'4位以上','party_size_min':4,'group_type':'private',
                               'discussion_route':'eleven'},
                      messages=[{'text':'先給您看最後點的11日參考行程。','asset_keys':['map-eleven']}])
    with session_factory() as db:
        row = create(db,monkeypatch,entry='九日',model=reply)
        monkeypatch.setattr(service,'compile_skills',lambda db:deepcopy(spec))
        monkeypatch.setattr(service,'candidate_materials',lambda *a:[deepcopy(material)])
        monkeypatch.setattr(service,'tenant_for_session',lambda *a:1)
        monkeypatch.setattr(service,'resolve_materials',lambda db,keys,route,tenant:[
            {**material,'asset_key':material['key'],'route_variant':route,'content':''}])
        service.add_message(db,row,'十一日','second-card')
        service.add_message(db,row,'我人數有4位以上，想自己包團','private')
        db.commit()
        for _ in range(7):step(db,row,2)
        assert [m['content'] for m in seen[0]['messages'] if m.get('direction')=='incoming'] == [
            '九日','十一日','我人數有4位以上，想自己包團']
        sent = [m for m in row.messages if m.get('status')=='simulated_delivered']
        assert [m['asset_key'] for m in sent if m.get('content_type')=='image'] == ['map-eleven']
        assert not service.state(row).get('completed_introductions')
        assert not any(m.get('v3_kind')=='introduction' for m in sent)
        assert row.memory['group_type']=='private' and row.memory['party_size_min']==4
        assert 'party_size' not in row.memory and row.memory['discussion_route']=='eleven'
