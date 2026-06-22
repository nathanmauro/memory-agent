"""
Local validation tests for pydantic-backed parsing and cleanup helpers.
Run: python -m tests.test_validation  (or `pytest tests/test_validation.py`)
"""

import json
import os
import shlex
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta

from mcp_memory_agent import db, hook_handler, hot, llm, tools
from mcp_memory_agent.integrations import claude, codex
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
