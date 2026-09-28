"""Independent, Skill-driven reception engine. Playground transport only."""
import hashlib
from pathlib import Path


def release_id() -> str:
    root = Path(__file__).parent
    files = sorted([*root.glob('*.py'), *root.glob('*.md')])
    digest = hashlib.sha256(b''.join(p.name.encode() + p.read_bytes() for p in files)).hexdigest()[:16]
    return f'reception-v3-{digest}'
