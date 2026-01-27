# GrantBox

This repository contains the source code and dataset for the paper **"Evaluating Privilege Usage of Agents on Real-World Tools"**. 

GrantBox is a security evaluation framework designed to systematically assess how autonomous agents handle privilege usage when interacting with real-world tools and services. The framework provides a sandbox environment that integrates real-world MCP servers with privilege-sensitive tools, enabling comprehensive security evaluation of agents across cloud infrastructure, databases, email services, and other critical systems.

## Deployment

### Prerequisites

**System Requirements:**
- **OS**: Linux (Ubuntu/Debian recommended) or macOS
- **uv**: Lightweight tool for managing Python dependencies and syncing reproducible environments
- **Docker**: Installed and running
- **Git**: For cloning the repository

### Step-by-Step Deployment

**1. Install Python Dependencies**

**Using uv (Recommended)**
```bash
# Install project dependencies
uv sync
```

**2. Build Base Docker Image**

Build the base Docker image that includes all runtime code (interceptors, proxy, replay):

```bash
./src/scripts/build_base_image.sh
```

This will:
- Create `mcp-sandbox-base:latest` image
- Pre-install Python, Node.js, uv, iptables
- Copy interceptors, proxy, and replay code into the image

**Note**: This step takes 2-5 minutes depending on your network speed.

**3. Configure Container**

Copy the container configuration file:

```bash
cp configs/container.example.yaml configs/container.yaml
```

Edit `configs/container.yaml` to customize container settings as needed.


**4. Configure Servers**

Copy and edit the servers configuration:

```bash
cp configs/servers.example.json configs/servers.json
```

Edit `configs/servers.json` to add your MCP servers. See `configs/servers.example.json` for examples.

**5. Configure Workflows**

Copy and edit the workflows configuration:

```bash
cp configs/workflows.example.json configs/workflows.json
```

Edit `configs/workflows.json` to define your workflows.


**6. Set Up Environment Variables**

Create a `.env` file in the project root:

```bash
# Model API keys
OPENAI_API_KEY=your-openai-key
ANTHROPIC_API_KEY=your-anthropic-key
DASHSCOPE_API_KEY=your-dashscope-key
GOOGLE_API_KEY=your-google-key
DEEPSEEK_API_KEY=your-deepseek-key

# Other configuration
LOG_LEVEL=INFO
```

**7. Verify Docker Setup**

Test Docker access:

```bash
docker ps
docker images
```

**8. Configure Pipeline Mode**

```bash
cp configs/react.example.yaml configs/react.yaml
```

## Quickstart

First, install dependencies:

```bash
uv sync
```

Then run a workflow evaluation:

```bash
# Workflow mode (required)
# --config contains model and agent settings (mode, model vendor/name, kwargs)
uv run python main.py \
  --config configs/react.yaml \
  --workflows configs/workflows.json \
  --workflow-id wf_notion_langfuse
```

## Evaluation Steps

**1. Configure Model and Provider**

Edit the corresponding mode file (`configs/react.yaml`) to configure your model and provider settings.

**2. Run Batch Experiments**

Execute the following command to run batch experiments:

```bash
uv run python main.py \
  --config configs/react.yaml \
  --workflows configs/workflows_benign.json \
  --attack-mode \
  --injection-workflows configs/workflows_injection.json \
  --injection-k 5
```

**3. Evaluate a Single Workflow**

To evaluate one benign workflow with a specific injection workflow:

```bash
uv run python main.py \
  --config configs/react.yaml \
  --workflows configs/workflows_benign.json \
  --workflow-id system-prompt-audit-workflow \
  --attack-mode \
  --injection-workflows configs/workflows_injection.json \
  --injection-id repo-purge-compliance-lure
```

The corresponding results will be stored in the `logs/` directory.

## Container and Server Management

The sandbox uses Docker containers to run MCP servers in isolation. Servers are deployed on-demand and accessed via container IP addresses.

```bash
# Deploy all servers
python src/scripts/configure_servers.py --config configs/react.yaml

# Check server status
python src/scripts/configure_servers.py --config configs/react.yaml --scan

# Remove all servers
python src/scripts/configure_servers.py --config configs/react.yaml --remove-all

# Deploy and start a specific server
python src/scripts/configure_servers.py --config configs/react.yaml --server <server_name>

# Stop a server
python src/scripts/configure_servers.py --config configs/react.yaml --server <server_name> --stop

# Remove a server's directory
python src/scripts/configure_servers.py --config configs/react.yaml --server <server_name> --remove
```

See [src/container/CONTAINER_README.md](src/container/CONTAINER_README.md) for complete documentation on:
- Server deployment and configuration
- Container lifecycle management
- Port management and networking
- Base image usage and benefits
- Troubleshooting and best practices


## Directories

The project structure is organized as follows:

- **`configs/`** - Configuration files for the framework
  - `workflows_benign.json` - Benign workflow definitions for evaluation
  - `workflows_injection.json` - Malicious injection workflow definitions
  - `*.example.*` - Example configuration templates

- **`src/`** - Core source code
  - `agents/` - Agent implementations (ReAct, Plan-and-Execute, etc.)
  - `container/` - Container management and sandbox infrastructure
  - `models/` - LLM model integration and API clients
  - `scripts/` - Utility scripts
  - `tools/` - Tool definitions and utilities
  - `attack_workflow_builder/` - Workflow builder
  - `pipeline.py` - Main evaluation pipeline
  - `config.py` - Configuration loading and validation

- **`servers_source/`** - MCP server source code repositories
  - Contains cloned or linked MCP server implementations (e.g., `github-mcp-server`, `notion-mcp`, `email-mcp`, etc.)
  - These servers are deployed into containers during evaluation

- **`logs/`** - Evaluation results and execution logs
  - Stores workflow execution results, agent responses, and evaluation metrics
  - Generated during evaluation runs

- **`workflow_logs/`** - Detailed workflow execution logs
  - Contains per-workflow execution traces and debugging information


## Connect to Container

To access the container shell:

```bash
docker exec -it mcp-sandbox /bin/bash
```
