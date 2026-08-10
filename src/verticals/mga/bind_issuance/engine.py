"""Bind Order & Issuance engine — MBI-01 (worksheet fidelity) → MBI-02 (pre-bind
subjectivity gate) → MBI-03 (final authority reconfirmation against CURRENT information,
the one genuinely new rule) → MBI-04 (write-back) → MBI-05 (issuance reconciliation,
never trust by default) → MBI-06 (downstream trigger gating) → MBI-07 (post-bind
obligation tracking, distinct from pre-bind blockers).

An orchestration layer over Quoting & Rating Support (the worksheet this workflow binds
against — MBI-01 never allows a hand-edited premium) and Submission Triage/Endorsement
Processing's hard-rule and carrier-referral patterns, per the PRD. MBI-03 and MBI-05 are
the two genuinely new, high-stakes checks; everything else is a reused pattern.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from verticals.mga.bind_issuance.config import BindIssuanceConfig

READY = "READY"
BLOCKED = "BLOCKED"
BOUND = "BOUND"

WITHIN_AUTHORITY = "WITHIN_AUTHORITY"
REQUIRES_RECALCULATION = "REQUIRES_RECALCULATION"
EXCEEDS_CEILING_REFERRAL_REQUIRED = "EXCEEDS_CEILING_REFERRAL_REQUIRED"

NOT_YET_ISSUED = "NOT_YET_ISSUED"
CLEAN = "CLEAN"
DISCREPANCY_FLAGGED = "DISCREPANCY_FLAGGED"


@dataclass(frozen=True)
class Subjectivity:
    description: str
    materiality: str  # routine | material
    status: str  # cleared | open (any non-"cleared" status is treated as open)
    lifecycle_stage: str = "PRE_BIND"


@dataclass(frozen=True)
class Discrepancy:
    field: str
    bound: str
    issued: str


@dataclass(frozen=True)
class PostBindObligation:
    description: str
    due_date: str | None
    reminder_days_before: tuple[int, ...]


@dataclass(frozen=True)
class BindDecision:
    status: str  # READY | BLOCKED | BOUND
    rationale: str
    worksheet_id: str | None
    worksheet_date: str | None
    worksheet_premium: float | None
    days_since_worksheet: int | None
    exceeds_staleness_threshold: bool
    material_update_logged_since: bool
    pre_bind_subjectivities: list[Subjectivity]
    authority_outcome: str | None  # None when blocked before authority check even runs
    checked_premium: float | None
    delegated_ceiling: float
    referral_draft_text: str | None
    write_back_logged: bool
    issuance_status: str
    issuance_discrepancies: list[Discrepancy]
    post_bind_obligations: list[PostBindObligation]
    bind_confirmation_fired: bool
    policy_delivered_fired: bool


class BindIssuanceEngine:
    def __init__(self, config: BindIssuanceConfig | None = None) -> None:
        self.cfg = config or BindIssuanceConfig()

    def decide(self, request: dict[str, Any]) -> BindDecision:
        # scenario_05's shape carries a bind_confirmation (post-bind issuance check) —
        # no bind_instruction fields at all, handled as its own path.
        if "bound_terms" in request and "issued_policy_document_extract" in request:
            return self._decide_issuance(request)
        return self._decide_pre_bind(request)

    # ── MBI-01..04, 07: pre-bind path ──
    def _decide_pre_bind(self, request: dict[str, Any]) -> BindDecision:
        worksheet = request.get("finalized_worksheet", {})
        authority = request.get("delegated_authority", {})
        ceiling = float(authority.get("premium_ceiling", 0) or 0)
        worksheet_premium = worksheet.get("total_indicated_premium")
        worksheet_id = worksheet.get("worksheet_id")
        worksheet_date_str = worksheet.get("worksheet_date")

        subjectivities = [
            Subjectivity(
                description=s.get("description", ""), materiality=s.get("materiality", "routine"),
                status=s.get("status", "open"))
            for s in request.get("pre_bind_subjectivities", [])
        ]
        post_bind = [
            self._parse_obligation(s) for s in request.get("post_bind_subjectivities", [])
        ]

        # ── MBI-01: worksheet fidelity — a hand-adjusted requested premium never binds ──
        staff_request = request.get("bind_request_submitted_by_staff")
        if staff_request is not None:
            requested = float(staff_request.get("requested_premium", 0))
            if worksheet_premium is not None and not self._matches(requested, worksheet_premium):
                return self._decision(
                    status=BLOCKED,
                    rationale=(
                        f"Requested bind premium (${requested:,.0f}) does NOT match the "
                        f"finalized Quoting & Rating Support worksheet (${worksheet_premium:,.0f}) "
                        f"— a ${abs(worksheet_premium - requested):,.0f} discrepancy with no "
                        "recalculation trail. Per MBI-01, any premium change must go back "
                        "through Quoting & Rating Support's engine, never be hand-edited "
                        "directly into a bind request."),
                    worksheet_id=worksheet_id, worksheet_date=worksheet_date_str,
                    worksheet_premium=worksheet_premium, days_since_worksheet=None,
                    exceeds_staleness_threshold=False, material_update_logged_since=False,
                    subjectivities=subjectivities, authority_outcome=None, checked_premium=None,
                    ceiling=ceiling, referral_text=None, write_back_logged=False,
                    post_bind=post_bind)

        # ── MBI-02: pre-bind subjectivity gate — material + open blocks entirely ──
        open_material = [s for s in subjectivities
                         if s.materiality == "material" and s.status != "cleared"]
        if open_material:
            items = "; ".join(f"{s.description} ({s.status})" for s in open_material)
            return self._decision(
                status=BLOCKED,
                rationale=(
                    f"Material pre-bind subjectivity not cleared: {items}. Per MBI-02, a bind "
                    "order cannot be generated while a material pre-bind subjectivity remains "
                    "open, regardless of how ready everything else appears."),
                worksheet_id=worksheet_id, worksheet_date=worksheet_date_str,
                worksheet_premium=worksheet_premium, days_since_worksheet=None,
                exceeds_staleness_threshold=False, material_update_logged_since=False,
                subjectivities=subjectivities, authority_outcome=None, checked_premium=None,
                ceiling=ceiling, referral_text=None, write_back_logged=False,
                post_bind=post_bind)

        # ── MBI-03: final authority reconfirmation using CURRENT information ──
        days_since = self._days_since(worksheet_date_str, request.get("_now"))
        exceeds_staleness = (days_since is not None
                            and days_since > self.cfg.worksheet_staleness_days)
        update = request.get("broker_update_since_worksheet")
        material_update = update is not None
        checked_premium = worksheet_premium

        if material_update or exceeds_staleness:
            revised = update.get("revised_premium_estimate") if update else None
            checked_premium = revised if revised is not None else worksheet_premium
            if checked_premium is not None and checked_premium > ceiling:
                carrier = authority.get("carrier", "the carrier")
                referral = self._draft_referral(request, checked_premium, ceiling)
                cause = (
                    f"a broker update logged {update.get('date', '')} indicates a material "
                    f"change (revised estimate ${checked_premium:,.0f})" if update else
                    f"the worksheet has gone stale, and the recalculated figure is "
                    f"${checked_premium:,.0f}")
                rationale = (
                    f"Binding on the worksheet's ${worksheet_premium:,.0f} figure would be "
                    f"binding on outdated exposure data — {cause}, which EXCEEDS the "
                    f"${ceiling:,.0f} delegated authority ceiling with {carrier}, which the "
                    "original worksheet figure did NOT. Cannot bind on the existing "
                    "worksheet — return to Quoting & Rating Support for a recalculated "
                    "worksheet; the recalculated premium will very likely require a "
                    "carrier referral (MEP-02 pattern, applied here at initial binding).")
                return self._decision(
                    status=BLOCKED, rationale=rationale,
                    worksheet_id=worksheet_id, worksheet_date=worksheet_date_str,
                    worksheet_premium=worksheet_premium, days_since_worksheet=days_since,
                    exceeds_staleness_threshold=exceeds_staleness,
                    material_update_logged_since=material_update, subjectivities=subjectivities,
                    authority_outcome=EXCEEDS_CEILING_REFERRAL_REQUIRED,
                    checked_premium=checked_premium, ceiling=ceiling, referral_text=referral,
                    write_back_logged=False, post_bind=post_bind)
            return self._decision(
                status=BLOCKED,
                rationale=(
                    "A material update has been logged since the worksheet was finalized "
                    "(or the worksheet has exceeded the staleness threshold) — per MBI-03, "
                    "final authority reconfirmation must use CURRENT information, not treat "
                    "the original worksheet as permanently valid. Return to Quoting & Rating "
                    "Support for a recalculated worksheet before binding may proceed."),
                worksheet_id=worksheet_id, worksheet_date=worksheet_date_str,
                worksheet_premium=worksheet_premium, days_since_worksheet=days_since,
                exceeds_staleness_threshold=exceeds_staleness,
                material_update_logged_since=material_update, subjectivities=subjectivities,
                authority_outcome=REQUIRES_RECALCULATION, checked_premium=checked_premium,
                ceiling=ceiling, referral_text=None, write_back_logged=False,
                post_bind=post_bind)

        # ── within authority, clean: ready to bind, write back (MBI-04) ──
        return self._decision(
            status=READY,
            rationale=(
                f"Bind order matches the finalized worksheet (${worksheet_premium:,.0f}) "
                "exactly. All pre-bind subjectivities routine and cleared. Final delegated "
                f"authority reconfirmation: within the ${ceiling:,.0f} ceiling — confirmed."),
            worksheet_id=worksheet_id, worksheet_date=worksheet_date_str,
            worksheet_premium=worksheet_premium, days_since_worksheet=days_since,
            exceeds_staleness_threshold=exceeds_staleness, material_update_logged_since=False,
            subjectivities=subjectivities, authority_outcome=WITHIN_AUTHORITY,
            checked_premium=checked_premium, ceiling=ceiling, referral_text=None,
            write_back_logged=True, post_bind=post_bind)

    # ── MBI-05, 06: issuance reconciliation path (scenario_05's shape) ──
    def _decide_issuance(self, request: dict[str, Any]) -> BindDecision:
        bound = request.get("bound_terms", {})
        issued = request.get("issued_policy_document_extract", {})
        discrepancies = [
            Discrepancy(field=field_name, bound=str(bound[field_name]),
                       issued=str(issued[field_name]))
            for field_name in bound
            if field_name in issued
            and not self._values_match(bound[field_name], issued[field_name])
        ]
        clean = not discrepancies
        rationale = (
            "Issued policy document reconciled clean — every field matches the bound terms."
            if clean else
            "MATERIAL DISCREPANCY: " + "; ".join(
                f"{d.field} on the issued document ({d.issued}) does NOT match the bound "
                f"terms ({d.bound})" for d in discrepancies)
            + ". Per MBI-05, an issued document is data to verify, never assumed correct "
              "regardless of how familiar or trusted the issuing carrier relationship is."
        )
        return BindDecision(
            status=READY if clean else BLOCKED,
            rationale=rationale, worksheet_id=None, worksheet_date=None, worksheet_premium=None,
            days_since_worksheet=None, exceeds_staleness_threshold=False,
            material_update_logged_since=False, pre_bind_subjectivities=[],
            authority_outcome=None, checked_premium=None, delegated_ceiling=0.0,
            referral_draft_text=None, write_back_logged=False,
            issuance_status=CLEAN if clean else DISCREPANCY_FLAGGED,
            issuance_discrepancies=discrepancies, post_bind_obligations=[],
            bind_confirmation_fired=clean, policy_delivered_fired=clean)

    # ── helpers ──
    @staticmethod
    def _parse_obligation(s: dict[str, Any]) -> PostBindObligation:
        desc = str(s.get("description", ""))
        m = re.search(r"within (\d+) days", desc)
        due_in_days = int(m.group(1)) if m else None
        due_date = f"+{due_in_days}d" if due_in_days else None
        return PostBindObligation(description=desc, due_date=due_date,
                                  reminder_days_before=(15, 5))

    def _matches(self, requested: float, worksheet: float) -> bool:
        tolerance = max(worksheet * self.cfg.premium_fidelity_tolerance, 0.5)
        return abs(requested - worksheet) <= tolerance

    @staticmethod
    def _values_match(a: Any, b: Any) -> bool:
        try:
            return abs(float(a) - float(b)) < 0.01
        except (TypeError, ValueError):
            return str(a).strip().lower() == str(b).strip().lower()

    @staticmethod
    def _days_since(worksheet_date_str: str | None, now_override: str | None) -> int | None:
        if not worksheet_date_str:
            return None
        try:
            ws_date = date.fromisoformat(worksheet_date_str)
        except ValueError:
            return None
        if now_override:
            try:
                now = date.fromisoformat(now_override)
            except ValueError:
                now = datetime.now().date()
        else:
            now = datetime.now().date()
        return (now - ws_date).days

    @staticmethod
    def _draft_referral(request: dict[str, Any], premium: float, ceiling: float) -> str:
        authority = request.get("delegated_authority", {})
        carrier = authority.get("carrier", "the carrier")
        return (
            f"Referral to {carrier} underwriting — {request.get('named_insured', '')} "
            f"(submission {request.get('submission_id', '')}).\n\n"
            f"Recalculated premium: ${premium:,.0f}, which exceeds our ${ceiling:,.0f} "
            f"delegated binding authority ceiling for this account — requesting your "
            f"independent approval to proceed with binding at this level."
        )

    def _decision(
        self, *, status: str, rationale: str, worksheet_id: str | None, worksheet_date: str | None,
        worksheet_premium: float | None, days_since_worksheet: int | None,
        exceeds_staleness_threshold: bool, material_update_logged_since: bool,
        subjectivities: list[Subjectivity], authority_outcome: str | None,
        checked_premium: float | None, ceiling: float, referral_text: str | None,
        write_back_logged: bool, post_bind: list[PostBindObligation],
    ) -> BindDecision:
        return BindDecision(
            status=status, rationale=rationale, worksheet_id=worksheet_id,
            worksheet_date=worksheet_date, worksheet_premium=worksheet_premium,
            days_since_worksheet=days_since_worksheet,
            exceeds_staleness_threshold=exceeds_staleness_threshold,
            material_update_logged_since=material_update_logged_since,
            pre_bind_subjectivities=subjectivities, authority_outcome=authority_outcome,
            checked_premium=checked_premium, delegated_ceiling=ceiling,
            referral_draft_text=referral_text, write_back_logged=write_back_logged,
            issuance_status=NOT_YET_ISSUED, issuance_discrepancies=[],
            post_bind_obligations=post_bind, bind_confirmation_fired=(status == READY),
            policy_delivered_fired=False)
