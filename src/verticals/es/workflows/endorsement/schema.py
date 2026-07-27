"""Pydantic output schema for Endorsement / Mid-Term Change Processing —
mirrors PRD §7.1's Endorsement Request Record Schema closely.
``requested_items``/``issued_items``/``state_licensing_clarification_needed``
are additive beyond the literal PRD schema (needed for EP-05's item-level
reconciliation and EP-02's location-addition state check) — same allowance
every prior workflow's schema.py took. Not a ``core.common`` contract; free
to evolve.
"""

from __future__ import annotations

from pydantic import BaseModel


class RequestedChangeOut(BaseModel):
    type: str
    detail: str
    requested_effective_date: str | None = None


class AppetiteRecheckOut(BaseModel):
    applicable: bool
    # WITHIN_APPETITE | OUTSIDE_APPETITE | APPETITE_UNKNOWN | NOT_APPLICABLE
    outcome: str = "NOT_APPLICABLE"
    detail: str = ""
    state_licensing_clarification_needed: bool = False


class ProrationInputsOut(BaseModel):
    days_elapsed: int
    days_remaining: int
    term_total_days: int


class PremiumImpactOut(BaseModel):
    premium_bearing: bool | None = None
    proration_inputs: ProrationInputsOut | None = None


class DraftedRequestCitationOut(BaseModel):
    claim: str
    source: str


class DraftedRequestOut(BaseModel):
    body: str
    citations: list[DraftedRequestCitationOut] = []


class DiscrepancyOut(BaseModel):
    requested_item: str
    issued_item: str | None = None


class CarrierResponseOut(BaseModel):
    endorsement_number: str | None = None
    issued_items: list[str] = []
    # PENDING | CLEAN | DISCREPANCY_FLAGGED | BROKER_RESOLVED
    reconciliation_status: str = "PENDING"
    discrepancy_detail: list[DiscrepancyOut] = []


class StatusLogEntryOut(BaseModel):
    action: str
    timestamp: str
    user: str


class EndorsementRequestPayload(BaseModel):
    endorsement_request_id: str
    bind_id: str | None = None
    named_insured: str | None = None
    carrier_id: str | None = None
    carrier_name: str = ""
    requested_change: RequestedChangeOut
    requested_items: list[str] = []
    classification: str = "UNDERWRITING_REVIEW_REQUIRED"  # ROUTINE | UNDERWRITING_REVIEW_REQUIRED
    classification_reasoning: str = ""
    appetite_recheck: AppetiteRecheckOut = AppetiteRecheckOut(applicable=False)
    premium_impact: PremiumImpactOut = PremiumImpactOut()
    drafted_request: DraftedRequestOut
    carrier_response: CarrierResponseOut = CarrierResponseOut()
    downstream_trigger_fired: bool = False
    status_log: list[StatusLogEntryOut] = []
