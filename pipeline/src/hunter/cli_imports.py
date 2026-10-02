"""CLI import wiring for Stage 2 subcommands.

Registers ``run-stage-two`` (flat) plus the nested ``pool approve`` and
``runtime status`` parsers. Argument parsing stays thin (no business logic);
every handler delegates to a worker in :mod:`hunter.runtime.stage2`.
"""

from __future__ import annotations

import os


def _resolve_data_dir(args) -> "Path":  # noqa: ANN001
    from pathlib import Path

    if getattr(args, "data_dir", None):
        return Path(args.data_dir)
    from .config import load_settings

    settings = load_settings()
    return Path(settings["paths"]["data_dir"])


def _handle_run_stage_two(args) -> int:  # noqa: ANN001
    import json

    from .pool_api import PoolControlClient
    from .runtime.stage2 import Stage2Runner

    data_dir = _resolve_data_dir(args)
    base_url = os.environ.get("HUNTER_POOL_CONTROL_URL", "")
    token = os.environ.get("HUNTER_POOL_CONTROL_TOKEN", "")
    if not base_url or not token:
        raise RuntimeError("pool control URL and token are required")
    pool_control = PoolControlClient(base_url, token)

    # Optional Feishu webhook adapter (no secrets in Git, env var only)
    webhook_url = os.environ.get("HUNTER_FEISHU_WEBHOOK_URL", "")
    notification_adapter = None
    if webhook_url:
        from .runtime.notify import WebhookFeishuAdapter
        notification_adapter = WebhookFeishuAdapter(webhook_url)

    runner = Stage2Runner(
        data_dir=data_dir,
        pool_control=pool_control,
        notification_adapter=notification_adapter,
    )
    summary = runner.run()
    print(json.dumps(summary.to_dict(), indent=2))
    if summary.skipped_locked:
        return 3  # documented skip exit code
    if summary.suspend_failed or summary.pool_runtime_split:
        return 1
    return 0


def _handle_pool_approve(args) -> int:  # noqa: ANN001
    import json

    from .runtime.stage2 import approve_provider

    data_dir = _resolve_data_dir(args)
    result = approve_provider(
        args.provider_id,
        args.expected_revision,
        data_dir=data_dir,
        approved_by=args.approved_by,
    )
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


def _handle_pool_control_status(args) -> int:  # noqa: ANN001
    """Sanitized pool/service status via PoolControlClient (no keys)."""
    import json

    from .pool_api import PoolControlClient

    base_url = os.environ.get("HUNTER_POOL_CONTROL_URL", "")
    token = os.environ.get("HUNTER_POOL_CONTROL_TOKEN", "")
    if not base_url or not token:
        raise RuntimeError("pool control URL and token are required")
    pc = PoolControlClient(base_url, token)
    status = pc.pool_status()
    print(json.dumps(status, indent=2))
    return 0


def _handle_pool_suspend(args) -> int:  # noqa: ANN001
    """Suspend a provider from the production pool via PoolControlClient."""
    import json

    # Enforce the exact literal BEFORE any env read or network call; argparse
    # required-ness alone does not stop a wrong value like --confirm WRONG.
    if args.confirm != "SUSPEND":
        print(json.dumps({"ok": False, "error": "confirm_word_mismatch"}))
        return 2

    from .pool_api import PoolControlClient

    base_url = os.environ.get("HUNTER_POOL_CONTROL_URL", "")
    token = os.environ.get("HUNTER_POOL_CONTROL_TOKEN", "")
    if not base_url or not token:
        raise RuntimeError("pool control URL and token are required")
    pc = PoolControlClient(base_url, token)
    result = pc.suspend(args.provider_id)
    print(json.dumps(result, indent=2))
    return 0 if result.get("suspended") else 1


def _handle_pool_stop(args) -> int:  # noqa: ANN001
    """Halt production containment via PoolControlClient."""
    import json

    # Enforce the exact literal BEFORE any env read or network call.
    if args.confirm != "STOP":
        print(json.dumps({"ok": False, "error": "confirm_word_mismatch"}))
        return 2

    from .pool_api import PoolControlClient

    base_url = os.environ.get("HUNTER_POOL_CONTROL_URL", "")
    token = os.environ.get("HUNTER_POOL_CONTROL_TOKEN", "")
    if not base_url or not token:
        raise RuntimeError("pool control URL and token are required")
    pc = PoolControlClient(base_url, token)
    result = pc.stop_production()
    print(json.dumps(result, indent=2))
    # Truthful exit codes: success ONLY when halted is true and no error.
    if result.get("halted") is True and not result.get("error"):
        return 0
    return 1


def _handle_runtime_status(args) -> int:  # noqa: ANN001
    import json

    from .runtime.stage2 import runtime_status

    rows = runtime_status(_resolve_data_dir(args))
    print(json.dumps(rows, indent=2))
    return 0


def _handle_client_config_write(args) -> int:  # noqa: ANN001
    """Generate client configs from readback-confirmed production providers."""
    import json
    from pathlib import Path

    from .client_config import ClientConfigError, write_client_configs
    from .pool_api import PoolApiError, PoolControlClient
    from .runtime.store import RuntimeStore

    data_dir = _resolve_data_dir(args)
    output_dir = Path(args.output_dir) if args.output_dir else data_dir / "clients"

    # Read runtime providers
    store = RuntimeStore(data_dir / "runtime_providers.json", data_dir / "runtime_history.jsonl")
    providers = store.list_providers()

    # Production config has no runtime-only or disk-catalog fallback.
    base_url = os.environ.get("HUNTER_POOL_CONTROL_URL", "")
    token = os.environ.get("HUNTER_POOL_CONTROL_TOKEN", "")
    if not base_url or not token:
        print(json.dumps({"error": "pool_control_not_configured"}))
        return 2
    try:
        pc = PoolControlClient(base_url, token)
        catalog = pc.production_catalog()
        outputs = write_client_configs(output_dir, providers, catalog)
    except (PoolApiError, ClientConfigError, OSError, ValueError):
        # Never reflect arbitrary remote errors, paths, or credential values.
        print(json.dumps({"error": "client_config_readback_or_write_failed"}))
        return 1
    summary = {k: str(v) for k, v in outputs.items()}
    print(json.dumps(summary, indent=2))
    return 0


def _handle_notifications_drain(args) -> int:  # noqa: ANN001
    """Deliver pending outbox messages through a real adapter.

    Order is authoritative: adapter.send() MUST succeed before mark-sent.
    Without HUNTER_FEISHU_WEBHOOK_URL this fails closed (exit 2) and keeps
    every message pending; delivery failures keep messages pending (exit 1).
    """
    import json

    from .runtime.notify import NotificationError, OutboxConsumer
    from .runtime.outbox import OutboxStore

    data_dir = _resolve_data_dir(args)
    webhook_url = os.environ.get("HUNTER_FEISHU_WEBHOOK_URL", "")
    if not webhook_url:
        print(
            json.dumps(
                {
                    "error": "feishu_webhook_not_configured",
                    "sent": [],
                    "failed": [],
                    "skipped_retry": [],
                    "sent_count": 0, "failed_count": 0, "skipped_retry_count": 0,
                },
                indent=2,
            )
        )
        return 2

    from .runtime.notify import WebhookFeishuAdapter

    try:
        adapter = WebhookFeishuAdapter(webhook_url)
    except NotificationError as exc:
        print(json.dumps({"error": exc.code, "sent": [], "failed": [], "skipped_retry": [],
                          "sent_count": 0, "failed_count": 0, "skipped_retry_count": 0}))
        return 2

    outbox = OutboxStore(data_dir / "notification_outbox.json")
    try:
        consumer = OutboxConsumer(outbox, adapter)
        result = consumer.process_once()
    finally:
        outbox.close()
    print(
        json.dumps(
            {
                "drained": len(result.sent),
                "sent": list(result.sent),
                "failed": list(result.failed),
                "skipped_retry": list(result.skipped_retry),
                "sent_count": len(result.sent),
                "failed_count": len(result.failed),
                "skipped_retry_count": len(result.skipped_retry),
            },
            indent=2,
        )
    )
    if result.failed:
        return 1
    return 0


def register_stage_two_parsers(subparsers) -> None:  # noqa: ANN001
    """Attach the Stage 2 CLI surface to the hunter parser."""
    from .cli import register_subcommand

    run_two = subparsers.add_parser(
        "run-stage-two",
        help="run the full stage-two lifecycle (lock→reconcile→suspend-first→import→checks→promote→verify)",
    )
    run_two.add_argument("--data-dir", default=None, help="data directory")
    run_two.set_defaults(handler=_handle_run_stage_two)

    pool = subparsers.add_parser("pool", help="pool control operations")
    pool_sub = pool.add_subparsers(dest="pool_command", metavar="ACTION")
    approve = pool_sub.add_parser(
        "approve", help="approve a provider with a registry revision guard"
    )
    approve.add_argument("--provider-id", required=True)
    approve.add_argument("--expected-revision", type=int, required=True)
    approve.add_argument("--approved-by", default="user")
    approve.add_argument("--data-dir", default=None, help="data directory")
    approve.set_defaults(handler=_handle_pool_approve)

    control_status = pool_sub.add_parser(
        "control-status",
        help="show sanitized pool/service state (no keys, no full upstream responses)",
    )
    control_status.add_argument("--data-dir", default=None, help="data directory")
    control_status.set_defaults(handler=_handle_pool_control_status)

    suspend_pool = pool_sub.add_parser(
        "suspend",
        help="suspend a provider from the production pool (requires --confirm)",
    )
    suspend_pool.add_argument("--provider-id", required=True)
    suspend_pool.add_argument("--confirm", required=True, help="literal SUSPEND")
    suspend_pool.add_argument("--data-dir", default=None, help="data directory")
    suspend_pool.set_defaults(handler=_handle_pool_suspend)

    stop_pool = pool_sub.add_parser(
        "stop",
        help="halt the production pool (requires --confirm STOP)",
    )
    stop_pool.add_argument("--confirm", required=True, help="literal STOP")
    stop_pool.add_argument("--data-dir", default=None, help="data directory")
    stop_pool.set_defaults(handler=_handle_pool_stop)

    pool.set_defaults(handler=_pool_usage)

    runtime = subparsers.add_parser("runtime", help="runtime store operations")
    runtime_sub = runtime.add_subparsers(dest="runtime_command", metavar="ACTION")
    status = runtime_sub.add_parser("status", help="list runtime providers (sanitized; distinct revisions)")
    status.add_argument("--data-dir", default=None, help="data directory")
    status.set_defaults(handler=_handle_runtime_status)
    runtime.set_defaults(handler=_runtime_usage)

    client_config = subparsers.add_parser(
        "client-config",
        help="generate client configuration files for Codex, OpenCode, and generic agents",
    )
    client_config_sub = client_config.add_subparsers(dest="client_config_command", metavar="ACTION")
    write = client_config_sub.add_parser(
        "write",
        help="write client configs from runtime state + pool readback",
    )
    write.add_argument("--output-dir", default=None, help="output directory (default: data_dir/clients)")
    write.add_argument("--data-dir", default=None, help="data directory")
    write.set_defaults(handler=_handle_client_config_write)
    client_config.set_defaults(handler=_client_config_usage)

    notifications = subparsers.add_parser("notifications", help="notification outbox operations")
    notifications_sub = notifications.add_subparsers(dest="notifications_command", metavar="ACTION")
    drain = notifications_sub.add_parser(
        "drain",
        help="deliver pending notifications; mark sent only after adapter acknowledgement",
    )
    drain.add_argument("--data-dir", default=None, help="data directory")
    drain.set_defaults(handler=_handle_notifications_drain)
    notifications.set_defaults(handler=_notifications_usage)


def _pool_usage(args) -> int:  # noqa: ANN001, ARG001
    print("usage: hunter pool approve|control-status|suspend|stop ...")
    print("  hunter pool approve     --provider-id ID --expected-revision N")
    print("  hunter pool control-status")
    print("  hunter pool suspend     --provider-id ID --confirm SUSPEND")
    print("  hunter pool stop        --confirm STOP")
    return 2


def _runtime_usage(args) -> int:  # noqa: ANN001, ARG001
    print("usage: hunter runtime status [--data-dir DIR]")
    return 2


def _notifications_usage(args) -> int:  # noqa: ANN001, ARG001
    print("usage: hunter notifications drain [--data-dir DIR]")
    return 2


def _client_config_usage(args) -> int:  # noqa: ANN001, ARG001
    print("usage: hunter client-config write [--output-dir DIR] [--data-dir DIR]")
    return 2


def load_cli_imports() -> None:
    """Import hook kept for hunter.cli compatibility."""
    return None


__all__ = ["load_cli_imports", "register_stage_two_parsers"]
