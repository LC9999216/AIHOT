"""Run discovery adapters (TASK-005/006).

Usage:
    python scripts/discover.py [--source github] [--data-dir DIR]
"""

from __future__ import annotations

import sys


def main(argv) -> int:
    from hunter.cli import main as cli_main

    args = ["discover"] + [a for a in argv[1:] if a]
    return cli_main(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
