"""Claude Code hook wiring for the memory agent."""

HOOK_EVENTS = {
    "SessionStart": [["inject-context"], ["curator"]],
    "UserPromptSubmit": [["record", "--kind", "prompt"]],
    "PostToolUse": [["record", "--kind", "tool_use"]],
    "SessionEnd": [["summarize-session"]],
}
