from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

from au_tax_change_impact_monitor.cli import main
from au_tax_change_impact_monitor.util import sample_path


def _compare_argv(out: Path) -> list[str]:
    return [
        "compare",
        "--baseline", str(sample_path("baseline", "sample-sources.json")),
        "--observation", str(sample_path("observations", "sample-register-observation.json")),
        "--map", str(sample_path("mappings", "sample-source-skill-map.json")),
        "--out", str(out),
    ]


def test_cli_compare_and_validate_review(tmp_path: Path) -> None:
    output = tmp_path / "cli-test"
    assert main(_compare_argv(output)) == 0
    validation_out = tmp_path / "validation" / "result.json"
    assert main([
        "validate-review",
        "--queue", str(output / "impact-queue.json"),
        "--decision", str(sample_path("decisions", "sample-technical-review.json")),
        "--out", str(validation_out),
    ]) == 0
    assert validation_out.is_file()


def test_a_non_ascii_output_path_does_not_fail_a_successful_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A redirected stdout on Windows is cp1252 with errors='strict'.

    Printing the output path used to raise UnicodeEncodeError and exit 1 after
    both queue files had already been written, so a scheduler keying off the
    exit status discarded a good run.
    """
    non_ascii = "M" + chr(0x0101) + "ori Trust"  # a-macron, outside cp1252
    output = tmp_path / non_ascii / "demo"
    try:
        output.mkdir(parents=True)
    except (OSError, UnicodeEncodeError) as exc:  # pragma: no cover - filesystem dependent
        pytest.skip(f"this filesystem cannot hold a non-ASCII path: {exc}")
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict", newline="")
    monkeypatch.setattr(sys, "stdout", stream)

    code = main(_compare_argv(output))

    stream.flush()
    assert code == 0
    assert (output / "impact-queue.json").is_file()
    assert b"M\\u0101ori Trust" in stream.buffer.getvalue()


def test_compare_with_a_file_where_the_output_directory_belongs_is_blocked(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    occupied = tmp_path / "not-a-directory"
    occupied.write_text("already here\n", encoding="utf-8")

    assert main(_compare_argv(occupied)) == 2
    assert "au-tax-change-impact-monitor: blocked:" in capsys.readouterr().err


def test_validate_review_with_an_unwritable_out_path_is_blocked(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    output = tmp_path / "queue"
    assert main(_compare_argv(output)) == 0
    occupied = tmp_path / "validation-out"
    occupied.mkdir()

    code = main([
        "validate-review",
        "--queue", str(output / "impact-queue.json"),
        "--decision", str(sample_path("decisions", "sample-technical-review.json")),
        "--out", str(occupied),
    ])

    assert code == 2
    assert "au-tax-change-impact-monitor: blocked:" in capsys.readouterr().err
