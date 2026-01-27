"""Main server lifecycle orchestrator."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, Optional, Dict, Any

from .docker_manager import DockerManager
from .server_deployer import ServerDeployer
from .state_manager import StateManager
from ..config import ContainerConfig, load_servers_config
from ..logging_config import get_logger

logger = get_logger(__name__)


class ServerManager:
    """Main orchestrator for server deployment and lifecycle management."""

    def __init__(self, container_config: ContainerConfig, enable_interceptors: bool = False):
        self.container_config = container_config
        self.container_name = container_config.name
        self.enable_interceptors = enable_interceptors
        self.docker = DockerManager()
        self.deployer = ServerDeployer(self.container_name, enable_interceptors=enable_interceptors)
        self.state_manager = StateManager()

    def ensure_servers_running(
        self,
        server_names: List[str],
        servers_config_path: str = "configs/servers.json",
        start_new_container: bool = False
    ) -> None:
        """Ensure required servers are running. Deploy and start if needed.
        
        Note: Network isolation can be enabled/disabled dynamically via
        No need to recreate container for network isolation changes.
        """
        if start_new_container:
            logger.info("Starting new container (removing existing if present)")
            if self.docker.container_exists(self.container_name):
                if self.docker.container_is_running(self.container_name):
                    self.docker.stop_container(self.container_name)
                self.docker.remove_container(self.container_name)
            # Reset state file when starting fresh container
            logger.info("Resetting state file for fresh container")
            self.state_manager.reset_state(self.container_name)
        
        # Ensure container is running
        if not self.docker.container_exists(self.container_name):
            logger.info(f"Creating container {self.container_name}")
            # Setup volume mounts for replay logs
            volume_mounts = self._get_volume_mounts()
            
            # Container is created with NET_ADMIN capability by default
            # Network isolation can be enabled/disabled dynamically without recreating container
            # Don't enable network isolation during deployment - servers need network to download dependencies
            # Network isolation will be enabled later, just before workflow execution
            self.docker.start_container(
                self.container_config.image,
                self.container_name,
                volume_mounts=volume_mounts,
                network_isolation=False  # Defer network isolation until after deployment
            )
            # Deploy network management script to new container
            self._deploy_network_script()
        else:
            self.docker.ensure_container_running(self.container_name)
            # Ensure network script is deployed (in case it was missing)
            self._deploy_network_script()
        
        # Load state
        state = self.state_manager.load_state(self.container_name)
        state["container_running"] = True
        # Update container IP in state
        container_ip = self.docker.get_container_ip(self.container_name)
        if container_ip:
            state["container_ip"] = container_ip
        self.state_manager.save_state(state, self.container_name)
        
        # Load servers config
        servers_config = load_servers_config(servers_config_path)
        
        # Ensure each required server is running
        for server_name in server_names:
            server_name_lower = server_name.lower()
            if server_name_lower not in servers_config:
                logger.warning(f"Server {server_name} not found in servers config, skipping")
                continue
            
            server_config = servers_config[server_name_lower]
            self.deploy_and_start_server(server_name, server_config, state)
        
        # Wait for all servers to be ready before proceeding
        # Use adaptive timeout: longer for servers with interceptors, shorter for normal servers
        self.wait_for_servers_ready(server_names, max_wait_seconds=30)
        
        # Save updated state
        self.state_manager.save_state(state, self.container_name)
    
    def run_pre_steps(
        self,
        server_names: List[str],
        servers_config_path: str = "configs/servers.json"
    ) -> None:
        """Execute optional pre_steps for the given servers inside the container.

        Pre-steps are commands defined per server in servers.json under the
        "pre_steps" array. They will be executed in the server's project
        directory and can use placeholders like {PROJECT_PATH} and {PORT}.

        Args:
            server_names: The list of server names to run pre-steps for.
            servers_config_path: Path to servers config JSON.
        """
        # Load servers config and current state
        servers_config = load_servers_config(servers_config_path)
        state = self.state_manager.load_state(self.container_name)

        for server_name in server_names:
            key = server_name.lower()
            if key not in servers_config:
                logger.debug(f"No config found for server {server_name}, skipping pre-steps")
                continue

            cfg = servers_config[key]
            pre_steps = cfg.get("pre_steps", []) or []
            if not pre_steps:
                logger.debug(f"No pre_steps for server {server_name}, skipping")
                continue

            # Resolve server path and port from state
            server_path = f"{self.deployer.base_path}/{server_name}"
            server_state = StateManager.get_server_state(server_name, state)
            port = (server_state or {}).get("port", 0)

            # Replace placeholders in each command
            replaced_commands = [self.deployer._replace_placeholders(cmd, server_path, port) for cmd in pre_steps]

            # Build environment with placeholders resolved and ensure PORT
            env = {k: self.deployer._replace_placeholders(str(v), server_path, port) for k, v in (cfg.get("env", {}) or {}).items()}
            if port:
                env["PORT"] = str(port)

            # Combine into a single bash session so env changes persist if any
            combined = " && ".join(replaced_commands)
            logger.info(f"Running pre-steps for {server_name} ({len(replaced_commands)} commands)")
            for idx, cmd in enumerate(replaced_commands):
                logger.info(f"  {idx + 1}. {cmd}")

            try:
                output = self.docker.exec_command(
                    self.container_name,
                    combined,
                    working_dir=server_path,
                    env=env
                )
                if output:
                    logger.debug(f"Pre-steps output for {server_name}: {output}")
            except Exception as e:
                logger.error(f"Pre-steps failed for {server_name}: {e}", exc_info=True)
                # Do not raise to avoid aborting entire workflow; log and continue
                # If pre-steps are critical, configure them inside the workflow itself
                continue

    def deploy_and_start_server(
        self,
        server_name: str,
        server_config: Dict[str, Any],
        state: Dict[str, Any]
    ) -> None:
        """Full deployment flow: deploy if needed, then start if not running."""
        # Initialize flag for interceptor restart (may be set during interceptor injection)
        needs_restart_for_interceptor = False
        
        server_state = StateManager.get_server_state(server_name, state)
        server_path = f"{self.deployer.base_path}/{server_name}"
        
        # Deploy if not deployed OR if directory doesn't exist (container may have been recreated)
        needs_deployment = False
        if not server_state or server_state.get("status") not in ["deployed", "running", "stopped"]:
            needs_deployment = True
        else:
            # Verify server directory actually exists (may have been deleted if container was recreated)
            try:
                check_result = self.docker.exec_command(
                    self.container_name,
                    f"test -d {server_path} && echo 'exists' || echo 'not_exists'",
                    check=False
                )
                if "not_exists" in check_result:
                    logger.warning(f"Server {server_name} is marked as deployed but directory {server_path} doesn't exist. Redeploying...")
                    needs_deployment = True
                else:
                    current_status = server_state.get("status", "unknown")
                    logger.debug(f"Server {server_name} is already deployed (status: {current_status}), skipping deployment")
            except Exception as e:
                logger.warning(f"Could not verify server directory exists for {server_name}: {e}. Attempting redeployment...")
                needs_deployment = True
        
        if needs_deployment:
            # If directory doesn't exist but state says deployed, clear the state first
            if server_state and server_state.get("status") in ["deployed", "running", "stopped"]:
                logger.info(f"Clearing stale deployment state for {server_name} before redeployment")
                # Remove server from state to force fresh deployment
                server_name_lower = server_name.lower()
                if "servers" in state and server_name_lower in state["servers"]:
                    del state["servers"][server_name_lower]
                    server_state = None
            
            self.deployer.deploy_server(server_name, server_config, state)
            server_state = StateManager.get_server_state(server_name, state)
            
            # Check if interceptors need to be injected (they should already be injected during deployment)
            # This is a fallback check - interceptors are always injected during deploy_server()
            # Only restart if interceptors were just injected (not if they already exist)
            if self.enable_interceptors:
                server_path = f"{self.deployer.base_path}/{server_name}"
                start_command = server_config.get("start_command", "")
                
                # Check if Python interceptor needs to be injected
                # Support both python and uv commands
                if "python" in start_command.lower() or ".py" in start_command or "uv" in start_command.lower():
                    python_file = None
                    
                    # Try to match uv commands: "uv ... run <file>"
                    uv_match = re.search(r'uv\s+(?:[^\s&|]+\s+)*run\s+([^\s&|]+)', start_command)
                    if uv_match:
                        python_file = uv_match.group(1).strip()
                    else:
                        # Try to match python commands: "python <file>" or "python3 <file>"
                        python_match = re.search(r'python3?\s+([^\s&|]+)', start_command)
                        if python_match:
                            python_file = python_match.group(1).strip()
                    
                    if python_file:
                        # Resolve file path (same logic as in _inject_python_interceptor)
                        if not python_file.startswith("/"):
                            # Check if start_command has --directory flag for uv
                            dir_match = re.search(r'--directory\s+([^\s&|]+)', start_command)
                            if dir_match:
                                # Replace placeholders in directory path
                                uv_dir = dir_match.group(1).strip()
                                uv_dir = uv_dir.replace('{PROJECT_PATH}', server_path)
                                uv_dir = uv_dir.replace('{PORT}', '')
                                uv_dir = uv_dir.rstrip('/')
                                python_file = f"{uv_dir}/{python_file}"
                            else:
                                python_file = f"{server_path}/{python_file}"
                        
                        # Check if interceptor is already injected
                        try:
                            content = self.docker.exec_command(
                                self.container_name,
                                f"cat {python_file}",
                                check=False
                            )
                            if content and "setup_python_interceptor" not in content:
                                # Interceptor not injected - inject it and mark for restart
                                logger.info(f"Interceptors enabled but not injected in {server_name}, injecting now")
                                self.deployer.deploy_interceptors(server_name, server_config, server_path)
                                needs_restart_for_interceptor = True
                                logger.info(f"Server {server_name} needs restart to use injected interceptors")
                            else:
                                # Interceptor already injected - no restart needed
                                # Server will use interceptors when it starts (if not already running)
                                logger.debug(f"Server {server_name} already has interceptors injected")
                        except Exception as e:
                            logger.debug(f"Could not check interceptor status for {server_name}: {e}")
                            # If we can't check, don't restart (safer to assume interceptors are there)
                    else:
                        logger.debug(f"Could not extract Python file from start_command for {server_name}: {start_command}")
                
                # Check if JavaScript interceptor needs to be deployed
                # JavaScript interceptors are now injected via NODE_OPTIONS, so no wrapper file needed
                elif "node" in start_command.lower() or "npm" in start_command.lower() or ".js" in start_command or 'ts' in start_command:
                    # Interceptor will be injected via NODE_OPTIONS in start_server method
                    # Just ensure interceptors are deployed (files copied to container)
                    logger.debug(f"JavaScript server detected for {server_name}. Interceptor will be injected via NODE_OPTIONS.")
                    # No restart needed - NODE_OPTIONS is set at runtime
        
        # Get or assign port
        port = server_state.get("port") if server_state else None
        if not port:
            # Get all ports already assigned in state file to avoid conflicts
            assigned_ports = set()
            for other_server_name, other_server_state in state.get("servers", {}).items():
                other_port = other_server_state.get("port")
                if other_port:
                    assigned_ports.add(other_port)
            
            port = self.docker.assign_port(self.container_name, start_port=20000, assigned_ports=assigned_ports)
            # Get container IP for endpoint
            container_ip = self.docker.get_container_ip(self.container_name)
            self.state_manager.register_server_port(server_name, port, state, container_ip)
            server_state = StateManager.get_server_state(server_name, state)
        
        # Check if server is running
        is_running = self.deployer.check_server_running(server_name, port)
        
        # Force restart servers when interceptors are enabled to ensure they pick up
        # fresh EXECUTION_ID and WORKFLOW_ID environment variables for each workflow execution
        force_restart_for_interceptors = self.enable_interceptors and is_running
        
        # Note: needs_restart_for_interceptor is only set when interceptors are just injected above
        # If interceptors already exist, we don't restart (assume server was started with them)
        # This ensures we only restart once when interceptors are first injected
        # However, with interceptors enabled, we always restart to get fresh execution_id
        
        if not is_running or needs_restart_for_interceptor or force_restart_for_interceptors:
            import time

            # Log restart reason if needed
            if needs_restart_for_interceptor:
                logger.debug(f"Restarting {server_name} (interceptor injection)")
            elif force_restart_for_interceptors:
                logger.debug(f"Restarting {server_name} (fresh execution_id/workflow_id)")
            elif not is_running:
                logger.debug(f"Starting {server_name} (not running)")
            
            # Stop the server first
            if is_running:
                logger.debug(f"Stopping {server_name} on port {port}")
                self.stop_server(server_name, port, state)
                # Wait a bit after stopping to ensure processes are fully terminated
                time.sleep(0.5)
            
            logger.debug(f"Starting {server_name} on port {port}")
            self.deployer.start_server(server_name, server_config, port, state)
            # Quick check: if server starts fast, we don't need to wait
            # wait_for_servers_ready will handle the actual readiness check
            time.sleep(0.3)  # Minimal wait for process to start
        else:
            logger.debug(f"Server {server_name} is already running on port {port}")
            # Update state to reflect running status
            self.state_manager.update_server_state(
                server_name,
                {"status": "running"},
                state
            )

    def remove_server(self, server_name: str, state: Dict[str, Any]) -> None:
        """Remove a server from container: stop it, remove files, and clean up state."""
        server_state = StateManager.get_server_state(server_name, state)
        if not server_state:
            logger.warning(f"Server {server_name} not found in state, nothing to remove")
            return
        
        port = server_state.get("port")
        status = server_state.get("status", "unknown")
        
        logger.info(f"Removing server {server_name} (status: {status}, port: {port})")
        
        # First, stop the server if it's running
        if status == "running" and port:
            try:
                self.stop_server(server_name, port, state)
                logger.info(f"Server {server_name} stopped before removal")
            except Exception as e:
                logger.warning(f"Failed to stop server {server_name} before removal: {e}")
        
        # Remove server files from container
        server_path = f"/mcp_servers/{server_name}"
        try:
            logger.info(f"Removing server files from {server_path}")
            self.docker.exec_command(
                self.container_name,
                f"rm -rf {server_path}"
            )
            logger.info(f"Server files removed from container")
        except Exception as e:
            logger.warning(f"Failed to remove server files: {e}")
        
        # Remove server from state
        server_name_lower = server_name.lower()
        if "servers" in state and server_name_lower in state["servers"]:
            del state["servers"][server_name_lower]
            logger.info(f"Server {server_name} removed from state")
        
        logger.info(f"Server {server_name} removed successfully")

    def stop_server(self, server_name: str, port: int, state: Dict[str, Any]) -> None:
        """Stop a running server."""
        server_state = StateManager.get_server_state(server_name, state)
        if not server_state:
            logger.warning(f"Server {server_name} not found in state")
            return
        
        transport = server_state.get("transport", "sse")
        logger.debug(f"Stopping server {server_name} on port {port} (transport: {transport})")
        
        try:
            if transport == "stdio":
                # Stop proxy process - find by port since proxy listens on the port
                # Method 1: Kill by checking environment variables of proxy processes (most reliable)
                # Find all stdio_to_sse_proxy.py processes and check their PORT env
                # Note: Environment variables are in /proc/pid/environ (null-separated)
                # Use printf to avoid null byte issues in Python string
                kill_script = (
                    "for pid in $(ps aux | grep '[s]tdio_to_sse_proxy.py' | awk '{print $2}'); do "
                    "  if [ -f /proc/$pid/environ ]; then "
                    "    port_env=$(cat /proc/$pid/environ 2>/dev/null | tr '\\0' '\\n' | grep '^PORT=' | cut -d'=' -f2); "
                    f"    if [ \"$port_env\" = \"{port}\" ]; then "
                    "      kill -9 $pid 2>/dev/null || true; "
                    "    fi; "
                    "  fi; "
                    "done || true"
                )
                logger.debug(f"Executing kill command for port {port}")
                try:
                    result = self.docker.exec_command(
                        self.container_name,
                        kill_script,
                        check=False
                    )
                    logger.debug(f"Kill command result: {result}")
                except Exception as e:
                    logger.warning(f"Kill command failed: {e}")
                
                # Method 2: Also kill the underlying server process (python src/server.py or node)
                # Find processes in the server directory
                server_path = f"/mcp_servers/{server_name}"
                self.docker.exec_command(
                    self.container_name,
                    f"ps aux | grep '{server_path}' | grep -v grep | awk '{{print $2}}' | xargs kill -9 2>/dev/null || true",
                    check=False
                )
                
                # Method 3: Kill process listening on the port (if lsof/fuser available)
                self.docker.exec_command(
                    self.container_name,
                    f"lsof -ti:{port} 2>/dev/null | xargs kill -9 2>/dev/null || true",
                    check=False
                )
                self.docker.exec_command(
                    self.container_name,
                    f"fuser -k {port}/tcp 2>/dev/null || true",
                    check=False
                )
                
                # Method 4: Kill by log file pattern (fallback)
                self.docker.exec_command(
                    self.container_name,
                    f"pkill -f '{server_name}_proxy.log' || true",
                    check=False
                )
            else:
                # Stop direct server process - find by port
                # Try lsof first (more reliable)
                self.docker.exec_command(
                    self.container_name,
                    f"lsof -ti:{port} 2>/dev/null | xargs kill -9 2>/dev/null || true",
                    check=False
                )
                # Fallback: kill processes that might be using the port
                self.docker.exec_command(
                    self.container_name,
                    f"fuser -k {port}/tcp 2>/dev/null || true",
                    check=False
                )
            
            # Wait a moment for processes to stop
            import time
            time.sleep(0.5)
            
            # Update state
            self.state_manager.update_server_state(
                server_name,
                {"status": "stopped"},
                state
            )
            logger.info(f"Server {server_name} stopped successfully")
        except Exception as e:
            logger.error(f"Failed to stop server {server_name}: {e}", exc_info=True)
            # Still update state to stopped
            self.state_manager.update_server_state(
                server_name,
                {"status": "stopped"},
                state
            )
            raise

    def wait_for_servers_ready(self, server_names: List[str], max_wait_seconds: int = 30) -> None:
        """Wait for servers to be ready (listening on ports and responding to health checks).
        
        Args:
            server_names: List of server names to wait for
            max_wait_seconds: Maximum time to wait per server in seconds
        """
        import time
        
        state = self.state_manager.load_state(self.container_name)
        
        for server_name in server_names:
            server_state = StateManager.get_server_state(server_name, state)
            if not server_state:
                logger.warning(f"Server {server_name} not found in state, skipping readiness check")
                continue
            
            port = server_state.get("port")
            if not port:
                logger.warning(f"Server {server_name} has no port assigned, skipping readiness check")
                continue
            
            logger.debug(f"Waiting for server {server_name} to be ready on port {port}...")
            server_start_time = time.time()
            
            # First, try immediate check (most servers start very fast)
            ready = self.deployer.check_server_running(server_name, port)
            if ready:
                elapsed = time.time() - server_start_time
                logger.debug(f"Server {server_name} is ready immediately ({elapsed:.2f}s)")
            else:
                # If not ready immediately, wait with short intervals
                attempts = 0
                while not ready and (time.time() - server_start_time) < max_wait_seconds:
                    attempts += 1
                    # Short wait intervals for fast response
                    time.sleep(0.2)
                    ready = self.deployer.check_server_running(server_name, port)
                    
                    if ready:
                        elapsed = time.time() - server_start_time
                        logger.info(f"Server {server_name} is ready after {elapsed:.1f}s")
                        break
                    else:
                        if attempts % 5 == 0:  # Log every second
                            elapsed = time.time() - server_start_time
                            logger.debug(f"Server {server_name} not ready yet (elapsed: {elapsed:.1f}s)")
            
            if not ready:
                elapsed = time.time() - server_start_time
                logger.warning(f"Server {server_name} did not become ready within {elapsed:.1f}s")
                logger.warning(f"Server may still be starting up or may have failed to start")
    
    def get_server_endpoint(self, server_name: str) -> Optional[str]:
        """Get server URL from state file."""
        state = self.state_manager.load_state(self.container_name)
        server_state = StateManager.get_server_state(server_name, state)
        if server_state:
            return server_state.get("endpoint")
        return None

    def get_all_server_endpoints(self) -> Dict[str, str]:
        """Get all server endpoints from state."""
        state = self.state_manager.load_state(self.container_name)
        endpoints = {}
        for server_name, server_state in state.get("servers", {}).items():
            endpoint = server_state.get("endpoint")
            if endpoint:
                endpoints[server_name] = endpoint
        return endpoints
    
    async def validate_servers_accessible(
        self, 
        server_names: List[str], 
        servers_config_path: str = "configs/servers.json"
    ) -> None:
        """Validate that all required servers are accessible before workflow execution.
        
        Tests connectivity and tool listing for each server. Raises RuntimeError
        if any server fails, aborting the workflow.
        
        Args:
            server_names: List of server names to validate
            servers_config_path: Path to servers config file
            
        Raises:
            RuntimeError: If any server is not accessible or fails to list tools
        """
        from ..config import load_servers_config
        from ..tools.http_mcp_client import HTTPMCPClient
        
        logger.info(f"Validating {len(server_names)} required server(s) before workflow execution")
        
        servers_config = load_servers_config(servers_config_path)
        failed_servers = []
        
        for server_name in server_names:
            server_key = server_name.lower()
            if server_key not in servers_config:
                error_msg = f"Server '{server_name}' not found in servers config"
                logger.error(error_msg)
                failed_servers.append((server_name, error_msg))
                continue
            
            server_config = servers_config[server_key]
            endpoint = self.get_server_endpoint(server_name)
            
            if not endpoint:
                error_msg = f"Server '{server_name}' has no endpoint - deployment may have failed"
                logger.error(error_msg)
                failed_servers.append((server_name, error_msg))
                continue
            
            # Test connectivity and tool listing
            try:
                logger.debug(f"Testing connectivity to server '{server_name}' at {endpoint}")
                client = HTTPMCPClient(
                    server_name,
                    "sse",
                    endpoint,
                    **server_config.get("kwargs", {})
                )
                
                # Connect and list tools
                await client.connect()
                tools = await client.list_tools()
                await client.close()
                
                logger.info(f"✓ Server '{server_name}' is accessible ({len(tools)} tools available)")
            except Exception as e:
                error_msg = f"Failed to connect or list tools from server '{server_name}': {e}"
                logger.error(error_msg)
                failed_servers.append((server_name, error_msg))
        
        # Abort workflow if any server failed
        if failed_servers:
            failed_names = [name for name, _ in failed_servers]
            error_details = "\n".join([f"  - {name}: {msg}" for name, msg in failed_servers])
            error_msg = (
                f"CRITICAL: {len(failed_servers)} of {len(server_names)} required server(s) failed validation.\n"
                f"Failed servers: {failed_names}\n"
                f"Details:\n{error_details}\n"
                f"Workflow cannot proceed without all required servers. Please check server deployment and connectivity."
            )
            logger.critical(error_msg)
            raise RuntimeError(error_msg)
        
        logger.info(f"✓ All {len(server_names)} required server(s) validated successfully")
    
    def _deploy_network_script(self) -> None:
        """Deploy network management script to container."""
        script_path = "/mcp_scripts"
        script_file = f"{script_path}/manage_network.sh"
        
        # Ensure script directory exists
        self.docker.exec_command(self.container_name, f"mkdir -p {script_path}")
        
        # Check if script already exists
        result = self.docker.exec_command(
            self.container_name,
            f"test -f {script_file}",
            check=False
        )
        
        if result == "":  # Script exists
            logger.debug(f"Network management script already exists, skipping deployment")
            return
        
        # Deploy script
        script_source = Path(__file__).parent / "scripts" / "manage_network.sh"
        if script_source.exists():
            self.docker.copy_to_container(
                self.container_name,
                str(script_source),
                script_file
            )
            # Make script executable
            self.docker.exec_command(self.container_name, f"chmod +x {script_file}")
            logger.debug(f"Network management script deployed to {script_file}")
        else:
            logger.warning(f"Network script source not found at {script_source}")
    
    def _get_volume_mounts(self) -> Dict[str, str]:
        """Get volume mounts for container.
        
        Mounts entire workflow_logs directory to /mcp_logs for replay logging.
        New structure: {server_name}/{execution_id}/http_replay and {server_name}/{execution_id}/llm_replay
        """
        # Get project root (assume we're in src/container/server_manager.py)
        project_root = Path(__file__).parent.parent.parent
        local_logs_dir = project_root / "workflow_logs"
        
        # Ensure local directory exists
        local_logs_dir.mkdir(parents=True, exist_ok=True)
        
        # Return volume mount mapping - mount entire workflow_logs to /mcp_logs
        return {
            str(local_logs_dir.absolute()): "/mcp_logs"
        }

