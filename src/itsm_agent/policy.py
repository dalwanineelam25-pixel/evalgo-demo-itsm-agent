"""Deterministic business rules for incident handling.

All authorization and lifecycle checks live here and are enforced in code by the tools,
independently of what the language model decides.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

FAILED_ATTEMPT_THRESHOLD = 2
DIAGNOSTIC_FAILURE_THRESHOLD = 3
ESCALATION_ERROR_CODES = frozenset({"CERT_REVOKED", "ACCOUNT_LOCKED", "GATEWAY_OUTAGE"})
NON_RESOLVABLE_PRIORITIES = frozenset({1})

ESCALATION_GROUPS = (
    "Network Operations L2",
    "Identity & Access L2",
    "Endpoint Support L2",
    "Major Incident Management",
)
MAJOR_INCIDENT_GROUP = "Major Incident Management"

STATES = ("new", "in_progress", "on_hold", "resolved", "closed")
HOLD_REASONS = ("awaiting_caller", "awaiting_vendor", "awaiting_change")
# Transitions allowed through update_incident_status. resolved/closed are only reachable via
# resolve_incident / close_incident, which carry extra preconditions.
STATUS_TRANSITIONS = {
    "new": {"in_progress", "on_hold"},
    "in_progress": {"on_hold"},
    "on_hold": {"in_progress"},
    "resolved": {"in_progress"},  # reopen
    "closed": set(),
}
RESOLUTION_CODES = (
    "client_updated",
    "certificate_reenrolled",
    "mfa_reregistered",
    "dns_cache_cleared",
    "profile_reset",
    "network_environment",
    "password_reset",
    "outlook_profile_repaired",
    "transient_recovered",
    "other",
)

# Actions that change incident data.
READ = "read"
RUN_DIAGNOSTIC = "run_diagnostic"
ADD_WORK_NOTE = "add_work_note"
RECORD_TROUBLESHOOTING = "record_troubleshooting"
UPDATE_STATUS = "update_status"
ESCALATE = "escalate"
REQUEST_CONFIRMATION = "request_confirmation"
RESOLVE = "resolve"
CLOSE = "close"

# Actions still allowed on an incident that is escalated to a group the actor is not in.
ALLOWED_WHEN_ESCALATED_AWAY = frozenset({ADD_WORK_NOTE, RUN_DIAGNOSTIC})
# Actions blocked while a mandatory escalation is outstanding.
BLOCKED_PENDING_ESCALATION = frozenset({RECORD_TROUBLESHOOTING, REQUEST_CONFIRMATION, RESOLVE, CLOSE})

STAFF_ROLES = frozenset({"service_desk", "network_engineer"})
READ_ALL_ROLES = frozenset({"service_desk", "network_engineer", "auditor"})
SELF_SERVICE_MODIFY_ROLES = frozenset({"employee"})


@dataclass(frozen=True)
class Decision:
    allowed: bool
    code: str = "ok"
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls) -> "Decision":
        return cls(True)

    @classmethod
    def deny(cls, code: str, reason: str, **details: Any) -> "Decision":
        return cls(False, code, reason, details)


def can_read(user: dict | None, incident: dict) -> Decision:
    if user is None:
        return Decision.deny("unknown_user", "Unknown user.")
    if not user["active"]:
        return Decision.deny("user_inactive", "User account is inactive.")
    if user["role"] in READ_ALL_ROLES:
        return Decision.ok()
    if incident["caller_id"] == user["user_id"]:
        return Decision.ok()
    return Decision.deny("not_authorized", "Users may only view incidents they reported.")


def can_modify(user: dict | None, incident: dict, action: str) -> Decision:
    """Who may change this incident through the agent."""
    read = can_read(user, incident)
    if not read.allowed:
        return read
    assert user is not None
    role = user["role"]
    if role in ("auditor", "contractor", "service_account"):
        return Decision.deny(
            "not_authorized",
            f"Role '{role}' may not modify incidents through the agent.",
        )
    if incident["state"] == "closed":
        return Decision.deny("incident_closed", "Closed incidents cannot be modified. Open a new incident instead.")

    if role in SELF_SERVICE_MODIFY_ROLES:
        if incident["caller_id"] != user["user_id"]:
            return Decision.deny("not_authorized", "Employees may only work on incidents they reported.")
        if incident["escalated"] and action not in ALLOWED_WHEN_ESCALATED_AWAY:
            return Decision.deny(
                "escalated_to_other_group",
                f"Incident is escalated to {incident['assignment_group']}; only that team can change its state.",
            )
        return Decision.ok()

    if role in STAFF_ROLES:
        if incident["assignment_group"] in user["groups"]:
            return Decision.ok()
        if action == ADD_WORK_NOTE:
            return Decision.ok()
        return Decision.deny(
            "not_in_assignment_group",
            f"Incident is assigned to {incident['assignment_group']}, which you are not a member of.",
        )
    return Decision.deny("not_authorized", f"Role '{role}' may not modify incidents.")


def escalation_requirements(
    incident: dict, troubleshooting_failures: int, diagnostic_runs: list[dict]
) -> list[str]:
    """Reason codes that make escalation mandatory. Empty list => no mandatory escalation."""
    if incident["escalated"]:
        return []
    reasons: list[str] = []
    if incident["priority"] in NON_RESOLVABLE_PRIORITIES:
        reasons.append("p1_major_incident")
    if troubleshooting_failures >= FAILED_ATTEMPT_THRESHOLD:
        reasons.append("failed_attempts_threshold")
    recent = diagnostic_runs[-DIAGNOSTIC_FAILURE_THRESHOLD:]
    if len(recent) == DIAGNOSTIC_FAILURE_THRESHOLD and all(r["outcome"] in ("timeout", "error") for r in recent):
        reasons.append("diagnostics_unavailable")
    if diagnostic_runs and diagnostic_runs[-1].get("error_code") in ESCALATION_ERROR_CODES:
        reasons.append(f"security_or_infrastructure_code:{diagnostic_runs[-1]['error_code']}")
    return reasons


def check_not_pending_escalation(action: str, reasons: list[str]) -> Decision:
    if reasons and action in BLOCKED_PENDING_ESCALATION:
        return Decision.deny(
            "escalation_required",
            "Escalation is mandatory before any further troubleshooting, confirmation, resolution or closure.",
            escalation_reasons=reasons,
        )
    return Decision.ok()


def check_status_transition(incident: dict, new_state: str, hold_reason: str | None) -> Decision:
    current = incident["state"]
    if new_state in ("resolved", "closed"):
        return Decision.deny(
            "use_dedicated_action",
            f"State '{new_state}' can only be set with the {'resolve_incident' if new_state == 'resolved' else 'close_incident'} tool.",
        )
    if new_state not in STATES:
        return Decision.deny("invalid_state", f"Unknown state '{new_state}'. Valid: {', '.join(STATES)}.")
    if new_state == current:
        return Decision.deny("no_change", f"Incident is already '{current}'.")
    if new_state not in STATUS_TRANSITIONS.get(current, set()):
        return Decision.deny("invalid_transition", f"Cannot move from '{current}' to '{new_state}'.")
    if new_state == "on_hold" and hold_reason not in HOLD_REASONS:
        return Decision.deny("hold_reason_required", f"on_hold requires hold_reason in {HOLD_REASONS}.")
    return Decision.ok()


def latest_run(diagnostic_runs: list[dict]) -> dict | None:
    return diagnostic_runs[-1] if diagnostic_runs else None


def check_resolution_evidence(diagnostic_runs: list[dict], evidence_run_id: str | None) -> Decision:
    """Evidence = the most recent diagnostic run for the incident, and it passed."""
    last = latest_run(diagnostic_runs)
    if last is None:
        return Decision.deny("no_evidence", "No diagnostic run exists for this incident; run the VPN diagnostic first.")
    if evidence_run_id is not None and evidence_run_id != last["run_id"]:
        known = {r["run_id"] for r in diagnostic_runs}
        if evidence_run_id not in known:
            return Decision.deny("unknown_evidence", f"Diagnostic run {evidence_run_id} does not belong to this incident.")
        return Decision.deny(
            "stale_evidence",
            f"Run {evidence_run_id} is not the most recent diagnostic run (latest is {last['run_id']}, outcome {last['outcome']}).",
        )
    if last["outcome"] != "pass":
        return Decision.deny(
            "evidence_not_passing",
            f"Latest diagnostic run {last['run_id']} has outcome '{last['outcome']}'. Only a passing run is resolution evidence; "
            "timeouts and errors never count as success.",
        )
    return Decision.ok()


def check_resolve(
    incident: dict,
    diagnostic_runs: list[dict],
    evidence_run_id: str,
    resolution_code: str,
    confirmations: list[dict] | None = None,
) -> Decision:
    if incident["priority"] in NON_RESOLVABLE_PRIORITIES:
        return Decision.deny("priority_not_resolvable", "P1 incidents are resolved by Major Incident Management, not the agent.")
    if incident["state"] not in ("new", "in_progress", "on_hold"):
        return Decision.deny("invalid_transition", f"Cannot resolve an incident in state '{incident['state']}'.")
    if resolution_code not in RESOLUTION_CODES:
        return Decision.deny("invalid_resolution_code", f"resolution_code must be one of {', '.join(RESOLUTION_CODES)}.")
    evidence = check_resolution_evidence(diagnostic_runs, evidence_run_id)
    if not evidence.allowed:
        return evidence
    if any(c["status"] == "declined" and c["evidence_run_id"] == evidence_run_id for c in confirmations or []):
        return Decision.deny(
            "caller_declined",
            f"The caller said the issue is not fixed after diagnostic {evidence_run_id}. Continue troubleshooting "
            "and obtain new evidence, or escalate.",
        )
    return Decision.ok()


def check_close(incident: dict, diagnostic_runs: list[dict], confirmations: list[dict]) -> Decision:
    if incident["priority"] in NON_RESOLVABLE_PRIORITIES:
        return Decision.deny("priority_not_resolvable", "P1 incidents are closed by Major Incident Management, not the agent.")
    if incident["state"] != "resolved":
        return Decision.deny("not_resolved", f"Only resolved incidents can be closed (current state '{incident['state']}').")
    evidence = check_resolution_evidence(diagnostic_runs, incident["resolution_evidence_run_id"])
    if not evidence.allowed:
        return evidence
    if False and incident["requires_caller_confirmation"]:  # MUTANT: confirmation check removed
        ok = [
            c
            for c in confirmations
            if c["status"] == "confirmed"
            and c["evidence_run_id"] == incident["resolution_evidence_run_id"]
            and c["responded_by"] == incident["caller_id"]
        ]
        if not ok:
            return Decision.deny(
                "caller_confirmation_required",
                "The caller has not confirmed the fix for the current resolution evidence. Use request_caller_confirmation.",
            )
    return Decision.ok()


def check_confirmation_request(incident: dict, diagnostic_runs: list[dict]) -> Decision:
    if incident["state"] not in ("new", "in_progress", "on_hold", "resolved"):
        return Decision.deny("invalid_state", f"Cannot request confirmation in state '{incident['state']}'.")
    return check_resolution_evidence(diagnostic_runs, None)


def check_escalation_target(incident: dict, target_group: str) -> Decision:
    if target_group not in ESCALATION_GROUPS:
        return Decision.deny("invalid_group", f"target_group must be one of: {', '.join(ESCALATION_GROUPS)}.")
    if target_group == incident["assignment_group"]:
        return Decision.deny("already_assigned", f"Incident is already assigned to {target_group}.")
    if incident["priority"] in NON_RESOLVABLE_PRIORITIES and target_group != MAJOR_INCIDENT_GROUP:
        return Decision.deny("p1_requires_major_incident", "P1 incidents must be escalated to Major Incident Management.")
    return Decision.ok()
