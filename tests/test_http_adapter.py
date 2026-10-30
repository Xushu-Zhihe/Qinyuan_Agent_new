from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

import pytest

from agent_adapters.web.http_adapter import (
    D7HttpAdapter,
    HttpCapabilityError,
    D7HttpEvidenceMetadata,
    D7HttpRequest,
    D7HttpResponseEvidenceStore,
    D7HttpWireResponse,
)


class FakeProtection:
    identity = "S-1-5-21-d7-test"

    def protect(self, value: bytes, entropy: bytes) -> bytes:
        return b"protected:" + hashlib.sha256(entropy).digest() + value

    def unprotect(self, value: bytes, entropy: bytes) -> bytes:
        prefix = b"protected:" + hashlib.sha256(entropy).digest()
        if not value.startswith(prefix):
            raise ValueError("fake protection failed")
        return value[len(prefix) :]

    def restrict_directory(self, _directory: Path) -> None:
        pass

    def verify_directory(self, _directory: Path) -> None:
        pass


class FakeEvidence:
    def __init__(self, task_run_id: str):
        self.task_run_id = task_run_id
        self.saved: list[bytes] = []

    def save(self, operation_id: str, body: bytes):
        self.saved.append(body)
        return D7HttpEvidenceMetadata(
            1,
            self.task_run_id,
            str(uuid4()),
            operation_id,
            hashlib.sha256(body).hexdigest(),
            "b" * 64,
            len(body),
            1.0,
        )


class FakeTransport:
    def __init__(self, *responses: D7HttpWireResponse):
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def send(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def _adapter(transport: FakeTransport, *, task_run_id: str | None = None):
    run_id = task_run_id or str(uuid4())
    evidence = FakeEvidence(run_id)
    return D7HttpAdapter(
        run_id,
        "https://example.test:443",
        evidence,
        transport=transport,
    ), evidence


def _request(*, headers=(), path="/", operation_id=None):
    return D7HttpRequest(
        operation_id or str(uuid4()),
        "GET",
        path,
        tuple(headers),
        "",
    )


def test_wire_header_limits_include_injected_cookie_and_encoding_headers():
    cookie_value = "s=" + "x" * 8174 + "; Path=/"
    transport = FakeTransport(
        D7HttpWireResponse(200, (("set-cookie", cookie_value),), b"login"),
        D7HttpWireResponse(200, (), b"ok"),
    )
    adapter, evidence = _adapter(transport)
    adapter.execute(_request(path="/login"))
    assert len(transport.calls) == 1
    assert evidence.saved == [b"login"]

    large_headers = tuple((f"X-{index}", "v" * 8192) for index in range(7))
    with pytest.raises(HttpCapabilityError) as error:
        adapter.execute(_request(headers=large_headers, path="/next"))

    assert error.value.code == "d7_http_headers_too_large"
    assert len(transport.calls) == 1
    assert evidence.saved == [b"login"]


@pytest.mark.parametrize(
    "headers",
    [
        (("x-secret", "must-not-be-forwarded"),),
        tuple(("set-cookie", "a=b") for _ in range(101)),
        (("set-cookie", "a=" + "x" * 8192),),
        tuple(("set-cookie", "a=" + "x" * 8180) for _ in range(9)),
    ],
)
def test_untrusted_transport_response_headers_are_rejected_before_cookie_or_evidence(
    headers,
):
    task_run_id = str(uuid4())
    evidence = FakeEvidence(task_run_id)
    transport = FakeTransport(D7HttpWireResponse(200, headers, b"response"))
    adapter = D7HttpAdapter(
        task_run_id,
        "https://example.test:443",
        evidence,
        transport=transport,
    )

    with pytest.raises(HttpCapabilityError):
        adapter.execute(_request(path="/login"))

    assert evidence.saved == []
    assert adapter._cookies.request_header("https://example.test:443/next") is None


def test_user_cannot_supply_cookie_or_oversized_request_headers():
    transport = FakeTransport(D7HttpWireResponse(200, (), b"ok"))
    adapter, evidence = _adapter(transport)

    with pytest.raises(HttpCapabilityError, match="d7_http_header_rejected"):
        adapter.execute(_request(headers=(("Cookie", "external=session"),)))
    with pytest.raises(HttpCapabilityError, match="d7_http_headers_too_many"):
        adapter.execute(_request(headers=tuple((f"X-{i}", "v") for i in range(101))))

    assert transport.calls == []
    assert evidence.saved == []


def test_response_evidence_lookup_is_idempotent_taskrun_scoped_and_integrity_checked(
    tmp_path,
):
    task_run_id = str(uuid4())
    operation_id = str(uuid4())
    store = D7HttpResponseEvidenceStore(
        tmp_path,
        task_run_id,
        protection=FakeProtection(),
    )
    body = b"protected response body"

    first = store.save(operation_id, body)
    assert store.save(operation_id, body) == first
    assert store.find_by_operation(operation_id) == first
    assert store.read(first.evidence_id) == body
    with pytest.raises(HttpCapabilityError, match="operation_conflict"):
        store.save(operation_id, b"different body")

    cipher = store.directory / f"{first.evidence_id}.cipher"
    cipher.write_bytes(cipher.read_bytes() + b"tamper")
    with pytest.raises(HttpCapabilityError, match="integrity_failed"):
        store.find_by_operation(operation_id)
    with pytest.raises(HttpCapabilityError, match="integrity_failed"):
        store.read(first.evidence_id)


def test_duplicate_evidence_operation_records_are_a_conflict(tmp_path):
    task_run_id = str(uuid4())
    operation_id = str(uuid4())
    store = D7HttpResponseEvidenceStore(
        tmp_path,
        task_run_id,
        protection=FakeProtection(),
    )
    metadata = store.save(operation_id, b"body")
    duplicate_id = str(uuid4())
    duplicate = asdict(metadata)
    duplicate["evidence_id"] = duplicate_id
    (store.directory / f"{duplicate_id}.cipher").write_bytes(
        (store.directory / f"{metadata.evidence_id}.cipher").read_bytes()
    )
    (store.directory / f"{duplicate_id}.meta.json").write_text(
        json.dumps(duplicate), encoding="utf-8"
    )

    with pytest.raises(HttpCapabilityError, match="operation_conflict"):
        store.find_by_operation(operation_id)


@pytest.mark.parametrize("suffix", [".cipher", ".meta.json"])
def test_response_evidence_sharing_retry_publishes_same_bytes_without_http_replay(tmp_path, monkeypatch, caplog, suffix):
    from agent_adapters.web import http_adapter
    caplog.set_level("INFO", logger="qinyuan")
    task, operation = str(uuid4()), str(uuid4())
    store = D7HttpResponseEvidenceStore(tmp_path, task, protection=FakeProtection())
    transport = FakeTransport(D7HttpWireResponse(200, (), b"observed"))
    adapter = D7HttpAdapter(task, "https://example.test:443", store, transport=transport)
    link, attempts, delays = http_adapter.os.link, [], []
    def sharing(source, destination):
        if str(destination).endswith(suffix):
            attempts.append((str(source), str(destination), Path(source).read_bytes()))
            if len(attempts) <= 3:
                error = PermissionError(13, "private sharing detail")
                error.winerror = 32
                raise error
        return link(source, destination)
    monkeypatch.setattr(http_adapter.os, "link", sharing)
    monkeypatch.setattr(http_adapter.time, "sleep", delays.append)
    response = adapter.execute(_request(operation_id=operation))
    assert len(transport.calls) == 1 and len(attempts) == 4 and len(set(attempts)) == 1
    assert delays == [0.05, 0.1, 0.2]
    assert store.read(response.body_evidence_id) == b"observed"
    assert store.find_by_operation(operation).evidence_id == response.body_evidence_id
    assert not list(store.directory.glob(".d7-http-pending-*"))
    events = [r.qy for r in caplog.records if getattr(r, "qy", {}).get("event") == "http.evidence_publish_recovered"]
    assert len(events) == 1 and events[0]["attempts"] == 4 and events[0]["run"] == task


@pytest.mark.parametrize("winerror,attempt_count", [(32, 4), (5, 1), (33, 1), (None, 1)])
def test_response_evidence_publication_failure_is_bounded_and_not_replayed(tmp_path, monkeypatch, winerror, attempt_count):
    from agent_adapters.web import http_adapter
    from agent_adapters.output_evidence import safe_failure_details
    task = str(uuid4())
    store = D7HttpResponseEvidenceStore(tmp_path, task, protection=FakeProtection())
    transport = FakeTransport(D7HttpWireResponse(200, (), b"private body"))
    adapter = D7HttpAdapter(task, "https://example.test:443", store, transport=transport)
    attempts, delays = [], []
    def blocked(source, destination):
        attempts.append((str(source), Path(source).read_bytes()))
        error = PermissionError(13, "private file path")
        if winerror is not None: error.winerror = winerror
        raise error
    monkeypatch.setattr(http_adapter.os, "link", blocked)
    monkeypatch.setattr(http_adapter.time, "sleep", delays.append)
    with pytest.raises(HttpCapabilityError, match="d7_http_evidence_write_failed") as caught:
        adapter.execute(_request())
    assert caught.value.context["phase"] == "response_evidence_save"
    assert len(transport.calls) == 1 and len(attempts) == attempt_count and len(set(attempts)) == 1
    assert delays == ([0.05, 0.1, 0.2] if winerror == 32 else [])
    assert not list(store.directory.glob("*.cipher")) and not list(store.directory.glob("*.meta.json"))
    assert not list(store.directory.glob(".d7-http-pending-*"))
    details = safe_failure_details(caught.value, include_chain=True)
    assert "d7_http_evidence_write_failed" in details["error_codes"] and "private" not in json.dumps(details)


def test_response_evidence_retry_never_overwrites_racing_destination(tmp_path, monkeypatch):
    from agent_adapters.web import http_adapter
    store = D7HttpResponseEvidenceStore(tmp_path, str(uuid4()), protection=FakeProtection())
    attempts, delays = [], []
    def race(source, destination):
        attempts.append(destination)
        if len(attempts) == 1:
            error = PermissionError(13, "sharing")
            error.winerror = 32
            raise error
        Path(destination).write_bytes(b"other writer")
        raise FileExistsError("already published")
    monkeypatch.setattr(http_adapter.os, "link", race)
    monkeypatch.setattr(http_adapter.time, "sleep", delays.append)
    with pytest.raises(HttpCapabilityError, match="d7_http_evidence_conflict"):
        store.save(str(uuid4()), b"observed")
    assert len(attempts) == 2 and delays == [0.05]
    assert Path(attempts[-1]).read_bytes() == b"other writer"
    assert not list(store.directory.glob(".d7-http-pending-*"))
