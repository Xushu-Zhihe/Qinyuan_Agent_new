from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

import pytest

from agent_adapters.d7_http_adapter import (
    D7HttpAdapter,
    D7HttpError,
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
    with pytest.raises(D7HttpError) as error:
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

    with pytest.raises(D7HttpError):
        adapter.execute(_request(path="/login"))

    assert evidence.saved == []
    assert adapter._cookies.request_header("https://example.test:443/next") is None


def test_user_cannot_supply_cookie_or_oversized_request_headers():
    transport = FakeTransport(D7HttpWireResponse(200, (), b"ok"))
    adapter, evidence = _adapter(transport)

    with pytest.raises(D7HttpError, match="d7_http_header_rejected"):
        adapter.execute(_request(headers=(("Cookie", "external=session"),)))
    with pytest.raises(D7HttpError, match="d7_http_headers_too_many"):
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
    with pytest.raises(D7HttpError, match="operation_conflict"):
        store.save(operation_id, b"different body")

    cipher = store.directory / f"{first.evidence_id}.cipher"
    cipher.write_bytes(cipher.read_bytes() + b"tamper")
    with pytest.raises(D7HttpError, match="integrity_failed"):
        store.find_by_operation(operation_id)
    with pytest.raises(D7HttpError, match="integrity_failed"):
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

    with pytest.raises(D7HttpError, match="operation_conflict"):
        store.find_by_operation(operation_id)
