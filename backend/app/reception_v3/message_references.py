"""Resolve explicit catalog references without inferring customer intentions."""
from app.reception_v3.route_understanding import DecisionContractError


def scripts(bundle, route):
    result = {s['id']: {'text': s['text'], 'assets': []}
              for s in bundle.get('common_scripts', []) if s.get('enabled', True)}
    result.update({s['id']: {'text': s['answer_text'], 'assets': s.get('asset_ids', [])}
                   for s in bundle['routes'].get(route, {}).get('scripts', []) if s.get('status') == 'active'})
    for key, group in bundle['routes'].get(route, {}).get('groups', {}).items():
        result.setdefault(key, {'text': group['text'], 'assets': group['assets']})
    return result


def message_route(bundle, message, default):
    """A message can quote another product without changing conversation ownership.

    Old decisions have no per-message route. Only an exact, unique catalog ID
    can supply that missing namespace; shared IDs require an explicit route.
    """
    explicit = message.get('route', '')
    route = explicit or default
    if route and route not in bundle['routes']:
        raise DecisionContractError('message_route_not_in_catalog')
    reference = message.get('script_id') or ''
    keys = ([reference] if reference and (not message.get('text') or reference.startswith('price')) else []) + message.get('quote_options', [])
    if not keys or all(k in scripts(bundle, route) for k in keys):
        return route
    if not explicit:
        matches = [r for r in bundle['routes'] if all(k in scripts(bundle, r) for k in keys)]
        if len(matches) == 1:
            return matches[0]
    raise DecisionContractError('message_script_reference_invalid:' + ','.join(keys))


def validate_references(decision, bundle, default):
    for message in decision['messages']:
        message_route(bundle, message, default)
