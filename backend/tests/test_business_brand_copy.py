from app.route_packages import ROUTE_PACKAGES


def test_business_document_brand_is_shared_by_both_routes():
    for package in ROUTE_PACKAGES.values():
        group = package['content_groups']['brand_positioning']
        assert '4至10人' in group['approved_text']
        assert group['evidence_refs'] == ['route.shared.brand_positioning']
        facts = {f['id']: f['text'] for f in package['knowledge_facts']}
        assert '4至10人' in facts['route.shared.brand_positioning']
        assert '多人同行' in facts['route.shared.group_offer']
        for key in ('party_intro_small', 'party_intro_group'):
            assert '4至6' not in package['content_groups'][key]['approved_text']
        for answer in package['fixed_answers']:
            if answer['id'] == 'small_party':
                assert '4至6' not in answer['answer_text'] and '4-6' not in answer['answer_text']
                assert answer['answer_origin'] == 'operator_approved'


def test_brand_does_not_expand_vehicle_capacity_or_price_scope():
    for package in ROUTE_PACKAGES.values():
        facts = {f['id']: f['text'] for f in package['knowledge_facts']}
        assert '9座' in facts['route.shared.vehicle_reference']
        assert '10位' not in facts['route.shared.vehicle_reference']
        assert '4至6人' in facts['route.shared.vehicle_reference']


def test_company_brand_is_not_mistaken_for_large_group_handoff_threshold():
    from app.reply_planning import build_reply_plan
    from app.reply_understanding import CustomerUnderstanding, SlotUpdate

    context = {
        'customer_text': '我們8位想看桃花9日',
        'context_messages': [{'direction': 'incoming', 'content': '我們8位想看桃花9日'}],
        'journey': {'sent_content_groups': [], 'customer_profile': {}},
        'route_variant': '',
        'lead_capture': {'status': 'not_started'},
        'available_materials': [],
        'reception_policy': {
            'route_switch': {'allowed_routes': ['peach_9d_2027', 'peach_11d_2027']},
            'handoff': {'large_group': {'enabled': True, 'minimum_party_size': 8, 'reason': 'large_group_custom_quote'}},
            'operator_configuration': {'lead_capture': {'enabled': True}, 'business_rules': []},
            'reply_style': {'max_characters': 200, 'max_messages_per_turn': 3, 'max_images_per_turn': 2},
            'silence_journey': {'wakeup_after_minutes': []},
        },
    }
    understanding = CustomerUnderstanding(
        intent='route_intro', route_candidate='peach_9d_2027', route_resolution='confirmed',
        slot_updates={'party_size': SlotUpdate(8, '8位')}, customer_questions=['itinerary'], confidence=1,
    )
    plan = build_reply_plan(context, understanding)
    assert plan.action == 'handoff'
    assert plan.handoff_reason == 'large_group_custom_quote'
