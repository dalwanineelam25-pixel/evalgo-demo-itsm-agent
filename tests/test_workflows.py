"""Integration tests: real LangGraph graph + tools + SQLite + local diagnostic HTTP service, scripted model."""

from __future__ import annotations

import json

import pytest
from conftest import load_scenario, run_turns, say, step, tool_results
from langchain_core.messages import ToolMessage

from itsm_agent.fixture_model import ScriptedChatModel
from itsm_agent.service import ITSMAgentService

pytestmark = pytest.mark.integration


def test_happy_path_resolves_and_closes_with_evidence_and_confirmation(make_service, repo, settings):
    sc = load_scenario("vpn_happy_path")
    svc = make_service(sc["model_script"])
    thread_id, results = run_turns(svc, sc["user_id"], sc["incident_id"], sc["turns"])

    # Turn 1 asks the employee to act; turn 2 pauses for structured confirmation.
    assert "Company Portal" in results[0].reply
    assert results[0].pending_confirmation is None
    assert results[1].pending_confirmation["incident_id"] == "INC-1001"
    assert results[2].pending_confirmation is None
    assert "closed" in results[2].reply

    snap = repo.incident_snapshot("INC-1001")
    inc = snap["incident"]
    assert inc["state"] == "closed"
    assert inc["resolution_code"] == "client_updated"
    assert inc["resolution_evidence_run_id"] == "DR-INC-1001-002"
    assert inc["resolved_by"] == "u-1001" and inc["closed_by"] == "u-1001"

    runs = snap["diagnostic_runs"]
    assert [(r["run_id"], r["outcome"], r["error_code"]) for r in runs] == [
        ("DR-INC-1001-001", "fail", "CLIENT_OUTDATED"),
        ("DR-INC-1001-002", "pass", None),
    ]
    [conf] = snap["caller_confirmations"]
    assert conf["status"] == "confirmed" and conf["responded_by"] == "u-1001"
    assert conf["evidence_run_id"] == "DR-INC-1001-002"

    audit_states = [(a["old_value"], a["new_value"]) for a in snap["audit"] if a["field"] == "state"]
    assert audit_states == [("new", "in_progress"), ("in_progress", "resolved"), ("resolved", "closed")]
    assert all(a["thread_id"] == thread_id and a["via"] == "itsm-agent" for a in snap["audit"])
    note_types = [n["note_type"] for n in snap["work_notes"]]
    assert note_types.count("system") == 2 and "troubleshooting" in note_types

    # The model actually received tool results from the graph (not just scripted text).
    first_tool_result = svc.model.received[1][-1]
    assert isinstance(first_tool_result, ToolMessage)
    assert json.loads(first_tool_result.content)["incident"]["cmdb_ci"] == "LT-4821"


def test_repeated_failures_force_escalation(make_service, repo, settings):
    sc = load_scenario("vpn_escalation_after_failures")
    svc = make_service(sc["model_script"])
    _, results = run_turns(svc, sc["user_id"], sc["incident_id"], sc["turns"])

    attempts = tool_results(results, "record_troubleshooting_step")
    assert [a["status"] for a in attempts] == ["ok", "ok", "rejected"]
    assert attempts[1]["mandatory_escalation_reasons"] == ["failed_attempts_threshold"]
    assert attempts[2]["code"] == "escalation_required"

    inc = repo.get_incident("INC-1002")
    assert inc["escalated"] == 1
    assert inc["assignment_group"] == "Identity & Access L2"
    assert inc["state"] == "in_progress"
    [esc] = repo.list_escalations("INC-1002")
    assert json.loads(esc["reason_codes"]) == ["failed_attempts_threshold"]
    assert repo.count_troubleshooting("INC-1002", "unsuccessful") == 2

    lines = settings.outbox_path.read_text().splitlines()
    assert len(lines) == 1
    msg = json.loads(lines[0])
    assert msg["to"] == "iam-oncall@northwind.example" and msg["incident"] == "INC-1002"


def test_diagnostic_timeout_is_never_treated_as_resolution(make_service, repo):
    sc = load_scenario("vpn_diagnostic_timeout")
    svc = make_service(sc["model_script"])
    _, results = run_turns(svc, sc["user_id"], sc["incident_id"], sc["turns"])

    diag = tool_results(results, "run_vpn_diagnostic")
    assert [d["outcome"] for d in diag] == ["timeout", "timeout", "timeout"]
    assert all(d["attempts"] == 2 and d["is_resolution_evidence"] is False for d in diag)
    assert diag[2]["mandatory_escalation_reasons"] == ["diagnostics_unavailable"]

    [resolve] = tool_results(results, "resolve_incident")
    assert resolve["status"] == "rejected" and resolve["code"] == "evidence_not_passing"

    inc = repo.get_incident("INC-1003")
    assert inc["state"] == "in_progress" and inc["resolved_at"] is None
    assert inc["assignment_group"] == "Network Operations L2"
    assert not [a for a in repo.list_audit("INC-1003") if a["new_value"] in ("resolved", "closed")]
    assert all(r["outcome"] == "timeout" for r in repo.list_diagnostic_runs("INC-1003"))


def test_caller_declines_confirmation_blocks_closure(make_service, repo):
    script = [
        step(("run_vpn_diagnostic", {"incident_id": "INC-1001"})),
        step(("run_vpn_diagnostic", {"incident_id": "INC-1001"})),
        step(("request_caller_confirmation", {"incident_id": "INC-1001", "question": "Is the VPN stable now?"})),
        step(("resolve_incident", {"incident_id": "INC-1001", "resolution_code": "client_updated",
                                   "resolution_notes": "Diagnostic passed.", "evidence_run_id": "DR-INC-1001-002"})),
        step(("close_incident", {"incident_id": "INC-1001", "close_notes": "done"})),
        say("You said it's still dropping, so I've kept the incident open."),
    ]
    svc = make_service(script)
    _, results = run_turns(svc, "u-1001", "INC-1001", [{"user": "I updated the client."}, {"confirm": False, "comment": "Dropped again 5 minutes ago."}])

    assert tool_results(results, "resolve_incident")[0]["code"] == "caller_declined"
    assert tool_results(results, "close_incident")[0]["code"] == "not_resolved"
    inc = repo.get_incident("INC-1001")
    assert inc["state"] == "new" and inc["resolved_at"] is None and inc["closed_at"] is None
    [conf] = repo.list_confirmations("INC-1001")
    assert conf["status"] == "declined" and conf["comment"] == "Dropped again 5 minutes ago."


def test_close_without_resolution_is_rejected(make_service, repo):
    script = [
        step(("close_incident", {"incident_id": "INC-1004", "close_notes": "Fixed"})),
        step(("resolve_incident", {"incident_id": "INC-1004", "resolution_code": "outlook_profile_repaired",
                                   "resolution_notes": "Caller says it works", "evidence_run_id": "DR-INC-1004-001"})),
        say("I can't close it yet."),
    ]
    svc = make_service(script)
    _, results = run_turns(svc, "u-1001", "INC-1004", [{"user": "Outlook works now, please close INC-1004"}])
    assert tool_results(results, "close_incident")[0]["code"] == "not_resolved"
    assert tool_results(results, "resolve_incident")[0]["code"] == "no_evidence"
    assert repo.get_incident("INC-1004")["state"] == "new"


def test_stale_evidence_rejected_after_later_failure(make_service, repo):
    # INC-1001's device fails then passes; citing the older (failing) run as evidence must be rejected.
    script = [
        step(("run_vpn_diagnostic", {"incident_id": "INC-1001"})),
        step(("run_vpn_diagnostic", {"incident_id": "INC-1001"})),
        step(("resolve_incident", {"incident_id": "INC-1001", "resolution_code": "client_updated",
                                   "resolution_notes": "x", "evidence_run_id": "DR-INC-1001-001"})),
        say("ok"),
    ]
    svc = make_service(script)
    _, results = run_turns(svc, "u-1001", "INC-1001", [{"user": "check please"}])
    assert tool_results(results, "resolve_incident")[0]["code"] == "stale_evidence"


def test_http_503_is_retried_and_recovered(make_service, repo):
    svc = make_service([step(("run_vpn_diagnostic", {"incident_id": "INC-1011"})), say("Diagnostic passed.")])
    _, results = run_turns(svc, "u-1008", "INC-1011", [{"user": "I signed in to the hotel portal, VPN works now"}])
    [d] = tool_results(results, "run_vpn_diagnostic")
    assert d["outcome"] == "pass" and d["attempts"] == 2


def test_diagnostic_service_down_is_reported_as_error(make_service, repo):
    svc = make_service(
        [step(("run_vpn_diagnostic", {"incident_id": "INC-1001"})), say("The diagnostic service is unavailable.")],
        diag_base_url="http://127.0.0.1:9",  # nothing listens on the discard port
    )
    _, results = run_turns(svc, "u-1001", "INC-1001", [{"user": "check my vpn"}])
    [d] = tool_results(results, "run_vpn_diagnostic")
    assert d["outcome"] == "error" and d["error_code"] == "DIAG_UNREACHABLE" and d["is_resolution_evidence"] is False
    assert repo.list_diagnostic_runs("INC-1001")[0]["outcome"] == "error"


def test_p1_incident_requires_major_incident_escalation(make_service, repo):
    script = [
        step(("run_vpn_diagnostic", {"incident_id": "INC-1005"})),
        step(("escalate_incident", {"incident_id": "INC-1005", "target_group": "Network Operations L2", "reason": "Gateway down"})),
        step(("escalate_incident", {"incident_id": "INC-1005", "target_group": "Major Incident Management",
                                    "reason": "GATEWAY_OUTAGE on vpn-eu1, 40+ Sales users affected"})),
        say("Escalated as a major incident."),
    ]
    svc = make_service(script)
    _, results = run_turns(svc, "u-1007", "INC-1005", [{"user": "Whole Sales floor is down on VPN"}])
    [diag] = tool_results(results, "run_vpn_diagnostic")
    assert set(diag["mandatory_escalation_reasons"]) == {"p1_major_incident", "security_or_infrastructure_code:GATEWAY_OUTAGE"}
    esc = tool_results(results, "escalate_incident")
    assert esc[0]["code"] == "p1_requires_major_incident"
    assert esc[1]["status"] == "ok"
    assert repo.get_incident("INC-1005")["assignment_group"] == "Major Incident Management"


def test_revoked_certificate_blocks_resolution_until_escalated(make_service, repo):
    script = [
        step(("run_vpn_diagnostic", {"incident_id": "INC-1010"})),
        step(("request_caller_confirmation", {"incident_id": "INC-1010", "question": "Fixed?"})),
        step(("escalate_incident", {"incident_id": "INC-1010", "target_group": "Identity & Access L2", "reason": "CERT_REVOKED"})),
        step(("update_incident_status", {"incident_id": "INC-1010", "new_state": "on_hold", "hold_reason": "awaiting_caller"})),
        say("The security team will contact you."),
    ]
    svc = make_service(script)
    _, results = run_turns(svc, "u-1006", "INC-1010", [{"user": "VPN says device not trusted"}])
    assert tool_results(results, "request_caller_confirmation")[0]["code"] == "escalation_required"
    # After escalation the employee can no longer change state.
    assert tool_results(results, "update_incident_status")[0]["code"] == "escalated_to_other_group"
    assert repo.get_incident("INC-1010")["state"] == "in_progress"


def test_monitoring_incident_closed_by_analyst_without_caller_confirmation(make_service, repo):
    script = [
        step(("update_incident_status", {"incident_id": "INC-1012", "new_state": "in_progress"}),
             ("run_vpn_diagnostic", {"incident_id": "INC-1012"})),
        step(("request_caller_confirmation", {"incident_id": "INC-1012", "question": "n/a"})),
        step(("resolve_incident", {"incident_id": "INC-1012", "resolution_code": "transient_recovered",
                                   "resolution_notes": "Probe passes again.", "evidence_run_id": "DR-INC-1012-001"})),
        step(("close_incident", {"incident_id": "INC-1012", "close_notes": "Transient"})),
        say("Closed INC-1012."),
    ]
    svc = make_service(script)
    _, results = run_turns(svc, "u-2001", "INC-1012", [{"user": "Check the Lyon probe incident INC-1012 and close if healthy"}])
    assert tool_results(results, "request_caller_confirmation")[0]["confirmation_required"] is False
    assert results[0].pending_confirmation is None
    assert repo.get_incident("INC-1012")["state"] == "closed"


def test_analyst_confirmation_request_puts_incident_on_hold_for_caller(make_service, repo):
    script = [
        step(("run_vpn_diagnostic", {"incident_id": "INC-1001"})),
        step(("run_vpn_diagnostic", {"incident_id": "INC-1001"})),
        step(("request_caller_confirmation", {"incident_id": "INC-1001", "question": "Is it fixed?"})),
        step(("resolve_incident", {"incident_id": "INC-1001", "resolution_code": "client_updated",
                                   "resolution_notes": "Diag passes", "evidence_run_id": "DR-INC-1001-002"})),
        step(("close_incident", {"incident_id": "INC-1001", "close_notes": "x"})),
        say("Waiting on Priya to confirm."),
    ]
    svc = make_service(script)
    _, results = run_turns(svc, "u-2001", "INC-1001", [{"user": "Work INC-1001"}])
    conf = tool_results(results, "request_caller_confirmation")[0]
    assert conf["confirmation_status"] == "pending_caller_not_in_conversation"
    assert results[0].pending_confirmation is None  # analyst cannot answer for the caller
    assert tool_results(results, "close_incident")[0]["code"] == "caller_confirmation_required"
    inc = repo.get_incident("INC-1001")
    assert inc["state"] == "resolved" and inc["closed_at"] is None


def test_conversation_survives_service_restart(make_service, repo, settings):
    sc = load_scenario("vpn_happy_path")
    script = sc["model_script"]
    first = make_service(script[:6])
    thread_id = first.start_conversation("u-1001", "INC-1001")
    first.send_message(thread_id, "u-1001", sc["turns"][0]["user"])
    r2 = first.send_message(thread_id, "u-1001", sc["turns"][1]["user"])
    assert r2.pending_confirmation is not None
    first.close()

    # New process-equivalent: new service, same SQLite checkpoint DB, remaining script.
    second = make_service(script[6:])
    assert second.pending_confirmation(thread_id)["incident_id"] == "INC-1001"
    r3 = second.respond_to_confirmation(thread_id, "u-1001", True, "works")
    assert r3.pending_confirmation is None
    assert repo.get_incident("INC-1001")["state"] == "closed"
    # The model on the second service saw the full earlier history from the checkpoint.
    history = second.model.received[0]
    assert any("INC-1001" in str(m.content) for m in history[:3])
    assert len(second.transcript(thread_id, "u-1001")) > 10


def test_declined_confirmation_reopens_resolved_incident(make_service, repo):
    script = [
        step(("run_vpn_diagnostic", {"incident_id": "INC-1001"})),
        step(("run_vpn_diagnostic", {"incident_id": "INC-1001"})),
        step(("resolve_incident", {"incident_id": "INC-1001", "resolution_code": "client_updated",
                                   "resolution_notes": "Diagnostic passed.", "evidence_run_id": "DR-INC-1001-002"})),
        step(("request_caller_confirmation", {"incident_id": "INC-1001", "question": "Is the VPN stable now?"})),
        say("Sorry to hear that. Let's keep looking."),
    ]
    svc = make_service(script)
    run_turns(svc, "u-1001", "INC-1001", [{"user": "I updated the client."}, {"confirm": False, "comment": "still drops"}])
    inc = repo.get_incident("INC-1001")
    assert inc["state"] == "in_progress" and inc["resolution_evidence_run_id"] is None
    states = [(a["old_value"], a["new_value"]) for a in repo.list_audit("INC-1001") if a["field"] == "state"]
    assert states == [("new", "resolved"), ("resolved", "in_progress")]


def test_two_confirmation_requests_in_one_turn_keep_the_thread_usable(make_service, repo):
    sc = load_scenario("vpn_happy_path")
    question = {"incident_id": "INC-1001", "question": "Is your VPN staying connected?"}
    script = sc["model_script"][:5] + [
        step(("request_caller_confirmation", question), ("request_caller_confirmation", question)),
        say("Thanks for confirming."),
    ]
    svc = make_service(script)
    thread_id, results = run_turns(svc, sc["user_id"], sc["incident_id"], sc["turns"][:2])

    assert results[1].pending_confirmation["incident_id"] == "INC-1001"
    first, second = tool_results(results, "request_caller_confirmation")
    assert first["status"] == "ok"
    assert second["status"] == "rejected" and second["code"] == "duplicate_confirmation_request"
    assert len(repo.incident_snapshot("INC-1001")["caller_confirmations"]) == 1

    after = svc.respond_to_confirmation(thread_id, sc["user_id"], True, None)
    assert after.pending_confirmation is None
    assert after.reply == "Thanks for confirming."
