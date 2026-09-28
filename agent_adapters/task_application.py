"""Assemble the v2 whole-task loop without changing the T5 application."""

from __future__ import annotations

import hashlib
import os
import stat
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_adapters.action_evidence_store import T6ActionEvidenceStore
from agent_adapters.application import ErrorEvidence, RecordedSandbox
from agent_adapters.attachment_import_journal import (
    T6AttachmentImportService,
    T6ImportClaim,
    T6ImportConflict,
    T6ImportJournal,
)
from agent_adapters.command_recorder import T6CommandRecorder
from agent_adapters.observation_proof_store import T6ObservationProofStore
from agent_adapters.pending_candidate_store import PendingCandidateStore
from agent_adapters.postcondition_store import T6PostconditionStore
from agent_adapters.run_files import immutable_write
from agent_adapters.task_contract import TaskRunRequest
from agent_adapters.task_simulation import (
    FixtureTurnGenerator,
    SimulatedTargetNetwork,
    SimulatedTaskSandbox,
    TaskFixtureRuntime,
)
from agent_core.action_audit import T6ActionJournal
from agent_core.action_invocation import AuditedT6ActionModel
from agent_core.command_contracts import (
    SandboxCommandAuthorization,
    SandboxCommandPolicy,
)
from agent_core.command_gateway import T6CommandGateway
from agent_core.contracts import ModelBackend
from agent_core.control_state_repository import ControlStateRepository
from agent_core.controller import TaskRunController, TaskRunControllerRegistry
from agent_core.incremental_intent_runner import IncrementalIntentRunner
from agent_core.intent_generation import AuditedTurnGenerator, OrderedIntentGenerator
from agent_core.intent_postconditions import T6PostconditionVerifier
from agent_core.modeling import ModelInvocationCoordinator
from agent_core.observation_proof import T6ObservationProofVerifier
from agent_core.pending_candidates import PendingCandidateRecorder
from agent_core.run_supervisor import RunSupervisor
from agent_core.runtime import TERMINAL_STATUSES
from agent_core.sqlite_control_store import SqliteControlStore
from Sandbox.command_attachment import (
    T6Attachment,
    T6AttachmentImporter,
    validate_attachment_batch,
)


class _Deadline:
    def __init__(self, controller) -> None:
        self.controller = controller

    def remaining_seconds(self) -> float:
        return self.controller.control_state.task_run.deadline_at - time.time()


class _WorkCopyImporter:
    def __init__(self, importer: T6AttachmentImporter) -> None:
        self.importer = importer

    def import_one(self, sandbox_id, attachment, timeout_seconds, token):
        return self.importer.copy_to_work(sandbox_id, attachment, timeout_seconds, token)

    def readback(self, sandbox_id, attachment, timeout_seconds, token):
        return self.importer.readback_work(sandbox_id, attachment, timeout_seconds, token)


def _prepare_attachment(root: Path, value: str) -> T6Attachment:
    source = root
    relative = Path(value)
    if not relative.parts or ".." in relative.parts or relative.is_absolute():
        raise ValueError("attachment_source_rejected")
    for index, part in enumerate(relative.parts):
        source = source / part
        info = source.lstat()
        if stat.S_ISLNK(info.st_mode) or (
            index < len(relative.parts) - 1 and not stat.S_ISDIR(info.st_mode)
        ):
            raise ValueError("attachment_source_rejected")
    if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024 * 1024:
        raise ValueError("attachment_source_rejected")
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError("attachment_source_changed")
        length = 0
        while block := stream.read(65536):
            length += len(block)
            if length > 64 * 1024 * 1024:
                raise ValueError("attachment_run_capacity_exceeded")
            digest.update(block)
        finished = os.fstat(stream.fileno())
    if (length != info.st_size or (opened.st_mtime_ns, opened.st_size)
            != (finished.st_mtime_ns, finished.st_size)):
        raise ValueError("attachment_source_changed")
    return T6Attachment(source.name, source, length, digest.hexdigest())


def prepare_attachments(source_root: str, request: TaskRunRequest) -> tuple[T6Attachment, ...]:
    root = Path(source_root).resolve(strict=True)
    items = [
        _prepare_attachment(root, attachment.value)
        for attachment in request.task.target.attachments
    ]
    result = tuple(items)
    validate_attachment_batch(result)
    return result


class TaskApplication:
    def __init__(self, directory: Path, declaration: dict, token, *, settings=None) -> None:
        self.directory, self.declaration, self.token = directory, declaration, token
        self.task_id = declaration["task_run_id"]
        self.request = TaskRunRequest.parse(declaration["request"])
        self.store = SqliteControlStore.open(directory / "control.sqlite")
        self.commands = T6CommandRecorder(directory / "commands")
        self.postcondition_store = T6PostconditionStore(directory / "conditions")
        self.proof_store = T6ObservationProofStore(directory / "proofs")
        self.candidate_store = PendingCandidateStore(directory / "candidates")
        self.journal = T6ActionJournal(
            self.store, T6ActionEvidenceStore(directory / "actions"),
        )
        self.proofs = T6ObservationProofVerifier(
            self.store, self.postcondition_store, self.commands,
            self.proof_store, time.time, self.journal,
        )
        self.repository = ControlStateRepository(
            self.store, self.commands, t6_observation_proofs=self.proofs,
        )
        self.sandbox: Any
        target_network = None
        if declaration["mode"] == "real":
            from Sandbox.command_sandbox import T6TaskRunSandbox
            from Sandbox.docker_runtime import DockerRuntime
            from Sandbox.settings import SandboxSettings
            from Sandbox.target_network import T6TargetNetwork

            runtime = DockerRuntime(SandboxSettings(), prepare_network=False)
            self.sandbox = T6TaskRunSandbox(runtime)
            if self.request.task.target.endpoints:
                target_network = T6TargetNetwork(runtime)
        elif self.request.task.target.endpoints:
            self.sandbox = SimulatedTaskSandbox()
            target_network = SimulatedTargetNetwork()
        else:
            self.sandbox = SimulatedTaskSandbox()
        self.controller = TaskRunController(
            RecordedSandbox(self.sandbox, directory), clock=time.time,
            store=self.store, repository=self.repository,
            registry=TaskRunControllerRegistry(),
            t6_observation_proofs=self.proofs,
            target_network_adapter=target_network,
        )
        self.fixture_runtime = (
            TaskFixtureRuntime(self._current_fixture(), self.controller, self.journal)
            if declaration["mode"] == "simulated" else None
        )
        self.settings = settings

    @property
    def state(self):
        return self.controller.control_state

    def _current_fixture(self):
        updates = sorted(
            self.directory.glob("fixture-update-*.json"),
            key=lambda path: path.stat().st_mtime_ns,
        )
        if updates:
            from agent_adapters.run_files import immutable_read
            return immutable_read(updates[-1])
        return self.declaration["fixture"]

    def _interrupt(self) -> bool:
        if self.state.task_run.status in TERMINAL_STATUSES:
            return True
        if self.token.is_cancelled:
            self.controller.cancel(self.declaration["ids"]["cancel"])
            return True
        if time.time() >= self.state.task_run.deadline_at:
            self.controller.advance_time(str(uuid4()))
            return True
        return False

    def _attachments(self) -> tuple[T6Attachment, ...]:
        expected = self.declaration["attachments"]
        paths = self.request.task.target.attachments
        if len(paths) != len(expected):
            raise ValueError("attachment_declaration_mismatch")
        items = tuple(
            T6Attachment(
                record["name"], Path(self.declaration["source_root"]) / path.value,
                record["byte_size"], record["sha256"],
            )
            for path, record in zip(paths, expected)
        )
        if any(Path(path.value).name != item.name for path, item in zip(paths, items)):
            raise ValueError("attachment_declaration_mismatch")
        validate_attachment_batch(items, check_source=False)
        return items

    def _import(self) -> None:
        if self.fixture_runtime is not None:
            return
        importer = T6AttachmentImporter(self.sandbox)
        original = T6AttachmentImportService(
            importer, T6ImportJournal(self.directory / "input-imports.sqlite"),
        )
        copies = T6AttachmentImportService(
            _WorkCopyImporter(importer),
            T6ImportJournal(self.directory / "work-copies.sqlite"),
            source_required=False,
        )
        items = self._attachments()
        for index, item in enumerate(items):
            if self._interrupt():
                return
            if original.journal.get(self.declaration["ids"]["imports"][index]) is None:
                current = _prepare_attachment(
                    Path(self.declaration["source_root"]).resolve(strict=True),
                    self.request.task.target.attachments[index].value,
                )
                if current != item:
                    raise ValueError("attachment_source_changed")
            original.import_one(
                self.declaration["ids"]["imports"][index], self.task_id,
                self.state.task_run.sandbox_id, item, 60.0, self.token,
            )
            copies.import_one(
                self.declaration["ids"]["copies"][index], self.task_id,
                self.state.task_run.sandbox_id, item, 60.0, self.token,
            )

    def recover_imports(self) -> None:
        """Explicitly verify claimed Sandbox files without resending their bytes."""
        if self.declaration["mode"] != "real":
            raise ValueError("import_recovery_requires_real_mode")
        importer = T6AttachmentImporter(self.sandbox)
        services = (
            (T6AttachmentImportService(
                importer, T6ImportJournal(self.directory / "input-imports.sqlite"),
            ), "imports"),
            (T6AttachmentImportService(
                _WorkCopyImporter(importer),
                T6ImportJournal(self.directory / "work-copies.sqlite"),
                source_required=False,
            ), "copies"),
        )
        for service, key in services:
            for index, item in enumerate(self._attachments()):
                if self._interrupt():
                    return
                import_id = self.declaration["ids"][key][index]
                previous = service.journal.get(import_id)
                if previous is None:
                    continue
                claim = T6ImportClaim.for_attachment(
                    import_id, self.task_id, self.state.task_run.sandbox_id, item,
                )
                if previous[0] != claim:
                    raise T6ImportConflict("import_claim_missing_or_changed")
                if previous[1] == "claimed":
                    service.recover_one(claim, 60.0, self.token)

    async def execute(self, *, start: bool) -> None:
        if start:
            immutable_write(self.directory / "start-attempt.json", {"task_run_id": self.task_id})
            self.controller.start(
                self.request.task, self.request.task.model_backend or ModelBackend.DEEPSEEK_OPENAI,
                self.declaration["ids"]["start"],
                source_root=self.declaration["source_root"], task_run_id=self.task_id,
            )
        else:
            self.controller.attach(self.task_id)
        if self._interrupt():
            return
        self._import()
        if self._interrupt():
            return
        turn_adapter: Any
        action_adapter: Any
        command_sandbox: Any
        files: Any
        if self.fixture_runtime is None:
            from agent_adapters.deepseek_model import (
                DeepSeekModelAdapter,
                DeepSeekT6ActionAdapter,
            )
            from Sandbox.command_runtime import T6CommandSandbox
            from Sandbox.work_file_digest import T6FileDigest

            turn_adapter = DeepSeekModelAdapter(self.settings)
            action_adapter = DeepSeekT6ActionAdapter(
                self.settings, output_tokens=self.declaration["t6_output_tokens"],
            )
            command_sandbox = T6CommandSandbox(self.sandbox)
            files = T6FileDigest(self.sandbox)
        else:
            turn_adapter = action_adapter = command_sandbox = files = self.fixture_runtime
        generator: Any = AuditedTurnGenerator(OrderedIntentGenerator(
            self.controller, self.repository, self.store,
            ModelInvocationCoordinator(
                turn_adapter, _Deadline(self.controller), ErrorEvidence(), audit=self.store,
            ),
        ))
        if self.fixture_runtime is not None:
            generator = FixtureTurnGenerator(generator, self.fixture_runtime)
        gateway = T6CommandGateway(
            controller=self.controller, policy=SandboxCommandPolicy(),
            sandbox=command_sandbox, recorder=self.commands,
            clock=time.time,
            step_limit_seconds=int(self.declaration.get("t6_step_timeout_seconds", 60)),
        )
        runner = IncrementalIntentRunner(
            controller=self.controller, store=self.store, repository=self.repository,
            journal=self.journal,
            model=AuditedT6ActionModel(self.journal, action_adapter),
            gateway=gateway,
            postconditions=T6PostconditionVerifier(
                self.controller, self.postcondition_store, self.commands, files,
            ),
            proofs=self.proofs,
            candidates=PendingCandidateRecorder(self.candidate_store, self.commands),
            candidate_store=self.candidate_store,
            authorization=SandboxCommandAuthorization(
                self.declaration["ids"]["authorization"], self.task_id,
                ("sandbox.command",),
            ),
            cancellation_token=self.token,
            fixture_budget=self.fixture_runtime,
        )
        result = await RunSupervisor(
            self.controller, self.commands, runner, generator=generator,
        ).run(self.token)
        immutable_write(
            self.directory / ("dispatch-stop-" + str(uuid4()) + ".json"),
            {"stop": result.stop, "reason": result.reason,
             "at": time.time(), "task_run_id": self.task_id},
        )

    def close(self) -> None:
        try:
            self.sandbox.close()
        finally:
            self.store.close()
