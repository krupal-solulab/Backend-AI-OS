"""MGA · Renewal Management (Workflow_2) — second MGA workflow.

Compares the expiring term (prior_policy_snapshot) against the current renewal
(renewal_questionnaire + updated loss run/financials) and maps the RN-01..RN-12 rules to
a frozen ``Decision``, then to the FE ``RenewalRecommendation``. All thresholds live in
``RenewalConfig`` (data); the RN-09 appetite recheck reuses the MGA ``AppetiteConfig``
data. Nothing here touches ``core/common`` or the Submission Triage workflow.
"""

from verticals.mga.renewal_management.comparison import RenewalComparisonEngine
from verticals.mga.renewal_management.config import RenewalConfig

__all__ = ["RenewalComparisonEngine", "RenewalConfig"]
