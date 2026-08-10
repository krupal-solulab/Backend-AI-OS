"""Fixtures loader for the Workflow-04 endorsement dataset — mirrors ``fixtures/loader.py``'s
discipline (never hardcode fixtures in workflow code) but points at this workflow's own
dataset location (``Data sets/Workflow-04/mga_endorsement_dataset``), since it lives
alongside the repo rather than under the shared ``TEST_DATA_ROOT``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, cast

log = logging.getLogger(__name__)

# Repo-relative: src/verticals/mga/endorsement_processing/fixtures.py -> repo root is 4 parents up.
_DATASET_DIR = (
    Path(__file__).resolve().parents[4] / "Data sets" / "Workflow-04" / "mga_endorsement_dataset"
)


def dataset_dir() -> Path | None:
    return _DATASET_DIR if _DATASET_DIR.is_dir() else None


def load_scenario(name: str) -> dict[str, Any] | None:
    """Load one ``scenario_NN``'s ``endorsement_request.json``. None if missing."""
    d = dataset_dir()
    if d is None:
        log.warning("Endorsement dataset not found at %s", _DATASET_DIR)
        return None
    path = d / name / "endorsement_request.json"
    if not path.is_file():
        return None
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def list_scenarios() -> list[str]:
    d = dataset_dir()
    if d is None:
        return []
    return sorted(
        p.name for p in d.iterdir()
        if p.is_dir() and (p / "endorsement_request.json").is_file()
    )
