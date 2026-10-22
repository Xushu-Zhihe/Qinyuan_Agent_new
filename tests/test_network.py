"""Unknown Docker creation results require exact, explicit reconciliation."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from docker.errors import APIError, NotFound

from Sandbox.target_address import TargetAddress, TargetAddressError
from Sandbox.target_network import T6TargetNetwork, T6TargetNetworkError


class Resource:
    def __init__(self, collection, name, labels, status=None):
        self.collection = collection
        self.name = name
        self.id = uuid4().hex * 2
        self.attrs = {"Labels": labels, "Config": {"Labels": labels}}
        self.status = status

    def reload(self):
        return None

    def remove(self, **_kwargs):
        self.collection.items.pop(self.name)


class Collection:
    def __init__(self, *, fail_on_create=0, status=None):
        self.items = {}
        self.calls = 0
        self.fail_on_create = fail_on_create
        self.status = status

    def get(self, name):
        if name not in self.items:
            resource = next((r for r in self.items.values() if r.id == name), None)
            if resource is not None:return resource
        if name not in self.items:
            raise NotFound("missing")
        return self.items[name]

    def create(self, name=None, **kwargs):
        self.calls += 1
        if self.calls == self.fail_on_create:
            raise TimeoutError("Docker response lost")
        resource = Resource(
            self,
            name or kwargs["name"],
            kwargs["labels"],
            self.status,
        )
        self.items[resource.name] = resource
        return resource


def network(*, fail_on_network_create=0):
    networks = Collection(fail_on_create=fail_on_network_create)
    containers = Collection(status="created")
    runtime = SimpleNamespace(
        client=SimpleNamespace(
            images=SimpleNamespace(get=lambda _name: SimpleNamespace(id="a" * 64)),
            networks=networks,
            containers=containers,
        )
    )
    return T6TargetNetwork(runtime), networks, containers


def test_partial_creation_is_unknown_and_requires_explicit_cleanup():
    adapter, networks, _containers = network(fail_on_network_create=2)
    task_id = str(uuid4())

    with pytest.raises(T6TargetNetworkError) as error:
        adapter.create(task_id, "tcp://8.8.8.8:31337")
    assert error.value.outcome == "outcome_unknown"
    assert len(error.value.resource_ids) == 1
    assert len(networks.items) == 1

    with pytest.raises(T6TargetNetworkError, match="already_attempted"):
        adapter.create(task_id, "tcp://8.8.8.8:31337")
    adapter.cleanup_uncommitted(task_id)
    assert not networks.items


def test_uncommitted_running_forwarder_is_not_stopped_automatically():
    adapter, networks, containers = network()
    task_id = str(uuid4())
    labels = adapter._labels(task_id)
    external = Resource(networks, adapter._egress_name(task_id), labels)
    networks.items[external.name] = external
    proxy = Resource(containers, "qinyuan-t6-forwarder-" + task_id, labels, "running")
    containers.items[proxy.name] = proxy

    with pytest.raises(T6TargetNetworkError, match="outcome_unknown"):
        adapter.cleanup_uncommitted(task_id)
    assert external.name in networks.items
    assert proxy.name in containers.items


def test_existing_named_resources_block_a_second_creation():
    adapter, networks, _containers = network()
    task_id = str(uuid4())
    name = adapter._egress_name(task_id)
    networks.items[name] = Resource(networks, name, adapter._labels(task_id))

    with pytest.raises(T6TargetNetworkError, match="existing_resources"):
        adapter.create(task_id, "tcp://8.8.8.8:31337")
    assert networks.calls == 0


def test_target_address_accepts_public_only():
    assert TargetAddress.from_url("tcp://8.8.8.8:31337").literal_ip == "8.8.8.8"
    for address in ("tcp://127.0.0.1:31337", "http://192.168.1.2:8080/"):
        with pytest.raises(TargetAddressError, match="non_public_target"):
            TargetAddress.from_url(address)


def stopped_binding(adapter, networks, containers, task_id):
    from agent_core.control_store import TargetNetworkBinding
    labels = adapter._labels(task_id)
    external = networks.create(name=adapter._egress_name(task_id), labels=labels)
    private = networks.create(name=adapter.private_name(task_id), labels=labels)
    proxy = containers.create(name="Ayui-forwarder-" + task_id, labels=labels)
    return TargetNetworkBinding(private.id, external.id, proxy.id, "a" * 64, "http://8.8.8.8:80/")


@pytest.mark.parametrize("unconfirmed", [False, True])
def test_bound_network_cleanup_requires_removal_confirmation(tmp_path, unconfirmed):
    from agent_adapters.target_network_cleanup import RecordedTargetNetwork
    from agent_adapters.run_files import immutable_read
    adapter, networks, containers = network()
    task = str(uuid4())
    binding = stopped_binding(adapter, networks, containers, task)
    if unconfirmed:
        networks.get(binding.private_network_id).remove = lambda: None
    recorded = RecordedTargetNetwork(adapter, tmp_path)
    if unconfirmed:
        with pytest.raises(T6TargetNetworkError):recorded.destroy(task, binding)
        assert not (tmp_path / "target-network-cleanup.json").exists()
        assert len(list(tmp_path.glob("target-network-cleanup-failure-*.json"))) == 1
    else:
        recorded.destroy(task, binding)
        assert not networks.items and not containers.items
        assert immutable_read(tmp_path / "target-network-cleanup.json")["status"] == "succeeded"
        recorded.destroy(task, binding)  # Confirmed absence is idempotent.


def test_daemon_rejection_keeps_partial_ids_and_classified_reason():
    adapter, networks, _ = network()
    create = networks.create
    def fail_second(*args, **kwargs):
        if networks.calls == 1:
            raise APIError("rejected", response=SimpleNamespace(status_code=400),
                           explanation="all predefined address pools have been fully subnetted")
        return create(*args, **kwargs)
    networks.create = fail_second
    with pytest.raises(T6TargetNetworkError) as error:adapter.create(str(uuid4()), "http://8.8.8.8:80/")
    assert error.value.outcome == "creation_failed" and len(error.value.resource_ids) == 1
    assert error.value.code == "docker_address_pool_exhausted"


def test_bound_cleanup_never_removes_another_tasks_resources():
    adapter, networks, containers = network()
    task = str(uuid4())
    binding = stopped_binding(adapter, networks, containers, task)
    with pytest.raises(T6TargetNetworkError):adapter.destroy(str(uuid4()), binding)
    assert len(networks.items) == 2 and len(containers.items) == 1


def test_new_forwarder_name_is_discovered():
    adapter, networks, containers = network()
    task = str(uuid4())
    binding = stopped_binding(adapter, networks, containers, task)
    assert adapter._discover(task)[2].id == binding.forwarder_id


@pytest.mark.parametrize("entry", ["utility", "workbench"])
@pytest.mark.parametrize("legacy", [False, True])
def test_explicit_cleanup_recognizes_new_and_existing_names(tmp_path, monkeypatch, entry, legacy):
    import sqlite3
    import shutil
    import threading
    import docker
    from agent_adapters.force_cleanup import force_cleanup_task
    from agent_adapters.workbench_runner import Runner
    task, run, challenge = (str(uuid4()) for _ in range(3))
    directory = tmp_path / "runs" / run
    (directory / "agent-state" / task).mkdir(parents=True)
    adapter, networks, containers = network()
    stopped_binding(adapter, networks, containers, task)
    containers.create(name=("qinyuan-task-" if legacy else "Ayui-") + task, labels=adapter._labels(task))
    if legacy:
        proxy = containers.items.pop("Ayui-forwarder-" + task)
        proxy.name = "qinyuan-t6-forwarder-" + task
        containers.items[proxy.name] = proxy
    for container in containers.items.values():container.stop = lambda **kw: None
    monkeypatch.setattr(docker, "from_env", lambda: SimpleNamespace(networks=networks, containers=containers))
    deleted = []
    monkeypatch.setattr(shutil, "rmtree", lambda p: deleted.append(p.resolve()))
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("CREATE TABLE runs(run_id,challenge_id); CREATE TABLE attachments(challenge_id); CREATE TABLE challenges(id);")
    db.execute("INSERT INTO runs VALUES(?,?)", (run, challenge))
    workbench = SimpleNamespace(root=tmp_path, lock=threading.RLock(), _db=lambda: db)
    try:
        if entry == "utility":
            result = force_cleanup_task(workbench, challenge)
            assert len(result["containers_stopped"]) == len(result["networks_removed"]) == 2
        else:
            runner = Runner.__new__(Runner)
            runner.workbench, runner._lock, runner._active = workbench, threading.RLock(), {}
            runner._current = lambda _: {"directory": str(directory), "run_id": run}
            runner.force_delete(challenge)
        assert not networks.items and not containers.items
        assert deleted == [directory.resolve()]  # No actual directory deletion in this test.
    finally:
        db.close()
