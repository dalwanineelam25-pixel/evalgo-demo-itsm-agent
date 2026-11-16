# Fixture run: vpn_happy_path

Employee reports VPN drops. Diagnostic finds an outdated client, employee updates it, diagnostic passes, employee confirms, incident resolved and closed.

User: `u-1001` | Incident: `INC-1001` | Model: scripted fixture (not a real LLM)

**Employee:** Hi, my VPN keeps dropping every few minutes when I work from home. Ticket is INC-1001.

- tool `get_incident` args `{"incident_id": "INC-1001"}`
  - result: `{"status": "ok", "incident": {"number": "INC-1001", "short_description": "VPN disconnects every few minutes when working from home", "description": "Since Monday my VPN drops every 5-10 minutes and I have to reconnect. Teams calls freeze when it happens.", "category": "network", "subcategory": "vpn", "priority": 3, "state": "new", "hold_reason": null, "assignment_group": "Service Desk L1", "cmdb_c`
- tool `update_incident_status` args `{"incident_id": "INC-1001", "new_state": "in_progress"}`
  - result: `{"status": "ok", "incident_id": "INC-1001", "previous_state": "new", "state": "in_progress", "hold_reason": null}`
- tool `run_vpn_diagnostic` args `{"incident_id": "INC-1001"}`
  - result: `{"status": "ok", "run_id": "DR-INC-1001-001", "device_id": "LT-4821", "outcome": "fail", "error_code": "CLIENT_OUTDATED", "summary": "client_version: GlobalLink 5.1.2 installed; minimum supported version is 6.0.4. Known keep-alive bug causes drops every 5-10 min.", "non_passing_checks": [{"name": "client_version", "status": "fail", "detail": "GlobalLink 5.1.2 installed; minimum supported version i`
- tool `search_knowledge_base` args `{"query": "VPN disconnects drops CLIENT_OUTDATED client update"}`
  - result: `{"status": "ok", "query": "VPN disconnects drops CLIENT_OUTDATED client update", "result_count": 5, "articles": [{"id": "KB0010001", "title": "VPN keeps disconnecting: update GlobalLink VPN client to a supported version", "category": "vpn", "status": "published", "last_reviewed": "2026-09-18", "review_overdue": false, "superseded_by": null, "body": "Symptoms: VPN drops every few minutes, Teams cal`

**Agent:** Thanks Priya. The diagnostic on your laptop (LT-4821) shows GlobalLink 5.1.2, which has a known bug that drops the tunnel every 5-10 minutes. Please:
1. Open Company Portal > Apps > GlobalLink VPN and click Install/Update.
2. Restart your laptop.
3. Reconnect and stay connected for about 10 minutes.
Let me know when that's done and I'll re-check.

**Employee:** OK, I installed the update from Company Portal (it says 6.0.5 now), restarted and I've been connected for 15 minutes without a drop.

- tool `record_troubleshooting_step` args `{"incident_id": "INC-1001", "step_description": "Employee updated GlobalLink via Company Portal to 6.0.5 per KB0010001, restarted, reports 15 min stable connection.", "outcome": "successful", "kb_article_id": "KB0010001"}`
  - result: `{"status": "ok", "work_note_id": 7, "unsuccessful_attempts": 0, "failed_attempt_threshold": 2, "mandatory_escalation_reasons": []}`
- tool `run_vpn_diagnostic` args `{"incident_id": "INC-1001"}`
  - result: `{"status": "ok", "run_id": "DR-INC-1001-002", "device_id": "LT-4821", "outcome": "pass", "error_code": null, "summary": "All 7 VPN checks passed on gateway vpn-eu1.northwind.example.", "non_passing_checks": [], "attempts": 1, "is_resolution_evidence": true, "consecutive_unavailable_runs": 0, "mandatory_escalation_reasons": []}`
- tool `request_caller_confirmation` args `{"incident_id": "INC-1001", "question": "The VPN diagnostic now passes. Is your VPN staying connected?"}`
  - result: `{"status": "ok", "confirmation_id": "CONF-B1D6B10445", "confirmation_status": "awaiting_caller_response", "evidence_run_id": "DR-INC-1001-002"}`

_Conversation paused for caller confirmation: The VPN diagnostic now passes. Is your VPN staying connected?_

**Employee (confirmation prompt):** YES - Yes, it's been stable through a Teams call.

- tool `resolve_incident` args `{"incident_id": "INC-1001", "resolution_code": "client_updated", "resolution_notes": "GlobalLink updated from 5.1.2 to 6.0.5 (KB0010001). Diagnostic DR-INC-1001-002 passed; caller confirmed stable connection.", "evidence_run_id": "DR-INC-1001-002"}`
  - result: `{"status": "ok", "incident_id": "INC-1001", "state": "resolved", "caller_confirmation_required": true, "caller_confirmation_on_file": true}`
- tool `close_incident` args `{"incident_id": "INC-1001", "close_notes": "Client update fixed VPN drops; caller confirmed."}`
  - result: `{"status": "ok", "incident_id": "INC-1001", "state": "closed"}`

**Agent:** Great news. The diagnostic passed and you confirmed it's stable, so I've resolved and closed INC-1001. If the drops come back, just open a new ticket.
