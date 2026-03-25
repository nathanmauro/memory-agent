"""MCP tool definitions for the memory agent."""

import sqlite3
import uuid
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

import db
import llm
from models import (
    ConsolidationResult,
    MemoryListOptions,
    MemoryQueryOptions,
    MemoryRecord,
)

mcp = FastMCP("memory-agent")


@mcp.tool()
def memory_store(content: str, scope: str = "global", source: str = "") -> str:
    """Store a memory. The LLM will categorize it, check for duplicates, and merge if needed.

    Args:
        content: The memory to store (what happened, what was decided, what the user prefers, etc.)
        scope: Project name (e.g. 'arduino', 'b2c-policies') or 'global' for cross-project memories
        source: Optional source identifier (e.g. session ID, filename)
    """
    conn = db.get_db()
    now = datetime.now(timezone.utc).isoformat()

    rows = conn.execute(
        "SELECT id, content, category, tags, importance FROM memories WHERE scope = ? ORDER BY updated_at DESC LIMIT 30",
        (scope,),
    ).fetchall()
    existing = [MemoryRecord.from_row(r) for r in rows]

    meta = llm.extract_memory_metadata(content, scope, existing)

    if meta.merge_with_id and meta.merged_content:
        conn.execute(
            "UPDATE memories SET content = ?, category = ?, tags = ?, importance = ?, updated_at = ? WHERE id = ?",
            (
                meta.merged_content,
                meta.category,
                meta.tags,
                meta.importance,
                now,
                meta.merge_with_id,
            ),
        )
        db.upsert_fts_memory(conn, meta.merge_with_id, meta.merged_content, meta.tags)
        conn.commit()
        conn.close()
        return f"Merged with existing memory {meta.merge_with_id}"

    mem_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO memories (id, content, category, scope, tags, created_at, updated_at, source, importance) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            mem_id,
            content,
            meta.category,
            scope,
            meta.tags,
            now,
            now,
            source,
            meta.importance,
        ),
    )
    db.upsert_fts_memory(conn, mem_id, content, meta.tags)
    conn.commit()
    conn.close()
    return f"Stored memory {mem_id} [{meta.category}] importance={meta.importance} tags={meta.tags}"


@mcp.tool()
def memory_query(
    query: str, scope: str = "", category: str = "", limit: int = 10
) -> str:
    """Retrieve relevant memories using natural language search.

    Args:
        query: What to search for (natural language)
        scope: Filter by scope (project name or 'global'). Empty = search all scopes.
        category: Filter by category (session_summary, code_decision, user_preference, project_knowledge). Empty = all.
        limit: Maximum number of results to return (default 10)
    """
    try:
        options = MemoryQueryOptions(
            query=query, scope=scope, category=category, limit=limit
        )
    except Exception:
        options = MemoryQueryOptions(query=str(query))

    conn = db.get_db()
    conditions, params = db.build_memory_filters(
        options.scope, options.category, alias="m"
    )

    # Try FTS first
    candidates = []
    try:
        words = db.extract_search_terms(options.query, 10)
        if words:
            fts_query = " OR ".join(words)
            scope_filter = ""
            fts_params = [fts_query]
            if conditions:
                scope_filter = " AND " + " AND ".join(conditions)
                fts_params += params
            fts_sql = f"""
                SELECT m.* FROM memories m
                INNER JOIN memories_fts f ON m.id = f.id
                WHERE f.memories_fts MATCH ?{scope_filter}
                ORDER BY rank
                LIMIT 30
            """
            rows = conn.execute(fts_sql, fts_params).fetchall()
            candidates = [MemoryRecord.from_row(r) for r in rows]
    except sqlite3.OperationalError:
        pass

    # Fallback: if FTS returned too few, supplement with LIKE search
    if len(candidates) < 5:
        like_words = db.extract_search_terms(options.query, 5)
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
        seen = {c.id for c in candidates}
        for r in rows:
            record = MemoryRecord.from_row(r)
            if record.id not in seen:
                candidates.append(record)
                seen.add(record.id)

    conn.close()

    if not candidates:
        return "No memories found."

    ranked = llm.rank_memories(options.query, candidates, options.limit)
    results = [m.format() for m in ranked]
    return f"Found {len(ranked)} memories:\n\n" + "\n\n".join(results)


@mcp.tool()
def memory_list(scope: str = "", category: str = "", limit: int = 20) -> str:
    """List recent memories. No LLM call — just a database query.

    Args:
        scope: Filter by scope. Empty = all scopes.
        category: Filter by category. Empty = all categories.
        limit: Maximum results (default 20)
    """
    try:
        options = MemoryListOptions(scope=scope, category=category, limit=limit)
    except Exception:
        options = MemoryListOptions()

    conn = db.get_db()
    conditions, params = db.build_memory_filters(options.scope, options.category)
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    params.append(options.limit)

    rows = conn.execute(
        f"SELECT * FROM memories {where} ORDER BY updated_at DESC LIMIT ?",
        params,
    ).fetchall()
    conn.close()

    if not rows:
        return "No memories found."

    results = [MemoryRecord.from_row(r).format(truncate=200) for r in rows]
    return f"{len(results)} memories:\n\n" + "\n\n".join(results)


@mcp.tool()
def memory_forget(id: str) -> str:
    """Delete a specific memory by ID (or first 8 chars of ID).

    Args:
        id: Memory UUID (full or first 8 characters)
    """
    conn = db.get_db()
    if len(id) < 36:
        row = conn.execute(
            "SELECT id FROM memories WHERE id LIKE ?", (f"{id}%",)
        ).fetchone()
        if row:
            id = row["id"]
        else:
            conn.close()
            return f"No memory found matching '{id}'"

    conn.execute("DELETE FROM memories WHERE id = ?", (id,))
    db.delete_fts_memory(conn, id)
    conn.commit()
    conn.close()
    return f"Deleted memory {id}"


@mcp.tool()
def memory_consolidate(scope: str) -> str:
    """Consolidate memories in a scope: merge duplicates, update stale info, remove obsolete entries.

    Args:
        scope: Which scope to consolidate (e.g. 'global', 'arduino')
    """
    conn = db.get_db()
    rows = conn.execute(
        "SELECT * FROM memories WHERE scope = ? ORDER BY category, updated_at DESC",
        (scope,),
    ).fetchall()
    memories = [MemoryRecord.from_row(r) for r in rows]
    conn.close()

    if not memories:
        return f"No memories in scope '{scope}' to consolidate."

    mem_text = ""
    for m in memories:
        mem_text += (
            f"[id={m.id}] category={m.category} importance={m.importance} "
            f"tags={m.tags}\n  {m.content}\n\n"
        )

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

    user_msg = f"Scope: {scope}\nMemories ({len(memories)} total):\n\n{mem_text}"

    try:
        raw = llm.llm_call(system, user_msg)
        result = ConsolidationResult.model_validate(llm.extract_json_object(raw))
        if not result.summary and not result.actions:
            return "Consolidation produced no actionable results."
    except Exception as e:
        return f"Consolidation failed: {e}"

    if not result.actions:
        return f"Scope '{scope}': {result.summary or 'nothing to consolidate'}"

    conn = db.get_db()
    now = datetime.now(timezone.utc).isoformat()
    applied = 0

    for action in result.actions:
        try:
            if (
                action.type == "merge"
                and action.keep_id
                and action.delete_id
                and action.new_content
            ):
                conn.execute(
                    "UPDATE memories SET content = ?, updated_at = ? WHERE id = ?",
                    (action.new_content, now, action.keep_id),
                )
                conn.execute("DELETE FROM memories WHERE id = ?", (action.delete_id,))
                db.delete_fts_memory(conn, action.delete_id)
                db.upsert_fts_memory(conn, action.keep_id, action.new_content, "")
                applied += 1
            elif action.type == "delete" and action.id:
                conn.execute("DELETE FROM memories WHERE id = ?", (action.id,))
                db.delete_fts_memory(conn, action.id)
                applied += 1
            elif action.type == "update" and action.id and action.new_content:
                conn.execute(
                    "UPDATE memories SET content = ?, updated_at = ? WHERE id = ?",
                    (action.new_content, now, action.id),
                )
                db.upsert_fts_memory(conn, action.id, action.new_content, "")
                applied += 1
        except Exception:
            continue

    conn.commit()
    conn.close()

    return f"Consolidated scope '{scope}': {applied} actions applied. {result.summary}"
