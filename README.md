# memory-agent

An [MCP](https://modelcontextprotocol.io) server that gives Claude Code (or any MCP client) persistent, structured memory backed by SQLite plus a local or cloud LLM. No vector databases, no embeddings — categorization, dedup/merge, and relevance ranking are delegated to the LLM, while storage and full-text search live in SQLite + FTS5.

## What it does

Exposes fourteen tools to the MCP client:

| Tool | Purpose |
|---|---|
| `memory_store` | Persist a memory; LLM picks category, tags, importance; merges with an existing memory when content overlaps. |
| `memory_query` | FTS5 search over content + tags, supplemented by a LIKE fallback, then re-ranked by the LLM. Defaults to active memories. |
| `memory_index` | Same candidate gathering as `memory_query` but no LLM rerank — one compact line per hit, cheap to scan. Defaults to active memories. |
| `memory_get` | Fetch full content for one or more IDs (full UUID or 8-char prefix). Use after `memory_index`. |
| `memory_pin` | Mark a memory as startup-safe so `inject-context` can include it. |
| `memory_unpin` | Remove startup-injection privilege without deleting the memory. |
| `memory_list` | Pure DB listing, filterable by scope, category, and status. No LLM call. Defaults to active memories. |
| `memory_timeline` | DB-only chronological view, optionally bounded by `before_iso` / `after_iso`. Defaults to active memories. |
| `memory_forget` | Delete a memory by full UUID or 8-char prefix. |
| `memory_session_search` | Search archived session transcripts in cold storage. |
| `memory_session_get` | Fetch the full archived transcript for one session. |
| `memory_hot_read` | Read the bounded per-scope hot memory file. |
| `memory_hot_edit` | Add, replace, or remove text in hot memory with size and duplicate guards. |
| `memory_consolidate` | Ask the LLM to propose merge/delete/update actions over a scope's memories; dry-run by default. |

Memories carry a `category` (`session_summary`, `code_decision`, `user_preference`, `project_knowledge`, `open_action`), a free-text `scope` (e.g. a project name or `global`), lifecycle `status`, `pinned` startup-injection privilege, an `importance` (1–5), and tags.

The database defaults to `~/.claude/memory/memory.db` (WAL mode). Set `MEMORY_AGENT_HOME` to move client-neutral state elsewhere.

## LLM backends

Backend is selected by `LLM_BACKEND`:

- `ollama` (default) — POSTs to `OLLAMA_URL` (default `http://localhost:11434`) with `OLLAMA_MODEL` (default `qwen2.5:14b`).
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
uv pip install --python .venv/bin/python "mcp[cli]" pydantic
# add boto3 only if using the Bedrock backend
```

For Ollama: install [Ollama](https://ollama.com) and pull a model, e.g.:

```bash
ollama pull gemma4:26b
```

## Install

One command registers the MCP server and writes hook entries:

```bash
# Claude Code (default)
.venv/bin/python -m mcp_memory_agent.install --client claude

# Codex (project-local .codex/hooks.json + user MCP registration)
.venv/bin/python -m mcp_memory_agent.install --client codex

# Both
.venv/bin/python -m mcp_memory_agent.install --client both
```

For Claude Code, hooks land in `~/.claude/settings.json` (backed up to `settings.json.bak`). For Codex, enable hooks in `~/.codex/config.toml` with `[features] codex_hooks = true`. `SessionStart` `inject-context` is intentionally strict: it injects bounded hot memory plus explicitly pinned active warm memories only. Broad warm memories and cold session pointers stay pull-based through query/search tools.

If you'd rather register the MCP server by hand, point the client at the installed console script:

```bash
claude mcp add -s user memory /absolute/path/to/.venv/bin/mcp-memory-agent-server
codex mcp add memory -- /absolute/path/to/.venv/bin/mcp-memory-agent-server
```

## Auto-capture

`install.py` writes client hook entries that call `mcp-memory-agent-hook`:

| Hook event | What it does |
|---|---|
| `SessionStart` | Injects bounded hot memory and explicitly pinned active warm memories. Broad warm memories and cold session pointers are retrieved on demand. |
| `UserPromptSubmit` | Claude only: appends the user prompt to a per-session JSONL buffer. |
| `PostToolUse` | Appends tool details to the same buffer. |
| `SessionEnd` / `Stop` | Archives any non-empty buffer, summarizes sessions with at least 3 records into warm memories, and stores extracted `open_action` rows. |

Scope is derived from the session's `cwd`: basename of the nearest `.git` toplevel, else basename of `cwd`, else `global`. Every hook is wrapped to exit 0 on failure — a memory-agent fault cannot break a Claude Code session.

## Run directly (debugging)

```bash
.venv/bin/python -m mcp_memory_agent
```

## Tests

```bash
# Pure-local validation, mocks the LLM, no network needed
.venv/bin/python tests/test_validation.py

# Live integration tests; defaults to Ollama, set LLM_BACKEND=bedrock to switch
LLM_BACKEND=ollama OLLAMA_MODEL=gemma4:26b .venv/bin/python tests/test_integration.py
```

## Architecture

Small and procedural; `models/` is the only subpackage.

| File / Dir            | Role                                                          |
|-----------------------|---------------------------------------------------------------|
| `src/mcp_memory_agent/server.py` | Entry point: `db.init_db()` then `mcp.run()`.       |
| `src/mcp_memory_agent/tools.py` | `FastMCP` app and the fourteen `@mcp.tool()` handlers. |
| `src/mcp_memory_agent/db.py` | SQLite path/init, FTS upsert/delete, query-term extraction, session-buffer and archive helpers. |
| `src/mcp_memory_agent/llm.py` | LLM dispatch (Ollama / LM Studio / Bedrock), JSON extraction, ranking. |
| `src/mcp_memory_agent/hook_handler.py` | CLI entry for hooks: `inject-context`, `record`, `summarize-session`, `finalize-session`, `sweep`, `curator`. |
| `src/mcp_memory_agent/integrations/` | Claude and Codex installer adapters. |
| `src/mcp_memory_agent/install.py` | One-shot client installer for Claude, Codex, or both. |
| `src/mcp_memory_agent/models/` | Pydantic models and reusable `Annotated` validators. |
| `tests/test_integration.py` | Live-LLM tests, isolated to `memory_test.db` + `__test__` scope. |
| `tests/test_validation.py` | Local tests; mocks `llm.llm_call`, uses a tempdir DB + sessions/archive dirs. |

See `CLAUDE.md` for deeper architecture notes and `AGENTS.md` for code-style conventions.

## License

MIT — see `LICENSE` if present, otherwise treat as MIT.

## Status

Personal project. Used daily as the persistent-memory layer for Claude Code on the author's machine. No CI, no release process, no support guarantees.
