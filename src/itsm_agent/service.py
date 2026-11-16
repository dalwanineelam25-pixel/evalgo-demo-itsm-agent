"""Conversation service used by the CLI and the HTTP API."""

from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.errors import GraphRecursionError
from langgraph.types import Command

from . import telemetry
from .config import Settings
from .db import Repository
from .diagnostics import VPNDiagnosticClient
from .graph import build_chat_model, build_graph
from .kb import KnowledgeBase
from .tools import ToolContext

log = logging.getLogger(__name__)


class ConversationError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class TurnResult:
    thread_id: str
    reply: str
    pending_confirmation: dict | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "reply": self.reply,
            "pending_confirmation": self.pending_confirmation,
            "tool_calls": self.tool_calls,
        }


class ITSMAgentService:
    def __init__(self, settings: Settings, model: BaseChatModel | None = None):
        telemetry.setup()  # off unless OTEL_EXPORTER_OTLP_ENDPOINT is set
        self.settings = settings
        self.repo = Repository(settings.db_path)
        self.repo.init_schema()
        self.kb = KnowledgeBase.from_file(settings.kb_path)
        self.diagnostics = VPNDiagnosticClient(settings.diag_base_url, settings.diag_timeout_s, settings.diag_retries)
        self.model = model or build_chat_model(settings)
        settings.checkpoint_db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False is safe: SqliteSaver serialises access with its own lock.
        self._checkpoint_conn = sqlite3.connect(settings.checkpoint_db_path, check_same_thread=False)
        self.checkpointer = SqliteSaver(self._checkpoint_conn)
        self.graph = build_graph(
            self.model,
            ToolContext(repo=self.repo, kb=self.kb, diagnostics=self.diagnostics, outbox_path=settings.outbox_path),
            self.checkpointer,
        )

    def close(self) -> None:
        self._checkpoint_conn.close()

    # ----------------------------------------------------------------- helpers
    def _config(self, thread_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": thread_id}, "recursion_limit": self.settings.recursion_limit}

    def _authorize_thread(self, thread_id: str, user_id: str) -> dict:
        conv = self.repo.get_conversation(thread_id)
        if conv is None:
            raise ConversationError("not_found", f"Conversation {thread_id} not found")
        if conv["user_id"] != user_id:
            log.warning("conversation access denied thread=%s user=%s", thread_id, user_id)
            raise ConversationError("forbidden", "This conversation belongs to another user")
        user = self.repo.get_user(user_id)
        if user is None or not user["active"]:
            raise ConversationError("forbidden", "User account is inactive")
        return conv

    def pending_confirmation(self, thread_id: str) -> dict | None:
        snapshot = self.graph.get_state(self._config(thread_id))
        for intr in snapshot.interrupts:
            if isinstance(intr.value, dict) and intr.value.get("type") == "caller_confirmation":
                return intr.value
        return None

    def _run(self, thread_id: str, graph_input: Any, before: int) -> TurnResult:
        config = self._config(thread_id)
        try:
            self.graph.invoke(graph_input, config)
        except GraphRecursionError:
            log.error("recursion limit reached thread=%s", thread_id)
            return TurnResult(
                thread_id,
                "I stopped because this request took too many steps. Changes already recorded are saved; "
                "a service desk analyst can take over if needed.",
                self.pending_confirmation(thread_id),
            )
        messages = self.graph.get_state(config).values.get("messages", [])
        new = messages[before:]
        tool_calls = []
        results = {m.tool_call_id: m for m in new if isinstance(m, ToolMessage)}
        for m in new:
            if isinstance(m, AIMessage):
                for tc in m.tool_calls:
                    res = results.get(tc["id"])
                    tool_calls.append({"name": tc["name"], "args": tc["args"], "result": res.content if res else None})
        reply = ""
        for m in reversed(new):
            if isinstance(m, AIMessage) and m.text.strip():
                reply = m.text.strip()
                break
        pending = self.pending_confirmation(thread_id)
        if pending and not reply:
            reply = pending["question"]
        return TurnResult(thread_id, reply, pending, tool_calls)

    def _message_count(self, thread_id: str) -> int:
        return len(self.graph.get_state(self._config(thread_id)).values.get("messages", []))

    # --------------------------------------------------------------------- API
    def start_conversation(self, user_id: str, incident_id: str | None = None) -> str:
        user = self.repo.get_user(user_id)
        if user is None:
            raise ConversationError("unknown_user", f"Unknown user {user_id}")
        if not user["active"]:
            log.warning("conversation refused for inactive user=%s", user_id)
            raise ConversationError("forbidden", "User account is inactive")
        thread_id = f"thr-{uuid.uuid4().hex[:12]}"
        self.repo.create_conversation(thread_id, user_id, incident_id)
        log.info("conversation started thread=%s user=%s incident=%s", thread_id, user_id, incident_id)
        return thread_id

    def send_message(self, thread_id: str, user_id: str, text: str) -> TurnResult:
        conv = self._authorize_thread(thread_id, user_id)
        if not text.strip():
            raise ConversationError("bad_request", "Message is empty")
        if self.pending_confirmation(thread_id):
            raise ConversationError("confirmation_pending", "Answer the pending confirmation question first")
        before = self._message_count(thread_id)
        graph_input: dict[str, Any] = {"messages": [HumanMessage(content=text.strip())]}
        if before == 0:
            graph_input.update(actor_id=user_id, incident_id=conv["incident_number"], pending_confirmation=None)
        log.info("user_message thread=%s user=%s chars=%d", thread_id, user_id, len(text))
        return self._run(thread_id, graph_input, before)

    def respond_to_confirmation(self, thread_id: str, user_id: str, confirmed: bool, comment: str | None = None) -> TurnResult:
        self._authorize_thread(thread_id, user_id)
        if not self.pending_confirmation(thread_id):
            raise ConversationError("no_pending_confirmation", "There is no pending confirmation in this conversation")
        before = self._message_count(thread_id)
        log.info("confirmation_answer thread=%s user=%s confirmed=%s", thread_id, user_id, confirmed)
        return self._run(thread_id, Command(resume={"confirmed": bool(confirmed), "comment": comment or ""}), before)

    def transcript(self, thread_id: str, user_id: str) -> list[dict[str, Any]]:
        self._authorize_thread(thread_id, user_id)
        out = []
        for m in self.graph.get_state(self._config(thread_id)).values.get("messages", []):
            if isinstance(m, HumanMessage):
                out.append({"role": "user", "content": m.text})
            elif isinstance(m, AIMessage):
                out.append({"role": "assistant", "content": m.text, "tool_calls": [{"name": t["name"], "args": t["args"]} for t in m.tool_calls]})
            elif isinstance(m, ToolMessage):
                out.append({"role": "tool", "name": m.name, "content": m.content})
        return out
