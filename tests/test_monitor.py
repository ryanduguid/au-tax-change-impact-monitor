from __future__ import annotations

import json
from pathlib import Path

import pytest

from au_tax_change_impact_monitor.errors import MonitorError
from au_tax_change_impact_monitor.monitor import _load_observation, compare, render_markdown, validate_review, write_queue
from au_tax_change_impact_monitor.util import sample_path


ROOT = Path(__file__).resolve().parents[1]


def _queue():
    return compare(
        baseline_path=sample_path("baseline", "sample-sources.json"),
        observation_path=sample_path("observations", "sample-register-observation.json"),
        mapping_path=sample_path("mappings", "sample-source-skill-map.json"),
    )


def test_superseded_source_creates_an_open_exactly_mapped_review_item() -> None:
    queue = _queue()

    assert queue["mode"] == "synthetic"
    assert queue["run_status"] == "REVIEW_REQUIRED"
    assert len(queue["items"]) == 1
    item = queue["items"][0]
    assert item["change_kind"] == "SUPERSEDED"
    assert item["state"] == "OPEN"
    assert item["mapping_status"] == "MAPPED"
    assert item["impact_candidates"][0]["mapping_basis"] == "exact_register_id_and_collection"


def test_complete_observation_rejects_missing_expected_source(tmp_path: Path) -> None:
    payload = json.loads(sample_path("observations", "sample-register-observation.json").read_text(encoding="utf-8"))
    payload["observations"] = payload["observations"][:1]
    bad = tmp_path / "incomplete.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MonitorError, match="cover every expected"):
        _load_observation(bad, {"C2099A00001", "F2099L00001"})


def test_markdown_keeps_limits_visible_and_escapes_source_text() -> None:
    queue = _queue()
    queue["items"][0]["source"]["title"] = "Demo | [not a link]"
    markdown = render_markdown(queue)

    assert "This is a synthetic metadata-review queue" in markdown
    assert "Demo \\| \\[not a link\\]" in markdown
    assert "does not establish the legal effect" in markdown


def test_queue_writes_and_human_decision_is_structurally_valid(tmp_path: Path) -> None:
    queue = _queue()
    paths = write_queue(queue, tmp_path / "test-output")
    validation = validate_review(
        queue_path=paths["json"],
        decision_path=sample_path("decisions", "sample-technical-review.json"),
    )

    assert validation["status"] == "DECISION_RECORDED"
    assert validation["decision_count"] == 1


def test_unknown_technical_decision_is_rejected(tmp_path: Path) -> None:
    queue = _queue()
    paths = write_queue(queue, tmp_path / "decision-test")
    payload = json.loads(sample_path("decisions", "sample-technical-review.json").read_text(encoding="utf-8"))
    payload["decisions"][0]["decision"] = "AUTO_UPDATE_SKILL"
    bad = tmp_path / "bad-decision.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MonitorError, match="allowlisted"):
        validate_review(queue_path=paths["json"], decision_path=bad)


def test_samples_resolve_from_the_installed_package() -> None:
    for parts in (
        ("baseline", "sample-sources.json"),
        ("observations", "sample-register-observation.json"),
        ("mappings", "sample-source-skill-map.json"),
        ("decisions", "sample-technical-review.json"),
    ):
        assert sample_path(*parts).is_file()


def test_decision_files_are_accepted_from_any_directory(tmp_path: Path) -> None:
    queue = _queue()
    paths = write_queue(queue, tmp_path / "queue")
    decision = tmp_path / "a-human-technical-review.json"
    decision.write_text(sample_path("decisions", "sample-technical-review.json").read_text(encoding="utf-8"), encoding="utf-8")

    validation = validate_review(queue_path=paths["json"], decision_path=decision)

    assert validation["status"] == "DECISION_RECORDED"


def test_outputs_write_relative_to_the_current_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    queue = _queue()

    paths = write_queue(queue, Path("build") / "relative-out")

    assert (tmp_path / "build" / "relative-out" / "impact-queue.json").is_file()
    assert paths["markdown"].read_text(encoding="utf-8").startswith("# AU Tax Change Impact Queue")


def test_v01_package_has_no_network_client_import() -> None:
    source = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "au_tax_change_impact_monitor").glob("*.py"))
    for forbidden in ("requests", "urllib", "http.client", "socket", "mcp"):
        assert forbidden not in source
