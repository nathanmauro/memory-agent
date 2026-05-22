"""Database helpers for the memory agent."""

import json
import os
import re
import shutil
import sqlite3
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

DEFAULT_MEMORY_HOME = os.path.join(os.path.expanduser("~"), ".claude", "memory")
DB_DIR = os.path.abspath(
    os.path.expanduser(os.environ.get("MEMORY_AGENT_HOME", DEFAULT_MEMORY_HOME))
)
DB_PATH = os.path.join(DB_DIR, "memory.db")
SESSIONS_DIR = os.path.join(DB_DIR, "sessions")
ARCHIVE_DIR = os.path.join(DB_DIR, "archive")
ARCHIVE_SESSIONS_DIR = os.path.join(ARCHIVE_DIR, "sessions")
HOT_DIR = os.path.join(DB_DIR, "hot")
PROPOSALS_DIR = os.path.join(DB_DIR, "proposals")
BACKUPS_DIR = os.path.join(DB_DIR, "backups")
CURATOR_LAST_RUN_PATH = os.path.join(DB_DIR, "curator_last_run.txt")
STALE_AFTER_DAYS = 30
ARCHIVE_AFTER_DAYS = 90
CURATOR_INTERVAL_DAYS = 7
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


def configure_paths(memory_home: str) -> None:
    global DB_DIR, DB_PATH, SESSIONS_DIR
    global ARCHIVE_DIR, ARCHIVE_SESSIONS_DIR, HOT_DIR
    global PROPOSALS_DIR, BACKUPS_DIR, CURATOR_LAST_RUN_PATH
    DB_DIR = os.path.abspath(os.path.expanduser(memory_home or DEFAULT_MEMORY_HOME))
    DB_PATH = os.path.join(DB_DIR, "memory.db")
    SESSIONS_DIR = os.path.join(DB_DIR, "sessions")
    ARCHIVE_DIR = os.path.join(DB_DIR, "archive")
    ARCHIVE_SESSIONS_DIR = os.path.join(ARCHIVE_DIR, "sessions")
    HOT_DIR = os.path.join(DB_DIR, "hot")
    PROPOSALS_DIR = os.path.join(DB_DIR, "proposals")
    BACKUPS_DIR = os.path.join(DB_DIR, "backups")
    CURATOR_LAST_RUN_PATH = os.path.join(DB_DIR, "curator_last_run.txt")


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_column(
    conn: sqlite3.Connection, table: str, column: str, definition: str
) -> None:
    cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _safe_path_component(value: str) -> str:
    safe = "".join(c for c in value if c.isalnum() or c in "-_")
    return safe or "global"


def init_db() -> None:
    os.makedirs(DB_DIR, exist_ok=True)
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    os.makedirs(ARCHIVE_SESSIONS_DIR, exist_ok=True)
    os.makedirs(HOT_DIR, exist_ok=True)
    os.makedirs(PROPOSALS_DIR, exist_ok=True)
    os.makedirs(BACKUPS_DIR, exist_ok=True)
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

        CREATE TABLE IF NOT EXISTS session_archive (
            session_id TEXT PRIMARY KEY,
            scope TEXT NOT NULL,
            archived_at TEXT NOT NULL,
            archive_path TEXT NOT NULL,
            index_text TEXT NOT NULL DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_session_archive_scope ON session_archive(scope);
        CREATE INDEX IF NOT EXISTS idx_session_archive_archived_at ON session_archive(archived_at);
    """)
    try:
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
            USING fts5(id UNINDEXED, content, tags)
        """)
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS session_archive_fts
            USING fts5(
                session_id UNINDEXED,
                scope UNINDEXED,
                archived_at UNINDEXED,
                content
            )
        """)
    except sqlite3.OperationalError:
        pass
    _ensure_column(conn, "memories", "status", "TEXT NOT NULL DEFAULT 'active'")
    _ensure_column(conn, "memories", "last_accessed_at", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "memories", "access_count", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "memories", "pinned", "INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    conn.close()


def build_memory_filters(
    scope: str = "", category: str = "", alias: str = "", status: str = ""
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
    if status:
        conditions.append(f"{prefix}status = ?")
        params.append(status)
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


def resolve_session_id(conn: sqlite3.Connection, id_or_prefix: str) -> str | None:
    id_or_prefix = (id_or_prefix or "").strip()
    if not id_or_prefix:
        return None
    row = conn.execute(
        "SELECT session_id FROM session_archive WHERE session_id = ?",
        (id_or_prefix,),
    ).fetchone()
    if row:
        return row["session_id"]
    row = conn.execute(
        "SELECT session_id FROM session_archive WHERE session_id LIKE ? LIMIT 1",
        (f"{id_or_prefix}%",),
    ).fetchone()
    return row["session_id"] if row else None


def format_archive_event(obj: dict) -> str:
    kind = str(obj.get("kind", ""))
    ts = str(obj.get("ts", ""))[:19]
    data = obj.get("data", {}) if isinstance(obj.get("data"), dict) else {}
    prefix = f"[{ts}] " if ts else ""
    if kind == "prompt":
        return f"{prefix}USER: {str(data.get('prompt', ''))[:800]}"
    if kind == "tool_use":
        tname = data.get("tool_name", "?")
        try:
            tinput = json.dumps(data.get("tool_input", {}))[:500]
        except Exception:
            tinput = ""
        try:
            tresp = json.dumps(data.get("tool_response", {}))[:500]
        except Exception:
            tresp = ""
        line = f"{prefix}TOOL {tname}: {tinput}"
        if tresp and tresp not in ("{}", "null"):
            line += f"\n  → {tresp}"
        return line
    return f"{prefix}{kind}: {json.dumps(data)[:400]}"


def read_archive_transcript(archive_path: str, limit: int = 500) -> str:
    if not archive_path or not os.path.exists(archive_path):
        return ""
    lines = []
    try:
        with open(archive_path) as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except Exception:
                    lines.append(raw[:400])
                    continue
                if isinstance(obj, dict):
                    lines.append(format_archive_event(obj))
                if len(lines) >= limit:
                    break
    except Exception:
        return ""
    return "\n".join(lines)


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


def archive_session_path(scope: str, session_id: str) -> str:
    scope_dir = os.path.join(ARCHIVE_SESSIONS_DIR, _safe_path_component(scope))
    os.makedirs(scope_dir, exist_ok=True)
    safe_id = _safe_path_component(session_id)
    return os.path.join(scope_dir, f"{safe_id}.jsonl")


def flatten_archive_line(line: str) -> str:
    line = line.strip()
    if not line:
        return ""
    try:
        obj = json.loads(line)
    except Exception:
        return line[:400]
    if not isinstance(obj, dict):
        return str(obj)[:400]
    kind = str(obj.get("kind", ""))
    data = obj.get("data", {})
    parts = [kind]
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, str):
                parts.append(value[:300])
            else:
                try:
                    parts.append(json.dumps(value)[:300])
                except Exception:
                    parts.append(str(value)[:300])
    return " ".join(part for part in parts if part)


def build_archive_index_text(archive_path: str) -> str:
    lines = []
    try:
        with open(archive_path) as f:
            for raw in f:
                flat = flatten_archive_line(raw)
                if flat:
                    lines.append(flat)
    except Exception:
        return ""
    return "\n".join(lines)


def delete_session_archive_fts(conn: sqlite3.Connection, session_id: str) -> None:
    try:
        conn.execute(
            "DELETE FROM session_archive_fts WHERE session_id = ?", (session_id,)
        )
    except sqlite3.OperationalError:
        pass


def upsert_session_archive_index(
    conn: sqlite3.Connection,
    session_id: str,
    scope: str,
    archived_at: str,
    archive_path: str,
    index_text: str,
) -> None:
    conn.execute(
        """
        INSERT INTO session_archive (session_id, scope, archived_at, archive_path, index_text)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            scope = excluded.scope,
            archived_at = excluded.archived_at,
            archive_path = excluded.archive_path,
            index_text = excluded.index_text
        """,
        (session_id, scope, archived_at, archive_path, index_text),
    )
    delete_session_archive_fts(conn, session_id)
    try:
        conn.execute(
            """
            INSERT INTO session_archive_fts (session_id, scope, archived_at, content)
            VALUES (?, ?, ?, ?)
            """,
            (session_id, scope, archived_at, index_text),
        )
    except sqlite3.OperationalError:
        pass


def archive_session_buffer(
    buffer_path: str, scope: str, session_id: str | None = None
) -> str | None:
    if not os.path.exists(buffer_path):
        return None
    if not session_id:
        session_id = os.path.basename(buffer_path).replace(".jsonl", "")
    if not session_id:
        return None

    archive_path = archive_session_path(scope, session_id)
    try:
        os.replace(buffer_path, archive_path)
    except Exception:
        try:
            shutil.copy2(buffer_path, archive_path)
            os.remove(buffer_path)
        except Exception:
            return None

    archived_at = datetime.now(timezone.utc).isoformat()
    index_text = build_archive_index_text(archive_path)
    conn = get_db()
    try:
        upsert_session_archive_index(
            conn, session_id, scope, archived_at, archive_path, index_text
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()
    return archive_path


def list_recent_archived_sessions(
    conn: sqlite3.Connection, scope: str, limit: int = 10
) -> list[sqlite3.Row]:
    try:
        rows = conn.execute(
            """
            SELECT session_id, scope, archived_at, archive_path
            FROM session_archive
            WHERE scope = ?
            ORDER BY archived_at DESC
            LIMIT ?
            """,
            (scope, max(1, min(limit, 50))),
        ).fetchall()
        return list(rows)
    except Exception:
        return []


def search_session_archive(
    conn: sqlite3.Connection, query: str, scope: str = "", limit: int = 10
) -> list[dict]:
    limit = max(1, min(limit, 50))
    hits: list[dict] = []
    seen: set[str] = set()

    try:
        words = extract_search_terms(query, 10)
        if words:
            fts_query = " OR ".join(words)
            scope_filter = ""
            params: list = [fts_query]
            if scope:
                scope_filter = " AND scope = ?"
                params.append(scope)
            rows = conn.execute(
                f"""
                SELECT session_id, scope, archived_at, content
                FROM session_archive_fts
                WHERE session_archive_fts MATCH ?{scope_filter}
                ORDER BY rank
                LIMIT ?
                """,
                params + [limit],
            ).fetchall()
            for row in rows:
                sid = row["session_id"]
                if sid in seen:
                    continue
                seen.add(sid)
                snippet = str(row["content"]).replace("\n", " ")[:200]
                hits.append(
                    {
                        "session_id": sid,
                        "scope": row["scope"],
                        "archived_at": row["archived_at"],
                        "snippet": snippet,
                    }
                )
    except sqlite3.OperationalError:
        pass

    if len(hits) < limit:
        like_words = extract_search_terms(query, 5)
        conditions = []
        params = []
        if scope:
            conditions.append("scope = ?")
            params.append(scope)
        for word in like_words:
            conditions.append("index_text LIKE ?")
            params.append(f"%{word}%")
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        rows = conn.execute(
            f"""
            SELECT session_id, scope, archived_at, index_text
            FROM session_archive
            {where}
            ORDER BY archived_at DESC
            LIMIT ?
            """,
            params + [limit],
        ).fetchall()
        for row in rows:
            sid = row["session_id"]
            if sid in seen:
                continue
            seen.add(sid)
            snippet = str(row["index_text"]).replace("\n", " ")[:200]
            hits.append(
                {
                    "session_id": sid,
                    "scope": row["scope"],
                    "archived_at": row["archived_at"],
                    "snippet": snippet,
                }
            )
            if len(hits) >= limit:
                break
    return hits[:limit]


def apply_lifecycle_transitions(conn: sqlite3.Connection, scope: str) -> int:
    now = datetime.now(timezone.utc)
    stale_cutoff = (now - timedelta(days=STALE_AFTER_DAYS)).isoformat()
    archive_cutoff = (now - timedelta(days=ARCHIVE_AFTER_DAYS)).isoformat()
    changed = 0

    try:
        cur = conn.execute(
            """
            UPDATE memories
            SET status = 'stale'
            WHERE scope = ?
              AND status = 'active'
              AND pinned = 0
              AND importance < 4
              AND (
                    (last_accessed_at != '' AND last_accessed_at < ?)
                 OR (last_accessed_at = '' AND updated_at < ?)
              )
            """,
            (scope, stale_cutoff, stale_cutoff),
        )
        changed += cur.rowcount
    except Exception:
        pass

    try:
        cur = conn.execute(
            """
            UPDATE memories
            SET status = 'archived'
            WHERE scope = ?
              AND status = 'stale'
              AND pinned = 0
              AND (
                    (last_accessed_at != '' AND last_accessed_at < ?)
                 OR (last_accessed_at = '' AND updated_at < ?)
              )
            """,
            (scope, archive_cutoff, archive_cutoff),
        )
        changed += cur.rowcount
    except Exception:
        pass

    return changed


def write_backup_snapshot(scope: str) -> str:
    os.makedirs(BACKUPS_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(BACKUPS_DIR, f"{_safe_path_component(scope)}-{stamp}.json")
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM memories WHERE scope = ? ORDER BY updated_at DESC",
            (scope,),
        ).fetchall()
        payload = {
            "scope": scope,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "memories": [dict(row) for row in rows],
        }
    finally:
        conn.close()
    try:
        with open(path, "w") as f:
            json.dump(payload, f, indent=2)
    except Exception:
        return ""
    return path


def write_proposal(scope: str, proposal: dict) -> str:
    os.makedirs(PROPOSALS_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(PROPOSALS_DIR, f"{_safe_path_component(scope)}-{stamp}.json")
    payload = {
        "scope": scope,
        "created_at": datetime.now(timezone.utc).isoformat(),
        **proposal,
    }
    try:
        with open(path, "w") as f:
            json.dump(payload, f, indent=2)
    except Exception:
        return ""
    return path


def read_curator_last_run() -> str:
    try:
        with open(CURATOR_LAST_RUN_PATH) as f:
            return f.read().strip()
    except Exception:
        return ""


def write_curator_last_run(iso: str) -> None:
    try:
        with open(CURATOR_LAST_RUN_PATH, "w") as f:
            f.write(iso)
    except Exception:
        pass


def curator_due() -> bool:
    last = read_curator_last_run()
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
    except Exception:
        return True
    return (datetime.now(timezone.utc) - last_dt) >= timedelta(days=CURATOR_INTERVAL_DAYS)


def list_memory_scopes(conn: sqlite3.Connection) -> list[str]:
    try:
        rows = conn.execute(
            "SELECT DISTINCT scope FROM memories ORDER BY scope"
        ).fetchall()
        return [str(row["scope"]) for row in rows if row["scope"]]
    except Exception:
        return []

