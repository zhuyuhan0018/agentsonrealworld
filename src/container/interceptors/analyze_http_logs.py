"""Utility script to analyze HTTP request logs."""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List


def load_logs(log_file: Path) -> List[Dict]:
    """Load log entries from a JSONL file."""
    entries = []
    if not log_file.exists():
        return entries
    
    with open(log_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    
    return entries


def analyze_logs(log_dir: Path) -> None:
    """Analyze HTTP request logs."""
    log_files = sorted(log_dir.glob("requests_*.jsonl"))
    
    if not log_files:
        print(f"No log files found in {log_dir}")
        return
    
    all_entries = []
    for log_file in log_files:
        entries = load_logs(log_file)
        all_entries.extend(entries)
    
    if not all_entries:
        print("No log entries found")
        return
    
    print(f"=== HTTP Request Log Analysis ===\n")
    print(f"Total requests: {len(all_entries)}\n")
    
    # Statistics by language
    by_language = defaultdict(int)
    by_server = defaultdict(int)
    by_method = defaultdict(int)
    by_status = defaultdict(int)
    errors = []
    total_duration = 0.0
    request_count = 0
    
    for entry in all_entries:
        by_language[entry.get("language", "unknown")] += 1
        by_server[entry.get("server", "unknown")] += 1
        
        req = entry.get("request", {})
        method = req.get("method", "UNKNOWN")
        by_method[method] += 1
        
        resp = entry.get("response")
        if resp:
            status = resp.get("status")
            if status:
                by_status[status] += 1
        else:
            errors.append(entry)
        
        if "duration_ms" in entry:
            total_duration += entry["duration_ms"]
            request_count += 1
    
    # Print statistics
    print("By Language:")
    for lang, count in sorted(by_language.items()):
        print(f"  {lang}: {count}")
    print()
    
    print("By Server:")
    for server, count in sorted(by_server.items()):
        print(f"  {server}: {count}")
    print()
    
    print("By Method:")
    for method, count in sorted(by_method.items()):
        print(f"  {method}: {count}")
    print()
    
    print("By Status Code:")
    for status, count in sorted(by_status.items()):
        print(f"  {status}: {count}")
    print()
    
    if errors:
        print(f"Errors: {len(errors)}")
        for err in errors[:5]:  # Show first 5 errors
            req = err.get("request", {})
            print(f"  {req.get('method')} {req.get('url')}: {err.get('error', 'Unknown error')}")
        if len(errors) > 5:
            print(f"  ... and {len(errors) - 5} more errors")
        print()
    
    if request_count > 0:
        avg_duration = total_duration / request_count
        print(f"Average request duration: {avg_duration:.2f}ms")
        print()
    
    # Show recent requests
    print("Recent Requests (last 10):")
    for entry in all_entries[-10:]:
        req = entry.get("request", {})
        resp = entry.get("response")
        method = req.get("method", "UNKNOWN")
        url = req.get("url", "unknown")
        status = resp.get("status") if resp else "ERROR"
        duration = entry.get("duration_ms", 0)
        print(f"  [{entry.get('language', '?')}] {method} {url} -> {status} ({duration:.1f}ms)")


def main():
    """Main entry point."""
    if len(sys.argv) > 1:
        log_dir = Path(sys.argv[1])
    else:
        # Default to logs/http_requests relative to project root
        project_root = Path(__file__).parent.parent.parent
        log_dir = project_root / "logs" / "http_requests"
    
    analyze_logs(log_dir)


if __name__ == "__main__":
    main()

