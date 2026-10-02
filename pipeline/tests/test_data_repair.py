from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hunter.data_repair import legacy_pipeline_evidence_ids, repair_legacy_pipeline_data


REPO_ROOT = Path(__file__).resolve().parents[1]
LEGACY_SNAPSHOT = REPO_ROOT / "data" / "repair-backup-20260911T000000Z"
AS_OF = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _copy_current_data(tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    for name in ("candidates.json", "evidence.json", "providers.json", "history.jsonl"):
        source_root = LEGACY_SNAPSHOT if LEGACY_SNAPSHOT.is_dir() else REPO_ROOT / "data"
        shutil.copy2(source_root / name, data_dir / name)
    return data_dir


def test_legacy_pipeline_ids_match_all_69_synthetic_records() -> None:
    ids = legacy_pipeline_evidence_ids(REPO_ROOT / "data" / "candidates.json", AS_OF)
    current = json.loads((LEGACY_SNAPSHOT / "evidence.json").read_text(encoding="utf-8"))
    assert len(ids) == 69
    assert ids == {item["evidence_id"] for item in current["items"]}


def test_repair_backs_up_data_migrates_providers_and_removes_only_exact_ids(
    tmp_path: Path,
) -> None:
    data_dir = _copy_current_data(tmp_path)
    backup_dir = tmp_path / "backup"

    report = repair_legacy_pipeline_data(data_dir, backup_dir, AS_OF, expected_evidence_count=69)

    assert report.backup_dir == backup_dir
    assert report.migrated_providers == ["assemblyai", "openrouter"]
    assert len(report.removed_evidence_ids) == 69
    assert {p.name for p in backup_dir.iterdir()} == {
        "candidates.json",
        "evidence.json",
        "providers.json",
        "history.jsonl",
    }

    providers = json.loads((data_dir / "providers.json").read_text(encoding="utf-8"))["items"]
    for provider in providers:
        assert provider["status"] == "UNCERTAIN"
        assert provider["evidence_ids"] == []
        assert provider["last_verified"] is None
        assert provider["verification_confidence"] is None
        assert provider["free_score"] is None
        assert provider["score_metadata"] is None

    evidence = json.loads((data_dir / "evidence.json").read_text(encoding="utf-8"))["items"]
    assert evidence == []
    assert (data_dir / "candidates.json").read_bytes() == (
        backup_dir / "candidates.json"
    ).read_bytes()

    history = [
        json.loads(line)
        for line in (data_dir / "history.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(history) == 4
    assert {event["provider_id"] for event in history[-2:]} == {"assemblyai", "openrouter"}
    assert all(event["revision"] == 2 for event in history[-2:])
    assert all(event["event_type"] == "provider.quarantined" for event in history[-2:])


def test_repair_rejects_unexpected_legacy_id_count_without_mutation(tmp_path: Path) -> None:
    data_dir = _copy_current_data(tmp_path)
    backup_dir = tmp_path / "backup"
    before = {name: (data_dir / name).read_bytes() for name in ("evidence.json", "providers.json")}

    with pytest.raises(ValueError, match="expected 68 legacy evidence IDs, found 69"):
        repair_legacy_pipeline_data(data_dir, backup_dir, AS_OF, expected_evidence_count=68)

    assert not backup_dir.exists()
    assert (data_dir / "evidence.json").read_bytes() == before["evidence.json"]
    assert (data_dir / "providers.json").read_bytes() == before["providers.json"]
