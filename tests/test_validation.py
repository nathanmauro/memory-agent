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
TEST_ARCHIVE = os.path.join(TEST_ROOT, "archive")
TEST_ARCHIVE_SESSIONS = os.path.join(TEST_ARCHIVE, "sessions")
TEST_HOT = os.path.join(TEST_ROOT, "hot")
TEST_PROPOSALS = os.path.join(TEST_ROOT, "proposals")
TEST_BACKUPS = os.path.join(TEST_ROOT, "backups")
TEST_CURATOR_LAST_RUN = os.path.join(TEST_ROOT, "curator_last_run.txt")
ORIGINAL_DB_PATH = db.DB_PATH
ORIGINAL_SESSIONS_DIR = db.SESSIONS_DIR
ORIGINAL_ARCHIVE_DIR = db.ARCHIVE_DIR
ORIGINAL_ARCHIVE_SESSIONS_DIR = db.ARCHIVE_SESSIONS_DIR
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
    db.ARCHIVE_DIR = TEST_ARCHIVE
    db.ARCHIVE_SESSIONS_DIR = TEST_ARCHIVE_SESSIONS
    db.HOT_DIR = TEST_HOT
    db.PROPOSALS_DIR = TEST_PROPOSALS
    db.BACKUPS_DIR = TEST_BACKUPS
    db.CURATOR_LAST_RUN_PATH = TEST_CURATOR_LAST_RUN
    db.init_db()


def restore_state() -> None:
    db.DB_PATH = ORIGINAL_DB_PATH
    db.SESSIONS_DIR = ORIGINAL_SESSIONS_DIR
    db.ARCHIVE_DIR = ORIGINAL_ARCHIVE_DIR
    db.ARCHIVE_SESSIONS_DIR = ORIGINAL_ARCHIVE_SESSIONS_DIR
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
    pinned: int = 0,
) -> None:
    now = updated_at or datetime.now(timezone.utc).isoformat()
    conn = db.get_db()
    conn.execute(
        """
        INSERT INTO memories (
            id, content, category, scope, tags, created_at, updated_at,
            source, importance, pinned
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (mem_id, content, category, scope, "", now, now, "test", importance, pinned),
    )
    conn.commit()
    conn.close()


def access_counts() -> dict[str, tuple[int, str]]:
    conn = db.get_db()
    rows = conn.execute(
        "SELECT id, access_count, last_accessed_at FROM memories"
    ).fetchall()
    conn.close()
    return {
        row["id"]: (row["access_count"], row["last_accessed_at"])
        for row in rows
    }


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


def test_memory_query_tracks_access() -> None:
    reset_db()
    first_id = "01010101-0101-0101-0101-010101010101"
    second_id = "02020202-0202-0202-0202-020202020202"
    insert_memory(first_id, "database memory one")
    insert_memory(second_id, "database memory two")

    result = tools.memory_query("database", scope="__validation__", limit=10)
    counts = access_counts()

    if (
        result.startswith("Found 2 memories")
        and counts[first_id][0] == 1
        and counts[second_id][0] == 1
        and counts[first_id][1]
        and counts[second_id][1]
    ):
        ok("memory_query_tracks_access")
    else:
        fail(
            "memory_query_tracks_access",
            f"result={result[:80]} counts={counts}",
        )


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
    counts = access_counts()
    if (
        calls["count"] == 0
        and "2 hits" in result
        and "aaaaaaaa" in result
        and counts["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"][0] == 1
        and counts["bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"][0] == 1
    ):
        ok("memory_index_no_llm_call")
    else:
        fail(
            "memory_index_no_llm_call",
            f"calls={calls['count']} result={result[:120]} counts={counts}",
        )


def test_memory_index_excludes_stale_by_default() -> None:
    reset_db()
    insert_memory(
        "51515151-5151-5151-5151-515151515151",
        "shared marker active memory",
    )
    insert_memory(
        "52525252-5252-5252-5252-525252525252",
        "shared marker stale memory",
    )
    conn = db.get_db()
    conn.execute(
        "UPDATE memories SET status = 'stale' WHERE id = ?",
        ("52525252-5252-5252-5252-525252525252",),
    )
    conn.commit()
    conn.close()

    default_result = tools.memory_index("shared marker", scope="__validation__")
    try:
        stale_result = tools.memory_index(
            "shared marker", scope="__validation__", status="stale"
        )
    except TypeError as exc:
        fail("memory_index_excludes_stale_by_default", f"missing status arg: {exc}")
        return

    if (
        "active memory" in default_result
        and "stale memory" not in default_result
        and "stale memory" in stale_result
        and "active memory" not in stale_result
    ):
        ok("memory_index_excludes_stale_by_default")
    else:
        fail(
            "memory_index_excludes_stale_by_default",
            f"default={default_result} stale={stale_result}",
        )


def test_memory_get_full_and_prefix() -> None:
    reset_db()
    full_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    insert_memory(full_id, "hello world")
    by_full = tools.memory_get([full_id])
    by_prefix = tools.memory_get(["cccccccc"])
    by_missing = tools.memory_get(["deadbeef"])
    counts = access_counts()
    if (
        "hello world" in by_full
        and "hello world" in by_prefix
        and by_missing == "No memories found."
        and counts[full_id][0] == 2
        and counts[full_id][1]
    ):
        ok("memory_get_full_and_prefix")
    else:
        fail(
            "memory_get_full_and_prefix",
            f"full={by_full[:80]} prefix={by_prefix[:80]} "
            f"missing={by_missing!r} counts={counts}",
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


def test_memory_list_and_timeline_exclude_stale_by_default() -> None:
    reset_db()
    active_ts = datetime(2026, 5, 1, tzinfo=timezone.utc).isoformat()
    stale_ts = datetime(2026, 5, 2, tzinfo=timezone.utc).isoformat()
    insert_memory(
        "61616161-6161-6161-6161-616161616161",
        "status filter active row",
        updated_at=active_ts,
    )
    insert_memory(
        "62626262-6262-6262-6262-626262626262",
        "status filter stale row",
        updated_at=stale_ts,
    )
    conn = db.get_db()
    conn.execute(
        "UPDATE memories SET status = 'stale' WHERE id = ?",
        ("62626262-6262-6262-6262-626262626262",),
    )
    conn.commit()
    conn.close()

    default_list = tools.memory_list(scope="__validation__")
    stale_list = tools.memory_list(scope="__validation__", status="stale")
    default_timeline = tools.memory_timeline(scope="__validation__")
    stale_timeline = tools.memory_timeline(scope="__validation__", status="stale")

    if (
        "active row" in default_list
        and "stale row" not in default_list
        and "stale row" in stale_list
        and "active row" not in stale_list
        and "active row" in default_timeline
        and "stale row" not in default_timeline
        and "stale row" in stale_timeline
        and "active row" not in stale_timeline
    ):
        ok("memory_list_and_timeline_exclude_stale_by_default")
    else:
        fail(
            "memory_list_and_timeline_exclude_stale_by_default",
            f"default_list={default_list} stale_list={stale_list} "
            f"default_timeline={default_timeline} stale_timeline={stale_timeline}",
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


def test_hook_summarize_archives_short_buffer_without_warm_memory() -> None:
    reset_db()
    session_id = "test-session-002"
    buffer_path = os.path.join(db.SESSIONS_DIR, f"{session_id}.jsonl")
    os.makedirs(db.SESSIONS_DIR, exist_ok=True)
    with open(buffer_path, "w") as f:
        f.write(
            json.dumps(
                {
                    "ts": "x",
                    "kind": "prompt",
                    "scope": "__validation__",
                    "data": {"prompt": "tiny unique archive marker"},
                }
            )
            + "\n"
        )
    calls = {"count": 0}
    llm.llm_call = lambda s, u: (calls.update(count=calls["count"] + 1) or "{}")

    hook_handler._summarize_buffer(buffer_path, fallback_scope="__validation__")

    archive_path = db.archive_session_path("__validation__", session_id)
    archive_exists = os.path.exists(archive_path)
    search = tools.memory_session_search(
        "tiny archive", scope="__validation__", limit=5
    )
    conn = db.get_db()
    n = conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"]
    archived = conn.execute(
        "SELECT COUNT(*) AS n FROM session_archive WHERE session_id = ?",
        (session_id,),
    ).fetchone()["n"]
    conn.close()
    if (
        calls["count"] == 0
        and not os.path.exists(buffer_path)
        and archive_exists
        and archived == 1
        and n == 0
        and "tiny" in search
    ):
        ok("hook_summarize_archives_short_buffer_without_warm_memory")
    else:
        fail(
            "hook_summarize_archives_short_buffer_without_warm_memory",
            f"calls={calls['count']} buffer={os.path.exists(buffer_path)} "
            f"archive={archive_exists} archived={archived} rows={n} search={search}",
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

    if marker in ctx:
        ok("inject_includes_hot_memory")
    else:
        fail("inject_includes_hot_memory", ctx[:300])


def test_inject_excludes_unpinned_warm_memory_by_default() -> None:
    reset_db()
    scope = "__validation__"
    marker = "broad unpinned warm memory should not inject"
    insert_memory(
        "abababab-abab-abab-abab-abababababab",
        marker,
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
        ctx = out.get("hookSpecificOutput", {}).get("additionalContext", "")
    except Exception as e:
        fail(
            "inject_excludes_unpinned_warm_memory_by_default",
            f"bad json: {e} raw={raw[:200]}",
        )
        return

    if marker not in ctx:
        ok("inject_excludes_unpinned_warm_memory_by_default")
    else:
        fail("inject_excludes_unpinned_warm_memory_by_default", ctx[:300])


def test_inject_includes_pinned_warm_memory() -> None:
    reset_db()
    scope = "__validation__"
    marker = "pinned warm memory can inject"
    insert_memory(
        "bcbcbcbc-bcbc-bcbc-bcbc-bcbcbcbcbcbc",
        marker,
        scope=scope,
        importance=5,
        pinned=1,
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
        fail("inject_includes_pinned_warm_memory", f"bad json: {e} raw={raw[:200]}")
        return

    if marker in ctx:
        ok("inject_includes_pinned_warm_memory")
    else:
        fail("inject_includes_pinned_warm_memory", ctx[:300])


def test_memory_pin_enables_startup_inject() -> None:
    reset_db()
    scope = "__validation__"
    marker = "production pin tool enables startup inject"
    llm.llm_call = lambda system, user: json.dumps(
        {"category": "project_knowledge", "tags": "", "importance": 5}
    )
    stored = tools.memory_store(marker, scope=scope, source="test")
    mem_id = stored.split()[2]
    try:
        pin_result = tools.memory_pin(mem_id[:8], scope=scope)
    except AttributeError as exc:
        fail("memory_pin_enables_startup_inject", f"missing memory_pin: {exc}")
        return

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
        fail("memory_pin_enables_startup_inject", f"bad json: {e} raw={raw[:200]}")
        return

    if "Pinned memory" in pin_result and marker in ctx:
        ok("memory_pin_enables_startup_inject")
    else:
        fail("memory_pin_enables_startup_inject", f"pin={pin_result} ctx={ctx[:300]}")


def test_memory_unpin_disables_startup_inject() -> None:
    reset_db()
    scope = "__validation__"
    marker = "production unpin tool disables startup inject"
    insert_memory(
        "53535353-5353-5353-5353-535353535353",
        marker,
        scope=scope,
        importance=5,
        pinned=1,
    )
    try:
        unpin_result = tools.memory_unpin("53535353", scope=scope)
    except AttributeError as exc:
        fail("memory_unpin_disables_startup_inject", f"missing memory_unpin: {exc}")
        return

    import io
    import contextlib

    payload = {"cwd": os.path.join(TEST_ROOT, scope)}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        hook_handler._inject_context(payload)
    raw = buf.getvalue().strip()
    try:
        out = json.loads(raw)
        ctx = out.get("hookSpecificOutput", {}).get("additionalContext", "")
    except Exception as e:
        fail("memory_unpin_disables_startup_inject", f"bad json: {e} raw={raw[:200]}")
        return

    if "Unpinned memory" in unpin_result and marker not in ctx:
        ok("memory_unpin_disables_startup_inject")
    else:
        fail("memory_unpin_disables_startup_inject", f"unpin={unpin_result} ctx={ctx[:300]}")


def test_large_hot_memory_does_not_crowd_out_pinned_warm_memory() -> None:
    reset_db()
    scope = "__validation__"
    marker = "pinned survives large hot memory"
    tools.memory_hot_edit(scope, "replace", "h" * hot.HOT_MAX_CHARS, target="")
    insert_memory(
        "54545454-5454-5454-5454-545454545454",
        marker,
        scope=scope,
        importance=5,
        pinned=1,
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
        fail("large_hot_memory_does_not_crowd_out_pinned_warm_memory", f"bad json: {e} raw={raw[:200]}")
        return

    if marker in ctx and len(ctx) <= hook_handler.INJECT_BUDGET_CHARS:
        ok("large_hot_memory_does_not_crowd_out_pinned_warm_memory")
    else:
        fail(
            "large_hot_memory_does_not_crowd_out_pinned_warm_memory",
            f"len={len(ctx)} ctx_tail={ctx[-300:]}",
        )


def test_inject_excludes_archive_pointers_by_default() -> None:
    reset_db()
    scope = "__validation__"
    session_id = "startup-archive-pointer-001"
    marker = "archived startup pointer should not inject"
    archive_path = db.archive_session_path(scope, session_id)
    os.makedirs(os.path.dirname(archive_path), exist_ok=True)
    with open(archive_path, "w") as f:
        f.write(
            json.dumps(
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "kind": "prompt",
                    "scope": scope,
                    "data": {"prompt": marker},
                }
            )
            + "\n"
        )
    conn = db.get_db()
    db.upsert_session_archive_index(
        conn,
        session_id,
        scope,
        datetime.now(timezone.utc).isoformat(),
        archive_path,
        db.build_archive_index_text(archive_path),
    )
    conn.commit()
    conn.close()

    import io
    import contextlib

    payload = {"cwd": os.path.join(TEST_ROOT, scope)}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        hook_handler._inject_context(payload)
    raw = buf.getvalue().strip()
    try:
        out = json.loads(raw)
        ctx = out.get("hookSpecificOutput", {}).get("additionalContext", "")
    except Exception as e:
        fail("inject_excludes_archive_pointers_by_default", f"bad json: {e} raw={raw[:200]}")
        return

    if session_id[:8] not in ctx and marker not in ctx:
        ok("inject_excludes_archive_pointers_by_default")
    else:
        fail("inject_excludes_archive_pointers_by_default", ctx[:300])


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


def test_lifecycle_stales_old_high_importance_unpinned_memory() -> None:
    reset_db()
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    insert_memory(
        "98989898-9898-9898-9898-989898989898",
        "old high importance unpinned memory",
        importance=5,
        updated_at=old,
    )

    conn = db.get_db()
    changed = db.apply_lifecycle_transitions(conn, "__validation__")
    conn.commit()
    row = conn.execute(
        "SELECT status FROM memories WHERE id = ?",
        ("98989898-9898-9898-9898-989898989898",),
    ).fetchone()
    conn.close()

    if changed == 1 and row and row["status"] == "stale":
        ok("lifecycle_stales_old_high_importance_unpinned_memory")
    else:
        fail(
            "lifecycle_stales_old_high_importance_unpinned_memory",
            f"changed={changed} status={row['status'] if row else None}",
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


def test_memory_session_get_uses_scope_for_ambiguous_prefix() -> None:
    reset_db()
    now = datetime.now(timezone.utc).isoformat()
    sessions = [
        ("ambiguous-session-alpha", "scope-one", "alpha prompt marker"),
        ("ambiguous-session-beta", "scope-two", "beta prompt marker"),
    ]
    conn = db.get_db()
    for session_id, scope, prompt_text in sessions:
        archive_path = db.archive_session_path(scope, session_id)
        os.makedirs(os.path.dirname(archive_path), exist_ok=True)
        with open(archive_path, "w") as f:
            f.write(
                json.dumps(
                    {
                        "ts": now,
                        "kind": "prompt",
                        "scope": scope,
                        "data": {"prompt": prompt_text},
                    }
                )
                + "\n"
            )
        db.upsert_session_archive_index(
            conn,
            session_id,
            scope,
            now,
            archive_path,
            db.build_archive_index_text(archive_path),
        )
    conn.commit()
    conn.close()

    ambiguous = tools.memory_session_get("ambiguous-session")
    scoped = tools.memory_session_get("ambiguous-session", scope="scope-two")

    if (
        "ambiguous" in ambiguous.lower()
        and "scope-one" in ambiguous
        and "scope-two" in ambiguous
        and "beta prompt marker" in scoped
        and "alpha prompt marker" not in scoped
    ):
        ok("memory_session_get_uses_scope_for_ambiguous_prefix")
    else:
        fail(
            "memory_session_get_uses_scope_for_ambiguous_prefix",
            f"ambiguous={ambiguous[:160]} scoped={scoped[:160]}",
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
        test_memory_query_tracks_access()
        test_memory_consolidate_ignores_invalid_actions()
        test_memory_consolidate_dry_run_by_default()
        test_curator_skips_within_interval()
        test_curator_runs_lifecycle_and_proposal()
        test_memory_index_no_llm_call()
        test_memory_index_excludes_stale_by_default()
        test_memory_get_full_and_prefix()
        test_memory_timeline_orders_and_filters()
        test_memory_list_and_timeline_exclude_stale_by_default()
        test_hook_summarize_inserts_memory()
        test_hook_summarize_stores_open_actions()
        test_hook_summarize_handles_noisy_open_actions()
        test_open_action_category_normalization()
        test_hook_summarize_archives_short_buffer_without_warm_memory()
        test_hot_edit_add_replace_remove()
        test_hot_edit_rejects_oversize()
        test_inject_includes_hot_memory()
        test_inject_excludes_unpinned_warm_memory_by_default()
        test_inject_includes_pinned_warm_memory()
        test_memory_pin_enables_startup_inject()
        test_memory_unpin_disables_startup_inject()
        test_large_hot_memory_does_not_crowd_out_pinned_warm_memory()
        test_inject_excludes_archive_pointers_by_default()
        test_lifecycle_stales_old_high_importance_unpinned_memory()
        test_memory_session_get_returns_transcript()
        test_memory_session_get_uses_scope_for_ambiguous_prefix()
        test_memory_session_search_finds_archived_content()
    finally:
        restore_state()

    print(f"\nResults: {PASS} passed, {FAIL} failed out of {PASS + FAIL}")
    raise SystemExit(1 if FAIL > 0 else 0)
