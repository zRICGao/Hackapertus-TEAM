"""Transactional, append-only run ledger and atomic call-budget reservations."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator
import uuid

from .contracts import canonical, digest, identifier


class BudgetExhausted(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EvidenceStore:
    """One SQLite ledger, independent from Wiki and mutable runtime memory.

    Public methods never replace evidence. SQL triggers prevent ordinary update/delete;
    hashes detect accidental content corruption, not an attacker controlling the host.
    Reservations stay spent after crashes, so reopening cannot reset a run's budget.
    """

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "evidence.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, spec TEXT NOT NULL, spec_hash TEXT NOT NULL,
                    max_calls INTEGER NOT NULL CHECK(max_calls > 0), created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                    case_id TEXT NOT NULL, request TEXT NOT NULL, request_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS results (
                    attempt_id TEXT PRIMARY KEY REFERENCES attempts(id),
                    payload TEXT NOT NULL, payload_hash TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                    kind TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime_snapshots (
                    run_id TEXT PRIMARY KEY REFERENCES runs(id), payload TEXT NOT NULL,
                    payload_hash TEXT NOT NULL
                );
            """)
            for table in ("runs", "attempts", "results", "events", "runtime_snapshots"):
                for action in ("UPDATE", "DELETE"):
                    db.execute(f"""CREATE TRIGGER IF NOT EXISTS no_{action}_{table}
                        BEFORE {action} ON {table} BEGIN
                        SELECT RAISE(ABORT, 'append-only ledger'); END""")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def create_run(self, spec: dict[str, Any]) -> str:
        run_id = f"run-{uuid.uuid4().hex}"
        with self.connect() as db:
            db.execute("INSERT INTO runs VALUES (?,?,?,?,?)", (
                run_id, canonical(spec).decode(), digest(spec), spec["max_calls"], now(),
            ))
        return run_id

    def reserve(self, run_id: str, case_id: str, request: dict[str, Any]) -> str:
        identifier(run_id)
        identifier(case_id)
        attempt_id = f"attempt-{uuid.uuid4().hex}"
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if run is None:
                raise ValueError("Unknown run")
            if db.execute("SELECT 1 FROM events WHERE run_id=? AND kind='cancelled'", (run_id,)).fetchone():
                raise RuntimeError("Run cancelled")
            count = db.execute("SELECT count(*) FROM attempts WHERE run_id=?", (run_id,)).fetchone()[0]
            if count >= run["max_calls"]:
                raise BudgetExhausted("Call budget exhausted")
            db.execute("INSERT INTO attempts VALUES (?,?,?,?,?,?)", (
                attempt_id, run_id, case_id, canonical(request).decode(), digest(request), now(),
            ))
        return attempt_id

    def finish(self, attempt_id: str, payload: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO results VALUES (?,?,?,?)", (
                attempt_id, canonical(payload).decode(), digest(payload), now(),
            ))

    def cancel(self, run_id: str) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO events(run_id,kind,created_at) VALUES (?,?,?)",
                       (run_id, "cancelled", now()))

    def attach_runtime(self, run_id: str, snapshot: dict[str, Any]) -> None:
        from .runtime_adapter import normalize_snapshot

        normalize_snapshot(snapshot)
        with self.connect() as db:
            row = db.execute("SELECT spec FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None or json.loads(row["spec"])["id"] != snapshot["experiment_id"]:
                raise ValueError("Runtime snapshot does not match experiment")
            db.execute("INSERT INTO runtime_snapshots VALUES (?,?,?)", (
                run_id, canonical(snapshot).decode(), digest(snapshot),
            ))

    def read_run(self, run_id: str) -> dict[str, Any]:
        identifier(run_id)
        with self.connect() as db:
            run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if run is None:
                raise ValueError("Unknown run")
            spec = json.loads(run["spec"])
            if digest(spec) != run["spec_hash"]:
                raise ValueError("Spec hash mismatch")
            attempts = []
            for row in db.execute("""SELECT a.*, r.payload, r.payload_hash FROM attempts a
                LEFT JOIN results r ON r.attempt_id=a.id WHERE a.run_id=? ORDER BY a.rowid""", (run_id,)):
                request = json.loads(row["request"])
                result = json.loads(row["payload"]) if row["payload"] else None
                if digest(request) != row["request_hash"]:
                    raise ValueError("Request hash mismatch")
                if result is not None and digest(result) != row["payload_hash"]:
                    raise ValueError("Result hash mismatch")
                attempts.append({"id": row["id"], "case_id": row["case_id"],
                                 "request": request, "result": result,
                                 "request_hash": row["request_hash"],
                                 "result_hash": row["payload_hash"]})
            cancelled = db.execute("SELECT 1 FROM events WHERE run_id=? AND kind='cancelled'",
                                   (run_id,)).fetchone() is not None
            runtime = db.execute("SELECT * FROM runtime_snapshots WHERE run_id=?", (run_id,)).fetchone()
            snapshot = json.loads(runtime["payload"]) if runtime else None
            if runtime and digest(snapshot) != runtime["payload_hash"]:
                raise ValueError("Runtime snapshot hash mismatch")
        return {"schema_version": "1.0", "run_id": run_id, "spec": spec,
                "created_at": run["created_at"],
                "runtime_snapshot": snapshot,
                "spec_hash": run["spec_hash"], "attempts": attempts, "cancelled": cancelled,
                "reserved_calls": len(attempts)}
