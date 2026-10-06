"""User-facing MVP commands; secrets are never CLI arguments or output."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

from .knowledge import check_sources, compile_candidate
from .live import confirmation_summary, execute
from .live_contracts import validate_spec
from .live_store import LiveStore
from .live_transport import CSCSClient
from .review import export_review, record_review

COMMANDS = {"validate-live", "run-live", "replay-live", "inspect-live", "list-live",
            "cancel-live", "recover-live", "compile-live", "check-wiki", "review-live", "export-live", "summary-live"}


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apertus 70B MVP: sponsored compute; bounded serial requests")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in sorted(COMMANDS):
        child = sub.add_parser(command)
        if command == "check-wiki":
            child.add_argument("--workspace", type=Path, required=True)
            continue
        if command in {"validate-live", "run-live"}:
            child.add_argument("--spec", type=Path, required=True)
        if command != "validate-live":
            child.add_argument("--store", type=Path, required=True)
        if command in {"replay-live", "inspect-live", "cancel-live", "recover-live", "compile-live", "review-live", "summary-live"}:
            child.add_argument("--run-id", required=True)
        if command == "compile-live":
            child.add_argument("--workspace", type=Path, required=True)
        if command in {"review-live", "summary-live"}:
            child.add_argument("--confirmation-id", required=True)
        if command == "review-live":
            child.add_argument("--review", type=Path, required=True)
        if command == "export-live":
            child.add_argument("--finding-id", required=True)
            child.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        command = args.command
        if command == "validate-live":
            spec = validate_spec(read_json(args.spec))
            result = {"valid": True, "experiment_id": spec["id"], "limits": spec["limits"],
                      "note": "No network request; no money or price controls"}
        elif command == "check-wiki":
            result = check_sources(args.workspace)
        else:
            # Validate before touching the evidence DB or accepting live credentials.
            spec = validate_spec(read_json(args.spec)) if command == "run-live" else None
            client = CSCSClient() if command in {"run-live", "replay-live"} else None
            store = LiveStore(args.store)
            if command in {"run-live", "replay-live"}:
                parent = args.run_id if command == "replay-live" else None
                if parent:
                    spec = store.read_run(parent)["spec"]
                run_id = execute(store, spec, client, replay_of=parent,
                                 on_start=lambda rid: print(json.dumps({"started_run_id": rid}), file=sys.stderr, flush=True))
                result = store.read_run(run_id)
            elif command == "inspect-live":
                result = store.read_run(args.run_id)
            elif command == "list-live":
                result = store.list_runs()
            elif command == "cancel-live":
                run = store.read_run(args.run_id)
                if run["outcome"]:
                    raise ValueError("Run is already closed")
                store.cancel(args.run_id)
                result = {"cancel_requested": args.run_id, "note": "In-flight requests may still finish"}
            elif command == "recover-live":
                result = store.recover(args.run_id)
            elif command == "compile-live":
                result = {"page": str(compile_candidate(store, args.run_id, args.workspace))}
            elif command == "summary-live":
                result = confirmation_summary(store.read_run(args.run_id), store.read_run(args.confirmation_id))
            elif command == "review-live":
                result = {"finding_id": record_review(store, args.run_id, args.confirmation_id, read_json(args.review))}
            else:
                result = {"output": str(export_review(store, args.finding_id, args.output))}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if command in {"run-live", "replay-live"} and result["status"] != "completed":
            return 2
        if command == "check-wiki" and any(r["freshness"] == "stale" for r in result):
            return 2
        return 0
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as error:
        # Only validation/domain errors carry messages; unexpected I/O errors are sanitized.
        message = str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
