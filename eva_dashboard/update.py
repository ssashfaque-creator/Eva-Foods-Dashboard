"""Self-update from GitHub ZIP (no git / Xcode required).

Always pulls ``DEFAULT_BRANCH`` into a canonical install folder so a stale
``eva-dashboard`` on PATH (old sales-dashboard-pdf install) cannot keep
re-downloading the wrong branch into the wrong directory.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

DEFAULT_REPO = "ssashfaque-creator/Eva-Foods-Dashboard"
DEFAULT_BRANCH = "main"
# Refuse to treat these as the live install (legacy agent folders).
LEGACY_PATH_MARKERS = (
    "sales-dashboard-pdf",
    "ai-chatbot-data-testing",
)
# Back-compat alias used by older call sites / tests
_LEGACY_PATH_MARKERS = LEGACY_PATH_MARKERS
CANONICAL_DIRNAMES = (
    "Eva-Foods-Dashboard-new",
    "Eva-Foods-Dashboard",
)
MIN_VERSION = "1.4.3"
BRANCH_MARKER = ".eva-install-branch"

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
    # Explicit env override only — never silently fall back to an old branch.
    return os.environ.get("EVA_UPDATE_BRANCH", DEFAULT_BRANCH).strip() or DEFAULT_BRANCH


def zip_url(repo: str | None = None, branch: str | None = None) -> str:
    repo = repo or update_repo()
    branch = branch or update_branch()
    return f"https://github.com/{repo}/archive/refs/heads/{branch}.zip"


def _is_legacy_path(path: Path) -> bool:
    text = str(path).lower().replace("\\", "/")
    return any(m in text for m in _LEGACY_PATH_MARKERS)


def _looks_like_install(root: Path) -> bool:
    return (root / "pyproject.toml").exists() or (root / "eva_dashboard").is_dir()


def canonical_home() -> Path:
    """Preferred install directory on this machine."""
    env = os.environ.get("EVA_HOME")
    if env:
        return Path(env).expanduser().resolve()
    home = Path.home()
    for name in CANONICAL_DIRNAMES:
        candidate = home / name
        if _looks_like_install(candidate):
            return candidate.resolve()
    # Default bootstrap location
    return (home / "Eva-Foods-Dashboard-new").resolve()


def find_install_root(explicit: Path | None = None) -> Path:
    """Locate the project folder that should be updated / launched."""
    if explicit is not None:
        root = explicit.expanduser().resolve()
        if not _looks_like_install(root):
            raise FileNotFoundError(
                f"Not an Eva Foods install folder (missing pyproject.toml): {root}"
            )
        return root

    env = os.environ.get("EVA_HOME")
    if env:
        return find_install_root(Path(env))

    cwd = Path.cwd().resolve()
    if _looks_like_install(cwd) and not _is_legacy_path(cwd):
        return cwd

    # Prefer canonical home over whatever stale package is on PATH
    home = canonical_home()
    if _looks_like_install(home):
        return home

    # Editable install of *this* package — only if not a legacy folder
    package_parent = Path(__file__).resolve().parent.parent
    if _looks_like_install(package_parent) and not _is_legacy_path(package_parent):
        return package_parent

    if _looks_like_install(cwd):
        # Last resort: cwd even if legacy (user explicitly standing in it)
        return cwd

    raise FileNotFoundError(
        "Could not find the Eva Foods install folder. "
        "Run: eva-dashboard update   # bootstraps ~/Eva-Foods-Dashboard-new\n"
        "Or:  eva-dashboard update --dir ~/Eva-Foods-Dashboard-new"
    )


def download_zip(url: str, timeout: int = 180) -> bytes:
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
    install_root.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for item in sorted(source_root.iterdir()):
        name = item.name
        if name in PRESERVE_NAMES:
            continue
        if name.startswith(".") and name not in {".gitignore", ".streamlit", BRANCH_MARKER}:
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


def _python_executable(install_root: Path) -> str:
    venv_py = install_root / ".venv" / "bin" / "python"
    if venv_py.exists():
        return str(venv_py)
    venv_py_win = install_root / ".venv" / "Scripts" / "python.exe"
    if venv_py_win.exists():
        return str(venv_py_win)
    return sys.executable


def ensure_venv(install_root: Path) -> None:
    venv = install_root / ".venv"
    if venv.exists():
        return
    subprocess.run(
        [sys.executable, "-m", "venv", str(venv)],
        check=True,
        cwd=str(install_root),
    )


def reinstall_package(install_root: Path) -> None:
    ensure_venv(install_root)
    pip = _pip_executable(install_root)
    venv = install_root / ".venv"
    if venv.exists():
        for pattern in (
            "**/site-packages/eva_dashboard",
            "**/site-packages/eva_dashboard-*.dist-info",
            "**/site-packages/eva_dashboard*.egg-link",
            "**/site-packages/__editable__.eva_dashboard*",
            "**/site-packages/__editable___eva_dashboard*",
            "**/eva_dashboard*.egg-info",
        ):
            for path in venv.glob(pattern):
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                elif path.exists():
                    path.unlink(missing_ok=True)
        for cache in (install_root / "eva_dashboard").rglob("__pycache__"):
            shutil.rmtree(cache, ignore_errors=True)

    subprocess.run(
        [*pip, "install", "-e", str(install_root), "--force-reinstall", "--no-deps"],
        check=True,
        cwd=str(install_root),
    )
    subprocess.run(
        [*pip, "install", "-e", str(install_root)],
        check=True,
        cwd=str(install_root),
    )


def read_installed_version(install_root: Path) -> str:
    init_file = install_root / "eva_dashboard" / "__init__.py"
    if init_file.exists():
        text = init_file.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("__version__"):
                return line.split("=", 1)[1].strip().strip("\"'")
    # Probe the venv import
    py = _python_executable(install_root)
    try:
        out = subprocess.check_output(
            [py, "-c", "import eva_dashboard; print(eva_dashboard.__version__)"],
            cwd=str(install_root),
            text=True,
        ).strip()
        return out
    except Exception:  # noqa: BLE001
        return "unknown"


def _version_tuple(version: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", version or "")
    return tuple(int(p) for p in parts[:3]) if parts else (0,)


def run_update(
    *,
    install_dir: Path | None = None,
    repo: str | None = None,
    branch: str | None = None,
    reinstall: bool = True,
    bootstrap: bool = True,
) -> dict:
    """Download DEFAULT_BRANCH ZIP and apply it. Preserves data/ and .venv/.

    If ``install_dir`` is omitted, uses the canonical home
    (``~/Eva-Foods-Dashboard-new``), creating it when missing — never the
    legacy ``*-sales-dashboard-pdf-*`` folder on PATH.
    """
    from eva_dashboard import __version__ as running_version

    repo = repo or update_repo()
    # Force known-good branch unless caller passed an explicit override
    branch = (branch or update_branch()).strip() or DEFAULT_BRANCH

    if install_dir is not None:
        install_root = install_dir.expanduser().resolve()
    else:
        # Prefer canonical home; migrate away from legacy PATH installs
        running_root = Path(__file__).resolve().parent.parent
        if _is_legacy_path(running_root) or not _looks_like_install(canonical_home()):
            install_root = canonical_home()
        else:
            try:
                install_root = find_install_root(None)
                if _is_legacy_path(install_root):
                    install_root = canonical_home()
            except FileNotFoundError:
                install_root = canonical_home()

    if bootstrap:
        install_root.mkdir(parents=True, exist_ok=True)

    if _is_legacy_path(install_root):
        raise RuntimeError(
            f"Refusing to update legacy folder:\n  {install_root}\n"
            f"Use: eva-dashboard update --dir ~/{CANONICAL_DIRNAMES[0]}"
        )

    url = zip_url(repo, branch)
    zip_bytes = download_zip(url)
    with tempfile.TemporaryDirectory(prefix="eva-update-") as tmp:
        extracted = _extract_repo_root(zip_bytes, Path(tmp) / "zip")
        # Sanity: ZIP must be the phase1 (or requested) tree
        marker_init = extracted / "eva_dashboard" / "__init__.py"
        if not marker_init.exists():
            raise RuntimeError(f"ZIP from {url} is missing eva_dashboard/")
        copied = apply_update(extracted, install_root)

    # Stamp which branch this install came from
    (install_root / BRANCH_MARKER).write_text(branch + "\n", encoding="utf-8")

    if reinstall:
        reinstall_package(install_root)

    new_version = read_installed_version(install_root)
    if _version_tuple(new_version) < _version_tuple(MIN_VERSION):
        raise RuntimeError(
            f"Update landed version {new_version} but need >={MIN_VERSION}. "
            f"Downloaded from {url}. Delete the folder and retry:\n"
            f"  rm -rf \"{install_root}\" && eva-dashboard update --dir \"{install_root}\""
        )

    py = _python_executable(install_root)
    return {
        "install_root": str(install_root),
        "repo": repo,
        "branch": branch,
        "url": url,
        "copied": copied,
        "old_version": running_version,
        "new_version": new_version,
        "python": py,
        "data_preserved": (install_root / "data").exists(),
        "venv_preserved": (install_root / ".venv").exists(),
        "legacy_blocked": True,
    }


def wrong_install_reason(app_file: Path, version: str) -> str | None:
    """Return a short reason string if this install should not be used, else None."""
    root = app_file.resolve().parent.parent
    if _is_legacy_path(root):
        return (
            f"legacy path ({root}) — version on this process: {version}"
        )
    if _version_tuple(version) < _version_tuple(MIN_VERSION):
        return f"v{version} is too old (need >={MIN_VERSION})"
    return None


def assert_launch_path_ok(app_file: Path, version: str) -> None:
    """Raise RuntimeError if this process is the stale legacy install."""
    root = app_file.resolve().parent.parent
    home = canonical_home()
    if _is_legacy_path(root):
        raise RuntimeError(
            "You are launching the OLD install:\n"
            f"  {root}\n"
            f"Version on this process: {version}\n\n"
            "Fix (copy-paste):\n"
            f'  eva-dashboard update --dir "{home}"\n'
            f'  "{home}/.venv/bin/eva-dashboard" app '
            "--data-dir ~/Documents/EvaFoodsData\n"
        )
    if _version_tuple(version) < _version_tuple(MIN_VERSION):
        raise RuntimeError(
            f"Eva Foods v{version} is too old (need >={MIN_VERSION}).\n"
            f'  eva-dashboard update --dir "{home}"\n'
            f'  "{home}/.venv/bin/eva-dashboard" app '
            "--data-dir ~/Documents/EvaFoodsData\n"
        )
