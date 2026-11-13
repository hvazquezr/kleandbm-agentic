"""Connect to source MCP servers and call their curated, read-only tools.

The backend is synchronous, while the MCP client is async. A session runs the async client
in a blocking portal (a background event-loop thread), so one MCP server process can serve
many tool calls, e.g. a whole agent run against one source.
"""

import json
import sys
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from anyio.from_thread import BlockingPortal, start_blocking_portal

# Tools the backend lets anyone call. The server may grow tools, but nothing reaches users or
# agents until it's added here deliberately.
ALLOWED_TOOLS = frozenset({
    "list_tables", "describe_table", "row_count", "column_profile", "candidate_key_check",
    "relationship_check", "sample_rows", "schema_fingerprint",
})


@dataclass(frozen=True)
class ConnectionParams:
    host: str
    port: int
    database: str
    username: str
    password: str


@dataclass(frozen=True)
class ToolResult:
    is_error: bool
    data: dict[str, Any] | None
    error: str | None
    truncated: bool = False

    @property
    def schema_fingerprint(self) -> str | None:
        return (self.data or {}).get("schema_fingerprint")


class SourceSession(Protocol):
    def call(self, tool: str, arguments: dict[str, Any]) -> ToolResult: ...


class SourceConnector(Protocol):
    def open(self, params: ConnectionParams) -> AbstractContextManager[SourceSession]: ...


def _cap(data: dict[str, Any], max_bytes: int) -> tuple[dict[str, Any], bool]:
    """Keep results under max_bytes by trimming list fields, which hold the bulky parts."""
    raw = json.dumps(data, default=str)
    if len(raw) <= max_bytes:
        return data, False
    trimmed = dict(data)
    for key, value in sorted(data.items(), key=lambda kv: -len(json.dumps(kv[1], default=str))):
        if isinstance(value, list):
            keep = len(value)
            while keep > 0 and len(json.dumps({**trimmed, key: value[:keep]}, default=str)) > max_bytes:
                keep //= 2
            trimmed[key] = value[:keep]
            if len(json.dumps(trimmed, default=str)) <= max_bytes:
                break
    if len(json.dumps(trimmed, default=str)) > max_bytes:
        return {"schema_fingerprint": data.get("schema_fingerprint"), "note": "result too large to store"}, True
    return trimmed, True


class _McpSession:
    def __init__(self, client, portal: BlockingPortal, timeout_s: float, max_bytes: int) -> None:
        self._client, self._portal = client, portal
        self._timeout, self._max_bytes = timeout_s, max_bytes

    def call(self, tool: str, arguments: dict[str, Any]) -> ToolResult:
        if tool not in ALLOWED_TOOLS:
            return ToolResult(True, None, f"tool {tool!r} is not allowed")
        try:
            result = self._portal.call(lambda: self._client.call_tool(tool, arguments, read_timeout_seconds=self._timeout))
        except Exception as exc:  # timeouts, a crashed server, transport errors
            return ToolResult(True, None, f"{type(exc).__name__}: {exc}"[:2000])
        if result.is_error:
            text = " ".join(getattr(c, "text", "") for c in (result.content or [])).strip()
            return ToolResult(True, None, text[:2000] or "tool error")
        data, truncated = _cap(result.structured_content or {}, self._max_bytes)
        return ToolResult(False, data, None, truncated)


class _SessionContext(AbstractContextManager):
    def __init__(self, make_client, timeout_s: float, max_bytes: int) -> None:
        self._make_client, self._timeout, self._max_bytes = make_client, timeout_s, max_bytes

    def __enter__(self) -> SourceSession:
        self._portal_cm = start_blocking_portal()
        portal = self._portal_cm.__enter__()
        try:
            self._client_cm = portal.wrap_async_context_manager(self._make_client())
            client = self._client_cm.__enter__()
        except BaseException:
            self._portal_cm.__exit__(*sys.exc_info())
            raise
        return _McpSession(client, portal, self._timeout, self._max_bytes)

    def __exit__(self, *exc) -> None:
        try:
            self._client_cm.__exit__(*exc)
        finally:
            self._portal_cm.__exit__(None, None, None)


class StdioMySqlConnector:
    """Starts `python -m mysql_source` per session, passing credentials only via its environment."""

    def __init__(self, mcp_dir: str | Path, query_timeout_ms: int, call_timeout_s: float, max_result_bytes: int) -> None:
        self.mcp_dir = Path(mcp_dir).resolve()
        self.query_timeout_ms = query_timeout_ms
        self.call_timeout_s = call_timeout_s
        self.max_result_bytes = max_result_bytes

    def open(self, params: ConnectionParams) -> AbstractContextManager[SourceSession]:
        from mcp import Client
        from mcp.client.stdio import StdioServerParameters

        server = StdioServerParameters(
            command=sys.executable,
            args=["-m", "mysql_source"],
            cwd=str(self.mcp_dir),
            env={
                "SOURCE_MYSQL_HOST": params.host,
                "SOURCE_MYSQL_PORT": str(params.port),
                "SOURCE_MYSQL_USER": params.username,
                "SOURCE_MYSQL_PASSWORD": params.password,
                "SOURCE_MYSQL_DATABASE": params.database,
                "SOURCE_QUERY_TIMEOUT_MS": str(self.query_timeout_ms),
            },
        )
        return _SessionContext(
            lambda: Client(server, read_timeout_seconds=self.call_timeout_s),
            self.call_timeout_s,
            self.max_result_bytes,
        )


class InProcessConnector:
    """Runs a given MCPServer factory in-process (tests): same client path, no subprocess."""

    def __init__(self, server_factory, call_timeout_s: float = 20.0, max_result_bytes: int = 200_000) -> None:
        self.server_factory = server_factory
        self.call_timeout_s = call_timeout_s
        self.max_result_bytes = max_result_bytes

    def open(self, params: ConnectionParams) -> AbstractContextManager[SourceSession]:
        from mcp import Client

        return _SessionContext(
            lambda: Client(self.server_factory(params), read_timeout_seconds=self.call_timeout_s),
            self.call_timeout_s,
            self.max_result_bytes,
        )
