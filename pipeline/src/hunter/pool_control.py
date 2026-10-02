"""
Pool Control — isolated key management and FreeLLMPool orchestration (Stage 3).

Three-process security boundary:
  Hunter          →  no FreeLLMPool config mount, no keys in env, no access to
                     staging/production directories; talks to Pool Control
                     ONLY through the loopback API (see pool_api.py).
  Pool Control    →  manages staging + production FreeLLMPool configs; the only
                     process that ever holds Provider Keys.
  FreeLLMPool     →  staging has keys (no client port); production serves only
                     approved providers on 127.0.0.1:8080.

Verified FreeLLMPool 0.13.0 public interface (installed package):
  - ``FREELLMPOOL_CONFIG`` → providers.toml (catalog; keys NEVER stored here)
  - ``FREELLMPOOL_CONFIG_FILE`` → config.toml with the ``[keys]`` table
  - ``freellmpool.config.load_catalog(path)`` / ``configured_providers(catalog, env)``
  - ``freellmpool.conformance.run_target_canaries(provider, model, env=..., features=...)``
  - ``freellmpool.conformance.classify_canary_exception(exc)``

Key isolation rules (review round 2, 二):
  - Keys enter ONLY through an injected secret reader (getpass in production).
    Never argv, never Hunter environment, never HTTP request bodies, never
    logs, never API responses.
  - Staging/production directories must live OUTSIDE the Hunter repository.
  - All config writes are escape-safe TOML with fsync + atomic replace.
  - probe results are classified fail-closed (auth / rate_limit / quota /
    timeout / transport / availability / unsupported) — never "healthy" by
    default.
"""

from __future__ import annotations

import getpass
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from .pool_toml import (
    atomic_write_json,
    atomic_write_toml,
    read_config_keys,
    read_providers_toml,
    render_config_toml,
    render_providers_toml,
    verify_permissions,
)
from .pool_proxy import ProxySupervisor

logger = logging.getLogger("hunter.pool_control")

# The exact FreeLLMPool version this integration is written against.
REQUIRED_FREELLMPOOL_VERSION = "0.13.0"

# Canary feature names verified in freellmpool.conformance.FEATURES.
FEATURE_CHAT = "chat"
FEATURE_RESPONSES = "responses"
FEATURE_STREAMING = "streaming"
FEATURE_TOOLS = "tools"

PROTOCOL_FEATURES = (FEATURE_CHAT, FEATURE_RESPONSES, FEATURE_STREAMING, FEATURE_TOOLS)


class PoolControlError(Exception):
    """Structured Pool Control failure; never carries key material."""


# ---------------------------------------------------------------------------
# Health classification
# ---------------------------------------------------------------------------

# upstream classification -> (HealthStatus value, detail code)
_CLASSIFICATION_MAP = {
    "auth": ("INVALID_KEY", "auth"),
    "rate_limit": ("RATE_LIMITED", "rate_limit"),
    "quota": ("EXHAUSTED", "quota"),
    "timeout": ("DOWN", "timeout"),
    "transport": ("DOWN", "network"),
    "availability": ("DOWN", "availability"),
    "unsupported": ("DOWN", "model_not_found"),
    "client": ("DOWN", "client_error"),
}


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ProbeOutcome:
    """Sanitized result of one probe — never contains keys or raw responses."""

    provider_id: str
    feature: str
    status: str            # pass | fail
    health_status: str     # HEALTHY | RATE_LIMITED | EXHAUSTED | INVALID_KEY | DOWN
    classification: str
    checked_at: str = ""


# ---------------------------------------------------------------------------
# Real FreeLLMPool probe runner (public API only)
# ---------------------------------------------------------------------------


class FreellmpoolProbeRunner:
    """Runs canaries through the FreeLLMPool public API.

    ``load_catalog`` / ``effective_env`` / ``run_target_canaries`` are the
    package's documented public surface. The staging config.toml supplies key
    values via ``effective_env`` INSIDE this process (Pool Control boundary);
    results carry only pass/fail + classification, never keys or responses.
    """

    def check_version(self) -> None:
        try:
            import freellmpool
        except ImportError as exc:  # pragma: no cover - environment-dependent
            raise PoolControlError("freellmpool_not_installed") from exc
        version = str(getattr(freellmpool, "__version__", ""))
        if version != REQUIRED_FREELLMPOOL_VERSION:
            raise PoolControlError(
                f"freellmpool_version_unsupported:{version or 'unknown'}"
            )

    def run(
        self,
        provider_record: Dict[str, Any],
        key_env: Optional[str],
        config_file: Path,
        features: Sequence[str] = (FEATURE_CHAT,),
        timeout: float = 20.0,
    ) -> Dict[str, Dict[str, str]]:
        self.check_version()
        from freellmpool.config import effective_env
        from freellmpool.conformance import run_target_canaries
        from freellmpool.models import Model, Provider

        models = [
            Model(name=m["name"])
            for m in (provider_record.get("models") or [])
            if m.get("name")
        ]
        provider = Provider(
            id=str(provider_record.get("id", "")),
            label=str(provider_record.get("label", "") or provider_record.get("id", "")),
            adapter=str(provider_record.get("adapter", "openai")),
            base_url=str(provider_record.get("base_url", "")),
            models=models or [Model(name="default")],
            key_env=key_env,
        )
        env = effective_env({"FREELLMPOOL_CONFIG_FILE": str(config_file)})
        model = provider.models[0].name if provider.models else "default"
        return run_target_canaries(
            provider,
            model,
            env=env,
            features=tuple(features),
            timeout=timeout,
        )


# ---------------------------------------------------------------------------
# Provider status view (no key material)
# ---------------------------------------------------------------------------


@dataclass
class PoolProviderStatus:
    """Status response for a provider — NEVER contains keys."""

    provider_id: str
    in_staging: bool = False
    in_production: bool = False
    key_configured: bool = False
    health_status: str = "unknown"
    protocol_status: str = "unchecked"
    production_halted: bool = False


# ---------------------------------------------------------------------------
# Pool Control
# ---------------------------------------------------------------------------


class PoolControl:
    """Manages two FreeLLMPool config directories and the key lifecycle.

    The ``read_secret`` callable is the ONLY key entry point (getpass in
    production; injected fakes in tests). ``probe_runner`` runs the real
    FreeLLMPool canaries in production and is injectable for offline tests.
    """

    def __init__(
        self,
        staging_dir: Path,
        production_dir: Path,
        *,
        read_secret: Callable[[str], str] = getpass.getpass,
        probe_runner: Optional[FreellmpoolProbeRunner] = None,
        proxy_supervisor: Optional[ProxySupervisor] = None,
        hunter_root: Optional[Path] = None,
    ):
        self.staging_dir = Path(staging_dir)
        self.production_dir = Path(production_dir)
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self.production_dir.mkdir(parents=True, exist_ok=True)
        self._read_secret = read_secret
        self._probe_runner = probe_runner
        self._proxy_supervisor = proxy_supervisor
        self._hunter_root = Path(hunter_root).resolve() if hunter_root else None
        self.ensure_isolated()
        self._production_halted, self._halt_reason = self._load_control_state()
        self._last_probe: Dict[str, ProbeOutcome] = {}

    # -- isolation ----------------------------------------------------------

    def ensure_isolated(self) -> None:
        """staging/production must not live inside the Hunter repository."""
        if self._hunter_root is None:
            return
        for directory in (self.staging_dir, self.production_dir):
            resolved = directory.resolve()
            if resolved == self._hunter_root or self._hunter_root in resolved.parents:
                raise PoolControlError(
                    f"pool directory {directory} must not live inside the hunter repository"
                )

    def verify_production_permissions(self) -> bool:
        """True when the production config files are permission-hardened."""
        ok = True
        for name in ("providers.toml", "config.toml"):
            path = self.production_dir / name
            if path.is_file() and not verify_permissions(path):
                ok = False
        return ok

    # -- config paths ---------------------------------------------------------

    @property
    def staging_providers_path(self) -> Path:
        return self.staging_dir / "providers.toml"

    @property
    def staging_config_path(self) -> Path:
        return self.staging_dir / "config.toml"

    @property
    def production_providers_path(self) -> Path:
        return self.production_dir / "providers.toml"

    @property
    def production_config_path(self) -> Path:
        return self.production_dir / "config.toml"

    @property
    def control_state_path(self) -> Path:
        return self.production_dir / "control_state.json"

    def _load_control_state(self) -> tuple[bool, Optional[str]]:
        if not self.control_state_path.is_file():
            return False, None
        try:
            payload = json.loads(self.control_state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PoolControlError("invalid_control_state") from exc
        if (
            not isinstance(payload, dict)
            or set(payload) != {"production_halted", "reason"}
            or not isinstance(payload["production_halted"], bool)
            or not isinstance(payload["reason"], str)
        ):
            raise PoolControlError("invalid_control_state")
        return payload["production_halted"], payload["reason"]

    def _persist_halt(self, reason: str) -> None:
        try:
            atomic_write_json(
                self.control_state_path,
                {"production_halted": True, "reason": reason},
            )
        except (OSError, TypeError, ValueError) as exc:
            raise PoolControlError("control_state_write_failed") from exc
        self._production_halted = True
        self._halt_reason = reason

    def _clear_halt(self) -> None:
        try:
            atomic_write_json(
                self.control_state_path,
                {"production_halted": False, "reason": ""},
            )
        except (OSError, TypeError, ValueError) as exc:
            raise PoolControlError("control_state_write_failed") from exc
        self._production_halted = False
        self._halt_reason = None

    def _staging_providers(self) -> List[Dict[str, Any]]:
        return read_providers_toml(self.staging_providers_path)

    def _staging_keys(self) -> Dict[str, str]:
        return read_config_keys(self.staging_config_path)

    def _production_providers(self) -> List[Dict[str, Any]]:
        return read_providers_toml(self.production_providers_path)

    def _production_keys(self) -> Dict[str, str]:
        return read_config_keys(self.production_config_path)

    def _write_staging(
        self, providers: List[Dict[str, Any]], keys: Dict[str, str]
    ) -> None:
        atomic_write_toml(self.staging_providers_path, render_providers_toml(providers))
        atomic_write_toml(self.staging_config_path, render_config_toml(keys))

    def _write_production(
        self, providers: List[Dict[str, Any]], keys: Dict[str, str]
    ) -> None:
        atomic_write_toml(self.production_providers_path, render_providers_toml(providers))
        atomic_write_toml(self.production_config_path, render_config_toml(keys))

    def _restore_production(
        self, providers: List[Dict[str, Any]], keys: Dict[str, str]
    ) -> None:
        try:
            self._write_production(providers, keys)
        except (OSError, ValueError):
            logger.error("production rollback failed")

    def _halt_after_live_failure(self, reason: str) -> bool:
        try:
            self._persist_halt(reason)
        except PoolControlError:
            logger.error("production halt latch persistence failed")
            return False
        if self._proxy_supervisor is not None:
            try:
                self._proxy_supervisor.halt()
            except Exception:  # noqa: BLE001 - containment is best effort
                logger.error("production proxy halt failed")
        return True

    # -- registration & key entry ---------------------------------------------

    def register_provider(
        self,
        provider_id: str,
        *,
        label: str = "",
        base_url: str = "",
        models: Optional[List[Dict[str, Any]]] = None,
        key_env: Optional[str] = None,
        adapter: str = "openai",
    ) -> Dict[str, Any]:
        """Register (or update) a provider definition in staging.

        Only PUBLIC data crosses this boundary: ids, urls, model names, the
        NAME of the key env var — never a key value.
        """
        if not provider_id or not str(provider_id).strip():
            raise PoolControlError("provider_id must be non-empty")
        if not base_url or not str(base_url).startswith(("http://", "https://")):
            raise PoolControlError("base_url must be an http(s) URL")
        safe_key_env = (
            key_env or f"{str(provider_id).upper().replace('-', '_')}_API_KEY"
        ).replace(" ", "_")
        old_record = next((p for p in self._staging_providers() if p.get("id") == provider_id), None)
        providers = [
            p for p in self._staging_providers() if p.get("id") != provider_id
        ]
        record: Dict[str, Any] = {
            "id": str(provider_id),
            "label": str(label or provider_id),
            "adapter": str(adapter),
            "base_url": str(base_url),
            "key_env": safe_key_env,
            "models": [
                {"name": m.get("name", "")} for m in (models or [{"name": "default"}])
            ],
        }
        old_key_env = str(old_record.get("key_env") or "") if old_record else ""
        new_key_env = safe_key_env
        keys = self._staging_keys()
        # Re-registering with a NEW key_env must not silently accumulate the
        # old secret: drop it only when no remaining provider references it.
        if old_key_env and old_key_env != new_key_env:
            if not any(p.get("key_env") == old_key_env for p in providers):
                keys.pop(old_key_env, None)
        providers.append(record)
        self._write_staging(providers, keys)
        logger.info("registered provider %s in staging (no key material)", provider_id)
        return {"provider_id": str(provider_id), "registered": True, "key_env": safe_key_env}

    def enter_key(self, provider_id: str) -> Dict[str, Any]:
        """Prompt the operator (TTY/getpass) for the provider key.

        The key goes straight from the secret reader into the staging
        config.toml; it is never returned, logged, or exposed via the API.
        """
        providers = self._staging_providers()
        record = next((p for p in providers if p.get("id") == provider_id), None)
        if record is None:
            return {
                "provider_id": provider_id,
                "configured": False,
                "error": "provider_not_registered",
            }
        key_env = record.get("key_env")
        if not key_env:
            return {
                "provider_id": provider_id,
                "configured": False,
                "error": "provider_has_no_key_env",
            }
        secret = self._read_secret(f"API key for {provider_id} ({key_env}): ")
        if not secret:
            return {"provider_id": provider_id, "configured": False, "error": "empty_key"}
        keys = self._staging_keys()
        keys[str(key_env)] = secret
        self._write_staging(providers, keys)
        logger.info("key configured for %s (value never logged)", provider_id)
        return {"provider_id": provider_id, "configured": True}

    def remove_provider(self, provider_id: str) -> Dict[str, Any]:
        """Remove a provider definition and its key from staging only.

        Refuses if the provider is currently in production.
        Removal is idempotent: removing a non-existent provider succeeds.
        """
        if any(p.get("id") == provider_id for p in self._production_providers()):
            return {
                "provider_id": provider_id,
                "removed": False,
                "error": "in_production",
            }
        providers = [p for p in self._staging_providers() if p.get("id") != provider_id]
        keys = self._staging_keys()
        removed_key = False
        retained_reason = ""
        record = next(
            (p for p in self._staging_providers() if p.get("id") == provider_id), None
        )
        if record is not None:
            key_env = str(record.get("key_env") or "")
            if key_env in keys:
                # Shared key_env safety: only delete when no remaining
                # provider still references it (remediation plan §5).
                still_referenced = any(
                    p.get("key_env") == key_env for p in providers
                )
                if still_referenced:
                    retained_reason = "referenced_by_remaining_provider"
                else:
                    del keys[key_env]
                    removed_key = True
        self._write_staging(providers, keys)
        logger.info("removed provider %s from staging", provider_id)
        return {
            "provider_id": provider_id,
            "removed": True,
            "key_removed": removed_key,
            **({"key_retained_reason": retained_reason} if retained_reason else {}),
        }

    def _is_configured(self, provider_id: str) -> bool:
        record = next(
            (p for p in self._staging_providers() if p.get("id") == provider_id), None
        )
        if record is None:
            return False
        key_env = record.get("key_env")
        if not key_env:
            return False
        return bool(self._staging_keys().get(str(key_env)))

    # -- status -----------------------------------------------------------------

    def status(self, provider_id: str) -> PoolProviderStatus:
        """Return provider status without ever revealing the key."""
        record = next(
            (p for p in self._staging_providers() if p.get("id") == provider_id), None
        )
        in_production = any(
            p.get("id") == provider_id for p in self._production_providers()
        )
        last = self._last_probe.get(provider_id)
        return PoolProviderStatus(
            provider_id=provider_id,
            in_staging=record is not None,
            in_production=in_production,
            key_configured=self._is_configured(provider_id),
            health_status=last.health_status if last else "unknown",
            protocol_status=last.status if last else "unchecked",
            production_halted=self._production_halted,
        )

    def production_catalog(self) -> Dict[str, Any]:
        """Strict live readback for client configs; unlike list_production, no disk fallback."""
        from .client_config import validate_production_catalog

        if self._production_halted or self._proxy_supervisor is None:
            raise PoolControlError("production_catalog_unavailable")
        try:
            FreellmpoolProbeRunner().check_version()
            return validate_production_catalog(self._proxy_supervisor.production_catalog())
        except Exception:
            raise PoolControlError("production_catalog_unavailable") from None

    def list_production(self) -> List[str]:
        """Provider ids loaded by the live proxy, or TOML for test-only use."""
        if self._proxy_supervisor is not None:
            try:
                return sorted(self._proxy_supervisor.running_provider_ids())
            except Exception:  # noqa: BLE001 - missing live readback is empty
                return []
        return sorted(
            str(p.get("id")) for p in self._production_providers() if p.get("id")
        )

    # -- probe (real health / protocol checks) ------------------------------------

    def probe(
        self,
        provider_id: str,
        features: Sequence[str] = (FEATURE_CHAT,),
        timeout: float = 20.0,
    ) -> Dict[str, Any]:
        """Run real canaries through FreeLLMPool; classify fail-closed.

        Returns a sanitized dict: provider_id, per-feature status, the mapped
        health_status, and a classification code. No keys, no raw responses.
        """
        if self._probe_runner is None:
            return {
                "provider_id": provider_id,
                "health_status": "UNKNOWN",
                "classification": "probe_runner_unavailable",
                "error": "probe_runner_unavailable",
                "features": {},
            }
        record = next(
            (p for p in self._staging_providers() if p.get("id") == provider_id), None
        )
        if record is None:
            return {
                "provider_id": provider_id,
                "health_status": "UNKNOWN",
                "classification": "provider_not_registered",
                "error": "provider_not_registered",
                "features": {},
            }
        if not self._is_configured(provider_id):
            return {
                "provider_id": provider_id,
                "health_status": "UNKNOWN",
                "classification": "key_not_configured",
                "error": "key_not_configured",
                "features": {},
            }
        try:
            rows = self._probe_runner.run(
                record,
                record.get("key_env"),
                self.staging_config_path,
                features=features,
                timeout=timeout,
            )
        except PoolControlError as exc:
            return {
                "provider_id": provider_id,
                "health_status": "DOWN",
                "classification": str(exc),
                "error": str(exc),
                "features": {},
            }
        except Exception:  # noqa: BLE001 - any probe failure is classified
            return {
                "provider_id": provider_id,
                "health_status": "DOWN",
                "classification": "probe_failed",
                "error": "probe_failed",
                "features": {},
            }
        feature_rows = {k: dict(v) for k, v in rows.items()}
        health = "HEALTHY"
        classification = "verified"
        for feature, row in feature_rows.items():
            if row.get("status") == "pass":
                continue
            mapped, code = _CLASSIFICATION_MAP.get(
                row.get("classification", ""), ("DOWN", "unknown")
            )
            if feature == FEATURE_CHAT:
                health = mapped
                classification = code
            elif classification == "verified":
                classification = code
        all_pass = all(r.get("status") == "pass" for r in feature_rows.values())
        self._last_probe[provider_id] = ProbeOutcome(
            provider_id=provider_id,
            feature=",".join(sorted(feature_rows)),
            status="pass" if all_pass else "fail",
            health_status=health,
            classification=classification,
            checked_at=_now_iso(),
        )
        return {
            "provider_id": provider_id,
            "health_status": health,
            "classification": classification,
            "checked_at": _now_iso(),
            "features": feature_rows,
            "error": None,
        }

    def probe_health(self, provider_id: str, timeout: float = 20.0) -> Dict[str, Any]:
        return self.probe(provider_id, features=(FEATURE_CHAT,), timeout=timeout)

    def probe_protocols(self, provider_id: str, timeout: float = 20.0) -> Dict[str, Any]:
        return self.probe(provider_id, features=PROTOCOL_FEATURES, timeout=timeout)

    # -- promote / suspend ---------------------------------------------------------

    def promote(self, provider_id: str) -> Dict[str, Any]:
        """Copy the approved provider's definition + key into production.

        Fail-closed semantics (review 四.1): the promotion only reports success
        after a full READBACK — the production catalog is re-parsed and the
        provider must appear with its key configured in production.
        """
        if self._production_halted:
            return {
                "provider_id": provider_id,
                "promoted": False,
                "error": "production_halted",
            }
        staging_providers = self._staging_providers()
        record = next(
            (p for p in staging_providers if p.get("id") == provider_id), None
        )
        if record is None:
            return {
                "provider_id": provider_id,
                "promoted": False,
                "error": "provider_not_registered",
            }
        if not self._is_configured(provider_id):
            return {
                "provider_id": provider_id,
                "promoted": False,
                "error": "key_not_configured",
            }

        key_env = str(record.get("key_env"))
        staging_keys = self._staging_keys()
        if not staging_keys.get(key_env):
            return {
                "provider_id": provider_id,
                "promoted": False,
                "error": "key_missing_in_staging",
            }

        previous_providers = self._production_providers()
        previous_keys = self._production_keys()
        production_providers = [
            p for p in previous_providers if p.get("id") != provider_id
        ]
        production_providers.append(record)
        production_keys = dict(previous_keys)
        production_keys[key_env] = staging_keys[key_env]
        try:
            self._write_production(production_providers, production_keys)
        except (OSError, ValueError):
            logger.error("promotion write failed for %s", provider_id)
            return {
                "provider_id": provider_id,
                "promoted": False,
                "error": "production_write_failed",
            }

        # READBACK: production catalog must contain the provider with its key.
        if not self._readback_configured(provider_id, self.production_dir):
            self._restore_production(previous_providers, previous_keys)
            return {
                "provider_id": provider_id,
                "promoted": False,
                "error": "production_readback_failed",
            }
        if self._proxy_supervisor is not None:
            try:
                self._proxy_supervisor.reload()
            except Exception:  # noqa: BLE001 - raw proxy errors may contain secrets
                self._restore_production(previous_providers, previous_keys)
                if not self._halt_after_live_failure("promotion_reload_failed"):
                    return {
                        "provider_id": provider_id,
                        "promoted": False,
                        "error": "control_state_write_failed",
                    }
                return {
                    "provider_id": provider_id,
                    "promoted": False,
                    "error": "live_proxy_reload_failed",
                }
            try:
                loaded = self._proxy_supervisor.running_provider_ids()
            except Exception:  # noqa: BLE001 - fail closed on unavailable readback
                loaded = set()
            if provider_id not in loaded:
                self._restore_production(previous_providers, previous_keys)
                if not self._halt_after_live_failure("promotion_readback_failed"):
                    return {
                        "provider_id": provider_id,
                        "promoted": False,
                        "error": "control_state_write_failed",
                    }
                return {
                    "provider_id": provider_id,
                    "promoted": False,
                    "error": "live_proxy_readback_failed",
                }
        logger.info("promoted %s to production (verified by readback)", provider_id)
        return {"provider_id": provider_id, "promoted": True, "error": None}

    def _readback_configured(self, provider_id: str, directory: Path) -> bool:
        """Re-parse the directory's files and confirm the provider is usable."""
        providers = read_providers_toml(directory / "providers.toml")
        record = next((p for p in providers if p.get("id") == provider_id), None)
        if record is None:
            return False
        key_env = record.get("key_env")
        if not key_env:
            return True  # keyless provider
        keys = read_config_keys(directory / "config.toml")
        return bool(keys.get(str(key_env)))

    def suspend(self, provider_id: str) -> Dict[str, Any]:
        """Remove a provider from production; verify removal by READBACK.

        Fail-closed semantics (review 四.3/四.4): returns suspended=False when
        the provider is still present after the removal attempt.
        """
        production_providers = self._production_providers()
        record = next(
            (p for p in production_providers if p.get("id") == provider_id), None
        )
        previous_keys = self._production_keys()
        if record is None:
            remaining = production_providers
            production_keys = previous_keys
        else:
            remaining = [p for p in production_providers if p.get("id") != provider_id]
            key_env = str(record.get("key_env") or "")
            production_keys = {
                k: v for k, v in previous_keys.items() if k != key_env
            }
            try:
                self._write_production(remaining, production_keys)
            except (OSError, ValueError):
                logger.error("suspend write failed for %s", provider_id)
                return {
                    "provider_id": provider_id,
                    "suspended": False,
                    "error": "production_write_failed",
                }
        still_there = any(p.get("id") == provider_id for p in self._production_providers())
        if still_there:
            return {
                "provider_id": provider_id,
                "suspended": False,
                "error": "production_readback_failed",
            }
        if self._proxy_supervisor is not None:
            try:
                self._proxy_supervisor.reload()
            except Exception:  # noqa: BLE001 - raw proxy errors may contain secrets
                if not self._halt_after_live_failure("suspension_reload_failed"):
                    return {
                        "provider_id": provider_id,
                        "suspended": False,
                        "error": "control_state_write_failed",
                    }
                return {
                    "provider_id": provider_id,
                    "suspended": False,
                    "error": "live_proxy_reload_failed",
                }
            try:
                loaded = self._proxy_supervisor.running_provider_ids()
            except Exception:  # noqa: BLE001 - fail closed on unavailable readback
                loaded = {provider_id}
            if provider_id in loaded:
                if not self._halt_after_live_failure("suspension_readback_failed"):
                    return {
                        "provider_id": provider_id,
                        "suspended": False,
                        "error": "control_state_write_failed",
                    }
                return {
                    "provider_id": provider_id,
                    "suspended": False,
                    "error": "live_proxy_readback_failed",
                }
        response = {"provider_id": provider_id, "suspended": True, "error": None}
        if record is None:
            response["already_absent"] = True
        return response

    def stop_production(self, reason: str = "manual_stop") -> Dict[str, Any]:
        """Halt the production pool: no further promotions; production marked
        blocked so clients can be reconfigured away (review 四.4 containment)."""
        try:
            self._persist_halt(reason)
        except PoolControlError:
            return {"halted": False, "error": "control_state_write_failed"}
        if self._proxy_supervisor is not None:
            try:
                self._proxy_supervisor.halt()
            except Exception:  # noqa: BLE001 - latch remains the containment authority
                logger.error("production proxy halt failed")
                return {"halted": True, "error": "proxy_halt_failed"}
        logger.error("production pool HALTED")
        return {"halted": True}

    def resume_production(self) -> Dict[str, Any]:
        self._clear_halt()
        return {"halted": False}

    @property
    def production_halted(self) -> bool:
        return self._production_halted
