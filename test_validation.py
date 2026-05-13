"""
Local validation tests for pydantic-backed parsing and cleanup helpers.
Run: python test_validation.py
"""

import json
import os
import tempfile
from datetime import datetime, timezone

import db
import llm
import tools

PASS = 0
FAIL = 0
TEST_DB = os.path.join(tempfile.gettempdir(), "memory_validation_test.db")
ORIGINAL_DB_PATH = db.DB_PATH
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
    for suffix in ["", "-wal", "-shm"]:
        try:
            os.remove(TEST_DB + suffix)
        except FileNotFoundError:
            pass
    db.DB_PATH = TEST_DB
    db.init_db()


def restore_state() -> None:
    db.DB_PATH = ORIGINAL_DB_PATH
    llm.llm_call = ORIGINAL_LLM_CALL
    for suffix in ["", "-wal", "-shm"]:
        try:
            os.remove(TEST_DB + suffix)
        except FileNotFoundError:
            pass


def insert_memory(mem_id: str, content: str, scope: str = "__validation__") -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = db.get_db()
    conn.execute(
        "INSERT INTO memories (id, content, category, scope, tags, created_at, updated_at, source, importance) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (mem_id, content, "project_knowledge", scope, "", now, now, "test", 3),
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
    result = tools.memory_consolidate("__validation__")
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


if __name__ == "__main__":
    try:
        test_extract_memory_metadata_defaults()
        test_extract_memory_metadata_normalizes_values()
        test_memory_query_clamps_limit()
        test_memory_consolidate_ignores_invalid_actions()
    finally:
        restore_state()

    print(f"\nResults: {PASS} passed, {FAIL} failed out of {PASS + FAIL}")
    raise SystemExit(1 if FAIL > 0 else 0)
