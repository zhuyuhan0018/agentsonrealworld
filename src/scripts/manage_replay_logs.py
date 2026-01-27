#!/usr/bin/env python3
"""CLI tool for managing HTTP and LLM replay logs."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# Add project root to path (script is at src/scripts/, so 2 levels up)
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from src.container.replay.log_manager import LogManager
from src.logging_config import setup_logging, get_logger

logger = get_logger(__name__)


def _run_docker_command(container_name: str, command: str) -> bool:
    """Run a command inside Docker container.
    
    Args:
        container_name: Name of the Docker container
        command: Command to execute
        
    Returns:
        True if successful, False otherwise
    """
    try:
        cmd = ["docker", "exec", container_name, "bash", "-c", command]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=30
        )
        logger.debug(f"Docker command output: {result.stdout}")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"Docker command failed: {e.stderr}")
        return False
    except subprocess.TimeoutExpired:
        logger.error(f"Docker command timed out")
        return False
    except Exception as e:
        logger.error(f"Docker command error: {e}")
        return False


def _delete_via_docker(container_name: str, exec_path: str) -> bool:
    """Delete execution directory via Docker.
    
    Args:
        container_name: Name of the Docker container
        exec_path: Path to execution directory (relative to mounted volume)
        
    Returns:
        True if successful, False otherwise
    """
    # Convert host path to container path
    # Assuming workflow_logs is mounted to /mcp_logs in container
    container_path = f"/mcp_logs/{exec_path}"
    command = f"rm -rf {container_path}"
    
    logger.info(f"Deleting via Docker: {container_path}")
    return _run_docker_command(container_name, command)


def _count_llm_calls(exec_dir: Path) -> int:
    """Count LLM calls from llm_responses.jsonl file.
    
    New structure: {execution_id}/llm_replay/llm_responses.jsonl
    """
    llm_file = exec_dir / "llm_replay" / "llm_responses.jsonl"
    if llm_file.exists():
        try:
            with open(llm_file, "r", encoding="utf-8") as f:
                return sum(1 for line in f if line.strip())
        except Exception:
            pass
    return 0


def _get_combined_execution_info(
    log_mgr: LogManager,
    execution_id: str,
    workflow_id: str = None
) -> dict:
    """Get combined execution info from both HTTP and LLM logs."""
    exec_info = log_mgr.get_execution(execution_id, workflow_id)
    
    if exec_info:
        exec_dir = Path(exec_info["path"])
        llm_calls = _count_llm_calls(exec_dir)
        exec_info["total_llm_calls"] = llm_calls
    
    return exec_info


def cmd_list(args):
    """List all execution records (HTTP and LLM combined)."""
    log_mgr = LogManager(args.replay_dir)
    
    # Get all executions
    executions = log_mgr.list_executions(args.workflow_id)
    
    if not executions:
        print(f"No execution records found" + (f" for workflow {args.workflow_id}" if args.workflow_id else ""))
        return
    
    print(f"\nFound {len(executions)} execution record(s):\n")
    print(f"{'Execution ID':<40} {'Workflow':<20} {'Status':<12} {'Start Time':<25} {'HTTP Req':<10} {'LLM Calls':<10}")
    print("-" * 130)
    
    for exec_info in executions:
        # Add LLM call count
        exec_dir = Path(exec_info["path"])
        llm_calls = _count_llm_calls(exec_dir)
        
        exec_id_short = exec_info["execution_id"][:38] + ".." if len(exec_info["execution_id"]) > 40 else exec_info["execution_id"]
        workflow = exec_info["workflow_name"][:18] + ".." if len(exec_info["workflow_name"]) > 20 else exec_info["workflow_name"]
        status = exec_info["status"]
        start_time = exec_info["start_time"][:23] if exec_info["start_time"] else "N/A"
        http_requests = exec_info.get("total_requests", 0)
        
        print(f"{exec_id_short:<40} {workflow:<20} {status:<12} {start_time:<25} {http_requests:<10} {llm_calls:<10}")
    
    print()


def cmd_show(args):
    """Show detailed information about an execution (HTTP and LLM combined)."""
    log_mgr = LogManager(args.replay_dir)
    
    exec_info = _get_combined_execution_info(log_mgr, args.execution_id, args.workflow_id)
    
    if not exec_info:
        print(f"Execution {args.execution_id} not found")
        sys.exit(1)
    
    print(f"\nExecution Details:")
    print("=" * 70)
    print(f"Execution ID:     {exec_info['execution_id']}")
    print(f"Workflow ID:      {exec_info['workflow_id']}")
    print(f"Workflow Name:    {exec_info['workflow_name']}")
    print(f"Status:           {exec_info['status']}")
    print(f"Start Time:       {exec_info['start_time'] or 'N/A'}")
    print(f"End Time:         {exec_info['end_time'] or 'N/A'}")
    print(f"HTTP Requests:    {exec_info.get('total_requests', 0)}")
    print(f"LLM Calls:        {exec_info.get('total_llm_calls', 0)}")
    print(f"Servers:          {', '.join(exec_info.get('servers', []))}")
    print(f"HTTP Log Path:    {exec_info.get('path', 'N/A')}")
    
    # Check for LLM log path
    # New structure: workflow_logs/{workflow_id}/{execution_id}/llm_replay/
    exec_dir = Path(exec_info.get('path', ''))
    llm_exec_dir = exec_dir / "llm_replay"
    if llm_exec_dir.exists():
        print(f"LLM Log Path:     {llm_exec_dir}")
    
    if exec_info.get("server_stats"):
        print(f"\nHTTP Server Statistics:")
        for server, count in exec_info["server_stats"].items():
            print(f"  {server}: {count} requests")
    
    print()


def cmd_delete(args):
    """Delete an execution record (both HTTP and LLM logs)."""
    log_mgr = LogManager(args.replay_dir)
    
    # Get execution info
    exec_info = _get_combined_execution_info(log_mgr, args.execution_id, args.workflow_id)
    if not exec_info:
        print(f"Execution {args.execution_id} not found")
        sys.exit(1)
    
    if not args.yes:
        print(f"\nAre you sure you want to delete execution {args.execution_id}?")
        print(f"Workflow: {exec_info['workflow_name']}")
        print(f"Status: {exec_info['status']}")
        print(f"HTTP Requests: {exec_info.get('total_requests', 0)}")
        print(f"LLM Calls: {exec_info.get('total_llm_calls', 0)}")
        response = input("\nType 'yes' to confirm: ")
        if response.lower() != 'yes':
            print("Cancelled")
            return
    
    # Try to delete directly first
    deleted = log_mgr.delete_execution(args.execution_id, args.workflow_id)
    
    if deleted:
        print(f"Deleted execution {args.execution_id}")
    else:
        # If direct deletion failed (likely permission issue), try via Docker
        if args.container_name:
            print(f"Direct deletion failed, attempting via Docker container '{args.container_name}'...")
            
            # Build relative path for docker deletion
            workflow_id = exec_info['workflow_id']
            exec_id = exec_info['execution_id']
            rel_path = f"{workflow_id}/{exec_id}"
            
            if _delete_via_docker(args.container_name, rel_path):
                print(f"Deleted execution {args.execution_id} via Docker")
            else:
                print(f"Failed to delete execution {args.execution_id}")
                print(f"Hint: Files may be owned by root. Try running with sudo or:")
                print(f"  docker exec {args.container_name} rm -rf /mcp_logs/{rel_path}")
                sys.exit(1)
        else:
            print(f"Failed to delete execution {args.execution_id}")
            print(f"Hint: Files may be owned by root. Retry with --container-name or run with sudo")
            sys.exit(1)


def cmd_cleanup(args):
    """Clean up old execution records (both HTTP and LLM logs)."""
    log_mgr = LogManager(args.replay_dir)
    
    if not args.workflow_id:
        print("Error: --workflow-id is required for cleanup")
        sys.exit(1)
    
    if args.dry_run:
        to_delete = log_mgr.cleanup_old_executions(args.workflow_id, args.keep, dry_run=True)
        print(f"\nDry run: Would delete {len(to_delete)} execution(s):")
        for exec_id in to_delete:
            print(f"  - {exec_id}")
        print()
    else:
        if not args.yes:
            to_delete = log_mgr.cleanup_old_executions(args.workflow_id, args.keep, dry_run=True)
            if not to_delete:
                print(f"No executions to clean up for workflow {args.workflow_id}")
                return
            
            print(f"\nWill delete {len(to_delete)} execution(s) for workflow {args.workflow_id}:")
            for exec_id in to_delete[:10]:  # Show first 10
                print(f"  - {exec_id}")
            if len(to_delete) > 10:
                print(f"  ... and {len(to_delete) - 10} more")
            
            response = input("\nType 'yes' to confirm: ")
            if response.lower() != 'yes':
                print("Cancelled")
                return
        
        deleted = log_mgr.cleanup_old_executions(args.workflow_id, args.keep, dry_run=False)
        
        # Check if some deletions failed (permission issues)
        to_delete = log_mgr.cleanup_old_executions(args.workflow_id, args.keep, dry_run=True)
        failed = [ex for ex in to_delete if ex not in deleted]
        
        if deleted:
            print(f"\nDeleted {len(deleted)} execution(s):")
            for exec_id in deleted:
                print(f"  - {exec_id}")
        
        if failed and args.container_name:
            print(f"\n{len(failed)} execution(s) failed to delete, retrying via Docker...")
            docker_deleted = []
            for exec_id in failed:
                rel_path = f"{args.workflow_id}/{exec_id}"
                if _delete_via_docker(args.container_name, rel_path):
                    docker_deleted.append(exec_id)
                    print(f"  - {exec_id} (via Docker)")
            
            if docker_deleted:
                print(f"\nDeleted {len(docker_deleted)} additional execution(s) via Docker")
            
            still_failed = [ex for ex in failed if ex not in docker_deleted]
            if still_failed:
                print(f"\nWarning: {len(still_failed)} execution(s) could not be deleted")
        elif failed:
            print(f"\nWarning: {len(failed)} execution(s) could not be deleted (permission denied)")
            print(f"Hint: Retry with --container-name to delete via Docker")
        
        print()


def cmd_export(args):
    """Export an execution record (both HTTP and LLM logs)."""
    log_mgr = LogManager(args.replay_dir)
    output_path = Path(args.output)
    
    exec_info = _get_combined_execution_info(log_mgr, args.execution_id, args.workflow_id)
    if not exec_info:
        print(f"Execution {args.execution_id} not found")
        sys.exit(1)
    
    # Export HTTP logs
    http_exported = log_mgr.export_execution(args.execution_id, output_path, args.workflow_id)
    
    # Add LLM logs to the export
    # New structure: workflow_logs/{workflow_id}/{execution_id}/llm_replay/llm_responses.jsonl
    if exec_info.get('workflow_id') and exec_info.get('execution_id'):
        exec_dir = Path(exec_info.get('path', ''))
        llm_file = exec_dir / "llm_replay" / "llm_responses.jsonl"
        
        if llm_file.exists():
            try:
                with open(output_path, "r", encoding="utf-8") as f:
                    export_data = json.load(f)
                
                # Add LLM responses
                export_data["llm_responses"] = []
                with open(llm_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                export_data["llm_responses"].append(json.loads(line))
                            except json.JSONDecodeError:
                                pass
                
                export_data["total_llm_calls"] = len(export_data["llm_responses"])
                
                with open(output_path, "w", encoding="utf-8") as f:
                    json.dump(export_data, f, indent=2, ensure_ascii=False)
            except Exception as e:
                logger.warning(f"Failed to add LLM logs to export: {e}")
    
    if http_exported:
        print(f"Exported execution {args.execution_id} to {output_path}")
    else:
        print(f"Failed to export execution {args.execution_id}")
        sys.exit(1)


def cmd_stats(args):
    """Show statistics (HTTP and LLM combined)."""
    log_mgr = LogManager(args.replay_dir)
    
    stats = log_mgr.get_stats(args.workflow_id)
    
    # Count total LLM calls
    total_llm_calls = 0
    if args.workflow_id:
        workflow_dir = args.replay_dir / args.workflow_id
        if workflow_dir.exists():
            for exec_dir in workflow_dir.iterdir():
                if exec_dir.is_dir():
                    total_llm_calls += _count_llm_calls(exec_dir)
    else:
        for workflow_dir in args.replay_dir.iterdir():
            if workflow_dir.is_dir():
                for exec_dir in workflow_dir.iterdir():
                    if exec_dir.is_dir():
                        total_llm_calls += _count_llm_calls(exec_dir)
    
    print(f"\nReplay Log Statistics (HTTP + LLM):")
    print("=" * 70)
    print(f"Total Executions: {stats['total_executions']}")
    print(f"Total HTTP Requests: {stats['total_requests']}")
    print(f"Total LLM Calls:     {total_llm_calls}")
    
    if stats['oldest_execution']:
        print(f"Oldest Execution: {stats['oldest_execution']}")
    if stats['newest_execution']:
        print(f"Newest Execution: {stats['newest_execution']}")
    
    if stats['by_status']:
        print(f"\nBy Status:")
        for status, count in sorted(stats['by_status'].items()):
            print(f"  {status}: {count}")
    
    if stats['by_workflow']:
        print(f"\nBy Workflow:")
        for workflow_id, wf_stats in sorted(stats['by_workflow'].items()):
            # Count LLM calls for this workflow
            wf_llm_calls = 0
            wf_dir = args.replay_dir / workflow_id
            if wf_dir.exists():
                for exec_dir in wf_dir.iterdir():
                    if exec_dir.is_dir():
                        wf_llm_calls += _count_llm_calls(exec_dir)
            
            print(f"  {workflow_id}:")
            print(f"    Executions: {wf_stats['count']}")
            print(f"    HTTP Requests: {wf_stats['total_requests']}")
            print(f"    LLM Calls: {wf_llm_calls}")
    
    print()


def cmd_latest(args):
    """Find latest execution ID for a workflow."""
    log_mgr = LogManager(args.replay_dir)
    
    if not args.workflow_id:
        print("Error: --workflow-id is required")
        sys.exit(1)
    
    latest_id = log_mgr.find_latest_execution(args.workflow_id)
    
    if latest_id:
        print(latest_id)
    else:
        print(f"No execution records found for workflow {args.workflow_id}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Manage HTTP and LLM replay logs")
    parser.add_argument(
        "--replay-dir",
        type=Path,
        default=Path("workflow_logs"),
        help="Base replay log directory (default: workflow_logs). Structure: {workflow_id}/{execution_id}/http_replay/ and llm_replay/."
    )
    parser.add_argument(
        "--workflow-id",
        type=str,
        help="Workflow ID (for filtering)"
    )
    parser.add_argument(
        "--container-name",
        type=str,
        default=os.environ.get("MCP_CONTAINER_NAME", "mcp-sandbox"),
        help="Docker container name for deleting files with permission issues (default: mcp-sandbox or MCP_CONTAINER_NAME env var)"
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Log level"
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Command to execute")
    
    # list command
    list_parser = subparsers.add_parser("list", help="List all execution records")
    
    # show command
    show_parser = subparsers.add_parser("show", help="Show execution details")
    show_parser.add_argument("execution_id", help="Execution ID")
    
    # delete command
    delete_parser = subparsers.add_parser("delete", help="Delete an execution record")
    delete_parser.add_argument("execution_id", help="Execution ID")
    delete_parser.add_argument("--yes", action="store_true", help="Skip confirmation")
    
    # cleanup command
    cleanup_parser = subparsers.add_parser("cleanup", help="Clean up old execution records, keeping only the most recent")
    cleanup_parser.add_argument("--keep", type=int, default=1, help="Number of executions to keep (default: 1)")
    cleanup_parser.add_argument("--dry-run", action="store_true", help="Show what would be deleted without actually deleting")
    cleanup_parser.add_argument("--yes", action="store_true", help="Skip confirmation")
    
    # export command
    export_parser = subparsers.add_parser("export", help="Export an execution record")
    export_parser.add_argument("execution_id", help="Execution ID")
    export_parser.add_argument("output", help="Output file path")
    
    # stats command
    stats_parser = subparsers.add_parser("stats", help="Show statistics")
    
    # latest command
    latest_parser = subparsers.add_parser("latest", help="Find latest execution ID for a workflow")
    
    args = parser.parse_args()
    
    # Setup logging
    setup_logging(console_level=args.log_level, file_level="DEBUG")
    
    if not args.command:
        parser.print_help()
        sys.exit(1)
    
    # Execute command
    commands = {
        "list": cmd_list,
        "show": cmd_show,
        "delete": cmd_delete,
        "cleanup": cmd_cleanup,
        "export": cmd_export,
        "stats": cmd_stats,
        "latest": cmd_latest,
    }
    
    commands[args.command](args)


if __name__ == "__main__":
    main()

