"""Memory Agent — MCP server for Claude Code.

Persistent structured memory with pluggable substrate routers.
"""

from . import db
from .tools import mcp  # noqa: F401 — registers all @mcp.tool() handlers


def main() -> None:
    db.init_db()
    mcp.run()


if __name__ == "__main__":
    main()
