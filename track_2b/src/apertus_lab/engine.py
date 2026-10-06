"""Local orchestration for frozen offline cases, with persistent reservations."""
from __future__ import annotations

from dataclasses import asdict

from .contracts import ExperimentSpec
from .ports import TargetAPIError, TargetPort, TargetReply
from .store import BudgetExhausted, EvidenceStore


class ExperimentManager:
    def __init__(self, store: EvidenceStore, client: TargetPort):
        self.store = store
        self.client = client

    def run(self, spec: ExperimentSpec, runtime_snapshot: dict | None = None) -> str:
        # Live is intentionally unavailable until all G2 controls are implemented.
        if spec.target.mode != "mock" or self.client.mode != "mock":
            raise RuntimeError("Live execution is disabled in the foundation")
        run_id = self.store.create_run(spec.to_dict())
        if runtime_snapshot is not None:
            self.store.attach_runtime(run_id, runtime_snapshot)
        for case in spec.cases:
            try:
                attempt_id = self.store.reserve(run_id, case.id, {
                    "target": asdict(spec.target), "case": asdict(case),
                    "mode": self.client.mode,
                    "generation": {
                        "temperature": 0,
                        "max_tokens": getattr(self.client, "max_output_tokens", None),
                        "max_input_chars": getattr(self.client, "max_input_chars", None),
                        "timeout_seconds": getattr(self.client, "timeout_seconds", None),
                    },
                    "provider": {"base_url": getattr(self.client, "base_url", None)},
                })
            except BudgetExhausted:
                break
            try:
                response = self.client.generate(spec.target, case)
                if isinstance(response, TargetReply):
                    response_text = response.text
                    serving_metadata = {"usage": response.usage,
                                        "provider_response_id": response.response_id}
                elif isinstance(response, str):
                    response_text = response
                    serving_metadata = {}
                else:
                    raise TypeError("Target returned non-text output")
            except Exception as error:
                payload = {"mode": self.client.mode, "transport_status": "error",
                           "response": None, "assessment": "error",
                           "error_type": type(error).__name__, "evaluator": spec.evaluator}
                if isinstance(error, TargetAPIError):
                    payload["error_category"] = error.category
                    payload["http_status"] = error.status_code
            else:
                payload = {"mode": self.client.mode, "transport_status": "ok",
                           "response": response_text,
                           "assessment": "pass" if response_text == case.expected else "fail",
                           "evaluator": spec.evaluator, **serving_metadata}
            # Storage failures deliberately propagate and halt the run.
            self.store.finish(attempt_id, payload)
        return run_id

    def replay(self, run_id: str) -> str:
        frozen = self.store.read_run(run_id)
        return self.run(ExperimentSpec.from_dict(frozen["spec"]))
