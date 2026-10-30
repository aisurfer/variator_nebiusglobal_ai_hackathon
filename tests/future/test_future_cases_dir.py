"""The case configs come from $VARIATOR_CASES_DIR when it is set (an image), else from the source checkout."""

from __future__ import annotations

import json

import pytest

from variator.future import forks


def test_default_is_the_source_checkout(monkeypatch):
    monkeypatch.delenv(forks.CASES_ENV, raising=False)
    assert forks.cases_dir() == forks.CASES_DIR


def test_env_wins(monkeypatch, tmp_path):
    (tmp_path / "company_best_year.json").write_text(
        json.dumps({"id": "from-env", "variables": []}), encoding="utf-8"
    )
    monkeypatch.setenv(forks.CASES_ENV, str(tmp_path))
    assert forks.cases_dir() == tmp_path
    assert forks.case_config("company_best_year")["id"] == "from-env"


def test_missing_config_in_env_dir_fails(monkeypatch, tmp_path):
    monkeypatch.setenv(forks.CASES_ENV, str(tmp_path))
    with pytest.raises(FileNotFoundError):
        forks.case_config("delivery_best_way")
