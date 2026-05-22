# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

An MCP server that gives Claude Code persistent, structured memory. SQLite + an LLM (local Qwen via Ollama by default, or Claude Haiku via Amazon Bedrock) handle storage, categorization, dedup/merge, and relevance ranking. No vector databases or embeddings.

The server is registered in `~/.mcp.json` and started automatically by Claude Code.

## Commands

```bash
# Run the server directly (debugging)
python server.py

# Integration tests — exercise the live LLM. Default backend is Ollama (qwen2.5:14b);
# set LLM_BACKEND=bedrock to use AWS instead.
python test_integration.py

# Pure-local validation tests — patch llm.llm_call with a mock, no LLM required.
python test_validation.py

# Run a single integration test: edit the `tests = [...]` list near the bottom
# of test_integration.py to a single-element list, then re-run.
```

No build step, linter, formatter, or CI is configured. See `AGENTS.md` for code-style conventions (imports, naming, type annotations, defensive error handling).

## Architecture

The codebase is small and procedural; `models/` is the only package.

| File / Dir                | Role                                                          |
|---------------------------|---------------------------------------------------------------|
| `server.py`               | Entry point: `db.init_db()` then `mcp.run()`.                 |
| `tools.py`                | `FastMCP` app + 8 `@mcp.tool()` handlers, plus `_insert_memory` / `_gather_candidates` shared helpers. |
| `db.py`                   | SQLite path/init, FTS upsert/delete, query-term extraction, `SESSIONS_DIR`, `resolve_memory_id`, `iter_session_buffers`. |
| `llm.py`                  | LLM dispatch (Ollama / Bedrock), JSON extraction, ranking.    |
| `hook_handler.py`         | Claude Code hook entrypoint: `inject-context`, `record`, `summarize-session`, `sweep`. Always exits 0. |
| `hooks/*.sh`              | Thin shell wrappers wired into `~/.claude/settings.json` by `install.py`. |
| `install.py`              | Idempotent installer: `claude mcp add -s user memory …` + merges hook entries into `~/.claude/settings.json` (with `.bak`). |
| `models/`                 | Pydantic models + reusable `Annotated` validators.            |
| `test_integration.py`     | Live-LLM tests, isolated to `memory_test.db` + `__test__` scope. |
| `test_validation.py`      | Local tests; mocks `llm.llm_call`, uses tempdir DB + sessions dir. |

### LLM backend

`llm.llm_call(system, user)` dispatches based on `LLM_BACKEND`:

- `ollama` (default): POSTs to `OLLAMA_URL` (default `http://localhost:11434`), model `OLLAMA_MODEL` (default `qwen2.5:14b`).
- `bedrock`: lazy `boto3` client, `BEDROCK_MODEL` (default `us.anthropic.claude-3-5-haiku-20241022-v1:0`), `AWS_REGION` (default `us-east-1`), `AWS_PROFILE` optional.

LLM responses are parsed with `extract_json_object` / `extract_json_array` (find first `{...}` / `[...]`), then validated through Pydantic models. LLM failures are swallowed and fall back to safe defaults — no `raise` propagates out of `llm.py`.

### Database

SQLite at `~/.claude/memory/memory.db`, WAL mode. Two tables: `memories` (rows) and `memories_fts` (FTS5 virtual table over `content` + `tags`, kept in sync via `upsert_fts_memory` / `delete_fts_memory`). FTS is treated as optional — every FTS write/read is wrapped in `try/except sqlite3.OperationalError: pass` so the server still works on builds without FTS5.

Tests patch `db.DB_PATH` before calling `db.init_db()` to redirect to a test DB.

### Data flow

- **`memory_store`** — load up to 30 most-recent memories in scope, ask LLM for `{category, tags, importance, merge_with_id?, merged_content?}`. If a merge is suggested, `UPDATE` the existing row; otherwise `INSERT` a new UUID. Both paths upsert into FTS. The body lives in `tools._insert_memory` so `hook_handler` shares it.
- **`memory_query`** — `tools._gather_candidates` runs FTS5 MATCH first (terms extracted via `extract_search_terms`, stop words filtered, punctuation stripped to avoid MATCH syntax errors). If FTS returns < 5 rows, supplement with a LIKE fallback. Candidates are then re-ranked by the LLM (`rank_memories`) and truncated to `limit`.
- **`memory_index`** — same `_gather_candidates` flow as `memory_query` but **no** LLM rerank. Returns one compact line per hit (id8, category, importance, tags, snippet). Use to scan many candidates cheaply.
- **`memory_get`** — accepts a list of full UUIDs or 8-char prefixes (each resolved via `db.resolve_memory_id`). Returns full `MemoryRecord.format()` for each found id.
- **`memory_list`** — pure DB query, no LLM.
- **`memory_timeline`** — pure DB query ordered by `updated_at`. Bounded optionally by `before_iso` / `after_iso`. Switches to ASC ordering when `after_iso` is set.
- **`memory_forget`** — accepts full UUID or first-8-char prefix; deletes from both tables.
- **`memory_consolidate`** — load all memories in a scope, ask LLM for `{actions: [merge|delete|update], summary}`, apply each action best-effort (per-action `try/except` so one bad action can't abort the batch).

### Auto-capture flow (Claude Code hooks)

`install.py` registers four entries in `~/.claude/settings.json`:

- **`SessionStart` → `hook_handler.py inject-context`** — derives scope from `cwd` (basename of nearest `.git` toplevel, else basename of cwd, else `global`), reads top 3 by `(importance, updated_at) DESC` plus the next 5 most-recent unique IDs, prints them as a `hookSpecificOutput.additionalContext` bullet list. No LLM call. Also calls `_sweep()` before returning so SessionEnd misses don't strand buffers forever.
- **`UserPromptSubmit` / `PostToolUse` → `hook_handler.py record --kind …`** — append a JSON line to `~/.claude/memory/sessions/<session_id>.jsonl`. Each `data` field is truncated to 500 bytes. Dumb, fast, no LLM.
- **`SessionEnd` → `hook_handler.py summarize-session`** — read the buffer, skip if `< 3` records, otherwise call LLM with the transcript, parse `{session_summary, memories[], open_actions[]}`, and insert each via `tools._insert_memory` (memories dedup/merge via LLM metadata; open actions stored as `open_action`). Archive and delete the buffer.

Every hook script ends with `exit 0`, and the Python entry wraps `main()` in `try/except Exception: pass` — a memory-agent fault cannot break a Claude Code session.

### Schema

- Categories: `session_summary`, `code_decision`, `user_preference`, `project_knowledge`, `open_action` (normalized in `models/types.py`; unknown values fall back to `project_knowledge`).
- Scope: project name (e.g. `arduino`) or `global`. Test scope is `__test__`.
- Importance: 1–5 (5 = critical), clamped via Pydantic `BeforeValidator`.

## Conventions Worth Knowing Up Front

- Pydantic is used for **validation only** (input options, LLM output parsing, DB row mapping via `MemoryRecord.from_row`). The rest of the code is plain procedural Python with module-level globals (`mcp`, `DB_PATH`, etc.).
- Defensive style: LLM and FTS calls never raise outward. Add new ones the same way.
- New reusable validators go in `models/types.py` as `Annotated[..., BeforeValidator(...)]` aliases. See `AGENTS.md` § "Code Style" for full details.

## Known Quirks

- The design doc under `docs/superpowers/specs/` predates the Ollama backend and the hook-based auto-capture.
- FTS query terms are stripped of punctuation and short/stop words before being joined with `OR` — anything shorter than 3 chars or in `db.STOP_WORDS` is dropped.
- `hook_handler.py` reuses `tools._insert_memory`, which calls `llm.extract_memory_metadata`. If Ollama is down during SessionEnd, the LLM call returns empty, the summarizer logs nothing, and the session buffer is removed anyway — by design, so the buffer dir doesn't grow unbounded.
