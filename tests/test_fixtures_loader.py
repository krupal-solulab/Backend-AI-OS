"""Fixtures loader tests — deterministic (build a temp dataset; no external drive)."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.common.enums import DocumentKind, Vertical
from core.config import Settings
from fixtures import loader


def _write_dataset(root: Path) -> None:
    ds = root / "Workflow_1" / "test_dataset"
    sub = ds / "submission_01"
    sub.mkdir(parents=True)
    (sub / "acord_application.txt").write_text("acord", encoding="utf-8")
    (sub / "email.txt").write_text("hello broker", encoding="utf-8")
    (sub / "loss_run.txt").write_text("losses", encoding="utf-8")
    (sub / "sov_report.txt").write_text("schedule of values", encoding="utf-8")
    (ds / "Validation_Rules_Test_Dataset.md").write_text("# Rules\n- required", encoding="utf-8")


@pytest.fixture
def point_loader_at(monkeypatch: pytest.MonkeyPatch):
    def _apply(root: str) -> None:
        monkeypatch.setattr(loader, "get_settings", lambda: Settings(test_data_root=root))

    return _apply


def test_load_workflow_parses_submissions_and_kinds(tmp_path, point_loader_at) -> None:
    _write_dataset(tmp_path)
    point_loader_at(str(tmp_path))

    loaded = loader.load_workflow(1, tenant_id="t1", vertical=Vertical.MGA)

    assert len(loaded) == 1
    ls = loaded[0]
    assert ls.submission.external_ref == "submission_01"
    assert ls.submission.vertical == Vertical.MGA
    assert len(ls.documents) == 4

    kinds = {d.filename: d.kind for d in ls.documents}
    assert kinds["acord_application.txt"] == DocumentKind.ACORD
    assert kinds["email.txt"] == DocumentKind.EMAIL
    assert kinds["loss_run.txt"] == DocumentKind.LOSS_RUN
    assert kinds["sov_report.txt"] == DocumentKind.SOV
    # documents share their parent submission id
    assert {d.submission_id for d in ls.documents} == {ls.submission.id}


def test_load_rules_returns_markdown(tmp_path, point_loader_at) -> None:
    _write_dataset(tmp_path)
    point_loader_at(str(tmp_path))

    rules = loader.load_rules(1)
    assert "markdown" in rules
    assert "Rules" in rules["markdown"]


def test_missing_path_returns_empty_without_crashing(tmp_path, point_loader_at) -> None:
    point_loader_at(str(tmp_path / "does_not_exist"))

    assert loader.load_workflow(1) == []
    assert loader.load_rules(1) == {}


def test_unset_test_data_root_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(loader, "get_settings", lambda: Settings(test_data_root=""))
    assert loader.load_workflow(1) == []
    assert loader.load_rules(1) == {}
