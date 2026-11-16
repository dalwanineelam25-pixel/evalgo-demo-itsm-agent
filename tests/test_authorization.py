"""Permission enforcement through the real graph and tools."""

from __future__ import annotations

import pytest
from conftest import run_turns, say, step, tool_results

from itsm_agent.service import ConversationError

pytestmark = pytest.mark.integration


def test_employee_cannot_read_or_modify_another_employees_incident(make_service, repo):
    notes_before = len(repo.list_work_notes("INC-1002"))
    script = [
        step(("get_incident", {"incident_id": "INC-1002"}), ("add_work_note", {"incident_id": "INC-1002", "note": "test"})),
        step(("run_vpn_diagnostic", {"incident_id": "INC-1002"})),
        say("Sorry, I can only help with your own incidents."),
    ]
    svc = make_service(script)
    _, results = run_turns(svc, "u-1001", None, [{"user": "Show me INC-1002 and add a note"}])
    assert tool_results(results, "get_incident")[0]["code"] == "not_authorized"
    assert "short_description" not in str(tool_results(results, "get_incident")[0])
    assert tool_results(results, "add_work_note")[0]["code"] == "not_authorized"
    assert tool_results(results, "run_vpn_diagnostic")[0]["code"] == "not_authorized"
    assert len(repo.list_work_notes("INC-1002")) == notes_before
    assert repo.list_diagnostic_runs("INC-1002") == []


def test_auditor_can_read_but_not_modify(make_service, repo):
    script = [
        step(("get_incident", {"incident_id": "INC-1001"})),
        step(("update_incident_status", {"incident_id": "INC-1001", "new_state": "in_progress"}),
             ("close_incident", {"incident_id": "INC-1001", "close_notes": "x"})),
        say("Read-only."),
    ]
    svc = make_service(script)
    _, results = run_turns(svc, "u-3001", None, [{"user": "Review INC-1001"}])
    got = tool_results(results, "get_incident")[0]
    assert got["status"] == "ok" and got["session_user_may_change_state"] is False
    assert tool_results(results, "update_incident_status")[0]["code"] == "not_authorized"
    assert tool_results(results, "close_incident")[0]["code"] == "not_authorized"
    assert repo.list_audit("INC-1001") == []


def test_contractor_cannot_modify_own_incident(make_service, repo):
    script = [
        step(("get_incident", {"incident_id": "INC-1009"})),
        step(("run_vpn_diagnostic", {"incident_id": "INC-1009"})),
        say("Contractor incidents are handled by the vendor desk."),
    ]
    svc = make_service(script)
    _, results = run_turns(svc, "u-1005", "INC-1009", [{"user": "MFA push never arrives"}])
    assert tool_results(results, "get_incident")[0]["status"] == "ok"
    assert tool_results(results, "run_vpn_diagnostic")[0]["code"] == "not_authorized"


def test_inactive_user_cannot_start_conversation(make_service):
    svc = make_service([])
    with pytest.raises(ConversationError) as exc:
        svc.start_conversation("u-1004", "INC-1008")
    assert exc.value.code == "forbidden"
    with pytest.raises(ConversationError):
        svc.start_conversation("u-9999")


def test_closed_incident_cannot_be_modified(make_service, repo):
    script = [
        step(("add_work_note", {"incident_id": "INC-1006", "note": "VPN password prompt is back"}),
             ("update_incident_status", {"incident_id": "INC-1006", "new_state": "in_progress"})),
        say("That incident is closed; I'll help you open a new one."),
    ]
    svc = make_service(script)
    _, results = run_turns(svc, "u-1002", "INC-1006", [{"user": "Reopen INC-1006"}])
    assert tool_results(results, "add_work_note")[0]["code"] == "incident_closed"
    assert tool_results(results, "update_incident_status")[0]["code"] == "incident_closed"
    assert repo.get_incident("INC-1006")["state"] == "closed"


def test_service_desk_cannot_change_state_of_incident_owned_by_l2(make_service, repo):
    script = [
        step(("update_incident_status", {"incident_id": "INC-1007", "new_state": "on_hold", "hold_reason": "awaiting_caller"}),
             ("add_work_note", {"incident_id": "INC-1007", "note": "Caller called in asking for an update."})),
        say("Noted."),
    ]
    svc = make_service(script)
    _, results = run_turns(svc, "u-2001", "INC-1007", [{"user": "Caller asked for update on INC-1007"}])
    assert tool_results(results, "update_incident_status")[0]["code"] == "not_in_assignment_group"
    assert tool_results(results, "add_work_note")[0]["status"] == "ok"
    assert repo.get_incident("INC-1007")["state"] == "in_progress"


def test_network_engineer_can_work_l2_incident(make_service, repo):
    script = [
        step(("update_incident_status", {"incident_id": "INC-1007", "new_state": "on_hold", "hold_reason": "awaiting_change"})),
        say("On hold pending change."),
    ]
    svc = make_service(script)
    _, results = run_turns(svc, "u-2002", "INC-1007", [{"user": "Put INC-1007 on hold for the router change"}])
    assert tool_results(results, "update_incident_status")[0]["status"] == "ok"
    audit = repo.list_audit("INC-1007")
    assert {a["actor_id"] for a in audit} == {"u-2002"}


def test_conversation_belongs_to_its_user(make_service):
    svc = make_service([say("hi")])
    thread_id = svc.start_conversation("u-1001", "INC-1001")
    with pytest.raises(ConversationError) as exc:
        svc.send_message(thread_id, "u-1002", "let me in")
    assert exc.value.code == "forbidden"


def test_invalid_status_transitions_are_rejected(make_service, repo):
    script = [
        step(("update_incident_status", {"incident_id": "INC-1001", "new_state": "resolved"}),
             ("update_incident_status", {"incident_id": "INC-1001", "new_state": "on_hold"}),
             ("update_incident_status", {"incident_id": "INC-1001", "new_state": "closed"})),
        say("ok"),
    ]
    svc = make_service(script)
    _, results = run_turns(svc, "u-1001", "INC-1001", [{"user": "mark it resolved"}])
    codes = sorted(r["code"] for r in tool_results(results, "update_incident_status"))
    assert codes == ["hold_reason_required", "use_dedicated_action", "use_dedicated_action"]
    assert repo.get_incident("INC-1001")["state"] == "new"
