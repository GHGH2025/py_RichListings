from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def data_path(filename: str) -> Path:
    return PROJECT_ROOT / "data" / filename


def resolve_project_path(path: str) -> Path:
    p = Path(path)
    if p.is_absolute():
        return p
    return PROJECT_ROOT / p


def accounts_dir(name: str) -> Path:
    return PROJECT_ROOT / "accounts" / name


def accounts_path(*parts: str) -> Path:
    return PROJECT_ROOT.joinpath("accounts", *parts)
