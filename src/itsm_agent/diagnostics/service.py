"""Local mock of the corporate VPN diagnostic service (fictional).

Run with ``itsm-agent diag-service``. It serves:

* ``GET  /health``
* ``POST /v1/vpn/diagnose``  body: ``{"device_id": "LT-4821", "requested_by": "u-1001"}``

Device behaviour is scripted in ``data/diagnostic_profiles.json``. Each call for a device returns
the next scripted state (the last one repeats), which simulates the device changing after the
employee follows troubleshooting steps. ``timeout`` entries sleep longer than the client timeout,
``http_error`` returns HTTP 503 and ``malformed`` returns a non-JSON body.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ..config import DATA_DIR

log = logging.getLogger("itsm_agent.diag_service")

DEFAULT_PROFILES = DATA_DIR / "diagnostic_profiles.json"
CHECK_NAMES = (
    "network_reachability",
    "dns_resolution",
    "gateway_reachability",
    "tls_certificate",
    "authentication",
    "client_version",
    "tunnel_stability",
)


class DiagnosticBackend:
    def __init__(self, profiles: dict[str, Any], timeout_delay_s: float = 30.0):
        self.gateway = profiles.get("gateway", "vpn-eu1.northwind.example")
        self.devices: dict[str, list[dict]] = profiles["devices"]
        self.timeout_delay_s = timeout_delay_s
        self._calls: dict[str, int] = {}
        self._lock = threading.Lock()
        self._seq = 0

    @classmethod
    def from_file(cls, path: Path | str = DEFAULT_PROFILES, timeout_delay_s: float = 30.0) -> "DiagnosticBackend":
        return cls(json.loads(Path(path).read_text()), timeout_delay_s=timeout_delay_s)

    def next_behavior(self, device_id: str) -> dict | None:
        script = self.devices.get(device_id)
        if script is None:
            return None
        with self._lock:
            n = self._calls.get(device_id, 0)
            self._calls[device_id] = n + 1
            self._seq += 1
        return script[min(n, len(script) - 1)]

    def build_report(self, device_id: str, behavior: dict) -> dict[str, Any]:
        with self._lock:
            seq = self._seq
        failing = behavior.get("check") if behavior["behavior"] == "fail" else None
        checks = []
        for name in CHECK_NAMES:
            if name == failing:
                checks.append({"name": name, "status": "fail", "detail": behavior.get("detail", "")})
            elif failing and CHECK_NAMES.index(name) > CHECK_NAMES.index(failing):
                checks.append({"name": name, "status": "skipped", "detail": f"Skipped because {failing} failed."})
            else:
                checks.append({"name": name, "status": "pass", "detail": "OK"})
        return {
            "diagnostic_id": f"VPD-{seq:05d}",
            "device_id": device_id,
            "gateway": self.gateway,
            "overall": "fail" if failing else "pass",
            "error_code": behavior.get("error_code") if failing else None,
            "checks": checks,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }


def _make_handler(backend: DiagnosticBackend):
    class Handler(BaseHTTPRequestHandler):
        server_version = "NorthwindVPNDiag/1.4"

        def log_message(self, fmt: str, *args: Any) -> None:  # route to logging instead of stderr
            log.info("%s %s", self.address_string(), fmt % args)

        def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                log.info("client went away before response was written")

        def _json(self, status: int, payload: dict) -> None:
            self._send(status, json.dumps(payload).encode())

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._json(200, {"status": "ok"})
            else:
                self._json(404, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/v1/vpn/diagnose":
                self._json(404, {"error": "not_found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                req = json.loads(self.rfile.read(length) or b"{}")
                device_id = req["device_id"]
            except (ValueError, KeyError):
                self._json(400, {"error": "bad_request", "message": "device_id is required"})
                return
            behavior = backend.next_behavior(device_id)
            if behavior is None:
                self._json(404, {"error": "device_not_found", "device_id": device_id})
                return
            kind = behavior["behavior"]
            log.info("diagnose device=%s behavior=%s", device_id, kind)
            if kind == "timeout":
                time.sleep(behavior.get("delay_s", backend.timeout_delay_s))
                self._json(504, {"error": "probe_timeout"})
            elif kind == "http_error":
                self._json(int(behavior.get("status", 503)), {"error": "service_unavailable"})
            elif kind == "malformed":
                self._send(200, b"<html>upstream proxy error</html>", "text/html")
            else:
                self._json(200, backend.build_report(device_id, behavior))

    return Handler


class DiagnosticServer:
    """Threaded HTTP server wrapper; usable in-process (tests) or as a standalone process."""

    def __init__(self, backend: DiagnosticBackend, host: str = "127.0.0.1", port: int = 8765):
        self.backend = backend
        self.httpd = ThreadingHTTPServer((host, port), _make_handler(backend))
        self.httpd.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def start_background(self) -> "DiagnosticServer":
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="diag-service", daemon=True)
        self._thread.start()
        return self

    def serve_forever(self) -> None:
        self.httpd.serve_forever()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
