"""Preserve exception identity while identifying the model boundary that failed."""

from contextlib import contextmanager


MODEL_CALL_PHASES = frozenset({
    "request_assembly", "provider_call", "response_processing", "review_adapter",
})


@contextmanager
def model_call_phase(phase):
    if phase not in MODEL_CALL_PHASES:
        raise ValueError("unsupported model call phase")
    try:
        yield
    except Exception as exc:
        if getattr(exc, "_d8_model_call_phase", None) not in MODEL_CALL_PHASES:
            exc._d8_model_call_phase = phase
        raise
