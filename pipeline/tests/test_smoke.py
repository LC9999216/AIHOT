"""TASK-000 smoke tests: package, CLI, configuration, and initial data files.

These tests are the acceptance evidence for the repository skeleton. They must run
fully offline and without any credentials.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


# --- package import ---------------------------------------------------------


def test_package_imports() -> None:
    import hunter

    assert isinstance(hunter.__version__, str)
    assert hunter.__version__


def test_config_module_imports() -> None:
    from hunter import config

    assert config is not None


def test_cli_module_imports() -> None:
    from hunter import cli

    assert callable(cli.main)


# --- CLI --------------------------------------------------------------------


def test_cli_help_succeeds() -> None:
    from hunter.cli import build_parser

    parser = build_parser()
    help_text = parser.format_help()
    assert "hunter" in help_text
    assert "version" in help_text


def test_cli_version_command(capsys: pytest.CaptureFixture[str]) -> None:
    from hunter.cli import main

    assert main(["version"]) == 0
    out = capsys.readouterr().out
    assert out.strip()


def test_cli_console_script_runs_help() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "hunter", "--help"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, result.stderr
    assert "usage" in result.stdout.lower()


def test_cli_rejects_unknown_command() -> None:
    from hunter.cli import main

    with pytest.raises(SystemExit):
        main(["definitely-not-a-command"])


# --- configuration ----------------------------------------------------------


def test_all_three_yaml_files_load() -> None:
    from hunter.config import load_settings, load_sources, load_scoring

    settings = load_settings(REPO_ROOT / "config" / "settings.yaml")
    sources = load_sources(REPO_ROOT / "config" / "sources.yaml")
    scoring = load_scoring(REPO_ROOT / "config" / "scoring.yaml")

    assert isinstance(settings, dict)
    assert isinstance(sources, dict)
    assert scoring["score_version"] == "v1"


def test_config_rejects_malformed_yaml(tmp_path: Path) -> None:
    from hunter.config import ConfigError, load_settings

    bad = tmp_path / "settings.yaml"
    bad.write_text("a: [1, 2\nb: :\n", encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        load_settings(bad)
    assert "settings.yaml" in str(excinfo.value)


def test_config_rejects_missing_file(tmp_path: Path) -> None:
    from hunter.config import ConfigError, load_settings

    with pytest.raises(ConfigError):
        load_settings(tmp_path / "absent.yaml")


def test_config_rejects_non_mapping_root(tmp_path: Path) -> None:
    from hunter.config import ConfigError, load_sources

    bad = tmp_path / "sources.yaml"
    bad.write_text("- just\n- a list\n", encoding="utf-8")

    with pytest.raises(ConfigError):
        load_sources(bad)


# --- initial data files -----------------------------------------------------


@pytest.mark.parametrize(
    "name", ["providers.json", "candidates.json", "evidence.json"]
)
def test_initial_json_collections_are_valid(name: str) -> None:
    path = REPO_ROOT / "data" / name
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    # Valid snapshot collection: "items" is a list (empty at skeleton time,
    # populated after import-seed / discovery runs).
    assert isinstance(payload["items"], list)


def test_initial_history_jsonl_is_valid() -> None:
    path = REPO_ROOT / "data" / "history.jsonl"
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.strip():
            json.loads(line)


# --- gitignore --------------------------------------------------------------


def test_gitignore_covers_env_caches_and_journal() -> None:
    text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    entries = {line.strip() for line in text.splitlines()}

    assert ".env" in entries
    assert "data/.registry_txn.json" in entries
    assert "__pycache__/" in entries
    assert "*.egg-info/" in entries


def test_env_example_has_names_only() -> None:
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assignments = [
        line for line in text.splitlines() if line.strip() and not line.startswith("#")
    ]
    assert assignments, "expected at least one variable name"
    for line in assignments:
        assert "=" in line, line
        name, _, value = line.partition("=")
        assert name.strip()
        assert value.strip() == "", f"{name} must not carry a value"
