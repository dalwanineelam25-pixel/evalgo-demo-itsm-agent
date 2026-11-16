"""Traces and business events (EvalGo Integration Spec S1, S2): what the agent reports, and what it never reports."""

from __future__ import annotations

import json

import pytest
from conftest import load_scenario, run_turns, say, step
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from itsm_agent import telemetry

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def exporter():
    exp = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    trace.set_tracer_provider(provider)
    return exp


@pytest.fixture
def spans(exporter):
    exporter.clear()
    return exporter.get_finished_spans


def events(spans) -> list[str]:
    return [e.name for s in spans() for e in s.events]


def test_every_tool_call_gets_an_execute_tool_span(make_service, spans):
    sc = load_scenario("vpn_happy_path")
    run_turns(make_service(sc["model_script"]), sc["user_id"], sc["incident_id"], sc["turns"])
    tools = [s for s in spans() if s.attributes.get("gen_ai.operation.name") == "execute_tool"]
    scripted = [c["name"] for m in sc["model_script"] for c in m.get("tool_calls", [])]
    assert [s.attributes["gen_ai.tool.name"] for s in tools] == scripted
    for s in tools:
        assert s.name == f"execute_tool {s.attributes['gen_ai.tool.name']}"
        assert len(s.attributes["evalgo.tool.args_sha256"]) == 64
        assert s.status.status_code == StatusCode.UNSET, s.name
    first = next(c for m in sc["model_script"] for c in m.get("tool_calls", []))
    assert tools[0].attributes["evalgo.tool.args_sha256"] == telemetry.args_sha256(first["args"])


def test_outcomes_are_business_events(make_service, spans):
    sc = load_scenario("vpn_happy_path")
    run_turns(make_service(sc["model_script"]), sc["user_id"], sc["incident_id"], sc["turns"])
    assert events(spans) == ["resolution_confirmed", "incident_resolved", "incident_closed"]
    confirmed = next(e for s in spans() for e in s.events if e.name == "resolution_confirmed")
    assert confirmed.attributes["itsm.incident.id"] == "INC-1001"
    assert confirmed.attributes["itsm.evidence_run.id"] == "DR-INC-1001-002"


def test_escalation_and_declined_confirmation_are_events(make_service, spans):
    sc = load_scenario("vpn_escalation_after_failures")
    run_turns(make_service(sc["model_script"]), sc["user_id"], sc["incident_id"], sc["turns"])
    assert "incident_escalated" in events(spans)


def test_a_rejected_tool_call_is_an_error_span(make_service, spans):
    svc = make_service([step(("close_incident", {"incident_id": "INC-1001", "close_notes": "done"})), say("Not yet.")])
    run_turns(svc, "u-1001", "INC-1001", [{"user": "close it"}])
    [s] = [s for s in spans() if s.name == "execute_tool close_incident"]
    assert s.status.status_code == StatusCode.ERROR
    assert s.attributes["error.type"] and s.attributes["evalgo.tool.outcome"] == "rejected"
    assert "incident_closed" not in events(spans)


def test_spans_never_carry_arguments_or_personal_data(make_service, spans, repo):
    sc = load_scenario("vpn_happy_path")
    run_turns(make_service(sc["model_script"]), sc["user_id"], sc["incident_id"], sc["turns"])
    text = json.dumps([{"a": dict(s.attributes), "e": [dict(e.attributes) for e in s.events]} for s in spans()], default=str)
    user = repo.get_user(sc["user_id"])
    assert user["name"] not in text and "@" not in text
    for m in sc["model_script"]:
        for c in m.get("tool_calls", []):
            for v in c["args"].values():
                if isinstance(v, str) and len(v) > 20:  # free text (notes, questions) must not leak into spans
                    assert v not in text
