"""Typed semantic evidence for the solve-only final review gate."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RequirementReview(BaseModel):
    """A critic's check of one immutable requirement against actual declarations."""

    model_config = ConfigDict(extra="forbid", strict=True)

    requirement_id: str = Field(min_length=1)
    status: Literal["pass", "fail", "not_checked"]
    declarations: list[str]
    rationale: str = Field(min_length=1)


class SemanticReview(BaseModel):
    """Evidence bound to one controller-verified source and kernel snapshot."""

    model_config = ConfigDict(extra="forbid", strict=True)

    snapshot_id: str = Field(min_length=1)
    requirements: list[RequirementReview]
