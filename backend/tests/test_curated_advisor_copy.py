from app.route_packages import ROUTE_PACKAGES


def test_customer_copy_has_no_planner_instructions_or_unsupported_group_offer():
    for package in ROUTE_PACKAGES.values():
        groups = package['content_groups']
        assert '更有彈性' not in groups['party_intro_group']['approved_text']
        assert '團體費用' not in groups['party_intro_group']['approved_text']
        assert '邀請客戶' not in groups['contact_request']['approved_text']
        assert '聯絡方式；人數' not in groups['contact_request']['approved_text']
        price = groups['price_deferral']
        amount = '9,980' if package['route_variant'] == 'peach_9d_2027' else '11,480'
        assert amount in price['approved_text']
        assert any(ref.endswith('.price') for ref in price['evidence_refs'])
        assert 'route.shared.vehicle_reference' in groups['price_reference']['evidence_refs']
        answers = {answer['id']: answer for answer in package['fixed_answers']}
        assert answers['group_party']['status'] == 'active'
        assert answers['group_party']['answer_origin'] == 'website_verbatim'
        assert '4-6人' in answers['group_party']['answer_text']
        for key in {'route_overview', 'rongbuk_hotel'} & answers.keys():
            assert answers[key]['status'] == 'active'
            assert answers[key]['answer_origin'] == 'website_verbatim'
        assert answers['spring_weather']['status'] == 'active'


def test_mainline_copy_and_legacy_sop_do_not_drift():
    keys = {'itinerary_overview', 'peach_highlights', 'hotel_reference', 'vehicle_reference', 'rongbuk_reference'}
    for package in ROUTE_PACKAGES.values():
        for node in package['sop']['nodes']:
            key = node.get('content_group_key') or node['key']
            if key not in keys:
                continue
            texts = [item['content'] for item in node['messages'] if item.get('content_type', 'text') == 'text']
            assert texts == [package['content_groups'][key]['approved_text']]
        for key in keys & package['content_groups'].keys():
            text = package['content_groups'][key]['approved_text']
            assert len(text) <= 200
            assert not any(claim in text for claim in ('保證不高反', '第一天不安排行程'))
