"""Enable the approved next-day priority without altering outreach schedules."""
from copy import deepcopy


def update_config(config):
    result = deepcopy(config)
    result.setdefault('lead_capture', {}).update(
        next_day_invitation_enabled=True, consultation_turn_threshold=3)
    return result
