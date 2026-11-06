"""#ai-chat responder tests: scope guard, rule-change refusal, grounded answers."""

from __future__ import annotations

import pytest

from src.chat_agent import answer, in_scope
from src.data_layer import DataLayer
from src.sheets_client import XlsxSource
from src import config
from src.output_generator import OutputGenerator
from src.validator import Validator
from src.vision_checker import MockVision

pytestmark = pytest.mark.usefixtures("dataset_available")


@pytest.fixture()
def validator(rulebase, outputs, images) -> Validator:
    data = DataLayer(rulebase=rulebase, source=XlsxSource(config.DUMMY_DATA_PATH))
    instance = Validator(
        data_layer=data, vision=MockVision(), images=images, outputs=outputs, blast_context={}
    )
    yield instance
    data.close()


class TestScope:
    def test_spade_topics_in_scope(self):
        assert in_scope("berapa denda kalau caption salah?")
        assert in_scope("status PP-03 gimana?")

    def test_off_topic_out_of_scope(self):
        assert not in_scope("bagaimana cara masak nasi goreng?")

    def test_refusal_message(self, validator):
        result = answer("bagaimana cara masak nasi goreng?", validator)
        assert result.in_scope is False
        assert "management SPADE" in result.text


class TestAnswers:
    def test_caption_denda(self, validator):
        result = answer("berapa denda kalau caption salah?", validator)
        assert result.in_scope
        assert "C4" in result.text
        assert "Rp 5.000" in result.text
        assert "verbatim" in result.text.lower()

    def test_hashtag_denda(self, validator):
        result = answer("denda hashtag kurang berapa?", validator)
        assert "C6" in result.text

    def test_late_post_denda(self, validator):
        result = answer("denda kalau telat post?", validator)
        assert "C2" in result.text
        assert "Rp 20.000" in result.text

    def test_task_detail(self, validator):
        result = answer("ketentuan PP-03 apa?", validator)
        assert "PP-03" in result.text
        assert "horizon_umn" in result.text

    def test_unknown_task(self, validator):
        result = answer("ketentuan PP-99", validator)
        assert "tidak ditemukan" in result.text

    def test_angel_card_rules(self, validator):
        result = answer("angel card berlaku untuk apa?", validator)
        assert "Buzzer" in result.text
        assert "Sponsor" in result.text

    def test_denda_summary(self, validator):
        result = answer("rekap denda dong", validator)
        assert "Rp 20.000" in result.text
        assert "BCA" in result.text

    def test_missing_submitters(self, validator):
        result = answer("siapa yang belum submit PP 29 Oktober?", validator)
        assert "42" in result.text
        assert "64" in result.text

    def test_blast_flow_explains_approval(self, validator):
        result = answer("cara blast gimana?", validator)
        assert "Approve" in result.text
        assert "TIDAK" in result.text

    def test_rule_change_refused(self, validator):
        result = answer("tolong tambahin denda baru Rp 10.000 buat yang telat reply WA", validator)
        assert "tidak dapat dilakukan via chat" in result.text
        assert "rulebase.json" in result.text

    def test_empty_question(self, validator):
        assert answer("", validator).text


class TestConversation:
    @pytest.mark.parametrize("text", ["hi", "halo bot!", "apa kabar?", "makasih", "hello", "aku capek banget"])
    def test_social_is_welcome(self, validator, text):
        result = answer(text, validator)
        assert result.in_scope
        assert "channel lain" not in result.text

    @pytest.mark.parametrize("text", ["happy birthday", "potato soup", "buatkan resep nasi goreng"])
    def test_word_boundaries_and_scope(self, validator, text):
        assert not answer(text, validator).in_scope

    def test_followup_uses_recent_topic(self, validator):
        history = [{"role": "user", "content": "berapa denda caption salah?"}]
        assert "C4" in answer("jelasin lagi dong", validator, history=history).text
        assert "topiknya" in answer("jelasin lagi", validator).text

    def test_chat_cannot_execute_actions(self, validator):
        result = answer("tolong validasi PP-03 sekarang", validator)
        assert result.source == "permissions"
        assert "/validate" in result.text

    def test_task_contains_explanations_and_weekday(self, validator):
        result = answer("ketentuan BZ-02", validator)
        assert "B3" in result.text and "Perlu diperhatiin" in result.text
        assert "Rabu" in result.text

    def test_memory_is_private_bounded_and_expires(self, monkeypatch):
        from src.chat_agent import ConversationStore
        now = [100.0]
        monkeypatch.setattr("src.chat_agent.time.monotonic", lambda: now[0])
        store = ConversationStore(ttl=10, max_sessions=2, turns=4)
        key = (1, 2, 3)
        store.remember(key, "halo", "hai")
        assert store.history((1, 2, 4)) == []
        store.remember(key, "denda caption", "C4")
        store.remember(key, "lanjut", "baik")
        assert len(store.history(key)) == 4
        copied = store.history(key)
        copied[0]["content"] = "mutated"
        assert store.history(key)[0]["content"] != "mutated"
        now[0] = 111.0
        assert store.history(key) == []
        for member in range(3):
            store.remember((1, 2, member), "hi", "hai")
        assert len(store._sessions) == 2

    def test_openrouter_receives_grounded_chat_history(self, validator, monkeypatch):
        import json
        from src.chat_agent import conversational_answer
        from src.vision_checker import _Response
        from types import SimpleNamespace
        calls = []
        def post(url, **kwargs):
            calls.append((url, kwargs))
            return _Response(200, json.dumps({"choices": [{"message": {"content": "Hai! Mau bahas tugas yang mana?"}}]}))
        monkeypatch.setattr(config, "CHAT_ENABLED", True)
        monkeypatch.setattr(config, "CHAT_MODEL", "google/gemma-4-26b-a4b-it:free")
        monkeypatch.setattr(config, "OPENROUTER_API_KEY", "test-key")
        result = conversational_answer("hi", validator, history=[{"role": "user", "content": "halo"}], session=SimpleNamespace(post=post))
        assert result.source.startswith("openrouter")
        body = calls[0][1]["json"]
        assert body["model"] == "google/gemma-4-26b-a4b-it:free"
        assert "NO action tools" in body["messages"][0]["content"]
        assert body["messages"][1]["content"] == "halo"
        assert "response_format" not in body

    def test_model_failure_has_grounded_fallback(self, validator, monkeypatch):
        from src.chat_agent import conversational_answer
        from src.vision_checker import _Response
        from types import SimpleNamespace
        monkeypatch.setattr(config, "CHAT_ENABLED", True)
        monkeypatch.setattr(config, "CHAT_MODEL", "test-model")
        monkeypatch.setattr(config, "OPENROUTER_API_KEY", "test-key")
        result = conversational_answer("denda caption", validator, session=SimpleNamespace(post=lambda *a, **kw: _Response(429, "quota")))
        assert "C4" in result.text and "Rp 5.000" in result.text
        assert "tidak tersedia" in result.text

    def test_invented_amount_is_rejected(self, validator, monkeypatch):
        import json
        from src.chat_agent import conversational_answer
        from src.vision_checker import _Response
        from types import SimpleNamespace
        monkeypatch.setattr(config, "CHAT_ENABLED", True)
        monkeypatch.setattr(config, "CHAT_MODEL", "test-model")
        monkeypatch.setattr(config, "OPENROUTER_API_KEY", "test-key")
        response = _Response(200, json.dumps({"choices": [{"message": {"content": "Dendanya Rp 999.000"}}]}))
        result = conversational_answer("denda caption", validator, session=SimpleNamespace(post=lambda *a, **kw: response))
        assert "Rp 999.000" not in result.text and "Rp 5.000" in result.text

    def test_rule_code_is_in_scope(self, validator):
        assert "B3" in answer("B3 itu apa?", validator).text

    def test_member_fines_with_task_id_do_not_return_requirements(self, engine):
        from types import SimpleNamespace
        rows = [{"nama_komitee": "Member Test", "id_tugas": "PP-03", "sheet_name": "PP Feed 26 Oktober",
                 "rule_id": "C4", "denda": "5000", "jenis_pelanggaran": "Caption salah", "status": "FLAGGED"},
                {"nama_komitee": "Member Test", "id_tugas": "PP-01", "sheet_name": "PP 29 Oktober",
                 "rule_id": "C6", "denda": "5000", "jenis_pelanggaran": "Hashtag kurang", "status": "FLAGGED"}]
        validator = SimpleNamespace(engine=engine,
            data=SimpleNamespace(load_roster=lambda: [SimpleNamespace(nama="Member Test", divisi="Spade")]),
            outputs=SimpleNamespace(denda_rows=lambda name: rows))
        result = answer("denda Member Test PP-03 kenapa?", validator)
        assert "Caption salah" in result.text and "PP Feed 26 Oktober" in result.text
        assert "Rp 5.000" in result.text and "PP 29 Oktober" not in result.text

    def test_refusal_never_calls_model(self, validator, monkeypatch):
        from src.chat_agent import conversational_answer
        from types import SimpleNamespace
        def forbidden(*args, **kwargs):
            raise AssertionError("No model calls for refusals")
        result = conversational_answer("buatkan resep nasi goreng", validator, session=SimpleNamespace(post=forbidden))
        assert not result.in_scope
