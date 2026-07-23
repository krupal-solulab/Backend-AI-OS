"""Dev seed script — inserts demo tenants + users so the header-stub auth and the
vertical lookup work immediately in dev.

Creates (idempotently):
  - Tenant "demo-mga"  (vertical MGA) with a junior + a senior user
  - Tenant "demo-es"   (vertical ES)  with a junior + a senior user

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


async def seed() -> None:
    async with async_session_factory() as session:
        for tenant_id, name, vertical in _TENANTS:
            existing = (
                await session.execute(select(Tenant).where(col(Tenant.id) == tenant_id))
            ).scalar_one_or_none()
            if existing is not None:
                print(f"= tenant '{tenant_id}' already exists, skipping")
                continue

            session.add(Tenant(id=tenant_id, name=name, vertical=vertical))
            session.add_all(
                [
                    User(
                        id=f"{tenant_id}-junior",
                        tenant_id=tenant_id,
                        email=f"junior@{tenant_id}.example",
                        name="Demo Junior",
                        role=Role.JUNIOR,
                    ),
                    User(
                        id=f"{tenant_id}-senior",
                        tenant_id=tenant_id,
                        email=f"senior@{tenant_id}.example",
                        name="Demo Senior",
                        role=Role.SENIOR,
                    ),
                ]
            )
            print(f"+ seeded tenant '{tenant_id}' ({vertical}) with junior + senior users")

        await session.commit()
    print("Seed complete.")


if __name__ == "__main__":
    asyncio.run(seed())
