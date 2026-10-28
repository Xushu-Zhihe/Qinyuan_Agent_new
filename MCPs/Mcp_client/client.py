from __future__ import annotations

import sys
import asyncio
import json
import os
import threading
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from Sandbox.models import CommandRequest
from Sandbox.service import SandboxService

class MCPClientError(RuntimeError):
    """MCP客户端、协议或命令计划错误"""

class MCPServerNotReadyError(MCPClientError):
    """MCP Server尚未启动或已经停止"""

class MCPCommandPlanError(MCPClientError):
    """MCP Server返回了非法的沙盒命令计划"""

class MCPCommandExecutionTimeout(MCPClientError):
    """沙盒命令超过宿主机强制期限"""

@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    """单个 MCP Server的启动和权限配置"""

    name: str
    transport: str = "stdio"
    command: str | None = None
    args: tuple[str,...] = ()
    cwd: Path | None = None
    #env: dict[str,str] = field(default=dict)
    env: dict[str,str] = field(default_factory=dict)
    url: str | None = None
    tags: frozenset[str] = field(default_factory=frozenset)
    sandbox_tools: frozenset[str] = field(default_factory=frozenset)
    enabled: bool = True

@dataclass(frozen=True, slots=True)
class MCPRegistryConfig:
    servers: dict[str, MCPServerConfig]

@dataclass(frozen=True, slots=True)
class MCPToolMeta:
    server_name: str
    tool_name: str
    qualified_name: str
    description: str
    input_schema: dict[str,Any]
    tags: frozenset[str]

@dataclass(slots=True)
class MCPServerRuntime:
    config: MCPServerConfig
    exit_stack: AsyncExitStack = field(default_factory=AsyncExitStack)
    session: ClientSession | None = None
    tools: dict[str, MCPToolMeta] = field(default_factory=dict)
    status: str = "stopped"
    last_error: str | None = None

@dataclass(frozen=True,slots=True)
class MCPExecutionResult:
    """
    MCP命令计划在 Agent专属沙盒中的实际执行结果

    agent_id和 sandbox_id保留在控制平面中用于审计
    to_tool_payload() 不会把它们暴露给 LLM
    """

    agent_id: str
    sandbox_id: str
    qualified_name: str
    executable: str
    arguments: tuple[str,...]
    status: str
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    def to_tool_payload(self) -> dict[str,Any]:
        """生成可以安全返回给 Agent的结果"""

        return{
            "ok": self.status == "completed",
            "status": self.status,
            "tool": self.qualified_name,
            "command":[self.executable,*self.arguments],
            "exit_code":self.exit_code,
            "stdout":self.stdout,
            "stderr":self.stderr,
            "stdout_truncated":self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
        }

def load_registry(path: str | Path) -> MCPRegistryConfig:
    """
    读取 MCP Server注册表

    cwd相对于 registry.json所在目录解析，因此项目移动后不需要修改绝对路径
    """
    
    registry_path = Path(path).resolve()
    raw = json.loads(registry_path.read_text(encoding="utf-8"))
    raw_servers = raw.get("mcpServers")

    if not isinstance(raw_servers,dict):
        raise ValueError("registry must contain an object named 'mcpServers'")

    servers: dict[str, MCPServerConfig] = {}

    for name, item in raw_servers.items():
        if not isinstance(name,str) or not name.strip():
            raise ValueError("MCP server name must be a non-empty string")

        if not isinstance(item,dict):
            raise ValueError(f"server config for {name!r} must be an object")

        transport = item.get("transport","stdio")
        command = item.get("command")
        url = item.get("url")
        enabled = item.get("enabled",True)

        args = _string_list(item,"args")
        tags = frozenset(_string_list(item,"tags"))
        sandbox_tools = frozenset(_string_list(item,"sandboxTools"))

        env = item.get("env",{})
        if not isinstance(env,dict) or not all(
            isinstance(key,str) and isinstance(value,str)
            for key, value in env.items()
        ):
            raise ValueError(f"{name!r}.env must be an object of strings")

        cwd: Path | None = None
        cwd_value = item.get("cwd")
        if cwd_value is not None:
            if not isinstance(cwd_value, str) or not cwd_value.strip():
                raise ValueError(f"{name!r}.cwd must be a non-empty string")

            cwd = Path(cwd_value)
            if not cwd.is_absolute():
                cwd = registry_path.parent / cwd
            cwd = cwd.resolve()

        if transport == "stdio":
            if not isinstance(command,str) or not command.strip():
                raise ValueError(
                    f"{name!r}.command is required for stdio transport"
                )
        elif transport == "streamable_http":
            if not isinstance(url,str) or not url.strip():
                raise ValueError(
                    f"{name!r}.url is required for streamable_http transport"
                )            
        else:
            raise ValueError(
                f"{name!r}.transport must be 'stdio' or 'streamable_http'"
            )

        servers[name] = MCPServerConfig(
            name = name,
            transport = transport,
            command = command,
            args = tuple(args),
            cwd = cwd,
            env = dict(env),
            url=url,
            tags=tags,
            sandbox_tools=sandbox_tools,
            enabled=enabled,
        )

    return MCPRegistryConfig(servers=servers)

def _string_list(item:dict[str,Any], field_name:str) -> list[str]:
    value = item.get(field_name, [])

    if not isinstance(value, list) or not all(
        isinstance(element, str) and element.strip()
        for element in value
    ):
        raise ValueError(f"{field_name} must be a list of non-empty strings")

    return value


def select_registry_servers(
    registry: MCPRegistryConfig,
    allowed_servers: Iterable[str] | None,
) -> MCPRegistryConfig:
    """Restrict the registry without enabling unknown or disabled servers."""

    if allowed_servers is None:
        return registry
    requested = frozenset(item.strip() for item in allowed_servers if item.strip())
    if not requested:
        raise ValueError("MCP server allowlist must not be empty")
    unknown = requested.difference(registry.servers)
    if unknown:
        raise ValueError(f"unknown MCP servers: {sorted(unknown)}")
    disabled = {
        name for name in requested if not registry.servers[name].enabled
    }
    if disabled:
        raise ValueError(f"disabled MCP servers cannot be enabled: {sorted(disabled)}")
    return MCPRegistryConfig(
        servers={name: registry.servers[name] for name in sorted(requested)}
    )


class MCPManager:
    """
    MCP 总管：
    1.启动和关闭 MCP Server     
    2.发现 MCP Tools
    3.读取并校验 sandbox_command       
    4.将命令绑定到内部 agent_id
    5.通过 SandboxService在 Agent专属容器中执行
    """

    def __init__(
        self,
        registry: MCPRegistryConfig,
        sandbox_service: SandboxService,
        *,
        mcp_call_timeout_seconds: float = 10.0,
        startup_timeout_seconds: float = 30.0,
        shutdown_timeout_seconds: float = 10.0,
        sandbox_execution_grace_seconds: float = 10.0,
        sandbox_interrupt_timeout_seconds: float = 10.0,
        output_character_limit: int = 20_000,
        audit_path: str | Path | None = None,
        bootstrap_marker_path: str | Path | None = None,
        required_servers: Iterable[str] | None = None,
    ) -> None:
        if mcp_call_timeout_seconds <= 0:
            raise ValueError("mcp_call_timeout_seconds must be positive")
        if startup_timeout_seconds <= 0:
            raise ValueError("startup_timeout_seconds must be positive")
        if shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown_timeout_seconds must be positive")
        if sandbox_execution_grace_seconds <= 0:
            raise ValueError("sandbox_execution_grace_seconds must be positive")
        if sandbox_interrupt_timeout_seconds <= 0:
            raise ValueError("sandbox_interrupt_timeout_seconds must be positive")

        if output_character_limit < 1_000:
            raise ValueError("output_character_limit must be at least 1000")

        self.registry = registry
        self.sandbox_service = sandbox_service
        self.mcp_call_timeout_seconds = mcp_call_timeout_seconds
        self.startup_timeout_seconds = startup_timeout_seconds
        self.shutdown_timeout_seconds = shutdown_timeout_seconds
        self.sandbox_execution_grace_seconds = sandbox_execution_grace_seconds
        self.sandbox_interrupt_timeout_seconds = sandbox_interrupt_timeout_seconds
        self.output_character_limit = output_character_limit
        self.audit_path = Path(audit_path).resolve() if audit_path is not None else None
        self.bootstrap_marker_path = (
            Path(bootstrap_marker_path).resolve()
            if bootstrap_marker_path is not None
            else None
        )

        requested_required = frozenset(
            item.strip() for item in (required_servers or ()) if item.strip()
        )
        unknown_required = requested_required.difference(registry.servers)
        if unknown_required:
            raise ValueError(
                f"unknown required MCP servers: {sorted(unknown_required)}"
            )
        disabled_required = {
            name for name in requested_required
            if not registry.servers[name].enabled
        }
        if disabled_required:
            raise ValueError(
                "required MCP servers cannot be disabled: "
                f"{sorted(disabled_required)}"
            )
        self.required_servers = requested_required

        self.runtimes: dict[str, MCPServerRuntime] = {}
        self.tool_index: dict[str, MCPToolMeta] = {}
        self.startup_failures: dict[str, str] = {}

        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._event_loop_thread_id: int | None = None
        self._audit_lock = asyncio.Lock()

    @staticmethod
    def _append_audit_line(path: Path, payload: dict[str, Any]) -> None:
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
            handle.flush()

    async def _write_audit(self, payload: dict[str, Any]) -> None:
        if self.audit_path is None:
            return
        record = {"timestamp": time.time(), **payload}
        async with self._audit_lock:
            await asyncio.to_thread(
                self._append_audit_line,
                self.audit_path,
                record,
            )

    async def start_all(self) -> None:
        """启动注册表中所有 enabled = true的 MCP Server"""

        if self.runtimes:
            return 

        self._event_loop = asyncio.get_running_loop()
        self._event_loop_thread_id = threading.get_ident()

        failures: dict[str, str] = {}
        for config in self.registry.servers.values():
            if not config.enabled:
                continue
            try:
                await self._start_server(config)
            except Exception as exc:
                failures[config.name] = str(exc)
        self.startup_failures = failures

        required_failures = {
            name: failures.get(name, f"MCP server {name!r} did not become ready")
            for name in self.required_servers
            if name not in self.runtimes
        }
        if required_failures or not self.runtimes:
            try:
                await self.stop_all()
            except Exception as exc:
                failures["cleanup"] = str(exc)
            details = {**failures, **required_failures}
            detail = "; ".join(
                f"{name}: {message}" for name, message in details.items()
            )
            raise MCPClientError(
                "no usable MCP servers are available" if not detail
                else f"MCP startup failed: {detail}"
            )

    async def _start_server(self,config: MCPServerConfig) -> None:
        if config.name in self.runtimes:
            return 

        runtime = MCPServerRuntime(config=config)
        runtime.status = "starting"

        try:
            if config.transport == "stdio":
                if config.cwd is not None and not config.cwd.is_dir():
                    raise MCPClientError(
                        f"MCP server cwd does not exist: {config.cwd}"
                    )

                command = config.command or ""

                # 使用当前正在运行 MCPManager 的 Python 解释器。
                # 避免写死 .venv/Scripts/python.exe，也避免创建 MCP 独立 uv 环境。
                if command == "{python}":
                    command = sys.executable

                environment = {
                    key: os.environ.get(key, value)
                    for key, value in config.env.items()
                }
                parameters = StdioServerParameters(
                    command=command,
                    args=list(config.args),
                    env=environment,
                    cwd=config.cwd,
                )

                read_stream,write_stream =(
                    await asyncio.wait_for(
                        runtime.exit_stack.enter_async_context(
                            stdio_client(parameters)
                        ),
                        timeout=self.startup_timeout_seconds,
                    )
                )
            else:
                read_stream,write_stream,_ = (
                    await asyncio.wait_for(
                        runtime.exit_stack.enter_async_context(
                            streamable_http_client(config.url or "")
                        ),
                        timeout=self.startup_timeout_seconds,
                    )
                )

            session = await asyncio.wait_for(
                runtime.exit_stack.enter_async_context(
                    ClientSession(read_stream,write_stream)
                ),
                timeout=self.startup_timeout_seconds,
            )

            await asyncio.wait_for(
                session.initialize(),
                timeout=self.startup_timeout_seconds,
            )

            runtime.session = session
            runtime.status = "ready"
            self.runtimes[config.name] = runtime

            await asyncio.wait_for(
                self.refresh_tools(config.name),
                timeout=self.startup_timeout_seconds,
            )

        except Exception as exc:
            runtime.status = "failed"
            runtime.last_error = str(exc)
            self.runtimes.pop(config.name,None) 
            try:
                await asyncio.wait_for(
                    runtime.exit_stack.aclose(),
                    timeout=self.shutdown_timeout_seconds,
                )
            except asyncio.TimeoutError:
                pass
            raise MCPClientError(
                f"failed to start MCP server {config.name!r}: {exc}"
            ) from exc

    async def stop_all(self) -> None:
        """尽最大可能关闭所有的 MCP Server"""

        errors:list[str] = []

        for runtime in reversed(list(self.runtimes.values())):
            try:
                await asyncio.wait_for(
                    runtime.exit_stack.aclose(),
                    timeout=self.shutdown_timeout_seconds,
                )
            except asyncio.TimeoutError:
                errors.append(
                    f"{runtime.config.name}: shutdown timed out after "
                    f"{self.shutdown_timeout_seconds:g}s"
                )
            except Exception as exc:
                errors.append(f"{runtime.config.name}: {exc}")
            finally:
                runtime.status = "stopped"
                runtime.session = None

        self.runtimes.clear()
        self.tool_index.clear()
        self.startup_failures.clear()
        self._event_loop = None
        self._event_loop_thread_id = None

        if errors:
            raise MCPClientError(
                "one or more MCP servers failed to close: "
                + "; ".join(errors)
            )
        
    async def refresh_tools(self,server_name: str) -> None:
        """重新读取指定 MCP Server的工具清单"""

        runtime = self.runtimes.get(server_name)
        if runtime is None or runtime.session is None:
            raise MCPServerNotReadyError(
                f"MCP server {server_name!r} is not ready"
            )

        result = await asyncio.wait_for(
            runtime.session.list_tools(),
            timeout=self.startup_timeout_seconds,
        )

        for old_name in runtime.tools:
            self.tool_index.pop(old_name,None)

        runtime.tools.clear()

        for tool in result.tools:
            qualified_name = f"{server_name}.{tool.name}"

            meta = MCPToolMeta(
                server_name=server_name,
                tool_name=tool.name,
                qualified_name=qualified_name,
                description=tool.description or "",
                input_schema=tool.inputSchema or {},
                tags=runtime.config.tags,
            )

            runtime.tools[qualified_name] = meta
            self.tool_index[qualified_name] = meta

    def list_registered_tools(self) -> list[MCPToolMeta]:
        return sorted(
            self.tool_index.values(),
            key=lambda item: item.qualified_name,
        )

    def bind_agent(
        self,
        agent_id: str,
        *,
        allowed_tools: Iterable[str],
    ) -> AgentMCPClient:
        """
        创建永久绑定 agent_id的客户端

        agent_id不会出现在 LLM的工具参数中
        """

        normalized_agent_id = agent_id.strip()
        if not normalized_agent_id:
            raise ValueError("agent_id cannot be empty")

        if self.sandbox_service.get_session(normalized_agent_id) is None:
            raise MCPClientError(
                f"Agent {normalized_agent_id!r} does not have a sandbox"
            )

        allowed = frozenset(allowed_tools)
        unknown = allowed.difference(self.tool_index)

        if unknown:
            raise MCPClientError(
                f"unknown MCP tools: {sorted(unknown)}"
            )

        return AgentMCPClient(
            manager=self,
            agent_id=normalized_agent_id,
            allowed_tools=allowed,
        )

    async def _execute_bound_tool(
        self,
        *,
        agent_id: str,
        qualified_name: str,
        arguments: dict[str,Any],
    ) -> MCPExecutionResult:
        meta = self.tool_index.get(qualified_name)
        if meta is None:
            raise MCPClientError(f"unknown MCP tool:{qualified_name}")

        runtime = self.runtimes.get(meta.server_name)
        if runtime is None or runtime.session is None:
            raise MCPServerNotReadyError(
                f"MCP server {meta.server_name!r} is not ready"
            )

        if not isinstance(arguments,dict) or not all(
            isinstance(key,str) for key in arguments
        ):
            raise MCPClientError("MCP tool arguments must be an object")

        try:
            mcp_result = await asyncio.wait_for(
                runtime.session.call_tool(
                    meta.tool_name,
                    arguments=arguments,
                ),
                timeout=self.mcp_call_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise MCPClientError(
                f"MCP server {meta.server_name!r} did not produce "
                "a command plan in time"
            ) from exc

        request, bootstrap_seconds = self._extract_command_plan(mcp_result)
        if meta.server_name == "download":
            prepare = getattr(self.sandbox_service, "prepared_tool_command", None)
            if prepare is not None:
                request = prepare(qualified_name, arguments, request)

        if bootstrap_seconds and meta.server_name != "download":
            raise MCPCommandPlanError(
                "only the download MCP server may request tool bootstrap compensation"
            )

        if request.tool not in runtime.config.sandbox_tools:
            raise MCPCommandPlanError(
                f"MCP server {meta.server_name!r} is not allowed to "
                f"request executable {request.tool!r}"
            )

        audit_base = {
            "agent_id": agent_id,
            "tool": qualified_name,
            "tool_arguments": arguments,
            "executable": request.tool,
            "arguments": list(request.arguments),
            "timeout_seconds": request.timeout_seconds,
        }
        if bootstrap_seconds:
            self._write_bootstrap_marker(
                qualified_name=qualified_name,
                compensate_seconds=bootstrap_seconds,
            )
        await self._write_audit({"event": "started", **audit_base})
        execution_task = asyncio.create_task(
            asyncio.to_thread(
                self.sandbox_service.execute,
                agent_id,
                request,
            )
        )
        host_timeout = request.timeout_seconds + self.sandbox_execution_grace_seconds
        try:
            result = await asyncio.wait_for(
                asyncio.shield(execution_task),
                timeout=host_timeout,
            )
        except asyncio.TimeoutError as exc:
            interrupt_error = await self._interrupt_sandbox_execution(
                agent_id,
                execution_task,
            )
            message = (
                "sandbox command exceeded host timeout of "
                f"{host_timeout:g}s and was interrupted"
            )
            if interrupt_error is not None:
                message += f"; sandbox interrupt failed: {interrupt_error}"
            await self._write_audit(
                {
                    "event": "failed",
                    **audit_base,
                    "error": "SandboxCommandTimeout",
                    "message": message,
                }
            )
            raise MCPCommandExecutionTimeout(message) from exc
        except asyncio.CancelledError:
            interrupt_error = await asyncio.shield(
                self._interrupt_sandbox_execution(agent_id, execution_task)
            )
            message = "sandbox command was cancelled and interrupted"
            if interrupt_error is not None:
                message += f"; sandbox interrupt failed: {interrupt_error}"
            await asyncio.shield(
                self._write_audit(
                    {
                        "event": "failed",
                        **audit_base,
                        "error": "CancelledError",
                        "message": message,
                    }
                )
            )
            raise
        except Exception as exc:
            await self._write_audit(
                {
                    "event": "failed",
                    **audit_base,
                    "error": type(exc).__name__,
                    "message": str(exc),
                }
            )
            raise

        # stdout,stdout_truncated = self._truncate_output(result.stdout)
        # stderr,stderr_truncated = self._truncate_output(result.stderr)
        stdout, character_stdout_truncated = self._truncate_output(
            result.stdout
        )
        stderr, character_stderr_truncated = self._truncate_output(
            result.stderr
        )

        # Runtime 可能按字节截断，MCPManager 还可能按字符数再次截断。
        stdout_truncated = (
            result.stdout_truncated
            or character_stdout_truncated
        )
        stderr_truncated = (
            result.stderr_truncated
            or character_stderr_truncated
        )

        if result.timed_out:
            status = "outcome_unknown"
        elif result.exit_code == 0:
            status = "completed"
        else:
            status = "failed"

        execution_result = MCPExecutionResult(
            agent_id=agent_id,
            sandbox_id=result.sandbox_id,
            qualified_name=qualified_name,
            executable=request.tool,
            arguments=request.arguments,
            status=status,
            exit_code=result.exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=result.timed_out,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )
        await self._write_audit(
            {
                "event": "finished",
                **audit_base,
                "sandbox_id": result.sandbox_id,
                "status": status,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
            }
        )
        return execution_result

    async def _interrupt_sandbox_execution(
        self,
        agent_id: str,
        execution_task: asyncio.Task[Any],
    ) -> str | None:
        interrupt_error: str | None = None
        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    self.sandbox_service.interrupt_for_agent,
                    agent_id,
                ),
                timeout=self.sandbox_interrupt_timeout_seconds,
            )
        except Exception as exc:
            interrupt_error = f"{type(exc).__name__}: {exc}"

        try:
            await asyncio.wait_for(
                asyncio.shield(execution_task),
                timeout=self.sandbox_interrupt_timeout_seconds,
            )
        except asyncio.TimeoutError:
            # The container was stopped but is deliberately not removed while
            # Docker may still own the exec stream. Do not extend the provider
            # timeout while waiting for a broken client-side stream to close.
            execution_task.cancel()
        except Exception:
            # Stopping the container normally makes the blocked execution fail;
            # awaiting it here retrieves that expected exception.
            pass
        return interrupt_error

    def _execute_bound_tool_sync(
        self,
        *,
        agent_id: str,
        qualified_name: str,
        arguments: dict[str,Any],
    ) -> MCPExecutionResult:
        """
        为当前同步 LangChain Agent提供线程安全桥接

        同步 Agent必须运行在 MCP实践循环之外的工作线程中
        下一轮接入 Orchestrator时使用 asyncio.to_thread(agent.run,...)
        """

        loop = self._event_loop

        if loop is None or not loop.is_running():
            raise MCPClientError("MCPManager is not running")

        if threading.get_ident() == self._event_loop_thread_id:
            raise MCPClientError(
                "synchronous MCP invocation cannot run on the MCP event-loop "
                "thread; use await tool.ainvoke(...) or run the Agent in "
                "asyncio.to_thread(...)"
            )

        future = asyncio.run_coroutine_threadsafe(
            self._execute_bound_tool(
                agent_id=agent_id,
                qualified_name=qualified_name,
                arguments=arguments,
            ),
            loop,
        )

        return future.result()

    def _extract_command_plan(self, result: Any) -> tuple[CommandRequest, int]:
        text_blocks = [
            block.text
            for block in (getattr(result,"content",None) or [])
            if getattr(block,"type",None) == "text"
            and isinstance(getattr(block,"text",None),str)
        ]

        if bool(getattr(result,"isError",False)):
            # This trusted server only prepares a command. No sandbox execution
            # task exists yet; a completed rejection is not an unknown execution.
            raise MCPCommandPlanError("mcp.planner_rejected")

        candidates: list[dict[str, Any]] = []

        structured = getattr(result,"structuredContent",None)
        if isinstance(structured,dict):
            candidates.append(structured)

        for text in text_blocks:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                continue

            if isinstance(parsed,dict):
                candidates.append(parsed)

        for candidate in candidates:
            plan = candidate.get("sandbox_command")
            bootstrap = candidate.get("bootstrap")

            # 兼容某些 MCP SDK将函数结果包装在 result字段中的情况
            if plan is None and isinstance(candidate.get("result"), dict):
                nested = candidate["result"]
                plan = nested.get("sandbox_command")
                if bootstrap is None:
                    bootstrap = nested.get("bootstrap")

            if isinstance(plan, dict):
                compensate_seconds = self._validate_bootstrap(bootstrap)
                return self._validate_command_plan(plan), compensate_seconds

        raise MCPCommandPlanError(
            "MCP Server did not return a sandbox_command object"
        )

    def _extract_command_request(self, result: Any) -> CommandRequest:
        """Compatibility wrapper for callers that only need the command."""
        request, _ = self._extract_command_plan(result)
        return request

    @staticmethod
    def _validate_bootstrap(value: Any) -> int:
        if value is None:
            return 0
        if not isinstance(value, dict) or set(value) != {"compensate_seconds"}:
            raise MCPCommandPlanError(
                "bootstrap must contain only compensate_seconds"
            )
        seconds = value.get("compensate_seconds")
        if isinstance(seconds, bool) or seconds != 300:
            raise MCPCommandPlanError(
                "bootstrap.compensate_seconds must be exactly 300"
            )
        return 300

    def _write_bootstrap_marker(
        self,
        *,
        qualified_name: str,
        compensate_seconds: int,
    ) -> None:
        if self.bootstrap_marker_path is None:
            return
        target = self.bootstrap_marker_path
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(
            f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        payload = {
            "version": 1,
            "qualified_tool": qualified_name,
            "compensate_seconds": compensate_seconds,
            "started_at": time.time(),
        }
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(target)

    @staticmethod
    def _validate_command_plan(plan:dict[str,Any]) -> CommandRequest:
        allowed_fields = {"tool","arguments","timeout_seconds"}
        extra_fields = set(plan).difference(allowed_fields)

        if extra_fields:
            raise MCPCommandPlanError(
                f"unsupported sandbox_command fields: {sorted(extra_fields)}"
            )

        tool = plan.get("tool")
        arguments = plan.get("arguments", [])
        timeout_seconds = plan.get("timeout_seconds", 30)

        if not isinstance(tool, str) or not tool.strip():
            raise MCPCommandPlanError(
                "sandbox_command.tool must be a non-empty string"
            )

        if not isinstance(arguments, list) or not all(
            isinstance(argument, str) for argument in arguments
        ):
            raise MCPCommandPlanError(
                "sandbox_command.arguments must be a list of strings"
            )

        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
        ):
            raise MCPCommandPlanError(
                "sandbox_command.timeout_seconds must be an integer"
            )

        return CommandRequest(
            tool=tool.strip(),
            arguments=tuple(arguments),
            timeout_seconds=timeout_seconds,
        )

    def _truncate_output(self,output: str) -> tuple[str,bool]:
        if len(output) <= self.output_character_limit:
            return output, False

        half = self.output_character_limit // 2
        omitted = len(output) - (half * 2)

        return (
            output[:half]
            + f"\n...[{omitted} characters omitted]...\n"
            + output[-half:],
            True,
        )

@dataclass(frozen=True,slots=True)
class AgentMCPClient:
    """
    已与单个 Agent和其沙盒绑定的 MCP客户端

    LLM无法修改 agent_id，也无法调用未分配给它的 MCP Tool
    """

    manager: MCPManager
    agent_id: str
    allowed_tools: frozenset[str]

    async def call_tool(
        self,
        qualified_name:str,
        arguments: dict[str,Any],
    ) -> MCPExecutionResult:
        self._ensure_allowed(qualified_name)

        return await self.manager._execute_bound_tool(
            agent_id=self.agent_id,
            qualified_name=qualified_name,
            arguments=arguments,
        )

    def call_tool_sync(
        self,
        qualified_name:str,
        arguments:dict[str,Any],
    ) -> MCPExecutionResult:
        self._ensure_allowed(qualified_name)

        return self.manager._execute_bound_tool_sync(
            agent_id=self.agent_id,
            qualified_name=qualified_name,
            arguments=arguments,
        )

    def list_tool_specs(self) -> list[dict[str, Any]]:
        return [
            {
                "qualified_name": meta.qualified_name,
                "description": meta.description,
                "input_schema": meta.input_schema,
                "tags": sorted(meta.tags),
            }
            for meta in self.manager.list_registered_tools()
            if meta.qualified_name in self.allowed_tools
        ]

    def _ensure_allowed(self, qualified_name: str) -> None:
        if qualified_name not in self.allowed_tools:
            raise MCPClientError(
                f"Agent {self.agent_id!r} is not allowed to call "
                f"{qualified_name!r}"
            )

async def build_mcp_runtime(
    sandbox_service: SandboxService,
    registry_path: str | Path | None = None,
    *,
    allowed_servers: Iterable[str] | None = None,
    required_servers: Iterable[str] | None = None,
    audit_path: str | Path | None = None,
    bootstrap_marker_path: str | Path | None = None,
) -> MCPManager:
    if registry_path is None:
        registry_path = Path(__file__).with_name("registry.json")

    manager = MCPManager(
        registry=select_registry_servers(
            load_registry(registry_path),
            allowed_servers,
        ),
        sandbox_service=sandbox_service,
        mcp_call_timeout_seconds=float(
            os.environ.get("QINYUAN_MCP_CALL_TIMEOUT_SECONDS", "10")
        ),
        startup_timeout_seconds=float(
            os.environ.get("QINYUAN_MCP_STARTUP_TIMEOUT_SECONDS", "30")
        ),
        shutdown_timeout_seconds=float(
            os.environ.get("QINYUAN_MCP_SHUTDOWN_TIMEOUT_SECONDS", "10")
        ),
        sandbox_execution_grace_seconds=float(
            os.environ.get("QINYUAN_SANDBOX_EXECUTION_GRACE_SECONDS", "10")
        ),
        sandbox_interrupt_timeout_seconds=float(
            os.environ.get("QINYUAN_SANDBOX_INTERRUPT_TIMEOUT_SECONDS", "10")
        ),
        output_character_limit=int(
            os.environ.get("QINYUAN_MCP_OUTPUT_CHARACTER_LIMIT", "20000")
        ),
        required_servers=required_servers,
        audit_path=audit_path,
        bootstrap_marker_path=bootstrap_marker_path,
    )

    await manager.start_all()
    return manager
