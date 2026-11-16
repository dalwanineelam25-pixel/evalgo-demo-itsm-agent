"""HTTP client for the VPN diagnostic service with timeout and retry handling.

The client never raises for transport problems. It always returns a DiagnosticResult whose
``outcome`` is one of ``pass``, ``fail``, ``timeout`` or ``error``, so callers cannot accidentally
treat a failed call as a successful diagnosis.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)


@dataclass
class DiagnosticResult:
    outcome: str  # pass | fail | timeout | error
    summary: str
    error_code: str | None = None
    checks: list[dict[str, Any]] = field(default_factory=list)
    service_run_id: str | None = None
    raw_response: str | None = None
    attempts: int = 1
    duration_ms: int = 0


class VPNDiagnosticClient:
    def __init__(self, base_url: str, timeout_s: float = 5.0, retries: int = 1):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.retries = retries

    def diagnose(self, device_id: str, requested_by: str) -> DiagnosticResult:
        started = time.monotonic()
        last: DiagnosticResult | None = None
        for attempt in range(1, self.retries + 2):
            last = self._attempt(device_id, requested_by)
            last.attempts = attempt
            log.info(
                "vpn_diagnostic attempt=%d device=%s outcome=%s error_code=%s",
                attempt, device_id, last.outcome, last.error_code,
            )
            if last.outcome in ("pass", "fail"):
                break
            if last.error_code == "DEVICE_NOT_FOUND":
                break  # not transient, retrying will not help
        assert last is not None
        last.duration_ms = int((time.monotonic() - started) * 1000)
        return last

    def _attempt(self, device_id: str, requested_by: str) -> DiagnosticResult:
        url = f"{self.base_url}/v1/vpn/diagnose"
        try:
            resp = httpx.post(
                url,
                json={"device_id": device_id, "requested_by": requested_by},
                timeout=self.timeout_s,
            )
        except httpx.TimeoutException:
            return DiagnosticResult(
                outcome="timeout",
                error_code="DIAG_TIMEOUT",
                summary=f"Diagnostic service did not respond within {self.timeout_s:.1f}s.",
            )
        except httpx.HTTPError as exc:
            return DiagnosticResult(
                outcome="error",
                error_code="DIAG_UNREACHABLE",
                summary=f"Diagnostic service unreachable: {exc.__class__.__name__}.",
            )

        body = resp.text
        if resp.status_code == 404:
            return DiagnosticResult(
                outcome="error",
                error_code="DEVICE_NOT_FOUND",
                summary=f"Device {device_id} is not registered with the diagnostic service.",
                raw_response=body[:2000],
            )
        if resp.status_code >= 400:
            return DiagnosticResult(
                outcome="timeout" if resp.status_code == 504 else "error",
                error_code=f"DIAG_HTTP_{resp.status_code}",
                summary=f"Diagnostic service returned HTTP {resp.status_code}.",
                raw_response=body[:2000],
            )
        try:
            data = resp.json()
            overall = data["overall"]
            checks = data.get("checks", [])
        except (json.JSONDecodeError, ValueError, KeyError, TypeError):
            return DiagnosticResult(
                outcome="error",
                error_code="DIAG_MALFORMED_RESPONSE",
                summary="Diagnostic service returned an unreadable response.",
                raw_response=body[:2000],
            )
        if overall not in ("pass", "fail"):
            return DiagnosticResult(
                outcome="error",
                error_code="DIAG_MALFORMED_RESPONSE",
                summary=f"Unexpected overall status {overall!r}.",
                raw_response=body[:2000],
            )
        failing = [c for c in checks if c.get("status") == "fail"]
        if overall == "pass":
            summary = f"All {len(checks)} VPN checks passed on gateway {data.get('gateway')}."
        else:
            summary = "; ".join(f"{c['name']}: {c.get('detail', '')}" for c in failing) or "Diagnostic failed."
        return DiagnosticResult(
            outcome=overall,
            error_code=data.get("error_code"),
            summary=summary,
            checks=checks,
            service_run_id=data.get("diagnostic_id"),
            raw_response=body[:4000],
        )
