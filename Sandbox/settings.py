from __future__ import annotations

import os
import re
from dataclasses import dataclass, field


DEFAULT_ALLOWED_TOOLS = frozenset(
    {
        "bash",
        "python3",
        "file",
        "jadx",
        "readelf",
        "objdump",
        "nm",
        "strings",
        "gdb",
        "nmap",
        "curl",
        "php",
        "subfinder",
        "amass",
        "httpx",
        "ffuf",
        "dirsearch",
        "nuclei",
        "tmux",
        "wine",
        "pwsh",
    }
)
_TOOL_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]*")


def _configured_tools() -> frozenset[str]:
    raw = os.environ.get("QINYUAN_SANDBOX_ALLOWED_TOOLS")
    if raw is None:
        return DEFAULT_ALLOWED_TOOLS
    values = frozenset(item.strip() for item in raw.split(",") if item.strip())
    if not values:
        raise ValueError("QINYUAN_SANDBOX_ALLOWED_TOOLS must not be empty")
    return values


def _configured_boolean(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _configured_positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _configured_nonnegative_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be a non-negative number") from exc
    if value < 0:
        raise ValueError(f"{name} must be a non-negative number")
    return value


def _default_docker_host() -> str:
    if os.name == "nt":
        return "npipe:////./pipe/dockerDesktopLinuxEngine"
    return "unix:///var/run/docker.sock"


@dataclass(frozen=True)
class SandboxSettings:
    image: str = "qinyuan/pentest-sandbox:0.3.6"
    network: str = "qinyuan_pentest_lab"
    network_internal: bool = True

    memory_limit: str = "512m"
    memory_swap_limit: str = "512m"
    cpu_limit: int = 500_000_000
    process_limit: int = 128

    workspace_size: str = "128m"
    temporary_size: str = "64m"

    maximum_timeout_seconds: int = 300

    # Docker 初始化连接要留出冷启动余量：容器引擎刚唤醒时，首次
    # /version 握手可能超过十秒，太短会让整轮在启动阶段直接失败。
    docker_connect_timeout_seconds: int = 20

    # 镜像、网络和容器生命周期操作必须快速失败。长时间运行的命令
    # 使用独立的流客户端，不得把控制面的超时一并放大。
    docker_control_timeout_seconds: int = 5

    # 命令最长可运行 300 秒，因此输出流读取超时要更长。
    docker_api_timeout_seconds: int = 315

    # 优雅停止超时后 Docker 会向容器发送 SIGKILL，但仍不会删除容器。
    docker_stop_timeout_seconds: int = 2

    # Keep the Docker API endpoint independent from DOCKER_HOST and proxy
    # variables inherited from a shell running Clash.
    docker_host: str = field(default_factory=_default_docker_host)
    docker_max_pool_size: int = 8
    docker_operation_retries: int = 3
    docker_retry_backoff_seconds: float = 0.25

    # 每个 stdout/stderr 最多在宿主机内存中保留 128 KiB。
    maximum_output_bytes_per_stream: int = 128 * 1024

    allowed_tools: frozenset[str] = field(default_factory=lambda: DEFAULT_ALLOWED_TOOLS)

    @classmethod
    def from_environment(cls) -> "SandboxSettings":
        return cls(
            image=os.environ.get(
                "QINYUAN_SANDBOX_IMAGE",
                "qinyuan/pentest-sandbox:0.3.6",
            ).strip(),
            network=os.environ.get(
                "QINYUAN_SANDBOX_NETWORK",
                "qinyuan_pentest_lab",
            ).strip(),
            network_internal=_configured_boolean(
                "QINYUAN_SANDBOX_NETWORK_INTERNAL",
                True,
            ),
            docker_host=os.environ.get(
                "QINYUAN_DOCKER_HOST",
                _default_docker_host(),
            ).strip(),
            docker_connect_timeout_seconds=_configured_positive_int(
                "QINYUAN_DOCKER_CONNECT_TIMEOUT_SECONDS",
                20,
            ),
            docker_control_timeout_seconds=_configured_positive_int(
                "QINYUAN_DOCKER_CONTROL_TIMEOUT_SECONDS",
                5,
            ),
            docker_api_timeout_seconds=_configured_positive_int(
                "QINYUAN_DOCKER_API_TIMEOUT_SECONDS",
                315,
            ),
            docker_stop_timeout_seconds=_configured_positive_int(
                "QINYUAN_DOCKER_STOP_TIMEOUT_SECONDS",
                2,
            ),
            docker_max_pool_size=_configured_positive_int(
                "QINYUAN_DOCKER_MAX_POOL_SIZE",
                8,
            ),
            docker_operation_retries=_configured_positive_int(
                "QINYUAN_DOCKER_OPERATION_RETRIES",
                3,
            ),
            docker_retry_backoff_seconds=_configured_nonnegative_float(
                "QINYUAN_DOCKER_RETRY_BACKOFF_SECONDS",
                0.25,
            ),
            allowed_tools=_configured_tools(),
        )

    def __post_init__(self) -> None:
        """在程序启动阶段发现错误配置。"""

        if not self.image:
            raise ValueError("sandbox image must not be empty")

        if not self.docker_host:
            raise ValueError("docker_host must not be empty")

        if self.docker_max_pool_size < 1:
            raise ValueError("docker_max_pool_size must be positive")

        if self.docker_operation_retries < 1:
            raise ValueError("docker_operation_retries must be positive")

        if self.docker_retry_backoff_seconds < 0:
            raise ValueError("docker_retry_backoff_seconds must be non-negative")

        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", self.network):
            raise ValueError("sandbox network name is invalid")

        if not isinstance(self.network_internal, bool):
            raise ValueError("network_internal must be a boolean")

        if not self.allowed_tools or any(
            _TOOL_NAME_PATTERN.fullmatch(tool) is None
            for tool in self.allowed_tools
        ):
            raise ValueError("allowed sandbox tools must be command names")

        if self.maximum_timeout_seconds < 1:
            raise ValueError("maximum_timeout_seconds must be positive")

        if self.docker_connect_timeout_seconds < 1:
            raise ValueError("docker_connect_timeout_seconds must be positive")

        if self.docker_control_timeout_seconds < 1:
            raise ValueError("docker_control_timeout_seconds must be positive")

        if self.docker_api_timeout_seconds <= self.maximum_timeout_seconds:
            raise ValueError(
                "docker_api_timeout_seconds must be greater than "
                "maximum_timeout_seconds"
            )

        if self.docker_stop_timeout_seconds < 1:
            raise ValueError("docker_stop_timeout_seconds must be positive")

        if self.maximum_output_bytes_per_stream < 1024:
            raise ValueError(
                "maximum_output_bytes_per_stream must be at least 1024"
            )
