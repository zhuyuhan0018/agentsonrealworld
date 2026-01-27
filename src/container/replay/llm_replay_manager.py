"""LLM Replay Manager for recording and replaying LLM responses."""

from __future__ import annotations

import json
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List

from ._logging import get_logger

logger = get_logger(__name__)


class LLMReplayManager:
    """
    Manage LLM response recording and replay.
    
    In recording mode: Saves LLM prompt-response pairs to a JSONL file.
    In replay mode: Loads recorded responses and returns them in order.
    
    Log structure:
        {replay_dir}/{workflow_id}/{execution_id}/llm_replay/llm_responses.jsonl
    """
    
    def __init__(
        self, 
        execution_id: str, 
        replay_dir: Path,
        workflow_id: Optional[str] = None
    ):
        """
        Args:
            execution_id: Execution ID (for locating replay logs)
            replay_dir: Replay log root directory
            workflow_id: Workflow ID (required for recording, optional for replay)
        """
        self.execution_id = execution_id
        self.replay_dir = Path(replay_dir)
        self.workflow_id = workflow_id
        
        # Cache for replay mode: list of responses in order
        self._replay_cache: List[Dict[str, Any]] = []
        self._replay_index: int = 0
        self._loaded: bool = False
        
        # Log file path: {workflow_id}/{execution_id}/llm_replay/llm_responses.jsonl
        # Note: LLM logs are workflow-level, not server-level
        if workflow_id:
            self._log_file = self.replay_dir / workflow_id / execution_id / "llm_replay" / "llm_responses.jsonl"
        else:
            self._log_file = None
    
    def _ensure_log_dir(self) -> None:
        """Ensure the log directory exists."""
        if self._log_file:
            self._log_file.parent.mkdir(parents=True, exist_ok=True)
    
    def _make_prompt_hash(self, prompt: str) -> str:
        """Create a hash of the prompt for verification."""
        return hashlib.md5(prompt.encode('utf-8')).hexdigest()[:12]
    
    def record_response(
        self,
        step: int,
        step_type: str,
        prompt: str,
        response_content: Optional[str],
        tool_calls: Optional[List[Dict[str, Any]]],
        model_name: str,
        duration_ms: int = 0
    ) -> None:
        """
        Record an LLM response for replay.
        
        Args:
            step: Step number in the workflow (0 for planning, 1+ for execution)
            step_type: Type of step ("planning", "execution", "react")
            prompt: The prompt sent to the LLM
            response_content: Text content of the response
            tool_calls: List of tool calls from the response
            model_name: Name of the model
            duration_ms: Time taken for the LLM call
        """
        if not self._log_file:
            logger.warning("Cannot record response: workflow_id not set")
            return
        
        self._ensure_log_dir()
        
        entry = {
            "timestamp": datetime.now().isoformat(),
            "execution_id": self.execution_id,
            "workflow_id": self.workflow_id,
            "step": step,
            "step_type": step_type,
            "model": model_name,
            "prompt_hash": self._make_prompt_hash(prompt),
            "prompt_length": len(prompt),
            "prompt": prompt,  # Store full prompt for debugging/verification
            "response": {
                "content": response_content,
                "tool_calls": tool_calls
            },
            "duration_ms": duration_ms
        }
        
        try:
            with open(self._log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            logger.debug(f"Recorded LLM response for step {step} ({step_type})")
        except Exception as e:
            logger.error(f"Failed to record LLM response: {e}")
    
    def load_replay_data(self) -> None:
        """
        Load recorded LLM responses for replay.
        
        Searches for llm_responses.jsonl at {workflow_id}/{execution_id}/llm_replay/llm_responses.jsonl.
        If workflow_id is not provided, searches all workflow directories to find the execution_id.
        """
        if self._loaded:
            return
        
        logger.info(f"Loading LLM replay data for execution {self.execution_id}")
        
        # Find the log file(s) for this execution
        log_files: List[Path] = []
        
        if self.replay_dir.exists():
            for workflow_dir in self.replay_dir.iterdir():
                if workflow_dir.is_dir():
                    execution_path = workflow_dir / self.execution_id / "llm_replay"
                    if execution_path.exists() and execution_path.is_dir():
                        log_file = execution_path / "llm_responses.jsonl"
                        if log_file.exists():
                            log_files.append(log_file)
                            # Also set workflow_id if not set, and update _log_file
                            if not self.workflow_id:
                                self.workflow_id = workflow_dir.name
                                # Update _log_file now that we know workflow_id
                                self._log_file = self.replay_dir / self.workflow_id / self.execution_id / "llm_replay" / "llm_responses.jsonl"
        
        if not log_files:
            logger.warning(f"No LLM replay data found for execution {self.execution_id}")
            self._loaded = True
            return
        
        # Load all entries and sort by step
        all_entries: List[Dict[str, Any]] = []
        
        for log_file in log_files:
            logger.debug(f"Loading LLM replay data from {log_file}")
            try:
                with open(log_file, "r", encoding="utf-8") as f:
                    for line_num, line in enumerate(f, 1):
                        line = line.strip()
                        if not line:
                            continue
                        
                        try:
                            entry = json.loads(line)
                            
                            # Verify execution_id matches
                            if entry.get("execution_id") != self.execution_id:
                                continue
                            
                            all_entries.append(entry)
                        except json.JSONDecodeError as e:
                            logger.warning(f"Failed to parse JSON at line {line_num}: {e}")
                            continue
            except Exception as e:
                logger.error(f"Failed to read LLM log file {log_file}: {e}")
        
        # Sort by step number to ensure correct order
        all_entries.sort(key=lambda x: (x.get("step", 0), x.get("timestamp", "")))
        
        self._replay_cache = all_entries
        self._replay_index = 0
        self._loaded = True
        
        logger.info(f"Loaded {len(all_entries)} LLM response(s) for replay")
    
    def get_next_response(self) -> Optional[Dict[str, Any]]:
        """
        Get the next recorded LLM response in order.
        
        Returns:
            The recorded response dict with 'content' and 'tool_calls' keys,
            or None if no more responses.
        """
        if not self._loaded:
            self.load_replay_data()
        
        if self._replay_index >= len(self._replay_cache):
            logger.warning(f"No more LLM responses to replay (index={self._replay_index})")
            return None
        
        entry = self._replay_cache[self._replay_index]
        self._replay_index += 1
        
        logger.debug(
            f"Replaying LLM response {self._replay_index}/{len(self._replay_cache)} "
            f"(step={entry.get('step')}, type={entry.get('step_type')})"
        )
        
        return entry.get("response")
    
    def peek_next_response(self) -> Optional[Dict[str, Any]]:
        """
        Peek at the next response without advancing the index.
        
        Returns:
            The next response entry (full entry, not just response), or None.
        """
        if not self._loaded:
            self.load_replay_data()
        
        if self._replay_index >= len(self._replay_cache):
            return None
        
        return self._replay_cache[self._replay_index]
    
    def get_response_by_step(self, step: int, step_type: str) -> Optional[Dict[str, Any]]:
        """
        Get a specific response by step number and type.
        
        Args:
            step: Step number
            step_type: Step type ("planning", "execution", "react")
            
        Returns:
            The response dict, or None if not found.
        """
        if not self._loaded:
            self.load_replay_data()
        
        for entry in self._replay_cache:
            if entry.get("step") == step and entry.get("step_type") == step_type:
                return entry.get("response")
        
        logger.warning(f"No LLM response found for step={step}, type={step_type}")
        return None
    
    def reset_replay_index(self) -> None:
        """Reset the replay index to start from the beginning."""
        self._replay_index = 0
        logger.debug("Reset LLM replay index to 0")
    
    @property
    def total_responses(self) -> int:
        """Get the total number of recorded responses."""
        if not self._loaded:
            self.load_replay_data()
        return len(self._replay_cache)
    
    @property
    def remaining_responses(self) -> int:
        """Get the number of remaining responses to replay."""
        if not self._loaded:
            self.load_replay_data()
        return max(0, len(self._replay_cache) - self._replay_index)

