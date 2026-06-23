"""LLM helpers for the memory agent. Supports Ollama, LM Studio, Amazon Bedrock, and Codex (cloud, no local GPU) backends."""

import json
import os
import urllib.request

from .models import MemoryMetadata, MemoryRecord

LLM_BACKEND = os.environ.get("LLM_BACKEND", "ollama")

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:14b")

LM_STUDIO_URL = os.environ.get("LM_STUDIO_URL", "http://localhost:1234")
LM_STUDIO_MODEL = os.environ.get(
    "LM_STUDIO_MODEL", "qwen3-4b-instruct-2507-mlx"
)
# Thinking models burn tokens on <reasoning> before the answer; give them room.
LM_STUDIO_MAX_TOKENS = int(os.environ.get("LM_STUDIO_MAX_TOKENS", "4096"))

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
BEDROCK_MODEL = os.environ.get(
    "BEDROCK_MODEL", "us.anthropic.claude-3-5-haiku-20241022-v1:0"
)

# Codex (cloud) summarization backend — keeps inference off the local GPU.
CODEX_BIN = os.environ.get("CODEX_BIN", "")  # resolved lazily in _codex_call
CODEX_MODEL = os.environ.get("CODEX_MODEL", "")  # empty -> Codex CLI default
CODEX_REASONING = os.environ.get("CODEX_REASONING", "low")
CODEX_TIMEOUT = int(os.environ.get("CODEX_TIMEOUT", "180"))

_bedrock_client = None


def _get_bedrock():
    global _bedrock_client
    if _bedrock_client is None:
        import boto3

        profile = os.environ.get("AWS_PROFILE")
        session = (
            boto3.Session(profile_name=profile, region_name=AWS_REGION)
            if profile
            else boto3.Session(region_name=AWS_REGION)
        )
        _bedrock_client = session.client("bedrock-runtime")
    return _bedrock_client


def _ollama_call(system: str, user: str) -> str:
    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": f"{system}\n\n{user}",
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 1024},
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())["response"]


def _lm_studio_call(system: str, user: str) -> str:
    payload = json.dumps({
        "model": LM_STUDIO_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.1,
        "max_tokens": LM_STUDIO_MAX_TOKENS,
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        f"{LM_STUDIO_URL}/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        body = json.loads(resp.read().decode())
    return body["choices"][0]["message"].get("content", "") or ""


def _bedrock_call(system: str, user: str) -> str:
    client = _get_bedrock()
    response = client.converse(
        modelId=BEDROCK_MODEL,
        system=[{"text": system}],
        messages=[
            {"role": "user", "content": [{"text": user}]},
        ],
        inferenceConfig={
            "maxTokens": 1024,
            "temperature": 0.1,
        },
    )
    return response["output"]["message"]["content"][0]["text"]


def _resolve_codex_bin() -> str:
    if CODEX_BIN:
        return CODEX_BIN
    import shutil

    return shutil.which("codex") or "/opt/homebrew/bin/codex"


def _codex_call(system: str, user: str) -> str:
    """Cloud summarization via the Codex CLI — keeps inference off the local GPU.

    Runs a lean, non-interactive `codex exec`: user config ignored (no MCP
    servers or hooks are spun up), read-only sandbox, ephemeral session, low
    reasoning. The agent's final message is captured via --output-last-message.
    Raises on failure so callers can fall back / skip gracefully.
    """
    import shlex
    import subprocess
    import tempfile

    prompt = (
        f"{system}\n\n{user}\n\n"
        "Output only the requested result. Do not run commands or use tools."
    )
    fd, out_path = tempfile.mkstemp(prefix="codex-mem-", suffix=".txt")
    os.close(fd)
    cmd = [
        _resolve_codex_bin(), "exec",
        "--ignore-user-config",   # skip MCP servers + hooks: lean, fast, quiet
        "--skip-git-repo-check",
        "--ephemeral",            # don't persist a session file per summary
        "-s", "read-only",
        "--color", "never",
        "-c", f'model_reasoning_effort="{CODEX_REASONING}"',
        "-o", out_path,
    ]
    if CODEX_MODEL:
        cmd += ["-m", CODEX_MODEL]
    extra = os.environ.get("CODEX_EXTRA_ARGS", "")
    if extra:
        cmd += shlex.split(extra)
    cmd.append("-")  # read the prompt from stdin
    try:
        subprocess.run(
            cmd,
            input=prompt.encode(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=CODEX_TIMEOUT,
            check=True,
        )
        with open(out_path, "r", errors="replace") as fh:
            return fh.read().strip()
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


def llm_call(system: str, user: str) -> str:
    if LLM_BACKEND == "codex":
        return _codex_call(system, user)
    if LLM_BACKEND == "bedrock":
        return _bedrock_call(system, user)
    if LLM_BACKEND in ("lm-studio", "lmstudio"):
        return _lm_studio_call(system, user)
    return _ollama_call(system, user)


def _strip_fences(raw: str) -> str:
    s = raw.strip()
    if s.startswith("```"):
        first_nl = s.find("\n")
        if first_nl >= 0:
            s = s[first_nl + 1 :]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    return s.strip()


def _repair_json(s: str) -> str:
    import re

    s = re.sub(r",\s*([}\]])", r"\1", s)
    return s


def extract_json_object(raw: str) -> dict:
    raw = _strip_fences(raw)
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start < 0 or end <= start:
        return {}
    candidate = raw[start:end]
    try:
        result = json.loads(candidate)
        if isinstance(result, dict):
            return result
    except Exception:
        pass
    try:
        result = json.loads(_repair_json(candidate))
        if isinstance(result, dict):
            return result
    except Exception:
        pass
    return {}


def extract_json_array(raw: str) -> list:
    start = raw.find("[")
    end = raw.rfind("]") + 1
    if start >= 0 and end > start:
        try:
            result = json.loads(raw[start:end])
            if isinstance(result, list):
                return result
        except Exception:
            pass
    return []


def extract_memory_metadata(
    content: str, scope: str, existing: list[MemoryRecord]
) -> MemoryMetadata:
    existing_text = ""
    if existing:
        lines = ["", "", "Existing memories in this scope:"]
        for m in existing[:20]:
            lines.append(f"- [id={m.id}] ({m.category}) {m.content[:200]}")
        existing_text = "\n".join(lines)

    system = """You are a memory management agent. Given a new memory and existing memories, you must:
1. Categorize it as one of: session_summary, code_decision, user_preference, project_knowledge, open_action
2. Extract relevant tags (comma-separated, lowercase, short)
3. Rate importance 1-5 (5=critical, 1=trivial)
4. Check if it should MERGE with an existing memory (same topic, updated info)

Respond with ONLY valid JSON:
{
  "category": "...",
  "tags": "tag1,tag2",
  "importance": 3,
  "merge_with_id": null,
  "merged_content": null
}

If merging, set merge_with_id to the existing memory's id and merged_content to the combined text."""

    user_msg = f"New memory to store (scope: {scope}):\n{content}{existing_text}"

    try:
        raw = llm_call(system, user_msg)
        return MemoryMetadata.model_validate(extract_json_object(raw))
    except Exception:
        return MemoryMetadata()


def rank_memories(
    query: str, candidates: list[MemoryRecord], limit: int
) -> list[MemoryRecord]:
    if not candidates:
        return []
    if len(candidates) <= limit:
        return candidates

    cand_text = ""
    for i, m in enumerate(candidates):
        cand_text += (
            f"{i}. [id={m.id}] ({m.category}, importance={m.importance}) "
            f"{m.content[:300]}\n"
        )

    system = """You are a memory retrieval agent. Given a query and candidate memories, rank them by relevance.
Respond with ONLY a JSON array of indices (integers) in order of relevance, most relevant first.
Example: [3, 0, 7, 1]
Return at most the number requested."""

    user_msg = (
        f"Query: {query}\nReturn top {limit} results.\n\nCandidates:\n{cand_text}"
    )

    try:
        raw = llm_call(system, user_msg)
        indices = extract_json_array(raw)
        ranked = []
        for idx in indices[:limit]:
            if isinstance(idx, int) and 0 <= idx < len(candidates):
                ranked.append(candidates[idx])
        if ranked:
            return ranked
    except Exception:
        pass

    return candidates[:limit]
