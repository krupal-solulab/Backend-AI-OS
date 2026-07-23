"""Step 0 extraction-tuning proofs on the REAL Workflow_1 submissions."""

from __future__ import annotations

import pytest

from core.common.dtos import Ctx, RawBundle, RawDocument
from core.common.enums import Role, Vertical
from core.config import get_settings
from core.extraction import DefaultExtractionService
from core.extraction.service import coerce_number
from fixtures import load_workflow

pytestmark = pytest.mark.skipif(
    not get_settings().test_data_root,
    reason="TEST_DATA_ROOT not set; real Workflow_1 fixtures unavailable",
)

CTX = Ctx(tenant_id="demo-mga", vertical=Vertical.MGA, user_id="u", role=Role.JUNIOR)


async def _fields(ref: str) -> dict:
    loaded = {ls.submission.external_ref: ls for ls in load_workflow(1)}
    ls = loaded[ref]
    raw = RawBundle(submission_id=ref, documents=[
        RawDocument(kind=d.kind, filename=d.filename, content=d.content or "")
        for d in ls.documents])
    model = await DefaultExtractionService().extract(CTX, raw)
    return {f.name: f for f in model.fields}


def test_coerce_number_handles_currency_and_qualifiers() -> None:
    assert coerce_number("$680,000 (rental income)") == 680000.0
    assert coerce_number("$2,635,000+ (incomplete)") == 2635000.0
    assert coerce_number("$0") == 0.0
    assert coerce_number("approx $20,100-$28,100 (range)") is None  # ranges not numeric
    assert coerce_number("N/A") is None


@pytest.mark.parametrize(
    ("ref", "period", "total", "claims"),
    [
        ("submission_02", "2yr", 20700.0, 2),
        ("submission_03", "5yr", 463500.0, 3),
        ("submission_05", "5yr", 373500.0, 3),
        ("submission_07", "5yr", 216400.0, 3),
    ],
)
async def test_loss_run_canonical_total_and_claims(ref, period, total, claims) -> None:
    f = await _fields(ref)
    assert f["loss_run.total_incurred"].value == total
    assert f["loss_run.total_incurred_period"].value == period
    assert isinstance(f["loss_run.claims"].value, list)
    assert len(f["loss_run.claims"].value) == claims


async def test_sov_locations_and_aggregate_tiv() -> None:
    f6 = await _fields("submission_06")
    assert len(f6["sov.locations"].value) == 1
    assert f6["sov.total_insurable_value"].value == 4200000.0

    f9 = await _fields("submission_09")
    assert len(f9["sov.locations"].value) == 5
    # 3.53M + 2.72M + 3.20M + 2.195M + 2.635M = 14.28M (exceeds $12.5M requested → CR-04)
    assert f9["sov.total_insurable_value"].value == 14280000.0


async def test_qualified_revenue_is_coercible() -> None:
    f6 = await _fields("submission_06")
    assert coerce_number(f6["acord.stated_annual_revenue"].value) == 680000.0


async def test_submission_08_low_confidence_markers() -> None:
    f = await _fields("submission_08")
    low = [n for n, v in f.items() if v.confidence < 0.8]
    assert len(low) >= 2, f"expected degraded-scan fields flagged low-confidence, got {low}"
    # its loss total is a non-numeric range → kept as string, not a false number
    assert isinstance(f["loss_run.total_incurred"].value, str)


async def test_clean_submission_has_no_low_confidence() -> None:
    f = await _fields("submission_01")
    assert all(v.confidence >= 0.8 for v in f.values())
