"""Fixtures loader — turns ``TEST_DATA_ROOT/Workflow_<N>/test_dataset`` into
``Submission`` + ``list[Document]`` for the dev seed script and workflow eval tests.

Rules (DATA_AND_FIXTURES.md):
- Never hardcode fixtures in workflow code — always go through this loader.
- Filenames drive document classification (``acord_application`` → ACORD, etc.).
- ``TEST_DATA_ROOT`` is config-driven; if the path is missing we warn and return ``[]``
  (never crash) so the app/tests run without the dataset present.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from core.common.enums import DocumentKind, Vertical
from core.config import get_settings
from core.models import Document, Submission

log = logging.getLogger(__name__)

# Filename-stem → DocumentKind. Extend as new document types appear in datasets.
_KIND_BY_STEM: dict[str, DocumentKind] = {
    "acord_application": DocumentKind.ACORD,
    "loss_run": DocumentKind.LOSS_RUN,
    "financial_statement": DocumentKind.FINANCIALS,
    "sov_report": DocumentKind.SOV,
    "sov": DocumentKind.SOV,
    "email": DocumentKind.EMAIL,
}


def _classify(filename: str) -> DocumentKind:
    return _KIND_BY_STEM.get(Path(filename).stem.lower(), DocumentKind.OTHER)


@dataclass
class LoadedSubmission:
    """One sample case: a Submission plus its Documents (unpersisted SQLModel rows)."""

    submission: Submission
    documents: list[Document] = field(default_factory=list)


def _dataset_dir(n: int) -> Path | None:
    root = get_settings().test_data_root
    if not root:
        log.warning("TEST_DATA_ROOT is not set; returning no fixtures.")
        return None
    dataset = Path(root) / f"Workflow_{n}" / "test_dataset"
    if not dataset.is_dir():
        log.warning("Fixture dataset not found at %s; returning no fixtures.", dataset)
        return None
    return dataset


def load_workflow(
    n: int,
    *,
    tenant_id: str = "fixture-tenant",
    vertical: Vertical = Vertical.MGA,
) -> list[LoadedSubmission]:
    """Scan ``Workflow_<n>/test_dataset/submission_*`` → list of LoadedSubmission.

    Each ``submission_XX/`` folder becomes one Submission; each ``.txt`` becomes one
    Document with ``kind`` inferred from the filename. Missing path → ``[]`` (warned).
    """
    dataset = _dataset_dir(n)
    if dataset is None:
        return []

    results: list[LoadedSubmission] = []
    for sub_dir in sorted(dataset.glob("submission_*")):
        if not sub_dir.is_dir():
            continue
        submission = Submission(
            tenant_id=tenant_id,
            vertical=vertical,
            external_ref=sub_dir.name,
            subject=f"Workflow_{n} / {sub_dir.name}",
        )
        documents: list[Document] = []
        for doc_path in sorted(sub_dir.glob("*.txt")):
            try:
                content = doc_path.read_text(encoding="utf-8")
            except OSError as exc:  # unreadable file → skip, don't crash the load
                log.warning("Could not read %s: %s", doc_path, exc)
                continue
            documents.append(
                Document(
                    tenant_id=tenant_id,
                    submission_id=submission.id,
                    kind=_classify(doc_path.name),
                    filename=doc_path.name,
                    uri=str(doc_path),
                    content=content,
                )
            )
        results.append(LoadedSubmission(submission=submission, documents=documents))

    log.info("Loaded %d submissions for Workflow_%d.", len(results), n)
    return results


def load_rules(n: int) -> dict[str, str]:
    """Read a workflow's expected-outcome spec.

    Returns ``{"markdown": <text>}`` from ``Validation_Rules_Test_Dataset.md`` and, if
    present, ``{"json": <text>}`` from a ``rules.json``. Missing path/files → ``{}``.
    """
    dataset = _dataset_dir(n)
    if dataset is None:
        return {}

    out: dict[str, str] = {}
    md = dataset / "Validation_Rules_Test_Dataset.md"
    if md.is_file():
        out["markdown"] = md.read_text(encoding="utf-8")
    else:
        log.warning("No Validation_Rules_Test_Dataset.md in %s.", dataset)

    rules_json = dataset / "rules.json"
    if rules_json.is_file():
        out["json"] = rules_json.read_text(encoding="utf-8")

    return out
