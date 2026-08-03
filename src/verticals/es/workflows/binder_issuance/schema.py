"""Pydantic output schema for Binder & Policy Issuance — mirrors PRD §7.1's
Bind Coordination Record Schema closely. Not a ``core.common`` contract;
free to evolve with this workflow's FE screen.
"""

from __future__ import annotations

from pydantic import BaseModel


class BindTermsOut(BaseModel):
    premium: float | None = None
    limits: str | None = None
    deductible_all_perils: float | None = None
    deductible_wind_hail: float | None = None
    effective_date: str | None = None
    # No source document (bind confirmation email or issued policy
    # declarations page) ever states an expiration date or policy term —
    # confirmed against every real sample in this dataset. Derived as a
    # standard 12-month term from effective_date and always flagged as an
    # assumed default, same treatment as policy_issuance's
    # timeline_is_assumed_default below.
    expiration_date: str | None = None
    expiration_date_is_assumed_default: bool = False


class SubjectivityOut(BaseModel):
    description: str
    materiality: str  # routine | material
    lifecycle_stage: str  # PRE_BIND | POST_BIND_ONGOING
    status: str  # cleared | open


class DiscrepancyOut(BaseModel):
    field: str
    requested_or_bound: str
    confirmed_or_issued: str


class CarrierConfirmationOut(BaseModel):
    binder_number: str | None = None
    confirmed_terms: BindTermsOut | None = None
    # PENDING | CLEAN | DISCREPANCY_FLAGGED | BROKER_RESOLVED
    reconciliation_status: str = "PENDING"
    discrepancy_detail: list[DiscrepancyOut] = []


class PolicyIssuanceOut(BaseModel):
    carrier_stated_timeline_days: int | None = None
    timeline_is_assumed_default: bool = False
    expected_by_date: str | None = None
    documents_received: bool = False
    overdue_alert_fired: bool = False


class IssuedPolicyReconciliationOut(BaseModel):
    # NOT_YET_RECEIVED | PENDING | CLEAN | POLICY_DISCREPANCY_FLAGGED | BROKER_RESOLVED
    status: str = "NOT_YET_RECEIVED"
    discrepancy_detail: list[DiscrepancyOut] = []


class OngoingObligationOut(BaseModel):
    description: str
    due_date: str | None = None
    status: str = "open"  # open | completed
    reminder_due: bool = False


class DownstreamTriggersFiredOut(BaseModel):
    placement_confirmation: bool = False
    policy_documents_delivered: bool = False


class StatusLogEntryOut(BaseModel):
    action: str
    timestamp: str
    user: str


class BindCoordinationPayload(BaseModel):
    bind_id: str
    submission_id: str | None = None
    named_insured: str | None = None
    carrier_id: str | None = None
    carrier_name: str
    requested_bind_terms: BindTermsOut
    pre_bind_subjectivities: list[SubjectivityOut] = []
    bind_order_status: str = "READY"  # BLOCKED | READY | SENT
    carrier_confirmation: CarrierConfirmationOut = CarrierConfirmationOut()
    policy_issuance: PolicyIssuanceOut = PolicyIssuanceOut()
    issued_policy_reconciliation: IssuedPolicyReconciliationOut = IssuedPolicyReconciliationOut()
    post_bind_ongoing_obligations: list[OngoingObligationOut] = []
    downstream_triggers_fired: DownstreamTriggersFiredOut = DownstreamTriggersFiredOut()
    status_log: list[StatusLogEntryOut] = []
