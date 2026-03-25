"""LLM helpers for the memory agent (Amazon Bedrock / Claude Haiku)."""

import json
import os

import boto3

from models import MemoryMetadata, MemoryRecord

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
BEDROCK_MODEL = os.environ.get(
    "BEDROCK_MODEL", "us.anthropic.claude-3-5-haiku-20241022-v1:0"
)

_profile = os.environ.get("AWS_PROFILE")
if _profile:
    _session = boto3.Session(profile_name=_profile, region_name=AWS_REGION)
else:
    _session = boto3.Session(region_name=AWS_REGION)
bedrock = _session.client("bedrock-runtime")


def llm_call(system: str, user: str) -> str:
    response = bedrock.converse(
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


def extract_json_object(raw: str) -> dict:
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            result = json.loads(raw[start:end])
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
1. Categorize it as one of: session_summary, code_decision, user_preference, project_knowledge
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
