"""Self-update from GitHub ZIP (no git / Xcode required)."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

DEFAULT_REPO = "ssashfaque-creator/Eva-Foods-Dashboard"
DEFAULT_BRANCH = "cursor/sales-dashboard-pdf-8203"

# Never overwrite these when applying an update
PRESERVE_NAMES = {
    "data",
    ".venv",
    "venv",
    ".git",
    ".env",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
}


def update_repo() -> str:
    return os.environ.get("EVA_UPDATE_REPO", DEFAULT_REPO).strip()


def update_branch() -> str:
    return os.environ.get("EVA_UPDATE_BRANCH", DEFAULT_BRANCH).strip()


def zip_url(repo: str | None = None, branch: str | None = None) -> str:
    repo = repo or update_repo()
    branch = branch or update_branch()
    return f"https://github.com/{repo}/archive/refs/heads/{branch}.zip"


def find_install_root(explicit: Path | None = None) -> Path:
    """Locate the project folder that should be updated."""
    if explicit is not None:
        root = explicit.expanduser().resolve()
        if not (root / "pyproject.toml").exists() and not (root / "eva_dashboard").is_dir():
            raise FileNotFoundError(
                f"Not an Eva Foods install folder (missing pyproject.toml): {root}"
            )
        return root

    env = os.environ.get("EVA_HOME")
    if env:
        return find_install_root(Path(env))

    cwd = Path.cwd().resolve()
    if (cwd / "pyproject.toml").exists() or (cwd / "eva_dashboard").is_dir():
        return cwd

    # Editable install: package lives at <root>/eva_dashboard/
    package_parent = Path(__file__).resolve().parent.parent
    if (package_parent / "pyproject.toml").exists():
        return package_parent

    raise FileNotFoundError(
        "Could not find the Eva Foods install folder. "
        "cd into it first, or pass --dir ~/Eva-Foods-Dashboard"
    )


def download_zip(url: str, timeout: int = 120) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "eva-dashboard-updater"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"Download failed ({exc.code}) from {url}. "
            "Check EVA_UPDATE_BRANCH / network access."
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Download failed: {exc.reason}") from exc


def _extract_repo_root(zip_bytes: bytes, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        zf.extractall(dest)
    children = [p for p in dest.iterdir() if p.is_dir()]
    if len(children) != 1:
        raise RuntimeError("Unexpected ZIP layout from GitHub")
    return children[0]


def apply_update(source_root: Path, install_root: Path) -> list[str]:
    """Copy updated project files onto install_root. Returns list of top-level names copied."""
    copied: list[str] = []
    for item in sorted(source_root.iterdir()):
        name = item.name
        if name in PRESERVE_NAMES:
            continue
        if name.startswith(".") and name not in {".gitignore", ".streamlit"}:
            continue
        target = install_root / name
        if item.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)
        copied.append(name)
    return copied


def _pip_executable(install_root: Path) -> list[str]:
    venv_pip = install_root / ".venv" / "bin" / "pip"
    if venv_pip.exists():
        return [str(venv_pip)]
    venv_pip_win = install_root / ".venv" / "Scripts" / "pip.exe"
    if venv_pip_win.exists():
        return [str(venv_pip_win)]
    return [sys.executable, "-m", "pip"]


def reinstall_package(install_root: Path) -> None:
    cmd = [*_pip_executable(install_root), "install", "-e", str(install_root)]
    subprocess.run(cmd, check=True, cwd=str(install_root))


def run_update(
    *,
    install_dir: Path | None = None,
    repo: str | None = None,
    branch: str | None = None,
    reinstall: bool = True,
) -> dict:
    """Download latest ZIP and apply it. Preserves data/ and .venv/."""
    from eva_dashboard import __version__ as old_version

    install_root = find_install_root(install_dir)
    repo = repo or update_repo()
    branch = branch or update_branch()
    url = zip_url(repo, branch)

    zip_bytes = download_zip(url)
    with tempfile.TemporaryDirectory(prefix="eva-update-") as tmp:
        extracted = _extract_repo_root(zip_bytes, Path(tmp) / "zip")
        copied = apply_update(extracted, install_root)

    if reinstall:
        reinstall_package(install_root)

    # Re-read version from updated package if possible
    new_version = old_version
    init_file = install_root / "eva_dashboard" / "__init__.py"
    if init_file.exists():
        text = init_file.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("__version__"):
                new_version = line.split("=", 1)[1].strip().strip("\"'")
                break

    return {
        "install_root": str(install_root),
        "repo": repo,
        "branch": branch,
        "url": url,
        "copied": copied,
        "old_version": old_version,
        "new_version": new_version,
        "data_preserved": (install_root / "data").exists(),
        "venv_preserved": (install_root / ".venv").exists(),
    }
