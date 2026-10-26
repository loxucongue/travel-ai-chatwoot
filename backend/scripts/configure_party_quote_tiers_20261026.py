"""Apply the confirmed reached-tier rule without changing base prices or group fees."""
from copy import deepcopy

CONFIRMATION = '2026-10-26用戶確認奇數人數使用已達到的優惠檔位，按每人扣減並以此類推'


def update_package(original):
    package = deepcopy(original)
    policy = package.get('party_pricing')
    if not policy:
        return package
    policy.pop('discounts_per_person', None)
    policy['discount_progression'] = {'starts_at': 4, 'party_size_step': 2, 'amount_step_cny': 200}
    # Discount eligibility does not authorize large-group sales or new vehicles.
    policy['auto_quote_party_size_max'] = 6
    if CONFIRMATION not in policy['source_ref']:
        policy['source_ref'] += '；' + CONFIRMATION
    for fact in package.get('knowledge_facts', []):
        if fact['id'].endswith('.pricing_policy'):
            fact['text'] = fact['text'].replace(
                '4人報名每人減200元，6人報名每人減400元。',
                '1–3人無多人優惠，4–5人每人減200元，6–7人每人減400元，8–9人每人減600元，10–11人每人減800元；之後每增加2人，每人優惠再增加200元。')
            fact['text'] = fact['text'].replace(
                '5人優惠檔位未確認，不猜相鄰檔；7人以上仍需顧問安排。',
                '5人拼團適用4人優惠檔，不因奇數人數交接。獨團加價與多人優惠是不同規則，不能把4人獨團加價推到5人；7人以上仍需顧問安排。')
            if CONFIRMATION not in fact.get('source_ref', ''):
                fact['source_ref'] = fact.get('source_ref', '') + '；' + CONFIRMATION
    package['package_version'] = '2026-10-26.party-quote-tiers-v1'
    return package
