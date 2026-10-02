"""Collect evidence (TASK-007).

Usage:
    python scripts/collect_evidence.py [--candidate-id ID] [--data-dir DIR] [--limit N]
"""

from __future__ import annotations

import sys


def main(argv) -> int:
    from hunter.cli import main as cli_main

    args = ["collect-evidence"] + [a for a in argv[1:] if a]
    return cli_main(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
