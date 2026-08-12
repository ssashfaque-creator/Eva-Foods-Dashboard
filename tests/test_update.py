"""Tests for self-update helpers (no network)."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
import pytest

from eva_dashboard.update import (
    DEFAULT_BRANCH,
    LEGACY_PATH_MARKERS,
    MIN_VERSION,
    apply_update,
    assert_launch_path_ok,
    canonical_home,
    find_install_root,
    run_update,
    wrong_install_reason,
    zip_url,
    _extract_repo_root,
    _version_tuple,
)


def test_zip_url_uses_defaults() -> None:
    url = zip_url("owner/repo", "my-branch")
    assert url.endswith("/owner/repo/archive/refs/heads/my-branch.zip")


def test_default_branch_is_phase1() -> None:
    assert "phase1-single-planner" in DEFAULT_BRANCH
    assert "sales-dashboard-pdf" not in DEFAULT_BRANCH


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


def test_canonical_home_prefers_eva_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "custom-home"
    target.mkdir()
    (target / "pyproject.toml").write_text("[project]\nname='eva'\n")
    monkeypatch.setenv("EVA_HOME", str(target))
    assert canonical_home() == target.resolve()


def test_run_update_refuses_legacy_path(tmp_path: Path) -> None:
    legacy = tmp_path / "Eva-Foods-Dashboard-cursor-sales-dashboard-pdf-8203"
    legacy.mkdir()
    (legacy / "pyproject.toml").write_text("[project]\nname='eva'\n")
    assert any(m in str(legacy).lower() for m in LEGACY_PATH_MARKERS)
    with pytest.raises(RuntimeError, match="Refusing to update legacy"):
        run_update(install_dir=legacy, reinstall=False, bootstrap=False)


def test_assert_launch_path_ok_blocks_legacy(tmp_path: Path) -> None:
    legacy = tmp_path / "x-sales-dashboard-pdf-y" / "eva_dashboard"
    legacy.mkdir(parents=True)
    app = legacy / "app.py"
    app.write_text("#")
    with pytest.raises(RuntimeError, match="OLD install"):
        assert_launch_path_ok(app, "1.2.2")


def test_assert_launch_path_ok_blocks_old_version(tmp_path: Path) -> None:
    root = tmp_path / "Eva-Foods-Dashboard-new" / "eva_dashboard"
    root.mkdir(parents=True)
    app = root / "app.py"
    app.write_text("#")
    with pytest.raises(RuntimeError, match="too old"):
        assert_launch_path_ok(app, "1.0.0")


def test_assert_launch_path_ok_accepts_current(tmp_path: Path) -> None:
    root = tmp_path / "Eva-Foods-Dashboard-new" / "eva_dashboard"
    root.mkdir(parents=True)
    app = root / "app.py"
    app.write_text("#")
    assert_launch_path_ok(app, MIN_VERSION)
    assert wrong_install_reason(app, MIN_VERSION) is None
    assert wrong_install_reason(app, "1.3.0") is None
    assert wrong_install_reason(app, "9.9.9") is None


def test_wrong_install_reason_flags_old_and_legacy(tmp_path: Path) -> None:
    good = tmp_path / "Eva-Foods-Dashboard-new" / "eva_dashboard"
    good.mkdir(parents=True)
    good_app = good / "app.py"
    good_app.write_text("#")
    assert wrong_install_reason(good_app, "1.0.0") is not None

    legacy = tmp_path / "sales-dashboard-pdf-8203" / "eva_dashboard"
    legacy.mkdir(parents=True)
    legacy_app = legacy / "app.py"
    legacy_app.write_text("#")
    assert wrong_install_reason(legacy_app, "1.3.0") is not None


def test_version_tuple_orders() -> None:
    assert _version_tuple("1.2.2") > _version_tuple("1.2.1")
    assert _version_tuple("1.2.2") > _version_tuple("1.0.0")
    assert _version_tuple(MIN_VERSION) >= _version_tuple("1.2.2")


def test_run_update_writes_branch_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install = tmp_path / "Eva-Foods-Dashboard-new"
    install.mkdir()
    (install / "pyproject.toml").write_text("[project]\nname='eva'\n")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "Eva-Foods-Dashboard-branch/eva_dashboard/__init__.py",
            f'__version__ = "{MIN_VERSION}"\n',
        )
        zf.writestr("Eva-Foods-Dashboard-branch/pyproject.toml", "version='x'\n")

    monkeypatch.setattr(
        "eva_dashboard.update.download_zip",
        lambda url, timeout=180: buf.getvalue(),
    )
    result = run_update(install_dir=install, reinstall=False, branch=DEFAULT_BRANCH)
    assert result["new_version"] == MIN_VERSION
    assert (install / ".eva-install-branch").read_text().strip() == DEFAULT_BRANCH
    assert (install / "eva_dashboard" / "__init__.py").exists()
