from pathlib import Path
import tomllib


def test_stage2_extra_pins_freellmpool_0130() -> None:
    config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    stage2 = config["project"]["optional-dependencies"]["stage2"]
    assert "freellmpool==0.13.0" in stage2
