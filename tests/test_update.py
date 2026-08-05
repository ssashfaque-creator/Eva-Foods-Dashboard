"""Tests for self-update helpers (no network)."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from eva_dashboard.update import (
    apply_update,
    find_install_root,
    zip_url,
    _extract_repo_root,
)


def test_zip_url_uses_defaults() -> None:
    url = zip_url("owner/repo", "my-branch")
    assert url.endswith("/owner/repo/archive/refs/heads/my-branch.zip")


def test_find_install_root_explicit(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='eva-dashboard'\n")
    assert find_install_root(tmp_path) == tmp_path.resolve()


def test_find_install_root_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        find_install_root(tmp_path / "nope")


def test_apply_update_preserves_data_and_venv(tmp_path: Path) -> None:
    install = tmp_path / "install"
    source = tmp_path / "source"
    install.mkdir()
    source.mkdir()

    (install / "data").mkdir()
    (install / "data" / "eva.db").write_text("keep-me")
    (install / ".venv").mkdir()
    (install / ".venv" / "marker").write_text("venv")
    (install / "eva_dashboard").mkdir()
    (install / "eva_dashboard" / "old.py").write_text("old")
    (install / "pyproject.toml").write_text("old")

    (source / "eva_dashboard").mkdir()
    (source / "eva_dashboard" / "new.py").write_text("new")
    (source / "pyproject.toml").write_text("new")
    (source / "README.md").write_text("docs")
    (source / "data").mkdir()
    (source / "data" / "should-not-copy").write_text("nope")

    copied = apply_update(source, install)
    assert "eva_dashboard" in copied
    assert "pyproject.toml" in copied
    assert "data" not in copied
    assert (install / "data" / "eva.db").read_text() == "keep-me"
    assert not (install / "data" / "should-not-copy").exists()
    assert (install / ".venv" / "marker").read_text() == "venv"
    assert (install / "eva_dashboard" / "new.py").read_text() == "new"
    assert not (install / "eva_dashboard" / "old.py").exists()


def test_extract_repo_root(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Eva-Foods-Dashboard-branch/pyproject.toml", "x")
        zf.writestr("Eva-Foods-Dashboard-branch/eva_dashboard/__init__.py", "__version__='1'\n")
    root = _extract_repo_root(buf.getvalue(), tmp_path / "out")
    assert root.name == "Eva-Foods-Dashboard-branch"
    assert (root / "pyproject.toml").exists()
