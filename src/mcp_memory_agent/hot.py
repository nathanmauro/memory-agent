"""Bounded per-scope hot memory files (Hermes-style edit operations)."""

import os

from . import db

HOT_MAX_CHARS = 8000


def hot_path(scope: str) -> str:
    os.makedirs(db.HOT_DIR, exist_ok=True)
    safe = db._safe_path_component(scope)
    return os.path.join(db.HOT_DIR, f"{safe}.md")


def read_hot(scope: str) -> str:
    path = hot_path(scope)
    try:
        with open(path) as f:
            text = f.read()
    except FileNotFoundError:
        return ""
    except Exception:
        return ""
    if len(text) > HOT_MAX_CHARS:
        return text[:HOT_MAX_CHARS]
    return text


def _count_matches(text: str, target: str) -> int:
    count = 0
    start = 0
    while True:
        idx = text.find(target, start)
        if idx < 0:
            break
        count += 1
        start = idx + max(1, len(target))
    return count


def _oversize_message(size: int) -> str:
    return (
        f"Error: hot memory would exceed {HOT_MAX_CHARS} characters "
        f"(would be {size})."
    )


def _write_hot(path: str, text: str) -> str:
    try:
        with open(path, "w") as f:
            f.write(text)
    except Exception:
        return "Error: could not write hot memory file."
    return f"Hot memory updated ({len(text)} chars)."


def edit_hot(
    scope: str,
    operation: str,
    content: str,
    target: str = "",
) -> str:
    op = (operation or "").strip().lower()
    if op not in ("add", "replace", "remove"):
        return f"Error: unknown operation '{operation}'. Use add, replace, or remove."

    path = hot_path(scope)
    current = read_hot(scope)
    piece = content if isinstance(content, str) else ""
    target_text = target if isinstance(target, str) else ""

    if op == "add":
        if not piece.strip():
            return "Error: add requires non-empty content."
        if piece.strip() in current:
            return "Error: duplicate content already present in hot memory."
        new_text = current
        if new_text and not new_text.endswith("\n"):
            new_text += "\n"
        new_text = (new_text + piece) if new_text else piece
        if len(new_text) > HOT_MAX_CHARS:
            return _oversize_message(len(new_text))
        return _write_hot(path, new_text)

    if op == "replace":
        if target_text:
            matches = _count_matches(current, target_text)
            if matches == 0:
                return "Error: target not found in hot memory."
            if matches > 1:
                return (
                    f"Error: target matches {matches} locations; "
                    "provide a more specific target string."
                )
            new_text = current.replace(target_text, piece, 1)
        else:
            new_text = piece
        if len(new_text) > HOT_MAX_CHARS:
            return _oversize_message(len(new_text))
        return _write_hot(path, new_text)

    if not target_text:
        return "Error: remove requires a target substring."
    matches = _count_matches(current, target_text)
    if matches == 0:
        return "Error: target not found in hot memory."
    if matches > 1:
        return (
            f"Error: target matches {matches} locations; "
            "provide a more specific target string."
        )
    new_text = current.replace(target_text, "", 1)
    return _write_hot(path, new_text)
