"""C01-1 competition profiles and read-only platform previews; no Agent dispatch."""

import http.client
import json
import socket
import ssl
import time
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from agent_adapters.workbench_contracts import WorkbenchError
from qinyuan_core import get_logger

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
READ_PATHS = {"arena": "/api/skill/agent/arena/current/", "benchmark": "/openapi/v1/challenges"}


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


def _exchange(profile, credential, method, path, payload=None):
    parsed = urlsplit(profile["base_url"])
    connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(parsed.hostname, parsed.port, timeout=profile["timeout_seconds"])
    header = {"Authorization":"Bearer "+credential} if profile["adapter"]=="arena" else {"BENCHMARK_TOKEN":credential}
    body = None if payload is None else json.dumps(payload,ensure_ascii=True,separators=(",",":")).encode()
    try:
        connection.request(method,path,body=body,headers={"Accept":"application/json",**header,
            **({"Content-Type":"application/json"} if body is not None else {})})
        response=connection.getresponse(); status=response.status
        if 300<=status<400:raise WorkbenchError("competition_redirect_blocked",502)
        if status in {401,403}:raise WorkbenchError("competition_auth_failed",502)
        errors={429:"competition_rate_limited",422:"competition_request_rejected",503:"competition_resource_unavailable"}
        if status in errors:raise WorkbenchError(errors[status],502)
        if status>=500:raise WorkbenchError("competition_http_error",502)
        raw=response.read(MAX_RESPONSE_BYTES+1)
        if len(raw)>MAX_RESPONSE_BYTES:raise WorkbenchError("competition_response_too_large",502)
        try:value=json.loads(raw)
        except (ValueError,UnicodeError,RecursionError):raise WorkbenchError("competition_response_invalid",502) from None
        code=value.get("code") if isinstance(value,dict) else None
        if ((profile["adapter"]=="benchmark" and code=="task_not_found")
            or (profile["adapter"]=="arena" and type(code) is int and code in {401,403})):
            raise WorkbenchError("competition_auth_failed",502)
        if code=="invalid_state":raise WorkbenchError("competition_invalid_state",502)
        if method=="POST" and profile["adapter"]=="benchmark" and status==409 and code=="duplicate":
            return {"duplicate":True}
        if status!=200:raise WorkbenchError("competition_http_error",502)
        if profile["adapter"]=="arena":
            if type(code) is not int:raise WorkbenchError("competition_response_invalid",502)
            if code!=200:raise WorkbenchError("competition_business_error",502)
        return value
    except (TimeoutError,socket.timeout):raise WorkbenchError("competition_timeout",502) from None
    except ssl.SSLError:raise WorkbenchError("competition_tls_failed",502) from None
    except (OSError,http.client.HTTPException):raise WorkbenchError("competition_transport_failed",502) from None
    finally:connection.close()


def _read(profile,credential):
    value=_exchange(profile,credential,"GET",READ_PATHS[profile["adapter"]])
    try:return _redact(_preview(profile["adapter"],value),credential)
    except (ValueError,KeyError,TypeError,AttributeError,RecursionError):
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
    value=_exchange(profile,credential,"POST",path,payload)
    try:
        if profile["adapter"]=="arena":
            data=value["data"]; attempt=data["attempt"];correct=data["correct"]
            if type(correct) is not bool or attempt["id"]!=remote_ref:raise ValueError()
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
        raise WorkbenchError("competition_response_invalid",502) from None


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
            return self._public(profile)

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


    def import_question(self,identity,value):
        """Import only platform-observed questions; platform actions remain separate."""
        if not isinstance(value,dict) or set(value)-{"remote_ref","endpoint","deadline_minutes","step_timeout_seconds","http_methods"} or "remote_ref" not in value:
            raise WorkbenchError("competition_import_invalid")
        observed_at=time.time()
        checked=self.check(identity);profile=checked["profile"]
        item=next((i for i in checked["preview"]["items"] if type(i["remote_ref"]) is type(value["remote_ref"]) and i["remote_ref"]==value["remote_ref"]),None)
        if item is None:raise WorkbenchError("competition_question_not_found",404)
        if item.get("is_completed") or profile["adapter"]=="arena" and item["state"]!="active":
            raise WorkbenchError("competition_question_not_active",409)
        target=value.get("endpoint","")
        if not isinstance(target,str):raise WorkbenchError("competition_import_invalid")
        if target:
            from agent_core.web_contracts import canonical_web_origin
            try:target=canonical_web_origin(target)
            except (TypeError,ValueError):raise WorkbenchError("competition_target_protocol_unsupported") from None
            matches=[]
            for raw in item["addresses"]:
                if "://" in raw:
                    try:matches.append(canonical_web_origin(raw)==target)
                    except (TypeError,ValueError):matches.append(False)
                else:matches.append(urlsplit(target).netloc==raw)
            if not any(matches):raise WorkbenchError("competition_target_not_in_question")
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
            if existing is not None:return self.workbench.get(existing["challenge_id"])
            category=item.get("category","").lower()
            from agent_adapters.workbench import VALID_CATEGORIES
            if category not in VALID_CATEGORIES:category="misc"
            minutes=value.get("deadline_minutes",60)
            local=self.workbench.create(dict(title=item["display_name"][:160],category=category,
                description=item["description"],endpoint=target,deadline_minutes=minutes,
                step_timeout_seconds=value.get("step_timeout_seconds",60),
                http_methods=value.get("http_methods",["GET","HEAD","OPTIONS"])))
            with self.workbench._db() as db:
                db.execute("INSERT INTO competition_questions VALUES (?,?,?,?,?)",(local["id"],identity,scope,remote,json.dumps(snapshot,ensure_ascii=True)))
            return local

    def freeze(self,challenge,directory,request):
        from agent_adapters.run_files import immutable_write
        with self.workbench.lock,self.workbench._db() as db:
            row=db.execute("SELECT snapshot_json FROM competition_questions WHERE challenge_id=?",(challenge["id"],)).fetchone()
            if row is None:return
            snapshot=json.loads(row["snapshot_json"])
            self.credential_for(snapshot["profile"])
            if not challenge["endpoint"] and not challenge["attachments"]:
                raise WorkbenchError("competition_question_input_required",409)
            if request.get("execution_profile")!="d8_v22":raise WorkbenchError("competition_requires_contract22",409)
            deadline=snapshot["platform_deadline_at"]
            if deadline is not None and deadline<=time.time():raise WorkbenchError("competition_question_not_active",409)
            immutable_write(directory/"competition-binding.json",{"request_id":request["request_id"],**snapshot})

    def credential_for(self,frozen):
        with self.workbench.lock:
            profile=self._get(frozen["id"])
            if any(profile[k]!=frozen[k] for k in ("adapter","base_url")):
                raise WorkbenchError("competition_profile_changed",409)
            credential=self.credentials.get(profile["id"])
            if not credential:raise WorkbenchError("competition_credential_missing",409)
            return credential
