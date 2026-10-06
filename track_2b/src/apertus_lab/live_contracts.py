"""Strict MVP contract. Resource bounds only; no monetary configuration."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .contracts import canonical, fields, identifier, text

MODEL = "swiss-ai/Apertus-v1.5-70B"
ENDPOINT = "https://api.inference.cscs.ch/v1"


def integer(value: Any, name: str, low: int, high: int) -> None:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer in [{low}, {high}]")


def validate_spec(data: dict) -> dict:
    fields(data, {"schema_version", "id", "hypothesis", "target", "cases", "limits",
                  "generation", "confirmation", "evaluator"})
    if data["schema_version"] != "2.0":
        raise ValueError("Live requires schema_version 2.0")
    identifier(data["id"])
    text(data["hypothesis"], "hypothesis")
    target = data["target"]
    fields(target, {"model", "endpoint", "revision", "service_version"})
    if target["model"] != MODEL or target["endpoint"] != ENDPOINT:
        raise ValueError("MVP only supports the configured CSCS Apertus v1.5 70B endpoint")
    for key in ("revision", "service_version"):
        text(target[key], key)
    if data["evaluator"] != "exact-stripped-text-v1":
        raise ValueError("MVP evaluator must be exact-stripped-text-v1")
    limits = data["limits"]
    fields(limits, {"exploration_calls", "confirmation_calls", "max_input_chars",
                    "run_seconds", "request_seconds"})
    for key in ("exploration_calls", "confirmation_calls"):
        integer(limits[key], key, 1, 10000)
    integer(limits["max_input_chars"], "max_input_chars", 1, 100000)
    integer(limits["run_seconds"], "run_seconds", 1, 86400)
    integer(limits["request_seconds"], "request_seconds", 1, 120)
    fields(data["generation"], {"temperature", "max_output_tokens"})
    if type(data["generation"]["temperature"]) not in (int, float) or data["generation"]["temperature"] != 0:
        raise ValueError("MVP temperature must be zero")
    integer(data["generation"]["max_output_tokens"], "max_output_tokens", 1, 1024)
    confirmation = data["confirmation"]
    fields(confirmation, {"repetitions", "min_probe_failures"})
    integer(confirmation["repetitions"], "repetitions", 2, 100)
    integer(confirmation["min_probe_failures"], "min_probe_failures", 1,
            confirmation["repetitions"])
    cases = data["cases"]
    if not isinstance(cases, list) or not cases or len(cases) > 100:
        raise ValueError("Expected 1–100 cases")
    ids, pairs = set(), {}
    for case in cases:
        fields(case, {"id", "pair", "role", "language", "prompt", "expected"})
        identifier(case["id"])
        identifier(case["pair"])
        if case["id"] in ids:
            raise ValueError("Duplicate case id")
        ids.add(case["id"])
        if case["role"] not in {"probe", "control"}:
            raise ValueError("Invalid case role")
        for key in ("language", "prompt", "expected"):
            text(case[key], key)
        if len(case["prompt"]) > limits["max_input_chars"]:
            raise ValueError("Case exceeds input character bound")
        pairs.setdefault(case["pair"], []).append(case)
    for pair in pairs.values():
        if len(pair) != 2 or {c["role"] for c in pair} != {"probe", "control"}:
            raise ValueError("Each pair needs exactly one probe and one control")
        if len({c["language"] for c in pair}) != 1:
            raise ValueError("Matched pairs must use the same language")
    if limits["exploration_calls"] < len(cases) or limits["confirmation_calls"] < len(cases) * confirmation["repetitions"]:
        raise ValueError("Call limits cannot cover the declared complete experiment")
    # Defensive copy and finite JSON validation.
    import json
    return json.loads(canonical(data))


def runner_manifest() -> dict:
    import platform
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(Path(__file__).parent.glob("*.py"))}
    return {"python": platform.python_version(), "source_hashes": files,
            "runner_sha256": hashlib.sha256(canonical(files)).hexdigest()}


def request_body(spec: dict, case: dict) -> dict:
    return {"model": spec["target"]["model"],
            "messages": [{"role": "user", "content": case["prompt"]}],
            "temperature": 0, "max_tokens": spec["generation"]["max_output_tokens"],
            "stream": False}


def schedule(spec: dict, phase: str) -> list[dict]:
    repetitions = 1 if phase == "exploration" else spec["confirmation"]["repetitions"]
    return [{"slot": f"{repeat}:{case['id']}", "repeat": repeat, "case": case}
            for repeat in range(repetitions) for case in spec["cases"]]
