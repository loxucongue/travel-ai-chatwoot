"""Shared deferred-job semantics for live and rehearsal workers."""
from datetime import datetime, timedelta


def defer_job(job, now: str, minutes: int) -> bool:
    payload = dict(job.payload or {})
    count = int(payload.get('v2_defer_count', 0))
    if count >= 6:
        return False
    job.payload = {**payload, 'v2_defer_count': count + 1}
    job.status, job.reason = 'scheduled', 'v2_deferred'
    job.scheduled_at = (datetime.fromisoformat(now.replace('Z', '+00:00')) +
                        timedelta(minutes=max(1, min(720, minutes or 60)))).isoformat()
    job.confirmed_at = None
    if hasattr(job, 'completed_at'):
        job.completed_at = None
    return True
