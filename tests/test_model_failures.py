"""Model-side failures: API errors, refusals, truncation, runaway loops, conversation guards."""

from __future__ import annotations

from typing import Any

import anthropic
import httpx2
import pytest
from conftest import run_turns, say, step
from langchain_core.language_models import BaseChatModel
from langchain_core.outputs import ChatResult

from itsm_agent.graph import SAFE_FAILURE_REPLY
from itsm_agent.service import ConversationError, ITSMAgentService

pytestmark = pytest.mark.integration


class _FailingModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "failing"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "_FailingModel":
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        raise anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))


def test_model_api_error_returns_safe_reply(settings, repo):
    svc = ITSMAgentService(settings, model=_FailingModel())
    try:
        thread_id = svc.start_conversation("u-1001", "INC-1001")
        result = svc.send_message(thread_id, "u-1001", "VPN broken")
        assert result.reply == SAFE_FAILURE_REPLY
        assert repo.list_audit("INC-1001") == []
    finally:
        svc.close()


def test_refusal_does_not_execute_tool_calls(make_service, repo):
    svc = make_service([step(("close_incident", {"incident_id": "INC-1001", "close_notes": "x"}), stop_reason="refusal")])
    _, results = run_turns(svc, "u-1001", "INC-1001", [{"user": "..."}])
    assert results[0].tool_calls == []
    assert "can't help" in results[0].reply
    assert repo.get_incident("INC-1001")["state"] == "new"


def test_truncated_response_does_not_execute_tool_calls(make_service, repo):
    svc = make_service([step(("add_work_note", {"incident_id": "INC-1001", "note": "partial"}), stop_reason="max_tokens")])
    _, results = run_turns(svc, "u-1001", "INC-1001", [{"user": "..."}])
    assert results[0].tool_calls == []
    assert "cut off" in results[0].reply
    assert len(repo.list_work_notes("INC-1001")) == 0


def test_runaway_tool_loop_is_stopped(make_service):
    loop = [step(("get_incident", {"incident_id": "INC-1001"})) for _ in range(30)]
    svc = make_service(loop, recursion_limit=10)
    _, results = run_turns(svc, "u-1001", "INC-1001", [{"user": "loop"}])
    assert "too many steps" in results[0].reply


def test_new_message_rejected_while_confirmation_pending(make_service):
    script = [
        step(("run_vpn_diagnostic", {"incident_id": "INC-1001"})),
        step(("run_vpn_diagnostic", {"incident_id": "INC-1001"})),
        step(("request_caller_confirmation", {"incident_id": "INC-1001", "question": "Fixed?"})),
    ]
    svc = make_service(script)
    thread_id, results = run_turns(svc, "u-1001", "INC-1001", [{"user": "updated"}])
    assert results[0].pending_confirmation is not None
    assert results[0].reply == "Fixed?"
    with pytest.raises(ConversationError) as exc:
        svc.send_message(thread_id, "u-1001", "hello?")
    assert exc.value.code == "confirmation_pending"


def test_confirmation_without_pending_request_is_rejected(make_service):
    svc = make_service([say("hi")])
    thread_id, _ = run_turns(svc, "u-1001", "INC-1001", [{"user": "hi"}])
    with pytest.raises(ConversationError) as exc:
        svc.respond_to_confirmation(thread_id, "u-1001", True)
    assert exc.value.code == "no_pending_confirmation"


def test_tool_error_from_invalid_arguments_is_returned_to_model(make_service, repo):
    # Missing required args: ToolNode returns an error ToolMessage instead of crashing the graph.
    svc = make_service([step(("resolve_incident", {"incident_id": "INC-1001"})), say("I need more details.")])
    _, results = run_turns(svc, "u-1001", "INC-1001", [{"user": "resolve it"}])
    assert "error" in results[0].tool_calls[0]["result"].lower()
    assert repo.get_incident("INC-1001")["state"] == "new"
