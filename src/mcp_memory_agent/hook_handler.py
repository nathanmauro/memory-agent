#!/usr/bin/env python3
"""Session-capture hook entrypoint for the memory agent.

Reads hook JSON from stdin. Subcommands:
  inject-context     Print client-specific startup context JSON.
  record             Append a transcript event to the per-session buffer.
  summarize-session  LLM-summarize the current buffer and persist memories.
  finalize-session   Summarize the current buffer and sweep stale buffers.
  sweep              Summarize orphaned (stale) buffers and delete them.

Every code path is best-effort: failures are swallowed so that a memory-agent
fault never breaks a coding-agent session.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from . import db, llm, tools
from .models import MemoryRecord

MAX_EVENT_BYTES = 500
SESSION_STALE_SECONDS = 3600
INJECT_BUDGET_CHARS = 8000
WARM_BUDGET_RATIO = 0.7


def _read_hook_input() -> dict:
    try:
        text = sys.stdin.read()
    except Exception:
        return {}
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _derive_scope(cwd: str) -> str:
    if not isinstance(cwd, str) or not cwd:
        return "global"
    path = os.path.abspath(cwd)
    current = path
    while True:
        if os.path.isdir(os.path.join(current, ".git")):
            return os.path.basename(current) or "global"
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return os.path.basename(path) or "global"


def _payload_value(payload: dict, keys: list[str], default: object = "") -> object:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return default


def _payload_cwd(payload: dict) -> str:
    value = _payload_value(
        payload, ["cwd", "workdir", "working_dir", "workingDirectory"], ""
    )
    if isinstance(value, str) and value:
        return value
    return os.environ.get("PWD", "")


def _payload_session_id(payload: dict) -> object:
    value = _payload_value(
        payload,
        [
            "session_id",
            "sessionId",
            "conversation_id",
            "conversationId",
            "thread_id",
            "threadId",
        ],
        "",
    )
    if value:
        return value
    return _payload_value(
        os.environ,
        ["MEMORY_AGENT_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID"],
        "",
    )


def _truncate(value: object, limit: int = MAX_EVENT_BYTES) -> object:
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "…"
    if isinstance(value, dict):
        return {k: _truncate(v, limit) for k, v in list(value.items())[:20]}
    if isinstance(value, list):
        return [_truncate(v, limit) for v in value[:20]]
    return value


def _session_path(session_id: object) -> str | None:
    if not isinstance(session_id, str):
        return None
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")
    if not safe:
        return None
    return os.path.join(db.SESSIONS_DIR, f"{safe}.jsonl")


def _append_event(payload: dict, kind: str) -> None:
    path = _session_path(_payload_session_id(payload))
    if not path:
        return
    scope = _derive_scope(_payload_cwd(payload))

    if kind == "prompt":
        data: object = {
            "prompt": _payload_value(
                payload, ["prompt", "user_prompt", "userPrompt", "input"], ""
            )
        }
    elif kind == "tool_use":
        data = {
            "tool_name": _payload_value(
                payload, ["tool_name", "toolName", "tool", "name"], ""
            ),
            "tool_input": _payload_value(
                payload, ["tool_input", "toolInput", "input", "arguments"], {}
            ),
            "tool_response": _payload_value(
                payload, ["tool_response", "toolResponse", "output", "result"], {}
            ),
        }
    else:
        return

    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "scope": scope,
        "data": _truncate(data),
    }
    try:
        os.makedirs(db.SESSIONS_DIR, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(event) + "\n")
    except Exception:
        return


def _read_buffer(path: str) -> list[dict]:
    events: list[dict] = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    events.append(obj)
    except Exception:
        pass
    return events


def _remove_buffer(path: str) -> None:
    try:
        os.remove(path)
    except Exception:
        pass


def _build_transcript(events: list[dict]) -> str:
    lines = []
    for e in events[:200]:
        kind = e.get("kind", "")
        data = e.get("data", {}) if isinstance(e.get("data"), dict) else {}
        if kind == "prompt":
            lines.append(f"USER: {str(data.get('prompt', ''))[:400]}")
        elif kind == "tool_use":
            tname = data.get("tool_name", "?")
            try:
                tinput = json.dumps(data.get("tool_input", {}))[:300]
            except Exception:
                tinput = ""
            lines.append(f"TOOL {tname}: {tinput}")
    return "\n".join(lines)


def _llm_summarize(events: list[dict], scope: str) -> dict:
    transcript = _build_transcript(events)
    if not transcript:
        return {}

    system = (
        "You are a memory extractor. Given a coding-agent session transcript, "
        "produce a JSON object describing what should be remembered for future sessions.\n\n"
        "Respond with ONLY valid JSON:\n"
        "{\n"
        '  "session_summary": "1-3 sentences capturing what was accomplished — concrete enough to be useful next time the user opens this project. Empty string if nothing meaningful.",\n'
        '  "memories": ["Specific code_decision, user_preference, or project_knowledge fact worth a separate memory. 0-3 items, short."]\n'
        "}"
    )
    user = f"Project scope: {scope}\n\nTranscript ({len(events)} events):\n{transcript}"

    try:
        raw = llm.llm_call(system, user)
    except Exception:
        return {}
    parsed = llm.extract_json_object(raw)
    return parsed if isinstance(parsed, dict) else {}


def _summarize_buffer(path: str, fallback_scope: str = "global") -> None:
    events = _read_buffer(path)
    if len(events) < 3:
        _remove_buffer(path)
        return

    scopes = [e.get("scope") for e in events if e.get("scope")]
    scope = scopes[0] if scopes else fallback_scope
    session_id = os.path.basename(path).replace(".jsonl", "")
    source = session_id

    db.archive_session_buffer(path, scope, session_id)

    parsed = _llm_summarize(events, scope)
    summary = parsed.get("session_summary", "") if parsed else ""
    sub = parsed.get("memories", []) if parsed else []
    if not isinstance(summary, str):
        summary = ""
    if not isinstance(sub, list):
        sub = []

    inserted = False
    conn = db.get_db()
    try:
        if summary.strip():
            try:
                tools._insert_memory(conn, summary.strip(), scope, source)
                inserted = True
            except Exception:
                pass
        for item in sub[:3]:
            content = ""
            if isinstance(item, str):
                content = item.strip()
            elif isinstance(item, dict):
                raw_content = item.get("content", "")
                if isinstance(raw_content, str):
                    content = raw_content.strip()
            if not content:
                continue
            try:
                tools._insert_memory(conn, content, scope, source)
                inserted = True
            except Exception:
                continue
        if inserted:
            conn.commit()
    finally:
        conn.close()


def _session_summary_for_source(
    conn, scope: str, session_id: str
) -> str:
    try:
        row = conn.execute(
            """
            SELECT content FROM memories
            WHERE scope = ? AND source = ? AND category = 'session_summary'
            ORDER BY updated_at DESC LIMIT 1
            """,
            (scope, session_id),
        ).fetchone()
        if row:
            return str(row["content"]).replace("\n", " ")[:120]
    except Exception:
        pass
    return ""


def _inject_context(payload: dict) -> None:
    scope = _derive_scope(_payload_cwd(payload))

    _sweep()

    conn = db.get_db()
    try:
        warm_budget = int(INJECT_BUDGET_CHARS * WARM_BUDGET_RATIO)
        header = f"Past memories for scope '{scope}':\n"
        used = len(header)
        bullets: list[str] = []

        rows = conn.execute(
            """
            SELECT * FROM memories
            WHERE scope = ? AND status = 'active'
            ORDER BY importance DESC, updated_at DESC
            """,
            (scope,),
        ).fetchall()
        for row in rows:
            m = MemoryRecord.from_row(row)
            snippet = m.content.replace("\n", " ")[:200]
            line = f"- [{m.id[:8]}] ({m.category}) {snippet}"
            line_len = len(line) + 1
            if used + line_len > warm_budget:
                break
            bullets.append(line)
            used += line_len

        pointer_lines: list[str] = []
        remaining = INJECT_BUDGET_CHARS - used
        if remaining > 120:
            archives = db.list_recent_archived_sessions(conn, scope, limit=8)
            if archives:
                pointer_header = "\nArchived sessions (use memory_session_search to thaw):\n"
                if used + len(pointer_header) <= INJECT_BUDGET_CHARS:
                    used += len(pointer_header)
                    for row in archives:
                        session_id = row["session_id"]
                        archived_at = str(row["archived_at"])[:10]
                        summary = _session_summary_for_source(conn, scope, session_id)
                        if summary:
                            line = (
                                f"- [{session_id[:8]}] {archived_at} — {summary}"
                            )
                        else:
                            line = f"- [{session_id[:8]}] {archived_at} — archived transcript"
                        line_len = len(line) + 1
                        if used + line_len > INJECT_BUDGET_CHARS:
                            break
                        pointer_lines.append(line)
                        used += line_len
                    if pointer_lines:
                        pointer_lines.insert(0, pointer_header.rstrip())
    finally:
        conn.close()

    if not bullets and not pointer_lines:
        print(json.dumps({}))
        return

    body = header + "\n".join(bullets)
    if pointer_lines:
        body += "\n" + "\n".join(pointer_lines)

    out = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": body[:INJECT_BUDGET_CHARS],
        }
    }
    print(json.dumps(out))


def _summarize_session(payload: dict) -> None:
    path = _session_path(_payload_session_id(payload))
    if not path or not os.path.exists(path):
        return
    fallback = _derive_scope(_payload_cwd(payload))
    _summarize_buffer(path, fallback_scope=fallback)


def _sweep() -> None:
    for path in db.iter_session_buffers(SESSION_STALE_SECONDS):
        try:
            _summarize_buffer(path)
        except Exception:
            _remove_buffer(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("inject-context")
    record = sub.add_parser("record")
    record.add_argument("--kind", choices=["prompt", "tool_use"], required=True)
    sub.add_parser("summarize-session")
    sub.add_parser("finalize-session")
    sub.add_parser("sweep")

    args = parser.parse_args()
    payload = _read_hook_input()
    db.init_db()

    if args.cmd == "inject-context":
        _inject_context(payload)
    elif args.cmd == "record":
        _append_event(payload, args.kind)
    elif args.cmd == "summarize-session":
        _summarize_session(payload)
    elif args.cmd == "finalize-session":
        _summarize_session(payload)
        _sweep()
    elif args.cmd == "sweep":
        _sweep()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
