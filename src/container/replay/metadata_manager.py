"""Metadata Manager for execution metadata."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional

from ._logging import get_logger

logger = get_logger(__name__)


class MetadataManager:
    """Manages execution metadata (metadata.json)"""
    
    def __init__(self, replay_dir: Path, workflow_id: str, execution_id: str):
        """
        Args:
            replay_dir: Replay log root directory (container path: /mcp_logs, mounted from workflow_logs)
            workflow_id: Workflow ID
            execution_id: Execution ID
        """
        self.replay_dir = Path(replay_dir)
        self.workflow_id = workflow_id
        self.execution_id = execution_id
        # Metadata is stored at: {workflow_id}/{execution_id}/metadata.json
        self.metadata_file = self.replay_dir / workflow_id / execution_id / "metadata.json"
    
    def create_metadata(
        self,
        workflow_name: Optional[str] = None,
        servers: Optional[list[str]] = None,
        start_time: Optional[str] = None
    ) -> None:
        """Create execution metadata file
        
        Args:
            workflow_name: Workflow name (optional)
            servers: Server list (optional)
            start_time: Start time (ISO format, optional, defaults to current time)
        """
        if start_time is None:
            start_time = datetime.now().isoformat()
        
        metadata = {
            "execution_id": self.execution_id,
            "workflow_id": self.workflow_id,
            "workflow_name": workflow_name or self.workflow_id,
            "start_time": start_time,
            "end_time": None,
            "status": "running",
            "servers": servers or [],
            "total_requests": 0,
            "replay_available": True
        }
        
        # Ensure directory exists
        self.metadata_file.parent.mkdir(parents=True, exist_ok=True)
        
        # Write metadata
        with open(self.metadata_file, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)
        
        logger.debug(f"Created metadata file: {self.metadata_file}")
    
    def update_metadata(
        self,
        status: Optional[str] = None,
        end_time: Optional[str] = None,
        total_requests: Optional[int] = None
    ) -> None:
        """Update execution metadata
        
        Args:
            status: Status (completed, failed, interrupted)
            end_time: End time (ISO format)
            total_requests: Total number of requests
        """
        if not self.metadata_file.exists():
            logger.warning(f"Metadata file not found: {self.metadata_file}, creating new one")
            self.create_metadata()
            return
        
        try:
            with open(self.metadata_file, "r", encoding="utf-8") as f:
                metadata = json.load(f)
            
            if status is not None:
                metadata["status"] = status
            if end_time is not None:
                metadata["end_time"] = end_time
            elif status in ["completed", "failed", "interrupted"] and metadata.get("end_time") is None:
                metadata["end_time"] = datetime.now().isoformat()
            if total_requests is not None:
                metadata["total_requests"] = total_requests
            
            with open(self.metadata_file, "w", encoding="utf-8") as f:
                json.dump(metadata, f, indent=2, ensure_ascii=False)
            
            logger.debug(f"Updated metadata file: {self.metadata_file}")
        except Exception as e:
            logger.error(f"Failed to update metadata: {e}", exc_info=True)

