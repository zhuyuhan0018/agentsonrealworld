#!/usr/bin/env python3
"""Start MCP servers from configs/servers.json sequentially and collect tool info.

This script leverages the existing container/server management utilities to:
- Ensure the Docker container and all servers are up
- Connect to each server via HTTP/SSE
- Retrieve available MCP tools (name, description, input schema)
- Write aggregated results to a JSON file

Usage:
  python src/scripts/analyze_servers.py \
	--config configs/react.yaml \
	--servers configs/servers.json \
	--out configs/server_tools.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List

# Ensure project root is on path (script located at src/scripts)
import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.config import PipelineConfig, load_servers_config
from src.container.server_manager import ServerManager
from src.logging_config import setup_logging, get_logger
from src.tools.http_mcp_client import HTTPMCPClient


logger = get_logger(__name__)


async def collect_server_tools(
	server_manager: ServerManager,
	servers_cfg_path: str,
	output_path: str,
) -> Dict[str, Any]:
	"""Ensure servers are running, then connect and list tools for each.

	Returns aggregated results as a dictionary and writes to `output_path`.
	"""
	servers_config = load_servers_config(servers_cfg_path)
	server_names: List[str] = list(servers_config.keys())

	# Ensure servers are up (deploy/start as needed)
	logger.info(f"Ensuring {len(server_names)} servers are running...")
	server_manager.disable_network_isolation()  # allow dependencies during startup
	server_manager.ensure_servers_running(server_names, servers_cfg_path)

	results: Dict[str, Any] = {
		"summary": {
			"count": len(server_names),
		},
		"servers": {},
	}

	# Collect tools from each server sequentially
	for server_key in server_names:
		server_info = servers_config.get(server_key, {})
		server_name = server_info.get("name", server_key)
		endpoint = server_manager.get_server_endpoint(server_key)

		server_result: Dict[str, Any] = {
			"name": server_name,
			"endpoint": endpoint or "",
			"transport": server_info.get("transport", "sse"),
			"tools": [],
			"error": None,
		}

		if not endpoint:
			server_result["error"] = "No endpoint found (server may not be running)"
			results["servers"][server_name] = server_result
			logger.warning(f"Skipping {server_name}: no endpoint")
			continue

		try:
			client = HTTPMCPClient(
				server_name,
				server_result["transport"],
				endpoint,
				**server_info.get("kwargs", {}),
			)
			await client.connect()
			tools = await client.list_tools()
			await client.close()

			# Normalize tools to serializable dicts
			server_result["tools"] = [
				{
					"name": t.name,
					"description": t.description,
					"input_schema": t.input_schema or {},
				}
				for t in tools
			]
			logger.info(f"{server_name}: {len(server_result['tools'])} tools")
		except Exception as e:
			server_result["error"] = f"Failed to list tools: {e}"
			logger.error(f"Failed to collect tools from {server_name}: {e}")

		results["servers"][server_name] = server_result

	# Write results
	out_path = Path(output_path)
	out_path.parent.mkdir(parents=True, exist_ok=True)
	with open(out_path, "w", encoding="utf-8") as f:
		json.dump(results, f, ensure_ascii=False, indent=2)
	logger.info(f"Wrote server tools to {out_path}")

	return results


def main() -> None:
	parser = argparse.ArgumentParser(description="Start servers and collect MCP tools")
	parser.add_argument(
		"--config",
		default="configs/react.yaml",
		help="Pipeline YAML config that includes container_config_path",
	)
	parser.add_argument(
		"--servers",
		default="configs/servers.json",
		help="Path to servers.json configuration",
	)
	parser.add_argument(
		"--out",
		default="configs/server_tools.json",
		help="Output path for aggregated tools JSON",
	)
	parser.add_argument(
		"--new-container",
		action="store_true",
		help="Start with a fresh container (remove existing)",
	)
	parser.add_argument(
		"--log-level",
		choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
		default="INFO",
		help="Console log level",
	)

	args = parser.parse_args()

	# Setup logging
	setup_logging(console_level=args.log_level, file_level="DEBUG")
	logger.info("==== Analyze MCP Servers ====")

	# Load pipeline + container config
	cfg = PipelineConfig.from_yaml(args.config)
	container_config = cfg.get_container_config()
	if not container_config:
		logger.error("No container_config_path in pipeline config")
		sys.exit(1)
	logger.info(f"Container: {container_config.name}, Image: {container_config.image}")

	server_manager = ServerManager(container_config, enable_interceptors=False)

	# Optionally start fresh container
	if args.new_container:
		# Reuse the built-in method by ensuring with start_new_container=True
		# (will also reset state)
		logger.info("Starting fresh container as requested...")
		server_manager.ensure_servers_running([], args.servers, start_new_container=True)

	# Run async collection
	try:
		asyncio.run(collect_server_tools(server_manager, args.servers, args.out))
	except Exception as e:
		logger.critical(f"Fatal error: {e}")
		sys.exit(1)


if __name__ == "__main__":
	main()

