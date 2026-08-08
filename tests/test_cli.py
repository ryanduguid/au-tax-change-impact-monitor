from __future__ import annotations

from pathlib import Path

from au_tax_change_impact_monitor.cli import main


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def test_cli_compare_and_validate_review() -> None:
    output = ROOT / "build" / "cli-test"
    assert main([
        "compare",
        "--baseline", str(SAMPLES / "baseline" / "sample-sources.json"),
        "--observation", str(SAMPLES / "observations" / "sample-register-observation.json"),
        "--map", str(SAMPLES / "mappings" / "sample-source-skill-map.json"),
        "--out", str(output),
    ]) == 0
    assert main([
        "validate-review",
        "--queue", str(output / "impact-queue.json"),
        "--decision", str(SAMPLES / "decisions" / "sample-technical-review.json"),
    ]) == 0
