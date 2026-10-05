"""Real current-user protection with real D3 Controller/SQLite/proof references."""

from __future__ import annotations

import json
import os
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_d3_chain import (  # noqa: F401 -- reuse real Controller chain fixture
    run,
    scenario,
)

from agent_adapters import cli
from agent_adapters.cli import parser
from agent_adapters.evidence_query import query_d5_evidence
from agent_adapters.protected_evidence import (
    ProtectedEvidenceDeleted,
    ProtectedEvidenceError,
    ProtectedEvidenceStore,
)
from agent_adapters.sensitive_evidence_service import (
    D6SensitiveOutputContract,
    TrustedSensitiveEvidenceService,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows current-user protection required")


def test_sensitive_cli_has_only_exact_terminal_actions():
    view = parser().parse_args([
        "evidence-sensitive", "view", "--run-dir", "run",
        "--task-run-id", str(uuid4()), "--evidence-id", str(uuid4()),
    ])
    delete = parser().parse_args([
        "evidence-sensitive", "delete", "--run-dir", "run",
        "--task-run-id", str(uuid4()), "--evidence-id", str(uuid4()),
    ])
    assert (view.action, delete.action) == ("view", "delete")
    assert not hasattr(view, "html") and not hasattr(view, "output")


class _TerminalOutput:
    encoding = "utf-8"

    def __init__(self, *, is_tty: bool = True) -> None:
        self.buffer = BytesIO()
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty

    def flush(self) -> None:
        pass


def _sensitive_args():
    return parser().parse_args([
        "evidence-sensitive", "view", "--run-dir", "run",
        "--task-run-id", str(uuid4()), "--evidence-id", str(uuid4()),
    ])


def _terminal(monkeypatch, *, stdin_tty=True, stdout_tty=True, stderr_tty=True):
    stderr = _TerminalOutput(is_tty=stderr_tty)
    monkeypatch.setattr(
        cli,
        "sys",
        SimpleNamespace(
            stdin=SimpleNamespace(isatty=lambda: stdin_tty),
            stdout=_TerminalOutput(is_tty=stdout_tty),
            stderr=stderr,
        ),
    )
    return stderr


def test_sensitive_cli_refuses_pipe_or_redirect_before_open(monkeypatch):
    _terminal(monkeypatch, stdout_tty=False)
    opened = []

    class Service:
        def __init__(self, *args, **kwargs):
            opened.append(True)

    monkeypatch.setattr(
        "agent_adapters.sensitive_evidence_service.TrustedSensitiveEvidenceService",
        Service,
    )
    with pytest.raises(ValueError, match="^d6_sensitive_view_requires_terminal$"):
        cli.dispatch(_sensitive_args())
    assert opened == []


def test_sensitive_cli_validates_full_utf8_body_before_any_output(monkeypatch):
    stderr = _terminal(monkeypatch)

    class Service:
        def __init__(self, *args, **kwargs):
            pass

        def view(self, evidence_id, *, validate_body):
            validate_body(b"prefix\xffsuffix")

    monkeypatch.setattr(
        "agent_adapters.sensitive_evidence_service.TrustedSensitiveEvidenceService",
        Service,
    )
    with pytest.raises(ProtectedEvidenceError, match="^d6_evidence_body_not_text$"):
        cli.dispatch(_sensitive_args())
    assert stderr.buffer.getvalue() == b""


def test_sensitive_cli_escapes_terminal_controls_before_output(monkeypatch):
    stderr = _terminal(monkeypatch)

    class Service:
        def __init__(self, *args, **kwargs):
            pass

        def view(self, evidence_id, *, validate_body):
            body = b"credential\n\x1b]52;c;payload"
            validate_body(body)
            return body

    monkeypatch.setattr(
        "agent_adapters.sensitive_evidence_service.TrustedSensitiveEvidenceService",
        Service,
    )
    result = cli.dispatch(_sensitive_args())
    assert result["status"] == "viewed"
    assert stderr.buffer.getvalue() == b"credential\\u000a\\u001b]52;c;payload\n"


def _service(case):
    assert run(case).stop_reason == "turn_completed_sandbox_retained"
    app = case[0]
    fact = app.state.facts[0]
    proof_id = fact.verification_ref.removeprefix("d3:")
    contract = D6SensitiveOutputContract("synthetic.credentials", "credential")
    service = TrustedSensitiveEvidenceService(
        app.directory, app.state.task_run.task_run_id,
        (contract,),
        clock=case[5],
    )
    return service, contract, fact.source_ref, proof_id


def _save(service, contract, execution_id, proof_id, *, body=b"D6-SYNTHETIC-PRIVATE-VALUE"):
    evidence_id = str(uuid4())
    service.save_sensitive(
        contract=contract, evidence_id=evidence_id,
        body=body, execution_id=execution_id, proof_id=proof_id,
    )
    return evidence_id


def test_sensitive_body_stays_out_of_ordinary_storage_and_query(scenario):  # noqa: F811 -- pytest fixture injection
    service, contract, execution_id, proof_id = _service(scenario)
    private = b"D6-SYNTHETIC-PRIVATE-VALUE"
    evidence_id = _save(service, contract, execution_id, proof_id, body=private)
    before = {str(path.relative_to(service.run_dir)): path.read_bytes() for path in service.run_dir.rglob("*") if path.is_file()}
    assert all(private not in value for value in before.values())
    result = query_d5_evidence(None, service.task_run_id, sensitive_config=None, run_dir=service.run_dir)
    assert private.decode() not in json.dumps(result)
    assert result["sensitive_evidence"][0]["body_status"] == "withheld"
    assert result["sensitive_evidence"][0]["body"] is None
    assert result["proof_recheck"] == [{"proof_id": proof_id, "evidence_ids": [evidence_id], "recheck_status": "available"}]
    after = {str(path.relative_to(service.run_dir)): path.read_bytes() for path in service.run_dir.rglob("*") if path.is_file()}
    assert before == after  # ordinary query is read-only, including audit/lock files
    assert service.view(evidence_id) == private
    audit = (service.store.evidence_dir / "audit.jsonl").read_text()
    assert private.decode() not in audit and "success" in audit


def test_sensitive_write_rejects_unregistered_and_foreign_references_before_body(scenario):  # noqa: F811 -- pytest fixture injection
    service, contract, execution_id, proof_id = _service(scenario)
    for adapter, execution, proof, code in (
        (D6SensitiveOutputContract("synthetic.credentials", "credential"), execution_id, proof_id, "d6_sensitive_adapter_unregistered"),
        (contract, str(uuid4()), proof_id, "d6_sensitive_reference_ownership_invalid"),
        (contract, execution_id, str(uuid4()), "d6_sensitive_reference_ownership_invalid"),
    ):
        with pytest.raises(ProtectedEvidenceError, match=f"^{code}$"):
            service.save_sensitive(contract=adapter, evidence_id=str(uuid4()), body=b"private", execution_id=execution, proof_id=proof)
    assert service.store.list_metadata() == ()


def test_view_refuses_before_decrypt_when_audit_cannot_commit(scenario, monkeypatch):  # noqa: F811 -- pytest fixture injection
    service, contract, execution_id, proof_id = _service(scenario)
    evidence_id = _save(service, contract, execution_id, proof_id)
    calls = []
    monkeypatch.setattr(service.store.protection, "unprotect", lambda *args: calls.append(args))

    def fail(**kwargs):
        raise ProtectedEvidenceError("d6_audit_write_failed")

    monkeypatch.setattr(service.store.audit, "record", fail)
    with pytest.raises(ProtectedEvidenceError, match="^d6_audit_write_failed$"):
        service.view(evidence_id)
    assert calls == []


def test_delete_in_live_run_retains_history_but_linked_proof_is_unverifiable(scenario):  # noqa: F811 -- pytest fixture injection
    service, contract, execution_id, proof_id = _service(scenario)
    evidence_id = _save(service, contract, execution_id, proof_id)
    historical = scenario[0].state.facts[0].status
    assert scenario[0].state.task_run.status.value == "running"
    assert service.delete(evidence_id)["body_status"] == "deleted"
    with pytest.raises(ProtectedEvidenceDeleted):
        service.view(evidence_id)
    assert scenario[0].state.facts[0].status == historical
    assert scenario[0].state.task_run.status.value == "running"
    result = query_d5_evidence(None, service.task_run_id, sensitive_config=None, run_dir=service.run_dir)
    assert result["facts"][0]["verification_status"] == "verified"
    assert result["facts"][0]["verification_source_status"] == "evidence_deleted_unverifiable"
    assert result["proof_recheck"][0]["recheck_status"] == "evidence_deleted_unverifiable"
    scenario[0].controller.cancel(str(uuid4()))
    scenario[0].controller.cleanup_sandbox()
    assert service.query(evidence_id)["body_status"] == "deleted"


def test_tombstone_prevents_view_and_repeated_delete_cleans_residual_cipher(scenario, monkeypatch):  # noqa: F811 -- pytest fixture injection
    service, contract, execution_id, proof_id = _service(scenario)
    evidence_id = _save(service, contract, execution_id, proof_id)
    cipher = service.store.evidence_dir / f"{evidence_id}.cipher"
    original = Path.unlink

    def fail_cipher(path, *args, **kwargs):
        if path == cipher:
            raise OSError("synthetic unlink failure")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_cipher)
        with pytest.raises(ProtectedEvidenceError, match="^d6_evidence_delete_failed$"):
            service.delete(evidence_id)
    assert cipher.exists()
    assert service.query(evidence_id)["status"] == "deleted"
    with pytest.raises(ProtectedEvidenceDeleted):
        service.view(evidence_id)
    service.delete(evidence_id)
    assert not cipher.exists()


def test_reopen_rejects_wrong_task_and_never_repairs_invalid_acl(scenario):  # noqa: F811 -- pytest fixture injection
    from test_d6_windows_protection import _replace_dacl

    service, contract, execution_id, proof_id = _service(scenario)
    _save(service, contract, execution_id, proof_id)
    with pytest.raises(ProtectedEvidenceError, match="^d6_evidence_ownership_invalid$"):
        ProtectedEvidenceStore(service.run_dir, str(uuid4()), service.store.protection, scenario[5], service.store.audit, create=False)
    directory = service.store.evidence_dir
    _replace_dacl(service.store.protection, directory, f"D:P(A;OICI;FA;;;{service.store.protection.identity})(A;OICI;FR;;;WD)")
    try:
        with pytest.raises(ProtectedEvidenceError, match="^d6_protected_directory_failed$"):
            TrustedSensitiveEvidenceService(service.run_dir, service.task_run_id, create=False)
        with pytest.raises(ProtectedEvidenceError, match="^d6_protected_directory_failed$"):
            service.view(str(uuid4()))
    finally:
        service.store.protection.restrict_directory(directory)
