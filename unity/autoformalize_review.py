"""Typed semantic evidence for the autoformalize-only final review gate."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RequirementReview(BaseModel):
    """A critic's check of one immutable requirement against actual declarations."""

    model_config = ConfigDict(extra="forbid", strict=True)

    requirement_id: str = Field(min_length=1)
    status: Literal["pass", "fail", "not_checked"]
    declarations: list[str]
    checked_anchor_ids: list[str]
    checked_prerequisite_ids: list[str] = Field(default_factory=list)
    rationale: str = Field(min_length=1)
    argument_rationale: str = Field(min_length=1)


class RepairReview(BaseModel):
    """Independent evidence about one explicitly recorded source repair."""

    model_config = ConfigDict(extra="forbid", strict=True)

    repair_id: str = Field(min_length=1)
    status: Literal["pass", "fail", "not_checked"]
    rationale: str = Field(min_length=1)


class SemanticReview(BaseModel):
    """Evidence bound to one controller-verified source and kernel snapshot."""

    model_config = ConfigDict(extra="forbid", strict=True)

    snapshot_id: str = Field(min_length=1)
    scope_rationale: str = Field(min_length=1)
    requirements: list[RequirementReview]
    repair_reviews: list[RepairReview] = Field(default_factory=list)


class RepresentationReview(BaseModel):
    """A scoped model judgment, not a kernel or final faithfulness receipt."""

    model_config = ConfigDict(extra="forbid", strict=True)

    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verdict: Literal["aligned", "encoding_error", "source_issue", "uncertain"]
    checked_anchor_ids: list[str]
    rationale: str = Field(min_length=1, max_length=4000)
    evidence: str = Field(min_length=1, max_length=16000)


class SourceDiagnosis(BaseModel):
    """Distinguish source defects from encodings and false alarms before replan."""

    model_config = ConfigDict(extra="forbid", strict=True)

    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verdict: Literal["false_alarm", "encoding_error", "source_defect", "uncertain"]
    evidence: str = Field(min_length=1, max_length=16000)
