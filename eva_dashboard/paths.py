"""Application paths and constants for Eva Dashboard storage."""

from __future__ import annotations

import os
from pathlib import Path


def data_root() -> Path:
    """Root folder for DB, uploads, and exports.

    Override with EVA_DATA_DIR. Defaults to ./data under the working directory.
    """
    env = os.environ.get("EVA_DATA_DIR")
    if env:
        root = Path(env).expanduser().resolve()
    else:
        root = (Path.cwd() / "data").resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def db_path() -> Path:
    return data_root() / "eva.db"


def uploads_dir(file_type: str) -> Path:
    path = data_root() / "uploads" / file_type
    path.mkdir(parents=True, exist_ok=True)
    return path


FILE_TYPES = (
    "sales",
    "categories",
    "clients",
    "product_costs",
    "packing_costs",
)
