"""Deterministic, candidate-only Markdown compilation from verified ledger records."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .contracts import canonical
from .store import EvidenceStore


def compile_candidate(store: EvidenceStore, run_id: str, workspace: Path) -> Path:
    run = store.read_run(run_id)
    mode = run.get("mode", "mock")
    workspace = workspace.resolve()
    for name in ("sources", "wiki"):
        directory = workspace / name
        if directory.is_symlink():
            raise ValueError("Wiki directories cannot be symlinks")
        directory.mkdir(parents=True, exist_ok=True)
        if not directory.resolve().is_relative_to(workspace):
            raise ValueError("Wiki path escapes workspace")
    completed = [a for a in run["attempts"] if a["result"] is not None]
    if not completed:
        raise ValueError("No completed evidence to compile")
    evidence = canonical(run)
    evidence_hash = hashlib.sha256(evidence).hexdigest()
    # A changed snapshot creates a new version rather than overwriting a page.
    name = f"{run_id}-{evidence_hash[:12]}"
    source = workspace / "sources" / f"{name}.json"
    page = workspace / "wiki" / f"{name}.md"
    metadata = {
        "schema_version": "1.0", "id": name,
        "title": f"{mode} experiment: {run['spec']['id']}",
        "description": "Experiment observations; not a confirmed finding",
        "date": run["created_at"][:10], "tags": ["apertus-lab", mode],
        "review_status": "candidate", "freshness": "current", "mode": mode,
        "run_status": run.get("status", "offline_fixture"),
        "scope": {"model": run['spec']['target']['model'],
                  "languages": sorted({c['language'] for c in run['spec']['cases']})},
        "model_revision": run["spec"]["target"]["revision"],
        "source_attempts": [a["id"] for a in completed],
        "evidence_refs": [{"path": f"sources/{name}.json", "sha256": evidence_hash}],
        "last_verified": None,
    }
    # JSON flow values are valid YAML and safely quote untrusted metadata.
    header = "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in metadata.items())
    content = (f"---\n{header}\n---\n\n"
               f"This {mode} candidate is an observation, not a confirmed model finding.\n\n"
               "## Hypothesis\n\n" + json.dumps(run['spec']['hypothesis'], ensure_ascii=False) + "\n\n"
               "## Results\n\n" + "\n".join(
                   f"- {a['case_id']}: {a['result']['assessment']} (attempt {a['id']})[^run]"
                   for a in completed) + "\n\n"
               f"## Evidence\n\nFrozen run data.[^run]\n\n"
               f"[^run]: [Source snapshot](../sources/{name}.json)\n\n"
               "## Review\n\nIndependent live confirmation is required before any finding.\n")
    for path, data in ((source, evidence), (page, content.encode("utf-8"))):
        if path.is_symlink():
            raise ValueError("Refusing symlink output")
        if path.exists():
            if path.read_bytes() != data:
                raise ValueError("Refusing to overwrite edited knowledge")
        else:
            with path.open("xb") as output:
                output.write(data)
    return page


def check_sources(workspace: Path) -> list[dict]:
    """Read-only freshness check for compiler pages, usable with the full Wiki app."""
    workspace = workspace.resolve()
    records = []
    for page in sorted((workspace / "wiki").glob("run-*.md")):
        if page.is_symlink():
            raise ValueError("Refusing symlink page")
        metadata = {}
        try:
            header = page.read_text(encoding="utf-8").split("---", 2)[1]
            for line in header.strip().splitlines():
                key, value = line.split(":", 1)
                metadata[key] = json.loads(value)
            valid = bool(metadata.get("evidence_refs"))
            for ref in metadata.get("evidence_refs", []):
                relative = Path(ref["path"])
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("Invalid source path")
                source = workspace / relative
                if source.is_symlink() or not source.resolve().is_relative_to(workspace):
                    raise ValueError("Source escapes workspace")
                valid = valid and source.is_file() and hashlib.sha256(source.read_bytes()).hexdigest() == ref["sha256"]
            records.append({"page": page.name, "review_status": metadata.get("review_status"),
                            "scope": metadata.get("scope"), "freshness": "current" if valid else "stale"})
        except (ValueError, KeyError, IndexError, OSError):
            records.append({"page": page.name, "freshness": "stale", "reason": "invalid_metadata_or_source"})
    return records
