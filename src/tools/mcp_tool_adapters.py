from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional, Type, Union

from langchain_core.tools import Tool, StructuredTool
from pydantic import BaseModel, Field, create_model

from .mcp_client import MCPClient, MCPToolSpec


def _serialize_pydantic_models(data: Any) -> Any:
    """Recursively serialize Pydantic model instances to dictionaries.
    
    This function ensures that nested Pydantic models are properly converted
    to JSON-serializable dictionaries before being sent to MCP servers.
    Also excludes None values from plain dicts to avoid sending unset optional
    parameters to MCP servers (which may have strict validation).
    
    Args:
        data: Any data structure that may contain Pydantic model instances
    
    Returns:
        Data structure with all Pydantic models converted to dicts and None values excluded
    """
    if isinstance(data, BaseModel):
        # Convert Pydantic model to dict, recursively handling nested models
        # Exclude None values so unset optional parameters aren't sent to MCP servers
        return data.model_dump(mode='python', exclude_none=True)
    elif isinstance(data, dict):
        # Recursively process dict values and exclude None values
        # This ensures None values from prepare_tool_arguments aren't sent to MCP servers
        result = {}
        for k, v in data.items():
            if v is not None:
                serialized_value = _serialize_pydantic_models(v)
                # Only add non-None values (after serialization, nested None values are filtered)
                if serialized_value is not None:
                    result[k] = serialized_value
        return result
    elif isinstance(data, list):
        # Recursively process list items
        return [_serialize_pydantic_models(item) for item in data if item is not None]
    else:
        # Primitive types (str, int, float, bool) - return as-is
        # None values are filtered out in dict/list processing above
        return data


def _resolve_schema_ref(schema: Dict[str, Any], ref_path: str) -> Optional[Dict[str, Any]]:
    """Resolve a JSON Schema $ref reference.
    
    Args:
        schema: The root schema dictionary
        ref_path: The reference path (e.g., "#/$defs/SearchRequestModel" or "#/definitions/MyModel")
    
    Returns:
        The resolved schema dictionary or None if not found
    """
    if not ref_path.startswith("#/"):
        # External references not supported
        return None
    
    # Remove the "#/" prefix and split the path
    path_parts = ref_path[2:].split("/")
    
    # Navigate through the schema
    current = schema
    for part in path_parts:
        if part == "":
            continue
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    
    return current if isinstance(current, dict) else None


def _get_schema_type(prop_schema: Dict[str, Any], root_schema: Dict[str, Any]) -> str:
    """Determine the JSON schema type of a property, handling $ref references.
    
    Args:
        prop_schema: The property schema (may contain $ref)
        root_schema: The root schema (for resolving $ref)
    
    Returns:
        The JSON schema type string ("string", "object", "array", etc.)
    """
    # Check for $ref first - resolve it if present
    if "$ref" in prop_schema:
        ref_path = prop_schema["$ref"]
        resolved_schema = _resolve_schema_ref(root_schema, ref_path)
        if resolved_schema:
            # Recursively get type from resolved schema
            return _get_schema_type(resolved_schema, root_schema)
        # If resolution fails, default to object (most common for $ref)
        return "object"
    
    # Check for explicit type
    prop_type = prop_schema.get("type")
    if prop_type:
        return prop_type
    
    # Infer type from schema structure
    # If it has "properties", it's an object
    if "properties" in prop_schema:
        return "object"
    # If it has "items", it's an array
    elif "items" in prop_schema:
        return "array"
    # Otherwise, default to string
    else:
        return "string"


def _json_schema_to_pydantic(
    schema: Dict[str, Any], 
    tool_name: str,
    model_cache: Optional[Dict[str, Type[BaseModel]]] = None
) -> Optional[Type[BaseModel]]:
    """Convert JSON schema to Pydantic BaseModel, handling nested models from $defs.
    
    This function recursively builds Pydantic models from JSON schemas, including:
    - Nested models defined in $defs
    - $ref references to those nested models
    - Proper type mapping for all JSON schema types
    
    Args:
        schema: JSON schema dictionary (from MCP tool inputSchema)
        tool_name: Name of the tool (used for model class name)
        model_cache: Cache of already-built models (for recursive calls)
    
    Returns:
        Pydantic BaseModel class or None if schema is empty/invalid
    """
    if not schema or not isinstance(schema, dict):
        return None
    
    if model_cache is None:
        model_cache = {}
    
    # Step 1: Build all nested models from $defs first
    # This ensures referenced models exist before we try to use them
    defs = schema.get("$defs", {}) or schema.get("definitions", {})
    for def_name, def_schema in defs.items():
        if def_name not in model_cache and isinstance(def_schema, dict):
            # Recursively build the nested model
            nested_model = _json_schema_to_pydantic(
                def_schema, 
                f"{tool_name}_{def_name}",
                model_cache
            )
            if nested_model:
                model_cache[def_name] = nested_model
                # Also cache by full reference path
                model_cache[f"#/$defs/{def_name}"] = nested_model
                model_cache[f"#/definitions/{def_name}"] = nested_model
    
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    
    if not properties:
        # No properties means no arguments needed. However, returning None here
        # makes LangChain infer a schema from the function signature (which uses
        # **kwargs), producing a model with a required 'kwargs' dict. Some LLMs
        # (e.g., Qwen) may send an empty object, which LangChain then maps to
        # kwargs=None and Pydantic rejects. To avoid this, provide an explicit
        # empty Pydantic model so validation succeeds on {}.
        model_name = f"{tool_name.replace('-', '_').replace(' ', '_').title()}Input"
        try:
            return create_model(model_name)
        except Exception:
            return None
    
    # Build field definitions for create_model
    field_definitions: Dict[str, tuple] = {}
    
    for prop_name, prop_schema in properties.items():
        # Check if this property references a model from $defs
        python_type = None
        prop_description = prop_schema.get("description", "")
        
        if "$ref" in prop_schema:
            ref_path = prop_schema["$ref"]
            # Check if we've already built this model
            if ref_path in model_cache:
                python_type = model_cache[ref_path]
                # Get description from resolved schema
                resolved_schema = _resolve_schema_ref(schema, ref_path)
                if resolved_schema:
                    prop_description = resolved_schema.get("description", prop_description) or prop_schema.get("title", "")
            else:
                # Try to resolve and build the model on-the-fly
                resolved_schema = _resolve_schema_ref(schema, ref_path)
                if resolved_schema:
                    # Extract model name from ref path
                    ref_parts = ref_path.split("/")
                    ref_model_name = ref_parts[-1] if ref_parts else "Model"
                    nested_model = _json_schema_to_pydantic(
                        resolved_schema,
                        f"{tool_name}_{ref_model_name}",
                        model_cache
                    )
                    if nested_model:
                        python_type = nested_model
                        model_cache[ref_path] = nested_model
                        prop_description = resolved_schema.get("description", prop_description) or prop_schema.get("title", "")
        
        # If not a $ref, determine type from schema
        if python_type is None:
            prop_type = _get_schema_type(prop_schema, schema)
            
            # Map JSON schema types to Python types
            if prop_type == "string":
                python_type = str
            elif prop_type == "integer":
                python_type = int
            elif prop_type == "number":
                python_type = float
            elif prop_type == "boolean":
                python_type = bool
            elif prop_type == "array":
                # Handle arrays with explicit item schemas to preserve JSON schema fidelity
                items_schema = prop_schema.get("items")
                if isinstance(items_schema, dict):
                    # Case 1: items reference a defined model
                    if "$ref" in items_schema:
                        items_ref = items_schema["$ref"]
                        if items_ref in model_cache:
                            item_model = model_cache[items_ref]
                            python_type = List[item_model]  # type: ignore
                        else:
                            # Try to resolve and build the referenced item model
                            resolved_items = _resolve_schema_ref(schema, items_ref)
                            if resolved_items:
                                ref_parts = items_ref.split("/")
                                ref_model_name = ref_parts[-1] if ref_parts else "ItemModel"
                                nested_item_model = _json_schema_to_pydantic(
                                    resolved_items,
                                    f"{tool_name}_{prop_name.title()}_{ref_model_name}",
                                    model_cache
                                )
                                if nested_item_model:
                                    model_cache[items_ref] = nested_item_model
                                    python_type = List[nested_item_model]  # type: ignore
                                else:
                                    python_type = list
                            else:
                                python_type = list
                    else:
                        # Case 2: inline item schema
                        item_type = _get_schema_type(items_schema, schema)
                        if item_type == "string":
                            python_type = List[str]
                        elif item_type == "integer":
                            python_type = List[int]
                        elif item_type == "number":
                            python_type = List[float]
                        elif item_type == "boolean":
                            python_type = List[bool]
                        elif item_type == "object":
                            # Build a nested model for object items to retain properties
                            nested_item_model = _json_schema_to_pydantic(
                                items_schema,
                                f"{tool_name}_{prop_name.title()}Item",
                                model_cache
                            )
                            if nested_item_model:
                                python_type = List[nested_item_model]  # type: ignore
                            else:
                                python_type = list
                        else:
                            # Fallback for complex/nested arrays
                            python_type = list
                else:
                    # No items schema provided; use untyped list (will serialize but Gemini may reject)
                    python_type = list
            elif prop_type == "object":
                # For objects, check if we should create a nested model
                if "properties" in prop_schema:
                    # Create a nested model for this object
                    nested_model_name = f"{tool_name}_{prop_name.title()}"
                    nested_model = _json_schema_to_pydantic(
                        prop_schema,
                        nested_model_name,
                        model_cache
                    )
                    if nested_model:
                        python_type = nested_model
                    else:
                        python_type = dict
                else:
                    python_type = dict
            else:
                python_type = Any
        
        # Determine if field is required and set field definition
        is_required = prop_name in required
        field_kwargs = {"description": prop_description} if prop_description else {}
        
        if is_required:
            field_definitions[prop_name] = (python_type, Field(**field_kwargs) if field_kwargs else Field(...))
        else:
            # Optional field - use None as default so unset values are excluded from serialization
            # This ensures only parameters explicitly provided by LLM are sent to MCP servers
            field_definitions[prop_name] = (Optional[python_type], Field(default=None, **field_kwargs))
    
    if not field_definitions:
        return None
    
    # Create a dynamic Pydantic model
    model_name = f"{tool_name.replace('-', '_').replace(' ', '_').title()}Input"
    try:
        return create_model(model_name, **field_definitions)
    except Exception:
        # Fallback: return None if model creation fails
        return None


def make_mcp_tool(mcp_client: MCPClient, tool_spec: MCPToolSpec) -> Tool:
    """Create a LangChain tool from an MCP tool spec.
    
    Converts the MCP tool's inputSchema to a Pydantic model for proper argument validation.
    """
    # Convert JSON schema to Pydantic model if available
    args_schema = None
    if tool_spec.input_schema:
        args_schema = _json_schema_to_pydantic(tool_spec.input_schema, tool_spec.name)
    
    async def _arun(**kwargs: Any) -> Any:
        # kwargs are validated by Pydantic via StructuredTool, but may contain
        # nested Pydantic model instances that need to be serialized to dicts
        # for JSON serialization before sending to MCP server
        serialized_kwargs = _serialize_pydantic_models(kwargs)
        return await mcp_client.invoke(tool_spec.name, serialized_kwargs)

    def _run(**kwargs: Any) -> Any:
        # Synchronous fallback could wrap event loop for demos.
        raise RuntimeError("This tool must be run asynchronously in the agent graph")

    # Use StructuredTool with the converted schema
    return StructuredTool.from_function(
        func=_run,
        coroutine=_arun,
        name=tool_spec.name,
        description=tool_spec.description or tool_spec.name,
        args_schema=args_schema,  # Use the converted Pydantic model
    )


