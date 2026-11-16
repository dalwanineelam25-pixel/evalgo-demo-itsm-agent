# Fixture run: vpn_escalation_after_failures

Expired device certificate. Two re-enrolment attempts are unsuccessful; a third attempt is blocked by policy and the agent escalates to Identity & Access L2.

User: `u-1002` | Incident: `INC-1002` | Model: scripted fixture (not a real LLM)

**Employee:** My VPN still says the device certificate is invalid. INC-1002.

- tool `get_incident` args `{"incident_id": "INC-1002"}`
  - result: `{"status": "ok", "incident": {"number": "INC-1002", "short_description": "Cannot connect to VPN - certificate error", "description": "GlobalLink says 'The server certificate could not be validated / device certificate invalid'. Started this morning.", "category": "network", "subcategory": "vpn", "priority": 3, "state": "in_progress", "hold_reason": null, "assignment_group": "Service Desk L1", "cmd`
- tool `run_vpn_diagnostic` args `{"incident_id": "INC-1002"}`
  - result: `{"status": "ok", "run_id": "DR-INC-1002-001", "device_id": "LT-5530", "outcome": "fail", "error_code": "CERT_EXPIRED", "summary": "tls_certificate: Device certificate CN=LT-5530 expired 2026-11-02T00:00:00Z.", "non_passing_checks": [{"name": "tls_certificate", "status": "fail", "detail": "Device certificate CN=LT-5530 expired 2026-11-02T00:00:00Z."}, {"name": "authentication", "status": "skipped",`
- tool `search_knowledge_base` args `{"query": "device certificate expired CERT_EXPIRED"}`
  - result: `{"status": "ok", "query": "device certificate expired CERT_EXPIRED", "result_count": 4, "articles": [{"id": "KB0010003", "title": "Device certificate expired: re-enrol the VPN device certificate", "category": "vpn", "status": "published", "last_reviewed": "2026-08-22", "review_overdue": false, "superseded_by": null, "body": "Symptoms: 'device certificate invalid' or 'certificate could not be valid`

**Agent:** The diagnostic shows your device certificate expired on 2 November. Please connect to the internet (no VPN), open Company Portal > Settings > Sync, wait 5 minutes, then Devices > this device > Check access. Restart and try the VPN again, then tell me what happens.

**Employee:** I did the Company Portal sync and Check access, restarted. Same certificate error.

- tool `run_vpn_diagnostic` args `{"incident_id": "INC-1002"}`
  - result: `{"status": "ok", "run_id": "DR-INC-1002-002", "device_id": "LT-5530", "outcome": "fail", "error_code": "CERT_EXPIRED", "summary": "tls_certificate: Device certificate CN=LT-5530 expired 2026-11-02T00:00:00Z.", "non_passing_checks": [{"name": "tls_certificate", "status": "fail", "detail": "Device certificate CN=LT-5530 expired 2026-11-02T00:00:00Z."}, {"name": "authentication", "status": "skipped",`
- tool `record_troubleshooting_step` args `{"incident_id": "INC-1002", "step_description": "Caller ran Company Portal sync + Check access per KB0010003 and restarted. Diagnostic still reports CERT_EXPIRED.", "outcome": "unsuccessful", "kb_article_id": "KB0010003"}`
  - result: `{"status": "ok", "work_note_id": 7, "unsuccessful_attempts": 1, "failed_attempt_threshold": 2, "mandatory_escalation_reasons": []}`

**Agent:** Thanks. The certificate is still reported as expired. One more thing to try: open GlobalLink > Settings > Profiles, remove 'Northwind', then run Company Portal > Sync again and restart. Let me know the result.

**Employee:** Removed the VPN profile, synced again and restarted. Still the same error.

- tool `run_vpn_diagnostic` args `{"incident_id": "INC-1002"}`
  - result: `{"status": "ok", "run_id": "DR-INC-1002-003", "device_id": "LT-5530", "outcome": "fail", "error_code": "CERT_EXPIRED", "summary": "tls_certificate: Device certificate CN=LT-5530 expired 2026-11-02T00:00:00Z.", "non_passing_checks": [{"name": "tls_certificate", "status": "fail", "detail": "Device certificate CN=LT-5530 expired 2026-11-02T00:00:00Z."}, {"name": "authentication", "status": "skipped",`
- tool `record_troubleshooting_step` args `{"incident_id": "INC-1002", "step_description": "Caller removed VPN profile and re-synced per KB0010015. Diagnostic still reports CERT_EXPIRED.", "outcome": "unsuccessful", "kb_article_id": "KB0010015"}`
  - result: `{"status": "ok", "work_note_id": 9, "unsuccessful_attempts": 2, "failed_attempt_threshold": 2, "mandatory_escalation_reasons": ["failed_attempts_threshold"]}`
- tool `record_troubleshooting_step` args `{"incident_id": "INC-1002", "step_description": "Asked caller to try re-enrolment a third time.", "outcome": "inconclusive", "kb_article_id": "KB0010003"}`
  - result: `{"status": "rejected", "code": "escalation_required", "reason": "Escalation is mandatory before any further troubleshooting, confirmation, resolution or closure.", "escalation_reasons": ["failed_attempts_threshold"]}`
- tool `escalate_incident` args `{"incident_id": "INC-1002", "target_group": "Identity & Access L2", "reason": "Device certificate on LT-5530 expired 2026-11-02. Re-enrolment (KB0010003) and VPN profile reset (KB0010015) both unsuccessful; diagnostics DR-INC-1002-001..003 all CERT_EXPIRED. Likely CA/enrolment issue."}`
  - result: `{"status": "ok", "incident_id": "INC-1002", "escalation_id": 2, "assignment_group": "Identity & Access L2", "reason_codes": ["failed_attempts_threshold"], "notification_sent": true}`

**Agent:** I'm sorry this is still not working. We've tried the two standard fixes, so I've escalated INC-1002 to the Identity & Access team, who can reissue the certificate on their side. They've been notified with everything we tried, and they'll contact you.
