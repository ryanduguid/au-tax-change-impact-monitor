from __future__ import annotations

from pathlib import Path

from au_tax_change_impact_monitor.cli import main
from au_tax_change_impact_monitor.util import sample_path


def test_cli_compare_and_validate_review(tmp_path: Path) -> None:
    output = tmp_path / "cli-test"
    assert main([
        "compare",
        "--baseline", str(sample_path("baseline", "sample-sources.json")),
        "--observation", str(sample_path("observations", "sample-register-observation.json")),
        "--map", str(sample_path("mappings", "sample-source-skill-map.json")),
        "--out", str(output),
    ]) == 0
    validation_out = tmp_path / "validation" / "result.json"
    assert main([
        "validate-review",
        "--queue", str(output / "impact-queue.json"),
        "--decision", str(sample_path("decisions", "sample-technical-review.json")),
        "--out", str(validation_out),
    ]) == 0
    assert validation_out.is_file()
