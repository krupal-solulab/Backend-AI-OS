"""Loads the E&S carrier appetite panel (``carrier_profiles/*.json``) for a
Workflow_<n> dataset. This is E&S-owned fixture access, separate from the shared
``fixtures.loader`` (which only turns ``submission_*/*.txt`` into Submission +
Document — it has no notion of a carrier panel). See DATA_AND_FIXTURES.md's
"Workflow_10 layout note".
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from core.config import get_settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SeverityCeiling:
    max_single_claim_incurred: float


@dataclass(frozen=True)
class PremiumBand:
    min: float
    max: float


@dataclass(frozen=True)
class SubmissionRequirements:
    min_loss_run_years: int
    required_documents: tuple[str, ...] = ()
    acceptance_window_days: int | None = None


@dataclass(frozen=True)
class CarrierProfile:
    """One carrier's appetite profile. ``ceiling_type`` ("hard" | "soft"), when
    present in the source JSON, is the carrier's own explicit MM-05
    hard/soft severity-ceiling declaration — see matching.py's
    `_severity_is_hard`, which uses it when set and falls back to the
    roofing-class heuristic only when a carrier's profile leaves it unset."""

    carrier_id: str
    carrier_name: str
    class_codes_accepted: tuple[str, ...]
    class_codes_excluded: tuple[str, ...]
    states_licensed: tuple[str, ...]
    premium_band: PremiumBand
    submission_requirements: SubmissionRequirements
    severity_ceiling: SeverityCeiling
    appetite_confidence: str
    historical_hit_rate_this_class: float
    lines_written: tuple[str, ...] = ()
    notes: str = ""
    ceiling_type: str | None = None  # "hard" | "soft" | None (unset -> heuristic)


def _dataset_dir(n: int) -> Path | None:
    root = get_settings().test_data_root
    if not root:
        log.warning("TEST_DATA_ROOT is not set; returning no carrier profiles.")
        return None
    dataset = Path(root) / f"Workflow_{n}" / "test_dataset"
    if not dataset.is_dir():
        log.warning("Fixture dataset not found at %s; returning no carrier profiles.", dataset)
        return None
    return dataset


def _to_profile(raw: dict[str, object]) -> CarrierProfile:
    pb = raw["premium_band"]
    sr = raw["submission_requirements"]
    sc = raw["severity_ceiling"]
    assert isinstance(pb, dict) and isinstance(sr, dict) and isinstance(sc, dict)
    return CarrierProfile(
        carrier_id=str(raw["carrier_id"]),
        carrier_name=str(raw["carrier_name"]),
        class_codes_accepted=tuple(raw.get("class_codes_accepted", []) or []),  # type: ignore[arg-type]
        class_codes_excluded=tuple(raw.get("class_codes_excluded", []) or []),  # type: ignore[arg-type]
        states_licensed=tuple(raw.get("states_licensed", []) or []),  # type: ignore[arg-type]
        premium_band=PremiumBand(min=float(pb["min"]), max=float(pb["max"])),
        submission_requirements=SubmissionRequirements(
            min_loss_run_years=int(sr["min_loss_run_years"]),
            required_documents=tuple(sr.get("required_documents", []) or []),
            acceptance_window_days=sr.get("acceptance_window_days"),
        ),
        severity_ceiling=SeverityCeiling(max_single_claim_incurred=float(sc["max_single_claim_incurred"])),
        appetite_confidence=str(raw.get("appetite_confidence", "medium")),
        historical_hit_rate_this_class=float(raw.get("historical_hit_rate_this_class", 0.5)),  # type: ignore[arg-type]
        lines_written=tuple(raw.get("lines_written", []) or []),  # type: ignore[arg-type]
        notes=str(raw.get("notes", "")),
        ceiling_type=raw.get("ceiling_type"),  # type: ignore[arg-type]
    )


def load_carrier_panel(n: int) -> list[CarrierProfile]:
    """Load every ``carrier_profiles/*.json`` for ``Workflow_<n>``. Missing path/folder
    -> ``[]`` (warned), matching the shared loader's never-crash convention."""
    dataset = _dataset_dir(n)
    if dataset is None:
        return []
    panel_dir = dataset / "carrier_profiles"
    if not panel_dir.is_dir():
        log.warning("No carrier_profiles/ folder in %s.", dataset)
        return []

    profiles: list[CarrierProfile] = []
    for path in sorted(panel_dir.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Could not read carrier profile %s: %s", path, exc)
            continue
        profiles.append(_to_profile(raw))

    log.info("Loaded %d carrier profiles for Workflow_%d.", len(profiles), n)
    return profiles


__all__ = [
    "CarrierProfile",
    "PremiumBand",
    "SeverityCeiling",
    "SubmissionRequirements",
    "load_carrier_panel",
]
