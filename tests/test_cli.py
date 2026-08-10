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


def test_a_non_ascii_output_path_does_not_fail_a_successful_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A redirected stdout on Windows is cp1252 with errors='strict'.

    Printing the output path used to raise UnicodeEncodeError and exit 1 after
    both queue files had already been written, so a scheduler keying off the
    exit status discarded a good run.

    The escaping is per line and stderr announces it. The stream's own error
    handler must be untouched afterwards: reconfiguring sys.stdout would hand
    every later writer in the process a different handler, and would hand this
    caller a backslash-escaped path with nothing to distinguish it from a real
    one.
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
    assert stream.errors == "strict"
    assert "backslash-escaped" in capsys.readouterr().err


def test_an_ascii_output_path_prints_verbatim_and_says_nothing_on_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The escaping note must not fire on the ordinary run; only the line that
    # cannot be encoded is touched.
    output = tmp_path / "plain"
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict", newline="")
    monkeypatch.setattr(sys, "stdout", stream)

    assert main(_compare_argv(output)) == 0

    stream.flush()
    assert str(output).encode("cp1252") in stream.buffer.getvalue()
    assert capsys.readouterr().err == ""


def test_a_malformed_command_line_is_argparse_not_a_blocked_line(capsys: pytest.CaptureFixture[str]) -> None:
    """argparse rejects this before main() ever runs, so it prints a usage block.

    README describes the two rejection shapes separately for this reason: the
    exit status is 2 either way, but only what the monitor itself rejects
    carries the "blocked:" prefix a caller might grep for.
    """
    with pytest.raises(SystemExit) as exit_info:
        main(["compare", "--baseline", "some-path.json"])

    assert exit_info.value.code == 2
    captured = capsys.readouterr().err
    assert captured.startswith("usage:")
    assert "blocked:" not in captured


def test_compare_with_a_file_where_the_output_directory_belongs_is_blocked(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    occupied = tmp_path / "not-a-directory"
    occupied.write_text("already here\n", encoding="utf-8")

    assert main(_compare_argv(occupied)) == 2
    assert "blocked: the output directory could not be written:" in capsys.readouterr().err


def test_validate_review_out_colliding_with_a_directory_is_blocked(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # A directory already sits where the validation JSON belongs. Named for
    # what it is: the file cannot be created, not that the location is
    # unwritable.
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
    assert "blocked: the validation output file could not be written:" in capsys.readouterr().err


def test_an_oserror_that_is_not_a_path_problem_is_not_labelled_as_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A BrokenPipeError from the summary print is not a bad --out path.

    The first version of the exit-code fix wrapped the whole command body in
    one `except OSError`, so any OSError raised after the files were written -
    a closed pipe on stdout, most obviously - was reported as "a file path
    could not be used" and exited 2 on a run whose output was complete.
    """
    class _ClosedPipe(io.TextIOBase):
        def write(self, text: str) -> int:
            raise BrokenPipeError(32, "broken pipe")

    output = tmp_path / "queue"
    monkeypatch.setattr(sys, "stdout", _ClosedPipe())

    with pytest.raises(BrokenPipeError):
        main(_compare_argv(output))

    assert (output / "impact-queue.json").is_file()
