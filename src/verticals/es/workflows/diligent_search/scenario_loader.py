"""Loads Workflow_<n>'s ``scenario_XX/case_context.json`` fixtures.

Same simplest-shape precedent as Renewal Remarketing: every scenario is a
single, already-structured JSON snapshot (no raw emails, no new
extraction target). Still doesn't fit ``src/fixtures/loader.py`` (glob
mismatch), so it's workflow-owned, same precedent as every prior E&S
workflow's loader. See DATA_AND_FIXTURES.md's Workflow_17 layout note.
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
    """Loads one scenario's ``case_context.json``. Raises
    ``FileNotFoundError`` if the dataset or scenario is missing — a caller
    asking for a SPECIFIC scenario wants a loud failure."""
    dataset = _dataset_dir(n)
    if dataset is None:
        raise FileNotFoundError(f"TEST_DATA_ROOT not set or Workflow_{n} dataset missing")
    path = dataset / scenario_ref / "case_context.json"
    if not path.is_file():
        raise FileNotFoundError(f"no case_context.json at {path}")
    result: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return result
