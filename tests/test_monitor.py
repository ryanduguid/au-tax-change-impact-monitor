from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from au_tax_change_impact_monitor.errors import MonitorError
from au_tax_change_impact_monitor.monitor import _https_url, _iso_date, _iso_timestamp, _load_observation, compare, render_markdown, validate_review, write_queue
from au_tax_change_impact_monitor.util import sample_path


ROOT = Path(__file__).resolve().parents[1]


def _queue():
    return compare(
        baseline_path=sample_path("baseline", "sample-sources.json"),
        observation_path=sample_path("observations", "sample-register-observation.json"),
        mapping_path=sample_path("mappings", "sample-source-skill-map.json"),
    )


def _payload(kind: str, name: str) -> dict:
    return json.loads(sample_path(kind, name).read_text(encoding="utf-8"))


def _compare_fixtures(tmp_path: Path, **mutated: dict) -> dict:
    """Run compare() over the shipped samples with any of the three replaced.

    Every classification rule below needs a fixture the shipped demo does not
    contain, so each case starts from the real samples and changes one thing.
    """
    defaults = {
        "baseline": ("baseline", "sample-sources.json"),
        "observation": ("observations", "sample-register-observation.json"),
        "mapping": ("mappings", "sample-source-skill-map.json"),
    }
    paths = {}
    for name, parts in defaults.items():
        payload = mutated[name] if name in mutated else _payload(*parts)
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        paths[name] = path
    return compare(baseline_path=paths["baseline"], observation_path=paths["observation"], mapping_path=paths["mapping"])


def _only(queue: dict, change_kind: str) -> dict:
    matches = [item for item in queue["items"] if item["change_kind"] == change_kind]
    assert len(matches) == 1, f"expected one {change_kind} item, got {[item['change_kind'] for item in queue['items']]}"
    return matches[0]


def _clear_compilation(entry: dict) -> dict:
    for field in ("observed_compilation_number", "observed_compilation_date", "observed_register_document_id"):
        entry[field] = None
    return entry


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


def test_incomplete_scope_blocks_even_when_every_observed_source_is_unchanged(tmp_path: Path) -> None:
    # The one conclusion this tool must never draw: "no change" from a scope
    # that was never fully observed.
    observation = _payload("observations", "sample-register-observation.json")
    observation["complete"] = False
    for entry in observation["observations"]:
        entry["state"] = "UNCHANGED"
        _clear_compilation(entry)

    queue = _compare_fixtures(tmp_path, observation=observation)

    assert queue["run_status"] == "BLOCKED"
    assert len(queue["items"]) == 1
    item = _only(queue, "INCOMPLETE_SCOPE")
    assert item["state"] == "BLOCKED"
    assert item["mapping_status"] == "NOT_EVALUATED"


def test_a_baseline_title_with_no_observation_is_blocked_not_dropped(tmp_path: Path) -> None:
    observation = _payload("observations", "sample-register-observation.json")
    observation["complete"] = False
    observation["observations"] = [entry for entry in observation["observations"] if entry["register_id"] != "F2099L00001"]

    queue = _compare_fixtures(tmp_path, observation=observation)

    missing = _only(queue, "MISSING_OBSERVATION")
    assert missing["state"] == "BLOCKED"
    assert missing["source"]["register_id"] == "F2099L00001"
    assert missing["mapping_status"] == "NOT_EVALUATED"
    assert queue["run_status"] == "BLOCKED"


@pytest.mark.parametrize(
    ("state", "extra"),
    [
        ("CURRENT_NO_PUBLISHED_COMPILATION", {"current_version_start": "2099-09-01"}),
        ("LOOKUP_FAILED", {"error_category": "register_unavailable"}),
    ],
)
def test_an_unresolved_register_state_is_blocked_not_open(tmp_path: Path, state: str, extra: dict) -> None:
    observation = _payload("observations", "sample-register-observation.json")
    entry = _clear_compilation(observation["observations"][0])
    entry["state"] = state
    entry.update(extra)

    queue = _compare_fixtures(tmp_path, observation=observation)

    item = _only(queue, state)
    assert item["state"] == "BLOCKED"
    assert queue["run_status"] == "BLOCKED"


def test_a_title_no_longer_in_force_is_open_for_human_review(tmp_path: Path) -> None:
    observation = _payload("observations", "sample-register-observation.json")
    entry = _clear_compilation(observation["observations"][0])
    entry["state"] = "NO_LONGER_IN_FORCE"

    queue = _compare_fixtures(tmp_path, observation=observation)

    item = _only(queue, "NO_LONGER_IN_FORCE")
    assert item["state"] == "OPEN"
    assert item["source"]["observed_compilation"] is None
    assert queue["run_status"] == "REVIEW_REQUIRED"


def test_a_stale_baseline_row_blocks_even_an_unchanged_observation(tmp_path: Path) -> None:
    # A baseline entry that is not itself the current version cannot support a
    # currency conclusion, so the UNCHANGED short-circuit must not swallow it.
    baseline = _payload("baseline", "sample-sources.json")
    baseline["titles"][1]["version_is_current"] = False

    queue = _compare_fixtures(tmp_path, baseline=baseline)

    item = _only(queue, "BASELINE_NOT_CURRENT")
    assert item["state"] == "BLOCKED"
    assert item["source"]["register_id"] == "F2099L00001"
    assert queue["run_status"] == "BLOCKED"


def test_a_mapping_for_another_collection_does_not_map_the_source(tmp_path: Path) -> None:
    # Mapping is by exact (register_id, collection). A register ID match alone
    # must leave the changed source visible as UNMAPPED_SOURCE.
    mapping = _payload("mappings", "sample-source-skill-map.json")
    mapping["entries"][0]["collection"] = "LegislativeInstrument"

    queue = _compare_fixtures(tmp_path, mapping=mapping)

    item = _only(queue, "SUPERSEDED")
    assert item["mapping_status"] == "UNMAPPED_SOURCE"
    assert item["impact_candidates"] == []
    assert item["state"] == "OPEN"


def test_an_empty_map_leaves_the_changed_source_visible(tmp_path: Path) -> None:
    mapping = _payload("mappings", "sample-source-skill-map.json")
    mapping["entries"] = []

    queue = _compare_fixtures(tmp_path, mapping=mapping)

    item = _only(queue, "SUPERSEDED")
    assert item["mapping_status"] == "UNMAPPED_SOURCE"
    assert item["impact_candidates"] == []
    assert queue["run_status"] == "REVIEW_REQUIRED"


def test_an_exactly_mapped_source_carries_the_review_question(tmp_path: Path) -> None:
    queue = _compare_fixtures(tmp_path)

    item = _only(queue, "SUPERSEDED")
    assert item["mapping_status"] == "MAPPED"
    assert [candidate["mapping_id"] for candidate in item["impact_candidates"]] == ["map:sample-consumption-tax-to-bas"]
    assert item["impact_candidates"][0]["skill_ref"] == "bas-preparation"


def test_an_observation_collection_that_disagrees_with_the_baseline_is_rejected(tmp_path: Path) -> None:
    observation = _payload("observations", "sample-register-observation.json")
    observation["observations"][0]["collection"] = "LegislativeInstrument"

    with pytest.raises(MonitorError, match="collection does not match baseline"):
        _compare_fixtures(tmp_path, observation=observation)


@pytest.mark.parametrize("field", ["observed_compilation_number", "observed_compilation_date", "observed_register_document_id"])
def test_a_superseded_observation_without_compilation_detail_is_rejected(tmp_path: Path, field: str) -> None:
    # Without this guard the item renders as "Observed compilation: None dated None".
    observation = _payload("observations", "sample-register-observation.json")
    observation["observations"][0][field] = None

    with pytest.raises(MonitorError, match=field):
        _compare_fixtures(tmp_path, observation=observation)


def test_a_current_title_with_no_compilation_cannot_carry_a_document_id(tmp_path: Path) -> None:
    observation = _payload("observations", "sample-register-observation.json")
    entry = observation["observations"][0]
    entry["state"] = "CURRENT_NO_PUBLISHED_COMPILATION"
    entry["current_version_start"] = "2099-09-01"

    with pytest.raises(MonitorError, match="null observed_register_document_id"):
        _compare_fixtures(tmp_path, observation=observation)


def test_a_failed_lookup_must_name_its_error_category(tmp_path: Path) -> None:
    observation = _payload("observations", "sample-register-observation.json")
    entry = _clear_compilation(observation["observations"][0])
    entry["state"] = "LOOKUP_FAILED"

    with pytest.raises(MonitorError, match="error_category"):
        _compare_fixtures(tmp_path, observation=observation)


def test_a_map_reusing_a_mapping_id_is_rejected(tmp_path: Path) -> None:
    mapping = _payload("mappings", "sample-source-skill-map.json")
    mapping["entries"].append(dict(mapping["entries"][0]))

    with pytest.raises(MonitorError, match="duplicate mapping IDs"):
        _compare_fixtures(tmp_path, mapping=mapping)


def test_a_baseline_with_no_titles_is_rejected(tmp_path: Path) -> None:
    baseline = _payload("baseline", "sample-sources.json")
    baseline["titles"] = []

    with pytest.raises(MonitorError, match="no titles"):
        _compare_fixtures(tmp_path, baseline=baseline)


def test_an_observation_scope_narrower_than_the_baseline_is_rejected(tmp_path: Path) -> None:
    # Without the equality check a scope mismatch becomes a queue full of
    # MISSING_OBSERVATION noise instead of a hard stop.
    observation = _payload("observations", "sample-register-observation.json")
    observation["expected_register_ids"] = ["C2099A00001"]
    observation["complete"] = False
    observation["observations"] = observation["observations"][:1]

    with pytest.raises(MonitorError, match="exactly match the baseline scope"):
        _compare_fixtures(tmp_path, observation=observation)


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


@pytest.mark.parametrize("value", ["2026-08-08T10:00:00", "2026-08-08 10:00:00", "2026-08-08T10:00:00.123456"])
def test_timestamps_require_an_explicit_timezone(value: str) -> None:
    with pytest.raises(MonitorError, match="explicit UTC offset"):
        _iso_timestamp(value, field="reviewed_at")


@pytest.mark.parametrize("value", ["2026-08-08", "2026-08-08Z", "2026-08-08+10:00", "2026-08-08-05:00"])
def test_a_date_alone_is_never_a_timestamp_however_it_is_qualified(value: str) -> None:
    """The clock is mandatory, and an offset does not substitute for one.

    A date-only value silently means midnight, so accepting one would let a
    review recorded earlier the same day as the observation pass the "cannot
    predate the observation" check. The first pattern written for this grammar
    made the clock and the offset independently optional, which accepted the
    last three values here - none of which any supported interpreter accepted
    before the pattern existed.
    """
    with pytest.raises(MonitorError, match="ISO 8601 timestamp"):
        _iso_timestamp(value, field="observed_at")


@pytest.mark.parametrize(
    "value",
    [
        "20260808T000000Z",        # ISO basic form
        "2026-W32-6T00:00:00Z",    # week date
        "2026-08-08T00:00:00+00",  # bare-hour offset
    ],
)
def test_timestamp_grammar_does_not_depend_on_the_interpreter(value: str) -> None:
    """Each of these parses on Python 3.11+ and raises on the declared 3.10 floor.

    Verified against origin/main's fromisoformat-based helper on 3.10.20 and
    3.12.10. datetime.fromisoformat decides the answer, so without an explicit
    pattern a queue written on one supported interpreter cannot be re-validated
    on another - which is the whole point of a replayable provenance artefact.
    """
    with pytest.raises(MonitorError, match="ISO 8601 timestamp"):
        _iso_timestamp(value, field="observed_at")


def test_a_lowercase_zulu_offset_is_refused_on_every_interpreter() -> None:
    """Not an interpreter divergence: no supported version accepts this.

    datetime.fromisoformat rejects "...00:00:00z" on 3.10, 3.11, 3.12 and 3.13
    alike - only an uppercase "Z" is normalised - so refusing it removes no
    divergence and simply keeps the documented grammar. It is pinned here
    rather than alongside the 3.11-only forms so the distinction stays honest.
    """
    with pytest.raises(MonitorError, match="ISO 8601 timestamp"):
        _iso_timestamp("2026-08-08T00:00:00z", field="observed_at")


@pytest.mark.parametrize("value", ["2026-08-08X00:00:00Z", "2026-08-08/00:00:00Z"])
def test_a_separator_outside_the_documented_grammar_is_refused(value: str) -> None:
    """Unlike the 3.11-only forms above, these parsed on 3.10 and 3.12 alike.

    fromisoformat took any single character as the date/time separator, so
    refusing them narrows the accepted set on every supported interpreter
    rather than removing a divergence. That is a deliberate choice, kept
    because neither ISO 8601 nor RFC 3339 admits an arbitrary separator, and
    README states the resulting grammar. "T", "t" and a space are still
    accepted, so an artefact stored under the old helper stays valid.
    """
    with pytest.raises(MonitorError, match="ISO 8601 timestamp"):
        _iso_timestamp(value, field="observed_at")


@pytest.mark.parametrize("value", ["20990701", "2099-W27-1", "2099-182"])
def test_iso_dates_reject_the_forms_only_newer_interpreters_accept(value: str) -> None:
    with pytest.raises(MonitorError, match="must be an ISO date"):
        _iso_date(value, field="compilation_date")


@pytest.mark.parametrize(
    "value",
    [
        "2026-08-08T00:00:00Z",
        "2026-08-08T00:00:00.5Z",
        "2026-08-08T00:00:00.123456+10:00",
        "2026-08-08 00:00:00+00:00",  # space separator, accepted on 3.10 and 3.12 before the pattern
        "2026-08-08t00:00:00Z",       # lowercase t, likewise
    ],
)
def test_the_accepted_timestamp_forms_are_the_same_on_every_supported_version(value: str) -> None:
    assert _iso_timestamp(value, field="observed_at") == value


def test_an_observation_stamped_with_a_date_and_an_offset_is_rejected_end_to_end(tmp_path: Path) -> None:
    # The unit case above, through the public entry point: with the clock
    # optional this reached the queue as observed_at "2026-08-08+10:00" and
    # compare() returned REVIEW_REQUIRED on it.
    observation = _payload("observations", "sample-register-observation.json")
    observation["observed_at"] = "2026-08-08+10:00"

    with pytest.raises(MonitorError, match="ISO 8601 timestamp"):
        _compare_fixtures(tmp_path, observation=observation)


def test_validate_review_parses_both_timestamps_with_the_pinned_grammar(tmp_path: Path) -> None:
    # A one-digit fractional second is accepted by fromisoformat on 3.11+ and
    # rejected on 3.10. The pinned pattern accepts 1 to 6 digits on every
    # supported version, so this is a case where the pattern is deliberately
    # WIDER than 3.10 rather than narrower: the answer stops moving with the
    # interpreter, which is the property that matters for a stored artefact.
    queue = _queue()
    queue["observation"]["observed_at"] = "2026-08-08T00:00:00.000000Z"
    queue_path = tmp_path / "queue.json"
    queue_path.write_text(json.dumps(queue), encoding="utf-8")
    decision = json.loads(sample_path("decisions", "sample-technical-review.json").read_text(encoding="utf-8"))
    decision["reviewed_at"] = "2026-08-09T00:00:00.5Z"
    decision_path = tmp_path / "decision.json"
    decision_path.write_text(json.dumps(decision), encoding="utf-8")

    validation = validate_review(queue_path=queue_path, decision_path=decision_path)

    assert validation["status"] == "DECISION_RECORDED"


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


def test_queue_item_with_a_list_valued_state_is_rejected_cleanly(tmp_path: Path) -> None:
    # Same trap as the observation loader: an unhashable state raises
    # TypeError from the set-membership test instead of MonitorError.
    queue = _queue()
    paths = write_queue(queue, tmp_path / "queue")
    payload = json.loads(paths["json"].read_text(encoding="utf-8"))
    payload["items"][0]["state"] = ["OPEN"]
    bad = tmp_path / "bad-queue.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MonitorError, match="unsupported state"):
        validate_review(
            queue_path=bad,
            decision_path=sample_path("decisions", "sample-technical-review.json"),
        )


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
    """Walk the AST rather than scanning text.

    A substring scan for "urllib.request" is blind to `from urllib import
    request`, so the package could hold a working network client while the
    assertion passed.
    """
    forbidden_roots = {
        "aiohttp",
        "asyncio",
        "ftplib",
        "http",
        "httpx",
        "mcp",
        "requests",
        "smtplib",
        "socket",
        "ssl",
        "telnetlib",
        "urllib3",
        "webbrowser",
    }
    allowed_urllib = {"urllib.parse"}

    for path in sorted((ROOT / "au_tax_change_impact_monitor").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # relative import, inside this package
                    continue
                base = node.module or ""
                imported = [f"{base}.{alias.name}" if base else alias.name for alias in node.names]
            else:
                continue
            for name in imported:
                root = name.split(".")[0]
                assert root not in forbidden_roots, f"{path.name} imports {name}"
                if root == "urllib":
                    assert name in allowed_urllib or name.startswith("urllib.parse."), (
                        f"{path.name} imports {name}"
                    )
                if root == "importlib":
                    # importlib.resources reads packaged sample data;
                    # importlib.import_module would load anything by name.
                    assert name.startswith("importlib.resources"), f"{path.name} imports {name}"

    # Second net: dynamic import by name would sidestep the walk above.
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in (ROOT / "au_tax_change_impact_monitor").glob("*.py")
    )
    for forbidden in ("__import__", "import_module"):
        assert forbidden not in source
