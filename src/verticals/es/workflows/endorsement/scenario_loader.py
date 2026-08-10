"""Loads Workflow_<n>'s ``scenario_XX/`` endorsement fixtures.

Same mixed-shape precedent as Binder & Issuance's Workflow_14: a pre-issuance
pass ships ``bound_policy_context.json`` (+ ``endorsement_request_email.txt``,
a raw email) and a post-issuance reconciliation pass ships
``endorsement_request_sent.json`` (+ ``carrier_issued_endorsement.txt``).
Doesn't fit ``src/fixtures/loader.py`` — workflow-owned, same precedent as
every prior E&S workflow's loader. See DATA_AND_FIXTURES.md's Workflow_15
layout note.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.config import get_settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScenarioBundle:
    scenario_ref: str
    bound_policy_context: dict[str, Any] | None = None
    endorsement_request_email_text: str | None = None
    endorsement_request_sent: dict[str, Any] | None = None
    carrier_issued_endorsement_text: str | None = None


def _dataset_dir(n: int) -> Path | None:
    root = get_settings().test_data_root
    if not root:
        log.warning("TEST_DATA_ROOT is not set; returning no scenarios.")
        return None
    dataset = Path(root) / f"Workflow_{n}" / "test_dataset"
    if not dataset.is_dir():
        log.warning("Fixture dataset not found at %s; returning no scenarios.", dataset)
        return None
    return dataset


def list_scenario_refs(n: int) -> list[str]:
    dataset = _dataset_dir(n)
    if dataset is None:
        return []
    return sorted(p.name for p in dataset.glob("scenario_*") if p.is_dir())


def _read_json(path: Path) -> dict[str, Any] | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def _read_text(path: Path) -> str | None:
    return path.read_text(encoding="utf-8") if path.is_file() else None


def load_scenario(n: int, scenario_ref: str) -> ScenarioBundle:
    """Loads whichever of the four fixture files exist for one scenario.
    Raises ``FileNotFoundError`` if the dataset or scenario folder itself is
    missing — a caller asking for a SPECIFIC scenario wants a loud failure."""
    dataset = _dataset_dir(n)
    if dataset is None:
        raise FileNotFoundError(f"TEST_DATA_ROOT not set or Workflow_{n} dataset missing")
    scenario_dir = dataset / scenario_ref
    if not scenario_dir.is_dir():
        raise FileNotFoundError(f"no scenario folder at {scenario_dir}")

    return ScenarioBundle(
        scenario_ref=scenario_ref,
        bound_policy_context=_read_json(scenario_dir / "bound_policy_context.json"),
        endorsement_request_email_text=_read_text(scenario_dir / "endorsement_request_email.txt"),
        endorsement_request_sent=_read_json(scenario_dir / "endorsement_request_sent.json"),
        carrier_issued_endorsement_text=_read_text(scenario_dir / "carrier_issued_endorsement.txt"),
    )
