"""Client templates from an explicit, sanitized live FreeLLMPool catalog.

No runtime-only or provider-ID-only fallback. Pure generators accept explicit
catalog fixtures for offline tests; the CLI always obtains authenticated readback.
All auth fields reference the single public proxy key's NAME, never its value.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit

import yaml

from hunter.runtime.models import ActualPoolStatus, RuntimeProvider

PRODUCTION_PROXY_BASE = "http://127.0.0.1:8080/v1"
PROXY_AUTH_ENV = "FREELLMPOOL_PROXY_KEY"
REQUIRED_FREELLMPOOL_VERSION = "0.13.0"
CODEX_REQUIRED_FEATURES = ("responses", "streaming", "tools")
OPENCODE_REQUIRED_FEATURES = ("chat",)
AGENT_REQUIRED_FEATURES = ("chat",)
_SECRET_SCAN = (
    re.compile(r"sk-[a-z0-9_\-]{16,}", re.IGNORECASE),
    re.compile(r"(?i)bearer\s+[a-z0-9._\-]{8,}"),
    re.compile(r"(?i)(api[_-]?key|authorization)\s*[:=]\s*\S{6,}"),
)


class ClientConfigError(Exception):
    """A sanitized config/readback validation error."""


def _assert_no_secrets(text: str, client: str) -> None:
    if any(pattern.search(text) for pattern in _SECRET_SCAN):
        raise ClientConfigError(f"{client}_credential_like_content")


def validate_production_catalog(payload: Any) -> dict[str, Any]:
    """Allowlist the entire readback schema, returning a detached canonical copy."""
    if not isinstance(payload, dict) or set(payload) != {
        "freellmpool_version", "proxy_base_url", "proxy_auth_env", "providers"
    }:
        raise ClientConfigError("invalid_production_catalog")
    if payload["freellmpool_version"] != REQUIRED_FREELLMPOOL_VERSION:
        raise ClientConfigError("freellmpool_version_unsupported")
    if payload["proxy_auth_env"] != PROXY_AUTH_ENV:
        raise ClientConfigError("invalid_proxy_auth_env")
    base = payload["proxy_base_url"]
    try:
        url = urlsplit(base) if isinstance(base, str) else None
        valid_url = (url is not None and url.scheme == "http" and url.hostname == "127.0.0.1"
                     and url.port is not None and 1 <= url.port <= 65535
                     and url.username is None and url.password is None
                     and url.path == "/v1" and not url.query and not url.fragment)
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ClientConfigError("invalid_proxy_base_url")
    rows = payload["providers"]
    if not isinstance(rows, list):
        raise ClientConfigError("invalid_production_catalog")
    clean = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"provider_id", "model_ids"}:
            raise ClientConfigError("invalid_production_catalog")
        pid, models = row["provider_id"], row["model_ids"]
        if (not isinstance(pid, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", pid)
                or pid in seen or not isinstance(models, list)):
            raise ClientConfigError("invalid_production_catalog")
        seen.add(pid)
        for model in models:
            if (not isinstance(model, str) or not model.startswith(pid + "/")
                    or len(model) <= len(pid) + 1 or len(model) > 512
                    or any(ord(ch) < 33 or ord(ch) == 127 for ch in model)):
                raise ClientConfigError("invalid_production_model")
            _assert_no_secrets(model, "catalog")
        if len(models) != len(set(models)):
            raise ClientConfigError("invalid_production_model")
        _assert_no_secrets(pid, "catalog")
        clean.append({"provider_id": pid, "model_ids": sorted(models)})
    return {"freellmpool_version": REQUIRED_FREELLMPOOL_VERSION,
            "proxy_base_url": base, "proxy_auth_env": PROXY_AUTH_ENV,
            "providers": sorted(clean, key=lambda row: row["provider_id"])}


def _features_passing(rp: RuntimeProvider) -> dict[str, bool]:
    return {name: getattr(rp.protocol_result, name) == "pass"
            for name in ("chat", "responses", "streaming", "tools")}


def _models(runtime: Sequence[RuntimeProvider], catalog: dict[str, Any],
            required: Sequence[str]) -> list[str]:
    admitted = {rp.provider_id for rp in runtime
                if rp.actual_pool_status == ActualPoolStatus.PRODUCTION
                and all(_features_passing(rp)[name] for name in required)}
    return sorted(model for row in catalog["providers"] if row["provider_id"] in admitted
                  for model in row["model_ids"])


def _toml_quote(value: str) -> str:
    # JSON strings are TOML basic strings for these validated/controlled values.
    return json.dumps(value, ensure_ascii=True)


def generate_codex_config(runtime_providers: Sequence[RuntimeProvider],
                          production_catalog: Any = None) -> str:
    catalog = validate_production_catalog(production_catalog)
    models = _models(runtime_providers, catalog, CODEX_REQUIRED_FEATURES)
    lines = ["# Hunter managed FreeLLMPool configuration; auth references a NAME only."]
    if models:
        lines += [f"model = {_toml_quote(models[0])}", 'model_provider = "freellmpool"',
                  "", "[model_providers.freellmpool]", 'name = "FreeLLMPool"',
                  f"base_url = {_toml_quote(catalog['proxy_base_url'])}",
                  'wire_api = "responses"', f"env_key = {_toml_quote(PROXY_AUTH_ENV)}"]
        for model in models:
            lines += ["", f"[profiles.{_toml_quote(model)}]",
                      f"model = {_toml_quote(model)}", 'model_provider = "freellmpool"']
    text = "\n".join(lines) + "\n"
    tomllib.loads(text)
    _assert_no_secrets(text, "codex")
    return text


def generate_opencode_config(runtime_providers: Sequence[RuntimeProvider],
                             production_catalog: Any = None) -> str:
    catalog = validate_production_catalog(production_catalog)
    models = _models(runtime_providers, catalog, OPENCODE_REQUIRED_FEATURES)
    payload: dict[str, Any] = {"$schema": "https://opencode.ai/config.json", "provider": {}}
    if models:
        payload["model"] = f"freellmpool/{models[0]}"
        payload["provider"]["freellmpool"] = {
            "name": "FreeLLMPool", "npm": "@ai-sdk/openai-compatible",
            "options": {"baseURL": catalog["proxy_base_url"], "apiKey": "{env:" + PROXY_AUTH_ENV + "}"},
            "models": {model: {"name": model} for model in models},
        }
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    assert json.loads(text) == payload
    _assert_no_secrets(text, "opencode")
    return text


def generate_agent_config(runtime_providers: Sequence[RuntimeProvider],
                          production_catalog: Any = None) -> str:
    """Project-owned generic OpenAI-compatible schema, not a third-party format."""
    catalog = validate_production_catalog(production_catalog)
    models = _models(runtime_providers, catalog, AGENT_REQUIRED_FEATURES)
    entries = []
    if models:
        entries.append({"id": "freellmpool", "name": "FreeLLMPool",
                        "endpoint": catalog["proxy_base_url"], "auth_env": PROXY_AUTH_ENV,
                        "models": models})
    payload = {"providers": entries}
    text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    assert yaml.safe_load(text) == payload
    _assert_no_secrets(text, "agent")
    return text


def _stage_file(directory: Path, name: str, content: bytes) -> Path:
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{name}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return Path(tmp)


def write_client_configs(directory: Path, runtime_providers: Sequence[RuntimeProvider],
                         production_catalog: Any = None) -> dict[str, Path]:
    """Prevalidate all documents, stage all bytes, then atomically replace each.

    Replacement errors roll back previous replacements. These fixed independent
    filenames cannot provide simultaneous visibility or power-loss atomicity
    across the set; consumers must not reload during an update. No success is
    reported on write/rollback failure.
    """
    catalog = validate_production_catalog(production_catalog)
    contents = {
        "codex": generate_codex_config(runtime_providers, catalog),
        "opencode": generate_opencode_config(runtime_providers, catalog),
        "agent": generate_agent_config(runtime_providers, catalog),
    }
    # Validate the complete set before any output directory or file is created.
    tomllib.loads(contents["codex"])
    json.loads(contents["opencode"])
    yaml.safe_load(contents["agent"])
    for name, text in contents.items():
        _assert_no_secrets(text, name)
    outputs = {"codex": directory / "codex-providers.toml", "opencode": directory / "opencode.json",
               "agent": directory / "agent-providers.yaml"}
    for path in outputs.values():
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ClientConfigError("invalid_output_target")
    directory.mkdir(parents=True, exist_ok=True)
    staged: dict[str, Path] = {}
    backups: dict[str, Path] = {}
    replaced = []
    preserve: set[Path] = set()
    try:
        for name, path in outputs.items():
            staged[name] = _stage_file(directory, path.name, contents[name].encode("utf-8"))
            if path.exists():
                backups[name] = _stage_file(directory, path.name, path.read_bytes())
        try:
            for name, path in outputs.items():
                os.replace(staged[name], path)
                replaced.append(name)
        except OSError:
            rollback_failed = False
            for name in reversed(replaced):
                try:
                    if name in backups:
                        os.replace(backups[name], outputs[name])
                    else:
                        outputs[name].unlink()
                except OSError:
                    rollback_failed = True
                    if name in backups:
                        preserve.add(backups[name])
                    # Restore remaining independent files despite this failure.
            if rollback_failed:
                raise OSError("client_config_rollback_failed_backups_preserved") from None
            raise
    finally:
        for path in (*staged.values(), *backups.values()):
            if path not in preserve:
                path.unlink(missing_ok=True)
    return outputs
