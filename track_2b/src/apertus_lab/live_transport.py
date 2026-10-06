"""Single bounded HTTPS request, no redirects/retries; raw response evidence."""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import socket
import time

from .contracts import canonical
from .live_contracts import ENDPOINT, MODEL
from .store import now


class CSCSClient:
    def __init__(self, environ: dict | None = None):
        env = os.environ if environ is None else environ
        if env.get("LLM_NAME") != MODEL or env.get("LLM_BASE_URL", "").rstrip("/") != ENDPOINT:
            raise ValueError("Set LLM_NAME and LLM_BASE_URL to the frozen CSCS target")
        self._key = env.get("LLM_API_KEY", "")
        if not self._key or any(c in self._key for c in "\r\n"):
            raise ValueError("Missing or invalid LLM_API_KEY")

    def check_request(self, body: dict) -> None:
        if self._key in canonical(body).decode():
            raise ValueError("Credential found in request; refusing to persist or send")

    def send(self, body: dict, timeout: float) -> dict:
        self.check_request(body)
        start, started_at = time.monotonic(), now()
        deadline = start + timeout
        raw, status, error = b"", None, None
        connection = http.client.HTTPSConnection("api.inference.cscs.ch", timeout=timeout)
        try:
            connection.connect()
            sock = connection.sock
            def remaining() -> float:
                value = deadline - time.monotonic()
                if value <= 0:
                    raise TimeoutError()
                sock.settimeout(value)
                return value
            remaining()
            connection.request("POST", "/v1/chat/completions", body=canonical(body),
                               headers={"Authorization": f"Bearer {self._key}",
                                        "Content-Type": "application/json"})
            remaining()
            response = connection.getresponse()
            status = response.status
            chunks, size = [], 0
            while True:
                remaining()
                chunk = response.read1(min(65536, 8 * 1024 * 1024 + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > 8 * 1024 * 1024:
                    error = "response_too_large"
                    break
            raw = b"".join(chunks)
        except (TimeoutError, socket.timeout):
            error = "timeout"
        except (OSError, http.client.HTTPException):
            error = "transport_error"
        finally:
            connection.close()
        result = {"started_at": started_at, "received_at": now(),
                  "duration_seconds": time.monotonic() - start, "http_status": status,
                  "transport_error": error, "raw_response_base64": None,
                  "raw_response_sha256": None, "raw_response_complete": error is None,
                  "response": None, "text": None, "usage": None,
                  "response_id": None, "finish_reason": None, "response_model": None}
        # Never preserve a credential echoed by an upstream failure. This is explicitly
        # a discarded response, not a supposedly original redacted response.
        if self._key.encode() in raw:
            result.update(transport_error="credential_echo_discarded", raw_response_complete=False)
        else:
            result["raw_response_base64"] = base64.b64encode(raw).decode()
            result["raw_response_sha256"] = hashlib.sha256(raw).hexdigest()
            try:
                payload = json.loads(raw)
                result["response"] = payload
            except (ValueError, UnicodeDecodeError):
                if error is None and status == 200:
                    result["transport_error"] = "invalid_json"
        return result


def assess(result: dict, body: dict, expected: str) -> dict:
    """pass means meets desired behavior, fail means mismatch; neither confirms a finding."""
    result = dict(result)
    status, reason = "error", result.get("transport_error")
    if not reason and result.get("http_status") != 200:
        reason = "rate_limited" if result.get("http_status") == 429 else "http_error"
    if not reason:
        payload = result.get("response")
        try:
            choice = payload["choices"][0]
            message = choice["message"]
            result.update(text=message.get("content"), response_model=payload.get("model"),
                          response_id=payload.get("id"), finish_reason=choice.get("finish_reason"))
            usage = payload.get("usage")
            result["usage"] = usage if isinstance(usage, dict) else None
            result["usage_status"] = ("reported" if isinstance(usage, dict) and all(
                type(usage.get(k)) is int and usage[k] >= 0
                for k in ("prompt_tokens", "completion_tokens", "total_tokens")) else "missing_or_invalid")
            if payload.get("model") != body["model"]:
                reason = "model_mismatch"
            elif choice.get("finish_reason") == "length":
                status, reason = "inconclusive", "truncated"
            elif message.get("refusal") or choice.get("finish_reason") == "content_filter":
                status, reason = "inconclusive", "refusal"
            elif choice.get("finish_reason") != "stop":
                status, reason = "inconclusive", "unrecognized_finish_reason"
            elif not isinstance(message.get("content"), str):
                reason = "non_text_response"
            else:
                status = "pass" if message["content"].strip() == expected.strip() else "fail"
                reason = "exact_stripped_match" if status == "pass" else "exact_stripped_mismatch"
        except (TypeError, KeyError, IndexError, AttributeError):
            status, reason = "error", "invalid_response_shape"
    result.update(assessment=status, reason=reason, evaluator="exact-stripped-text-v1",
                  mode="live", transport_status="error" if status == "error" else "ok")
    return result
