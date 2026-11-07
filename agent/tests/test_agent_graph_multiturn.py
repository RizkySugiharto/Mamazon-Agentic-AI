from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
import pytest

from src.agent.graph import AgentGraph, DiscordAgentContext


class _SequentialMockModel:
    """Mock chat model returning a sequence of responses and recording received messages."""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.invocations: list[list[Any]] = []

    def bind_tools(self, tools: Any) -> _SequentialMockModel:
        return self

    def invoke(self, messages: list[Any]) -> Any:
        self.invocations.append(list(messages))
        if not self.responses:
            raise RuntimeError("No more responses queued in mock model")
        return self.responses.pop(0)


def test_multiturn_history_preserves_ai_message_and_followup(validator, monkeypatch):
    """Ensure assistant turn in history is passed as AIMessage and multi-turn context works."""
    turn1_user = "yg pp bundle 26 okt udh di validasi blm?"
    turn1_assistant = (
        "Belum, PP Bundle 26 Oktober (PP-05) belum pernah divalidasi.\n"
        "Mau saya buatkan rencana validasi untuk:\n"
        "1. Seluruh sheet PP Bundle 26 Oktober (semua divisi), atau\n"
        "2. Satu divisi tertentu saja (mis. Holmes, Fletch, Conan)?"
    )
    turn2_user = "yg divisi fletch aja, sama kasih tau nama org yg kena siapa aja"

    history = [
        {"role": "user", "content": turn1_user},
        {"role": "assistant", "content": turn1_assistant},
    ]

    # Step 1: Model invokes tool plan_validate_sheet
    step1_call = SimpleNamespace(
        content="",
        tool_calls=[
            {
                "name": "plan_validate_sheet",
                "args": {"sheet": "PP 29 Oktober", "divisi": "Fletch"},
                "id": "call_validate_1",
            }
        ],
    )
    # Step 2: Model receives ToolMessage and answers with text
    step2_answer = SimpleNamespace(
        content=(
            "Rencana validasi untuk divisi Fletch sudah saya siapkan. "
            "Silakan konfirmasi lewat tombol di bawah ini."
        ),
        tool_calls=[],
    )

    mock_model = _SequentialMockModel([step1_call, step2_answer])
    monkeypatch.setattr("src.agent.graph.build_chat_model", lambda *a, **k: mock_model)

    member = SimpleNamespace(id=1, roles=[SimpleNamespace(name="spade")])
    graph = AgentGraph(validator, member=member, provider="openrouter")

    ctx = DiscordAgentContext(
        user_id="123",
        channel_id="456",
        actor="TestUser",
        member=member,
        validator=validator,
    )

    answer = graph.run(turn2_user, history=history, context=ctx)

    # 1. Assert exactly 2 steps taken (not looping 6 times)
    assert len(mock_model.invocations) == 2

    # 2. Assert step 1 prompt has AIMessage for assistant turn
    step1_messages = mock_model.invocations[0]
    has_ai_turn = any(isinstance(m, AIMessage) and turn1_assistant in str(m.content) for m in step1_messages)
    assert has_ai_turn, "Assistant history turn must be an AIMessage"

    # 3. Assert step 2 prompt has ToolMessage containing the tool result
    step2_messages = mock_model.invocations[1]
    has_tool_message = any(isinstance(m, ToolMessage) and m.tool_call_id == "call_validate_1" for m in step2_messages)
    assert has_tool_message, "Model must receive ToolMessage on step 2"

    # 4. Assert answer text is from the model, and pending_action is attached
    assert "Rencana validasi untuk divisi Fletch" in answer.text
    assert answer.pending_action is not None
    assert answer.pending_action["command"] == "validate"
    assert answer.pending_action["params"]["divisi"] == "Fletch"

    # 5. Assert generic greeting is NOT emitted
    assert "Ada yang bisa kubantu seputar tugas PP" not in answer.text


def test_respond_node_empty_answer_with_pending_action_synthesizes_plan(validator, monkeypatch):
    """If model reaches max steps while having planned an action, describe the plan instead of fallback greeting."""
    # Model repeatedly calls tool up to max steps without text
    step1_call = SimpleNamespace(
        content="",
        tool_calls=[
            {
                "name": "plan_validate_sheet",
                "args": {"sheet": "PP 29 Oktober", "divisi": "Fletch"},
                "id": "call_validate_loop",
            }
        ],
    )
    # Return 10 calls to exceed CHAT_AGENT_MAX_STEPS (6)
    mock_model = _SequentialMockModel([step1_call] * 10)
    monkeypatch.setattr("src.agent.graph.build_chat_model", lambda *a, **k: mock_model)

    member = SimpleNamespace(id=1, roles=[SimpleNamespace(name="spade")])
    graph = AgentGraph(validator, member=member, provider="openrouter")

    answer = graph.run("tolong validasi sheet PP 29 Oktober divisi Fletch")

    # Must NOT be the generic fallback greeting
    assert "Ada yang bisa kubantu seputar tugas PP, Buzzer, validasi, atau denda SPADE?" not in answer.text

    # Must describe the planned action clearly
    assert "Rencana aksi untuk **validate** telah disiapkan" in answer.text
    assert answer.pending_action is not None
    assert answer.pending_action["command"] == "validate"


def test_fallback_greeting_never_carries_pending_action(validator):
    """Generic greeting from deterministic fallback must never have pending_action attached."""
    graph = AgentGraph(validator, provider="openrouter")
    # Non-planning random question that falls back
    answer = graph.run("info acak yang tidak merencanakan aksi")
    if "Ada yang bisa kubantu" in answer.text:
        assert answer.pending_action is None
