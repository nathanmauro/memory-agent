"""Codex installer adapter."""

import json
import os
import shlex
import shutil
import subprocess

from .common import entry_point_command

HOOK_EVENTS = {
    "SessionStart": [
        {"matcher": "startup|resume", "args": ["inject-context"]},
        {"matcher": "startup|resume", "args": ["curator"]},
    ],
    "PostToolUse": [{"matcher": "", "args": ["record", "--kind", "tool_use"]}],
    "Stop": [{"matcher": "", "args": ["finalize-session"]}],
}
ENV_KEYS = [
    "MEMORY_AGENT_HOME",
    "LLM_BACKEND",
    "OLLAMA_URL",
    "OLLAMA_MODEL",
    "LM_STUDIO_URL",
    "LM_STUDIO_MODEL",
    "LM_STUDIO_MAX_TOKENS",
    "AWS_REGION",
    "AWS_PROFILE",
    "BEDROCK_MODEL",
]


def server_command() -> str:
    return entry_point_command("mcp-memory-agent-server", "mcp_memory_agent")


def hook_command(args: list[str]) -> str:
    command = entry_point_command(
        "mcp-memory-agent-hook", "mcp_memory_agent.hook_handler"
    )
    return " ".join([command, *map(shlex.quote, args)])


def has_cli() -> bool:
    return shutil.which("codex") is not None


def mcp_get() -> str:
    try:
        result = subprocess.run(
            ["codex", "mcp", "get", "memory"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout


def mcp_env_args() -> list[str]:
    args = []
    for key in ENV_KEYS:
        value = os.environ.get(key)
        if value:
            args.extend(["--env", f"{key}={value}"])
    return args


def register_mcp() -> None:
    if not has_cli():
        print(
            "  skipped: `codex` CLI not on PATH; edit ~/.codex/config.toml manually"
        )
        return

    command = server_command()
    env_args = mcp_env_args()
    current = mcp_get()
    if f"command: {command}" in current and not env_args:
        print("  already registered: Codex `memory`")
        return

    if current:
        try:
            subprocess.run(
                ["codex", "mcp", "remove", "memory"],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except Exception:
            pass

    cmd = [
        "codex",
        "mcp",
        "add",
        "memory",
        *env_args,
        "--",
        *shlex.split(command),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception as e:
        print(f"  failed: {e}")
        return
    if result.returncode != 0:
        print(f"  failed (exit {result.returncode}): {result.stderr.strip()}")
        return
    print("  registered: Codex `memory`")


def hook_path(project_dir: str) -> str:
    return os.path.join(project_dir, ".codex", "hooks.json")


def load_hooks(path: str) -> dict:
    if not os.path.exists(path):
        return {"hooks": {}}
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        return {"hooks": {}}
    if not isinstance(data, dict):
        return {"hooks": {}}
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        data["hooks"] = {}
    return data


def entry_has_command(entry: dict, command: str) -> bool:
    hooks = entry.get("hooks", [])
    if not isinstance(hooks, list):
        return False
    for h in hooks:
        if isinstance(h, dict) and h.get("command") == command:
            return True
    return False


def save_hooks(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def install_hooks(project_dir: str) -> int:
    path = hook_path(project_dir)
    data = load_hooks(path)
    hooks = data["hooks"]
    added = 0

    for event, entries in HOOK_EVENTS.items():
        hook_entries = hooks.get(event)
        if not isinstance(hook_entries, list):
            hook_entries = []
            hooks[event] = hook_entries
        for spec in entries:
            args = spec["args"]
            matcher = spec.get("matcher", "")
            command = hook_command(args)
            if any(
                isinstance(e, dict) and entry_has_command(e, command)
                for e in hook_entries
            ):
                print(f"  exists: Codex {event}")
                continue
            hook_entries.append(
                {
                    "matcher": matcher,
                    "hooks": [{"type": "command", "command": command}],
                }
            )
            print(f"  added:  Codex {event} -> {command}")
            added += 1

    save_hooks(path, data)
    return added


def install(project_dir: str) -> int:
    print("1. Registering Codex MCP server")
    register_mcp()
    print("2. Writing Codex project hook entries")
    print(f"   hooks file: {hook_path(project_dir)}")
    return install_hooks(project_dir)
