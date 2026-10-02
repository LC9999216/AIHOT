"""Command-line interface for Free Token Hunter.

This module is a thin argument-parsing shell. It contains no business logic;
it validates arguments and delegates to worker functions registered by later
tasks. Unknown commands fail with a non-zero exit code.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import __version__

# Subcommand registry: name -> (help, handler). Handlers are injected by the
# modules that implement them so that no business logic lives in the CLI.
_SUBCOMMANDS: Dict[str, "tuple[str, Callable[[argparse.Namespace], int]]"] = {}


def register_subcommand(
    name: str, help_text: str, handler: Callable[[argparse.Namespace], int]
) -> None:
    """Register a subcommand handler."""
    _SUBCOMMANDS[name] = (help_text, handler)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hunter",
        description="Free Token Hunter — discover and verify free AI/LLM API offers.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"hunter {__version__}",
        help="show the version and exit",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    version_parser = subparsers.add_parser("version", help="print the version")
    version_parser.set_defaults(handler=_handle_version)

    import_seed = subparsers.add_parser(
        "import-seed", help="import the pinned upstream seed into Candidates"
    )
    import_seed.add_argument("--input", required=True, help="path to the v2.9.0 seed JSON")
    import_seed.add_argument(
        "--data-dir",
        default=None,
        help="data directory (defaults to config paths.data_dir)",
    )
    import_seed.set_defaults(handler=_handle_import_seed)

    discover = subparsers.add_parser(
        "discover", help="run discovery adapters to create Candidates"
    )
    discover.add_argument(
        "--source",
        choices=["github", "all"],
        default="github",
        help="which discovery source to run (github, or all)",
    )
    discover.add_argument(
        "--data-dir",
        default=None,
        help="data directory (defaults to config paths.data_dir)",
    )
    discover.set_defaults(handler=_handle_discover)

    collect_evidence = subparsers.add_parser(
        "collect-evidence", help="resolve and fetch candidate evidence URLs"
    )
    collect_evidence.add_argument(
        "--candidate-id", default=None, help="limit to one candidate"
    )
    collect_evidence.add_argument(
        "--data-dir",
        default=None,
        help="data directory (defaults to config paths.data_dir)",
    )
    collect_evidence.add_argument(
        "--limit", type=int, default=50, help="max URLs to fetch this run"
    )
    collect_evidence.add_argument(
        "--max-pages-per-provider",
        type=int,
        default=6,
        help="max fetched pages per candidate (SSRF/size bound)",
    )
    collect_evidence.set_defaults(handler=_handle_collect_evidence)

    pipeline = subparsers.add_parser(
        "run-stage-one",
        help="run the deterministic offline stage-one pipeline (alias: pipeline)",
    )
    pipeline.add_argument(
        "--seed",
        default=None,
        help="seed fixture JSON path (pinned upstream dataset)",
    )
    pipeline.add_argument(
        "--as-of",
        default=None,
        help="explicit timezone-aware as_of (defaults to UTC now)",
    )
    pipeline.add_argument(
        "--data-dir",
        default=None,
        help="data directory (defaults to config paths.data_dir)",
    )
    pipeline.add_argument(
        "--require-llm",
        action="store_true",
        help="exit nonzero unless the real grounded LLM extractor is configured",
    )
    pipeline.set_defaults(handler=_handle_pipeline)

    alias = subparsers.add_parser("pipeline", help="alias for run-stage-one")
    alias.add_argument("--seed", default=None)
    alias.add_argument("--as-of", default=None)
    alias.add_argument("--data-dir", default=None)
    alias.add_argument("--require-llm", action="store_true")
    alias.set_defaults(handler=_handle_pipeline)

    # Stage 2 subcommands (nested parsers; handlers live in cli_imports and
    # delegate to hunter.runtime.stage2 workers — no business logic here).
    try:
        from .cli_imports import register_stage_two_parsers

        register_stage_two_parsers(subparsers)
    except ImportError:  # pragma: no cover - defensive
        pass

    for name, (help_text, handler) in sorted(_SUBCOMMANDS.items()):
        sub = subparsers.add_parser(name, help=help_text)
        sub.set_defaults(handler=handler)

    return parser


def _handle_discover(args: argparse.Namespace) -> int:
    from datetime import datetime, timezone

    from .collectors.github import GitHubCollector
    from .config import load_settings, load_sources
    from .discovery.orchestrator import DiscoveryOrchestrator, RunContext
    from .discovery.store import CandidateStore

    settings = load_settings()
    sources = load_sources()
    if args.data_dir:
        data_dir = Path(args.data_dir)
    else:
        data_dir = Path(settings["paths"]["data_dir"])
    store = CandidateStore(data_dir / "candidates.json")

    config = sources.get("discovery", {})
    github_cfg = config.get("github", {})
    queries = github_cfg.get("queries") or ["free llm api"]
    per_page = int(github_cfg.get("results_per_query", 10))
    max_total = int(github_cfg.get("max_total_results", 50))
    token = _env_or_none("GITHUB_TOKEN")

    collectors = {}
    if args.source in ("github", "all"):
        collectors["github"] = GitHubCollector(
            queries=queries, per_page=per_page, max_total=max_total, token=token
        )
    if args.source == "all":
        from .collectors.curated_repos import CuratedRepoCollector
        from .collectors.hackernews import HackerNewsCollector
        from .collectors.reddit import RedditCollector
        from .collectors.web_search import WebSearchCollector
        from .collectors.x_twitter import XCollector
        from .discovery.orchestrator import _domain_from_url

        class _HttpTransport:
            """Minimal real HTTP transport for the discovery adapters."""

            def get(self, url, params=None, headers=None):
                import urllib.parse
                import urllib.request

                if params:
                    url = url + "?" + urllib.parse.urlencode(params)
                request = urllib.request.Request(url, headers=headers or {})
                with urllib.request.urlopen(request, timeout=30) as response:
                    import json

                    return json.loads(response.read().decode("utf-8"))

            def get_text(self, url):
                with urllib.request.urlopen(url, timeout=30) as response:
                    return response.read().decode("utf-8")

        transport = _HttpTransport()
        curated_cfg = config.get("curated", {})
        collectors["curated"] = CuratedRepoCollector(
            transport=transport,
            sources=curated_cfg.get("sources", []),
        )
        hn_cfg = config.get("hackernews", {})
        collectors["hackernews"] = HackerNewsCollector(
            transport=transport,
            base_url=hn_cfg.get("base_url", "https://hn.algolia.com/api/v1/search"),
            max_results=int(hn_cfg.get("max_results", 20)),
        )
        reddit_cfg = config.get("reddit", {})
        collectors["reddit"] = RedditCollector(
            transport=transport,
            base_url=reddit_cfg.get("base_url", "https://www.reddit.com/search.json"),
            max_results=int(reddit_cfg.get("max_results", 25)),
            subreddit=reddit_cfg.get("subreddit") or None,
            enabled=bool(reddit_cfg.get("enabled", True)),
        )
        x_cfg = config.get("x", {})
        collectors["x"] = XCollector(
            transport=transport,
            bearer_token=_env_or_none("X_BEARER_TOKEN"),
            base_url=x_cfg.get(
                "base_url", "https://api.twitter.com/2/tweets/search/recent"
            ),
            max_results=int(x_cfg.get("max_results", 10)),
        )
        web_cfg = config.get("web_search", {})
        collectors["web_search"] = WebSearchCollector(
            transport=transport,
            api_key=_env_or_none("WEB_SEARCH_API_KEY"),
            base_url=web_cfg.get("base_url", "https://search.example/api"),
        )

    orch = DiscoveryOrchestrator(
        store=store, collectors=collectors, enabled=list(collectors)
    )
    ctx = RunContext(
        run_timestamp=datetime.now(timezone.utc).isoformat(),
        max_results_per_collector=max_total,
        credentials={"github_token": token},
        config=github_cfg,
    )
    summary = orch.run(ctx)
    for name, info in summary["collectors"].items():
        print(f"{name}: observations={info['observations']} errors={info['errors']} disabled={info['disabled']}")
    print(f"total_observations={summary['total_observations']} errors={summary['errors']}")
    store_info = summary.get("store", {})
    print(
        f"store: candidates_created={store_info.get('candidates_created', 0)} "
        f"observations_added={store_info.get('observations_added', 0)} "
        f"unchanged={store_info.get('unchanged', 0)}"
    )
    return 0


def _env_or_none(name: str):
    import os

    return os.environ.get(name) or None


def _handle_collect_evidence(args: argparse.Namespace) -> int:
    from .config import load_settings
    from .discovery.store import CandidateStore
    from .evidence.collect import collect_evidence
    from .evidence.fetcher import SafeFetcher
    from .evidence.store import EvidenceStore

    settings = load_settings()
    if args.data_dir:
        data_dir = Path(args.data_dir)
    else:
        data_dir = Path(settings["paths"]["data_dir"])
    candidate_store = CandidateStore(data_dir / "candidates.json")
    evidence_store = EvidenceStore(data_dir / "evidence.json")
    fetcher = SafeFetcher(max_pages_per_provider=args.max_pages_per_provider)
    report = collect_evidence(
        candidate_store,
        evidence_store,
        fetcher,
        candidate_id=args.candidate_id,
        limit=args.limit,
    )
    for line in report.as_lines():
        print(line)
    return 0


def _guess_source_type(url: str) -> str:
    if "pricing" in url or "plan" in url:
        return "pricing"
    if "api" in url or "developer" in url:
        return "api-docs"
    if "blog" in url or "news" in url:
        return "blog"
    if "docs" in url or "documentation" in url:
        return "docs"
    return "page"


def _plain_text_excerpt(text: str) -> str:
    import re

    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _handle_pipeline(args: argparse.Namespace) -> int:
    from datetime import datetime, timezone

    from .llm.client import LlmConfigurationError
    from .llm.runtime import build_grounded_extractor_from_env
    from .pipeline import Pipeline

    if args.data_dir:
        data_dir = Path(args.data_dir)
    else:
        settings = load_settings()
        data_dir = Path(settings["paths"]["data_dir"])
    try:
        extractor = build_grounded_extractor_from_env()
    except LlmConfigurationError as exc:
        print(f"hunter: {exc}", file=sys.stderr)
        return 2
    if args.require_llm and extractor is None:
        print(
            "hunter: a real grounded LLM extractor is required; configure "
            "HUNTER_LLM_BASE_URL, HUNTER_LLM_API_KEY, and HUNTER_LLM_MODEL",
            file=sys.stderr,
        )
        return 2
    pipeline = Pipeline(
        data_dir=data_dir,
        seed_path=Path(args.seed) if args.seed else None,
        as_of=datetime.fromisoformat(args.as_of) if args.as_of else datetime.now(timezone.utc),
        extractor=extractor,
    )
    summary = pipeline.run()
    for key in (
        "candidates_processed",
        "providers_created",
        "providers_updated",
        "free_confirmed",
        "uncertain",
        "not_free",
        "expired",
        "rejected",
        "unchanged",
        "errors",
    ):
        print(f"{key}={summary.get(key, 0)}")
    detail = summary.get("detail") or {}
    if detail:
        print(f"evidence_built={detail.get('evidence_built', 0)}")
        print(f"providers={detail.get('providers', 0)}")
        for outcome in detail.get("candidate_outcomes", []):
            print(
                "candidate_outcome="
                + json.dumps(outcome, sort_keys=True, separators=(",", ":"))
            )
    return 0


def _handle_version(args: argparse.Namespace) -> int:
    print(f"hunter {__version__}")
    return 0


def _handle_import_seed(args: argparse.Namespace) -> int:
    from .collectors.registry_seed import RegistrySeedImporter
    from .config import load_settings
    from .discovery.store import CandidateStore

    importer = RegistrySeedImporter()
    result = importer.load(Path(args.input))
    if args.data_dir:
        data_dir = Path(args.data_dir)
    else:
        settings = load_settings()
        data_dir = Path(settings["paths"]["data_dir"])
    store = CandidateStore(data_dir / "candidates.json")
    counts = store.ingest(result.entries)
    print(f"version={result.version} rejected={result.rejected}")
    print(
        f"candidates_created={counts.candidates_created} "
        f"candidates_merged={counts.candidates_merged} "
        f"observations_added={counts.observations_added} "
        f"unchanged={counts.unchanged} "
        f"rejected={counts.rejected} "
        f"errors={counts.errors}"
    )
    return 0


def _run_handler(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.error("no command given")
    return int(handler(args))


def _load_subcommands() -> None:
    """Import task modules that register their subcommands.

    Each module below calls :func:`register_subcommand` at import time. This
    indirection keeps argument parsing free of business logic and lets the
    console script degrade gracefully if an adapter import fails.
    """
    from .cli_imports import load_cli_imports

    load_cli_imports()


def main(argv: Optional[List[str]] = None) -> int:
    try:
        _load_subcommands()
    except Exception as exc:  # pragma: no cover - defensive
        print(f"hunter: failed to load subcommands: {exc}", file=sys.stderr)
        return 2

    parser = build_parser()
    args = parser.parse_args(argv)
    return _run_handler(parser, args)


if __name__ == "__main__":
    sys.exit(main())
