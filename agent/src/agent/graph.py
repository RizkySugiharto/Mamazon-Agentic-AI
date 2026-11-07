"""The LangGraph agent that powers conversational #ai-chat.

Graph shape:

    START -> guard -> retrieve -> agent <-> tools -> respond -> END

    guard     deterministic refusal checks (rule edits, out-of-scope, action
              requests). Never calls a model, so a refusal can never be
              talked past.
    retrieve  hybrid search over the local zvec knowledge base. The retrieved
              text becomes authoritative context for the model.
    agent     the model decides whether it can answer or needs a tool. This
              is what makes the bot conversational instead of templated: it
              picks its own next move.
    tools     read-only, role-gated tools (denda, task requirements, sheets,
              audit, calculator, memory). The tool result goes back to the
              model, which then answers in natural language.
    respond   the final reply is checked for invented Rupiah amounts and
              invented actions before it is returned.

The deterministic `chat_agent.answer()` remains the fallback: if the graph
fails for any reason, the caller gets the grounded deterministic answer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Sequence, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.runtime import Runtime

from .. import config
from ..chat_agent import (
    AgentToolkit,
    ChatAnswer,
    _assert_grounded_output,
    _scrub_error,
    answer as deterministic_answer,
)
from ..utils import channel_mention, format_discord_message
from .providers import build_chat_model, provider_label

logger = logging.getLogger(__name__)

# Module-level checkpointer for keyed conversation state (persistent across restarts)
try:
    from ..memory_store import SqliteCheckpointSaver
    _AGENT_CHECKPOINTER = SqliteCheckpointSaver()
except Exception as _exc:
    logger.warning("Failed to initialize SqliteCheckpointSaver, falling back to MemorySaver: %s", _exc)
    _AGENT_CHECKPOINTER = MemorySaver()


@dataclass
class DiscordAgentContext:
    """Run-scoped context passed via LangGraph context_schema."""

    user_id: str = ""
    channel_id: str = ""
    guild_id: str = ""
    actor: str = ""
    member: Any = None
    validator: Any = None
    runner: Any = None
    session: Any = None
    provider: str | None = None


def _get_agent_rules_text() -> str:
    path = config.AGENT_RULES_PATH
    if path.exists():
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return ""


class AgentState(TypedDict, total=False):
    """State carried through the graph."""

    question: str
    actor: str
    history: list[dict[str, str]]
    retrieved: list[str]
    context: str
    tool_output: list[str]
    tool_calls: list[dict[str, Any]]
    messages: list[Any]
    answer: str
    pending_action: dict[str, Any] | None
    boundary_guidance: str
    blocked: bool
    refusal: str
    error: str
    steps: int


def _merge(left: list[str], right: list[str]) -> list[str]:
    """Reducer that appends tool output without duplicating it."""
    return list(left) + [item for item in right if item not in left]


class AgentGraph:
    """A compiled LangGraph agent bound to one validator/member context."""

    def __init__(
        self,
        validator: Any,
        actor: str = "",
        member: Any = None,
        provider: str | None = None,
        runner: Any = None,
        session: Any = None,
        channel_id: str = "",
        user_id: str = "",
    ) -> None:
        self.validator = validator
        self.actor = actor or ""
        self.member = member
        self.provider = provider
        self.runner = runner
        self.session = session
        self.channel_id = str(channel_id or "")
        self.user_id = str(user_id or (getattr(member, "id", None) or actor or ""))
        self._graph: Any = None
        self._toolkit: Any = None

    # ── construction ──────────────────────────────────────────────────────
    def _toolkit_instance(self) -> Any:
        """The existing role-gating toolkit, reused as-is."""
        if self._toolkit is None:
            from ..chat_agent import AgentToolkit

            self._toolkit = AgentToolkit(self.validator, self.actor, self.member)
        return self._toolkit

    def _build(self) -> Any:
        """Compile the graph once with checkpointer and reuse it."""
        if self._graph is not None:
            return self._graph
        from langgraph.graph import END, START, StateGraph

        from .tools import build_tools

        graph = StateGraph(AgentState, context_schema=DiscordAgentContext)
        graph.add_node("guard", self._guard_node)
        graph.add_node("retrieve", self._retrieve_node)
        graph.add_node("agent", self._agent_node)
        graph.add_node("tools", self._tools_node)
        graph.add_node("respond", self._respond_node)

        graph.add_edge(START, "guard")
        graph.add_conditional_edges(
            "guard",
            lambda state: "respond" if state.get("blocked") else "retrieve",
            {"respond": "respond", "retrieve": "retrieve"},
        )
        graph.add_edge("retrieve", "agent")
        graph.add_conditional_edges(
            "agent",
            self._after_agent,
            {"tools": "tools", "respond": "respond"},
        )
        graph.add_edge("tools", "agent")
        graph.add_edge("respond", END)

        self._graph = graph.compile(checkpointer=_AGENT_CHECKPOINTER)
        return self._graph

    # ── nodes ─────────────────────────────────────────────────────────────
    def _guard_node(
        self, state: AgentState, config_arg: Any = None, runtime: Any = None
    ) -> dict[str, Any]:
        """Boundary guidance. Injects natural guidance instead of rigid canned refusals."""
        question = str(state.get("question") or "")
        ctx: DiscordAgentContext | None = getattr(runtime, "context", None) if runtime else None
        val = ctx.validator if (ctx and ctx.validator) else self.validator
        act = ctx.actor if (ctx and ctx.actor) else self.actor

        grounded = deterministic_answer(
            question, val, act, history=state.get("history") or ()
        )

        # Developer-only rule change request
        if grounded.source == "workflow §13 unhappy flow 2":
            guidance = (
                "Pengguna meminta menambah, mengubah, atau menghapus aturan denda/kepanitiaan. "
                "Jelaskan secara ramah, santun, dan bersahabat dalam bahasa Indonesia (aku/kamu) bahwa "
                "aturan kepanitiaan SPADE bersifat developer-only di rulebase untuk mencegah perubahan tidak terverifikasi. "
                "Arahkan mereka untuk berkoordinasi dengan koordinator SPADE atau developer jika ada usulan perubahan."
            )
            return {"blocked": False, "boundary_guidance": guidance, "context": guidance}

        # Out-of-scope query: let the AI respond politely and guide back to SPADE
        if not grounded.in_scope:
            guidance = (
                "Topik ini di luar lingkup kepanitiaan SPADE (Paid Promote, Buzzer, validasi tugas, denda, Angel Card). "
                "Tanggapi secara ramah, hangat, dan luwes dalam bahasa Indonesia (aku/kamu). "
                "Sampaikan secara santun bahwa kamu asisten khusus tugas dan denda SPADE, "
                "lalu tanyakan apakah ada urusan tugas SPADE yang bisa kamu bantu."
            )
            return {"blocked": False, "boundary_guidance": guidance, "context": guidance}

        # Direct mutating action requests
        if grounded.source == "permissions":
            guidance = (
                "Pengguna meminta aksi langsung. Beritahu dengan ramah bahwa aksi yang mengubah data "
                "dapat disiapkan rencananya terlebih dahulu menggunakan perintah chat (mis. /validate) "
                "atau melalui tombol konfirmasi sesuai role mereka."
            )
            return {"blocked": False, "boundary_guidance": guidance, "context": guidance}

        return {"blocked": False, "context": grounded.text if config.CHAT_INCLUDE_TEMPLATES else ""}

    def _retrieve_node(
        self, state: AgentState, config_arg: Any = None, runtime: Any = None
    ) -> dict[str, Any]:
        """Pull relevant rules/commands/info from the local vector store."""
        question = str(state.get("question") or "")
        try:
            from . import knowledge

            store = knowledge.ensure_store()
            if store is None:
                return {"retrieved": [], "context": state.get("context", "")}
            results = knowledge.search(question, store=store)
            snippets = [
                f"[{doc.metadata.get('id', '?')}] {doc.page_content}"
                for doc, _score in results
            ]
            return {"retrieved": snippets, "context": state.get("context", "")}
        except Exception as exc:
            logger.warning("Retrieval failed, continuing without context: %s", exc)
            return {"retrieved": [], "context": state.get("context", "")}

    def _agent_node(
        self, state: AgentState, config_arg: Any = None, runtime: Any = None
    ) -> dict[str, Any]:
        """Ask the model for the next move (answer or tool call)."""
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

        from .tools import build_tools

        ctx: DiscordAgentContext | None = getattr(runtime, "context", None) if runtime else None
        toolkit = (
            AgentToolkit(ctx.validator, ctx.actor, ctx.member)
            if ctx and ctx.validator
            else self._toolkit_instance()
        )
        tools = build_tools(toolkit)
        provider = ctx.provider if (ctx and ctx.provider) else self.provider
        model = build_chat_model(provider)

        if hasattr(model, "bind_tools"):
            try:
                model = model.bind_tools(tools)
            except Exception as exc:
                logger.warning("bind_tools failed, continuing without tools: %s", exc)

        history = list(state.get("history") or [])
        system = self._system_prompt(state)

        existing_messages = list(state.get("messages") or [])
        if not existing_messages:
            messages: list[Any] = [SystemMessage(content=system)]
            for turn in history[-8:]:
                role = str(turn.get("role") or "")
                content = str(turn.get("content") or "")[:1500]
                if not content:
                    continue
                if role == "assistant":
                    messages.append(AIMessage(content=content))
                else:
                    messages.append(HumanMessage(content=content))

            user_turn = str(state.get("question") or "")
            context_block = _context_block(system)
            if context_block:
                user_turn += "\n\n" + context_block
            messages.append(HumanMessage(content=user_turn))
        else:
            messages = list(existing_messages)
            if messages and isinstance(messages[0], SystemMessage):
                messages[0] = SystemMessage(content=system)

        current_step = int(state.get("steps") or 0)
        logger.debug("Agent invoking model at step %d with %d messages", current_step + 1, len(messages))

        try:
            response = model.invoke(messages)
        except Exception as exc:
            reason = _scrub_error(exc)
            logger.warning("Agent model call failed: %s", reason)
            return {"error": reason, "steps": current_step + 1}

        tool_calls = list(getattr(response, "tool_calls", None) or [])
        content = getattr(response, "content", "") or ""
        if isinstance(content, list):
            content = " ".join(
                str(block.get("text", "")) for block in content if isinstance(block, dict)
            )

        from langchain_core.messages import BaseMessage

        if isinstance(response, BaseMessage):
            messages.append(response)
        else:
            messages.append(
                AIMessage(
                    content=str(content),
                    tool_calls=tool_calls,
                )
            )

        if tool_calls:
            logger.debug("Agent model returned %d tool call(s) at step %d", len(tool_calls), current_step + 1)
            return {
                "tool_calls": tool_calls,
                "messages": messages,
                "steps": current_step + 1,
            }

        logger.debug("Agent model completed answer at step %d", current_step + 1)
        return {
            "answer": str(content),
            "messages": messages,
            "steps": current_step + 1,
        }

    def _after_agent(self, state: AgentState) -> str:
        """Route to the tools node when the model asked for a tool."""
        if state.get("tool_calls"):
            if int(state.get("steps") or 0) >= config.CHAT_AGENT_MAX_STEPS:
                return "respond"
            return "tools"
        return "respond"

    def _tools_node(
        self, state: AgentState, config_arg: Any = None, runtime: Any = None
    ) -> dict[str, Any]:
        """Execute the requested tools and collect their output."""
        from langchain_core.messages import ToolMessage
        from .tools import build_tools

        ctx: DiscordAgentContext | None = getattr(runtime, "context", None) if runtime else None
        toolkit = (
            AgentToolkit(ctx.validator, ctx.actor, ctx.member)
            if ctx and ctx.validator
            else self._toolkit_instance()
        )
        calls = list(state.get("tool_calls") or [])
        tools = build_tools(toolkit)
        by_name = {getattr(item, "name", ""): item for item in tools}
        outputs: list[str] = []
        messages = list(state.get("messages") or [])

        for call in calls:
            name = str(call.get("name") or "")
            call_id = str(call.get("id") or "")
            arguments = call.get("args") or {}
            if not isinstance(arguments, dict):
                arguments = {}
            tool = by_name.get(name)
            if tool is None:
                err_text = f"Tool '{name}' tidak tersedia."
                outputs.append(err_text)
                messages.append(ToolMessage(content=err_text, tool_call_id=call_id, name=name))
                continue
            try:
                result = tool.invoke(arguments)
            except Exception as exc:
                err_text = f"Tool '{name}' gagal: {_scrub_error(exc)}"
                outputs.append(err_text)
                messages.append(ToolMessage(content=err_text, tool_call_id=call_id, name=name))
                continue

            res_str = str(result)
            outputs.append(f"[{name}] {res_str}")
            messages.append(ToolMessage(content=res_str, tool_call_id=call_id, name=name))

        pending = getattr(toolkit, "last_pending_action", None)
        return {
            "tool_output": outputs,
            "tool_calls": [],
            "messages": messages,
            "context": _append_context(state.get("context", ""), outputs),
            "pending_action": pending or state.get("pending_action"),
        }

    def _respond_node(
        self, state: AgentState, config_arg: Any = None, runtime: Any = None
    ) -> dict[str, Any]:
        """Produce the final, grounded reply formatted for Discord."""
        if state.get("blocked"):
            return {
                "answer": format_discord_message(state.get("refusal", "")),
                "pending_action": None,
            }
        answer = str(state.get("answer") or "").strip()
        pending = state.get("pending_action")
        tool_outputs = state.get("tool_output") or []

        if not answer:
            # If a pending mutating action was prepared, formulate an informative answer
            if pending and isinstance(pending, dict):
                cmd = pending.get("command", "")
                desc = pending.get("description", "")
                lines = [f"Rencana aksi untuk **{cmd}** telah disiapkan: {desc}."]
                if tool_outputs:
                    lines.append("\nDetail data yang diperoleh:")
                    lines.extend(f"• {out}" for out in tool_outputs[:5])
                lines.append("\nSilakan klik tombol di bawah untuk konfirmasi atau pembatalan.")
                return {
                    "answer": format_discord_message("\n".join(lines)),
                    "pending_action": pending,
                }
            # If tool outputs exist but no pending action
            if tool_outputs:
                lines = ["Berikut informasi yang berhasil diperoleh:"]
                lines.extend(f"• {out}" for out in tool_outputs[:10])
                return {
                    "answer": format_discord_message("\n".join(lines)),
                    "pending_action": None,
                }
            # Fallback only when no answer and no tool output
            grounded = deterministic_answer(
                str(state.get("question") or ""), self.validator, self.actor,
                history=state.get("history") or (),
            )
            return {
                "answer": format_discord_message(grounded.text),
                "pending_action": None,
            }

        return {
            "answer": format_discord_message(answer),
            "pending_action": pending,
        }

    # ── prompt ────────────────────────────────────────────────────────────
    def _system_prompt(self, state: AgentState) -> str:
        """Instructions plus rules knowledge and authoritative retrieved context."""
        context = str(state.get("context") or "")
        retrieved = "\n".join(state.get("retrieved") or [])
        tool_output = "\n".join(state.get("tool_output") or [])
        rules_text = _get_agent_rules_text()

        blocks = [
            "You are Mamazon, a warm, helpful, and natural AI assistant for SPADE committee management.",
            "Reply in polite, friendly, and empathetic Indonesian using aku/kamu.",
            "Always respond warmly to social greetings (hai, halo, pagi, dll.) and thanks (makasih, terima kasih).",
            "STRICT PROHIBITION ON ROBOTIC TEMPLATES: Never output rigid canned templates or repetitive robot scripts.",
            "",
            "CORE BEHAVIORAL BOUNDARIES (HARD LIMITS):",
            "- You cannot execute arbitrary code, shell commands, or modify local files.",
            "- Committee rules and denda tariffs are strictly developer-only; you cannot change them via chat.",
            "- Mutating actions (validate, blast, add/remove angel card, scan, hold/release blast) must be prepared using plan_* tools. Never claim an action is executed without the user confirmation button. Blast drafts require coordinator approval.",
            "- No hallucination: Never invent committee members, Rupiah amounts, dates, deadlines, evidence links, or claims of performed actions.",
            "- Permitted scope: SPADE committee management (Paid Promote, Buzzer, validation, fines recap, Angel Card registry).",
            "",
            "AMBIGUITY CONFIRMATION (CRITICAL & STRICT):",
            "- When a question, order, task code, sheet name, or member name is ambiguous or has multiple possible candidates:",
            "  NEVER guess or assume independently. You MUST ask the user to confirm first, presenting the available options clearly as bullet points.",
            "",
            "DISCORD MESSAGE FORMATTING (CRITICAL & STRICT):",
            "- NEVER use markdown tables with pipe characters (| col | col |). Discord chat does NOT support markdown tables.",
            "- Always format structured or tabular information using clean bullet points with bold keys (e.g. • **Nama:** Nilai) or monospace code blocks if tabular layout is necessary.",
            "- Use clean bullet points ('• ') instead of asterisks or raw hyphens.",
            "",
            "FLEXIBLE REQUEST COMPREHENSION:",
            "- Understand and respect the user's specific output format requests (e.g., 'hanya namanya saja', 'cuma nama', 'dalam bentuk poin', 'ringkas').",
            "  Do not force fixed tables or standard metadata when a custom format is requested.",
            "",
            "DENDA MEMBER LISTING:",
            "- When asked for list of penalized members / 'siapa kena denda' / 'daftar denda':",
            "  Use the get_fines() tool to retrieve the actual list of penalized members, their division, and amount.",
            "  Do NOT return generic tariff terms or sheet tables unless explicitly asked for tariff rules.",
            "",
            "KETENTUAN TUGAS PP & BUZZER (MANDATORY CHANNEL MENTION):",
            f"- When answering about ketentuan, rules, or requirements for PP or Buzzer tasks (both specific tasks like PP-01..PP-06, BZ-01..BZ-07 and general rules):",
            f"  ALWAYS mention the task channel {channel_mention(config.DISCORD_BLAST_TUGAS_CHANNEL_ID)} in your response as the place where coordinators blast official briefs, captions, Drive links, and deadlines.",
        ]
        guidance = state.get("boundary_guidance")
        if guidance:
            blocks.append(f"\nPANDUAN KHUSUS UNTUK PERTANYAAN INI:\n{guidance}")
        if rules_text:
            blocks.append("\nKNOWLEDGE RULES (markdown vector rules):\n" + rules_text[:6000])
        if context:
            blocks.append("\nCONTEXT (deterministic grounded facts):\n" + context[:6000])
        if retrieved:
            blocks.append("\nRETRIEVED KNOWLEDGE (authoritative):\n" + retrieved[:6000])
        if tool_output:
            blocks.append("\nTOOL OUTPUT (authoritative):\n" + tool_output[:6000])
        return "\n".join(blocks)

    # ── public API ────────────────────────────────────────────────────────
    def run(
        self,
        question: str,
        history: Sequence[dict[str, str]] = (),
        context: DiscordAgentContext | None = None,
    ) -> ChatAnswer:
        """Run the graph and return a chat answer."""
        graph = self._build()
        ctx = context or DiscordAgentContext(
            user_id=self.user_id,
            channel_id=self.channel_id,
            actor=self.actor,
            member=self.member,
            validator=self.validator,
            provider=self.provider,
            runner=self.runner,
            session=self.session,
        )

        toolkit = self._toolkit_instance()
        if hasattr(toolkit, "last_pending_action"):
            toolkit.last_pending_action = None

        state: AgentState = {
            "question": question,
            "actor": ctx.actor or self.actor,
            "history": [dict(turn) for turn in history],
            "retrieved": [],
            "context": "",
            "tool_output": [],
            "tool_calls": [],
            "messages": [],
            "answer": "",
            "pending_action": None,
            "boundary_guidance": "",
            "blocked": False,
            "refusal": "",
            "error": "",
            "steps": 0,
        }
        thread_id = (
            f"{ctx.channel_id}:{ctx.user_id}"
            if (ctx.channel_id and ctx.user_id)
            else (ctx.actor or self.actor or "default")
        )
        run_config = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": 30,
        }
        try:
            result = graph.invoke(state, config=run_config, context=ctx)
        except Exception as exc:
            reason = _scrub_error(exc)
            logger.warning("Agent graph failed: %s", reason)
            grounded = deterministic_answer(question, self.validator, self.actor, history=history)
            grounded.error = reason
            grounded.source = "grounded fallback"
            return grounded

        text = format_discord_message(str(result.get("answer") or "").strip())
        error = str(result.get("error") or "")
        blocked = bool(result.get("blocked"))
        pending = result.get("pending_action")
        if blocked:
            grounded = deterministic_answer(question, self.validator, self.actor, history=history)
            return ChatAnswer(
                text,
                in_scope=grounded.in_scope,
                source=grounded.source,
                error=error,
                pending_action=None,
            )
        if not text:
            grounded = deterministic_answer(question, self.validator, self.actor, history=history)
            if error:
                grounded.error = error
                grounded.source = "grounded fallback"
            return grounded

        history_text = "\n".join(str(turn.get("content") or "") for turn in history)
        authoritative = "\n".join(
            [
                str(result.get("context") or ""),
                "\n".join(result.get("retrieved") or []),
                "\n".join(result.get("tool_output") or []),
                history_text,
            ]
        )
        if not result.get("boundary_guidance"):
            try:
                text = _assert_grounded_output(text, authoritative)
            except ValueError:
                grounded = deterministic_answer(question, self.validator, self.actor, history=history)
                grounded.error = "ungrounded output rejected"
                grounded.source = "grounded fallback"
                return grounded

        return ChatAnswer(
            text,
            in_scope=True,
            source=f"agent:{provider_label(self.provider)}",
            error=error,
            pending_action=pending,
        )


def _context_block(system: str) -> str:
    """Extract the authoritative context blocks from the system prompt."""
    blocks: list[str] = []
    for marker in ("CONTEXT (deterministic grounded facts):",
                   "RETRIEVED KNOWLEDGE (authoritative):",
                   "TOOL OUTPUT (authoritative):"):
        index = system.find(marker)
        if index != -1:
            blocks.append(system[index:].strip())
    return "\n\n".join(blocks)


def _append_context(context: str, outputs: Sequence[str]) -> str:
    """Append tool output to the accumulated context."""
    if not outputs:
        return context
    joined = "\n".join(outputs)
    return f"{context}\n{joined}".strip()


def answer_with_agent(
    question: str,
    validator: Any,
    actor: str = "",
    history: Sequence[dict[str, str]] = (),
    member: Any = None,
    provider: str | None = None,
    runner: Any = None,
    session: Any = None,
    channel_id: str = "",
    user_id: str = "",
    context: DiscordAgentContext | None = None,
) -> ChatAnswer:
    """Convenience wrapper: build an agent for one request and run it."""
    graph = AgentGraph(
        validator,
        actor=actor,
        member=member,
        provider=provider,
        runner=runner,
        session=session,
        channel_id=channel_id,
        user_id=user_id,
    )
    return graph.run(question, history, context=context)

