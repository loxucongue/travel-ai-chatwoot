from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path


SKILL_ROOT = Path(__file__).with_name("skills")


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    digest: str
    route_variant: str = ''
    followup_groups: tuple[str, ...] = ()
    route_aliases: tuple[str, ...] = ()


def _load(path: Path) -> Skill:
    text = path.read_text(encoding="utf-8")
    match = re.fullmatch(r"---\s*\n(.*?)\n---\s*\n(.*)", text, re.DOTALL)
    if not match:
        raise ValueError(f"v2_skill_frontmatter_invalid:{path}")
    headers = {}
    for line in match.group(1).splitlines():
        key, separator, value = line.partition(":")
        if separator:
            headers[key.strip()] = value.strip()
    name, description = headers.get("name", ""), headers.get("description", "")
    if not name or not description:
        raise ValueError(f"v2_skill_metadata_missing:{path}")
    return Skill(name, description, match.group(2).strip(), hashlib.sha256(text.encode()).hexdigest(),
                 headers.get('route_variant', ''),
                 tuple(v.strip() for v in headers.get('followup_groups', '').split(',') if v.strip()),
                 tuple(v.strip() for v in headers.get('route_aliases', '').split('|') if v.strip()))


class SkillRegistry:
    def __init__(self, root: Path = SKILL_ROOT):
        skills = [_load(path) for path in sorted(root.rglob("SKILL.md"))]
        if len({item.name for item in skills}) != len(skills):
            raise ValueError("v2_skill_name_duplicate")
        self._skills = {item.name: item for item in skills}

    def index(self) -> list[dict]:
        return [{"name": item.name, "description": item.description} for item in self._skills.values()]

    def load(self, name: str) -> dict:
        skill = self._skills.get(name)
        if skill is None:
            raise ValueError("v2_skill_not_found")
        result = {"name": skill.name, "instructions": skill.body, "digest": skill.digest}
        if skill.route_variant:
            # Use the same published route snapshot as facts and delivery.
            # Business edits belong in the route configuration, not SKILL.md.
            from app.route_packages import ROUTES
            route = ROUTES.get(skill.route_variant, {})
            result['route_variant'] = skill.route_variant
            result['scripts'] = deepcopy([
                item for item in route.get('fixed_answers', [])
                if item.get('status') == 'active'
            ])
            embedded_ids = set()
            for item in route.get('fixed_answers', []):
                if '{{script:' + item['id'] + '}}' in result['instructions']:
                    embedded_ids.add(item['id'])
                result['instructions'] = result['instructions'].replace(
                    '{{script:' + item['id'] + '}}',
                    item['answer_text'] if item.get('status') == 'active' else '',
                )
            result['package_version'] = route.get('package_version', '')
            result['script_source'] = deepcopy(route.get('source', {}))
            result['evidence_refs'] = list(dict.fromkeys(
                ref for item in result['scripts'] for ref in item.get('fact_ids', [])
            ))
            result['introduction'] = [
                {'group_key': key, 'text': group.get('text', ''),
                 'asset_keys': list(group.get('assets', [])),
                 'evidence_refs': list(group.get('evidence', []))}
                for key in route.get('introduction_sequence', route.get('sequence', []))
                if (group := route.get('groups', {}).get(key))
            ]
            # Make the loaded Skill itself readable, with the actual configured
            # copy. The package remains the only editable source of these texts.
            result['instructions'] += '\n\n## 本版场景话术\n' + '\n\n'.join(
                f"### {item.get('name', item['id'])} [{item['id']}]\n"
                + '场景：' + '；'.join(item.get('positive_examples', [])) + '\n'
                + item['answer_text'] + '\n'
                + ('使用说明：' + item['usage_note'] + '\n' if item.get('usage_note') else '')
                + '素材：' + ', '.join(item.get('asset_ids', []))
                for item in result['scripts']
                if item['id'] not in embedded_ids
            )
        return result

    def release_digest(self) -> str:
        value = "\n".join(f"{item.name}:{item.digest}" for item in self._skills.values())
        return hashlib.sha256(value.encode()).hexdigest()

    def route_skill(self, route_variant: str) -> str | None:
        matches = [skill.name for skill in self._skills.values() if skill.route_variant == route_variant and route_variant]
        if len(matches) > 1:
            raise ValueError('v2_route_skill_duplicate')
        return matches[0] if matches else None

    def route_metadata(self, route_variant: str) -> Skill | None:
        name = self.route_skill(route_variant)
        return self._skills.get(name) if name else None
