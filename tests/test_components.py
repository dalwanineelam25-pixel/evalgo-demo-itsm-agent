"""Unit tests for the knowledge base, diagnostic client/mock service, persistence and seed data."""

from __future__ import annotations

import json
from datetime import date

import pytest

from itsm_agent.config import DATA_DIR, ConfigError, Settings
from itsm_agent.db import Repository
from itsm_agent.diagnostics import VPNDiagnosticClient
from itsm_agent.diagnostics.service import DiagnosticBackend, DiagnosticServer
from itsm_agent.kb import KnowledgeBase


# ----------------------------------------------------------------------------- knowledge base
@pytest.fixture
def kb():
    return KnowledgeBase(json.loads((DATA_DIR / "kb_articles.json").read_text()), today=date(2026, 11, 14))


def test_kb_has_expected_size_and_variety(kb):
    statuses = [a["status"] for a in kb.articles.values()]
    assert 15 <= len(kb.articles) <= 30
    assert "retired" in statuses
    assert any(a.get("superseded_by") and a["status"] == "published" for a in kb.articles.values())


def test_kb_ranks_current_article_first_and_flags_outdated(kb):
    results = kb.search("VPN disconnects drops CLIENT_OUTDATED client update")
    assert results[0]["id"] == "KB0010001"
    legacy = next(r for r in results if r["id"] == "KB0010002")
    assert legacy["review_overdue"] is True and legacy["superseded_by"] == "KB0010001"


def test_kb_excludes_retired_by_default(kb):
    assert "KB0010004" not in [r["id"] for r in kb.search("certmgr certificate manual")]
    assert "KB0010004" in [r["id"] for r in kb.search("certmgr certificate manual", include_retired=True)]


def test_kb_empty_query(kb):
    assert kb.search("the and of") == []


# ----------------------------------------------------------------------------- diagnostics
def _server(devices, delay=1.0):
    backend = DiagnosticBackend({"gateway": "gw.example", "devices": devices}, timeout_delay_s=delay)
    return DiagnosticServer(backend, port=0).start_background()


@pytest.mark.parametrize(
    "script,outcome,code,attempts",
    [
        ([{"behavior": "pass"}], "pass", None, 1),
        ([{"behavior": "fail", "error_code": "CERT_EXPIRED", "check": "tls_certificate", "detail": "expired"}], "fail", "CERT_EXPIRED", 1),
        ([{"behavior": "timeout"}], "timeout", "DIAG_TIMEOUT", 2),
        ([{"behavior": "http_error", "status": 503}], "error", "DIAG_HTTP_503", 2),
        ([{"behavior": "http_error", "status": 503}, {"behavior": "pass"}], "pass", None, 2),
        ([{"behavior": "malformed"}], "error", "DIAG_MALFORMED_RESPONSE", 2),
        ([{"behavior": "timeout"}, {"behavior": "pass"}], "pass", None, 2),
    ],
)
def test_diagnostic_client_outcomes(script, outcome, code, attempts):
    server = _server({"D1": script})
    try:
        result = VPNDiagnosticClient(server.url, timeout_s=0.25, retries=1).diagnose("D1", "u-1")
    finally:
        server.stop()
    assert (result.outcome, result.error_code, result.attempts) == (outcome, code, attempts)
    if outcome == "fail":
        assert "tls_certificate" in result.summary and result.checks


def test_diagnostic_unknown_device_is_not_retried():
    server = _server({})
    try:
        result = VPNDiagnosticClient(server.url, timeout_s=0.25, retries=3).diagnose("NOPE", "u-1")
    finally:
        server.stop()
    assert (result.outcome, result.error_code, result.attempts) == ("error", "DEVICE_NOT_FOUND", 1)


def test_diagnostic_service_unreachable():
    result = VPNDiagnosticClient("http://127.0.0.1:9", timeout_s=0.25, retries=1).diagnose("D1", "u-1")
    assert result.outcome == "error" and result.error_code == "DIAG_UNREACHABLE" and result.attempts == 2


# ----------------------------------------------------------------------------- persistence
def test_seed_counts_and_edge_cases(repo):
    assert repo.get_user("u-1004")["active"] is False
    assert repo.get_user("u-2002")["groups"] == ["Network Operations L2", "Service Desk L1"]
    assert repo.get_incident("INC-1006")["state"] == "closed"
    assert repo.get_incident("INC-1007")["escalated"] == 1
    assert repo.get_incident("INC-1012")["requires_caller_confirmation"] == 0
    assert repo.count_troubleshooting("INC-1007", "unsuccessful") == 2


def test_update_writes_one_audit_row_per_changed_field(repo):
    repo.update_incident_fields("INC-1001", {"state": "in_progress", "priority": 3}, actor_id="u-2001", thread_id="t1")
    audit = repo.list_audit("INC-1001")
    assert [(a["field"], a["old_value"], a["new_value"]) for a in audit] == [("state", "new", "in_progress")]
    assert repo.get_incident("INC-1001")["state"] == "in_progress"


def test_diagnostic_run_ids_are_sequential_per_incident(repo):
    base = {"device_id": "D", "requested_by": "u", "outcome": "pass", "summary": "s", "attempts": 1, "duration_ms": 1}
    with repo.connect(immediate=True) as conn:
        a = repo.add_diagnostic_run({**base, "incident_number": "INC-1001"}, conn=conn)
        b = repo.add_diagnostic_run({**base, "incident_number": "INC-1001"}, conn=conn)
        c = repo.add_diagnostic_run({**base, "incident_number": "INC-1002"}, conn=conn)
    assert (a, b, c) == ("DR-INC-1001-001", "DR-INC-1001-002", "DR-INC-1002-001")


def test_confirmation_response_is_idempotent(repo):
    cid = repo.create_confirmation("INC-1001", requested_by="u-1001", question="ok?", evidence_run_id="r", thread_id=None)
    first = repo.record_confirmation_response(cid, confirmed=True, responded_by="u-1001", comment=None)
    second = repo.record_confirmation_response(cid, confirmed=False, responded_by="u-1001", comment="changed my mind")
    assert first["status"] == second["status"] == "confirmed"


def test_reseed_is_deterministic(settings):
    from itsm_agent.seed import seed_database

    r = Repository(settings.db_path)
    seed_database(r, settings.seed_path)
    r.update_incident_fields("INC-1001", {"state": "closed"}, actor_id="x", thread_id=None)
    seed_database(r, settings.seed_path)
    assert r.get_incident("INC-1001")["state"] == "new"
    assert r.list_audit("INC-1001") == []


# ----------------------------------------------------------------------------- config
def test_settings_validation(monkeypatch):
    monkeypatch.setenv("ITSM_MODEL_MODE", "bogus")
    with pytest.raises(ConfigError):
        Settings.from_env(load_dotenv_file=False)
    monkeypatch.setenv("ITSM_MODEL_MODE", "real")
    monkeypatch.setenv("MODEL_EFFORT", "medium")
    s = Settings.from_env(load_dotenv_file=False)
    assert s.model == "claude-opus-5-5" and s.max_tokens == 16000


def test_real_mode_requires_api_key(monkeypatch):
    from itsm_agent.graph import build_chat_model

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    with pytest.raises(ConfigError):
        build_chat_model(Settings(model_mode="real"))


def test_real_mode_model_configuration(monkeypatch):
    """Builds (does not call) the real ChatAnthropic client and checks the request parameters."""
    from itsm_agent.graph import build_chat_model

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    model = build_chat_model(Settings(model_mode="real", effort="medium"))
    params = model._get_request_payload([("user", "hi")])
    assert params["model"] == "claude-opus-5-5"
    assert params["max_tokens"] == 16000
    assert params["output_config"] == {"effort": "medium"}
    for forbidden in ("temperature", "top_p", "top_k", "tool_choice"):
        assert params.get(forbidden) is None
