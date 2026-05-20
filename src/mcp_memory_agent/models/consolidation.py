"""Consolidation action and result models."""

from pydantic import BaseModel, Field, model_validator

from .types import OptionalText, StrippedText


class ConsolidationAction(BaseModel):
    """A single merge/delete/update action from consolidation."""

    type: StrippedText = ""
    keep_id: OptionalText = None
    delete_id: OptionalText = None
    id: OptionalText = None
    new_content: OptionalText = None
    reason: OptionalText = None

    @model_validator(mode="after")
    def validate_required_fields(self) -> "ConsolidationAction":
        action_type = self.type.lower()
        if (
            action_type == "merge"
            and self.keep_id
            and self.delete_id
            and self.new_content
        ):
            self.type = action_type
        elif action_type == "delete" and self.id:
            self.type = action_type
        elif action_type == "update" and self.id and self.new_content:
            self.type = action_type
        else:
            self.type = ""
        return self


class ConsolidationResult(BaseModel):
    """Parsed result of the consolidation LLM response."""

    actions: list[ConsolidationAction] = Field(default_factory=list)
    summary: StrippedText = ""

    @model_validator(mode="after")
    def drop_invalid_actions(self) -> "ConsolidationResult":
        self.actions = [a for a in self.actions if a.type]
        return self
