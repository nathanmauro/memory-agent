# memory-agent

An [MCP](https://modelcontextprotocol.io) server that gives Claude Code (or any MCP client) persistent, structured memory backed by SQLite plus a local or cloud LLM. No vector databases, no embeddings — categorization, dedup/merge, and relevance ranking are delegated to the LLM, while storage and full-text search live in SQLite + FTS5.

## What it does

Exposes eight tools to the MCP client:

| Tool | Purpose |
|---|---|
| `memory_store` | Persist a memory; LLM picks category, tags, importance; merges with an existing memory when content overlaps. |
| `memory_query` | FTS5 search over content + tags, supplemented by a LIKE fallback, then re-ranked by the LLM. |
| `memory_index` | Same candidate gathering as `memory_query` but no LLM rerank — one compact line per hit, cheap to scan. |
| `memory_get` | Fetch full content for one or more IDs (full UUID or 8-char prefix). Use after `memory_index`. |
| `memory_list` | Pure DB listing, filterable by scope + category. No LLM call. |
| `memory_timeline` | DB-only chronological view, optionally bounded by `before_iso` / `after_iso`. |
| `memory_forget` | Delete a memory by full UUID or 8-char prefix. |
| `memory_consolidate` | Ask the LLM to propose merge/delete/update actions over a scope's memories; applied best-effort. |

Memories carry a `category` (`session_summary`, `code_decision`, `user_preference`, `project_knowledge`, `open_action`), a free-text `scope` (e.g. a project name or `global`), an `importance` (1–5), and tags.

The database lives at `~/.claude/memory/memory.db` (WAL mode).

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

For Claude Code, hooks land in `~/.claude/settings.json` (backed up to `settings.json.bak`). For Codex, enable hooks in `~/.codex/config.toml` with `[features] codex_hooks = true`. Codex `SessionStart` runs the same `inject-context` output as Claude (hot + warm memories + cold session pointers) on `startup|resume`.

If you'd rather register the MCP server by hand, the wrapper script in `run_server.sh` honors `OLLAMA_MODEL` / `OLLAMA_URL` / `LLM_BACKEND` env vars before exec'ing `server.py`:

```bash
claude mcp add -s user memory /absolute/path/to/run_server.sh
```

## Auto-capture (Claude Code hooks)

`install.py` wires four shell scripts in `hooks/` to Claude Code lifecycle events:

| Hook event         | Script                          | What it does |
|--------------------|---------------------------------|--------------|
| `SessionStart`     | `hooks/session_start.sh`        | Emits up to 8 past memories for the current scope as `additionalContext`. Also sweeps any orphaned session buffers older than 1 hour. No LLM call. |
| `UserPromptSubmit` | `hooks/user_prompt_submit.sh`   | Appends the user prompt to a per-session JSONL buffer at `~/.claude/memory/sessions/<id>.jsonl`. |
| `PostToolUse`      | `hooks/post_tool_use.sh`        | Appends the tool name + truncated input/output to the same buffer. |
| `SessionEnd`       | `hooks/session_end.sh`          | LLM-summarizes the buffer into one `session_summary`, up to three sub-memories, and up to three `open_action` items, then deletes the buffer. |

Scope is derived from the session's `cwd`: basename of the nearest `.git` toplevel, else basename of `cwd`, else `global`. Every hook is wrapped to exit 0 on failure — a memory-agent fault cannot break a Claude Code session.

## Run directly (debugging)

```bash
.venv/bin/python server.py
```

## Tests

```bash
# Pure-local validation, mocks the LLM, no network needed
.venv/bin/python test_validation.py

# Live integration tests; defaults to Ollama, set LLM_BACKEND=bedrock to switch
LLM_BACKEND=ollama OLLAMA_MODEL=gemma4:26b .venv/bin/python test_integration.py
```

## Architecture

Small and procedural; `models/` is the only package.

| File / Dir            | Role                                                          |
|-----------------------|---------------------------------------------------------------|
| `server.py`           | Entry point: `db.init_db()` then `mcp.run()`.                 |
| `tools.py`            | `FastMCP` app and the eight `@mcp.tool()` handlers.           |
| `db.py`               | SQLite path/init, FTS upsert/delete, query-term extraction, session-buffer iteration. |
| `llm.py`              | LLM dispatch (Ollama / Bedrock), JSON extraction, ranking.    |
| `hook_handler.py`     | CLI entry for Claude Code hooks: `inject-context`, `record`, `summarize-session`, `sweep`. |
| `hooks/*.sh`          | Thin shell wrappers pointed at `hook_handler.py` from `~/.claude/settings.json`. |
| `install.py`          | One-shot: registers the MCP server and merges hook entries into `~/.claude/settings.json`. |
| `models/`             | Pydantic models and reusable `Annotated` validators.          |
| `test_integration.py` | Live-LLM tests, isolated to `memory_test.db` + `__test__` scope. |
| `test_validation.py`  | Local tests; mocks `llm.llm_call`, uses a tempdir DB + sessions dir. |

See `CLAUDE.md` for deeper architecture notes and `AGENTS.md` for code-style conventions.

## License

MIT — see `LICENSE` if present, otherwise treat as MIT.

## Status

Personal project. Used daily as the persistent-memory layer for Claude Code on the author's machine. No CI, no release process, no support guarantees.
