"""State management for container server status."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from ..logging_config import get_logger

logger = get_logger(__name__)


class StateManager:
    """Manages state.json files for tracking server deployment status."""

    @staticmethod
    def get_state_path(container_name: str) -> Path:
        """Get state file path: containers/{container_name}/state.json"""
        container_dir = Path("containers") / container_name
        container_dir.mkdir(parents=True, exist_ok=True)
        return container_dir / "state.json"

    @staticmethod
    def load_state(container_name: str) -> Dict[str, Any]:
        """Load state.json for container. Returns empty state if file doesn't exist."""
        state_path = StateManager.get_state_path(container_name)
        if not state_path.exists():
            logger.debug(f"State file not found for {container_name}, returning empty state")
            return {
                "container_name": container_name,
                "container_running": False,
                "servers": {}
            }
        
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                state = json.load(f)
            logger.debug(f"Loaded state for container {container_name}")
            return state
        except Exception as e:
            logger.error(f"Failed to load state file {state_path}: {e}", exc_info=True)
            return {
                "container_name": container_name,
                "container_running": False,
                "servers": {}
            }

    @staticmethod
    def save_state(state: Dict[str, Any], container_name: str) -> None:
        """Save state.json for container."""
        state_path = StateManager.get_state_path(container_name)
        try:
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
            logger.debug(f"Saved state for container {container_name}")
        except Exception as e:
            logger.error(f"Failed to save state file {state_path}: {e}", exc_info=True)
            raise
    
    @staticmethod
    def reset_state(container_name: str) -> None:
        """Reset state file for container (used when starting fresh container).
        
        Resets server states to empty, keeping container metadata.
        """
        state = StateManager.load_state(container_name)
        # Reset servers but keep container metadata
        state["servers"] = {}
        state["container_running"] = False
        # Clear container IP as it may change with new container
        if "container_ip" in state:
            del state["container_ip"]
        StateManager.save_state(state, container_name)
        logger.info(f"Reset state for container {container_name} (cleared all server states)")

    @staticmethod
    @staticmethod
    def get_server_state(server_name: str, state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Get server status from state."""
        servers = state.get("servers", {})
        return servers.get(server_name.lower())

    @staticmethod
    def update_server_state(server_name: str, updates: Dict[str, Any], state: Dict[str, Any]) -> None:
        """Update server info in state."""
        server_name_lower = server_name.lower()
        if "servers" not in state:
            state["servers"] = {}
        
        if server_name_lower not in state["servers"]:
            state["servers"][server_name_lower] = {}
        
        state["servers"][server_name_lower].update(updates)
        logger.debug(f"Updated state for server {server_name}: {updates}")

    @staticmethod
    def register_server_port(server_name: str, port: int, state: Dict[str, Any], container_ip: Optional[str] = None) -> None:
        """Register port assignment for server."""
        if container_ip:
            endpoint = f"http://{container_ip}:{port}"
        else:
            endpoint = f"http://localhost:{port}"  # Fallback if IP not available
        StateManager.update_server_state(
            server_name,
            {
                "port": port,
                "endpoint": endpoint,
                "deployed_at": datetime.utcnow().isoformat() + "Z"
            },
            state
        )

