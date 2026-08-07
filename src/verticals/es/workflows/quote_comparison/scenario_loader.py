"""Loads Workflow_<n>'s ``scenario_XX/`` carrier-response fixtures.

Unlike Workflow_11/12's single JSON per case, this dataset's ``scenario_XX/``
folders hold RAW carrier-response ``.txt`` files (unstructured email text,
non-uniform filenames — ``carrier_response_ironclad.txt``,
``carrier_response_alt_market.txt``, ``carrier_response_a.txt``, etc.) plus
an optional ``system_check_context.json`` (only scenario_06 ships one, for
QC-07's "as of" reference date — see ``comparison_engine.py``). Neither shape
matches the shared ``src/fixtures/loader.py`` glob (``submission_*``, fixed
filenames), so this is workflow-owned, same precedent as Workflow_11/12's
loaders. See DATA_AND_FIXTURES.md's Workflow_13 layout note.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.config import get_settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CarrierResponseFile:
    filename: str
    content: str


@dataclass(frozen=True)
class ScenarioBundle:
    scenario_ref: str
    responses: list[CarrierResponseFile] = field(default_factory=list)
    system_check_context: dict[str, Any] | None = None


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


def load_scenario(n: int, scenario_ref: str) -> ScenarioBundle:
    """Loads every ``carrier_response_*.txt`` in one scenario folder, plus its
    ``system_check_context.json`` if present. Raises ``FileNotFoundError`` if
    the dataset or scenario folder is missing — a caller asking for a
    SPECIFIC scenario wants a loud failure, not a silently empty result."""
    dataset = _dataset_dir(n)
    if dataset is None:
        raise FileNotFoundError(f"TEST_DATA_ROOT not set or Workflow_{n} dataset missing")
    scenario_dir = dataset / scenario_ref
    if not scenario_dir.is_dir():
        raise FileNotFoundError(f"no scenario folder at {scenario_dir}")

    responses = [
        CarrierResponseFile(filename=p.name, content=p.read_text(encoding="utf-8"))
        for p in sorted(scenario_dir.glob("carrier_response_*.txt"))
    ]
    context_path = scenario_dir / "system_check_context.json"
    context = (
        json.loads(context_path.read_text(encoding="utf-8")) if context_path.is_file() else None
    )
    return ScenarioBundle(
        scenario_ref=scenario_ref, responses=responses, system_check_context=context
    )
