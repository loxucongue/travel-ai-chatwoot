import pytest

from app.advisor_voice import advisor_voice_contract, tone_preset_guidance
from app.reception_policy_views import reception_policy_views
from app.reply_generation import _system_prompt as reply_prompt
from app.reply_generation import ASSET_DESCRIPTION_PATTERN
from app.silence_generation import _system_prompt as silence_prompt
from app.reply_fact_verification import _system_prompt as verifier_prompt


@pytest.mark.parametrize('tone', ['friendly_professional', 'concise', 'warm'])
def test_presets_resolve_to_descriptive_expression_only(tone):
    views = reception_policy_views({'reply_style': {'operator_tone': tone},
                                    'operator_configuration': {'tone_guidance': '輕柔但不裝熟'}})
    assert views['prompt_policy']['tone_description'] == tone_preset_guidance(tone)
    assert views['prompt_policy']['tone_guidance'] == '輕柔但不裝熟'
    assert 'tone_description' not in views['decision_policy']
    assert 'tone_description' not in views['runtime_policy']


def test_generation_nodes_share_tone_contract_and_serious_context_exception():
    for prompt in (reply_prompt(200, 2), silence_prompt(200, 2)):
        assert 'tone_description' in prompt
        assert '健康、用藥、客訴、退款與合約問題要溫柔但認真' in prompt
        assert '不得迴避或暗示自己是真人' in prompt
    assert '輕巧可愛' in advisor_voice_contract()


def test_verifier_combines_reviewed_asset_and_text_evidence_without_medical_inference():
    prompt = verifier_prompt()
    assert '素材敘事也是已審核依據' in prompt
    assert '不支持保證每晚入住同一飯店' in prompt
    assert '醫療效果' in prompt


@pytest.mark.parametrize('body', ['這幾張是客房的實際環境。', '布達拉宮的相片傳您看～', '這幅是布達拉宮的外觀。', '客房環境傳給您看。'])
def test_taiwan_photo_introductions_are_recognized(body):
    assert ASSET_DESCRIPTION_PATTERN.search(body)


def test_pure_attraction_copy_is_not_a_photo_description():
    assert not ASSET_DESCRIPTION_PATTERN.search('行程包含布達拉宮。')
