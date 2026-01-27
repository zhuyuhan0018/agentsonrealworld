from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional
from pathlib import Path

import yaml
import json
from pydantic import BaseModel, Field, ValidationError

Mode = Literal["planning", "react"]
Vendor = Literal["openai", "gemini", "anthropic", "qwen", "deepseek"]

class ModelConfig(BaseModel):
    vendor: Vendor
    name: str
    kwargs: Dict[str, Any] = Field(default_factory=dict)
    # Optional separate LLM for execution result judging
    judger_vendor: Optional[Vendor] = None
    judger_name: Optional[str] = None
    judger_kwargs: Dict[str, Any] = Field(default_factory=dict)

class ToolServerConfig(BaseModel):
    server: str
    transport: Literal["stdio", "sse"]
    endpoint: str
    select: List[str] = Field(default_factory=list)
    kwargs: Dict[str, Any] = Field(default_factory=dict)

class PipelineEnv(BaseModel):
    max_steps: int = 20
    tracing: bool = False
    timeouts: Dict[str, float] = Field(default_factory=dict)
    extras: Dict[str, Any] = Field(default_factory=dict)

class InputConfig(BaseModel):
    task: str
    context: Dict[str, Any] = Field(default_factory=dict)

# Updated to match new config schema
default_false = Field(default=False)
class TaskConfig(BaseModel):
    request: str
    context: Dict[str, Any] = Field(default_factory=dict)
    attack: bool = True

class Workflow(BaseModel):
    id: str
    name: Optional[str] = None
    servers: List[str] = Field(default_factory=list)
    task: TaskConfig

class WorkflowsFile(BaseModel):
    workflows: List[Workflow]


def load_workflows_json(path: str) -> WorkflowsFile:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return WorkflowsFile.model_validate(data)

def load_servers_config(path: str = "servers.json") -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    # Handle both array format (legacy) and object format (new)
    if isinstance(data, list):
        # Legacy format: array of server objects
        # Filter out inactive servers (active == False)
        result: Dict[str, Any] = {}
        for server in data:
            try:
                name = str(server.get("server", "")).lower()
                if not name:
                    continue
                # Treat missing 'active' as active for backward compatibility
                if server.get("active", True) is False:
                    continue
                result[name] = server
            except Exception:
                continue
        return result
    elif isinstance(data, dict):
        # New format: object with server names as keys
        # Filter out inactive servers (active == False)
        result: Dict[str, Any] = {}
        for name, config in data.items():
            try:
                # Treat missing 'active' as active for backward compatibility
                if isinstance(config, dict) and config.get("active", True) is False:
                    continue
                result[name.lower()] = {**config, "name": name}
            except Exception:
                continue
        return result
    else:
        raise ValueError(f"servers.json must be either an array or object, got {type(data)}")

class ContainerConfig(BaseModel):
    image: str
    name: str

    @classmethod
    def from_yaml(cls, path: str) -> "ContainerConfig":
        """Load container configuration from a YAML file."""
        path_obj = Path(path)
        # Assume path is already absolute/resolved by caller
        with open(path_obj, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return cls.model_validate(data)

# Pipeline configuration: model and overall agent settings
class PipelineConfig(BaseModel):
    mode: Mode
    model: ModelConfig
    env: PipelineEnv = Field(default_factory=PipelineEnv)
    container_config_path: Optional[str] = None  # Path to container config file

    @classmethod
    def from_yaml(cls, path: str) -> "PipelineConfig":
        path_obj = Path(path)
        if not path_obj.is_absolute():
            path_obj = Path.cwd() / path_obj
        with open(path_obj, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return cls.model_validate(data)
    
    def get_container_config(self) -> Optional[ContainerConfig]:
        """Load container config from the specified path if available.
        Relative paths are resolved relative to the current working directory (project root).
        """
        if not self.container_config_path:
            return None
        container_path = Path(self.container_config_path)
        if not container_path.is_absolute():
            # Resolve relative to current working directory (project root)
            container_path = Path.cwd() / container_path
        # Pass absolute path
        return ContainerConfig.from_yaml(str(container_path.resolve()))



