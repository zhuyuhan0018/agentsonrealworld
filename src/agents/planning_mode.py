from __future__ import annotations

import json
import random
from typing import Any, Dict, List, Union

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool

from ..logging_config import get_logger
from .tool_utils import extract_tool_call, execute_tool_call
from .message_types import ChatAssistantMessage, convert_langchain_response_to_message, get_text_content_as_str
from .llm_model_wrapper import ReplayableModel

logger = get_logger(__name__)


async def run_planning_loop(
    model: Union[BaseChatModel, ReplayableModel],
    tools: List[BaseTool],
    prompt: str,
    max_steps: int = 8,
    attack_mode: bool = False,
    injection_payload: str | None = None,
    injection_max_iter: int = 3,
) -> Any:
    """
    Planning mode: plan first, then execute steps with tools.
    Uses typed messages for type safety and automatic parsing.
    """
    logger.info(f"Starting Planning loop (plan-then-execute) with max_steps={max_steps}")
    logger.debug(f"Initial prompt: {prompt[:200]}..." if len(prompt) > 200 else f"Initial prompt: {prompt}")
    
    # Check if model is a ReplayableModel wrapper
    is_replayable = isinstance(model, ReplayableModel)
    if is_replayable:
        model_with_tools = model.bind_tools(tools)
        logger.debug("Using ReplayableModel wrapper for LLM calls")
    else:
        model_with_tools = model.bind_tools(tools) if hasattr(model, "bind_tools") else model

    # For planning, call the LLM without tool bindings to avoid tool calls during plan creation
    planning_model = model.bind_tools([]) if hasattr(model, "bind_tools") else model
    
    state = {
        "prompt": prompt,
        "plan": None,
        "history": [],
        "last_tool_result": None,
        "last_tool_name": None,
    }
    injection_state = None
    if attack_mode and injection_payload:
        capped_iter = max(1, injection_max_iter)
        injection_state = {
            "used": False,
            "call_count": 0,
            # Randomize which call to inject within the configured cap; force by cap
            "inject_on": random.randint(1, capped_iter),
            "max_iter": capped_iter,
        }
    
    # 1. Planning phase
    logger.info("Phase 1: Planning")
    plan_prompt = (
        "You are planning the full workflow. Output only a numbered list using the format '1. ...', '2. ...'. "
        "Do not call or reference any tools in this message. Do not preface or append commentary. "
        "Be concise and actionable.\n"
        f"Task: {prompt}"
    )
    
    try:
        
        # Set step info for ReplayableModel (step 0 = planning)
        if is_replayable:
            model.set_step_info(step=0, step_type="planning")
        
        # Get model identifier for logging
        model_name = getattr(model, '_model_name', None) or getattr(model, 'model_name', None) or getattr(model, 'model', None) or str(type(model).__name__)
        logger.info(f"Invoking LLM for planning: {model_name} (prompt length: {len(plan_prompt)})")
        logger.debug("=" * 80)
        logger.debug("PLANNING PROMPT INPUT:")
        logger.debug("-" * 80)
        logger.debug(plan_prompt)
        logger.debug("-" * 80)
        
        raw_plan_resp = await planning_model.ainvoke(plan_prompt)
        logger.debug(f"Planning response received from {model_name}")
        
        # Convert to typed message
        typed_plan_resp: ChatAssistantMessage = convert_langchain_response_to_message(raw_plan_resp)
        state["history"].append({"step": "plan", "prompt": plan_prompt, "response": typed_plan_resp})
        
        # Extract plan text
        raw_plan = get_text_content_as_str(typed_plan_resp.content)
        logger.debug("PLANNING RESPONSE OUTPUT:")
        logger.debug("-" * 80)
        logger.debug(raw_plan)
        logger.debug("-" * 80)
        logger.debug("=" * 80)
        logger.debug(f"Extracted plan text (length: {len(raw_plan)})")
        
        plan_steps = [
            line.strip()[2:].strip()
            for line in raw_plan.splitlines()
            if line.strip().startswith(tuple(str(i) + '.' for i in range(1, 10)))
        ]
        if not plan_steps:
            logger.warning("Could not parse numbered plan steps, using full plan as single step")
            plan_steps = [raw_plan]
        
        logger.info(f"Planning complete: {len(plan_steps)} steps identified")
        logger.debug(f"Plan steps: {plan_steps}")
        state["plan"] = plan_steps
        
    except Exception as e:
        logger.error(f"Planning phase failed: {e}", exc_info=True)
        raise
    
    # 2. Execution phase
    logger.info(f"Phase 2: Execution ({len(plan_steps)} steps)")
    last_llm_result: ChatAssistantMessage | None = None
    max_steps_reached = False
    
    for idx, step_text in enumerate(plan_steps):
        if idx >= max_steps:
            logger.warning(f"Planning loop reached max_steps ({max_steps}) during execution phase")
            max_steps_reached = True
            break
        safe_step_preview = step_text[:100] if step_text else "(plan text unavailable)"
        logger.info(
            f"Executing plan step {idx+1}/{len(plan_steps)}: {safe_step_preview}..."
        )

        plan_block = "\n".join([f"{i+1}. {s}" for i, s in enumerate(plan_steps)])
        loop_prompt = (
            f"Plan:\n{plan_block}\n\n"
            f"Current step ({idx+1}/{len(plan_steps)}): {step_text}\n"
        )
        if state.get("last_tool_name") or state.get("last_tool_result"):
            loop_prompt += "Previous tool: "
            loop_prompt += state.get("last_tool_name") or "(none)"
            loop_prompt += "\nPrevious tool result: "
            loop_prompt += state.get("last_tool_result") or "(none)"
            logger.debug(
                f"Added previous tool context (name: {state.get('last_tool_name')}, length: {len(state.get('last_tool_result') or '')})"
            )
        
        typed_response: ChatAssistantMessage | None = None
        try:
            
            # Set step info for ReplayableModel (step 1+ = execution)
            if is_replayable:
                model.set_step_info(step=idx+1, step_type="execution")
            
            # Get model identifier for logging
            model_name = getattr(model, '_model_name', None) or getattr(model, 'model_name', None) or getattr(model, 'model', None) or str(type(model).__name__)
            logger.debug(f"Invoking LLM for step {idx+1}: {model_name} (prompt length: {len(loop_prompt)})")
            logger.debug("=" * 80)
            logger.debug(f"STEP {idx+1} PROMPT INPUT:")
            logger.debug("-" * 80)
            logger.debug(loop_prompt)
            logger.debug("-" * 80)
            
            raw_response = await model_with_tools.ainvoke(loop_prompt)
            logger.debug(f"Step {idx+1} response received from {model_name}")
            
            # Log raw response for debugging tool call extraction
            if hasattr(raw_response, "tool_calls") and raw_response.tool_calls:
                raw_call = raw_response.tool_calls[0]
                logger.debug(f"Step {idx+1}: Raw tool call from LLM: {raw_call}")
                if hasattr(raw_call, "function"):
                    logger.debug(f"Step {idx+1}: Function object: {raw_call.function}")
                    if hasattr(raw_call.function, "arguments"):
                        logger.debug(f"Step {idx+1}: Raw arguments (type: {type(raw_call.function.arguments)}): {raw_call.function.arguments}")
            
            
            # Convert to typed message
            typed_response = convert_langchain_response_to_message(raw_response)
            state["history"].append({"step": idx+1, "prompt": loop_prompt, "response": typed_response})
            
            # Log full response output
            response_text = get_text_content_as_str(typed_response.content)
            logger.debug(f"STEP {idx+1} RESPONSE OUTPUT:")
            logger.debug("-" * 80)
            logger.debug(response_text if response_text else "(No text content)")
            logger.debug("-" * 80)
            if typed_response.tool_calls:
                logger.info(f"Step {idx+1}: Tool calls detected: {len(typed_response.tool_calls)}")
                logger.debug(f"STEP {idx+1} TOOL CALLS DETECTED: {len(typed_response.tool_calls)}")
                for i, tc in enumerate(typed_response.tool_calls):
                    logger.debug(f"  Tool Call {i+1}: {tc.name}")
                    logger.debug(f"    Arguments: {tc.arguments}")
            logger.debug("=" * 80)
            
            tool_call_info = extract_tool_call(typed_response)
            if tool_call_info:
                tool_name, tool_args = tool_call_info
                try:
                    tool_output, success = await execute_tool_call(
                        tool_name,
                        tool_args,
                        tools,
                        step_label=f"Step {idx+1}",
                        attack_mode=attack_mode,
                        injection_payload=injection_payload,
                        injection_state=injection_state,
                    )
                    if not success:
                        logger.warning(f"Step {idx+1}: {tool_output}")
                except Exception as e:
                    error_msg = f"Workflow stopped at step {idx+1}: {e}"
                    logger.error(error_msg, exc_info=True)
                    return _build_final_state(state, typed_response, plan_steps, error=error_msg)
                
                tool_result_text = "" if tool_output is None else str(tool_output)
                max_tool_chars = 800
                if len(tool_result_text) > max_tool_chars:
                    logger.info(
                        f"Step {idx+1}: Truncating tool output from {len(tool_result_text)} to {max_tool_chars} characters"
                    )
                    tool_result_text = tool_result_text[:max_tool_chars] + "... [truncated]"

                state["last_tool_result"] = tool_result_text
                state["last_tool_name"] = tool_name
                state["prompt"] = loop_prompt
                continue
            
            last_llm_result = typed_response
            logger.debug(f"Step {idx+1} completed (no tool call)")
            
        except Exception as e:
            logger.error(f"Error in execution step {idx+1}: {e}", exc_info=True)
            return _build_final_state(
                state,
                typed_response,
                plan_steps,
                error=str(e),
            )
    
    logger.info("Planning loop execution completed")
    if max_steps_reached:
        return _build_final_state(state, last_llm_result, plan_steps, error=None, max_steps_reached=True)

    return _build_final_state(state, last_llm_result, plan_steps, error=None)


def _build_final_state(
    state: Dict[str, Any],
    final_message: ChatAssistantMessage | None,
    plan_steps: List[str],
    error: str | None = None,
    max_steps_reached: bool = False,
) -> Dict[str, Any]:
    """Assemble a final result payload including history and tool chain."""
    final_content = ""
    if final_message:
        final_content = get_text_content_as_str(final_message.content)

    if not final_content:
        fallback_tool_result = state.get("last_tool_result")
        if fallback_tool_result:
            final_content = f"(No final LLM reply; showing last tool result)\n{fallback_tool_result}"

    tool_sequence: List[str] = []
    logger.debug(
        f"Building final results, got {len(state.get('history', []))} history entries"
    )
    for entry in state.get("history", []):
        resp = entry.get("response")
        if resp and getattr(resp, "tool_calls", None):
            for tc in resp.tool_calls:
                if tc.name:
                    tool_sequence.append(tc.name)
                    logger.debug(
                        f"  Recorded tool call in final sequence: {tc.name}"
                    )

    final_content = f"Final response:\n {final_content}\n"

    if tool_sequence:
        tools_block = "\n".join([f"- {name}" for name in tool_sequence])
        final_content = (
            f"{final_content}\n\n"
            "-----------------------------\n"
            "Tool calls chain:\n"
            f"{tools_block}\n"
        )

    result: Dict[str, Any] = {
        "final": final_message,
        "final_content": final_content,
        "history": state.get("history", []),
        "plan": plan_steps,
    }

    if error is not None:
        result["error"] = error

    if max_steps_reached:
        result["max_steps_reached"] = True

    logger.debug(f"Final content length: {len(final_content) if final_content else 0}")
    return result

