"""Memory record and metadata models."""

import sqlite3

from pydantic import BaseModel, model_validator

from .types import Category, Importance, OptionalText, Tags


class MemoryRecord(BaseModel):
    """A single memory row from the database."""

    id: str
    content: str
    category: str = "project_knowledge"
    scope: str = "global"
    tags: str = ""
    created_at: str = ""
    updated_at: str = ""
    source: str = ""
    importance: int = 3

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "MemoryRecord":
        return cls(**dict(row))

    def format(self, truncate: int = 0) -> str:
        content = self.content[:truncate] if truncate else self.content
        return (
            f"[{self.id[:8]}] ({self.scope}/{self.category}) importance={self.importance} "
            f"tags={self.tags} updated={self.updated_at[:10]}\n  {content}"
        )


class MemoryMetadata(BaseModel):
    """LLM-extracted metadata for a new or merged memory."""

    category: Category = "project_knowledge"
    tags: Tags = ""
    importance: Importance = 3
    merge_with_id: OptionalText = None
    merged_content: OptionalText = None

    @model_validator(mode="after")
    def require_both_merge_fields(self) -> "MemoryMetadata":
        if not self.merge_with_id or not self.merged_content:
            self.merge_with_id = None
            self.merged_content = None
        return self
