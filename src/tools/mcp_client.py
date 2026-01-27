from __future__ import annotations

from typing import Any, Dict, List, Optional


class MCPToolSpec:
    def __init__(self, name: str, description: str = "", input_schema: Optional[Dict[str, Any]] = None) -> None:
        self.name = name
        self.description = description
        self.input_schema = input_schema or {}


class MCPClient:
    """Base MCP client interface for MCP servers.

    This is the base class. Use HTTPMCPClient for HTTP/SSE connections.
    """

    def __init__(self, server: str, transport: str, endpoint: str, **kwargs: Any) -> None:
        self.server = server
        self.transport = transport
        self.endpoint = endpoint
        self.kwargs = kwargs
        self._tools: List[MCPToolSpec] = []

    async def connect(self) -> None:
        """Connect to the MCP server."""
        raise NotImplementedError("Subclass must implement connect()")

    async def list_tools(self) -> List[MCPToolSpec]:
        """List available tools from the server."""
        raise NotImplementedError("Subclass must implement list_tools()")

    async def invoke(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """Invoke a tool on the server."""
        raise NotImplementedError("Subclass must implement invoke()")
    
    async def close(self) -> None:
        """Close the connection."""
        pass


