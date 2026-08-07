"""Loads Workflow_<n>'s ``scenario_XX/underlying_data.json`` fixtures.

Same "no new extraction target" precedent as every other already-
structured-JSON E&S workflow this session — each scenario is itself a
pre-aggregated period snapshot, not raw per-submission events. See
DATA_AND_FIXTURES.md's Workflow_19 layout note for why this workflow
doesn't attempt a live cross-workflow DB aggregation.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from core.config import get_settings

log = logging.getLogger(__name__)


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


def load_scenario(n: int, scenario_ref: str) -> dict[str, Any]:
    """Loads one scenario's ``underlying_data.json``. Raises
    ``FileNotFoundError`` if the dataset or scenario is missing — a caller
    asking for a SPECIFIC scenario wants a loud failure."""
    dataset = _dataset_dir(n)
    if dataset is None:
        raise FileNotFoundError(f"TEST_DATA_ROOT not set or Workflow_{n} dataset missing")
    path = dataset / scenario_ref / "underlying_data.json"
    if not path.is_file():
        raise FileNotFoundError(f"no underlying_data.json at {path}")
    result: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return result
