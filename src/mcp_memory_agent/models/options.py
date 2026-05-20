"""Query and list option models."""

from pydantic import BaseModel, Field

from .types import IdList, ListLimit, QueryLimit, StrippedText


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


class MemoryIndexOptions(BaseModel):
    """Validated options for the memory_index tool."""

    query: StrippedText
    scope: StrippedText = ""
    category: StrippedText = ""
    limit: ListLimit = 20


class MemoryGetOptions(BaseModel):
    """Validated options for the memory_get tool."""

    ids: IdList = Field(default_factory=list)


class MemoryTimelineOptions(BaseModel):
    """Validated options for the memory_timeline tool."""

    scope: StrippedText = ""
    before_iso: StrippedText = ""
    after_iso: StrippedText = ""
    limit: ListLimit = 20
