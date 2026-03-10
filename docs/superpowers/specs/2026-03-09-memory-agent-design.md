# Memory Agent — MCP Server for Claude Code

## Overview

An MCP server that gives Claude Code persistent, structured memory using SQLite and Claude Haiku for intelligent storage and retrieval. No vector databases or embeddings — the LLM handles categorization, deduplication, and relevance ranking directly.

## Architecture

Single Python MCP server process. Claude Code connects to it natively via MCP protocol. On write, Haiku extracts structured fields and deduplicates. On read, SQLite FTS5 retrieves candidates and Haiku ranks by relevance.

## Storage

SQLite database at `~/.claude/memory/memory.db`.

### Schema

```sql
CREATE VIRTUAL TABLE memories_fts USING fts5(content, tags);

CREATE TABLE memories (
  id TEXT PRIMARY KEY,
  content TEXT NOT NULL,
  category TEXT NOT NULL CHECK(category IN ('session_summary', 'code_decision', 'user_preference', 'project_knowledge')),
  scope TEXT NOT NULL DEFAULT 'global',
  tags TEXT DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  source TEXT DEFAULT '',
  importance INTEGER NOT NULL DEFAULT 3 CHECK(importance BETWEEN 1 AND 5)
);
```

### Scoping

- `global` — cross-project preferences, workflow, tools
- Per-project — e.g., `arduino`, `b2c-policies`. Matches project directory name.

## MCP Tools

### memory_store

Store a memory. Haiku extracts category, tags, importance, checks for duplicates, merges if overlapping.

**Parameters:**
- `content` (string, required) — the memory to store
- `scope` (string, optional) — project name or "global" (default: "global")
- `source` (string, optional) — where this came from

**Flow:**
1. Call Haiku with new memory + existing memories in same scope
2. Haiku returns: category, tags, importance, and whether to merge with an existing memory
3. Upsert into SQLite + update FTS index

### memory_query

Retrieve relevant memories using natural language.

**Parameters:**
- `query` (string, required) — what to look for
- `scope` (string, optional) — filter by scope (default: search all)
- `category` (string, optional) — filter by category
- `limit` (integer, optional) — max results (default: 10)

**Flow:**
1. FTS5 search for candidate memories (top 20-30)
2. Call Haiku to rank by relevance to query
3. Return top N ranked results

### memory_list

List recent memories. No LLM call.

**Parameters:**
- `scope` (string, optional) — filter by scope
- `category` (string, optional) — filter by category
- `limit` (integer, optional) — max results (default: 20)

### memory_forget

Delete a memory by ID.

**Parameters:**
- `id` (string, required) — memory UUID to delete

### memory_consolidate

Manually trigger consolidation for a scope. Haiku reviews all memories in scope, merges duplicates, updates stale info, removes obsolete entries.

**Parameters:**
- `scope` (string, required) — which scope to consolidate

## Stack

- Python 3.11
- `mcp` Python SDK (pip install mcp)
- `sqlite3` (stdlib)
- `anthropic` SDK (Haiku calls)
- Project: `~/code/project/memory-agent/`
- Entry: `server.py`

## Configuration

Added to `~/.claude/settings.json` under `mcpServers`:

```json
{
  "mcpServers": {
    "memory": {
      "command": "python",
      "args": ["C:\\Users\\nathan\\code\\project\\memory-agent\\server.py"],
      "env": {
        "ANTHROPIC_API_KEY": "<key>"
      }
    }
  }
}
```

## Design Decisions

- **No vector DB** — SQLite FTS5 for candidate retrieval, Haiku for semantic ranking. Simpler stack, less infrastructure.
- **On-write dedup** — prevents memory bloat without a background process.
- **On-read ranking** — ensures relevance without maintaining embeddings.
- **Per-project scoping** — mirrors Claude Code's existing project structure.
- **Haiku for all LLM ops** — fast and cheap (~$0.25/M input tokens). Memory operations are small.
