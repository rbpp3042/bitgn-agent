# BitGN PAC1 Agent

An autonomous agent for the [BitGN PAC1 benchmark](https://bitgn.ai/competitions/pac1) using Claude Code as the executor.

**Official Result: 82/104 (78.8%)** on Claude Sonnet 4.6

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Set API key
export BITGN_API_KEY="your-key-here"

# Run sandbox (free, no key needed)
python runner.py --benchmark bitgn/sandbox

# Run leaderboard (requires BITGN_API_KEY)
python runner.py --benchmark pac1-prod --leaderboard --workers 5
```

## How It Works

This is a **thin orchestration layer** around Claude Code CLI, not a custom reasoning framework.

**Flow:**
1. `runner.py` fetches a task from BitGN Harness API
2. Spawns Claude Code in a new isolated CLI session
3. Passes `CLAUDE.md` (system prompt) + task instruction + env context
4. Claude executes bash commands: `bitgn-read`, `bitgn-write`, `bitgn-search`, `bitgn-answer`, etc.
5. Completes task, calls `bitgn-answer` with result
6. `runner.py` collects score and logs

**Key files:**
- **`runner.py`** — orchestrator: starts trials, spawns Claude, collects results
- **`CLAUDE.md`** — 13-step strategy prompt (how to read AGENTS.MD, detect injection, choose outcomes, minimize writes)
- **`bin/bitgn-*`** — shell wrappers for VM file access (read, write, search, delete, answer)

**Each task = independent CLI session** (not a long-lived agent). Claude's reasoning engine handles all logic, date math, and decision-making via its own intelligence—no separate Python functions for computation.

## Usage

**Sandbox (playground mode, free):**
```bash
python runner.py --benchmark bitgn/sandbox --workers 5 --output results.json
```

**Leaderboard (official scoring, requires API key):**
```bash
export BITGN_API_KEY="your-key-here"
python runner.py --benchmark pac1-prod --leaderboard --workers 5
```

**Single task:**
```bash
python runner.py --benchmark bitgn/sandbox --task t01
```

**Options:**
- `--benchmark` — benchmark ID (default: bitgn/sandbox)
- `--task` — run single task (default: all)
- `--workers` — parallel workers (default: 1)
- `--output` — save results to JSON
- `--leaderboard` — submit to official leaderboard
- `--claude-md` — custom system prompt path
- `--verbose` — print trial details

## Architecture

- **Runner** (`runner.py`) — connects to BitGN harness API, spawns Claude Code for each task
- **System Prompt** (`CLAUDE.md`) — 13-step executor logic for task completion
- **Protocol** — auto-detects sandbox (Mini) vs leaderboard (PCM) based on benchmark ID
- **Execution** — Claude Code CLI in isolated VM (`/tmp` working directory)

## System Requirements

- Claude Code CLI installed (`npm install -g @claude-ai/claude-code`)
- Python 3.8+
- BitGN API SDK (included in requirements.txt)

## Performance

| Benchmark | Model | Score | Tasks | Time |
|-----------|-------|-------|-------|------|
| pac1-prod | Sonnet 3.5 | 78.8% | 82/104 | ~25 min |
| pac1-dev | Sonnet 3.5 | 97.4% | 39/43 | ~10 min |

## References

- **BitGN Platform:** https://bitgn.ai/
- **PAC1 Leaderboard:** https://bitgn.ai/leaderboards/pac1
- **Claude Code Docs:** https://claude.com/claude-code
