import base64
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from apertus_lab.contracts import canonical, digest
from apertus_lab.knowledge import check_sources, compile_candidate
from apertus_lab.live import confirmation_summary, execute
from apertus_lab.live_contracts import request_body, schedule, validate_spec
from apertus_lab.live_store import LiveStore, execution_lock
from apertus_lab.live_transport import CSCSClient, assess
from apertus_lab.mvp_cli import main
from apertus_lab.review import export_review, record_review

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def response(body, content="BLUE", finish="stop", model=None, usage=True):
    payload = {"model": model or body["model"], "id": "fixture-response",
               "choices": [{"message": {"content": content}, "finish_reason": finish}]}
    if usage:
        payload["usage"] = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
    raw = canonical(payload)
    return {"response": payload, "http_status": 200, "transport_error": None,
            "raw_response_base64": base64.b64encode(raw).decode(),
            "raw_response_sha256": hashlib.sha256(raw).hexdigest(), "raw_response_complete": True}


class FakeClient:
    def __init__(self, handler=None):
        self.calls = []
        self.handler = handler

    def check_request(self, body):
        pass

    def send(self, body, timeout):
        self.calls.append((body, timeout))
        if self.handler:
            return self.handler(body, timeout)
        return response(body, "RED" if "\n\n" in body["messages"][0]["content"] else "BLUE")


class MVPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.spec = json.loads((EXAMPLES / "live-spec.json").read_text(encoding="utf-8"))
        self.store = LiveStore(self.root / "evidence")
        self.client = FakeClient()

    def test_contract_rejects_missing_extra_and_insufficient_limits(self):
        for change in (lambda d: d.pop("limits"),
                       lambda d: d["limits"].update(money=10),
                       lambda d: d["limits"].update(exploration_calls=1),
                       lambda d: d["limits"].update(request_seconds=True),
                       lambda d: d["target"].update(endpoint="https://example.com"),
                       lambda d: d["cases"].pop(),
                       lambda d: d["generation"].update(temperature=float("nan"))):
            data = copy.deepcopy(self.spec)
            change(data)
            with self.assertRaises(ValueError):
                validate_spec(data)

    def test_live_flow_replay_fresh_and_review_export(self):
        first = execute(self.store, self.spec, self.client)
        second = execute(self.store, self.spec, self.client, first)
        a, b = self.store.read_run(first), self.store.read_run(second)
        self.assertEqual(len(self.client.calls), 18)
        self.assertEqual(b["replay_of"], first)
        self.assertEqual(b["status"], "completed")
        self.assertFalse({x['id'] for x in a['attempts']} & {x['id'] for x in b['attempts']})
        summary = confirmation_summary(a, b)
        self.assertEqual(summary["supported_probe_ids"], ["en-probe", "de-probe", "fr-probe"])
        review = json.loads((EXAMPLES / "review-template.json").read_text())
        with self.assertRaises(ValueError):
            record_review(self.store, first, second, review)
        review.update(reviewer="offline-test-reviewer", human_reviewed=True,
                      title="Offline fixture review", claim="Fixture-only observation")
        finding = record_review(self.store, first, second, review)
        out = export_review(self.store, finding, self.root / "export")
        self.assertTrue((out / "data/evidence.json").exists())
        with self.assertRaises(ValueError):
            export_review(self.store, finding, out)

    def test_restart_and_duplicate_phase_never_send(self):
        run = execute(self.store, self.spec, self.client)
        reopened = LiveStore(self.root / "evidence")
        with self.assertRaises(ValueError):
            execute(reopened, self.spec, self.client)
        self.assertEqual(len(self.client.calls), 6)
        self.assertEqual(reopened.read_run(run)["reserved_calls"], 6)

    def test_crashed_reservation_recovery_never_resends(self):
        rid = self.store.start(self.spec)
        item = schedule(self.spec, "exploration")[0]
        self.store.reserve_slot(rid, item, {"slot": item["slot"], "mode": "live"})
        reopened = LiveStore(self.root / "evidence")
        run = reopened.recover(rid)
        self.assertEqual(run["status"], "interrupted")
        self.assertEqual(len(run["unknown_attempts"]), 1)
        with self.assertRaises(ValueError):
            execute(reopened, self.spec, self.client)
        self.assertEqual(len(self.client.calls), 0)

    def test_serial_store_lock(self):
        with execution_lock(self.store.root):
            with self.assertRaises(RuntimeError):
                with execution_lock(self.store.root):
                    pass

    def test_atomic_duplicate_reservation(self):
        rid = self.store.start(self.spec)
        item = schedule(self.spec, "exploration")[0]
        def reserve(_):
            try:
                return self.store.reserve_slot(rid, item, {"slot": item["slot"]})
            except RuntimeError:
                return None
        with ThreadPoolExecutor(max_workers=8) as pool:
            values = list(pool.map(reserve, range(16)))
        self.assertEqual(sum(v is not None for v in values), 1)

    def test_cancel_stops_after_inflight(self):
        rid = []
        def handler(body, timeout):
            self.store.cancel(rid[0])
            return response(body)
        client = FakeClient(handler)
        run_id = execute(self.store, self.spec, client, on_start=rid.append)
        run = self.store.read_run(run_id)
        self.assertEqual(run["status"], "cancelled")
        self.assertEqual(len(client.calls), 1)

    def test_error_stops_no_retry(self):
        client = FakeClient(lambda b, t: {"http_status": 429, "transport_error": None})
        rid = execute(self.store, self.spec, client)
        run = self.store.read_run(rid)
        self.assertEqual(run["status"], "rate_limited")
        self.assertEqual(run["attempts"][0]["result"]["assessment"], "error")
        self.assertEqual(len(client.calls), 1)

    def test_timeout_and_model_mismatch_are_not_fail(self):
        body = request_body(self.spec, self.spec["cases"][0])
        for raw, expected in ((response(body, finish="length"), "inconclusive"),
                              (response(body, model="wrong-model"), "error"),
                              ({"transport_error": "timeout"}, "error")):
            self.assertEqual(assess(raw, body, "BLUE")["assessment"], expected)
        missing = assess(response(body, usage=False), body, "BLUE")
        self.assertEqual(missing["assessment"], "pass")
        self.assertEqual(missing["usage_status"], "missing_or_invalid")

    def test_deadline_prevents_next_dispatch(self):
        spec = copy.deepcopy(self.spec)
        spec["limits"]["run_seconds"] = 1
        import time
        client = FakeClient(lambda b, t: (time.sleep(1.05), response(b))[1])
        rid = execute(self.store, spec, client)
        self.assertEqual(self.store.read_run(rid)["status"], "deadline_exceeded")
        self.assertEqual(len(client.calls), 1)
        self.assertLessEqual(client.calls[0][1], 1)

    def test_changed_rule_or_spec_replay_rejected(self):
        rid = execute(self.store, self.spec, self.client)
        modified = copy.deepcopy(self.spec)
        modified["cases"][0]["expected"] = "RED"
        with self.assertRaises(ValueError):
            execute(self.store, modified, self.client, rid)
        with patch("apertus_lab.live_store.runner_manifest", return_value={"runner_sha256": "changed"}):
            with self.assertRaises(ValueError):
                execute(self.store, self.spec, self.client, rid)

    def test_wiki_is_candidate_and_stale_is_visible(self):
        rid = execute(self.store, self.spec, self.client)
        workspace = self.root / "wiki-workspace"
        page = compile_candidate(self.store, rid, workspace)
        self.assertIn('mode: "live"', page.read_text())
        self.assertIn('review_status: "candidate"', page.read_text())
        self.assertEqual(page, compile_candidate(self.store, rid, workspace))
        self.assertEqual(check_sources(workspace)[0]["freshness"], "current")
        next((workspace / "sources").glob("*.json")).write_text("corrupt")
        self.assertEqual(check_sources(workspace)[0]["freshness"], "stale")

    def test_no_finding_for_passing_probe_or_failed_control(self):
        client = FakeClient(lambda b, t: response(b))
        a = execute(self.store, self.spec, client)
        b = execute(self.store, self.spec, client, a)
        self.assertEqual(confirmation_summary(self.store.read_run(a), self.store.read_run(b))["status"], "not_supported")

    def test_cli_missing_key_before_request(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(main(["run-live", "--spec", str(EXAMPLES / "live-spec.json"),
                                   "--store", str(self.root / "new")]), 1)
        self.assertFalse((self.root / "new").exists())

    def test_transport_no_redirect_raw_capture_and_secret_discard(self):
        env = {"LLM_NAME": self.spec["target"]["model"], "LLM_BASE_URL": self.spec["target"]["endpoint"], "LLM_API_KEY": "dummy-secret-token"}
        body = request_body(self.spec, self.spec["cases"][0])
        from unittest.mock import MagicMock
        conn = MagicMock()
        resp = conn.getresponse.return_value
        resp.status = 302
        resp.read1.side_effect = [b'{"error":"redirect"}', b'']
        with patch("apertus_lab.live_transport.http.client.HTTPSConnection", return_value=conn):
            result = CSCSClient(env).send(body, 1)
        self.assertEqual(conn.request.call_count, 1)
        self.assertEqual(base64.b64decode(result["raw_response_base64"]), b'{"error":"redirect"}')
        self.assertEqual(assess(result, body, "BLUE")["assessment"], "error")
        resp.read1.side_effect = [b'dummy-secret-token', b'']
        with patch("apertus_lab.live_transport.http.client.HTTPSConnection", return_value=conn):
            result = CSCSClient(env).send(body, 1)
        self.assertEqual(result["transport_error"], "credential_echo_discarded")
        self.assertIsNone(result["raw_response_base64"])


if __name__ == "__main__":
    unittest.main()
