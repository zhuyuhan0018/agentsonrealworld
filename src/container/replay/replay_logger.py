"""Unified HTTP Logger for writing HTTP request logs.

All HTTP logs are written to a fixed location:
    /mcp_logs/{workflow_id}/{execution_id}/http_replay/{server_name}/requests.jsonl

When replay mode is enabled (HTTP_REPLAY_MODE env var is set), logging is skipped
since we're replaying recorded responses, not making new requests.

Note: This module uses absolute imports (e.g., `from replay._logging`) that rely on
PYTHONPATH=/mcp_interceptors being set in the container environment (see Dockerfile.base).
"""

from __future__ import annotations

import json as json_module
import builtins
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional
import threading

# Save original functions before they get intercepted
_original_open = builtins.open
_original_makedirs = os.makedirs

# Import logger using absolute import (works with PYTHONPATH=/mcp_interceptors)
# Fallback chain: package import -> absolute import -> standard logging
def _get_logger(name: str) -> logging.Logger:
    # Try relative import first (when used as a package)
    try:
        from ._logging import get_logger as _pkg_get_logger
        return _pkg_get_logger(name)
    except (ImportError, ValueError):
        pass
    
    # Try absolute import (when PYTHONPATH includes /mcp_interceptors)
    try:
        from replay._logging import get_logger as _abs_get_logger  # type: ignore
        return _abs_get_logger(name)
    except ImportError:
        pass
    
    # Fallback to standard logging
    return logging.getLogger(name)

logger = _get_logger(__name__)

# Default replay directory (in container) - now mounts entire workflow_logs
DEFAULT_REPLAY_DIR = Path("/mcp_logs")


class ReplayLogger:
    """Unified HTTP Logger for writing request logs.
    
    Log location: {replay_dir}/{workflow_id}/{execution_id}/http_replay/{server_name}/requests.jsonl
    
    When replay mode is enabled, logging is disabled since we're replaying,
    not making new requests.
    """
    
    _instances: Dict[str, "ReplayLogger"] = {}
    _lock = threading.Lock()
    
    def __init__(
        self,
        replay_dir: Path,
        workflow_id: str,
        execution_id: str,
        server_name: str,
    ):
        """
        Args:
            replay_dir: Root directory for HTTP logs
            workflow_id: Workflow ID
            execution_id: Execution ID
            server_name: Name of the MCP server
        """
        self.replay_dir = Path(replay_dir)
        self.workflow_id = workflow_id
        self.execution_id = execution_id
        self.server_name = server_name
        
        # Check if we're in replay mode (skip logging)
        
    
        self.log_dir = self.replay_dir / workflow_id / execution_id / "http_replay" / server_name
        self.log_file = self.log_dir / "requests.jsonl"
        
        # Ensure directory exists
        try:
            _original_makedirs(self.log_dir, exist_ok=True)
        except Exception:
            # Fallback to /tmp
            self.log_dir = Path("/tmp/mcp_logs") / workflow_id / execution_id / "http_replay" / server_name
            _original_makedirs(self.log_dir, exist_ok=True)
            self.log_file = self.log_dir / "requests.jsonl"
        
        self._file_lock = threading.Lock()
    
    def log_request(self, log_entry: Dict[str, Any]) -> None:
        """Write HTTP request log entry to file.
        
        In replay mode, this is a no-op since we're replaying recorded responses.
        
        Args:
            log_entry: Log entry dict (contains request, response, etc.)
        """
        
        log_line = json_module.dumps(log_entry, ensure_ascii=False) + "\n"
        
        with self._file_lock:
            try:
                with _original_open(self.log_file, "a", encoding="utf-8") as f:
                    f.write(log_line)
            except Exception as e:
                logger.error(f"Failed to write HTTP log: {e}")
    
    @classmethod
    def get_instance(
        cls,
        server_name: str,
        workflow_id: str,
        execution_id: str,
        replay_dir: Optional[Path] = None,
    ) -> "ReplayLogger":
        """Get or create a ReplayLogger instance.
        
        Args:
            server_name: Name of the MCP server
            workflow_id: Workflow ID
            execution_id: Execution ID  
            replay_dir: Root directory for logs (defaults to /mcp_logs, which is mounted from workflow_logs)
        """
        if replay_dir is None:
            replay_dir = DEFAULT_REPLAY_DIR
        
        instance_key = f"{replay_dir}:{workflow_id}:{execution_id}:{server_name}"
        
        if instance_key not in cls._instances:
            with cls._lock:
                if instance_key not in cls._instances:
                    cls._instances[instance_key] = cls(
                        replay_dir=replay_dir,
                        workflow_id=workflow_id,
                        execution_id=execution_id,
                        server_name=server_name,
                    )
        
        return cls._instances[instance_key]
