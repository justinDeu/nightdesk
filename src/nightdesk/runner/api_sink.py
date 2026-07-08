"""HTTP write-back client for the runner pod.

The pod holds only its scoped ``ndr_`` run token; it streams transcript batches
and uploads its diff/result to the nightdesk API's run-token write-back surface
(``POST /api/v1/runs/{rid}/{transcript,diff,result}``). Uses stdlib ``urllib``
so the runner image needs no HTTP dependency.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger(__name__)


class ApiSink:
    def __init__(self, api_url: str, run_token: str, run_id: str, *, timeout: float = 30.0):
        self.base = api_url.rstrip("/")
        self.run_token = run_token
        self.run_id = run_id
        self.timeout = timeout

    def _post(self, path: str, body: bytes, content_type: str) -> bool:
        url = f"{self.base}/api/v1/runs/{self.run_id}/{path}"
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Authorization", f"Bearer {self.run_token}")
        req.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return 200 <= resp.status < 300
        except urllib.error.HTTPError as exc:
            log.warning("write-back %s -> HTTP %s: %s", path, exc.code, exc.reason)
            return False
        except (urllib.error.URLError, OSError) as exc:
            log.warning("write-back %s failed: %s", path, exc)
            return False

    def post_transcript(self, events: list[dict]) -> bool:
        if not events:
            return True
        body = "\n".join(json.dumps(e) for e in events).encode("utf-8")
        return self._post("transcript", body, "application/x-ndjson")

    def post_diff(self, diff_payload: dict) -> bool:
        return self._post("diff", json.dumps(diff_payload).encode("utf-8"),
                          "application/json")

    def post_result(self, result_payload: dict) -> bool:
        return self._post("result", json.dumps(result_payload).encode("utf-8"),
                          "application/json")
