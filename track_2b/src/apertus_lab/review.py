"""Human review boundary and official finding export, independent of Wiki labels."""
from __future__ import annotations

import json
from pathlib import Path
import uuid

from .contracts import canonical, digest, fields, identifier, text
from .live import confirmation_summary
from .live_store import LiveStore
from .store import now


def record_review(store: LiveStore, exploration_id: str, confirmation_id: str, review: dict) -> str:
    fields(review, {"reviewer", "human_reviewed", "title", "claim", "desired_behavior",
                    "impact", "novelty", "limitations", "scope", "scope_basis", "probe_ids",
                    "severity", "breadth", "breadth_justification", "topic_area"})
    for key in set(review) - {"human_reviewed", "probe_ids"}:
        text(review[key], key)
    if review["human_reviewed"] is not True:
        raise ValueError("Explicit human review attestation is required")
    if review["severity"] not in {"1", "2", "5", "10"} or review["breadth"] not in {"1", "2", "5", "10"}:
        raise ValueError("Use an official nonzero severity/breadth score with justification")
    exploration, confirmation = store.read_run(exploration_id), store.read_run(confirmation_id)
    summary = confirmation_summary(exploration, confirmation)
    probes = review["probe_ids"]
    if not isinstance(probes, list) or not probes or any(type(p) is not str for p in probes):
        raise ValueError("Select supported probe ids")
    if len(set(probes)) != len(probes) or not set(probes) <= set(summary["supported_probe_ids"]):
        raise ValueError("Review selects unsupported probes")
    if review["scope"] not in {"hosted_service", "model_revision"}:
        raise ValueError("scope must be hosted_service or model_revision")
    if exploration["spec"]["target"]["revision"] == "unknown" and review["scope"] != "hosted_service":
        raise ValueError("Unknown revision cannot support model_revision scope")
    record = {"schema_version": "2.0", "id": f"finding-{uuid.uuid4().hex}",
              "status": "confirmed", "mode": "live", "created_at": now(),
              "exploration_id": exploration_id, "confirmation_id": confirmation_id,
              "exploration_hash": digest(exploration), "confirmation_hash": digest(confirmation),
              "review": review, "summary": summary}
    # Validate export limits before admitting a confirmed record.
    official_finding(record, exploration, confirmation)
    with store.connect() as db:
        if any(json.loads(r[0])["confirmation_id"] == confirmation_id for r in db.execute("SELECT payload FROM reviews")):
            raise ValueError("This confirmation already has a finding; do not split one issue")
        db.execute("INSERT INTO reviews VALUES (?,?,?)", (record["id"], canonical(record).decode(), digest(record)))
    return record["id"]


def validate_schema(value, schema: dict, path: str = "$") -> None:
    """Only the keyword subset used by the pinned official schema; not a general validator."""
    kind = schema.get("type")
    valid = {"object": isinstance(value, dict), "array": isinstance(value, list),
             "string": isinstance(value, str), "integer": type(value) is int,
             "number": type(value) in (int, float)}
    if kind and not valid.get(kind, False):
        raise ValueError(f"Schema type violation at {path}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"Schema enum violation at {path}")
    if isinstance(value, dict):
        if not set(schema.get("required", [])) <= value.keys():
            raise ValueError(f"Missing required fields at {path}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False and not value.keys() <= properties.keys():
            raise ValueError(f"Unexpected fields at {path}")
        for key, item in value.items():
            if key in properties:
                validate_schema(item, properties[key], f"{path}.{key}")
    if isinstance(value, list):
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", float("inf")):
            raise ValueError(f"Array size violation at {path}")
        for item in value:
            validate_schema(item, schema.get("items", {}), path + "[]")
    if isinstance(value, str) and len(value) > schema.get("maxLength", float("inf")):
        raise ValueError(f"Text too long at {path}; shorten the reviewed text")


def official_finding(record: dict, exploration: dict, confirmation: dict) -> dict:
    review, spec = record["review"], exploration["spec"]
    walkthroughs = []
    for attempt in confirmation["attempts"]:
        if attempt["case_id"] in review["probe_ids"] and attempt["result"]["assessment"] == "fail":
            walkthroughs.append("USER:\n" + attempt["request"]["case"]["prompt"] +
                                "\n\nASSISTANT:\n" + attempt["result"]["text"])
    result = {
        "schema_version": "1.0.0", "issue_title": review["title"],
        "model": {"name": spec["target"]["model"], "version": spec["target"]["revision"],
                  "provider": "CSCS", "parameters": spec["generation"]},
        "environment": {"endpoint": spec["target"]["endpoint"],
                        "hardware": "CSCS hosted inference; server hardware not independently verified",
                        "libraries": ["Python " + exploration["manifest"]["python"] + " standard library"]},
        "issue_summary": {"title": review["title"], "overview": review["claim"],
                          "desired_behavior": review["desired_behavior"],
                          "observed_behavior": "Matched probe failures were observed in exploration and independent replay. " + review["claim"],
                          "topic_area": review["topic_area"], "self_assessed_severity": review["severity"],
                          "self_assessed_severity_justification": review["impact"],
                          "self_assessed_breadth": review["breadth"],
                          "self_assessed_breadth_justification": review["breadth_justification"]},
        "apertus_response_walkthroughs": walkthroughs[:10],
        "steps_to_reproduce": ["Configure LLM_NAME, LLM_BASE_URL and LLM_API_KEY as documented.",
                               "Run make run with the exported spec in a fresh output directory.",
                               "Inspect all attempts and summary; sampling and hosted-service changes may change results."],
        "notes": f"Scope: {review['scope']}. {review['scope_basis']}\nNovelty: {review['novelty']}\nLimitations: {review['limitations']}\nAll denominators, failures and evidence hashes are in the companion evidence package."
    }
    schema = json.loads((Path(__file__).parent / "findings.schema.json").read_text(encoding="utf-8"))
    validate_schema(result, schema)
    return result


def export_review(store: LiveStore, finding_id: str, directory: Path) -> Path:
    identifier(finding_id)
    with store.connect() as db:
        row = db.execute("SELECT * FROM reviews WHERE id=?", (finding_id,)).fetchone()
    if row is None:
        raise ValueError("Unknown reviewed finding")
    record = json.loads(row["payload"])
    if digest(record) != row["payload_hash"]:
        raise ValueError("Review hash mismatch")
    first, second = store.read_run(record["exploration_id"]), store.read_run(record["confirmation_id"])
    if digest(first) != record["exploration_hash"] or digest(second) != record["confirmation_hash"]:
        raise ValueError("Reviewed evidence has changed")
    confirmation_summary(first, second)
    finding = official_finding(record, first, second)
    if directory.exists():
        raise ValueError("Export directory must be new; do not overwrite evidence")
    artifacts = {"findings/" + finding_id + ".json": canonical(finding),
                 "data/spec.json": canonical(first["spec"]),
                 "data/evidence.json": canonical({"exploration": first, "confirmation": second, "review": record}),
                 "findings/LICENSE.txt": b"Findings licensed under CDLA-Permissive-2.0.\nhttps://cdla.dev/permissive-2-0/\n"}
    if sum(len(v) for k, v in artifacts.items() if k.startswith("data/")) > 100_000_000:
        raise ValueError("data exceeds 100 MB")
    for name, data in artifacts.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (directory / "manifest.json").write_bytes(canonical({
        "finding_id": finding_id, "files": {k: __import__("hashlib").sha256(v).hexdigest() for k, v in artifacts.items()},
        "schema_status": "local syntax repair; organizer compatibility pending",
        "publication_status": "private; not a complete submission"}))
    return directory
