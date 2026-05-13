"""Query and list option models."""

from pydantic import BaseModel

from models.types import ListLimit, QueryLimit, StrippedText


class MemoryQueryOptions(BaseModel):
    """Validated options for the memory_query tool."""

    query: StrippedText
    scope: StrippedText = ""
    category: StrippedText = ""
    limit: QueryLimit = 10


class MemoryListOptions(BaseModel):
    """Validated options for the memory_list tool."""

    scope: StrippedText = ""
    category: StrippedText = ""
    limit: ListLimit = 20
