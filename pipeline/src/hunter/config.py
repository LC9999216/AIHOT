"""Configuration loading for Free Token Hunter.

All configuration lives in YAML files under ``config/``. Loading is deliberately
strict: malformed YAML, missing files, and non-mapping roots raise typed
``ConfigError`` exceptions instead of silently defaulting.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import yaml

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


class ConfigError(Exception):
    """Raised when a configuration file cannot be loaded or is malformed."""


def _load_yaml_mapping(path: Path, what: str) -> Dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"{what} file missing: {path}")
    try:
        with path.open("r", encoding="utf-8") as fh:
            payload = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ConfigError(f"malformed YAML in {what} file {path}: {exc}") from exc
    if payload is None:
        raise ConfigError(f"{what} file {path} is empty; expected a mapping")
    if not isinstance(payload, dict):
        raise ConfigError(
            f"{what} file {path} must contain a YAML mapping, got {type(payload).__name__}"
        )
    return payload


def load_settings(path: Path = DEFAULT_CONFIG_DIR / "settings.yaml") -> Dict[str, Any]:
    """Load ``settings.yaml`` as a mapping."""
    return _load_yaml_mapping(path, "settings")


def load_sources(path: Path = DEFAULT_CONFIG_DIR / "sources.yaml") -> Dict[str, Any]:
    """Load ``sources.yaml`` as a mapping."""
    return _load_yaml_mapping(path, "sources")


def load_scoring(path: Path = DEFAULT_CONFIG_DIR / "scoring.yaml") -> Dict[str, Any]:
    """Load ``scoring.yaml`` as a mapping."""
    return _load_yaml_mapping(path, "scoring")
