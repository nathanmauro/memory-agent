# AGENTS.md — Memory Agent

## Project Overview

MCP server providing persistent structured memory for AI coding agents.
Python 3.11+, SQLite + FTS5, Claude Haiku via Amazon Bedrock.
Pydantic models for validation; otherwise procedural.

## File Layout

| File / Directory          | Purpose                                                      |
|---------------------------|--------------------------------------------------------------|
| `server.py`               | Entry point: calls `init_db()`, runs `mcp.run()`            |
| `tools.py`                | MCP `FastMCP` app + 5 `@mcp.tool()` functions               |
| `db.py`                   | DB config, `get_db`, `init_db`, FTS/query helpers            |
| `llm.py`                  | AWS/Bedrock client, `llm_call`, JSON extractors, ranking     |
| `models/`                 | Pydantic models package                                      |
| `models/types.py`         | Reusable validators + `Annotated` type aliases               |
| `models/memory.py`        | `MemoryRecord`, `MemoryMetadata`                             |
| `models/options.py`       | `MemoryQueryOptions`, `MemoryListOptions`                    |
| `models/consolidation.py` | `ConsolidationAction`, `ConsolidationResult`                 |
| `setup.py`                | One-time interactive setup (API key + MCP registration)      |
| `test_integration.py`     | Integration tests (requires Bedrock access)                  |
| `test_validation.py`      | Local validation tests (no Bedrock needed)                   |
| `docs/`                   | Design specs                                                 |

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

# Run local validation tests (no Bedrock needed)
python test_validation.py

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

Stdlib first, then third-party, then local modules, separated by blank lines.
Alphabetical within groups. All at module top level. No relative imports. No `from __future__`.

```python
import json
import os
import sqlite3

from mcp.server.fastmcp import FastMCP

import db
import llm
from models import MemoryRecord, MemoryMetadata
```

### Naming Conventions

| Element             | Convention         | Examples                                   |
|---------------------|--------------------|--------------------------------------------|
| Files               | `snake_case`       | `server.py`, `test_integration.py`         |
| Functions           | `snake_case`       | `get_db()`, `llm_call()`, `memory_store()` |
| Variables           | `snake_case`       | `mem_id`, `fts_query`, `merged_content`    |
| Constants           | `UPPER_SNAKE_CASE` | `DB_PATH`, `AWS_REGION`, `BEDROCK_MODEL`   |
| Temp/loop vars      | Short `snake_case` | `m`, `r`, `row`, `idx`                     |

Pydantic model classes use `PascalCase` (e.g. `MemoryRecord`, `MemoryMetadata`).

### Type Annotations

- Annotate function signatures (params + return). Do NOT annotate locals.
- Use PEP 585 lowercase generics: `list[dict]`, not `List[Dict]`.
- Only import `Annotated` from `typing` — no `Optional`, `Union`, `Any`.
- Use Pydantic `BaseModel` for validated data structures. No dataclasses, TypedDicts, or NamedTuples.
- Use `Annotated` with `BeforeValidator` for reusable field-level validation.

```python
def rank_memories(query: str, candidates: list[MemoryRecord], limit: int) -> list[MemoryRecord]:
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

- All `def` — no lambdas, no async/await (boto3 is blocking).
- Access module-level globals directly (`DB_PATH`, `bedrock`, `mcp`).
- MCP tools: decorated `@mcp.tool()` with docstrings containing `Args:` sections.
- Use default parameter values for optional args: `scope: str = "global"`.

### Docstrings and Comments

- Module-level docstrings at top of file (triple-quoted).
- MCP tool functions: docstrings with `Args:` (one line per arg, no types in docstring).
- Internal helpers: no docstrings required.
- Inline comments for non-obvious logic. No TODO/FIXME/HACK in production code.

### Module Responsibilities

| Module               | Owns                                                       |
|----------------------|-------------------------------------------------------------|
| `models/types.py`    | `VALID_CATEGORIES`, validator functions, `Annotated` aliases|
| `models/memory.py`   | `MemoryRecord`, `MemoryMetadata`                            |
| `models/options.py`  | `MemoryQueryOptions`, `MemoryListOptions`                   |
| `models/consolidation.py` | `ConsolidationAction`, `ConsolidationResult`           |
| `db.py`              | `DB_DIR`, `DB_PATH`, `STOP_WORDS`, all DB helpers           |
| `llm.py`             | `AWS_REGION`, `BEDROCK_MODEL`, `bedrock`, all LLM helpers   |
| `tools.py`           | `mcp` (FastMCP app), all `@mcp.tool()` functions            |
| `server.py`          | Entry point only: `init_db()` + `mcp.run()`                 |

### Data Patterns

- DB rows: fetch as `sqlite3.Row`, convert via `MemoryRecord.from_row(r)`.
- UUIDs: `str(uuid.uuid4())`
- Timestamps: `datetime.now(timezone.utc).isoformat()`
- JSON from LLM: `extract_json_object(raw)` / `extract_json_array(raw)`, then `Model.model_validate()`
- Config: `os.environ.get("KEY", "default")`

### Testing Patterns

- Tests are plain functions (no framework, no decorators, no classes).
- Report with `ok(name)` / `fail(name, reason)` helpers.
- All test data uses `scope="__test__"` for isolation.
- Patch module globals at the source: `db.DB_PATH = TEST_DB`, `llm.llm_call = mock_fn`.
- Cleanup removes all `__test__` scope data; test DB deleted after run.
- Tests run sequentially from a list; exceptions caught by runner. Exit 0/1.
