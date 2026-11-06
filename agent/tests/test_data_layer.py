"""Data layer tests: sheet resolution, task mapping, submission parsing."""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from src.data_layer import DataLayer, dedupe_submissions, map_headers
from src.models import Submission
from src.sheets_client import GoogleSheetsSource, SheetAccessError, SheetRateLimitError


class TestHeaderMapping:
    def test_google_forms_headers(self):
        headers = [
            "Timestamp",
            "Nama Lengkap",
            "NIM",
            "Divisi",
            "Username Instagram",
            "Link Bukti (Google Drive)",
        ]
        mapping = map_headers(headers)
        assert mapping["timestamp"] == 0
        assert mapping["nama"] == 1
        assert mapping["nim"] == 2
        assert mapping["divisi"] == 3
        assert mapping["ig"] == 4
        assert mapping["link"] == 5

    def test_variant_headers(self):
        mapping = map_headers(["Waktu Pengisian", "Name", "NRP", "Instagram", "Bukti"])
        assert mapping["nama"] == 1
        assert mapping["nim"] == 2
        assert mapping["ig"] == 3
        assert mapping["link"] == 4


@pytest.mark.usefixtures("dataset_available")
class TestSheetResolution:
    def test_exact(self, data: DataLayer):
        resolution = data.resolve_sheet("PP 29 Oktober")
        assert resolution.resolved == "PP 29 Oktober"
        assert resolution.exact

    def test_abbreviated_month(self, data: DataLayer):
        assert data.resolve_sheet("pp feed 26 okt").resolved == "PP Feed 26 Oktober"

    def test_ambiguous_returns_both_variants(self, data: DataLayer):
        resolution = data.resolve_sheet("PP 26 Oktober")
        assert resolution.ambiguous
        assert set(resolution.options()) == {"PP Feed 26 Oktober", "PP Bundle 26 Oktober"}

    def test_wrong_day_is_not_found(self, data: DataLayer):
        """The workflow demands a refusal here, not a guess."""
        resolution = data.resolve_sheet("PP 27 Oktober")
        assert resolution.not_found

    def test_unknown_sheet(self, data: DataLayer):
        assert data.resolve_sheet("PP 15 November").not_found


@pytest.mark.usefixtures("dataset_available")
class TestTaskMapping:
    def test_rulebase_sheet_wins(self, data: DataLayer):
        task = data.resolve_task("PP Feed 26 Oktober")
        assert task.task_id == "PP-03"
        assert task.source == "rulebase"

    def test_config_fallback(self, data: DataLayer):
        task = data.resolve_task("PP 29 Oktober")
        assert task.task_id == "PP-01"
        assert task.source == "config"

    def test_conflict_is_reported_not_hidden(self, data: DataLayer):
        """rulebase says PP-01 owns 'PP - 5 Oktober', config says PP-06."""
        task = data.resolve_task("PP - 5 Oktober")
        assert task.task_id == "PP-01"
        assert any("PP-06" in warning for warning in task.warnings)

    def test_unmapped_sheet_warns(self, data: DataLayer):
        task = data.resolve_task("PP 07 Oktober")
        assert task.task_id is None
        assert task.warnings


@pytest.mark.usefixtures("dataset_available")
class TestSubmissions:
    def test_multi_link_rows_are_split(self, data: DataLayer):
        submissions = data.read_submissions("PP 29 Oktober")
        assert len(submissions) == 42
        multi = [row for row in submissions if row.multi_link]
        assert len(multi) == 34  # matches the planning document statistic
        assert all(len(row.file_ids) == len(row.links) for row in multi)

    def test_timestamp_parsed(self, data: DataLayer):
        submissions = data.read_submissions("PP 29 Oktober")
        assert all(row.timestamp is not None for row in submissions)

    def test_angel_registry_loaded(self, data: DataLayer):
        registry = data.load_angel_registry()
        assert len(registry) == 17
        assert all(entry.tanggal_beli is not None for entry in registry)

    def test_roster_loaded(self, data: DataLayer):
        assert len(data.load_roster()) == 106

    def test_bukti_tambahan_loaded(self, data: DataLayer):
        rows = data.load_bukti_tambahan()
        assert rows
        assert all("deskripsi" in row for row in rows)


@pytest.fixture
def live_layer():
    service = Mock()
    sheets = service.spreadsheets.return_value
    sheets.get.return_value.execute.return_value = {
        "sheets": [{"properties": {"title": "PP 29 Oktober"}}]
    }
    sheets.values.return_value.get.return_value.execute.return_value = {
        "values": [["Nama Lengkap", "NIM"], ["Budi", "001"]]
    }
    source = GoogleSheetsSource("x" * 30, service=service, max_retries=0)
    return DataLayer(source=source, rulebase={}), sheets


def sheets_error(status, reason=None):
    from googleapiclient.errors import HttpError
    from httplib2 import Response

    return HttpError(
        Response({"status": status}),
        json.dumps({"error": {"message": "Mock Sheets error", "errors": [
            {"reason": reason or "forbidden"}
        ]}}).encode(),
    )


class TestLiveDataLayer:
    def test_parsed_submissions_bypass_cache_and_reflect_edits(self, live_layer):
        layer, sheets = live_layer
        values = sheets.values.return_value.get.return_value.execute
        layer._cache["PP 29 Oktober"] = [
            Submission(row_index=2, nama="stale parsed value", sheet_name="PP 29 Oktober")
        ]
        original = layer.read_submissions("PP 29 Oktober")
        assert original[0].nama == "Budi"
        original[0].nama = "caller mutation"
        values.return_value = {
            "values": [["Nama Lengkap", "NIM"], ["Sari", "002"], ["Dewi", "003"]]
        }
        fresh = layer.read_submissions("PP 29 Oktober")
        assert [submission.nama for submission in fresh] == ["Sari", "Dewi"]
        assert values.call_count == 2
        assert layer._cache["PP 29 Oktober"][0].nama == "stale parsed value"

    def test_live_reads_do_not_populate_parsed_cache(self, live_layer):
        layer, _ = live_layer
        layer.read_submissions("PP 29 Oktober")
        assert layer._cache == {}

    def test_quota_reparses_isolated_snapshot_then_recovers(self, live_layer):
        layer, sheets = live_layer
        values = sheets.values.return_value.get.return_value.execute
        first = layer.read_submissions("PP 29 Oktober")
        first[0].nama = "caller mutation"
        values.side_effect = sheets_error(429)
        fallback = layer.read_submissions("PP 29 Oktober")
        assert fallback[0].nama == "Budi"
        assert len(layer.warnings) == 1
        assert "rows:PP 29 Oktober" in layer.warnings[0]
        assert "snapshot read at" in layer.warnings[0]
        layer.warnings.clear()
        assert layer.warnings == layer.source.warnings
        assert len(layer.warnings) == 1
        fallback[0].nama = "fallback mutation"
        assert layer.read_submissions("PP 29 Oktober")[0].nama == "Budi"
        values.side_effect = None
        values.return_value = {"values": [["Nama Lengkap"], ["Recovered"]]}
        assert layer.read_submissions("PP 29 Oktober")[0].nama == "Recovered"
        assert layer.warnings == []
        assert layer._cache == {}

    def test_permission_failure_not_hidden_by_parsed_cache(self, live_layer, monkeypatch):
        layer, sheets = live_layer
        monkeypatch.setattr(layer.source, "service_account_email", lambda: "mock@example.test")
        layer.read_submissions("PP 29 Oktober")
        sheets.values.return_value.get.return_value.execute.side_effect = sheets_error(403)
        with pytest.raises(SheetAccessError):
            layer.read_submissions("PP 29 Oktober")
        assert layer.warnings == []

    def test_cache_false_does_not_fallback_to_parsed_submissions(self, live_layer):
        layer, sheets = live_layer
        layer.source._cache_enabled = False
        layer.read_submissions("PP 29 Oktober")
        sheets.values.return_value.get.return_value.execute.side_effect = sheets_error(429)
        with pytest.raises(SheetRateLimitError):
            layer.read_submissions("PP 29 Oktober")
        assert layer._cache == {}
        assert layer.warnings == []

    def test_sheet_resolution_sees_added_and_renamed_tabs(self, live_layer):
        layer, sheets = live_layer
        assert layer.resolve_sheet("PP 29 Oktober").resolved == "PP 29 Oktober"
        sheets.get.return_value.execute.return_value = {
            "sheets": [{"properties": {"title": "PP 30 Oktober"}}]
        }
        assert layer.resolve_sheet("PP 30 Oktober").resolved == "PP 30 Oktober"
        assert layer.sheet_names() == ["PP 30 Oktober"]
        assert sheets.get.return_value.execute.call_count == 3

    @pytest.mark.parametrize(
        "loader,title,headers,old_row,new_row,field,old_value,new_value",
        [
            (
                "load_roster", "Roster", ["Nama Lengkap", "NIM", "Divisi"],
                ["Budi", "001", "Conan"], ["Budi", "001", "Spade"],
                "divisi", "Conan", "Spade",
            ),
            (
                "load_angel_registry", "Angel Card Registry",
                ["Nama Lengkap", "Status Angel Card", "Tanggal Pembelian"],
                ["Budi", "ACTIVE", "2026-10-01"], ["Budi", "INACTIVE", "2026-10-02"],
                "status", "ACTIVE", "INACTIVE",
            ),
        ],
    )
    def test_registry_reads_are_fresh(
        self, live_layer, monkeypatch, loader, title, headers, old_row, new_row,
        field, old_value, new_value,
    ):
        layer, sheets = live_layer
        monkeypatch.setattr("src.data_layer.apply_angel_overrides", lambda entries: entries)
        sheets.get.return_value.execute.return_value = {
            "sheets": [{"properties": {"title": title}}]
        }
        values = sheets.values.return_value.get.return_value.execute
        values.return_value = {"values": [headers, old_row]}
        assert getattr(getattr(layer, loader)()[0], field) == old_value
        values.return_value = {"values": [headers, new_row]}
        assert getattr(getattr(layer, loader)()[0], field) == new_value
        assert values.call_count == 2
        assert sheets.get.return_value.execute.call_count == 2

    def test_registry_discovery_reads_names_once_per_lookup(self, live_layer):
        layer, sheets = live_layer
        sheets.get.return_value.execute.return_value = {
            "sheets": [{"properties": {"title": "Roster Panitia Extra"}}]
        }
        assert layer.find_sheet("Roster") == "Roster Panitia Extra"
        assert sheets.get.return_value.execute.call_count == 1
        sheets.get.return_value.execute.return_value = {
            "sheets": [{"properties": {"title": "Roster Renamed"}}]
        }
        assert layer.find_sheet("Roster") == "Roster Renamed"
        assert sheets.get.return_value.execute.call_count == 2

    def test_local_source_keeps_parsed_cache_and_force_reload(self):
        source = Mock()
        source.name = "xlsx"
        source.rows.return_value = [["Nama Lengkap"], ["Budi"]]
        layer = DataLayer(source=source, rulebase={})
        first = layer.read_submissions("A")
        source.rows.return_value = [["Nama Lengkap"], ["Sari"]]
        assert layer.read_submissions("A") is first
        assert source.rows.call_count == 1
        assert layer.read_submissions("A", force_reload=True)[0].nama == "Sari"
        assert source.rows.call_count == 2

    def test_sources_without_warning_api_return_empty_warnings(self):
        class Source:
            name = "xlsx"
            label = "offline"

        assert DataLayer(source=Source(), rulebase={}).warnings == []


class TestDedupe:
    @staticmethod
    def _sub(name: str, nim: str, stamp, row: int = 2) -> Submission:
        return Submission(
            row_index=row,
            nama=name,
            nim=nim,
            timestamp=stamp,
            sheet_name="PP 29 Oktober",
        )

    def test_latest_resubmit_wins(self):
        from datetime import datetime

        rows = [
            self._sub("Budi", "001", datetime(2026, 10, 29, 7, 0), 2),
            self._sub("Budi", "001", datetime(2026, 10, 29, 9, 0), 5),
            self._sub("Sari", "002", datetime(2026, 10, 29, 8, 0), 3),
        ]
        kept, notes = dedupe_submissions(rows)
        assert len(kept) == 2
        budi = next(row for row in kept if row.nama == "Budi")
        assert budi.timestamp.hour == 9
        assert notes and "submitted 2x" in notes[0]

    def test_spam_logged(self):
        from datetime import datetime

        rows = [
            self._sub("Budi", "001", datetime(2026, 10, 29, hour), hour)
            for hour in (7, 8, 9, 10, 11)
        ]
        kept, notes = dedupe_submissions(rows)
        assert len(kept) == 1
        assert "submitted 5x" in notes[0]
