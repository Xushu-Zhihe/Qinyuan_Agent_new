"""Controlled download preparation: no real Docker, external requests or pip installs."""
import asyncio
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tarfile
from uuid import uuid4
import zipfile
import pytest
from docker.errors import NotFound
from Sandbox.tool_download import ToolDownload, ToolDownloadError, verified_artifacts
from Sandbox import tool_download_worker as worker
from MCPs.Download_Mcp.server import install_tool


def packed(task, operation, payload=b"portable tool", name="native", digest=None):
    actual = hashlib.sha256(payload).hexdigest()
    manifest = dict(task_run_id=task, operation_id=operation, status="completed", errors=[],
                    artifacts=[dict(name=name,size=len(payload),sha256=digest or actual)])
    output=io.BytesIO()
    with tarfile.open(fileobj=output,mode="w") as archive:
        for path,content in [("manifest.json",json.dumps(manifest).encode()),(name,payload)]:
            info=tarfile.TarInfo(path);info.size=len(content);archive.addfile(info,io.BytesIO(content))
    return output.getvalue(),actual


@pytest.mark.parametrize("tamper", ["bytes","owner","path","sha"])
def test_transfer_rejects_changed_bytes_owner_and_archive_paths(tamper):
    task,operation=str(uuid4()),str(uuid4())
    data,sha=packed(task,operation,name="../outside" if tamper=="path" else "native",
                    digest="0"*64 if tamper=="sha" else None)
    if tamper=="bytes":sha="1"*64
    with pytest.raises(ToolDownloadError):
        verified_artifacts(data,{"sha256":sha},str(uuid4()) if tamper=="owner" else task,operation)


@pytest.mark.parametrize("address", ["127.0.0.1","10.0.0.1","169.254.169.254","::1"])
def test_fetch_rejects_nonpublic_dns_before_connect(monkeypatch,address):
    monkeypatch.setattr(worker.socket,"getaddrinfo",lambda *a,**k:[(2,1,6,"",(address,443))])
    with pytest.raises(ValueError,match="download.non_public_source"):
        worker.public_addresses("source.test",443,set())


@pytest.mark.parametrize("url", ["http://source.test/file", "https://u:p@source.test/file", "https://source.test/file#fragment"])
def test_source_requires_https_without_credentials_or_fragment(url):
    with pytest.raises(ValueError):worker.source_url(url)


def test_fetch_pins_public_address_and_checks_sha(monkeypatch,tmp_path):
    body=b"portable bytes"
    sent=[]
    class Socket:
        def settimeout(self,value):pass
        def connect(self,address):sent.append(address)
        def close(self):pass
    class Response:
        status=200
        def __init__(self):self.body=body
        def read(self,n):value,self.body=self.body,b"";return value
    class Connection:
        def __init__(self,*a,context=None,**kw):self._context=context;self.sock=None
        def request(self,*a,**k):pass
        def getresponse(self):return Response()
        def close(self):pass
    monkeypatch.setattr(worker.socket,"getaddrinfo",lambda *a,**k:[(2,1,6,"",("8.8.8.8",443))])
    monkeypatch.setattr(worker.socket,"socket",lambda *a:Socket())
    monkeypatch.setattr(worker.ssl,"create_default_context",lambda:SimpleNamespace(wrap_socket=lambda sock,server_hostname:sock))
    monkeypatch.setattr(worker.http.client,"HTTPSConnection",Connection)
    sha=hashlib.sha256(body).hexdigest()
    result=worker.fetch("https://source.test/file",tmp_path/"source",sha,lambda:10,set())
    assert sent==[("8.8.8.8",443)] and result["sha256"]==sha
    with pytest.raises(ValueError,match="sha256_mismatch"):
        worker.fetch("https://source.test/file",tmp_path/"changed","0"*64,lambda:10,set())
    with pytest.raises(ValueError,match="target_not_allowed"):
        worker.fetch("https://source.test/file",tmp_path/"target",sha,lambda:10,{"source.test"})


@pytest.mark.parametrize("archive_format", ["zip", "tar.xz"])
def test_offline_install_unpacks_prepared_native_and_binds_receipt(tmp_path,monkeypatch,capsys,archive_format):
    source=io.BytesIO()
    if archive_format=="zip":
        with zipfile.ZipFile(source,"w") as archive:archive.writestr("7zz",b"controlled portable executable")
    else:
        with tarfile.open(fileobj=source,mode="w:xz") as archive:
            content=b"controlled portable executable";info=tarfile.TarInfo("7zz");info.size=len(content)
            archive.addfile(info,io.BytesIO(content))
    payload=source.getvalue();sha=hashlib.sha256(payload).hexdigest()
    root=tmp_path/"tools";(root/"sources").mkdir(parents=True)
    (root/"sources"/sha).write_bytes(payload)
    monkeypatch.setenv("QINYUAN_OFFLINE_TOOL_INSTALL","1")
    plan=install_tool(name="sevenzip",url="https://source.test/7zip.zip",sha256=sha,
                      archive_format=archive_format,executable_path="7zz")
    code=plan["sandbox_command"]["arguments"][1].replace("ROOT = Path('/tools')","ROOT = Path("+repr(str(root))+")")
    monkeypatch.setattr(worker.socket,"create_connection",lambda *a,**k:pytest.fail("offline installer attempted network"))
    exec(compile(code,"controlled-installer","exec"),{})
    assert (root/"apps/sevenzip/7zz").read_bytes()==b"controlled portable executable"
    receipt=json.loads((root/"receipts/sevenzip.json").read_text())
    assert receipt["request_sha256"] and receipt["path"]==str(root/"apps/sevenzip/7zz")
    capsys.readouterr()
    with pytest.raises(SystemExit) as repeated:
        exec(compile(code,"controlled-installer","exec"),{})
    assert repeated.value.code==0
    reused=json.loads(capsys.readouterr().out)
    assert reused["status"]=="already_installed" and reused["path"]==receipt["path"]
    (root/"sources"/sha).write_bytes(b"changed")
    (root/"receipts/sevenzip.json").unlink()
    with pytest.raises(RuntimeError,match="tool installation failed"):
        exec(compile(code,"controlled-installer","exec"),{})


class Evidence:
    def __init__(self):self.rows=[]
    def save(self,kind,key,value):self.rows.append((kind,key,dict(value)))


def test_source_allowlist_is_fail_closed_before_any_worker():
    task=SimpleNamespace(task_run_id=str(uuid4()))
    sandbox=SimpleNamespace(_runtime=SimpleNamespace(client=object()))
    broker=ToolDownload(sandbox,task,str(uuid4()),Evidence(),allowed_hosts={"source.test"})
    with pytest.raises(ToolDownloadError,match="download.source_not_allowed"):
        broker.prepare({"url":"https://evil.test/tool","sha256":"0"*64},10)
    assert broker._source_allowed("https://source.test/tool")
    assert not broker._source_allowed("https://sub.source.test/tool")
    assert ToolDownload(sandbox,task,str(uuid4()),Evidence(),
                        allowed_hosts={".example.com"})._source_allowed("https://a.example.com/x")


def test_worker_public_addresses_enforces_source_allowlist(monkeypatch):
    monkeypatch.setattr(worker.socket,"getaddrinfo",lambda *a,**k:[(2,1,6,"",("8.8.8.8",443))])
    with pytest.raises(ValueError,match="download.source_not_allowed"):
        worker.public_addresses("evil.test",443,set(),{"source.test"})
    assert worker.public_addresses("source.test",443,set(),{"source.test"})


def test_installer_is_bounded_and_atomic():
    plan=install_tool(name="sevenzip",url="https://source.test/7zip.zip",sha256="0"*64,
                      archive_format="zip",executable_path="7zz")
    script=plan["sandbox_command"]["arguments"][1]
    assert "__SHA256__" not in script and "__TIMEOUT_SECONDS__" not in script
    assert "sha256_file" in script and "read_bytes()" not in script
    assert "MAX_ARCHIVE_MEMBERS" in script and "MAX_EXTRACTED_BYTES" in script
    assert "os.link(source, staging)" in script
    assert "os.replace(temporary, receipt)" in script
    assert "read_receipt()" in script
    assert "shell=True" not in script


def test_reserved_platform_names_are_rejected():
    for name in ("python3","timeout","curl","nmap","bash"):
        with pytest.raises(ValueError,match="platform executable"):
            install_tool(name=name,url="https://source.test/tool",sha256="0"*64)


def _offline_code(tmp_path, archive_format="zip", **kwargs):
    payload = io.BytesIO()
    if archive_format == "zip":
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("7zz", b"portable executable")
    else:
        with tarfile.open(fileobj=payload, mode="w:xz") as archive:
            content = b"portable executable"
            info = tarfile.TarInfo("7zz")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    raw = payload.getvalue()
    sha = hashlib.sha256(raw).hexdigest()
    root = tmp_path / "tools"
    (root / "sources").mkdir(parents=True)
    (root / "sources" / sha).write_bytes(raw)
    monkeypatch = kwargs.pop("monkeypatch")
    monkeypatch.setenv("QINYUAN_OFFLINE_TOOL_INSTALL", "1")
    plan = install_tool(name="sevenzip", url="https://source.test/tool", sha256=sha,
                        archive_format=archive_format, executable_path="7zz")
    code = plan["sandbox_command"]["arguments"][1].replace(
        "ROOT = Path('/tools')", "ROOT = Path(" + repr(str(root)) + ")"
    )
    return root, code


def test_offline_install_recovers_from_corrupt_receipt(tmp_path, monkeypatch):
    root, code = _offline_code(tmp_path, monkeypatch=monkeypatch)
    exec(compile(code, "installer", "exec"), {})
    (root / "receipts/sevenzip.json").write_text("{broken")
    exec(compile(code, "installer", "exec"), {})
    assert (root / "apps/sevenzip/7zz").read_bytes() == b"portable executable"


def test_offline_install_rejects_oversized_archive(tmp_path, monkeypatch):
    root, code = _offline_code(tmp_path, monkeypatch=monkeypatch)
    code = code.replace("MAX_EXTRACTED_BYTES = 240 * 1024 * 1024", "MAX_EXTRACTED_BYTES = 10")
    with pytest.raises(RuntimeError, match="archive expands"):
        exec(compile(code, "installer", "exec"), {})


def test_offline_install_prunes_unreferenced_artifacts(tmp_path, monkeypatch):
    root, code = _offline_code(tmp_path, monkeypatch=monkeypatch)
    current = {item.name for item in (root / "sources").iterdir()}
    (root / "sources/dead").write_bytes(b"dead")
    (root / "sources/keep").write_bytes(b"keep")
    (root / "wheels").mkdir()
    (root / "wheels/old").mkdir()
    (root / "wheels/keep").mkdir()
    (root / "receipts").mkdir()
    (root / "receipts/old.json").write_text(json.dumps({
        "source_sha256": "keep", "wheelhouse": str(root / "wheels/keep")
    }))
    exec(compile(code, "installer", "exec"), {})
    names = {item.name for item in (root / "sources").iterdir()}
    assert current <= names and "dead" not in names and "keep" in names
    wheels = {item.name for item in (root / "wheels").iterdir()}
    assert wheels == {"keep"}


def test_python_fallback_uses_per_tool_target_and_wrapper(tmp_path, monkeypatch):
    import subprocess
    from types import SimpleNamespace
    root = tmp_path / "tools"
    wheelhouse = root / "wheels" / "op"
    wheelhouse.mkdir(parents=True)
    wheel = wheelhouse / "demo-1-py3-none-any.whl"
    wheel.write_bytes(b"wheel bytes")
    sha = hashlib.sha256(b"wheel bytes").hexdigest()
    monkeypatch.setenv("QINYUAN_OFFLINE_TOOL_INSTALL", "1")
    monkeypatch.setenv("QINYUAN_TOOL_WHEELHOUSE", str(wheelhouse))
    monkeypatch.setenv("QINYUAN_TOOL_WHEEL_HASHES", json.dumps({wheel.name: sha}))
    plan = install_tool(name="demo", python_package="demo", python_version="1")
    code = plan["sandbox_command"]["arguments"][1]
    assert "environment['TMPDIR']" in code and "'.tmp-'" in code
    code = code.replace(
        "ROOT = Path('/tools')", "ROOT = Path(" + repr(str(root)) + ")"
    )
    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
        exec(compile(code, "installer", "exec"), {})
    target = root / "python/demo"
    wrapper = root / "apps/demo/bin/python3"
    assert target.is_dir() and wrapper.is_file()
    assert str(target) in wrapper.read_text()
    receipt = json.loads((root / "receipts/demo.json").read_text())
    assert receipt["kind"] == "python"
    assert receipt["path"] == str(wrapper)
    assert receipt["python_target"] == str(target)
    with __import__("contextlib").redirect_stdout(io.StringIO()) as out:
        exec(compile(code, "installer", "exec"), {})
    assert json.loads(out.getvalue().strip().splitlines()[-1])["status"] == "already_installed"


def test_worker_native_survives_invalid_wheel(tmp_path, monkeypatch):
    import sys
    class Output:
        def __init__(self):self.buffer=io.BytesIO()
        def write(self,value):return len(value)
        def flush(self):pass
    output=Output()
    monkeypatch.setattr(sys,"stdout",output)
    monkeypatch.setattr(worker.os,"setsid",lambda:None,raising=False)
    monkeypatch.setattr(worker,"Path",lambda value:tmp_path if value=="/tools-download" else Path(value))
    def fetch(url,path,expected,remaining,forbidden,allowed_hosts=None):
        path.write_bytes(b"native")
        return {"name":"native","sha256":hashlib.sha256(b"native").hexdigest(),"size":6}
    def download(arguments,**kwargs):
        destination=Path(arguments[arguments.index("--dest")+1])
        (destination/"bad.whl").mkdir()
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(worker,"fetch",fetch)
    monkeypatch.setattr(worker.subprocess,"run",download)
    worker.main({"operation_id":str(uuid4()),"task_run_id":str(uuid4()),"timeout":10,
        "allowed_hosts":["source.test"],
        "arguments":{"url":"https://source.test/native","sha256":"0"*64,
                     "python_package":"fallback","python_version":"1"}})
    with tarfile.open(fileobj=io.BytesIO(output.buffer.getvalue()),mode="r:") as archive:
        manifest=json.load(archive.extractfile("manifest.json"))
        assert manifest["status"]=="completed"
        assert [a["name"] for a in manifest["artifacts"]]==["native"]
        assert any("download.invalid_wheel" in item for item in manifest["errors"])
        assert archive.extractfile("native").read()==b"native"


@pytest.mark.parametrize("reuse", [False,True])
@pytest.mark.parametrize("tamper", [False,True])
def test_broker_reuses_forwarder_or_cleans_temporary_worker_without_host_files(reuse,tamper):
    task_id,operation=str(uuid4()),str(uuid4())
    archive,sha=packed(task_id,operation,digest="0"*64 if tamper else None)
    class Container:
        def __init__(self,identity,labels):self.id=identity;self.status="running";self.attrs={"Config":{"Labels":labels}};self.copies=[]
        def reload(self):pass
        def start(self):self.status="running"
        def stop(self,**kw):self.status="exited"
        def remove(self,**kw):containers.items.pop(self.id)
        def put_archive(self,destination,data):self.copies.append((destination,data));return True
    class Containers:
        def __init__(self):self.items={};self.creates=[]
        def get(self,key):
            if key not in self.items:raise NotFound("absent")
            return self.items[key]
        def create(self,**kwargs):
            self.creates.append(kwargs)
            result=Container("helper",kwargs["labels"]);self.items[result.id]=result;return result
    containers=Containers()
    target=Container("sandbox",{"qinyuan.task_run_id":task_id});containers.items[target.id]=target
    forwarder=Container("forwarder",{"qinyuan.task_run_id":task_id,"qinyuan.component":"t6-target-network"})
    containers.items[forwarder.id]=forwarder
    class API:
        def exec_create(self,*a,**kw):return {"Id":"exec"}
        def exec_start(self,*a,**kw):return iter([(b'{"pid":123}\n'+archive,None)])
        def exec_inspect(self,*a):return {"Running":False,"ExitCode":0}
    sandbox=SimpleNamespace(_runtime=SimpleNamespace(client=SimpleNamespace(containers=containers,api=API())),
        _settings=SimpleNamespace(image="frozen-image",memory_limit="128m",memory_swap_limit="128m",cpu_limit=1000000000,process_limit=64),
        _verify_active=lambda *a:None)
    task=SimpleNamespace(task_run_id=task_id,sandbox_id="sandbox",
        target_network=SimpleNamespace(forwarder_id="forwarder",target_url="http://target.test:80") if reuse else None)
    evidence=Evidence();broker=ToolDownload(sandbox,task,operation,evidence)
    if tamper:
        with pytest.raises(ToolDownloadError,match="sha256_mismatch"):broker.prepare({"sha256":sha},10)
        assert not target.copies
    else:
        result=broker.prepare({"sha256":sha},10)
        assert result["wheel_hashes"]=={} and len(target.copies)==1
        with tarfile.open(fileobj=io.BytesIO(target.copies[0][1])) as copied:
            assert copied.extractfile("sources/"+sha).read()==b"portable tool"
    assert "helper" not in containers.items and forwarder.status=="running"
    assert len(containers.creates)==(0 if reuse else 1)
    if not reuse:assert evidence.rows[-1][2]["cleanup"]=="confirmed"


def test_cancelled_preparation_joins_confirmed_worker_without_installing():
    import threading
    from Sandbox.tool_download import ToolDownloadUnknown
    task=SimpleNamespace(task_run_id=str(uuid4()))
    sandbox=SimpleNamespace(_runtime=SimpleNamespace(client=object()))
    broker=ToolDownload(sandbox,task,str(uuid4()),Evidence())
    started,stopped=threading.Event(),threading.Event()
    def prepare(*args):
        started.set()
        assert stopped.wait(2)
        raise ToolDownloadError("download.cancelled_before_install")
    broker.prepare=prepare
    broker.interrupt=lambda:stopped.set()
    async def run():
        work=asyncio.create_task(broker.run({},10))
        while not started.is_set():await asyncio.sleep(0)
        work.cancel()
        with pytest.raises(ToolDownloadError,match="preparation_cancelled"):
            await work
    asyncio.run(run())
    assert stopped.is_set() and broker.evidence.rows[-1][2]["state"]=="failed"


def test_pip_dependency_requests_keep_https_public_pypi_boundary(monkeypatch):
    import sys
    import types
    # The project uses uv without host pip. Model only the vendor transport API;
    # the real pip implementation remains part of the user's Docker acceptance.
    for name in ["pip","pip._vendor","pip._vendor.requests","pip._vendor.requests.sessions",
                 "pip._vendor.urllib3","pip._vendor.urllib3.util","pip._vendor.urllib3.util.connection",
                 "pip._internal","pip._internal.cli","pip._internal.cli.main"]:
        module=types.ModuleType(name);module.__path__=[]
        monkeypatch.setitem(sys.modules,name,module)
        if "." in name:
            parent,child=name.rsplit(".",1)
            monkeypatch.setattr(sys.modules[parent],child,module,raising=False)
    class Session:
        def send(self,request,**kwargs):pass
    sys.modules["pip._vendor.requests.sessions"].Session=Session
    connection=sys.modules["pip._vendor.urllib3.util.connection"]
    connection.create_connection=lambda *a,**k:None
    cli=sys.modules["pip._internal.cli.main"]
    cli.main=lambda args:0
    sent=[]
    monkeypatch.setattr(Session,"send",lambda self,request,**kwargs:sent.append(request.url))
    monkeypatch.setattr(connection,"create_connection",lambda *a,**k:object())
    monkeypatch.setattr(cli,"main",lambda args:0)
    monkeypatch.setattr(sys,"argv",["guard","[]"])
    monkeypatch.setattr(worker.socket,"getaddrinfo",lambda *a,**k:[(2,1,6,"",("8.8.8.8",443))])
    namespace={}
    with pytest.raises(SystemExit) as stopped:exec(worker.PIP_GUARD,namespace)
    assert stopped.value.code==0
    Session().send(SimpleNamespace(url="https://pypi.org/simple/example/"))
    assert sent==["https://pypi.org/simple/example/"]
    for url in ["http://pypi.org/simple/example/","https://target.test/file.whl","https://files.pythonhosted.org:8443/file.whl"]:
        with pytest.raises(ValueError,match="python_source_not_allowed"):
            Session().send(SimpleNamespace(url=url))
    monkeypatch.setattr(worker.socket,"getaddrinfo",lambda *a,**k:[(2,1,6,"",("127.0.0.1",443))])
    with pytest.raises(ValueError,match="non_public_source"):
        connection.create_connection(("pypi.org",443))


def test_unknown_worker_creation_never_claims_cleanup_or_retries():
    from Sandbox.tool_download import ToolDownloadUnknown
    creates=[]
    def create(**kwargs):
        creates.append(kwargs)
        raise TimeoutError("Docker may have created the helper")
    sandbox=SimpleNamespace(_runtime=SimpleNamespace(client=SimpleNamespace(containers=SimpleNamespace(create=create))),
        _settings=SimpleNamespace(image="frozen",memory_limit="128m",memory_swap_limit="128m",cpu_limit=1000000000,process_limit=64))
    task=SimpleNamespace(task_run_id=str(uuid4()),target_network=None)
    evidence=Evidence();broker=ToolDownload(sandbox,task,str(uuid4()),evidence)
    with pytest.raises(ToolDownloadUnknown):broker.prepare({},10)
    assert len(creates)==1
    assert evidence.rows[-1][2]["cleanup"]=="unknown"
    assert not any(row[2].get("cleanup")=="confirmed" for row in evidence.rows)


def test_native_success_still_prepares_configured_offline_python_fallback(tmp_path,monkeypatch):
    import sys
    import hashlib
    class Output:
        def __init__(self):self.buffer=io.BytesIO()
        def write(self,value):return len(value)
        def flush(self):pass
    output=Output()
    monkeypatch.setattr(sys,"stdout",output)
    monkeypatch.setattr(worker.os,"setsid",lambda:None,raising=False)
    monkeypatch.setattr(worker,"Path",lambda value:tmp_path if value=="/tools-download" else Path(value))
    def fetch(url,path,expected,remaining,forbidden,allowed_hosts=None):
        path.write_bytes(b"native")
        return {"name":"native","sha256":hashlib.sha256(b"native").hexdigest(),"size":6}
    def download(arguments,**kwargs):
        destination=Path(arguments[arguments.index("--dest")+1])
        (destination/"fallback-1-py3-none-any.whl").write_bytes(b"wheel")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(worker,"fetch",fetch)
    monkeypatch.setattr(worker.subprocess,"run",download)
    worker.main({"operation_id":str(uuid4()),"task_run_id":str(uuid4()),"timeout":10,
        "arguments":{"url":"https://source.test/native","sha256":"0"*64,
                     "python_package":"fallback","python_version":"1"}})
    with tarfile.open(fileobj=io.BytesIO(output.buffer.getvalue()),mode="r:") as archive:
        manifest=json.load(archive.extractfile("manifest.json"))
        assert manifest["status"]=="completed"
        assert [a["name"] for a in manifest["artifacts"]]==["native","wheels/fallback-1-py3-none-any.whl"]
    assert list(tmp_path.iterdir())==[]
