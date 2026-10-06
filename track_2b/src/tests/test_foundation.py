from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from apertus_lab.contracts import ExperimentSpec
from apertus_lab.engine import ExperimentManager
from apertus_lab.findings import export_findings
from apertus_lab.knowledge import compile_candidate
from apertus_lab.ports import MockTarget, UnconfiguredMcp, UnconfiguredTarget
from apertus_lab.runtime_adapter import normalize_snapshot
from apertus_lab.store import BudgetExhausted, EvidenceStore

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="apertus-foundation-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = EvidenceStore(self.root / "runs")
        self.data = json.loads((EXAMPLES / "mock-spec.json").read_text())
        self.spec = ExperimentSpec.from_dict(self.data)
        self.client = MockTarget(json.loads((EXAMPLES / "mock-responses.json").read_text()))
        self.manager = ExperimentManager(self.store, self.client)

    def test_control_probe_and_replay_use_fresh_attempts(self):
        first_id = self.manager.run(self.spec)
        first = self.store.read_run(first_id)
        self.assertEqual([a["result"]["assessment"] for a in first["attempts"]], ["fail", "pass"])
        self.client.responses["probe"] = "A"
        second = self.store.read_run(self.manager.replay(first_id))
        self.assertEqual(second["attempts"][0]["result"]["assessment"], "pass")
        self.assertNotEqual(first["attempts"][0]["id"], second["attempts"][0]["id"])
        self.assertEqual(first["spec_hash"], second["spec_hash"])

    def test_concurrent_budget_and_restart(self):
        run = self.store.create_run(self.spec.to_dict())
        def reserve(_):
            try:
                return self.store.reserve(run, "probe", {"prompt": "x"})
            except BudgetExhausted:
                return None
        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(reserve, range(20)))
        self.assertEqual(len([x for x in outcomes if x]), 2)
        reopened = EvidenceStore(self.root / "runs")
        with self.assertRaises(BudgetExhausted):
            reopened.reserve(run, "probe", {})
        self.assertTrue(all(a["result"] is None for a in reopened.read_run(run)["attempts"]))

    def test_evidence_cannot_be_overwritten(self):
        run = self.store.read_run(self.manager.run(self.spec))
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.finish(run["attempts"][0]["id"], {"assessment": "pass"})
        with self.assertRaises(sqlite3.IntegrityError), self.store.connect() as db:
            db.execute("DELETE FROM results")

    def test_cancellation_blocks_new_requests(self):
        run = self.store.create_run(self.spec.to_dict())
        self.store.cancel(run)
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            self.store.reserve(run, "probe", {})

    def test_transport_failure_is_error_not_model_failure(self):
        self.client.responses.pop("probe")
        run = self.store.read_run(self.manager.run(self.spec))
        self.assertEqual(run["attempts"][0]["result"]["assessment"], "error")
        self.assertEqual(run["reserved_calls"], 2)

    def test_live_and_unconfigured_interfaces_fail_closed(self):
        self.data["target"]["mode"] = "live"
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            self.manager.run(ExperimentSpec.from_dict(self.data))
        with self.assertRaises(NotImplementedError):
            UnconfiguredTarget().generate(self.spec.target, self.spec.cases[0])
        with self.assertRaises(NotImplementedError):
            UnconfiguredMcp().call("search", {})

    def test_compiler_idempotence_and_edit_preservation(self):
        run_id = self.manager.run(self.spec)
        wiki = self.root / "wiki"
        page = compile_candidate(self.store, run_id, wiki)
        original = page.read_bytes()
        self.assertIn(b'review_status: "candidate"', original)
        self.assertEqual(page, compile_candidate(self.store, run_id, wiki))
        self.assertEqual(original, page.read_bytes())
        page.write_text("Manual review", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "overwrite"):
            compile_candidate(self.store, run_id, wiki)

    def test_empty_run_and_mock_export_rejected(self):
        run = self.store.create_run(self.spec.to_dict())
        with self.assertRaisesRegex(ValueError, "No completed evidence"):
            compile_candidate(self.store, run, self.root / "wiki")
        with self.assertRaises(ValueError):
            export_findings([{"mode": "mock", "status": "confirmed"}])

    def test_invalid_contracts(self):
        for mutate in (
            lambda d: d.update(extra=True),
            lambda d: d.update(cases=[d["cases"][0]]),
            lambda d: d.update(id="../escape"),
            lambda d: d.update(max_calls=True),
        ):
            data = json.loads(json.dumps(self.data))
            mutate(data)
            with self.assertRaises(ValueError):
                ExperimentSpec.from_dict(data)

    def test_upstream_verified_is_only_candidate(self):
        snapshot = {"schema_version": "1.0", "experiment_id": "e", "memory": [],
                    "ideas": [{"id": "idea-1", "content": "Hypothesis", "status": "verified"}]}
        event = normalize_snapshot(snapshot)[0]
        self.assertEqual(event["review_status"], "candidate")
        self.assertEqual(event["upstream_status"], "verified")
        snapshot["memory"] = [{"id": "m", "content": "x", "challengeId": "other"}]
        with self.assertRaises(ValueError):
            normalize_snapshot(snapshot)


if __name__ == "__main__":
    unittest.main()
