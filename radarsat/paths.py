"""Resolve Radar-Sat runtime storage paths."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


PROJECT_NAME = "radar-sat"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MACHINE_CONFIG = Path("~/.config/project-data.env").expanduser()


def _expanded_path(value: str) -> Path:
    return Path(os.path.expandvars(value)).expanduser().resolve(strict=False)


def _runtime_environment() -> dict[str, str]:
    env = dict(os.environ)
    if env.get("PROJECT_DATA_ROOT"):
        return env
    config_path = _expanded_path(
        env.get("PROJECT_DATA_CONFIG", str(DEFAULT_MACHINE_CONFIG))
    )
    try:
        lines = config_path.read_text().splitlines()
    except FileNotFoundError:
        return env
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == "PROJECT_DATA_ROOT":
            env[name.strip()] = value.strip().strip("\"'")
            break
    return env


def data_root(
    environ: Mapping[str, str] | None = None,
    project_root: Path = PROJECT_ROOT,
) -> Path:
    env = _runtime_environment() if environ is None else environ
    override = env.get("RADARSAT_DATA_ROOT", "").strip()
    if override:
        path = _expanded_path(override)
        source = "RADARSAT_DATA_ROOT"
    else:
        shared = env.get("PROJECT_DATA_ROOT", "").strip()
        if not shared:
            return project_root / "data"
        shared_root = _expanded_path(shared)
        if not shared_root.is_dir():
            raise RuntimeError(
                f"PROJECT_DATA_ROOT is configured but unavailable: {shared_root}. "
                "Mount the data volume or correct the machine-level setting."
            )
        path = shared_root / PROJECT_NAME / "data"
        source = "PROJECT_DATA_ROOT"
    if not path.is_dir():
        raise RuntimeError(
            f"{source} resolved to an unavailable Radar-Sat directory: {path}. "
            "Mount the data volume or create the configured project directory first."
        )
    return path


def output_root(
    environ: Mapping[str, str] | None = None,
    project_root: Path = PROJECT_ROOT,
) -> Path:
    env = _runtime_environment() if environ is None else environ
    override = env.get("RADARSAT_OUTPUT_ROOT", "").strip()
    if override:
        path = _expanded_path(override)
        if not path.is_dir():
            raise RuntimeError(
                f"RADARSAT_OUTPUT_ROOT is configured but unavailable: {path}."
            )
        return path
    return data_root(env, project_root) / "output"


def sibling_project_path(
    project_name: str,
    *parts: str,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Resolve another project's runtime path through the same shared root."""

    env = _runtime_environment() if environ is None else environ
    shared = env.get("PROJECT_DATA_ROOT", "").strip()
    if shared:
        shared_root = _expanded_path(shared)
        if not shared_root.is_dir():
            raise RuntimeError(
                f"PROJECT_DATA_ROOT is configured but unavailable: {shared_root}."
            )
        return shared_root.joinpath(project_name, *parts)
    return Path.home().joinpath("projects", project_name, *parts)
