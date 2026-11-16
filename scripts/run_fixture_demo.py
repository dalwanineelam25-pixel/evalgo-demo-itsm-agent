"""Run fixture-mode scenarios end to end and save transcripts, logs and before/after state to samples/.

Usage: uv run python scripts/run_fixture_demo.py [scenario ...]
(default: all files in fixtures/scenarios/)
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

from itsm_agent.config import DATA_DIR, PROJECT_ROOT, Settings
from itsm_agent.db import Repository
from itsm_agent.diagnostics.service import DEFAULT_PROFILES, DiagnosticBackend, DiagnosticServer
from itsm_agent.fixture_model import ScriptedChatModel
from itsm_agent.logging_setup import configure_logging
from itsm_agent.seed import seed_database
from itsm_agent.service import ITSMAgentService

SCENARIOS = PROJECT_ROOT / "fixtures" / "scenarios"
SAMPLES = PROJECT_ROOT / "samples"


def _markdown(name: str, sc: dict, turns: list[tuple[dict, dict]]) -> str:
    lines = [f"# Fixture run: {name}", "", sc["description"], "",
             f"User: `{sc['user_id']}` | Incident: `{sc['incident_id']}` | Model: scripted fixture (not a real LLM)", ""]
    for turn, result in turns:
        if "user" in turn:
            lines += [f"**Employee:** {turn['user']}", ""]
        else:
            lines += [f"**Employee (confirmation prompt):** {'YES' if turn['confirm'] else 'NO'}"
                      + (f" - {turn['comment']}" if turn.get("comment") else ""), ""]
        for call in result["tool_calls"]:
            lines += [f"- tool `{call['name']}` args `{json.dumps(call['args'])}`",
                      f"  - result: `{(call['result'] or '')[:400]}`"]
        if result["tool_calls"]:
            lines.append("")
        if result["pending_confirmation"]:
            lines += [f"_Conversation paused for caller confirmation: {result['pending_confirmation']['question']}_", ""]
        else:
            lines += [f"**Agent:** {result['reply']}", ""]
    return "\n".join(lines)


def run(name: str) -> Path:
    sc = json.loads((SCENARIOS / f"{name}.json").read_text())
    out = SAMPLES / name
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    work = Path(tempfile.mkdtemp(prefix=f"itsm-{name}-"))
    server = DiagnosticServer(DiagnosticBackend.from_file(DEFAULT_PROFILES, timeout_delay_s=3.0), port=0).start_background()
    settings = Settings(
        model_mode="fixture",
        db_path=work / "itsm.db",
        checkpoint_db_path=work / "checkpoints.db",
        outbox_path=work / "outbox" / "notifications.jsonl",
        log_path=work / "itsm-agent.log",
        kb_path=DATA_DIR / "kb_articles.json",
        seed_path=DATA_DIR / "seed_data.json",
        diag_base_url=server.url,
        diag_timeout_s=1.0,
    )
    configure_logging(settings.log_path, "INFO")
    repo = Repository(settings.db_path)
    seed_database(repo, settings.seed_path)
    (out / "state_before.json").write_text(json.dumps(repo.incident_snapshot(sc["incident_id"]), indent=2, default=str))

    svc = ITSMAgentService(settings, model=ScriptedChatModel(script=sc["model_script"]))
    try:
        thread_id = svc.start_conversation(sc["user_id"], sc["incident_id"])
        turns = []
        for turn in sc["turns"]:
            if "user" in turn:
                result = svc.send_message(thread_id, sc["user_id"], turn["user"])
            else:
                result = svc.respond_to_confirmation(thread_id, sc["user_id"], turn["confirm"], turn.get("comment"))
            turns.append((turn, result.as_dict()))
        (out / "conversation.json").write_text(json.dumps(
            {"thread_id": thread_id, "scenario": sc["description"], "turns": [{"input": t, "result": r} for t, r in turns],
             "checkpointed_transcript": svc.transcript(thread_id, sc["user_id"])}, indent=2, default=str))
        (out / "conversation.md").write_text(_markdown(name, sc, turns))
    finally:
        svc.close()
        server.stop()
    (out / "state_after.json").write_text(json.dumps(repo.incident_snapshot(sc["incident_id"]), indent=2, default=str))
    shutil.copy(settings.log_path, out / "itsm-agent.log")
    if settings.outbox_path.exists():
        shutil.copy(settings.outbox_path, out / "outbox_notifications.jsonl")
    shutil.rmtree(work, ignore_errors=True)
    return out


def main(argv: list[str]) -> int:
    names = argv or sorted(p.stem for p in SCENARIOS.glob("*.json"))
    for name in names:
        print(f"{name}: {run(name)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
