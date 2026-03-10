"""
Memory Agent — MCP Server for Claude Code
Persistent structured memory using SQLite + local Qwen (Ollama).
No vector databases. LLM-driven categorization, dedup, and ranking.
"""

import json
import os
import re
import sqlite3
import urllib.request
import uuid
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

# --- Config ---
DB_DIR = os.path.join(os.path.expanduser("~"), ".claude", "memory")
DB_PATH = os.path.join(DB_DIR, "memory.db")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:14b")

# --- Init ---
os.makedirs(DB_DIR, exist_ok=True)
mcp = FastMCP("memory-agent")


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'project_knowledge',
            scope TEXT NOT NULL DEFAULT 'global',
            tags TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            source TEXT DEFAULT '',
            importance INTEGER NOT NULL DEFAULT 3
        );

        CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope);
        CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category);
    """)
    # Create FTS table if not exists
    try:
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
            USING fts5(id UNINDEXED, content, tags)
        """)
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


init_db()


# --- Haiku helpers ---

def llm_call(system: str, user: str) -> str:
    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": f"{system}\n\n{user}",
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 1024},
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    return data.get("response", "")


def extract_memory_metadata(content: str, scope: str, existing_memories: list[dict]) -> dict:
    existing_text = ""
    if existing_memories:
        existing_text = "\n\nExisting memories in this scope:\n"
        for m in existing_memories[:20]:
            existing_text += f"- [id={m['id']}] ({m['category']}) {m['content'][:200]}\n"

    system = """You are a memory management agent. Given a new memory and existing memories, you must:
1. Categorize it as one of: session_summary, code_decision, user_preference, project_knowledge
2. Extract relevant tags (comma-separated, lowercase, short)
3. Rate importance 1-5 (5=critical, 1=trivial)
4. Check if it should MERGE with an existing memory (same topic, updated info)

Respond with ONLY valid JSON:
{
  "category": "...",
  "tags": "tag1,tag2",
  "importance": 3,
  "merge_with_id": null,
  "merged_content": null
}

If merging, set merge_with_id to the existing memory's id and merged_content to the combined text."""

    user = f"New memory to store (scope: {scope}):\n{content}{existing_text}"

    try:
        raw = llm_call(system, user)
        # Extract JSON from response
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(raw[start:end])
    except Exception:
        pass

    return {
        "category": "project_knowledge",
        "tags": "",
        "importance": 3,
        "merge_with_id": None,
        "merged_content": None,
    }


def rank_memories(query: str, candidates: list[dict], limit: int) -> list[dict]:
    if not candidates:
        return []
    if len(candidates) <= limit:
        return candidates

    cand_text = ""
    for i, m in enumerate(candidates):
        cand_text += f"{i}. [id={m['id']}] ({m['category']}, importance={m['importance']}) {m['content'][:300]}\n"

    system = """You are a memory retrieval agent. Given a query and candidate memories, rank them by relevance.
Respond with ONLY a JSON array of indices (integers) in order of relevance, most relevant first.
Example: [3, 0, 7, 1]
Return at most the number requested."""

    user = f"Query: {query}\nReturn top {limit} results.\n\nCandidates:\n{cand_text}"

    try:
        raw = llm_call(system, user)
        start = raw.find("[")
        end = raw.rfind("]") + 1
        if start >= 0 and end > start:
            indices = json.loads(raw[start:end])
            ranked = []
            for idx in indices[:limit]:
                if isinstance(idx, int) and 0 <= idx < len(candidates):
                    ranked.append(candidates[idx])
            return ranked
    except Exception:
        pass

    return candidates[:limit]


# --- MCP Tools ---

@mcp.tool()
def memory_store(content: str, scope: str = "global", source: str = "") -> str:
    """Store a memory. The LLM will categorize it, check for duplicates, and merge if needed.

    Args:
        content: The memory to store (what happened, what was decided, what the user prefers, etc.)
        scope: Project name (e.g. 'arduino', 'b2c-policies') or 'global' for cross-project memories
        source: Optional source identifier (e.g. session ID, filename)
    """
    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()

    # Get existing memories in scope for dedup check
    rows = conn.execute(
        "SELECT id, content, category, tags, importance FROM memories WHERE scope = ? ORDER BY updated_at DESC LIMIT 30",
        (scope,),
    ).fetchall()
    existing = [dict(r) for r in rows]

    meta = extract_memory_metadata(content, scope, existing)

    merge_id = meta.get("merge_with_id")
    merged_content = meta.get("merged_content")

    if merge_id and merged_content:
        # Merge with existing memory
        conn.execute(
            "UPDATE memories SET content = ?, category = ?, tags = ?, importance = ?, updated_at = ? WHERE id = ?",
            (merged_content, meta["category"], meta["tags"], meta["importance"], now, merge_id),
        )
        # Update FTS
        try:
            conn.execute("DELETE FROM memories_fts WHERE id = ?", (merge_id,))
            conn.execute(
                "INSERT INTO memories_fts (id, content, tags) VALUES (?, ?, ?)",
                (merge_id, merged_content, meta["tags"]),
            )
        except sqlite3.OperationalError:
            pass
        conn.commit()
        conn.close()
        return f"Merged with existing memory {merge_id}"
    else:
        # Insert new memory
        mem_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO memories (id, content, category, scope, tags, created_at, updated_at, source, importance) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (mem_id, content, meta["category"], scope, meta["tags"], now, now, source, meta["importance"]),
        )
        try:
            conn.execute(
                "INSERT INTO memories_fts (id, content, tags) VALUES (?, ?, ?)",
                (mem_id, content, meta["tags"]),
            )
        except sqlite3.OperationalError:
            pass
        conn.commit()
        conn.close()
        return f"Stored memory {mem_id} [{meta['category']}] importance={meta['importance']} tags={meta['tags']}"


@mcp.tool()
def memory_query(query: str, scope: str = "", category: str = "", limit: int = 10) -> str:
    """Retrieve relevant memories using natural language search.

    Args:
        query: What to search for (natural language)
        scope: Filter by scope (project name or 'global'). Empty = search all scopes.
        category: Filter by category (session_summary, code_decision, user_preference, project_knowledge). Empty = all.
        limit: Maximum number of results to return (default 10)
    """
    conn = get_db()

    # Build query
    conditions = []
    params = []
    if scope:
        conditions.append("m.scope = ?")
        params.append(scope)
    if category:
        conditions.append("m.category = ?")
        params.append(category)

    where = ""
    if conditions:
        where = "WHERE " + " AND ".join(conditions)

    # Try FTS first
    candidates = []
    stop_words = {"the", "a", "an", "is", "are", "was", "were", "do", "does", "did", "has", "have", "had",
                  "what", "which", "who", "whom", "this", "that", "these", "those", "how", "when", "where",
                  "why", "can", "could", "will", "would", "should", "may", "might", "shall", "for", "and",
                  "but", "or", "not", "no", "with", "from", "about", "into", "over", "after", "before"}
    try:
        words = [re.sub(r'[^\w]', '', w).lower() for w in query.split()[:10]]
        words = [w for w in words if len(w) > 2 and w not in stop_words]
        if words:
            fts_query = " OR ".join(words)
            scope_filter = ""
            fts_params = [fts_query]
            if conditions:
                scope_filter = " AND " + " AND ".join(["m." + c if not c.startswith("m.") else c for c in conditions])
                fts_params += params
            fts_sql = f"""
                SELECT m.* FROM memories m
                INNER JOIN memories_fts f ON m.id = f.id
                WHERE f.memories_fts MATCH ?{scope_filter}
                ORDER BY rank
                LIMIT 30
            """
            rows = conn.execute(fts_sql, fts_params).fetchall()
            candidates = [dict(r) for r in rows]
    except sqlite3.OperationalError:
        pass

    # Fallback: if FTS returned too few, supplement with LIKE search
    if len(candidates) < 5:
        like_words = [re.sub(r'[^\w]', '', w).lower() for w in query.split()[:10]]
        like_words = [w for w in like_words if len(w) > 2 and w not in stop_words][:5]
        like_conditions = list(conditions)
        like_params = list(params)
        for word in like_words:
            like_conditions.append("(m.content LIKE ? OR m.tags LIKE ?)")
            like_params.extend([f"%{word}%", f"%{word}%"])
        like_where = "WHERE " + " AND ".join(like_conditions) if like_conditions else ""
        rows = conn.execute(
            f"SELECT m.* FROM memories m {like_where} ORDER BY m.importance DESC, m.updated_at DESC LIMIT 30",
            like_params,
        ).fetchall()
        seen = {c["id"] for c in candidates}
        for r in rows:
            d = dict(r)
            if d["id"] not in seen:
                candidates.append(d)
                seen.add(d["id"])

    conn.close()

    if not candidates:
        return "No memories found."

    # Rank with Haiku
    ranked = rank_memories(query, candidates, limit)

    results = []
    for m in ranked:
        results.append(
            f"[{m['id'][:8]}] ({m['scope']}/{m['category']}) importance={m['importance']} "
            f"tags={m['tags']} updated={m['updated_at'][:10]}\n  {m['content']}"
        )

    return f"Found {len(ranked)} memories:\n\n" + "\n\n".join(results)


@mcp.tool()
def memory_list(scope: str = "", category: str = "", limit: int = 20) -> str:
    """List recent memories. No LLM call — just a database query.

    Args:
        scope: Filter by scope. Empty = all scopes.
        category: Filter by category. Empty = all categories.
        limit: Maximum results (default 20)
    """
    conn = get_db()
    conditions = []
    params = []
    if scope:
        conditions.append("scope = ?")
        params.append(scope)
    if category:
        conditions.append("category = ?")
        params.append(category)
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    params.append(limit)

    rows = conn.execute(
        f"SELECT * FROM memories {where} ORDER BY updated_at DESC LIMIT ?",
        params,
    ).fetchall()
    conn.close()

    if not rows:
        return "No memories found."

    results = []
    for m in rows:
        m = dict(m)
        results.append(
            f"[{m['id'][:8]}] ({m['scope']}/{m['category']}) importance={m['importance']} "
            f"tags={m['tags']} updated={m['updated_at'][:10]}\n  {m['content'][:200]}"
        )

    return f"{len(results)} memories:\n\n" + "\n\n".join(results)


@mcp.tool()
def memory_forget(id: str) -> str:
    """Delete a specific memory by ID (or first 8 chars of ID).

    Args:
        id: Memory UUID (full or first 8 characters)
    """
    conn = get_db()
    if len(id) < 36:
        row = conn.execute("SELECT id FROM memories WHERE id LIKE ?", (f"{id}%",)).fetchone()
        if row:
            id = row["id"]
        else:
            conn.close()
            return f"No memory found matching '{id}'"

    conn.execute("DELETE FROM memories WHERE id = ?", (id,))
    try:
        conn.execute("DELETE FROM memories_fts WHERE id = ?", (id,))
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()
    return f"Deleted memory {id}"


@mcp.tool()
def memory_consolidate(scope: str) -> str:
    """Consolidate memories in a scope: merge duplicates, update stale info, remove obsolete entries.

    Args:
        scope: Which scope to consolidate (e.g. 'global', 'arduino')
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM memories WHERE scope = ? ORDER BY category, updated_at DESC",
        (scope,),
    ).fetchall()
    memories = [dict(r) for r in rows]
    conn.close()

    if not memories:
        return f"No memories in scope '{scope}' to consolidate."

    mem_text = ""
    for m in memories:
        mem_text += f"[id={m['id']}] category={m['category']} importance={m['importance']} tags={m['tags']}\n  {m['content']}\n\n"

    system = """You are a memory consolidation agent. Review all memories in this scope and identify:
1. Duplicates that should be merged (same topic, redundant info)
2. Outdated memories that should be updated
3. Obsolete memories that should be deleted

Respond with ONLY valid JSON:
{
  "actions": [
    {"type": "merge", "keep_id": "...", "delete_id": "...", "new_content": "..."},
    {"type": "delete", "id": "...", "reason": "..."},
    {"type": "update", "id": "...", "new_content": "..."}
  ],
  "summary": "what was consolidated"
}

If nothing needs consolidation, return {"actions": [], "summary": "all memories are clean"}."""

    user = f"Scope: {scope}\nMemories ({len(memories)} total):\n\n{mem_text}"

    try:
        raw = llm_call(system, user)
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            result = json.loads(raw[start:end])
        else:
            return "Consolidation produced no actionable results."
    except Exception as e:
        return f"Consolidation failed: {e}"

    actions = result.get("actions", [])
    if not actions:
        return f"Scope '{scope}': {result.get('summary', 'nothing to consolidate')}"

    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()
    applied = 0

    for action in actions:
        try:
            if action["type"] == "merge":
                conn.execute(
                    "UPDATE memories SET content = ?, updated_at = ? WHERE id = ?",
                    (action["new_content"], now, action["keep_id"]),
                )
                conn.execute("DELETE FROM memories WHERE id = ?", (action["delete_id"],))
                try:
                    conn.execute("DELETE FROM memories_fts WHERE id = ?", (action["delete_id"],))
                    conn.execute("DELETE FROM memories_fts WHERE id = ?", (action["keep_id"],))
                    conn.execute(
                        "INSERT INTO memories_fts (id, content, tags) VALUES (?, ?, ?)",
                        (action["keep_id"], action["new_content"], ""),
                    )
                except sqlite3.OperationalError:
                    pass
                applied += 1
            elif action["type"] == "delete":
                conn.execute("DELETE FROM memories WHERE id = ?", (action["id"],))
                try:
                    conn.execute("DELETE FROM memories_fts WHERE id = ?", (action["id"],))
                except sqlite3.OperationalError:
                    pass
                applied += 1
            elif action["type"] == "update":
                conn.execute(
                    "UPDATE memories SET content = ?, updated_at = ? WHERE id = ?",
                    (action["new_content"], now, action["id"]),
                )
                try:
                    conn.execute("DELETE FROM memories_fts WHERE id = ?", (action["id"],))
                    conn.execute(
                        "INSERT INTO memories_fts (id, content, tags) VALUES (?, ?, ?)",
                        (action["id"], action["new_content"], ""),
                    )
                except sqlite3.OperationalError:
                    pass
                applied += 1
        except Exception:
            continue

    conn.commit()
    conn.close()

    return f"Consolidated scope '{scope}': {applied} actions applied. {result.get('summary', '')}"


if __name__ == "__main__":
    mcp.run()
