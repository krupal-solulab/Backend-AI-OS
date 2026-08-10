"""Fixtures loader for the Workflow-06 bind/issuance dataset — mirrors
``endorsement_processing/fixtures.py`` and ``quoting_rating/fixtures.py``'s discipline
(never hardcode fixtures in workflow code) and dataset location convention
(``Data sets/Workflow-06/mga_bind_issuance_dataset``, alongside the repo rather than
under the shared ``TEST_DATA_ROOT``). Each scenario carries either a ``bind_instruction``
(pre-bind) or a ``bind_confirmation`` (post-bind issuance reconciliation, scenario_05).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, cast

log = logging.getLogger(__name__)

_DATASET_DIR = (
    Path(__file__).resolve().parents[4] / "Data sets" / "Workflow-06" / "mga_bind_issuance_dataset"
)


def dataset_dir() -> Path | None:
    return _DATASET_DIR if _DATASET_DIR.is_dir() else None


def load_scenario(name: str) -> dict[str, Any] | None:
    """Load one ``scenario_NN``'s ``bind_instruction.json`` or ``bind_confirmation.json``.
    Returns None if the scenario directory or a recognized file isn't present."""
    d = dataset_dir()
    if d is None:
        log.warning("Bind/issuance dataset not found at %s", _DATASET_DIR)
        return None
    scenario_dir = d / name
    for filename in ("bind_instruction.json", "bind_confirmation.json"):
        path = scenario_dir / filename
        if path.is_file():
            return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    return None


def list_scenarios() -> list[str]:
    d = dataset_dir()
    if d is None:
        return []
    return sorted(
        p.name for p in d.iterdir()
        if p.is_dir() and (
            (p / "bind_instruction.json").is_file() or (p / "bind_confirmation.json").is_file()
        )
    )
