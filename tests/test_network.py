"""Unknown Docker creation results require exact, explicit reconciliation."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from docker.errors import NotFound

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
