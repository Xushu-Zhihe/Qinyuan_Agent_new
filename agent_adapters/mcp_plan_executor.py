"""Rolling MCP calls over the existing TaskRun, accounting and safe evidence."""

import asyncio
import hashlib
import json
import os
from time import monotonic
from dataclasses import asdict, replace
from pathlib import Path

from agent_core.actions import MCPCall
from agent_core.dual_llm_contracts import D3RecoveryRequired
from agent_core.operation_accounting import D4OperationStart, D4OperationCompletion
from agent_core.turn_modeling import digest
from qinyuan_core import get_logger


def _trace(event, **fields):
    """Localize repeated discovery; silent unless QINYUAN_DEBUG_TRACE is set."""
    import os
    if not os.environ.get("QINYUAN_DEBUG_TRACE"):
        return
    import sys
    print("[trace]", event,
          " ".join(f"{k}={v}" for k, v in fields.items()), file=sys.stderr, flush=True)


_DECORATIVE_SCHEMA_KEYS = frozenset({"description", "default", "title", "examples",
                                     "$comment", "deprecated"})


def _trim_schema(node):
    """Keep validation-relevant schema keys; drop prose and presentation only."""
    if isinstance(node, dict):
        return {k: _trim_schema(v) for k, v in node.items()
                if k not in _DECORATIVE_SCHEMA_KEYS}
    if isinstance(node, list):
        return [_trim_schema(v) for v in node]
    return node


def compact_tools(tools):
    """Shrink the catalog sent to the model without changing what it validates.

    ``input_schema`` stays in place because the output contract derives argument
    validation from it; only prose-level keys are removed and the tool blurb is
    shortened.
    """
    compact = []
    for spec in tools or ():
        compact.append({
            "qualified_name": spec.get("qualified_name"),
            "description": (spec.get("description") or "")[:60],
            "tags": list(spec.get("tags") or ()),
            "input_schema": _trim_schema(spec.get("input_schema") or {}),
        })
    return compact


def _is_operation_id(value):
    """True only for the canonical D4 operation id shape."""
    from uuid import UUID
    if not isinstance(value, str):
        return False
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return str(parsed) == value.lower()


class MCPPlanExecutor:
    """One invocation per inner decision; tool success never creates a Fact."""

    def __init__(self, app, manager=None, *, public_web=None):
        self.app, self.manager = app, manager
        self.public_web = public_web
        self.client = None
        self.service = getattr(manager, 'sandbox_service', None)
        self.tools = []
        from agent_adapters.http_experiments import HttpExperiments
        self.experiments = HttpExperiments(self)
        self._log = get_logger("exec")

    @property
    def evidence(self):
        return self.app.output_boundary.evidence

    @staticmethod
    def registry_path():
        return Path(__file__).resolve().parents[1] / "MCPs" / "Mcp_client" / "registry.json"

    def authorize(self, task_id):
        """Freeze one capability binding for new runs; old runs have no marker."""
        self.evidence.save("mcp-registry", task_id, {
            "registry_sha256": hashlib.sha256(self.registry_path().read_bytes()).hexdigest(),
            **({"public_web": self.public_web.binding} if self.public_web else {})
        })

    def enabled(self, task_id):
        return self.evidence.read("mcp-registry", task_id) is not None

    async def open(self):
        _trace("mcp.open.enter", instance=id(self) % 100000,
               has_client=self.client is not None, has_manager=self.manager is not None)
        if self.client is not None:
            return
        task = self.app.state.task_run
        frozen = self.evidence.read("mcp-registry", task.task_run_id)
        expected = hashlib.sha256(self.registry_path().read_bytes()).hexdigest()
        if frozen is None or frozen["registry_sha256"] != expected:
            raise D3RecoveryRequired("mcp.registry_binding_changed")
        retrieval = frozen.get("public_web")
        if retrieval is not None and (self.public_web is None or self.public_web.binding != retrieval):
            raise D3RecoveryRequired("retrieval.configuration_unavailable")
        if self.manager is None:
            from MCPs.Mcp_client.client import MCPManager, load_registry
            from agent_adapters.sandbox_adapter import D8SandboxAdapter
            workspace = self.app.workspace.sandbox
            service = self.service = D8SandboxAdapter(workspace.sandbox, task.task_run_id, task.sandbox_id)
            registry = load_registry(self.registry_path())
            project = str(self.registry_path().parents[2])
            registry = replace(registry, servers={
                name: replace(server, env={**server.env,
                    "PYTHONPATH": project + os.pathsep + server.env.get("PYTHONPATH", "")})
                for name, server in registry.servers.items()})
            self.manager = MCPManager(
                registry, service,
                required_servers=[s.name for s in registry.servers.values() if s.enabled])
        try:
            await self.manager.start_all()
            names = [m.qualified_name for m in self.manager.list_registered_tools()]
            self.client = self.manager.bind_agent(task.task_run_id, allowed_tools=names)
            self.tools = self.client.list_tool_specs()
            if self.experiments.enabled:
                from agent_adapters.http_experiments import SPEC
                self.tools = [*self.tools, SPEC]
            if retrieval is not None:
                from agent_adapters.public_web import SPECS
                self.tools = [*self.tools, *SPECS]
            if not self.tools:
                raise D3RecoveryRequired("mcp.no_tools_available")
            # Drift is rejected on recovery; no silent replacement of a frozen catalog.
            self.evidence.save("mcp-catalog", task.task_run_id, {"tools": self.tools})
        except BaseException as exc:
            await self.manager.stop_all()
            self.client = None
            if isinstance(exc, Exception) and not isinstance(exc, D3RecoveryRequired):
                self._log.error("mcp.discovery_failed", run=task.task_run_id,
                                code="mcp.discovery_failed", category="dependency",
                                error_type=type(exc).__name__)
                raise D3RecoveryRequired("mcp.discovery_failed") from None
            raise
        self._log.info("mcp.connected", run=task.task_run_id, tool_count=len(self.tools))

    async def close(self):
        _trace("mcp.close", instance=id(self) % 100000)
        if self.manager is not None:
            await self.manager.stop_all()
        self.client = None

    def records(self, intent_id=None):
        prefix = "mcp/" + (intent_id + "/" if intent_id is not None else "")
        return tuple(r for r in self.app.store.list_d4_operations(self.app.state.task_run.task_run_id)
                     if r.start.resource_ref.startswith(prefix))

    def count(self, intent_id):
        return sum(r.completion is not None for r in self.records(intent_id))

    def validate(self, request, call):
        visible = json.loads(request.history_context).get("mcp_tools", [])
        if call.tool_name not in {t["qualified_name"] for t in visible}:
            raise ValueError("mcp.tool_not_available")
        from agent_adapters.http_experiments import TOOL
        if call.tool_name == TOOL:
            self.experiments.validate(call)
        from agent_adapters.public_web import NAMES
        if call.tool_name in NAMES:
            if self.public_web is None:
                raise ValueError("retrieval.configuration_unavailable")
            self.public_web.validate(call)

    def feedback(self, operation_id):
        # An application reference is not always a D4 operation id: reviews use
        # "d3-review:<id>" and answer decisions use "d9_no_candidate". Those simply
        # carry no MCP tool result, so they must not be treated as a bad UUID.
        if not _is_operation_id(operation_id):
            return None
        session = self.experiments.result(operation_id)
        if session is not None:
            return session
        result = self.evidence.read("mcp-result", operation_id)
        if result is None:
            return None
        operation = self.app.store.load_d4_operation(operation_id)
        claim = self.evidence.read("mcp-call", operation_id)
        if (operation is None or claim is None
                or operation.start.context_sha256 != digest(claim)
                or result["request_sha256"] != digest(claim)):
            raise D3RecoveryRequired("mcp.result_binding_changed")
        if 'experiment_request' in claim:
            self.experiments.validate_receipt(operation,claim,result)
        from agent_adapters.public_web import IDENTITY, NAMES, preview
        if (claim['call']['tool_name'] in NAMES and operation.completion is not None
                and (operation.completion.evidence_sha256 != digest(result)
                     or operation.completion.evidence_ref != 'mcp-result/' + operation_id)):
            raise D3RecoveryRequired("mcp.result_binding_changed")
        payload = result["payload"]
        return preview(payload, operation_id, claim) if payload.get("identity") == IDENTITY else payload

    def _apply(self, operation, claim, result):
        if operation.start.context_sha256 != digest(claim) or result["request_sha256"] != digest(claim):
            raise D3RecoveryRequired("mcp.result_binding_changed")
        payload = result["payload"]
        if payload["status"] not in {"completed", "failed", "rejected"}:
            raise D3RecoveryRequired("mcp.outcome_unknown")
        if 'experiment_request' in claim:
            self.experiments.validate_receipt(operation,claim,result)
        completion = D4OperationCompletion(
            operation.start.operation_id, result["finished_at"],
            "completed" if payload["status"] == "completed" else
            "rejected" if payload["status"] == "rejected" else "failed",
            "mcp-result/" + operation.start.operation_id, digest(result))
        if operation.completion is None:
            self.app.store.complete_d4_operation(completion)
        elif operation.completion != completion:
            raise D3RecoveryRequired("mcp.completion_binding_changed")
        if 'experiment_request' not in claim:
            if self.app.store.load_stage_run_config(claim["task_run_id"]) is not None:
                self.app.store.mark_d4_operation_applied(claim["model_request_id"], operation.start.operation_id)
            else:
                self.app.store.mark_d3_operation_applied(claim["model_request_id"], operation.start.operation_id)
        if payload["status"] == "completed":
            self.app.store.mark_d4_operation_applied(operation.start.operation_id, operation.start.operation_id)

    def reconcile(self):
        # Repair only persisted known results. An unfinished invocation is never replayed.
        for operation in self.records():
            claim = self.evidence.read("mcp-call", operation.start.operation_id)
            result = self.evidence.read("mcp-result", operation.start.operation_id)
            if result is not None:
                if claim is None:
                    raise D3RecoveryRequired("mcp.call_evidence_missing")
                try:
                    self._apply(operation, claim, result)
                except Exception as error:
                    if 'experiment_request' in claim:
                        self.experiments.record_failure(operation.start.operation_id,claim,result,'receipt_apply',error)
                    raise
        self.experiments.reconcile()

    async def execute(self, request_id, intent_id, call, token):
        from MCPs.Mcp_client.client import MCPCommandPlanError
        from Sandbox.service import SandboxPolicyError
        from Sandbox.tool_download import ToolDownloadError, ToolDownloadUnknown
        from agent_adapters.http_experiments import TOOL
        if call.tool_name == TOOL:
            await self.experiments.run(request_id,intent_id,call,token)
            return
        self.app._check_dispatch()
        from agent_adapters.public_web import NAMES
        public = call.tool_name in NAMES
        if public:
            frozen = self.evidence.read("mcp-registry", self.app.state.task_run.task_run_id)
            if (self.public_web is None or frozen is None
                    or frozen.get("public_web") != self.public_web.binding):
                raise D3RecoveryRequired("retrieval.configuration_unavailable")
            self.public_web.validate(call)
        identity = self.app._id("mcp/" + request_id)
        claim = {"task_run_id": self.app.state.task_run.task_run_id,
                 "intent_id": intent_id, "model_request_id": request_id, "call": asdict(call)}
        prior = self.app.store.load_d4_operation(identity)
        if prior is not None:
            result = self.evidence.read("mcp-result", identity)
            if result is None:
                raise D3RecoveryRequired("mcp.outcome_unknown")
            self._apply(prior, claim, result)
            return
        stage_config = self.app.store.load_stage_run_config(claim["task_run_id"])
        stage_turn = next((t for t in self.app.store.list_stage_turns(claim["task_run_id"])
            if any(r.road_id == intent_id for r in t.roads)), None) if stage_config is not None else None
        if stage_config is not None and stage_turn is None:
            raise D3RecoveryRequired("stage.tool_road_not_running")
        if stage_turn is not None:
            from agent_core.domain import IntentStatus
            if not any(r.road_id == intent_id and r.status is IntentStatus.RUNNING for r in stage_turn.roads):
                raise D3RecoveryRequired("stage.tool_road_not_running")
            from types import SimpleNamespace
            intent = SimpleNamespace(turn_id=stage_turn.turn_id)
        else:
            intent = next(i for i in self.app.state.intents if i.intent_id == intent_id)
        allowed, maximum = self.app.validator.action_limits(
            claim["task_run_id"], intent.turn_id, intent_id)
        # Reuse the existing execution permission; completion-only reviews forbid tools.
        if "probe" not in allowed:
            raise D3RecoveryRequired("mcp.execution_blocked_by_review")
        remaining = int(self.app.state.task_run.deadline_at - self.app.clock())
        if remaining < 1:
            raise D3RecoveryRequired("mcp.deadline_insufficient")
        if not public and self.service is not None:
            self.service.maximum_timeout_seconds = min(maximum, remaining)
        self.evidence.save("mcp-call", identity, claim)
        self.app.store.begin_d4_operation(D4OperationStart(
            identity, claim["task_run_id"], "tool_dispatch", self.app.clock(),
            self.app.state.latest_checkpoint.sequence, digest(claim),
            "mcp/" + intent_id + "/" + request_id,
            self.app.config.d4_budget.budget_policy_ref))
        self._log.info("mcp.call_started", run=claim["task_run_id"], intent=intent_id,
                       exec=identity, tool=call.tool_name)
        # 公共检索要把"当前目标"作为禁止命中的地址传下去，来源是已授权的 origin。
        authorization = self.app.config.web_authorization
        target = (authorization.allowed_origin if authorization else
                  self.app.config.tcp_authorization.allowed_endpoint if self.app.config.tcp_authorization else None)
        async def invoke():
            if public:
                return await self.public_web.call(call, min(maximum, remaining), token, target)
            if call.tool_name == "download.install_tool":
                from Sandbox.tool_download import ToolDownload
                from agent_adapters.sandbox_adapter import D8SandboxAdapter
                if not isinstance(self.service, D8SandboxAdapter):
                    raise SandboxPolicyError("download.task_run_preparation_unavailable")
                from MCPs.Download_Mcp.server import install_tool
                try:installation = install_tool(**dict(call.arguments))
                except (TypeError, ValueError):raise SandboxPolicyError("download.arguments_invalid") from None
                preparation_limit = min(maximum, remaining, installation["sandbox_command"]["timeout_seconds"])
                preparation_started = monotonic()
                self.service.prepared_download = None
                preparation = ToolDownload(self.service.sandbox, self.app.state.task_run, identity, self.evidence)
                # Reuse the latest successful preparation for the exact same tool
                # selection in this TaskRun. A later failure invalidates that shortcut.
                prepared = None
                for prior_call in reversed(self.records()):
                    if prior_call.start.operation_id == identity:continue
                    old = self.evidence.read("mcp-call", prior_call.start.operation_id)
                    if old is None or old.get("call") != asdict(call):continue
                    outcome = self.evidence.read("mcp-result", prior_call.start.operation_id)
                    lease = self.evidence.read("mcp-download", prior_call.start.operation_id)
                    if (outcome and outcome["payload"]["status"] == "completed" and lease
                            and lease.get("state") == "prepared" and lease.get("cleanup") != "unknown"):
                        prepared = lease.get("prepared")
                        if prepared is not None:
                            self.evidence.save("mcp-download", identity,
                                {**lease, "operation_id": identity, "reused_from": prior_call.start.operation_id})
                    break
                if prepared is None:
                    prepared = await preparation.run(dict(call.arguments), preparation_limit)
                remaining_install = int(min(self.app.state.task_run.deadline_at - self.app.clock(),
                    preparation_limit - (monotonic() - preparation_started)))
                if token.is_cancelled or remaining_install < 1:
                    raise ToolDownloadError("download.stopped_before_install")
                self.service.maximum_timeout_seconds = min(maximum, remaining_install)
                self.service.prepared_download = (digest(dict(call.arguments)), prepared)
            return await self.client.call_tool(call.tool_name, dict(call.arguments))
        pending = asyncio.create_task(invoke())
        try:
            while not pending.done():
                await asyncio.wait({pending}, timeout=0.25)
                if token.is_cancelled or self.app.clock() >= self.app.state.task_run.deadline_at:
                    pending.cancel()
                    if public:
                        # Read-only transport confirms its child has stopped before returning.
                        await pending
                        break
                    try:
                        await pending
                    except ToolDownloadError:
                        break  # Confirmed preparation stop; no installer was dispatched.
                    except BaseException:
                        pass
                    raise D3RecoveryRequired("mcp.outcome_unknown")
            response = pending.result()
            if not public and (response.agent_id != claim["task_run_id"]
                    or response.sandbox_id != self.app.state.task_run.sandbox_id
                    or response.qualified_name != call.tool_name):
                raise D3RecoveryRequired("mcp.execution_binding_changed")
            payload = self.app.redactor.redact_value(response if public else response.to_tool_payload())
        except ToolDownloadError as exc:
            payload = {"ok": False, "status": "failed", "tool": call.tool_name,
                       "code": str(exc), "category": ("integrity" if "sha256" in str(exc) else
                           "authorization" if "not_allowed" in str(exc) or "non_public" in str(exc) else "dependency")}
        except (MCPCommandPlanError, SandboxPolicyError):
            payload = {"ok": False, "status": "rejected",
                       "tool": call.tool_name, "code": "mcp.command_rejected"}
        except BaseException as exc:
            if not pending.done():
                pending.cancel()
                try:
                    await pending
                except BaseException:
                    pass
            code = str(exc) if isinstance(exc, ToolDownloadUnknown) else "retrieval.outcome_unknown" if public else "mcp.outcome_unknown"
            if public and isinstance(exc, D3RecoveryRequired) and exc.args == ("retrieval.worker_stop_unconfirmed",):
                code = "retrieval.worker_stop_unconfirmed"
            self._log.error("mcp.call_unknown", run=claim["task_run_id"], intent=intent_id,
                            exec=identity, tool=call.tool_name, code=code, category="unknown",
                            error_type=type(exc).__name__)
            raise D3RecoveryRequired(code) from None
        result = {"request_sha256": digest(claim), "payload": payload, "finished_at": self.app.clock()}
        self.evidence.save("mcp-result", identity, result)
        self._apply(self.app.store.load_d4_operation(identity), claim, result)
        self._log.info("mcp.call_finished", run=claim["task_run_id"], intent=intent_id,
                       exec=identity, status=payload["status"],
                       code=payload.get("code"), category=payload.get("category"),
                       http_status=payload.get("http_status"), exit_code=payload.get("exit_code"))
