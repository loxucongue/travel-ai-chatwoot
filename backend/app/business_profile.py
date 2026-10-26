"""Explicit company content; platform code never falls back to another company."""
import json
from pathlib import Path

from app.config import settings


ROOT = Path(__file__).resolve().parents[2] / 'businesses' / settings.business_profile
PROFILE = json.loads((ROOT / 'profile.json').read_text(encoding='utf-8'))
COMMON_SKILL = PROFILE['common_skill']


def read_text(name: str) -> str:
    return (ROOT / name).read_text(encoding='utf-8')


def reception_defaults() -> dict:
    return json.loads(read_text('reception-defaults.json'))
