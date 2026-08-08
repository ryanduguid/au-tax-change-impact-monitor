from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .errors import MonitorError
from .util import canonical_json, load_json_exact, safe_markdown, sha256_file, sha256_json


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
    return value.strip()


def _iso_date(value: Any, *, field: str, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    text = _non_empty(value, field=field)
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise MonitorError(f"{field} must be an ISO date.") from exc
    return text


def _https_url(value: Any, *, field: str) -> str:
    text = _non_empty(value, field=field)
    if not text.startswith("https://"):
        raise MonitorError(f"{field} must be an https URL.")
    return text


def _load_baseline(path: Path) -> tuple[list[BaselineTitle], dict[str, Any]]:
    raw = load_json_exact(path, {"corpus", "retrieved", "source", "source_api", "titles"}, label="baseline source index")
    if raw["source"] != "Federal Register of Legislation" or not isinstance(raw["titles"], list):
        raise MonitorError("Baseline must be a Federal Register title index with a titles list.")
    _iso_date(raw["retrieved"], field="baseline retrieved")
    _https_url(raw["source_api"], field="baseline source_api")
    titles: list[BaselineTitle] = []
    seen: set[tuple[str, str]] = set()
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
        if (title.register_id, title.collection) in seen:
            raise MonitorError("Baseline source index contains duplicate register_id/collection pairs.")
        seen.add((title.register_id, title.collection))
        titles.append(title)
    if not titles:
        raise MonitorError("Baseline source index has no titles.")
    return titles, raw


def _load_observation(path: Path, expected_ids: set[str]) -> dict[str, Any]:
    raw = load_json_exact(path, {"schema_version", "mode", "observed_at", "expected_register_ids", "complete", "observations"}, label="Register observation")
    if raw["schema_version"] != "au-tax-register-observation.v1" or raw["mode"] != "synthetic":
        raise MonitorError("Only au-tax-register-observation.v1 in synthetic mode is supported.")
    _non_empty(raw["observed_at"], field="observed_at")
    if not isinstance(raw["expected_register_ids"], list) or set(raw["expected_register_ids"]) != expected_ids:
        raise MonitorError("Observation expected_register_ids must exactly match the baseline scope.")
    if not isinstance(raw["complete"], bool) or not isinstance(raw["observations"], list):
        raise MonitorError("Observation complete/observations fields are invalid.")
    required = {"register_id", "collection", "state", "observed_compilation_number", "observed_compilation_date", "observed_register_document_id", "current_version_start", "evidence_url", "checked_at", "error_category"}
    seen: set[str] = set()
    for index, item in enumerate(raw["observations"], start=1):
        if not isinstance(item, dict) or set(item) != required:
            raise MonitorError(f"Observation item {index} has an invalid shape.")
        register_id = _non_empty(item["register_id"], field=f"observation {index} register_id")
        if register_id in seen:
            raise MonitorError("Observation contains duplicate register IDs.")
        seen.add(register_id)
        _non_empty(item["collection"], field=f"observation {index} collection")
        if item["state"] not in OBSERVATION_STATES:
            raise MonitorError(f"Observation {index} has an unsupported state.")
        _https_url(item["evidence_url"], field=f"observation {index} evidence_url")
        _non_empty(item["checked_at"], field=f"observation {index} checked_at")
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
                f"- Baseline compilation: {source['baseline_compilation']['number']} dated {source['baseline_compilation']['date']}",
                f"- Evidence: {source['evidence_url']}",
            ]
            if source["observed_compilation"]:
                observed = source["observed_compilation"]
                lines.append(f"- Observed compilation: {observed['number']} dated {observed['date']} (`{observed['document_id']}`)")
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
    if queue["run_id"] != decision["run_id"]:
        raise MonitorError("Technical review decision must refer to the exact queue run_id.")
    open_items = {item["item_id"] for item in queue["items"] if item["state"] == "OPEN"}
    if not isinstance(decision["decisions"], list) or not decision["decisions"]:
        raise MonitorError("Technical review decision must include at least one decision.")
    seen: set[str] = set()
    for item in decision["decisions"]:
        if not isinstance(item, dict) or set(item) != {"item_id", "decision", "rationale", "evidence_note"}:
            raise MonitorError("Each technical decision must contain exactly item_id, decision, rationale, and evidence_note.")
        if item["item_id"] not in open_items or item["item_id"] in seen:
            raise MonitorError("Technical decision references an unknown, blocked, or duplicate item.")
        if item["decision"] not in ALLOWED_DECISIONS:
            raise MonitorError("Technical decision is not allowlisted.")
        _non_empty(item["rationale"], field="technical decision rationale")
        _non_empty(item["evidence_note"], field="technical decision evidence_note")
        seen.add(item["item_id"])
    return {"schema_version": "au-tax-review-decision-validation.v1", "run_id": queue["run_id"], "status": "DECISION_RECORDED", "decision_count": len(seen), "limitation": "Validation records a structurally complete human decision only; it does not establish legal effect, change a skill, notify anyone, or produce tax advice."}
