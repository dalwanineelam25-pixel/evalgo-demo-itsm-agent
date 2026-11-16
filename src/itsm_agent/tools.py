"""LangChain tools the agent can call. Every data change is authorized and validated in code."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt import ToolRuntime
from langgraph.types import Command

from . import policy, telemetry
from .db import Repository, utcnow
from .diagnostics import VPNDiagnosticClient
from .kb import KnowledgeBase

log = logging.getLogger(__name__)

GROUP_ALIASES = {
    "Network Operations L2": "netops-oncall@northwind.example",
    "Identity & Access L2": "iam-oncall@northwind.example",
    "Endpoint Support L2": "endpoint-l2@northwind.example",
    "Major Incident Management": "major-incident@northwind.example",
}
TROUBLESHOOTING_OUTCOMES = ("successful", "unsuccessful", "inconclusive")
MAX_NOTE_LENGTH = 4000


@dataclass
class ToolContext:
    repo: Repository
    kb: KnowledgeBase
    diagnostics: VPNDiagnosticClient
    outbox_path: Path


@dataclass
class Session:
    actor_id: str
    thread_id: str | None
    user: dict | None


def _json(status: str, **data: Any) -> str:
    return json.dumps({"status": status, **data}, default=str)


def _rejected(decision: policy.Decision) -> str:
    return _json("rejected", code=decision.code, reason=decision.reason, **decision.details)


def build_tools(ctx: ToolContext) -> list[BaseTool]:
    repo, kb = ctx.repo, ctx.kb

    def session_of(runtime: ToolRuntime) -> Session:
        actor_id = runtime.state["actor_id"]
        thread_id = (runtime.config or {}).get("configurable", {}).get("thread_id")
        return Session(actor_id, thread_id, repo.get_user(actor_id))

    def call_args(runtime: ToolRuntime) -> dict[str, Any]:
        """This call's arguments as the model sent them (for the trace's arguments hash)."""
        calls = getattr(runtime.state["messages"][-1], "tool_calls", None) or []
        return next((tc["args"] for tc in calls if tc["id"] == runtime.tool_call_id), {})

    def guarded(name: str, runtime: ToolRuntime, incident_id: str | None, fn: Callable[[Session], Any]) -> Any:
        s = session_of(runtime)
        with telemetry.tool_span(name, call_args(runtime), incident_id) as span:
            try:
                result = fn(s)
            except sqlite3.Error:
                log.exception("tool=%s incident=%s actor=%s database error", name, incident_id, s.actor_id)
                result = _json("error", code="database_error", reason="Database error; no change was made.")
            except Exception:  # noqa: BLE001 - never crash the conversation on an unexpected tool bug
                log.exception("tool=%s incident=%s actor=%s unexpected error", name, incident_id, s.actor_id)
                result = _json("error", code="internal_error", reason="Unexpected tool error; no change was confirmed.")
            parsed = {"status": "awaiting_confirmation"} if isinstance(result, Command) else json.loads(result)
            telemetry.record_tool_result(span, parsed.get("status"), parsed.get("code"))
        log.info(
            "tool=%s incident=%s actor=%s thread=%s status=%s code=%s%s",
            name, incident_id, s.actor_id, s.thread_id, parsed.get("status"), parsed.get("code"),
            f" outcome={parsed['outcome']}" if "outcome" in parsed else "",
        )
        return result

    def load(s: Session, incident_id: str, action: str) -> tuple[dict | None, str | None]:
        inc = repo.get_incident(incident_id)
        if inc is None:
            return None, _json("not_found", reason=f"Incident {incident_id} does not exist.")
        decision = policy.can_read(s.user, inc) if action == policy.READ else policy.can_modify(s.user, inc, action)
        if not decision.allowed:
            log.warning("authorization denied action=%s incident=%s actor=%s code=%s", action, incident_id, s.actor_id, decision.code)
            return None, _rejected(decision)
        return inc, None

    def escalation_reasons(inc: dict) -> list[str]:
        return policy.escalation_requirements(
            inc,
            repo.count_troubleshooting(inc["number"], "unsuccessful"),
            repo.list_diagnostic_runs(inc["number"]),
        )

    def blocked_by_escalation(inc: dict, action: str) -> str | None:
        decision = policy.check_not_pending_escalation(action, escalation_reasons(inc))
        return None if decision.allowed else _rejected(decision)

    # ------------------------------------------------------------------ read
    @tool(parse_docstring=True)
    def get_incident(incident_id: str, runtime: ToolRuntime) -> str:
        """Retrieve an incident record with its recent work notes, diagnostic runs and escalation status.

        Args:
            incident_id: Incident number, e.g. "INC-1001".
        """

        def run(s: Session) -> str:
            inc, err = load(s, incident_id, policy.READ)
            if err:
                return err
            caller = repo.get_user(inc["caller_id"])
            runs = repo.list_diagnostic_runs(incident_id)
            modify = policy.can_modify(s.user, inc, policy.UPDATE_STATUS)
            return _json(
                "ok",
                incident={
                    k: inc[k]
                    for k in (
                        "number", "short_description", "description", "category", "subcategory", "priority",
                        "state", "hold_reason", "assignment_group", "cmdb_ci", "escalated",
                        "requires_caller_confirmation", "resolution_code", "resolution_notes",
                        "resolution_evidence_run_id", "opened_at", "updated_at",
                    )
                },
                caller={"user_id": inc["caller_id"], "name": caller["name"] if caller else None},
                recent_work_notes=[
                    {k: n[k] for k in ("created_at", "author_id", "note_type", "outcome", "kb_article_id", "body")}
                    for n in repo.list_work_notes(incident_id, limit=10)
                ],
                diagnostic_runs=[
                    {k: r[k] for k in ("run_id", "created_at", "outcome", "error_code", "summary")} for r in runs[-5:]
                ],
                unsuccessful_troubleshooting_attempts=repo.count_troubleshooting(incident_id, "unsuccessful"),
                failed_attempt_threshold=policy.FAILED_ATTEMPT_THRESHOLD,
                mandatory_escalation_reasons=escalation_reasons(inc),
                session_user_may_change_state=modify.allowed,
                session_user_restriction=None if modify.allowed else modify.reason,
            )

        return guarded("get_incident", runtime, incident_id, run)

    @tool(parse_docstring=True)
    def search_knowledge_base(query: str, runtime: ToolRuntime, include_retired: bool = False) -> str:
        """Search troubleshooting knowledge articles. Results include status, last_reviewed, review_overdue and superseded_by; prefer current articles over outdated or superseded ones.

        Args:
            query: Symptoms, error codes or keywords, e.g. "VPN drops CLIENT_OUTDATED".
            include_retired: Also return retired articles (for reference only; never follow retired steps).
        """

        def run(s: Session) -> str:
            if s.user is None or not s.user["active"]:
                return _json("rejected", code="user_inactive", reason="User account is inactive.")
            results = kb.search(query, include_retired=include_retired)
            return _json("ok", query=query, result_count=len(results), articles=results)

        return guarded("search_knowledge_base", runtime, None, run)

    # ---------------------------------------------------------- diagnostics
    @tool(parse_docstring=True)
    def run_vpn_diagnostic(incident_id: str, runtime: ToolRuntime) -> str:
        """Run the VPN diagnostic against the device registered on the incident (cmdb_ci) and record the run. Only a run with outcome "pass" is resolution evidence; "timeout" and "error" mean the check could not be completed.

        Args:
            incident_id: Incident number whose registered device should be diagnosed.
        """

        def run(s: Session) -> str:
            inc, err = load(s, incident_id, policy.RUN_DIAGNOSTIC)
            if err:
                return err
            device = inc["cmdb_ci"]
            if not device:
                return _json("rejected", code="no_device", reason="Incident has no registered device (cmdb_ci).")
            result = ctx.diagnostics.diagnose(device, s.actor_id)
            with repo.connect(immediate=True) as conn:
                run_id = repo.add_diagnostic_run(
                    {
                        "incident_number": incident_id,
                        "device_id": device,
                        "requested_by": s.actor_id,
                        "outcome": result.outcome,
                        "error_code": result.error_code,
                        "summary": result.summary,
                        "checks": result.checks,
                        "raw_response": result.raw_response,
                        "service_run_id": result.service_run_id,
                        "attempts": result.attempts,
                        "duration_ms": result.duration_ms,
                        "thread_id": s.thread_id,
                    },
                    conn=conn,
                )
                repo.add_work_note(
                    incident_id,
                    author_id=s.actor_id,
                    note_type="system",
                    body=(
                        f"Automated VPN diagnostic {run_id} on {device}: {result.outcome.upper()}"
                        f"{f' ({result.error_code})' if result.error_code else ''}. {result.summary}"
                        f" Attempts: {result.attempts}."
                    ),
                    thread_id=s.thread_id,
                    conn=conn,
                )
            runs = repo.list_diagnostic_runs(incident_id)
            consecutive_unavailable = 0
            for r in reversed(runs):
                if r["outcome"] in ("timeout", "error"):
                    consecutive_unavailable += 1
                else:
                    break
            payload: dict[str, Any] = {
                "run_id": run_id,
                "device_id": device,
                "outcome": result.outcome,
                "error_code": result.error_code,
                "summary": result.summary,
                "non_passing_checks": [c for c in result.checks if c.get("status") != "pass"],
                "attempts": result.attempts,
                "is_resolution_evidence": result.outcome == "pass",
                "consecutive_unavailable_runs": consecutive_unavailable,
                "mandatory_escalation_reasons": escalation_reasons(repo.require_incident(incident_id)),
            }
            if result.outcome in ("timeout", "error"):
                payload["note"] = (
                    "The diagnostic could not be completed. This is NOT evidence that the issue is fixed or that "
                    "it is still broken."
                )
            return _json("ok", **payload)

        return guarded("run_vpn_diagnostic", runtime, incident_id, run)

    # ------------------------------------------------------------ work notes
    @tool(parse_docstring=True)
    def add_work_note(incident_id: str, note: str, runtime: ToolRuntime) -> str:
        """Add an internal work note to the incident (findings, information from the employee, next steps).

        Args:
            incident_id: Incident number.
            note: The note text. Be factual; do not include passwords or secrets.
        """

        def run(s: Session) -> str:
            if not note.strip() or len(note) > MAX_NOTE_LENGTH:
                return _json("rejected", code="invalid_note", reason=f"Note must be 1-{MAX_NOTE_LENGTH} characters.")
            inc, err = load(s, incident_id, policy.ADD_WORK_NOTE)
            if err:
                return err
            note_id = repo.add_work_note(incident_id, author_id=s.actor_id, note_type="agent", body=note.strip(), thread_id=s.thread_id)
            return _json("ok", work_note_id=note_id)

        return guarded("add_work_note", runtime, incident_id, run)

    @tool(parse_docstring=True)
    def record_troubleshooting_step(
        incident_id: str,
        step_description: str,
        outcome: str,
        runtime: ToolRuntime,
        kb_article_id: str | None = None,
    ) -> str:
        """Record a troubleshooting attempt and its outcome. Two "unsuccessful" attempts make escalation mandatory.

        Args:
            incident_id: Incident number.
            step_description: What was tried and what the employee or diagnostic reported.
            outcome: One of "successful", "unsuccessful", "inconclusive".
            kb_article_id: Knowledge article the step came from, if any.
        """

        def run(s: Session) -> str:
            if outcome not in TROUBLESHOOTING_OUTCOMES:
                return _json("rejected", code="invalid_outcome", reason=f"outcome must be one of {TROUBLESHOOTING_OUTCOMES}.")
            if not step_description.strip() or len(step_description) > MAX_NOTE_LENGTH:
                return _json("rejected", code="invalid_note", reason="step_description is required.")
            inc, err = load(s, incident_id, policy.RECORD_TROUBLESHOOTING)
            if err:
                return err
            if (blocked := blocked_by_escalation(inc, policy.RECORD_TROUBLESHOOTING)):
                return blocked
            if kb_article_id:
                article = kb.get(kb_article_id)
                if article is None:
                    return _json("rejected", code="unknown_article", reason=f"Knowledge article {kb_article_id} does not exist.")
                if article["status"] == "retired":
                    return _json(
                        "rejected", code="retired_article",
                        reason=f"{kb_article_id} is retired; use {article['superseded_by'] or 'a current article'}.",
                    )
            note_id = repo.add_work_note(
                incident_id,
                author_id=s.actor_id,
                note_type="troubleshooting",
                body=step_description.strip(),
                kb_article_id=kb_article_id,
                outcome=outcome,
                thread_id=s.thread_id,
            )
            failures = repo.count_troubleshooting(incident_id, "unsuccessful")
            return _json(
                "ok",
                work_note_id=note_id,
                unsuccessful_attempts=failures,
                failed_attempt_threshold=policy.FAILED_ATTEMPT_THRESHOLD,
                mandatory_escalation_reasons=escalation_reasons(inc),
            )

        return guarded("record_troubleshooting_step", runtime, incident_id, run)

    # ---------------------------------------------------------------- status
    @tool(parse_docstring=True)
    def update_incident_status(
        incident_id: str,
        new_state: str,
        runtime: ToolRuntime,
        hold_reason: str | None = None,
    ) -> str:
        """Change incident state to "in_progress" or "on_hold", or reopen a "resolved" incident to "in_progress". Use resolve_incident / close_incident for resolution and closure.

        Args:
            incident_id: Incident number.
            new_state: "in_progress" or "on_hold".
            hold_reason: Required for on_hold: "awaiting_caller", "awaiting_vendor" or "awaiting_change".
        """

        def run(s: Session) -> str:
            inc, err = load(s, incident_id, policy.UPDATE_STATUS)
            if err:
                return err
            decision = policy.check_status_transition(inc, new_state, hold_reason)
            if not decision.allowed:
                return _rejected(decision)
            changes: dict[str, Any] = {"state": new_state, "hold_reason": hold_reason if new_state == "on_hold" else None}
            if inc["state"] == "resolved":  # reopen
                changes.update(resolved_at=None, resolved_by=None, resolution_code=None, resolution_evidence_run_id=None)
            updated = repo.update_incident_fields(incident_id, changes, actor_id=s.actor_id, thread_id=s.thread_id)
            return _json("ok", incident_id=incident_id, previous_state=inc["state"], state=updated["state"], hold_reason=updated["hold_reason"])

        return guarded("update_incident_status", runtime, incident_id, run)

    # ------------------------------------------------------------ escalation
    @tool(parse_docstring=True)
    def escalate_incident(incident_id: str, target_group: str, reason: str, runtime: ToolRuntime) -> str:
        """Escalate the incident to a human resolver group and notify its on-call alias.

        Args:
            incident_id: Incident number.
            target_group: One of "Network Operations L2", "Identity & Access L2", "Endpoint Support L2", "Major Incident Management".
            reason: Summary for the receiving team: symptoms, what was tried, diagnostic run IDs, why escalation is needed.
        """

        def run(s: Session) -> str:
            if not reason.strip():
                return _json("rejected", code="reason_required", reason="An escalation reason is required.")
            inc, err = load(s, incident_id, policy.ESCALATE)
            if err:
                return err
            decision = policy.check_escalation_target(inc, target_group)
            if not decision.allowed:
                return _rejected(decision)
            reason_codes = escalation_reasons(inc) or ["agent_judgement"]
            with repo.connect(immediate=True) as conn:
                changes: dict[str, Any] = {"assignment_group": target_group, "escalated": 1, "assigned_to": None}
                if inc["state"] in ("new", "on_hold", "resolved"):
                    changes.update(state="in_progress", hold_reason=None)
                repo.update_incident_fields(incident_id, changes, actor_id=s.actor_id, thread_id=s.thread_id, conn=conn)
                esc_id = repo.add_escalation(
                    incident_id,
                    from_group=inc["assignment_group"],
                    to_group=target_group,
                    reason=reason.strip(),
                    reason_codes=reason_codes,
                    escalated_by=s.actor_id,
                    thread_id=s.thread_id,
                    conn=conn,
                )
                repo.add_work_note(
                    incident_id,
                    author_id=s.actor_id,
                    note_type="agent",
                    body=f"Escalated from {inc['assignment_group']} to {target_group} ({', '.join(reason_codes)}): {reason.strip()}",
                    thread_id=s.thread_id,
                    conn=conn,
                )
            notified = True
            try:
                ctx.outbox_path.parent.mkdir(parents=True, exist_ok=True)
                with ctx.outbox_path.open("a") as fh:
                    fh.write(
                        json.dumps(
                            {
                                "type": "incident_escalation",
                                "to": GROUP_ALIASES[target_group],
                                "incident": incident_id,
                                "priority": inc["priority"],
                                "short_description": inc["short_description"],
                                "reason": reason.strip(),
                                "reason_codes": reason_codes,
                                "escalation_id": esc_id,
                                "sent_at": utcnow(),
                            }
                        )
                        + "\n"
                    )
            except OSError:
                notified = False
                log.exception("failed to write escalation notification incident=%s", incident_id)
            telemetry.business_event("incident_escalated", {"itsm.incident.id": incident_id, "itsm.escalation.id": esc_id,
                                                            "itsm.assignment_group": target_group,
                                                            "itsm.escalation.reason_codes": reason_codes})
            return _json(
                "ok",
                incident_id=incident_id,
                escalation_id=esc_id,
                assignment_group=target_group,
                reason_codes=reason_codes,
                notification_sent=notified,
            )

        return guarded("escalate_incident", runtime, incident_id, run)

    # ------------------------------------------------------- confirmation
    @tool(parse_docstring=True)
    def request_caller_confirmation(incident_id: str, question: str, runtime: ToolRuntime) -> Command | str:
        """Ask the caller to confirm that the issue is fixed. Requires a passing latest diagnostic. The caller answers yes/no through a structured prompt; the conversation pauses until they answer. Call this tool on its own, not together with other tools.

        Args:
            incident_id: Incident number.
            question: Short yes/no question for the caller, e.g. "Is your VPN now staying connected?".
        """

        def run(s: Session) -> Command | str:
            # Parallel calls in one model turn would each set pending_confirmation; only the first may.
            same_turn = [tc["id"] for tc in getattr(runtime.state["messages"][-1], "tool_calls", [])
                         if tc["name"] == "request_caller_confirmation"]
            if same_turn and same_turn[0] != runtime.tool_call_id:
                return _json("rejected", code="duplicate_confirmation_request",
                             reason="Only one caller confirmation can be requested at a time; this duplicate call was ignored.")
            inc, err = load(s, incident_id, policy.REQUEST_CONFIRMATION)
            if err:
                return err
            if (blocked := blocked_by_escalation(inc, policy.REQUEST_CONFIRMATION)):
                return blocked
            if not inc["requires_caller_confirmation"]:
                return _json("ok", confirmation_required=False, reason="This incident does not require caller confirmation.")
            runs = repo.list_diagnostic_runs(incident_id)
            decision = policy.check_confirmation_request(inc, runs)
            if not decision.allowed:
                return _rejected(decision)
            evidence_run_id = runs[-1]["run_id"]
            cid = repo.create_confirmation(
                incident_id, requested_by=s.actor_id, question=question.strip(), evidence_run_id=evidence_run_id, thread_id=s.thread_id
            )
            if s.actor_id != inc["caller_id"]:
                # Caller is not in this conversation: record a pending request and hold the incident.
                if inc["state"] != "resolved":
                    repo.update_incident_fields(
                        incident_id, {"state": "on_hold", "hold_reason": "awaiting_caller"}, actor_id=s.actor_id, thread_id=s.thread_id
                    )
                return _json(
                    "ok",
                    confirmation_id=cid,
                    confirmation_status="pending_caller_not_in_conversation",
                    detail="The caller must confirm in their own session. The incident cannot be closed until then.",
                )
            content = _json(
                "ok",
                confirmation_id=cid,
                confirmation_status="awaiting_caller_response",
                evidence_run_id=evidence_run_id,
            )
            return Command(
                update={
                    "pending_confirmation": {
                        "confirmation_id": cid,
                        "incident_id": incident_id,
                        "question": question.strip(),
                        "evidence_run_id": evidence_run_id,
                    },
                    "messages": [ToolMessage(content=content, tool_call_id=runtime.tool_call_id, name="request_caller_confirmation")],
                }
            )

        return guarded("request_caller_confirmation", runtime, incident_id, run)

    # ----------------------------------------------------- resolve / close
    @tool(parse_docstring=True)
    def resolve_incident(
        incident_id: str, resolution_code: str, resolution_notes: str, evidence_run_id: str, runtime: ToolRuntime
    ) -> str:
        """Mark the incident resolved. Requires that evidence_run_id is the most recent diagnostic run and that it passed.

        Args:
            incident_id: Incident number.
            resolution_code: One of client_updated, certificate_reenrolled, mfa_reregistered, dns_cache_cleared, profile_reset, network_environment, password_reset, outlook_profile_repaired, transient_recovered, other.
            resolution_notes: What fixed the issue and how it was verified.
            evidence_run_id: run_id of the passing diagnostic run that proves the fix.
        """

        def run(s: Session) -> str:
            if not resolution_notes.strip():
                return _json("rejected", code="notes_required", reason="resolution_notes are required.")
            inc, err = load(s, incident_id, policy.RESOLVE)
            if err:
                return err
            if (blocked := blocked_by_escalation(inc, policy.RESOLVE)):
                return blocked
            decision = policy.check_resolve(
                inc, repo.list_diagnostic_runs(incident_id), evidence_run_id, resolution_code, repo.list_confirmations(incident_id)
            )
            if not decision.allowed:
                return _rejected(decision)
            with repo.connect(immediate=True) as conn:
                repo.update_incident_fields(
                    incident_id,
                    {
                        "state": "resolved",
                        "hold_reason": None,
                        "resolution_code": resolution_code,
                        "resolution_notes": resolution_notes.strip(),
                        "resolution_evidence_run_id": evidence_run_id,
                        "resolved_by": s.actor_id,
                        "resolved_at": utcnow(),
                    },
                    actor_id=s.actor_id,
                    thread_id=s.thread_id,
                    conn=conn,
                )
                repo.add_work_note(
                    incident_id, author_id=s.actor_id, note_type="agent",
                    body=f"Resolved ({resolution_code}) with evidence {evidence_run_id}: {resolution_notes.strip()}",
                    thread_id=s.thread_id, conn=conn,
                )
            confirmed = any(
                c["status"] == "confirmed" and c["evidence_run_id"] == evidence_run_id
                for c in repo.list_confirmations(incident_id)
            )
            telemetry.business_event("incident_resolved", {"itsm.incident.id": incident_id,
                                                           "itsm.resolution_code": resolution_code,
                                                           "itsm.evidence_run.id": evidence_run_id})
            return _json(
                "ok",
                incident_id=incident_id,
                state="resolved",
                caller_confirmation_required=bool(inc["requires_caller_confirmation"]),
                caller_confirmation_on_file=confirmed,
            )

        return guarded("resolve_incident", runtime, incident_id, run)

    @tool(parse_docstring=True)
    def close_incident(incident_id: str, close_notes: str, runtime: ToolRuntime) -> str:
        """Close a resolved incident. Requires resolution evidence that is still the latest passing diagnostic and, where required, the caller's confirmation of that fix.

        Args:
            incident_id: Incident number.
            close_notes: Short closing summary.
        """

        def run(s: Session) -> str:
            inc, err = load(s, incident_id, policy.CLOSE)
            if err:
                return err
            if (blocked := blocked_by_escalation(inc, policy.CLOSE)):
                return blocked
            decision = policy.check_close(inc, repo.list_diagnostic_runs(incident_id), repo.list_confirmations(incident_id))
            if not decision.allowed:
                return _rejected(decision)
            with repo.connect(immediate=True) as conn:
                repo.update_incident_fields(
                    incident_id,
                    {"state": "closed", "closed_by": s.actor_id, "closed_at": utcnow()},
                    actor_id=s.actor_id, thread_id=s.thread_id, conn=conn,
                )
                repo.add_work_note(
                    incident_id, author_id=s.actor_id, note_type="agent",
                    body=f"Closed: {close_notes.strip() or 'no notes'}", thread_id=s.thread_id, conn=conn,
                )
            telemetry.business_event("incident_closed", {"itsm.incident.id": incident_id})
            return _json("ok", incident_id=incident_id, state="closed")

        return guarded("close_incident", runtime, incident_id, run)

    return [
        get_incident,
        search_knowledge_base,
        run_vpn_diagnostic,
        add_work_note,
        record_troubleshooting_step,
        update_incident_status,
        escalate_incident,
        request_caller_confirmation,
        resolve_incident,
        close_incident,
    ]
