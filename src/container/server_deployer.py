"""Server deployment logic for MCP servers in containers."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from .docker_manager import DockerManager
from .state_manager import StateManager
from ..logging_config import get_logger

logger = get_logger(__name__)


class ServerDeployer:
    """Handles deployment of MCP servers to Docker containers."""

    def __init__(self, container_name: str, enable_interceptors: bool = False):
        self.container_name = container_name
        self.enable_interceptors = enable_interceptors
        self.docker = DockerManager()
        self.base_path = "/mcp_servers"
        self.interceptor_path = "/mcp_interceptors/interceptors"
        self.proxy_path = "/mcp_proxy"
        self.replay_path = "/mcp_interceptors/replay"
        self.scripts_path = "/mcp_scripts"
    
    def sync_infrastructure(self, project_root: Optional[Path] = None) -> None:
        """Sync all infrastructure files to container.
        
        This syncs interceptors, proxy, replay, and scripts to a running container
        without rebuilding the Docker image. Useful for development.
        
        Args:
            project_root: Path to project root. If None, auto-detected.
        """
        if project_root is None:
            project_root = Path(__file__).parent.parent.parent
        
        logger.info(f"Syncing infrastructure files to container {self.container_name}")
        
        # Ensure container is running
        self.docker.ensure_container_running(self.container_name)
        
        # Define source -> destination mappings
        sync_items = [
            (project_root / "src" / "container" / "interceptors", self.interceptor_path, "interceptors"),
            (project_root / "src" / "container" / "proxy", self.proxy_path, "proxy"),
            (project_root / "src" / "container" / "replay", self.replay_path, "replay"),
        ]
        
        for source, dest, description in sync_items:
            if source.exists():
                logger.info(f"Syncing {description}...")
                self._sync_directory(source, dest)
            else:
                logger.warning(f"Source directory not found: {source}")
        
        # Sync network management script
        network_script = project_root / "src" / "container" / "manage_network.sh"
        if network_script.exists():
            logger.info("Syncing network management script...")
            self.docker.exec_command(self.container_name, f"mkdir -p {self.scripts_path}")
            self.docker.copy_to_container(
                self.container_name,
                str(network_script),
                f"{self.scripts_path}/manage_network.sh"
            )
            self.docker.exec_command(self.container_name, f"chmod +x {self.scripts_path}/manage_network.sh")
        
        logger.info("Infrastructure sync completed")
    
    def _sync_directory(self, source: Path, dest: str) -> None:
        """Sync a directory to container, replacing existing content.
        
        Note: docker cp copies the directory itself, creating nested structure.
        To avoid double nesting, we copy to a temp location first, then move contents.
        """
        # Remove existing directory
        self.docker.exec_command(self.container_name, f"rm -rf {dest}", check=False)
        self.docker.exec_command(self.container_name, f"mkdir -p {dest}")
        
        # Copy to a temp location (docker cp creates source.name subdirectory)
        temp_dest = f"/tmp/sync_{source.name}_{id(source)}"
        self.docker.exec_command(self.container_name, f"rm -rf {temp_dest}", check=False)
        self.docker.exec_command(self.container_name, f"mkdir -p {temp_dest}")
        self.docker.copy_to_container(self.container_name, str(source), temp_dest)
        
        # Move contents from temp_dest/source.name to dest
        source_name = source.name
        nested_path = f"{temp_dest}/{source_name}"
        self.docker.exec_command(
            self.container_name,
            f"if [ -d {nested_path} ]; then mv {nested_path}/* {dest}/ 2>/dev/null || true; rmdir {nested_path} 2>/dev/null || true; fi"
        )
        # Clean up temp directory
        self.docker.exec_command(self.container_name, f"rm -rf {temp_dest}", check=False)

    def _replace_placeholders(self, text: str, project_path: str, port: int) -> str:
        """Replace placeholders in text with actual values.
        
        Supported placeholders:
        - {PROJECT_PATH}: Replaced with the server's project path in container
        - {PORT}: Replaced with the assigned port number
        """
        if not isinstance(text, str):
            text = str(text)
        text = text.replace("{PROJECT_PATH}", project_path)
        text = text.replace("{PORT}", str(port))
        return text

    def _get_timeout(self, server_config: Dict[str, Any]) -> int:
        """Return timeout in seconds from server_config or default to 2 seconds.
        
        This timeout controls initial startup wait and verification request timeouts.
        """
        timeout = server_config.get("timeout")
        try:
            return int(timeout) if timeout is not None else 2
        except (ValueError, TypeError):
            return 2

    def deploy_server(self, server_name: str, server_config: Dict[str, Any], state: Dict[str, Any]) -> None:
        """Deploy single server. Check if already deployed first."""
        server_state = StateManager.get_server_state(server_name, state)
        
        if server_state and server_state.get("status") in ["deployed", "running", "stopped"]:
            logger.info(f"Server {server_name} is already deployed, skipping deployment")
            return
        
        logger.info(f"Deploying server {server_name}")
        server_path = f"{self.base_path}/{server_name}"
        
        # Ensure base directory exists
        self.docker.exec_command(self.container_name, f"mkdir -p {self.base_path}")
        
        # Copy or download code
        source_type = server_config.get("source_type")
        if source_type == "url":
            source_url = server_config.get("source_url")
            if not source_url:
                raise ValueError(f"source_url is required for source_type='url'")
            self.download_server_code(source_url, server_path)
        elif source_type == "project":
            local_path = server_config.get("project_path")
            if not local_path:
                raise ValueError(f"project_path is required for source_type='project'")
            self.copy_server_code(local_path, server_path)
        else:
            raise ValueError(f"Unknown source_type: {source_type}. Must be 'url' or 'project'")
        
        # Setup environment
        self.setup_server_environment(server_name, server_config, server_path)
        
        # Always deploy interceptors (injection happens, but activation depends on env vars)
        # The injected code will only activate when EXECUTION_ID and WORKFLOW_ID are set
        self.deploy_interceptors(server_name, server_config, server_path)
        
        # Deploy proxy if transport is stdio
        transport = server_config.get("transport", "sse")
        if transport == "stdio":
            self.deploy_proxy(server_name)
        
        # Mark as deployed
        from datetime import datetime
        StateManager.update_server_state(
            server_name,
            {
                "status": "deployed",
                "transport": transport,
                "deployed_at": datetime.utcnow().isoformat() + "Z"
            },
            state
        )
        logger.info(f"Server {server_name} deployed successfully")

    def download_server_code(self, url: str, container_path: str) -> None:
        """Download server code from URL (Git repo or archive)."""
        logger.info(f"Downloading server code from {url} to {container_path}")
        
        # Remove existing directory if it exists
        self.docker.exec_command(self.container_name, f"rm -rf {container_path}")
        
        # Clone or download
        if url.endswith(".git") or "github.com" in url or "gitlab.com" in url:
            # Git clone
            self.docker.exec_command(
                self.container_name,
                f"git clone {url} {container_path}"
            )
        else:
            # Download and extract archive
            # This is a simplified version - may need to handle different archive types
            temp_file = "/tmp/server_archive.tar.gz"
            self.docker.exec_command(
                self.container_name,
                f"curl -L {url} -o {temp_file} || wget -O {temp_file} {url}"
            )
            self.docker.exec_command(
                self.container_name,
                f"mkdir -p {container_path} && tar -xzf {temp_file} -C {container_path} --strip-components=1 || unzip -q {temp_file} -d {container_path}"
            )
            self.docker.exec_command(self.container_name, f"rm -f {temp_file}")
        
        logger.debug(f"Downloaded code to {container_path}")

    def copy_server_code(self, local_path: str, container_path: str) -> None:
        """Copy server code from local path to container."""
        local_path_obj = Path(local_path)
        if not local_path_obj.exists():
            raise FileNotFoundError(f"Local path {local_path} does not exist")
        
        # Resolve absolute path
        abs_local_path = local_path_obj.resolve()
        
        logger.info(f"Copying {abs_local_path} to {self.container_name}:{container_path}")
        
        # Remove existing directory in container
        self.docker.exec_command(self.container_name, f"rm -rf {container_path}")
        
        # Copy to container
        self.docker.copy_to_container(self.container_name, str(abs_local_path), container_path)
        
        logger.debug(f"Copied code to {container_path}")

    def setup_server_environment(self, server_name: str, server_config: Dict[str, Any], server_path: str) -> None:
        """Run setup_commands array in order.
        
        Supports various languages including:
        - Python: pip install, uv pip install, etc.
        - Node.js: npm install, npm run build, etc.
        - Go: go mod download, go build, etc.
        """
        setup_commands = server_config.get("setup_commands", [])
        if not setup_commands:
            logger.debug(f"No setup commands for server {server_name}")
            return
        
        logger.info(f"Setting up environment for server {server_name}")
        
        # Combine all setup commands into a single bash session
        # This ensures virtual environment activation (source .venv/bin/activate) 
        # and other environment changes persist across commands
        # Commands are joined with && to stop on first error
        combined_command = " && ".join(setup_commands)
        logger.info(f"Running combined setup commands in single bash session:")
        for idx, cmd in enumerate(setup_commands):
            logger.info(f"  {idx + 1}. {cmd}")
        
        try:
            output = self.docker.exec_command(
                self.container_name,
                combined_command,
                working_dir=server_path
            )
            if output:
                logger.debug(f"Setup commands output: {output}")
            logger.debug(f"All setup commands completed successfully")
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Setup commands failed")
            
            # Try to get more details from CalledProcessError
            if isinstance(e, subprocess.CalledProcessError):
                if e.stderr:
                    logger.error(f"Command stderr: {e.stderr}")
                if e.stdout:
                    logger.error(f"Command stdout: {e.stdout}")
                logger.error(f"Exit code: {e.returncode}")
            else:
                logger.error(f"Error: {error_msg}")
            
            # Log helpful debugging information
            logger.error(f"Working directory: {server_path}")
            logger.error(f"Combined command that failed: {combined_command}")           
            raise
        
        logger.info(f"Environment setup completed for server {server_name}")

    def deploy_proxy(self, server_name: str) -> None:
        """Deploy proxy code to container."""
        proxy_path = "/mcp_proxy"
        logger.info(f"Deploying proxy to {proxy_path}")
        
        # Ensure proxy directory exists
        self.docker.exec_command(self.container_name, f"mkdir -p {proxy_path}")
        
        # Check if proxy already exists in expected location
        proxy_file = f"{proxy_path}/stdio_to_sse_proxy.py"
        result = self.docker.exec_command(
            self.container_name,
            f"test -f {proxy_file} && echo 'exists' || echo 'missing'",
            check=False
        )
        
        if "exists" in result:
            logger.debug(f"Proxy already exists in container (from base image), skipping copy")
        else:
            # Check if proxy exists in subdirectory (from Dockerfile COPY issue)
            proxy_file_alt = f"{proxy_path}/proxy/stdio_to_sse_proxy.py"
            result_alt = self.docker.exec_command(
                self.container_name,
                f"test -f {proxy_file_alt} && echo 'exists' || echo 'missing'",
                check=False
            )
            
            if "exists" in result_alt:
                # Move proxy from subdirectory to expected location
                logger.info(f"Moving proxy from {proxy_file_alt} to {proxy_file}")
                self.docker.exec_command(
                    self.container_name,
                    f"cp {proxy_file_alt} {proxy_file}"
                )
                logger.debug(f"Proxy moved to expected location")
            else:
                # Proxy doesn't exist, copy it (backward compatibility with standard images)
                proxy_source = Path(__file__).parent / "proxy" / "stdio_to_sse_proxy.py"
                
                if proxy_source.exists():
                    # Copy proxy file to container
                    self.docker.copy_to_container(
                        self.container_name,
                        str(proxy_source),
                        proxy_file
                    )
                    logger.debug(f"Proxy deployed to {proxy_path}")
                else:
                    logger.warning(f"Proxy source not found at {proxy_source}, proxy may not work correctly")
        
        # Verify proxy file is in correct location
        verify_result = self.docker.exec_command(
            self.container_name,
            f"test -f {proxy_file} && echo 'ok' || echo 'fail'",
            check=False
        )
        if "ok" not in verify_result:
            error_msg = f"Proxy file not found at {proxy_file} after deployment"
            logger.error(error_msg)
            raise RuntimeError(error_msg)
        
        # Note: Proxy dependencies (aiohttp, aiohttp-sse) are pre-installed in base image
        # No need to install them here - use mcp-sandbox-base image for proper setup

    def start_server(self, server_name: str, server_config: Dict[str, Any], port: int, state: Dict[str, Any]) -> None:
        """Start server process with env vars in background."""
        server_path = f"{self.base_path}/{server_name}"
        start_command = server_config.get("start_command")
        if not start_command:
            raise ValueError(f"start_command is required for server {server_name}")
        
        # Replace placeholders in start_command
        start_command = self._replace_placeholders(start_command, server_path, port)
        
        transport = server_config.get("transport", "sse")
        env = server_config.get("env", {}).copy()
        # Determine startup wait timeout (default 2s)
        timeout_sec = self._get_timeout(server_config)
        
        # Replace placeholders in env values and ensure PORT is set
        env = {k: self._replace_placeholders(v, server_path, port) for k, v in env.items()}
        env["PORT"] = str(port)  # Ensure PORT is set
        
        # For JavaScript/Node.js servers, inject interceptor via NODE_OPTIONS
        # This works for all Node.js commands: node, npm, ts-node, etc.
        if self.enable_interceptors and ("node" in start_command.lower() or "npm" in start_command.lower() or ".js" in start_command or 'ts' in start_command):
            interceptor_path = f"{self.interceptor_path}/http_javascript_interceptor.js"
            # Append to existing NODE_OPTIONS if present, otherwise create new
            existing_node_options = env.get("NODE_OPTIONS", "")
            if existing_node_options:
                env["NODE_OPTIONS"] = f"{existing_node_options} --require {interceptor_path}"
            else:
                env["NODE_OPTIONS"] = f"--require {interceptor_path}"
            logger.debug(f"Set NODE_OPTIONS for {server_name}: {env['NODE_OPTIONS']}")
        
        # Add execution_id and workflow_id from environment (for interceptors)
        execution_id = os.environ.get("EXECUTION_ID")
        workflow_id = os.environ.get("WORKFLOW_ID")
        
        if execution_id:
            env["EXECUTION_ID"] = execution_id
        if workflow_id:
            env["WORKFLOW_ID"] = workflow_id

        
        # Get container IP for endpoint
        container_ip = self.docker.get_container_ip(self.container_name)
        
        logger.debug(f"Starting server {server_name} on port {port} (transport: {transport})")
        
        if transport == "stdio":
            # Start proxy instead of direct server
            self.start_proxy(server_name, start_command, port, server_path, env, container_ip, state, timeout_sec)
        else:
            # Start server directly (SSE)
            self.start_direct_server(server_name, start_command, port, server_path, env, container_ip, state, timeout_sec)
    
    def start_proxy(self, server_name: str, start_command: str, port: int, 
                    server_path: str, env: Dict[str, str], container_ip: str, state: Dict[str, Any], timeout_sec: int) -> None:
        """Start proxy for stdio-based server."""
        proxy_path = "/mcp_proxy"
        
        # Verify server directory exists before starting proxy
        try:
            check_result = self.docker.exec_command(
                self.container_name,
                f"test -d {server_path}",
                check=False
            )
            if check_result != "":
                # Directory doesn't exist - server not deployed
                error_msg = f"Server directory {server_path} does not exist. Server '{server_name}' must be deployed before starting."
                logger.error(error_msg)
                raise RuntimeError(error_msg)
        except Exception as e:
            if "does not exist" in str(e):
                raise
            # If check command failed for other reasons, log but continue
            logger.warning(f"Could not verify server directory exists: {e}")
        
        # Build environment variables for proxy
        proxy_env = {
            "PORT": str(port),
            "SERVER_COMMAND": start_command,
            "WORKING_DIR": server_path
        }
        
        # Add server env vars with SERVER_ENV_ prefix
        for key, value in env.items():
            proxy_env[f"SERVER_ENV_{key}"] = str(value)
        
        # Add interceptor environment variables
        proxy_env["SERVER_ENV_MCP_SERVER_NAME"] = server_name
        proxy_env["SERVER_ENV_HTTP_LOG_DIR"] = "/mcp_logs/http_requests"
        
        # NODE_OPTIONS is already in env dict if set, and will be passed via SERVER_ENV_NODE_OPTIONS
        
        # These are critical for ensuring each execution writes to the correct directory
        execution_id = os.environ.get("EXECUTION_ID")
        workflow_id = os.environ.get("WORKFLOW_ID")
        
        if execution_id:
            proxy_env["SERVER_ENV_EXECUTION_ID"] = execution_id
        if workflow_id:
            proxy_env["SERVER_ENV_WORKFLOW_ID"] = workflow_id
        
        # Start proxy in background
        env_exports = " ".join([f"export {k}={shlex.quote(str(v))}" for k, v in proxy_env.items()])
        full_command = f"cd {proxy_path} && {env_exports} && nohup python stdio_to_sse_proxy.py > /tmp/{server_name}_proxy.log 2>&1 &"
        
        try:
            self.docker.exec_command(self.container_name, full_command)
            
            # Wait for proxy to initialize
            import time
            time.sleep(timeout_sec)  # Give proxy time to start and initialize server
            
            # Verify server is actually running by requesting tools list
            endpoint = f"http://{container_ip}:{port}" if container_ip else f"http://localhost:{port}"
            if not self._verify_server_running(endpoint, server_name):
                # Check proxy logs for error details
                try:
                    log_output = self.docker.exec_command(
                        self.container_name,
                        f"tail -n 20 /tmp/{server_name}_proxy.log",
                        check=False
                    )
                    if log_output:
                        logger.error(f"Proxy log for {server_name}:\n{log_output}")
                except Exception:
                    pass
                
                error_msg = f"Server {server_name} failed to start - tools/list request failed"
                logger.error(error_msg)
                StateManager.update_server_state(
                    server_name,
                    {"status": "stopped"},
                    state
                )
                raise RuntimeError(error_msg)
            
            # Update state only after verification succeeds
            from datetime import datetime
            StateManager.update_server_state(
                server_name,
                {
                    "status": "running",
                    "port": port,
                    "endpoint": endpoint,
                    "transport": "stdio",
                    "last_started_at": datetime.utcnow().isoformat() + "Z"
                },
                state
            )
            logger.info(f"Proxy for server {server_name} started on port {port} (endpoint: {endpoint})")
        except RuntimeError:
            # Re-raise RuntimeError (server verification failed)
            raise
        except Exception as e:
            logger.error(f"Failed to start proxy for server {server_name}: {e}", exc_info=True)
            StateManager.update_server_state(
                server_name,
                {"status": "stopped"},
                state
            )
            raise
    
    def start_direct_server(self, server_name: str, start_command: str, port: int,
                           server_path: str, env: Dict[str, str], container_ip: str, state: Dict[str, Any], timeout_sec: int) -> None:
        """Start server directly (for SSE transport).
        
        Supports various server types:
        - Python: python server.py, python -m module, etc.
        - Node.js: node server.js, npm start, etc.
        - Go: ./server, go run main.go, or pre-built binaries
        """
        # Start server in background with nohup, using env vars
        env_exports = " ".join([f"export {k}={shlex.quote(str(v))}" for k, v in env.items()])
        full_command = f"cd {server_path} && {env_exports} && nohup {start_command} > /tmp/{server_name}.log 2>&1 &"
        
        try:
            self.docker.exec_command(self.container_name, full_command)
            
            # Wait for server to initialize
            import time
            time.sleep(timeout_sec)  # Give server time to start
            
            # Verify server is actually running by requesting tools list
            endpoint = f"http://{container_ip}:{port}" if container_ip else f"http://localhost:{port}"
            if not self._verify_server_running(endpoint, server_name):
                error_msg = f"Server {server_name} failed to start - tools/list request failed"
                logger.error(error_msg)
                StateManager.update_server_state(
                    server_name,
                    {"status": "stopped"},
                    state
                )
                raise RuntimeError(error_msg)
            
            # Update state only after verification succeeds
            from datetime import datetime
            StateManager.update_server_state(
                server_name,
                {
                    "status": "running",
                    "port": port,
                    "endpoint": endpoint,
                    "transport": "sse",
                    "last_started_at": datetime.utcnow().isoformat() + "Z"
                },
                state
            )
            logger.info(f"Server {server_name} started on port {port} (endpoint: {endpoint})")
        except RuntimeError:
            # Re-raise RuntimeError (server verification failed)
            raise
        except Exception as e:
            logger.error(f"Failed to start server {server_name}: {e}", exc_info=True)
            StateManager.update_server_state(
                server_name,
                {"status": "stopped"},
                state
            )
            raise

    def _verify_server_running(self, endpoint: str, server_name: str) -> bool:
        """Verify server is actually running by requesting tools list via MCP protocol."""
        import json
        import subprocess
        
        try:
            # Make MCP tools/list request to verify server is responding
            # This is a JSON-RPC 2.0 request
            request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list"
            }
            
            # Send POST request to SSE endpoint
            # For SSE transport, we need to use POST to /message endpoint
            import urllib.parse
            request_json = json.dumps(request)
            
            # Construct message endpoint URL
            message_url = f"{endpoint}/message" if not endpoint.endswith("/message") else endpoint
            
            # Try to make request via curl inside container
            # Escape single quotes in JSON for shell command
            request_json_escaped = request_json.replace("'", "'\\''")
            curl_cmd = (
                f"curl -s -X POST '{message_url}' "
                f"-H 'Content-Type: application/json' "
                f"-d '{request_json_escaped}' "
                f"--max-time 5"
            )
            
            result = subprocess.run(
                ["docker", "exec", self.container_name, "bash", "-c", curl_cmd],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode != 0:
                logger.debug(f"tools/list request failed for {server_name}: {result.stderr}")
                return False
            
            # Parse response
            try:
                response = json.loads(result.stdout)
                if "error" in response:
                    error_msg = response.get("error", {}).get("message", "Unknown error")
                    logger.debug(f"tools/list returned error for {server_name}: {error_msg}")
                    return False
                
                # Success - server responded with tools list
                tools = response.get("result", {}).get("tools", [])
                logger.debug(f"Server {server_name} verified - returned {len(tools)} tools")
                return True
            except json.JSONDecodeError:
                logger.debug(f"Invalid JSON response from {server_name}: {result.stdout[:200]}")
                return False
                
        except subprocess.TimeoutExpired:
            logger.debug(f"tools/list request timed out for {server_name}")
            return False
        except Exception as e:
            logger.debug(f"Failed to verify server {server_name}: {e}")
            return False
    
    def check_server_running(self, server_name: str, port: int) -> bool:
        """Verify server status by checking process, port, and health endpoint."""
        # First check if port is listening
        port_listening = False
        try:
            result = self.docker.exec_command(
                self.container_name,
                f"ss -ltn 2>/dev/null | grep -q ':{port} ' || netstat -ltn 2>/dev/null | grep -q ':{port} ' || echo 'not_listening'",
                check=False
            )
            port_listening = "not_listening" not in result
        except Exception:
            pass
        
        # For stdio servers (proxy), also check health endpoint
        # Get container IP
        try:
            container_ip = self.docker.get_container_ip(self.container_name)
            health_url = f"http://{container_ip}:{port}/health" if container_ip else f"http://localhost:{port}/health"
            
            # Try to check health endpoint
            import subprocess
            result = subprocess.run(
                ["docker", "exec", self.container_name, "bash", "-c", 
                 f"curl -s -f {health_url} 2>/dev/null || echo 'health_check_failed'"],
                capture_output=True,
                text=True,
                timeout=2
            )
            
            if result.returncode == 0 and "health_check_failed" not in result.stdout:
                try:
                    import json
                    health_data = json.loads(result.stdout)
                    server_running = health_data.get("server_running", False)
                    status = health_data.get("status", "unhealthy")
                    
                    # If health check says unhealthy, log the error
                    if not server_running or status != "healthy":
                        error = health_data.get("error")
                        if error:
                            logger.warning(f"Server {server_name} health check failed: {error}")
                        return False
                    
                    return server_running
                except (json.JSONDecodeError, KeyError):
                    pass
        except Exception as e:
            logger.debug(f"Health check failed for {server_name}: {e}")
        
        # Fallback to port check
        return port_listening
    
    def deploy_interceptors(self, server_name: str, server_config: Dict[str, Any], server_path: str) -> None:
        """Deploy HTTP and system interceptors to container and inject into server code.
        
        Interceptors are always injected during deployment, but only activate when:
        - EXECUTION_ID and WORKFLOW_ID environment variables are set (by pipeline)
        - This allows interceptors to be present but inactive until needed
        """
        logger.info(f"Deploying interceptors for server {server_name}")
        
        # Always copy interceptor files to container (needed for injection)
        self._copy_interceptor_files()
        
        # Determine server language from start_command
        start_command = server_config.get("start_command", "")
        # Replace placeholders so injection resolves real paths inside the container
        start_command = self._replace_placeholders(start_command, server_path, 0)

        if "python" in start_command.lower() or ".py" in start_command:
            # Python server - inject HTTP interceptor
            # Injection happens always, but activation depends on EXECUTION_ID/WORKFLOW_ID env vars
            self._inject_python_interceptor(server_name, server_path, start_command)
        elif "node" in start_command.lower() or "npm" in start_command.lower() or ".js" in start_command or 'ts' in start_command:
            # Node.js server - use NODE_OPTIONS to preload interceptor
            # This works for all Node.js commands (node, npm, ts-node, etc.)
            # Activation depends on EXECUTION_ID/WORKFLOW_ID env vars
            logger.info(f"JavaScript server detected for {server_name}. Using NODE_OPTIONS to inject interceptor.")
            # No wrapper file needed - NODE_OPTIONS will be set in start_server method
        elif "go" in start_command.lower() or start_command.endswith(".go") or "/" in start_command and ".go" in start_command: 
            # Go server - provide instructions (Go requires compile-time integration)
            logger.info(f"Go server detected for {server_name}. Go interceptors require compile-time integration.")
            logger.info(f"  - Copy system_go_interceptor.go and http_go_interceptor.go to your Go server")
            logger.info(f"  - Import and call system_interceptor.Init() and http_interceptor.Init() in main()")
            logger.info(f"  - Use system_interceptor.Command() instead of exec.Command()")
            logger.info(f"  - Use http_interceptor.WrapClient() for HTTP clients")
        else:
            logger.warning(f"Unknown server language for {server_name}, skipping interceptor injection")
    
    def _copy_interceptor_files(self) -> None:
        """Copy interceptor files (HTTP and system) to container.
        
        Always syncs to ensure container has latest interceptor code.
        Also syncs replay directory for latest replay manager code.
        """
        project_root = Path(__file__).parent.parent.parent
        
        # Sync interceptors (always sync to get latest code)
        interceptor_source = project_root / "src" / "container" / "interceptors"
        if interceptor_source.exists():
            logger.debug(f"Syncing interceptors from {interceptor_source} to {self.container_name}:{self.interceptor_path}")
            self._sync_directory(interceptor_source, self.interceptor_path)
            logger.debug("Interceptor files synced to container")
        else:
            logger.warning(f"Interceptor source directory not found: {interceptor_source}")
        
        # Sync replay directory (contains replay_logger.py and other replay managers)
        replay_source = project_root / "src" / "container" / "replay"
        if replay_source.exists():
            logger.debug(f"Syncing replay directory from {replay_source} to {self.container_name}:{self.replay_path}")
            self._sync_directory(replay_source, self.replay_path)
            logger.debug("Replay directory synced to container")
        else:
            logger.warning(f"Replay source directory not found: {replay_source}")
    
    def _inject_python_interceptor(self, server_name: str, server_path: str, start_command: str) -> None:
        """Inject Python interceptor into server file."""
        # Extract Python file from start_command
        # Examples: 
        #   "python src/server.py"
        #   "source .venv/bin/activate && python src/server.py"
        #   "uv --directory {PROJECT_PATH}/src/ run server.py"
        #   "uv run server.py"
        #   "uv run python server.py"
        #   "uv run python3 server.py"
        #   "python -m module" (not supported for injection, but won't fail)
        
        python_file = None
        
        # Try to match uv commands: "uv ... run [python/python3] <file>"
        # Matches: "uv run file.py", "uv run python file.py", "uv --directory path run python3 file.py", etc.
        # Pattern: uv ... run (optionally python/python3) <file>
        uv_match = re.search(r'uv\s+(?:[^\s&|]+\s+)*run\s+(?:python3?\s+)?([^\s&|]+)', start_command)
        if uv_match:
            python_file = uv_match.group(1).strip()
        else:
            # Try to match python commands: "python <file>" or "python3 <file>"
            python_match = re.search(r'python3?\s+([^\s&|]+)', start_command)
            if python_match:
                python_file = python_match.group(1).strip()
                # Skip if it's a module (-m flag) as we can't inject into modules
                if python_file.startswith('-m'):
                    logger.warning(f"Python module execution (-m) not supported for injection: {start_command}")
                    return
        
        if not python_file:
            logger.warning(f"Could not extract Python file from start_command: {start_command}")
            return
        
        # Handle relative paths - if the file doesn't start with /, it's relative to server_path
        # For uv commands, the file is typically relative to the --directory or current directory
        if not python_file.startswith("/"):
            # Check if start_command has --directory flag for uv
            dir_match = re.search(r'--directory\s+([^\s&|]+)', start_command)
            if dir_match:
                # Replace placeholders in directory path
                uv_dir = dir_match.group(1).strip()
                uv_dir = uv_dir.replace('{PROJECT_PATH}', server_path)
                uv_dir = uv_dir.replace('{PORT}', '')  # Remove PORT placeholder if present
                # Normalize path (remove trailing slashes)
                uv_dir = uv_dir.rstrip('/')
                python_file = f"{uv_dir}/{python_file}"
            else:
                python_file = f"{server_path}/{python_file}"
        
        logger.info(f"Injecting Python interceptor into {python_file}")
        
        # Read the file from container
        try:
            content = self.docker.exec_command(
                self.container_name,
                f"cat {python_file}",
                check=False
            )
            
            if not content:
                logger.warning(f"Could not read file {python_file}")
                return
            
            # Check if already injected
            if "setup_python_interceptor" in content:
                logger.debug(f"HTTP interceptor already present in {python_file}")
                return
            
            # Find insertion point: BEFORE any imports (critical for libraries like amadeus
            # that import requests internally at module import time)
            lines = content.split("\n")
            insert_index = 0
            in_docstring = False
            docstring_char = None
            
            # Find the first non-comment, non-docstring, non-shebang line
            # This is where we'll insert the interceptor BEFORE any imports
            for i, line in enumerate(lines):
                stripped = line.strip()
                
                # Skip shebang
                if i == 0 and stripped.startswith("#!"):
                    insert_index = i + 1
                    continue
                
                # Handle docstrings
                if in_docstring:
                    # Looking for closing docstring
                    if docstring_char in line:
                        insert_index = i + 1
                        in_docstring = False
                        docstring_char = None
                    continue
                
                # Check for docstring start
                if stripped.startswith('"""') or stripped.startswith("'''"):
                    insert_index = i + 1
                    docstring_char = '"""' if stripped.startswith('"""') else "'''"
                    # Check if it's a single-line docstring
                    if stripped.count(docstring_char) == 2:
                        in_docstring = False
                        docstring_char = None
                    else:
                        in_docstring = True
                    continue
                
                # Skip empty lines
                if not stripped:
                    insert_index = i + 1
                    continue
                
                # Skip comments
                if stripped.startswith("#"):
                    insert_index = i + 1
                    continue
                
                # Found first non-comment, non-docstring line - insert BEFORE it
                # This ensures interceptor is set up before any imports (including
                # imports that might trigger HTTP library imports internally)
                insert_index = i
                break
            
            # Skip blank lines at insertion point
            while insert_index < len(lines) and not lines[insert_index].strip():
                insert_index += 1
            
            # Prepare interceptor code with maximum safety
            # Note: interceptor_path is /mcp_interceptors/interceptors
            # We add /mcp_interceptors to sys.path so "from interceptors" works
            # All code is wrapped in try-except to ensure server startup is never blocked
            # The interceptor will only activate when EXECUTION_ID and WORKFLOW_ID env vars are set
            interceptor_code = [
                "",
                "# ===== HTTP Interceptor - Auto-injected (Safe Mode) =====",
                "# This block is always injected but only activates when EXECUTION_ID and WORKFLOW_ID are set",
                "# Server will start normally even if interceptor setup fails",
                "try:",
                "    import sys",
                "    from pathlib import Path",
                "    import os",
                "",
                "    # Add interceptor parent path to sys.path for proper imports",
                f"    interceptor_parent = Path('{self.interceptor_path}').parent",
                "    if str(interceptor_parent) not in sys.path:",
                "        sys.path.insert(0, str(interceptor_parent))",
                "",
                "    # Import HTTP interceptor only",
                "    from interceptors import setup_python_interceptor",
                "",
                "    # Set environment variables (MCP_SERVER_NAME is always set)",
                f"    os.environ['MCP_SERVER_NAME'] = '{server_name}'",
                f"    os.environ['HTTP_LOG_DIR'] = '/mcp_logs/http_requests'",
                "",
                "    # Setup interceptor (this function itself has error handling)",
                "    # Interceptor will only log requests when EXECUTION_ID and WORKFLOW_ID are set",
                "    setup_python_interceptor()",
                "",
                "except ImportError:",
                "    # Interceptor module not found - server can continue without interception",
                "    pass",
                "except Exception:",
                "    # Any other error - server can continue without interception",
                "    pass",
                "# ===== End HTTP Interceptor ======",
                "",
            ]
            
            # Insert the interceptor code
            new_lines = lines[:insert_index] + interceptor_code + lines[insert_index:]
            new_content = "\n".join(new_lines)
            
            # Write back to container using Python script
            python_script = f"""
import sys
with open('{python_file}', 'w') as f:
    f.write({repr(new_content)})
"""
            self.docker.exec_command(
                self.container_name,
                f"python3 -c {shlex.quote(python_script)}",
                working_dir=server_path
            )
            
            # Verify injection succeeded
            verify_content = self.docker.exec_command(
                self.container_name,
                f"cat {python_file}",
                check=False
            )
            injection_success = "setup_python_interceptor" in verify_content if verify_content else False
            
            logger.info(f"Python interceptor injected into {python_file} (verified: {injection_success})")
            
        except Exception as e:
            logger.error(f"Failed to inject Python interceptor: {e}", exc_info=True)
    
    def _create_javascript_wrapper(self, server_name: str, server_path: str, start_command: str) -> None:
        """Create JavaScript wrapper file that loads interceptor before server."""
        # Extract JavaScript file from start_command
        # Examples: "node build/index.js", "node {PROJECT_PATH}/build/index.js"
        js_file_match = re.search(r'node\s+([^\s&|]+)', start_command)
        if not js_file_match:
            logger.warning(f"Could not extract JavaScript file from start_command: {start_command}")
            return
        
        js_file = js_file_match.group(1).strip()
        # Replace {PROJECT_PATH} placeholder with actual server_path
        js_file = js_file.replace("{PROJECT_PATH}", server_path)
        # Handle relative paths (after placeholder replacement)
        if not js_file.startswith("/"):
            js_file = f"{server_path}/{js_file}"
        
        # Determine wrapper file extension based on module type
        # Will be set in the if/else block below
        wrapper_file = None
        
        # Check if package.json exists and has "type": "module"
        is_es_module = False
        try:
            package_json_content = self.docker.exec_command(
                self.container_name,
                f"cat {server_path}/package.json 2>/dev/null || echo ''",
                check=False
            )
            if package_json_content and '"type"' in package_json_content and '"module"' in package_json_content:
                is_es_module = True
                logger.debug(f"Detected ES module project for {server_name}")
        except Exception:
            pass
        
        # Create wrapper content - use .cjs extension for ES modules
        if is_es_module:
            # For ES modules, use .cjs extension to force CommonJS mode
            wrapper_file = f"{server_path}/server_wrapper.cjs"
            wrapper_content = f"""/**
 * Wrapper file that loads HTTP interceptor before the MCP server.
 * This file was auto-generated. Modify the original server file instead.
 * Uses .cjs extension to force CommonJS mode for interceptor compatibility.
 */

// Set server name and log directory for logging
process.env.MCP_SERVER_NAME = '{server_name}';
process.env.HTTP_LOG_DIR = '/mcp_logs/http_requests';

// Note: EXECUTION_ID and WORKFLOW_ID are set by the proxy via SERVER_ENV_* prefix
// The proxy converts SERVER_ENV_EXECUTION_ID -> EXECUTION_ID in the server process environment
// So we don't need to set them here - they're available via process.env.EXECUTION_ID at runtime

// Load HTTP interceptor only (system interceptor disabled)
require('{self.interceptor_path}/http_javascript_interceptor.js');

// Load the actual server (ES module via dynamic import)
// Use async IIFE and handle unhandled rejections
(async () => {{
    try {{
        await import('{js_file}');
        // Server module loaded successfully - it will keep running
        // (reading from stdin for stdio mode or serving HTTP for SSE mode)
    }} catch (err) {{
        console.error('Failed to load server:', err);
        process.exit(1);
    }}
}})();

// Handle unhandled promise rejections to prevent silent failures
process.on('unhandledRejection', (reason, promise) => {{
    console.error('Unhandled Rejection at:', promise, 'reason:', reason);
    process.exit(1);
}});
"""
        else:
            # CommonJS syntax - use .js extension
            wrapper_file = f"{server_path}/server_wrapper.js"
            wrapper_content = f"""/**
 * Wrapper file that loads HTTP interceptor before the MCP server.
 * This file was auto-generated. Modify the original server file instead.
 */

// Set server name and log directory for logging
process.env.MCP_SERVER_NAME = '{server_name}';
process.env.HTTP_LOG_DIR = '/mcp_logs/http_requests';

// Note: EXECUTION_ID and WORKFLOW_ID are set by the proxy via SERVER_ENV_* prefix
// The proxy converts SERVER_ENV_EXECUTION_ID -> EXECUTION_ID in the server process environment
// So we don't need to set them here - they're available via process.env.EXECUTION_ID at runtime

// Load HTTP interceptor only (system interceptor disabled)
require('{self.interceptor_path}/http_javascript_interceptor.js');

// Load the actual server
require('{js_file}');
"""
        
        # Write wrapper to container
        try:
            python_script = f"""
import sys
with open('{wrapper_file}', 'w') as f:
    f.write({repr(wrapper_content)})
"""
            self.docker.exec_command(
                self.container_name,
                f"python3 -c {shlex.quote(python_script)}",
                working_dir=server_path
            )
            
            logger.info(f"JavaScript wrapper created at {wrapper_file}")
            logger.info(f"Note: Update start_command to use: node {wrapper_file}")
            
        except Exception as e:
            logger.error(f"Failed to create JavaScript wrapper: {e}", exc_info=True)

