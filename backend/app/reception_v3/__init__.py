"""Independent, Skill-driven reception engine. Shared by playground and Chatwoot delivery."""
import hashlib
from pathlib import Path


def release_id() -> str:
    from app.business_profile import ROOT as business_root
    root = Path(__file__).parent
    files = sorted([*root.rglob('*.py'), *root.rglob('*.md')])
    content = b''.join(p.relative_to(root).as_posix().encode() + p.read_bytes() for p in files)
    business_files = [*business_root.glob('*.json'), *business_root.glob('*.txt'),
                      *(business_root / 'skills').rglob('*.md')]
    content += b''.join(p.relative_to(business_root).as_posix().encode() + p.read_bytes()
                        for p in sorted(business_files))
    digest = hashlib.sha256(content).hexdigest()[:16]
    return f'reception-v3-{digest}'
