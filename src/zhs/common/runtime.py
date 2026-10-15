"""Shared execution errors and task results."""

from dataclasses import dataclass, field
from functools import wraps
from inspect import signature


class TimeLimitExceeded(TimeoutError):
    pass


class CaptchaException(Exception):
    pass


class OperationFailed(RuntimeError):
    pass


class PendingAnswerError(OperationFailed):
    """Exam processing requires attention; automatic task execution cannot continue."""


class SubmissionUncertainError(PendingAnswerError):
    """A request may have been accepted; automatic resubmission is unsafe."""


class CourseResolutionError(OperationFailed):
    pass


def course_run(method):
    """Reset a course deadline and propagate partial failures to the CLI."""
    method_signature = signature(method)
    course_parameter = list(method_signature.parameters)[1]

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        bound = method_signature.bind(self, *args, **kwargs)
        course_id = bound.arguments[course_parameter]
        previous = getattr(self, "_active_course_id", None)
        self._startCourse(course_id)
        self._active_course_id = course_id
        failures = []
        pending = []
        previous_failures = getattr(self, "_course_failures", None)
        previous_pending = getattr(self, "_course_pending", None)
        self._course_failures = failures
        self._course_pending = pending
        try:
            result = method(self, *args, **kwargs)
            if pending:
                error_type = (
                    SubmissionUncertainError
                    if any(isinstance(error, SubmissionUncertainError) for error in pending)
                    else PendingAnswerError
                )
                raise error_type(f"课程 {course_id} 有 {len(pending)} 项等待恢复：{pending[0]}")
            if failures:
                raise OperationFailed(f"课程 {course_id} 有 {len(failures)} 项失败：{failures[0]}")
            return result
        except TimeLimitExceeded as error:
            if failures:
                raise OperationFailed(
                    f"课程 {course_id} 达到时限，且此前有 {len(failures)} 项失败：{failures[0]}"
                ) from error
            raise
        finally:
            self._active_course_id = previous
            self._course_failures = previous_failures
            self._course_pending = previous_pending

    return wrapped


@dataclass
class TaskSummary:
    succeeded: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    error_types: list[str] = field(default_factory=list)

    @property
    def exit_code(self):
        return 1 if self.failed or self.pending else 0

    def __str__(self):
        return (
            f"完成 {len(self.succeeded)}，失败 {len(self.failed)}，"
            f"待处理 {len(self.pending)}，跳过 {len(self.skipped)}"
        )
