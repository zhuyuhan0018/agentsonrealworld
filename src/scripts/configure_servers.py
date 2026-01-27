#!/usr/bin/env python3
"""Standalone script to configure and deploy MCP servers in Docker container."""

import argparse
import sys
from pathlib import Path
from typing import Optional

# Add project root to path (script is at src/scripts/, so 2 levels up)
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.config import PipelineConfig, load_servers_config
from src.container.server_manager import ServerManager
from src.container.server_deployer import ServerDeployer
from src.container.docker_manager import DockerManager
from src.container.state_manager import StateManager
from src.logging_config import setup_logging, get_logger


def main() -> None:
    parser = argparse.ArgumentParser(description="Configure and deploy MCP servers in Docker container")
    parser.add_argument("--config", required=True, help="Pipeline YAML config file (to get container settings)")
    parser.add_argument("--servers", default="configs/servers.json", help="Servers config file")
    parser.add_argument("--server", help="Deploy/stop/remove specific server (optional, all servers if omitted)")
    parser.add_argument("--new-container", action="store_true", help="Delete existing container and start fresh")
    parser.add_argument("--stop", action="store_true", help="Stop servers instead of starting them")
    parser.add_argument("--stop-all", action="store_true", help="Stop all running servers")
    parser.add_argument("--remove", action="store_true", help="Remove server(s) from container (stops, removes files, and cleans state)")
    parser.add_argument("--remove-all", action="store_true", help="Remove all servers from container")
    parser.add_argument("--attach", action="store_true", help="After deployment, attach to container for manual configuration")
    parser.add_argument("--scan", action="store_true", help="Scan server status and update state.json (does not deploy/start servers)")
    parser.add_argument("--log", action="store_true", help="Show execution logs for a specific server")
    parser.add_argument("--log-lines", type=int, default=100, help="Number of lines to show from log file (default: 100, use 0 for all)")
    parser.add_argument("--log-follow", action="store_true", help="Follow log output (like tail -f)")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], 
                       default="INFO", help="Console log level (default: INFO)")
    
    args = parser.parse_args()
    
    # Setup logging
    logger = setup_logging(console_level=args.log_level, file_level="DEBUG")
    logger.info("=" * 60)
    logger.info("MCP Server Configuration Script")
    logger.info("=" * 60)
    
    try:
        # Load pipeline config
        logger.info(f"Loading pipeline config: {args.config}")
        cfg = PipelineConfig.from_yaml(args.config)
        
        container_config = cfg.get_container_config()
        if not container_config:
            logger.error("No container configuration found in pipeline config. Set container_config_path in pipeline config.")
            sys.exit(1)
        
        logger.info(f"Container: {container_config.name}, Image: {container_config.image}")
        
        # Initialize managers
        # Note: Interceptors are always injected during deployment (regardless of enable_interceptors flag)
        # They only activate when EXECUTION_ID and WORKFLOW_ID environment variables are set
        server_manager = ServerManager(container_config, enable_interceptors=True)
        docker_manager = DockerManager()
        state_manager = StateManager()
        deployer = ServerDeployer(container_config.name, enable_interceptors=True)
        
        # Load state once for operations that need it (log, remove, stop, scan)
        # Deployment loads state after container creation, so handled separately
        state= {}
        needs_state = args.log or args.remove or args.remove_all or args.stop or args.stop_all or args.scan
        
        if needs_state:
            # Ensure container exists and is running (except scan which can work without container)
            if not args.scan:
                if not docker_manager.container_exists(container_config.name):
                    logger.error(f"Container {container_config.name} does not exist")
                    sys.exit(1)
                docker_manager.ensure_container_running(container_config.name)
            
            # Load state (create empty state for scan if container doesn't exist)
            try:
                state = state_manager.load_state(container_config.name)
            except Exception:
                if args.scan:
                    state = {"container_name": container_config.name, "servers": {}}
                else:
                    raise
        
        # Handle log viewing operation
        if args.log:
            if not args.server:
                logger.error("Please specify --server <name> to show logs for a specific server")
                sys.exit(1)
            server_name = args.server
            server_state = StateManager.get_server_state(server_name, state)
            if not server_state:
                logger.error(f"Server {args.server} not found in state")
                sys.exit(1)
            
            transport = server_state.get("transport", "stdio")
            log_file = f"/tmp/{server_name}{'_proxy' if transport == 'stdio' else ''}.log"
            
            logger.info(f"Showing logs for server: {args.server} (log file: {log_file})")
            logger.info("=" * 60)
            
            # Check if log file exists
            if "not_exists" in DockerManager.exec_command(
                container_config.name, f"test -f {log_file} && echo 'exists' || echo 'not_exists'", check=False
            ):
                logger.warning(f"Log file {log_file} does not exist. Server may not have been started yet.")
                sys.exit(1)
            
            # Show logs
            if args.log_follow:
                logger.info("Following log output (press Ctrl+C to stop)...")
                import subprocess
                try:
                    subprocess.run(["docker", "exec", container_config.name, "tail", "-f", log_file], check=True)
                except KeyboardInterrupt:
                    logger.info("\nStopped following logs")
            else:
                cmd = f"tail -n {args.log_lines} {log_file}" if args.log_lines > 0 else f"cat {log_file}"
                log_output = DockerManager.exec_command(container_config.name, cmd, check=False)
                print(log_output) if log_output else logger.warning("Log file is empty")
            
            logger.info("=" * 60)
            return
        
        # Handle scan operation (check server status)
        if args.scan:
            # Load servers config
            logger.info(f"Loading servers config: {args.servers}")
            servers_config = load_servers_config(args.servers)
            logger.info(f"Found {len(servers_config)} servers in config")
            
            # Check container running status
            container_running = docker_manager.container_is_running(container_config.name)
            state["container_running"] = container_running
            logger.info(f"Container running: {container_running}")
            
            if not container_running:
                logger.warning("Container is not running. All servers will be marked as stopped.")
                # Mark all servers as stopped
                for server_name in servers_config.keys():
                    StateManager.update_server_state(
                        server_name,
                        {"status": "stopped"},
                        state
                    )
            else:
                # Check each server
                for server_name, server_config in servers_config.items():
                    logger.info(f"Checking server: {server_name}")
                    
                    server_state = StateManager.get_server_state(server_name, state)
                    
                    if not server_state:
                        logger.debug(f"Server {server_name} not found in state, marking as not_deployed")
                        StateManager.update_server_state(
                            server_name,
                            {"status": "not_deployed"},
                            state
                        )
                        continue
                    
                    port = server_state.get("port")
                    if not port:
                        logger.debug(f"Server {server_name} has no port assigned, marking as not_deployed")
                        StateManager.update_server_state(
                            server_name,
                            {"status": "not_deployed"},
                            state
                        )
                        continue
                    
                    # Check if server is running
                    is_running = deployer.check_server_running(server_name, port)
                    
                    if is_running:
                        # Get container IP for endpoint
                        container_ip = docker_manager.get_container_ip(container_config.name)
                        endpoint = f"http://{container_ip}:{port}" if container_ip else f"http://localhost:{port}"
                        logger.info(f"Server {server_name} is running on port {port} (endpoint: {endpoint})")
                        StateManager.update_server_state(
                            server_name,
                            {
                                "status": "running",
                                "endpoint": endpoint
                            },
                            state
                        )
                    else:
                        logger.info(f"Server {server_name} is not running")
                        StateManager.update_server_state(
                            server_name,
                            {"status": "stopped"},
                            state
                        )
            
            # Save updated state
            state_manager.save_state(state, container_config.name)
            logger.info("State updated successfully")
            
            # Print summary
            logger.info("=" * 60)
            logger.info("Status Summary:")
            logger.info(f"Container: {container_config.name} ({'running' if container_running else 'stopped'})")
            for server_name, server_state in state.get("servers", {}).items():
                status = server_state.get("status", "unknown")
                port = server_state.get("port", "N/A")
                logger.info(f"  {server_name}: {status} (port: {port})")
            logger.info("=" * 60)
            return
        
        # Handle remove operations
        if args.remove or args.remove_all:
            
            # Determine which servers to remove
            if args.remove_all:
                # Remove all servers in state
                servers_to_remove = list(state.get("servers", {}).keys())
                logger.info(f"Removing all servers: {servers_to_remove}")
            elif args.server:
                servers_to_remove = [args.server.lower()]
                logger.info(f"Removing specific server: {args.server}")
            else:
                logger.error("Please specify --server <name> or --remove-all")
                sys.exit(1)
            
            # Remove servers
            for server_name in servers_to_remove:
                try:
                    server_manager.remove_server(server_name, state)
                    logger.info(f"Successfully removed server: {server_name}")
                except Exception as e:
                    logger.error(f"Failed to remove server {server_name}: {e}", exc_info=True)
            
            # Save state
            state_manager.save_state(state, container_config.name)
            logger.info("State saved successfully")
            
            logger.info("=" * 60)
            logger.info("Remove operation completed")
            logger.info("=" * 60)
            return
        
        # Handle stop operations
        if args.stop or args.stop_all:
            
            # Determine which servers to stop
            if args.stop_all:
                # Stop all servers in state
                servers_to_stop = list(state.get("servers", {}).keys())
                logger.info(f"Stopping all servers: {servers_to_stop}")
            elif args.server:
                servers_to_stop = [args.server.lower()]
                logger.info(f"Stopping specific server: {args.server}")
            else:
                logger.error("Please specify --server <name> or --stop-all")
                sys.exit(1)
            
            # Stop servers
            for server_name in servers_to_stop:
                server_state = state_manager.get_server_state(server_name, state)
                if not server_state:
                    logger.warning(f"Server {server_name} not found in state, skipping")
                    continue
                
                port = server_state.get("port")
                if not port:
                    logger.warning(f"Server {server_name} has no port assigned, skipping")
                    continue
                
                try:
                    server_manager.stop_server(server_name, port, state)
                    logger.info(f"Successfully stopped server: {server_name}")
                except Exception as e:
                    logger.error(f"Failed to stop server {server_name}: {e}", exc_info=True)
            
            # Save state
            state_manager.save_state(state, container_config.name)
            logger.info("State saved successfully")
            
            logger.info("=" * 60)
            logger.info("Stop operation completed")
            logger.info("=" * 60)
            return
        
        # Load servers config for deployment
        logger.info(f"Loading servers config: {args.servers}")
        servers_config = load_servers_config(args.servers)
        logger.info(f"Found {len(servers_config)} servers in config")
        
        # Start new container if requested
        if args.new_container:
            logger.info("Starting new container (removing existing if present)")
            if docker_manager.container_exists(container_config.name):
                docker_manager.remove_container(container_config.name)
        
        # Ensure container is running
        if not docker_manager.container_exists(container_config.name):
            logger.info(f"Creating container {container_config.name}")
            docker_manager.start_container(container_config.image, container_config.name)
        else:
            docker_manager.ensure_container_running(container_config.name)
        
        # Load state if not already loaded
        if state is None:
            state = state_manager.load_state(container_config.name)
        state["container_running"] = True
        
       
        
        # Determine which servers to deploy
        if args.server:
            server_names = [args.server]
            logger.info(f"Deploying specific server: {args.server}")
        else:
            server_names = list(servers_config.keys())
            logger.info(f"Deploying all servers: {server_names}")
        
        # Deploy and start servers
        deployed_servers = []
        for server_name in server_names:
            server_name_lower = server_name.lower()
            if server_name_lower not in servers_config:
                logger.warning(f"Server {server_name} not found in servers config, skipping")
                continue
            
            server_config = servers_config[server_name_lower]
            logger.info(f"Processing server: {server_name}")
            
            try:
                server_manager.deploy_and_start_server(server_name, server_config, state)
                logger.info(f"Successfully deployed and started server: {server_name}")
                deployed_servers.append(server_name)
            except Exception as e:
                logger.error(f"Failed to deploy server {server_name}: {e}", exc_info=True)
        
        # Save state
        state_manager.save_state(state, container_config.name)
        logger.info("State saved successfully")

        # Execute optional pre_steps for any successfully started servers
        if deployed_servers:
            try:
                logger.info(f"Executing pre_steps for servers: {deployed_servers}")
                server_manager.run_pre_steps(deployed_servers, args.servers)
            except Exception as e:
                logger.warning(f"Pre_steps execution encountered an error (continuing): {e}")
        
        # Attach to container if requested
        if args.attach:
            logger.info("Attaching to container for manual configuration...")
            logger.info("Type 'exit' to leave the container")
            import subprocess
            subprocess.run(["docker", "exec", "-it", container_config.name, "/bin/bash"])
        
        logger.info("=" * 60)
        logger.info("Configuration completed successfully")
        logger.info("=" * 60)
        
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()

