"""
Type definitions for chat messages and responses across different model vendors.

Uses Pydantic for automatic validation and transformation from LLM responses.
Inspired by OpenAI API structure but generalized for all vendors.
"""

import json
from typing import Any, Literal, Union

from pydantic import BaseModel, Field, field_validator


class TextContentBlock(BaseModel):
    """Text content block in a message."""
    type: Literal["text"] = "text"
    content: str


class ImageContentBlock(BaseModel):
    """Image content block (for multimodal models)."""
    type: Literal["image"] = "image"
    image_url: str


MessageContentBlock = Union[TextContentBlock, ImageContentBlock]
"""Union type for all supported content block types."""


class FunctionCall(BaseModel):
    """A function/tool call from the assistant."""
    id: str | None = None
    """Unique ID for this tool call (required by OpenAI/Anthropic)."""
    name: str
    """Name of the function/tool to call."""
    arguments: str | dict = Field(default_factory=dict)
    """Arguments as JSON string or dict. JSON string is common in OpenAI format."""
    
    @field_validator("arguments", mode="before")
    @classmethod
    def parse_arguments(cls, v: Any) -> dict:
        """Parse JSON string arguments to dict if needed."""
        if isinstance(v, dict):
            return v
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                return {}
        return {}
    
    def get_args_dict(self) -> dict:
        """Get arguments as dict (parsed if needed)."""
        if isinstance(self.arguments, dict):
            return self.arguments
        if isinstance(self.arguments, str):
            try:
                return json.loads(self.arguments)
            except json.JSONDecodeError:
                return {}
        return {}


class ChatUserMessage(BaseModel):
    """Message from the user in the chat."""
    role: Literal["user"] = "user"
    content: Union[str, list[MessageContentBlock]]
    """The content of the message (string or structured blocks)."""


class ChatAssistantMessage(BaseModel):
    """Message from the assistant in the chat."""
    role: Literal["assistant"] = "assistant"
    content: str | list[MessageContentBlock] | None = None
    """The content of the message, can be None if tool_calls are provided."""
    tool_calls: list[FunctionCall] | None = None
    """A list of tool calls that the assistant requests."""
    
    @field_validator("tool_calls", mode="before")
    @classmethod
    def normalize_tool_calls(cls, v: Any) -> list[FunctionCall] | None:
        """Normalize tool_calls from various formats using Pydantic."""
        if v is None or (isinstance(v, list) and len(v) == 0):
            return None
        if isinstance(v, list):
            result = []
            for call in v:
                if isinstance(call, FunctionCall):
                    result.append(call)
                else:
                    # Use Pydantic's automatic parsing
                    try:
                        call_dict = None
                        # Handle OpenAI format: call.function.name/arguments
                        if hasattr(call, "function"):
                            func = call.function
                            raw_args = getattr(func, "arguments", {})
                            # Handle string arguments (common in OpenAI format)
                            if isinstance(raw_args, str):
                                try:
                                    raw_args = json.loads(raw_args)
                                except json.JSONDecodeError:
                                    pass  # Keep as string, will be parsed by FunctionCall validator
                            call_dict = {
                                "id": getattr(call, "id", None),
                                "name": getattr(func, "name", None),
                                "arguments": raw_args
                            }
                        elif isinstance(call, dict):
                            # Handle nested function structure
                            if "function" in call:
                                func_data = call["function"]
                                raw_args = func_data.get("arguments", {}) if isinstance(func_data, dict) else getattr(func_data, "arguments", {})
                                # Handle string arguments
                                if isinstance(raw_args, str):
                                    try:
                                        raw_args = json.loads(raw_args)
                                    except json.JSONDecodeError:
                                        pass  # Keep as string, will be parsed by FunctionCall validator
                                call_dict = {
                                    "id": call.get("id"),
                                    "name": func_data.get("name") if isinstance(func_data, dict) else getattr(func_data, "name", None),
                                    "arguments": raw_args
                                }
                            else:
                                # Handle dict format: check for 'args' or 'arguments' key
                                # Some LLMs use 'args' instead of 'arguments'
                                raw_args = call.get("arguments") or call.get("args", {})
                                # Handle string arguments
                                if isinstance(raw_args, str):
                                    try:
                                        raw_args = json.loads(raw_args)
                                    except json.JSONDecodeError:
                                        pass  # Keep as string, will be parsed by FunctionCall validator
                                call_dict = {
                                    "id": call.get("id"),
                                    "name": call.get("name"),
                                    "arguments": raw_args
                                }
                                # Log if we had to convert 'args' to 'arguments'
                                if "args" in call and "arguments" not in call:
                                    from ..logging_config import get_logger
                                    logger = get_logger(__name__)
                                    logger.debug(f"Converted 'args' to 'arguments' in tool call: {call.get('name')}")
                        else:
                            # Generic object access
                            raw_args = getattr(call, "args", getattr(call, "arguments", {}))
                            if isinstance(raw_args, str):
                                try:
                                    raw_args = json.loads(raw_args)
                                except json.JSONDecodeError:
                                    pass  # Keep as string, will be parsed by FunctionCall validator
                            call_dict = {
                                "id": getattr(call, "id", None),
                                "name": getattr(call, "name", None),
                                "arguments": raw_args
                            }
                        
                        if call_dict and call_dict.get("name"):
                            result.append(FunctionCall.model_validate(call_dict))
                    except Exception as e:
                        # Log the error but skip invalid tool calls
                        from ..logging_config import get_logger
                        logger = get_logger(__name__)
                        logger.warning(f"Failed to parse tool call: {e}, raw call: {call}")
                        continue
            return result if result else None
        return None


class ChatToolResultMessage(BaseModel):
    """Message from the tool with the result of a function call."""
    role: Literal["tool"] = "tool"
    tool_call_id: str | None = None
    """The ID of the tool call this result corresponds to."""
    content: Union[str, list[MessageContentBlock]]
    """The output of the tool call."""
    error: str | None = None
    """Error message if the tool call failed."""


class ChatSystemMessage(BaseModel):
    """System message in the chat."""
    role: Literal["system"] = "system"
    content: Union[str, list[MessageContentBlock]]
    """The content of the system message."""


ChatMessage = Union[ChatUserMessage, ChatAssistantMessage, ChatToolResultMessage, ChatSystemMessage]
"""Union type of all possible messages in a chat conversation."""


def text_content_block_from_string(content: str) -> TextContentBlock:
    """Create a text content block from a string."""
    return TextContentBlock(type="text", content=content)


def get_text_content_as_str(content: str | list[MessageContentBlock] | None) -> str:
    """Extract text content as string from various content formats."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = []
        for block in content:
            if isinstance(block, TextContentBlock):
                text_parts.append(block.content)
            elif isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(block.get("content", ""))
        return "\n".join(text_parts)
    return str(content)


def _normalize_content(content: Any) -> str | list[dict] | None:
    """Normalize vendor-specific content structures into our schema.

    - Gemini often returns list of parts: {'type': 'text', 'text': '...'}
      We convert to {'type': 'text', 'content': '...'} expected by TextContentBlock.
    - OpenAI-style images may use {'type': 'image_url', 'image_url': '...'} or
      {'type': 'image_url', 'image_url': {'url': '...'}}; convert to type 'image'.
    """
    if content is None:
        return None
    if isinstance(content, str):
        return content
    # Normalize list of content blocks
    if isinstance(content, list):
        normalized: list[dict] = []
        for block in content:
            # Already normalized TextContentBlock dict
            if isinstance(block, dict):
                btype = block.get("type")
                # Normalize Gemini text blocks {'type': 'text', 'text': '...'}
                if btype == "text":
                    text_val = block.get("content")
                    if text_val is None:
                        text_val = block.get("text")
                    # Fallback stringify if still None
                    if text_val is None:
                        text_val = ""
                    normalized.append({"type": "text", "content": text_val})
                    continue
                # Normalize image blocks to our schema
                if btype in ("image", "image_url", "image_url_block"):  # accommodate variants
                    image_val = block.get("image_url")
                    if isinstance(image_val, dict):
                        image_val = image_val.get("url") or image_val.get("image_url")
                    if image_val is None and "url" in block:
                        image_val = block.get("url")
                    if image_val is None:
                        # If there's inline data or other forms, skip rather than failing
                        continue
                    normalized.append({"type": "image", "image_url": image_val})
                    continue
                # Unknown block type: coerce to text
                normalized.append({"type": "text", "content": str(block)})
            else:
                # Non-dict block: stringify
                normalized.append({"type": "text", "content": str(block)})
        return normalized
    # Unknown structure: coerce to string
    return str(content)


def convert_langchain_response_to_message(response: Any) -> ChatAssistantMessage:
    """
    Convert LangChain AIMessage to Pydantic ChatAssistantMessage.
    
    Uses Pydantic's automatic parsing to handle various response formats.
    """
    # Extract content
    content = None
    if hasattr(response, "content"):
        content = response.content
    elif hasattr(response, "text"):
        content = response.text
    
    # Normalize vendor-specific content to our MessageContentBlock schema
    content = _normalize_content(content)

    # Extract tool_calls - Pydantic will handle normalization
    tool_calls_raw = None
    if hasattr(response, "tool_calls") and response.tool_calls:
        tool_calls_raw = response.tool_calls
    
    # Build dict for Pydantic parsing
    message_data = {
        "role": "assistant",
        "content": content,
        "tool_calls": tool_calls_raw
    }
    
    # Pydantic will automatically:
    # - Parse tool_calls from various formats via field_validator
    # - Handle None values
    # - Validate types
    return ChatAssistantMessage.model_validate(message_data)

