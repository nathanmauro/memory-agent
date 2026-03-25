"""
Memory Agent — MCP Server for Claude Code
Persistent structured memory using SQLite + Claude Haiku (Bedrock).
No vector databases. LLM-driven categorization, dedup, and ranking.
"""

import db
from tools import mcp  # noqa: F401 — registers all @mcp.tool() handlers

db.init_db()

if __name__ == "__main__":
    mcp.run()
