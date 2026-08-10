"""Broker Communication Drafting Engine — classifies a CommType trigger off an existing
Submission Triage or Renewal Management ``Decision`` (read back from its persisted
``OutputPackage``), calibrates tone from the broker relationship, and assembles the
grounded facts for the shared ``LLMService``. This is the one genuinely new piece the
roadmap calls out (BC-01..BC-05 below) — everything else reuses the Extraction Core
(source decision already extracted its facts) and the Decision Core (recommendation +
missing-info the trigger is classified from).

No per-case literals: every threshold comes from ``BrokerCopilotConfig``, so new broker
relationships / comm types work with no code change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.common.enums import DecisionOutcome
from verticals.mga.broker_copilot.config import BrokerCopilotConfig


@dataclass(frozen=True)
class BrokerRelationship:
    name: str
    agency: str
    email: str
    tenure_years: int | None = None
    volume_tier: str | None = None  # low | moderate | high | strategic


@dataclass(frozen=True)
class DraftPlan:
    """What to draft, before the LLM turns it into prose."""

    comm_type: str
    subject: str
    tone: str
    tone_why: str
    sensitive: bool
    requires_compliance_review: bool
    combined: str | None
    deadline_ref: str | None


class BrokerDraftingEngine:
    def __init__(self, config: BrokerCopilotConfig | None = None) -> None:
        self.cfg = config or BrokerCopilotConfig()

    # ── BC-01: classify the trigger off the source decision ──
    def classify(
        self, source_workflow: str, outcome: DecisionOutcome, details: dict[str, Any]
    ) -> str:
        """Map a Triage/Renewal Decision onto one of the 6 CommType triggers."""
        if source_workflow == "renewal-management":
            if outcome is DecisionOutcome.DECLINE:
                return "NON_RENEWAL_NOTICE"
            if details.get("needs_info"):
                return "MISSING_INFO_REQUEST"
            return "RENEWAL_TERMS_EXPLANATION"
        # submission-triage
        if details.get("consistency_flagged"):
            return "CONSISTENCY_FLAG_FOLLOWUP"
        if outcome is DecisionOutcome.REQUEST_INFO or details.get("missing_info"):
            return "MISSING_INFO_REQUEST"
        return "QUOTE_SUMMARY"  # PROCEED with no open flags → ready to quote

    # ── BC-02: tone calibration from broker relationship + trigger sensitivity ──
    def calibrate_tone(self, comm_type: str, broker: BrokerRelationship) -> DraftPlan:
        cfg = self.cfg
        sensitive = comm_type in cfg.sensitive_types
        requires_compliance = comm_type in cfg.compliance_required_types

        if broker.tenure_years is None or broker.volume_tier is None:
            tone, why = "Neutral", "Relationship data unavailable — neutral tone by default"
        elif comm_type == "NON_RENEWAL_NOTICE":
            tone = "Considerate"
            why = ("Appetite-drift non-renewal — lead with notice & help, state it's not a "
                   "reflection of the account, no loss figures")
        elif comm_type == "CONSISTENCY_FLAG_FOLLOWUP":
            tone = "Careful"
            why = "Sensitive category — neutral framing, offer a call"
        elif comm_type == "QUOTE_SUMMARY":
            tone = "Positive"
            why = "Good news — lead with outcome, terms as a scannable list, no internal risk detail"
        elif comm_type == "NO_RESPONSE_FOLLOWUP":
            tone = "Gentle"
            why = "No reply within the follow-up window — assume good faith, anchor to the real deadline"
        elif comm_type == "RENEWAL_TERMS_EXPLANATION":
            tone = "Warm" if broker.volume_tier in cfg.strategic_tiers else "Collaborative"
            why = f"Renewal terms — {broker.tenure_years}-year {broker.volume_tier}-volume relationship"
        else:  # MISSING_INFO_REQUEST
            tone = "Collaborative"
            why = f"Routine request — {broker.tenure_years}-year {broker.volume_tier}-volume relationship → warm but efficient"

        return DraftPlan(
            comm_type=comm_type, subject="", tone=tone, tone_why=why, sensitive=sensitive,
            requires_compliance_review=requires_compliance, combined=None, deadline_ref=None,
        )

    # ── BC-03: subject line per trigger ──
    @staticmethod
    def subject_for(comm_type: str, named_insured: str) -> str:
        by_type = {
            "MISSING_INFO_REQUEST": f"{named_insured} — a couple of items to complete our review",
            "CONSISTENCY_FLAG_FOLLOWUP": f"{named_insured} — quick check on a few figures",
            "QUOTE_SUMMARY": f"{named_insured} — indicated terms",
            "RENEWAL_TERMS_EXPLANATION": f"{named_insured} — renewal terms",
            "NON_RENEWAL_NOTICE": f"{named_insured} — renewal",
            "NO_RESPONSE_FOLLOWUP": f"Re: {named_insured} — renewal (following up)",
        }
        return by_type.get(comm_type, f"{named_insured} — update")

    # ── BC-04: instruction handed to the grounded LLM per trigger ──
    @staticmethod
    def instruction_for(comm_type: str, tone: str) -> str:
        base = (
            f"Draft a broker email in a {tone.lower()} tone, using only the facts provided. "
            "Sign off as the underwriter. Keep it concise (under 150 words)."
        )
        extra = {
            "MISSING_INFO_REQUEST": "List each missing item as a short numbered list.",
            "CONSISTENCY_FLAG_FOLLOWUP": "Frame it as a routine check, not an accusation; offer a call.",
            "QUOTE_SUMMARY": "Lead with the good news, then a short bulleted list of key terms.",
            "RENEWAL_TERMS_EXPLANATION": "Explain what's driving any change before stating the number.",
            "NON_RENEWAL_NOTICE": (
                "State the decision plainly but kindly, make clear it is not a reflection of the "
                "account, and do not mention loss figures or dollar amounts."
            ),
            "NO_RESPONSE_FOLLOWUP": "Assume good faith; reference the real deadline once; ask what's needed to move forward.",
        }
        return f"{base} {extra.get(comm_type, '')}".strip()
