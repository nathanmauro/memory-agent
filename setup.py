"""Setup script for Memory Agent MCP server.
Run this once to configure the API key and register with Claude Code.
"""
import json
import os
import sys

DB_DIR = os.path.join(os.path.expanduser("~"), ".claude", "memory")
KEY_FILE = os.path.join(DB_DIR, ".api_key")
SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
SERVER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py")

os.makedirs(DB_DIR, exist_ok=True)

# Get API key
if os.path.exists(KEY_FILE):
    with open(KEY_FILE) as f:
        existing = f.read().strip()
    if existing:
        print(f"API key already configured ({len(existing)} chars)")
        resp = input("Replace it? (y/N): ").strip().lower()
        if resp != "y":
            print("Keeping existing key.")
        else:
            key = input("Paste your Anthropic API key: ").strip()
            with open(KEY_FILE, "w") as f:
                f.write(key)
            print("Key saved.")
    else:
        key = input("Paste your Anthropic API key: ").strip()
        with open(KEY_FILE, "w") as f:
            f.write(key)
        print("Key saved.")
else:
    key = input("Paste your Anthropic API key: ").strip()
    with open(KEY_FILE, "w") as f:
        f.write(key)
    print("Key saved.")

# Register MCP server in Claude Code settings
with open(SETTINGS_FILE) as f:
    settings = json.load(f)

if "mcpServers" not in settings:
    settings["mcpServers"] = {}

settings["mcpServers"]["memory"] = {
    "command": "python",
    "args": [SERVER_PATH.replace("\\", "\\\\")],
}

with open(SETTINGS_FILE, "w") as f:
    json.dump(settings, f, indent=2)

print(f"\nMCP server registered in {SETTINGS_FILE}")
print(f"Server path: {SERVER_PATH}")
print("\nRestart Claude Code to activate the memory agent.")
