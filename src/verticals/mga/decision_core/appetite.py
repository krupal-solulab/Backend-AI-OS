"""Appetite Engine — maps validation results + extracted facts → a frozen ``Decision``.

Order of evaluation (mirrors the dataset spec):
  1. EC-01 extraction confidence → manual review (REQUEST_INFO + MANUAL_REVIEW), no LLM.
  2. Hard rules (HR-01..04) → short-circuit DECLINE before the LLM narrative (FR-23).
  3. Completeness (CR-01..04) → REQUEST_INFO (missing-info list).
  4. Consistency (CC-01/02) + SOV/limit reconciliation → REQUEST_INFO (flagged).
  5. Timing (TR-01) → REQUEST_INFO / expedite flag.
  6. Loss trend (LT-02) → soft factor for the narrative (never declines on its own).

Score (0–100): start 100; −``soft_flag_penalty`` per soft trigger; REQUEST_INFO capped at
``request_info_score_cap``; any hard-rule failure → DECLINE / score 0; manual review → None.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from core.common.dtos import Decision, ExtractedModel, RuleResult
from core.common.enums import DecisionOutcome
from core.extraction.service import coerce_number
from verticals.mga.decision_core.config import AppetiteConfig

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _first_money(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"\$[\d,]+", str(text))
    return coerce_number(m.group(0)) if m else None


def _parse_mdy(value: Any) -> date | None:
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", str(value or ""))
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None


def _parse_email_date(value: Any) -> date | None:
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})", str(value or ""))
    if not m:
        return None
    month = _MONTHS.get(m.group(2).lower())
    if month is None:
        return None
    try:
        return date(int(m.group(3)), month, int(m.group(1)))
    except ValueError:
        return None


def _business_days(start: date | None, end: date | None) -> int | None:
    if start is None or end is None or end < start:
        return None
    days, cur = 0, start
    while cur < end:
        cur = date.fromordinal(cur.toordinal() + 1)
        if cur.weekday() < 5:
            days += 1
    return days


class AppetiteEngine:
    def __init__(self, config: AppetiteConfig | None = None) -> None:
        self.cfg = config or AppetiteConfig()

    def decide(
        self, model: ExtractedModel, rule_results: list[RuleResult] | None = None
    ) -> Decision:
        cfg = self.cfg
        rule_results = rule_results or []
        data: dict[str, Any] = {f.name: f.value for f in model.fields}
        conf: dict[str, float] = {
            f.name: (f.confidence if f.confidence is not None else 1.0)
            for f in model.fields if not f.name.startswith("documents.")}

        appetite: list[dict[str, Any]] = []
        missing: list[dict[str, str]] = []
        consistency: list[dict[str, str]] = []
        factors: list[dict[str, Any]] = []
        failed: list[str] = []
        flags: list[str] = []

        def add(rule: str, passed: bool, hard: bool, detail: str) -> None:
            appetite.append({"rule": rule, "pass": passed, "hard": hard, "detail": detail})
            if not passed:
                failed.append(rule)

        class_code = str(data.get("acord.class_code") or "")
        revenue = (coerce_number(data.get("acord.stated_annual_revenue"))
                   or coerce_number(data.get("financials.total_revenue"))
                   or coerce_number(data.get("financials.total_revenue_rental_income")))
        trend = self._trend(data)
        factors.append({"name": "Class code", "value": class_code or "—", "weight": 3})
        factors.append({"name": "Annual revenue",
                        "value": data.get("acord.stated_annual_revenue") or "—", "weight": 2})
        factors.append({"name": "Loss trend", "value": trend, "weight": 3})
        lr = self._loss_ratio(data)
        if lr is not None:
            factors.append({"name": "Loss ratio", "value": f"{lr:.0%}", "weight": 2})

        # ── 1. EC-01 extraction confidence → manual review ──
        low_conf = sorted(n for n, c in conf.items() if c < cfg.confidence_floor)
        extraction_confidence = min(conf.values()) if conf else 1.0
        if len(low_conf) >= cfg.low_confidence_min_fields:
            add("EC-01", False, False,
                f"{len(low_conf)} field(s) below confidence floor {cfg.confidence_floor}")
            flags.append("MANUAL_REVIEW")
            factors.append({"name": "Manual review", "value": "low extraction confidence",
                            "weight": 0})
            return Decision(
                outcome=DecisionOutcome.REQUEST_INFO, score=None,
                rationale="Low extraction confidence — routed to manual review (no auto-triage).",
                rule_results=rule_results,
                details=self._details(appetite, missing, consistency, factors, failed, flags,
                                      trend, low_conf, extraction_confidence,
                                      manual_review=True, reason="low_extraction_confidence",
                                      suppress_narrative=True, hard_rule_passed=True))
        add("EC-01", True, False, "Extraction confidence acceptable")

        # ── 2. Hard rules (short-circuit DECLINE) ──
        reasons = self._hard_rules(data, class_code, revenue, add)
        if reasons:
            return Decision(
                outcome=DecisionOutcome.DECLINE, score=0.0,
                rationale="; ".join(reasons),
                rule_results=rule_results,
                details=self._details(appetite, missing, consistency, factors, failed, flags,
                                      trend, low_conf, extraction_confidence,
                                      suppress_narrative=True, hard_rule_passed=False))

        # ── 3. Completeness ──
        self._completeness(data, add, missing)
        # ── 4. Consistency + SOV/limit ──
        self._consistency(data, revenue, add, consistency)
        # ── 5. Timing ──
        timing = self._timing(data, add, factors, flags)
        # ── 6. Trend (soft factor only) ──
        add("LT-02", trend != "worsening", False, f"Loss severity trend: {trend}")

        soft = len(missing) + sum(1 for c in consistency if c["status"] == "fail") + int(timing)
        score: float
        if soft:
            outcome = DecisionOutcome.REQUEST_INFO
            score = min(cfg.request_info_score_cap, max(0, 100 - cfg.soft_flag_penalty * soft))
            rationale = "Requires more information / clarification before a decision."
        else:
            outcome = DecisionOutcome.PROCEED
            score = 100.0
            rationale = "Within appetite; all hard rules and checks passed."

        return Decision(
            outcome=outcome, score=float(score), rationale=rationale,
            rule_results=rule_results,
            details=self._details(appetite, missing, consistency, factors, failed, flags,
                                  trend, low_conf, extraction_confidence,
                                  suppress_narrative=False, hard_rule_passed=True))

    # ── hard rules ──────────────────────────────────
    def _hard_rules(
        self, data: dict[str, Any], class_code: str, revenue: float | None,
        add: Any,
    ) -> list[str]:
        cfg = self.cfg
        reasons: list[str] = []

        code = (re.match(r"\d+", class_code) or [""])[0] if class_code else ""
        hr01 = code in cfg.excluded_class_codes
        add("HR-01", not hr01, True,
            f"Class code {code or '?'} " + ("is on the excluded list" if hr01 else "is acceptable"))
        if hr01:
            reasons.append(f"Excluded class code {code} (HR-01)")

        hr02, detail = self._severity(data, class_code, revenue)
        add("HR-02", not hr02, True, detail)
        if hr02:
            reasons.append(f"Loss severity ceiling breached (HR-02): {detail}")

        state = str(data.get("acord.states_of_operation") or "").lower()
        tokens = {t.strip() for t in re.split(r"[,/]", state) if t.strip()}
        hr03 = bool(tokens) and not any(
            any(lic in tok or tok in lic for lic in cfg.licensed_states) for tok in tokens)
        add("HR-03", not hr03, True,
            "State(s) in appetite" if not hr03 else f"Unlicensed state(s): {state}")
        if hr03:
            reasons.append(f"Prohibited state (HR-03): {state}")

        hr04 = revenue is not None and revenue > cfg.revenue_authority_ceiling
        add("HR-04", not hr04, True,
            "Revenue within binding authority" if not hr04 else "Revenue over binding authority")
        if hr04:
            reasons.append("Revenue over binding authority (HR-04)")
        return reasons

    def _severity(
        self, data: dict[str, Any], class_code: str, revenue: float | None
    ) -> tuple[bool, str]:
        cfg = self.cfg
        claims = data.get("loss_run.claims")
        claims = claims if isinstance(claims, list) else []
        # (a) big open claim on a smaller risk
        if revenue is not None and revenue < cfg.severity_revenue_threshold:
            for c in claims:
                inc = coerce_number(c.get("incurred"))
                is_open = str(c.get("status", "")).lower() == "open"
                if is_open and inc and inc > cfg.severity_ceiling_incurred:
                    return True, (
                        f"open claim ${inc:,.0f} exceeds "
                        f"${cfg.severity_ceiling_incurred:,.0f} on revenue ${revenue:,.0f}")
        # (b) fall from height in a fall-prone class
        prone = any(k in class_code.lower() for k in cfg.fall_prone_class_keywords)
        if prone:
            for c in claims:
                cause = str(c.get("cause_of_loss", "")).lower()
                h = re.search(r"(\d{1,3})\s*ft", cause)
                if "fall" in cause and h and int(h.group(1)) > cfg.fall_height_ft:
                    return True, f"fall from {h.group(1)}ft in a fall-prone class"
        return False, "No hard-severity breach"

    # ── completeness ────────────────────────────────
    def _completeness(
        self, data: dict[str, Any], add: Any, missing: list[dict[str, str]]
    ) -> None:
        cfg = self.cfg
        # CR-02 financials present
        fin_present = "documents.financials.present" in data
        add("CR-02", fin_present, False,
            "Financial statement present" if fin_present else "Financial statement missing")
        if not fin_present:
            missing.append({"item": "Financial statement", "severity": "required",
                            "reason": "No financial statement was provided with the submission."})
        # CR-01 minimum loss-run history
        years = self._loss_years(data)
        cr01_ok = years is None or years >= cfg.min_loss_years
        add("CR-01", cr01_ok, False,
            f"Loss history {years or '?'} yr(s) (need {cfg.min_loss_years})")
        if years is not None and years < cfg.min_loss_years:
            missing.append({"item": "Loss run history", "severity": "required",
                            "reason": f"Only {years} of {cfg.min_loss_years} required years."})
        # CR-03 SOV location completeness (business income)
        locations = data.get("sov.locations")
        if isinstance(locations, list) and locations:
            incomplete = [i + 1 for i, loc in enumerate(locations)
                          if coerce_number(loc.get("business_income_12mo")) is None]
            add("CR-03", not incomplete, False,
                "All SOV locations complete" if not incomplete
                else f"SOV location(s) {incomplete} missing business income")
            if incomplete:
                missing.append({"item": "SOV business income", "severity": "required",
                                "reason": f"Location(s) {incomplete} missing business income."})

    # ── consistency ─────────────────────────────────
    def _consistency(
        self, data: dict[str, Any], revenue: float | None, add: Any,
        consistency: list[dict[str, str]],
    ) -> None:
        cfg = self.cfg
        # CC-01 revenue variance
        app_rev = coerce_number(data.get("acord.stated_annual_revenue"))
        fin_rev = (coerce_number(data.get("financials.total_revenue"))
                   or coerce_number(data.get("financials.total_revenue_rental_income")))
        if app_rev and fin_rev:
            var = abs(app_rev - fin_rev) / max(app_rev, fin_rev)
            fail = var > cfg.revenue_variance_pct
            add("CC-01", not fail, False, f"Revenue variance {var:.0%}")
            consistency.append({
                "label": "Revenue consistency",
                "detail": f"Application ${app_rev:,.0f} vs financials ${fin_rev:,.0f} ({var:.0%})",
                "status": "fail" if fail else "ok"})
        # CC-02 loss-disclosure consistency
        narrative = str(
            data.get("acord.prior_losses_disclosed_broker_insured_narrative") or "").lower()
        total_inc = coerce_number(data.get("loss_run.total_incurred"))
        asserts_none = any(p in narrative for p in cfg.disclosure_phrases)
        cc02_fail = (asserts_none and total_inc is not None
                     and total_inc > cfg.disclosure_materiality)
        add("CC-02", not cc02_fail, False,
            "Disclosure consistent with loss run" if not cc02_fail
            else "Disclosure understates loss run")
        if asserts_none and total_inc is not None:
            consistency.append({
                "label": "Loss disclosure consistency",
                "detail": (f"Application states no significant losses; loss run shows "
                           f"${total_inc:,.0f} incurred"),
                "status": "fail" if cc02_fail else "ok"})
        # CR-04 SOV total vs requested blanket limit
        tiv = coerce_number(data.get("sov.total_insurable_value"))
        requested = (_first_money(data.get("sov.portfolio_total_stated"))
                     or _first_money(data.get("acord.limits_requested")))
        if tiv and requested:
            over = tiv > requested
            add("CR-04", not over, False,
                f"SOV total ${tiv:,.0f} vs requested ${requested:,.0f}")
            consistency.append({
                "label": "SOV vs requested limit",
                "detail": f"SOV totals ${tiv:,.0f} against a ${requested:,.0f} blanket limit",
                "status": "fail" if over else "ok"})

    # ── timing ──────────────────────────────────────
    def _timing(
        self, data: dict[str, Any], add: Any, factors: list[dict[str, Any]], flags: list[str]
    ) -> bool:
        received = _parse_email_date(data.get("email.date"))
        effective = _parse_mdy(data.get("acord.effective_date"))
        lead = _business_days(received, effective)
        tight = lead is not None and lead < self.cfg.min_lead_time_business_days
        add("TR-01", not tight, False,
            f"Lead time {lead if lead is not None else '?'} business day(s)")
        if tight:
            flags.append("EXPEDITE")
            factors.append({"name": "Timing", "value": f"{lead} business days to effective",
                            "weight": 2})
        return tight

    # ── helpers ─────────────────────────────────────
    def _loss_years(self, data: dict[str, Any]) -> int | None:
        period = str(data.get("loss_run.total_incurred_period") or "")
        m = re.match(r"(\d+)", period)
        return int(m.group(1)) if m else None

    def _loss_ratio(self, data: dict[str, Any]) -> float | None:
        inc = coerce_number(data.get("loss_run.total_incurred"))
        prem = coerce_number(data.get("acord.prior_premium"))
        if inc is not None and prem:
            return inc / prem
        return None

    def _trend(self, data: dict[str, Any]) -> str:
        text = str(data.get("loss_run.loss_frequency_trend") or "").lower()
        if any(k in text for k in self.cfg.loss_trend_worsening_keywords):
            return "worsening"
        if "improving" in text:
            return "improving"
        return "flat"

    @staticmethod
    def _details(
        appetite: list[dict[str, Any]], missing: list[dict[str, str]],
        consistency: list[dict[str, str]], factors: list[dict[str, Any]],
        failed: list[str], flags: list[str], trend: str, low_conf: list[str],
        extraction_confidence: float, *, suppress_narrative: bool, hard_rule_passed: bool,
        manual_review: bool = False, reason: str | None = None,
    ) -> dict[str, Any]:
        return {
            "appetite": appetite, "missing_info": missing, "consistency": consistency,
            "factors": factors, "failed_rules": failed, "flags": flags, "trend": trend,
            "low_confidence_fields": low_conf, "extraction_confidence": extraction_confidence,
            "hard_rule_passed": hard_rule_passed, "manual_review": manual_review,
            "reason": reason, "suppress_narrative": suppress_narrative,
            "triggered_rule_ids": failed,
        }
