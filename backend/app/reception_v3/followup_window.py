"""Business follow-up horizon; channel availability is checked independently."""
from datetime import timedelta

from app.runtime_settings import dt


FOLLOWUP_WINDOW = timedelta(days=7)


def deadline(customer_at):
    return dt(customer_at) + FOLLOWUP_WINDOW


def within_window(customer_at, now):
    return bool(customer_at and timedelta(seconds=-30) <= dt(now) - dt(customer_at) < FOLLOWUP_WINDOW)
