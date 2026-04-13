#!/usr/bin/env python3
"""
BitGN Runner — executes benchmark tasks using Claude Code as the executor.

Architecture:
- Connects to BitGN harness API (api.bitgn.com) via connectrpc SDK
- For each task: start_playground() → run claude CLI → end_trial() → score
- Claude executor uses bin/ wrappers to interact with the VM (HARNESS_URL)
- System prompt for executor is loaded from CLAUDE.md

Usage:
  python runner.py --benchmark sandbox                    # run all sandbox tasks
  python runner.py --benchmark sandbox --task t01        # single task
  python runner.py --benchmark pac1-dev                  # run all pac1-dev tasks
  python runner.py --benchmark sandbox --output results.json
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from threading import Lock

# Add .venv to path for SDK imports
SCRIPT_DIR = Path(__file__).parent
_venv_lib = SCRIPT_DIR / ".venv" / "lib"
if _venv_lib.exists():
    for _py_dir in sorted(_venv_lib.iterdir()):
        if _py_dir.name.startswith("python"):
            _site = _py_dir / "site-packages"
            if _site.exists():
                sys.path.insert(0, str(_site))
                break

from bitgn.harness_connect import HarnessServiceClientSync
from bitgn import harness_pb2

CLAUDE_CLI = os.environ.get("CLAUDE_CLI", "claude")
BITGN_API = os.environ.get("BITGN_API", "https://api.bitgn.com")
BITGN_API_KEY = os.environ.get("BITGN_API_KEY", "")
BITGN_RUN_NAME = os.environ.get("BITGN_RUN_NAME", "pac1-py-run")
TASK_TIMEOUT = int(os.environ.get("TASK_TIMEOUT", "600"))  # seconds per task


def load_claude_md(path: Path | None = None) -> str:
    if path is None:
        path = SCRIPT_DIR / "CLAUDE.md"
    return path.read_text(encoding="utf-8")


def execute_trial(
    trial_id: str,
    task_id: str,
    instruction: str,
    harness_url: str,
    benchmark_id: str,
    claude_md: str,
    verbose: bool = False,
    print_lock: Lock | None = None,
) -> dict:
    """Execute a single trial (run executor + score). Core shared by playground and leaderboard modes."""
    harness_client = HarnessServiceClientSync(BITGN_API)

    def log(msg: str):
        if print_lock:
            with print_lock:
                print(msg, flush=True)
        else:
            print(msg, flush=True)

    if verbose:
        log(f"  trial_id: {trial_id}")
        log(f"  instruction: {instruction[:80]}...")

    # Build environment for executor
    bin_dir = str(SCRIPT_DIR / "bin")
    # Select VM runtime protocol: sandbox uses Mini, pac1-dev uses PCM
    runtime = "mini" if "sandbox" in benchmark_id else "pcm"
    env = {
        **os.environ,
        "HARNESS_URL": harness_url,
        "BITGN_RUNTIME": runtime,
        "PATH": bin_dir + ":" + os.environ.get("PATH", ""),
    }

    # Wrap instruction to force tool usage
    user_message = f"""{instruction}

---
You MUST complete this task using bash tools. Do NOT answer from your knowledge.
Required steps:
1. Run bash: bitgn-read /AGENTS.MD  (read workspace instructions)
2. Follow AGENTS.MD exactly
3. Run bash: bitgn-answer "<answer>" <refs...>  (required to score)"""

    start_ts = time.time()
    try:
        proc = subprocess.run(
            [
                CLAUDE_CLI,
                "--dangerously-skip-permissions",
                "--max-turns", "20",
                "--system-prompt",
                claude_md,
                user_message,
            ],
            env=env,
            cwd="/tmp",  # isolate executor from local project files
            capture_output=True,
            text=True,
            timeout=TASK_TIMEOUT,
        )
        executor_output = proc.stdout
        executor_stderr = proc.stderr[-2000:] if proc.stderr else ""
        executor_exit = proc.returncode
    except subprocess.TimeoutExpired:
        executor_output = "[TIMEOUT]"
        executor_stderr = f"Task timed out after {TASK_TIMEOUT}s"
        executor_exit = -1
    except Exception as e:
        executor_output = f"[ERROR: {e}]"
        executor_stderr = str(e)
        executor_exit = -1

    elapsed = time.time() - start_ts

    # Fetch runtime log from VM harness
    runtime_log = ""
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(harness_url, timeout=10, context=ctx) as resp:
            runtime_log = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        runtime_log = f"[log fetch failed: {e}]"

    # Get score from harness
    try:
        result = harness_client.end_trial(
            harness_pb2.EndTrialRequest(trial_id=trial_id)
        )
        score = result.score
        score_detail = list(result.score_detail)
        state = result.state
    except Exception as e:
        score = -1.0
        score_detail = [f"end_trial error: {e}"]
        state = -1

    return {
        "task_id": task_id,
        "trial_id": trial_id,
        "instruction": instruction,
        "score": score,
        "score_detail": score_detail,
        "state": state,
        "elapsed_s": round(elapsed, 1),
        "executor_output": executor_output,
        "executor_stderr": executor_stderr,
        "executor_exit": executor_exit,
        "runtime_log": runtime_log,
    }


def run_task(
    benchmark_id: str,
    task_id: str,
    claude_md: str,
    verbose: bool = False,
) -> dict:
    """Run a single benchmark task via playground (no API key needed). Returns dict with score and metadata."""
    harness_client = HarnessServiceClientSync(BITGN_API)

    trial = harness_client.start_playground(
        harness_pb2.StartPlaygroundRequest(
            benchmark_id=benchmark_id,
            task_id=task_id,
        )
    )

    return execute_trial(
        trial_id=trial.trial_id,
        task_id=task_id,
        instruction=trial.instruction,
        harness_url=trial.harness_url,
        benchmark_id=benchmark_id,
        claude_md=claude_md,
        verbose=verbose,
    )


def _print_task_result(task_id: str, r: dict, verbose: bool, lock: Lock | None = None):
    """Print result for a single task (thread-safe if lock provided)."""
    score = r.get("score", -1.0)
    elapsed = r.get("elapsed_s", 0)
    status = "✓" if score == 1.0 else ("~" if 0 < score < 1.0 else "✗")
    lines = [f"\n[{task_id}] {status} score={score:.2f} ({elapsed}s)"]
    if verbose and r.get("score_detail"):
        lines.append(f"  detail: {r['score_detail']}")
    msg = "\n".join(lines)
    if lock:
        with lock:
            print(msg, flush=True)
    else:
        print(msg, flush=True)


def run_benchmark(
    benchmark_id: str,
    task_ids: list[str] | None = None,
    claude_md: str | None = None,
    output_path: Path | None = None,
    verbose: bool = True,
    workers: int = 1,
) -> dict:
    """Run all (or specified) tasks in a benchmark (playground mode). Returns results dict."""
    if claude_md is None:
        claude_md = load_claude_md()

    harness_client = HarnessServiceClientSync(BITGN_API)

    if task_ids:
        run_ids = list(task_ids)
    else:
        benchmark = harness_client.get_benchmark(
            harness_pb2.GetBenchmarkRequest(benchmark_id=benchmark_id)
        )
        run_ids = [t.task_id for t in benchmark.tasks]

    print(f"\nBenchmark: {benchmark_id} — {len(run_ids)} tasks (workers={workers})")
    print(f"Started: {datetime.utcnow().isoformat()}Z")
    print("-" * 60)

    results = {}
    total_score = 0.0
    scored = 0
    print_lock = Lock() if workers > 1 else None

    if workers <= 1:
        for task_id in run_ids:
            print(f"\n[{task_id}] ", end="", flush=True)
            try:
                r = run_task(benchmark_id, task_id, claude_md, verbose=verbose)
                results[task_id] = r
                score = r["score"]
                if score >= 0:
                    total_score += score
                    scored += 1
                status = "✓" if score == 1.0 else ("~" if 0 < score < 1.0 else "✗")
                print(f"{status} score={score:.2f} ({r['elapsed_s']}s)")
                if r["score_detail"]:
                    print(f"  detail: {r['score_detail']}")
            except Exception as e:
                print(f"ERROR: {e}")
                results[task_id] = {"task_id": task_id, "error": str(e), "score": -1.0}
    else:
        futures = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for task_id in run_ids:
                fut = pool.submit(run_task, benchmark_id, task_id, claude_md, verbose)
                futures[fut] = task_id
            for fut in as_completed(futures):
                task_id = futures[fut]
                try:
                    r = fut.result()
                    results[task_id] = r
                    score = r["score"]
                    if score >= 0:
                        total_score += score
                        scored += 1
                    _print_task_result(task_id, r, verbose, print_lock)
                except Exception as e:
                    with print_lock:
                        print(f"\n[{task_id}] ERROR: {e}", flush=True)
                    results[task_id] = {"task_id": task_id, "error": str(e), "score": -1.0}

    print("\n" + "=" * 60)
    avg = total_score / scored if scored > 0 else 0.0
    print(f"Result: {scored}/{len(run_ids)} scored, avg={avg:.1%}")
    print(f"Finished: {datetime.utcnow().isoformat()}Z")

    summary = {
        "benchmark_id": benchmark_id,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "tasks_run": len(run_ids),
        "tasks_scored": scored,
        "avg_score": avg,
        "results": results,
    }

    if output_path:
        output_path.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"Results saved: {output_path}")

    return summary


def run_leaderboard_benchmark(
    benchmark_id: str,
    api_key: str,
    run_name: str,
    claude_md: str | None = None,
    output_path: Path | None = None,
    verbose: bool = True,
    workers: int = 1,
) -> dict:
    """Run benchmark in leaderboard mode (requires API key). Submits result to leaderboard."""
    if claude_md is None:
        claude_md = load_claude_md()

    harness_client = HarnessServiceClientSync(BITGN_API)

    # Start a named run — registers this run on the leaderboard
    run_resp = harness_client.start_run(
        harness_pb2.StartRunRequest(
            benchmark_id=benchmark_id,
            name=run_name,
            api_key=api_key,
        )
    )
    run_id = run_resp.run_id
    trial_ids = list(run_resp.trial_ids)

    print(f"\nLeaderboard run: {run_name} (run_id={run_id})")
    print(f"Benchmark: {benchmark_id} — {len(trial_ids)} trials (workers={workers})")
    print(f"Started: {datetime.utcnow().isoformat()}Z")
    print("-" * 60)

    results = {}
    total_score = 0.0
    scored = 0
    print_lock = Lock() if workers > 1 else None

    def run_one_trial(trial_id: str) -> dict:
        trial = harness_client.start_trial(
            harness_pb2.StartTrialRequest(trial_id=trial_id)
        )
        return execute_trial(
            trial_id=trial_id,
            task_id=trial.task_id,
            instruction=trial.instruction,
            harness_url=trial.harness_url,
            benchmark_id=benchmark_id,
            claude_md=claude_md,
            verbose=verbose,
            print_lock=print_lock,
        )

    if workers <= 1:
        for trial_id in trial_ids:
            print(f"\n[{trial_id[:12]}] ", end="", flush=True)
            try:
                r = run_one_trial(trial_id)
                results[r["task_id"]] = r
                score = r["score"]
                if score >= 0:
                    total_score += score
                    scored += 1
                status = "✓" if score == 1.0 else ("~" if 0 < score < 1.0 else "✗")
                print(f"{status} score={score:.2f} ({r['elapsed_s']}s)")
            except Exception as e:
                print(f"ERROR: {e}")
                results[trial_id] = {"trial_id": trial_id, "error": str(e), "score": -1.0}
    else:
        futures = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for trial_id in trial_ids:
                fut = pool.submit(run_one_trial, trial_id)
                futures[fut] = trial_id
            for fut in as_completed(futures):
                trial_id = futures[fut]
                try:
                    r = fut.result()
                    results[r["task_id"]] = r
                    score = r["score"]
                    if score >= 0:
                        total_score += score
                        scored += 1
                    _print_task_result(r["task_id"], r, verbose, print_lock)
                except Exception as e:
                    with print_lock:
                        print(f"\n[{trial_id[:12]}] ERROR: {e}", flush=True)
                    results[trial_id] = {"trial_id": trial_id, "error": str(e), "score": -1.0}

    # Submit to leaderboard
    try:
        harness_client.submit_run(harness_pb2.SubmitRunRequest(run_id=run_id))
        print("\nSubmitted to leaderboard.")
    except Exception as e:
        print(f"\nWARNING: submit_run failed: {e}")

    print("\n" + "=" * 60)
    avg = total_score / scored if scored > 0 else 0.0
    print(f"Result: {scored}/{len(trial_ids)} scored, avg={avg:.1%}")
    print(f"Finished: {datetime.utcnow().isoformat()}Z")

    summary = {
        "benchmark_id": benchmark_id,
        "run_id": run_id,
        "run_name": run_name,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "tasks_run": len(trial_ids),
        "tasks_scored": scored,
        "avg_score": avg,
        "results": results,
    }

    if output_path:
        output_path.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"Results saved: {output_path}")

    return summary


def main():
    parser = argparse.ArgumentParser(description="BitGN benchmark runner")
    parser.add_argument(
        "--benchmark", default="bitgn/sandbox",
        help="Benchmark ID (default: bitgn/sandbox)"
    )
    parser.add_argument(
        "--task", action="append", dest="tasks",
        help="Task ID(s) to run (default: all)"
    )
    parser.add_argument(
        "--claude-md", type=Path,
        help="Path to CLAUDE.md (default: ./CLAUDE.md)"
    )
    parser.add_argument(
        "--output", type=Path,
        help="Save results to JSON file"
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print trial details"
    )
    parser.add_argument(
        "--workers", type=int, default=1,
        help="Number of parallel workers (default: 1)"
    )
    parser.add_argument(
        "--leaderboard", action="store_true",
        help="Submit to leaderboard (requires BITGN_API_KEY env var)"
    )
    args = parser.parse_args()

    claude_md = load_claude_md(args.claude_md)

    if args.leaderboard:
        api_key = BITGN_API_KEY
        if not api_key:
            print("ERROR: BITGN_API_KEY env var is required for leaderboard mode", file=sys.stderr)
            sys.exit(1)
        run_leaderboard_benchmark(
            benchmark_id=args.benchmark,
            api_key=api_key,
            run_name=BITGN_RUN_NAME,
            claude_md=claude_md,
            output_path=args.output,
            verbose=args.verbose,
            workers=args.workers,
        )
    else:
        run_benchmark(
            benchmark_id=args.benchmark,
            task_ids=args.tasks,
            claude_md=claude_md,
            output_path=args.output,
            verbose=args.verbose,
            workers=args.workers,
        )


if __name__ == "__main__":
    main()
