"""Dev seed script — inserts demo tenants + users so the header-stub auth and the
vertical lookup work immediately in dev.

Creates (idempotently, checked per-user so re-running after adding new users
to `_USERS` below still seeds the new ones even though the tenant already
exists):
  - Tenant "demo-mga"  (vertical MGA) with a junior + a senior user
  - Tenant "demo-es"   (vertical ES)  with a junior + a senior user, plus
    two real-email login users for the email-based-role login feature
    (``manager.j@gmail.com`` -> junior, ``manager.s@gmail.com`` -> senior)

Run (from the repo root, with the venv active):
    python -m core.seed          # if src is on PYTHONPATH
    python src/core/seed.py      # self-bootstraps src onto sys.path
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Allow direct execution (`python src/core/seed.py`) by putting src/ on the path.
_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from sqlmodel import col, select  # noqa: E402

from core.common.enums import Role, Vertical  # noqa: E402
from core.db import async_session_factory  # noqa: E402
from core.models import Tenant, User  # noqa: E402

_TENANTS = [
    ("demo-mga", "Demo MGA Ltd", Vertical.MGA),
    ("demo-es", "Demo E&S Brokerage", Vertical.ES),
]

_USERS = [
    ("demo-mga", "demo-mga-junior", "junior@demo-mga.example", "Demo Junior", Role.JUNIOR),
    ("demo-mga", "demo-mga-senior", "senior@demo-mga.example", "Demo Senior", Role.SENIOR),
    ("demo-es", "demo-es-junior", "junior@demo-es.example", "Demo Junior", Role.JUNIOR),
    ("demo-es", "demo-es-senior", "senior@demo-es.example", "Demo Senior", Role.SENIOR),
    # Real-email login users (email-based-role login feature) — the email
    # itself is what determines the role, per the login endpoint's lookup.
    ("demo-es", "demo-es-manager-j", "manager.j@gmail.com", "Manager J", Role.JUNIOR),
    ("demo-es", "demo-es-manager-s", "manager.s@gmail.com", "Manager S", Role.SENIOR),
]


async def seed() -> None:
    async with async_session_factory() as session:
        for tenant_id, name, vertical in _TENANTS:
            existing = (
                await session.execute(select(Tenant).where(col(Tenant.id) == tenant_id))
            ).scalar_one_or_none()
            if existing is None:
                session.add(Tenant(id=tenant_id, name=name, vertical=vertical))
                print(f"+ seeded tenant '{tenant_id}' ({vertical})")
            else:
                print(f"= tenant '{tenant_id}' already exists, skipping")
        await session.commit()

        for tenant_id, user_id, email, display_name, role in _USERS:
            existing_user = (
                await session.execute(select(User).where(col(User.id) == user_id))
            ).scalar_one_or_none()
            if existing_user is None:
                session.add(
                    User(
                        id=user_id, tenant_id=tenant_id, email=email, name=display_name, role=role
                    )
                )
                print(f"+ seeded user '{email}' ({role.value}) for tenant '{tenant_id}'")
            else:
                print(f"= user '{email}' already exists, skipping")
        await session.commit()
    print("Seed complete.")


if __name__ == "__main__":
    asyncio.run(seed())
