"""C01-1 actual loopback HTTP, no competition account, Agent or Docker."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_adapters.workbench import Workbench, WorkbenchServer
from agent_adapters.workbench_contracts import WorkbenchError
from agent_adapters import competition
from test_workbench import call


@pytest.fixture
def platform(tmp_path):
    state = SimpleNamespace(payload=[], status=200, headers={}, requests=[], routes={})
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args):pass
        def do_GET(self):
            state.requests.append((self.command,self.path,dict(self.headers)))
            status,body,headers=state.routes.get(self.path,(state.status,state.payload,state.headers))
            body = body if isinstance(body,bytes) else json.dumps(body).encode()
            self.send_response(status)
            for key,value in headers.items():self.send_header(key,value)
            self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
        def do_POST(self):
            state.requests.append((self.command,self.path,dict(self.headers)))
            if not hasattr(state,"post_payload"):self.send_error(405);return
            length=int(self.headers.get("Content-Length","0"))
            state.bodies.append(json.loads(self.rfile.read(length)) if length else None)
            body=json.dumps(state.post_payload).encode()
            self.send_response(getattr(state,"post_status",200));self.send_header("Content-Length",str(len(body)))
            self.end_headers();self.wfile.write(body)
    upstream=ThreadingHTTPServer(("127.0.0.1",0),Upstream)
    workbench=Workbench(tmp_path)
    server=WorkbenchServer(("127.0.0.1",0),workbench)
    threads=[threading.Thread(target=s.serve_forever,daemon=True) for s in (upstream,server)]
    for t in threads:t.start()
    state.workbench,state.port,state.url=workbench,server.server_port,f"http://127.0.0.1:{upstream.server_port}"
    def profile(adapter="benchmark",**updates):
        token = str(uuid4()) if adapter=="benchmark" else "controlled-arena-token"
        value=dict(name="Local test",adapter=adapter,base_url=state.url,timeout_seconds=2,credential=token)
        value.update(updates)
        status,result=call(state.port,"POST","/api/competition/profiles",value)
        assert status==201,result
        return result,value["credential"]
    state.profile=profile
    yield state
    for s in (upstream,server):s.shutdown();s.server_close()
    for t in threads:t.join(timeout=3)


def probe(p,profile):return call(p.port,"POST",f"/api/competition/profiles/{profile['id']}/check",{})


def benchmark(**changes):
    return dict(unique_code="test-code",description="Controlled question",flag_count=3,correct_flag_count=1,
        is_completed=False,container_status="available",container_addr=["10.0.1.5:8080"],**changes)


def test_arena_no_current_question_is_connected_without_receiving_question(platform):
    p=platform;profile,token=p.profile("arena")
    p.payload={"code":200,"data":{"agent":{"rating":1200,"attempt_count":0},"attempt":None}}
    status,result=probe(p,profile)
    assert status==200 and result["status"]=="connected" and result["preview"]["items"]==[]
    assert p.requests[0][:2]==("GET","/api/skill/agent/arena/current/")
    assert p.requests[0][2]["Authorization"]=="Bearer "+token
    assert "BENCHMARK_TOKEN" not in p.requests[0][2]
    assert token not in json.dumps(result)
    assert not p.workbench.list()


def test_arena_anonymous_attempt_preview_and_credential_reflection(platform):
    p=platform;profile,token=p.profile("arena")
    p.payload={"code":200,"data":{"agent":{"rating":1200,"secret":token},"attempt":{
        "id":123,"state_label":"active","wrong_count":2,"max_wrong_count":20,"remaining_seconds":300,
        "problem":{"type_label":"Pwn","content":"untrusted <script> " + token,"container":None,
            "annex":{"name":"test.zip","size":12,"url":"https://example.test/a"}}}}}
    status,result=probe(p,profile)
    item=result["preview"]["items"][0]
    assert status==200 and item["remote_ref"]==123 and item["display_name"]=="Arena attempt #123"
    assert item["addresses"]==[] and item["wrong_count"]==2 and item["remaining_seconds"]==300
    assert item["attachment"]["name"]=="test.zip" and token not in json.dumps(result)
    assert len(p.requests)==1


def test_benchmark_multiflag_and_exact_auth_header_without_start(platform):
    p=platform;profile,token=p.profile();p.payload=[benchmark()]
    status,result=probe(p,profile)
    assert status==200,result
    item=result["preview"]["items"][0]
    assert item["flag_count"]==3 and item["correct_flag_count"]==1 and not item["is_completed"]
    assert item["addresses"]==["10.0.1.5:8080"] and item["remaining_seconds"] is None
    assert p.requests[0][:2]==("GET","/openapi/v1/challenges")
    assert p.requests[0][2]["BENCHMARK_TOKEN"]==token and "Authorization" not in p.requests[0][2]
    assert not p.workbench.list() and len(p.requests)==1


def test_profile_persists_but_secret_does_not_and_clearing_blocks_reads(platform):
    p=platform;profile,token=p.profile()
    assert token not in p.workbench.database.read_bytes().decode("utf-8",errors="ignore")
    restarted=Workbench(p.workbench.root)
    assert restarted.competitions.list()==[{**profile,"credential_configured":False}]
    status,result=call(p.port,"DELETE",f"/api/competition/profiles/{profile['id']}/credential")
    assert status==200 and not result["credential_configured"]
    status,result=probe(p,profile)
    assert status==409 and result["error"]=="competition_credential_missing" and not p.requests


def test_change_origin_clears_old_secret_and_blank_edit_preserves_same_origin(platform):
    p=platform;profile,token=p.profile()
    body={k:v for k,v in profile.items() if k not in {"id","credential_configured"}}
    body["credential"]="";body["name"]="Edited"
    status,saved=call(p.port,"PUT",f"/api/competition/profiles/{profile['id']}",body)
    assert status==200 and saved["credential_configured"]
    body["base_url"]="https://changed.example.test"
    status,saved=call(p.port,"PUT",f"/api/competition/profiles/{profile['id']}",body)
    assert status==200 and not saved["credential_configured"]
    assert not p.requests


@pytest.mark.parametrize("http_status,payload,code",[
    (401,b"no JSON needed","competition_auth_failed"),
    (403,{},"competition_auth_failed"),
    (404,{"code":"task_not_found"},"competition_auth_failed"),
    (409,{"code":"invalid_state"},"competition_invalid_state"),
    (422,b"not JSON","competition_request_rejected"),
    (429,{},"competition_rate_limited"),
    (503,b"down","competition_resource_unavailable"),
    (500,b"down","competition_http_error"),
    (404,{"code":"different_failure"},"competition_http_error"),
    (200,b"<html>not JSON</html>","competition_response_invalid"),
    (200,{},"competition_response_invalid"),
])
def test_read_errors_classified_without_echoing_body(platform,http_status,payload,code):
    p=platform;profile,token=p.profile();p.status,p.payload=http_status,payload
    status,result=probe(p,profile)
    assert status==502 and result=={"error":code}
    assert token not in json.dumps(result) and len(p.requests)==1


def test_redirect_never_forwards_authentication(platform):
    p=platform;profile,_=p.profile();p.status=302;p.headers={"Location":p.url+"/credential-sink"}
    status,result=probe(p,profile)
    assert status==502 and result["error"]=="competition_redirect_blocked"
    assert len(p.requests)==1 and p.requests[0][1]=="/openapi/v1/challenges"


@pytest.mark.parametrize("url",["https://example.test/api","https://user:password@example.test","file:///tmp/test","https://example.test?token=x","https://example.test#x","https://bad host"])
def test_profile_rejects_non_origin_addresses_before_network(platform,url):
    p=platform
    status,result=call(p.port,"POST","/api/competition/profiles",dict(name="bad",adapter="arena",base_url=url,timeout_seconds=2))
    assert status==400 and result["error"]=="competition_origin_invalid" and not p.requests


def test_bounded_payload_and_secret_control_fields(platform,monkeypatch):
    p=platform;profile,_=p.profile()
    monkeypatch.setattr(competition,"MAX_RESPONSE_BYTES",64)
    p.payload=[benchmark()]
    assert probe(p,profile)[1]["error"]=="competition_response_too_large"
    status,result=call(p.port,"POST",f"/api/competition/profiles/{profile['id']}/check",{"verdict":"accepted"})
    assert status==400 and result["error"]=="competition_check_fields_invalid" and len(p.requests)==1
    status,_=call(p.port,"POST",f"/api/competition/profiles/{profile['id']}/submit",{})
    assert status==405 and len(p.requests)==1
    status,_=call(p.port,"GET","/api/competition/profiles",origin="https://outside.example")
    assert status==403


def test_configuration_change_during_request_is_not_reported_as_new_profile(platform,monkeypatch):
    p=platform;profile,_=p.profile()
    def change(config,credential):
        value={k:v for k,v in config.items() if k!="id"};value["base_url"]="https://changed.example.test"
        p.workbench.competitions.save(value,config["id"])
        return {"items":[],"agent":None}
    monkeypatch.setattr(competition,"_read",change)
    assert probe(p,profile)==(409,{"error":"competition_profile_changed"})


def test_transport_timeout_stays_technical(platform,monkeypatch):
    p=platform;profile,_=p.profile()
    class TimeoutConnection:
        def __init__(self,*args,**kwargs):pass
        def request(self,*args,**kwargs):raise TimeoutError()
        def close(self):pass
    monkeypatch.setattr(competition.http.client,"HTTPConnection",TimeoutConnection)
    with pytest.raises(WorkbenchError,match="competition_timeout"):
        competition._read(profile,str(uuid4()))


def arena_attachment(p, url=None, body=b"controlled archive bytes"):
    p.payload={"code":200,"data":{"agent":{},"attempt":{"id":11905,"state_label":"active",
        "remaining_seconds":600,"problem":{"type_label":"Crypto","content":"controlled attachment task",
        "container":None,"annex":{"name":"equivalent.zip","size":len(body),"url":url or p.url+"/archive"}}}}}
    p.routes["/archive"]=(200,body,{})
    return body


def import_attachment(p, profile):
    p.workbench.protocol_version=3;p.workbench.execution_profile="d8_v22"
    return p.workbench.competitions.import_question(profile["id"],{"remote_ref":11905})


def test_platform_attachment_uses_original_registration_and_reuses_verified_bytes(platform):
    import hashlib
    p=platform;profile,token=p.profile("arena");body=arena_attachment(p)
    task=import_attachment(p,profile)
    file,=task["attachments"]
    assert file["name"]=="equivalent.zip" and file["byte_size"]==len(body)
    assert file["sha256"]==hashlib.sha256(body).hexdigest()
    assert (p.workbench.uploads/task["id"]/file["id"]).read_bytes()==body
    assert import_attachment(p,profile)["attachments"]==task["attachments"]
    downloads=[r for r in p.requests if r[1]=="/archive"]
    assert len(downloads)==1 and downloads[0][2]["Authorization"]=="Bearer "+token
    assert token not in p.workbench.database.read_bytes().decode("utf-8",errors="ignore")
    assert p.workbench.runner.status(task["id"]) is None


def test_cross_origin_attachment_redirect_strips_all_platform_auth(platform,monkeypatch):
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    from Sandbox import tool_download_worker
    p=platform;profile,token=p.profile("arena");body=arena_attachment(p)
    requests=[]
    class CDN(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            requests.append(dict(self.headers));self.send_response(200)
            self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
    cdn=ThreadingHTTPServer(("127.0.0.1",0),CDN)
    worker=threading.Thread(target=cdn.serve_forever,daemon=True);worker.start()
    p.routes["/archive"]=(302,b"",{"Location":f"http://127.0.0.1:{cdn.server_port}/file"})
    # The controlled CDN is local; production still rejects cross-origin private addresses.
    monkeypatch.setattr(tool_download_worker,"public_addresses",lambda host,port,forbidden:socket_addresses(host,port))
    try:
        assert len(import_attachment(p,profile)["attachments"])==1
        assert requests and "Authorization" not in requests[0] and "BENCHMARK_TOKEN" not in requests[0]
        assert token not in json.dumps(requests)
    finally:
        cdn.shutdown();cdn.server_close();worker.join(timeout=3)


def socket_addresses(host,port):
    import socket
    return socket.getaddrinfo(host,port,type=socket.SOCK_STREAM)


@pytest.mark.parametrize("failure",["http","size","private","oversized","redirect","scheme","name","storage"])
def test_attachment_failure_does_not_register_partial_bytes_or_start_agent(platform,monkeypatch,failure):
    p=platform;profile,_=p.profile("arena");arena_attachment(p)
    expected="competition_attachment_http_403"
    if failure=="http":p.routes["/archive"]=(403,b"private upstream body",{})
    elif failure=="size":p.routes["/archive"]=(200,b"short",{});expected="competition_attachment_size_mismatch"
    elif failure=="private":p.payload["data"]["attempt"]["problem"]["annex"]["url"]="http://127.0.0.1:1/private";expected="competition_attachment_url_rejected"
    elif failure=="oversized":p.payload["data"]["attempt"]["problem"]["annex"]["size"]=64*1024*1024+1;expected="competition_attachment_too_large"
    elif failure=="redirect":p.routes["/archive"]=(302,b"",{"Location":"/archive"});expected="competition_attachment_redirect_limit"
    elif failure=="scheme":p.payload["data"]["attempt"]["problem"]["annex"]["url"]="file:///private";expected="competition_attachment_url_rejected"
    elif failure=="name":p.payload["data"]["attempt"]["problem"]["annex"]["name"]="../private";expected="competition_attachment_metadata_invalid"
    else:
        def unavailable():raise OSError("controlled local storage failure")
        monkeypatch.setattr(competition.tempfile,"TemporaryFile",unavailable)
        expected="competition_attachment_store_failed"
    with pytest.raises(WorkbenchError,match=expected):import_attachment(p,profile)
    task,=p.workbench.list()
    assert not task["attachments"] and p.workbench.runner.status(task["id"]) is None
    assert not list(p.workbench.uploads.rglob("*.part"))


def test_attachment_registration_interruption_reuses_identical_registered_file(platform,monkeypatch):
    p=platform;profile,_=p.profile("arena");arena_attachment(p)
    register=p.workbench.add_attachment
    def interrupted(*args):
        register(*args)
        raise OSError("controlled interruption after attachment registration")
    monkeypatch.setattr(p.workbench,"add_attachment",interrupted)
    with pytest.raises(WorkbenchError,match="competition_attachment_store_failed"):import_attachment(p,profile)
    task,=p.workbench.list();original=task["attachments"]
    assert len(original)==1
    monkeypatch.setattr(p.workbench,"add_attachment",register)
    assert import_attachment(p,profile)["attachments"]==original
    before=len(p.requests)
    assert import_attachment(p,profile)["attachments"]==original
    assert len(p.requests)==before+1  # Refresh platform metadata, reuse the registered download.


def test_changed_registered_attachment_is_not_silently_downloaded_over(platform):
    p=platform;profile,_=p.profile("arena");arena_attachment(p)
    task=import_attachment(p,profile);file,=task["attachments"]
    path=p.workbench.uploads/task["id"]/file["id"];path.write_bytes(b"changed")
    before=len([r for r in p.requests if r[1]=="/archive"])
    with pytest.raises(WorkbenchError,match="competition_attachment_integrity_failed"):import_attachment(p,profile)
    assert len([r for r in p.requests if r[1]=="/archive"])==before and path.read_bytes()==b"changed"

def test_arena_abandon_posts_to_the_attempt_and_accepts_an_empty_reply(monkeypatch):
    calls=[]
    def exchange(*args,**kwargs):
        calls.append((args,kwargs));return {}
    monkeypatch.setattr(competition,"_exchange",exchange)
    receipt=competition.platform_action({"adapter":"arena"},"controlled","abandon",12041)
    assert receipt["abandoned"] is True
    args,kwargs=calls[0]
    assert args[2]=="POST" and args[3]=="/api/skill/agent/arena/attempt/12041/abandon/"
    assert kwargs.get("empty_ok") is True

def test_benchmark_protocol_has_no_abandon_action():
    with pytest.raises(WorkbenchError,match="competition_platform_action_unsupported"):
        competition.platform_action({"adapter":"benchmark"},"controlled","abandon","test-code")
    with pytest.raises(WorkbenchError,match="competition_platform_action_unsupported"):
        competition.platform_action({"adapter":"arena"},"controlled","close","test-code")
