"""
Local validation tests for pydantic-backed parsing and cleanup helpers.
Run: python -m tests.test_validation  (or `pytest tests/test_validation.py`)
"""

import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta

from mcp_memory_agent import db, hook_handler, hot, install, llm, tools
from mcp_memory_agent.integrations import claude, codex, common
from mcp_memory_agent.models import MemoryRecord

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
ORIGINAL_LLM_BACKEND = llm.LLM_BACKEND
ORIGINAL_OLLAMA_CALL = llm._ollama_call
ORIGINAL_LM_STUDIO_CALL = llm._lm_studio_call
ORIGINAL_BEDROCK_CALL = llm._bedrock_call
ORIGINAL_CODEX_CALL = llm._codex_call
ORIGINAL_CODEX_BIN = llm.CODEX_BIN
ORIGINAL_CODEX_MODEL = llm.CODEX_MODEL
ORIGINAL_CODEX_REASONING = llm.CODEX_REASONING
ORIGINAL_CLAUDE_SETTINGS_PATH = claude.SETTINGS_PATH
ORIGINAL_CLAUDE_SETTINGS_BACKUP = claude.SETTINGS_BACKUP


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
    llm.LLM_BACKEND = ORIGINAL_LLM_BACKEND
    llm._ollama_call = ORIGINAL_OLLAMA_CALL
    llm._lm_studio_call = ORIGINAL_LM_STUDIO_CALL
    llm._bedrock_call = ORIGINAL_BEDROCK_CALL
    llm._codex_call = ORIGINAL_CODEX_CALL
    llm.CODEX_BIN = ORIGINAL_CODEX_BIN
    llm.CODEX_MODEL = ORIGINAL_CODEX_MODEL
    llm.CODEX_REASONING = ORIGINAL_CODEX_REASONING
    claude.SETTINGS_PATH = ORIGINAL_CLAUDE_SETTINGS_PATH
    claude.SETTINGS_BACKUP = ORIGINAL_CLAUDE_SETTINGS_BACKUP
    shutil.rmtree(TEST_ROOT, ignore_errors=True)


def assert_result(name: str, condition: bool, reason: str) -> None:
    if condition:
        ok(name)
        return
    fail(name, reason)
    assert condition, reason


def redirect_claude_settings(root: str) -> None:
    claude.SETTINGS_PATH = os.path.join(root, ".claude", "settings.json")
    claude.SETTINGS_BACKUP = claude.SETTINGS_PATH + ".bak"


def restore_claude_settings() -> None:
    claude.SETTINGS_PATH = ORIGINAL_CLAUDE_SETTINGS_PATH
    claude.SETTINGS_BACKUP = ORIGINAL_CLAUDE_SETTINGS_BACKUP


def assert_claude_settings_in_tempdir(root: str) -> None:
    settings_path = os.path.abspath(claude.SETTINGS_PATH)
    temp_root = os.path.abspath(root)
    assert os.path.commonpath([settings_path, temp_root]) == temp_root


def snapshot_codex_env() -> dict[str, str | None]:
    return {key: os.environ.get(key) for key in codex.ENV_KEYS}


def clear_codex_env() -> None:
    for key in codex.ENV_KEYS:
        os.environ.pop(key, None)


def restore_codex_env(snapshot: dict[str, str | None]) -> None:
    for key, value in snapshot.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def insert_memory(
    mem_id: str,
    content: str,
    scope: str = "__validation__",
    category: str = "project_knowledge",
    importance: int = 3,
    updated_at: str = "",
    pinned: int = 0,
) -> None:
    now = updated_at or datetime.now(UTC).isoformat()
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


def fts_has_memory(conn, mem_id: str, content: str = "", tags: str = "") -> bool:
    try:
        row = conn.execute(
            "SELECT content, tags FROM memories_fts WHERE id = ?",
            (mem_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return True
    if not row:
        return False
    if content and row["content"] != content:
        return False
    if tags and row["tags"] != tags:
        return False
    return True


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


def adjacent_pair(items: list[str], first: str, second: str) -> bool:
    return any(
        left == first and right == second
        for left, right in zip(items, items[1:])
    )


def assert_llm_dispatch(backend: str, expected: str, name: str) -> None:
    calls = {}

    def make_spy(spy_name: str):
        def spy(system: str, user: str) -> str:
            calls[spy_name] = {"system": system, "user": user}
            return spy_name

        return spy

    llm.llm_call = ORIGINAL_LLM_CALL
    llm.LLM_BACKEND = backend
    llm._ollama_call = make_spy("ollama")
    llm._lm_studio_call = make_spy("lm-studio")
    llm._bedrock_call = make_spy("bedrock")
    llm._codex_call = make_spy("codex")

    result = llm.llm_call("sys", "usr")
    if (
        result == expected
        and list(calls.keys()) == [expected]
        and calls[expected] == {"system": "sys", "user": "usr"}
    ):
        ok(name)
    else:
        fail(name, f"backend={backend!r} result={result!r} calls={calls!r}")


def test_llm_call_dispatches_ollama_backend() -> None:
    assert_llm_dispatch("ollama", "ollama", "llm_call_dispatches_ollama_backend")


def test_llm_call_dispatches_unknown_backend_to_ollama() -> None:
    assert_llm_dispatch(
        "bogus", "ollama", "llm_call_dispatches_unknown_backend_to_ollama"
    )


def test_llm_call_dispatches_lm_studio_backend() -> None:
    assert_llm_dispatch(
        "lm-studio", "lm-studio", "llm_call_dispatches_lm_studio_backend"
    )


def test_llm_call_dispatches_lmstudio_alias() -> None:
    assert_llm_dispatch("lmstudio", "lm-studio", "llm_call_dispatches_lmstudio_alias")


def test_llm_call_dispatches_bedrock_backend() -> None:
    assert_llm_dispatch("bedrock", "bedrock", "llm_call_dispatches_bedrock_backend")


def test_llm_call_dispatches_codex_backend() -> None:
    assert_llm_dispatch("codex", "codex", "llm_call_dispatches_codex_backend")


def test_extract_json_object_parses_common_llm_shapes() -> None:
    cases = [
        (
            "plain",
            '{"kind": "decision", "score": 2}',
            {"kind": "decision", "score": 2},
        ),
        ("fenced", '```json\n{"kind": "decision"}\n```', {"kind": "decision"}),
        (
            "embedded",
            'Here is the result:\n{"accepted": true}\nDone.',
            {"accepted": True},
        ),
    ]
    failures = []
    for label, raw, expected in cases:
        result = llm.extract_json_object(raw)
        if result != expected:
            failures.append(f"{label}={result!r}")

    if not failures:
        ok("extract_json_object_parses_common_llm_shapes")
    else:
        fail("extract_json_object_parses_common_llm_shapes", "; ".join(failures))


def test_extract_json_object_repairs_trailing_commas_and_falls_back() -> None:
    repaired = llm.extract_json_object('{"name": "alpha", "values": [1, 2,],}')
    no_braces = llm.extract_json_object("no json here")
    unrepairable = llm.extract_json_object('{"name": }')

    if (
        repaired == {"name": "alpha", "values": [1, 2]}
        and no_braces == {}
        and unrepairable == {}
    ):
        ok("extract_json_object_repairs_trailing_commas_and_falls_back")
    else:
        fail(
            "extract_json_object_repairs_trailing_commas_and_falls_back",
            f"repaired={repaired!r} no_braces={no_braces!r} unrepairable={unrepairable!r}",
        )


def test_extract_json_array_parses_embedded_and_malformed_inputs() -> None:
    plain = llm.extract_json_array('[1, {"score": 2}]')
    embedded = llm.extract_json_array("Ranked indices: [3, 0, 1]\nThanks.")
    malformed = llm.extract_json_array("[1,]")

    if plain == [1, {"score": 2}] and embedded == [3, 0, 1] and malformed == []:
        ok("extract_json_array_parses_embedded_and_malformed_inputs")
    else:
        fail(
            "extract_json_array_parses_embedded_and_malformed_inputs",
            f"plain={plain!r} embedded={embedded!r} malformed={malformed!r}",
        )


def test_strip_fences_handles_tags_and_passthrough() -> None:
    with_tag = llm._strip_fences('```json\n{"a": 1}\n```')
    without_tag = llm._strip_fences("```\n[1, 2]\n```")
    passthrough = llm._strip_fences("no fences")

    if (
        with_tag == '{"a": 1}'
        and without_tag == "[1, 2]"
        and passthrough == "no fences"
    ):
        ok("strip_fences_handles_tags_and_passthrough")
    else:
        fail(
            "strip_fences_handles_tags_and_passthrough",
            f"with_tag={with_tag!r} without_tag={without_tag!r} passthrough={passthrough!r}",
        )


def test_repair_json_removes_trailing_commas() -> None:
    object_repaired = llm._repair_json('{"a": 1,}')
    array_repaired = llm._repair_json('{"items": [1, 2,]}')

    if object_repaired == '{"a": 1}' and array_repaired == '{"items": [1, 2]}':
        ok("repair_json_removes_trailing_commas")
    else:
        fail(
            "repair_json_removes_trailing_commas",
            f"object={object_repaired!r} array={array_repaired!r}",
        )


def test_resolve_codex_bin_prefers_module_attr_then_path_default() -> None:
    original_bin = llm.CODEX_BIN
    try:
        llm.CODEX_BIN = "/tmp/custom-codex"
        custom = llm._resolve_codex_bin()
        llm.CODEX_BIN = ""
        default = llm._resolve_codex_bin()
        expected_default = shutil.which("codex") or "/opt/homebrew/bin/codex"
        if custom == "/tmp/custom-codex" and default == expected_default:
            ok("resolve_codex_bin_prefers_module_attr_then_path_default")
        else:
            fail(
                "resolve_codex_bin_prefers_module_attr_then_path_default",
                f"custom={custom!r} default={default!r} expected={expected_default!r}",
            )
    finally:
        llm.CODEX_BIN = original_bin


def test_codex_call_builds_offline_argv_and_cleans_output() -> None:
    original_bin = llm.CODEX_BIN
    original_codex_call = llm._codex_call
    original_extra = os.environ.get("CODEX_EXTRA_ARGS")
    original_model = llm.CODEX_MODEL
    original_reasoning = llm.CODEX_REASONING
    original_run = subprocess.run
    calls = []
    output_path = ""

    def fake_run(
        cmd: list[str],
        input: bytes,
        stdout: int,
        stderr: int,
        timeout: int,
        check: bool,
    ) -> subprocess.CompletedProcess:
        nonlocal output_path
        calls.append(
            {
                "check": check,
                "cmd": cmd,
                "input": input,
                "stderr": stderr,
                "stdout": stdout,
                "timeout": timeout,
            }
        )
        output_path = cmd[cmd.index("-o") + 1]
        with open(output_path, "w") as f:
            f.write("  offline codex result  \n")
        return subprocess.CompletedProcess(cmd, 0)

    try:
        llm.CODEX_BIN = "/tmp/fake-codex"
        llm._codex_call = ORIGINAL_CODEX_CALL
        llm.CODEX_MODEL = "gpt-test"
        llm.CODEX_REASONING = "low"
        os.environ["CODEX_EXTRA_ARGS"] = '--profile test-profile --flag "two words"'
        subprocess.run = fake_run

        result = llm._codex_call("system prompt", "user prompt")
        call = calls[0] if calls else {}
        cmd = call.get("cmd", [])
        stdin = call.get("input", b"")
        out_index = cmd.index("-o") if "-o" in cmd else -1
        output_unlinked = bool(output_path) and not os.path.exists(output_path)
        prompt_ok = (
            b"system prompt" in stdin
            and b"user prompt" in stdin
            and b"Output only the requested result" in stdin
        )
        required = [
            "exec",
            "--ignore-user-config",
            "--skip-git-repo-check",
            "--ephemeral",
        ]
        argv_ok = all(item in cmd for item in required)
        pairs_ok = (
            adjacent_pair(cmd, "-s", "read-only")
            and adjacent_pair(cmd, "-c", 'model_reasoning_effort="low"')
            and adjacent_pair(cmd, "-m", "gpt-test")
        )
        output_arg_ok = (
            out_index >= 0
            and out_index + 1 < len(cmd)
            and cmd[out_index + 1] == output_path
        )
        extra_ok = (
            "--profile" in cmd
            and "test-profile" in cmd
            and "--flag" in cmd
            and "two words" in cmd
        )

        if (
            result == "offline codex result"
            and argv_ok
            and pairs_ok
            and output_arg_ok
            and extra_ok
            and prompt_ok
            and output_unlinked
        ):
            ok("codex_call_builds_offline_argv_and_cleans_output")
        else:
            fail(
                "codex_call_builds_offline_argv_and_cleans_output",
                (
                    f"result={result!r} cmd={cmd!r} prompt_ok={prompt_ok!r} "
                    f"output_path={output_path!r} output_unlinked={output_unlinked!r}"
                ),
            )
    finally:
        llm.CODEX_BIN = original_bin
        llm._codex_call = original_codex_call
        llm.CODEX_MODEL = original_model
        llm.CODEX_REASONING = original_reasoning
        subprocess.run = original_run
        if original_extra is None:
            os.environ.pop("CODEX_EXTRA_ARGS", None)
        else:
            os.environ["CODEX_EXTRA_ARGS"] = original_extra


def test_entry_point_command_prefers_local_path_then_module() -> None:
    name = "entry_point_command_prefers_local_path_then_module"
    original_executable = common.sys.executable
    original_which = common.shutil.which
    with tempfile.TemporaryDirectory(prefix="memory validation ") as root:
        try:
            bin_dir = os.path.join(root, "venv bin")
            path_dir = os.path.join(root, "path bin")
            os.makedirs(bin_dir, exist_ok=True)
            os.makedirs(path_dir, exist_ok=True)
            script_name = "mcp-memory-agent-hook"
            module_name = "mcp_memory_agent.hook_handler"
            executable = os.path.join(bin_dir, "python")
            local_script = os.path.join(bin_dir, script_name)
            path_script = os.path.join(path_dir, script_name)
            calls = []

            def fake_which(name: str) -> str | None:
                calls.append(name)
                if name == script_name:
                    return path_script
                return None

            def fake_missing_which(name: str) -> str | None:
                return None

            common.sys.executable = executable
            common.shutil.which = fake_which

            with open(local_script, "w") as f:
                f.write("#!/bin/sh\n")
            local_result = common.entry_point_command(script_name, module_name)

            os.remove(local_script)
            path_result = common.entry_point_command(script_name, module_name)

            common.shutil.which = fake_missing_which
            fallback_result = common.entry_point_command(script_name, module_name)

            expected_fallback = f"{shlex.quote(executable)} -m {module_name}"
            if (
                local_result == shlex.quote(local_script)
                and path_result == shlex.quote(path_script)
                and fallback_result == expected_fallback
                and calls == [script_name]
            ):
                ok(name)
            else:
                fail(
                    name,
                    (
                        f"local={local_result!r} path={path_result!r} "
                        f"fallback={fallback_result!r} calls={calls!r}"
                    ),
                )
        except Exception as e:
            fail(name, str(e))
        finally:
            common.sys.executable = original_executable
            common.shutil.which = original_which


def test_install_main_routes_clients_and_sums_hooks() -> None:
    import contextlib
    import io
    import sys

    name = "install_main_routes_clients_and_sums_hooks"
    original_argv = sys.argv
    original_claude_install = install.claude.install
    original_codex_install = install.codex.install
    calls = []

    def fake_claude_install() -> int:
        calls.append(("claude", ""))
        return 2

    def fake_codex_install(project_dir: str) -> int:
        calls.append(("codex", project_dir))
        return 3

    def run_install(client: str, project_dir: str) -> tuple[list[tuple[str, str]], str]:
        calls.clear()
        sys.argv = [
            "install",
            "--client",
            client,
            "--project-dir",
            project_dir,
        ]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            install.main()
        return calls[:], output.getvalue()

    with tempfile.TemporaryDirectory() as root:
        try:
            install.claude.install = fake_claude_install
            install.codex.install = fake_codex_install

            claude_project = os.path.join(root, "claude-project")
            codex_project = os.path.join(root, "codex-project")
            both_project = os.path.join(root, "both-project")
            claude_calls, claude_output = run_install("claude", claude_project)
            codex_calls, codex_output = run_install("codex", codex_project)
            both_calls, both_output = run_install("both", both_project)

            if (
                claude_calls == [("claude", "")]
                and codex_calls == [("codex", codex_project)]
                and both_calls == [("claude", ""), ("codex", both_project)]
                and "Hooks added: 2 " in claude_output
                and "Hooks added: 3 " in codex_output
                and "Hooks added: 5 " in both_output
            ):
                ok(name)
            else:
                fail(
                    name,
                    (
                        f"claude_calls={claude_calls!r} "
                        f"codex_calls={codex_calls!r} both_calls={both_calls!r} "
                        f"claude_output={claude_output!r} "
                        f"codex_output={codex_output!r} both_output={both_output!r}"
                    ),
                )
        except Exception as e:
            fail(name, str(e))
        finally:
            install.claude.install = original_claude_install
            install.codex.install = original_codex_install
            sys.argv = original_argv


def test_claude_load_settings_returns_empty_for_missing_and_bad_json() -> None:
    name = "claude_load_settings_returns_empty_for_missing_and_bad_json"
    with tempfile.TemporaryDirectory() as root:
        try:
            redirect_claude_settings(root)
            missing = claude.load_settings()

            os.makedirs(os.path.dirname(claude.SETTINGS_PATH), exist_ok=True)
            with open(claude.SETTINGS_PATH, "w") as f:
                f.write("{bad json")
            corrupt = claude.load_settings()

            with open(claude.SETTINGS_PATH, "w") as f:
                json.dump(["not", "a", "dict"], f)
            non_dict = claude.load_settings()

            assert_result(
                name,
                missing == {} and corrupt == {} and non_dict == {},
                f"missing={missing!r} corrupt={corrupt!r} non_dict={non_dict!r}",
            )
        finally:
            restore_claude_settings()


def test_claude_entry_has_command_matches_only_list_hooks() -> None:
    command = "mcp-memory-agent-hook record --kind prompt"
    matching = {"hooks": [{"type": "command", "command": command}]}
    non_matching = {"hooks": [{"type": "command", "command": "other"}]}
    non_list_hooks = {"hooks": {"type": "command", "command": command}}

    assert_result(
        "claude_entry_has_command_matches_only_list_hooks",
        claude.entry_has_command(matching, command)
        and not claude.entry_has_command(non_matching, command)
        and not claude.entry_has_command(non_list_hooks, command),
        (
            f"matching={claude.entry_has_command(matching, command)!r} "
            f"non_matching={claude.entry_has_command(non_matching, command)!r} "
            f"non_list={claude.entry_has_command(non_list_hooks, command)!r}"
        ),
    )


def test_claude_install_hooks_adds_all_events_and_is_idempotent() -> None:
    name = "claude_install_hooks_adds_all_events_and_is_idempotent"
    with tempfile.TemporaryDirectory() as root:
        try:
            redirect_claude_settings(root)
            assert_claude_settings_in_tempdir(root)
            added = claude.install_hooks()
            assert_claude_settings_in_tempdir(root)
            second_added = claude.install_hooks()

            with open(claude.SETTINGS_PATH) as f:
                settings = json.load(f)
            hooks = settings.get("hooks", {})
            expected = {
                event: [claude.hook_command(args) for args in arg_lists]
                for event, arg_lists in claude.HOOK_EVENTS.items()
            }
            actual = {
                event: [
                    entry["hooks"][0]["command"]
                    for entry in hooks.get(event, [])
                    if isinstance(entry, dict) and entry.get("hooks")
                ]
                for event in claude.HOOK_EVENTS
            }

            assert_result(
                name,
                added == 5
                and second_added == 0
                and set(claude.HOOK_EVENTS).issubset(hooks)
                and len(hooks.get("SessionStart", [])) == 2
                and actual == expected,
                (
                    f"added={added} second={second_added} "
                    f"events={list(hooks.keys())!r} actual={actual!r} expected={expected!r}"
                ),
            )
        finally:
            restore_claude_settings()


def test_claude_backup_settings_copies_existing_settings() -> None:
    name = "claude_backup_settings_copies_existing_settings"
    with tempfile.TemporaryDirectory() as root:
        try:
            redirect_claude_settings(root)
            os.makedirs(os.path.dirname(claude.SETTINGS_PATH), exist_ok=True)
            with open(claude.SETTINGS_PATH, "w") as f:
                json.dump({"hooks": {"Existing": []}}, f)

            assert_claude_settings_in_tempdir(root)
            claude.backup_settings()

            with open(claude.SETTINGS_BACKUP) as f:
                backup = json.load(f)
            assert_result(
                name,
                backup == {"hooks": {"Existing": []}},
                f"backup={backup!r} path={claude.SETTINGS_BACKUP!r}",
            )
        finally:
            restore_claude_settings()


def test_codex_load_hooks_returns_default_for_missing_and_bad_shapes() -> None:
    name = "codex_load_hooks_returns_default_for_missing_and_bad_shapes"
    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, ".codex", "hooks.json")
        missing = codex.load_hooks(path)

        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("{bad json")
        corrupt = codex.load_hooks(path)

        with open(path, "w") as f:
            json.dump(["not", "a", "dict"], f)
        non_dict = codex.load_hooks(path)

        with open(path, "w") as f:
            json.dump({"hooks": ["bad"], "keep": True}, f)
        bad_hooks = codex.load_hooks(path)

        assert_result(
            name,
            missing == {"hooks": {}}
            and corrupt == {"hooks": {}}
            and non_dict == {"hooks": {}}
            and bad_hooks == {"hooks": {}, "keep": True},
            (
                f"missing={missing!r} corrupt={corrupt!r} "
                f"non_dict={non_dict!r} bad_hooks={bad_hooks!r}"
            ),
        )


def test_codex_install_hooks_writes_all_events_and_is_idempotent() -> None:
    name = "codex_install_hooks_writes_all_events_and_is_idempotent"
    env_snapshot = snapshot_codex_env()
    with tempfile.TemporaryDirectory() as root:
        try:
            clear_codex_env()
            added = codex.install_hooks(root)
            second_added = codex.install_hooks(root)
            path = codex.hook_path(root)

            with open(path) as f:
                data = json.load(f)
            hooks = data.get("hooks", {})
            expected = {
                event: [
                    (spec.get("matcher", ""), codex.hook_command(spec["args"]))
                    for spec in specs
                ]
                for event, specs in codex.HOOK_EVENTS.items()
            }
            actual = {
                event: [
                    (entry.get("matcher", ""), entry["hooks"][0]["command"])
                    for entry in hooks.get(event, [])
                    if isinstance(entry, dict) and entry.get("hooks")
                ]
                for event in codex.HOOK_EVENTS
            }
            expected_added = sum(len(entries) for entries in codex.HOOK_EVENTS.values())

            assert_result(
                name,
                added == expected_added
                and second_added == 0
                and os.path.exists(path)
                and actual == expected,
                (
                    f"added={added} expected_added={expected_added} "
                    f"second={second_added} actual={actual!r} expected={expected!r}"
                ),
            )
        finally:
            restore_codex_env(env_snapshot)


def test_codex_env_helpers_include_only_set_keys_and_quote_commands() -> None:
    name = "codex_env_helpers_include_only_set_keys_and_quote_commands"
    env_snapshot = snapshot_codex_env()
    expected_env = "LLM_BACKEND=lm studio's test"
    try:
        clear_codex_env()
        os.environ["LLM_BACKEND"] = "lm studio's test"
        env_args = codex.mcp_env_args()
        prefix = codex.hook_env_command_prefix()
        command = codex.hook_command(["record", "--kind", "prompt value"])
        split_command = shlex.split(command)

        assert_result(
            name,
            env_args == ["--env", expected_env]
            and prefix == ["env", expected_env]
            and expected_env in split_command
            and split_command[-3:] == ["record", "--kind", "prompt value"]
            and shlex.quote(expected_env) in command
            and shlex.quote("prompt value") in command,
            (
                f"env_args={env_args!r} prefix={prefix!r} "
                f"command={command!r} split={split_command!r}"
            ),
        )
    finally:
        restore_codex_env(env_snapshot)


def test_codex_hook_command_quotes_shell_sensitive_args() -> None:
    name = "codex_hook_command_quotes_shell_sensitive_args"
    env_snapshot = snapshot_codex_env()
    try:
        clear_codex_env()
        command = codex.hook_command(["record", "--kind", "prompt value", "semi;colon"])
        split_command = shlex.split(command)
        assert_result(
            name,
            split_command[-4:] == ["record", "--kind", "prompt value", "semi;colon"]
            and shlex.quote("prompt value") in command
            and shlex.quote("semi;colon") in command,
            f"command={command!r} split={split_command!r}",
        )
    finally:
        restore_codex_env(env_snapshot)


def test_llm_safe_fallbacks_when_llm_call_raises() -> None:
    def raising_llm_call(system: str, user: str) -> str:
        raise RuntimeError("offline failure")

    llm.llm_call = raising_llm_call
    metadata = llm.extract_memory_metadata("remember the fallback", "global", [])
    candidates = [
        MemoryRecord(id="mem-1", content="first memory", importance=5),
        MemoryRecord(id="mem-2", content="second memory", importance=4),
        MemoryRecord(id="mem-3", content="third memory", importance=3),
    ]
    ranked = llm.rank_memories("memory", candidates, 2)

    if (
        metadata.category == "project_knowledge"
        and metadata.importance == 3
        and metadata.tags == ""
        and ranked == candidates[:2]
    ):
        ok("llm_safe_fallbacks_when_llm_call_raises")
    else:
        fail(
            "llm_safe_fallbacks_when_llm_call_raises",
            f"metadata={metadata.model_dump()} ranked={ranked!r}",
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


def test_insert_memory_explicit_category_skips_llm_and_sets_importance() -> None:
    reset_db()
    calls = {"count": 0}

    def should_not_call(system, user):
        calls["count"] += 1
        raise AssertionError("explicit category should not call the LLM")

    llm.llm_call = should_not_call
    conn = db.get_db()
    open_result = tools._insert_memory(
        conn,
        "add coverage for direct open action insert",
        "__validation__",
        "test",
        category="OPEN_ACTION",
        tags=" Tests, offline ",
    )
    decision_result = tools._insert_memory(
        conn,
        "record the direct category branch decision",
        "__validation__",
        "test",
        category="code_decision",
        tags=" decision ",
    )
    conn.commit()
    rows = conn.execute(
        """
        SELECT id, content, category, tags, importance FROM memories
        WHERE scope = ?
        """,
        ("__validation__",),
    ).fetchall()
    by_content = {row["content"]: row for row in rows}
    open_row = by_content.get("add coverage for direct open action insert")
    decision_row = by_content.get("record the direct category branch decision")
    fts_ok = (
        open_row
        and decision_row
        and fts_has_memory(
            conn,
            open_row["id"],
            "add coverage for direct open action insert",
            "tests,offline",
        )
        and fts_has_memory(
            conn,
            decision_row["id"],
            "record the direct category branch decision",
            "decision",
        )
    )
    conn.close()

    if (
        calls["count"] == 0
        and open_result.startswith("Stored memory ")
        and decision_result.startswith("Stored memory ")
        and len(rows) == 2
        and open_row
        and open_row["category"] == "open_action"
        and open_row["tags"] == "tests,offline"
        and open_row["importance"] == 4
        and decision_row
        and decision_row["category"] == "code_decision"
        and decision_row["tags"] == "decision"
        and decision_row["importance"] == 3
        and fts_ok
    ):
        ok("insert_memory_explicit_category_skips_llm_and_sets_importance")
    else:
        fail(
            "insert_memory_explicit_category_skips_llm_and_sets_importance",
            f"calls={calls['count']} open={dict(open_row) if open_row else None} "
            f"decision={dict(decision_row) if decision_row else None} "
            f"fts_ok={fts_ok} results={[open_result, decision_result]}",
        )


def test_insert_memory_merges_existing_memory_in_place() -> None:
    reset_db()
    conn = db.get_db()
    seed_result = tools._insert_memory(
        conn,
        "old merge target content",
        "__validation__",
        "seed",
        category="project_knowledge",
        tags="old",
    )
    seed_id = seed_result.split()[2]
    old_updated_at = "2020-01-01T00:00:00+00:00"
    conn.execute(
        "UPDATE memories SET updated_at = ? WHERE id = ?",
        (old_updated_at, seed_id),
    )
    conn.commit()

    llm.llm_call = lambda system, user: json.dumps(
        {
            "category": "code_decision",
            "tags": " Merge, direct ",
            "importance": 5,
            "merge_with_id": seed_id,
            "merged_content": "merged target content with new detail",
        }
    )
    before_count = conn.execute(
        "SELECT COUNT(*) AS n FROM memories WHERE scope = ?",
        ("__validation__",),
    ).fetchone()["n"]
    result = tools._insert_memory(
        conn,
        "new merge detail",
        "__validation__",
        "test",
    )
    conn.commit()
    after_count = conn.execute(
        "SELECT COUNT(*) AS n FROM memories WHERE scope = ?",
        ("__validation__",),
    ).fetchone()["n"]
    row = conn.execute(
        "SELECT * FROM memories WHERE id = ? AND scope = ?",
        (seed_id, "__validation__"),
    ).fetchone()
    fts_ok = fts_has_memory(
        conn,
        seed_id,
        "merged target content with new detail",
        "merge,direct",
    )
    conn.close()

    if (
        result == f"Merged with existing memory {seed_id}"
        and before_count == 1
        and after_count == 1
        and row
        and row["content"] == "merged target content with new detail"
        and row["category"] == "code_decision"
        and row["tags"] == "merge,direct"
        and row["importance"] == 5
        and row["updated_at"] != old_updated_at
        and fts_ok
    ):
        ok("insert_memory_merges_existing_memory_in_place")
    else:
        fail(
            "insert_memory_merges_existing_memory_in_place",
            f"result={result!r} counts=({before_count},{after_count}) "
            f"row={dict(row) if row else None} fts_ok={fts_ok}",
        )


def test_insert_memory_drops_stale_merge_id_and_inserts_fresh_row() -> None:
    reset_db()
    conn = db.get_db()
    seed_result = tools._insert_memory(
        conn,
        "existing guard target remains untouched",
        "__validation__",
        "seed",
        category="project_knowledge",
    )
    seed_id = seed_result.split()[2]
    conn.commit()

    stale_id = "99999999-9999-9999-9999-999999999999"
    llm.llm_call = lambda system, user: json.dumps(
        {
            "category": "session_summary",
            "tags": " guard ",
            "importance": 4,
            "merge_with_id": stale_id,
            "merged_content": "should not overwrite anything",
        }
    )
    before_count = conn.execute(
        "SELECT COUNT(*) AS n FROM memories WHERE scope = ?",
        ("__validation__",),
    ).fetchone()["n"]
    result = tools._insert_memory(
        conn,
        "fresh row after stale merge guard",
        "__validation__",
        "test",
    )
    conn.commit()
    after_count = conn.execute(
        "SELECT COUNT(*) AS n FROM memories WHERE scope = ?",
        ("__validation__",),
    ).fetchone()["n"]
    seed_row = conn.execute(
        "SELECT content FROM memories WHERE id = ?",
        (seed_id,),
    ).fetchone()
    stale_row = conn.execute(
        "SELECT id FROM memories WHERE id = ?",
        (stale_id,),
    ).fetchone()
    fresh_id = result.split()[2] if result.startswith("Stored memory ") else ""
    fresh_row = conn.execute(
        "SELECT * FROM memories WHERE id = ?",
        (fresh_id,),
    ).fetchone()
    fts_ok = bool(fresh_row) and fts_has_memory(
        conn,
        fresh_id,
        "fresh row after stale merge guard",
        "guard",
    )
    conn.close()

    if (
        result.startswith("Stored memory ")
        and before_count == 1
        and after_count == 2
        and seed_row
        and seed_row["content"] == "existing guard target remains untouched"
        and stale_row is None
        and fresh_row
        and fresh_row["content"] == "fresh row after stale merge guard"
        and fresh_row["category"] == "session_summary"
        and fresh_row["tags"] == "guard"
        and fresh_row["importance"] == 4
        and fts_ok
    ):
        ok("insert_memory_drops_stale_merge_id_and_inserts_fresh_row")
    else:
        fail(
            "insert_memory_drops_stale_merge_id_and_inserts_fresh_row",
            f"result={result!r} counts=({before_count},{after_count}) "
            f"seed={dict(seed_row) if seed_row else None} "
            f"fresh={dict(fresh_row) if fresh_row else None} fts_ok={fts_ok}",
        )


def test_gather_candidates_returns_fts_matchable_records() -> None:
    reset_db()
    conn = db.get_db()
    match_result = tools._insert_memory(
        conn,
        "vectorclock retrieval target",
        "__validation__",
        "test",
        category="project_knowledge",
        tags="retrieval",
    )
    miss_result = tools._insert_memory(
        conn,
        "unrelated checkpoint note",
        "__validation__",
        "test",
        category="project_knowledge",
        tags="other",
    )
    conn.commit()
    match_id = match_result.split()[2]
    miss_id = miss_result.split()[2]

    candidates = tools._gather_candidates(
        conn,
        "vectorclock",
        scope="__validation__",
    )
    ids = [candidate.id for candidate in candidates]
    conn.close()

    if match_id in ids and miss_id not in ids:
        ok("gather_candidates_returns_fts_matchable_records")
    else:
        fail(
            "gather_candidates_returns_fts_matchable_records",
            f"ids={ids} match={match_id} miss={miss_id}",
        )


def test_gather_candidates_like_fallback_adds_substring_matches_once() -> None:
    reset_db()
    conn = db.get_db()
    exact_result = tools._insert_memory(
        conn,
        "pha exact token memory",
        "__validation__",
        "test",
        category="project_knowledge",
    )
    substring_result = tools._insert_memory(
        conn,
        "alpha substring memory",
        "__validation__",
        "test",
        category="project_knowledge",
    )
    miss_result = tools._insert_memory(
        conn,
        "beta unrelated memory",
        "__validation__",
        "test",
        category="project_knowledge",
    )
    conn.commit()
    exact_id = exact_result.split()[2]
    substring_id = substring_result.split()[2]
    miss_id = miss_result.split()[2]

    candidates = tools._gather_candidates(conn, "pha", scope="__validation__")
    ids = [candidate.id for candidate in candidates]
    conn.close()

    if (
        exact_id in ids
        and substring_id in ids
        and miss_id not in ids
        and len(ids) == len(set(ids))
    ):
        ok("gather_candidates_like_fallback_adds_substring_matches_once")
    else:
        fail(
            "gather_candidates_like_fallback_adds_substring_matches_once",
            f"ids={ids} exact={exact_id} substring={substring_id} miss={miss_id}",
        )


def test_gather_candidates_filters_scope_and_active_status() -> None:
    reset_db()
    conn = db.get_db()
    active_result = tools._insert_memory(
        conn,
        "scopefilter active target memory",
        "__validation__",
        "test",
        category="project_knowledge",
    )
    stale_result = tools._insert_memory(
        conn,
        "scopefilter stale target memory",
        "__validation__",
        "test",
        category="project_knowledge",
    )
    other_scope_result = tools._insert_memory(
        conn,
        "scopefilter other scope memory",
        "__other__",
        "test",
        category="project_knowledge",
    )
    conn.execute(
        "UPDATE memories SET status = 'stale' WHERE id = ?",
        (stale_result.split()[2],),
    )
    conn.commit()
    active_id = active_result.split()[2]
    stale_id = stale_result.split()[2]
    other_scope_id = other_scope_result.split()[2]

    default_candidates = tools._gather_candidates(
        conn,
        "scopefilter",
        scope="__validation__",
    )
    stale_candidates = tools._gather_candidates(
        conn,
        "scopefilter",
        scope="__validation__",
        status="stale",
    )
    default_ids = [candidate.id for candidate in default_candidates]
    stale_ids = [candidate.id for candidate in stale_candidates]
    conn.close()

    if (
        active_id in default_ids
        and stale_id not in default_ids
        and other_scope_id not in default_ids
        and stale_id in stale_ids
        and active_id not in stale_ids
        and other_scope_id not in stale_ids
    ):
        ok("gather_candidates_filters_scope_and_active_status")
    else:
        fail(
            "gather_candidates_filters_scope_and_active_status",
            f"default={default_ids} stale={stale_ids} "
            f"active={active_id} stale_id={stale_id} other={other_scope_id}",
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
    base = datetime(2026, 5, 1, tzinfo=UTC)
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
    active_ts = datetime(2026, 5, 1, tzinfo=UTC).isoformat()
    stale_ts = datetime(2026, 5, 2, tzinfo=UTC).isoformat()
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


def test_hook_derive_scope_uses_git_root_or_path_basename() -> None:
    with tempfile.TemporaryDirectory() as root:
        repo = os.path.abspath(os.path.join(root, "repo-alpha"))
        nested = os.path.join(repo, "nested", "child")
        no_git = os.path.abspath(os.path.join(root, "plain-leaf"))
        os.makedirs(os.path.join(repo, ".git"), exist_ok=True)
        os.makedirs(nested, exist_ok=True)
        os.makedirs(no_git, exist_ok=True)

        git_scope = hook_handler._derive_scope(nested)
        path_scope = hook_handler._derive_scope(no_git)
        empty_scope = hook_handler._derive_scope("")
        non_string_scope = hook_handler._derive_scope(123)

        assert_result(
            "hook_derive_scope_uses_git_root_or_path_basename",
            git_scope == "repo-alpha"
            and path_scope == "plain-leaf"
            and empty_scope == "global"
            and non_string_scope == "global",
            (
                f"git={git_scope!r} path={path_scope!r} empty={empty_scope!r} "
                f"non_string={non_string_scope!r}"
            ),
        )


def test_hook_payload_value_respects_precedence_and_defaults() -> None:
    payload = {
        "first": "",
        "second": None,
        "third": "selected",
        "fourth": "ignored",
    }
    selected = hook_handler._payload_value(payload, ["first", "second", "third"])
    early = hook_handler._payload_value(payload, ["fourth", "third"])
    missing = hook_handler._payload_value(payload, ["missing", "first"], "fallback")

    assert_result(
        "hook_payload_value_respects_precedence_and_defaults",
        selected == "selected" and early == "ignored" and missing == "fallback",
        f"selected={selected!r} early={early!r} missing={missing!r}",
    )


def test_hook_payload_cwd_aliases_and_pwd_fallback() -> None:
    original_pwd = os.environ.get("PWD")
    try:
        os.environ["PWD"] = "/tmp/pwd-fallback"
        aliases = [
            ("cwd", "/tmp/cwd"),
            ("workdir", "/tmp/workdir"),
            ("working_dir", "/tmp/working-dir"),
            ("workingDirectory", "/tmp/working-directory"),
        ]
        alias_results = [
            hook_handler._payload_cwd({key: value})
            for key, value in aliases
        ]
        precedence = hook_handler._payload_cwd(
            {"cwd": "", "workdir": "/tmp/workdir-selected"}
        )
        fallback = hook_handler._payload_cwd({})

        assert_result(
            "hook_payload_cwd_aliases_and_pwd_fallback",
            alias_results
            == [
                "/tmp/cwd",
                "/tmp/workdir",
                "/tmp/working-dir",
                "/tmp/working-directory",
            ]
            and precedence == "/tmp/workdir-selected"
            and fallback == "/tmp/pwd-fallback",
            (
                f"aliases={alias_results!r} precedence={precedence!r} "
                f"fallback={fallback!r}"
            ),
        )
    finally:
        if original_pwd is None:
            os.environ.pop("PWD", None)
        else:
            os.environ["PWD"] = original_pwd


def test_hook_payload_session_id_aliases_and_env_fallback() -> None:
    env_keys = [
        "MEMORY_AGENT_SESSION_ID",
        "CLAUDE_SESSION_ID",
        "CODEX_SESSION_ID",
    ]
    original_env = {key: os.environ.get(key) for key in env_keys}
    try:
        for key in env_keys:
            os.environ.pop(key, None)

        aliases = [
            ("session_id", "session-a"),
            ("sessionId", "session-b"),
            ("conversation_id", "conversation-a"),
            ("conversationId", "conversation-b"),
            ("thread_id", "thread-a"),
            ("threadId", "thread-b"),
        ]
        alias_results = [
            hook_handler._payload_session_id({key: value})
            for key, value in aliases
        ]
        no_fallback = hook_handler._payload_session_id({})

        os.environ["MEMORY_AGENT_SESSION_ID"] = "memory-env"
        os.environ["CLAUDE_SESSION_ID"] = "claude-env"
        os.environ["CODEX_SESSION_ID"] = "codex-env"
        env_fallback = hook_handler._payload_session_id({})

        assert_result(
            "hook_payload_session_id_aliases_and_env_fallback",
            alias_results
            == [
                "session-a",
                "session-b",
                "conversation-a",
                "conversation-b",
                "thread-a",
                "thread-b",
            ]
            and no_fallback == ""
            and env_fallback == "memory-env",
            (
                f"aliases={alias_results!r} no_fallback={no_fallback!r} "
                f"env_fallback={env_fallback!r}"
            ),
        )
    finally:
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_hook_truncate_limits_strings_dicts_and_lists() -> None:
    long_string = hook_handler._truncate("abcdef", limit=3)
    unchanged_string = hook_handler._truncate("abc", limit=3)
    truncated_dict = hook_handler._truncate(
        {f"k{idx}": "abcdef" for idx in range(25)}, limit=2
    )
    truncated_list = hook_handler._truncate(["abcdef" for _ in range(25)], limit=2)
    passthrough = hook_handler._truncate(42, limit=2)

    assert_result(
        "hook_truncate_limits_strings_dicts_and_lists",
        long_string == "abc" + "\u2026"
        and unchanged_string == "abc"
        and isinstance(truncated_dict, dict)
        and list(truncated_dict) == [f"k{idx}" for idx in range(20)]
        and set(truncated_dict.values()) == {"ab" + "\u2026"}
        and truncated_list == ["ab" + "\u2026" for _ in range(20)]
        and passthrough == 42,
        (
            f"long={long_string!r} unchanged={unchanged_string!r} "
            f"dict_len={len(truncated_dict) if isinstance(truncated_dict, dict) else None} "
            f"list_len={len(truncated_list) if isinstance(truncated_list, list) else None} "
            f"passthrough={passthrough!r}"
        ),
    )


def test_hook_session_path_sanitizes_ids_under_sessions_dir() -> None:
    original_sessions_dir = db.SESSIONS_DIR
    with tempfile.TemporaryDirectory() as root:
        try:
            db.SESSIONS_DIR = os.path.join(root, "sessions")
            path = hook_handler._session_path("ab c/!D-_")
            non_string = hook_handler._session_path(123)
            empty_safe = hook_handler._session_path(" !/")

            assert_result(
                "hook_session_path_sanitizes_ids_under_sessions_dir",
                path == os.path.join(db.SESSIONS_DIR, "abcD-_.jsonl")
                and os.path.commonpath([path, db.SESSIONS_DIR]) == db.SESSIONS_DIR
                and non_string is None
                and empty_safe is None,
                f"path={path!r} non_string={non_string!r} empty={empty_safe!r}",
            )
        finally:
            db.SESSIONS_DIR = original_sessions_dir


def test_hook_append_event_writes_prompt_and_tool_use_only() -> None:
    original_sessions_dir = db.SESSIONS_DIR
    with tempfile.TemporaryDirectory() as root:
        try:
            db.SESSIONS_DIR = os.path.join(root, "sessions")
            repo = os.path.join(root, "repo-scope")
            nested = os.path.join(repo, "nested")
            os.makedirs(os.path.join(repo, ".git"), exist_ok=True)
            os.makedirs(nested, exist_ok=True)

            hook_handler._append_event(
                {
                    "session_id": "prompt session!/1",
                    "cwd": nested,
                    "prompt": "remember this",
                },
                "prompt",
            )
            prompt_path = os.path.join(db.SESSIONS_DIR, "promptsession1.jsonl")
            with open(prompt_path) as f:
                prompt_lines = f.readlines()
            prompt_event = json.loads(prompt_lines[0])

            hook_handler._append_event(
                {
                    "session_id": "tool-session_2",
                    "cwd": nested,
                    "tool_name": "Bash",
                    "tool_input": {"cmd": "pwd"},
                    "tool_response": {"stdout": nested},
                },
                "tool_use",
            )
            tool_path = os.path.join(db.SESSIONS_DIR, "tool-session_2.jsonl")
            with open(tool_path) as f:
                tool_lines = f.readlines()
            tool_event = json.loads(tool_lines[0])

            hook_handler._append_event(
                {
                    "session_id": "unknown !/session",
                    "cwd": nested,
                    "prompt": "do not write",
                },
                "unknown",
            )
            unknown_path = os.path.join(db.SESSIONS_DIR, "unknownsession.jsonl")

            assert_result(
                "hook_append_event_writes_prompt_and_tool_use_only",
                len(prompt_lines) == 1
                and prompt_event["kind"] == "prompt"
                and prompt_event["scope"] == "repo-scope"
                and prompt_event["data"] == {"prompt": "remember this"}
                and len(tool_lines) == 1
                and tool_event["kind"] == "tool_use"
                and tool_event["scope"] == "repo-scope"
                and tool_event["data"]["tool_name"] == "Bash"
                and tool_event["data"]["tool_input"] == {"cmd": "pwd"}
                and not os.path.exists(unknown_path),
                (
                    f"prompt_lines={len(prompt_lines)} prompt={prompt_event!r} "
                    f"tool_lines={len(tool_lines)} tool={tool_event!r} "
                    f"unknown_exists={os.path.exists(unknown_path)!r}"
                ),
            )
        finally:
            db.SESSIONS_DIR = original_sessions_dir


def test_hook_read_buffer_skips_invalid_lines_and_missing_files() -> None:
    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "buffer.jsonl")
        with open(path, "w") as f:
            f.write(json.dumps({"kind": "prompt"}) + "\n")
            f.write("\n")
            f.write("not json\n")
            f.write(json.dumps(["not", "a", "dict"]) + "\n")
            f.write(json.dumps({"kind": "tool_use"}) + "\n")

        events = hook_handler._read_buffer(path)
        missing = hook_handler._read_buffer(os.path.join(root, "missing.jsonl"))

        assert_result(
            "hook_read_buffer_skips_invalid_lines_and_missing_files",
            events == [{"kind": "prompt"}, {"kind": "tool_use"}] and missing == [],
            f"events={events!r} missing={missing!r}",
        )


def test_hook_remove_buffer_deletes_existing_and_ignores_missing() -> None:
    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "buffer.jsonl")
        with open(path, "w") as f:
            f.write("event\n")

        hook_handler._remove_buffer(path)
        removed = not os.path.exists(path)
        hook_handler._remove_buffer(path)

        assert_result(
            "hook_remove_buffer_deletes_existing_and_ignores_missing",
            removed and not os.path.exists(path),
            f"removed={removed!r} exists={os.path.exists(path)!r}",
        )


def test_hook_build_transcript_formats_limits_and_caps_events() -> None:
    events = [
        {"kind": "prompt", "data": {"prompt": "first"}},
        {"kind": "tool_use", "data": {"tool_name": "Bash", "tool_input": {"cmd": "pwd"}}},
        {"kind": "note", "data": {"detail": "ignored"}},
    ]
    transcript = hook_handler._build_transcript(events, max_chars=200)
    limited = hook_handler._build_transcript(events, max_chars=12)
    many_events = [
        {"kind": "prompt", "data": {"prompt": f"event-{idx}"}}
        for idx in range(201)
    ]
    capped = hook_handler._build_transcript(many_events, max_chars=10000)

    assert_result(
        "hook_build_transcript_formats_limits_and_caps_events",
        transcript == 'USER: first\nTOOL Bash: {"cmd": "pwd"}'
        and limited == "USER: first"
        and "event-199" in capped
        and "event-200" not in capped,
        (
            f"transcript={transcript!r} limited={limited!r} "
            f"contains199={'event-199' in capped} contains200={'event-200' in capped}"
        ),
    )


def test_hook_parse_string_list_filters_and_caps_items() -> None:
    non_list = hook_handler._parse_string_list("not-a-list")
    parsed = hook_handler._parse_string_list(
        [None, "", " alpha ", "beta", " ", "gamma", "delta"]
    )

    assert_result(
        "hook_parse_string_list_filters_and_caps_items",
        non_list == [] and parsed == ["alpha", "beta", "gamma"],
        f"non_list={non_list!r} parsed={parsed!r}",
    )


def test_hook_parse_memory_item_accepts_strings_and_dicts() -> None:
    from_string = hook_handler._parse_memory_item("  remember me  ")
    from_dict = hook_handler._parse_memory_item(
        {"content": "  stored content  ", "tags": "  one,two  "}
    )
    bad_dict = hook_handler._parse_memory_item({"content": 123, "tags": ["one"]})
    junk = hook_handler._parse_memory_item(["not", "valid"])

    assert_result(
        "hook_parse_memory_item_accepts_strings_and_dicts",
        from_string == ("remember me", "")
        and from_dict == ("stored content", "one,two")
        and bad_dict == ("", "")
        and junk == ("", ""),
        (
            f"string={from_string!r} dict={from_dict!r} "
            f"bad_dict={bad_dict!r} junk={junk!r}"
        ),
    )


def _write_summarize_buffer(session_id: str, event_count: int = 4) -> str:
    buffer_path = os.path.join(db.SESSIONS_DIR, f"{session_id}.jsonl")
    os.makedirs(db.SESSIONS_DIR, exist_ok=True)
    with open(buffer_path, "w") as f:
        for i in range(event_count):
            event = {
                "ts": datetime.now(UTC).isoformat(),
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

    import contextlib
    import io

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

    import contextlib
    import io

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

    import contextlib
    import io

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

    import contextlib
    import io

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

    import contextlib
    import io

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

    import contextlib
    import io

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
                    "ts": datetime.now(UTC).isoformat(),
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
        datetime.now(UTC).isoformat(),
        archive_path,
        db.build_archive_index_text(archive_path),
    )
    conn.commit()
    conn.close()

    import contextlib
    import io

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
    db.write_curator_last_run(datetime.now(UTC).isoformat())
    calls = {"count": 0}
    llm.llm_call = lambda s, u: (calls.update(count=calls["count"] + 1) or "{}")

    hook_handler._curator({"cwd": TEST_ROOT})

    if calls["count"] == 0:
        ok("curator_skips_within_interval")
    else:
        fail("curator_skips_within_interval", f"llm calls={calls['count']}")


def test_curator_runs_lifecycle_and_proposal() -> None:
    reset_db()
    old = (datetime.now(UTC) - timedelta(days=40)).isoformat()
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
    old = (datetime.now(UTC) - timedelta(days=40)).isoformat()
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
                    "ts": datetime.now(UTC).isoformat(),
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
        datetime.now(UTC).isoformat(),
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
    now = datetime.now(UTC).isoformat()
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
                    "ts": datetime.now(UTC).isoformat(),
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
        datetime.now(UTC).isoformat(),
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


def test_extract_search_terms_filters_and_limits() -> None:
    reset_db()
    query = "the ai c++ memory? and db go pipe|danger alpha beta gamma aftercap"
    terms = db.extract_search_terms(query, 10)
    limited = db.extract_search_terms(query, 2)
    unsafe_chars = set("+-:\"*()|?")

    if (
        terms == ["memory", "pipedanger", "alpha", "beta"]
        and limited == ["memory", "pipedanger"]
        and all(not unsafe_chars.intersection(term) for term in terms)
    ):
        ok("extract_search_terms_filters_and_limits")
    else:
        fail(
            "extract_search_terms_filters_and_limits",
            f"terms={terms} limited={limited}",
        )


def test_build_memory_filters_composes_conditions() -> None:
    reset_db()
    empty = db.build_memory_filters()
    filtered = db.build_memory_filters(
        scope="__validation__",
        category="project_knowledge",
        status="stale",
    )
    aliased = db.build_memory_filters(scope="global", alias="m", status="active")

    if (
        empty == ([], [])
        and filtered
        == (
            ["scope = ?", "category = ?", "status = ?"],
            ["__validation__", "project_knowledge", "stale"],
        )
        and aliased == (["m.scope = ?", "m.status = ?"], ["global", "active"])
    ):
        ok("build_memory_filters_composes_conditions")
    else:
        fail(
            "build_memory_filters_composes_conditions",
            f"empty={empty} filtered={filtered} aliased={aliased}",
        )


def test_format_archive_event_outputs_expected_text() -> None:
    reset_db()
    ts = "2026-06-22T12:34:56+00:00"
    prompt = db.format_archive_event(
        {"ts": ts, "kind": "prompt", "data": {"prompt": "hello memory"}}
    )
    tool = db.format_archive_event(
        {
            "ts": ts,
            "kind": "tool_use",
            "data": {
                "tool_name": "Read",
                "tool_input": {"path": "AGENTS.md"},
                "tool_response": {},
            },
        }
    )
    generic = db.format_archive_event(
        {"ts": ts, "kind": "note", "data": {"detail": "done"}}
    )

    if (
        prompt == "[2026-06-22T12:34:56] USER: hello memory"
        and tool == '[2026-06-22T12:34:56] TOOL Read: {"path": "AGENTS.md"}'
        and generic == '[2026-06-22T12:34:56] note: {"detail": "done"}'
    ):
        ok("format_archive_event_outputs_expected_text")
    else:
        fail(
            "format_archive_event_outputs_expected_text",
            f"prompt={prompt!r} tool={tool!r} generic={generic!r}",
        )


def test_flatten_archive_line_outputs_index_text() -> None:
    reset_db()
    prompt = db.flatten_archive_line(
        json.dumps({"kind": "prompt", "data": {"prompt": "hello memory"}})
    )
    tool = db.flatten_archive_line(
        json.dumps(
            {
                "kind": "tool_use",
                "data": {
                    "tool_name": "Bash",
                    "tool_input": {"cmd": "git status"},
                    "tool_response": {"stdout": "clean"},
                },
            }
        )
    )
    passthrough = db.flatten_archive_line("not json at all")

    if (
        prompt == "prompt hello memory"
        and tool == 'tool_use Bash {"cmd": "git status"} {"stdout": "clean"}'
        and passthrough == "not json at all"
    ):
        ok("flatten_archive_line_outputs_index_text")
    else:
        fail(
            "flatten_archive_line_outputs_index_text",
            f"prompt={prompt!r} tool={tool!r} passthrough={passthrough!r}",
        )


def test_build_archive_index_text_flattens_file() -> None:
    reset_db()
    archive_path = db.archive_session_path("__validation__", "index-text-001")
    os.makedirs(os.path.dirname(archive_path), exist_ok=True)
    with open(archive_path, "w") as f:
        f.write(
            json.dumps({"kind": "prompt", "data": {"prompt": "index alpha"}})
            + "\n"
        )
        f.write("\n")
        f.write(
            json.dumps(
                {
                    "kind": "tool_use",
                    "data": {"tool_name": "Bash", "tool_input": {"cmd": "pwd"}},
                }
            )
            + "\n"
        )
        f.write("plain transcript line\n")

    text = db.build_archive_index_text(archive_path)
    expected = "\n".join(
        [
            "prompt index alpha",
            'tool_use Bash {"cmd": "pwd"}',
            "plain transcript line",
        ]
    )

    if text == expected:
        ok("build_archive_index_text_flattens_file")
    else:
        fail("build_archive_index_text_flattens_file", text)


def test_read_archive_transcript_limits_and_missing() -> None:
    reset_db()
    ts = "2026-06-22T12:34:56+00:00"
    archive_path = db.archive_session_path("__validation__", "read-limit-001")
    os.makedirs(os.path.dirname(archive_path), exist_ok=True)
    with open(archive_path, "w") as f:
        f.write(
            json.dumps(
                {"ts": ts, "kind": "prompt", "data": {"prompt": "first prompt"}}
            )
            + "\n"
        )
        f.write(
            json.dumps(
                {"ts": ts, "kind": "note", "data": {"detail": "second event"}}
            )
            + "\n"
        )
        f.write(
            json.dumps(
                {"ts": ts, "kind": "prompt", "data": {"prompt": "third prompt"}}
            )
            + "\n"
        )

    transcript = db.read_archive_transcript(archive_path, limit=2)
    missing = db.read_archive_transcript(os.path.join(TEST_ROOT, "missing.jsonl"))
    expected = "\n".join(
        [
            "[2026-06-22T12:34:56] USER: first prompt",
            '[2026-06-22T12:34:56] note: {"detail": "second event"}',
        ]
    )

    if transcript == expected and missing == "":
        ok("read_archive_transcript_limits_and_missing")
    else:
        fail(
            "read_archive_transcript_limits_and_missing",
            f"transcript={transcript!r} missing={missing!r}",
        )


def test_find_session_matches_full_and_prefix() -> None:
    reset_db()
    now = datetime.now(UTC).isoformat()
    session_id = "unique01-session-001"
    archive_path = db.archive_session_path("__validation__", session_id)
    os.makedirs(os.path.dirname(archive_path), exist_ok=True)
    with open(archive_path, "w") as f:
        f.write(
            json.dumps(
                {
                    "ts": now,
                    "kind": "prompt",
                    "scope": "__validation__",
                    "data": {"prompt": "find session marker"},
                }
            )
            + "\n"
        )

    conn = db.get_db()
    db.upsert_session_archive_index(
        conn,
        session_id,
        "__validation__",
        now,
        archive_path,
        db.build_archive_index_text(archive_path),
    )
    conn.commit()
    by_full = db.find_session_matches(conn, session_id, "__validation__")
    by_prefix = db.find_session_matches(conn, session_id[:8], "__validation__")
    conn.close()

    if (
        len(by_full) == 1
        and by_full[0]["session_id"] == session_id
        and len(by_prefix) == 1
        and by_prefix[0]["session_id"] == session_id
    ):
        ok("find_session_matches_full_and_prefix")
    else:
        fail(
            "find_session_matches_full_and_prefix",
            f"by_full={by_full} by_prefix={by_prefix}",
        )


def test_resolve_session_id_requires_single_match() -> None:
    reset_db()
    now = datetime.now(UTC).isoformat()
    sessions = [
        "dupe0001-session-alpha",
        "dupe0001-session-beta",
        "solo0001-session-alpha",
    ]
    conn = db.get_db()
    for session_id in sessions:
        archive_path = db.archive_session_path("__validation__", session_id)
        os.makedirs(os.path.dirname(archive_path), exist_ok=True)
        with open(archive_path, "w") as f:
            f.write(
                json.dumps(
                    {
                        "ts": now,
                        "kind": "prompt",
                        "scope": "__validation__",
                        "data": {"prompt": f"resolve marker {session_id}"},
                    }
                )
                + "\n"
            )
        db.upsert_session_archive_index(
            conn,
            session_id,
            "__validation__",
            now,
            archive_path,
            db.build_archive_index_text(archive_path),
        )
    conn.commit()

    unique = db.resolve_session_id(conn, "solo0001", "__validation__")
    exact = db.resolve_session_id(conn, "dupe0001-session-alpha", "__validation__")
    ambiguous = db.resolve_session_id(conn, "dupe0001", "__validation__")
    conn.close()

    if (
        unique == "solo0001-session-alpha"
        and exact == "dupe0001-session-alpha"
        and ambiguous is None
    ):
        ok("resolve_session_id_requires_single_match")
    else:
        fail(
            "resolve_session_id_requires_single_match",
            f"unique={unique!r} exact={exact!r} ambiguous={ambiguous!r}",
        )


if __name__ == "__main__":
    try:
        test_extract_memory_metadata_defaults()
        test_extract_memory_metadata_normalizes_values()
        test_llm_call_dispatches_ollama_backend()
        test_llm_call_dispatches_unknown_backend_to_ollama()
        test_llm_call_dispatches_lm_studio_backend()
        test_llm_call_dispatches_lmstudio_alias()
        test_llm_call_dispatches_bedrock_backend()
        test_llm_call_dispatches_codex_backend()
        test_extract_json_object_parses_common_llm_shapes()
        test_extract_json_object_repairs_trailing_commas_and_falls_back()
        test_extract_json_array_parses_embedded_and_malformed_inputs()
        test_strip_fences_handles_tags_and_passthrough()
        test_repair_json_removes_trailing_commas()
        test_resolve_codex_bin_prefers_module_attr_then_path_default()
        test_codex_call_builds_offline_argv_and_cleans_output()
        test_entry_point_command_prefers_local_path_then_module()
        test_install_main_routes_clients_and_sums_hooks()
        test_claude_load_settings_returns_empty_for_missing_and_bad_json()
        test_claude_entry_has_command_matches_only_list_hooks()
        test_claude_install_hooks_adds_all_events_and_is_idempotent()
        test_claude_backup_settings_copies_existing_settings()
        test_codex_load_hooks_returns_default_for_missing_and_bad_shapes()
        test_codex_install_hooks_writes_all_events_and_is_idempotent()
        test_codex_env_helpers_include_only_set_keys_and_quote_commands()
        test_codex_hook_command_quotes_shell_sensitive_args()
        test_llm_safe_fallbacks_when_llm_call_raises()
        test_memory_query_clamps_limit()
        test_memory_query_tracks_access()
        test_memory_consolidate_ignores_invalid_actions()
        test_memory_consolidate_dry_run_by_default()
        test_curator_skips_within_interval()
        test_curator_runs_lifecycle_and_proposal()
        test_memory_index_no_llm_call()
        test_memory_index_excludes_stale_by_default()
        test_insert_memory_explicit_category_skips_llm_and_sets_importance()
        test_insert_memory_merges_existing_memory_in_place()
        test_insert_memory_drops_stale_merge_id_and_inserts_fresh_row()
        test_gather_candidates_returns_fts_matchable_records()
        test_gather_candidates_like_fallback_adds_substring_matches_once()
        test_gather_candidates_filters_scope_and_active_status()
        test_memory_get_full_and_prefix()
        test_memory_timeline_orders_and_filters()
        test_memory_list_and_timeline_exclude_stale_by_default()
        test_hook_derive_scope_uses_git_root_or_path_basename()
        test_hook_payload_value_respects_precedence_and_defaults()
        test_hook_payload_cwd_aliases_and_pwd_fallback()
        test_hook_payload_session_id_aliases_and_env_fallback()
        test_hook_truncate_limits_strings_dicts_and_lists()
        test_hook_session_path_sanitizes_ids_under_sessions_dir()
        test_hook_append_event_writes_prompt_and_tool_use_only()
        test_hook_read_buffer_skips_invalid_lines_and_missing_files()
        test_hook_remove_buffer_deletes_existing_and_ignores_missing()
        test_hook_build_transcript_formats_limits_and_caps_events()
        test_hook_parse_string_list_filters_and_caps_items()
        test_hook_parse_memory_item_accepts_strings_and_dicts()
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
        test_extract_search_terms_filters_and_limits()
        test_build_memory_filters_composes_conditions()
        test_format_archive_event_outputs_expected_text()
        test_flatten_archive_line_outputs_index_text()
        test_build_archive_index_text_flattens_file()
        test_read_archive_transcript_limits_and_missing()
        test_find_session_matches_full_and_prefix()
        test_resolve_session_id_requires_single_match()
    finally:
        restore_state()

    print(f"\nResults: {PASS} passed, {FAIL} failed out of {PASS + FAIL}")
    raise SystemExit(1 if FAIL > 0 else 0)
