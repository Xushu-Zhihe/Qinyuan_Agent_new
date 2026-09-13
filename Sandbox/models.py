from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


def _resolved_directory(path: Path, field_name: str) -> Path:
    candidate = path.expanduser()
    if ".." in candidate.parts:
        raise ValueError(f"{field_name} must not contain traversal: {candidate}")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"{field_name} does not exist: {candidate}") from exc
    if not resolved.is_dir():
        raise ValueError(f"{field_name} is not a directory: {resolved}")
    if candidate.absolute() != resolved:
        raise ValueError(
            f"{field_name} must not contain traversal or symbolic links: {candidate}"
        )
    return resolved


@dataclass(frozen=True, slots=True)
class SandboxMounts:
    challenge_dir: Path | None = None
    work_dir: Path | None = None

    def __post_init__(self) -> None:
        challenge = (
            _resolved_directory(self.challenge_dir, "challenge_dir")
            if self.challenge_dir is not None
            else None
        )
        work = (
            _resolved_directory(self.work_dir, "work_dir")
            if self.work_dir is not None
            else None
        )
        if challenge is not None and work is not None:
            if challenge.is_relative_to(work) or work.is_relative_to(challenge):
                raise ValueError("challenge_dir and work_dir must not overlap")
        object.__setattr__(self, "challenge_dir", challenge)
        object.__setattr__(self, "work_dir", work)

class SandboxStatus(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"

@dataclass(frozen=True,slots=True)
class SandboxSession:
    agent_id: str
    sandbox_id: str
    status: SandboxStatus
    mounts: SandboxMounts = field(default_factory=SandboxMounts)

@dataclass(frozen=True,slots=True)
class CommandRequest:
    tool: str
    arguments: tuple[str,...] = ()
    timeout_seconds: int = 30

    @property
    def argv(self) -> list[str]:
        return [self.tool,*self.arguments]
     
@dataclass(frozen=True,slots=True)
class CommandResult:
    sandbox_id: str
    exit_code: int
    stdout: str
    stderr: str

    timed_out: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    termination_confirmed: bool = False
