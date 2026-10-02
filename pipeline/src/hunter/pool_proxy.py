"""Live FreeLLMPool proxy supervision inside the Pool Control process only."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Callable, Optional, Protocol


def _control_error(code: str) -> Exception:
    # Imported lazily so PoolControl can depend on this module without a cycle.
    from .pool_control import PoolControlError

    return PoolControlError(code)


class ProxySupervisor(Protocol):
    def reload(self) -> None: ...

    def halt(self) -> None: ...

    def running_provider_ids(self) -> set[str]: ...

    def production_catalog(self) -> dict[str, Any]: ...


class FreellmpoolProxySupervisor:
    """Own a FreeLLMPool 0.13.0 proxy and expose sanitized loaded IDs only."""

    def __init__(
        self,
        providers_path: Path,
        config_path: Path,
        *,
        host: str = "127.0.0.1",
        port: int = 8080,
        proxy_key: Optional[str] = None,
        pool_factory: Optional[Callable[[], Any]] = None,
        server_factory: Optional[Callable[[Any], Any]] = None,
    ):
        if host != "127.0.0.1":
            raise _control_error("proxy_loopback_host_required")
        self.providers_path = Path(providers_path)
        self.config_path = Path(config_path)
        self.host = host
        self.port = port
        self.proxy_key = proxy_key
        self._pool_factory = pool_factory or self._build_pool
        self._server_factory = server_factory or self._build_server
        self._server: Any = None
        self._thread: Optional[threading.Thread] = None

    def _build_pool(self) -> Any:
        from freellmpool import Pool

        expected_catalog = str(self.providers_path)
        expected_config = str(self.config_path)
        if os.environ.get("FREELLMPOOL_CONFIG") != expected_catalog:
            raise _control_error("freellmpool_catalog_path_mismatch")
        if os.environ.get("FREELLMPOOL_CONFIG_FILE") != expected_config:
            raise _control_error("freellmpool_config_path_mismatch")
        return Pool.from_default_config()

    def _build_server(self, pool: Any) -> Any:
        from freellmpool.proxy import serve

        return serve(pool, host=self.host, port=self.port, api_key=self.proxy_key)

    def reload(self) -> None:
        self.halt()
        server = self._server_factory(self._pool_factory())
        thread = threading.Thread(
            target=server.serve_forever,
            name="hunter-freellmpool-proxy",
            daemon=True,
        )
        self._server = server
        self._thread = thread
        thread.start()

    def halt(self) -> None:
        server = self._server
        self._server = None
        self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()

    def production_catalog(self) -> dict[str, Any]:
        """Snapshot enabled models from the loaded pool, never from config files."""
        from .client_config import PROXY_AUTH_ENV, validate_production_catalog
        from .pool_control import FreellmpoolProbeRunner

        FreellmpoolProbeRunner().check_version()
        server = self._server
        thread = self._thread
        if server is None or thread is None or not thread.is_alive() or not self.proxy_key:
            raise _control_error("production_catalog_unavailable")
        rows = [{"provider_id": provider.id,
                 "model_ids": sorted(f"{provider.id}/{model.name}" for model in provider.models if model.enabled)}
                for provider in server.pool.providers]
        host, port = server.server_address[:2]
        result = validate_production_catalog({
            "freellmpool_version": "0.13.0", "proxy_base_url": f"http://{host}:{port}/v1",
            "proxy_auth_env": PROXY_AUTH_ENV, "providers": rows,
        })
        if server is not self._server or not thread.is_alive():
            raise _control_error("production_catalog_unavailable")
        return result

    def running_provider_ids(self) -> set[str]:
        server = self._server
        if server is None:
            return set()
        return {
            str(provider.id)
            for provider in server.pool.providers
            if getattr(provider, "id", None)
        }


__all__ = ["ProxySupervisor", "FreellmpoolProxySupervisor"]
