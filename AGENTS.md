# AGENTS.md — Memory Agent

## Project Overview

MCP server providing persistent structured memory for AI coding agents.
Python 3.11+, SQLite + FTS5, Claude Haiku via Amazon Bedrock.
Single-file app (`server.py`, ~500 lines). Purely procedural — no classes.

## File Layout

| File                  | Purpose                                                      |
|-----------------------|--------------------------------------------------------------|
| `server.py`           | Entire application: config, DB, LLM helpers, 5 MCP tools    |
| `setup.py`            | One-time interactive setup (API key + MCP registration)      |
| `test_integration.py` | Integration tests (requires Bedrock access)                  |
| `docs/`               | Design specs                                                 |

## Dependencies

Only two third-party packages: `mcp` (MCP Python SDK) and `boto3` (AWS SDK).
No `requirements.txt` or `pyproject.toml`. Install with:

```sh
pip install mcp boto3
```

## Build / Run / Test Commands

```sh
# Run the MCP server (no build step)
python server.py

# Run one-time setup (interactive — registers with Claude Code)
python setup.py

# Run ALL integration tests (requires AWS/Bedrock credentials)
python test_integration.py

# Run a SINGLE test — edit test_integration.py:
#   1. Find the `tests = [...]` list near line 301
#   2. Replace with a single-element list, e.g.: tests = [test_store_basic]
#   3. Run: python test_integration.py
```

No linter, formatter, CI pipeline, or pre-commit hooks are configured.
Follow PEP 8 conventions manually.

## Key Infrastructure

- **Database:** SQLite with FTS5 (optional — degrades gracefully)
- **LLM:** Amazon Bedrock, model `us.anthropic.claude-3-5-haiku-20241022-v1:0`
- **Auth:** AWS credentials via `~/.aws/credentials` (or `AWS_PROFILE` env var)
- **Config:** `AWS_REGION` (default `us-east-1`), `BEDROCK_MODEL` env vars
- **MCP tools:** `memory_store`, `memory_query`, `memory_list`, `memory_forget`, `memory_consolidate`

## Code Style

### Imports

Stdlib first, then third-party, separated by a blank line. Alphabetical within groups.
All at module top level. No relative imports. No `from __future__`.

```python
import json
import os
import sqlite3

from mcp.server.fastmcp import FastMCP
```

### Naming Conventions

| Element             | Convention         | Examples                                   |
|---------------------|--------------------|--------------------------------------------|
| Files               | `snake_case`       | `server.py`, `test_integration.py`         |
| Functions           | `snake_case`       | `get_db()`, `llm_call()`, `memory_store()` |
| Variables           | `snake_case`       | `mem_id`, `fts_query`, `merged_content`    |
| Constants           | `UPPER_SNAKE_CASE` | `DB_PATH`, `AWS_REGION`, `BEDROCK_MODEL`   |
| Temp/loop vars      | Short `snake_case` | `m`, `r`, `row`, `idx`                     |

No classes exist. If adding classes, use `PascalCase`.

### Type Annotations

- Annotate function signatures (params + return). Do NOT annotate locals.
- Use PEP 585 lowercase generics: `list[dict]`, not `List[Dict]`.
- Do NOT import from `typing` — no `Optional`, `Union`, `Any`.
- No dataclasses, TypedDicts, Protocols, or NamedTuples — use plain `dict`.

```python
def rank_memories(query: str, candidates: list[dict], limit: int) -> list[dict]:
```

### Error Handling — Defensive, Never-Crash

- **LLM calls:** Always `try/except Exception`, return sensible defaults on failure.
- **FTS operations:** `try/except sqlite3.OperationalError: pass` on every FTS read/write.
  FTS is optional — fall back to LIKE queries silently.
- **No custom exceptions.** No `raise`. Errors are absorbed locally with fallbacks.
- Consolidation failures: `except Exception: continue` (skip action, keep going).

```python
# LLM call with fallback
try:
    raw = llm_call(system, user)
    result = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
except Exception:
    pass
return default_value

# Optional FTS operation
try:
    conn.execute("INSERT INTO memories_fts ...")
except sqlite3.OperationalError:
    pass
```

### Functions

- All `def` — no lambdas, no closures, no async/await (boto3 is blocking).
- Access module-level globals directly (`DB_PATH`, `bedrock`, `mcp`).
- MCP tools: decorated `@mcp.tool()` with docstrings containing `Args:` sections.
- Use default parameter values for optional args: `scope: str = "global"`.

### Docstrings and Comments

- Module-level docstrings at top of file (triple-quoted).
- Section separators: `# --- Section Name ---`
- MCP tool functions: docstrings with `Args:` (one line per arg, no types in docstring).
- Internal helpers: no docstrings required.
- Inline comments for non-obvious logic. No TODO/FIXME/HACK in production code.

### File Organization (server.py top-down order)

1. Module docstring
2. Imports (stdlib, then third-party)
3. Config constants (`DB_DIR`, `DB_PATH`, `AWS_REGION`, `BEDROCK_MODEL`)
4. Module init (`os.makedirs`, `mcp = FastMCP(...)`, `bedrock` client)
5. DB helpers (`get_db`, `init_db`)
6. `init_db()` call at module level
7. LLM helpers (`llm_call`, `extract_memory_metadata`, `rank_memories`)
8. MCP tool functions (`@mcp.tool()`)
9. `if __name__ == "__main__": mcp.run()`

### Data Patterns

- DB rows: fetch as `sqlite3.Row`, immediately convert via `dict(r)`.
- UUIDs: `str(uuid.uuid4())`
- Timestamps: `datetime.now(timezone.utc).isoformat()`
- JSON from LLM: extract with `raw[raw.find("{"):raw.rfind("}") + 1]`, then `json.loads()`
- Config: `os.environ.get("KEY", "default")`

### Testing Patterns

- Tests are plain functions (no framework, no decorators, no classes).
- Report with `ok(name)` / `fail(name, reason)` helpers.
- All test data uses `scope="__test__"` for isolation.
- Patch module globals before import: `server.DB_PATH = TEST_DB`.
- Cleanup removes all `__test__` scope data; test DB deleted after run.
- Tests run sequentially from a list; exceptions caught by runner. Exit 0/1.
