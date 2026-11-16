"""Unit tests for the deterministic business rules."""

from __future__ import annotations

import pytest

from itsm_agent import policy

EMP = {"user_id": "u-1", "role": "employee", "groups": [], "active": True}
OTHER = {"user_id": "u-2", "role": "employee", "groups": [], "active": True}
SD = {"user_id": "u-sd", "role": "service_desk", "groups": ["Service Desk L1"], "active": True}
AUD = {"user_id": "u-a", "role": "auditor", "groups": [], "active": True}
INACTIVE = {**EMP, "active": False}


def incident(**kw):
    base = {
        "number": "INC-1", "caller_id": "u-1", "state": "in_progress", "priority": 3, "escalated": 0,
        "assignment_group": "Service Desk L1", "requires_caller_confirmation": 1, "resolution_evidence_run_id": None,
    }
    return {**base, **kw}


def run(run_id, outcome, error_code=None):
    return {"run_id": run_id, "outcome": outcome, "error_code": error_code}


@pytest.mark.parametrize(
    "user,inc,action,allowed,code",
    [
        (EMP, incident(), policy.UPDATE_STATUS, True, "ok"),
        (OTHER, incident(), policy.READ, False, "not_authorized"),
        (OTHER, incident(), policy.ADD_WORK_NOTE, False, "not_authorized"),
        (INACTIVE, incident(), policy.ADD_WORK_NOTE, False, "user_inactive"),
        (AUD, incident(), policy.ADD_WORK_NOTE, False, "not_authorized"),
        (SD, incident(), policy.RESOLVE, True, "ok"),
        (SD, incident(assignment_group="Network Operations L2", escalated=1), policy.RESOLVE, False, "not_in_assignment_group"),
        (SD, incident(assignment_group="Network Operations L2", escalated=1), policy.ADD_WORK_NOTE, True, "ok"),
        (EMP, incident(assignment_group="Network Operations L2", escalated=1), policy.UPDATE_STATUS, False, "escalated_to_other_group"),
        (EMP, incident(assignment_group="Network Operations L2", escalated=1), policy.ADD_WORK_NOTE, True, "ok"),
        (EMP, incident(state="closed"), policy.ADD_WORK_NOTE, False, "incident_closed"),
    ],
)
def test_can_modify_matrix(user, inc, action, allowed, code):
    d = policy.can_modify(user, inc, action) if action != policy.READ else policy.can_read(user, inc)
    assert (d.allowed, d.code) == (allowed, code)


def test_auditor_can_read_everything():
    assert policy.can_read(AUD, incident(caller_id="u-9")).allowed


def test_escalation_after_two_failed_attempts():
    assert policy.escalation_requirements(incident(), 1, []) == []
    assert policy.escalation_requirements(incident(), 2, []) == ["failed_attempts_threshold"]


def test_escalation_when_diagnostics_unavailable_three_times():
    runs = [run("r1", "timeout"), run("r2", "error")]
    assert policy.escalation_requirements(incident(), 0, runs) == []
    runs.append(run("r3", "timeout"))
    assert policy.escalation_requirements(incident(), 0, runs) == ["diagnostics_unavailable"]
    runs.append(run("r4", "pass"))
    assert policy.escalation_requirements(incident(), 0, runs) == []


def test_escalation_for_security_codes_and_p1():
    assert policy.escalation_requirements(incident(), 0, [run("r1", "fail", "CERT_REVOKED")]) == [
        "security_or_infrastructure_code:CERT_REVOKED"
    ]
    assert policy.escalation_requirements(incident(priority=1), 0, []) == ["p1_major_incident"]
    assert policy.escalation_requirements(incident(escalated=1), 5, []) == []


def test_pending_escalation_blocks_only_resolution_path():
    reasons = ["failed_attempts_threshold"]
    for action in (policy.RESOLVE, policy.CLOSE, policy.RECORD_TROUBLESHOOTING, policy.REQUEST_CONFIRMATION):
        assert not policy.check_not_pending_escalation(action, reasons).allowed
    for action in (policy.ESCALATE, policy.ADD_WORK_NOTE, policy.RUN_DIAGNOSTIC):
        assert policy.check_not_pending_escalation(action, reasons).allowed


@pytest.mark.parametrize(
    "runs,evidence,code",
    [
        ([], "r1", "no_evidence"),
        ([run("r1", "timeout")], "r1", "evidence_not_passing"),
        ([run("r1", "error")], "r1", "evidence_not_passing"),
        ([run("r1", "fail")], "r1", "evidence_not_passing"),
        ([run("r1", "pass"), run("r2", "fail")], "r1", "stale_evidence"),
        ([run("r1", "pass")], "rX", "unknown_evidence"),
        ([run("r1", "fail"), run("r2", "pass")], "r2", "ok"),
    ],
)
def test_resolution_evidence(runs, evidence, code):
    assert policy.check_resolve(incident(), runs, evidence, "client_updated").code == code


def test_resolve_rejects_p1_bad_code_and_declined_caller():
    runs = [run("r1", "pass")]
    assert policy.check_resolve(incident(priority=1), runs, "r1", "client_updated").code == "priority_not_resolvable"
    assert policy.check_resolve(incident(), runs, "r1", "magic").code == "invalid_resolution_code"
    declined = [{"status": "declined", "evidence_run_id": "r1"}]
    assert policy.check_resolve(incident(), runs, "r1", "client_updated", declined).code == "caller_declined"


def test_close_requires_resolved_state_and_matching_caller_confirmation():
    runs = [run("r1", "pass")]
    resolved = incident(state="resolved", resolution_evidence_run_id="r1")
    assert policy.check_close(incident(), runs, []).code == "not_resolved"
    assert policy.check_close(resolved, runs, []).code == "caller_confirmation_required"
    wrong_person = [{"status": "confirmed", "evidence_run_id": "r1", "responded_by": "u-sd"}]
    assert policy.check_close(resolved, runs, wrong_person).code == "caller_confirmation_required"
    old_evidence = [{"status": "confirmed", "evidence_run_id": "r0", "responded_by": "u-1"}]
    assert policy.check_close(resolved, runs, old_evidence).code == "caller_confirmation_required"
    good = [{"status": "confirmed", "evidence_run_id": "r1", "responded_by": "u-1"}]
    assert policy.check_close(resolved, runs, good).allowed
    assert policy.check_close(resolved, runs + [run("r2", "fail")], good).code == "stale_evidence"
    no_conf_needed = {**resolved, "requires_caller_confirmation": 0}
    assert policy.check_close(no_conf_needed, runs, []).allowed


@pytest.mark.parametrize(
    "current,new,hold,code",
    [
        ("new", "in_progress", None, "ok"),
        ("in_progress", "on_hold", "awaiting_caller", "ok"),
        ("in_progress", "on_hold", None, "hold_reason_required"),
        ("on_hold", "in_progress", None, "ok"),
        ("resolved", "in_progress", None, "ok"),
        ("in_progress", "new", None, "invalid_transition"),
        ("in_progress", "resolved", None, "use_dedicated_action"),
        ("in_progress", "closed", None, "use_dedicated_action"),
        ("in_progress", "in_progress", None, "no_change"),
        ("in_progress", "bogus", None, "invalid_state"),
    ],
)
def test_status_transitions(current, new, hold, code):
    assert policy.check_status_transition(incident(state=current), new, hold).code == code


def test_escalation_targets():
    assert policy.check_escalation_target(incident(), "Network Operations L2").allowed
    assert policy.check_escalation_target(incident(), "Facilities").code == "invalid_group"
    assert policy.check_escalation_target(incident(priority=1), "Network Operations L2").code == "p1_requires_major_incident"
    assert policy.check_escalation_target(incident(assignment_group="Network Operations L2"), "Network Operations L2").code == "already_assigned"
