"""Independent, Skill-driven reception engine. Shared by playground and Chatwoot delivery."""
import hashlib
from pathlib import Path


def release_id() -> str:
    root = Path(__file__).parent
    files = sorted([*root.rglob('*.py'), *root.rglob('*.md')])
    digest = hashlib.sha256(b''.join(p.relative_to(root).as_posix().encode() + p.read_bytes() for p in files)).hexdigest()[:16]
    return f'reception-v3-{digest}'
