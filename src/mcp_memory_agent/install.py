"""Install the memory agent: register the MCP server and client hooks.

Idempotent. Safe to re-run.

Usage:
  python -m mcp_memory_agent.install --client claude
  python -m mcp_memory_agent.install --client codex
  python -m mcp_memory_agent.install --client both
"""

import argparse
import os

from .integrations import claude, codex


def main() -> None:
    parser = argparse.ArgumentParser(description="Install memory-agent MCP + hooks")
    parser.add_argument(
        "--client",
        choices=["claude", "codex", "both"],
        default="claude",
        help="Which client to configure (default: claude)",
    )
    parser.add_argument(
        "--project-dir",
        default=os.getcwd(),
        help="Project directory for Codex .codex/hooks.json (default: cwd)",
    )
    args = parser.parse_args()

    print("mcp-memory-agent install")
    print("------------------------")

    added = 0
    if args.client in ("claude", "both"):
        print()
        print("Claude Code")
        print("-----------")
        added += claude.install()
    if args.client in ("codex", "both"):
        print()
        print("Codex")
        print("-----")
        added += codex.install(args.project_dir)

    print()
    print(f"Hooks added: {added} (existing entries left untouched)")
    if args.client in ("claude", "both"):
        print("Claude settings:", claude.SETTINGS_PATH)
    if args.client in ("codex", "both"):
        print("Codex hooks:     ", codex.hook_path(args.project_dir))
        print()
        print("Codex notes:")
        print("  - Enable hooks in ~/.codex/config.toml: [features] codex_hooks = true")
        print("  - SessionStart inject-context uses the same additionalContext JSON")
        print("    shape as Claude Code (hot + warm + cold pointers).")


if __name__ == "__main__":
    main()
