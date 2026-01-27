#!/usr/bin/env python3
"""Sync infrastructure files (interceptors, proxy, replay) to a running container."""

import argparse
import sys
from pathlib import Path

# Add project root to path (script is at src/scripts/, so 2 levels up)
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.container.server_deployer import ServerDeployer
from src.container.docker_manager import DockerManager
from src.logging_config import setup_logging, get_logger


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync infrastructure files to a running Docker container",
        epilog="""
Examples:
  python sync_infrastructure.py                    # Use default container
  python sync_infrastructure.py --container my-container
  python sync_infrastructure.py --log-level DEBUG
        """
    )
    parser.add_argument(
        "--container", "-c",
        default="mcp-sandbox",
        help="Container name (default: mcp-sandbox)"
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Log level (default: INFO)"
    )
    
    args = parser.parse_args()
    setup_logging(console_level=args.log_level)
    logger = get_logger(__name__)
    
    # Check container exists
    docker = DockerManager()
    if not docker.container_exists(args.container):
        logger.error(f"Container '{args.container}' does not exist")
        sys.exit(1)
    
    # Sync infrastructure
    deployer = ServerDeployer(args.container)
    deployer.sync_infrastructure(PROJECT_ROOT)
    
    logger.info("=" * 60)
    logger.info("Sync Complete!")
    logger.info("=" * 60)
    logger.info("")
    logger.info("To verify, check files in the container:")
    logger.info(f"  docker exec {args.container} ls -la /mcp_interceptors/interceptors")
    logger.info(f"  docker exec {args.container} ls -la /mcp_proxy")
    logger.info(f"  docker exec {args.container} ls -la /mcp_interceptors/replay")
    logger.info(f"  docker exec {args.container} ls -la /mcp_scripts/manage_network.sh")


if __name__ == "__main__":
    main()

