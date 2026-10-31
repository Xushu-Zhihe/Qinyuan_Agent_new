"""Competition profiles, observed question imports and fixed platform requests."""

import http.client
import hashlib
import json
import socket
import ssl
import tempfile
import time
from contextlib import contextmanager
from urllib.parse import urlsplit, urlencode, urljoin
from uuid import UUID, uuid4

from agent_adapters.workbench_contracts import WorkbenchError
from agent_core.web_contracts import D7_WEB_DEFAULT_METHODS
from qinyuan_core import get_logger

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
READ_RETRY_DELAYS = (0.1, 0.3)
READ_PATHS = {"arena": "/api/skill/agent/arena/current/", "benchmark": "/openapi/v1/challenges"}
# Actions each platform protocol really defines. Ending a round must never assume a
# capability the protocol does not have (for example benchmark has no abandon).
PLATFORM_ACTIONS = {"arena": frozenset({"next", "abandon"}), "benchmark": frozenset({"start", "close"})}


def supports_action(adapter, action):
    return action in PLATFORM_ACTIONS.get(adapter, frozenset())


def _origin(value):
    try:
        parsed = urlsplit(value)
        if (not isinstance(value, str) or len(value) > 2048 or any(c.isspace() or ord(c) < 32 for c in value)
                or parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or parsed.port is not None and not 1 <= parsed.port <= 65535):
            raise ValueError()
        return value.rstrip("/")
    except (ValueError, TypeError, AttributeError):
        raise WorkbenchError("competition_origin_invalid") from None


def _target_origin(value):
    """Canonical HTTP(S) origin, or the exact TCP endpoint for ``tcp://``.

    Arena Pwn questions are raw TCP services; HTTP(S) questions keep the
    standard-port origin behavior. Both forms are normalized so a stored
    challenge endpoint and a fresh platform address compare equal.
    """
    parsed = urlsplit(value)
    if parsed.scheme == "tcp":
        from agent_core.target_contracts import canonical_tcp_endpoint
        try:
            return canonical_tcp_endpoint(value)
        except (TypeError, ValueError):
            raise ValueError("competition_target_protocol_unsupported") from None
    from agent_core.web_contracts import canonical_web_origin
    if parsed.scheme not in {"http","https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ValueError("competition_target_protocol_unsupported")
    host=parsed.hostname
    host=f"[{host}]" if ":" in host else host
    port=parsed.port if parsed.port is not None else (80 if parsed.scheme=="http" else 443)
    return canonical_web_origin(f"{parsed.scheme}://{host}:{port}")


def _matches_target(raw, target):
    if "://" not in raw:return urlsplit(target).netloc==raw
    try:return _target_origin(raw)==target
    except (TypeError,ValueError):return False


def _integer(value, *, minimum=0):
    if type(value) is not int or value < minimum:raise ValueError()
    return value


def _text(value):
    if not isinstance(value, str):raise ValueError()
    return value


def _addresses(value):
    if not isinstance(value, list):raise ValueError()
    return [_text(item) for item in value]


def _preview(adapter, value):
    if adapter == "arena":
        if type(value.get("code")) is not int or value["code"] != 200:raise ValueError()
        data = value["data"]
        agent = data["agent"]
        ratings = {k: v for k, v in agent.items() if k in {"rating", "attempt_count", "solved_count", "failed_count"} and type(v) is int}
        attempt = data["attempt"]
        if attempt is None:return {"items": [], "agent": ratings}
        identity = _integer(attempt["id"], minimum=1)
        problem = attempt["problem"]
        container = problem.get("container")
        annex = problem.get("annex")
        if annex is not None and not isinstance(annex, dict):raise ValueError()
        item = {"remote_ref": identity, "display_name": f"Arena attempt #{identity}",
            "state": _text(attempt["state_label"]), "description": _text(problem.get("content") or ""),
            "category": _text(problem.get("type_label") or ""),
            "addresses": _addresses(container["url"]) if container is not None else [],
            "remaining_seconds": _integer(attempt["remaining_seconds"]) if "remaining_seconds" in attempt else None,
            "wrong_count": _integer(attempt["wrong_count"]) if "wrong_count" in attempt else None,
            "max_wrong_count": _integer(attempt["max_wrong_count"]) if "max_wrong_count" in attempt else None,
            "attachment": {k: annex[k] for k in ("name", "size", "url") if k in annex} if annex else None}
        return {"items": [item], "agent": ratings}
    if not isinstance(value, list):raise ValueError()
    items = []
    for entry in value:
        total, correct = _integer(entry["flag_count"]), _integer(entry["correct_flag_count"])
        completed, status = entry["is_completed"], _text(entry["container_status"])
        if (type(completed) is not bool or correct > total or not entry["unique_code"]
                or status not in {"pending", "available", "stop_pending", "stopped"}):raise ValueError()
        items.append({"remote_ref": _text(entry["unique_code"]), "display_name": entry["unique_code"],
            "state": "completed" if completed else status, "description": _text(entry.get("description") or ""),
            "addresses": _addresses(entry["container_addr"]), "flag_count": total,
            "correct_flag_count": correct, "is_completed": completed,
            "difficulty": _text(entry.get("difficulty") or ""), "remaining_seconds": None})
    return {"items": items, "agent": None}


def _redact(value, credential):
    if isinstance(value, str):return value.replace(credential, "[REDACTED]")
    if isinstance(value, list):return [_redact(item, credential) for item in value]
    if isinstance(value, dict):return {key: _redact(item, credential) for key, item in value.items()}
    return value


def _exchange(profile, credential, method, path, payload=None, *, empty_ok=False, operation="request"):
    parsed = urlsplit(profile["base_url"])
    connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(parsed.hostname, parsed.port, timeout=profile["timeout_seconds"])
    header = _credential_headers(profile, credential)
    body = None if payload is None else json.dumps(payload,ensure_ascii=True,separators=(",",":")).encode()
    try:
        connection.request(method,path,body=body,headers={"Accept":"application/json",**header,
            **({"Content-Type":"application/json"} if body is not None else {})})
        response=connection.getresponse(); status=response.status
        def http_error():
            error=WorkbenchError("competition_http_error",502);error.http_status=status;return error
        if 300<=status<400:raise WorkbenchError("competition_redirect_blocked",502)
        if status in {401,403}:raise WorkbenchError("competition_auth_failed",502)
        errors={429:"competition_rate_limited",422:"competition_request_rejected",503:"competition_resource_unavailable"}
        if status in errors:raise WorkbenchError(errors[status],502)
        if status>=500:raise http_error()
        raw=response.read(MAX_RESPONSE_BYTES+1)
        if len(raw)>MAX_RESPONSE_BYTES:raise WorkbenchError("competition_response_too_large",502)
        if empty_ok and status in {200,204} and not raw.strip():return {}
        try:value=json.loads(raw)
        except (ValueError,UnicodeError,RecursionError):
            _log_response_invalid(operation=operation,path=path,response=response,raw=raw,reason="json")
            error=WorkbenchError("competition_response_invalid",502)
            error.http_status=status;error.content_type=_content_type(response)
            raise error from None
        code=value.get("code") if isinstance(value,dict) else None
        if ((profile["adapter"]=="benchmark" and code=="task_not_found")
            or (profile["adapter"]=="arena" and type(code) is int and code in {401,403})):
            raise WorkbenchError("competition_auth_failed",502)
        if code=="invalid_state":raise WorkbenchError("competition_invalid_state",502)
        if method=="POST" and profile["adapter"]=="benchmark" and status==409 and code=="duplicate":
            return {"duplicate":True}
        if status!=200:raise http_error()
        if profile["adapter"]=="arena":
            if type(code) is not int:
                _log_response_invalid(operation=operation,path=path,response=response,raw=raw,reason="arena_code")
                error=WorkbenchError("competition_response_invalid",502)
                error.http_status=status;error.content_type=_content_type(response)
                raise error
            if code!=200:
                # A business refusal is a definite platform answer; keep only the
                # numeric code (never the message) so operators can see why.
                error=WorkbenchError("competition_business_error",502)
                error.platform_code=code
                raise error
        return value
    except (TimeoutError,socket.timeout):raise WorkbenchError("competition_timeout",502) from None
    except ssl.SSLError:raise WorkbenchError("competition_tls_failed",502) from None
    except (OSError,http.client.HTTPException):raise WorkbenchError("competition_transport_failed",502) from None
    finally:connection.close()


def _content_type(response):
    try:return response.getheader("Content-Type") if response is not None else None
    except (AttributeError, OSError):return None


def _log_response_invalid(*, operation, path, response=None, raw=None, reason):
    """Keep enough context to identify a bad platform answer, never its body."""
    get_logger("ctrl").warning("competition.response_invalid", category="platform_connection",
        operation=operation, path=path,
        http_status=getattr(response, "status", None) if response is not None else None,
        content_type=_content_type(response), response_bytes=len(raw or b""), reason=reason)


def _read_exchange(profile, credential, path, *, operation):
    """Only idempotent JSON reads retry; submissions must stay single-attempt."""
    for attempt in range(len(READ_RETRY_DELAYS)+1):
        try:return _exchange(profile,credential,"GET",path,operation=operation)
        except WorkbenchError as error:
            content_type=(getattr(error,"content_type",None) or "").lower()
            if (error.code!="competition_response_invalid" or not content_type.startswith("application/json")
                    or attempt==len(READ_RETRY_DELAYS)):raise
            get_logger("ctrl").info("competition.response_read_retry", category="platform_connection",
                operation=operation, path=path, attempt=attempt+1)
            time.sleep(READ_RETRY_DELAYS[attempt])


def _credential_headers(profile, credential):
    return {"Authorization": "Bearer " + credential} if profile["adapter"] == "arena" else {"BENCHMARK_TOKEN": credential}


@contextmanager
def _download_attachment(profile, credential, attachment, check):
    """Fetch only platform-observed bytes; cross-origin hops never receive credentials."""
    from agent_adapters.web.request_staging import MAX_V3_REQUEST_BYTES
    from Sandbox.tool_download_worker import public_addresses, MAX_REDIRECTS
    limit = MAX_V3_REQUEST_BYTES
    expected = attachment.get("size")
    if type(expected) is int and (expected < 0 or expected > limit):
        raise WorkbenchError("competition_attachment_too_large", 409)
    url = urljoin(profile["base_url"] + "/", attachment["url"])
    end = time.monotonic() + profile["timeout_seconds"]
    authenticated = True
    with tempfile.TemporaryFile() as output:
        try:
            for hop in range(MAX_REDIRECTS + 1):
                check()
                value = urlsplit(url)
                if (len(url) > 8192 or any(ord(c) <= 32 or ord(c) == 127 for c in url) or "\\" in url
                        or value.fragment or value.username is not None or value.password is not None):
                    raise WorkbenchError("competition_attachment_url_rejected", 409)
                try:
                    same_origin = _target_origin(url) == _target_origin(profile["base_url"])
                except ValueError:
                    raise WorkbenchError("competition_attachment_url_rejected", 409) from None
                authenticated = authenticated and same_origin
                remaining = end - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                connection_type = http.client.HTTPSConnection if value.scheme == "https" else http.client.HTTPConnection
                connection = connection_type(value.hostname, value.port, timeout=remaining)
                try:
                    if not same_origin:
                        try:
                            addresses = public_addresses(value.hostname, value.port or (443 if value.scheme == "https" else 80), set())
                        except ValueError:
                            raise WorkbenchError("competition_attachment_url_rejected", 409) from None
                        family, kind, protocol, _, address = addresses[0]
                        plain = socket.socket(family, kind, protocol)
                        connection.sock = plain
                        plain.settimeout(remaining)
                        plain.connect(address)
                        if value.scheme == "https":
                            connection.sock = ssl.create_default_context().wrap_socket(plain, server_hostname=value.hostname)
                    connection.request("GET", (value.path or "/") + ("?" + value.query if value.query else ""),
                        headers={"Accept-Encoding": "identity", **(_credential_headers(profile, credential) if authenticated else {})})
                    response = connection.getresponse()
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.getheader("Location")
                        if not location or hop == MAX_REDIRECTS:
                            raise WorkbenchError("competition_attachment_redirect_limit", 502)
                        next_url = urljoin(url, location)
                        if value.scheme == "https" and urlsplit(next_url).scheme != "https":
                            raise WorkbenchError("competition_attachment_url_rejected", 409)
                        url = next_url
                        continue
                    if response.status != 200:
                        raise WorkbenchError("competition_attachment_http_" + str(response.status), 502)
                    length = response.getheader("Content-Length")
                    if length is not None and (not length.isdecimal() or int(length) > limit):
                        raise WorkbenchError("competition_attachment_too_large", 502)
                    total, digest = 0, hashlib.sha256()
                    while True:
                        check()
                        if time.monotonic() >= end:
                            raise TimeoutError()
                        block = response.read(64 * 1024)
                        if not block:
                            break
                        total += len(block)
                        if total > limit:
                            raise WorkbenchError("competition_attachment_too_large", 502)
                        try:
                            output.write(block)
                        except OSError:
                            raise WorkbenchError("competition_attachment_store_failed", 500) from None
                        digest.update(block)
                    if length is not None and total != int(length) or type(expected) is int and total != expected:
                        raise WorkbenchError("competition_attachment_size_mismatch", 502)
                    break
                finally:
                    connection.close()
            check()
        except WorkbenchError:
            raise
        except (TimeoutError, socket.timeout):
            raise WorkbenchError("competition_attachment_timeout", 502) from None
        except ssl.SSLError:
            raise WorkbenchError("competition_attachment_tls_failed", 502) from None
        except ValueError:
            raise WorkbenchError("competition_attachment_url_rejected", 409) from None
        except (OSError, http.client.HTTPException):
            raise WorkbenchError("competition_attachment_transport_failed", 502) from None
        output.seek(0)
        yield output, total, digest.hexdigest()


def _read(profile,credential):
    path=READ_PATHS[profile["adapter"]]
    value=_read_exchange(profile,credential,path,operation="read")
    try:return _redact(_preview(profile["adapter"],value),credential)
    except (ValueError,KeyError,TypeError,AttributeError,RecursionError):
        _log_response_invalid(operation="read",path=path,reason="shape")
        raise WorkbenchError("competition_response_invalid",502) from None


def submit_candidate(profile,credential,remote_ref,flag):
    """Called only after a durable submission start; never returns raw response/body."""
    if profile["adapter"]=="arena":
        path=f"/api/skill/agent/arena/attempt/{_integer(remote_ref,minimum=1)}/submit/"
        payload={"flag":flag}
    else:
        if not isinstance(remote_ref,str) or not remote_ref or not 1<=len(flag)<=4096:
            raise WorkbenchError("competition_submission_invalid")
        path="/openapi/v1/challenges/submit";payload={"unique_code":remote_ref,"flag":flag}
    value=_exchange(profile,credential,"POST",path,payload,operation="submit")
    try:
        if profile["adapter"]=="arena":
            data=value["data"]; attempt=data["attempt"];correct=data["correct"]
            if type(correct) is not bool or _integer(attempt["id"],minimum=1)!=remote_ref:raise ValueError()
            state=_text(attempt["state_label"])
            if correct and state=="solved":return {"verdict":"accepted","platform_state":state}
            if not correct and state=="active":return {"verdict":"rejected","platform_state":state}
            if not correct and state in {"failed","expired","abandoned"}:
                return {"verdict":"unknown","code":"competition_question_ended","platform_state":state}
            raise ValueError()
        duplicate=value=={"duplicate":True}
        if duplicate:
            item=next((i for i in _read(profile,credential)["items"] if i["remote_ref"]==remote_ref),None)
            if item is None:raise ValueError()
            correct,total=item["correct_flag_count"],item["flag_count"]
            if correct<1:raise ValueError()
            completed=item["is_completed"]
            if completed!=(correct==total):raise ValueError()
            return {"verdict":"accepted" if completed else "partial","duplicate":True,
                    "correct_flag_count":correct,"total_flag_count":total}
        if type(value["correct"]) is not bool:raise ValueError()
        correct,total=_integer(value["correct_flag_count"]),_integer(value["total_flag_count"],minimum=1)
        if correct>total or value["correct"] and correct==0 or not value["correct"] and correct==total:raise ValueError()
        return {"verdict":("accepted" if correct==total else "partial") if value["correct"] else "rejected",
                "correct_flag_count":correct,"total_flag_count":total}
    except (ValueError,KeyError,TypeError,AttributeError):
        _log_response_invalid(operation="submit",path=path,reason="shape")
        raise WorkbenchError("competition_response_invalid",502) from None


def read_question(profile,credential,remote_ref=None):
    if profile["adapter"]=="benchmark":
        items=_read(profile,credential)["items"]
        return next((item for item in items if item["remote_ref"]==remote_ref),None) if remote_ref is not None else items
    path=READ_PATHS["arena"] if remote_ref is None else f"/api/skill/agent/arena/attempt/{_integer(remote_ref,minimum=1)}/"
    try:
        items=_redact(_preview("arena",_read_exchange(profile,credential,path,operation="read_question")),credential)["items"]
        item=items[0] if items else None
        if remote_ref is not None and (item is None or item["remote_ref"]!=remote_ref):raise ValueError()
        return item
    except WorkbenchError:raise
    except (ValueError,KeyError,TypeError):
        _log_response_invalid(operation="read_question",path=path,reason="shape")
        raise WorkbenchError("competition_response_invalid",502) from None


def platform_action(profile,credential,action,remote_ref):
    if not supports_action(profile["adapter"],action):
        raise WorkbenchError("competition_platform_action_unsupported")
    if profile["adapter"]=="arena" and action=="next":
        value=_exchange(profile,credential,"POST","/api/skill/agent/arena/next/",operation="next")
        try:
            if type(value["data"]["reused"]) is not bool:raise ValueError()
            items=_redact(_preview("arena",value),credential)["items"]
            if len(items)!=1 or items[0]["state"]!="active":raise ValueError()
            return {"item":items[0],"reused":value["data"]["reused"]}
        except (ValueError,KeyError,TypeError):raise WorkbenchError("competition_response_invalid",502) from None
    if profile["adapter"]=="arena" and action=="abandon":
        # Give the attempt back before the token/account moves on; otherwise the
        # platform keeps it open and refuses to hand out the next question.
        path=f"/api/skill/agent/arena/attempt/{_integer(remote_ref,minimum=1)}/abandon/"
        value=_exchange(profile,credential,"POST",path,empty_ok=True,operation="abandon")
        return {"abandoned":True,"platform_state":(value.get("data") or {}).get("attempt",{}).get("state_label")}
    if profile["adapter"]!="benchmark" or not isinstance(remote_ref,str) or not remote_ref:
        raise WorkbenchError("competition_platform_action_invalid")
    path=f"/openapi/v1/challenges/{action}?"+urlencode({"unique_code":remote_ref})
    value=_exchange(profile,credential,"POST",path,operation=action)
    try:
        if value["unique_code"]!=remote_ref:raise ValueError()
        if action=="start":return {"addresses":_addresses(value["container_addr"]),"owned":True}
        if value["closed"] is not True:raise ValueError()
        return {"closed":True}
    except (ValueError,KeyError,TypeError):raise WorkbenchError("competition_response_invalid",502) from None


class CompetitionConnections:
    def __init__(self, workbench):
        self.workbench = workbench
        self.credentials = {}  # Same process-memory lifecycle as workbench model keys.
        with workbench._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS competition_profiles (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, adapter TEXT NOT NULL,
                base_url TEXT NOT NULL, timeout_seconds INTEGER NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS competition_questions (
                challenge_id TEXT PRIMARY KEY REFERENCES challenges(id) ON DELETE CASCADE,
                profile_id TEXT NOT NULL REFERENCES competition_profiles(id),
                scope_sha256 TEXT NOT NULL, remote_ref TEXT NOT NULL, snapshot_json TEXT NOT NULL,
                UNIQUE(scope_sha256,remote_ref))""")

    def _get(self, identity):
        with self.workbench._db() as db:
            row = db.execute("SELECT * FROM competition_profiles WHERE id=?", (identity,)).fetchone()
        if row is None:raise WorkbenchError("competition_profile_not_found", 404)
        return dict(row)

    def _public(self, profile):
        return {**profile, "credential_configured": bool(self.credentials.get(profile["id"]))}

    def list(self):
        with self.workbench.lock, self.workbench._db() as db:
            rows = db.execute("SELECT * FROM competition_profiles ORDER BY rowid").fetchall()
            return [self._public(dict(row)) for row in rows]

    def save(self, value, identity=None):
        fields = {"name", "adapter", "base_url", "timeout_seconds"}
        if (not isinstance(value, dict) or not fields <= value.keys() or value.keys() - fields - {"credential"}
                or not isinstance(value["name"], str) or not 1 <= len(value["name"].strip()) <= 80
                or not isinstance(value["adapter"], str) or value["adapter"] not in READ_PATHS or type(value["timeout_seconds"]) is not int
                or not 1 <= value["timeout_seconds"] <= 60):raise WorkbenchError("competition_profile_invalid")
        base = _origin(value["base_url"])
        credential = value.get("credential")
        if credential is not None:
            if (not isinstance(credential, str) or len(credential) > 8192
                    or any(c.isspace() or ord(c) < 32 or ord(c) > 126 for c in credential)):
                raise WorkbenchError("competition_credential_invalid")
            if credential and value["adapter"] == "benchmark":
                try:UUID(credential)
                except ValueError:raise WorkbenchError("competition_credential_invalid") from None
        with self.workbench.lock:
            previous = self._get(identity) if identity is not None else None
            identity = identity or str(uuid4())
            schedules=getattr(self.workbench,"schedules",None)
            if previous and schedules is not None and schedules.uses_profile(identity) and (previous["adapter"],previous["base_url"])!=(value["adapter"],base):
                raise WorkbenchError("competition_profile_in_use",409)
            profile = dict(id=identity, name=value["name"].strip(), adapter=value["adapter"],
                           base_url=base, timeout_seconds=value["timeout_seconds"])
            with self.workbench._db() as db:
                db.execute("INSERT OR REPLACE INTO competition_profiles VALUES (?,?,?,?,?)",
                    (identity, profile["name"], profile["adapter"], base, profile["timeout_seconds"]))
            if previous and (previous["adapter"], previous["base_url"]) != (profile["adapter"], base):
                self.credentials.pop(identity, None)
            if credential:self.credentials[identity] = credential
            return self._public(profile)

    def clear(self, identity):
        with self.workbench.lock:
            profile = self._get(identity)
            self.credentials.pop(identity, None)
            public=self._public(profile)
        schedules=getattr(self.workbench,"schedules",None)
        if schedules is not None:schedules.pause_profile(identity)
        return public

    def check(self, identity):
        with self.workbench.lock:
            profile = self._get(identity)
            credential = self.credentials.get(identity)
        if not credential:raise WorkbenchError("competition_credential_missing", 409)
        started = time.monotonic()
        try:
            preview = _read(profile, credential)
            with self.workbench.lock:
                if self._get(identity) != profile or self.credentials.get(identity) != credential:
                    raise WorkbenchError("competition_profile_changed", 409)
                public = self._public(profile)
        except WorkbenchError as error:
            get_logger("sys").warning("competition.connection_failed", profile=identity, adapter=profile["adapter"],
                category="platform_connection", code=error.code)
            raise
        get_logger("sys").info("competition.connected", profile=identity, adapter=profile["adapter"],
            category="platform_connection", elapsed_seconds=round(time.monotonic()-started, 3))
        return {"status": "connected", "profile": public, "preview": preview}


    def import_question(self,identity,value,*,check_active=None):
        """Import only platform-observed questions; platform actions remain separate."""
        if not isinstance(value,dict) or set(value)-{"remote_ref","endpoint","deadline_minutes","step_timeout_seconds","http_methods","default_description","default_description_enabled"} or "remote_ref" not in value:
            raise WorkbenchError("competition_import_invalid")
        if self.workbench.execution_profile != "d8_v22":raise WorkbenchError("competition_requires_contract22",409)
        observed_at=time.time()
        checked=self.check(identity);profile=checked["profile"]
        item=next((i for i in checked["preview"]["items"] if type(i["remote_ref"]) is type(value["remote_ref"]) and i["remote_ref"]==value["remote_ref"]),None)
        if item is None:raise WorkbenchError("competition_question_not_found",404)
        if item.get("is_completed") or profile["adapter"]=="arena" and item["state"]!="active":
            raise WorkbenchError("competition_question_not_active",409)
        target=value.get("endpoint","")
        if not isinstance(target,str):raise WorkbenchError("competition_import_invalid")
        if target:
            try:target=_target_origin(target)
            except (TypeError,ValueError):raise WorkbenchError("competition_target_protocol_unsupported") from None
            matches=[_matches_target(raw,target) for raw in item["addresses"]]
            if not any(matches):raise WorkbenchError("competition_target_not_in_question")
            from Sandbox.target_address import TargetAddress,TargetAddressError
            try:TargetAddress.from_url(target,allow_private_ipv4=True)
            except TargetAddressError as error:
                raise WorkbenchError("competition_target_address_not_supported" if error.code=="non_public_target" else "competition_target_protocol_unsupported",409) from None
        elif item["addresses"]:
            raise WorkbenchError("competition_target_selection_required")
        snapshot={"profile":{k:profile[k] for k in ("id","adapter","base_url","timeout_seconds")},
                  "question":item,"platform_deadline_at":observed_at+item["remaining_seconds"] if item["remaining_seconds"] is not None else None}
        from agent_core.turn_modeling import digest
        scope=digest({k:profile[k] for k in ("id","adapter","base_url")})
        remote=json.dumps(item["remote_ref"])
        with self.workbench.lock:
            # Recheck identity after the network read before recording the binding.
            current=self._get(identity)
            if any(current[k]!=profile[k] for k in current):raise WorkbenchError("competition_profile_changed",409)
            with self.workbench._db() as db:
                existing=db.execute("SELECT challenge_id FROM competition_questions WHERE scope_sha256=? AND remote_ref=?",(scope,remote)).fetchone()
            if existing is not None:
                try:local=self.workbench.get(existing["challenge_id"])
                except WorkbenchError as error:
                    if error.code!="challenge_not_found":raise
                    with self.workbench._db() as db:
                        db.execute("DELETE FROM competition_questions WHERE challenge_id=?",(existing["challenge_id"],))
                    existing=None
            if existing is None:
                category=item.get("category","").lower()
                from agent_adapters.workbench import VALID_CATEGORIES, merged_description
                if category not in VALID_CATEGORIES:category="misc"
                minutes=value.get("deadline_minutes",60)
                description=item["description"]
                selected=next((raw for raw,match in zip(item["addresses"],matches) if match),None) if target else None
                if selected:description += "\n\n平台所选入口："+selected
                if value.get("default_description_enabled"):
                    description=merged_description(description,value.get("default_description"))
                local=self.workbench.create(dict(title=item["display_name"][:160],category=category,
                    description=description,endpoint=target,deadline_minutes=minutes,
                    step_timeout_seconds=value.get("step_timeout_seconds",60),
                    http_methods=value.get("http_methods",list(D7_WEB_DEFAULT_METHODS))))
                with self.workbench._db() as db:
                    db.execute("INSERT INTO competition_questions VALUES (?,?,?,?,?)",(local["id"],identity,scope,remote,json.dumps(snapshot,ensure_ascii=True)))
            if target and local["status"]=="draft" and not local["archived"] and local["endpoint"]!=target:
                local=self.workbench.update(local["id"],dict(title=local["title"],category=local["category"],
                    description=local["description"],endpoint=target,deadline_minutes=local["deadline_minutes"],
                    step_timeout_seconds=local["step_timeout_seconds"],http_methods=local["http_methods"]))
            if value.get("default_description_enabled") and local["status"]=="draft" and not local["archived"]:
                from agent_adapters.workbench import merged_description
                merged=merged_description(local["description"],value.get("default_description"))
                if merged!=(local["description"] or ""):
                    local=self.workbench.update(local["id"],dict(title=local["title"],category=local["category"],
                        description=merged,endpoint=local["endpoint"],deadline_minutes=local["deadline_minutes"],
                        step_timeout_seconds=local["step_timeout_seconds"],http_methods=local["http_methods"]))
        try:
            return self._import_attachment(profile,item,local,check_active)
        except (WorkbenchError,OSError) as error:
            code=error.code if isinstance(error,WorkbenchError) else "competition_attachment_store_failed"
            get_logger("store").warning("competition.attachment_failed",challenge=local["id"],code=code,category="attachment_import")
            if isinstance(error,WorkbenchError):raise
            raise WorkbenchError(code,500) from None

    def _import_attachment(self,profile,item,local,check_active):
        attachment=item.get("attachment")
        if not attachment:
            return local
        name,url=attachment.get("name"),attachment.get("url")
        if (not isinstance(name,str) or not name or len(name)>255 or name in {".",".."}
                or any(c in name for c in ("/","\\","\x00")) or not isinstance(url,str) or not url):
            raise WorkbenchError("competition_attachment_metadata_invalid",409)
        def check():
            if check_active is not None:check_active()
            self.credential_for(profile)
        def verify(value):
            path=self.workbench.uploads/local["id"]/value["id"]
            if path.is_symlink() or not path.is_file():
                raise WorkbenchError("competition_attachment_integrity_failed",409)
            with path.open("rb") as source:
                if path.stat().st_size!=value["byte_size"] or hashlib.file_digest(source,"sha256").hexdigest()!=value["sha256"]:
                    raise WorkbenchError("competition_attachment_integrity_failed",409)
        check()
        with self.workbench.lock,self.workbench._db() as db:
            snapshot=json.loads(db.execute("SELECT snapshot_json FROM competition_questions WHERE challenge_id=?",(local["id"],)).fetchone()[0])
            saved=snapshot["question"].get("attachment") or {}
            if (saved.get("name"),saved.get("size"))!=(name,attachment.get("size")):
                raise WorkbenchError("competition_attachment_changed",409)
            bound=next((v for v in local["attachments"] if v["id"]==snapshot.get("attachment_id")),None)
            if bound is not None:
                verify(bound)
                return local
        try:
            with _download_attachment(profile,self.credential_for(profile),attachment,check) as (source,size,digest):
                with self.workbench.lock:
                    check()
                    current=self.workbench.get(local["id"])
                    existing=next((v for v in current["attachments"] if (v["name"],v["byte_size"],v["sha256"])==(name,size,digest)),None)
                    if existing is None:
                        from agent_adapters.web.request_staging import MAX_V3_REQUEST_BYTES
                        if size+sum(v["byte_size"] for v in current["attachments"])>MAX_V3_REQUEST_BYTES:
                            raise WorkbenchError("competition_attachment_too_large",409)
                        current=self.workbench.add_attachment(local["id"],name,source,size)
                        existing=next(v for v in current["attachments"] if (v["name"],v["byte_size"],v["sha256"])==(name,size,digest))
                    verify(existing)
                    snapshot["attachment_id"]=existing["id"]
                    with self.workbench._db() as db:
                        db.execute("UPDATE competition_questions SET snapshot_json=? WHERE challenge_id=?",(json.dumps(snapshot,ensure_ascii=True),local["id"]))
                    get_logger("store").info("competition.attachment_registered",challenge=local["id"],attachment=existing["id"],category="attachment_import")
                    return current
        except OSError:
            raise WorkbenchError("competition_attachment_store_failed",500) from None

    def freeze(self,challenge,directory,request,*,schedule=None):
        from agent_adapters.run_files import immutable_write
        with self.workbench.lock,self.workbench._db() as db:
            row=db.execute("SELECT snapshot_json FROM competition_questions WHERE challenge_id=?",(challenge["id"],)).fetchone()
            if row is None:return
            snapshot=json.loads(row["snapshot_json"])
            self.credential_for(snapshot["profile"])
            if challenge["endpoint"] and not any(_matches_target(raw,challenge["endpoint"]) for raw in snapshot["question"]["addresses"]):
                raise WorkbenchError("competition_target_not_in_question",409)
            if not challenge["endpoint"] and not challenge["attachments"]:
                raise WorkbenchError("competition_question_input_required",409)
            if request.get("execution_profile")!="d8_v22":raise WorkbenchError("competition_requires_contract22",409)
            deadline=snapshot["platform_deadline_at"]
            if deadline is not None and deadline<=time.time():raise WorkbenchError("competition_question_not_active",409)
            immutable_write(directory/"competition-binding.json",{"request_id":request["request_id"],**snapshot,
                **({"schedule":schedule} if schedule is not None else {})})

    def credential_for(self,frozen):
        with self.workbench.lock:
            profile=self._get(frozen["id"])
            if any(profile[k]!=frozen[k] for k in ("adapter","base_url")):
                raise WorkbenchError("competition_profile_changed",409)
            credential=self.credentials.get(profile["id"])
            if not credential:raise WorkbenchError("competition_credential_missing",409)
            return credential
