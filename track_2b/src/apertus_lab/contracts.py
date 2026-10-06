"""Versioned boundary contracts with strict, dependency-free validation."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import re
from typing import Any


def canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n").encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
        raise ValueError(f"Invalid identifier: {value!r}")
    return value


def text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be nonempty text")
    return value


def fields(data: Any, expected: set[str]) -> None:
    if not isinstance(data, dict) or set(data) != expected:
        raise ValueError(f"Expected exactly these fields: {sorted(expected)}")


@dataclass(frozen=True)
class Target:
    id: str
    model: str
    revision: str
    mode: str

    def __post_init__(self) -> None:
        identifier(self.id)
        text(self.model, "model")
        text(self.revision, "revision")
        if self.mode not in {"mock", "live"}:
            raise ValueError("mode must be mock or live")


@dataclass(frozen=True)
class Case:
    id: str
    role: str
    language: str
    prompt: str
    expected: str

    def __post_init__(self) -> None:
        identifier(self.id)
        if self.role not in {"probe", "control"}:
            raise ValueError("case role must be probe or control")
        for name in ("language", "prompt", "expected"):
            text(getattr(self, name), name)


@dataclass(frozen=True)
class ExperimentSpec:
    schema_version: str
    id: str
    hypothesis: str
    target: Target
    cases: tuple[Case, ...]
    max_calls: int
    evaluator: str

    def __post_init__(self) -> None:
        if self.schema_version != "1.0":
            raise ValueError("Unsupported schema_version")
        identifier(self.id)
        text(self.hypothesis, "hypothesis")
        if type(self.max_calls) is not int or self.max_calls < 1:
            raise ValueError("max_calls must be a positive integer")
        if not self.cases or len({c.id for c in self.cases}) != len(self.cases):
            raise ValueError("cases must be nonempty with unique ids")
        if not any(c.role == "control" for c in self.cases):
            raise ValueError("At least one matched control is required")
        if self.evaluator != "exact-text-v1":
            raise ValueError("Only the fixture evaluator exact-text-v1 is configured")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExperimentSpec:
        fields(data, {"schema_version", "id", "hypothesis", "target", "cases",
                      "max_calls", "evaluator"})
        fields(data["target"], {"id", "model", "revision", "mode"})
        if not isinstance(data["cases"], list):
            raise ValueError("cases must be a list")
        for case in data["cases"]:
            fields(case, {"id", "role", "language", "prompt", "expected"})
        return cls(**{**data, "target": Target(**data["target"]),
                      "cases": tuple(Case(**case) for case in data["cases"])})
