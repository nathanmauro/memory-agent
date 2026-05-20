"""Database helpers for the memory agent."""

import os
import re
import sqlite3
import time
from collections.abc import Iterator

DB_DIR = os.path.join(os.path.expanduser("~"), ".claude", "memory")
DB_PATH = os.path.join(DB_DIR, "memory.db")
SESSIONS_DIR = os.path.join(DB_DIR, "sessions")
STOP_WORDS = {
    "the",
    "a",
    "an",
    "is",
    "are",
    "was",
    "were",
    "do",
    "does",
    "did",
    "has",
    "have",
    "had",
    "what",
    "which",
    "who",
    "whom",
    "this",
    "that",
    "these",
    "those",
    "how",
    "when",
    "where",
    "why",
    "can",
    "could",
    "will",
    "would",
    "should",
    "may",
    "might",
    "shall",
    "for",
    "and",
    "but",
    "or",
    "not",
    "no",
    "with",
    "from",
    "about",
    "into",
    "over",
    "after",
    "before",
}


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    os.makedirs(DB_DIR, exist_ok=True)
    os.makedirs(SESSIONS_DIR, exist_ok=True)
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
    try:
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
            USING fts5(id UNINDEXED, content, tags)
        """)
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


def build_memory_filters(
    scope: str = "", category: str = "", alias: str = ""
) -> tuple[list[str], list]:
    prefix = f"{alias}." if alias else ""
    conditions = []
    params = []
    if scope:
        conditions.append(f"{prefix}scope = ?")
        params.append(scope)
    if category:
        conditions.append(f"{prefix}category = ?")
        params.append(category)
    return conditions, params


def extract_search_terms(query: str, limit: int) -> list[str]:
    words = [re.sub(r"[^\w]", "", word).lower() for word in query.split()[:10]]
    return [word for word in words if len(word) > 2 and word not in STOP_WORDS][:limit]


def delete_fts_memory(conn: sqlite3.Connection, mem_id: str) -> None:
    try:
        conn.execute("DELETE FROM memories_fts WHERE id = ?", (mem_id,))
    except sqlite3.OperationalError:
        pass


def upsert_fts_memory(
    conn: sqlite3.Connection, mem_id: str, content: str, tags: str
) -> None:
    try:
        conn.execute("DELETE FROM memories_fts WHERE id = ?", (mem_id,))
        conn.execute(
            "INSERT INTO memories_fts (id, content, tags) VALUES (?, ?, ?)",
            (mem_id, content, tags),
        )
    except sqlite3.OperationalError:
        pass


def resolve_memory_id(conn: sqlite3.Connection, id_or_prefix: str) -> str | None:
    id_or_prefix = (id_or_prefix or "").strip()
    if not id_or_prefix:
        return None
    if len(id_or_prefix) >= 36:
        row = conn.execute(
            "SELECT id FROM memories WHERE id = ?", (id_or_prefix,)
        ).fetchone()
        return row["id"] if row else None
    row = conn.execute(
        "SELECT id FROM memories WHERE id LIKE ? LIMIT 1", (f"{id_or_prefix}%",)
    ).fetchone()
    return row["id"] if row else None


def iter_session_buffers(stale_seconds: int) -> Iterator[str]:
    if not os.path.isdir(SESSIONS_DIR):
        return
    cutoff = time.time() - max(0, stale_seconds)
    for name in os.listdir(SESSIONS_DIR):
        if not name.endswith(".jsonl"):
            continue
        path = os.path.join(SESSIONS_DIR, name)
        try:
            if os.path.getmtime(path) < cutoff:
                yield path
        except OSError:
            continue
