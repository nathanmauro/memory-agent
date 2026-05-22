# memory-agent

An [MCP](https://modelcontextprotocol.io) server that gives coding agents persistent, structured memory backed by SQLite plus a local or cloud LLM. Claude Code and Codex are supported first-class clients. No vector databases, no embeddings — categorization, dedup/merge, and relevance ranking are delegated to the LLM, while storage and full-text search live in SQLite + FTS5.

## What it does

Exposes nine tools to the MCP client:

| Tool | Purpose |
|---|---|
| `memory_store` | Persist a memory; LLM picks category, tags, importance; merges with an existing memory when content overlaps. |
| `memory_query` | FTS5 search over content + tags, supplemented by a LIKE fallback, then re-ranked by the LLM. |
| `memory_index` | Same candidate gathering as `memory_query` but no LLM rerank — one compact line per hit, cheap to scan. |
| `memory_get` | Fetch full content for one or more IDs (full UUID or 8-char prefix). Use after `memory_index`. |
| `memory_list` | Pure DB listing, filterable by scope, category, and status. No LLM call. |
| `memory_timeline` | DB-only chronological view, optionally bounded by `before_iso` / `after_iso`. |
| `memory_forget` | Delete a memory by full UUID or 8-char prefix. |
| `memory_session_search` | Search archived session JSONL (cold storage) via FTS5 index. |
| `memory_consolidate` | LLM proposes merge/delete/update actions for a scope. Dry-run by default; pass `apply=True` to backup and apply. |

Memories carry a `category` (`session_summary`, `code_decision`, `user_preference`, `project_knowledge`), a free-text `scope` (e.g. a project name or `global`), an `importance` (1–5), tags, and lifecycle fields (`status`, `last_accessed_at`, `access_count`, `pinned`).

Session buffers are archived to `{MEMORY_AGENT_HOME}/archive/sessions/{scope}/` before summarization. Proposals and backups land in `proposals/` and `backups/` under the same home directory.

The database defaults to `~/.claude/memory/memory.db` (WAL mode) to preserve existing installs. Set `MEMORY_AGENT_HOME` to move client-neutral state elsewhere, e.g. `~/.memory-agent`.

## LLM backends

Backend is selected by `LLM_BACKEND`:

- `ollama` (default) — POSTs to `OLLAMA_URL` (default `http://localhost:11434`) with `OLLAMA_MODEL` (default `qwen2.5:14b`).
- `lm-studio` — POSTs to `LM_STUDIO_URL` (default `http://localhost:1234`) with `LM_STUDIO_MODEL` (default `qwen3-4b-instruct-2507-mlx`).
- `bedrock` — uses `boto3` against `BEDROCK_MODEL` (default `us.anthropic.claude-3-5-haiku-20241022-v1:0`) in `AWS_REGION` (default `us-east-1`), honoring `AWS_PROFILE` if set.

LLM failures never raise out of `llm.py`; the server falls back to safe defaults. FTS5 is treated as optional too — every FTS read/write is wrapped so the server still runs on SQLite builds without it.

### Recommended local models

Tested on an M4 Max / 128 GB Mac:

| Tag | Result on full integration suite |
|---|---|
| `qwen2.5:14b` | Solid baseline — small pull, strong JSON adherence. The code-level default. |
| `qwen3:30b-a3b` | Fast (MoE, 3B active), but weaker on structured-output tasks: tag extraction, merge proposals, and consolidation actions sometimes empty. |
| **`gemma4:26b`** | 17/17 integration tests pass. Built on Gemini 3 research, MoE with 3.8B active params. Currently the best local fit for this workload. |

## Install

```bash
git clone https://github.com/nathanmauro/memory-agent
cd memory-agent

# Python 3.10+ required (FastMCP)
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -e .
# add boto3 only if using the Bedrock backend
```

For Ollama: install [Ollama](https://ollama.com) and pull a model, e.g.:

```bash
ollama pull gemma4:26b
```

## Install for a Client

One command registers the MCP server and writes hook entries for the chosen client:

```bash
.venv/bin/python -m mcp_memory_agent.install --client claude
.venv/bin/python -m mcp_memory_agent.install --client codex
.venv/bin/python -m mcp_memory_agent.install --client both
```

The install is idempotent — re-running leaves existing hook entries alone.

Claude install:

- Registers `memory` with `claude mcp add -s user`.
- Merges hook entries into `~/.claude/settings.json` and backs it up to `settings.json.bak`.
- Enables startup context injection plus prompt/tool/session capture.

Codex install:

- Registers `memory` with `codex mcp add`.
- Merges project hook entries into `.codex/hooks.json` in the current project directory.
- Preserves existing hooks, including non-memory hooks already in that file.
- Captures `PostToolUse` events and finalizes/sweeps buffers on `Stop` when Codex provides a usable session id.

After install, restart the target client; the eight tools appear under `mcp__memory__*`.

If you'd rather register the MCP server by hand, point the client at the console script:

```bash
claude mcp add -s user memory /absolute/path/to/.venv/bin/mcp-memory-agent-server
codex mcp add memory -- /absolute/path/to/.venv/bin/mcp-memory-agent-server
```

For Codex with LM Studio:

```bash
codex mcp add memory \
  --env LLM_BACKEND=lm-studio \
  --env LM_STUDIO_URL=http://localhost:1234 \
  --env LM_STUDIO_MODEL=qwen3-4b-instruct-2507-mlx \
  -- /absolute/path/to/.venv/bin/mcp-memory-agent-server
```

## Auto-capture

The hook handler is client-neutral. Client adapters translate each host's hook configuration into the same internal buffer format:

| Client | Events wired | What it does |
|---|---|---|
| Claude Code | `SessionStart`, `UserPromptSubmit`, `PostToolUse`, `SessionEnd` | Injects startup context, appends prompts/tools to a per-session JSONL buffer, then summarizes on session end. |
| Codex | `PostToolUse`, `Stop` | Appends tool events to the same buffer format, then finalizes/sweeps on stop when the payload includes a stable session id. |

Scope is derived from the session's `cwd`: basename of the nearest `.git` toplevel, else basename of `cwd`, else `global`. Every hook is wrapped to exit 0 on failure — a memory-agent fault cannot break the client session.

## Run directly (debugging)

```bash
.venv/bin/python -m mcp_memory_agent
```

## Tests

```bash
# Pure-local validation, mocks the LLM, no network needed
.venv/bin/python tests/test_validation.py

# Live integration tests; defaults to Ollama, set LLM_BACKEND to switch
LLM_BACKEND=ollama OLLAMA_MODEL=gemma4:26b .venv/bin/python tests/test_integration.py
LLM_BACKEND=lm-studio .venv/bin/python tests/test_integration.py
```

## Architecture

Small and procedural; `models/` owns validation and `integrations/` owns client setup.

| File / Dir            | Role                                                          |
|-----------------------|---------------------------------------------------------------|
| `server.py`           | Entry point: `db.init_db()` then `mcp.run()`.                 |
| `tools.py`            | `FastMCP` app and the eight `@mcp.tool()` handlers.           |
| `db.py`               | SQLite path/init, `MEMORY_AGENT_HOME`, FTS upsert/delete, query-term extraction, session-buffer iteration. |
| `llm.py`              | LLM dispatch (Ollama / LM Studio / Bedrock), JSON extraction, ranking. |
| `hook_handler.py`     | CLI entry for session-capture hooks: `inject-context`, `record`, `summarize-session`, `finalize-session`, `sweep`. |
| `integrations/`       | Claude and Codex MCP/hook installers. |
| `install.py`          | Thin client selector for `--client claude`, `--client codex`, or `--client both`. |
| `models/`             | Pydantic models and reusable `Annotated` validators.          |
| `test_integration.py` | Live-LLM tests, isolated to `memory_test.db` + `__test__` scope. |
| `test_validation.py`  | Local tests; mocks `llm.llm_call`, uses a tempdir DB + sessions dir. |

See `CLAUDE.md` for deeper architecture notes and `AGENTS.md` for code-style conventions.

## License

MIT — see `LICENSE` if present, otherwise treat as MIT.

## Status

Personal project. Used daily as the persistent-memory layer for local coding agents on the author's machine. No CI, no release process, no support guarantees.
