"""Scripted chat model for local test mode (no API calls, no credits).

It replays a fixed list of assistant turns (text and/or tool calls). The real graph, tools,
database and diagnostic service still run, so this exercises application logic, but it says
nothing about how well a real model would choose tools.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import PrivateAttr


class FixtureScriptExhausted(RuntimeError):
    """The scripted model was called more times than the script has steps."""


class ScriptedChatModel(BaseChatModel):
    """Returns the next scripted AIMessage on each call.

    Script step format::

        {"content": "text shown to the user", "tool_calls": [{"name": "get_incident", "args": {...}}],
         "stop_reason": "end_turn"}
    """

    script: list[dict[str, Any]]
    _position: int = PrivateAttr(default=0)
    _bound_tools: list[str] = PrivateAttr(default_factory=list)
    _received: list[list[BaseMessage]] = PrivateAttr(default_factory=list)

    @classmethod
    def from_file(cls, path: Path | str) -> "ScriptedChatModel":
        data = json.loads(Path(path).read_text())
        steps = data["model_script"] if isinstance(data, dict) else data
        return cls(script=steps)

    @property
    def _llm_type(self) -> str:
        return "scripted-fixture"

    @property
    def position(self) -> int:
        return self._position

    @property
    def received(self) -> list[list[BaseMessage]]:
        """Message lists the model was called with (most recent last)."""
        return self._received

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "ScriptedChatModel":
        self._bound_tools = [convert_to_openai_tool(t)["function"]["name"] for t in tools]
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self._received.append(list(messages))
        if self._position >= len(self.script):
            raise FixtureScriptExhausted(
                f"Fixture script has {len(self.script)} steps but the model was called again."
            )
        step = self.script[self._position]
        self._position += 1
        tool_calls = []
        for i, call in enumerate(step.get("tool_calls", [])):
            if self._bound_tools and call["name"] not in self._bound_tools:
                raise ValueError(f"Scripted tool call {call['name']!r} is not a bound tool")
            tool_calls.append(
                {"name": call["name"], "args": call.get("args", {}), "id": f"toolu_fixture_{self._position:03d}_{i}", "type": "tool_call"}
            )
        stop_reason = step.get("stop_reason") or ("tool_use" if tool_calls else "end_turn")
        message = AIMessage(
            content=step.get("content", ""),
            tool_calls=tool_calls,
            response_metadata={"model_name": "scripted-fixture", "stop_reason": stop_reason},
        )
        return ChatResult(generations=[ChatGeneration(message=message)])
