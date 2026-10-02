"""Import the pinned upstream v2.9.0 seed into Candidates (TASK-002).

Usage:
    python scripts/import_seed.py PATH_TO_SEED.json [DATA_DIR]
"""

from __future__ import annotations

import sys
from pathlib import Path


def main(argv) -> int:
    if len(argv) < 2:
        print("usage: python scripts/import_seed.py SEED.json [DATA_DIR]", file=sys.stderr)
        return 2
    from hunter.collectors.registry_seed import RegistrySeedImporter
    from hunter.config import load_settings
    from hunter.discovery.store import CandidateStore

    seed_path = Path(argv[1])
    data_dir = Path(argv[2]) if len(argv) > 2 else Path(load_settings()["paths"]["data_dir"])

    importer = RegistrySeedImporter()
    result = importer.load(seed_path)
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


if __name__ == "__main__":
    sys.exit(main(sys.argv))
