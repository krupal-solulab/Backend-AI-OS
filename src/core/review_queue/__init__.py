"""Review Queue (Phase 1) — items awaiting a human, with role-based actions.

Enforces RBAC + the ``JUNIOR_PREMIUM_CAP`` authority limit on actions (a junior cannot
approve above the cap → must escalate; send/issue/override are senior/admin only). This
gating lives here so workflows never re-implement it.
"""

from core.review_queue.service import (
    AuthorityError,
    DefaultReviewQueueService,
    ReviewQueueService,
)

__all__ = ["AuthorityError", "DefaultReviewQueueService", "ReviewQueueService"]
