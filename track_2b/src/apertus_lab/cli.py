from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .contracts import ExperimentSpec
from .engine import ExperimentManager
from .knowledge import compile_candidate
from .ports import MockTarget
from .store import EvidenceStore


def main() -> int:
    from .mvp_cli import COMMANDS, main as mvp_main
    if len(sys.argv) > 1 and sys.argv[1] in COMMANDS:
        return mvp_main()
    parser = argparse.ArgumentParser(description="Apertus Lab offline foundation")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run-mock")
    run.add_argument("--spec", type=Path, required=True)
    run.add_argument("--responses", type=Path, required=True)
    run.add_argument("--runtime-snapshot", type=Path)
    replay = sub.add_parser("replay-mock")
    replay.add_argument("--run-id", required=True)
    replay.add_argument("--responses", type=Path, required=True)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--run-id", required=True)
    compile_parser = sub.add_parser("compile")
    compile_parser.add_argument("--run-id", required=True)
    compile_parser.add_argument("--workspace", type=Path, required=True)
    for child in (run, replay, inspect, compile_parser):
        child.add_argument("--store", type=Path, required=True)
    args = parser.parse_args()
    try:
        store = EvidenceStore(args.store)
        if args.command == "inspect":
            result = store.read_run(args.run_id)
        elif args.command == "compile":
            result = {"page": str(compile_candidate(store, args.run_id, args.workspace))}
        else:
            responses = json.loads(args.responses.read_text(encoding="utf-8"))
            manager = ExperimentManager(store, MockTarget(responses))
            if args.command == "run-mock":
                spec = ExperimentSpec.from_dict(json.loads(args.spec.read_text(encoding="utf-8")))
                snapshot = (json.loads(args.runtime_snapshot.read_text(encoding="utf-8"))
                            if args.runtime_snapshot else None)
                run_id = manager.run(spec, snapshot)
            else:
                run_id = manager.replay(args.run_id)
            result = store.read_run(run_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, RuntimeError, OSError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
