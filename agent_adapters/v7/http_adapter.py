"""TaskRun-bound, origin-restricted HTTP actions for D7 real-mode runs."""

from __future__ import annotations

import hashlib
import http.cookiejar
import json
import math
import os
import re
import tempfile
import time
import urllib.request
from dataclasses import asdict, dataclass
from email.utils import parsedate_to_datetime
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from threading import RLock
from typing import Protocol
from urllib.parse import urljoin, urlsplit
from uuid import UUID, uuid4

from agent_adapters.windows_evidence_protection import (
    WindowsEvidenceProtection,
    WindowsEvidenceProtectionError,
)
from agent_core.v7.web_contracts import (
    D7_WEB_MAX_HEADER_BYTES,
    D7_WEB_MAX_HEADER_COUNT,
    D7_WEB_MAX_HEADER_VALUE_BYTES,
    D7_WEB_MAX_MODEL_TEXT_BYTES,
    D7_WEB_MAX_REQUEST_BODY_BYTES,
    D7_WEB_MAX_RESPONSE_BODY_BYTES,
    D7_WEB_RESPONSE_HEADERS,
)
from agent_core.sqlite_control_store import SqliteControlStore

MAX_D7_HTTP_REQUEST_BYTES = D7_WEB_MAX_REQUEST_BODY_BYTES
MAX_D7_HTTP_RESPONSE_BYTES = D7_WEB_MAX_RESPONSE_BODY_BYTES
MAX_D7_HTTP_MODEL_TEXT_BYTES = D7_WEB_MAX_MODEL_TEXT_BYTES
_ALLOWED_METHODS = frozenset(
    {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
)
_FORBIDDEN_HEADERS = frozenset(
    {
        "authorization",
        "connection",
        "content-length",
        "cookie",
        "host",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


class D7HttpError(RuntimeError):
    """Fixed, non-sensitive D7 HTTP adapter error."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _uuid4(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise D7HttpError(f"{name}_invalid")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise D7HttpError(f"{name}_invalid") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise D7HttpError(f"{name}_invalid")
    return value


@dataclass(frozen=True, slots=True)
class D7HttpOrigin:
    scheme: str
    host: str
    port: int

    @classmethod
    def parse(cls, value: object) -> D7HttpOrigin:
        if not isinstance(value, str) or any(ord(char) < 32 for char in value):
            raise D7HttpError("d7_http_origin_invalid")
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError as exc:
            raise D7HttpError("d7_http_origin_invalid") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise D7HttpError("d7_http_origin_invalid")
        host = parsed.hostname.lower()
        resolved_port = (
            port if port is not None else (443 if parsed.scheme == "https" else 80)
        )
        if not 1 <= resolved_port <= 65535:
            raise D7HttpError("d7_http_origin_invalid")
        return cls(parsed.scheme, host, resolved_port)

    @property
    def authority(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        default = 443 if self.scheme == "https" else 80
        return host if self.port == default else f"{host}:{self.port}"

    @property
    def url(self) -> str:
        return f"{self.scheme}://{self.authority}"

    def matches_url(self, value: str) -> bool:
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError:
            return False
        actual_port = (
            port if port is not None else (443 if parsed.scheme == "https" else 80)
        )
        return (
            parsed.scheme == self.scheme
            and parsed.hostname is not None
            and parsed.hostname.lower() == self.host
            and actual_port == self.port
            and parsed.username is None
            and parsed.password is None
        )


@dataclass(frozen=True, slots=True)
class D7HttpRequest:
    operation_id: str
    method: str
    path_query: str
    headers: tuple[tuple[str, str], ...] = ()
    body: str = ""


@dataclass(frozen=True, slots=True)
class D7HttpWireResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


class D7HttpTransport(Protocol):
    def send(
        self,
        *,
        origin: D7HttpOrigin,
        method: str,
        path_query: str,
        headers: tuple[tuple[str, str], ...],
        body: bytes,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> D7HttpWireResponse: ...


class _Protection(Protocol):
    identity: str

    def protect(self, value: bytes, entropy: bytes) -> bytes: ...

    def unprotect(self, value: bytes, entropy: bytes) -> bytes: ...

    def restrict_directory(self, directory: Path) -> None: ...

    def verify_directory(self, directory: Path) -> None: ...


@dataclass(frozen=True, slots=True)
class D7HttpEvidenceMetadata:
    schema_version: int
    task_run_id: str
    evidence_id: str
    operation_id: str
    body_sha256: str
    cipher_sha256: str
    byte_size: int
    created_at: float

    def __post_init__(self) -> None:
        _uuid4(self.task_run_id, "task_run_id")
        _uuid4(self.evidence_id, "evidence_id")
        _uuid4(self.operation_id, "operation_id")
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or not _is_sha256(self.body_sha256)
            or not _is_sha256(self.cipher_sha256)
            or type(self.byte_size) is not int
            or not 0 <= self.byte_size <= MAX_D7_HTTP_RESPONSE_BYTES
            or isinstance(self.created_at, bool)
            or not isinstance(self.created_at, (int, float))
            or not math.isfinite(self.created_at)
        ):
            raise D7HttpError("d7_http_evidence_metadata_invalid")


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


class D7HttpResponseEvidenceStore:
    """Low-level current-user protected response storage."""

    def __init__(
        self,
        run_dir: Path,
        task_run_id: str,
        *,
        protection: _Protection | None = None,
        clock=time.time,
        create: bool = True,
    ) -> None:
        self.task_run_id = _uuid4(task_run_id, "task_run_id")
        self.run_dir = Path(run_dir)
        self.protection = protection or WindowsEvidenceProtection()
        self.clock = clock
        self.directory = self.run_dir / "d7-http-evidence"
        self._lock = RLock()
        try:
            if self.run_dir.is_symlink() or not self.run_dir.is_dir():
                raise D7HttpError("d7_http_evidence_run_directory_invalid")
            new = not self.directory.exists()
            if new and create:
                self.directory.mkdir()
                self.protection.restrict_directory(self.directory)
            if self.directory.is_symlink() or not self.directory.is_dir():
                raise D7HttpError("d7_http_evidence_directory_invalid")
            identity = getattr(self.protection, "identity", None)
            if not isinstance(identity, str) or not identity.startswith("S-1-"):
                raise D7HttpError("d7_http_evidence_identity_invalid")
            self.protection.verify_directory(self.directory)
            owner_path = self.directory / "owner.json"
            owner = {
                "schema_version": 1,
                "task_run_id": self.task_run_id,
                "identity": identity,
            }
            if new and create:
                self._write_exclusive(owner_path, self._encode(owner))
            if (
                owner_path.is_symlink()
                or json.loads(owner_path.read_text("utf-8")) != owner
            ):
                raise D7HttpError("d7_http_evidence_ownership_invalid")
        except WindowsEvidenceProtectionError as exc:
            raise D7HttpError("d7_http_evidence_protection_failed") from exc
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            if isinstance(exc, D7HttpError):
                raise
            raise D7HttpError("d7_http_evidence_store_invalid") from exc

    @staticmethod
    def _encode(value: object) -> bytes:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")

    @staticmethod
    def _write_exclusive(path: Path, payload: bytes) -> None:
        if path.exists() or path.is_symlink():
            raise D7HttpError("d7_http_evidence_conflict")
        descriptor, temporary = tempfile.mkstemp(
            prefix=".d7-http-pending-", dir=path.parent
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError as exc:
                raise D7HttpError("d7_http_evidence_conflict") from exc
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _verify_owner(self) -> None:
        owner_path = self.directory / "owner.json"
        expected = {
            "schema_version": 1,
            "task_run_id": self.task_run_id,
            "identity": self.protection.identity,
        }
        try:
            if (
                owner_path.is_symlink()
                or not owner_path.is_file()
                or owner_path.stat().st_size > 4096
            ):
                raise D7HttpError("d7_http_evidence_ownership_invalid")
            if json.loads(owner_path.read_text("utf-8")) != expected:
                raise D7HttpError("d7_http_evidence_ownership_invalid")
        except D7HttpError:
            raise
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise D7HttpError("d7_http_evidence_ownership_invalid") from exc

    def _metadata(self, evidence_id: str) -> D7HttpEvidenceMetadata:
        _uuid4(evidence_id, "evidence_id")
        path = self.directory / f"{evidence_id}.meta.json"
        fields = {
            "schema_version",
            "task_run_id",
            "evidence_id",
            "operation_id",
            "body_sha256",
            "cipher_sha256",
            "byte_size",
            "created_at",
        }
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 8192:
                raise D7HttpError("d7_http_evidence_not_found")
            value = json.loads(path.read_text("utf-8"))
            if not isinstance(value, dict) or set(value) != fields:
                raise D7HttpError("d7_http_evidence_metadata_invalid")
            metadata = D7HttpEvidenceMetadata(**value)
            if (
                metadata.task_run_id != self.task_run_id
                or metadata.evidence_id != evidence_id
            ):
                raise D7HttpError("d7_http_evidence_ownership_invalid")
            return metadata
        except D7HttpError:
            raise
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise D7HttpError("d7_http_evidence_metadata_invalid") from exc

    def _ciphertext(self, metadata: D7HttpEvidenceMetadata) -> bytes:
        path = self.directory / f"{metadata.evidence_id}.cipher"
        maximum = MAX_D7_HTTP_RESPONSE_BYTES * 2 + 16 * 1024
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
                raise D7HttpError("d7_http_evidence_integrity_failed")
            value = path.read_bytes()
        except D7HttpError:
            raise
        except OSError as exc:
            raise D7HttpError("d7_http_evidence_read_failed") from exc
        if hashlib.sha256(value).hexdigest() != metadata.cipher_sha256:
            raise D7HttpError("d7_http_evidence_integrity_failed")
        return value

    def find_by_operation(self, operation_id: str) -> D7HttpEvidenceMetadata | None:
        """Return the unique persisted response for one operation, without decrypting it."""
        _uuid4(operation_id, "operation_id")
        with self._lock:
            try:
                self.protection.verify_directory(self.directory)
                self._verify_owner()
                matches = []
                for path in self.directory.glob("*.meta.json"):
                    if path.is_symlink() or not path.is_file():
                        raise D7HttpError("d7_http_evidence_metadata_invalid")
                    evidence_id = path.name[: -len(".meta.json")]
                    metadata = self._metadata(evidence_id)
                    if metadata.operation_id == operation_id:
                        self._ciphertext(metadata)
                        matches.append(metadata)
                if len(matches) > 1:
                    raise D7HttpError("d7_http_evidence_operation_conflict")
                return matches[0] if matches else None
            except WindowsEvidenceProtectionError as exc:
                raise D7HttpError("d7_http_evidence_protection_failed") from exc

    def read(self, evidence_id: str) -> bytes:
        """Decrypt one response after verifying TaskRun ownership and both hashes."""
        _uuid4(evidence_id, "evidence_id")
        with self._lock:
            try:
                self.protection.verify_directory(self.directory)
                self._verify_owner()
                metadata = self._metadata(evidence_id)
                ciphertext = self._ciphertext(metadata)
                entropy = hashlib.sha256(
                    f"qinyuan-d7-http:{self.task_run_id}:{evidence_id}".encode("ascii")
                ).digest()
                envelope = self.protection.unprotect(ciphertext, entropy)
                value = json.loads(envelope)
                if (
                    not isinstance(value, dict)
                    or set(value) != {"body_sha256", "body"}
                    or not isinstance(value["body_sha256"], str)
                    or not isinstance(value["body"], str)
                ):
                    raise D7HttpError("d7_http_evidence_integrity_failed")
                body = bytes.fromhex(value["body"])
                body_sha = hashlib.sha256(body).hexdigest()
                if (
                    len(body) != metadata.byte_size
                    or body_sha != metadata.body_sha256
                    or value["body_sha256"] != metadata.body_sha256
                ):
                    raise D7HttpError("d7_http_evidence_integrity_failed")
                return body
            except WindowsEvidenceProtectionError as exc:
                raise D7HttpError("d7_http_evidence_protection_failed") from exc
            except D7HttpError:
                raise
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                raise D7HttpError("d7_http_evidence_integrity_failed") from exc

    def save(self, operation_id: str, body: bytes) -> D7HttpEvidenceMetadata:
        _uuid4(operation_id, "operation_id")
        if not isinstance(body, bytes) or len(body) > MAX_D7_HTTP_RESPONSE_BYTES:
            raise D7HttpError("d7_http_evidence_body_invalid")
        evidence_id = str(uuid4())
        body_sha = hashlib.sha256(body).hexdigest()
        envelope = self._encode({"body_sha256": body_sha, "body": body.hex()})
        entropy = hashlib.sha256(
            f"qinyuan-d7-http:{self.task_run_id}:{evidence_id}".encode("ascii")
        ).digest()
        with self._lock:
            try:
                self.protection.verify_directory(self.directory)
                self._verify_owner()
                existing = self.find_by_operation(operation_id)
                if existing is not None:
                    if existing.body_sha256 == body_sha and existing.byte_size == len(
                        body
                    ):
                        return existing
                    raise D7HttpError("d7_http_evidence_operation_conflict")
                ciphertext = self.protection.protect(envelope, entropy)
                metadata = D7HttpEvidenceMetadata(
                    1,
                    self.task_run_id,
                    evidence_id,
                    operation_id,
                    body_sha,
                    hashlib.sha256(ciphertext).hexdigest(),
                    len(body),
                    float(self.clock()),
                )
                self._write_exclusive(
                    self.directory / f"{evidence_id}.cipher", ciphertext
                )
                try:
                    self._write_exclusive(
                        self.directory / f"{evidence_id}.meta.json",
                        self._encode(asdict(metadata)),
                    )
                except (OSError, ValueError, TypeError, D7HttpError):
                    (self.directory / f"{evidence_id}.cipher").unlink(missing_ok=True)
                    raise
                return metadata
            except WindowsEvidenceProtectionError as exc:
                raise D7HttpError("d7_http_evidence_protection_failed") from exc
            except D7HttpError:
                raise
            except OSError as exc:
                raise D7HttpError("d7_http_evidence_write_failed") from exc


class D7HttpEvidenceWriter(Protocol):
    task_run_id: str

    def save(self, operation_id: str, body: bytes) -> D7HttpEvidenceMetadata: ...


class D7HttpEvidenceReader(Protocol):
    """TaskRun-scoped application port; ordinary projections must not expose bodies."""

    task_run_id: str

    def find_by_operation(self, operation_id: str) -> D7HttpEvidenceMetadata | None: ...

    def read(self, evidence_id: str) -> bytes: ...


def _validate_owned_run(run_dir: Path, task_run_id: str) -> None:
    database = run_dir / "control.sqlite"
    if (
        run_dir.is_symlink()
        or not run_dir.is_dir()
        or database.is_symlink()
        or not database.is_file()
    ):
        raise D7HttpError("d7_http_evidence_ownership_invalid")
    try:
        with SqliteControlStore.open_readonly(database) as ledger:
            if (
                ledger.list_task_run_refs() != (task_run_id,)
                or ledger.load_task_run(task_run_id) is None
            ):
                raise D7HttpError("d7_http_evidence_ownership_invalid")
    except D7HttpError:
        raise
    except Exception as exc:
        raise D7HttpError("d7_http_evidence_ownership_invalid") from exc


class D7HttpResponseEvidenceService:
    """Production evidence facade bound to one persisted TaskRun and DPAPI."""

    def __init__(self, run_dir: Path, task_run_id: str, *, create: bool = True) -> None:
        self.task_run_id = _uuid4(task_run_id, "task_run_id")
        self.run_dir = Path(run_dir)
        _validate_owned_run(self.run_dir, self.task_run_id)
        self.store = D7HttpResponseEvidenceStore(
            self.run_dir,
            self.task_run_id,
            protection=WindowsEvidenceProtection(),
            create=create,
        )

    def save(self, operation_id: str, body: bytes) -> D7HttpEvidenceMetadata:
        _validate_owned_run(self.run_dir, self.task_run_id)
        return self.store.save(operation_id, body)

    def find_by_operation(self, operation_id: str) -> D7HttpEvidenceMetadata | None:
        _validate_owned_run(self.run_dir, self.task_run_id)
        return self.store.find_by_operation(operation_id)

    def read(self, evidence_id: str) -> bytes:
        _validate_owned_run(self.run_dir, self.task_run_id)
        return self.store.read(evidence_id)


@dataclass(frozen=True, slots=True)
class D7HttpResult:
    operation_id: str
    status: int
    body_evidence_id: str
    body_sha256: str
    body_bytes: int
    body_text: str
    body_text_truncated: bool
    content_type: str | None
    redirect_path_query: str | None
    cross_origin_redirect_blocked: bool


@dataclass(frozen=True, slots=True)
class D7HttpPolicy:
    """Fixed D7 method, origin, header, and body limits for one TaskRun."""

    origin: D7HttpOrigin

    @classmethod
    def for_origin(cls, value: str) -> D7HttpPolicy:
        return cls(D7HttpOrigin.parse(value))

    def validate(
        self, request: D7HttpRequest
    ) -> tuple[str, str, tuple[tuple[str, str], ...], bytes]:
        if not isinstance(request, D7HttpRequest):
            raise D7HttpError("d7_http_request_invalid")
        _uuid4(request.operation_id, "operation_id")
        if not isinstance(request.method, str):
            raise D7HttpError("d7_http_method_invalid")
        method = request.method.upper()
        if method not in _ALLOWED_METHODS:
            raise D7HttpError("d7_http_method_forbidden")
        path_query = _valid_path_query(request.path_query)
        if not isinstance(request.body, str):
            raise D7HttpError("d7_http_body_must_be_utf8_text")
        try:
            body = request.body.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise D7HttpError("d7_http_body_must_be_utf8_text") from exc
        if len(body) > MAX_D7_HTTP_REQUEST_BYTES:
            raise D7HttpError("d7_http_request_too_large")
        return method, path_query, _validated_headers(request.headers), body


class _TaskRunCookieJar:
    """In-memory cookies scoped to the adapter's one immutable HTTP origin."""

    def __init__(self, origin: D7HttpOrigin) -> None:
        self.origin = origin
        self._jar = http.cookiejar.CookieJar()

    def request_header(self, url: str) -> str | None:
        request = urllib.request.Request(url)
        self._jar.add_cookie_header(request)
        return request.get_header("Cookie")

    def accept(self, headers: tuple[tuple[str, str], ...], request_path: str) -> None:
        for raw in _header_values(headers, "set-cookie"):
            parsed = SimpleCookie()
            try:
                parsed.load(raw)
            except (CookieError, ValueError):
                continue
            for name, morsel in parsed.items():
                domain_attr = morsel["domain"].strip().lower().lstrip(".")
                if domain_attr and domain_attr != self.origin.host:
                    continue
                path_attr = morsel["path"]
                path = (
                    path_attr
                    if path_attr.startswith("/")
                    else _default_cookie_path(urlsplit(request_path).path)
                )
                expires = None
                if morsel["max-age"]:
                    try:
                        expires = int(time.time()) + int(morsel["max-age"])
                    except (TypeError, ValueError):
                        continue
                elif morsel["expires"]:
                    try:
                        expires = int(
                            parsedate_to_datetime(morsel["expires"]).timestamp()
                        )
                    except (TypeError, ValueError, OverflowError):
                        continue
                cookie = http.cookiejar.Cookie(
                    version=0,
                    name=name,
                    value=morsel.value,
                    port=None,
                    port_specified=False,
                    domain=self.origin.host,
                    domain_specified=bool(domain_attr),
                    domain_initial_dot=bool(morsel["domain"].startswith(".")),
                    path=path,
                    path_specified=bool(path_attr),
                    secure=bool(morsel["secure"]),
                    expires=expires,
                    discard=expires is None,
                    comment=None,
                    comment_url=None,
                    rest={"HttpOnly": morsel["httponly"]},
                    rfc2109=False,
                )
                self._jar.set_cookie(cookie)

    def clear(self) -> None:
        self._jar.clear()


def _valid_path_query(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise D7HttpError("d7_http_path_invalid")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise D7HttpError("d7_http_path_invalid") from exc
    if parsed.scheme or parsed.netloc or parsed.fragment:
        raise D7HttpError("d7_http_path_invalid")
    return value


def _validated_headers(headers: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(headers, (tuple, list)):
        raise D7HttpError("d7_http_headers_invalid")
    if len(headers) > D7_WEB_MAX_HEADER_COUNT:
        raise D7HttpError("d7_http_headers_too_many")
    output: list[tuple[str, str]] = []
    seen: set[str] = set()
    total_bytes = 0
    for item in headers:
        if (
            not isinstance(item, (tuple, list))
            or len(item) != 2
            or not isinstance(item[0], str)
            or not isinstance(item[1], str)
        ):
            raise D7HttpError("d7_http_headers_invalid")
        name, value = item
        lower = name.lower()
        try:
            name_bytes = name.encode("ascii", errors="strict")
            value_bytes = value.encode("latin-1", errors="strict")
        except UnicodeEncodeError as exc:
            raise D7HttpError("d7_http_header_rejected") from exc
        if (
            not _TOKEN.fullmatch(name)
            or lower in _FORBIDDEN_HEADERS
            or lower in seen
            or any(
                (ord(char) < 32 and char != "\t") or ord(char) == 127 for char in value
            )
            or len(value_bytes) > D7_WEB_MAX_HEADER_VALUE_BYTES
            or lower == "accept-encoding"
            and value.strip().lower() != "identity"
        ):
            raise D7HttpError("d7_http_header_rejected")
        seen.add(lower)
        total_bytes += len(name_bytes) + len(value_bytes) + 4
        output.append((name, value))
    if total_bytes > D7_WEB_MAX_HEADER_BYTES:
        raise D7HttpError("d7_http_headers_too_large")
    return tuple(output)


def _validated_wire_headers(
    headers: object,
) -> tuple[tuple[str, str], ...]:
    if not isinstance(headers, (tuple, list)) or len(headers) > D7_WEB_MAX_HEADER_COUNT:
        raise D7HttpError("d7_http_headers_invalid")
    output: list[tuple[str, str]] = []
    seen: set[str] = set()
    total_bytes = 0
    for item in headers:
        if (
            not isinstance(item, (tuple, list))
            or len(item) != 2
            or not isinstance(item[0], str)
            or not isinstance(item[1], str)
        ):
            raise D7HttpError("d7_http_headers_invalid")
        name, value = item
        lower = name.lower()
        try:
            name_bytes = name.encode("ascii", errors="strict")
            value_bytes = value.encode("latin-1", errors="strict")
        except UnicodeEncodeError as exc:
            raise D7HttpError("d7_http_header_rejected") from exc
        if (
            not _TOKEN.fullmatch(name)
            or lower in (_FORBIDDEN_HEADERS - {"cookie"})
            or lower in seen
            or any(
                (ord(char) < 32 and char != "\t") or ord(char) == 127 for char in value
            )
            or len(value_bytes) > D7_WEB_MAX_HEADER_VALUE_BYTES
            or lower == "accept-encoding"
            and value.strip().lower() != "identity"
        ):
            raise D7HttpError("d7_http_header_rejected")
        seen.add(lower)
        total_bytes += len(name_bytes) + len(value_bytes) + 4
        output.append((name, value))
    if total_bytes > D7_WEB_MAX_HEADER_BYTES:
        raise D7HttpError("d7_http_headers_too_large")
    return tuple(output)


def _validated_response_headers(
    headers: object,
) -> tuple[tuple[str, str], ...]:
    if not isinstance(headers, tuple) or len(headers) > D7_WEB_MAX_HEADER_COUNT:
        raise D7HttpError("d7_http_response_headers_invalid")
    output: list[tuple[str, str]] = []
    seen_singletons: set[str] = set()
    total_bytes = 0
    for pair in headers:
        if (
            not isinstance(pair, tuple)
            or len(pair) != 2
            or not isinstance(pair[0], str)
            or not isinstance(pair[1], str)
        ):
            raise D7HttpError("d7_http_response_headers_invalid")
        name, value = pair
        lower = name.lower()
        try:
            name_bytes = name.encode("ascii", errors="strict")
            value_bytes = value.encode("latin-1", errors="strict")
        except UnicodeEncodeError as exc:
            raise D7HttpError("d7_http_response_header_invalid") from exc
        if (
            not _TOKEN.fullmatch(name)
            or lower not in D7_WEB_RESPONSE_HEADERS
            or any(
                (ord(char) < 32 and char != "\t") or ord(char) == 127 for char in value
            )
            or len(value_bytes) > D7_WEB_MAX_HEADER_VALUE_BYTES
            or lower != "set-cookie"
            and lower in seen_singletons
        ):
            raise D7HttpError("d7_http_response_header_invalid")
        if lower != "set-cookie":
            seen_singletons.add(lower)
        total_bytes += len(name_bytes) + len(value_bytes) + 4
        output.append((lower, value))
    if total_bytes > D7_WEB_MAX_HEADER_BYTES:
        raise D7HttpError("d7_http_response_headers_too_large")
    return tuple(output)


def _header_values(headers: tuple[tuple[str, str], ...], name: str) -> tuple[str, ...]:
    lower = name.lower()
    return tuple(value for key, value in headers if key.lower() == lower)


def _default_cookie_path(path: str) -> str:
    if not path.startswith("/") or path.count("/") <= 1:
        return "/"
    return path.rsplit("/", 1)[0] or "/"


class D7HttpAdapter:
    """One TaskRun's in-memory cookie session and origin-bound HTTP action."""

    def __init__(
        self,
        task_run_id: str,
        authorized_origin: str,
        evidence_store: D7HttpEvidenceWriter,
        *,
        transport: D7HttpTransport,
        timeout_seconds: float = 15.0,
    ) -> None:
        self.task_run_id = _uuid4(task_run_id, "task_run_id")
        self.policy = D7HttpPolicy.for_origin(authorized_origin)
        self.origin = self.policy.origin
        if evidence_store.task_run_id != self.task_run_id:
            raise D7HttpError("d7_http_evidence_ownership_invalid")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise D7HttpError("d7_http_timeout_invalid")
        self.evidence_store = evidence_store
        if transport is None or not callable(getattr(transport, "send", None)):
            raise D7HttpError("d7_http_transport_required")
        self.transport = transport
        self.timeout_seconds = float(timeout_seconds)
        self._cookies = _TaskRunCookieJar(self.origin)
        self._lock = RLock()
        self._closed = False

    @staticmethod
    def _content_type(headers: tuple[tuple[str, str], ...]) -> str | None:
        values = _header_values(headers, "content-type")
        if not values:
            return None
        value = values[0]
        if len(value) > 256 or any(ord(char) < 32 or ord(char) > 126 for char in value):
            return None
        return value

    def _redirect_location(
        self, headers: tuple[tuple[str, str], ...], request_url: str
    ) -> tuple[str | None, bool]:
        locations = _header_values(headers, "location")
        if not locations:
            return None, False
        location = locations[0]
        if any(ord(char) < 32 or ord(char) == 127 for char in location):
            return None, True
        resolved = urljoin(request_url, location)
        if not self.origin.matches_url(resolved):
            return None, True
        parsed = urlsplit(resolved)
        path_query = parsed.path or "/"
        if parsed.query:
            path_query += "?" + parsed.query
        try:
            return _valid_path_query(path_query), False
        except D7HttpError:
            return None, True

    def execute(self, request: D7HttpRequest) -> D7HttpResult:
        method, path_query, headers, body = self.policy.validate(request)
        operation_id = request.operation_id
        request_url = self.origin.url + path_query
        with self._lock:
            if self._closed:
                raise D7HttpError("d7_http_session_closed")
            cookie = self._cookies.request_header(request_url)
            wire_headers = list(headers)
            if not any(name.lower() == "accept-encoding" for name, _ in wire_headers):
                wire_headers.append(("Accept-Encoding", "identity"))
            if cookie:
                wire_headers.append(("Cookie", cookie))
            validated_wire_headers = _validated_wire_headers(wire_headers)
            try:
                response = self.transport.send(
                    origin=self.origin,
                    method=method,
                    path_query=path_query,
                    headers=validated_wire_headers,
                    body=body,
                    timeout_seconds=self.timeout_seconds,
                    max_response_bytes=MAX_D7_HTTP_RESPONSE_BYTES,
                )
            except D7HttpError:
                raise
            except Exception as exc:
                raise D7HttpError("d7_http_transport_failed") from exc
            if not isinstance(response, D7HttpWireResponse):
                raise D7HttpError("d7_http_transport_response_invalid")
            if (
                type(response.status) is not int
                or not 100 <= response.status <= 599
                or not isinstance(response.body, bytes)
            ):
                raise D7HttpError("d7_http_response_invalid")
            if len(response.body) > MAX_D7_HTTP_RESPONSE_BYTES:
                raise D7HttpError("d7_http_response_too_large")
            response_headers = _validated_response_headers(response.headers)
            self._cookies.accept(response_headers, path_query)
            metadata = self.evidence_store.save(operation_id, response.body)
            location, blocked = self._redirect_location(response_headers, request_url)
            text_bytes = response.body[:MAX_D7_HTTP_MODEL_TEXT_BYTES]
            body_text = text_bytes.decode("utf-8", errors="ignore")
            return D7HttpResult(
                operation_id=operation_id,
                status=response.status,
                body_evidence_id=metadata.evidence_id,
                body_sha256=metadata.body_sha256,
                body_bytes=len(response.body),
                body_text=body_text,
                body_text_truncated=len(response.body) > len(text_bytes),
                content_type=self._content_type(response_headers),
                redirect_path_query=location,
                cross_origin_redirect_blocked=blocked,
            )

    def close(self) -> None:
        """Discard this TaskRun's in-memory cookies at lifecycle completion."""
        with self._lock:
            self._cookies.clear()
            self._closed = True
