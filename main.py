import random
from dotenv import load_dotenv
load_dotenv()

import argparse
import sys
from pathlib import Path
import json
from datetime import datetime
import traceback

from src.logging_config import setup_logging, get_logger
from src.config import (
    PipelineConfig,
    load_workflows_json,
    Workflow,
    TaskConfig,
)
from src.pipeline import run_from_parts
from src.pipeline import _load_injection_workflows



def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Agent Execution Sandbox")
    p.add_argument("--config", required=True, help="Pipeline YAML config (contains model and agent settings)")
    p.add_argument("--workflows", required=True, help="Path to workflows JSON file")
    p.add_argument("--workflow-id", required=False, default=None,
                   help="Workflow id(s) to run (comma-separated). If omitted, all workflows in the file are executed.")
    p.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], 
                   default="INFO", help="Console log level (default: INFO)")
    p.add_argument("--start-new-container", action="store_true", default=False,
                   help="Delete existing container and start fresh (default: False)")
    p.add_argument("--enable-interceptors", action="store_true", default=False,
                   help="Enable HTTP request interceptors for MCP servers (default: False)")
    p.add_argument("--attack-mode", action="store_true", default=False,
                   help="Enable attack injection from workflows_injection.json")
    p.add_argument("--injection-workflows", type=str, default="configs/workflows_injection.json",
                   help="(attack-mode only) Path to injection workflows JSON (default: configs/workflows_injection.json)")
    p.add_argument("--injection-workflow-id", type=str, default=None,
                   help="(attack-mode only) Injection workflow id; if omitted, any injection workflow may be chosen")
    p.add_argument("--injection-k", type=int, default=-1,
                   help="(attack-mode only) Number of injection workflows to sample at random; -1 means use all available")
    p.add_argument("--random-seed", type=int, default=2333,
                   help="Optional random seed for reproducible sampling (e.g., injection selection)")
    p.add_argument("--result-output", type=str, default=None,
                   help="Optional path to write a JSON report of execution results")
    p.add_argument("--resume", type=str, default=None,
                   help="Resume from an existing results JSON to continue unfinished batch runs")
    return p.parse_args()


def _build_resume_index(records: list[dict]) -> dict[str, set[str]]:
    """Index previously executed injections per workflow (including errors).

    Resume should not rerun any injection-workflow pair that already produced a
    record, regardless of state (normal, hijacked, failed, unknown, error).
    """
    index: dict[str, set[str]] = {}
    for rec in records or []:
        wf_id = rec.get("workflow_id")
        inj_id = rec.get("injection_workflow_id")
        if not wf_id or inj_id is None:
            continue

        index.setdefault(wf_id, set()).add(inj_id)
    return index


def main() -> None:
    args = parse_args()
    
    # Initialize logging
    logger = setup_logging(console_level=args.log_level, file_level="DEBUG")
    logger.info("=" * 60)
    logger.info("Starting Agent Execution Sandbox")
    logger.info("=" * 60)
    
    try:
        resume_data = None
        resume_path = Path(args.resume) if args.resume else None
        if resume_path:
            if not resume_path.exists():
                logger.error(f"Resume file not found: {resume_path}")
                raise SystemExit(f"Resume file not found: {resume_path}")
            try:
                with open(resume_path, "r", encoding="utf-8") as f:
                    resume_data = json.load(f)
                logger.info(f"Resume enabled: loaded previous results from {resume_path}")
            except Exception as e:
                logger.error(f"Failed to load resume file {resume_path}: {e}")
                raise SystemExit(f"Failed to load resume file {resume_path}: {e}")

        # Load pipeline config (model and agent settings)
        logger.info(f"Loading pipeline config: {args.config}")
        cfg = PipelineConfig.from_yaml(args.config)
        logger.debug(f"Config loaded: mode={cfg.mode}, model={cfg.model.vendor}/{cfg.model.name}")
        
        # Workflow mode: load workflows and determine targets (supports batch via comma-separated IDs; empty -> all)
        logger.info(f"Running workflow(s) from {args.workflows}: {args.workflow_id or 'ALL'}")
        workflows_file = load_workflows_json(args.workflows)
        workflow_map = {w.id: w for w in workflows_file.workflows}
        if args.workflow_id:
            workflow_ids = [wid.strip() for wid in args.workflow_id.split(",") if wid.strip()]
        else:
            workflow_ids = list(workflow_map.keys())
        if not workflow_ids:
            logger.error("No workflow ids provided or found in workflows file")
            raise SystemExit("No workflow ids provided or found in workflows file")

        # Enforce pairing: injection options only valid in attack mode; if injection-workflow-id is set, workflow-id must also be set
        if args.injection_workflow_id and not args.attack_mode:
            logger.error("--injection-workflow-id is only valid when --attack-mode is enabled")
            raise SystemExit("--injection-workflow-id requires --attack-mode")
        if args.injection_k != -1 and not args.attack_mode:
            logger.error("--injection-k is only valid when --attack-mode is enabled")
            raise SystemExit("--injection-k requires --attack-mode")
        if args.injection_workflow_id and not args.workflow_id:
            logger.error("--injection-workflow-id was provided without --workflow-id. Please specify the target workflow(s).")
            raise SystemExit("--injection-workflow-id requires --workflow-id")
        
        # Resolve report path once (auto-generate if not provided)
        if args.result_output:
            report_path = Path(args.result_output)
        elif resume_path:
            report_path = resume_path
        else:
            logs_dir = Path("logs")
            logs_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            llm_tag = f"{cfg.model.vendor}-{cfg.model.name}"
            mode_tag = "attack" if args.attack_mode else "benign"
            filename = f"{ts}_{llm_tag}_{mode_tag}.json"
            report_path = logs_dir / filename
        report_path.parent.mkdir(parents=True, exist_ok=True)

        # Load injections and determine batch vs single mode
        batch_run = (len(workflow_ids) > 1) 
        # Only write overall report when in batch mode or when user explicitly requests output path
        report_enabled = batch_run or bool(args.result_output) or bool(resume_path)

        # If the report exists, load it once for aggregation (only when enabled)
        existing = resume_data
        if existing is None and report_enabled and report_path.exists():
            try:
                with open(report_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception as e:
                logger.warning(f"Failed to read existing report at {report_path}: {e}")

        if args.random_seed is not None:
            random.seed(args.random_seed)
        # Initialize summary/counts
        summary = (existing or {}).get("summary", {})
        counts = summary.get("counts_by_state", {"normal": 0, "hijacked": 0, "failed": 0, "unknown": 0, "error": 0})
        # Normalize keys for older reports
        for k in ("normal", "hijacked", "failed", "unknown", "error"):
            counts[k] = counts.get(k, 0)
        executions = (existing or {}).get("executions", [])
        completed_injections_by_workflow = _build_resume_index(list(executions))

        inj_list: list[Workflow] = []
        if args.attack_mode:
            logger.info(f"Attack mode enabled: loading injection workflows from {args.injection_workflows}")
            inj_list = _load_injection_workflows(args.injection_workflows)
            logger.info(f"Loaded {len(inj_list)} injection workflows")

            # If specific injection workflow id is provided, filter the list
            if args.injection_workflow_id:
                inj_list = [inj for inj in inj_list if inj.id == args.injection_workflow_id]
                if not inj_list:
                    logger.error(f"Injection workflow id '{args.injection_workflow_id}' not found in injection workflows file")
                    raise SystemExit(f"Injection workflow id '{args.injection_workflow_id}' not found")
                logger.info(f"Filtered to injection workflow id '{args.injection_workflow_id}'")

        # Process each requested workflow (and each selected injection)
        for idx, wf_id in enumerate(workflow_ids):

            workflow = workflow_map.get(wf_id)
            if not workflow:
                logger.error(f"Workflow id '{wf_id}' not found in workflows file")
                continue

            logger.info(f"Running {idx}th workflow '{workflow.name or workflow.id}' with {len(workflow.servers)} servers")
            logger.debug(f"Task: {workflow.task.request[:100]}..." if len(workflow.task.request) > 100 else f"Task: {workflow.task.request}")
            
            selected_injections: list[Workflow] = []
            if args.attack_mode:
                k = int(args.injection_k)
                total_available = len(inj_list)
                already_done = completed_injections_by_workflow.get(workflow.id, set())
                target_k = 0

                if total_available > 0:
                    target_k = total_available if k == -1 else min(max(1, k), total_available)
                    remaining_needed = max(target_k - len(already_done), 0)
                    remaining_candidates = [inj for inj in inj_list if inj.id not in already_done]

                    if remaining_needed <= 0:
                        logger.info(
                            f"Skipping workflow '{workflow.id}' - resume data shows injection-k target ({target_k}) already met"
                        )
                        continue

                    if remaining_candidates and remaining_needed > len(remaining_candidates):
                        remaining_needed = len(remaining_candidates)

                    if remaining_needed == 0 or not remaining_candidates:
                        logger.info(
                            f"Skipping workflow '{workflow.id}' - no remaining injections to run after applying resume filter"
                        )
                        continue

                    selected_injections = random.sample(remaining_candidates, remaining_needed)
                    logger.info(
                        f"Resume progress for '{workflow.id}': {len(already_done)}/{target_k} injections complete, running {len(selected_injections)} more"
                    )
                else:
                    selected_injections = []

            # Iterate over chosen injections (None means no injection)
            if not selected_injections:
                logger.warning("Attack mode enabled but no injection workflows available; proceeding without injection")
            for injection_workflow in (selected_injections or [None]):
                if args.attack_mode and injection_workflow:
                    logger.info(f"Attack mode: using injection workflow {injection_workflow.id}")

                try:
                    out = run_from_parts(
                        cfg.model, 
                        workflow, 
                        mode=cfg.mode,
                        container_config=cfg.get_container_config(),
                        start_new_container=args.start_new_container,
                        enable_interceptors=args.enable_interceptors,
                        attack_mode=args.attack_mode,
                        injection_workflow=injection_workflow,
                    )

                    logger.info("Execution completed successfully")
                    logger.debug(f"Result keys: {list(out.keys())}")

                    result = out.get("result", {})
                    final_content = result.get("final_content") if isinstance(result, dict) else None
                    verification = out.get("verification") if isinstance(out, dict) else None
                    if final_content:
                        print(final_content)
                    else:
                        print(out.get("result"))

                    # Derive execution state from verification label
                    label = None
                    if isinstance(verification, dict):
                        label = verification.get("label")
                    if not label:
                        label = "unknown"

                    exec_record = {
                        "execution_id": out.get("execution_id"),
                        "workflow_id": workflow.id,
                        "injection_workflow_id": injection_workflow.id if injection_workflow else None,
                        "state": label,
                        "final_response": final_content,
                        "verification": verification,
                    }
                    logger.info(f"Execution {idx}th workflow with injection {injection_workflow.id if injection_workflow else 'None'}, state: {label}")

                    counts[label] = counts.get(label, 0) + 1
                    executions.append(exec_record)

                    # Log a concise execution summary
                    logger.info(
                        f"Recorded execution {exec_record['execution_id']} | workflow={workflow.id} | injection={exec_record['injection_workflow_id']} | state={label}"
                    )

                except Exception as e:
                    tb = traceback.format_exc()
                    logger.error(
                        f"Execution failed for workflow={workflow.id} injection={injection_workflow.id if injection_workflow else None}: {e}",
                        exc_info=True,
                    )

                    exec_record = {
                        "execution_id": None,
                        "workflow_id": workflow.id,
                        "injection_workflow_id": injection_workflow.id if injection_workflow else None,
                        "state": "error",
                        "final_response": None,
                        "verification": None,
                        "exception": {"message": str(e), "traceback": tb},
                    }

                    counts["error"] = counts.get("error", 0) + 1
                    executions.append(exec_record)

                    # If single execution, print traceback and abort
                    if not batch_run:
                        print(tb)
                        # Persist report before exiting
                        try:
                            with open(report_path, "w", encoding="utf-8") as f:
                                json.dump({"summary": {"total_executions": len(executions), "counts_by_state": counts}, "executions": executions}, f, ensure_ascii=False, indent=2)
                        except Exception:
                            pass
                        raise SystemExit(1)

                # Persist report incrementally after each execution to avoid data loss (only when enabled)
                if report_enabled:
                    incremental_summary = {
                        "total_executions": len(executions),
                        "counts_by_state": counts,
                        "config_path": args.config,
                        "workflow_file": args.workflows,
                        "injection_workflow_file": args.injection_workflows if args.attack_mode else None,
                        "model": {"vendor": cfg.model.vendor, "name": cfg.model.name},
                        "judger_model": {"vendor": cfg.model.judger_vendor, "name": cfg.model.judger_name},
                        "mode": cfg.mode,
                        "random_seed": args.random_seed,
                    }

                    incremental_report = {
                        "summary": incremental_summary,
                        "executions": executions,
                    }

                    try:
                        with open(report_path, "w", encoding="utf-8") as f:
                            json.dump(incremental_report, f, ensure_ascii=False, indent=2)
                        logger.debug(f"Incremental report updated at {report_path}")
                    except Exception as e:
                        logger.warning(f"Failed to write incremental execution report to {report_path}: {e}")

        # Update summary after batch
        prev_exec_count = len((existing or {}).get("executions", []))
        new_exec_count = len(executions) - prev_exec_count
        summary.update({
            "total_executions": summary.get("total_executions", 0) + max(new_exec_count, 0),
            "counts_by_state": counts,
            "config_path": args.config,
            "workflow_file": args.workflows,
            "injection_workflow_file": args.injection_workflows if args.attack_mode else None,
            "model": {"vendor": cfg.model.vendor, "name": cfg.model.name},
            "judger_model": {"vendor": cfg.model.judger_vendor, "name": cfg.model.judger_name},
            "mode": cfg.mode,
            "random_seed": args.random_seed,
        })

        # Final overall report (only when enabled)
        if report_enabled:
            report = {
                "summary": summary,
                "executions": executions,
            }

            try:
                with open(report_path, "w", encoding="utf-8") as f:
                    json.dump(report, f, ensure_ascii=False, indent=2)
                logger.info(f"Wrote execution report to {report_path}")
            except Exception as e:
                logger.warning(f"Failed to write execution report to {report_path}: {e}")
            
    except Exception as e:
        logger.critical(f"Fatal error during execution: {e}", exc_info=True)
        sys.exit(1)
    finally:
        logger.info("=" * 60)
        logger.info("Agent Execution Sandbox finished")
        logger.info("=" * 60)


if __name__ == "__main__":
    main()
