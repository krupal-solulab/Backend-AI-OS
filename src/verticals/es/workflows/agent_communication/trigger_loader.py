"""Loads Workflow_<n>'s ``trigger_XX/trigger_input.json`` fixtures.

Same precedent as package_assembly's ``scenario_loader.py``: this dataset shape
(``trigger_XX/`` folders holding a single JSON object, not ``submission_*/*.txt``)
doesn't match the shared fixtures loader's glob, so it's workflow-owned. See
DATA_AND_FIXTURES.md's Workflow_12 layout note. Used by the eval suite only —
the live API's ``POST /run`` accepts a trigger object directly in the request
body (PRD FR-2's manually-logged path), it doesn't read fixtures itself.
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
class TriggerInput:
    """One trigger's raw ``trigger_input.json``, unparsed beyond JSON decoding."""

    trigger_ref: str
    data: dict[str, Any]


def _dataset_dir(n: int) -> Path | None:
    root = get_settings().test_data_root
    if not root:
        log.warning("TEST_DATA_ROOT is not set; returning no triggers.")
        return None
    dataset = Path(root) / f"Workflow_{n}" / "test_dataset"
    if not dataset.is_dir():
        log.warning("Fixture dataset not found at %s; returning no triggers.", dataset)
        return None
    return dataset


def list_trigger_refs(n: int) -> list[str]:
    """All ``trigger_*`` folder names for ``Workflow_<n>``, sorted."""
    dataset = _dataset_dir(n)
    if dataset is None:
        return []
    return sorted(p.name for p in dataset.glob("trigger_*") if p.is_dir())


def load_trigger(n: int, trigger_ref: str) -> TriggerInput:
    """Loads one trigger's ``trigger_input.json``. Raises ``FileNotFoundError`` if
    the dataset or trigger is missing — a caller asking for a SPECIFIC trigger by
    name wants a loud failure, not a silently empty result."""
    dataset = _dataset_dir(n)
    if dataset is None:
        raise FileNotFoundError(f"TEST_DATA_ROOT not set or Workflow_{n} dataset missing")
    path = dataset / trigger_ref / "trigger_input.json"
    if not path.is_file():
        raise FileNotFoundError(f"no trigger_input.json at {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return TriggerInput(trigger_ref=trigger_ref, data=data)
