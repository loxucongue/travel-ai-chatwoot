"""Calendar arithmetic for a window interpreted by the conversation model.

No customer-text classification or tour/transfer policy lives here. The model
supplies the itinerary duration and buffers from the applicable business facts.
"""
from datetime import date, timedelta


def calculate_travel_window(itinerary_days, days_before, days_after, *,
                            start=None, end=None, available_days=None):
    values = {'itinerary_days': itinerary_days, 'days_before': days_before,
              'days_after': days_after, 'available_days': available_days}
    for name, value in values.items():
        if name == 'available_days' and value is None:
            continue
        minimum = 0 if name in ('days_before', 'days_after') else 1
        if type(value) is not int or not minimum <= value <= 730:
            raise ValueError(f'{name}_must_be_integer_between_{minimum}_and_730')
    if (start is None) != (end is None):
        raise ValueError('start_and_end_required_together')
    first = last = None
    if start is not None:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        span = (last - first).days + 1
        if not 1 <= span <= 730:
            raise ValueError('travel_window_must_be_1_to_730_days_inclusive')
        if available_days is not None and available_days != span:
            raise ValueError('available_days_conflicts_with_dates')
        available_days = span
    required = itinerary_days + days_before + days_after
    result = {**values, 'available_days': available_days, 'required_total_days': required,
              'counting': 'inclusive_calendar_days',
              'scope': 'duration_only_not_departure_or_flight_availability'}
    if available_days is not None:
        fits = available_days >= required
        result.update(fits_duration=fits, shortfall_days=max(0, required - available_days),
                      available_itinerary_days=max(0, available_days - days_before - days_after))
        if first is not None:
            # Empty windows must never be passed on as possible departures.
            result['possible_departure_window'] = ({
                'start': (first + timedelta(days=days_before)).isoformat(),
                'end': (last - timedelta(days=days_after + itinerary_days - 1)).isoformat(),
            } if fits else None)
    return result
