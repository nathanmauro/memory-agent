#!/usr/bin/env python3
"""Sample resource usage of the memory-agent stack (Python MCP servers + LM Studio worker).

Writes one JSONL line per sample to ~/.claude/memory/monitor.log so you can
`tail -f` it or pipe through `jq`. Single-file, stdlib only.

Run:
    python scripts/monitor_usage.py [--interval 60] [--once]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

LOG_DIR = Path.home() / ".claude" / "memory"
MONITOR_LOG = LOG_DIR / "monitor.log"
STATE_FILE = LOG_DIR / ".monitor_state.json"
SESSIONS_DIR = LOG_DIR / "sessions"
LMS_LOG_DIR = Path.home() / ".lmstudio" / "server-logs"

USAGE_BLOCK_RE = re.compile(
    r'"prompt_tokens":\s*(\d+).*?'
    r'"completion_tokens":\s*(\d+).*?'
    r'"total_tokens":\s*(\d+)'
    r'(?:.*?"reasoning_tokens":\s*(\d+))?',
    re.DOTALL,
)
PREDICTION_RE = re.compile(r"Generated prediction:")
DONE_REASONING_RE = re.compile(r"Done reasoning\. Reasoned for ([0-9.]+) seconds")


def current_lms_log() -> Path | None:
    """Return today's LM Studio server log path, or None if not present."""
    today = datetime.now().strftime("%Y-%m")
    day = datetime.now().strftime("%Y-%m-%d")
    month_dir = LMS_LOG_DIR / today
    if not month_dir.is_dir():
        return None
    matches = sorted(month_dir.glob(f"{day}.*.log"))
    return matches[-1] if matches else None


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state))


def ps_workers() -> list[dict]:
    """Return RSS/CPU info for LM Studio LLM worker(s) and memory-agent Python procs."""
    out = subprocess.run(
        ["ps", "-axo", "pid=,pcpu=,rss=,command="],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    workers = []
    for line in out.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        pid, pcpu, rss_kb, cmd = parts
        if "llmworker.js" in cmd:
            kind = "lm_studio_worker"
        elif "mcp-memory-agent-server" in cmd:
            kind = "memory_agent_server"
        elif "hook_handler" in cmd:
            kind = "memory_agent_hook"
        else:
            continue
        try:
            workers.append({
                "kind": kind,
                "pid": int(pid),
                "cpu_pct": float(pcpu),
                "rss_gb": round(int(rss_kb) / 1024 / 1024, 3),
            })
        except ValueError:
            continue
    return workers


def lms_model_state() -> dict:
    """Snapshot loaded models via `lms ps`. Returns identifier + status + size."""
    try:
        out = subprocess.run(
            [str(Path.home() / ".lmstudio" / "bin" / "lms"), "ps"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
    except Exception:
        return {"available": False}
    rows = []
    for line in out.splitlines():
        if not line.strip() or line.lstrip().startswith("IDENTIFIER"):
            continue
        cols = line.split()
        if len(cols) < 4:
            continue
        rows.append({
            "identifier": cols[0],
            "status": cols[2] if len(cols) > 2 else "?",
            "size": " ".join(cols[3:5]) if len(cols) > 4 else "?",
        })
    return {"available": True, "models": rows}


def scan_lms_log(state: dict) -> dict:
    """Read new bytes from today's LM Studio log; return call/token deltas."""
    log_path = current_lms_log()
    if log_path is None:
        return {"available": False}

    last_path = state.get("lms_log_path")
    last_offset = state.get("lms_log_offset", 0)
    if str(log_path) != last_path:
        last_offset = 0

    size = log_path.stat().st_size
    if last_offset > size:
        last_offset = 0

    with open(log_path, "rb") as fh:
        fh.seek(last_offset)
        chunk = fh.read(size - last_offset).decode("utf-8", errors="replace")

    state["lms_log_path"] = str(log_path)
    state["lms_log_offset"] = size

    calls = len(PREDICTION_RE.findall(chunk))
    reasoning_secs = sum(float(m.group(1)) for m in DONE_REASONING_RE.finditer(chunk))

    prompt_tokens = completion_tokens = reasoning_tokens = total_tokens = 0
    for m in USAGE_BLOCK_RE.finditer(chunk):
        prompt_tokens += int(m.group(1))
        completion_tokens += int(m.group(2))
        total_tokens += int(m.group(3))
        if m.group(4):
            reasoning_tokens += int(m.group(4))

    return {
        "available": True,
        "calls": calls,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": total_tokens,
        "reasoning_secs": round(reasoning_secs, 2),
    }


def session_buffer_stats() -> dict:
    if not SESSIONS_DIR.is_dir():
        return {"count": 0, "bytes": 0}
    files = list(SESSIONS_DIR.glob("*.jsonl"))
    return {
        "count": len(files),
        "bytes": sum(f.stat().st_size for f in files),
    }


def sample(state: dict) -> dict:
    workers = ps_workers()
    by_kind: dict[str, dict] = {}
    for w in workers:
        bucket = by_kind.setdefault(w["kind"], {"count": 0, "cpu_pct": 0.0, "rss_gb": 0.0})
        bucket["count"] += 1
        bucket["cpu_pct"] += w["cpu_pct"]
        bucket["rss_gb"] += w["rss_gb"]
    for v in by_kind.values():
        v["cpu_pct"] = round(v["cpu_pct"], 2)
        v["rss_gb"] = round(v["rss_gb"], 3)

    return {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "procs": by_kind,
        "model": lms_model_state(),
        "lms_delta": scan_lms_log(state),
        "sessions": session_buffer_stats(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=60, help="Seconds between samples")
    parser.add_argument("--once", action="store_true", help="Take one sample and exit")
    args = parser.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()

    while True:
        try:
            row = sample(state)
        except Exception as e:
            row = {"ts": datetime.now().isoformat(timespec="seconds"), "error": str(e)}
        with open(MONITOR_LOG, "a") as fh:
            fh.write(json.dumps(row) + "\n")
        save_state(state)
        if args.once:
            print(json.dumps(row, indent=2))
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
