"""An encoding-damaged campaign must fail before installation or paid tests."""
import json
import pytest

from app.business_text import read_business_json, validate_business_text


@pytest.mark.parametrize('damaged', ['????????????????', '取得\ufffd聯絡方式'])
def test_damaged_nested_business_content_fails_with_field_location(tmp_path, damaged):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'reply': {'goal': damaged}}), encoding='utf-8')
    with pytest.raises(ValueError, match=r'business_text_encoding_damage:config.json.reply.goal'):
        read_business_json(path)


def test_real_unicode_and_normal_question_marks_round_trip(tmp_path):
    value = {'answers': ['方便留 LINE 嗎？', 'Is a QR code okay?', 'Really??'], 'count': 3}
    path = tmp_path / 'faq.json'
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    assert read_business_json(path) == value


def test_effective_bundle_detects_damage_even_when_json_is_valid():
    with pytest.raises(ValueError, match=r'facts\[0\].text'):
        validate_business_text({'facts': [{'text': '???????????'}]}, 'effective_skills')
