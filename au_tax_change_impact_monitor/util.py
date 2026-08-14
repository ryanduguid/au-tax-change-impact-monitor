from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

from .errors import MonitorError


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    """One immutable read of a JSON source and the digest of those exact bytes."""

    path: Path
    content: bytes
    sha256: str

    @classmethod
    def capture(cls, path: Path, *, label: str) -> SourceSnapshot:
        try:
            content = path.read_bytes()
        except FileNotFoundError as exc:
            raise MonitorError(f"{label} does not exist: {path}.") from exc
        except OSError as exc:
            raise MonitorError(f"{label} could not be read as UTF-8: {path}.") from exc
        return cls(path=path, content=content, sha256=hashlib.sha256(content).hexdigest())

    def text(self, *, label: str) -> str:
        try:
            return self.content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MonitorError(f"{label} could not be read as UTF-8: {self.path}.") from exc


def sample_path(*parts: str) -> Path:
    """Locate a shipped sample fixture inside the installed package.

    The samples ship as package data, so this resolves correctly for editable
    checkouts and plain ``pip install`` alike. The package installs as a real
    directory; zip imports are not supported.
    """
    resource = files(__package__)
    for part in ("samples", *parts):
        resource = resource.joinpath(part)
    return Path(str(resource))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def load_json_exact(
    path: Path | SourceSnapshot, required: set[str], *, label: str
) -> dict[str, Any]:
    snapshot = (
        path if isinstance(path, SourceSnapshot) else SourceSnapshot.capture(path, label=label)
    )
    source_path = snapshot.path
    try:
        payload = json.loads(snapshot.text(label=label))
    except json.JSONDecodeError as exc:
        raise MonitorError(f"{label} is not valid JSON: {source_path}.") from exc
    if not isinstance(payload, dict) or set(payload) != required:
        raise MonitorError(f"{label} must contain exactly: {', '.join(sorted(required))}.")
    return payload


def safe_markdown(value: str) -> str:
    return value.replace("\\", "\\\\").replace("`", "\\`").replace("|", "\\|").replace("[", "\\[").replace("]", "\\]").replace("\n", " ").replace("\r", " ")
