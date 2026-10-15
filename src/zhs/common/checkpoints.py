"""Task-local observers for durable checkpoints before irreversible requests."""

from contextlib import contextmanager
from contextvars import ContextVar

_recorder = ContextVar("submission_recorder", default=None)


@contextmanager
def track_submissions(recorder):
    token = _recorder.set(recorder)
    try:
        yield
    finally:
        _recorder.reset(token)


def submission_checkpoint(submission_id, state, **details):
    recorder = _recorder.get()
    if recorder is not None:
        recorder(submission_id, state, details)
