"""HTTP Request Logger - delegates to unified ReplayLogger.

All HTTP logs are written to:
    /mcp_logs/{workflow_id}/{execution_id}/http_replay/{server_name}/requests.jsonl

When replay mode is enabled (HTTP_REPLAY_MODE env var), logging is skipped.

Note: This module uses absolute imports (e.g., `from replay.replay_logger`) that rely on
PYTHONPATH=/mcp_interceptors being set in the container environment (see Dockerfile.base).
"""

from __future__ import annotations

import json as json_module
import logging
import os
from datetime import datetime
from typing import Any, Dict, Optional
import threading


# Debug file logger - writes to /tmp/interceptor.log for troubleshooting
def _debug_log(msg: str) -> None:
    """Write debug message to /tmp/interceptor.log."""
    try:
        with open("/tmp/interceptor.log", "a") as f:
            f.write(f"[{datetime.now().isoformat()}] [http_logger] {msg}\n")
    except Exception:
        pass

# Container code uses standard logging directly
# No need to import host's logging_config
def get_logger(name: str):
    """Get logger using standard logging module."""
    return logging.getLogger(name)

logger = get_logger(__name__)



def _get_replay_logger():
    """Import ReplayLogger using importlib to avoid triggering replay/__init__.py imports."""
    try:
        import importlib.util
        from pathlib import Path
        
        # Try container path first
        replay_logger_path = Path("/mcp_interceptors/replay/replay_logger.py")
        _debug_log(f"Checking ReplayLogger at: {replay_logger_path}")
        if not replay_logger_path.exists():
            # Fallback to relative path
            replay_logger_path = Path(__file__).parent.parent / "replay" / "replay_logger.py"
            _debug_log(f"Container path not found, trying relative: {replay_logger_path}")
        
        if replay_logger_path.exists():
            _debug_log(f"ReplayLogger file found at: {replay_logger_path}")
            # Load module directly from file to avoid triggering __init__.py
            spec = importlib.util.spec_from_file_location("replay_logger", str(replay_logger_path))
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                _debug_log(f"Successfully imported ReplayLogger via importlib")
                return module.ReplayLogger
            else:
                _debug_log(f"Failed to create spec or loader for ReplayLogger")
        else:
            _debug_log(f"ReplayLogger file not found at either path")
    except Exception as e:
        import traceback
        _debug_log(f"Failed to import ReplayLogger via importlib: {e}")
        _debug_log(f"Traceback: {traceback.format_exc()}")
    
    # Fallback: try package import (may fail if __init__.py has issues)
    try:
        _debug_log(f"Trying package import: from replay.replay_logger import ReplayLogger")
        from replay.replay_logger import ReplayLogger  # type: ignore
        _debug_log(f"Successfully imported ReplayLogger via package import")
        return ReplayLogger
    except ImportError as e:
        import traceback
        _debug_log(f"Failed to import ReplayLogger via package import: {e}")
        _debug_log(f"Traceback: {traceback.format_exc()}")
    
    logger.debug("Could not import ReplayLogger")
    return None


class HTTPRequestLogger:
    """HTTP Request Logger - delegates to unified ReplayLogger."""
    
    _instance: Optional["HTTPRequestLogger"] = None
    _lock = threading.Lock()
    
    def __init__(self, log_dir: Optional[str] = None, server_name: Optional[str] = None):
        self.server_name = server_name or os.environ.get("MCP_SERVER_NAME") or os.environ.get("SERVER_NAME") or "unknown"
        self._replay_logger_class = _get_replay_logger()
    
    def log_request(
        self,
        language: str,
        method: str,
        url: str,
        headers: Dict[str, str],
        body: Optional[Any] = None,
        response_status: Optional[int] = None,
        response_headers: Optional[Dict[str, str]] = None,
        response_body: Optional[Any] = None,
        duration_ms: Optional[float] = None,
        error: Optional[str] = None,
        server_name: Optional[str] = None,
        execution_id: Optional[str] = None,
        workflow_id: Optional[str] = None,
    ) -> None:
        """Log an HTTP request.
        
        In replay mode, this is a no-op.
        Without workflow context (workflow_id, execution_id), logging is skipped.
        """
        _debug_log(f"log_request called: {method} {url}")
        
        # Fallback to environment variables if not provided
        if not execution_id:
            execution_id = os.environ.get("EXECUTION_ID")
        if not workflow_id:
            workflow_id = os.environ.get("WORKFLOW_ID")
        
        _debug_log(f"  execution_id={execution_id}, workflow_id={workflow_id}")
        
        # Skip if no workflow context
        if not workflow_id or not execution_id:
            _debug_log(f"  Skipping (no workflow context)")
            logger.debug(f"Skipping HTTP log (no workflow context): {method} {url}")
            return
        
        final_server_name = server_name or self.server_name
        
        # Build log entry
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "language": language,
            "server": final_server_name,
            "execution_id": execution_id,
            "workflow_id": workflow_id,
            "request": {
                "method": method,
                "url": url,
                "headers": headers,
                "body": self._serialize_body(body),
            },
            "response": {
                "status": response_status,
                "headers": response_headers or {},
                "body": self._serialize_body(response_body),
            } if response_status is not None else None,
            "duration_ms": duration_ms,
            "error": error,
        }
        
        # Write via ReplayLogger
        _debug_log(f"  _replay_logger_class={self._replay_logger_class}")
        if self._replay_logger_class:
            try:
                replay_logger = self._replay_logger_class.get_instance(
                    server_name=final_server_name,
                    workflow_id=workflow_id,
                    execution_id=execution_id,
                )
                log_file_path = replay_logger.log_file if hasattr(replay_logger, 'log_file') else 'unknown'
                _debug_log(f"  Writing to ReplayLogger: {log_file_path}")
                _debug_log(f"  ReplayLogger instance: workflow_id={replay_logger.workflow_id}, execution_id={replay_logger.execution_id}, server_name={replay_logger.server_name}")
                replay_logger.log_request(log_entry)
                _debug_log(f"  Write successful")
            except Exception as e:
                import traceback
                _debug_log(f"  Write failed: {e}")
                _debug_log(f"  Traceback: {traceback.format_exc()}")
                logger.debug(f"Failed to write HTTP log: {e}")
        else:
            _debug_log(f"  No ReplayLogger class available, skipping write")
            _debug_log(f"  Attempted import path: /mcp_interceptors/replay/replay_logger.py")
        
        logger.debug(f"HTTP [{language}] {method} {url} -> {response_status or 'ERROR'}")
    
    def _serialize_body(self, body: Any) -> Optional[str]:
        """Serialize request/response body to string."""
        if body is None:
            return None
        if isinstance(body, bytes):
            return body.decode('utf-8', errors='replace')
        if isinstance(body, str):
            return body
        try:
            return json_module.dumps(body, ensure_ascii=False)
        except Exception:
            return str(body)
    
    @classmethod
    def get_instance(cls, log_dir: Optional[str] = None, server_name: Optional[str] = None) -> "HTTPRequestLogger":
        """Get singleton instance of the logger."""
        if server_name is None:
            server_name = os.environ.get("MCP_SERVER_NAME") or os.environ.get("SERVER_NAME")
        
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(log_dir, server_name)
        
        return cls._instance
