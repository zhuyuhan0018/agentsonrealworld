# GrantBox 项目介绍与文件功能说明

> 本文档为 [GrantBox](https://github.com/ZQ-Struggle/Agent-GrantBox) 项目的中文详细介绍，面向论文 **"Evaluating Privilege Usage of Agents on Real-World Tools"** 的配套代码库。

---

## 一、项目概述

### 1.1 项目定位

**GrantBox** 是一个**安全评估框架**，用于系统性评估自主智能体（Agent）在与真实世界工具和服务交互时，对**权限使用**的处理能力。该框架提供沙箱环境，将真实的 MCP（Model Context Protocol）服务器与具有权限敏感性的工具集成，从而对 Agent 在云基础设施、数据库、邮件服务等关键系统中的安全性进行全面评估。

### 1.2 核心能力

- **沙箱隔离**：通过 Docker 容器运行 MCP 服务器，实现环境隔离
- **多模型支持**：支持 OpenAI、Anthropic、Google Gemini、通义千问、DeepSeek 等主流 LLM
- **双模式 Agent**：支持 ReAct（推理-行动）和 Plan-and-Execute（规划-执行）两种 Agent 模式
- **攻击注入**：支持注入恶意指令，评估 Agent 是否被劫持或误用权限
- **可复现**：支持日志重放、随机种子等机制，便于实验复现

### 1.3 技术栈

- **语言**：Python 3.11+
- **依赖管理**：uv
- **Agent 框架**：LangChain、LangGraph
- **运行时**：Docker
- **协议**：MCP（Model Context Protocol）

---

## 二、项目结构概览

```
Agent-GrantBox/
├── main.py                 # 主程序入口
├── pyproject.toml          # Python 项目配置与依赖
├── uv.lock                 # 依赖锁定文件
├── .env.example            # 环境变量示例
├── .python-version         # Python 版本指定
├── configs/                # 配置文件目录
├── src/                    # 核心源代码
├── servers_source/         # MCP 服务器源码（需单独下载）
├── logs/                   # 评估结果与日志（运行后生成）
└── workflow_logs/          # 详细工作流执行日志（运行后生成）
```

---

## 三、根目录文件说明

| 文件 | 功能 |
|------|------|
| **main.py** | 主入口脚本。解析命令行参数（如 `--config`、`--workflows`、`--attack-mode`），加载配置，调用评估流水线，支持单工作流、批量实验、攻击模式、断点续跑等。 |
| **pyproject.toml** | 项目元数据与依赖声明。定义 GrantBox 项目名、Python 版本要求、主要依赖（pydantic、langchain、langgraph、httpx 等）。 |
| **uv.lock** | 由 `uv` 生成的依赖锁定文件，保证环境可复现。 |
| **.env.example** | 环境变量模板。包含各 LLM 厂商的 API Key 占位符（如 `OPENAI_API_KEY`、`ANTHROPIC_API_KEY` 等），复制为 `.env` 并填入真实密钥后使用。 |
| **.python-version** | 指定项目使用的 Python 版本（如 3.11），供 uv 等工具读取。 |

---

## 四、configs/ 配置目录

| 文件 | 功能 |
|------|------|
| **container.example.yaml** | 容器配置模板。定义 Docker 镜像、网络、资源限制等，复制为 `container.yaml` 后使用。 |
| **react.example.yaml** | ReAct 模式配置模板。包含 `mode: react`、模型厂商/名称、最大步数、judger 模型等，复制为 `react.yaml` 后使用。 |
| **planning.example.yaml** | Plan-and-Execute 模式配置模板。用于规划-执行类 Agent 的配置。 |
| **servers.example.json** | MCP 服务器配置模板。定义各服务器的源码路径、启动命令、环境变量、是否启用等，复制为 `servers.json` 后使用。 |
| **workflows.example.json** | 工作流配置模板。定义评估任务列表：每个任务包含 `id`、`name`、依赖的 `servers`、`task.request`（用户请求）、`attack` 等。 |
| **workflows_benign.json** | 良性工作流定义。用于正常场景下的评估，通常由 `build_benign_workflow.py` 生成或手动编写。 |
| **workflows_injection.json** | 注入式（攻击）工作流定义。用于在良性任务中注入恶意指令，测试 Agent 是否被劫持。 |

---

## 五、src/ 核心源码目录

### 5.1 顶层模块

| 文件/目录 | 功能 |
|-----------|------|
| **pipeline.py** | 评估流水线核心。负责工作流加载、模型构建、MCP 工具初始化、Agent 循环调用、日志记录、攻击注入与结果判定（normal/hijacked/failed）等。 |
| **config.py** | 配置加载与校验。定义 `PipelineConfig`、`Workflow`、`TaskConfig`、`ModelConfig`、`ToolServerConfig` 等 Pydantic 模型，提供 `load_workflows_json`、`load_servers_config` 等加载函数。 |
| **logging_config.py** | 日志配置。统一设置控制台与文件日志级别、格式等。 |

### 5.2 agents/ — Agent 实现

| 文件 | 功能 |
|------|------|
| **react_mode.py** | ReAct 模式实现。实现逐步推理-行动循环：调用 LLM → 解析工具调用 → 执行工具 → 将观察反馈给 LLM，支持攻击注入（在指定轮次注入恶意 payload）。 |
| **planning_mode.py** | Plan-and-Execute 模式实现。先由 LLM 生成计划，再按计划逐步执行工具调用。 |
| **llm_model_wrapper.py** | LLM 模型包装器。提供 `ReplayableModel` 等包装，支持日志重放、调用拦截等。 |
| **tool_utils.py** | 工具调用工具函数。解析 LLM 返回中的工具调用、执行工具并格式化结果。 |
| **message_types.py** | 消息类型定义。定义 `ChatAssistantMessage` 等类型，用于类型安全的消息解析与转换。 |

### 5.3 models/ — LLM 集成

| 文件 | 功能 |
|------|------|
| **base.py** | 模型基类与通用逻辑。 |
| **openai_provider.py** | OpenAI（GPT 系列）模型集成。 |
| **anthropic_provider.py** | Anthropic（Claude 系列）模型集成。 |
| **gemini_provider.py** | Google Gemini 模型集成。 |
| **qwen_provider.py** | 阿里通义千问（DashScope）模型集成。 |
| **deepseek_provider.py** | DeepSeek 模型集成。 |
| **factory** | 模型工厂，根据 `vendor` 和 `name` 创建对应 LLM 实例。 |

### 5.4 tools/ — MCP 工具适配

| 文件 | 功能 |
|------|------|
| **mcp_client.py** | MCP 客户端基类。定义 `MCPClient`、`MCPToolSpec`，声明 `connect`、`list_tools`、`invoke` 等接口。 |
| **http_mcp_client.py** | 基于 HTTP/SSE 的 MCP 客户端实现。用于连接通过 HTTP 暴露的 MCP 服务。 |
| **mcp_tool_adapters.py** | 将 MCP 工具规范转换为 LangChain `BaseTool`，供 Agent 调用。 |

### 5.5 container/ — 容器与沙箱

| 文件/目录 | 功能 |
|-----------|------|
| **docker_manager.py** | Docker 容器管理。负责容器的创建、启动、停止、删除、网络配置等。 |
| **server_manager.py** | 服务器管理。协调容器与 MCP 服务器的部署、状态查询。 |
| **server_deployer.py** | 服务器部署逻辑。根据 `servers.json` 将 MCP 服务器部署到容器中。 |
| **state_manager.py** | 状态管理。维护沙箱、服务器等运行时状态。 |
| **interceptors/** | HTTP 请求拦截器。用于记录、分析 MCP 服务器的 HTTP 流量，支持安全审计。 |
| **proxy/** | 代理模块。如 stdio 到 SSE 的协议转换，便于远程连接 MCP。 |
| **replay/** | 重放模块。支持将记录下的 LLM 调用重放，用于调试与复现。 |
| **manage_network.sh** | 网络管理脚本。配置容器网络、端口等。 |

### 5.6 scripts/ — 工具脚本

| 文件 | 功能 |
|------|------|
| **build_base_image.sh** | 构建基础 Docker 镜像。预装 Python、Node.js、uv、iptables，并复制拦截器、代理、重放代码。 |
| **configure_servers.py** | 服务器配置脚本。支持部署、扫描、停止、删除指定或全部 MCP 服务器。 |
| **sync_infrastructure.py** | 基础设施同步。将本地代码同步到容器中。 |
| **sync_to_container.sh** | 同步到容器的 Shell 脚本。 |
| **manage_replay_logs.py** | 重放日志管理。清理、归档或分析重放日志。 |

### 5.7 workflow_build/ — 工作流构建

| 文件 | 功能 |
|------|------|
| **build_benign_workflow.py** | 良性工作流构建器。可结合 LLM 自动生成良性评估工作流，输出到 `workflows_benign.json`。 |
| **build_injection_workflow.py** | 注入工作流构建器。生成用于攻击注入的工作流定义。 |
| **analyze_servers.py** | 服务器分析。分析 MCP 服务器的工具列表、能力等，辅助工作流构建。 |
| **Info.json** | 工作流构建所需的元信息。 |

---

## 六、运行时生成的目录

| 目录 | 功能 |
|------|------|
| **logs/** | 评估结果与执行日志。存放工作流执行结果、Agent 响应、评估指标等。 |
| **workflow_logs/** | 详细工作流执行日志。包含每次执行的完整调用链与调试信息。 |
| **servers_source/** | MCP 服务器源码。需从项目 Release 页面下载 `servers_source.zip` 并解压到此目录。 |

---

## 七、典型使用流程

1. **安装依赖**：`uv sync`
2. **构建镜像**：`./src/scripts/build_base_image.sh`
3. **配置**：复制各 `*.example.*` 为正式配置，编辑 `container.yaml`、`servers.json`、`workflows.json`、`.env`
4. **部署服务器**：`python src/scripts/configure_servers.py --config configs/react.yaml`
5. **运行评估**：
   - 单工作流：`uv run python main.py --config configs/react.yaml --workflows configs/workflows.json --workflow-id wf_notion_langfuse`
   - 攻击模式：`uv run python main.py --config configs/react.yaml --workflows configs/workflows_benign.json --attack-mode --injection-workflows configs/workflows_injection.json --injection-k 5`

---

## 八、相关链接

- **论文**：Evaluating Privilege Usage of Agents on Real-World Tools
- **仓库**：[https://github.com/ZQ-Struggle/Agent-GrantBox](https://github.com/ZQ-Struggle/Agent-GrantBox)
- **MCP 服务器源码**：见项目 Release 页面的 `servers_source.zip`
