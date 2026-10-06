"""Persistent phase reservations and append-only run outcomes for the live MVP."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import time
import uuid

from .contracts import canonical, digest
from .live_contracts import runner_manifest, schedule, validate_spec
from .store import BudgetExhausted, EvidenceStore, now


@contextmanager
def execution_lock(root: Path):
    """OS releases the lock after a crash; cancellation uses a separate DB connection."""
    root.mkdir(parents=True, exist_ok=True)
    with (root / "live.lock").open("a+b") as handle:
        handle.seek(0)
        handle.write(b"0")
        handle.flush()
        handle.seek(0)
        try:
            if __import__("os").name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("Another live executor holds this store") from None
        try:
            yield
        finally:
            handle.seek(0)
            if __import__("os").name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


class LiveStore(EvidenceStore):
    def __init__(self, root: Path):
        super().__init__(root)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS live_runs (
                    run_id TEXT PRIMARY KEY REFERENCES runs(id), experiment_id TEXT NOT NULL,
                    phase TEXT NOT NULL, replay_of TEXT REFERENCES runs(id),
                    manifest TEXT NOT NULL, manifest_hash TEXT NOT NULL, deadline REAL NOT NULL,
                    UNIQUE(experiment_id, phase)
                );
                CREATE TABLE IF NOT EXISTS live_outcomes (
                    run_id TEXT PRIMARY KEY REFERENCES runs(id), payload TEXT NOT NULL,
                    payload_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL, payload_hash TEXT NOT NULL
                );
            """)
            for table in ("live_runs", "live_outcomes", "reviews"):
                for action in ("UPDATE", "DELETE"):
                    db.execute(f"""CREATE TRIGGER IF NOT EXISTS no_{action}_{table}
                        BEFORE {action} ON {table} BEGIN
                        SELECT RAISE(ABORT, 'append-only ledger'); END""")

    def start(self, spec: dict, replay_of: str | None = None) -> str:
        spec = validate_spec(spec)
        manifest = runner_manifest()
        phase = "confirmation" if replay_of else "exploration"
        if replay_of:
            original = self.read_run(replay_of)
            if (original["phase"] != "exploration" or original["status"] != "completed"
                    or digest(original["spec"]) != digest(spec)
                    or original["manifest"]["runner_sha256"] != manifest["runner_sha256"]):
                raise ValueError("Replay requires a completed exploration with frozen spec and runner")
        run_id = f"run-{uuid.uuid4().hex}"
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM live_runs WHERE experiment_id=? AND phase=?",
                          (spec["id"], phase)).fetchone():
                raise ValueError("This experiment phase already exists; inspect/recover it, never resend")
            db.execute("INSERT INTO runs VALUES (?,?,?,?,?)", (
                run_id, canonical(spec).decode(), digest(spec), spec["limits"][f"{phase}_calls"], now()))
            db.execute("INSERT INTO live_runs VALUES (?,?,?,?,?,?,?)", (
                run_id, spec["id"], phase, replay_of, canonical(manifest).decode(),
                digest(manifest), time.time() + spec["limits"]["run_seconds"]))
        return run_id

    def reserve_slot(self, run_id: str, item: dict, request: dict) -> str:
        attempt_id = f"attempt-{uuid.uuid4().hex}"
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT r.*,l.deadline FROM runs r JOIN live_runs l ON r.id=l.run_id WHERE r.id=?", (run_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown live run")
            if db.execute("SELECT 1 FROM live_outcomes WHERE run_id=?", (run_id,)).fetchone():
                raise RuntimeError("Run already closed")
            if db.execute("SELECT 1 FROM events WHERE run_id=? AND kind='cancelled'", (run_id,)).fetchone():
                raise RuntimeError("cancelled")
            if time.time() >= row["deadline"]:
                raise TimeoutError("run_deadline")
            attempts = db.execute("SELECT request FROM attempts WHERE run_id=?", (run_id,)).fetchall()
            if len(attempts) >= row["max_calls"]:
                raise BudgetExhausted("budget_exhausted")
            if any(json.loads(a[0])["slot"] == item["slot"] for a in attempts):
                raise RuntimeError("Slot already reserved; outcome may be unknown, never resend")
            db.execute("INSERT INTO attempts VALUES (?,?,?,?,?,?)", (
                attempt_id, run_id, item["case"]["id"], canonical(request).decode(), digest(request), now()))
        return attempt_id

    def close_run(self, run_id: str, status: str) -> None:
        payload = {"status": status, "ended_at": now()}
        with self.connect() as db:
            db.execute("INSERT INTO live_outcomes VALUES (?,?,?)", (
                run_id, canonical(payload).decode(), digest(payload)))

    def read_run(self, run_id: str) -> dict:
        result = super().read_run(run_id)
        with self.connect() as db:
            row = db.execute("SELECT * FROM live_runs WHERE run_id=?", (run_id,)).fetchone()
            end = db.execute("SELECT * FROM live_outcomes WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("Not a live MVP run")
        manifest = json.loads(row["manifest"])
        if digest(manifest) != row["manifest_hash"]:
            raise ValueError("Manifest hash mismatch")
        outcome = json.loads(end["payload"]) if end else None
        if end and digest(outcome) != end["payload_hash"]:
            raise ValueError("Outcome hash mismatch")
        result.update(mode="live", phase=row["phase"], replay_of=row["replay_of"],
                      manifest=manifest, deadline=row["deadline"], outcome=outcome,
                      status=outcome["status"] if outcome else "in_progress_or_interrupted")
        attempted = {a["request"]["slot"] for a in result["attempts"]}
        result["unexecuted_slots"] = [i["slot"] for i in schedule(result["spec"], row["phase"]) if i["slot"] not in attempted]
        result["unknown_attempts"] = [a["id"] for a in result["attempts"] if a["result"] is None]
        result["reserved_output_tokens"] = len(attempted) * result["spec"]["generation"]["max_output_tokens"]
        result["input_accounting"] = "characters bounded; exact input tokens unknown before response"
        return result

    def list_runs(self) -> list[dict]:
        with self.connect() as db:
            ids = [r[0] for r in db.execute("SELECT run_id FROM live_runs ORDER BY rowid")]
        return [{k: run[k] for k in ("run_id", "phase", "status", "reserved_calls")}
                for run in (self.read_run(rid) for rid in ids)]

    def recover(self, run_id: str) -> dict:
        with execution_lock(self.root):
            run = self.read_run(run_id)
            if run["outcome"] is None:
                self.close_run(run_id, "interrupted")
        return self.read_run(run_id)
