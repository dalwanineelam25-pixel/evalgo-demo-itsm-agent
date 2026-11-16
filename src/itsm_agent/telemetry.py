"""OpenTelemetry traces for tool calls and business outcomes (EvalGo Integration Spec S1, S2).

Off unless OTEL_EXPORTER_OTLP_ENDPOINT is set; then spans go over OTLP/HTTP, configured
by the standard OTEL_* environment variables. Tool spans follow OpenTelemetry's GenAI
conventions for executing a tool:
https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/execute-tool.md

Spans carry identifiers only: never tool arguments, user names, emails or secrets.
"""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Iterator

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

AGENT_ID = "itsm-agent"
_tracer = trace.get_tracer("itsm_agent")
_configured = False


def _version() -> str:
    try:
        return version("itsm-agent")
    except PackageNotFoundError:
        return "unknown"


def setup() -> None:
    """Install the OTLP exporter when an endpoint is configured. Safe to call more than once."""
    global _configured
    if _configured or not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    # Resource.create also reads OTEL_RESOURCE_ATTRIBUTES and OTEL_SERVICE_NAME.
    resource = Resource.create({"service.name": AGENT_ID, "service.version": _version(), "evalgo.agent_id": AGENT_ID})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    _configured = True


def args_sha256(args: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()


@contextmanager
def tool_span(name: str, args: dict[str, Any], incident_id: str | None) -> Iterator[Span]:
    attributes = {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name,
                  "evalgo.tool.args_sha256": args_sha256(args)}
    if incident_id:
        attributes["itsm.incident.id"] = incident_id
    with _tracer.start_as_current_span(f"execute_tool {name}", kind=trace.SpanKind.INTERNAL, attributes=attributes,
                                       record_exception=False, set_status_on_exception=True) as span:
        yield span


def record_tool_result(span: Span, status: str | None, code: str | None) -> None:
    """A tool's JSON status: ok / awaiting_confirmation succeed; anything else is a failed call."""
    span.set_attribute("evalgo.tool.outcome", status or "unknown")
    if status not in ("ok", "awaiting_confirmation"):
        span.set_attribute("error.type", code or status or "unknown")
        span.set_status(Status(StatusCode.ERROR, code or status or "unknown"))


@contextmanager
def outcome_span(name: str, incident_id: str | None) -> Iterator[Span]:
    """A span for a decision made outside a tool (the caller's yes/no)."""
    with _tracer.start_as_current_span(name, attributes={"itsm.incident.id": incident_id} if incident_id else {}) as span:
        yield span


def business_event(name: str, attributes: dict[str, Any]) -> None:
    """A named business outcome on the span that decided it (Integration Spec S2; declared in evalgo.yaml)."""
    trace.get_current_span().add_event(name, {k: v for k, v in attributes.items() if v is not None})
