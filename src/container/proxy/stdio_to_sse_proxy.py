"""MCP stdio-to-SSE proxy server.

This proxy converts stdio-based MCP servers to SSE (Server-Sent Events) HTTP endpoints,
allowing them to be accessed via HTTP/SSE while maintaining stdio communication internally.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from typing import Any, Dict, Optional, List, Callable

try:
    from aiohttp import web
    from aiohttp_sse import sse_response
except ImportError:
    print("Installing required dependencies: aiohttp, aiohttp-sse...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "aiohttp", "aiohttp-sse"])
    from aiohttp import web
    from aiohttp_sse import sse_response


class StdioMCPServer:
    """Manages a stdio-based MCP server process.
    
    This class is language-agnostic and works with any MCP server that:
    - Communicates via stdin/stdout using JSON-RPC protocol
    - Uses newline-delimited JSON format
    - Reads/writes text (UTF-8) rather than binary data
    
    Supported languages include:
    - Python (via python/python3)
    - Node.js (via node)
    - Go (via go run or pre-built binaries)
    - Any other language that can read/write JSON-RPC over stdio
    """
    
    def __init__(self, command: str, working_dir: str, env: Dict[str, str]):
        self.command = command
        self.working_dir = working_dir
        self.env = env
        self.process: Optional[subprocess.Popen] = None
        self.request_id = 0
        self.pending_requests: Dict[int, asyncio.Future] = {}
        self._lock = asyncio.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._read_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._message_callbacks: List[Callable[[Dict[str, Any]], None]] = []
        self._last_error: Optional[str] = None
        self._process_exited: bool = False
        
    async def start(self) -> None:
        """Start the stdio MCP server process."""
        import shlex
        
        # Merge environment variables
        process_env = {**os.environ.copy(), **self.env}
        
        # Check if command contains shell operators or builtins (like 'source', '&&', '||', etc.)
        # If so, execute through bash shell
        needs_shell = any(op in self.command for op in ['&&', '||', ';', '|', '>', '<']) or \
                     any(builtin in self.command for builtin in ['source', '. ', 'export ', 'cd '])
        
        if needs_shell:
            # Execute through bash shell
            cmd_parts = ['bash', '-c', self.command]
        else:
            # Simple command, can split safely
            cmd_parts = shlex.split(self.command)
        
        try:
            self.process = subprocess.Popen(
                cmd_parts,
                cwd=self.working_dir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=process_env,
                text=True,  # Text mode (UTF-8) - works with Python, Node.js, Go, etc.
                bufsize=0   # Unbuffered for real-time communication
            )
        except Exception as e:
            error_msg = f"Failed to start server process: {e}"
            self._last_error = error_msg
            raise RuntimeError(error_msg)
        
        # Verify process started successfully
        await asyncio.sleep(0.1)  # Brief wait to check if process exits immediately
        if self.process.poll() is not None:
            # Process exited immediately, read stderr for error
            stderr_output = ""
            try:
                if self.process.stderr:
                    stderr_output = self.process.stderr.read()
            except Exception:
                pass
            
            exit_code = self.process.returncode
            error_msg = f"Server process exited immediately with code {exit_code}"
            if stderr_output:
                error_msg += f": {stderr_output[:500]}"  # Limit error message length
            self._last_error = error_msg
            self._process_exited = True
            raise RuntimeError(error_msg)
        
        # Store event loop for thread callbacks
        self._loop = asyncio.get_event_loop()
        
        # Start reader task for stdout
        self._read_thread = threading.Thread(target=self._read_stdout_sync, daemon=True)
        self._read_thread.start()
        
        # Start stderr reader to capture errors
        self._stderr_thread = threading.Thread(target=self._read_stderr_sync, daemon=True)
        self._stderr_thread.start()
        
        # Automatically initialize the MCP server
        await self._auto_initialize()
    
    def _read_stderr_sync(self) -> None:
        """Read stderr in separate thread to capture errors."""
        if not self.process or not self.process.stderr:
            return
        
        error_lines = []
        try:
            while True:
                if not self.process or self.process.poll() is not None:
                    break
                line = self.process.stderr.readline()
                if not line:
                    if self.process.poll() is not None:
                        break
                    continue
                
                line = line.strip()
                if line:
                    error_lines.append(line)
                    # Store last error
                    self._last_error = line
                    # Print to stderr for logging
                    print(f"Server stderr: {line}", flush=True, file=sys.stderr)
        except Exception as e:
            print(f"Error reading stderr: {e}", flush=True, file=sys.stderr)
        
        # If process exited, mark it
        if self.process and self.process.poll() is not None:
            self._process_exited = True
            if error_lines:
                self._last_error = "\n".join(error_lines[-5:])  # Last 5 error lines
    
    def is_running(self) -> bool:
        """Check if server process is actually running."""
        if not self.process:
            return False
        return self.process.poll() is None
    
    def get_error(self) -> Optional[str]:
        """Get last error message."""
        return self._last_error
    
    def has_exited(self) -> bool:
        """Check if process has exited."""
        return self._process_exited or (self.process is not None and self.process.poll() is not None)
    
    async def _auto_initialize(self) -> None:
        """Automatically initialize MCP server when it starts."""
        try:
            # Wait a bit for server to be ready
            await asyncio.sleep(0.5)
            
            # Send initialize request
            init_response = await self.send_request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {
                    "name": "mcp-proxy",
                    "version": "1.0.0"
                }
            })
            
            # Send initialized notification
            initialized_notification = {
                "jsonrpc": "2.0",
                "method": "notifications/initialized"
            }
            notification_json = json.dumps(initialized_notification) + "\n"
            if self.process and self.process.stdin:
                self.process.stdin.write(notification_json)
                self.process.stdin.flush()
        except Exception as e:
            # If initialization fails, log but don't fail - server might not need it
            print(f"Auto-initialization warning (server may not require it): {e}", flush=True, file=sys.stderr)
        
    def _read_stdout_sync(self) -> None:
        """Synchronous read loop in separate thread.
        
        Reads newline-delimited JSON from stdout. Works with any language
        that writes JSON-RPC messages as UTF-8 text, one message per line.
        """
        while True:
            if not self.process or self.process.poll() is not None:
                self._process_exited = True
                break
            try:
                # Read line-by-line (newline-delimited JSON format)
                # This works with Python, Node.js, Go, and any other language
                line = self.process.stdout.readline()
                if not line:
                    if self.process.poll() is not None:
                        self._process_exited = True
                        break
                    continue
                        
                line = line.strip()
                if not line:
                    continue
                        
                try:
                    # Parse JSON message (language-agnostic)
                    message = json.loads(line)
                    # Handle all messages (responses, notifications, etc.)
                    if self._loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            self._handle_message(message),
                            self._loop
                        )
                except json.JSONDecodeError:
                    continue
            except Exception as e:
                print(f"Error reading stdout: {e}", flush=True, file=sys.stderr)
                self._process_exited = True
                break
    
    async def _handle_message(self, message: Dict[str, Any]) -> None:
        """Handle any message from stdio (response or notification)."""
        # If it's a response to a pending request, resolve the future
        if "id" in message:
            request_id = message["id"]
            async with self._lock:
                if request_id in self.pending_requests:
                    self.pending_requests[request_id].set_result(message)
                    # Also send to SSE clients (for full streaming)
                    for callback in self._message_callbacks:
                        try:
                            if asyncio.iscoroutinefunction(callback):
                                await callback(message)
                            else:
                                callback(message)
                        except Exception as e:
                            print(f"Error in message callback: {e}", flush=True, file=sys.stderr)
                    return
        
        # Otherwise, it's a notification or server-initiated message
        # Notify all registered callbacks (e.g., SSE clients)
        async with self._lock:
            for callback in self._message_callbacks:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(message)
                    else:
                        callback(message)
                except Exception as e:
                    print(f"Error in message callback: {e}", flush=True, file=sys.stderr)
    
    def register_message_callback(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """Register callback for server-initiated messages (notifications)."""
        async def add_callback():
            async with self._lock:
                self._message_callbacks.append(callback)
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(add_callback(), self._loop)
        else:
            self._message_callbacks.append(callback)
    
    def unregister_message_callback(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """Unregister a message callback."""
        async def remove_callback():
            async with self._lock:
                if callback in self._message_callbacks:
                    self._message_callbacks.remove(callback)
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(remove_callback(), self._loop)
        else:
            if callback in self._message_callbacks:
                self._message_callbacks.remove(callback)
                
    async def send_request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Send a JSON-RPC request to the stdio server."""
        # Check if process is still running
        if not self.is_running():
            error_msg = self.get_error() or "Server process not running"
            raise RuntimeError(error_msg)
        
        async with self._lock:
            self.request_id += 1
            request_id = self.request_id
        
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {}
        }
        
        if not self.process or not self.process.stdin:
            error_msg = self.get_error() or "Server process not running"
            raise RuntimeError(error_msg)
            
        future = asyncio.Future()
        async with self._lock:
            self.pending_requests[request_id] = future
        
        # Write request to stdin (newline-delimited JSON)
        # Format: JSON object followed by newline (JSON-RPC over stdio standard)
        # Works with Python, Node.js, Go, and any language that reads JSON from stdin
        request_json = json.dumps(request) + "\n"
        try:
            self.process.stdin.write(request_json)
            self.process.stdin.flush()  # Ensure immediate write (important for Go servers)
        except Exception as e:
            async with self._lock:
                self.pending_requests.pop(request_id, None)
            error_msg = self.get_error() or str(e)
            raise RuntimeError(f"Failed to write to stdin: {error_msg}")
        
        # Wait for response with timeout
        try:
            response = await asyncio.wait_for(future, timeout=30.0)
            return response
        except asyncio.TimeoutError:
            async with self._lock:
                self.pending_requests.pop(request_id, None)
            # Check if process died during wait
            if not self.is_running():
                error_msg = self.get_error() or "Server process died"
                raise RuntimeError(error_msg)
            raise TimeoutError(f"Request {request_id} timed out")
            
                
    async def stop(self) -> None:
        """Stop the server process."""
        if self.process:
            self.process.terminate()
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(self.process.wait),
                    timeout=5.0
                )
            except asyncio.TimeoutError:
                self.process.kill()
                self.process.wait()
            self.process = None
            if self._read_thread:
                self._read_thread.join(timeout=2.0)


class MCPSSEProxy:
    """HTTP/SSE proxy for stdio-based MCP servers."""
    
    def __init__(self, port: int, server_command: str, working_dir: str, env: Dict[str, str]):
        self.port = port
        self.server_command = server_command
        self.working_dir = working_dir
        self.env = env
        self.stdio_server: Optional[StdioMCPServer] = None
        self.app = web.Application()
        self.runner: Optional[web.AppRunner] = None
        self._sse_clients: List[web.StreamResponse] = []
        self._setup_routes()
        
    def _setup_routes(self) -> None:
        """Setup HTTP routes."""
        self.app.router.add_get('/sse', self.handle_sse)
        self.app.router.add_post('/message', self.handle_message)
        self.app.router.add_post('/', self.handle_message)  # Also accept POST on root
        self.app.router.add_get('/health', self.handle_health)
        
    async def handle_sse(self, request: web.Request) -> web.StreamResponse:
        """Handle SSE connection - streams all messages from stdio server."""
        # Ensure server is running
        if not self.stdio_server:
            await self._ensure_server_running()
        
        async with sse_response(request) as resp:
            # Register this SSE client to receive all messages
            async def send_to_sse(message: Dict[str, Any]) -> None:
                try:
                    await resp.send(json.dumps(message))
                except Exception:
                    pass  # Client disconnected
            
            self.stdio_server.register_message_callback(send_to_sse)
            self._sse_clients.append(resp)
            
            # Send initial connection message
            await resp.send(json.dumps({"jsonrpc": "2.0", "type": "connected"}))
            
            # Keep connection alive and stream messages
            try:
                while True:
                    await asyncio.sleep(30)
                    await resp.send(json.dumps({"jsonrpc": "2.0", "type": "ping"}))
            except (asyncio.CancelledError, ConnectionError):
                pass
            finally:
                # Cleanup
                if self.stdio_server:
                    self.stdio_server.unregister_message_callback(send_to_sse)
                if resp in self._sse_clients:
                    self._sse_clients.remove(resp)
                
        return resp
        
    async def handle_message(self, request: web.Request) -> web.Response:
        """Handle JSON-RPC message via HTTP POST."""
        try:
            data = await request.json()
            method = data.get("method")
            params = data.get("params", {})
            request_id = data.get("id")
            
            if not self.stdio_server:
                await self._ensure_server_running()
                
            response = await self.stdio_server.send_request(method, params)
            # Ensure response has the correct id
            if "id" not in response:
                response["id"] = request_id
            return web.json_response(response)
            
        except Exception as e:
            return web.json_response(
                {
                    "jsonrpc": "2.0",
                    "id": data.get("id") if 'data' in locals() else None,
                    "error": {"code": -32603, "message": str(e)}
                },
                status=500
            )
            
    async def handle_health(self, request: web.Request) -> web.Response:
        """Health check endpoint."""
        if not self.stdio_server:
            return web.json_response({
                "status": "unhealthy",
                "server_running": False,
                "error": "Server not initialized"
            }, status=503)
        
        is_running = self.stdio_server.is_running()
        error = self.stdio_server.get_error() if not is_running else None
        
        response_data = {
            "status": "healthy" if is_running else "unhealthy",
            "server_running": is_running
        }
        
        if error:
            response_data["error"] = error
        
        status_code = 200 if is_running else 503
        return web.json_response(response_data, status=status_code)
        
    async def _ensure_server_running(self) -> None:
        """Ensure stdio server is running."""
        if not self.stdio_server or not self.stdio_server.is_running():
            # If previous server existed but died, log the error
            if self.stdio_server and self.stdio_server.has_exited():
                error = self.stdio_server.get_error()
                if error:
                    print(f"Previous server process exited: {error}", flush=True, file=sys.stderr)
            
            self.stdio_server = StdioMCPServer(
                self.server_command,
                self.working_dir,
                self.env
            )
            try:
                await self.stdio_server.start()
            except Exception as e:
                error_msg = f"Failed to start server: {e}"
                print(error_msg, flush=True, file=sys.stderr)
                raise RuntimeError(error_msg)
            
    async def start(self) -> None:
        """Start the proxy server."""
        try:
            await self._ensure_server_running()
        except Exception as e:
            error_msg = f"Failed to start stdio server: {e}"
            print(error_msg, flush=True, file=sys.stderr)
            # Still start the proxy so health endpoint can report the error
            # But exit with error code after a delay
            self.runner = web.AppRunner(self.app)
            await self.runner.setup()
            site = web.TCPSite(self.runner, '0.0.0.0', self.port)
            await site.start()
            print(f"MCP Proxy started on port {self.port} (server failed to start)", flush=True)
            # Exit after a short delay to allow health checks
            asyncio.create_task(self._exit_on_server_failure())
            return
        
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '0.0.0.0', self.port)
        await site.start()
        print(f"MCP Proxy started on port {self.port}", flush=True)
        
        # Monitor server health in background
        asyncio.create_task(self._monitor_server_health())
    
    async def _exit_on_server_failure(self) -> None:
        """Exit proxy if server failed to start."""
        await asyncio.sleep(5)  # Give time for health checks
        print("Exiting proxy due to server startup failure", flush=True, file=sys.stderr)
        os._exit(1)
    
    async def _monitor_server_health(self) -> None:
        """Monitor server health and log errors."""
        while True:
            await asyncio.sleep(10)  # Check every 10 seconds
            if self.stdio_server and not self.stdio_server.is_running():
                error = self.stdio_server.get_error()
                error_msg = f"Server process died: {error}" if error else "Server process died"
                print(error_msg, flush=True, file=sys.stderr)
                # Don't exit, let health endpoint report the error
        
    async def stop(self) -> None:
        """Stop the proxy server."""
        if self.stdio_server:
            await self.stdio_server.stop()
        if self.runner:
            await self.runner.cleanup()


async def main():
    """Main entry point for proxy server."""
    # Get configuration from environment
    port = int(os.environ.get("PORT", "20000"))
    server_command = os.environ.get("SERVER_COMMAND", "")
    working_dir = os.environ.get("WORKING_DIR", "/mcp_servers")
    
    # Parse environment variables (SERVER_ENV_* prefix)
    env = {}
    for key, value in os.environ.items():
        if key.startswith("SERVER_ENV_"):
            env_key = key.replace("SERVER_ENV_", "")
            env[env_key] = value
    
    if not server_command:
        print("Error: SERVER_COMMAND environment variable is required", file=sys.stderr)
        sys.exit(1)
        
    proxy = MCPSSEProxy(port, server_command, working_dir, env)
    
    try:
        await proxy.start()
        # Keep running indefinitely
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        print("Stopping proxy...", flush=True)
        await proxy.stop()


if __name__ == "__main__":
    asyncio.run(main())

