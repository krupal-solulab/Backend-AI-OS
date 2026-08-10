"""Renewal Comparison Engine — maps RN-01..RN-12 (+ RN-09 appetite recheck) onto a
frozen ``Decision``. Every branch is driven by extracted fields + ``RenewalConfig`` +
the reused ``AppetiteConfig`` data — no per-case literals, so new cases/rules/docs work
with no code change.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date
from typing import Any

from core.common.dtos import Decision, ExtractedModel, RuleResult
from core.common.enums import DecisionOutcome
from core.extraction.service import coerce_number
from verticals.mga.decision_core import AppetiteConfig
from verticals.mga.renewal_management.config import RenewalConfig

# ── ops-change seam (RN-05): default keyword detector; swap for an LLM check later ──
OpsChangeDetector = Callable[[str], bool]


def keyword_ops_change(answer: str) -> bool:
    a = answer.strip().lower()
    return bool(a) and not any(a.startswith(neg) for neg in ("no", "none", "n/a"))


def _mdy(value: Any) -> date | None:
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", str(value or ""))
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _email_date(value: Any) -> date | None:
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})", str(value or ""))
    if not m or m.group(2).lower() not in _MONTHS:
        return None
    try:
        return date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))
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


class RenewalComparisonEngine:
    def __init__(
        self,
        config: RenewalConfig | None = None,
        appetite_config: AppetiteConfig | None = None,
        ops_change_detector: OpsChangeDetector = keyword_ops_change,
    ) -> None:
        self.cfg = config or RenewalConfig()
        self.appetite = appetite_config or AppetiteConfig()
        self.detect_ops_change = ops_change_detector

    def decide(
        self, model: ExtractedModel, rule_results: list[RuleResult] | None = None
    ) -> Decision:
        cfg = self.cfg
        rule_results = rule_results or []
        data: dict[str, Any] = {f.name: f.value for f in model.fields}
        conf = {
            f.name: (f.confidence if f.confidence is not None else 1.0)
            for f in model.fields if not f.name.startswith("documents.")
        }

        flags: list[dict[str, Any]] = []       # ChangeFlag[]
        comparison: list[dict[str, Any]] = []   # CompareRow[]
        loss_changes: list[dict[str, Any]] = []  # LossChange[]
        appetite: list[dict[str, Any]] = []      # AppetiteResult[]
        missing: list[dict[str, str]] = []       # MissingItem[]
        changes: list[dict[str, str]] = []       # RenewalChangeItem[]
        triggered: list[str] = []

        def flag(cat: str, label: str, detail: str, direction: str, rule: str) -> None:
            flags.append({"category": cat, "label": label, "detail": detail,
                          "direction": direction})
            triggered.append(rule)
            if cat != "timing":  # timing is procedural, not a "what's changing" item
                changes.append({"item": label, "reason": detail, "source": rule})

        # ── values ──
        prior_rev = coerce_number(self._get(
            data, "prior_policy.stated_revenue_at_prior_renewal",
            "prior_policy.stated_rental_income_at_prior_renewal"))
        cur_rev = coerce_number(self._get(
            data, "renewal_questionnaire.stated_current_annual_revenue",
            "renewal_questionnaire.stated_current_rental_income"))
        prior_emp = coerce_number(data.get("prior_policy.number_of_employees_at_prior_renewal"))
        cur_emp = coerce_number(data.get("renewal_questionnaire.current_number_of_employees"))
        class_code = str(data.get("prior_policy.class_code") or "")
        new_states = str(data.get("renewal_questionnaire.any_new_locations_states") or "")
        ops_answer = str(data.get("renewal_questionnaire.any_change_in_operations") or "")
        prior_claims = coerce_number(self._prefix(data, "prior_policy.claims_during_expiring_term"))
        prior_reserve = self._money(self._prefix(data, "prior_policy.claims_during_expiring_term"))
        new_claims_ct = coerce_number(data.get("loss_run.claims_in_expiring_term")) or 0.0
        expiration = _mdy(data.get("renewal_questionnaire.effective_date_requested"))
        received = _email_date(data.get("email.date"))

        # ── EC: degraded-scan → manual review (parity with Triage) ──
        low_conf = sorted(n for n, c in conf.items() if c < cfg.confidence_floor)
        extraction_confidence = min(conf.values()) if conf else 1.0
        if len(low_conf) >= cfg.low_confidence_min_fields:
            return self._decision(
                DecisionOutcome.REQUEST_INFO, None,
                "Low extraction confidence — routed to manual review.",
                rule_results, flags, comparison, loss_changes, appetite, missing, changes,
                triggered, extraction_confidence, data,
                manual_review=True, needs_info=True)

        # ── completeness (from published validation rule set) → REQUEST_INFO ──
        for r in rule_results:
            if not r.passed and r.check_type.value == "required":
                missing.append({"item": r.rule_id, "severity": "required",
                                "reason": r.message or "Required renewal document/field missing."})
        # RN-12 (no submission): questionnaire absent
        if "documents.renewal_questionnaire.present" not in data:
            missing.append({"item": "Renewal questionnaire", "severity": "required",
                            "reason": "No renewal questionnaire (RN-12) — outreach needed."})

        # ── RN-09 appetite recheck (hard → NON_RENEW), reusing AppetiteConfig data ──
        drift = self._appetite_recheck(class_code, appetite)
        if drift is not None:
            return self._decision(
                DecisionOutcome.DECLINE, 0.0, drift, rule_results, flags, comparison,
                loss_changes, appetite, missing, changes, triggered + ["RN-09"],
                extraction_confidence, data, appetite_drift=drift)

        if missing:
            return self._decision(
                DecisionOutcome.REQUEST_INFO, None,
                "Missing renewal information — request from broker before deciding.",
                rule_results, flags, comparison, loss_changes, appetite, missing, changes,
                triggered, extraction_confidence, data, needs_info=True)

        # ── RN-01/02 revenue ──
        if prior_rev and cur_rev:
            delta = (cur_rev - prior_rev) / prior_rev
            comparison.append(self._row("Stated revenue", prior_rev, cur_rev, delta))
            if delta > cfg.revenue_growth_pct:
                flag("exposure", "Revenue growth",
                     f"Stated revenue up {delta:.0%} since prior term — rate/limit review",
                     "unfavorable", "RN-01")
            elif -delta > cfg.revenue_decline_pct:
                flag("exposure", "Revenue decline", f"Stated revenue down {-delta:.0%} — premium "
                     f"right-sizing opportunity", "favorable", "RN-02")

        # ── RN-03 headcount ──
        if prior_emp and cur_emp:
            delta = (cur_emp - prior_emp) / prior_emp
            comparison.append(self._row("Employees", prior_emp, cur_emp, delta, money=False))
            if abs(delta) > cfg.headcount_change_pct:
                flag("exposure", "Headcount change", f"Employee count changed {delta:+.0%} "
                     f"since prior term", "unfavorable", "RN-03")

        # ── RN-04 new state ──
        if new_states and not self._is_negative(new_states):
            comparison.append({"label": "New states", "prior": "—", "current": new_states,
                               "change": "added", "direction": "unfavorable"})
            flag("appetite", "New state added", f"New state(s)/locations: {new_states} "
                 f"— state licensing/appetite check required", "unfavorable", "RN-04")

        # ── RN-05 operations change (seam) ──
        if ops_answer and self.detect_ops_change(ops_answer):
            flag("info", "Operations change", f"Operations narrative changed: {ops_answer}",
                 "neutral", "RN-05")

        # ── RN-06 / RN-08 loss ──
        max_open = self._max_open_incurred(data)
        if new_claims_ct >= cfg.max_new_claims_in_term or (
            max_open is not None and max_open > cfg.new_claim_severity
        ):
            detail = f"{int(new_claims_ct)} new claim(s) in expiring term"
            if max_open:
                detail += f"; largest open ${max_open:,.0f}"
            flag("loss", "Loss deterioration", detail, "unfavorable", "RN-06")
            loss_changes.append({"type": "new_claim", "description": detail,
                                 "direction": "unfavorable", "source": "loss_run"})
        if prior_claims == 0 and new_claims_ct >= 1:
            flag("loss", "Frequency trend break", "Zero claims in prior years, then "
                 f"{int(new_claims_ct)} in the expiring term", "unfavorable", "RN-08")
            loss_changes.append({"type": "trend", "description": "Trend break: claim-free history "
                                 "→ new claim(s) this term", "direction": "unfavorable"})

        # ── RN-07 favorable resolution ──
        if prior_reserve and self._favorable_close(data, prior_reserve):
            triggered.append("RN-07")
            loss_changes.append({"type": "favorable_closure", "description": "Prior open claim "
                                 f"closed materially below its ${prior_reserve:,.0f} reserve",
                                 "direction": "favorable", "source": "loss_run"})

        # ── RN-11 timing / lapse risk ──
        biz = _business_days(received, expiration)
        lapse = biz is not None and biz < cfg.min_lead_time_business_days
        if lapse:
            flag("timing", "Lapse risk", f"Only {biz} business day(s) to expiration — expedite",
                 "unfavorable", "RN-11")

        # ── outcome + score ──
        change_flags = [f for f in flags if f["category"] != "timing"]
        score = max(0, 100 - cfg.change_flag_penalty * len(change_flags))
        if change_flags:
            outcome = DecisionOutcome.PROCEED
            rationale = "Renewable with changes — material change(s) require underwriter review."
        else:
            outcome = DecisionOutcome.PROCEED
            score = 100
            rationale = "Clean renewal — no material change since prior term."
        return self._decision(
            outcome, float(score), rationale, rule_results, flags, comparison, loss_changes,
            appetite, missing, changes, triggered, extraction_confidence, data,
            lapse_risk=lapse, days_to_expiration=self._days(received, expiration))

    # ── RN-09: reuse AppetiteConfig data for the current-appetite recheck ──
    def _appetite_recheck(self, class_code: str, appetite: list[dict[str, Any]]) -> str | None:
        code = (re.match(r"\d+", class_code) or [""])[0] if class_code else ""
        excluded = code in self.appetite.excluded_class_codes
        appetite.append({"rule": "RN-09/HR-01 excluded class", "pass": not excluded,
                         "hard": True, "detail": f"Class {code or '?'}"})
        if excluded:
            return (f"was in appetite at binding; current appetite rules exclude class {code} "
                    f"(appetite drift, RN-10) → NON_RENEW")
        return None

    # ── helpers ──
    @staticmethod
    def _get(data: dict[str, Any], *names: str) -> Any:
        for n in names:
            if n in data:
                return data[n]
        return None

    @staticmethod
    def _prefix(data: dict[str, Any], prefix: str) -> Any:
        for n, v in data.items():
            if n.startswith(prefix):
                return v
        return None

    def _is_negative(self, answer: str) -> bool:
        a = answer.strip().lower()
        return not a or any(a.startswith(neg) for neg in self.cfg.negative_answers)

    @staticmethod
    def _money(text: Any) -> float | None:
        m = re.search(r"\$[\d,]+", str(text or ""))
        return coerce_number(m.group(0)) if m else None

    @staticmethod
    def _max_open_incurred(data: dict[str, Any]) -> float | None:
        claims = data.get("loss_run.claims")
        if not isinstance(claims, list):
            return None
        vals = [n for c in claims if str(c.get("status", "")).strip().lower().startswith("open")
                and (n := coerce_number(c.get("incurred"))) is not None]
        return max(vals) if vals else None

    def _favorable_close(self, data: dict[str, Any], prior_reserve: float) -> bool:
        claims = data.get("loss_run.claims")
        if not isinstance(claims, list):
            return False
        threshold = (1 - self.cfg.favorable_close_pct) * prior_reserve
        for c in claims:
            paid = coerce_number(c.get("paid"))
            closed = "closed" in str(c.get("status", "")).lower()
            if closed and paid is not None and paid <= threshold:
                return True
        return False

    @staticmethod
    def _row(
        label: str, prior: float, current: float, delta: float, money: bool = True
    ) -> dict[str, Any]:
        def fmt(v: float) -> str:
            return f"${v:,.0f}" if money else f"{v:,.0f}"
        direction = "favorable" if delta < 0 else "unfavorable" if delta > 0 else "neutral"
        return {"label": label, "prior": fmt(prior), "current": fmt(current),
                "change": f"{delta:+.0%}", "direction": direction, "strong": abs(delta) > 0.25}

    @staticmethod
    def _days(a: date | None, b: date | None) -> int:
        return (b - a).days if a and b and b >= a else 0

    def _decision(
        self, outcome: DecisionOutcome, score: float | None, rationale: str,
        rule_results: list[RuleResult], flags: list[dict[str, Any]],
        comparison: list[dict[str, Any]], loss_changes: list[dict[str, Any]],
        appetite: list[dict[str, Any]], missing: list[dict[str, str]],
        changes: list[dict[str, str]], triggered: list[str], extraction_confidence: float,
        data: dict[str, Any], *, manual_review: bool = False, needs_info: bool = False,
        appetite_drift: str | None = None, lapse_risk: bool = False,
        days_to_expiration: int = 0,
    ) -> Decision:
        favorable = "RN-07" in triggered
        loss = any(t in triggered for t in ("RN-06", "RN-08"))
        retention = ("favorable" if favorable and not loss and outcome is DecisionOutcome.PROCEED
                     else "at-risk" if loss or outcome is DecisionOutcome.DECLINE else "neutral")
        return Decision(
            outcome=outcome, score=score, rationale=rationale, rule_results=rule_results,
            details={
                "change_flags": flags, "comparison": comparison, "loss_changes": loss_changes,
                "appetite": appetite, "missing_info": missing, "changes": changes,
                "triggered_rules": sorted(set(triggered)), "retention": retention,
                "hard_rule_passed": outcome is not DecisionOutcome.DECLINE,
                "manual_review": manual_review, "needs_info": needs_info,
                "appetite_drift": appetite_drift, "lapse_risk": lapse_risk,
                "no_submission": "documents.renewal_questionnaire.present" not in data,
                "days_to_expiration": days_to_expiration,
                "extraction_confidence": extraction_confidence,
                "prior_source": ("PAS" if "documents.prior_policy.present" in data
                                 else "manual_queue"),
            },
        )
