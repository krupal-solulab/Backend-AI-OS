"""Pydantic output schema for Quote Comparison — mirrors PRD §7.1 (Extracted
Quote Schema) and §7.2 (Comparison/Recommendation Output Schema) closely.
``carrier_id``/``quotes`` (the full per-quote breakdown, not just
``quotes_considered`` ids) are additive beyond the literal PRD schema, for
the FE's side-by-side table (FR-21) — same allowance every prior workflow's
schema.py took. Not a ``core.common`` contract; free to evolve.
"""

from __future__ import annotations

from pydantic import BaseModel


class DeductiblesOut(BaseModel):
    all_perils: str | None = None
    wind_hail: str | None = None


class EndorsementOut(BaseModel):
    type: str
    basis: str  # included_blanket | scheduled_only | additional_premium | not_offered


class SubjectivityOut(BaseModel):
    description: str
    materiality: str  # routine | material
    deadline_or_dependency: str | None = None
    is_dependency: bool = False  # true for an unresolved-external-dependency subjectivity
    # (e.g. "primary carrier binding confirmed") — stored so read-time urgency
    # recompute (router.py) doesn't need to re-parse the description text.


class ExtractedQuoteOut(BaseModel):
    quote_id: str
    submission_id: str | None = None
    carrier_id: str | None = None
    carrier_name: str
    response_type: str  # QUOTE | DECLINATION
    premium: float | None = None
    limits: str | None = None
    deductibles: DeductiblesOut | None = None
    key_endorsements: list[EndorsementOut] = []
    subjectivities: list[SubjectivityOut] = []
    effective_date: str | None = None
    quote_valid_through: str | None = None
    declination_reason: str | None = None
    declination_appetite_consistency: str | None = None
    source_email_reference: str


class ComparabilityAssessmentOut(BaseModel):
    directly_comparable: bool
    material_differences: list[str] = []


class RecommendationCitationOut(BaseModel):
    claim: str
    source: str


class RecommendationReasoningOut(BaseModel):
    summary: str
    citations: list[RecommendationCitationOut] = []


class RecommendationOut(BaseModel):
    primary_quote_id: str | None = None
    reasoning: RecommendationReasoningOut


class UrgencyFlagOut(BaseModel):
    quote_id: str
    flag_type: str  # validity_window | material_subjectivity | dependency_unresolved
    detail: str


class ComparisonPayload(BaseModel):
    """The FE Quote Comparison screen's data needs for one submission's
    carrier response set."""

    submission_id: str | None = None
    named_insured: str | None = None
    quotes_considered: list[str] = []
    quotes: list[ExtractedQuoteOut] = []
    comparability_assessment: ComparabilityAssessmentOut
    # SINGLE_RECOMMENDATION | MULTI_OPTION | SINGLE_QUOTE_URGENT | SINGLE_QUOTE_ROUTINE
    output_mode: str
    recommendation: RecommendationOut
    urgency_flags: list[UrgencyFlagOut] = []
    selected_quote_id: str | None = None
    status: str = "PENDING_REVIEW"  # PENDING_REVIEW | PRESENTED | REVISION_REQUESTED | LAPSED
    processing_metadata: dict[str, str] = {}
