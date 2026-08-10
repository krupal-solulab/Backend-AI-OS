"""Fixtures loader for the Workflow-07 appetite governance dataset — mirrors every other
MGA workflow's fixtures.py discipline (never hardcode fixtures in workflow code) and
dataset location convention (``Data sets/Workflow-07/appetite_governance_dataset``,
alongside the repo rather than under the shared ``TEST_DATA_ROOT``).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, cast

log = logging.getLogger(__name__)

_DATASET_DIR = (
    Path(__file__).resolve().parents[4] / "Data sets" / "Workflow-07"
    / "appetite_governance_dataset"
)


def dataset_dir() -> Path | None:
    return _DATASET_DIR if _DATASET_DIR.is_dir() else None


def load_scenario(name: str) -> dict[str, Any] | None:
    """Load one ``scenario_NN``'s ``audit_input.json``. None if missing."""
    d = dataset_dir()
    if d is None:
        log.warning("Appetite governance dataset not found at %s", _DATASET_DIR)
        return None
    path = d / name / "audit_input.json"
    if not path.is_file():
        return None
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def list_scenarios() -> list[str]:
    d = dataset_dir()
    if d is None:
        return []
    return sorted(
        p.name for p in d.iterdir()
        if p.is_dir() and (p / "audit_input.json").is_file()
    )
