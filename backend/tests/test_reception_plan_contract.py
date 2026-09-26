import pytest
from types import SimpleNamespace

from app.realtime_reply_pipeline import _covered_groups
from app.reply_generation import GeneratedReply
from app.reply_planning import ReplyPlan
from app.route_reply import (content_progress_from_values, update_content_progress_values,
                            prepare_route_reply_values, route_snapshot_from_values)
from app.silence_generation import _reject_recent_advisor_repeat


def _plan(group: str) -> ReplyPlan:
    return ReplyPlan(
        action='reply', intent='other', route_variant='peach_9d_2027', branch='peach_9d',
        next_stage='value_building', reply_goal='test', allowed_content_group_keys=[group],
        follow_up=None, allowed_fact_ids=[], allowed_asset_ids=[], reply_options=[], slots={},
        slot_evidence={}, missing_slots=[], handoff_reason=None, lead_action='none',
        contact_values={}, route_evidence='', confidence=1.0, safety_flags=[],
    )


def test_one_hotel_photo_marks_group_touched_but_not_complete():
    plan = _plan('hotel_reference')
    generated = GeneratedReply(
        body='這張是飯店客房照片。', follow_up=None,
        used_fact_ids=['route.shared.hotel_reference'],
        asset_ids=['routes12-hilton-room'],
    )
    assert _covered_groups(plan, generated) == ['hotel_reference']
    slots, sent, complete = update_content_progress_values(
        'peach_9d_2027', {}, [], 'hotel_reference',
        text_delivered=True, asset_keys=['routes12-hilton-room'],
    )
    assert complete is False
    assert sent == []
    assert content_progress_from_values('peach_9d_2027', slots, sent)['hotel_reference'] == {
        'text_delivered': False,
        'asset_keys': ['routes12-hilton-room'],
        'topic_covered': True,
        'history_unknown': True,
        'schema_version': 2,
    }


def test_all_hotel_photos_complete_content_group():
    plan = _plan('hotel_reference')
    generated = GeneratedReply(
        body='這兩張是飯店客房與房內設備。', follow_up=None,
        used_fact_ids=['route.shared.hotel_reference'],
        asset_ids=['routes12-hilton-room', 'routes12-hilton-oxygen'],
    )
    assert _covered_groups(plan, generated) == ['hotel_reference']
    _, progress = prepare_route_reply_values(SimpleNamespace(route_variant='peach_9d_2027'))
    slots = progress['slots']
    text = route_snapshot_from_values('peach_9d_2027', slots)['groups']['hotel_reference']['text']
    _, sent, complete = update_content_progress_values(
        'peach_9d_2027', slots, [], 'hotel_reference',
        delivered_text=text,
        asset_keys=['routes12-hilton-room', 'routes12-hilton-oxygen'],
    )
    assert complete is True
    assert sent == ['hotel_reference']


def test_silence_copy_rejects_reuse_of_recent_advisor_answer():
    generated = GeneratedReply(
        body='我們沒有安排隨隊醫師，但導遊有接受高原急救訓練，也備有血氧儀、氧氣瓶和急救包。接著介紹布達拉宮。',
        follow_up=None, used_fact_ids=[], asset_ids=[],
    )
    with pytest.raises(ValueError, match='reply_repeats_recent_advisor_message'):
        _reject_recent_advisor_repeat(generated, [{
            'direction': 'outgoing',
            'content': '我們沒有安排隨隊醫師，但導遊有接受高原急救訓練，也備有血氧儀、氧氣瓶和急救包。',
        }])


def test_silence_copy_accepts_distinct_new_value():
    generated = GeneratedReply(
        body='除了賞桃花，行程也會到布達拉宮和八廓街，這張照片可以先看看宮殿外觀。',
        follow_up=None, used_fact_ids=[], asset_ids=[],
    )
    assert _reject_recent_advisor_repeat(generated, [{
        'direction': 'outgoing',
        'content': '我們沒有安排隨隊醫師，但導遊有接受高原急救訓練。',
    }]) is generated
