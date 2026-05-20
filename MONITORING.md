# Memory-agent resource usage — findings & monitor

Snapshot taken 2026-05-20 ~13:40 local. Sampling continues via `scripts/monitor_usage.py`.

## TL;DR

The memory-agent Python code is essentially free (≈40 MB resident across all hooks + MCP server).
**The cost is the LLM backend choice.** `~/.mcp.json` and the Claude Code hook entries route
every `memory_store`, `memory_query`, `memory_consolidate`, and SessionEnd `summarize-session`
call to LM Studio running **`qwen3-next-80b-a3b-thinking-mlx`**. That model:

- is 80 B parameters, ~45 GB resident when warm
- is a **thinking variant** — every reply burns thousands of internal chain-of-thought
  ("reasoning") tokens that the agent never uses
- runs MLX on the Mac GPU, so each call ties up the GPU for seconds-to-minutes

Switching to a 7–14 B non-thinking model for these structured-JSON tasks should cut load
by roughly an order of magnitude.

## Process inventory (at sample time)

| Process kind         | Count | RSS    | CPU  | Notes                              |
|----------------------|-------|--------|------|------------------------------------|
| `llmworker.js` (LMS) | 1     | 3.4 GB | 0.0% | IDLE — balloons to ~45 GB on call  |
| `mcp-memory-agent-server` | 5 | 142 MB | 0.0% | One per active Claude Code session |
| Memory SQLite DB     | —     | 48 KB  | —    | `~/.claude/memory/memory.db`       |
| Session buffers      | 4     | 79 KB  | —    | `~/.claude/memory/sessions/*.jsonl`|

Code-side: nothing to worry about. Steady state is dormant Python processes waiting on stdio.

## Call volume today (from LM Studio log)

Parsed from `~/.lmstudio/server-logs/2026-05/2026-05-20.1.log`:

- 11:00 hour → 80 generations
- 13:00 hour → 262 generations
- First sample's cumulative: **41 calls, 89,248 total tokens, 54,749 reasoning tokens**
- That's ~61 % of generated tokens spent on internal "thinking" that's then discarded

A single observed call to categorize one trivial memory ("standalone script at
`~/bin/claude-sessions` is the target for redesign") produced **2,223 reasoning tokens**
of meandering self-debate before emitting a 6-field JSON object. The reasoning content
shows the model genuinely uncertain, going in circles — typical failure mode when a
heavy reasoning model is asked to do simple classification.

## Why this fires often

Every one of these triggers an LLM call:

- `memory_store` — categorise + dedup/merge (`llm.extract_memory_metadata`)
- `memory_query` — re-rank candidates after FTS (`llm.rank_memories`)
- `memory_consolidate` — batch merge/delete plan
- SessionEnd hook → `summarize-session` → `_insert_memory` per extracted memory

SessionStart's `inject-context`, `record` (UserPromptSubmit/PostToolUse), and the
pure-DB tools (`memory_list`, `memory_timeline`, `memory_get`, `memory_index`,
`memory_forget`) do **not** call the LLM. So `record` writing tiny JSONL append
lines on every prompt/tool-use is not the cost driver.

## Recommendations, ordered by impact ÷ effort

1. **Swap the LM Studio model.** For categorisation + ranking + short summary,
   try `qwen2.5-7b-instruct` (non-thinking) or `qwen3-4b-instruct`. Either is
   ~1/10th the RAM and probably 5–20× faster end-to-end.
   - Edit `run_server.sh`: `LM_STUDIO_MODEL=<new-model-id>`
   - Reload model in LM Studio (or `lms load <id>`)
   - Restart Claude Code so the env var propagates to the MCP server
   - Hook entries in `~/.claude/settings.json` also pin `LM_STUDIO_MODEL=...`
     and need updating in lockstep — or remove the env var from those entries
     so they fall back to whatever's loaded.

2. **Set a TTL on the loaded model** in LM Studio so it unloads after idle
   (`lms server` GUI → model → TTL). Right now `qwen3-next-80b...` shows TTL
   empty, so it stays resident until the server stops.

3. **Cap session-end summarisation.** `_insert_memory` is called once per
   memory the summariser extracts. A long session can produce many extracted
   memories, each triggering its own categorise/merge LLM round-trip. Worth
   capping at e.g. 3 memories per session in `summarize-session`, or batching
   them into one LLM call.

4. **Make hook env honest.** The hook entries hardcode
   `LLM_BACKEND=lm-studio LM_STUDIO_MODEL=qwen3-next-80b-a3b-thinking-mlx`.
   Either (a) drop the pin so it follows the GUI, or (b) keep the pin but
   update it in lockstep with `run_server.sh`. Otherwise a model swap in the
   server doesn't apply to hooks.

5. **(Optional) Add a "skip if model busy" guard.** `summarize-session` could
   peek at `lms ps` and skip if any model is already actively generating, to
   avoid stacking heavy calls during interactive work.

## The monitor

Located at `scripts/monitor_usage.py`. Writes one JSONL line per sample to
`~/.claude/memory/monitor.log`. Tracks:

- per-process CPU and RSS for LM Studio worker, MCP server, hook handlers
- loaded model identifier + status + size (`lms ps`)
- LM Studio log deltas since last sample: call count, prompt/completion/reasoning tokens
- session-buffer file count and size

It's stdlib-only and tracks its read offset in `~/.claude/memory/.monitor_state.json`
so each sample reports new activity (not cumulative).

### Run modes

```bash
# Single sample to stdout (also appends to monitor.log)
.venv/bin/python scripts/monitor_usage.py --once

# Continuous, sampling every 60 s
.venv/bin/python scripts/monitor_usage.py --interval 60

# Tail what the monitor is writing
tail -f ~/.claude/memory/monitor.log | jq -c '{ts, calls:.lms_delta.calls, rs_tok:.lms_delta.reasoning_tokens, rss:.procs.lm_studio_worker.rss_gb}'
```

A monitor is currently running in this Claude Code session (background task `be8kdm1cs`,
60 s interval). It dies when the session ends. To make it persistent across reboots,
add a LaunchAgent (mirroring `~/Library/LaunchAgents/com.nathan.lms-server.plist`) or
a `launchd` entry pointing at the script — ask if you want one wired up.

### Known caveats in the data

- `reasoning_secs` in samples is parsed from LM Studio's `Done reasoning. Reasoned
  for X seconds` log line, which appears to be in microseconds or another non-second
  unit (one observed value of 517 884.58 "seconds" = 6 days, clearly wrong). Use it
  as a relative number across samples, not an absolute wall-clock.
- First sample after startup covers the whole day's log (because offset starts at 0).
  Subsequent samples are true deltas.
