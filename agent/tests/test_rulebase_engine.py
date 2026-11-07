"""Rulebase engine tests — the 17 checks plus Angel Card boundaries."""

from __future__ import annotations

from datetime import datetime

import pytest

from src.data_layer import AngelEntry
from src.models import BlastContext, CheckStatus, ResultStatus, Submission, VisionFacts
from src.validator import TaskEngineAdapter as RulebaseEngine
from src.agent.evaluator import lint_rulebase


def make_submission(**overrides) -> Submission:
    defaults = dict(
        row_index=2,
        nama="Budi Santoso",
        nim="000001234567",
        divisi="Conan (Acara)",
        ig_username="budi_santoso",
        links=["https://drive.google.com/open?id=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"],
        file_ids=["AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"],
        timestamp=datetime(2026, 10, 28, 10, 0),
        sheet_name="PP Feed 26 Oktober",
        task_id="PP-03",
    )
    defaults.update(overrides)
    return Submission(**defaults)


def make_facts(**overrides) -> VisionFacts:
    defaults = dict(
        image_count=2,
        caption_text="",
        hashtags=[],
        mentions=[],
        followers=850,
        is_private=False,
        posted_at=datetime(2026, 10, 28, 10, 0),
        confidence=0.9,
    )
    defaults.update(overrides)
    return VisionFacts(**defaults)


class TestLint:
    def test_detects_deadline_before_jadwal(self, rulebase):
        issues = lint_rulebase(rulebase)
        codes = {(issue.code, issue.task_id) for issue in issues}
        assert ("DEADLINE_BEFORE_JADWAL", "PP-01") in codes

    def test_detects_sheet_map_conflict(self, rulebase):
        issues = lint_rulebase(rulebase)
        assert any(issue.code == "SHEET_MAP_CONFLICT" for issue in issues)

    def test_does_not_mutate_rulebase(self, rulebase):
        before = str(rulebase)
        lint_rulebase(rulebase)
        assert str(rulebase) == before


class TestMerge:
    def test_rulebase_verbatim_caption(self, engine):
        merged = engine.merge("PP-03")
        assert "Pusing dengan project akhir" in merged.caption_reference
        assert merged.is_feed and not merged.is_story

    def test_tags_and_hashtags_merged_across_platforms(self, engine):
        merged = engine.merge("PP-01")
        assert {tag.lower() for tag in merged.tags} >= {"@insantransport", "@horizon.umn"}
        assert len(merged.hashtags) >= 5

    def test_extra_caption_hashtags_required(self, engine):
        merged = engine.merge("PP-03")
        assert "#jasacoding" in [tag.lower() for tag in merged.hashtags]

    def test_blast_context_overrides_deadline(self, rulebase):
        blast = BlastContext(
            task_id="PP-03",
            blast_message_id="123",
            blast_date=datetime(2026, 10, 27),
            deadline=datetime(2026, 10, 30, 10, 0),
            tags=["@klien_baru"],
            hashtags=["#Tambahan"],
        )
        engine = RulebaseEngine(rulebase, {"PP-03": blast})
        merged = engine.merge("PP-03")
        assert merged.deadline == datetime(2026, 10, 30, 10, 0)
        assert "@klien_baru" in merged.tags
        assert "#Tambahan" in merged.hashtags
        assert merged.blast_date == datetime(2026, 10, 27)

    def test_blast_caption_used_when_rulebase_has_none(self, rulebase):
        blast = BlastContext(
            task_id="PP-05",
            blast_date=datetime(2026, 10, 26),
            caption_referensi="Caption dari blast",
        )
        engine = RulebaseEngine(rulebase, {"PP-05": blast})
        assert engine.merge("PP-05").caption_reference == "Caption dari blast"

    def test_inconsistent_dates_flagged_in_warnings(self, engine):
        merged = engine.merge("PP-01")
        assert not merged.timing_consistent
        assert any("tidak konsisten" in warning for warning in merged.warnings)


class TestPPChecks:
    def test_c1_pass(self, engine):
        result = engine._check_c1(make_submission(), engine.merge("PP-03"), make_facts(), None)
        assert result.status == CheckStatus.PASS

    def test_c1_low_followers(self, engine):
        result = engine._check_c1(
            make_submission(), engine.merge("PP-03"), make_facts(followers=120), None
        )
        assert result.status == CheckStatus.FAIL
        assert result.denda == 5000

    def test_c1_private_account(self, engine):
        result = engine._check_c1(
            make_submission(), engine.merge("PP-03"), make_facts(is_private=True), None
        )
        assert result.status == CheckStatus.FAIL

    def test_c1_inconclusive_without_data(self, engine):
        result = engine._check_c1(
            make_submission(),
            engine.merge("PP-03"),
            make_facts(followers=None, is_private=None),
            None,
        )
        assert result.status == CheckStatus.INCONCLUSIVE

    def test_c1_account_swap_is_flag_not_fine(self, engine):
        result = engine._check_c1(
            make_submission(),
            engine.merge("PP-03"),
            make_facts(ig_username_seen="akun_lain"),
            None,
        )
        assert result.status == CheckStatus.FLAG
        assert result.denda == 0

    def test_c2_date_only_schedule_accepts_morning_post(self, engine):
        """'Rabu, 28 Oktober 2026' means the whole day, not 23:59."""
        result = engine._check_c2(
            make_submission(),
            engine.merge("PP-03"),
            make_facts(posted_at=datetime(2026, 10, 28, 9, 0)),
            None,
        )
        assert result.status == CheckStatus.PASS

    def test_c2_late_post(self, engine):
        result = engine._check_c2(
            make_submission(),
            engine.merge("PP-03"),
            make_facts(posted_at=datetime(2026, 10, 29, 20, 0)),
            None,
        )
        assert result.status == CheckStatus.FAIL
        assert result.denda == 20000

    def test_c2_too_early(self, engine):
        result = engine._check_c2(
            make_submission(),
            engine.merge("PP-03"),
            make_facts(posted_at=datetime(2026, 10, 27, 10, 0)),
            None,
        )
        assert result.status == CheckStatus.FAIL

    def test_c2_inconsistent_rulebase_is_flag_not_fine(self, engine):
        """PP-01 has deadline before jadwal — must not mass-fine panitia."""
        result = engine._check_c2(
            make_submission(task_id="PP-01", sheet_name="PP 29 Oktober"),
            engine.merge("PP-01"),
            make_facts(),
            None,
        )
        assert result.status == CheckStatus.FLAG
        assert result.denda == 0

    def test_c4_verbatim_pass(self, engine):
        merged = engine.merge("PP-03")
        result = engine._check_c4(
            make_submission(), merged, make_facts(caption_text=merged.caption_reference), None
        )
        assert result.status == CheckStatus.PASS

    def test_c4_edited_caption_fails(self, engine):
        merged = engine.merge("PP-03")
        result = engine._check_c4(
            make_submission(),
            merged,
            make_facts(caption_text=merged.caption_reference[:40] + " (diedit)"),
            None,
        )
        assert result.status == CheckStatus.FAIL

    def test_c4_inconclusive_without_reference(self, engine):
        result = engine._check_c4(
            make_submission(task_id="PP-05"), engine.merge("PP-05"), make_facts(caption_text="apa saja"), None
        )
        assert result.status == CheckStatus.INCONCLUSIVE

    def test_c5_missing_tag(self, engine):
        merged = engine.merge("PP-03")
        result = engine._check_c5(
            make_submission(), merged, make_facts(mentions=[], caption_text="caption"), None
        )
        assert result.status == CheckStatus.FAIL
        assert "@horizon_umn" in result.detail

    def test_c5_dot_underscore_equivalent(self, engine):
        """@horizon.umn and @horizon_umn are the same account."""
        merged = engine.merge("PP-01")
        result = engine._check_c5(
            make_submission(task_id="PP-01"),
            merged,
            make_facts(mentions=["@insantransport", "@horizon_umn"], caption_text="x"),
            None,
        )
        assert result.status == CheckStatus.PASS

    def test_c6_missing_hashtag(self, engine):
        merged = engine.merge("PP-03")
        result = engine._check_c6(
            make_submission(),
            merged,
            make_facts(hashtags=merged.hashtags[:-1]),
            None,
        )
        assert result.status == CheckStatus.FAIL
        assert result.denda == 5000

    def test_c6_all_present(self, engine):
        merged = engine.merge("PP-03")
        result = engine._check_c6(
            make_submission(), merged, make_facts(hashtags=list(merged.hashtags)), None
        )
        assert result.status == CheckStatus.PASS

    def test_c7_story_expired(self, engine):
        result = engine._check_c7(
            make_submission(task_id="PP-07", sheet_name="PP Story 09 Oktober"),
            engine.merge("PP-07"),
            make_facts(story_live=False),
            None,
        )
        assert result.status == CheckStatus.FAIL

    def test_c7_uploading_screenshot(self, engine):
        result = engine._check_c7(
            make_submission(task_id="PP-07"),
            engine.merge("PP-07"),
            make_facts(story_live=None, story_uploading_text=True),
            None,
        )
        assert result.status == CheckStatus.FAIL
        assert "belum selesai" in result.detail

    def test_c7_inconclusive_is_not_a_fine(self, engine):
        result = engine._check_c7(
            make_submission(task_id="PP-07"),
            engine.merge("PP-07"),
            make_facts(story_live=None),
            None,
        )
        assert result.status == CheckStatus.INCONCLUSIVE
        assert result.denda == 0

    def test_c8_deleted_feed(self, engine):
        facts = make_facts()
        facts.flag("post_missing")
        result = engine._check_c8(make_submission(), engine.merge("PP-03"), facts, None)
        assert result.status == CheckStatus.FAIL

    def test_c10_no_evidence(self, engine):
        submission = make_submission(links=[], file_ids=[])
        result = engine._check_c10(submission, engine.merge("PP-03"), make_facts(), None)
        assert result.status == CheckStatus.FAIL
        assert result.denda == 20000

    def test_c10_late_proof(self, engine):
        submission = make_submission(timestamp=datetime(2026, 10, 30, 9, 0))
        result = engine._check_c10(submission, engine.merge("PP-03"), make_facts(), None)
        assert result.status == CheckStatus.FAIL

    def test_c10_inconsistent_rulebase_is_flag(self, engine):
        result = engine._check_c10(
            make_submission(task_id="PP-01"), engine.merge("PP-01"), make_facts(), None
        )
        assert result.status == CheckStatus.FLAG
        assert result.denda == 0


class TestBuzzerChecks:
    def test_b1_dual_platform_missing_tiktok(self, engine):
        result = engine._check_b1(
            make_submission(task_id="BZ-01"),
            engine.merge("BZ-01"),
            make_facts(like_platforms=["instagram"]),
            None,
        )
        assert result.status == CheckStatus.FAIL
        assert "tiktok" in result.detail

    def test_b1_both_platforms(self, engine):
        result = engine._check_b1(
            make_submission(task_id="BZ-01"),
            engine.merge("BZ-01"),
            make_facts(like_platforms=["instagram", "tiktok"]),
            None,
        )
        assert result.status == CheckStatus.PASS

    def test_b2_emoji_only(self, engine):
        result = engine._check_b2(
            make_submission(task_id="BZ-06"),
            engine.merge("BZ-06"),
            make_facts(comment_text="🔥🔥🔥"),
            None,
        )
        assert result.status == CheckStatus.FAIL

    def test_b2_short_comment(self, engine):
        result = engine._check_b2(
            make_submission(task_id="BZ-06"),
            engine.merge("BZ-06"),
            make_facts(comment_text="mantap"),
            None,
        )
        assert result.status == CheckStatus.FAIL

    def test_b2_natural_comment(self, engine):
        result = engine._check_b2(
            make_submission(task_id="BZ-06"),
            engine.merge("BZ-06"),
            make_facts(comment_text="keren banget postingannya bermanfaat buat mahasiswa"),
            None,
        )
        assert result.status == CheckStatus.PASS

    def test_b4_rating_below_five(self, engine):
        result = engine._check_b4(
            make_submission(task_id="BZ-04"),
            engine.merge("BZ-04"),
            make_facts(rating=4),
            None,
        )
        assert result.status == CheckStatus.FAIL

    def test_b5_wrong_campaign(self, engine):
        result = engine._check_b5(
            make_submission(task_id="BZ-03"),
            engine.merge("BZ-03"),
            make_facts(vote_target="https://example.com/lain"),
            None,
        )
        assert result.status == CheckStatus.FAIL

    def test_b5_correct_campaign(self, engine):
        result = engine._check_b5(
            make_submission(task_id="BZ-03"),
            engine.merge("BZ-03"),
            make_facts(vote_target="https://www.saraswati.global/special-projects/climate-besties-2026"),
            None,
        )
        assert result.status == CheckStatus.PASS


class TestAngelCard:
    @staticmethod
    def registry(date: datetime) -> list[AngelEntry]:
        return [AngelEntry(nama="Budi Santoso", divisi="Conan (Acara)", status="ACTIVE", tanggal_beli=date)]

    def test_bought_before_blast_is_exempt(self, engine):
        exempt, note = engine.angel_exemption(
            "Budi Santoso", "PP-03", self.registry(datetime(2026, 10, 1)), datetime(2026, 10, 28)
        )
        assert exempt
        assert "dibebaskan" in note

    def test_bought_on_blast_date_is_exempt(self, engine):
        """Boundary: beli <= blast counts as exempt."""
        exempt, _ = engine.angel_exemption(
            "Budi Santoso", "PP-03", self.registry(datetime(2026, 10, 28)), datetime(2026, 10, 28)
        )
        assert exempt

    def test_bought_after_blast_is_not_exempt(self, engine):
        exempt, note = engine.angel_exemption(
            "Budi Santoso", "PP-03", self.registry(datetime(2026, 10, 29)), datetime(2026, 10, 28)
        )
        assert not exempt
        assert "setelah blast" in note

    def test_inactive_card_does_not_exempt(self, engine):
        registry = [
            AngelEntry(nama="Budi Santoso", status="INACTIVE", tanggal_beli=datetime(2026, 10, 1))
        ]
        exempt, _ = engine.angel_exemption(
            "Budi Santoso", "PP-03", registry, datetime(2026, 10, 28)
        )
        assert not exempt

    def test_unknown_name_does_not_exempt(self, engine):
        exempt, note = engine.angel_exemption(
            "Orang Lain", "PP-03", self.registry(datetime(2026, 10, 1)), datetime(2026, 10, 28)
        )
        assert not exempt
        assert note == ""


class TestEvaluate:
    def test_status_flagged_when_any_fail(self, engine):
        submission = make_submission()
        facts = make_facts(followers=100, hashtags=[])
        result = engine.evaluate(submission, "PP-03", facts)
        assert result.status == ResultStatus.FLAGGED
        assert result.total_denda > 0
        assert "C1" in result.violation_ids

    def test_status_verified_when_all_pass(self, engine):
        merged = engine.merge("PP-03")
        submission = make_submission()
        facts = make_facts(
            caption_text=merged.caption_reference,
            hashtags=list(merged.hashtags),
            mentions=list(merged.tags),
            carousel_slides=["1", "2", "3", "4"],
        )
        result = engine.evaluate(submission, "PP-03", facts)
        assert result.status == ResultStatus.VERIFIED, result.violation_summary

    def test_applicable_checks_pp_feed(self, engine):
        checks = engine.applicable_checks("PP-03")
        assert "C4" in checks and "C7" not in checks  # feed only
        assert checks[-1] == "C10"

    def test_applicable_checks_pp_story(self, engine):
        checks = engine.applicable_checks("PP-07")
        assert "C7" in checks
        assert "C3" not in checks  # story only, no carousel

    def test_applicable_checks_buzzer(self, engine):
        assert engine.applicable_checks("BZ-06") == ["B1", "B2", "B7"]
        assert engine.applicable_checks("BZ-04") == ["B3", "B4", "B7"]
