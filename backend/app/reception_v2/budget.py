"""Context-local total inference budget, also consumed by shared verification."""
from contextvars import ContextVar
from functools import wraps
import time

deadline = ContextVar('v2_inference_deadline', default=None)
task_proofs = ContextVar('v2_turn_task_proofs', default=None)
turn_trace = ContextVar('v2_turn_trace', default=None)


def remaining(default: float) -> float:
    until = deadline.get()
    value = min(default, until - time.monotonic()) if until is not None else default
    if value <= 0:
        raise TimeoutError('v2_turn_deadline_exceeded')
    return value


def bounded_turn(function):
    @wraps(function)
    def run(*args, **kwargs):
        token = deadline.set(time.monotonic() + 30)
        proof_token = task_proofs.set({})
        trace_token = turn_trace.set([])
        try:
            result = function(*args, **kwargs)
            remaining(30)  # Reject a late result even if a transport ignored its timeout.
            return result
        except TimeoutError as exc:
            from app.deepseek_evaluation import EvaluationCallError
            raise EvaluationCallError('v2_turn_deadline_exceeded', list(turn_trace.get() or []), '') from exc
        finally:
            turn_trace.reset(trace_token)
            task_proofs.reset(proof_token)
            deadline.reset(token)
    return run
