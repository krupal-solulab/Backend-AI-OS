"""Pydantic output schema for the Market Matching workflow — shapes what rides in
``OutputPackage.payload`` (the frozen contract's vertical-specific escape hatch).
Not a `core.common` contract; free to evolve with this workflow's FE screen.
"""

from __future__ import annotations

from pydantic import BaseModel


class CarrierMatchOut(BaseModel):
    carrier_id: str
    carrier_name: str
    score: float
    missing: list[str] = []
    flags: list[str] = []


class ExcludedCarrierOut(BaseModel):
    carrier_id: str
    carrier_name: str
    rule: str
    reason: str


class DiligentSearchOut(BaseModel):
    required: bool
    on_file: int
    compliant: bool
    note: str


class MarketMatchingPayload(BaseModel):
    """The FE Market Matching screen's data needs: a ranked panel, what got
    excluded and why, and the independent compliance flag."""

    submission_id: str | None
    matches: list[CarrierMatchOut] = []
    excluded: list[ExcludedCarrierOut] = []
    diligent_search: DiligentSearchOut
