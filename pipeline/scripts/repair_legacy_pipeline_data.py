from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hunter.data_repair import repair_legacy_pipeline_data  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Quarantine providers and remove only legacy build_evidence records."
    )
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--expected-evidence-count", type=int, default=69)
    args = parser.parse_args()
    report = repair_legacy_pipeline_data(
        args.data_dir,
        args.backup_dir,
        datetime.fromisoformat(args.as_of),
        expected_evidence_count=args.expected_evidence_count,
    )
    print(
        json.dumps(
            {
                "backup_dir": str(report.backup_dir),
                "migrated_providers": report.migrated_providers,
                "removed_evidence_count": len(report.removed_evidence_ids),
                "removed_evidence_ids": report.removed_evidence_ids,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
