"""API/MCP/provider boundaries. Unconfigured ports fail, never fake success."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .contracts import Case, Target


class TargetPort(Protocol):
    mode: str
    def generate(self, target: Target, case: Case) -> str | TargetReply: ...


@dataclass(frozen=True)
class TargetReply:
    """Text plus non-secret serving metadata that belongs in experiment evidence."""

    text: str
    usage: dict[str, int] | None = None
    response_id: str | None = None


class TargetAPIError(RuntimeError):
    """Sanitized API failure; deliberately excludes response bodies and headers."""

    def __init__(self, category: str, status_code: int | None = None):
        self.category = category
        self.status_code = status_code
        suffix = f" (HTTP {status_code})" if status_code is not None else ""
        super().__init__(f"CSCS inference request failed: {category}{suffix}")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


class CSCSChatCompletionsTarget:
    """CSCS hosted OpenAI-compatible target, bounded per request.

    Construction requires explicit output and timeout limits. ExperimentManager
    continues to reject live execution until the remaining G2 controls are ready.
    """

    mode = "live"

    def __init__(self, *, max_output_tokens: int, max_input_chars: int,
                 timeout_seconds: int,
                 environ: dict[str, str] | None = None):
        env = os.environ if environ is None else environ
        self.base_url = env.get("LLM_BASE_URL", "").rstrip("/")
        self.api_key = env.get("LLM_API_KEY", "")
        self.model = env.get("LLM_NAME", "")
        if not self.base_url or not self.api_key or not self.model:
            raise ValueError("LLM_BASE_URL, LLM_API_KEY and LLM_NAME are required")
        parsed = urlparse(self.base_url)
        if (parsed.scheme != "https" or parsed.hostname != "api.inference.cscs.ch"
                or parsed.port not in (None, 443) or parsed.path != "/v1"
                or parsed.query or parsed.fragment or parsed.username or parsed.password):
            raise ValueError("CSCS endpoint must be https://api.inference.cscs.ch/v1")
        if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 1024:
            raise ValueError("max_output_tokens must be between 1 and 1024")
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 120:
            raise ValueError("timeout_seconds must be between 1 and 120")
        if type(max_input_chars) is not int or not 1 <= max_input_chars <= 100_000:
            raise ValueError("max_input_chars must be between 1 and 100000")
        self.max_output_tokens = max_output_tokens
        self.max_input_chars = max_input_chars
        self.timeout_seconds = timeout_seconds
        self._opener = build_opener(_NoRedirect)

    def generate(self, target: Target, case: Case) -> TargetReply:
        if target.mode != "live":
            raise ValueError("CSCS client cannot impersonate a mock target")
        if target.model != self.model:
            raise ValueError("Experiment model does not match configured CSCS model")
        if len(case.prompt) > self.max_input_chars:
            raise ValueError("Prompt exceeds the configured CSCS input character limit")
        from .live_transport import CSCSClient, assess
        client = CSCSClient({"LLM_NAME": self.model, "LLM_BASE_URL": self.base_url,
                             "LLM_API_KEY": self.api_key})
        body = {"model": target.model, "messages": [{"role": "user", "content": case.prompt}],
                "temperature": 0, "max_tokens": self.max_output_tokens, "stream": False}
        result = assess(client.send(body, self.timeout_seconds), body, case.expected)
        if result["assessment"] in {"error", "inconclusive"}:
            raise TargetAPIError(result["reason"], result.get("http_status"))
        return TargetReply(text=result["text"], usage=result["usage"],
                           response_id=result["response_id"])



class MockTarget:
    mode = "mock"

    def __init__(self, responses: dict[str, str]):
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in responses.items()):
            raise ValueError("Mock responses must map case ids to text")
        self.responses = dict(responses)

    def generate(self, target: Target, case: Case) -> str:
        if target.mode != "mock":
            raise ValueError("Mock client cannot impersonate a live target")
        return self.responses[case.id]


class UnconfiguredTarget:
    mode = "live"

    def generate(self, target: Target, case: Case) -> str:
        raise NotImplementedError("Live model API is not configured")


class UnconfiguredMcp:
    def call(self, tool: str, arguments: dict) -> dict:
        raise NotImplementedError("MCP transport is not configured")


class UnconfiguredResearchAgent:
    def propose(self, experiment_id: str) -> list[dict]:
        raise NotImplementedError("Planner model is not configured; use explicit cases")
