"""
Meta Model API client — Peridot "60-Second System" hackathon demo.

SIMULATED DEMO context: this module is NOT called by default. The default
backend is the deterministic mock responder (mock_responder.py). No real
API call is made unless LLM_BACKEND=meta AND a key is configured.

Configuration (environment variables):
    META_MODEL_API_KEY   -- the API key (never hard-code it, never commit it)
    META_MODEL_API_BASE  -- optional, OpenAI-compatible base URL
                            (default: https://api.meta.ai/v1)
    META_MODEL_NAME      -- optional, model name (default: muse-spark-1.3)

This client is structured as an OpenAI-compatible chat client:
    POST {base}/chat/completions
    Authorization: Bearer <META_MODEL_API_KEY>
    {"model": ..., "messages": [{"role": ..., "content": ...}, ...]}

ENDPOINT DETAILS — LANDED (verified live Oct 23, 2026):
    Base URL https://api.meta.ai/v1 and model muse-spark-1.3 are now the
    defaults below. A real chat completion through this exact request /
    response shape returned HTTP 200 with model "muse-spark-1.3" on
    Oct 23, 2026 (see ~/workspace/hackathon/real-model-status-2026-10-23.md).
    No extra headers beyond Content-Type and Authorization were needed.
    Do not remove the SIMULATED telephony labelling when wiring this in —
    telephony stays simulated for the demo regardless of the LLM backend.
"""

import json
import os
import urllib.request
import urllib.error


class MetaClientError(RuntimeError):
    pass


class MetaModelClient:
    """Minimal OpenAI-compatible chat client for the Meta Model API."""

    def __init__(self, api_key=None, base_url=None, model=None):
        self.api_key = api_key if api_key is not None else os.environ.get("META_MODEL_API_KEY", "")
        self.base_url = (
            base_url
            or os.environ.get("META_MODEL_API_BASE", "")
            # Final Meta Model API base URL (verified live Oct 23, 2026).
            or "https://api.meta.ai/v1"
        ).rstrip("/")
        self.model = model or os.environ.get("META_MODEL_NAME", "muse-spark-1.3")

    @property
    def configured(self):
        return bool(self.api_key) and "TODO" not in self.base_url and "TODO" not in self.model

    def chat(self, messages, temperature=0.3):
        """Return the assistant message content (str).

        Raises MetaClientError if not configured — callers in the demo
        fall back to the mock responder so the demo always runs key-free.
        """
        if not self.api_key:
            raise MetaClientError(
                "META_MODEL_API_KEY is not set; demo should use LLM_BACKEND=mock"
            )
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, ValueError) as exc:
            raise MetaClientError(f"Meta Model API call failed: {exc}") from exc
        # OpenAI-compatible response shape.
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise MetaClientError(f"Unexpected Meta Model API response shape: {data!r}") from exc
