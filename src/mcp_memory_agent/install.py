"""Install the memory agent: register the MCP server and Claude Code hooks.

Idempotent. Safe to re-run.

What it does:
  1. Registers `memory` with `claude mcp add -s user` (if not already registered).
  2. Backs up ~/.claude/settings.json to settings.json.bak.
  3. Merges hook entries (SessionStart, UserPromptSubmit, PostToolUse, SessionEnd)
     keyed by the absolute command string.

After `pip install`, the package exposes two console scripts:
  - mcp-memory-agent-server  → `mcp_memory_agent.server:main`
  - mcp-memory-agent-hook    → `mcp_memory_agent.hook_handler:main`

This installer prefers those entry points; if they aren't on PATH (e.g. the
user is running from a source checkout without `pip install`), it falls back
to invoking the current Python interpreter with `-m mcp_memory_agent.<mod>`.
"""

import json
import os
import shlex
import shutil
import subprocess
import sys

SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
SETTINGS_BACKUP = SETTINGS_PATH + ".bak"

HOOK_EVENTS = {
    "SessionStart": [["inject-context"], ["curator"]],
    "UserPromptSubmit": [["record", "--kind", "prompt"]],
    "PostToolUse": [["record", "--kind", "tool_use"]],
    "SessionEnd": [["summarize-session"]],
}


def _has_claude_cli() -> bool:
    return shutil.which("claude") is not None


def _is_mcp_registered() -> bool:
    try:
        result = subprocess.run(
            ["claude", "mcp", "list"], capture_output=True, text=True, timeout=15
        )
    except Exception:
        return False
    if result.returncode != 0:
        return False
    for line in result.stdout.splitlines():
        if line.strip().startswith("memory") or line.strip().startswith("memory:"):
            return True
    return False


def _server_command() -> str:
    """Absolute command string for `claude mcp add`."""
    installed = shutil.which("mcp-memory-agent-server")
    if installed:
        return installed
    return f"{shlex.quote(sys.executable)} -m mcp_memory_agent"


def _hook_command(args: list[str]) -> str:
    """Absolute command string for a settings.json hook entry."""
    installed = shutil.which("mcp-memory-agent-hook")
    if installed:
        return " ".join([shlex.quote(installed), *map(shlex.quote, args)])
    return " ".join(
        [
            shlex.quote(sys.executable),
            "-m",
            "mcp_memory_agent.hook_handler",
            *map(shlex.quote, args),
        ]
    )


def register_mcp() -> None:
    if not _has_claude_cli():
        print(
            "  skipped: `claude` CLI not on PATH — register manually with `claude mcp add -s user memory <command>`"
        )
        return
    if _is_mcp_registered():
        print("  already registered: `memory`")
        return
    server_cmd = _server_command()
    cmd = ["claude", "mcp", "add", "-s", "user", "memory", *shlex.split(server_cmd)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception as e:
        print(f"  failed: {e}")
        return
    if result.returncode != 0:
        print(f"  failed (exit {result.returncode}): {result.stderr.strip()}")
        return
    print(f"  registered: {' '.join(cmd)}")


def _load_settings() -> dict:
    if not os.path.exists(SETTINGS_PATH):
        return {}
    try:
        with open(SETTINGS_PATH) as f:
            data = json.load(f)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _backup_settings() -> None:
    if os.path.exists(SETTINGS_PATH):
        shutil.copy2(SETTINGS_PATH, SETTINGS_BACKUP)


def _save_settings(data: dict) -> None:
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    with open(SETTINGS_PATH, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def _entry_has_command(entry: dict, command: str) -> bool:
    hooks = entry.get("hooks", [])
    if not isinstance(hooks, list):
        return False
    for h in hooks:
        if isinstance(h, dict) and h.get("command") == command:
            return True
    return False


def install_hooks() -> int:
    settings = _load_settings()
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        settings["hooks"] = hooks

    _backup_settings()

    added = 0
    for event, arg_lists in HOOK_EVENTS.items():
        if arg_lists and isinstance(arg_lists[0], str):
            arg_lists = [arg_lists]
        for args in arg_lists:
            command = _hook_command(args)
            entries = hooks.get(event)
            if not isinstance(entries, list):
                entries = []
                hooks[event] = entries
            if any(
                isinstance(e, dict) and _entry_has_command(e, command) for e in entries
            ):
                print(f"  exists: {event}")
                continue
            entries.append(
                {
                    "matcher": "",
                    "hooks": [{"type": "command", "command": command}],
                }
            )
            print(f"  added:  {event} -> {command}")
            added += 1

    _save_settings(settings)
    return added


def main() -> None:
    print("mcp-memory-agent install")
    print("------------------------")
    print("1. Registering MCP server")
    register_mcp()
    print("2. Writing Claude Code hook entries")
    added = install_hooks()
    print()
    print("Settings file:", SETTINGS_PATH)
    if os.path.exists(SETTINGS_BACKUP):
        print("Backup:       ", SETTINGS_BACKUP)
    print(f"Hooks added:   {added} (existing entries left untouched)")
    print()
    print("Next steps:")
    print("  - Ensure Ollama is running:    `ollama serve`")
    print("  - Pull a supported model:      `ollama pull qwen2.5:14b`")
    print("  - Restart Claude Code to pick up the new hooks and MCP server.")


if __name__ == "__main__":
    main()
