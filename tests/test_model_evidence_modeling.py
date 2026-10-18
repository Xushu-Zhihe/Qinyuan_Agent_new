"""Model results stay bound to their original request and actual configuration."""

import asyncio
import threading
from dataclasses import asdict, replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_adapters.model_evidence import D1ModelEvidenceStore
from agent_core.attachment_import import AttachmentImportReceipt
from agent_core.domain import Intent, Turn
from agent_core.runtime import TaskRunStatus
from agent_core.turn_contracts import (
    D1Authorization,
    D1OperationRecord,
    D1RecoveryRequired,
    D1RoleConfig,
    D1RunConfig,
    D1TurnBinding,
)
from agent_core.turn_modeling import (
    D1AuditedModel,
    D1ModelRequest,
    D1OuterContext,
    D1ProposalRejected,
    digest,
)


class Ledger:
    def __init__(self, config):
        self.config = config
        self.records = {}
        self.bindings = {}

    def load_d1_run_config(self, task_run_id):
        return self.config

    def load_d1_operation(self, model_request_id):
        return self.records.get(model_request_id)

    def load_d1_turn_binding(self, turn_id):
        return self.bindings.get(turn_id)

    def begin_d1_operation(self, start):
        self.records[start.operation_id] = D1OperationRecord(start)

    def complete_d1_operation(self, completion):
        record = self.records[completion.operation_id]
        value = replace(record, completion=completion)
        self.records[completion.operation_id] = value
        return value


class Adapter:
    def __init__(self, role_config):
        self.role_config = role_config
        self.calls = 0

    def invoke(self, request, timeout_seconds, cancellation_token):
        self.calls += 1
        return {
            "turn_target": "inspect copy",
            "completion_rule": {
                "rule_id": "attachment_copy_sha256",
                "version": 1,
                "parameters": {"attachment_index": 0},
            },
            "intents": [{"description": "verify copy", "intent_type": "digest"}],
        }


@pytest.fixture
def model_case(tmp_path):
    outer = D1RoleConfig("outer_planning", "fake", "model-a", "outer-v1")
    inner = D1RoleConfig("inner_action", "fake", "model-a", "inner-v1")
    config = D1RunConfig(outer, inner, D1Authorization(str(uuid4())))
    task_id = str(uuid4())
    receipt = AttachmentImportReceipt(
        str(uuid4()),
        task_id,
        "sandbox",
        0,
        "sample.txt",
        "C:/source",
        "/workspace/input/sample.txt",
        3,
        "a" * 64,
        1.0,
        2.0,
    )
    context = D1OuterContext("inspect copy", receipt)
    request = D1ModelRequest(
        str(uuid4()),
        task_id,
        1,
        digest(asdict(context)),
        outer.role,
        outer.role_config_ref,
        str(uuid4()),
        "outer",
        context,
    )
    state = SimpleNamespace(
        task_run=SimpleNamespace(
            task_run_id=task_id, status=TaskRunStatus.RUNNING, deadline_at=100.0
        ),
        latest_checkpoint=SimpleNamespace(sequence=1),
        turns=(),
        intents=(),
        plans=(),
        facts=(),
    )
    ledger, adapter = Ledger(config), Adapter(outer)
    evidence = D1ModelEvidenceStore(tmp_path / "model-evidence")
    model = D1AuditedModel(
        SimpleNamespace(control_state=state),
        ledger,
        evidence,
        adapter,
        outer,
        lambda: 3.0,
    )
    return model, request, ledger, adapter, state, evidence


def test_saved_result_reuses_provider_and_rejects_changed_original_checkpoint(
    model_case,
):
    model, request, _, adapter, _, _ = model_case
    first = model.propose(request, SimpleNamespace(is_cancelled=False))
    assert model.propose(request, SimpleNamespace(is_cancelled=False)) == first
    with pytest.raises(D1RecoveryRequired, match="envelope_mismatch"):
        model.propose(
            replace(request, base_checkpoint_sequence=2),
            SimpleNamespace(is_cancelled=False),
        )
    assert adapter.calls == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("resource_ref", "changed-registration"),
        ("task_run_id", "changed-task"),
        ("category", "inner_probe_model"),
        ("role", "inner_action"),
    ],
)
def test_saved_result_rejects_changed_audit_envelope(model_case, field, value):
    model, request, ledger, adapter, _, _ = model_case
    model.propose(request, SimpleNamespace(is_cancelled=False))
    record = ledger.records[request.model_request_id]
    # Bypass constructor checks to represent corrupted persisted metadata.
    start = SimpleNamespace(**asdict(record.start))
    setattr(start, field, value)
    ledger.records[request.model_request_id] = replace(record, start=start)
    with pytest.raises(D1RecoveryRequired, match="envelope_mismatch"):
        model.propose(request, SimpleNamespace(is_cancelled=False))
    assert adapter.calls == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("model", "model-b"),
        ("prompt_template", "outer-v2"),
        ("parameters", (("temperature", 0.5),)),
    ],
)
def test_actual_adapter_config_change_stops_before_call_or_replay(
    model_case, field, value
):
    model, request, _, adapter, _, _ = model_case
    original = adapter.role_config
    adapter.role_config = replace(original, **{field: value})
    with pytest.raises(D1RecoveryRequired, match="adapter_role_configuration_changed"):
        model.propose(request, SimpleNamespace(is_cancelled=False))
    assert adapter.calls == 0
    adapter.role_config = original
    model.propose(request, SimpleNamespace(is_cancelled=False))
    adapter.role_config = replace(original, **{field: value})
    with pytest.raises(D1RecoveryRequired, match="adapter_role_configuration_changed"):
        model.propose(request, SimpleNamespace(is_cancelled=False))
    assert adapter.calls == 1


def test_application_rejection_preserves_known_call_and_replays_rejection(model_case):
    model, request, ledger, adapter, _, evidence = model_case
    model.propose(request, SimpleNamespace(is_cancelled=False))
    completion = ledger.records[request.model_request_id].completion
    original = evidence.read(request.model_request_id)
    model.reject_application(request.model_request_id, "fixed_probe_rejected")
    model.reject_application(request.model_request_id, "fixed_probe_rejected")
    assert ledger.records[request.model_request_id].completion == completion
    assert evidence.read(request.model_request_id) == original
    assert (
        evidence.read_application_rejection(request.model_request_id).rejection_code
        == "fixed_probe_rejected"
    )
    with pytest.raises(D1ProposalRejected, match="fixed_probe_rejected"):
        model.propose(request, SimpleNamespace(is_cancelled=False))
    assert adapter.calls == 1


def test_historical_result_requires_applied_record_and_matching_registered_objects(
    model_case,
):
    model, request, ledger, adapter, state, _ = model_case
    model.propose(request, SimpleNamespace(is_cancelled=False))
    state.latest_checkpoint.sequence = 2
    token = SimpleNamespace(is_cancelled=False)
    with pytest.raises(D1RecoveryRequired, match="saved_model_result_stale"):
        model.read_saved(
            request.model_request_id,
            "outer",
            request.context,
            request.registration_mutation_id,
            token,
        )
    turn_id, intent_id = str(uuid4()), str(uuid4())
    binding = D1TurnBinding(
        str(uuid4()),
        request.task_run_id,
        turn_id,
        intent_id,
        0,
        request.context.attachment.import_id,
        "a" * 64,
        f"/workspace/work/intents/{intent_id}/attachment",
        str(uuid4()),
        request.model_request_id,
    )
    ledger.bindings[turn_id] = binding
    state.turns = (Turn(turn_id, request.task_run_id, 1, "inspect copy", (intent_id,)),)
    state.intents = (Intent(intent_id, turn_id, "verify copy", 1, "digest"),)
    ledger.records[request.model_request_id] = replace(
        ledger.records[request.model_request_id], application_ref=turn_id
    )
    assert (
        model.read_saved(
            request.model_request_id,
            "outer",
            request.context,
            request.registration_mutation_id,
            token,
        )[0]
        == request.model_request_id
    )
    state.turns[0].turn_target = "changed target"
    with pytest.raises(D1RecoveryRequired, match="saved_model_result_stale"):
        model.read_saved(
            request.model_request_id,
            "outer",
            request.context,
            request.registration_mutation_id,
            token,
        )
    assert adapter.calls == 1


def test_async_cancel_returns_without_waiting_for_uncooperative_adapter(model_case):
    model, request, ledger, adapter, _, evidence = model_case
    entered, release, completed = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    original = adapter.invoke
    token = SimpleNamespace(is_cancelled=False)

    def blocked(*args):
        entered.set()
        release.wait(2)
        try:
            return original(*args)
        finally:
            completed.set()

    adapter.invoke = blocked

    async def exercise():
        task = asyncio.create_task(model.propose_async(request, token))
        while not entered.is_set():
            await asyncio.sleep(0.001)
        token.is_cancelled = True
        with pytest.raises(D1RecoveryRequired, match="outcome_unknown"):
            await asyncio.wait_for(task, timeout=0.3)
        assert ledger.records[request.model_request_id].completion is None
        assert evidence.read(request.model_request_id) is None
        release.set()
        while not completed.is_set():
            await asyncio.sleep(0.001)
        token.is_cancelled = False
        with pytest.raises(D1RecoveryRequired):
            await model.propose_async(request, token)
        assert ledger.records[request.model_request_id].completion is None
        assert evidence.read(request.model_request_id) is None
        assert adapter.calls == 1

    try:
        asyncio.run(exercise())
    finally:
        release.set()


def test_async_known_late_response_is_saved_stale_and_never_applied(model_case):
    model, request, ledger, adapter, _, evidence = model_case
    original = adapter.invoke
    token = SimpleNamespace(is_cancelled=False)

    def late(*args):
        value = original(*args)
        token.is_cancelled = True
        return value

    adapter.invoke = late
    with pytest.raises(D1ProposalRejected, match="model_response_stale"):
        asyncio.run(model.propose_async(request, token))
    assert ledger.records[request.model_request_id].completion.result_kind == "stale"
    assert evidence.read(request.model_request_id).outcome == "stale"
    assert ledger.records[request.model_request_id].application_ref is None
