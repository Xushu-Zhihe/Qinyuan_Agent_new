"""Immutable declaration of Intent postconditions before tool dispatch."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from uuid import UUID, uuid5

from agent_adapters.command_recorder import T6CommandRecorder, T6EvidenceError
from agent_core.intent_postconditions import (
    FileDigestObservation,
    FileSha256,
    IntentPostconditions,
    OutputContains,
)


class T6PostconditionStore:
    def __init__(self, directory: Path, *, create: bool = True) -> None:
        self._records = T6CommandRecorder(directory, create=create)

    @staticmethod
    def _plan_key(task_run_id: str, plan_id: str) -> str:
        return str(uuid5(UUID(task_run_id), f"postcondition:{plan_id}"))

    def _uses_plan_key(self, binding: IntentPostconditions) -> bool:
        return self._records._read(
            "postconditions", self._plan_key(binding.task_run_id, binding.plan_id)
        ) is not None

    def bind(self, binding: IntentPostconditions) -> None:
        value = {
            "task_run_id": binding.task_run_id,
            "intent_id": binding.intent_id,
            "plan_id": binding.plan_id,
            "conditions": [
                {
                    "kind": "output_contains",
                    "step_index": c.step_index,
                    "stream": c.stream,
                    "literal": c.literal,
                }
                if isinstance(c, OutputContains)
                else {
                    "kind": "file_sha256",
                    "step_index": c.step_index,
                    "path": c.path,
                    "expected_sha256": c.expected_sha256,
                }
                for c in binding.conditions
            ],
        }
        if self._records._read("postconditions", binding.intent_id) is None:
            self._records._write("postconditions", binding.intent_id, value)
        self._records._write(
            "postconditions", self._plan_key(binding.task_run_id, binding.plan_id), value
        )

    def read(
        self, plan_id: str, intent_id: str | None = None
    ) -> IntentPostconditions | None:
        legacy = self._records._read("postconditions", intent_id or plan_id)
        if intent_id is None:
            value = legacy
        elif legacy is None:
            value = None
        else:
            value = self._records._read(
                "postconditions", self._plan_key(legacy["task_run_id"], plan_id)
            )
            if value is None and legacy.get("plan_id") == plan_id:
                value = legacy
        if value is None:
            return None
        try:
            conditions = []
            for item in value["conditions"]:
                data = dict(item)
                kind = data.pop("kind")
                if kind == "output_contains":
                    conditions.append(OutputContains(**data))
                elif kind == "file_sha256":
                    conditions.append(FileSha256(**data))
                else:
                    raise ValueError("unsupported_postcondition_kind")
            return IntentPostconditions(
                value["task_run_id"],
                value["intent_id"],
                value["plan_id"],
                tuple(conditions),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise T6EvidenceError("invalid_postcondition_binding") from exc

    def observe_file(
        self,
        binding: IntentPostconditions,
        index: int,
        path: str,
        loader: Callable[[], FileDigestObservation],
    ) -> FileDigestObservation:
        key = binding.plan_id if self._uses_plan_key(binding) else binding.intent_id
        identity = str(uuid5(UUID(binding.task_run_id), f"{key}:{index}"))
        expected = {
            "task_run_id": binding.task_run_id,
            "intent_id": binding.intent_id,
            "index": index,
            "path": path,
        }
        if key == binding.plan_id:
            expected["plan_id"] = binding.plan_id
        existing = self._records._read("file-observation", identity)
        if existing is not None:
            if any(existing.get(k) != v for k, v in expected.items()):
                raise T6EvidenceError("file_observation_conflict")
            return FileDigestObservation(existing["status"], existing["sha256"])
        claim = self._records.directory / f"file-observation-claim-{identity}"
        try:
            claim.mkdir()
        except FileExistsError:
            return FileDigestObservation("unknown")
        observed = loader()
        if observed.status != "unknown":
            self._records._write(
                "file-observation",
                identity,
                {
                    **expected,
                    "status": observed.status,
                    "sha256": observed.sha256,
                },
            )
        return observed

    def read_file(
        self, binding: IntentPostconditions, index: int, path: str
    ) -> FileDigestObservation | None:
        key = binding.plan_id if self._uses_plan_key(binding) else binding.intent_id
        identity = str(uuid5(UUID(binding.task_run_id), f"{key}:{index}"))
        claim = self._records.directory / f"file-observation-claim-{identity}"
        if not claim.is_dir():
            return None
        value = self._records._read("file-observation", identity)
        if value is None:
            return None
        if any(
            value.get(key) != expected
            for key, expected in {
                "task_run_id": binding.task_run_id,
                "intent_id": binding.intent_id,
                "index": index,
                "path": path,
                **({"plan_id": binding.plan_id} if key == binding.plan_id else {}),
            }.items()
        ):
            raise T6EvidenceError("file_observation_conflict")
        return FileDigestObservation(value["status"], value["sha256"])
