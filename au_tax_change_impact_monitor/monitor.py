from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .errors import MonitorError
from .util import load_json_exact, safe_markdown, sha256_file, sha256_json


OBSERVATION_STATES = {
    "UNCHANGED",
    "SUPERSEDED",
    "CURRENT_NO_PUBLISHED_COMPILATION",
    "NO_LONGER_IN_FORCE",
    "LOOKUP_FAILED",
}
ALLOWED_DECISIONS = {
    "AWAIT_PRIMARY_TEXT",
    "NO_WORKFLOW_CHANGE",
    "UPDATE_CANDIDATE",
    "ESCALATE_TECHNICAL_REVIEW",
}
# The one calendar-date and timestamp grammar this package accepts. ISO 8601
# extended forms only: no basic form, no week or ordinal dates, no bare-hour
# offset. See _parse_timestamp for why the grammar is pinned here.
DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
# The clock is mandatory: a timestamp orders an audit trail, and a date alone
# would silently mean midnight. An offset carried by a date alone does not make
# it one, so "2026-08-08+10:00" is rejected here rather than reaching the
# tzinfo check below and passing it.
TIMESTAMP_PATTERN = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2})"
    # "T", "t" and a space are the separators datetime.fromisoformat accepted
    # on every version this package supports, so an artefact already stored
    # with one stays valid. Any other separator is refused.
    r"[Tt ]"
    r"(?P<clock>\d{2}:\d{2}:\d{2})"
    r"(?P<fraction>\.\d{1,6})?"
    r"(?P<offset>Z|[+-]\d{2}:\d{2})?"
)


@dataclass(frozen=True)
class BaselineTitle:
    register_id: str
    collection: str
    name: str
    compilation_number: str
    compilation_date: str
    version_is_current: bool
    current_version_start: str | None
    source_url: str
    register_page: str


def _non_empty(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MonitorError(f"{field} must be a non-empty string.")
    text = value.strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise MonitorError(f"{field} must not contain control characters.")
    return text


def _iso_date(value: Any, *, field: str, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    text = _non_empty(value, field=field)
    # Match before parsing: date.fromisoformat accepts the ISO basic form
    # ("20990701") and week dates ("2099-W27-1") from Python 3.11 and rejects
    # both on the declared 3.10 floor, so delegating to it alone would let the
    # interpreter decide whether a stored artefact is valid.
    if DATE_PATTERN.fullmatch(text) is None:
        raise MonitorError(f"{field} must be an ISO date.")
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise MonitorError(f"{field} must be an ISO date.") from exc
    return text


def _parse_timestamp(text: str, *, field: str) -> datetime:
    """Parse the one timestamp grammar this package accepts.

    datetime.fromisoformat widened its grammar in Python 3.11: the basic form
    ("20260808T000000Z"), week dates, a bare-hour offset ("+00") and a
    lowercase "z" all parse there and raise on 3.10, which is inside this
    package's declared requires-python range and inside its own CI matrix.
    Delegating to it would make an artefact valid or invalid according to
    whichever interpreter the next reviewer happens to run. Matching
    TIMESTAMP_PATTERN first, then parsing with strptime, keeps the accepted set
    identical on every supported version.

    The pattern is deliberately no wider than the narrowest supported
    interpreter: everything it accepts, 3.10 accepted too. It is narrower in
    one respect that no version distinguished - fromisoformat took any single
    character as the date/time separator, and only "T", "t" and a space are
    accepted here. README documents the resulting grammar.
    """
    match = TIMESTAMP_PATTERN.fullmatch(text)
    if match is None:
        raise MonitorError(f"{field} must be an ISO 8601 timestamp.")
    stamp = f"{match['date']}T{match['clock']}"
    fmt = "%Y-%m-%dT%H:%M:%S"
    if match["fraction"] is not None:
        stamp += match["fraction"]
        fmt += ".%f"
    offset = match["offset"]
    if offset is not None:
        # strptime's %z takes an extended offset; Z is not one of its forms.
        stamp += "+00:00" if offset == "Z" else offset
        fmt += "%z"
    try:
        return datetime.strptime(stamp, fmt)
    except ValueError as exc:
        raise MonitorError(f"{field} must be an ISO 8601 timestamp.") from exc


def _iso_timestamp(value: Any, *, field: str) -> str:
    text = _non_empty(value, field=field)
    parsed = _parse_timestamp(text, field=field)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MonitorError(f"{field} must include an explicit UTC offset or Z.")
    return text


def _https_url(value: Any, *, field: str) -> str:
    text = _non_empty(value, field=field)
    try:
        parsed = urlsplit(text)
        hostname = parsed.hostname
        _ = parsed.port  # Force validation of a supplied port.
    except ValueError as exc:
        raise MonitorError(f"{field} must be an https URL.") from exc
    invalid_authority = (
        not hostname
        or parsed.username is not None
        or parsed.password is not None
        or "\\" in parsed.netloc
        or any(character.isspace() for character in parsed.netloc)
    )
    if parsed.scheme.lower() != "https" or invalid_authority:
        raise MonitorError(f"{field} must be an https URL.")
    return text


def _load_baseline(path: Path) -> tuple[list[BaselineTitle], dict[str, Any]]:
    raw = load_json_exact(path, {"corpus", "retrieved", "source", "source_api", "titles"}, label="baseline source index")
    if raw["source"] != "Federal Register of Legislation" or not isinstance(raw["titles"], list):
        raise MonitorError("Baseline must be a Federal Register title index with a titles list.")
    _iso_date(raw["retrieved"], field="baseline retrieved")
    _https_url(raw["source_api"], field="baseline source_api")
    titles: list[BaselineTitle] = []
    seen: set[str] = set()
    expected = {"register_id", "name", "collection", "compilation_number", "compilation_date", "version_is_current", "current_version_start", "retrieved", "source_url", "register_page"}
    for index, raw_title in enumerate(raw["titles"], start=1):
        if not isinstance(raw_title, dict) or set(raw_title) != expected:
            raise MonitorError(f"Baseline title {index} has an invalid shape.")
        title = BaselineTitle(
            register_id=_non_empty(raw_title["register_id"], field=f"title {index} register_id"),
            collection=_non_empty(raw_title["collection"], field=f"title {index} collection"),
            name=_non_empty(raw_title["name"], field=f"title {index} name"),
            compilation_number=_non_empty(raw_title["compilation_number"], field=f"title {index} compilation_number"),
            compilation_date=_iso_date(raw_title["compilation_date"], field=f"title {index} compilation_date") or "",
            version_is_current=raw_title["version_is_current"],
            current_version_start=_iso_date(raw_title["current_version_start"], field=f"title {index} current_version_start", nullable=True),
            source_url=_https_url(raw_title["source_url"], field=f"title {index} source_url"),
            register_page=_https_url(raw_title["register_page"], field=f"title {index} register_page"),
        )
        if not isinstance(title.version_is_current, bool):
            raise MonitorError(f"title {index} version_is_current must be a boolean.")
        if title.register_id in seen:
            raise MonitorError("Baseline source index contains duplicate register IDs.")
        seen.add(title.register_id)
        titles.append(title)
    if not titles:
        raise MonitorError("Baseline source index has no titles.")
    return titles, raw


def _load_observation(path: Path, expected_ids: set[str]) -> dict[str, Any]:
    raw = load_json_exact(path, {"schema_version", "mode", "observed_at", "expected_register_ids", "complete", "observations"}, label="Register observation")
    if raw["schema_version"] != "au-tax-register-observation.v1" or raw["mode"] != "synthetic":
        raise MonitorError("Only au-tax-register-observation.v1 in synthetic mode is supported.")
    _iso_timestamp(raw["observed_at"], field="observed_at")
    if not isinstance(raw["expected_register_ids"], list) or not all(isinstance(item, str) for item in raw["expected_register_ids"]):
        raise MonitorError("Observation expected_register_ids must be a list of strings.")
    cleaned_expected = [_non_empty(item, field="expected_register_ids item") for item in raw["expected_register_ids"]]
    if len(cleaned_expected) != len(set(cleaned_expected)):
        raise MonitorError("Observation expected_register_ids must not contain duplicates.")
    if set(cleaned_expected) != expected_ids:
        raise MonitorError("Observation expected_register_ids must exactly match the baseline scope.")
    if not isinstance(raw["complete"], bool) or not isinstance(raw["observations"], list):
        raise MonitorError("Observation complete/observations fields are invalid.")
    required = {"register_id", "collection", "state", "observed_compilation_number", "observed_compilation_date", "observed_register_document_id", "current_version_start", "evidence_url", "checked_at", "error_category"}
    seen: set[str] = set()
    for index, item in enumerate(raw["observations"], start=1):
        if not isinstance(item, dict) or set(item) != required:
            raise MonitorError(f"Observation item {index} has an invalid shape.")
        register_id = _non_empty(item["register_id"], field=f"observation {index} register_id")
        if register_id not in expected_ids:
            raise MonitorError("Observation contains a register ID outside the baseline scope.")
        if register_id in seen:
            raise MonitorError("Observation contains duplicate register IDs.")
        seen.add(register_id)
        _non_empty(item["collection"], field=f"observation {index} collection")
        # isinstance first: an unhashable value such as a list would raise
        # TypeError from the set-membership test instead of a clean error.
        if not isinstance(item["state"], str) or item["state"] not in OBSERVATION_STATES:
            raise MonitorError(f"Observation {index} has an unsupported state.")
        _https_url(item["evidence_url"], field=f"observation {index} evidence_url")
        _iso_timestamp(item["checked_at"], field=f"observation {index} checked_at")
        if item["state"] == "SUPERSEDED":
            for field in ("observed_compilation_number", "observed_compilation_date", "observed_register_document_id"):
                _non_empty(item[field], field=f"observation {index} {field}")
            _iso_date(item["observed_compilation_date"], field=f"observation {index} observed_compilation_date")
        elif item["state"] == "CURRENT_NO_PUBLISHED_COMPILATION":
            _iso_date(item["current_version_start"], field=f"observation {index} current_version_start")
            if item["observed_register_document_id"] is not None:
                raise MonitorError("CURRENT_NO_PUBLISHED_COMPILATION must have a null observed_register_document_id.")
        elif item["state"] == "LOOKUP_FAILED":
            _non_empty(item["error_category"], field=f"observation {index} error_category")
    if raw["complete"] and seen != expected_ids:
        raise MonitorError("A complete observation must cover every expected register ID exactly once.")
    return raw


def _load_mapping(path: Path) -> list[dict[str, str]]:
    raw = load_json_exact(path, {"schema_version", "mapping_version", "entries"}, label="source-to-skill map")
    if raw["schema_version"] != "au-tax-source-skill-map.v1" or not isinstance(raw["entries"], list):
        raise MonitorError("Source-to-skill map has an unsupported schema.")
    required = {"mapping_id", "register_id", "collection", "source_kind", "skill_ref", "skill_path", "owner_role", "review_question"}
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw["entries"], start=1):
        if not isinstance(item, dict) or set(item) != required:
            raise MonitorError(f"Mapping entry {index} has an invalid shape.")
        cleaned = {key: _non_empty(value, field=f"mapping {index} {key}") for key, value in item.items()}
        if cleaned["mapping_id"] in seen:
            raise MonitorError("Source-to-skill map contains duplicate mapping IDs.")
        seen.add(cleaned["mapping_id"])
        result.append(cleaned)
    return result


def _candidate(mapping: dict[str, str]) -> dict[str, str]:
    return {
        "mapping_id": mapping["mapping_id"],
        "skill_ref": mapping["skill_ref"],
        "owner_role": mapping["owner_role"],
        "review_question": mapping["review_question"],
        "mapping_basis": "exact_register_id_and_collection",
    }


def compare(*, baseline_path: Path, observation_path: Path, mapping_path: Path) -> dict[str, Any]:
    titles, baseline_raw = _load_baseline(baseline_path)
    expected_ids = {title.register_id for title in titles}
    observation = _load_observation(observation_path, expected_ids)
    mappings = _load_mapping(mapping_path)
    observations = {item["register_id"]: item for item in observation["observations"]}
    items: list[dict[str, Any]] = []
    if not observation["complete"]:
        items.append({
            "item_id": "impact:scope-incomplete",
            "state": "BLOCKED",
            "change_kind": "INCOMPLETE_SCOPE",
            "source": None,
            "impact_candidates": [],
            "mapping_status": "NOT_EVALUATED",
            "limitations": ["The observation scope is incomplete; no unchanged result can be relied on."],
        })
    for title in sorted(titles, key=lambda value: (value.register_id, value.collection)):
        observed = observations.get(title.register_id)
        if observed is None:
            items.append({
                "item_id": "impact:" + sha256_json({"missing": title.register_id, "collection": title.collection})[:24],
                "state": "BLOCKED",
                "change_kind": "MISSING_OBSERVATION",
                "source": {"register_id": title.register_id, "collection": title.collection, "title": title.name, "baseline_compilation": {"number": title.compilation_number, "date": title.compilation_date}, "observed_compilation": None, "evidence_url": title.register_page},
                "impact_candidates": [],
                "mapping_status": "NOT_EVALUATED",
                "limitations": ["The expected source was not observed; currency cannot be assessed."],
            })
            continue
        if observed["collection"] != title.collection:
            raise MonitorError(f"Observation collection does not match baseline for {title.register_id}.")
        if observed["state"] == "UNCHANGED" and title.version_is_current:
            continue
        state = "BLOCKED" if observed["state"] in {"CURRENT_NO_PUBLISHED_COMPILATION", "LOOKUP_FAILED"} or not title.version_is_current else "OPEN"
        applicable = [_candidate(item) for item in mappings if item["register_id"] == title.register_id and item["collection"] == title.collection]
        mapping_status = "MAPPED" if applicable else "UNMAPPED_SOURCE"
        observed_compilation = None
        if observed["state"] == "SUPERSEDED":
            observed_compilation = {"number": observed["observed_compilation_number"], "date": observed["observed_compilation_date"], "document_id": observed["observed_register_document_id"]}
        item_id = "impact:" + sha256_json({"baseline": sha256_file(baseline_path), "register_id": title.register_id, "state": observed["state"], "observation": sha256_file(observation_path)})[:24]
        items.append({
            "item_id": item_id,
            "state": state,
            "change_kind": observed["state"] if title.version_is_current else "BASELINE_NOT_CURRENT",
            "source": {
                "register_id": title.register_id,
                "collection": title.collection,
                "title": title.name,
                "baseline_compilation": {"number": title.compilation_number, "date": title.compilation_date},
                "observed_compilation": observed_compilation,
                "evidence_url": observed["evidence_url"],
            },
            "impact_candidates": applicable,
            "mapping_status": mapping_status,
            "limitations": [
                "A source-version state does not establish the legal effect of a change.",
                "This item is not tax advice and does not update any workflow.",
            ],
        })
    items.sort(key=lambda item: (item["state"] != "BLOCKED", item["change_kind"], item["item_id"]))
    if any(item["state"] == "BLOCKED" for item in items):
        run_status = "BLOCKED"
    elif items:
        run_status = "REVIEW_REQUIRED"
    else:
        run_status = "NO_CHANGE_DETECTED"
    run_id = "sha256:" + sha256_json({"baseline": sha256_file(baseline_path), "observation": sha256_file(observation_path), "mapping": sha256_file(mapping_path)})
    return {
        "schema_version": "au-tax-impact-queue.v1",
        "run_id": run_id,
        "mode": "synthetic",
        "run_status": run_status,
        "baseline": {"id": "sha256:" + sha256_file(baseline_path), "retrieved": baseline_raw["retrieved"], "source": baseline_raw["source"]},
        "observation": {"id": "sha256:" + sha256_file(observation_path), "observed_at": observation["observed_at"], "complete": observation["complete"]},
        "items": items,
    }


def render_markdown(queue: dict[str, Any]) -> str:
    lines = [
        "# AU Tax Change Impact Queue",
        "",
        f"**Run status: {queue['run_status']}**",
        "",
        "This is a synthetic metadata-review queue. It does not establish current law, legal effect, tax advice, a workflow update, or a client action.",
        "",
        f"- Baseline source: {safe_markdown(queue['baseline']['source'])}",
        f"- Baseline retrieved: {queue['baseline']['retrieved']}",
        f"- Observation complete: {queue['observation']['complete']}",
        "",
        "## Open items",
        "",
    ]
    if not queue["items"]:
        lines.append("No changed items were identified within the complete synthetic observation scope. This is not a statement about live law.")
    for item in queue["items"]:
        lines += [f"### {item['state']}: {item['change_kind']}", ""]
        source = item["source"]
        if source is not None:
            lines += [
                f"- Source: {safe_markdown(source['title'])} (`{safe_markdown(source['register_id'])}`, {safe_markdown(source['collection'])})",
                f"- Baseline compilation: {safe_markdown(source['baseline_compilation']['number'])} dated {source['baseline_compilation']['date']}",
                f"- Evidence: {safe_markdown(source['evidence_url'])}",
            ]
            if source["observed_compilation"]:
                observed = source["observed_compilation"]
                lines.append(f"- Observed compilation: {safe_markdown(observed['number'])} dated {observed['date']} (`{safe_markdown(observed['document_id'])}`)")
        lines.append(f"- Mapping status: {item['mapping_status']}")
        for candidate in item["impact_candidates"]:
            lines.append(f"- Review candidate: `{safe_markdown(candidate['skill_ref'])}` — {safe_markdown(candidate['review_question'])}")
        for limitation in item["limitations"]:
            lines.append(f"- Limitation: {safe_markdown(limitation)}")
        lines.append("")
    return "\n".join(lines)


def write_queue(queue: dict[str, Any], output_dir: Path) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "impact-queue.json"
    markdown_path = output_dir / "impact-queue.md"
    json_path.write_text(json.dumps(queue, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(queue), encoding="utf-8")
    return {"json": json_path, "markdown": markdown_path}


def validate_review(*, queue_path: Path, decision_path: Path) -> dict[str, Any]:
    queue = load_json_exact(queue_path, {"schema_version", "run_id", "mode", "run_status", "baseline", "observation", "items"}, label="impact queue")
    decision = load_json_exact(decision_path, {"schema_version", "run_id", "reviewer_ref", "reviewed_at", "decisions"}, label="technical review decision")
    if queue["schema_version"] != "au-tax-impact-queue.v1" or decision["schema_version"] != "au-tax-technical-review.v1":
        raise MonitorError("Queue or decision schema version is unsupported.")
    if queue["mode"] != "synthetic":
        raise MonitorError("Only synthetic impact queues can be validated.")
    if queue["run_id"] != decision["run_id"]:
        raise MonitorError("Technical review decision must refer to the exact queue run_id.")
    _non_empty(decision["reviewer_ref"], field="technical review reviewer_ref")
    reviewed_at = _iso_timestamp(decision["reviewed_at"], field="technical review reviewed_at")
    if not isinstance(queue["observation"], dict) or set(queue["observation"]) != {"id", "observed_at", "complete"}:
        raise MonitorError("Impact queue observation has an invalid shape.")
    observed_at = _iso_timestamp(queue["observation"]["observed_at"], field="impact queue observed_at")
    # Same parser as the validation above: a second fromisoformat call here
    # would reintroduce the interpreter-dependent grammar it just removed.
    reviewed_value = _parse_timestamp(reviewed_at, field="technical review reviewed_at")
    observed_value = _parse_timestamp(observed_at, field="impact queue observed_at")
    try:
        review_predates_observation = reviewed_value < observed_value
    except TypeError as exc:
        raise MonitorError("Review and observation timestamps must use compatible timezone qualifiers.") from exc
    if review_predates_observation:
        raise MonitorError("Technical review reviewed_at cannot predate the queue observation.")
    if not isinstance(queue["items"], list):
        raise MonitorError("Impact queue items must be a list.")
    open_items: set[str] = set()
    queue_item_ids: set[str] = set()
    for index, item in enumerate(queue["items"], start=1):
        if not isinstance(item, dict) or "item_id" not in item or "state" not in item:
            raise MonitorError(f"Impact queue item {index} has an invalid shape.")
        item_id = _non_empty(item["item_id"], field=f"impact queue item {index} item_id")
        if item_id in queue_item_ids:
            raise MonitorError("Impact queue contains duplicate item IDs.")
        queue_item_ids.add(item_id)
        # isinstance first: an unhashable value such as a list would raise
        # TypeError from the set membership test instead of MonitorError.
        if not isinstance(item["state"], str) or item["state"] not in {"OPEN", "BLOCKED"}:
            raise MonitorError(f"Impact queue item {index} has an unsupported state.")
        if item["state"] == "OPEN":
            open_items.add(item_id)
    if any(item["state"] == "BLOCKED" for item in queue["items"]):
        expected_run_status = "BLOCKED"
    elif queue["items"]:
        expected_run_status = "REVIEW_REQUIRED"
    else:
        expected_run_status = "NO_CHANGE_DETECTED"
    if queue["run_status"] != expected_run_status:
        raise MonitorError("Impact queue run_status does not match its items.")
    if not isinstance(decision["decisions"], list) or not decision["decisions"]:
        raise MonitorError("Technical review decision must include at least one decision.")
    seen: set[str] = set()
    for item in decision["decisions"]:
        if not isinstance(item, dict) or set(item) != {"item_id", "decision", "rationale", "evidence_note"}:
            raise MonitorError("Each technical decision must contain exactly item_id, decision, rationale, and evidence_note.")
        item_id = _non_empty(item["item_id"], field="technical decision item_id")
        if item_id not in open_items or item_id in seen:
            raise MonitorError("Technical decision references an unknown, blocked, or duplicate item.")
        # isinstance first: an unhashable value such as a list would raise
        # TypeError from the set-membership test instead of a clean error.
        if not isinstance(item["decision"], str) or item["decision"] not in ALLOWED_DECISIONS:
            raise MonitorError("Technical decision is not allowlisted.")
        _non_empty(item["rationale"], field="technical decision rationale")
        _non_empty(item["evidence_note"], field="technical decision evidence_note")
        seen.add(item_id)
    undecided_count = len(open_items - seen)
    status = "DECISION_RECORDED" if undecided_count == 0 else "PARTIAL_DECISION_RECORDED"
    return {"schema_version": "au-tax-review-decision-validation.v1", "run_id": queue["run_id"], "mode": "synthetic", "status": status, "decision_count": len(seen), "undecided_count": undecided_count, "limitation": "Validation records structurally valid human decisions only; it does not establish legal effect, change a skill, notify anyone, or produce tax advice."}
