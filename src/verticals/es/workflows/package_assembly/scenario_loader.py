"""Loads Workflow_<n>'s ``scenario_*/market_matching_output.json`` fixtures.

This is package_assembly-owned fixture access, separate from the shared
``fixtures.loader`` — that loader only turns ``submission_*/*.txt`` into
Submission + Document rows; it has no notion of a ``scenario_*`` folder or a
``market_matching_output.json`` file. Same precedent as Workflow_10's
``carrier_profiles/`` loader in decision_core. See DATA_AND_FIXTURES.md's
"Workflow_11 layout note".
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
class ScenarioInput:
    """One scenario's raw ``market_matching_output.json``, unparsed beyond
    JSON decoding — callers pick out the single- or multi-carrier shape."""

    scenario_ref: str
    data: dict[str, Any]


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
    """All ``scenario_*`` folder names for ``Workflow_<n>``, sorted."""
    dataset = _dataset_dir(n)
    if dataset is None:
        return []
    return sorted(p.name for p in dataset.glob("scenario_*") if p.is_dir())


def load_scenario(n: int, scenario_ref: str) -> ScenarioInput:
    """Loads one scenario's ``market_matching_output.json``. Raises
    ``FileNotFoundError`` if the dataset or scenario is missing — unlike the
    shared loader's never-crash convention, a caller asking for a SPECIFIC
    scenario by name wants a loud failure, not a silently empty result."""
    dataset = _dataset_dir(n)
    if dataset is None:
        raise FileNotFoundError(f"TEST_DATA_ROOT not set or Workflow_{n} dataset missing")
    path = dataset / scenario_ref / "market_matching_output.json"
    if not path.is_file():
        raise FileNotFoundError(f"no market_matching_output.json at {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return ScenarioInput(scenario_ref=scenario_ref, data=data)


def carrier_view(scenario: ScenarioInput, carrier_id: str | None = None) -> dict[str, Any]:
    """Normalizes the single-carrier vs. multi-carrier (``carriers: [...]``)
    JSON shapes into one flat per-carrier dict. If the scenario is
    multi-carrier and ``carrier_id`` is omitted, the first carrier is used."""
    data = scenario.data
    if "carriers" in data:
        carriers = data["carriers"]
        if carrier_id is not None:
            match = next((c for c in carriers if c["carrier_id"] == carrier_id), None)
            if match is None:
                raise KeyError(f"carrier '{carrier_id}' not in scenario '{scenario.scenario_ref}'")
        else:
            match = carriers[0]
        merged = {**match}
        merged["submission_id"] = data.get("submission_id")
        merged["named_insured"] = data.get("named_insured")
        merged.setdefault("documents_available_from_extraction", match.get("documents_available"))
        merged.setdefault("missing_info_from_market_matching", [])
        return merged

    if carrier_id is not None and data.get("carrier_id") != carrier_id:
        raise KeyError(f"carrier '{carrier_id}' not in scenario '{scenario.scenario_ref}'")
    view = {**data}
    view.setdefault("missing_info_from_market_matching", [])
    return view


def all_carrier_ids(scenario: ScenarioInput) -> list[str]:
    """Every carrier this scenario's broker selection covers — used to fan
    out one independent assembly pass per carrier (FR-2/FR-4/FR-23)."""
    data = scenario.data
    if "carriers" in data:
        return [c["carrier_id"] for c in data["carriers"]]
    if "carrier_id" in data:
        return [data["carrier_id"]]
    return list(data.get("broker_selected_carriers", []))
