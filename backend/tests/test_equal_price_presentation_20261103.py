import pytest

from app.reception_v3 import service
from app.reception_v3.party_quotes import comparison_text, quote_error
from app.reception_v3.runtime import Decision
from test_party_quote_tiers_20261026 import current
from test_reception_v3 import create


@pytest.mark.parametrize('days,counts,expected', [
    (9, (2, 3), ['2位／3位報名：拼團升級「4-6人小團」優惠價：人民幣¥ 10,780 /人']),
    (11, (4, 5), ['4位／5位報名：拼團升級「4-6人小團」優惠價：人民幣¥ 12,580 /人']),
    (9, (2, 3, 4), ['2位／3位報名：拼團升級「4-6人小團」優惠價：人民幣¥ 10,780 /人',
                   '4位報名：拼團升級「4-6人小團」優惠價：人民幣¥ 10,580 /人']),
    (11, (5, 2, 4), ['5位／4位報名：拼團升級「4-6人小團」優惠價：人民幣¥ 12,580 /人',
                    '2位報名：拼團升級「4-6人小團」優惠價：人民幣¥ 12,780 /人']),
])
def test_equal_prices_group_only_requested_counts_and_preserve_distinct_prices(days, counts, expected):
    text = comparison_text(current(days)['routes']['nine'], [f'price_shared_{n}' for n in counts])
    assert [line for line in text.splitlines() if '報名：' in line] == expected
    assert text.count('包含：') == text.count('不含：') == 1
    assert '原價' not in text


def test_grouping_preserves_custom_price_line_and_terms():
    spec = current()['routes']['nine']
    spec['party_pricing']['shared_template'] = '測試線\n兩人一房，¥{amount}/人（含早餐）\n不含機票'
    assert comparison_text(spec, ['price_shared_2', 'price_shared_3']) == (
        '測試線\n2位／3位報名：兩人一房，¥12,780/人（含早餐）\n不含機票')


def test_same_price_explanation_kept_once_with_quote_and_without_repeating_quote(session_factory, monkeypatch):
    explanation = '2位和3位的每人費用在這個檔位是相同的～都是上述給您的優惠價～'
    with session_factory() as db:
        row = create(db, monkeypatch)
        value = current(9)
        for messages in ([{'quote_options': ['price_shared_2', 'price_shared_3']}, {'text': explanation}],
                         [{'text': explanation}]):
            decision = Decision(route_variant='nine', messages=messages).model_dump()
            assert quote_error(decision, value, {'party_size_range': [2, 3]}) is None
            _, parts = service.parts_for(db, row, value, decision)
            assert parts[-1]['content'] == explanation
            assert len(parts) == len(messages)
            text = '\n'.join(p['content'] for p in parts)
            assert text.count('10,780') == len(messages) - 1
