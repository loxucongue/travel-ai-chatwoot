"""Business wording only; keep factual hotel and departure conditions intact."""
from copy import deepcopy


def update_package(original):
    def visit(value):
        if isinstance(value, str):
            return value.replace('全酒店','所有酒店').replace('人數也是有限定的喔', '訂完就沒有了喔').replace('人數也是有限的喔', '訂完就沒有了喔')
        if isinstance(value, list):
            return [visit(item) for item in value]
        if isinstance(value, dict):
            return {k:visit(v) for k,v in value.items()}
        return value
    updated = visit(deepcopy(original))
    if updated != original and not updated['package_version'].endswith('.wording-20261020'):
        updated['package_version'] += '.wording-20261020'
    return updated
