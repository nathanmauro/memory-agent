"""Claude Code installer adapter."""

import json
import os
import shlex
import shutil
import subprocess

from .common import entry_point_command

SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
SETTINGS_BACKUP = SETTINGS_PATH + ".bak"

HOOK_EVENTS = {
    "SessionStart": [["inject-context"], ["curator"]],
    "UserPromptSubmit": [["record", "--kind", "prompt"]],
    "PostToolUse": [["record", "--kind", "tool_use"]],
    "SessionEnd": [["summarize-session"]],
}


def server_command() -> str:
    return entry_point_command("mcp-memory-agent-server", "mcp_memory_agent")


def hook_command(args: list[str]) -> str:
    command = entry_point_command(
        "mcp-memory-agent-hook", "mcp_memory_agent.hook_handler"
    )
    return " ".join([command, *map(shlex.quote, args)])


def has_cli() -> bool:
    return shutil.which("claude") is not None


def is_mcp_registered() -> bool:
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


def register_mcp() -> None:
    if not has_cli():
        print(
            "  skipped: `claude` CLI not on PATH; register manually with "
            "`claude mcp add -s user memory <command>`"
        )
        return
    if is_mcp_registered():
        print("  already registered: Claude `memory`")
        return
    command = server_command()
    cmd = ["claude", "mcp", "add", "-s", "user", "memory", *shlex.split(command)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception as e:
        print(f"  failed: {e}")
        return
    if result.returncode != 0:
        print(f"  failed (exit {result.returncode}): {result.stderr.strip()}")
        return
    print(f"  registered: {' '.join(cmd)}")


def load_settings() -> dict:
    if not os.path.exists(SETTINGS_PATH):
        return {}
    try:
        with open(SETTINGS_PATH) as f:
            data = json.load(f)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def backup_settings() -> None:
    if os.path.exists(SETTINGS_PATH):
        shutil.copy2(SETTINGS_PATH, SETTINGS_BACKUP)


def save_settings(data: dict) -> None:
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    with open(SETTINGS_PATH, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def entry_has_command(entry: dict, command: str) -> bool:
    hooks = entry.get("hooks", [])
    if not isinstance(hooks, list):
        return False
    for h in hooks:
        if isinstance(h, dict) and h.get("command") == command:
            return True
    return False


def install_hooks() -> int:
    settings = load_settings()
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        settings["hooks"] = hooks

    backup_settings()

    added = 0
    for event, arg_lists in HOOK_EVENTS.items():
        if arg_lists and isinstance(arg_lists[0], str):
            arg_lists = [arg_lists]
        for args in arg_lists:
            command = hook_command(args)
            entries = hooks.get(event)
            if not isinstance(entries, list):
                entries = []
                hooks[event] = entries
            if any(
                isinstance(e, dict) and entry_has_command(e, command) for e in entries
            ):
                print(f"  exists: Claude {event}")
                continue
            entries.append(
                {
                    "matcher": "",
                    "hooks": [{"type": "command", "command": command}],
                }
            )
            print(f"  added:  Claude {event} -> {command}")
            added += 1

    save_settings(settings)
    return added


def install() -> int:
    print("1. Registering Claude MCP server")
    register_mcp()
    print("2. Writing Claude Code hook entries")
    return install_hooks()
