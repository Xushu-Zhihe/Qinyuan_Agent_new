"""TaskRun-owned target-only Docker sidecar and exact resource lifecycle."""

from __future__ import annotations

import hashlib
import json
from uuid import UUID

from docker.errors import APIError, NotFound

from agent_core.control_store import TargetNetworkBinding
from Sandbox.target_address import TargetAddress, is_rfc1918_ipv4

FORWARDER_IMAGE = "qinyuan/t6-target-forwarder:0.1.0"
PRIVATE_IPV4_LABEL = "qinyuan.target.private_ipv4"


class T6TargetNetworkError(RuntimeError):
    def __init__(self, phase: str, outcome: str, resource_ids: tuple[str, ...], *, code="docker_resource_outcome_unknown") -> None:
        self.phase = phase
        self.outcome = outcome
        self.resource_ids = resource_ids
        self.code = code
        super().__init__(f"T6 target network {phase}: {outcome}")


def docker_error_code(error):
    """Classify daemon explanations without putting raw target/config text in logs."""
    explanation = str(getattr(error, "explanation", "")).lower()
    if "fully subnetted" in explanation or ("address pool" in explanation and "available" in explanation):
        return "docker_address_pool_exhausted"
    if "active endpoints" in explanation:
        return "docker_network_in_use"
    status = getattr(getattr(error, "response", None), "status_code", None)
    if status == 403:return "docker_permission_denied"
    if status == 409:return "docker_resource_conflict"
    return "docker_resource_outcome_unknown"


class T6TargetNetwork:
    def __init__(self, runtime, *, allow_private_ipv4: bool = False) -> None:
        self.allow_private_ipv4 = allow_private_ipv4
        self.runtime = runtime
        self.client = runtime.client
        self._attempted: set[str] = set()
        self._stop_requested: set[str] = set()

    @staticmethod
    def _task_id(task_run_id: str) -> None:
        parsed = UUID(task_run_id)
        if parsed.version != 4 or str(parsed) != task_run_id:
            raise ValueError("invalid_task_run_id")

    @staticmethod
    def private_name(task_run_id: str) -> str:
        T6TargetNetwork._task_id(task_run_id)
        return "qinyuan-t6-in-" + task_run_id

    @staticmethod
    def _egress_name(task_run_id: str) -> str:
        return "qinyuan-t6-out-" + task_run_id

    @staticmethod
    def _labels(task_run_id: str) -> dict[str, str]:
        return {
            "qinyuan.managed": "true",
            "qinyuan.component": "t6-target-network",
            "qinyuan.task_run_id": task_run_id,
        }

    def _image_id(self, *, require_private_ipv4=False) -> str:
        image = self.client.images.get(FORWARDER_IMAGE)
        if require_private_ipv4 and (image.labels or {}).get(PRIVATE_IPV4_LABEL) != "rfc1918":
            raise T6TargetNetworkError("create", "unsupported_image", (), code="forwarder_private_support_missing")
        return str(image.id)

    def _command(self, target_url):
        target = TargetAddress.from_url(target_url, allow_private_ipv4=self.allow_private_ipv4)
        return ["--allow-private-ipv4", target_url] if is_rfc1918_ipv4(target.literal_ip) else [target_url]

    def _fingerprint(self, task_run_id: str, target_url: str) -> str:
        value = {
            "image_id": self._image_id(),
            "target_url": target_url,
            "private_name": self.private_name(task_run_id),
            "egress_name": self._egress_name(task_run_id),
            "user": "10001:10001",
            "read_only": True,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
        }
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def _discover(self, task_run_id: str):
        resources = []
        for collection, name in (
            (self.client.networks, self._egress_name(task_run_id)),
            (self.client.networks, self.private_name(task_run_id)),
            (self.client.containers, "Ayui-forwarder-" + task_run_id),
        ):
            try:
                resource = collection.get(name)
                resource.reload()
            except NotFound:
                resource = None
                if collection is self.client.containers:
                    # Failed old creations have no durable ID binding yet.
                    try:
                        resource = collection.get("qinyuan-t6-forwarder-" + task_run_id)
                        resource.reload()
                    except NotFound:
                        pass
            resources.append(resource)
        return tuple(resources)

    def _owned_discovery(self, task_run_id: str, resources) -> tuple[str, ...]:
        ids = []
        for resource in resources:
            if resource is None:
                continue
            labels = resource.attrs.get("Labels")
            if labels is None:
                labels = (resource.attrs.get("Config") or {}).get("Labels") or {}
            if any(labels.get(k) != v for k, v in self._labels(task_run_id).items()):
                raise ValueError("target_resource_ownership_mismatch")
            ids.append(str(resource.id))
        return tuple(ids)

    def create(self, task_run_id: str, target_url: str) -> TargetNetworkBinding:
        self._task_id(task_run_id)
        command = self._command(target_url)
        if len(command) == 2:
            self._image_id(require_private_ipv4=True)
        if task_run_id in self._attempted:
            raise T6TargetNetworkError("create", "already_attempted", ())
        self._attempted.add(task_run_id)
        external = private = proxy = None
        create_sent = False
        try:
            image_id = self._image_id()
            if any(self._discover(task_run_id)):
                raise T6TargetNetworkError("create", "existing_resources", ())
            labels = self._labels(task_run_id)
            create_sent = True
            external = self.client.networks.create(
                self._egress_name(task_run_id),
                driver="bridge",
                internal=False,
                check_duplicate=True,
                labels=labels,
            )
            private = self.client.networks.create(
                self.private_name(task_run_id),
                driver="bridge",
                internal=True,
                check_duplicate=True,
                labels=labels,
            )
            proxy = self.client.containers.create(
                image=image_id,
                command=command,
                network=external.name,
                name="Ayui-forwarder-" + task_run_id,
                labels=labels,
                read_only=True,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                user="10001:10001",
                mem_limit="128m",
                memswap_limit="128m",
                pids_limit=64,
            )
            private.connect(proxy, aliases=["target.qinyuan"])
            proxy.start()
            binding = TargetNetworkBinding(
                private_network_id=str(private.id),
                egress_network_id=str(external.id),
                forwarder_id=str(proxy.id),
                configuration_sha256=self._fingerprint(task_run_id, target_url),
                target_url=target_url,
            )
            self.bind(task_run_id, binding, terminal=False)
            return binding
        except Exception as exc:
            if isinstance(exc, T6TargetNetworkError):
                raise
            ids = {
                str(resource.id)
                for resource in (external, private, proxy)
                if resource is not None
            }
            if create_sent:
                try:
                    ids.update(self._owned_discovery(task_run_id, self._discover(task_run_id)))
                except Exception:  # noqa: BLE001 - Docker SDK boundary
                    exc.add_note(
                        "T6 resource discovery failed; reported IDs may be incomplete."
                    )
            raise T6TargetNetworkError(
                "create", "creation_failed" if not create_sent or (
                    isinstance(exc, APIError) and getattr(exc.response, "status_code", None) in {400, 403, 404, 409}
                ) else "outcome_unknown", tuple(sorted(ids)), code=docker_error_code(exc)
            ) from exc

    def bind(
        self, task_run_id: str, binding: TargetNetworkBinding, *, terminal: bool
    ) -> None:
        self._task_id(task_run_id)
        if not isinstance(binding, TargetNetworkBinding):
            raise TypeError("invalid_target_network_binding")
        if terminal:
            return
        try:
            private = self.client.networks.get(binding.private_network_id)
            external = self.client.networks.get(binding.egress_network_id)
            proxy = self.client.containers.get(binding.forwarder_id)
            private.reload()
            external.reload()
            proxy.reload()
            labels = self._labels(task_run_id)
            for network, name, internal in (
                (private, self.private_name(task_run_id), True),
                (external, self._egress_name(task_run_id), False),
            ):
                attrs = network.attrs
                if (
                    network.name != name
                    or bool(attrs.get("Internal")) != internal
                    or any((attrs.get("Labels") or {}).get(k) != v for k, v in labels.items())
                ):
                    raise ValueError("network_configuration_mismatch")
            config = proxy.attrs.get("Config") or {}
            host = proxy.attrs.get("HostConfig") or {}
            networks = (proxy.attrs.get("NetworkSettings") or {}).get("Networks") or {}
            if (
                proxy.status != "running"
                or any((config.get("Labels") or {}).get(k) != v for k, v in labels.items())
                or proxy.attrs.get("Image") != self._image_id()
                or config.get("Cmd") != self._command(binding.target_url)
                or config.get("User") != "10001:10001"
                or host.get("ReadonlyRootfs") is not True
                or host.get("CapDrop") != ["ALL"]
                or host.get("SecurityOpt") != ["no-new-privileges:true"]
                or host.get("NetworkMode") != external.name
                or set(networks) != {private.name, external.name}
                or "target.qinyuan" not in networks[private.name].get("Aliases", ())
                or any(host.get(field) for field in ("Binds", "Mounts", "PortBindings", "Devices", "CapAdd"))
                or binding.configuration_sha256
                != self._fingerprint(task_run_id, binding.target_url)
            ):
                raise ValueError("forwarder_configuration_mismatch")
        except Exception as exc:
            raise T6TargetNetworkError(
                "bind", "configuration_or_outcome_unknown", self._ids(binding)
            ) from exc

    @staticmethod
    def _ids(binding: TargetNetworkBinding) -> tuple[str, ...]:
        return (
            binding.private_network_id,
            binding.egress_network_id,
            binding.forwarder_id,
        )

    def destroy(self, task_run_id: str, binding: TargetNetworkBinding) -> None:
        self._task_id(task_run_id)
        ids = self._ids(binding)
        try:
            try:
                proxy = self.client.containers.get(binding.forwarder_id)
                proxy.reload()
            except NotFound:
                proxy = None
            if proxy is not None:
                labels = (proxy.attrs.get("Config") or {}).get("Labels") or {}
                if any(labels.get(k) != v for k, v in self._labels(task_run_id).items()):
                    raise ValueError("forwarder_ownership_mismatch")
                if proxy.status == "running":
                    if binding.forwarder_id in self._stop_requested:
                        raise ValueError("forwarder_stop_outcome_unknown")
                    self._stop_requested.add(binding.forwarder_id)
                    proxy.stop(timeout=2)
                    proxy.reload()
                if proxy.status not in {"created", "exited", "dead"}:
                    raise ValueError("forwarder_not_stopped")
                proxy.remove(force=False)
                try:
                    self.client.containers.get(binding.forwarder_id).reload()
                except NotFound:
                    pass
                else:
                    raise ValueError("forwarder_removal_unconfirmed")
            for resource_id in (binding.private_network_id, binding.egress_network_id):
                try:
                    network = self.client.networks.get(resource_id)
                except NotFound:
                    continue
                network.reload()
                labels = network.attrs.get("Labels") or {}
                if any(labels.get(k) != v for k, v in self._labels(task_run_id).items()):
                    raise ValueError("network_ownership_mismatch")
                network.remove()
                try:
                    self.client.networks.get(resource_id).reload()
                except NotFound:
                    pass
                else:
                    raise ValueError("network_removal_unconfirmed")
        except Exception as exc:
            raise T6TargetNetworkError("destroy", "outcome_unknown", ids, code=docker_error_code(exc)) from exc

    def cleanup_uncommitted(self, task_run_id: str) -> None:
        """Explicitly inspect a failed creation; only remove confirmed stopped resources."""
        self._task_id(task_run_id)
        try:
            external, private, proxy = self._discover(task_run_id)
            ids = self._owned_discovery(task_run_id, (external, private, proxy))
            if not ids:
                # An empty lookup cannot rule out a late Docker create response.
                raise ValueError("creation_may_still_complete")
            if proxy is not None:
                if proxy.status not in {"created", "exited", "dead"}:
                    raise ValueError("forwarder_stop_not_confirmed")
                proxy.remove(force=False)
            for network in (private, external):
                if network is not None:
                    network.remove()
            if any(self._discover(task_run_id)):
                raise ValueError("uncommitted_removal_unconfirmed")
        except Exception as exc:
            raise T6TargetNetworkError(
                "cleanup_uncommitted", "outcome_unknown", locals().get("ids", ())
            ) from exc
