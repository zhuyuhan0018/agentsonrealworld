"""MCP Client implementation for connecting to MCP servers via HTTP/SSE."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional
import aiohttp

from ..logging_config import get_logger
from .mcp_client import MCPClient, MCPToolSpec

logger = get_logger(__name__)


class HTTPMCPClient(MCPClient):
    """MCP Client that connects via HTTP/SSE endpoints.
    
    Works transparently with both:
    - Direct SSE servers (native HTTP/SSE)
    - stdio servers (via transparent proxy that converts stdio to SSE)
    
    The proxy is completely transparent - clients use the same interface regardless.
    """
    
    def __init__(self, server: str, transport: str, endpoint: str, **kwargs: Any) -> None:
        super().__init__(server, transport, endpoint, **kwargs)
        self._session: Optional[aiohttp.ClientSession] = None
        self._connected = False
        
    async def connect(self) -> None:
        """Connect to MCP server via HTTP endpoint.
        
        Note: Initialization is handled automatically by the proxy when the server starts.
        Clients can directly call list_tools() without explicit initialization.
        """
        if self._connected:
            return
            
        if not self.endpoint:
            raise ValueError(f"No endpoint provided for server {self.server}")
        
        # Create HTTP session
        self._session = aiohttp.ClientSession()
        
        # Mark as connected - initialization is handled by proxy
        self._connected = True
    
    async def _send_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Send JSON-RPC request and wait for response."""
        if not self._session:
            raise RuntimeError("Not connected")
        
        # Normalize endpoint URL
        url = self.endpoint
        if not url.startswith("http://") and not url.startswith("https://"):
            url = f"http://{url}"
        
        # Use /message endpoint
        if not url.endswith("/message"):
            url = f"{url}/message" if url.endswith("/") else f"{url}/message"
        
        # Debug: Log request being sent
        logger.debug(f"MCP sending request to {url}: {json.dumps(request, indent=2)}")
        
        async with self._session.post(url, json=request, timeout=aiohttp.ClientTimeout(total=120)) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                logger.debug(f"MCP HTTP error response (status {resp.status}): {error_text}")
                raise RuntimeError(f"HTTP {resp.status}: {error_text}")
            
            # Read response text once (can't read twice)
            raw_response_text = await resp.text()
            # logger.debug(f"MCP raw HTTP response text: {raw_response_text[:1000]}{'...' if len(raw_response_text) > 1000 else ''}")
            
            # Parse JSON response
            try:
                parsed_response = json.loads(raw_response_text)
                # logger.debug(f"MCP parsed JSON response: {json.dumps(parsed_response, indent=2)}")
                return parsed_response
            except Exception as e:
                logger.error(f"MCP JSON parsing failed. Raw response: {raw_response_text}")
                raise RuntimeError(f"Failed to parse JSON response: {e}") from e
    
    async def _send_notification(self, notification: Dict[str, Any]) -> None:
        """Send JSON-RPC notification (no response expected)."""
        if not self._session:
            raise RuntimeError("Not connected")
        
        # Normalize endpoint URL
        url = self.endpoint
        if not url.startswith("http://") and not url.startswith("https://"):
            url = f"http://{url}"
        
        # Use /message endpoint
        if not url.endswith("/message"):
            url = f"{url}/message" if url.endswith("/") else f"{url}/message"
        
        async with self._session.post(url, json=notification, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                raise RuntimeError(f"HTTP {resp.status}: {error_text}")
    
    async def list_tools(self) -> List[MCPToolSpec]:
        """List available tools from MCP server."""
        # Auto-connect if not connected
        if not self._connected:
            await self.connect()
        
        request = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list"
        }
        
        try:
            response = await self._send_request(request)
            if "error" in response:
                raise RuntimeError(f"tools/list failed: {response['error']}")
            
            tools_data = response.get("result", {}).get("tools", [])
            self._tools = [
                MCPToolSpec(
                    name=tool.get("name", ""),
                    description=tool.get("description", ""),
                    input_schema=tool.get("inputSchema", {})
                )
                for tool in tools_data
            ]
            return self._tools
        except Exception as e:
            raise RuntimeError(f"Failed to list tools: {e}")
    
    async def invoke(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """Invoke a tool on the MCP server."""
        # Auto-connect if not connected
        if not self._connected:
            await self.connect()
        
        # Log API invocation at DEBUG level (tool_utils already logs at INFO)
        logger.debug(f"MCP API call: {tool_name}")
        
        # Debug: Log arguments being sent (after serialization, None values should be excluded)
        logger.debug(f"MCP arguments for {tool_name}: {json.dumps(arguments, indent=2)}")
        
        request = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments or {}
            }
        }
        
        try:
            response = await self._send_request(request)
            
            # Debug: Log raw JSON-RPC response
            # logger.debug(f"MCP raw response for {tool_name}: {json.dumps(response, indent=2)}")
            
            if "error" in response:
                error = response["error"]
                error_msg = f"Tool invocation failed: {error.get('message', 'Unknown error')}"
                logger.error(f"❌ {error_msg}")
                logger.error(f"Error details: {json.dumps(error, indent=2)}")
                raise RuntimeError(error_msg)
            
            result = response.get("result", {})
            
            # Debug: Log parsed result object
            logger.debug(f"MCP result object for {tool_name}: {json.dumps(result, indent=2)}")
            
            # Log successful response at DEBUG level
            logger.debug(f"API call {tool_name} successful")
            
            # MCP tools/call returns result with content
            content = result.get("content", [])
            # logger.debug(f"MCP content blocks for {tool_name}: {json.dumps(content, indent=2)}")
            
            if content:
                # Extract text from content blocks
                text_parts = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        elif "text" in block:
                            text_parts.append(str(block["text"]))
                
                # logger.debug(f"MCP extracted text parts for {tool_name}: {text_parts}")
                
                if text_parts:
                    output = "\n".join(text_parts)
                    logger.debug(f"MCP final output for {tool_name} ({len(output)} chars): {output[:500]}{'...' if len(output) > 500 else ''}")

                    return output
            
            # Fallback to returning the whole result
            result_str = json.dumps(result) if isinstance(result, dict) else str(result)
            logger.debug(f"MCP fallback result_str for {tool_name} ({len(result_str)} chars): {result_str[:500]}{'...' if len(result_str) > 500 else ''}")

            return result_str
        except Exception as e:
            logger.error(f"❌ Failed to invoke tool {tool_name}: {e}")
            raise RuntimeError(f"Failed to invoke tool {tool_name}: {e}")
    
    async def close(self) -> None:
        """Close the connection."""
        if self._session:
            await self._session.close()
            self._session = None
        self._connected = False
    
    def __del__(self):
        """Cleanup on deletion (fallback - explicit close() is preferred)."""
        if self._session and not self._session.closed:
            # Try to close, but don't fail if event loop is closed or unavailable
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # Can't use run_until_complete in running loop, schedule task
                    asyncio.create_task(self._session.close())
                else:
                    # Event loop exists but not running, can use run_until_complete
                    loop.run_until_complete(self._session.close())
            except (RuntimeError, AttributeError):
                # No event loop or loop is closed - session will be cleaned up by GC
                # This is acceptable as a fallback
                pass
            except Exception:
                # Any other error - ignore in destructor
                pass

