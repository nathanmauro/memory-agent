"""MCP tool definitions for the memory agent."""

import sqlite3
import uuid
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

from . import db, hot, llm
from .models import (
    ConsolidationResult,
    MemoryGetOptions,
    MemoryIndexOptions,
    MemoryListOptions,
    MemoryQueryOptions,
    MemoryRecord,
    MemorySessionGetOptions,
    MemorySessionSearchOptions,
    MemoryTimelineOptions,
)
from .models.types import _normalize_category, _normalize_tags

mcp = FastMCP("memory-agent")


def _insert_memory(
    conn: sqlite3.Connection,
    content: str,
    scope: str,
    source: str,
    category: str = "",
    tags: str = "",
) -> str:
    """Insert or merge a memory using LLM-derived metadata. Caller commits."""
    now = datetime.now(timezone.utc).isoformat()

    if category.strip():
        cat = _normalize_category(category)
        tag_str = _normalize_tags(tags) if tags else ""
        importance = 4 if cat == "open_action" else 3
        mem_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO memories (
                id, content, category, scope, tags, created_at, updated_at,
                source, importance
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mem_id,
                content,
                cat,
                scope,
                tag_str,
                now,
                now,
                source,
                importance,
            ),
        )
        db.upsert_fts_memory(conn, mem_id, content, tag_str)
        return (
            f"Stored memory {mem_id} [{cat}] importance={importance} tags={tag_str}"
        )

    rows = conn.execute(
        "SELECT id, content, category, tags, importance FROM memories WHERE scope = ? ORDER BY updated_at DESC LIMIT 30",
        (scope,),
    ).fetchall()
    existing = [MemoryRecord.from_row(r) for r in rows]

    meta = llm.extract_memory_metadata(content, scope, existing)
    if tags.strip():
        meta.tags = _normalize_tags(tags)

    existing_ids = {m.id for m in existing}
    if meta.merge_with_id not in existing_ids:
        meta.merge_with_id = None
        meta.merged_content = None

    if meta.merge_with_id and meta.merged_content:
        cursor = conn.execute(
            """
            UPDATE memories
            SET content = ?, category = ?, tags = ?, importance = ?, updated_at = ?
            WHERE id = ? AND scope = ?
            """,
            (
                meta.merged_content,
                meta.category,
                meta.tags,
                meta.importance,
                now,
                meta.merge_with_id,
                scope,
            ),
        )
        if cursor.rowcount:
            db.upsert_fts_memory(
                conn, meta.merge_with_id, meta.merged_content, meta.tags
            )
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
    return f"Stored memory {mem_id} [{meta.category}] importance={meta.importance} tags={meta.tags}"


def _gather_candidates(
    conn: sqlite3.Connection,
    query: str,
    scope: str = "",
    category: str = "",
    status: str = "active",
) -> list[MemoryRecord]:
    """FTS5 search over content+tags with a LIKE fallback when FTS returns too few rows."""
    conditions, params = db.build_memory_filters(scope, category, alias="m", status=status)

    candidates: list[MemoryRecord] = []
    try:
        words = db.extract_search_terms(query, 10)
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

    if len(candidates) < 5:
        like_words = db.extract_search_terms(query, 5)
        like_conditions = list(conditions)
        like_params = list(params)
        for word in like_words:
            like_conditions.append("(m.content LIKE ? OR m.tags LIKE ?)")
            like_params.extend([f"%{word}%", f"%{word}%"])
        like_where = (
            "WHERE " + " AND ".join(like_conditions) if like_conditions else ""
        )
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
    return candidates


def _touch_memory_ids(ids: list[str]) -> None:
    if not ids:
        return
    conn = db.get_db()
    try:
        db.touch_memories(conn, ids)
        conn.commit()
    finally:
        conn.close()


@mcp.tool()
def memory_store(content: str, scope: str = "global", source: str = "") -> str:
    """Store a memory. The LLM will categorize it, check for duplicates, and merge if needed.

    Args:
        content: The memory to store (what happened, what was decided, what the user prefers, etc.)
        scope: Project name (e.g. 'arduino', 'b2c-policies') or 'global' for cross-project memories
        source: Optional source identifier (e.g. session ID, filename)
    """
    conn = db.get_db()
    result = _insert_memory(conn, content, scope, source)
    conn.commit()
    conn.close()
    return result


@mcp.tool()
def memory_query(
    query: str,
    scope: str = "",
    category: str = "",
    status: str = "active",
    limit: int = 10,
) -> str:
    """Retrieve relevant memories using natural language search.

    Args:
        query: What to search for (natural language)
        scope: Filter by scope (project name or 'global'). Empty = search all scopes.
        category: Filter by category (session_summary, code_decision, user_preference, project_knowledge). Empty = all.
        status: Filter by lifecycle status (active, stale, archived). Empty = all.
        limit: Maximum number of results to return (default 10)
    """
    try:
        options = MemoryQueryOptions(
            query=query, scope=scope, category=category, status=status, limit=limit
        )
    except Exception:
        options = MemoryQueryOptions(query=str(query))

    conn = db.get_db()
    candidates = _gather_candidates(
        conn, options.query, options.scope, options.category, options.status
    )
    conn.close()

    if not candidates:
        return "No memories found."

    ranked = llm.rank_memories(options.query, candidates, options.limit)
    _touch_memory_ids([m.id for m in ranked])
    results = [m.format() for m in ranked]
    return f"Found {len(ranked)} memories:\n\n" + "\n\n".join(results)


@mcp.tool()
def memory_index(
    query: str,
    scope: str = "",
    category: str = "",
    status: str = "active",
    limit: int = 20,
) -> str:
    """Token-efficient search: FTS5+LIKE candidates with no LLM rerank. Returns one line per hit.

    Use this to scan many candidates cheaply, then call memory_get for full content of the IDs you want.

    Args:
        query: What to search for (natural language)
        scope: Filter by scope. Empty = search all scopes.
        category: Filter by category. Empty = all categories.
        status: Filter by lifecycle status (active, stale, archived). Empty = all.
        limit: Maximum results (default 20)
    """
    try:
        options = MemoryIndexOptions(
            query=query, scope=scope, category=category, status=status, limit=limit
        )
    except Exception:
        options = MemoryIndexOptions(query=str(query))

    conn = db.get_db()
    candidates = _gather_candidates(
        conn, options.query, options.scope, options.category, options.status
    )
    conn.close()

    if not candidates:
        return "No memories found."

    hits = candidates[: options.limit]
    _touch_memory_ids([m.id for m in hits])
    lines = []
    for m in hits:
        snippet = m.content[:160].replace("\n", " ")
        lines.append(
            f"{m.id[:8]} [{m.category}] i={m.importance} tags={m.tags} — {snippet}"
        )
    return f"{len(hits)} hits:\n" + "\n".join(lines)


@mcp.tool()
def memory_get(ids: list[str]) -> str:
    """Fetch full content for one or more memories by ID.

    Args:
        ids: List of memory IDs (full UUIDs or 8-char prefixes)
    """
    try:
        options = MemoryGetOptions(ids=ids)
    except Exception:
        return "No memories found."

    if not options.ids:
        return "No memories found."

    conn = db.get_db()
    found: list[MemoryRecord] = []
    missing: list[str] = []
    seen: set[str] = set()
    for raw_id in options.ids:
        resolved = db.resolve_memory_id(conn, raw_id)
        if not resolved or resolved in seen:
            if not resolved:
                missing.append(raw_id)
            continue
        row = conn.execute(
            "SELECT * FROM memories WHERE id = ?", (resolved,)
        ).fetchone()
        if row:
            found.append(MemoryRecord.from_row(row))
            seen.add(resolved)
    if found:
        db.touch_memories(conn, [m.id for m in found])
        conn.commit()
    conn.close()

    if not found:
        return "No memories found."

    out = "\n\n".join(m.format() for m in found)
    if missing:
        out += f"\n\n(not found: {', '.join(missing)})"
    return out


def _set_memory_pin(id: str, scope: str, pinned: int) -> str:
    conn = db.get_db()
    matches = db.find_memory_matches(conn, id, scope)
    if len(matches) > 1:
        lines = [
            f"- {row['id'][:8]} [{row['scope']}] {str(row['updated_at'])[:10]}"
            for row in matches
        ]
        conn.close()
        return (
            f"Memory ID prefix '{id}' is ambiguous; pass a longer ID or scope.\n"
            + "\n".join(lines)
        )
    resolved = matches[0]["id"] if matches else None
    if not resolved:
        conn.close()
        return f"No memory found matching '{id}'"

    now = datetime.now(timezone.utc).isoformat()
    if pinned:
        conn.execute(
            """
            UPDATE memories
            SET pinned = 1, status = 'active', updated_at = ?
            WHERE id = ?
            """,
            (now, resolved),
        )
        action = "Pinned"
    else:
        conn.execute(
            """
            UPDATE memories
            SET pinned = 0, updated_at = ?
            WHERE id = ?
            """,
            (now, resolved),
        )
        action = "Unpinned"
    conn.commit()
    conn.close()
    return f"{action} memory {resolved}"


@mcp.tool()
def memory_pin(id: str, scope: str = "") -> str:
    """Mark a memory as startup-safe so inject-context can include it.

    Args:
        id: Memory UUID (full or first 8 characters)
        scope: Optional scope filter when resolving ambiguous prefixes
    """
    return _set_memory_pin(id, scope, 1)


@mcp.tool()
def memory_unpin(id: str, scope: str = "") -> str:
    """Remove startup-injection privilege from a memory without deleting it.

    Args:
        id: Memory UUID (full or first 8 characters)
        scope: Optional scope filter when resolving ambiguous prefixes
    """
    return _set_memory_pin(id, scope, 0)


@mcp.tool()
def memory_timeline(
    scope: str = "",
    status: str = "active",
    before_iso: str = "",
    after_iso: str = "",
    limit: int = 20,
) -> str:
    """List memories ordered by update time. No LLM call. Useful for chronological review.

    Args:
        scope: Filter by scope. Empty = all scopes.
        status: Filter by lifecycle status (active, stale, archived). Empty = all.
        before_iso: Only memories with updated_at < this ISO timestamp. Empty = no upper bound.
        after_iso: Only memories with updated_at > this ISO timestamp. Empty = no lower bound.
        limit: Maximum results (default 20)
    """
    try:
        options = MemoryTimelineOptions(
            scope=scope,
            status=status,
            before_iso=before_iso,
            after_iso=after_iso,
            limit=limit,
        )
    except Exception:
        options = MemoryTimelineOptions()

    conditions, params = db.build_memory_filters(options.scope, "", status=options.status)
    if options.before_iso:
        conditions.append("updated_at < ?")
        params.append(options.before_iso)
    if options.after_iso:
        conditions.append("updated_at > ?")
        params.append(options.after_iso)
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    order = "ASC" if options.after_iso else "DESC"
    params.append(options.limit)

    conn = db.get_db()
    rows = conn.execute(
        f"SELECT * FROM memories {where} ORDER BY updated_at {order} LIMIT ?",
        params,
    ).fetchall()
    conn.close()

    if not rows:
        return "No memories found."

    results = [MemoryRecord.from_row(r).format(truncate=200) for r in rows]
    return f"{len(results)} memories ({order.lower()}):\n\n" + "\n\n".join(results)


@mcp.tool()
def memory_list(
    scope: str = "", category: str = "", status: str = "active", limit: int = 20
) -> str:
    """List recent memories. No LLM call — just a database query.

    Args:
        scope: Filter by scope. Empty = all scopes.
        category: Filter by category. Empty = all categories.
        status: Filter by lifecycle status (active, stale, archived). Empty = all.
        limit: Maximum results (default 20)
    """
    try:
        options = MemoryListOptions(
            scope=scope, category=category, status=status, limit=limit
        )
    except Exception:
        options = MemoryListOptions()

    conn = db.get_db()
    conditions, params = db.build_memory_filters(
        options.scope, options.category, status=options.status
    )
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
def memory_forget(id: str, scope: str = "") -> str:
    """Delete a specific memory by ID (or first 8 chars of ID).

    Args:
        id: Memory UUID (full or first 8 characters)
        scope: Optional scope filter when resolving ambiguous prefixes
    """
    conn = db.get_db()
    matches = db.find_memory_matches(conn, id, scope)
    if len(matches) > 1:
        lines = [
            f"- {row['id'][:8]} [{row['scope']}] {str(row['updated_at'])[:10]}"
            for row in matches
        ]
        conn.close()
        return (
            f"Memory ID prefix '{id}' is ambiguous; pass a longer ID or scope.\n"
            + "\n".join(lines)
        )
    resolved = matches[0]["id"] if matches else None
    if not resolved:
        conn.close()
        return f"No memory found matching '{id}'"

    conn.execute("DELETE FROM memories WHERE id = ?", (resolved,))
    db.delete_fts_memory(conn, resolved)
    conn.commit()
    conn.close()
    return f"Deleted memory {resolved}"

@mcp.tool()
def memory_session_search(query: str, scope: str = "", limit: int = 10) -> str:
    """Search archived session transcripts (cold storage). JSONL on disk is source of truth.

    Args:
        query: What to search for in archived session content
        scope: Filter by project scope. Empty = search all scopes.
        limit: Maximum results (default 10)
    """
    try:
        options = MemorySessionSearchOptions(query=query, scope=scope, limit=limit)
    except Exception:
        options = MemorySessionSearchOptions(query=str(query))

    conn = db.get_db()
    hits = db.search_session_archive(
        conn, options.query, options.scope, options.limit
    )
    conn.close()

    if not hits:
        return "No archived sessions found."

    lines = []
    for hit in hits:
        lines.append(
            f"{hit['session_id'][:8]} [{hit['scope']}] {hit['archived_at'][:10]} — "
            f"{hit['snippet']}"
        )
    return f"{len(hits)} archived sessions:\n" + "\n".join(lines)


@mcp.tool()
def memory_session_get(session_id: str, scope: str = "") -> str:
    """Fetch the full archived transcript for a session (thaw cold storage).

    Args:
        session_id: Archived session ID (full or prefix match)
        scope: Optional scope filter when resolving ambiguous prefixes
    """
    try:
        options = MemorySessionGetOptions(session_id=session_id, scope=scope)
    except Exception:
        options = MemorySessionGetOptions(session_id=str(session_id))

    conn = db.get_db()
    matches = db.find_session_matches(conn, options.session_id, options.scope)
    if len(matches) > 1:
        lines = [
            f"- {row['session_id'][:8]} [{row['scope']}] {str(row['archived_at'])[:10]}"
            for row in matches
        ]
        conn.close()
        return (
            f"Archived session prefix '{options.session_id}' is ambiguous; "
            "pass a longer ID or scope.\n"
            + "\n".join(lines)
        )
    resolved = matches[0]["session_id"] if matches else None
    if not resolved:
        conn.close()
        return f"No archived session found matching '{options.session_id}'."

    params: list = [resolved]
    scope_filter = ""
    if options.scope:
        scope_filter = " AND scope = ?"
        params.append(options.scope)

    row = conn.execute(
        f"""
        SELECT session_id, scope, archived_at, archive_path
        FROM session_archive
        WHERE session_id = ?{scope_filter}
        """,
        params,
    ).fetchone()
    conn.close()

    if not row:
        return f"No archived session found matching '{options.session_id}'."

    transcript = db.read_archive_transcript(str(row["archive_path"]))
    if not transcript.strip():
        return (
            f"Session {row['session_id'][:8]} [{row['scope']}] "
            f"archived {str(row['archived_at'])[:10]} — transcript empty or unreadable."
        )

    header = (
        f"Session {row['session_id']} [{row['scope']}] "
        f"archived {row['archived_at']}\n"
        f"Source: {row['archive_path']}\n\n"
    )
    return header + transcript


def _apply_consolidation_actions(
    conn: sqlite3.Connection, result: ConsolidationResult, allowed_ids: set[str]
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    applied = 0

    for action in result.actions:
        try:
            if (
                action.type == "merge"
                and action.keep_id
                and action.delete_id
                and action.new_content
                and action.keep_id in allowed_ids
                and action.delete_id in allowed_ids
            ):
                update_cursor = conn.execute(
                    "UPDATE memories SET content = ?, updated_at = ? WHERE id = ?",
                    (action.new_content, now, action.keep_id),
                )
                delete_cursor = conn.execute(
                    "DELETE FROM memories WHERE id = ?", (action.delete_id,)
                )
                if update_cursor.rowcount and delete_cursor.rowcount:
                    db.delete_fts_memory(conn, action.delete_id)
                    db.upsert_fts_memory(conn, action.keep_id, action.new_content, "")
                    allowed_ids.discard(action.delete_id)
                    applied += 1
            elif action.type == "delete" and action.id and action.id in allowed_ids:
                cursor = conn.execute("DELETE FROM memories WHERE id = ?", (action.id,))
                if cursor.rowcount:
                    db.delete_fts_memory(conn, action.id)
                    allowed_ids.discard(action.id)
                    applied += 1
            elif (
                action.type == "update"
                and action.id
                and action.new_content
                and action.id in allowed_ids
            ):
                cursor = conn.execute(
                    "UPDATE memories SET content = ?, updated_at = ? WHERE id = ?",
                    (action.new_content, now, action.id),
                )
                if cursor.rowcount:
                    db.upsert_fts_memory(conn, action.id, action.new_content, "")
                    applied += 1
        except Exception:
            continue
    return applied



@mcp.tool()
def memory_hot_read(scope: str = "global") -> str:
    """Read the bounded hot memory file for a scope.

    Args:
        scope: Project scope name or global
    """
    text = hot.read_hot(scope)
    if not text.strip():
        return f"No hot memory for scope '{scope}'."
    return text


@mcp.tool()
def memory_hot_edit(
    scope: str,
    operation: str,
    content: str,
    target: str = "",
) -> str:
    """Edit bounded hot memory for a scope (Hermes-style add/replace/remove).

    Args:
        scope: Project scope name or global
        operation: add, replace, or remove
        content: Text to add or replacement content
        target: Substring to replace or remove; omit to replace entire file
    """
    return hot.edit_hot(scope, operation, content, target)


@mcp.tool()
def memory_consolidate(scope: str, apply: bool = False) -> str:
    """Consolidate memories in a scope: merge duplicates, update stale info, remove obsolete entries.

    Args:
        scope: Which scope to consolidate (e.g. 'global', 'arduino')
        apply: When False (default), write a proposal file only. When True, backup then apply actions.
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
        summary = result.summary or "nothing to consolidate"
        if not apply:
            proposal_path = db.write_proposal(
                scope, {"actions": [], "summary": summary}
            )
            if proposal_path:
                return f"Scope '{scope}': {summary} (proposal: {proposal_path})"
            return f"Scope '{scope}': {summary}"
        return f"Scope '{scope}': {summary}"

    if not apply:
        proposal_path = db.write_proposal(
            scope,
            {
                "actions": [action.model_dump() for action in result.actions],
                "summary": result.summary,
            },
        )
        if proposal_path:
            return (
                f"Proposal written to {proposal_path}. "
                f"{len(result.actions)} proposed actions. {result.summary}"
            )
        return (
            f"Dry run for scope '{scope}': {len(result.actions)} proposed actions. "
            f"{result.summary}"
        )

    backup_path = db.write_backup_snapshot(scope)
    conn = db.get_db()
    lifecycle_changed = db.apply_lifecycle_transitions(conn, scope)
    applied = _apply_consolidation_actions(conn, result, {m.id for m in memories})
    conn.commit()
    conn.close()

    backup_note = f" Backup: {backup_path}." if backup_path else ""
    lifecycle_note = ""
    if lifecycle_changed:
        lifecycle_note = f" Lifecycle transitions: {lifecycle_changed}."
    return (
        f"Consolidated scope '{scope}': {applied} actions applied."
        f"{lifecycle_note}{backup_note} {result.summary}"
    )
