from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from pathlib import Path
from typing import Any

from .errors import MonitorError


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


def load_json_exact(path: Path, required: set[str], *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MonitorError(f"{label} does not exist: {path}.") from exc
    except json.JSONDecodeError as exc:
        raise MonitorError(f"{label} is not valid JSON: {path}.") from exc
    if not isinstance(payload, dict) or set(payload) != required:
        raise MonitorError(f"{label} must contain exactly: {', '.join(sorted(required))}.")
    return payload


def safe_markdown(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("[", "\\[").replace("]", "\\]").replace("\n", " ").replace("\r", " ")
