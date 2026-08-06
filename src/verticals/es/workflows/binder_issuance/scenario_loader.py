"""Loads Workflow_<n>'s ``scenario_XX/`` binder/issuance fixtures.

Unlike every prior workflow, the input SHAPE genuinely varies by lifecycle
stage within this same dataset: pre-bind scenarios ship
``broker_bind_instruction.json`` (+ optionally ``carrier_bind_confirmation.txt``,
a raw email); post-issuance scenarios ship ``bind_record.json`` (+ optionally
``issued_policy_document_extract.txt``, a declarations-page dump — NOT an
email, no headers at all). All four files are optional per scenario; the
service infers which lifecycle stage a scenario represents from which JSON
file is present. Doesn't fit ``src/fixtures/loader.py`` (glob mismatch, and
the shared loader has no notion of any of these shapes) — workflow-owned,
same precedent as every prior E&S workflow's loader. See
DATA_AND_FIXTURES.md's Workflow_14 layout note.
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
    broker_bind_instruction: dict[str, Any] | None = None
    carrier_bind_confirmation_text: str | None = None
    bind_record: dict[str, Any] | None = None
    issued_policy_text: str | None = None


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
        broker_bind_instruction=_read_json(scenario_dir / "broker_bind_instruction.json"),
        carrier_bind_confirmation_text=_read_text(scenario_dir / "carrier_bind_confirmation.txt"),
        bind_record=_read_json(scenario_dir / "bind_record.json"),
        issued_policy_text=_read_text(scenario_dir / "issued_policy_document_extract.txt"),
    )
