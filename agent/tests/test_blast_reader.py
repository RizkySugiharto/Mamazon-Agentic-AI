"""Blast reader tests: message parsing and context storage."""

from __future__ import annotations

from datetime import datetime

from src.blast_reader import (
    BlastContextStore,
    ProcessedMessageCache,
    looks_like_blast,
    parse_blast_message,
)

DEMO = """📋 BLAST TUGAS: PP Feed 26 Oktober 2026

Caption: Pusing dengan project akhir/TA codingmu? Coba jasa kami yuk, FREE diskusi sampai paham!

Media: https://drive.google.com/drive/folders/1EeG1JHNMHZUYbsy57egmmvmGwacv5RRl

Tags: @horizon_umn
Hashtag: #PPHorizon #PPHorizon_divisi #CaseOfTheUnseen
Deadline: Kamis 29 Oktober 2026 15:00 WIB
Form: https://forms.gle/iubikzSdexns1pb69
Catatan: caption wajib verbatim termasuk emoji
"""


class TestParse:
    def test_detects_blast(self):
        assert looks_like_blast(DEMO)
        assert not looks_like_blast("selamat pagi semua")

    def test_task_id_from_form_link(self, rulebase):
        """Form link is unique per task — strongest signal after an explicit id."""
        parsed = parse_blast_message(DEMO, rulebase)
        assert parsed.task_id == "PP-03"

    def test_explicit_task_id_wins(self, rulebase):
        text = "BLAST TUGAS PP-05\nDeadline: 27 Oktober 2026 12:00"
        assert parse_blast_message(text, rulebase).task_id == "PP-05"

    def test_caption_extracted(self, rulebase):
        parsed = parse_blast_message(DEMO, rulebase)
        assert parsed.caption_referensi.startswith("Pusing dengan project akhir")
        assert "FREE diskusi" in parsed.caption_referensi

    def test_tags_and_hashtags(self, rulebase):
        parsed = parse_blast_message(DEMO, rulebase)
        assert "@horizon_umn" in parsed.tags
        assert "#PPHorizon" in parsed.hashtags
        assert "#CaseOfTheUnseen" in parsed.hashtags

    def test_deadline(self, rulebase):
        parsed = parse_blast_message(DEMO, rulebase)
        assert parsed.deadline == datetime(2026, 10, 29, 15, 0)

    def test_drive_and_form(self, rulebase):
        parsed = parse_blast_message(DEMO, rulebase)
        assert "drive.google.com" in parsed.drive_folder
        assert "forms.gle" in parsed.form_link

    def test_buzzer_detected(self, rulebase):
        text = "BLAST TUGAS BUZZER IG 09 Oktober\nDeadline: Sabtu 10 Oktober 2026 18:00"
        parsed = parse_blast_message(text, rulebase)
        assert parsed.task_type == "BUZZER"

    def test_unknown_task_keeps_none(self, rulebase):
        text = "BLAST TUGAS acara rapat divisi\nDeadline: 1 Desember 2026 10:00"
        parsed = parse_blast_message(text, rulebase)
        assert parsed.task_id is None

    def test_confidence_rewards_completeness(self, rulebase):
        rich = parse_blast_message(DEMO, rulebase)
        poor = parse_blast_message("blast tugas", rulebase)
        assert rich.confidence > poor.confidence

    def test_guardrail_flags_wrong_weekday(self, rulebase):
        """27 Oct 2026 is a Tuesday, not Friday."""
        text = "BLAST TUGAS PP-05\nDeadline: Jumat 27 Oktober 2026 10:00 WIB"
        parsed = parse_blast_message(text, rulebase)
        assert any("nama hari" in note for note in parsed.notes)
        assert parsed.confidence < 0.75

    def test_guardrail_flags_deadline_before_blast(self, rulebase):
        text = (
            "BLAST TUGAS PP-05 30 Oktober 2026\n"
            "Deadline: 29 Oktober 2026 10:00 WIB"
        )
        parsed = parse_blast_message(text, rulebase)
        assert any("mendahului" in note for note in parsed.notes)

    def test_clean_blast_has_no_guardrail_notes(self, rulebase):
        parsed = parse_blast_message(DEMO, rulebase)
        assert not [note for note in parsed.notes if note.startswith("⚠️")]
        # The client's own note is kept verbatim for the koor to read.
        assert any("verbatim" in note for note in parsed.notes)


class TestStore:
    def test_roundtrip(self, tmp_path, rulebase):
        path = tmp_path / "blast_context.json"
        store = BlastContextStore(path)
        parsed = parse_blast_message(DEMO, rulebase)
        store.upsert(parsed.to_context(message_id="999"))

        reloaded = BlastContextStore(path).get("PP-03")
        assert reloaded is not None
        assert reloaded.blast_message_id == "999"
        assert reloaded.deadline == datetime(2026, 10, 29, 15, 0)
        assert reloaded.caption_referensi.startswith("Pusing")

    def test_remove(self, tmp_path, rulebase):
        store = BlastContextStore(tmp_path / "ctx.json")
        store.upsert(parse_blast_message(DEMO, rulebase).to_context())
        assert store.remove("PP-03")
        assert store.get("PP-03") is None

    def test_summary(self, tmp_path, rulebase):
        store = BlastContextStore(tmp_path / "ctx.json")
        store.upsert(parse_blast_message(DEMO, rulebase).to_context())
        assert store.summary()["count"] == 1


class TestProcessedCache:
    def test_persist(self, tmp_path):
        path = tmp_path / "processed.json"
        cache = ProcessedMessageCache(path)
        cache.add("12345")
        assert "12345" in ProcessedMessageCache(path)
        assert len(ProcessedMessageCache(path)) == 1
