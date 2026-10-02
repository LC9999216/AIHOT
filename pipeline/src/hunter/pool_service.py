"""Dedicated Pool Control service that owns TOML, keys, and the proxy."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from .pool_api import PoolControlServer
from .pool_control import FreellmpoolProbeRunner, PoolControl, PoolControlError
from .pool_proxy import FreellmpoolProxySupervisor, ProxySupervisor
from .runtime.locks import LockHeldError, ProcessFileLock


class PoolServiceError(Exception):
    """Safe service startup failure without credential material."""


@dataclass
class PoolService:
    pool_control: PoolControl
    proxy_supervisor: ProxySupervisor
    control_server: PoolControlServer
    service_lock: Optional[ProcessFileLock] = None

    def run(self) -> None:
        if self.service_lock is not None:
            try:
                self.service_lock.acquire(reentrant=False)
            except LockHeldError as exc:
                raise PoolServiceError("pool_control_service_running") from exc
        try:
            if not self.pool_control.production_halted:
                self.proxy_supervisor.reload()
            self.control_server.serve_forever()
        finally:
            self.proxy_supervisor.halt()
            self.control_server.close()
            if self.service_lock is not None:
                self.service_lock.release()


def _build_pool_components(
    *,
    staging_dir: Path,
    production_dir: Path,
    host: str,
    proxy_port: int,
) -> tuple[PoolControl, ProxySupervisor, str]:
    token = os.environ.get("HUNTER_POOL_CONTROL_TOKEN", "")
    if not token:
        raise PoolServiceError("control_token_required")
    if host != "127.0.0.1":
        raise PoolServiceError("loopback_host_required")
    staging_dir = Path(staging_dir)
    production_dir = Path(production_dir)
    os.environ["FREELLMPOOL_CONFIG"] = str(production_dir / "providers.toml")
    os.environ["FREELLMPOOL_CONFIG_FILE"] = str(production_dir / "config.toml")
    supervisor = FreellmpoolProxySupervisor(
        production_dir / "providers.toml",
        production_dir / "config.toml",
        host="127.0.0.1",
        port=proxy_port,
        proxy_key=os.environ.get("FREELLMPOOL_PROXY_KEY") or None,
    )
    control = PoolControl(
        staging_dir,
        production_dir,
        probe_runner=FreellmpoolProbeRunner(),
        proxy_supervisor=supervisor,
    )
    return control, supervisor, token


def build_service(
    *,
    staging_dir: Path,
    production_dir: Path,
    host: str,
    port: int,
    proxy_port: int,
) -> PoolService:
    control, supervisor, token = _build_pool_components(
        staging_dir=staging_dir,
        production_dir=production_dir,
        host=host,
        proxy_port=proxy_port,
    )
    FreellmpoolProbeRunner().check_version()
    server = PoolControlServer(control, token, host=host, port=port)
    return PoolService(
        pool_control=control,
        proxy_supervisor=supervisor,
        control_server=server,
        service_lock=ProcessFileLock(production_dir / ".pool-control-service.lock"),
    )


def _default_pool_dir() -> Path:
    return Path.home() / ".hunter-pool"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hunter-pool-control",
        description="Run the isolated loopback Pool Control service.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # serve / resume
    for command in ("serve", "resume"):
        child = subparsers.add_parser(command)
        child.add_argument("--staging-dir", type=Path, default=_default_pool_dir() / "staging")
        child.add_argument(
            "--production-dir", type=Path, default=_default_pool_dir() / "production"
        )
        child.add_argument("--host", default="127.0.0.1")
        child.add_argument("--port", type=int, default=8091)
        child.add_argument("--proxy-port", type=int, default=8080)
        if command == "resume":
            child.add_argument("--confirm", required=True)

    # Local maintenance commands (require service to be STOPPED)
    for command in ("register-provider", "enter-key", "provider-status", "remove-provider"):
        child = subparsers.add_parser(command)
        child.add_argument("--staging-dir", type=Path, default=_default_pool_dir() / "staging")
        child.add_argument(
            "--production-dir", type=Path, default=_default_pool_dir() / "production"
        )
        child.add_argument("--provider-id", required=True)
        if command == "register-provider":
            child.add_argument("--label", default="")
            child.add_argument("--base-url", required=True)
            child.add_argument("--adapter", default="openai")
            child.add_argument("--model", nargs="*", default=["default"])
            child.add_argument("--key-env", default=None)
        elif command == "remove-provider":
            child.add_argument("--confirm", required=True, help="exact Provider-ID confirmation")

    return parser


def _acquire_service_lock(production_dir: Path) -> ProcessFileLock:
    """Acquire the pool-control service lock; raises if the service is running."""
    lock = ProcessFileLock(production_dir / ".pool-control-service.lock")
    try:
        lock.acquire(reentrant=False)
    except LockHeldError as exc:
        raise PoolServiceError("pool_control_service_running") from exc
    return lock


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        # Local maintenance commands: require service to be STOPPED.
        if args.command == "register-provider":
            lock = _acquire_service_lock(args.production_dir)
            try:
                control, _, _ = _build_pool_components(
                    staging_dir=args.staging_dir,
                    production_dir=args.production_dir,
                    host="127.0.0.1",
                    proxy_port=8080,
                )
                models = [{"name": m} for m in (args.model or ["default"])]
                result = control.register_provider(
                    provider_id=args.provider_id,
                    label=args.label,
                    base_url=args.base_url,
                    adapter=args.adapter,
                    models=models,
                    key_env=args.key_env,
                )
                print(f"registered={result['registered']} provider_id={result['provider_id']} key_env={result.get('key_env', '')}")
            finally:
                lock.release()
            return 0

        if args.command == "enter-key":
            lock = _acquire_service_lock(args.production_dir)
            try:
                control, _, _ = _build_pool_components(
                    staging_dir=args.staging_dir,
                    production_dir=args.production_dir,
                    host="127.0.0.1",
                    proxy_port=8080,
                )
                result = control.enter_key(provider_id=args.provider_id)
                configured = result.get("configured", False)
                error = result.get("error", "")
                if configured:
                    print(f"key_configured=true provider_id={args.provider_id}")
                else:
                    print(f"key_configured=false provider_id={args.provider_id} error={error}")
            finally:
                lock.release()
            return 0 if configured else 1

        if args.command == "provider-status":
            lock = _acquire_service_lock(args.production_dir)
            try:
                control, _, _ = _build_pool_components(
                    staging_dir=args.staging_dir,
                    production_dir=args.production_dir,
                    host="127.0.0.1",
                    proxy_port=8080,
                )
                status = control.status(args.provider_id)
                print(
                    f"provider_id={status.provider_id} "
                    f"in_staging={status.in_staging} "
                    f"in_production={status.in_production} "
                    f"key_configured={status.key_configured} "
                    f"health_status={status.health_status} "
                    f"protocol_status={status.protocol_status} "
                    f"production_halted={status.production_halted}"
                )
            finally:
                lock.release()
            return 0

        if args.command == "remove-provider":
            if args.confirm != args.provider_id:
                raise PoolServiceError(
                    f"remove_provider_confirmation_mismatch: expected {args.provider_id}"
                )
            lock = _acquire_service_lock(args.production_dir)
            try:
                control, _, _ = _build_pool_components(
                    staging_dir=args.staging_dir,
                    production_dir=args.production_dir,
                    host="127.0.0.1",
                    proxy_port=8080,
                )
                result = control.remove_provider(provider_id=args.provider_id)
                removed = result.get("removed", False)
                error = result.get("error", "")
                if removed:
                    print(f"removed=true provider_id={args.provider_id}")
                else:
                    print(f"removed=false provider_id={args.provider_id} error={error}")
            finally:
                lock.release()
            return 0 if removed else 1

        if args.command == "resume":
            if args.confirm != "RESUME":
                raise PoolServiceError("resume_confirmation_required")
            control, _, _ = _build_pool_components(
                staging_dir=args.staging_dir,
                production_dir=args.production_dir,
                host=args.host,
                proxy_port=args.proxy_port,
            )
            lock = _acquire_service_lock(args.production_dir)
            try:
                control.resume_production()
            finally:
                lock.release()
            print("production halt latch cleared; start serve to reload the proxy")
            return 0

        service = build_service(
            staging_dir=args.staging_dir,
            production_dir=args.production_dir,
            host=args.host,
            port=args.port,
            proxy_port=args.proxy_port,
        )
        service.run()
        return 0
    except (PoolControlError, PoolServiceError) as exc:
        print(str(exc))
        return 1
    except Exception:  # noqa: BLE001 - never expose proxy or upstream details
        print("pool_service_start_failed")
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["PoolService", "PoolServiceError", "build_service", "main"]
