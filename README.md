# memory-agent

An [MCP](https://modelcontextprotocol.io) server that gives Claude Code (or any MCP client) persistent, structured memory backed by SQLite plus a local or cloud LLM. No vector databases, no embeddings — categorization, dedup/merge, and relevance ranking are delegated to the LLM, while storage and full-text search live in SQLite + FTS5.

## What it does

Exposes five tools to the MCP client:

| Tool | Purpose |
|---|---|
| `memory_store` | Persist a memory; LLM picks category, tags, importance; merges with an existing memory when content overlaps. |
| `memory_query` | FTS5 search over content + tags, supplemented by a LIKE fallback, then re-ranked by the LLM. |
| `memory_list` | Pure DB listing, filterable by scope + category. No LLM call. |
| `memory_forget` | Delete a memory by full UUID or 8-char prefix. |
| `memory_consolidate` | Ask the LLM to propose merge/delete/update actions over a scope's memories; applied best-effort. |

Memories carry a `category` (`session_summary`, `code_decision`, `user_preference`, `project_knowledge`), a free-text `scope` (e.g. a project name or `global`), an `importance` (1–5), and tags.

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

## Register with Claude Code

```bash
claude mcp add -s user memory /absolute/path/to/.venv/bin/python -- /absolute/path/to/server.py
```

To inject env vars (e.g. selecting a different Ollama model), the easiest path is a small wrapper script and pointing the MCP entry at that:

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export OLLAMA_MODEL="${OLLAMA_MODEL:-gemma4:26b}"
export LLM_BACKEND="${LLM_BACKEND:-ollama}"
exec ./.venv/bin/python server.py
```

```bash
claude mcp add -s user memory /absolute/path/to/run_server.sh
```

Restart Claude Code; the five tools above appear under `mcp__memory__*`.

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
| `tools.py`            | `FastMCP` app and the five `@mcp.tool()` handlers.            |
| `db.py`               | SQLite path/init, FTS upsert/delete, query-term extraction.   |
| `llm.py`              | LLM dispatch (Ollama / Bedrock), JSON extraction, ranking.    |
| `models/`             | Pydantic models and reusable `Annotated` validators.          |
| `test_integration.py` | Live-LLM tests, isolated to `memory_test.db` + `__test__` scope. |
| `test_validation.py`  | Local tests; mocks `llm.llm_call`, uses a tempdir DB.         |

See `CLAUDE.md` for deeper architecture notes and `AGENTS.md` for code-style conventions.

## License

MIT — see `LICENSE` if present, otherwise treat as MIT.

## Status

Personal project. Used daily as the persistent-memory layer for Claude Code on the author's machine. No CI, no release process, no support guarantees.
