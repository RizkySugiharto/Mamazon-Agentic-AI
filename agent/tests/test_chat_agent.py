"""#ai-chat responder tests: scope guard, rule-change refusal, grounded answers."""

from __future__ import annotations

import json

import pytest
from types import SimpleNamespace

from src.chat_agent import answer, conversational_answer, in_scope
from src.data_layer import DataLayer
from src.sheets_client import XlsxSource
from src import config
from src.output_generator import OutputGenerator
from src.validator import Validator

pytestmark = pytest.mark.usefixtures("dataset_available")


@pytest.fixture(autouse=True)
def default_chat_provider(monkeypatch):
    """Keep the unified provider selection hermetic regardless of the developer's .env."""
    monkeypatch.setattr(config, "AI_PROVIDER", "9router")


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
        assert "Channel tugas:" in result.text
        assert "blast-tugas" in result.text or "<#" in result.text

    def test_generic_ketentuan_mentions_channel(self, validator):
        result_pp = answer("apa ketentuan tugas pp?", validator)
        assert "Ketentuan Tugas PP" in result_pp.text
        assert "blast-tugas" in result_pp.text or "<#" in result_pp.text

        result_buzzer = answer("aturan buzzer gimana?", validator)
        assert "Ketentuan Tugas BUZZER" in result_buzzer.text
        assert "blast-tugas" in result_buzzer.text or "<#" in result_buzzer.text

    def test_toolkit_ketentuan_mentions_channel(self, validator):
        from src.chat_agent import AgentToolkit
        from types import SimpleNamespace
        member = SimpleNamespace(id=1, roles=[SimpleNamespace(name="spade")])
        toolkit = AgentToolkit(validator, member=member)
        res = toolkit._ketentuan("PP-03")
        assert "Channel tugas:" in res.text
        assert "blast-tugas" in res.text or "<#" in res.text

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

    def test_ninerouter_receives_grounded_chat_history(self, validator, monkeypatch):
        import json
        from src.chat_agent import conversational_answer
        from src.vision_checker import _Response
        from types import SimpleNamespace
        calls = []
        def post(url, **kwargs):
            calls.append((url, kwargs))
            return _Response(200, json.dumps({"choices": [{"message": {"content": "Hai! Mau bahas tugas yang mana?"}}]}))
        monkeypatch.setattr(config, "CHAT_ENABLED", True)
        monkeypatch.setattr(config, "NINEROUTER_MODEL", "mamazon")
        monkeypatch.setattr(config, "NINEROUTER_API_KEY", "test-key")
        result = conversational_answer("hi", validator, history=[{"role": "user", "content": "halo"}], session=SimpleNamespace(post=post))
        assert result.source.startswith("9router")
        body = calls[0][1]["json"]
        assert body["model"] == "mamazon"
        assert "NO action tools" in body["messages"][0]["content"]
        assert body["messages"][1]["content"] == "halo"
        assert "response_format" not in body

    def test_model_failure_has_grounded_fallback(self, validator, monkeypatch):
        from src.chat_agent import conversational_answer
        from src.vision_checker import _Response
        from types import SimpleNamespace
        monkeypatch.setattr(config, "CHAT_ENABLED", True)
        monkeypatch.setattr(config, "NINEROUTER_MODEL", "test-model")
        monkeypatch.setattr(config, "NINEROUTER_API_KEY", "test-key")
        result = conversational_answer("denda caption", validator, session=SimpleNamespace(post=lambda *a, **kw: _Response(429, "quota")))
        assert "C4" in result.text and "Rp 5.000" in result.text
        assert "tidak tersedia" in result.text

    def test_invented_amount_is_rejected(self, validator, monkeypatch):
        import json
        from src.chat_agent import conversational_answer
        from src.vision_checker import _Response
        from types import SimpleNamespace
        monkeypatch.setattr(config, "CHAT_ENABLED", True)
        monkeypatch.setattr(config, "NINEROUTER_MODEL", "test-model")
        monkeypatch.setattr(config, "NINEROUTER_API_KEY", "test-key")
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
        def forbidden(*args, **kwargs):
            raise AssertionError("No model calls for refusals")
        result = conversational_answer("buatkan resep nasi goreng", validator, session=SimpleNamespace(post=forbidden))
        assert not result.in_scope


class TestUnifiedProvider:
    def test_chat_disabled_uses_grounded_facts_without_any_model(self, validator, monkeypatch):
        monkeypatch.setattr(config, "AI_PROVIDER", "9router")
        monkeypatch.setattr(config, "CHAT_ENABLED", False)

        def forbidden(*args, **kwargs):
            raise AssertionError("disabled chat must not call any model")

        result = conversational_answer(
            "denda caption", validator,
            session=SimpleNamespace(post=forbidden), runner=forbidden,
        )
        assert "C4" in result.text and "tidak tersedia" not in result.text

    def test_mock_provider_uses_grounded_facts_without_any_model(self, validator, monkeypatch):
        monkeypatch.setattr(config, "AI_PROVIDER", "mock")
        monkeypatch.setattr(config, "CHAT_ENABLED", True)

        def forbidden(*args, **kwargs):
            raise AssertionError("mock must not call any model")

        result = conversational_answer(
            "denda caption", validator,
            session=SimpleNamespace(post=forbidden), runner=forbidden,
        )
        assert "C4" in result.text and "tidak tersedia" not in result.text

    @pytest.mark.parametrize("removed", ["tencent", "agy"])
    def test_removed_provider_falls_back_without_any_model(self, validator, monkeypatch, removed):
        monkeypatch.setattr(config, "AI_PROVIDER", removed)
        monkeypatch.setattr(config, "CHAT_ENABLED", True)

        def forbidden(*args, **kwargs):
            raise AssertionError("removed provider must not call any model")

        result = conversational_answer(
            "denda caption", validator,
            session=SimpleNamespace(post=forbidden), runner=forbidden,
        )
        assert result.source == "grounded fallback"
        assert "C4" in result.text and "Rp 5.000" in result.text

    @pytest.mark.parametrize("status", [429, 500])
    def test_provider_http_failure_uses_grounded_fallback(self, validator, monkeypatch, status):
        """The gateway transport failing must never invent facts: grounded fallback wins."""
        from src.chat_agent import conversational_answer
        from src.vision_checker import _Response

        monkeypatch.setattr(config, "CHAT_ENABLED", True)
        monkeypatch.setattr(config, "NINEROUTER_MODEL", "test-model")
        monkeypatch.setattr(config, "NINEROUTER_API_KEY", "test-key")

        def forbidden(*args, **kwargs):
            raise AssertionError("provider failure must not call any CLI runner")

        response = _Response(status, "upstream error")
        result = conversational_answer(
            "denda caption", validator,
            session=SimpleNamespace(post=lambda *a, **kw: response),
            runner=forbidden,
        )
        assert result.source == "grounded fallback"
        assert "C4" in result.text and "Rp 5.000" in result.text


class TestSanitizedDiagnostics:
    def test_scrub_error_redacts_bearer_and_prefixed_keys(self):
        from src.chat_agent import _scrub_error

        scrubbed = _scrub_error(
            "auth failed: Authorization Bearer sk-or-v1-ABCDEFGH12345678 "
            "and oc_sk_ZYXWVUTS98765432 and sk-abcdefgh1234"
        )
        assert "Bearer [redacted]" in scrubbed
        assert "sk-or-v1-ABCDEFGH12345678" not in scrubbed
        assert "oc_sk_ZYXWVUTS98765432" not in scrubbed
        assert "sk-abcdefgh1234" not in scrubbed
        assert "[redacted-key]" in scrubbed

    def test_scrub_error_redacts_key_token_secret_password_assignments(self):
        from src.chat_agent import _scrub_error

        scrubbed = _scrub_error(
            "api_key: 'value with spaces' password=hunter2 token=abcdef123456 "
            "secret: shhh credential=pwd123"
        )
        for secret in ("value with spaces", "hunter2", "abcdef123456", "shhh", "pwd123"):
            assert secret not in scrubbed
        assert "[redacted]" in scrubbed

    def test_scrub_error_redacts_url_query_strings(self):
        from src.chat_agent import _scrub_error

        scrubbed = _scrub_error(
            "request to https://api.example.com/v1?token=abc&secret=xyz failed"
        )
        assert "token=abc" not in scrubbed
        assert "secret=xyz" not in scrubbed
        assert "https://api.example.com/v1?[redacted]" in scrubbed

    def test_provider_failure_sets_sanitized_error_and_leaks_no_secret(self, validator, monkeypatch):
        from src.chat_agent import conversational_answer

        secret = "sk-super-secret-value-12345678"
        fake_exc = RuntimeError(
            f"upstream failed: Authorization=Bearer {secret} password=hunter2 "
            "see https://api.example/v1?token=abc"
        )
        monkeypatch.setattr(config, "AI_PROVIDER", "9router")
        monkeypatch.setattr(config, "CHAT_ENABLED", True)
        monkeypatch.setattr(config, "CHAT_TOOLS_ENABLED", False)
        monkeypatch.setattr(config, "NINEROUTER_API_KEY", "test-key")
        monkeypatch.setattr(config, "NINEROUTER_MODEL", "test-model")

        def boom(*args, **kwargs):
            raise fake_exc

        result = conversational_answer(
            "denda caption", validator,
            session=SimpleNamespace(post=boom), provider="9router",
        )
        assert result.source == "grounded fallback"
        assert result.error
        assert secret not in result.text
        assert secret not in result.error
        assert "hunter2" not in result.text
        assert "token=abc" not in result.text
        assert "C4" in result.text and "Rp 5.000" in result.text


class TestAgentToolkit:
    @pytest.fixture(autouse=True)
    def _tools(self, monkeypatch, tmp_path):
        # Explicit tools are deterministic: keep every model out of these tests.
        monkeypatch.setattr(config, "CHAT_TOOLS_ENABLED", True)
        monkeypatch.setattr(config, "CHAT_MEMORY_ENABLED", True)
        monkeypatch.setattr(config, "CHAT_MEMORY_PATH", tmp_path / "chat_memory.json")
        monkeypatch.setattr(config, "CHAT_MEMORY_MAX_ITEMS", 20)
        monkeypatch.setattr(config, "CHAT_ENABLED", False)
        # Hermetic, fixed role matrix.
        monkeypatch.setattr(config, "ROLE_BPH", "BPH")
        monkeypatch.setattr(config, "ROLE_KOOR", "koor-spade")
        monkeypatch.setattr(config, "ROLE_SPADE", "spade")
        monkeypatch.setattr(config, "ROLE_PANITIA", "panitia")
        monkeypatch.setattr(config, "PERMISSIONS", {
            "validate": ("BPH", "koor-spade", "spade"),
            "check-angel": ("BPH", "koor-spade", "spade"),
            "add-angel": ("BPH", "koor-spade"),
            "remove-angel": ("BPH", "koor-spade"),
            "blast": ("BPH", "koor-spade"),
            "hold-blast": ("BPH", "koor-spade"),
            "release-blast": ("BPH", "koor-spade"),
            "list-active": ("BPH", "koor-spade", "spade"),
            "resubmit-status": ("BPH", "koor-spade", "spade"),
            "audit-log": ("BPH", "koor-spade"),
            "denda-list": ("BPH", "koor-spade", "spade", "panitia"),
            "ketentuan-pp": ("BPH", "koor-spade", "spade", "panitia"),
            "ketentuan-buzzer": ("BPH", "koor-spade", "spade", "panitia"),
            "ketentuan-angel-card": ("BPH", "koor-spade", "spade", "panitia"),
            "scan-blast": ("BPH", "koor-spade"),
            "list-sheets": ("BPH", "koor-spade", "spade"),
            "roster-set-discord": ("BPH", "koor-spade"),
            "roster-remove-discord": ("BPH", "koor-spade"),
        })

    @staticmethod
    def _member(role: str, member_id: int = 1):
        return SimpleNamespace(
            id=member_id,
            roles=[SimpleNamespace(name=role)],
            guild_permissions=None,
        )

    def test_long_term_memory_ingat_recall_forget(self, validator, tmp_path):
        from src.chat_agent import conversational_answer

        member = self._member("spade", 777)
        stored = conversational_answer("ingat divisi aku Spade", validator, member=member)
        assert "Tersimpan" in stored.text
        assert "divisi aku Spade" in stored.text
        assert (tmp_path / "chat_memory.json").exists()

        recalled = conversational_answer("apa yang kamu ingat", validator, member=member)
        assert "divisi aku Spade" in recalled.text

        forgotten = conversational_answer("lupa semua", validator, member=member)
        assert "dihapus" in forgotten.text

        after = conversational_answer("apa yang kamu ingat", validator, member=member)
        assert "belum punya catatan" in after.text

    def test_long_term_memory_refuses_secret_like_text(self, validator):
        from src.chat_agent import conversational_answer

        member = self._member("spade", 777)
        for text in (
            "ingat password rahasia123",
            "ingat token sk-abcdefgh1234",
            "ingat api_key: super-secret",
            "catat Bearer sk-abcdefgh12345678",
        ):
            result = conversational_answer(text, validator, member=member)
            assert "tidak menyimpan" in result.text.lower()
            assert "Tersimpan" not in result.text

        recalled = conversational_answer("apa yang kamu ingat", validator, member=member)
        assert "belum punya catatan" in recalled.text

    def test_safe_calculator(self):
        from src.chat_agent import conversational_answer

        result = conversational_answer("hitung 2 + 3 * 4", SimpleNamespace())
        assert "14" in result.text

    def test_safe_calculator_rejects_code_execution(self):
        from src.chat_agent import conversational_answer

        result = conversational_answer(
            "hitung __import__('os').system('echo hi')", SimpleNamespace()
        )
        assert "echo hi" not in result.text
        assert "Maaf" in result.text
        assert "14" not in result.text

    def test_safe_calculator_rejects_exponent_names_and_strings(self):
        from src.chat_agent import conversational_answer

        for expression in ("2 ** 10", "os.getcwd()", "' texto '", "2 + 'abc'"):
            result = conversational_answer(f"hitung {expression}", SimpleNamespace())
            assert "Maaf" in result.text or "diizinkan" in result.text

    def test_status_ai_reports_provider_without_secrets(self, validator, monkeypatch):
        from src.chat_agent import conversational_answer

        monkeypatch.setattr(config, "AI_PROVIDER", "9router")
        monkeypatch.setattr(config, "NINEROUTER_API_KEY", "sk-secret-key-abcdefgh")
        monkeypatch.setattr(config, "NINEROUTER_MODEL", "mamazon")
        monkeypatch.setattr(config, "NINEROUTER_BASE_URL", "http://127.0.0.1:20128/v1")
        monkeypatch.setattr(config, "NINEROUTER_USER_AGENT", "MamazonTest/1.0")
        monkeypatch.setattr(config, "WEB_SEARCH_URL", "")

        result = conversational_answer("status ai", validator)
        assert "Status AI" in result.text
        assert "9router" in result.text
        assert "mamazon" in result.text
        assert "sk-secret-key-abcdefgh" not in result.text
        assert "Tools aktif" in result.text
        assert "Memori aktif" in result.text

    def test_role_gated_denda_list(self, monkeypatch):
        from src.chat_agent import conversational_answer

        rows = [
            {"nama_komitee": "Member A", "sheet_name": "PP 29 Oktober", "id_tugas": "PP-01",
             "rule_id": "C4", "jenis_pelanggaran": "Caption salah", "denda": "5000", "status": "FLAGGED"},
            {"nama_komitee": "Member B", "sheet_name": "PP 07 Oktober", "id_tugas": "PP-02",
             "rule_id": "C6", "jenis_pelanggaran": "Hashtag kurang", "denda": "5000", "status": "FLAGGED"},
        ]
        calls = []

        def denda_rows(name=None):
            calls.append(name)
            return [row for row in rows if name is None or row["nama_komitee"] == name]

        roster = [
            SimpleNamespace(nama="Member A", divisi="Spade"),
            SimpleNamespace(nama="Member B", divisi="Spade"),
        ]
        fake = SimpleNamespace(
            engine=SimpleNamespace(
                check_meta=lambda check_id: {"nama": check_id, "denda": 5000, "syarat": "-"},
                task=lambda task_id: None,
            ),
            data=SimpleNamespace(
                load_roster=lambda: roster,
                find_person=lambda name: (None, None),
            ),
            outputs=SimpleNamespace(denda_rows=denda_rows, audit=lambda **kwargs: None),
        )

        # A member with the validate role sees the full, unfiltered list.
        validate_member = self._member("spade", 1)
        all_rows = conversational_answer("daftar denda", fake, member=validate_member)
        assert "(semua panitia)" in all_rows.text
        assert "2 item" in all_rows.text
        assert "PP 29 Oktober" in all_rows.text and "PP 07 Oktober" in all_rows.text
        assert calls == [None]

        calls.clear()
        panitia = self._member("panitia", 2)
        monkeypatch.setattr(config, "DISCORD_MEMBER_MAP", {})
        unbound = conversational_answer("daftar denda", fake, member=panitia)
        assert "🔒" in unbound.text
        assert "DISCORD_MEMBER_MAP" in unbound.text
        assert calls == []

        monkeypatch.setattr(config, "DISCORD_MEMBER_MAP", {"2": "Member A"})
        bound = conversational_answer("daftar denda", fake, member=panitia)
        assert "🔒" not in bound.text
        assert "Member A" in bound.text
        assert "1 item" in bound.text
        assert calls == ["Member A"]

    def test_angel_registry_and_sheet_detail_require_privileged_role(self, monkeypatch):
        from unittest.mock import Mock
        from src.chat_agent import conversational_answer

        data = SimpleNamespace(
            load_angel_registry=Mock(side_effect=AssertionError("must not read registry")),
            describe_sheets=Mock(side_effect=AssertionError("must not read sheets")),
            resolve_sheet=Mock(side_effect=AssertionError("must not resolve sheet")),
            load_roster=Mock(side_effect=AssertionError("must not read roster")),
        )
        fake = SimpleNamespace(
            engine=SimpleNamespace(check_meta=lambda c: None, task=lambda t: None),
            data=data,
            outputs=SimpleNamespace(denda_rows=Mock(), audit=lambda **kwargs: None),
        )
        panitia = self._member("panitia", 9)
        for question in ("daftar angel card", "detail sheet PP 29 Oktober", "daftar sheet"):
            result = conversational_answer(question, fake, member=panitia)
            assert "🔒" in result.text
            assert "hanya" in result.text.lower()
        data.load_angel_registry.assert_not_called()
        data.describe_sheets.assert_not_called()
        data.resolve_sheet.assert_not_called()

        # A privileged role is allowed to read the same data (fake, no Sheets).
        angel_entry = SimpleNamespace(
            active=True, nama="Angel One", divisi="Spade", tanggal_beli=None
        )
        sheet_info = SimpleNamespace(
            name="PP 29 Oktober", rows=3, task_id="PP-01", task_source="rulebase",
            multi_link=0, no_link=0,
        )
        source = SimpleNamespace(
            freshness_warnings=[],
            rows=lambda sheet: [["Nama", "Link"], ["Budi", "http://x"]],
        )
        good = SimpleNamespace(
            engine=SimpleNamespace(check_meta=lambda c: None, task=lambda t: None),
            data=SimpleNamespace(
                load_angel_registry=lambda: [angel_entry],
                describe_sheets=lambda: [sheet_info],
                resolve_sheet=lambda name: SimpleNamespace(matches=[("PP 29 Oktober", 1.0)]),
                source=source,
                submission_sheets=lambda: ["PP 29 Oktober"],
            ),
            outputs=SimpleNamespace(denda_rows=Mock(), audit=lambda **kwargs: None),
        )
        privileged = self._member("spade", 10)
        angels = conversational_answer("daftar angel card", good, member=privileged)
        assert "Angel One" in angels.text and "🔒" not in angels.text
        detail = conversational_answer("detail sheet PP 29 Oktober", good, member=privileged)
        assert "PP 29 Oktober" in detail.text
        assert "Header" in detail.text and "🔒" not in detail.text

    def test_confirmed_validate_action_returns_pending_without_calling_provider(self, validator):
        from src.chat_agent import conversational_answer

        def forbidden(*args, **kwargs):
            raise AssertionError("planning must not call the provider/session/runner")

        privileged = self._member("bph", 11)
        result = conversational_answer(
            "/validate sheet:PP 29 Oktober divisi:Spade", validator,
            session=SimpleNamespace(post=forbidden), runner=forbidden,
            member=privileged,
        )
        assert result.pending_action is not None
        pending = result.pending_action
        assert pending["command"] == "validate"
        assert pending["params"]["sheet"] == "PP 29 Oktober"
        assert pending["params"]["divisi"] == "Spade"
        assert pending["params"]["force"] is False
        assert pending["permission"] == "validate"
        assert pending["mutating"] is True
        assert "belum dijalankan" in result.text

        unprivileged = self._member("panitia", 12)
        denied = conversational_answer(
            "/validate sheet:PP 29 Oktober divisi:Spade", validator,
            member=unprivileged,
        )
        assert denied.pending_action is None
        assert "🔒" in denied.text

    def test_blast_pending_action_description_mentions_draft_and_koor_approval(self, validator):
        from src.chat_agent import conversational_answer

        privileged = self._member("bph", 13)
        result = conversational_answer(
            "/blast sheet:PP 29 Oktober", validator, member=privileged
        )
        assert result.pending_action is not None
        assert result.pending_action["command"] == "blast"
        assert result.pending_action["mutating"] is True
        description = result.pending_action["description"].lower()
        assert "draft" in description and "approval koor" in description
        assert "draft" in result.text.lower()
        assert "approval koor" in result.text.lower()

    def test_conversational_answer_without_member_handles_greetings_and_leaks_nothing(self, validator):
        from src.chat_agent import conversational_answer

        greeting = conversational_answer("halo", validator, member=None)
        assert greeting.in_scope
        assert "Hai" in greeting.text or "Mamazon" in greeting.text
        assert conversational_answer("halo", validator).text  # keyword-only default

        denied = conversational_answer("daftar denda", validator, member=None)
        assert "🔒" in denied.text
        assert "hanya" in denied.text.lower()

    def test_roster_set_and_remove_discord_planning(self, validator):
        from src.chat_agent import conversational_answer

        koor = self._member("koor-spade", 14)
        slash_set = conversational_answer(
            "/roster-set-discord nama:Budi Hartono discord_id:1234567890", validator, member=koor
        )
        assert slash_set.pending_action is not None
        assert slash_set.pending_action["command"] == "roster-set-discord"
        assert slash_set.pending_action["params"]["nama"] == "Budi Hartono"
        assert slash_set.pending_action["params"]["discord_id"] == "1234567890"

        nl_set = conversational_answer("set discord Budi Hartono 999888777", validator, member=koor)
        assert nl_set.pending_action is not None
        assert nl_set.pending_action["command"] == "roster-set-discord"
        assert nl_set.pending_action["params"]["nama"] == "Budi Hartono"
        assert nl_set.pending_action["params"]["discord_id"] == "999888777"

        slash_remove = conversational_answer("/roster-remove-discord nama:Budi Hartono", validator, member=koor)
        assert slash_remove.pending_action is not None
        assert slash_remove.pending_action["command"] == "roster-remove-discord"
        assert slash_remove.pending_action["params"]["nama"] == "Budi Hartono"

        nl_remove = conversational_answer("hapus discord Budi Hartono", validator, member=koor)
        assert nl_remove.pending_action is not None
        assert nl_remove.pending_action["command"] == "roster-remove-discord"
        assert nl_remove.pending_action["params"]["nama"] == "Budi Hartono"

        unprivileged = self._member("panitia", 15)
        denied = conversational_answer("/roster-set-discord nama:Budi discord_id:123", validator, member=unprivileged)
        assert denied.pending_action is None
        assert "🔒" in denied.text

    def test_panitia_denda_resolved_via_roster_column_e(self, monkeypatch):
        from src.chat_agent import conversational_answer

        panitia = self._member("panitia", 777)
        monkeypatch.setattr(config, "DISCORD_MEMBER_MAP", {})

        roster_entry = SimpleNamespace(nama="Panitia Tujuh", divisi="Spade", discord_id="777")
        fake_data = SimpleNamespace(
            resolve_discord_user=lambda uid: roster_entry if str(uid) == "777" else None,
            load_roster=lambda: [roster_entry],
            find_person=lambda q: (roster_entry, 1.0),
        )
        fake_validator = SimpleNamespace(
            engine=SimpleNamespace(check_meta=lambda c: None, task=lambda t: None),
            data=fake_data,
            outputs=SimpleNamespace(
                denda_rows=lambda name: [
                    {"nama_komitee": "Panitia Tujuh", "denda": 5000, "sheet_name": "PP Test", "id_tugas": "PP-01"}
                ],
                audit=lambda **kw: None,
            ),
        )

        ans = conversational_answer("daftar denda", fake_validator, member=panitia)
        assert "🔒" not in ans.text
        assert "Panitia Tujuh" in ans.text
        assert "Rp 5.000" in ans.text
