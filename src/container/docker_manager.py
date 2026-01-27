"""Docker container management."""

from __future__ import annotations

import subprocess
import time
from typing import Optional, Dict, Any, Set

from ..logging_config import get_logger

logger = get_logger(__name__)


class DockerManager:
    """Manages Docker container lifecycle and operations."""

    @staticmethod
    def _run_command(cmd: list[str], check: bool = True, timeout: Optional[float] = None) -> subprocess.CompletedProcess:
        """Run a docker command and return the result."""
        logger.debug(f"Running command: {' '.join(cmd)}")
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=check,
                timeout=timeout,
                stdin=subprocess.DEVNULL  # Explicitly close stdin to prevent hanging
            )
            if result.stdout:
                logger.debug(f"Command output: {result.stdout.strip()}")
            return result
        except subprocess.TimeoutExpired as e:
            logger.warning(f"Command timed out after {timeout}s: {' '.join(cmd)}")
            raise
        except subprocess.CalledProcessError as e:
            logger.error(f"Command failed: {' '.join(cmd)}")
            logger.error(f"Error: {e.stderr}")
            raise

    @staticmethod
    def container_exists(name: str) -> bool:
        """Check if container exists."""
        try:
            result = DockerManager._run_command(
                ["docker", "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}"],
                check=False
            )
            return name in result.stdout
        except Exception as e:
            logger.error(f"Failed to check if container exists: {e}")
            return False

    @staticmethod
    def container_is_running(name: str) -> bool:
        """Check if container is running."""
        try:
            result = DockerManager._run_command(
                ["docker", "ps", "--filter", f"name=^{name}$", "--format", "{{.Names}}"],
                check=False
            )
            return name in result.stdout
        except Exception as e:
            logger.error(f"Failed to check if container is running: {e}")
            return False

    @staticmethod
    def start_container(image: str, name: str, volume_mounts: Optional[Dict[str, str]] = None, 
                       network_isolation: bool = False) -> None:
        """Create/start container. If container exists but is stopped, start it.
        
        Args:
            image: Docker image name
            name: Container name
            volume_mounts: Optional dict mapping local paths to container paths for volume mounts
            network_isolation: If True, block external internet access while allowing host-container communication
        """
        if DockerManager.container_exists(name):
            if DockerManager.container_is_running(name):
                logger.info(f"Container {name} is already running")
                return
            else:
                logger.info(f"Starting existing container {name}")
                DockerManager._run_command(["docker", "start", name])
                return
        
        logger.info(f"Creating new container {name} from image {image}")
        # Create container with bridge network (allows host-to-container communication)
        cmd = [
            "docker", "run", "-d",
            "--name", name,
        ]
        
        # Always add NET_ADMIN capability by default (allows network isolation to be enabled/disabled anytime)
        # Use bridge network (allows host-container communication via port mapping)
        # Network isolation can be enabled/disabled at runtime via enable_network_isolation()/disable_network_isolation()
        cmd.extend([
            "--network", "bridge",
            "--cap-add", "NET_ADMIN",  # Required for iptables (enables dynamic network isolation)
        ])
        
        if network_isolation:
            logger.info(f"Network isolation enabled at container creation")
            logger.info(f"Host-to-container communication will still work via bridge network")
        else:
            logger.debug(f"Container created with NET_ADMIN capability (network isolation can be enabled anytime)")
        
        # Add volume mounts if provided
        if volume_mounts:
            for local_path, container_path in volume_mounts.items():
                cmd.extend(["-v", f"{local_path}:{container_path}"])
                logger.debug(f"Adding volume mount: {local_path} -> {container_path}")
        
        cmd.extend([
            image,
            "tail", "-f", "/dev/null"  # Keep container running
        ])
        
        DockerManager._run_command(cmd)
        
        # After container starts, block outbound traffic if network isolation is enabled
        # Note: All dependencies (aiohttp, aiohttp-sse) should be pre-installed in base image
        if network_isolation:
            DockerManager._block_outbound_traffic(name)
        
        logger.info(f"Container {name} created and started")

    @staticmethod
    def _block_outbound_traffic(container_name: str) -> None:
        """Block outbound internet traffic from container while allowing host-container communication.
        
        Uses iptables rules inside the container to block all outbound traffic except:
        - Localhost (127.0.0.1)
        - Private network ranges (for container-to-container and host communication)
        - Docker bridge network (172.17.0.0/16)
        
        Note: OUTPUT rules only affect outbound traffic FROM the container.
        Inbound connections TO the container are not affected by OUTPUT rules.
        """
        try:
            logger.info(f"Blocking external internet access for container {container_name}")
            
            # First, flush existing OUTPUT rules to start clean
            flush_cmd = "iptables -F OUTPUT 2>/dev/null || true"
            DockerManager._run_command(
                ["docker", "exec", container_name, "sh", "-c", flush_cmd],
                check=False
            )
            
            # Block outbound traffic using iptables
            # Strategy: Allow private networks and localhost first, then drop everything else
            # This ensures Docker bridge network (172.17.0.0/16) communication works
            block_commands = [
                # Allow localhost (for internal container communication)
                "iptables -A OUTPUT -d 127.0.0.0/8 -j ACCEPT 2>/dev/null || true",
                # Allow Docker bridge network (172.17.0.0/16) - critical for host-container communication
                "iptables -A OUTPUT -d 172.17.0.0/16 -j ACCEPT 2>/dev/null || true",
                # Allow other private networks (for container-to-container communication)
                "iptables -A OUTPUT -d 172.16.0.0/12 -j ACCEPT 2>/dev/null || true",
                "iptables -A OUTPUT -d 10.0.0.0/8 -j ACCEPT 2>/dev/null || true",
                "iptables -A OUTPUT -d 192.168.0.0/16 -j ACCEPT 2>/dev/null || true",
                # Drop all other outbound traffic (external internet)
                # This will block external IPs while allowing all private network communication
                "iptables -A OUTPUT -j DROP 2>/dev/null || true",
            ]
            
            # Execute iptables commands inside container
            # Note: Container needs NET_ADMIN capability (added in start_container)
            for cmd in block_commands:
                try:
                    DockerManager._run_command(
                        ["docker", "exec", container_name, "sh", "-c", cmd],
                        check=False
                    )
                except Exception as e:
                    logger.debug(f"Failed to execute iptables command: {e}")
            
            # Verify rules were applied
            try:
                result = DockerManager._run_command(
                    ["docker", "exec", container_name, "sh", "-c", "iptables -L OUTPUT -n -v 2>/dev/null || true"],
                    check=False
                )
                logger.debug(f"Iptables OUTPUT rules:\n{result.stdout}")
            except Exception:
                pass
            
            logger.info(f"External internet access blocked for {container_name}")
            logger.info(f"Container can still communicate with host via Docker bridge network (172.17.0.0/16)")
        except Exception as e:
            logger.warning(f"Failed to block outbound traffic: {e}")
            logger.warning(f"Container {container_name} may still have external network access")
    
    @staticmethod
    def _allow_outbound_traffic(container_name: str) -> None:
        """Remove network isolation by flushing OUTPUT iptables rules.
        
        This restores full internet access to the container.
        """
        try:
            logger.debug(f"Removing network isolation for container {container_name}")
            
            # Flush OUTPUT rules to restore internet access
            flush_cmd = "iptables -F OUTPUT 2>/dev/null || true"
            DockerManager._run_command(
                ["docker", "exec", container_name, "sh", "-c", flush_cmd],
                check=False
            )
            
            logger.debug(f"Network isolation removed for {container_name}")
            logger.debug(f"Container now has full internet access")
        except Exception as e:
            logger.warning(f"Failed to remove network isolation: {e}")
            logger.warning(f"Container {container_name} may still have network isolation")
    
    @staticmethod
    def enable_network_isolation(container_name: str) -> None:
        """Enable network isolation for a container (public API).
        
        Blocks external internet access while allowing host-container communication.
        Container must have NET_ADMIN capability (added by default).
        """
        DockerManager._block_outbound_traffic(container_name)
    
    @staticmethod
    def disable_network_isolation(container_name: str) -> None:
        """Disable network isolation for a container (public API).
        
        Restores full internet access.
        """
        DockerManager._allow_outbound_traffic(container_name)

    @staticmethod
    def stop_container(name: str) -> None:
        """Stop container."""
        if not DockerManager.container_exists(name):
            logger.warning(f"Container {name} does not exist")
            return
        
        if not DockerManager.container_is_running(name):
            logger.info(f"Container {name} is already stopped")
            return
        
        logger.info(f"Stopping container {name}")
        DockerManager._run_command(["docker", "stop", name])
        logger.info(f"Container {name} stopped")

    @staticmethod
    def remove_container(name: str) -> None:
        """Remove container (must be stopped first)."""
        if not DockerManager.container_exists(name):
            logger.warning(f"Container {name} does not exist")
            return
        
        # Check if container is already being removed
        try:
            result = DockerManager._run_command(
                ["docker", "ps", "-a", "--filter", f"name={name}", "--format", "{{.Status}}"],
                check=False
            )
            status = result.stdout.strip().lower() if result.stdout else ""
            if "removal in progress" in status:
                logger.warning(f"Container {name} is already being removed. Waiting for removal to complete...")
                # Wait a bit and check if it's gone
                for _ in range(10):  # Wait up to 5 seconds
                    time.sleep(0.5)
                    if not DockerManager.container_exists(name):
                        logger.info(f"Container {name} has been removed")
                        return
                logger.warning(f"Container {name} is still in removal state. Attempting force removal...")
        except Exception as e:
            logger.debug(f"Could not check container status: {e}")
        
        if DockerManager.container_is_running(name):
            logger.info(f"Stopping container {name} before removal")
            DockerManager.stop_container(name)
        
        logger.info(f"Removing container {name}")
        # Try normal removal first
        result = DockerManager._run_command(["docker", "rm", name], check=False)
        if result.returncode != 0:
            # If normal removal fails, try force removal
            logger.warning(f"Normal removal failed, attempting force removal: {result.stderr}")
            result = DockerManager._run_command(["docker", "rm", "-f", name], check=False)
            if result.returncode != 0:
                error_msg = f"Failed to remove container {name}: {result.stderr}"
                logger.error(error_msg)
                raise RuntimeError(error_msg)
        logger.info(f"Container {name} removed")

    @staticmethod
    def ensure_container_running(name: str) -> None:
        """Check and start container if needed."""
        if not DockerManager.container_exists(name):
            raise RuntimeError(f"Container {name} does not exist. Please create it first.")
        
        if not DockerManager.container_is_running(name):
            logger.info(f"Starting container {name}")
            DockerManager._run_command(["docker", "start", name])
        else:
            logger.debug(f"Container {name} is already running")

    @staticmethod
    def exec_command(name: str, command: str, working_dir: Optional[str] = None, env: Optional[Dict[str, str]] = None, check: bool = True) -> str:
        """Execute command in container. Returns stdout.
        Uses bash instead of sh to support bash builtins like 'source'.
        
        Args:
            name: Container name
            command: Command to execute
            working_dir: Optional working directory
            env: Optional environment variables dict
            check: If True, raise exception on non-zero exit code. If False, return empty string on failure.
        """
        cmd = ["docker", "exec"]
        
        if working_dir:
            cmd.extend(["-w", working_dir])
        
        if env:
            for key, value in env.items():
                cmd.extend(["-e", f"{key}={value}"])
        
        cmd.append(name)
        # Use bash to execute complex commands with pipes, redirects, source, etc.
        cmd.extend(["bash", "-c", command])
        
        try:
            result = DockerManager._run_command(cmd, check=check)
            return result.stdout.strip()
        except subprocess.CalledProcessError as e:
            if check:
                # Log detailed error information
                logger.error(f"Command failed: {command}")
                if e.stderr:
                    logger.error(f"Stderr: {e.stderr}")
                if e.stdout:
                    logger.error(f"Stdout: {e.stdout}")
                # Re-raise with original exception to preserve stderr/stdout
                raise
            return ""

    @staticmethod
    def copy_to_container(name: str, local_path: str, container_path: str) -> None:
        """Copy files/directories to container."""
        DockerManager._run_command([
            "docker", "cp",
            local_path,
            f"{name}:{container_path}"
        ])
        logger.debug(f"Copied {local_path} to {name}:{container_path}")

    @staticmethod
    def port_is_available(name: str, port: int) -> bool:
        """Check if port is available (not in use by container)."""
        try:
            # Use a simpler, more reliable approach:
            # Try ss first (faster), then netstat as fallback
            # Use explicit exit codes and ensure command terminates
            # The command should exit immediately after checking
            cmd = (
                f"ss -ltn 2>/dev/null | grep -qE ':{port}[[:space:]]' && exit 1 || "
                f"netstat -ltn 2>/dev/null | grep -qE ':{port}[[:space:]]' && exit 1 || "
                f"exit 0"
            )
            result = DockerManager._run_command(
                ["docker", "exec", name, "bash", "-c", cmd],
                check=False,
                timeout=2.0
            )
            # Exit 0 = port not found = available (return True)
            # Exit 1 = port found = not available (return False)
            return result.returncode == 0
        except subprocess.TimeoutExpired:
            logger.debug(f"Port check timed out for port {port}, assuming available")
            return True
        except Exception as e:
            logger.debug(f"Error checking port availability: {e}, assuming available")
            # If command fails, assume port might be available
            return True

    @staticmethod
    def assign_port(name: str, start_port: int = 20000, assigned_ports: Optional[Set[int]] = None) -> int:
        """Auto-assign available port starting from start_port.
        
        Args:
            name: Container name
            start_port: Starting port number
            assigned_ports: Set of ports already assigned in state file (to avoid conflicts)
        
        Returns:
            Available port number
        """
        assigned_ports = assigned_ports or set()
        port = start_port
        max_port = start_port + 1000  # Prevent infinite loop
        while port < max_port:
            # Check both container availability and state file assignments
            if port not in assigned_ports and DockerManager.port_is_available(name, port):
                logger.debug(f"Assigned port {port} for container {name}")
                return port
            port += 1
        
        raise RuntimeError(f"Could not find available port starting from {start_port}")

    @staticmethod
    def get_process_info(name: str, pattern: str) -> list[str]:
        """Get process info matching pattern in container."""
        try:
            result = DockerManager._run_command(
                ["docker", "exec", name, "sh", "-c", f"ps aux | grep '{pattern}' | grep -v grep"],
                check=False
            )
            if result.stdout:
                return [line.strip() for line in result.stdout.strip().split('\n') if line.strip()]
            return []
        except Exception:
            return []

    @staticmethod
    def get_container_ip(name: str) -> Optional[str]:
        """Get container IP address."""
        try:
            result = DockerManager._run_command(
                ["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", name],
                check=False
            )
            ip = result.stdout.strip()
            if ip:
                return ip
            # Fallback: try getting IP from network inspect
            result = DockerManager._run_command(
                ["docker", "inspect", "-f", "{{.NetworkSettings.IPAddress}}", name],
                check=False
            )
            ip = result.stdout.strip()
            return ip if ip else None
        except Exception as e:
            logger.error(f"Failed to get container IP for {name}: {e}")
            return None

