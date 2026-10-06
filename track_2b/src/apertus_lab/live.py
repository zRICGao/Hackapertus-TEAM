"""Serial, non-resumable live execution. Crashed reservations are never replayed implicitly."""
from __future__ import annotations

from datetime import datetime
import time
from typing import Callable

from .contracts import digest
from .live_contracts import request_body, schedule, validate_spec
from .live_store import LiveStore, execution_lock
from .live_transport import CSCSClient, assess
from .store import BudgetExhausted, now


def execute(store: LiveStore, spec: dict, client: CSCSClient,
            replay_of: str | None = None, on_start: Callable | None = None) -> str:
    spec = validate_spec(spec)
    for case in spec["cases"]:
        client.check_request(request_body(spec, case))
    with execution_lock(store.root):
        run_id = store.start(spec, replay_of)
        if on_start:
            on_start(run_id)
        run = store.read_run(run_id)
        deadline = time.monotonic() + max(0, run["deadline"] - time.time())
        status = "completed"
        try:
            for item in schedule(spec, run["phase"]):
                remaining = min(deadline - time.monotonic(), run["deadline"] - time.time())
                if remaining <= 0:
                    status = "deadline_exceeded"
                    break
                body = request_body(spec, item["case"])
                request = {"slot": item["slot"], "repeat": item["repeat"], "case": item["case"],
                           "body": body, "endpoint": spec["target"]["endpoint"],
                           "body_sha256": digest(body), "mode": "live",
                           "timeout_seconds": min(spec["limits"]["request_seconds"], remaining)}
                try:
                    attempt_id = store.reserve_slot(run_id, item, request)
                except BudgetExhausted:
                    status = "budget_exhausted"
                    break
                except TimeoutError:
                    status = "deadline_exceeded"
                    break
                except RuntimeError as error:
                    if str(error) == "cancelled":
                        status = "cancelled"
                        break
                    raise
                # Cancellation linearizes against reservation. A reserved request is
                # in flight; cancellation never promises to revoke server work.
                response = client.send(body, request["timeout_seconds"])
                result = assess(response, body, item["case"]["expected"])
                store.finish(attempt_id, result)
                if result["assessment"] in {"error", "inconclusive"}:
                    status = result["reason"]
                    break
                if time.monotonic() >= deadline:
                    status = "deadline_exceeded"
                    break
            if store.read_run(run_id)["cancelled"]:
                status = "cancelled"
        except KeyboardInterrupt:
            store.cancel(run_id)
            status = "cancelled"
        except Exception:
            # Never log arbitrary exception messages: they could contain credentials.
            store.close_run(run_id, "internal_error")
            raise RuntimeError(f"Live run stopped; inspect {run_id}") from None
        store.close_run(run_id, status)
        return run_id


def confirmation_summary(exploration: dict, confirmation: dict) -> dict:
    spec = exploration["spec"]
    if (exploration["status"] != "completed" or confirmation["status"] != "completed"
            or exploration["phase"] != "exploration" or confirmation["phase"] != "confirmation"
            or confirmation["replay_of"] != exploration["run_id"]
            or exploration["spec_hash"] != confirmation["spec_hash"]
            or exploration["manifest"]["runner_sha256"] != confirmation["manifest"]["runner_sha256"]):
        raise ValueError("Complete frozen exploration and linked replay are required")
    summaries = []
    for run in (exploration, confirmation):
        if run["unexecuted_slots"] or run["unknown_attempts"]:
            raise ValueError("Incomplete run")
        counts = {}
        for case in spec["cases"]:
            results = [a["result"]["assessment"] for a in run["attempts"] if a["case_id"] == case["id"]]
            counts[case["id"]] = {"n": len(results), **{k: results.count(k) for k in ("pass", "fail", "inconclusive", "error")}}
        summaries.append(counts)
    supported = []
    for case in spec["cases"]:
        if case["role"] != "probe":
            continue
        control = next(c for c in spec["cases"] if c["pair"] == case["pair"] and c["role"] == "control")
        controls_pass = all(s[control["id"]]["n"] == s[control["id"]]["pass"] for s in summaries)
        if (controls_pass and summaries[0][case["id"]]["fail"] >= 1
                and summaries[1][case["id"]]["fail"] >= spec["confirmation"]["min_probe_failures"]):
            supported.append(case["id"])
    return {"exploration": summaries[0], "confirmation": summaries[1],
            "supported_probe_ids": supported,
            "status": "candidate" if supported else "not_supported",
            "requires_independent_review": True}
