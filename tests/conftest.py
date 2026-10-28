"""Shared fixtures. Every test works on a private copy of the acme template under tmp_path."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import spendcoder
from spendcoder.pipeline import open_project

ACME_TEMPLATE = Path(spendcoder.__file__).parent / "templates" / "acme"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    dst = tmp_path / "acme"
    shutil.copytree(ACME_TEMPLATE, dst)
    return dst


@pytest.fixture
def ctx(project: Path):
    return open_project(project, backend="mock")
