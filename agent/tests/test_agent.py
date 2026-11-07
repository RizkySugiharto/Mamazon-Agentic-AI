"""Agent tests: knowledge corpus, vector store, providers, and the graph."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src import config
from src.agent import knowledge, providers
from src.agent.graph import AgentGraph
from src.agent.vectorstore import ZvecVectorStore


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A small zvec collection built from the real corpus."""
    monkeypatch.setattr(config, "VECTOR_DB_DIR", tmp_path / "vec")
    monkeypatch.setattr(config, "VECTOR_DB_COLLECTION", "kb")
    monkeypatch.setattr(config, "EMBEDDING_DIM", 384)
    return knowledge.build_store(force=True)


class TestKnowledgeCorpus:
    def test_corpus_covers_rules_commands_and_allowed_info(self):
        docs = knowledge.build_corpus()
        kinds = {doc["metadata"]["kind"] for doc in docs}
        assert kinds == {"rule", "command", "info"}
        ids = {doc["metadata"]["id"] for doc in docs}
        assert "check:C4" in ids
        assert "command:validate" in ids
        assert "info:chat_boundaries" in ids

    def test_rule_documents_carry_amounts_from_rulebase(self):
        docs = knowledge.build_corpus()
        caption = next(d for d in docs if d["metadata"]["id"] == "check:C4")
        assert "Rp 5.000" in caption["page_content"]

    def test_command_documents_state_role_gate(self):
        docs = knowledge.build_corpus()
        validate = next(d for d in docs if d["metadata"]["id"] == "command:validate")
        assert "Role yang boleh menjalankan" in validate["page_content"]

    def test_allowed_info_states_chat_cannot_mutate(self):
        docs = knowledge.build_corpus()
        boundary = next(d for d in docs if d["metadata"]["id"] == "info:chat_boundaries")
        assert "tidak bisa memvalidasi" in boundary["page_content"]
        assert "konfirmasi" in boundary["page_content"]

    def test_corpus_never_writes_the_rulebase(self):
        before = {p: p.read_bytes() for p in config.RULES_DIR.glob("*.md")}
        knowledge.build_corpus()
        assert {p: p.read_bytes() for p in config.RULES_DIR.glob("*.md")} == before


class TestVectorStore:
    def test_hybrid_search_finds_the_right_rule(self, store):
        results = knowledge.search("berapa denda kalau caption salah", store=store)
        assert results
        assert results[0][0].metadata["id"] == "check:C4"

    def test_search_can_filter_by_kind(self, store):
        results = knowledge.search("validasi", store=store, kinds=["command"])
        assert all(doc.metadata["kind"] == "command" for doc, _ in results)

    def test_off_topic_question_is_filtered_out(self, store):
        results = knowledge.search("cara install python di windows", store=store)
        assert results == []

    def test_documents_keep_their_metadata(self, store):
        results = knowledge.search("angel card", store=store)
        assert results
        doc = results[0][0]
        assert doc.metadata["kind"]
        assert doc.metadata["source"]
        assert doc.page_content

    def test_index_blast_task_and_search(self, store):
        from src.models import BlastContext
        ctx = BlastContext(
            task_id="PP-99",
            caption_referensi="Contoh caption sponsor Horizon",
            tags=["@horizon_umn", "@sponsor_keren"],
            hashtags=["#PPHorizon", "#SponsorTest"],
            short_description="Tugas PP-99 (Story & Feed) • Target/Sponsor: @sponsor_keren • Deadline: 30 Nov 2026",
            search_tags=["#PP-99", "#PP99", "#PaidPromote", "#Story", "#Feed", "#sponsor_keren", "#PPHorizon"],
        )
        doc_id = knowledge.index_blast_task(ctx, store=store)
        assert doc_id == "blast:PP-99"

        hits = knowledge.search("tugas PP-99", store=store)
        assert hits
        assert any(d.metadata.get("task_id") == "PP-99" for d, _ in hits)

        hits_tag = knowledge.search("sponsor_keren", store=store)
        assert hits_tag
        assert any(d.metadata.get("task_id") == "PP-99" for d, _ in hits_tag)


class TestProviders:
    def test_model_uses_the_configured_gateway_and_model(self, monkeypatch):
        monkeypatch.setattr(config, "NINEROUTER_API_KEY", "test-key")
        monkeypatch.setattr(config, "NINEROUTER_MODEL", "mamazon")
        monkeypatch.setattr(config, "NINEROUTER_BASE_URL", "http://127.0.0.1:20128/v1")
        model = providers.build_chat_model()
        assert model.model_name == "mamazon"
        assert str(model.openai_api_base) == "http://127.0.0.1:20128/v1"

    def test_model_requires_an_api_key(self, monkeypatch):
        monkeypatch.setattr(config, "NINEROUTER_API_KEY", "")
        with pytest.raises(RuntimeError, match="NINEROUTER_API_KEY"):
            providers.build_chat_model()

    def test_model_requires_a_model_id(self, monkeypatch):
        monkeypatch.setattr(config, "NINEROUTER_API_KEY", "test-key")
        monkeypatch.setattr(config, "NINEROUTER_MODEL", "")
        with pytest.raises(RuntimeError, match="NINEROUTER_MODEL"):
            providers.build_chat_model()

    def test_unknown_provider_is_rejected(self, monkeypatch):
        with pytest.raises(RuntimeError, match="Unsupported AI provider"):
            providers.build_chat_model("openrouter")

    def test_native_tool_calls_are_supported(self):
        assert providers.supports_native_tools("9router")
        assert not providers.supports_native_tools("opencode")

    def test_provider_label_uses_the_configured_model(self, monkeypatch):
        monkeypatch.setattr(config, "NINEROUTER_MODEL", "mamazon")
        assert providers.provider_label() == "9router:mamazon"



class _FakeModel:
    """Minimal chat model that replays scripted responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def invoke(self, messages, **kwargs):
        self.calls.append(messages)
        item = self.responses.pop(0) if self.responses else SimpleNamespace(
            content="Jawaban akhir.", tool_calls=[]
        )
        if isinstance(item, str):
            return SimpleNamespace(content=item, tool_calls=[])
        return item

    def bind_tools(self, tools, **kwargs):
        return self


class TestGraph:
    def test_refuses_rule_changes_without_calling_the_model(self, validator, monkeypatch):
        def forbidden(*args, **kwargs):
            raise AssertionError("a refusal must not call the model")

        graph = AgentGraph(validator, provider="openrouter")
        monkeypatch.setattr("src.agent.graph.build_chat_model", lambda *a, **k: forbidden())
        result = graph.run("tambah aturan baru dong")
        assert "tidak dapat dilakukan" in result.text.lower() or "rulebase" in result.text

    def test_refuses_out_of_scope_without_calling_the_model(self, validator, monkeypatch):
        def forbidden(*args, **kwargs):
            raise AssertionError("an out-of-scope reply must not call the model")

        graph = AgentGraph(validator, provider="openrouter")
        monkeypatch.setattr("src.agent.graph.build_chat_model", lambda *a, **k: forbidden())
        result = graph.run("buatkan resep nasi goreng")
        assert not result.in_scope

    def test_model_failure_falls_back_to_the_deterministic_answer(self, validator, monkeypatch):
        def broken(*args, **kwargs):
            raise RuntimeError("upstream is down")

        graph = AgentGraph(validator, provider="openrouter")
        monkeypatch.setattr("src.agent.graph.build_chat_model", lambda *a, **k: broken())
        result = graph.run("denda caption")
        assert "C4" in result.text
        assert result.source == "grounded fallback"

    def test_invented_amount_is_rejected(self, validator, monkeypatch):
        model = _FakeModel([SimpleNamespace(content="Dendanya Rp 999.000", tool_calls=[])])
        monkeypatch.setattr("src.agent.graph.build_chat_model", lambda *a, **k: model)
        graph = AgentGraph(validator, provider="openrouter")
        result = graph.run("denda caption")
        assert "Rp 999.000" not in result.text

    def test_tool_call_is_executed_and_used_as_context(self, validator, monkeypatch):
        first = SimpleNamespace(
            content="",
            tool_calls=[
                {"name": "get_task_requirements", "args": {"task_id": "PP-03"}, "id": "1"}
            ],
        )
        second = SimpleNamespace(
            content="Tugas PP-03 punya deadline tersendiri.",
            tool_calls=[],
        )
        model = _FakeModel([first, second])
        monkeypatch.setattr("src.agent.graph.build_chat_model", lambda *a, **k: model)
        graph = AgentGraph(validator, provider="openrouter")
        result = graph.run("ketentuan PP-03")
        assert "PP-03" in result.text
        # The tool output must reach the model on the next turn.
        assert len(model.calls) == 2

    def test_grounded_answer_survives_the_grounding_check(self, validator, monkeypatch):
        model = _FakeModel([SimpleNamespace(content="Denda caption salah Rp 5.000.", tool_calls=[])])
        monkeypatch.setattr("src.agent.graph.build_chat_model", lambda *a, **k: model)
        graph = AgentGraph(validator, provider="openrouter")
        result = graph.run("denda caption")
        assert "Rp 5.000" in result.text
        assert result.source.startswith("agent:")

    def test_system_prompt_mandates_tugas_channel_mention(self, validator, monkeypatch):
        monkeypatch.setattr(config, "DISCORD_BLAST_TUGAS_CHANNEL_ID", "112233")
        graph = AgentGraph(validator, provider="openrouter")
        prompt = graph._system_prompt({"question": "ketentuan PP-03"})
        assert "MANDATORY CHANNEL MENTION" in prompt
        assert "<#112233>" in prompt

    def test_context_schema_with_discord_context(self, validator, monkeypatch):
        from src.agent.graph import DiscordAgentContext
        model = _FakeModel([SimpleNamespace(content="Halo panitia!", tool_calls=[])])
        monkeypatch.setattr("src.agent.graph.build_chat_model", lambda *a, **k: model)
        graph = AgentGraph(validator, provider="openrouter")
        ctx = DiscordAgentContext(
            user_id="998877",
            channel_id="554433",
            actor="TestUser",
            validator=validator,
        )
        result = graph.run("halo bot", context=ctx)
        assert result.text
        assert "Saya bisa bantu soal denda, ketentuan PP/Buzzer" not in result.text

    def test_agent_command_planning_sets_pending_action(self, validator, monkeypatch):
        from types import SimpleNamespace
        member = SimpleNamespace(id=1, roles=[SimpleNamespace(name="spade")])
        call = SimpleNamespace(
            content="",
            tool_calls=[
                {
                    "name": "plan_validate_sheet",
                    "args": {"sheet": "PP 29 Oktober", "divisi": "Spade"},
                    "id": "call_1",
                }
            ],
        )
        second = SimpleNamespace(
            content="Rencana validasi disiapkan, silakan klik konfirmasi.",
            tool_calls=[],
        )
        model = _FakeModel([call, second])
        monkeypatch.setattr("src.agent.graph.build_chat_model", lambda *a, **k: model)
        graph = AgentGraph(validator, member=member, provider="openrouter")
        result = graph.run("tolong validasi sheet PP 29 Oktober divisi Spade")
        assert result.pending_action is not None
        assert result.pending_action["command"] == "validate"
        assert result.pending_action["params"]["sheet"] == "PP 29 Oktober"

    def test_ambiguous_sheet_asks_user_to_confirm(self, validator, monkeypatch):
        from src.chat_agent import AgentToolkit
        from types import SimpleNamespace
        member = SimpleNamespace(id=1, roles=[SimpleNamespace(name="spade")])
        toolkit = AgentToolkit(validator, member=member)
        res = toolkit.plan_command("validate", {"sheet": "PP"})
        assert "ambigu" in res.lower()
        assert "•" in res

    def test_discord_table_conversion(self):
        from src.utils import format_discord_message
        table_md = (
            "Berikut rekap:\n"
            "| Nama | Divisi | Denda |\n"
            "| --- | --- | --- |\n"
            "| Budi | Spade | Rp 20.000 |\n"
            "| Ani | Acara | Rp 5.000 |\n"
        )
        formatted = format_discord_message(table_md)
        assert "|" not in formatted
        assert "• **Budi**" in formatted
        assert "Divisi: Spade" in formatted
        assert "Denda: Rp 20.000" in formatted

