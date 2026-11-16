from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from itsm_agent.config import DATA_DIR, PROJECT_ROOT, Settings
from itsm_agent.db import Repository
from itsm_agent.diagnostics.service import DEFAULT_PROFILES, DiagnosticBackend, DiagnosticServer
from itsm_agent.fixture_model import ScriptedChatModel
from itsm_agent.seed import seed_database
from itsm_agent.service import ITSMAgentService

SCENARIO_DIR = PROJECT_ROOT / "fixtures" / "scenarios"


@pytest.fixture
def diag_server():
    backend = DiagnosticBackend.from_file(DEFAULT_PROFILES, timeout_delay_s=1.0)
    server = DiagnosticServer(backend, port=0).start_background()
    yield server
    server.stop()


@pytest.fixture
def settings(tmp_path: Path, diag_server: DiagnosticServer) -> Settings:
    return Settings(
        model_mode="fixture",
        db_path=tmp_path / "itsm.db",
        checkpoint_db_path=tmp_path / "checkpoints.db",
        outbox_path=tmp_path / "outbox" / "notifications.jsonl",
        log_path=tmp_path / "logs" / "itsm-agent.log",
        kb_path=DATA_DIR / "kb_articles.json",
        seed_path=DATA_DIR / "seed_data.json",
        diag_base_url=diag_server.url,
        diag_timeout_s=0.25,
        diag_retries=1,
    )


@pytest.fixture
def repo(settings: Settings) -> Repository:
    r = Repository(settings.db_path)
    seed_database(r, settings.seed_path)
    return r


@pytest.fixture
def make_service(settings: Settings, repo: Repository):
    created: list[ITSMAgentService] = []

    def factory(script: list[dict[str, Any]], **overrides: Any) -> ITSMAgentService:
        svc = ITSMAgentService(replace(settings, **overrides) if overrides else settings, model=ScriptedChatModel(script=script))
        created.append(svc)
        return svc

    yield factory
    for svc in created:
        svc.close()


def load_scenario(name: str) -> dict[str, Any]:
    return json.loads((SCENARIO_DIR / f"{name}.json").read_text())


def run_turns(service: ITSMAgentService, user_id: str, incident_id: str | None, turns: list[dict]) -> tuple[str, list]:
    thread_id = service.start_conversation(user_id, incident_id)
    results = []
    for turn in turns:
        if "user" in turn:
            results.append(service.send_message(thread_id, user_id, turn["user"]))
        else:
            results.append(service.respond_to_confirmation(thread_id, user_id, turn["confirm"], turn.get("comment")))
    return thread_id, results


def tool_results(results: list, name: str) -> list[dict]:
    out = []
    for r in results:
        for call in r.tool_calls:
            if call["name"] == name:
                out.append(json.loads(call["result"]))
    return out


def step(*calls: tuple[str, dict], content: str = "", **extra: Any) -> dict:
    return {"content": content, "tool_calls": [{"name": n, "args": a} for n, a in calls], **extra}


def say(text: str) -> dict:
    return {"content": text}
