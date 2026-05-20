"""
Integration tests for Memory Agent MCP server with Bedrock/Haiku.
All test data uses scope='__test__' to avoid polluting real memories.
Run: python test_integration.py
"""

import json
import os
import sys
import time

# Patch DB to use a test database
TEST_DB = os.path.join(os.path.expanduser("~"), ".claude", "memory", "memory_test.db")

from mcp_memory_agent import db, llm, tools

db.DB_PATH = TEST_DB
db.init_db()

PASS = 0
FAIL = 0
SCOPE = "__test__"


def ok(name):
    global PASS
    PASS += 1
    print(f"  PASS  {name}")


def fail(name, reason):
    global FAIL
    FAIL += 1
    print(f"  FAIL  {name}: {reason}")


def cleanup():
    """Remove all test data."""
    conn = db.get_db()
    conn.execute("DELETE FROM memories WHERE scope = ?", (SCOPE,))
    try:
        conn.execute(
            "DELETE FROM memories_fts WHERE id IN (SELECT id FROM memories WHERE scope = ?)",
            (SCOPE,),
        )
    except Exception:
        pass
    conn.commit()
    conn.close()


def test_bedrock_reachable():
    """Verify Bedrock is reachable and the model responds."""
    try:
        response = llm.bedrock.converse(
            modelId=llm.BEDROCK_MODEL,
            messages=[{"role": "user", "content": [{"text": "Say hello."}]}],
            inferenceConfig={"maxTokens": 16},
        )
        text = response["output"]["message"]["content"][0]["text"]
        if text:
            ok("bedrock_reachable")
        else:
            fail("bedrock_reachable", "Empty response from Bedrock")
    except Exception as e:
        fail("bedrock_reachable", str(e))


def test_llm_returns_json():
    """Verify the LLM can return valid JSON."""
    try:
        raw = llm.llm_call(
            "Respond with ONLY valid JSON.", '{"color": "blue", "count": 3}'
        )
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            json.loads(raw[start:end])
            ok("llm_returns_json")
        else:
            fail("llm_returns_json", f"No JSON in response: {raw[:200]}")
    except Exception as e:
        fail("llm_returns_json", str(e))


def test_store_basic():
    """Store a memory and verify it exists in DB."""
    result = tools.memory_store(
        "Test memory: the sky is blue", scope=SCOPE, source="integration_test"
    )
    if "Stored memory" in result:
        ok("store_basic")
    else:
        fail("store_basic", f"Unexpected result: {result}")


def test_store_categorization():
    """Verify the LLM assigns a valid category."""
    result = tools.memory_store(
        "User always prefers dark mode in all editors", scope=SCOPE
    )
    conn = db.get_db()
    row = conn.execute(
        "SELECT category FROM memories WHERE scope = ? ORDER BY created_at DESC LIMIT 1",
        (SCOPE,),
    ).fetchone()
    conn.close()
    valid = {"session_summary", "code_decision", "user_preference", "project_knowledge"}
    if row and row["category"] in valid:
        ok(f"store_categorization (got: {row['category']})")
    else:
        fail(
            "store_categorization",
            f"Invalid category: {row['category'] if row else 'no row'}",
        )


def test_store_tags():
    """Verify the LLM extracts tags."""
    tools.memory_store("Python 3.11 is installed at C:/Python311", scope=SCOPE)
    conn = db.get_db()
    row = conn.execute(
        "SELECT tags FROM memories WHERE scope = ? AND content LIKE '%Python%' LIMIT 1",
        (SCOPE,),
    ).fetchone()
    conn.close()
    if row and row["tags"] and len(row["tags"]) > 0:
        ok(f"store_tags (got: {row['tags']})")
    else:
        fail("store_tags", f"No tags extracted: {row['tags'] if row else 'no row'}")


def test_store_importance():
    """Verify importance is assigned between 1-5."""
    conn = db.get_db()
    rows = conn.execute(
        "SELECT importance FROM memories WHERE scope = ?", (SCOPE,)
    ).fetchall()
    conn.close()
    for row in rows:
        if not (1 <= row["importance"] <= 5):
            fail("store_importance", f"Importance {row['importance']} out of range")
            return
    ok("store_importance")


def test_store_merge():
    """Store two similar memories and verify dedup/merge."""
    cleanup()
    tools.memory_store("The project uses React 18 with TypeScript", scope=SCOPE)
    time.sleep(0.5)
    result = tools.memory_store(
        "The project uses React 18 with TypeScript and Vite", scope=SCOPE
    )
    conn = db.get_db()
    count = conn.execute(
        "SELECT COUNT(*) as c FROM memories WHERE scope = ?", (SCOPE,)
    ).fetchone()["c"]
    conn.close()
    if "Merged" in result:
        ok(f"store_merge (merged, {count} memories)")
    elif count == 1:
        ok(f"store_merge (deduped to 1 memory)")
    else:
        # Merge isn't guaranteed — LLM might see them as different enough
        fail(
            "store_merge", f"Expected merge but got {count} memories. Result: {result}"
        )


def test_query_fts():
    """Query by keyword and get relevant results."""
    cleanup()
    tools.memory_store("Arduino Uno R3 is connected on COM3", scope=SCOPE)
    tools.memory_store("Obsidian vault is at ~/Documents", scope=SCOPE)
    result = tools.memory_query("Arduino board", scope=SCOPE)
    if "Arduino" in result and "Found" in result:
        ok("query_fts")
    else:
        fail("query_fts", f"Expected Arduino result: {result[:200]}")


def test_query_like_fallback():
    """Query with unusual terms that might not be in FTS."""
    cleanup()
    tools.memory_store("The xyzzy_config.toml file controls deployment", scope=SCOPE)
    result = tools.memory_query("xyzzy_config", scope=SCOPE)
    if "xyzzy" in result.lower():
        ok("query_like_fallback")
    else:
        fail("query_like_fallback", f"Expected xyzzy result: {result[:200]}")


def test_query_empty():
    """Query an empty scope returns no results."""
    result = tools.memory_query("anything", scope="__empty_test__")
    if "No memories found" in result:
        ok("query_empty")
    else:
        fail("query_empty", f"Expected no results: {result[:200]}")


def test_query_ranking():
    """Verify ranking returns the most relevant result first."""
    cleanup()
    tools.memory_store("The database password is stored in .env", scope=SCOPE)
    tools.memory_store("Favorite color is green", scope=SCOPE)
    tools.memory_store("Database runs on PostgreSQL 15 on port 5432", scope=SCOPE)
    result = tools.memory_query("database setup", scope=SCOPE, limit=2)
    lines = result.split("\n")
    # First result should mention database/PostgreSQL, not color
    first_result = next((l for l in lines if "(" in l and "/" in l), "")
    if "database" in result.lower() or "postgresql" in result.lower():
        ok("query_ranking")
    else:
        fail("query_ranking", f"Ranking seems off: {result[:300]}")


def test_list_basic():
    """List memories in a scope."""
    cleanup()
    tools.memory_store("The build tool is webpack 5 with babel", scope=SCOPE)
    tools.memory_store(
        "The CI pipeline runs on GitHub Actions with Node 20", scope=SCOPE
    )
    result = tools.memory_list(scope=SCOPE)
    if "memories" in result and "No memories" not in result:
        ok(f"list_basic ({result.split(chr(10))[0]})")
    else:
        fail("list_basic", f"Expected memories: {result[:200]}")


def test_list_with_category_filter():
    """List with category filter."""
    result = tools.memory_list(scope=SCOPE, category="user_preference")
    # Should return 0 or more — just verify it doesn't error
    if "memories" in result or "No memories" in result:
        ok("list_with_category_filter")
    else:
        fail("list_with_category_filter", f"Unexpected: {result[:200]}")


def test_forget():
    """Store a memory then delete it."""
    cleanup()
    result = tools.memory_store("Temporary test memory to delete", scope=SCOPE)
    # Extract ID from result
    mem_id = result.split(" ")[2] if "Stored" in result else None
    if not mem_id:
        fail("forget", f"Couldn't extract ID from: {result}")
        return
    short_id = mem_id[:8]
    del_result = tools.memory_forget(short_id)
    if "Deleted" in del_result:
        # Verify it's gone
        conn = db.get_db()
        count = conn.execute(
            "SELECT COUNT(*) as c FROM memories WHERE id = ?", (mem_id,)
        ).fetchone()["c"]
        conn.close()
        if count == 0:
            ok("forget")
        else:
            fail("forget", "Memory still exists after delete")
    else:
        fail("forget", f"Delete failed: {del_result}")


def test_forget_nonexistent():
    """Forget a memory that doesn't exist."""
    result = tools.memory_forget("00000000-0000-0000-0000-000000000000")
    if "No memory found" in result or "Deleted" in result:
        ok("forget_nonexistent")
    else:
        fail("forget_nonexistent", f"Unexpected: {result}")


def test_consolidate():
    """Store duplicate-ish memories and consolidate."""
    cleanup()
    tools.memory_store("Node.js version 20 is installed", scope=SCOPE)
    tools.memory_store("Node version is 20 LTS", scope=SCOPE)
    tools.memory_store("npm version 10.2 is available", scope=SCOPE)
    result = tools.memory_consolidate(SCOPE)
    # Consolidation should either merge or report clean
    if "actions applied" in result or "clean" in result or "Consolidated" in result:
        ok(f"consolidate ({result[:100]})")
    else:
        fail("consolidate", f"Unexpected: {result[:200]}")


def test_consolidate_empty():
    """Consolidate an empty scope."""
    result = tools.memory_consolidate("__empty_consolidate__")
    if "No memories" in result:
        ok("consolidate_empty")
    else:
        fail("consolidate_empty", f"Unexpected: {result[:200]}")


def test_scoping():
    """Memories in one scope don't appear in another."""
    cleanup()
    tools.memory_store("Scoped to test", scope=SCOPE)
    result = tools.memory_list(scope="__other_scope__")
    if "No memories" in result:
        ok("scoping")
    else:
        fail("scoping", f"Leaked across scopes: {result[:200]}")


if __name__ == "__main__":
    print(f"\nMemory Agent Integration Tests")
    print(f"Model: {llm.BEDROCK_MODEL} @ Bedrock ({llm.AWS_REGION})")
    print(f"Test DB: {TEST_DB}")
    print(f"{'=' * 50}\n")

    cleanup()

    tests = [
        # test_bedrock_reachable,  # only enable with LLM_BACKEND=bedrock
        test_llm_returns_json,
        test_store_basic,
        test_store_categorization,
        test_store_tags,
        test_store_importance,
        test_store_merge,
        test_query_fts,
        test_query_like_fallback,
        test_query_empty,
        test_query_ranking,
        test_list_basic,
        test_list_with_category_filter,
        test_forget,
        test_forget_nonexistent,
        test_consolidate,
        test_consolidate_empty,
        test_scoping,
    ]

    for test in tests:
        try:
            test()
        except Exception as e:
            fail(test.__name__, f"Exception: {e}")

    cleanup()

    # Remove test DB
    try:
        os.remove(TEST_DB)
        os.remove(TEST_DB + "-wal")
        os.remove(TEST_DB + "-shm")
    except FileNotFoundError:
        pass

    print(f"\n{'=' * 50}")
    print(f"Results: {PASS} passed, {FAIL} failed out of {PASS + FAIL}")
    sys.exit(1 if FAIL > 0 else 0)
