from __future__ import annotations

import json
from pathlib import Path

import pytest

from au_tax_change_impact_monitor.errors import MonitorError
from au_tax_change_impact_monitor.monitor import _load_observation, compare, render_markdown, validate_review, write_queue


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def _queue():
    return compare(
        baseline_path=SAMPLES / "baseline" / "sample-sources.json",
        observation_path=SAMPLES / "observations" / "sample-register-observation.json",
        mapping_path=SAMPLES / "mappings" / "sample-source-skill-map.json",
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
    payload = json.loads((SAMPLES / "observations" / "sample-register-observation.json").read_text(encoding="utf-8"))
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


def test_queue_writes_and_human_decision_is_structurally_valid() -> None:
    queue = _queue()
    paths = write_queue(queue, ROOT / "build" / "test-output")
    validation = validate_review(
        queue_path=paths["json"],
        decision_path=SAMPLES / "decisions" / "sample-technical-review.json",
    )

    assert validation["status"] == "DECISION_RECORDED"
    assert validation["decision_count"] == 1


def test_unknown_technical_decision_is_rejected(tmp_path: Path) -> None:
    queue = _queue()
    paths = write_queue(queue, ROOT / "build" / "decision-test")
    payload = json.loads((SAMPLES / "decisions" / "sample-technical-review.json").read_text(encoding="utf-8"))
    payload["decisions"][0]["decision"] = "AUTO_UPDATE_SKILL"
    bad = tmp_path / "bad-decision.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")

    from au_tax_change_impact_monitor.monitor import load_json_exact

    # The path boundary is part of the public command. This unit test exercises the decision rule directly.
    with pytest.raises(MonitorError, match="allowlisted"):
        # replace loader inside the module only for the decision fixture, without widening the production path policy
        import au_tax_change_impact_monitor.monitor as module

        original = module.path_within
        try:
            module.path_within = lambda path, parent, **kwargs: bad if path == bad else original(path, parent, **kwargs)
            validate_review(queue_path=paths["json"], decision_path=bad)
        finally:
            module.path_within = original


def test_v01_package_has_no_network_client_import() -> None:
    source = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "au_tax_change_impact_monitor").glob("*.py"))
    for forbidden in ("requests", "urllib", "http.client", "socket", "mcp"):
        assert forbidden not in source
