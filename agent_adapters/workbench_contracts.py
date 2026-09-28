"""Small contracts shared by the workbench HTTP boundary and its runner."""

from __future__ import annotations

from http import HTTPStatus

ACTIVE_RUN_STATUSES = {"starting", "running", "cancelling", "cleaning", "paused", "recovery_required"}


class WorkbenchError(ValueError):
    def __init__(self, code: str, status: int = HTTPStatus.BAD_REQUEST):
        super().__init__(code)
        self.code = code
        self.status = status
