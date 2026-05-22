"""Reusable Pydantic validators and annotated type aliases."""

from typing import Annotated

from pydantic import BeforeValidator

VALID_CATEGORIES = {
    "session_summary",
    "code_decision",
    "user_preference",
    "project_knowledge",
    "open_action",
}


# --- Validator functions ---


def _strip_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _strip_optional(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _normalize_category(value: object) -> str:
    if not isinstance(value, str):
        return "project_knowledge"
    value = value.strip().lower()
    return value if value in VALID_CATEGORIES else "project_knowledge"


def _normalize_tags(value: object) -> str:
    if not isinstance(value, str):
        return ""
    seen = []
    for tag in value.split(","):
        clean = tag.strip().lower()
        if clean and clean not in seen:
            seen.append(clean)
    return ",".join(seen)


def _normalize_importance(value: object) -> int:
    if isinstance(value, int):
        return max(1, min(5, value))
    try:
        n = int(str(value))
    except (ValueError, TypeError):
        return 3
    return max(1, min(5, n))


def _clamp(low: int, high: int, default: int):
    def validator(value: object) -> int:
        if isinstance(value, int):
            return max(low, min(high, value))
        try:
            n = int(str(value))
        except (ValueError, TypeError):
            return default
        return max(low, min(high, n))

    return validator


def _normalize_id_list(value: object) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out = []
    seen = set()
    for item in value[:50]:
        if not isinstance(item, str):
            continue
        item = item.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


# --- Annotated types ---

StrippedText = Annotated[str, BeforeValidator(_strip_text)]
OptionalText = Annotated[str | None, BeforeValidator(_strip_optional)]
Category = Annotated[str, BeforeValidator(_normalize_category)]
Tags = Annotated[str, BeforeValidator(_normalize_tags)]
Importance = Annotated[int, BeforeValidator(_normalize_importance)]
QueryLimit = Annotated[int, BeforeValidator(_clamp(1, 50, 10))]
ListLimit = Annotated[int, BeforeValidator(_clamp(1, 100, 20))]
IdList = Annotated[list[str], BeforeValidator(_normalize_id_list)]
