"""HTTP API and CLI end-to-end tests in fixture mode."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from conftest import SCENARIO_DIR, load_scenario
from fastapi.testclient import TestClient

from itsm_agent.api import create_app
from itsm_agent.config import PROJECT_ROOT
from itsm_agent.db import Repository
from itsm_agent.fixture_model import ScriptedChatModel
from itsm_agent.service import ITSMAgentService

pytestmark = pytest.mark.integration


@pytest.fixture
def api(settings, repo):
    sc = load_scenario("vpn_happy_path")
    svc = ITSMAgentService(settings, model=ScriptedChatModel(script=sc["model_script"]))
    with TestClient(create_app(service=svc)) as client:
        yield client, sc
    svc.close()


def test_api_full_conversation(api, repo):
    client, sc = api
    h = {"X-User-Id": "u-1001"}
    assert client.get("/health").json()["status"] == "ok"
    r = client.post("/conversations", json={"incident_id": "INC-1001"}, headers=h)
    assert r.status_code == 201
    tid = r.json()["thread_id"]

    r1 = client.post(f"/conversations/{tid}/messages", json={"message": sc["turns"][0]["user"]}, headers=h).json()
    assert [c["name"] for c in r1["tool_calls"]] == [
        "get_incident", "update_incident_status", "run_vpn_diagnostic", "search_knowledge_base"
    ]
    r2 = client.post(f"/conversations/{tid}/messages", json={"message": sc["turns"][1]["user"]}, headers=h).json()
    assert r2["pending_confirmation"]["incident_id"] == "INC-1001"

    # A new chat message is refused while the confirmation is pending.
    r = client.post(f"/conversations/{tid}/messages", json={"message": "hello?"}, headers=h)
    assert r.status_code == 409

    # Another user cannot answer the caller's confirmation.
    r = client.post(f"/conversations/{tid}/confirmation", json={"confirmed": True}, headers={"X-User-Id": "u-2001"})
    assert r.status_code == 403

    r3 = client.post(f"/conversations/{tid}/confirmation", json={"confirmed": True, "comment": "stable"}, headers=h).json()
    assert r3["pending_confirmation"] is None

    snap = client.get("/incidents/INC-1001", headers=h).json()
    assert snap["incident"]["state"] == "closed"
    assert client.get("/incidents/INC-1001", headers={"X-User-Id": "u-1002"}).status_code == 403
    assert client.get("/incidents/INC-1001", headers={"X-User-Id": "u-3001"}).status_code == 200
    transcript = client.get(f"/conversations/{tid}", headers=h).json()["messages"]
    assert transcript[0]["role"] == "user" and any(m["role"] == "tool" for m in transcript)


def test_api_rejects_missing_identity_and_inactive_user(api):
    client, _ = api
    assert client.post("/conversations", json={}).status_code == 422
    assert client.post("/conversations", json={}, headers={"X-User-Id": "u-1004"}).status_code == 403
    assert client.post("/conversations", json={}, headers={"X-User-Id": "u-0000"}).status_code == 401


def test_cli_chat_end_to_end_fixture_mode(tmp_path, diag_server):
    env = {
        **os.environ,
        "ITSM_MODEL_MODE": "fixture",
        "ITSM_FIXTURE_SCRIPT": str(SCENARIO_DIR / "vpn_happy_path.json"),
        "ITSM_DB_PATH": str(tmp_path / "itsm.db"),
        "ITSM_CHECKPOINT_DB_PATH": str(tmp_path / "cp.db"),
        "ITSM_OUTBOX_PATH": str(tmp_path / "outbox.jsonl"),
        "ITSM_LOG_PATH": str(tmp_path / "agent.log"),
        "DIAG_SERVICE_URL": diag_server.url,
        "DIAG_TIMEOUT_S": "0.5",
    }
    cli = [sys.executable, "-m", "itsm_agent.cli"]
    seeded = subprocess.run([*cli, "seed"], env=env, capture_output=True, text=True, cwd=PROJECT_ROOT, timeout=60)
    assert seeded.returncode == 0, seeded.stderr

    sc = load_scenario("vpn_happy_path")
    stdin = "\n".join([sc["turns"][0]["user"], sc["turns"][1]["user"], "y", "works now", "/quit"]) + "\n"
    chat = subprocess.run(
        [*cli, "chat", "--user", "u-1001", "--incident", "INC-1001", "-v"],
        env=env, input=stdin, capture_output=True, text=True, cwd=PROJECT_ROOT, timeout=120,
    )
    assert chat.returncode == 0, chat.stderr
    assert "Confirmation requested for INC-1001" in chat.stdout
    assert "[tool] close_incident" in chat.stdout

    show = subprocess.run([*cli, "show", "INC-1001"], env=env, capture_output=True, text=True, cwd=PROJECT_ROOT, timeout=60)
    assert json.loads(show.stdout)["incident"]["state"] == "closed"
    assert Repository(tmp_path / "itsm.db").get_incident("INC-1001")["resolution_evidence_run_id"] == "DR-INC-1001-002"
    log = (tmp_path / "agent.log").read_text()
    assert "tool=close_incident incident=INC-1001 actor=u-1001" in log and "status=ok" in log


def test_cli_rejects_inactive_user(tmp_path):
    env = {**os.environ, "ITSM_DB_PATH": str(tmp_path / "itsm.db"), "ITSM_CHECKPOINT_DB_PATH": str(tmp_path / "cp.db"),
           "ITSM_LOG_PATH": str(tmp_path / "a.log"), "ITSM_FIXTURE_SCRIPT": str(SCENARIO_DIR / "vpn_happy_path.json")}
    cli = [sys.executable, "-m", "itsm_agent.cli"]
    subprocess.run([*cli, "seed"], env=env, capture_output=True, cwd=PROJECT_ROOT, timeout=60, check=True)
    r = subprocess.run([*cli, "chat", "--user", "u-1004"], env=env, input="/quit\n", capture_output=True, text=True,
                       cwd=PROJECT_ROOT, timeout=60)
    assert r.returncode == 2 and "inactive" in r.stderr
