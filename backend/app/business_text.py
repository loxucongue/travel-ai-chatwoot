"""Reject obvious encoding damage in authored business data, never customer input."""
import json
from pathlib import Path
import re


def validate_business_text(value, location='business_data'):
    if isinstance(value, str):
        if '\ufffd' in value or re.search(r'\?{8,}', value):
            raise ValueError(f'business_text_encoding_damage:{location}')
    elif isinstance(value, dict):
        for key, item in value.items():
            validate_business_text(item, f'{location}.{key}')
    elif isinstance(value, list):
        for index, item in enumerate(value):
            validate_business_text(item, f'{location}[{index}]')


def read_business_json(path):
    path = Path(path)
    data = json.loads(path.read_text(encoding='utf-8'))
    validate_business_text(data, path.name)
    return data
