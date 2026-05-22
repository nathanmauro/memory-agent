"""
Local validation tests for pydantic-backed parsing and cleanup helpers.
Run: python -m tests.test_validation  (or `pytest tests/test_validation.py`)
"""

import json
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone

from mcp_memory_agent import db, hook_handler, hot, llm, tools

PASS = 0
FAIL = 0
TEST_ROOT = os.path.join(tempfile.gettempdir(), "memory_validation_test")
TEST_DB = os.path.join(TEST_ROOT, "memory.db")
TEST_SESSIONS = os.path.join(TEST_ROOT, "sessions")
TEST_HOT = os.path.join(TEST_ROOT, "hot")
TEST_PROPOSALS = os.path.join(TEST_ROOT, "proposals")
TEST_BACKUPS = os.path.join(TEST_ROOT, "backups")
TEST_CURATOR_LAST_RUN = os.path.join(TEST_ROOT, "curator_last_run.txt")
ORIGINAL_DB_PATH = db.DB_PATH
ORIGINAL_SESSIONS_DIR = db.SESSIONS_DIR
ORIGINAL_HOT_DIR = db.HOT_DIR
ORIGINAL_PROPOSALS_DIR = db.PROPOSALS_DIR
ORIGINAL_BACKUPS_DIR = db.BACKUPS_DIR
ORIGINAL_CURATOR_LAST_RUN_PATH = db.CURATOR_LAST_RUN_PATH
ORIGINAL_LLM_CALL = llm.llm_call


def ok(name: str) -> None:
    global PASS
    PASS += 1
    print(f"  PASS  {name}")


def fail(name: str, reason: str) -> None:
    global FAIL
    FAIL += 1
    print(f"  FAIL  {name}: {reason}")


def reset_db() -> None:
    shutil.rmtree(TEST_ROOT, ignore_errors=True)
    os.makedirs(TEST_ROOT, exist_ok=True)
    db.DB_PATH = TEST_DB
    db.SESSIONS_DIR = TEST_SESSIONS
    db.HOT_DIR = TEST_HOT
    db.PROPOSALS_DIR = TEST_PROPOSALS
    db.BACKUPS_DIR = TEST_BACKUPS
    db.CURATOR_LAST_RUN_PATH = TEST_CURATOR_LAST_RUN
    db.init_db()


def restore_state() -> None:
    db.DB_PATH = ORIGINAL_DB_PATH
    db.SESSIONS_DIR = ORIGINAL_SESSIONS_DIR
    db.HOT_DIR = ORIGINAL_HOT_DIR
    db.PROPOSALS_DIR = ORIGINAL_PROPOSALS_DIR
    db.BACKUPS_DIR = ORIGINAL_BACKUPS_DIR
    db.CURATOR_LAST_RUN_PATH = ORIGINAL_CURATOR_LAST_RUN_PATH
    llm.llm_call = ORIGINAL_LLM_CALL
    shutil.rmtree(TEST_ROOT, ignore_errors=True)


def insert_memory(
    mem_id: str,
    content: str,
    scope: str = "__validation__",
    category: str = "project_knowledge",
    importance: int = 3,
    updated_at: str = "",
) -> None:
    now = updated_at or datetime.now(timezone.utc).isoformat()
    conn = db.get_db()
    conn.execute(
        "INSERT INTO memories (id, content, category, scope, tags, created_at, updated_at, source, importance) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (mem_id, content, category, scope, "", now, now, "test", importance),
    )
    conn.commit()
    conn.close()


def test_extract_memory_metadata_defaults() -> None:
    llm.llm_call = lambda system, user: "not json"
    meta = llm.extract_memory_metadata("test", "global", [])
    if (
        meta.category == "project_knowledge"
        and meta.importance == 3
        and meta.tags == ""
    ):
        ok("extract_memory_metadata_defaults")
    else:
        fail(
            "extract_memory_metadata_defaults",
            f"Unexpected metadata: {meta.model_dump()}",
        )


def test_extract_memory_metadata_normalizes_values() -> None:
    payload = {
        "category": "USER_PREFERENCE",
        "tags": " Python, python, AWS ",
        "importance": 99,
        "merge_with_id": "abc123",
        "merged_content": "   ",
    }
    llm.llm_call = lambda system, user: json.dumps(payload)
    meta = llm.extract_memory_metadata("test", "global", [])
    if (
        meta.category == "user_preference"
        and meta.tags == "python,aws"
        and meta.importance == 5
    ):
        if meta.merge_with_id is None and meta.merged_content is None:
            ok("extract_memory_metadata_normalizes_values")
            return
    fail(
        "extract_memory_metadata_normalizes_values",
        f"Unexpected metadata: {meta.model_dump()}",
    )


def test_memory_query_clamps_limit() -> None:
    reset_db()
    for idx in range(3):
        insert_memory(
            f"00000000-0000-0000-0000-00000000000{idx}", f"database memory {idx}"
        )
    result = tools.memory_query("database", scope="__validation__", limit=0)
    if result.startswith("Found 1 memories"):
        ok("memory_query_clamps_limit")
    else:
        fail("memory_query_clamps_limit", f"Unexpected result: {result}")


def test_memory_consolidate_ignores_invalid_actions() -> None:
    reset_db()
    insert_memory("11111111-1111-1111-1111-111111111111", "old content")
    payload = {
        "actions": [
            {"type": "noop", "id": "11111111-1111-1111-1111-111111111111"},
            {
                "type": "update",
                "id": "11111111-1111-1111-1111-111111111111",
                "new_content": "new content",
            },
        ],
        "summary": "updated one memory",
    }
    llm.llm_call = lambda system, user: json.dumps(payload)
    result = tools.memory_consolidate("__validation__", apply=True)
    conn = db.get_db()
    row = conn.execute(
        "SELECT content FROM memories WHERE id = ?",
        ("11111111-1111-1111-1111-111111111111",),
    ).fetchone()
    conn.close()
    if "1 actions applied" in result and row and row["content"] == "new content":
        ok("memory_consolidate_ignores_invalid_actions")
    else:
        fail(
            "memory_consolidate_ignores_invalid_actions", f"Unexpected result: {result}"
        )


def test_memory_index_no_llm_call() -> None:
    reset_db()
    insert_memory("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "database migration plan")
    insert_memory("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "database indexes notes")
    calls = {"count": 0}

    def spy(system, user):
        calls["count"] += 1
        return ""

    llm.llm_call = spy
    result = tools.memory_index("database", scope="__validation__", limit=5)
    if calls["count"] == 0 and "2 hits" in result and "aaaaaaaa" in result:
        ok("memory_index_no_llm_call")
    else:
        fail(
            "memory_index_no_llm_call",
            f"calls={calls['count']} result={result[:120]}",
        )


def test_memory_get_full_and_prefix() -> None:
    reset_db()
    full_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    insert_memory(full_id, "hello world")
    by_full = tools.memory_get([full_id])
    by_prefix = tools.memory_get(["cccccccc"])
    by_missing = tools.memory_get(["deadbeef"])
    if (
        "hello world" in by_full
        and "hello world" in by_prefix
        and by_missing == "No memories found."
    ):
        ok("memory_get_full_and_prefix")
    else:
        fail(
            "memory_get_full_and_prefix",
            f"full={by_full[:80]} prefix={by_prefix[:80]} missing={by_missing!r}",
        )


def test_memory_timeline_orders_and_filters() -> None:
    reset_db()
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    for idx in range(3):
        ts = (base + timedelta(days=idx)).isoformat()
        insert_memory(
            f"dddddddd-dddd-dddd-dddd-dddddddddd0{idx}",
            f"event {idx}",
            updated_at=ts,
        )
    insert_memory(
        "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
        "other-scope event",
        scope="__other__",
        updated_at=(base + timedelta(days=10)).isoformat(),
    )
    desc = tools.memory_timeline(scope="__validation__", limit=10)
    asc = tools.memory_timeline(
        scope="__validation__",
        after_iso=(base - timedelta(days=1)).isoformat(),
        limit=10,
    )
    cross_scope = tools.memory_timeline(limit=10)

    desc_lines = desc.splitlines()
    asc_lines = asc.splitlines()
    desc_order_ok = desc_lines.index("  event 2") < desc_lines.index("  event 0")
    asc_order_ok = asc_lines.index("  event 0") < asc_lines.index("  event 2")
    scope_isolated = "other-scope event" not in desc
    cross_scope_includes = "other-scope event" in cross_scope

    if desc_order_ok and asc_order_ok and scope_isolated and cross_scope_includes:
        ok("memory_timeline_orders_and_filters")
    else:
        fail(
            "memory_timeline_orders_and_filters",
            f"desc_ok={desc_order_ok} asc_ok={asc_order_ok} "
            f"isolated={scope_isolated} cross={cross_scope_includes}",
        )


def _write_summarize_buffer(session_id: str, event_count: int = 4) -> str:
    buffer_path = os.path.join(db.SESSIONS_DIR, f"{session_id}.jsonl")
    os.makedirs(db.SESSIONS_DIR, exist_ok=True)
    with open(buffer_path, "w") as f:
        for i in range(event_count):
            event = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "kind": "prompt" if i % 2 == 0 else "tool_use",
                "scope": "__validation__",
                "data": {"prompt": f"do thing {i}"}
                if i % 2 == 0
                else {"tool_name": "Bash", "tool_input": {"cmd": f"cmd {i}"}},
            }
            f.write(json.dumps(event) + "\n")
    return buffer_path


def test_hook_summarize_inserts_memory() -> None:
    reset_db()
    session_id = "test-session-001"
    buffer_path = _write_summarize_buffer(session_id)

    summary_payload = {
        "session_summary": "User implemented hook auto-capture",
        "memories": ["Use SQLite + FTS5, no embeddings"],
        "open_actions": [],
    }
    llm.llm_call = lambda system, user: json.dumps(summary_payload)

    hook_handler._summarize_buffer(buffer_path, fallback_scope="__validation__")

    buffer_gone = not os.path.exists(buffer_path)
    archive_path = db.archive_session_path("__validation__", session_id)
    archive_exists = os.path.exists(archive_path)
    conn = db.get_db()
    rows = conn.execute(
        "SELECT content FROM memories WHERE scope = ? ORDER BY content",
        ("__validation__",),
    ).fetchall()
    archive_rows = conn.execute(
        "SELECT session_id FROM session_archive WHERE session_id = ?",
        (session_id,),
    ).fetchall()
    conn.close()
    contents = [r["content"] for r in rows]

    if (
        buffer_gone
        and archive_exists
        and archive_rows
        and "User implemented hook auto-capture" in contents
        and "Use SQLite + FTS5, no embeddings" in contents
    ):
        ok("hook_summarize_inserts_memory")
    else:
        fail(
            "hook_summarize_inserts_memory",
            f"buffer_gone={buffer_gone} archive={archive_exists} "
            f"indexed={bool(archive_rows)} contents={contents}",
        )


def test_hook_summarize_stores_open_actions() -> None:
    reset_db()
    session_id = "test-session-open-actions"
    buffer_path = _write_summarize_buffer(session_id)

    summary_payload = {
        "session_summary": "Refactored summarizer",
        "memories": [],
        "open_actions": [
            "Add integration tests for open_action extraction",
            "Update README category list",
        ],
    }
    llm.llm_call = lambda system, user: json.dumps(summary_payload)

    hook_handler._summarize_buffer(buffer_path, fallback_scope="__validation__")

    conn = db.get_db()
    rows = conn.execute(
        """
        SELECT content, category FROM memories
        WHERE scope = ? AND category = 'open_action'
        ORDER BY content
        """,
        ("__validation__",),
    ).fetchall()
    conn.close()
    contents = [r["content"] for r in rows]

    if (
        len(contents) == 2
        and "Add integration tests for open_action extraction" in contents
        and "Update README category list" in contents
    ):
        ok("hook_summarize_stores_open_actions")
    else:
        fail(
            "hook_summarize_stores_open_actions",
            f"open_action rows={[(r['content'], r['category']) for r in rows]}",
        )


def test_hook_summarize_handles_noisy_open_actions() -> None:
    reset_db()
    session_id = "test-session-noisy-actions"
    buffer_path = _write_summarize_buffer(session_id)

    summary_payload = {
        "session_summary": "Worked on parser hardening",
        "memories": ["Keep defensive parsing"],
        "open_actions": "not-a-list",
    }
    llm.llm_call = lambda system, user: json.dumps(summary_payload)

    hook_handler._summarize_buffer(buffer_path, fallback_scope="__validation__")

    conn = db.get_db()
    open_rows = conn.execute(
        "SELECT COUNT(*) AS n FROM memories WHERE category = 'open_action'"
    ).fetchone()["n"]
    all_rows = conn.execute("SELECT content FROM memories ORDER BY content").fetchall()
    conn.close()
    contents = [r["content"] for r in all_rows]

    if (
        open_rows == 0
        and "Worked on parser hardening" in contents
        and "Keep defensive parsing" in contents
    ):
        ok("hook_summarize_handles_noisy_open_actions")
    else:
        fail(
            "hook_summarize_handles_noisy_open_actions",
            f"open_rows={open_rows} contents={contents}",
        )


def test_open_action_category_normalization() -> None:
    from mcp_memory_agent.models.types import _normalize_category

    if (
        _normalize_category("OPEN_ACTION") == "open_action"
        and _normalize_category("not_a_category") == "project_knowledge"
    ):
        ok("open_action_category_normalization")
    else:
        fail(
            "open_action_category_normalization",
            f"open={_normalize_category('OPEN_ACTION')} "
            f"invalid={_normalize_category('not_a_category')}",
        )


def test_hook_summarize_skips_short_buffer() -> None:
    reset_db()
    session_id = "test-session-002"
    buffer_path = os.path.join(db.SESSIONS_DIR, f"{session_id}.jsonl")
    os.makedirs(db.SESSIONS_DIR, exist_ok=True)
    with open(buffer_path, "w") as f:
        f.write(
            json.dumps({"ts": "x", "kind": "prompt", "scope": "x", "data": {}}) + "\n"
        )
    calls = {"count": 0}
    llm.llm_call = lambda s, u: (calls.update(count=calls["count"] + 1) or "{}")

    hook_handler._summarize_buffer(buffer_path, fallback_scope="__validation__")

    conn = db.get_db()
    n = conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"]
    conn.close()
    if calls["count"] == 0 and not os.path.exists(buffer_path) and n == 0:
        ok("hook_summarize_skips_short_buffer")
    else:
        fail(
            "hook_summarize_skips_short_buffer",
            f"calls={calls['count']} buffer={os.path.exists(buffer_path)} rows={n}",
        )


def test_hot_edit_add_replace_remove() -> None:
    reset_db()
    scope = "__validation__"

    result = tools.memory_hot_edit(scope, "add", "alpha line")
    if "Error" in result:
        fail("hot_edit_add", result)
        return
    ok("hot_edit_add")

    result = tools.memory_hot_edit(scope, "add", "beta line")
    if "Error" in result:
        fail("hot_edit_add_second", result)
        return
    ok("hot_edit_add_second")

    dup = tools.memory_hot_edit(scope, "add", "alpha line")
    if "duplicate" in dup.lower():
        ok("hot_edit_rejects_duplicate")
    else:
        fail("hot_edit_rejects_duplicate", dup)

    result = tools.memory_hot_edit(scope, "replace", "ALPHA", target="alpha")
    if "Error" in result:
        fail("hot_edit_replace", result)
        return
    ok("hot_edit_replace")

    read_back = tools.memory_hot_read(scope)
    if "ALPHA" in read_back and "beta line" in read_back:
        ok("hot_read_after_replace")
    else:
        fail("hot_read_after_replace", read_back)

    result = tools.memory_hot_edit(scope, "remove", "", target="beta line")
    if "Error" in result:
        fail("hot_edit_remove", result)
        return
    ok("hot_edit_remove")

    read_back = tools.memory_hot_read(scope)
    if "beta line" not in read_back and "ALPHA" in read_back:
        ok("hot_read_after_remove")
    else:
        fail("hot_read_after_remove", read_back)


def test_hot_edit_rejects_oversize() -> None:
    reset_db()
    scope = "__validation__"
    big = "x" * (hot.HOT_MAX_CHARS + 1)
    result = tools.memory_hot_edit(scope, "replace", big, target="")
    if "exceed" in result.lower():
        ok("hot_edit_rejects_oversize")
    else:
        fail("hot_edit_rejects_oversize", result[:120])


def test_inject_includes_hot_memory() -> None:
    reset_db()
    scope = "__validation__"
    marker = "unique-hot-marker-xyzzy"
    tools.memory_hot_edit(scope, "replace", marker, target="")
    insert_memory(
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "warm memory snippet for inject test",
        scope=scope,
        importance=5,
    )

    import io
    import contextlib

    payload = {"cwd": os.path.join(TEST_ROOT, scope)}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        hook_handler._inject_context(payload)
    raw = buf.getvalue().strip()
    try:
        out = json.loads(raw)
        ctx = out["hookSpecificOutput"]["additionalContext"]
    except Exception as e:
        fail("inject_includes_hot_memory", f"bad json: {e} raw={raw[:200]}")
        return

    if marker in ctx and "warm memory snippet" in ctx:
        ok("inject_includes_hot_memory")
    elif marker in ctx:
        ok("inject_includes_hot_memory")
        fail("inject_includes_warm_memory", ctx[:300])
    else:
        fail("inject_includes_hot_memory", ctx[:300])


def test_memory_consolidate_dry_run_by_default() -> None:
    reset_db()
    insert_memory("11111111-1111-1111-1111-111111111111", "old content")
    payload = {
        "actions": [
            {
                "type": "update",
                "id": "11111111-1111-1111-1111-111111111111",
                "new_content": "new content",
            },
        ],
        "summary": "proposed update",
    }
    llm.llm_call = lambda system, user: json.dumps(payload)
    result = tools.memory_consolidate("__validation__")
    conn = db.get_db()
    row = conn.execute(
        "SELECT content FROM memories WHERE id = ?",
        ("11111111-1111-1111-1111-111111111111",),
    ).fetchone()
    conn.close()
    proposal_files = os.listdir(TEST_PROPOSALS) if os.path.isdir(TEST_PROPOSALS) else []
    if (
        row
        and row["content"] == "old content"
        and "Proposal written" in result
        and proposal_files
    ):
        ok("memory_consolidate_dry_run_by_default")
    else:
        fail(
            "memory_consolidate_dry_run_by_default",
            f"result={result} content={row['content'] if row else None} "
            f"proposals={proposal_files}",
        )


def test_curator_skips_within_interval() -> None:
    reset_db()
    db.write_curator_last_run(datetime.now(timezone.utc).isoformat())
    calls = {"count": 0}
    llm.llm_call = lambda s, u: (calls.update(count=calls["count"] + 1) or "{}")

    hook_handler._curator({"cwd": TEST_ROOT})

    if calls["count"] == 0:
        ok("curator_skips_within_interval")
    else:
        fail("curator_skips_within_interval", f"llm calls={calls['count']}")


def test_curator_runs_lifecycle_and_proposal() -> None:
    reset_db()
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    conn = db.get_db()
    conn.execute(
        """
        INSERT INTO memories (
            id, content, category, scope, tags, created_at, updated_at,
            source, importance, status, last_accessed_at, access_count, pinned
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "99999999-9999-9999-9999-999999999999",
            "stale candidate",
            "project_knowledge",
            "__validation__",
            "",
            old,
            old,
            "test",
            2,
            "active",
            "",
            0,
            0,
        ),
    )
    conn.commit()
    conn.close()

    payload = {"actions": [], "summary": "all clean"}
    llm.llm_call = lambda system, user: json.dumps(payload)

    hook_handler._curator({"cwd": os.path.join(TEST_ROOT, "__validation__")})

    conn = db.get_db()
    row = conn.execute(
        "SELECT status FROM memories WHERE id = ?",
        ("99999999-9999-9999-9999-999999999999",),
    ).fetchone()
    conn.close()
    last_run = db.read_curator_last_run()
    proposal_files = os.listdir(TEST_PROPOSALS) if os.path.isdir(TEST_PROPOSALS) else []

    if row and row["status"] == "stale" and last_run and proposal_files:
        ok("curator_runs_lifecycle_and_proposal")
    else:
        fail(
            "curator_runs_lifecycle_and_proposal",
            f"status={row['status'] if row else None} last_run={last_run!r} "
            f"proposals={proposal_files}",
        )


def test_memory_session_get_returns_transcript() -> None:
    reset_db()
    session_id = "session-get-full-001"
    archive_path = db.archive_session_path("__validation__", session_id)
    os.makedirs(os.path.dirname(archive_path), exist_ok=True)
    prompt_text = "unique thaw marker abc123"
    with open(archive_path, "w") as f:
        f.write(
            json.dumps(
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "kind": "prompt",
                    "scope": "__validation__",
                    "data": {"prompt": prompt_text},
                }
            )
            + "\n"
        )
    conn = db.get_db()
    db.upsert_session_archive_index(
        conn,
        session_id,
        "__validation__",
        datetime.now(timezone.utc).isoformat(),
        archive_path,
        db.build_archive_index_text(archive_path),
    )
    conn.commit()
    conn.close()

    by_full = tools.memory_session_get(session_id, scope="__validation__")
    by_prefix = tools.memory_session_get(session_id[:8], scope="__validation__")
    missing = tools.memory_session_get("deadbeef")

    if (
        prompt_text in by_full
        and prompt_text in by_prefix
        and "deadbeef" in missing.lower()
    ):
        ok("memory_session_get_returns_transcript")
    else:
        fail(
            "memory_session_get_returns_transcript",
            f"full={by_full[:120]} prefix={by_prefix[:120]} missing={missing!r}",
        )


def test_memory_session_search_finds_archived_content() -> None:
    reset_db()
    session_id = "archive-search-001"
    archive_path = db.archive_session_path("__validation__", session_id)
    os.makedirs(os.path.dirname(archive_path), exist_ok=True)
    with open(archive_path, "w") as f:
        f.write(
            json.dumps(
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "kind": "prompt",
                    "scope": "__validation__",
                    "data": {"prompt": "unique glacier keyword xyzzy"},
                }
            )
            + "\n"
        )
    conn = db.get_db()
    db.upsert_session_archive_index(
        conn,
        session_id,
        "__validation__",
        datetime.now(timezone.utc).isoformat(),
        archive_path,
        db.build_archive_index_text(archive_path),
    )
    conn.commit()
    conn.close()

    result = tools.memory_session_search("xyzzy", scope="__validation__", limit=5)
    if "xyzzy" in result.lower() and "__validation__" in result:
        ok("memory_session_search_finds_archived_content")
    else:
        fail("memory_session_search_finds_archived_content", result)


if __name__ == "__main__":
    try:
        test_extract_memory_metadata_defaults()
        test_extract_memory_metadata_normalizes_values()
        test_memory_query_clamps_limit()
        test_memory_consolidate_ignores_invalid_actions()
        test_memory_consolidate_dry_run_by_default()
        test_curator_skips_within_interval()
        test_curator_runs_lifecycle_and_proposal()
        test_memory_index_no_llm_call()
        test_memory_get_full_and_prefix()
        test_memory_timeline_orders_and_filters()
        test_hook_summarize_inserts_memory()
        test_hook_summarize_stores_open_actions()
        test_hook_summarize_handles_noisy_open_actions()
        test_open_action_category_normalization()
        test_hook_summarize_skips_short_buffer()
        test_hot_edit_add_replace_remove()
        test_hot_edit_rejects_oversize()
        test_inject_includes_hot_memory()
        test_memory_session_get_returns_transcript()
        test_memory_session_search_finds_archived_content()
    finally:
        restore_state()

    print(f"\nResults: {PASS} passed, {FAIL} failed out of {PASS + FAIL}")
    raise SystemExit(1 if FAIL > 0 else 0)
