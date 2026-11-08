"""Unit tests for the parsing helpers — the root cause of problem #1 lives here."""

from __future__ import annotations

from datetime import datetime

import pytest

from src.utils import (
    adapt_hashtags_for_division,
    caption_similarity,
    day_name_matches,
    extract_drive_file_id,
    extract_hashtags,
    extract_mentions,
    format_rupiah,
    get_division_hashtag_prefix,
    is_division_placeholder_hashtag,
    is_emoji_only,
    match_hashtag,
    name_similarity,
    normalize_handle,
    normalize_sheet_name,
    parse_dt,
    parse_date_range,
    resolve_division_key,
    sheet_similarity,
    split_links,
    word_count,
)


class TestSplitLinks:
    def test_single_link(self):
        cell = "https://drive.google.com/open?id=ABC1234567890123456789012"
        assert len(split_links(cell)) == 1

    def test_comma_separated_multi_upload(self):
        """Google Forms joins multiple uploads with ', ' — 74-81% of PP rows."""
        cell = (
            "https://drive.google.com/open?id=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA, "
            "https://drive.google.com/open?id=BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB, "
            "https://drive.google.com/file/d/CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC/view"
        )
        links = split_links(cell)
        assert len(links) == 3
        assert all(link.startswith("https://") for link in links)
        # The naive parser would return one long, broken URL.
        assert not any(link.endswith(",") for link in links)

    def test_newline_and_semicolon_separators(self):
        cell = (
            "https://drive.google.com/open?id=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n"
            "https://drive.google.com/open?id=BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB;"
            "https://drive.google.com/open?id=CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
        )
        assert len(split_links(cell)) == 3

    def test_bare_file_ids(self):
        cell = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA, BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
        assert len(split_links(cell)) == 2

    def test_duplicates_removed(self):
        cell = (
            "https://drive.google.com/open?id=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA, "
            "https://drive.google.com/open?id=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        )
        assert len(split_links(cell)) == 1

    @pytest.mark.parametrize("value", [None, "", "   ", "tidak ada link"])
    def test_empty_inputs(self, value):
        assert split_links(value) == []

    def test_trailing_punctuation_stripped(self):
        links = split_links("https://drive.google.com/open?id=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA).")
        assert links[0].endswith("AAAAAAAAA")


class TestDriveFileId:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://drive.google.com/open?id=ABCdef123456789012345678901", "ABCdef123456789012345678901"),
            ("https://drive.google.com/file/d/XYZ789012345678901234567890/view?usp=sharing", "XYZ789012345678901234567890"),
            ("https://drive.google.com/open?id=AAA111222333444555666777888&authuser=0", "AAA111222333444555666777888"),
            ("https://docs.google.com/document/d/DOC1234567890123456789012/edit", "DOC1234567890123456789012"),
        ],
    )
    def test_variants(self, url, expected):
        assert extract_drive_file_id(url) == expected

    def test_not_a_drive_url(self):
        assert extract_drive_file_id("https://example.com/foo") is None


class TestParseDate:
    def test_indonesian_with_time(self):
        assert parse_dt("Kamis, 29 Oktober 2026 15:00 WIB") == datetime(2026, 10, 29, 15, 0)

    def test_abbreviated_month(self):
        assert parse_dt("29 Okt 2026 15:00") == datetime(2026, 10, 29, 15, 0)

    def test_date_only_defaults_to_end_of_day(self):
        assert parse_dt("Rabu, 28 Oktober 2026") == datetime(2026, 10, 28, 23, 59)

    def test_date_only_start_of_day(self):
        assert parse_dt("Rabu, 28 Oktober 2026", default_time=(0, 0)) == datetime(2026, 10, 28, 0, 0)

    def test_iso_timestamp_from_google_forms(self):
        assert parse_dt("2026-10-29 14:23:11") == datetime(2026, 10, 29, 14, 23, 11)

    def test_seconds_preserved(self):
        assert parse_dt("Kamis, 29 Oktober 2026 15:04:09") == datetime(2026, 10, 29, 15, 4, 9)

    def test_invalid_date_returns_none(self):
        assert parse_dt("2026-13-45") is None
        assert parse_dt("besok pagi") is None

    def test_date_range(self):
        start, end = parse_date_range("Rabu, 28 Oktober 2026 - Senin, 2 November 2026")
        assert start.date().isoformat() == "2026-10-28"
        assert end.date().isoformat() == "2026-11-02"

    def test_day_name_matches(self):
        # 27 Oct 2026 is a Tuesday, not Friday.
        assert day_name_matches(datetime(2026, 10, 27), "Jumat, 27 Oktober 2026") is False
        assert day_name_matches(datetime(2026, 10, 29), "Kamis, 29 Oktober 2026") is True
        assert day_name_matches(datetime(2026, 10, 29), "29 Oktober 2026") is None


class TestSheetSimilarity:
    def test_exact_after_normalization(self):
        assert sheet_similarity("pp feed 26 okt", "PP Feed 26 Oktober") == 1.0

    def test_different_day_is_a_hard_conflict(self):
        """'PP 27 Oktober' must never resolve to 'PP 07 Oktober'."""
        assert sheet_similarity("PP 27 Oktober", "PP 07 Oktober") < 0.2

    def test_different_family_is_a_hard_conflict(self):
        assert sheet_similarity("PP 26 Oktober", "Buzzer vote 26 Oktober") < 0.2

    def test_same_day_different_variant_is_ambiguous(self):
        feed = sheet_similarity("PP 26 Oktober", "PP Feed 26 Oktober")
        bundle = sheet_similarity("PP 26 Oktober", "PP Bundle 26 Oktober")
        assert feed == bundle

    def test_partial_name_matches_variant(self):
        assert sheet_similarity("PP Feed", "PP Feed 26 Oktober") > 0.8

    def test_normalize_expands_month_and_drops_year(self):
        assert normalize_sheet_name("PP Feed 26 Okt 2026") == "pp feed 26 oktober"


class TestTextHelpers:
    def test_caption_verbatim_match(self):
        text = "Pusing dengan project akhir/TA codingmu? Coba jasa kami yuk!"
        assert caption_similarity(text, text) == 1.0

    def test_caption_whitespace_insensitive(self):
        assert caption_similarity("a  b\nc", "a b c") > 0.99

    def test_caption_edit_detected(self):
        original = "Caption resmi dari klien yang wajib dipakai apa adanya"
        edited = "Caption resmi dari klien yang wajib dipakai apa adanya (diedit)"
        assert caption_similarity(original, edited) < 0.97

    def test_emoji_only_comment(self):
        assert is_emoji_only("🔥🔥🔥") is True
        assert is_emoji_only("keren banget") is False
        assert is_emoji_only("🔥 mantap 🔥") is False

    def test_word_count(self):
        assert word_count("mantap banget sih ini") == 4

    def test_extract_hashtags_unique(self):
        tags = extract_hashtags("#PPHorizon #PPHorizon #jasacoding")
        assert tags == ["#PPHorizon", "#jasacoding"]

    def test_extract_mentions(self):
        assert extract_mentions("tag @horizon_umn dan @insantransport ya") == [
            "@horizon_umn",
            "@insantransport",
        ]

    def test_normalize_handle(self):
        assert normalize_handle("@Horizon_UMN/") == "horizon_umn"

    def test_name_similarity_typo(self):
        assert name_similarity("Wildan Alhaqq", "Wildan Al Haqq") > 0.9
        assert name_similarity("Wildan Alhaqq", "Budi Santoso") < 0.5

    def test_format_rupiah(self):
        assert format_rupiah(15000) == "Rp 15.000"
        assert format_rupiah(0) == "Rp 0"

    def test_resolve_division_key(self):
        assert resolve_division_key("Marple (Dekorasi)") == "marple"
        assert resolve_division_key("Maple") == "marple"
        assert resolve_division_key("Dekorasi") == "marple"
        assert resolve_division_key("Spade (Fresh Money)") == "spade"
        assert resolve_division_key("Conan (Acara)") == "conan"
        assert resolve_division_key("Kindaichi") == "kindaichi"
        assert resolve_division_key("") is None
        assert resolve_division_key(None) is None

    def test_division_placeholder_detection(self):
        assert is_division_placeholder_hashtag("#PPHorizon_divisi") is True
        assert is_division_placeholder_hashtag("#PPHorizon_division") is True
        assert is_division_placeholder_hashtag("#PPHorizon_<divisi>") is True
        assert is_division_placeholder_hashtag("#PPHorizon") is False
        assert is_division_placeholder_hashtag("#CaseOfTheUnseen") is False
        assert get_division_hashtag_prefix("#PPHorizon_divisi") == "#PPHorizon"

    def test_adapt_hashtags_for_division(self):
        raw = ["#PPHorizon", "#PPHorizon_divisi", "#CaseOfTheUnseen"]
        adapted = adapt_hashtags_for_division(raw, "Marple (Dekorasi)")
        assert adapted == ["#PPHorizon", "#PPHorizon_Marple", "#CaseOfTheUnseen"]

    def test_match_hashtag_exact_and_division(self):
        present = {"#pphorizon", "#pphorizon_maple", "#caseoftheunseen"}
        assert match_hashtag("#PPHorizon", present) is True
        assert match_hashtag("#CaseOfTheUnseen", present) is True
        # #PPHorizon_divisi matches member with division "Marple (Dekorasi)" posting #pphorizon_maple
        assert match_hashtag("#PPHorizon_divisi", present, "Marple (Dekorasi)") is True
        assert match_hashtag("#PPHorizon_divisi", present, "Maple") is True
        # Fails for different division
        assert match_hashtag("#PPHorizon_divisi", present, "Spade (Fresh Money)") is False
        # Matches literal #pphorizon_divisi if posted
        assert match_hashtag("#PPHorizon_divisi", {"#pphorizon_divisi"}, "Spade") is True
        # Unrelated hashtag fails
        assert match_hashtag("#jasacoding", present) is False
