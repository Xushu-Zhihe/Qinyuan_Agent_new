from __future__ import annotations

import ipaddress
import os
import re
from typing import Any

from MCPs.fastmcp_compat import FastMCP

mcp = FastMCP(
    "qinyuan-nmap-command-planner",
    instructions=(
        "Validate an authorized Nmap TCP scan request and return a "
        "sandbox_command. This server never executes Nmap itself."
    ),
    json_response=True,
)

_HOSTNAME_PATTERN = re.compile(
    r"^(?=.{1,253}\.?$)"
    r"(?:[A-Za-z0-9]"
    r"(?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9]"
    r"(?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.?$"
)

def _allowed_target_rules() -> tuple[str, ...]:
    """
    读取允许扫描的目标。

    支持：
    - 精确主机名：target-web
    - 精确 IP：127.0.0.1
    - CIDR：172.20.0.0/24
    """

    raw = os.getenv(
        "MCP_NMAP_ALLOWED_TARGETS",
        "127.0.0.1,localhost",
    )

    rules = tuple(
        item.strip()
        for item in raw.split(",")
        if item.strip()
    )
    if not rules:
        raise RuntimeError(
            "MCP_NMAP_ALLOWED_TARGETS must contain at least one target"
        )

    return rules

def _maximum_port_count() -> int:
    raw = os.getenv("MCP_NMAP_MAX_PORTS", "1024")

    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(
            "MCP_NMAP_MAX_PORTS must be an integer"
        ) from exc

    if not 1 <= value <= 65_535:
        raise RuntimeError(
            "MCP_NMAP_MAX_PORTS must be between 1 and 65535"
        )

    return value

def _validate_target(target: str) -> tuple[str, bool]:
    """
    校验目标并判断是否为 IPv6。

    这里不在宿主机解析 DNS，因为真实解析和扫描发生在容器网络中。
    """

    candidate = target.strip()

    if not candidate:
        raise ValueError("target cannot be empty")

    if candidate.startswith("-"):
        raise ValueError("target cannot start with '-'")

    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        if not _HOSTNAME_PATTERN.fullmatch(candidate):
            raise ValueError(
                "target must be one hostname or one IP address"
            )

        normalized_hostname = candidate.rstrip(".").lower()

        allowed = any(
            normalized_hostname == rule.rstrip(".").lower()
            for rule in _allowed_target_rules()
            if "/" not in rule
        )

        if not allowed:
            raise PermissionError(
                f"target {candidate!r} is not in "
                "MCP_NMAP_ALLOWED_TARGETS"
            )

        return normalized_hostname, False

    for rule in _allowed_target_rules():
        try:
            network = ipaddress.ip_network(rule, strict=False)
        except ValueError:
            continue

        if address in network:
            return str(address), address.version == 6

    raise PermissionError(
        f"target {candidate!r} is not in MCP_NMAP_ALLOWED_TARGETS"
    )

def _normalize_ports(ports: str) -> str:
    """
    校验并标准化 Nmap 端口列表。

    支持：
    - 80
    - 80,443
    - 8000-8100
    - 22,80,443,8000-8100
    """

    raw_parts = [part.strip() for part in ports.split(",")]

    if not raw_parts or any(not part for part in raw_parts):
        raise ValueError(
            "ports must be a comma-separated port list"
        )

    normalized_parts: list[str] = []
    total_ports = 0

    # 环境配置只读取一次，不需要在每个端口循环中重复读取。
    maximum = _maximum_port_count()

    for part in raw_parts:
        if "-" in part:
            if part.count("-") != 1:
                raise ValueError(
                    f"invalid port range: {part!r}"
                )

            start_text, end_text = part.split(
                "-",
                maxsplit=1,
            )

            if (
                not start_text.isdigit()
                or not end_text.isdigit()
            ):
                raise ValueError(
                    f"invalid port range: {part!r}"
                )

            start = int(start_text)
            end = int(end_text)

            if not 1 <= start <= end <= 65_535:
                raise ValueError(
                    f"invalid port range: {part!r}"
                )

            total_ports += end - start + 1
            normalized_parts.append(f"{start}-{end}")

        else:
            if not part.isdigit():
                raise ValueError(
                    f"invalid port: {part!r}"
                )

            port = int(part)

            if not 1 <= port <= 65_535:
                raise ValueError(
                    f"invalid port: {port}"
                )

            total_ports += 1
            normalized_parts.append(str(port))

        # 尽早拒绝超大端口范围，不必继续处理后面的字符串。
        if total_ports > maximum:
            raise ValueError(
                f"requested {total_ports} ports; "
                f"maximum is {maximum}"
            )

    return ",".join(normalized_parts)

def _validate_timeout(timeout_seconds: int) -> int:
    if isinstance(timeout_seconds, bool):
        raise ValueError("timeout_seconds must be an integer")

    if not 5 <= timeout_seconds <= 300:
        raise ValueError(
            "timeout_seconds must be between 5 and 300"
        )

    return timeout_seconds

@mcp.tool(
    title="Nmap TCP Connect Scan",
    description=(
        "Prepare an authorized Nmap TCP connect scan for execution in "
        "the current Agent's Docker sandbox. The MCP Server validates "
        "the target and ports but does not run Nmap on the host."
    ),
)
def run_nmap_tcp_scan(
    target: str,
    ports: str = "80,443,8080",
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    """
    返回命令计划，不执行 subprocess，也不查找宿主机 Nmap。
    """

    normalized_target, is_ipv6 = _validate_target(target)
    normalized_ports = _normalize_ports(ports)
    normalized_timeout = _validate_timeout(timeout_seconds)

    arguments = [
        "-sT",
        "-Pn",
        "-n",
        "-p",
        normalized_ports,
    ]

    if is_ipv6:
        arguments.insert(0, "-6")

    arguments.append(normalized_target)

    return {
        "sandbox_command": {
            # 这里只写命令名，由容器内 PATH 解析。
            "tool": "nmap",
            "arguments": arguments,
            "timeout_seconds": normalized_timeout,
        },
        "request": {
            "target": normalized_target,
            "ports": normalized_ports,
            "scan_type": "tcp_connect",
        },
    }


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
