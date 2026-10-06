"""Translate BreachWeave snapshots without promoting their assertions."""
from __future__ import annotations

from typing import Any
from .contracts import fields, identifier, text


def normalize_snapshot(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    fields(snapshot, {"schema_version", "experiment_id", "memory", "ideas"})
    if snapshot["schema_version"] != "1.0":
        raise ValueError("Unsupported runtime snapshot version")
    experiment_id = identifier(snapshot["experiment_id"])
    events = []
    seen = set()
    for collection, kind in (("memory", "memory"), ("ideas", "idea")):
        if not isinstance(snapshot[collection], list):
            raise ValueError("Snapshot records must be lists")
        for item in snapshot[collection]:
            if not isinstance(item, dict):
                raise ValueError("Runtime record must be an object")
            original_id = identifier(item["id"])
            if (kind, original_id) in seen:
                raise ValueError("Duplicate runtime record")
            seen.add((kind, original_id))
            content = text(item["content"], "content")
            if kind == "memory" and item.get("challengeId") != experiment_id:
                raise ValueError("Memory belongs to a different scope")
            refs = item.get("refs", [])
            if not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs):
                raise ValueError("Runtime refs must be strings")
            events.append({
                "schema_version": "1.0", "experiment_id": experiment_id,
                "source_id": original_id, "source_kind": kind,
                "content": content, "unverified_refs": refs,
                "upstream_status": item.get("status", item.get("kind")),
                "review_status": "candidate",
            })
    return events
