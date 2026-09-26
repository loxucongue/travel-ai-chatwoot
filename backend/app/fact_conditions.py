"""Literal, reviewed fact conditions; never customer-intent routing."""
import re
import unicodedata


def validate_answer_conditions(fact):
    conditions = fact.get('answer_conditions', [])
    if not isinstance(conditions, list) or len(conditions) > 8:
        raise ValueError('fact_answer_conditions_invalid')
    for item in conditions:
        if not isinstance(item, dict) or not isinstance(item.get('label'), str) or not 1 <= len(item['label']) <= 160:
            raise ValueError('fact_answer_conditions_invalid')
        for key in ('when_any_of', 'require_any_of'):
            values = item.get(key)
            if (not isinstance(values, list) or not 1 <= len(values) <= 12
                    or any(not isinstance(v, str) or not v.strip() or len(v) > 160 for v in values)):
                raise ValueError('fact_answer_conditions_invalid')
    return conditions


def missing_answer_conditions(body, fact):
    def normalize(value):
        return re.sub(r'[\s,，]', '', unicodedata.normalize('NFKC', value)).casefold()
    text = normalize(body)
    return [c['label'] for c in validate_answer_conditions(fact)
            if any(normalize(v) in text for v in c['when_any_of'])
            and not any(normalize(v) in text for v in c['require_any_of'])]
