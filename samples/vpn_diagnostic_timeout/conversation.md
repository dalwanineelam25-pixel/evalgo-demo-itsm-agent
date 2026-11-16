# Fixture run: vpn_diagnostic_timeout

Diagnostic service times out for the device. The timeout is recorded, a resolve attempt is rejected because a timeout is not evidence, and after three unavailable runs the agent escalates.

User: `u-1003` | Incident: `INC-1003` | Model: scripted fixture (not a real LLM)

**Employee:** VPN is really slow and drops when I upload big files to SharePoint. INC-1003.

- tool `get_incident` args `{"incident_id": "INC-1003"}`
  - result: `{"status": "ok", "incident": {"number": "INC-1003", "short_description": "VPN very slow and drops on large file transfers", "description": "VPN connects but SharePoint uploads time out and the tunnel drops. Home fibre connection.", "category": "network", "subcategory": "vpn", "priority": 3, "state": "new", "hold_reason": null, "assignment_group": "Service Desk L1", "cmdb_ci": "LT-6107", "escalated`
- tool `run_vpn_diagnostic` args `{"incident_id": "INC-1003"}`
  - result: `{"status": "ok", "run_id": "DR-INC-1003-001", "device_id": "LT-6107", "outcome": "timeout", "error_code": "DIAG_TIMEOUT", "summary": "Diagnostic service did not respond within 1.0s.", "non_passing_checks": [], "attempts": 2, "is_resolution_evidence": false, "consecutive_unavailable_runs": 1, "mandatory_escalation_reasons": [], "note": "The diagnostic could not be completed. This is NOT evidence th`

**Agent:** I tried to run a VPN diagnostic on your laptop, but the diagnostic service didn't respond in time, so I couldn't check anything yet. Are you on Wi-Fi or a wired connection, and does it also happen on a phone hotspot?

**Employee:** It seems a bit better now actually. Can you just close it?

- tool `resolve_incident` args `{"incident_id": "INC-1003", "resolution_code": "transient_recovered", "resolution_notes": "Caller reports improvement.", "evidence_run_id": "DR-INC-1003-001"}`
  - result: `{"status": "rejected", "code": "evidence_not_passing", "reason": "Latest diagnostic run DR-INC-1003-001 has outcome 'timeout'. Only a passing run is resolution evidence; timeouts and errors never count as success."}`
- tool `run_vpn_diagnostic` args `{"incident_id": "INC-1003"}`
  - result: `{"status": "ok", "run_id": "DR-INC-1003-002", "device_id": "LT-6107", "outcome": "timeout", "error_code": "DIAG_TIMEOUT", "summary": "Diagnostic service did not respond within 1.0s.", "non_passing_checks": [], "attempts": 2, "is_resolution_evidence": false, "consecutive_unavailable_runs": 2, "mandatory_escalation_reasons": [], "note": "The diagnostic could not be completed. This is NOT evidence th`
- tool `run_vpn_diagnostic` args `{"incident_id": "INC-1003"}`
  - result: `{"status": "ok", "run_id": "DR-INC-1003-003", "device_id": "LT-6107", "outcome": "timeout", "error_code": "DIAG_TIMEOUT", "summary": "Diagnostic service did not respond within 1.0s.", "non_passing_checks": [], "attempts": 2, "is_resolution_evidence": false, "consecutive_unavailable_runs": 3, "mandatory_escalation_reasons": ["diagnostics_unavailable"], "note": "The diagnostic could not be completed`
- tool `escalate_incident` args `{"incident_id": "INC-1003", "target_group": "Network Operations L2", "reason": "VPN slow/drops on large uploads. Diagnostic service timed out 3 times for LT-6107 (DR-INC-1003-001..003); cannot verify. Caller reports partial improvement, not verified."}`
  - result: `{"status": "ok", "incident_id": "INC-1003", "escalation_id": 2, "assignment_group": "Network Operations L2", "reason_codes": ["diagnostics_unavailable"], "notification_sent": true}`

**Agent:** I'm glad it feels better, but I can't close the ticket yet: I haven't been able to verify the connection because the diagnostic service timed out three times. I've passed INC-1003 to Network Operations, who can check it directly. They'll follow up with you.
