"""Deterministic, injection-safe TOML writing for FreeLLMPool config files.

FreeLLMPool 0.13.0 layout (verified against the installed package):

- ``providers.toml`` (FREELLMPOOL_CONFIG): ``[[provider]]`` array entries with
  id/label/adapter/base_url/key_env/models. Keys are NEVER stored here.
- ``config.toml`` (FREELLMPOOL_CONFIG_FILE): ``[keys]`` table mapping
  KEY_ENV_NAME -> secret value.

Review round 2 (二.4/二.5):
- serialization is escape-correct — provider ids and key values can never
  break out of a quoted string or table header (no f-string concatenation);
- every write is temp-file + flush + fsync + atomic replace;
- written files get owner-only permissions where the OS supports it;
- every serialized document is re-parsed with tomllib before it replaces the
  live file, so a malformed write fails closed without touching the config.
"""

from __future__ import annotations

import json
import os
import tempfile
import tomllib
from pathlib import Path
from typing import Any, Dict, List

# Owner-only permissions for config files containing provider keys.
SECURE_MODE = 0o600

_BARE_KEY_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")

_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\b": "\\b",
    "\f": "\\f",
}


def escape_basic_string(value: str) -> str:
    """Escape a string as a TOML basic (double-quoted) string."""
    out: List[str] = ['"']
    for ch in str(value):
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def table_key(key: str) -> str:
    """Render a table/keys key, quoting anything that is not bare-safe."""
    key = str(key)
    if key and all(ch in _BARE_KEY_CHARS for ch in key):
        return key
    return escape_basic_string(key)


def _inline_table_value(value: Dict[str, Any]) -> str:
    parts = []
    for k, v in value.items():
        rendered = _render_value(v)
        parts.append(f"{table_key(k)} = {rendered}")
    return "{" + ", ".join(parts) + "}"


def _render_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return escape_basic_string(value)
    if isinstance(value, list):
        if not value:
            return "[]"
        return "[" + ", ".join(_render_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return _inline_table_value(value)
    raise TypeError(f"cannot serialize {type(value).__name__} to TOML")


def render_providers_toml(providers: List[Dict[str, Any]]) -> str:
    """Render ``[[provider]]`` entries deterministically (no keys inside)."""
    blocks: List[str] = [
        "# freellmpool provider catalog managed by hunter pool-control.",
        "# Provider API keys are NEVER stored here - only key_env names.",
        "",
    ]
    for provider in sorted(providers, key=lambda p: str(p.get("id", ""))):
        if "api_key" in provider or "key" in provider or "key_value" in provider:
            raise ValueError("provider entries must never contain key material")
        blocks.append("[[provider]]")
        for field_name in ("id", "label", "adapter", "base_url", "key_env", "auth", "key_optional"):
            if field_name in provider and provider[field_name] is not None:
                blocks.append(f"{field_name} = {_render_value(provider[field_name])}")
        if provider.get("extra_env"):
            for env_name in sorted(provider["extra_env"]):
                blocks.append(f"extra_env.{table_key(env_name)} = {_render_value(provider['extra_env'][env_name])}")
        models = provider.get("models") or []
        blocks.append("models = [")
        for model in models:
            parts = []
            for model_key in ("name", "rpd", "enabled", "auto", "context"):
                if model_key in model and model[model_key] is not None:
                    parts.append(f"{table_key(model_key)} = {_render_value(model[model_key])}")
            blocks.append("    { " + ", ".join(parts) + " },")
        blocks.append("]")
        blocks.append("")
    return "\n".join(blocks)


def render_config_toml(keys: Dict[str, str]) -> str:
    """Render ``[keys]`` with the given env-name -> secret mapping."""
    lines = [
        "# freellmpool config managed by hunter pool-control.",
        "# [keys] holds provider key env values. Keep this file private.",
        "",
        "[keys]",
    ]
    for env_name in sorted(keys):
        value = keys[env_name]
        if value is None:
            continue
        if "\x00" in str(value):
            raise ValueError("key values must not contain NUL bytes")
        lines.append(f"{table_key(env_name)} = {escape_basic_string(str(value))}")
    return "\n".join(lines) + "\n"


def _apply_mode(path: Path) -> None:
    try:
        os.chmod(path, SECURE_MODE)
    except OSError:
        # Windows FAT-like filesystems may refuse chmod; the file is still
        # inside the Pool Control boundary. Best effort only.
        pass


def atomic_write_toml(path: Path, content: str) -> None:
    """Validate, then atomically replace ``path`` with the rendered TOML.

    Fails closed: the content is re-parsed with tomllib BEFORE any write, and
    the live file is only replaced after a successful temp-file fsync.
    """
    parsed = tomllib.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("serialized TOML did not parse to a table")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
        _apply_mode(path)
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    """Validate, fsync, and atomically replace a small control-state file."""
    content = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("serialized JSON did not parse to an object")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
        _apply_mode(path)
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def read_providers_toml(path: Path) -> List[Dict[str, Any]]:
    """Parse a providers.toml into a list of provider dicts."""
    if not path.is_file():
        return []
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    entries = data.get("provider", [])
    if not isinstance(entries, list):
        raise ValueError(f"{path}: 'provider' must be an array of tables")
    return [dict(entry) for entry in entries]


def read_config_keys(path: Path) -> Dict[str, str]:
    """Parse the [keys] table from a config.toml."""
    if not path.is_file():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    keys = data.get("keys", {})
    if not isinstance(keys, dict):
        raise ValueError(f"{path}: 'keys' must be a table")
    return {str(k): str(v) for k, v in keys.items()}


def verify_permissions(path: Path) -> bool:
    """True when the file is not world/group readable (best effort check)."""
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    return (mode & 0o077) == 0


__all__ = [
    "escape_basic_string",
    "table_key",
    "render_providers_toml",
    "render_config_toml",
    "atomic_write_toml",
    "atomic_write_json",
    "read_providers_toml",
    "read_config_keys",
    "verify_permissions",
    "SECURE_MODE",
]
