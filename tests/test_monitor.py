from __future__ import annotations

import json
from pathlib import Path

import pytest

from au_tax_change_impact_monitor.errors import MonitorError
from au_tax_change_impact_monitor.monitor import _https_url, _iso_timestamp, _load_observation, compare, render_markdown, validate_review, write_queue
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


def test_partial_observation_rejects_sources_outside_the_baseline(tmp_path: Path) -> None:
    payload = json.loads(sample_path("observations", "sample-register-observation.json").read_text(encoding="utf-8"))
    payload["complete"] = False
    extra = dict(payload["observations"][0])
    extra["register_id"] = "C2099A99999"
    payload["observations"].append(extra)
    bad = tmp_path / "out-of-scope.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MonitorError, match="outside the baseline scope"):
        _load_observation(bad, {"C2099A00001", "F2099L00001"})


def test_markdown_keeps_limits_visible_and_escapes_source_text() -> None:
    queue = _queue()
    queue["items"][0]["source"]["title"] = "Demo | [not a link]"
    markdown = render_markdown(queue)

    assert "This is a synthetic metadata-review queue" in markdown
    assert "Demo \\| \\[not a link\\]" in markdown
    assert "does not establish the legal effect" in markdown


def test_baseline_reusing_a_register_id_across_collections_is_rejected(tmp_path: Path) -> None:
    payload = json.loads(sample_path("baseline", "sample-sources.json").read_text(encoding="utf-8"))
    reused = dict(payload["titles"][0])
    reused["collection"] = "LegislativeInstrument"
    payload["titles"].append(reused)
    bad = tmp_path / "baseline.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MonitorError, match="duplicate register IDs"):
        compare(
            baseline_path=bad,
            observation_path=sample_path("observations", "sample-register-observation.json"),
            mapping_path=sample_path("mappings", "sample-source-skill-map.json"),
        )


def test_markdown_escapes_observed_compilation_metadata() -> None:
    queue = _queue()
    source = queue["items"][0]["source"]
    source["baseline_compilation"]["number"] = "1 | [not a link]"
    source["observed_compilation"]["number"] = "2`code"
    source["observed_compilation"]["document_id"] = "C2099C00002`injected"
    source["evidence_url"] = "https://example.test/C2099A00001`tick"

    markdown = render_markdown(queue)

    assert "1 \\| \\[not a link\\]" in markdown
    assert "2\\`code" in markdown
    assert "C2099C00002\\`injected" in markdown
    assert "https://example.test/C2099A00001\\`tick" in markdown


def test_control_characters_in_source_metadata_are_rejected(tmp_path: Path) -> None:
    payload = json.loads(sample_path("baseline", "sample-sources.json").read_text(encoding="utf-8"))
    payload["titles"][0]["compilation_number"] = "1\n## Injected heading"
    bad = tmp_path / "baseline.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MonitorError, match="control characters"):
        compare(
            baseline_path=bad,
            observation_path=sample_path("observations", "sample-register-observation.json"),
            mapping_path=sample_path("mappings", "sample-source-skill-map.json"),
        )


def test_queue_writes_and_human_decision_is_structurally_valid(tmp_path: Path) -> None:
    queue = _queue()
    paths = write_queue(queue, tmp_path / "test-output")
    validation = validate_review(
        queue_path=paths["json"],
        decision_path=sample_path("decisions", "sample-technical-review.json"),
    )

    assert validation["status"] == "DECISION_RECORDED"
    assert validation["decision_count"] == 1
    assert validation["mode"] == "synthetic"


def test_unknown_technical_decision_is_rejected(tmp_path: Path) -> None:
    queue = _queue()
    paths = write_queue(queue, tmp_path / "decision-test")
    payload = json.loads(sample_path("decisions", "sample-technical-review.json").read_text(encoding="utf-8"))
    payload["decisions"][0]["decision"] = "AUTO_UPDATE_SKILL"
    bad = tmp_path / "bad-decision.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MonitorError, match="allowlisted"):
        validate_review(queue_path=paths["json"], decision_path=bad)


def test_review_with_blank_reviewer_ref_is_rejected(tmp_path: Path) -> None:
    queue = _queue()
    paths = write_queue(queue, tmp_path / "queue")
    payload = json.loads(sample_path("decisions", "sample-technical-review.json").read_text(encoding="utf-8"))
    payload["reviewer_ref"] = "   "
    bad = tmp_path / "decision.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MonitorError, match="reviewer_ref"):
        validate_review(queue_path=paths["json"], decision_path=bad)


def test_review_with_a_non_timestamp_reviewed_at_is_rejected(tmp_path: Path) -> None:
    queue = _queue()
    paths = write_queue(queue, tmp_path / "queue")
    payload = json.loads(sample_path("decisions", "sample-technical-review.json").read_text(encoding="utf-8"))
    payload["reviewed_at"] = "last tuesday"
    bad = tmp_path / "decision.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MonitorError, match="ISO 8601 timestamp"):
        validate_review(queue_path=paths["json"], decision_path=bad)


def test_review_cannot_predate_the_observation(tmp_path: Path) -> None:
    queue = _queue()
    paths = write_queue(queue, tmp_path / "queue")
    payload = json.loads(sample_path("decisions", "sample-technical-review.json").read_text(encoding="utf-8"))
    payload["reviewed_at"] = "2026-08-07T23:59:59Z"
    bad = tmp_path / "decision.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MonitorError, match="cannot predate"):
        validate_review(queue_path=paths["json"], decision_path=bad)


def test_reviewed_at_accepts_utc_z_and_explicit_offsets() -> None:
    # The shipped sample uses a trailing Z, which datetime.fromisoformat only
    # accepts natively from Python 3.11; the helper must normalise it on 3.10.
    assert _iso_timestamp("2026-08-08T00:00:00Z", field="reviewed_at") == "2026-08-08T00:00:00Z"
    assert _iso_timestamp("2026-08-08T10:00:00+10:00", field="reviewed_at") == "2026-08-08T10:00:00+10:00"


@pytest.mark.parametrize("value", ["2026-08-08", "2026-08-08T10:00:00"])
def test_timestamps_require_an_explicit_timezone(value: str) -> None:
    with pytest.raises(MonitorError, match="explicit UTC offset"):
        _iso_timestamp(value, field="reviewed_at")


def test_partial_technical_review_remains_explicit(tmp_path: Path) -> None:
    queue = _queue()
    second = dict(queue["items"][0])
    second["item_id"] = "impact:second-open-item"
    queue["items"].append(second)
    queue_path = tmp_path / "queue.json"
    queue_path.write_text(json.dumps(queue), encoding="utf-8")

    validation = validate_review(
        queue_path=queue_path,
        decision_path=sample_path("decisions", "sample-technical-review.json"),
    )

    assert validation["status"] == "PARTIAL_DECISION_RECORDED"
    assert validation["undecided_count"] == 1


def test_observation_rejects_duplicate_expected_ids(tmp_path: Path) -> None:
    payload = json.loads(sample_path("observations", "sample-register-observation.json").read_text(encoding="utf-8"))
    payload["expected_register_ids"].append(payload["expected_register_ids"][0])
    bad = tmp_path / "observation.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MonitorError, match="must not contain duplicates"):
        _load_observation(bad, set(payload["expected_register_ids"]))


@pytest.mark.parametrize(
    "value",
    [
        "https://[malformed",
        "https://example.test:not-a-port/path",
        "https://example.test:65536/path",
        "https://exa mple.test/path",
        "https://example.test\\@other.test/path",
    ],
)
def test_https_urls_fail_with_a_domain_error_for_malformed_authorities(value: str) -> None:
    with pytest.raises(MonitorError, match="must be an https URL"):
        _https_url(value, field="evidence_url")


def test_observation_register_ids_with_non_string_entries_fail_cleanly(tmp_path: Path) -> None:
    payload = json.loads(sample_path("observations", "sample-register-observation.json").read_text(encoding="utf-8"))
    payload["expected_register_ids"] = [["C2099A00001"], "F2099L00001"]
    bad = tmp_path / "observation.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MonitorError, match="list of strings"):
        _load_observation(bad, {"C2099A00001", "F2099L00001"})


def test_observation_with_a_list_valued_state_is_rejected_cleanly(tmp_path: Path) -> None:
    # An unhashable state would raise TypeError from the set-membership test
    # instead of the clean MonitorError exit.
    payload = json.loads(sample_path("observations", "sample-register-observation.json").read_text(encoding="utf-8"))
    payload["observations"][0]["state"] = ["SUPERSEDED"]
    bad = tmp_path / "observation.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MonitorError, match="unsupported state"):
        _load_observation(bad, set(payload["expected_register_ids"]))


def test_non_synthetic_queue_cannot_be_validated(tmp_path: Path) -> None:
    queue = _queue()
    queue["mode"] = "live"
    bad = tmp_path / "live-queue.json"
    bad.write_text(json.dumps(queue), encoding="utf-8")

    with pytest.raises(MonitorError, match="Only synthetic impact queues"):
        validate_review(queue_path=bad, decision_path=sample_path("decisions", "sample-technical-review.json"))


def test_queue_with_duplicate_item_ids_is_rejected(tmp_path: Path) -> None:
    queue = _queue()
    queue["items"].append(dict(queue["items"][0]))
    bad = tmp_path / "duplicate-items.json"
    bad.write_text(json.dumps(queue), encoding="utf-8")

    with pytest.raises(MonitorError, match="duplicate item IDs"):
        validate_review(queue_path=bad, decision_path=sample_path("decisions", "sample-technical-review.json"))


def test_queue_state_and_run_status_must_be_consistent(tmp_path: Path) -> None:
    queue = _queue()
    queue["run_status"] = "NO_CHANGE_DETECTED"
    bad = tmp_path / "inconsistent-queue.json"
    bad.write_text(json.dumps(queue), encoding="utf-8")

    with pytest.raises(MonitorError, match="run_status does not match"):
        validate_review(queue_path=bad, decision_path=sample_path("decisions", "sample-technical-review.json"))


def test_decision_with_a_list_valued_decision_is_rejected_cleanly(tmp_path: Path) -> None:
    queue = _queue()
    paths = write_queue(queue, tmp_path / "queue")
    payload = json.loads(sample_path("decisions", "sample-technical-review.json").read_text(encoding="utf-8"))
    payload["decisions"][0]["decision"] = ["adopt"]
    bad = tmp_path / "decision.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MonitorError, match="not allowlisted"):
        validate_review(queue_path=paths["json"], decision_path=bad)


def test_queue_with_non_dict_items_is_rejected_without_a_traceback(tmp_path: Path) -> None:
    queue = _queue()
    queue["items"] = ["not-an-item"]
    bad = tmp_path / "queue.json"
    bad.write_text(json.dumps(queue), encoding="utf-8")

    with pytest.raises(MonitorError, match="invalid shape"):
        validate_review(queue_path=bad, decision_path=sample_path("decisions", "sample-technical-review.json"))


def test_decision_with_a_non_string_item_id_is_rejected(tmp_path: Path) -> None:
    queue = _queue()
    paths = write_queue(queue, tmp_path / "queue")
    payload = json.loads(sample_path("decisions", "sample-technical-review.json").read_text(encoding="utf-8"))
    payload["decisions"][0]["item_id"] = ["impact:64ea8458e99ade934803959f"]
    bad = tmp_path / "decision.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MonitorError, match="non-empty string"):
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
    # urllib.parse is a pure URL parser; reject modules that can perform I/O.
    for forbidden in ("requests", "urllib.request", "http.client", "socket", "mcp"):
        assert forbidden not in source
