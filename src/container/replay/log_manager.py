"""Log Manager for managing HTTP replay logs."""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

from ._logging import get_logger

logger = get_logger(__name__)


class LogManager:
    """Manages HTTP replay logs: cleanup, statistics, export, etc."""
    
    def __init__(self, replay_dir: Path):
        """
        Args:
            replay_dir: Replay log root directory (local path: workflow_logs, new structure: {server_name}/{execution_id}/http_replay)
        """
        self.replay_dir = Path(replay_dir)
        if not self.replay_dir.exists():
            self.replay_dir.mkdir(parents=True, exist_ok=True)
    
    def list_executions(self, workflow_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """List all execution records
        
        Args:
            workflow_id: Optional workflow ID filter
            
        Returns:
            List of execution records, each containing execution_id, workflow_id, start_time, end_time, status, etc.
        """
        executions = []
        
        # Iterate through all workflow directories
        for workflow_dir in self.replay_dir.iterdir():
            if not workflow_dir.is_dir():
                continue
            
            wf_id = workflow_dir.name
            if workflow_id and wf_id != workflow_id:
                continue
            
            # Iterate through each execution directory
            for exec_dir in workflow_dir.iterdir():
                if not exec_dir.is_dir():
                    continue
                
                exec_id = exec_dir.name
                metadata_file = exec_dir / "metadata.json"
                
                if metadata_file.exists():
                    try:
                        with open(metadata_file, "r", encoding="utf-8") as f:
                            metadata = json.load(f)
                            executions.append({
                                "execution_id": exec_id,
                                "workflow_id": wf_id,
                                "workflow_name": metadata.get("workflow_name", wf_id),
                                "start_time": metadata.get("start_time"),
                                "end_time": metadata.get("end_time"),
                                "status": metadata.get("status", "unknown"),
                                "servers": metadata.get("servers", []),
                                "total_requests": metadata.get("total_requests", 0),
                                "replay_available": metadata.get("replay_available", True),
                                "path": str(exec_dir)
                            })
                    except Exception as e:
                        logger.warning(f"Failed to read metadata from {metadata_file}: {e}")
                else:
                    # If metadata.json doesn't exist, try to infer from directory structure
                    # New structure: {execution_id}/http_replay/{server_name}/
                    http_replay_dir = exec_dir / "http_replay"
                    if http_replay_dir.exists():
                        server_dirs = [d for d in http_replay_dir.iterdir() if d.is_dir()]
                        if server_dirs:
                            executions.append({
                                "execution_id": exec_id,
                                "workflow_id": wf_id,
                                "workflow_name": wf_id,
                                "start_time": None,
                                "end_time": None,
                                "status": "unknown",
                                "servers": [d.name for d in server_dirs],
                                "total_requests": 0,
                                "replay_available": True,
                                "path": str(exec_dir)
                            })
        
        # Sort by start_time (newest first)
        executions.sort(key=lambda x: x.get("start_time") or "", reverse=True)
        return executions
    
    def get_execution(self, execution_id: str, workflow_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Get detailed information for a specific execution record
        
        Args:
            execution_id: Execution ID
            workflow_id: Optional workflow ID (for quick lookup)
            
        Returns:
            Execution record details, or None if not found
        """
        if workflow_id:
            exec_dir = self.replay_dir / workflow_id / execution_id
        else:
            # Search through all workflow directories
            for workflow_dir in self.replay_dir.iterdir():
                if not workflow_dir.is_dir():
                    continue
                exec_dir = workflow_dir / execution_id
                if exec_dir.exists():
                    workflow_id = workflow_dir.name
                    break
            else:
                return None
        
        if not exec_dir.exists():
            return None
        
        metadata_file = exec_dir / "metadata.json"
        metadata = {}
        if metadata_file.exists():
            try:
                with open(metadata_file, "r", encoding="utf-8") as f:
                    metadata = json.load(f)
            except Exception as e:
                logger.warning(f"Failed to read metadata from {metadata_file}: {e}")
        
        # Count requests per server
        # New structure: {execution_id}/http_replay/{server_name}/requests.jsonl
        server_stats = {}
        total_requests = 0
        http_replay_dir = exec_dir / "http_replay"
        if http_replay_dir.exists():
            for server_dir in http_replay_dir.iterdir():
                if not server_dir.is_dir():
                    continue
                
                log_file = server_dir / "requests.jsonl"
                if log_file.exists():
                    try:
                        count = sum(1 for _ in open(log_file, "r", encoding="utf-8") if _.strip())
                        server_stats[server_dir.name] = count
                        total_requests += count
                    except Exception:
                        pass
        
        return {
            "execution_id": execution_id,
            "workflow_id": workflow_id or metadata.get("workflow_id"),
            "workflow_name": metadata.get("workflow_name", workflow_id),
            "start_time": metadata.get("start_time"),
            "end_time": metadata.get("end_time"),
            "status": metadata.get("status", "unknown"),
            "servers": metadata.get("servers", list(server_stats.keys())),
            "total_requests": metadata.get("total_requests", total_requests),
            "server_stats": server_stats,
            "replay_available": metadata.get("replay_available", True),
            "path": str(exec_dir)
        }
    
    def delete_execution(self, execution_id: str, workflow_id: Optional[str] = None) -> bool:
        """Delete a specific execution record
        
        Args:
            execution_id: Execution ID
            workflow_id: Optional workflow ID (for quick lookup)
            
        Returns:
            True if deleted, False if not found
        """
        if workflow_id:
            exec_dir = self.replay_dir / workflow_id / execution_id
        else:
            # Search through all workflow directories
            for workflow_dir in self.replay_dir.iterdir():
                if not workflow_dir.is_dir():
                    continue
                exec_dir = workflow_dir / execution_id
                if exec_dir.exists():
                    workflow_id = workflow_dir.name
                    break
            else:
                return False
        
        if not exec_dir.exists():
            return False
        
        try:
            shutil.rmtree(exec_dir)
            logger.info(f"Deleted execution {execution_id} for workflow {workflow_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete execution {execution_id}: {e}")
            return False
    
    def cleanup_old_executions(
        self,
        workflow_id: str,
        keep: int = 10,
        dry_run: bool = False
    ) -> List[str]:
        """Clean up old execution records, keeping the most recent N executions
        
        Args:
            workflow_id: Workflow ID
            keep: Number of execution records to keep
            dry_run: If True, only return the list to be deleted without actually deleting
            
        Returns:
            List of deleted execution IDs
        """
        workflow_dir = self.replay_dir / workflow_id
        if not workflow_dir.exists():
            return []
        
        # Get all execution records
        executions = []
        for exec_dir in workflow_dir.iterdir():
            if not exec_dir.is_dir():
                continue
            
            exec_id = exec_dir.name
            metadata_file = exec_dir / "metadata.json"
            
            end_time = None
            if metadata_file.exists():
                try:
                    with open(metadata_file, "r", encoding="utf-8") as f:
                        metadata = json.load(f)
                        end_time = metadata.get("end_time")
                except Exception:
                    pass
            
            # If no end_time, use directory modification time
            if not end_time:
                try:
                    end_time = datetime.fromtimestamp(exec_dir.stat().st_mtime).isoformat()
                except Exception:
                    end_time = ""
            
            executions.append((end_time, exec_id))
        
        # Sort by end_time (newest first)
        executions.sort(reverse=True)
        
        # Keep the first N, delete the rest
        to_delete = [exec_id for _, exec_id in executions[keep:]]
        
        if not dry_run:
            deleted = []
            for exec_id in to_delete:
                if self.delete_execution(exec_id, workflow_id):
                    deleted.append(exec_id)
            return deleted
        else:
            return to_delete
    
    def export_execution(self, execution_id: str, output_path: Path, workflow_id: Optional[str] = None) -> bool:
        """Export execution record to specified path
        
        Args:
            execution_id: Execution ID
            output_path: Output file path (JSON format)
            workflow_id: Optional workflow ID
            
        Returns:
            True if exported successfully
        """
        exec_info = self.get_execution(execution_id, workflow_id)
        if not exec_info:
            return False
        
        exec_dir = Path(exec_info["path"])
        
        # Collect all log data
        export_data = {
            "execution_id": exec_info["execution_id"],
            "workflow_id": exec_info["workflow_id"],
            "workflow_name": exec_info["workflow_name"],
            "start_time": exec_info["start_time"],
            "end_time": exec_info["end_time"],
            "status": exec_info["status"],
            "servers": exec_info["servers"],
            "total_requests": exec_info["total_requests"],
            "server_stats": exec_info["server_stats"],
            "requests": {}
        }
        
        # Read logs for each server
        # New structure: {execution_id}/http_replay/{server_name}/requests.jsonl
        http_replay_dir = exec_dir / "http_replay"
        for server_name in exec_info["servers"]:
            server_dir = http_replay_dir / server_name
            log_file = server_dir / "requests.jsonl"
            
            if log_file.exists():
                requests = []
                try:
                    with open(log_file, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if line:
                                try:
                                    requests.append(json.loads(line))
                                except json.JSONDecodeError:
                                    pass
                    export_data["requests"][server_name] = requests
                except Exception as e:
                    logger.warning(f"Failed to read log file {log_file}: {e}")
        
        # Write export file
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(export_data, f, indent=2, ensure_ascii=False)
            logger.info(f"Exported execution {execution_id} to {output_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to export execution {execution_id}: {e}")
            return False
    
    def get_stats(self, workflow_id: Optional[str] = None) -> Dict[str, Any]:
        """Get statistics
        
        Args:
            workflow_id: Optional workflow ID filter
            
        Returns:
            Statistics dictionary
        """
        executions = self.list_executions(workflow_id)
        
        stats = {
            "total_executions": len(executions),
            "by_status": {},
            "by_workflow": {},
            "total_requests": 0,
            "oldest_execution": None,
            "newest_execution": None
        }
        
        for exec_info in executions:
            # Count by status
            status = exec_info.get("status", "unknown")
            stats["by_status"][status] = stats["by_status"].get(status, 0) + 1
            
            # Count by workflow
            wf_id = exec_info.get("workflow_id", "unknown")
            if wf_id not in stats["by_workflow"]:
                stats["by_workflow"][wf_id] = {
                    "count": 0,
                    "total_requests": 0
                }
            stats["by_workflow"][wf_id]["count"] += 1
            stats["by_workflow"][wf_id]["total_requests"] += exec_info.get("total_requests", 0)
            
            # Total requests
            stats["total_requests"] += exec_info.get("total_requests", 0)
            
            # Oldest and newest execution
            start_time = exec_info.get("start_time")
            if start_time:
                if not stats["oldest_execution"] or start_time < stats["oldest_execution"]:
                    stats["oldest_execution"] = start_time
                if not stats["newest_execution"] or start_time > stats["newest_execution"]:
                    stats["newest_execution"] = start_time
        
        return stats
    
    def find_latest_execution(self, workflow_id: str) -> Optional[str]:
        """Find the latest execution ID for a specific workflow
        
        Args:
            workflow_id: Workflow ID
            
        Returns:
            Latest execution_id, or None if not found
        """
        executions = self.list_executions(workflow_id)
        if not executions:
            return None
        
        # Already sorted by start_time (newest first)
        return executions[0]["execution_id"]

