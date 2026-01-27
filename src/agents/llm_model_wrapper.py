"""
LLM Model Wrapper for recording and replaying LLM responses.

This module provides a wrapper around LangChain chat models that can:
1. Recording mode: Invoke the real LLM and record responses for later replay.
2. Replay mode: Return recorded responses without calling the real LLM.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage

from ..logging_config import get_logger

if TYPE_CHECKING:
    from ..container.replay.llm_replay_manager import LLMReplayManager

logger = get_logger(__name__)


class ReplayableModel:
    """
    A wrapper around LangChain chat models that supports recording and replay.
    
    In recording mode:
        - Calls the real LLM and records prompt-response pairs
        - Responses are saved to llm_responses.jsonl
    
    In replay mode:
        - Returns recorded responses in order without calling the LLM
        - Verifies step progression matches recorded data
    
    Usage:
        # Recording mode
        wrapper = ReplayableModel(model, replay_manager, is_replay=False)
        wrapped_model = wrapper.bind_tools(tools)
        response = await wrapped_model.ainvoke(prompt)
        
        # Replay mode
        wrapper = ReplayableModel(model, replay_manager, is_replay=True)
        wrapped_model = wrapper.bind_tools(tools)
        response = await wrapped_model.ainvoke(prompt)  # Returns recorded response
    """
    
    def __init__(
        self,
        model: BaseChatModel,
        replay_manager: Optional["LLMReplayManager"],
        is_replay: bool = False,
        model_name: Optional[str] = None
    ):
        """
        Args:
            model: The underlying LangChain chat model
            replay_manager: LLMReplayManager instance for recording/replay
            is_replay: True for replay mode, False for recording mode
            model_name: Optional model name for logging
        """
        self._model = model
        self._replay_manager = replay_manager
        self._is_replay = is_replay
        self._model_name = model_name or self._get_model_name()
        
        # Step tracking for recording
        self._current_step = 0
        self._current_step_type = "unknown"
        
        # Bound tools reference (set by bind_tools)
        self._bound_tools: Optional[List[Any]] = None
        self._model_with_tools: Optional[Any] = None
    
    def _get_model_name(self) -> str:
        """Get the model name for logging."""
        return (
            getattr(self._model, 'model_name', None) or 
            getattr(self._model, 'model', None) or 
            str(type(self._model).__name__)
        )
    
    def set_step_info(self, step: int, step_type: str) -> None:
        """
        Set the current step info for recording.
        
        Args:
            step: Step number (0 for planning, 1+ for execution)
            step_type: Step type ("planning", "execution", "react")
        """
        self._current_step = step
        self._current_step_type = step_type
    
    def bind_tools(self, tools: List[Any]) -> "ReplayableModelWithTools":
        """
        Bind tools to the model (like LangChain's bind_tools).
        
        Returns a wrapper that supports tool-bound invocation.
        """
        self._bound_tools = tools
        if hasattr(self._model, "bind_tools"):
            self._model_with_tools = self._model.bind_tools(tools)
        else:
            self._model_with_tools = self._model
        
        return ReplayableModelWithTools(self)
    
    def _serialize_tool_calls(self, tool_calls: Any) -> Optional[List[Dict[str, Any]]]:
        """Serialize tool calls to JSON-serializable format."""
        if not tool_calls:
            return None
        
        result = []
        for call in tool_calls:
            if hasattr(call, "function"):
                # OpenAI format
                func = call.function
                result.append({
                    "id": getattr(call, "id", None),
                    "name": getattr(func, "name", None),
                    "arguments": getattr(func, "arguments", {})
                })
            elif isinstance(call, dict):
                result.append(call)
            else:
                # Generic format
                result.append({
                    "id": getattr(call, "id", None),
                    "name": getattr(call, "name", None),
                    "arguments": getattr(call, "args", getattr(call, "arguments", {}))
                })
        return result if result else None
    
    def _deserialize_to_ai_message(self, response_data: Dict[str, Any]) -> AIMessage:
        """
        Convert recorded response data back to an AIMessage.
        
        Args:
            response_data: Dict with 'content' and 'tool_calls' keys
            
        Returns:
            AIMessage that mimics what the LLM would have returned
        """
        content = response_data.get("content") or ""
        tool_calls_data = response_data.get("tool_calls")
        
        # Build tool_calls in LangChain format
        tool_calls = []
        if tool_calls_data:
            for tc in tool_calls_data:
                # LangChain expects tool_calls as list of dicts
                # Handle both "arguments" and "args" keys (stored data may use either)
                args_data = tc.get("arguments") or tc.get("args", {})
                tool_call = {
                    "id": tc.get("id") or f"call_{len(tool_calls)}",
                    "name": tc.get("name"),
                    "args": args_data
                }
                tool_calls.append(tool_call)
        
        # Create AIMessage with tool_calls if present
        if tool_calls:
            return AIMessage(content=content, tool_calls=tool_calls)
        else:
            return AIMessage(content=content)
    
    async def ainvoke(self, prompt: str) -> AIMessage:
        """
        Async invoke the model or return recorded response.
        
        Args:
            prompt: The prompt to send to the LLM
            
        Returns:
            AIMessage response (real or replayed)
        """
        if self._is_replay:
            return await self._replay_invoke(prompt)
        else:
            return await self._record_invoke(prompt)
    
    async def _record_invoke(self, prompt: str) -> AIMessage:
        """Invoke the real LLM and record the response."""
        start_time = time.time()
        
        # Use model_with_tools if available, otherwise use base model
        model_to_invoke = self._model_with_tools or self._model
        
        logger.debug(f"[LLMWrapper] Invoking real LLM: {self._model_name}")
        response = await model_to_invoke.ainvoke(prompt)
        
        duration_ms = int((time.time() - start_time) * 1000)
        
        # Extract content and tool_calls from response
        content = getattr(response, "content", None)
        tool_calls = getattr(response, "tool_calls", None)
        
        # Record the response
        if self._replay_manager:
            self._replay_manager.record_response(
                step=self._current_step,
                step_type=self._current_step_type,
                prompt=prompt,
                response_content=content,
                tool_calls=self._serialize_tool_calls(tool_calls),
                model_name=self._model_name,
                duration_ms=duration_ms
            )
        
        return response

class ReplayableModelWithTools:
    """
    Wrapper returned by ReplayableModel.bind_tools().
    
    Provides the same interface as a tool-bound LangChain model.
    """
    
    def __init__(self, wrapper: ReplayableModel):
        self._wrapper = wrapper
    
    async def ainvoke(self, prompt: str) -> AIMessage:
        """Async invoke with tools."""
        return await self._wrapper.ainvoke(prompt)
    
    def invoke(self, prompt: str) -> AIMessage:
        """Sync invoke with tools (not recommended, use ainvoke)."""
        import asyncio
        return asyncio.run(self._wrapper.ainvoke(prompt))


def create_inteceptor_model(
    model: BaseChatModel,
    execution_id: str,
    workflow_id: str,
    workflow_log_dir: Path,
) -> ReplayableModel:
    """
    Factory function to create a ReplayableModel with replay manager.
    
    Args:
        model: The underlying LangChain chat model
        execution_id: Execution ID for logging/replay
        workflow_id: Workflow ID for logging/replay
        replay_dir: Directory for replay logs
        is_replay: True for replay mode, False for recording mode
        
    Returns:
        Configured ReplayableModel instance
    """
    from ..container.replay.llm_replay_manager import LLMReplayManager
    
    replay_manager = LLMReplayManager(
        execution_id=execution_id,
        replay_dir=workflow_log_dir,
        workflow_id=workflow_id
    )
    
    return ReplayableModel(
        model=model,
        replay_manager=replay_manager,
        is_replay=False
    )

