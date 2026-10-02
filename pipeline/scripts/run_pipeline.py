"""Run the deterministic offline end-to-end pipeline (TASK-010).

Usage:
    python scripts/run_pipeline.py --seed tests/fixtures/upstream/free-llm-api-hub-v2.9.0.json \
        --as-of 2026-09-01T00:00:00+00:00 [--data-dir data]

Run twice with identical inputs: the Registry is byte-identical and no no-op
History events are appended.
"""

from __future__ import annotations

import sys


def main(argv) -> int:
    from hunter.cli import main as cli_main

    args = ["pipeline"] + [a for a in argv[1:] if a]
    return cli_main(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
